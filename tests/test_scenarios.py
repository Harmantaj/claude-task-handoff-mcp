"""Scenario tests from TESTING_STRATEGY.md, each against a real temporary git repository."""

import json
import shutil

import pytest

from task_handoff import checks as checks_mod
from task_handoff import gitstate
from task_handoff.handoff import build_report, render_markdown


def by_id(report, check_id):
    return next(c for c in report["checks"] if c["id"] == check_id)


def evidence(report, label):
    return [f["text"] for f in report["findings"] if f["evidence"] == label]


# ----------------------------------------------------------------------------- backend


def test_backend_change_runs_only_mapped_tests_and_verifies(py_repo):
    gitstate.record_baseline(py_repo.root, "tweak add")
    py_repo.write("app/calc.py", "def add(a, b):\n    # fast path\n    return a + b\n")
    report = build_report(str(py_repo.root))
    pytest_check = by_id(report, "pytest")
    assert pytest_check["outcome"] == "passed"
    assert pytest_check["scope"] == "targeted"
    assert "tests/test_calc.py" in pytest_check["command"]
    assert "test_util" not in pytest_check["command"]  # never blindly run everything
    assert report["verdict"] == "VERIFIED"
    assert by_id(report, "python-syntax")["outcome"] == "passed"


def test_failing_test_is_reported_as_failed_with_next_prompt(py_repo):
    gitstate.record_baseline(py_repo.root, "break add")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a - b\n")
    report = build_report(str(py_repo.root))
    assert by_id(report, "pytest")["outcome"] == "failed"
    assert report["verdict"] == "FAILED"
    assert "Fix the failing check(s): pytest" in report["next_prompt"]
    assert any("FAIL" in line or "assert" in line for line in by_id(report, "pytest")["excerpt"])


