"""MCP Apps UI (interactive report card + dashboard) and menu prompts, over the real stdio protocol."""

import json
import subprocess
import sys

from task_handoff import gitstate
from task_handoff.handoff import build_report
from task_handoff.ui import MAX_UI_CHANGES, REPORT_KEY, UI_URI, report_payload

INIT = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "t", "version": "0"},
                   "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}}}


def session(requests, *roots):
    msgs = [INIT] + [{"jsonrpc": "2.0", "id": i + 1, **r} for i, r in enumerate(requests)]
    out = subprocess.run([sys.executable, "-m", "task_handoff.server", *roots],
                         input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, timeout=180)
    replies = {m["id"]: m for m in map(json.loads, out.stdout.splitlines()) if "id" in m}
    return [replies[i] for i in range(len(msgs))]


def test_handshake_tools_and_resource_expose_the_ui():
    init, tools, resources, read = session([
        {"method": "tools/list"}, {"method": "resources/list"}, {"method": "resources/read", "params": {"uri": UI_URI}}])
    caps = init["result"]["capabilities"]
    assert "io.modelcontextprotocol/ui" in caps["extensions"] and "resources" in caps and "prompts" in caps
    bound = {t["name"] for t in tools["result"]["tools"] if t.get("_meta", {}).get("ui", {}).get("resourceUri") == UI_URI}
    assert bound == {"verify_task", "summarize_handoff", "inspect_task_state", "open_dashboard"}
    assert all(t["_meta"]["ui/resourceUri"] == UI_URI for t in tools["result"]["tools"] if t["name"] in bound)
    assert resources["result"]["resources"][0]["uri"] == UI_URI
    content = read["result"]["contents"][0]
    assert content["mimeType"] == "text/html;profile=mcp-app"
    assert '"ui/initialize"' in content["text"] and "ui/notifications/tool-result" in content["text"]
    missing = session([{"method": "resources/read", "params": {"uri": "ui://nope"}}])[1]
    assert missing["error"]["code"] == -32002


def test_report_data_travels_in_meta_not_model_text(py_repo):
    gitstate.record_baseline(py_repo.root, "Break add")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a - b\n")
    reply = session([{"method": "tools/call", "params": {"name": "verify_task",
                                                        "arguments": {"repo_path": str(py_repo.root)}}}])[1]
    result = reply["result"]
    text = result["content"][0]["text"]
    assert text.startswith("## Break add - FAILED") and "evidence_counts" not in text
    payload = result["_meta"][REPORT_KEY]
    assert payload["kind"] == "report" and payload["verdict"] == "FAILED" and payload["repo_name"] == "proj"
    assert payload["evidence_counts"]["FAILED"] == 1  # the failure is its own (red) bucket, not "verified"
    assert payload["evidence_counts"]["VERIFIED"] == 1  # python-syntax passed
    assert payload["branch"] and payload["checks_ran"] is True


def test_summary_marks_stale_reports(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # x\n")
    build_report(str(py_repo.root))
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # y\n")
    reply = session([{"method": "tools/call", "params": {"name": "summarize_handoff",
                                                        "arguments": {"repo_path": str(py_repo.root)}}}])[1]
    assert reply["result"]["_meta"][REPORT_KEY]["stale"] is True
    assert reply["result"]["content"][0]["text"].startswith("[STALE]")


def test_dashboard_lists_projects_with_last_verdict(py_repo, tmp_path):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # x\n")
    build_report(str(py_repo.root))
    subprocess.run(["git", "init", "-q", str(tmp_path / "other")], check=True)
    reply = session([{"method": "tools/call", "params": {"name": "open_dashboard", "arguments": {}}}],
                    str(tmp_path))[1]
    payload = reply["result"]["_meta"]["task-handoff/dashboard"]
    by_name = {r["name"]: r for r in payload["repos"]}
    assert by_name["proj"]["last_verdict"] == "VERIFIED" and by_name["other"]["last_verdict"] is None
    assert by_name["proj"]["location"] and "last verification: VERIFIED" in reply["result"]["content"][0]["text"]


def test_menu_prompts():
    _, listed, verify, start, unknown = session([
        {"method": "prompts/list"},
        {"method": "prompts/get", "params": {"name": "verify-work", "arguments": {"project": "shop"}}},
        {"method": "prompts/get", "params": {"name": "start-task", "arguments": {"project": "shop", "task": "dark mode"}}},
        {"method": "prompts/get", "params": {"name": "nope"}},
    ])
    names = [p["name"] for p in listed["result"]["prompts"]]
    assert names == ["verify-work", "handoff-summary", "projects-dashboard", "start-task", "check-setup"]
    assert all(p["title"] and p["description"] for p in listed["result"]["prompts"])
    text = verify["result"]["messages"][0]["content"]["text"]
    assert "my project 'shop'" in text and "verify_task" in text
    assert "'dark mode'" in start["result"]["messages"][0]["content"]["text"]
    assert unknown["error"]["code"] == -32602


def test_ui_payload_caps_huge_change_lists(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    for i in range(MAX_UI_CHANGES + 20):
        py_repo.write(f"data/f{i}.txt", "x\n")
    payload = report_payload(build_report(str(py_repo.root), run=False))
    assert len(payload["changes"]) == MAX_UI_CHANGES and payload["changes_total"] == MAX_UI_CHANGES + 20
