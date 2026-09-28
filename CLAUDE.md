# Claude Task Handoff MCP

Python package `task_handoff/`, **stdlib only at runtime** (Python >= 3.9). The spec is in `docs/`.

- Pipeline: `gitstate` (baseline/attribution) → `impact` (categories/risks) → `checks` + `playwright_adapter` (selection) → `runner` (execution) → `handoff` (evidence labels, verdict, markdown).
- `hooks.py`: Claude Code hook mode (`task-handoff hook session-start|stop`); `discovery.py`: find_repos/check_setup.
- `ui.py` + `ui/app.html`: MCP Apps card/dashboard (data in result `_meta`, never model text); menu prompts live in `server.py`.
- `protocol.py` is a minimal MCP stdio server; `server.py` defines the tools. Compatibility is tested with the official `mcp` client (dev dependency) in `tests/test_mcp_stdio.py`.
- Never report a failed/blocked/empty check as passed; never install, reset, delete, commit or push.
- Tests: `python -m pytest -q`. Desktop extension: `./scripts/build_mcpb.sh` → `dist/*.mcpb`. Keep `bundle/manifest.json` version equal to `task_handoff/__init__.py`.
