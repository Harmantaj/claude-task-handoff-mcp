"""Command-line entry point (for hooks, CI, or manual use).

  task-handoff start  <repo> "<task>"
  task-handoff inspect <repo> [--json]
  task-handoff verify  <repo> [--claim TEXT ...] [--json]
  task-handoff hook session-start|stop   (Claude Code hooks; payload JSON on stdin)
Exit status of `verify`: 0 VERIFIED/NO CHANGES/NO EXECUTABLE CHANGES, 1 FAILED, 2 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import gitstate
from .handoff import build_report, render_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task-handoff")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("repo"); s.add_argument("task")
    i = sub.add_parser("inspect"); i.add_argument("repo"); i.add_argument("--json", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("repo"); v.add_argument("--claim", action="append", default=[])
    v.add_argument("--json", action="store_true")
    h = sub.add_parser("hook"); h.add_argument("event", choices=["session-start", "stop"])
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout, sys.stderr):  # hook payloads and reports are UTF-8 on every OS
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.cmd == "hook":
        from .hooks import run as run_hook

        out, code = run_hook(args.event, sys.stdin.read())
        if out:
            print(out)
        return code

    try:
        if args.cmd == "start":
            b = gitstate.record_baseline(gitstate.repo_root(args.repo), args.task)
            print(f"Baseline recorded ({len(b['dirty'])} pre-existing uncommitted files).")
            return 0
        report = build_report(args.repo, run=args.cmd == "verify", claims=getattr(args, "claim", []))
    except gitstate.GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(report, indent=2) if args.json else render_markdown(report))
    if args.cmd == "inspect":
        return 0
    return {"VERIFIED": 0, "NO CHANGES": 0, "NO EXECUTABLE CHANGES": 0, "FAILED": 1}.get(report["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
