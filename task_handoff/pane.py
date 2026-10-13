"""Read-only data for the live pane in Claude Code (`task-handoff pane <repo>`).

Returns the last saved report as-is (never re-runs checks, never overwrites last_report.json),
whether the repository changed since that report, and the unified diff of the changes attributed
to the session's baseline. Secrets in the diff are redacted.
"""

from __future__ import annotations

from pathlib import Path

from . import gitstate
from .handoff import load_report
from .redact import redact

MAX_DIFF_CHARS = 60_000


def _file_diff(root: Path, base: str | None, change: gitstate.FileChange) -> str:
    if change.status == "reverted" or change.binary:
        return ""
    if change.status == "untracked" or base is None:
        if not (root / change.path).is_file():
            return ""
        # --no-index exits 1 when the files differ, which is the expected case here.
        return gitstate.git(root, "diff", "--no-color", "--no-index", "--", "/dev/null", change.path, check=False)
    return gitstate.git(root, "diff", "--no-color", base, "--", change.path, check=False)


def pane_data(repo_path: str, session_id: str | None = None, max_diff_chars: int = MAX_DIFF_CHARS) -> dict:
    root = gitstate.repo_root(repo_path)
    own = (gitstate.state_dir(root) / "sessions" / f"{gitstate._safe_id(session_id)}.json") if session_id else None
    baseline = gitstate.load_baseline(root, session_id)
    changes = gitstate.collect_changes(root, baseline)

    diff_parts: list[str] = []
    used = 0
    omitted: list[str] = []
    for change in changes.attributed:
        text = redact(_file_diff(root, changes.base_ref, change))
        if not text:
            continue
        if used + len(text) > max_diff_chars:  # cut at file boundaries so every hunk still parses
            omitted.append(change.path)
            continue
        diff_parts.append(text if text.endswith("\n") else text + "\n")
        used += len(text)

    report = load_report(root)
    return {
        "repo": str(root),
        "name": root.name,
        "state_dir": str(gitstate.state_dir(root)),
        "baseline": None if baseline is None else {
            "task": baseline.get("task"),
            "created_at": baseline.get("created_at"),
            "head": (baseline.get("head") or "")[:12] or None,
            "is_session": bool(own and own.is_file()),
        },
        "changes": [
            {"path": c.path, "status": c.status, "added": c.added, "removed": c.removed,
             **({"note": c.note} if c.note else {})}
            for c in changes.attributed
        ],
        "pre_existing_unrelated": changes.pre_existing,
        "diff": "".join(diff_parts),
        "diff_omitted": omitted,
        "report": report,
        "stale": bool(report) and report.get("state_fingerprint") != gitstate.state_fingerprint(root),
    }
