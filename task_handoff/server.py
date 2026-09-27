"""Task Handoff MCP server (stdio, no third-party dependencies).

Usage: python -m task_handoff.server [ALLOWED_ROOT ...]
If allowed roots are given (or TASK_HANDOFF_ALLOWED_ROOTS is set, os.pathsep-separated),
tools refuse repositories outside them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import __version__, gitstate
from .discovery import check_setup as _check_setup
from .discovery import find_repos as _find_repos
from .handoff import build_report, load_report, render_markdown
from .protocol import StdioServer, Tool, ToolError

INSTRUCTIONS = """\
Independent verification of coding work in a local git repository (pass an absolute repo_path).
If the user names a project instead of giving a path, call find_repos to locate it (confirm if
several match). If something doesn't work, call check_setup and relay its fixes.
Workflow: call start_task before modifying code (records a baseline so pre-existing
uncommitted changes are not attributed to the task), then verify_task when done. Report the
verdict honestly: FAILED/BLOCKED/NOT TESTED items are not successes. Every finding is
labelled VERIFIED, CLAIMED, INFERRED, NOT TESTED or BLOCKED. verify_task runs the project's
own tests/build, which can take minutes on large projects."""

# GUI apps (e.g. Claude Desktop launched from the Dock) get a minimal PATH; add the usual
# tool locations that exist so npm/go/etc. are found. Nothing is installed.
EXTRA_PATH_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin", "~/go/bin",
                   "/usr/local/go/bin", "~/.bun/bin", "~/.volta/bin"]

ALLOWED_ROOTS: list[Path] = []


def _augment_path() -> None:
    current = os.environ.get("PATH", "").split(os.pathsep)
    extra = [str(Path(d).expanduser()) for d in EXTRA_PATH_DIRS]
    extra = [d for d in extra if Path(d).is_dir() and d not in current]
    if extra:
        os.environ["PATH"] = os.pathsep.join(current + extra)


def _resolve(repo_path: str) -> Path:
    if not repo_path or not str(repo_path).strip():
        raise ToolError("repo_path is required (absolute path to a git repository).")
    if not Path(repo_path).expanduser().is_absolute():
        raise ToolError(f"repo_path must be absolute, got: {repo_path}")
    try:
        root = gitstate.repo_root(repo_path)
    except gitstate.GitError as exc:
        raise ToolError(str(exc)) from exc
    if ALLOWED_ROOTS and not any(root == r or r in root.parents for r in ALLOWED_ROOTS):
        allowed = ", ".join(str(r) for r in ALLOWED_ROOTS)
        raise ToolError(f"{root} is outside the allowed directories ({allowed}). "
                        "Change 'Allowed directories' in the extension settings to permit it.")
    return root


def _out(report: dict, fmt: str) -> str:
    return json.dumps(report, indent=2) if fmt == "json" else render_markdown(report)


# ------------------------------------------------------------------------------ tools


def start_task(repo_path: str, task: str) -> str:
    root = _resolve(repo_path)
    previous = gitstate.load_baseline(root)
    baseline = gitstate.record_baseline(root, task)
    msg = (f"Baseline recorded for '{task}' at {(baseline['head'] or 'no commits')[:12]} "
           f"with {len(baseline['dirty'])} pre-existing uncommitted file(s).")
    if previous:
        msg += f" Replaced previous baseline for '{previous.get('task')}'."
    return msg


def inspect_task_state(repo_path: str, format: str = "markdown") -> str:
    root = _resolve(repo_path)
    return _out(build_report(str(root), run=False), format)


def run_relevant_checks(repo_path: str, dry_run: bool = False, format: str = "markdown") -> str:
    root = _resolve(repo_path)
    report = build_report(str(root), run=not dry_run)
    if format == "json":
        return json.dumps(report["checks"], indent=2)
    lines = [f"Verdict: {report['verdict']}"]
    for c in report["checks"]:
        lines.append(f"- {c['outcome'].upper()} {c['id']}: {c['detail']} | `{c['command']}` | why: {c['reason']}")
        if c["outcome"] == "failed":
            lines += [f"    > {ln}" for ln in (c.get("excerpt") or [])[-6:]]
    if not report["checks"]:
        lines.append("No checks apply to the current changes.")
    return "\n".join(lines)


def verify_task(repo_path: str, claims: list | None = None, notes: str = "", format: str = "markdown") -> str:
    root = _resolve(repo_path)
    report = build_report(str(root), run=True, claims=[str(c) for c in (claims or [])], notes=notes)
    return _out(report, format)


def summarize_handoff(repo_path: str, format: str = "markdown") -> str:
    root = _resolve(repo_path)
    report = load_report(root)
    if report and report.get("state_fingerprint") == gitstate.state_fingerprint(root):
        return _out(report, format)
    fresh = build_report(str(root), run=False)
    if format == "json":
        return json.dumps({"stale": bool(report), **fresh}, indent=2)
    prefix = ("[STALE] The repository changed since the last verify_task; checks below were NOT re-run.\n\n"
              if report else "[NO PRIOR VERIFICATION] Showing inspection only; call verify_task to run checks.\n\n")
    return prefix + render_markdown(fresh)


def find_repos(query: str = "", limit: int = 20) -> str:
    roots = ALLOWED_ROOTS or [Path.home()]
    result = _find_repos(roots, query=query, limit=max(1, min(int(limit), 50)))
    if not result["repos"]:
        where = ", ".join(result["searched"])
        hint = f" matching '{query}'" if query else ""
        return (f"No git repositories{hint} found under {where} (searched {result['total_found']} repos, "
                f"depth <= 4, skipping hidden folders and Library). Ask the user for the full path.")
    lines = [f"Found {result['matched']} repo(s)" + (f" matching '{query}'" if query else "")
             + f" (showing {len(result['repos'])}, most recently active first):"]
    for r in result["repos"]:
        dirty = "?" if r["uncommitted_files"] is None else r["uncommitted_files"]
        lines.append(f"- `{r['path']}` · branch {r['branch']} · {dirty} uncommitted file(s) · active {r['last_activity']}")
    if result["search_truncated"]:
        lines.append("(search stopped at the time limit; results may be incomplete - pass a query to narrow it)")
    return "\n".join(lines)


def check_setup(repo_path: str = "") -> str:
    return _check_setup(ALLOWED_ROOTS, repo_path or None)


REPO = {"type": "string", "description": "Absolute path to (a directory inside) the git repository."}
FORMAT = {"type": "string", "enum": ["markdown", "json"], "default": "markdown",
          "description": "markdown (compact handoff) or json (full structured report)."}


def build_server() -> StdioServer:
    server = StdioServer("task-handoff", __version__, INSTRUCTIONS)
    server.tool(Tool(
        "start_task",
        "Record a baseline before starting work: HEAD plus fingerprints of files that are already dirty, so later "
        "verification attributes only the task's own changes. Overwrites any previous baseline for the repository. "
        "Writes only inside the .git directory.",
        {"type": "object", "properties": {"repo_path": REPO, "task": {"type": "string",
         "description": "Short description of the task about to be done."}}, "required": ["repo_path", "task"]},
        start_task, {"title": "Start task (record baseline)", "readOnlyHint": False, "destructiveHint": False},
    ))
    server.tool(Tool(
        "inspect_task_state",
        "Read-only snapshot: files changed since the baseline (excluding pre-existing edits), impact categories, "
        "risk flags, and which checks would be selected. Runs nothing.",
        {"type": "object", "properties": {"repo_path": REPO, "format": FORMAT}, "required": ["repo_path"]},
        inspect_task_state, {"title": "Inspect task state", "readOnlyHint": True},
    ))
    server.tool(Tool(
        "run_relevant_checks",
        "Select the cheapest meaningful checks for the changed files (syntax, lint, typecheck, targeted tests, build "
        "when deps/config changed, Playwright for UI changes if configured) and run them. Never installs anything. "
        "dry_run=true only lists the selection.",
        {"type": "object", "properties": {"repo_path": REPO, "dry_run": {"type": "boolean", "default": False},
                                          "format": FORMAT}, "required": ["repo_path"]},
        run_relevant_checks, {"title": "Run relevant checks", "readOnlyHint": False, "destructiveHint": False},
    ))
    server.tool(Tool(
        "verify_task",
        "Full pipeline: inspect changes, run relevant checks, cross-check the agent's claims, and return a compact "
        "evidence-labelled handoff with a verdict and a suggested next prompt.",
        {"type": "object", "properties": {
            "repo_path": REPO,
            "claims": {"type": "array", "items": {"type": "string"},
                       "description": "Statements about the work to check, e.g. 'fixed login redirect in auth.py'."},
            "notes": {"type": "string", "description": "Optional free-text notes to include in the handoff."},
            "format": FORMAT}, "required": ["repo_path"]},
        verify_task, {"title": "Verify task", "readOnlyHint": False, "destructiveHint": False},
    ))
    server.tool(Tool(
        "summarize_handoff",
        "Return the last verification report without re-running checks. If the repository changed since then, the "
        "report is marked stale and a fresh inspection-only report is returned.",
        {"type": "object", "properties": {"repo_path": REPO, "format": FORMAT}, "required": ["repo_path"]},
        summarize_handoff, {"title": "Summarize handoff", "readOnlyHint": True},
    ))
    server.tool(Tool(
        "find_repos",
        "Find git repositories on this computer (inside the allowed directories, or the home folder), most recently "
        "active first. Use it when the user names a project ('my shop app') instead of giving a path.",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "Case-insensitive part of the repo path, e.g. 'shop'. Empty lists all."},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50}}},
        find_repos, {"title": "Find repositories", "readOnlyHint": True},
    ))
    server.tool(Tool(
        "check_setup",
        "Diagnose the local setup: git, Python, node/npm, go, poetry, conda and other tools the checks use, plus "
        "(given repo_path) which interpreter that repo's checks would use and whether its test deps are installed. "
        "Returns exact fixes for anything missing.",
        {"type": "object", "properties": {"repo_path": {"type": "string",
         "description": "Optional repository to diagnose."}}},
        check_setup, {"title": "Check setup", "readOnlyHint": True},
    ))
    return server


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    roots = [a for a in args if a.strip() and not a.startswith("${")]
    roots += [r for r in os.environ.get("TASK_HANDOFF_ALLOWED_ROOTS", "").split(os.pathsep) if r.strip()]
    ALLOWED_ROOTS[:] = [Path(r).expanduser().resolve() for r in roots]
    _augment_path()
    server = build_server()
    server.log(f"v{__version__} starting; python {sys.version.split()[0]}; "
               f"allowed roots: {', '.join(map(str, ALLOWED_ROOTS)) or 'any'}")
    server.serve()


if __name__ == "__main__":
    main()
