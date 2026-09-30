"""Help people who don't know paths or tooling: find local repos, and diagnose the setup."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, gitstate
from .checks import _has_module
from .pyenvs import find_python

SKIP_DIRS = {"Library", "Applications", "node_modules", "__pycache__", "site-packages", "venv", "env",
             "Pictures", "Music", "Movies", "Public", "dist", "build", "target", "vendor"}
MAX_DEPTH = 4
TIME_BUDGET_S = 6.0


def find_repos(roots: list[Path], query: str = "", limit: int = 20) -> dict:
    """Breadth-first search for git repositories (depth-limited, time-limited, never enters a repo)."""
    deadline = time.monotonic() + TIME_BUDGET_S
    found: list[Path] = []
    queue = [(root, 0) for root in roots if root.is_dir()]
    truncated = False
    while queue:
        if time.monotonic() > deadline:
            truncated = True
            break
        directory, depth = queue.pop(0)
        if (directory / ".git").exists():
            found.append(directory)
            continue  # nested repos (submodules etc.) are not listed separately
        if depth >= MAX_DEPTH:
            continue
        try:
            children = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError:  # permission denied, vanished, macOS privacy protection
            continue
        for entry in children:
            if entry.name.startswith(".") or entry.name in SKIP_DIRS or entry.name.endswith(".app"):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    queue.append((Path(entry.path), depth + 1))
            except OSError:
                continue

    needle = query.lower().strip()
    matches = [p for p in found if needle in str(p).lower()] if needle else found

    def last_activity(repo: Path) -> float:
        for marker in (".git/index", ".git/HEAD"):
            try:
                return (repo / marker).stat().st_mtime
            except OSError:
                continue
        return 0.0

    activity = {repo: last_activity(repo) for repo in matches}  # measured once, before any git command runs
    matches.sort(key=activity.__getitem__, reverse=True)
    repos = []
    for repo in matches[:limit]:
        try:
            dirty = len(gitstate.dirty_files(repo))
            branch = gitstate.git(repo, "branch", "--show-current", check=False).strip() or "(detached)"
        except gitstate.GitError:
            dirty, branch = None, "?"
        repos.append({"path": str(repo), "branch": branch, "uncommitted_files": dirty,
                      "last_activity": time.strftime("%Y-%m-%d", time.localtime(activity[repo]))})
    return {"repos": repos, "total_found": len(found), "matched": len(matches), "search_truncated": truncated,
            "searched": [str(r) for r in roots]}


def _version(argv: list[str]) -> str | None:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or proc.stderr).strip().splitlines()
    if proc.returncode != 0 or not out or any(m in (proc.stderr or "") for m in gitstate.XCRUN_MISSING):
        return None
    return out[0][:80]


TOOLS = [  # (name, version argv, what it's used for)
    ("git", ["git", "--version"], "required"),
    ("node", ["node", "--version"], "JS/TS projects"),
    ("npm", ["npm", "--version"], "JS/TS projects"),
    ("pnpm", ["pnpm", "--version"], "pnpm projects"),
    ("yarn", ["yarn", "--version"], "yarn projects"),
    ("go", ["go", "version"], "Go projects"),
    ("poetry", ["poetry", "--version"], "Poetry projects (optional: its envs are also found without it)"),
    ("conda", ["conda", "--version"], "conda projects (optional: envs are found from environment.yml)"),
    ("shellcheck", ["shellcheck", "--version"], "optional shell-script linting"),
]


def check_setup(allowed_roots: list[Path], repo_path: str | None = None) -> str:
    lines = [f"## Task Handoff setup (v{__version__})",
             f"- Python {platform.python_version()} at `{sys.executable}` · {platform.system()} {platform.machine()}"]
    problems: list[str] = []
    for name, argv, purpose in TOOLS:
        version = _version(argv) if shutil.which(name) else None
        if version:
            lines.append(f"- OK `{name}`: {version}")
        elif purpose == "required":
            lines.append(f"- MISSING `{name}` (required)")
            problems.append(gitstate.GIT_INSTALL_HINT)
        else:
            lines.append(f"- not found `{name}`: needed only for {purpose}")
    lines.append("- Allowed directories: " + (", ".join(f"`{r}`" for r in allowed_roots) or "any (no restriction)"))

    if repo_path:
        try:
            root = gitstate.repo_root(repo_path)
            python, source = find_python(root)
            has_pytest = bool(python) and _has_module(python, "pytest")
            lines.append(f"\n**Repository** `{root}`")
            lines.append(f"- Python checks would use `{python}` ({source}); pytest {'available' if has_pytest else 'NOT installed'}")
            if not has_pytest and (root / "tests").is_dir():
                problems.append(f"pytest is not installed for {python}: install your project's dev dependencies "
                                "into its environment (e.g. `poetry install`, `pip install -r requirements-dev.txt` "
                                "in its .venv, or `conda env create`).")
            if (root / "package.json").is_file() and not (root / "node_modules").is_dir():
                problems.append("This repo has package.json but no node_modules: run `npm install` (or pnpm/yarn) there.")
        except gitstate.GitError as exc:
            problems.append(str(exc))

    lines.append("\n**Status:** " + ("ready." if not problems else "needs attention:"))
    lines += [f"- {p}" for p in problems]
    return "\n".join(lines)
