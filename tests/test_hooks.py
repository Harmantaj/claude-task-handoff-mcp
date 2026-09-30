"""Claude Code hook mode: automatic baseline and automatic verification."""

import json
import os
import subprocess
import sys
from pathlib import Path

from task_handoff import gitstate, hooks
from task_handoff.handoff import build_report


def _stop(repo, **extra):
    out, code = hooks.run("stop", json.dumps({"cwd": str(repo.root), "session_id": "s1", **extra}))
    assert code == 0
    return json.loads(out) if out else None


def test_outside_git_repo_does_nothing(tmp_path):
    assert hooks.run("session-start", json.dumps({"cwd": str(tmp_path)})) == ("", 0)
    assert hooks.run("stop", json.dumps({"cwd": str(tmp_path)})) == ("", 0)


def test_session_start_baseline_excludes_prior_work_and_survives_resume(py_repo):
    py_repo.write("app/util.py", "def ident(x):\n    return x  # WIP from before\n")
    text, _ = hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "abc12345xyz"}))
    assert "baseline recorded" in text and "1 pre-existing uncommitted file(s)" in text
    first = gitstate.load_baseline(py_repo.root)
    assert first["session_id"] == "abc12345xyz"
    again, _ = hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "abc12345xyz",
                                                      "source": "resume"}))
    assert again == "" and gitstate.load_baseline(py_repo.root)["created_at"] == first["created_at"]
    # The pre-existing edit is not verified or blamed at stop time.
    assert _stop(py_repo) is None


def test_stop_reports_pass_once_then_stays_quiet(py_repo):
    hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "s1"}))
    assert _stop(py_repo) is None  # nothing changed yet
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # tidy\n")
    out = _stop(py_repo)
    assert out == {"systemMessage": "task-handoff: VERIFIED · python-syntax passed, pytest passed"}
    assert _stop(py_repo) is None  # same state: not re-run


def test_stop_blocks_once_on_failure(py_repo):
    hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "s1"}))
    py_repo.write("app/calc.py", "def add(a, b):\n    return a - b\n")
    out = _stop(py_repo)
    assert out["decision"] == "block"
    assert "pytest" in out["reason"] and "pre-existing or unrelated" in out["reason"]
    py_repo.write("app/calc.py", "def add(a, b):\n    return a * b\n")  # Claude's "fix" is still wrong
    again = _stop(py_repo, stop_hook_active=True)
    assert "decision" not in again and again["systemMessage"].startswith("task-handoff: FAILED")


def test_docs_only_turn_is_silent(py_repo):
    hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "s1"}))
    py_repo.write("README.md", "# fixture\n\nmore\n")
    assert _stop(py_repo) is None


def test_hook_mode_runs_targeted_checks_only(py_repo):
    hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "s1"}))
    py_repo.write("app/newmod.py", "X = 1\n")  # no mapped test: full suite would be the fallback
    out = _stop(py_repo)
    assert "pytest" not in out["systemMessage"]
    assert "PARTIALLY VERIFIED" in out["systemMessage"] and "untested: app/newmod.py" in out["systemMessage"]


def test_opt_out_per_repo_and_globally(py_repo, monkeypatch):
    py_repo.write(".task-handoff.json", json.dumps({"hooks": {"enabled": False}}))
    py_repo.commit("opt out")
    assert hooks.run("session-start", json.dumps({"cwd": str(py_repo.root)})) == ("", 0)
    (py_repo.root / ".task-handoff.json").unlink()
    py_repo.commit("opt back in")
    monkeypatch.setenv("TASK_HANDOFF_HOOKS", "0")
    assert hooks.run("session-start", json.dumps({"cwd": str(py_repo.root)})) == ("", 0)


def test_report_only_mode_never_blocks(py_repo):
    py_repo.write(".task-handoff.json", json.dumps({"hooks": {"block_on_failure": False}}))
    py_repo.commit("report only")
    hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "s1"}))
    py_repo.write("app/calc.py", "def add(a, b):\n    return a - b\n")
    assert _stop(py_repo)["systemMessage"].startswith("task-handoff: FAILED")


def test_cli_hook_subprocess(py_repo):
    def call(event, payload):
        return subprocess.run([sys.executable, "-m", "task_handoff.cli", "hook", event], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=120)

    started = call("session-start", {"cwd": str(py_repo.root), "session_id": "cli"})
    assert started.returncode == 0 and "baseline recorded" in started.stdout
    py_repo.write("app/calc.py", "def add(a, b):\n    return a - b\n")
    stopped = call("stop", {"cwd": str(py_repo.root), "session_id": "cli", "stop_hook_active": False})
    assert stopped.returncode == 0 and json.loads(stopped.stdout)["decision"] == "block"
    project = str(Path(hooks.__file__).resolve().parent.parent)
    garbage = subprocess.run([sys.executable, "-m", "task_handoff.cli", "hook", "stop"], input="not json",
                             capture_output=True, text=True, timeout=60, cwd=str(py_repo.root),
                             env={**os.environ, "PYTHONPATH": project})
    assert garbage.returncode == 0


def test_parallel_sessions_keep_their_own_baselines(py_repo):
    hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "A"}))
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # from A\n")
    hooks.run("session-start", json.dumps({"cwd": str(py_repo.root), "session_id": "B"}))  # must not reset A
    py_repo.write("app/util.py", "def ident(x):\n    return x  # from B\n")

    a = _stop(py_repo, session_id="A")
    b = _stop(py_repo, session_id="B")
    assert "python-syntax passed, pytest passed" in a["systemMessage"]
    report_b = build_report(str(py_repo.root), run=False, session_id="B")
    assert [c["path"] for c in report_b["changes"]] == ["app/util.py"]  # A's edit predates B's baseline
    report_a = build_report(str(py_repo.root), run=False, session_id="A")
    assert {c["path"] for c in report_a["changes"]} == {"app/calc.py", "app/util.py"}
    assert b is not None


def test_old_session_baselines_are_pruned(py_repo, monkeypatch):
    monkeypatch.setattr(gitstate, "MAX_SESSION_BASELINES", 3)
    for i in range(6):
        gitstate.record_baseline(py_repo.root, "t", session_id=f"s{i}")
        (gitstate.state_dir(py_repo.root) / "sessions" / f"s{i}.hook.json").write_text("{}")
        os.utime(gitstate.state_dir(py_repo.root) / "sessions" / f"s{i}.json", (1000 + i, 1000 + i))
    gitstate.record_baseline(py_repo.root, "t", session_id="s6")
    names = sorted(p.name for p in (gitstate.state_dir(py_repo.root) / "sessions").iterdir())
    assert names == ["s4.hook.json", "s4.json", "s5.hook.json", "s5.json", "s6.json"]
