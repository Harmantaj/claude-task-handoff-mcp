"""Raw protocol behaviour of the stdlib MCP server, driven over real pipes."""

import json
import subprocess
import sys

import pytest


def _session(messages, *args, env=None):
    proc = subprocess.run(
        [sys.executable, "-m", "task_handoff.server", *args],
        input="".join(json.dumps(m) + "\n" for m in messages) if isinstance(messages, list) else messages,
        capture_output=True, text=True, timeout=120, env=env,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()], proc.stderr


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def test_handshake_ping_list_and_errors():
    replies, stderr = _session([
        INIT,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "no/such"},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
    ])
    by_id = {r["id"]: r for r in replies}
    assert len(replies) == 5  # the notification got no reply
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert by_id[1]["result"]["serverInfo"]["name"] == "task-handoff"
    assert by_id[2]["result"] == {}
    names = {t["name"] for t in by_id[3]["result"]["tools"]}
    assert names == {"start_task", "inspect_task_state", "run_relevant_checks", "verify_task", "summarize_handoff"}
    assert all(t["inputSchema"]["type"] == "object" for t in by_id[3]["result"]["tools"])
    assert by_id[4]["error"]["code"] == -32601
    assert by_id[5]["error"]["code"] == -32602
    assert "starting" in stderr


def test_unknown_protocol_version_negotiates_latest_supported():
    init = json.loads(json.dumps(INIT))
    init["params"]["protocolVersion"] = "2099-01-01"
    replies, _ = _session([init])
    assert replies[0]["result"]["protocolVersion"] == "2025-11-25"


def test_parse_error_does_not_kill_server():
    replies, _ = _session("not json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n")
    assert replies[0]["error"]["code"] == -32700 and replies[1] == {"jsonrpc": "2.0", "id": 9, "result": {}}


def _call(name, arguments, *args):
    replies, _ = _session([INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}}], *args)
    return next(r for r in replies if r["id"] == 2)["result"]


def test_tool_errors_are_results_not_crashes(tmp_path):
    relative = _call("inspect_task_state", {"repo_path": "relative/path"})
    assert relative["isError"] and "must be absolute" in relative["content"][0]["text"]
    not_repo = _call("inspect_task_state", {"repo_path": str(tmp_path)})
    assert not_repo["isError"] and "Not inside a git repository" in not_repo["content"][0]["text"]
    missing_arg = _call("start_task", {"repo_path": str(tmp_path)})
    assert missing_arg["isError"]


def test_allowed_roots_are_enforced(py_repo, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    denied = _call("inspect_task_state", {"repo_path": str(py_repo.root)}, str(other))
    assert denied["isError"] and "outside the allowed directories" in denied["content"][0]["text"]
    allowed = _call("inspect_task_state", {"repo_path": str(py_repo.root / "app")}, str(py_repo.root.parent))
    assert not allowed["isError"] and "Task handoff" in allowed["content"][0]["text"]
    # an unexpanded Claude Desktop placeholder (setting left empty) means "no restriction"
    unset = _call("inspect_task_state", {"repo_path": str(py_repo.root)}, "${user_config.allowed_directories}")
    assert not unset["isError"]


def test_progress_heartbeats_for_long_calls(py_repo, monkeypatch):
    import task_handoff.protocol as protocol
    script = (
        "import sys, task_handoff.protocol as p, task_handoff.server as s\n"
        "p.PROGRESS_INTERVAL_S = 0.2\n"
        "s.main([])\n"
    )
    (py_repo.root / "tests" / "test_calc.py").write_text("import time\n\ndef test_slow():\n    time.sleep(1.5)\n")
    msgs = [INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "run_relevant_checks", "arguments": {"repo_path": str(py_repo.root)},
                              "_meta": {"progressToken": "tok"}}}]
    proc = subprocess.run([sys.executable, "-c", script], input="".join(json.dumps(m) + "\n" for m in msgs),
                          capture_output=True, text=True, timeout=120)
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    progress = [l for l in lines if l.get("method") == "notifications/progress"]
    assert progress and progress[0]["params"]["progressToken"] == "tok"
    assert lines[-1]["id"] == 2 and not lines[-1]["result"]["isError"]
