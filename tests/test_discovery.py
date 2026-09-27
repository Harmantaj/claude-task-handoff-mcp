"""Repo discovery, setup diagnosis and git-missing handling."""

import json
import os
import subprocess
import sys
import time

import pytest

from conftest import Repo
from task_handoff import discovery, gitstate


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_find_repos_respects_depth_skips_and_query(tmp_path):
    base = tmp_path / "home"
    shop = _init(base / "code" / "shop-app")
    blog = _init(base / "code" / "clients" / "blog")
    _init(base / ".hidden" / "secret-repo")
    _init(base / "Library" / "cached-repo")
    _init(base / "code" / "node_modules" / "dep-repo")
    _init(base / "a" / "b" / "c" / "d" / "e" / "too-deep")
    _init(shop / "vendor-sub" / "inner")  # inside a repo: not listed separately
    os.utime(blog / ".git" / "HEAD", (time.time() - 3600, time.time() - 3600))
    (shop / "new.txt").write_text("x")
    subprocess.run(["git", "-C", str(shop), "add", "new.txt"], check=True)  # touches index: most recent

    result = discovery.find_repos([base])
    paths = [r["path"] for r in result["repos"]]
    assert paths == [str(shop), str(blog)]
    assert result["repos"][0]["uncommitted_files"] >= 1
    assert [r["path"] for r in discovery.find_repos([base], query="BLOG")["repos"]] == [str(blog)]


def test_check_setup_reports_tools_and_repo_readiness(py_repo, monkeypatch):
    text = discovery.check_setup([], str(py_repo.root))
    assert "OK `git`" in text and "(in-repo .venv/)" in text and "pytest available" in text
    assert text.rstrip().endswith("**Status:** ready.")

    monkeypatch.setattr(discovery, "_has_module", lambda python, module: False)
    text = discovery.check_setup([], str(py_repo.root))
    assert "needs attention" in text and "pytest is not installed" in text


def test_missing_git_gives_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    with pytest.raises(gitstate.GitMissing, match="xcode-select --install"):
        gitstate.repo_root(tmp_path)


def test_macos_git_stub_without_command_line_tools(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    stub = fake / "git"
    stub.write_text("#!/bin/sh\necho 'xcrun: error: invalid active developer path "
                    "(/Library/Developer/CommandLineTools), missing xcrun' >&2\nexit 1\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}/bin")
    with pytest.raises(gitstate.GitMissing, match="xcode-select --install"):
        gitstate.repo_root(tmp_path)
    assert "MISSING `git`" in discovery.check_setup([])


def _tool(name, arguments, *roots):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    out = subprocess.run([sys.executable, "-m", "task_handoff.server", *roots],
                         input=json.dumps(init) + "\n" + json.dumps(call) + "\n",
                         capture_output=True, text=True, timeout=120).stdout
    return [json.loads(l) for l in out.splitlines() if l.strip()][-1]["result"]


def test_find_repos_tool_is_limited_to_allowed_roots(tmp_path):
    inside = _init(tmp_path / "allowed" / "shop")
    _init(tmp_path / "other" / "shop2")
    result = _tool("find_repos", {"query": "shop"}, str(tmp_path / "allowed"))
    text = result["content"][0]["text"]
    assert not result["isError"] and str(inside) in text and "shop2" not in text
    empty = _tool("find_repos", {"query": "zzz-nothing"}, str(tmp_path / "allowed"))["content"][0]["text"]
    assert "No git repositories matching 'zzz-nothing'" in empty
