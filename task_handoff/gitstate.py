"""Deterministic repository state collection, baselines, and change attribution.

Only read-only git commands are used here. The baseline lives inside the git
directory (``.git/task-handoff/``) so it never shows up as a working-tree change.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .redact import find_secrets

MAX_SCAN_BYTES = 512_000


class GitError(RuntimeError):
    pass




class GitMissing(GitError):
    pass


GIT_INSTALL_HINT = ("git is not available. On macOS run `xcode-select --install` (Apple's Command Line Tools, "
                    "which include git and python3); on Linux install git with your package manager.")
# macOS ships /usr/bin/git as a stub that fails like this until the Command Line Tools are installed.
XCRUN_MISSING = ("invalid active developer path", "xcrun: error", "No developer tools were found")


def git(root: Path, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            errors="replace",
            # Keep git strictly read-only: otherwise `git status` may rewrite .git/index to refresh stat data.
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except FileNotFoundError as exc:
        raise GitMissing(GIT_INSTALL_HINT) from exc
    if proc.returncode != 0 and any(marker in proc.stderr for marker in XCRUN_MISSING):
        raise GitMissing(GIT_INSTALL_HINT)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(path: str | Path) -> Path:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise GitError(f"Path does not exist: {path}")
    try:
        return Path(git(path, "rev-parse", "--show-toplevel").strip())
    except GitMissing:
        raise
    except GitError as exc:
        raise GitError(f"Not inside a git repository: {path}") from exc


def state_dir(root: Path) -> Path:
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").strip())
    return git_dir / "task-handoff"


def head(root: Path) -> str | None:
    out = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    return out or None


def dirty_files(root: Path) -> dict[str, str]:
    """Map of path -> porcelain status for every modified/staged/untracked file."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = out.split("\0")
    result: dict[str, str] = {}
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":  # rename/copy: next entry is the source path
            i += 1
        result[path] = status
    return result


def fingerprint(root: Path, path: str) -> str:
    full = root / path
    if not full.exists():
        return "<deleted>"
    if full.is_dir():  # e.g. nested repository shown as untracked dir
        return "<dir>"
    return hashlib.sha256(full.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- baseline


def record_baseline(root: Path, task: str, session_id: str = "") -> dict:
    baseline = {
        "task": task,
        "session_id": session_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "head": head(root),
        "dirty": {p: fingerprint(root, p) for p in dirty_files(root)},
    }
    directory = state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "baseline.json").write_text(json.dumps(baseline, indent=2))
    return baseline


def load_baseline(root: Path) -> dict | None:
    path = state_dir(root) / "baseline.json"
    return json.loads(path.read_text()) if path.is_file() else None


def state_fingerprint(root: Path) -> str:
    """Identity of the current repository state, used to detect stale reports."""
    parts = [head(root) or ""]
    parts += [f"{p}:{fingerprint(root, p)}" for p in sorted(dirty_files(root))]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- changes


@dataclass
class FileChange:
    path: str
    status: str  # added | modified | deleted | renamed | untracked | reverted
    added: int = 0
    removed: int = 0
    binary: bool = False
    categories: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ChangeSet:
    baseline_present: bool
    base_ref: str | None
    attributed: list[FileChange]
    pre_existing: list[str]  # dirty before the task and untouched since
    commits_since_baseline: int
    secrets: list[dict]  # [{path, patterns}] - never values
    notes: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _status_word(code: str) -> str:
    code = code.strip() or "M"
    if code == "??":
        return "untracked"
    return {"A": "added", "D": "deleted", "R": "renamed", "C": "added"}.get(code[0], "modified")


def _numstat(root: Path, base: str | None) -> dict[str, tuple[int, int, bool]]:
    if base is None:
        return {}
    out = git(root, "diff", "--numstat", "-z", "--no-renames", base, "--", check=False)
    stats: dict[str, tuple[int, int, bool]] = {}
    for record in out.split("\0"):
        if not record:
            continue
        added, removed, path = record.split("\t", 2)
        binary = added == "-"
        stats[path] = (0 if binary else int(added), 0 if binary else int(removed), binary)
    return stats


