"""Data for the interactive UI (MCP Apps). Travels in a tool result's `_meta`, never to the model."""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from . import gitstate
from .discovery import find_repos
from .handoff import load_report

UI_URI = "ui://task-handoff/app.html"
REPORT_KEY = "task-handoff/report"
DASHBOARD_KEY = "task-handoff/dashboard"
MAX_UI_CHANGES = 150
EVIDENCE_ORDER = ("VERIFIED", "FAILED", "CLAIMED", "INFERRED", "NOT TESTED", "BLOCKED")


def load_app_html() -> str:
    return (Path(__file__).parent / "ui" / "app.html").read_text(encoding="utf-8")


def _branch(root: str) -> str:
    try:
        return gitstate.git(Path(root), "branch", "--show-current", check=False).strip() or "detached"
    except gitstate.GitError:
        return ""


def report_payload(report: dict, *, stale: bool = False) -> dict:
    changes = report["changes"]
    # A verified *failure* is shown as its own bucket so the strip never reads green on a failure.
    counts = Counter("FAILED" if f["evidence"] == "VERIFIED" and " FAILED (" in f["text"] else f["evidence"]
                     for f in report["findings"])
    return {
        "kind": "report",
        "repo": report["repo"],
        "repo_name": Path(report["repo"]).name,
        "branch": _branch(report["repo"]),
        "task": report.get("task"),
        "verdict": report["verdict"],
        "generated_at": report["generated_at"],
        "stale": stale,
        "checks_ran": report["checks_ran"],
        "baseline": report["baseline"],
        "changes": changes[:MAX_UI_CHANGES],
        "changes_total": len(changes),
        "added_total": sum(c["added"] for c in changes),
        "removed_total": sum(c["removed"] for c in changes),
        "pre_existing_count": len(report["pre_existing_unrelated"]),
        "checks": report["checks"],
        "findings": report["findings"],
        "evidence_counts": {label: counts.get(label, 0) for label in EVIDENCE_ORDER},
        "risks": report["risks"],
        "next_prompt": report["next_prompt"],
    }


def _display_parent(path: str) -> str:
    parent = Path(path).parent
    try:
        return "~/" + str(parent.relative_to(Path.home())) if parent != Path.home() else "~"
    except ValueError:
        parts = parent.parts
        return str(parent) if len(parts) <= 3 else ".../" + "/".join(parts[-2:])


def dashboard_payload(roots: list[Path], limit: int = 30) -> dict:
    found = find_repos(roots, limit=limit)
    repos = []
    for entry in found["repos"]:
        last = None
        try:
            last = load_report(Path(entry["path"]))
        except (OSError, ValueError, gitstate.GitError):
            pass
        repos.append({
            **entry,
            "name": Path(entry["path"]).name,
            "location": _display_parent(entry["path"]),
            "last_verdict": last["verdict"] if last else None,
            "last_checked": last["generated_at"] if last else None,
            "last_task": (last or {}).get("task"),
        })
    return {"kind": "dashboard", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "repos": repos,
            "total_found": found["total_found"], "searched": found["searched"],
            "search_truncated": found["search_truncated"]}


def dashboard_text(payload: dict) -> str:
    if not payload["repos"]:
        return "No git repositories found under " + ", ".join(payload["searched"]) + "."
    lines = [f"{len(payload['repos'])} project(s), most recently active first:"]
    for r in payload["repos"]:
        verdict = r["last_verdict"] or "never verified"
        lines.append(f"- {r['name']} (`{r['path']}`): branch {r['branch']}, "
                     f"{r['uncommitted_files']} uncommitted file(s), last verification: {verdict}")
    return "\n".join(lines)
