"""Claude Code hook mode: automatic baseline at session start, automatic verification at stop.

Wire-up (user settings, applies to every repository):
  SessionStart -> task-handoff hook session-start
  Stop         -> task-handoff hook stop

Both read the hook payload (JSON) on stdin and never fail the session: outside a git
repo, or with hooks disabled, they do nothing. Opt out per repo with
{"hooks": {"enabled": false}} in .task-handoff.json, or globally with TASK_HANDOFF_HOOKS=0.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import gitstate
from .config import load_config
from .handoff import build_report, render_markdown

QUIET_VERDICTS = {"NO CHANGES", "NO EXECUTABLE CHANGES"}


def _root_and_config(payload: dict) -> tuple[Path, dict] | None:
    if os.environ.get("TASK_HANDOFF_HOOKS", "1") == "0":
        return None
    try:
        root = gitstate.repo_root(payload.get("cwd") or os.getcwd())
    except gitstate.GitError:
        return None  # not a git repo (or git unavailable): nothing to do
    config = load_config(root)
    return (root, config) if config["hooks"]["enabled"] else None


def session_start(payload: dict) -> str | None:
    """Record a baseline for this session; returns text added to Claude's context."""
    found = _root_and_config(payload)
    if not found:
        return None
    root, _ = found
    session_id = payload.get("session_id") or ""
    existing = gitstate.load_baseline(root, session_id) if session_id else None
    if existing and existing.get("session_id") == session_id:
        return None  # resumed/compacted session keeps its original baseline
    baseline = gitstate.record_baseline(root, f"Claude Code session {session_id[:8]}".strip(), session_id=session_id)
    pre = len(baseline["dirty"])
    return (f"task-handoff: baseline recorded for {root} at {(baseline['head'] or 'no commits')[:8]}"
            + (f"; {pre} pre-existing uncommitted file(s) will not be attributed to this session" if pre else "")
            + ". Your changes are verified automatically when you finish a turn; report failures honestly.")


def _hook_state_path(root: Path, session_id: str = "") -> Path:
    if session_id:
        return gitstate.state_dir(root) / "sessions" / f"{gitstate._safe_id(session_id)}.hook.json"
    return gitstate.state_dir(root) / "hook_state.json"


def _one_line(report: dict) -> str:
    parts = [f"{c['id']} {c['outcome'].replace('_', ' ')}" for c in report["checks"]]
    untested = [f for f in report["findings"] if f["evidence"] == "NOT TESTED" and f["text"].startswith("No passing")]
    text = f"task-handoff: {report['verdict']}"
    if parts:
        text += " · " + ", ".join(parts)
    if untested:
        text += " · " + untested[0]["text"].replace("No passing test exercised:", "untested:")
    return text


def stop(payload: dict) -> dict | None:
    """Verify changes made since the last automatic check. Returns the hook's JSON output."""
    found = _root_and_config(payload)
    if not found:
        return None
    root, config = found
    session_id = payload.get("session_id") or ""
    state_file = _hook_state_path(root, session_id)
    try:
        last = json.loads(state_file.read_text(encoding="utf-8")).get("fingerprint")
    except (OSError, ValueError):
        last = None
    if gitstate.state_fingerprint(root) == last:
        return None  # nothing changed since the last automatic verification

    hooks = config["hooks"]
    report = build_report(str(root), run=True, session_id=session_id or None, overrides={"verification": {
        "timeout_seconds": hooks["timeout_seconds"], "full_suite_fallback": hooks["full_suite_fallback"]}})
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"fingerprint": gitstate.state_fingerprint(root), "verdict": report["verdict"]}), encoding="utf-8")

    if report["verdict"] in QUIET_VERDICTS:
        return None
    if report["verdict"] == "FAILED" and hooks["block_on_failure"] and not payload.get("stop_hook_active"):
        return {
            "decision": "block",
            "reason": (
                "Automatic task-handoff verification FAILED for the changes in this repository.\n\n"
                + render_markdown(report)
                + "\n\nIf your change caused this, fix it and finish. If the failure is pre-existing or unrelated "
                  "to your change, do not modify unrelated code: tell the user plainly what fails and why it is "
                  "not from this task."
            ),
        }
    return {"systemMessage": _one_line(report)}


def run(event: str, stdin_text: str) -> tuple[str, int]:
    """Entry point used by the CLI. Returns (stdout, exit code); never raises."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    try:
        if event == "session-start":
            return session_start(payload) or "", 0
        if event == "stop":
            out = stop(payload)
            return (json.dumps(out) if out else ""), 0
        return json.dumps({"systemMessage": f"task-handoff: unknown hook event '{event}'"}), 0
    except Exception as exc:  # surface the problem without breaking the user's session
        return json.dumps({"systemMessage": f"task-handoff hook error ({event}): {type(exc).__name__}: {exc}"}), 0