def _count_lines(full: Path) -> tuple[int, bool]:
    try:
        data = full.read_bytes()[:MAX_SCAN_BYTES]
    except OSError:
        return 0, False
    if b"\0" in data:
        return 0, True
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), False


def _added_text(root: Path, base: str | None, change: FileChange) -> str:
    """Text added by this change (for secret scanning only; never returned)."""
    full = root / change.path
    if change.status == "deleted" or change.binary or not full.is_file():
        return ""
    if change.status == "untracked" or base is None:
        return full.read_bytes()[:MAX_SCAN_BYTES].decode("utf-8", "replace")
    diff = git(root, "diff", "-U0", "--no-color", base, "--", change.path, check=False)
    return "\n".join(
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def collect_changes(root: Path, baseline: dict | None) -> ChangeSet:
    notes: list[str] = []
    current_head = head(root)
    dirty = dirty_files(root)
    base = (baseline or {}).get("head") if baseline else current_head
    base_dirty: dict[str, str] = (baseline or {}).get("dirty", {})

    committed: dict[str, str] = {}
    commits = 0
    if baseline and base and current_head and base != current_head:
        is_ancestor = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", base, current_head],
            capture_output=True,
            stdin=subprocess.DEVNULL,
        ).returncode == 0
        if not is_ancestor:
            notes.append(
                "HEAD is no longer a descendant of the baseline commit (reset/rebase/branch switch); "
                "attribution compares against the baseline commit directly."
            )
        commits = int(git(root, "rev-list", "--count", f"{base}..{current_head}", check=False).strip() or 0)
        out = git(root, "diff", "--name-status", "-z", "--no-renames", base, current_head)
        parts = [p for p in out.split("\0") if p]
        committed = {parts[i + 1]: _status_word(parts[i]) for i in range(0, len(parts) - 1, 2)}
    if baseline is None:
        notes.append(
            "No baseline recorded (call start_task before work begins); every uncommitted change is "
            "attributed to the task and pre-existing edits cannot be separated."
        )

    attributed: list[FileChange] = []
    pre_existing: list[str] = []
    for path in sorted(set(dirty) | set(committed) | set(base_dirty)):
        in_now = path in dirty or path in committed
        if path in base_dirty:
            now_fp = fingerprint(root, path)
            if not in_now:
                # Was dirty at baseline, now matches HEAD: the pre-existing change was discarded
                # or committed without a diff against the baseline commit.
                attributed.append(
                    FileChange(path, "reverted", note="pre-existing uncommitted change is gone (discarded?)")
                )
                continue
            if now_fp == base_dirty[path] and path not in committed:
                pre_existing.append(path)
                continue
            status = committed.get(path) or _status_word(dirty[path])
            attributed.append(
                FileChange(path, status, note="also had pre-existing uncommitted changes; attribution is partial")
            )
            continue
        if not in_now:
            continue
        status = committed.get(path) or _status_word(dirty[path])
        if path in committed and path not in dirty:
            status = committed[path]
        attributed.append(FileChange(path, status))

    stats = _numstat(root, base)
    secrets: list[dict] = []
    for change in attributed:
        if change.path in stats:
            change.added, change.removed, change.binary = stats[change.path]
        elif change.status == "untracked" or (change.status == "added" and base is None):
            change.added, change.binary = _count_lines(root / change.path)
        found = find_secrets(_added_text(root, base, change))
        if found:
            secrets.append({"path": change.path, "patterns": sorted(set(found))})

    return ChangeSet(
        baseline_present=baseline is not None,
        base_ref=base,
        attributed=attributed,
        pre_existing=pre_existing,
        commits_since_baseline=commits,
        secrets=secrets,
        notes=notes,
    )


def tracked_files(root: Path) -> list[str]:
    out = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [p for p in out.split("\0") if p]