def test_syntax_error_fails(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/util.py", "def ident(x)\n    return x\n")
    report = build_report(str(py_repo.root))
    assert by_id(report, "python-syntax")["outcome"] == "failed"
    assert report["verdict"] == "FAILED"


def test_unmapped_source_falls_back_to_labelled_full_suite(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/newmod.py", "X = 1\n")
    report = build_report(str(py_repo.root))
    check = by_id(report, "pytest")
    assert check["scope"] == "full" and "fallback" in check["reason"]
    assert any("full-suite" in t for t in evidence(report, "INFERRED"))


def test_unmapped_source_without_fallback_is_not_tested(py_repo):
    py_repo.write(".task-handoff.json", json.dumps({"verification": {"full_suite_fallback": False}}))
    py_repo.commit("cfg")
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/newmod.py", "X = 1\n")
    report = build_report(str(py_repo.root))
    assert not any(c["id"] == "pytest" for c in report["checks"])
    assert report["verdict"] == "PARTIALLY VERIFIED"
    assert any("app/newmod.py" in t for t in evidence(report, "NOT TESTED"))


def test_no_tests_collected_is_not_a_pass(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("tests/test_empty.py", "# nothing yet\n")
    report = build_report(str(py_repo.root))
    assert by_id(report, "pytest")["outcome"] == "not_tested"
    assert report["verdict"] != "VERIFIED"


def test_missing_test_dependency_is_blocked(py_repo, monkeypatch):
    monkeypatch.setattr(checks_mod, "_has_module", lambda python, module: False)
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/calc.py", "def add(a, b):\n    return b + a\n")
    report = build_report(str(py_repo.root))
    check = by_id(report, "pytest")
    assert check["outcome"] == "blocked" and "not installed" in check["detail"]
    assert report["verdict"] != "VERIFIED"
    assert "Unblock verification" in report["next_prompt"]


def test_timeout_is_blocked_not_passed(py_repo):
    py_repo.write(".task-handoff.json", json.dumps({"verification": {"timeout_seconds": 2}}))
    py_repo.commit("cfg")
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("tests/test_calc.py", "import time\n\ndef test_slow():\n    time.sleep(30)\n")
    report = build_report(str(py_repo.root))
    check = by_id(report, "pytest")
    assert check["outcome"] == "blocked" and "timed out" in check["detail"]


# ----------------------------------------------------------------------------- attribution


def test_pre_existing_unrelated_changes_are_excluded(py_repo):
    py_repo.write("app/util.py", "def ident(x):\n    return x  # local WIP\n")
    py_repo.write("scratch.txt", "notes\n")
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # task\n")
    report = build_report(str(py_repo.root))
    assert [c["path"] for c in report["changes"]] == ["app/calc.py"]
    assert set(report["pre_existing_unrelated"]) == {"app/util.py", "scratch.txt"}
    assert "Excluded 2 pre-existing" in render_markdown(report)


def test_pre_existing_file_touched_again_is_partial(py_repo):
    py_repo.write("app/util.py", "def ident(x):\n    return x  # WIP\n")
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/util.py", "def ident(x):\n    return x  # WIP + task\n")
    report = build_report(str(py_repo.root))
    change = report["changes"][0]
    assert change["path"] == "app/util.py" and "partial" in change["note"]


def test_discarded_pre_existing_work_is_flagged(py_repo):
    py_repo.write("app/util.py", "def ident(x):\n    return x  # WIP\n")
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.git("checkout", "--", "app/util.py")
    report = build_report(str(py_repo.root))
    assert report["changes"][0]["status"] == "reverted"
    assert any("disappeared" in r for r in report["risks"])


def test_commits_made_during_task_are_attributed(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    py_repo.commit("task commit")
    report = build_report(str(py_repo.root))
    assert [c["path"] for c in report["changes"]] == ["app/calc.py"]
    assert report["baseline"]["commits_since"] == 1
    assert report["verdict"] == "VERIFIED"


def test_without_baseline_attribution_is_inferred(py_repo):
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # x\n")
    report = build_report(str(py_repo.root))
    assert any("No baseline recorded" in t for t in evidence(report, "INFERRED"))
    assert report["changes"][0]["path"] == "app/calc.py"


def test_no_changes(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    report = build_report(str(py_repo.root))
    assert report["verdict"] == "NO CHANGES" and report["checks"] == []


def test_docs_only(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("README.md", "# fixture\n\nMore docs.\n")
    report = build_report(str(py_repo.root))
    assert report["verdict"] == "NO EXECUTABLE CHANGES"
    assert report["changes"][0]["categories"] == ["docs"]


# ----------------------------------------------------------------------------- risk categories


def test_dependency_change_runs_full_suite_and_flags_risk(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("requirements.txt", "requests==2.32.0\n")
    report = build_report(str(py_repo.root))
    assert by_id(report, "pytest")["reason"].startswith("dependencies changed")
    assert any(r.startswith("Dependencies changed") for r in report["risks"])


def test_auth_change_flags_review(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/auth.py", "def check_password(p):\n    return bool(p)\n")
    report = build_report(str(py_repo.root))
    assert any(r.startswith("Authentication") for r in report["risks"])


def test_schema_change_flags_risk(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("migrations/0002_add_col.sql", "ALTER TABLE users ADD COLUMN age int;\n")
    report = build_report(str(py_repo.root))
    assert "schema" in report["changes"][0]["categories"]
    assert any(r.startswith("Schema/migration") for r in report["risks"])


def test_secret_is_detected_but_never_echoed(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    secret = "sk-ant-" + "A1b2C3d4" * 5
    py_repo.write("app/settings.py", f"API_KEY = '{secret}'\n")
    report = build_report(str(py_repo.root))
    dumped = json.dumps(report) + render_markdown(report)
    assert secret not in dumped
    assert any("Possible secret" in t for t in evidence(report, "VERIFIED"))
    assert report["risks"][0].startswith("Possible secrets")


def test_large_diff_is_summarised_by_directory(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    for i in range(40):
        py_repo.write(f"docs/gen/page{i}.md", "x\n" * 60)
    report = build_report(str(py_repo.root))
    md = render_markdown(report)
    assert any(r.startswith("Large change") for r in report["risks"])
    assert "`docs/gen/` 40 files" in md
    assert md.count("\n") < 40  # stays skimmable


# ----------------------------------------------------------------------------- claims


def test_claims_are_labelled_and_cross_checked(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/util.py", "def ident(x):\n    return x  # doc\n")
    report = build_report(str(py_repo.root), run=False,
                          claims=["Updated util.py", "Fixed bug in app/calc.py", "All tests pass"])
    claimed = evidence(report, "CLAIMED")
    inferred = evidence(report, "INFERRED")
    assert claimed == ["Updated util.py", "Fixed bug in app/calc.py", "All tests pass"]
    assert any("'util.py' from the claim was modified" in t for t in inferred)
    assert any("'app/calc.py' but it is not among the changed files" in t for t in inferred)
    assert any("not supported by any test run" in t for t in inferred)
    assert report["verdict"] == "NOT VERIFIED (inspection only)"


# ----------------------------------------------------------------------------- javascript / browser


def _node_project(repo, scripts, deps_installed):
    repo.write("web/package.json", json.dumps({"name": "web", "version": "0.0.0", "scripts": scripts}))
    repo.write("web/src/components/Button.tsx", "export const Button = () => null;\n")
    repo.commit("node project")
    if deps_installed:
        (repo.root / "web" / "node_modules" / ".bin").mkdir(parents=True)


needs_npm = pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")


@needs_npm
def test_frontend_missing_dependencies_is_blocked(repo):
    _node_project(repo, {"test": "vitest run", "lint": "eslint ."}, deps_installed=False)
    gitstate.record_baseline(repo.root, "t")
    repo.write("web/src/components/Button.tsx", "export const Button = () => 'hi';\n")
    report = build_report(str(repo.root))
    for cid in ("test[web]", "lint[web]"):
        check = by_id(report, cid)
        assert check["outcome"] == "blocked" and "node_modules" in check["detail"]
    assert report["verdict"] == "NOT VERIFIED"


@needs_npm
def test_frontend_without_playwright_is_not_tested(repo):
    _node_project(repo, {"test": "node -e \"process.exit(0)\""}, deps_installed=True)
    gitstate.record_baseline(repo.root, "t")
    repo.write("web/src/components/Button.tsx", "export const Button = () => 'hi';\n")
    report = build_report(str(repo.root))
    assert by_id(report, "test[web]")["outcome"] == "passed"
    pw = by_id(report, "playwright")
    assert pw["outcome"] == "not_tested" and "no playwright.config" in pw["detail"]
    assert report["verdict"] == "PARTIALLY VERIFIED"


@needs_npm
def test_playwright_configured_but_unavailable_is_blocked(repo):
    _node_project(repo, {"test": "node -e \"process.exit(0)\""}, deps_installed=True)
    repo.write("web/playwright.config.ts", "export default {};\n")
    repo.write("web/e2e/button.spec.ts", "// e2e for Button\n")
    repo.write("web/e2e/checkout.spec.ts", "// unrelated\n")
    repo.commit("pw")
    gitstate.record_baseline(repo.root, "t")
    repo.write("web/src/components/Button.tsx", "export const Button = () => 'hi';\n")
    report = build_report(str(repo.root))
    pw = by_id(report, "playwright[web]")
    assert pw["outcome"] == "blocked" and "not installing" in pw["detail"]
    assert "button.spec.ts" in pw["command"] and "checkout" not in pw["command"]  # targeted


@needs_npm
def test_build_failure_after_dependency_change(repo):
    _node_project(repo, {"build": "node -e \"console.error('Build error: boom'); process.exit(1)\""},
                  deps_installed=True)
    gitstate.record_baseline(repo.root, "t")
    manifest = json.loads((repo.root / "web/package.json").read_text())
    manifest["dependencies"] = {"left-pad": "1.3.0"}
    repo.write("web/package.json", json.dumps(manifest))
    report = build_report(str(repo.root))
    build = by_id(report, "build[web]")
    assert build["outcome"] == "failed"
    assert report["verdict"] == "FAILED"
    assert any("boom" in line for line in build["excerpt"])


def test_backend_only_change_proposes_no_browser_check(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # y\n")
    report = build_report(str(py_repo.root), run=False)
    assert not any(c["kind"] == "browser" for c in report["checks"])


def test_placeholders_and_docs_do_not_raise_false_alarms(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/tmpl.py", "A = f\"password = '{value_here}'\"\nB = \"token: '${SECRET_TOKEN}'\"\n")
    py_repo.write("docs/SECURITY.md", "# Security\n")
    report = build_report(str(py_repo.root), run=False)
    assert report["changes"] and not evidence(report, "VERIFIED")  # no secret finding
    assert not any(r.startswith("Authentication") for r in report["risks"])
    assert "auth" not in next(c for c in report["changes"] if c["path"] == "docs/SECURITY.md")["categories"]


def test_shell_script_gets_syntax_check_but_is_not_fully_verified(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("scripts/deploy.sh", "#!/bin/bash\necho ok\n")
    report = build_report(str(py_repo.root))
    assert by_id(report, "bash-syntax")["outcome"] == "passed"
    assert report["verdict"] == "PARTIALLY VERIFIED"
    assert any("scripts/deploy.sh" in t for t in evidence(report, "NOT TESTED"))


def test_shell_syntax_error_fails(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("scripts/deploy.sh", "#!/bin/bash\nif true; then\necho ok\n")
    report = build_report(str(py_repo.root))
    assert by_id(report, "bash-syntax")["outcome"] == "failed"
    assert report["verdict"] == "FAILED"


def test_inspection_next_prompt_names_selected_checks(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # z\n")
    report = build_report(str(py_repo.root), run=False)
    assert report["next_prompt"] == "Call verify_task to run 2 selected check(s): python-syntax, pytest."


def test_generated_files_in_one_directory_collapse_to_one_line(py_repo):
    gitstate.record_baseline(py_repo.root, "t")
    for i in range(6):
        py_repo.write(f"db/reports/r{i}.json", "{}\n")
    py_repo.write("app/calc.py", "def add(a, b):\n    return a + b  # z\n")
    md = render_markdown(build_report(str(py_repo.root), run=False))
    assert "- `db/reports/` 6 non-code files (untracked) +6/-0" in md
    assert "r3.json" not in md and "`app/calc.py` modified" in md
