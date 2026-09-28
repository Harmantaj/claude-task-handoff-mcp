"""End-to-end: launch the real server over stdio and drive it with the MCP client."""

import asyncio

import pytest

pytest.importorskip("mcp")
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {"start_task", "inspect_task_state", "run_relevant_checks", "verify_task", "summarize_handoff", "find_repos", "check_setup", "open_dashboard"}


def _text(result) -> str:
    assert not result.is_error, result
    return "".join(getattr(block, "text", "") for block in result.content)


async def _session_flow(repo_root: str) -> dict:
    params = StdioServerParameters(command=sys.executable, args=["-m", "task_handoff.server"])
    out = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            out["tools"] = {t.name for t in (await session.list_tools()).tools}
            out["start"] = _text(await session.call_tool("start_task", {"repo_path": repo_root, "task": "Make add robust"}))
            with open(f"{repo_root}/app/calc.py", "w") as fh:
                fh.write("def add(a, b):\n    return a - b\n")
            out["inspect"] = _text(await session.call_tool("inspect_task_state", {"repo_path": repo_root}))
            out["verify"] = _text(await session.call_tool(
                "verify_task", {"repo_path": repo_root, "claims": ["Fixed add in calc.py; all tests pass"]}))
            out["summary"] = _text(await session.call_tool("summarize_handoff", {"repo_path": repo_root}))
            with open(f"{repo_root}/app/calc.py", "w") as fh:
                fh.write("def add(a, b):\n    return a + b\n\n# fixed\n")
            out["stale"] = _text(await session.call_tool("summarize_handoff", {"repo_path": repo_root}))
            out["checks"] = _text(await session.call_tool("run_relevant_checks", {"repo_path": repo_root}))
            out["bad"] = await session.call_tool("inspect_task_state", {"repo_path": "/nonexistent/path"})
            out["prompts"] = [p.name for p in (await session.list_prompts()).prompts]
            out["resource"] = (await session.read_resource("ui://task-handoff/app.html")).contents[0]
    return out


def test_stdio_end_to_end(py_repo):
    out = asyncio.run(_session_flow(str(py_repo.root)))
    assert out["tools"] == EXPECTED_TOOLS
    assert "Baseline recorded for 'Make add robust'" in out["start"]
    assert "NOT VERIFIED (inspection only)" in out["inspect"]
    assert "## Make add robust - FAILED" in out["verify"]
    assert "[CLAIMED] Fixed add in calc.py; all tests pass" in out["verify"]
    assert "Next prompt:** Fix the failing check(s): pytest" in out["verify"]
    assert out["summary"] == out["verify"]  # served from the stored report, no re-run
    assert out["stale"].startswith("[STALE]")
    assert "PASSED pytest" in out["checks"] and "Verdict: VERIFIED" in out["checks"]
    assert out["bad"].is_error
    assert "verify-work" in out["prompts"] and "projects-dashboard" in out["prompts"]
    assert out["resource"].mime_type == "text/html;profile=mcp-app" and "ui/initialize" in out["resource"].text
