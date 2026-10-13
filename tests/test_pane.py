import json

from task_handoff import gitstate
from task_handoff.cli import main
from task_handoff.handoff import build_report
from task_handoff.pane import pane_data


def test_pane_diff_covers_attributed_changes_only(repo):
    repo.write("notes.txt", "mine, before the session\n")
    gitstate.record_baseline(repo.root, "task", session_id="s1")
    repo.write("README.md", "# fixture\nmore\n")
    repo.write("new.py", "x = 1\n")

    data = pane_data(str(repo.root), "s1")

    assert data["baseline"]["is_session"] is True
    assert {c["path"] for c in data["changes"]} == {"README.md", "new.py"}
    assert data["pre_existing_unrelated"] == ["notes.txt"]
    assert "+more" in data["diff"] and "+x = 1" in data["diff"]
    assert "notes.txt" not in data["diff"]
    assert data["report"] is None and data["stale"] is False


def test_pane_is_read_only_and_marks_stale_reports(repo):
    gitstate.record_baseline(repo.root, "task")
    repo.write("a.md", "one\n")
    report = build_report(str(repo.root), run=False)
    saved = (gitstate.state_dir(repo.root) / "last_report.json").read_text()

    assert pane_data(str(repo.root))["stale"] is False
    repo.write("a.md", "two\n")
    data = pane_data(str(repo.root))

    assert data["stale"] is True
    assert data["report"]["verdict"] == report["verdict"]
    assert (gitstate.state_dir(repo.root) / "last_report.json").read_text() == saved


def test_pane_redacts_secrets_and_cuts_at_file_boundaries(repo):
    gitstate.record_baseline(repo.root, "task")
    repo.write("a.py", "token = 'ghp_" + "a" * 36 + "'\n")
    repo.write("b.py", "y = 2\n" * 400)

    data = pane_data(str(repo.root), max_diff_chars=1000)

    assert "ghp_" not in data["diff"] and "[REDACTED" in data["diff"]
    assert data["diff_omitted"] == ["b.py"]
    assert data["diff"].count("diff --git") == 1


def test_pane_cli_prints_json(repo, capsys):
    gitstate.record_baseline(repo.root, "task")
    repo.write("README.md", "# changed\n")

    assert main(["pane", str(repo.root)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "proj" and data["changes"][0]["path"] == "README.md"
