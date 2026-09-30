"""Interpreter detection: in-repo venv, Poetry (CLI and cache), conda (registry, envs dirs, prefix)."""

import os
import shutil

import pytest

from conftest import make_python, posix_shell
from task_handoff import gitstate, pyenvs
from task_handoff.handoff import build_report


@pytest.fixture
def no_poetry_cli(monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(pyenvs.shutil, "which", lambda name, *a, **k: None if name == "poetry" else real_which(name, *a, **k))


def _python_project(repo, pyproject=None, extra=None):
    repo.write("pyproject.toml", pyproject or "[project]\nname = 'plain'\nversion = '0'\n")
    repo.write("app/__init__.py", "")
    repo.write("app/calc.py", "def add(a, b):\n    return a + b\n")
    repo.write("tests/test_calc.py", "from app.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    repo.write("conftest.py", "")
    for path, content in (extra or {}).items():
        repo.write(path, content)
    repo.commit("project")
    gitstate.record_baseline(repo.root, "t")
    repo.write("app/calc.py", "def add(a, b):\n    return a + b  # changed\n")


POETRY_TOML = "[tool.poetry]\nname = \"Demo App\"\nversion = \"0.1.0\"\n\n[tool.pytest.ini_options]\n"


def test_poetry_env_name_matches_real_poetry():
    # Reference value produced by poetry 2.5.1's EnvManager.generate_env_name for these inputs.
    assert pyenvs.poetry_env_name("Demo App", "/opt/no-such-dir/demo-app") == "demo_app-33OaQNpZ"


def test_poetry_env_found_in_cache_without_cli(repo, tmp_path, monkeypatch, no_poetry_cli):
    venvs = tmp_path / "poetry-venvs"
    monkeypatch.setenv("POETRY_VIRTUALENVS_PATH", str(venvs))
    _python_project(repo, POETRY_TOML)
    name = pyenvs.poetry_env_name("Demo App", repo.root)
    old = make_python(venvs / f"{name}-py3.9" / "bin" / "python")
    new = make_python(venvs / f"{name}-py3.12" / "bin" / "python")
    make_python(venvs / "other_project-AAAAAAAA-py3.12" / "bin" / "python")

    python, source = pyenvs.find_python(repo.root)
    assert python == str(new) and python != str(old) and source == "poetry env"
    report = build_report(str(repo.root))
    pytest_check = next(c for c in report["checks"] if c["id"] == "pytest")
    assert pytest_check["outcome"] == "passed" and "python: poetry env" in pytest_check["reason"]


@posix_shell
def test_poetry_cli_is_preferred_when_available(repo, tmp_path, monkeypatch):
    interpreter = make_python(tmp_path / "somewhere" / "bin" / "python")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    poetry = fake_bin / "poetry"
    poetry.write_text(f"#!/bin/sh\n[ \"$1 $2 $3\" = 'env info --executable' ] && echo '{interpreter}' && exit 0\nexit 1\n")
    poetry.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    _python_project(repo, POETRY_TOML)
    assert pyenvs.find_python(repo.root) == (str(interpreter), "poetry env")


def test_in_repo_venv_takes_precedence_over_poetry(repo, tmp_path, monkeypatch, no_poetry_cli):
    venvs = tmp_path / "poetry-venvs"
    monkeypatch.setenv("POETRY_VIRTUALENVS_PATH", str(venvs))
    _python_project(repo, POETRY_TOML)
    make_python(venvs / f"{pyenvs.poetry_env_name('Demo App', repo.root)}-py3.12" / "bin" / "python")
    local = make_python(repo.root / ".venv" / "bin" / "python")
    assert pyenvs.find_python(repo.root) == (str(local), "in-repo .venv/")


def test_poetry_project_without_env_says_so(repo, tmp_path, monkeypatch, no_poetry_cli):
    monkeypatch.setenv("POETRY_VIRTUALENVS_PATH", str(tmp_path / "empty"))
    _python_project(repo, POETRY_TOML)
    _, source = pyenvs.find_python(repo.root)
    assert source == "python3 on PATH (poetry project but no poetry env found)"


def test_conda_env_from_registry(repo, tmp_path, monkeypatch, no_poetry_cli):
    home = tmp_path / "home"
    env_dir = tmp_path / "anywhere" / "shopenv"
    interpreter = make_python(env_dir / "bin" / "python")
    (home / ".conda").mkdir(parents=True)
    (home / ".conda" / "environments.txt").write_text(f"{tmp_path}/anywhere/base\n{env_dir}\n")
    monkeypatch.setenv("HOME", str(home))
    _python_project(repo, extra={"environment.yml": "name: shopenv\nchannels: [conda-forge]\n"})
    python, source = pyenvs.find_python(repo.root)
    assert python == str(interpreter) and source == "conda env 'shopenv'"
    report = build_report(str(repo.root))
    assert next(c for c in report["checks"] if c["id"] == "pytest")["outcome"] == "passed"


def test_conda_env_from_install_root(repo, tmp_path, monkeypatch, no_poetry_cli):
    home = tmp_path / "home"
    interpreter = make_python(home / "miniforge3" / "envs" / "shopenv" / "bin" / "python")
    monkeypatch.setenv("HOME", str(home))
    _python_project(repo, extra={"environment.yaml": "name: \"shopenv\"  # dev env\n"})
    assert pyenvs.find_python(repo.root) == (str(interpreter), "conda env 'shopenv'")


def test_conda_prefix_is_honoured(repo, tmp_path, monkeypatch, no_poetry_cli):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    interpreter = make_python(tmp_path / "envs" / "custom" / "bin" / "python")
    _python_project(repo, extra={"environment.yml": f"name: custom\nprefix: {tmp_path}/envs/custom\n"})
    assert pyenvs.find_python(repo.root)[0] == str(interpreter)


def test_missing_conda_env_is_reported(repo, tmp_path, monkeypatch, no_poetry_cli):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CONDA_ENVS_PATH", raising=False)
    monkeypatch.delenv("MAMBA_ROOT_PREFIX", raising=False)
    _python_project(repo, extra={"environment.yml": "name: nowhere-env-xyz\n"})
    _, source = pyenvs.find_python(repo.root)
    assert "environment.yml present but its conda env was not found" in source


@pytest.mark.skipif(shutil.which("poetry") is None, reason="poetry not installed")
def test_real_poetry_cli_and_cache_agree(repo, tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("POETRY_CACHE_DIR", str(tmp_path / "poetry-cache"))
    monkeypatch.setenv("POETRY_VIRTUALENVS_IN_PROJECT", "false")
    _python_project(repo, POETRY_TOML.replace("[tool.pytest", "package-mode = false\n\n[tool.pytest"))
    subprocess.run(["poetry", "env", "use", "python3"], cwd=repo.root, check=True, capture_output=True)
    via_cli = pyenvs._poetry_via_cli(repo.root)
    via_cache = pyenvs._poetry_via_cache(repo.root)
    assert via_cli is not None and via_cache is not None
    assert via_cli.parent.parent == via_cache.parent.parent


def test_pdm_interpreter_file(repo, tmp_path, no_poetry_cli):
    interpreter = make_python(tmp_path / "pdm-env" / "bin" / "python")
    _python_project(repo, extra={".pdm-python": f"{interpreter}\n"})
    assert pyenvs.find_python(repo.root) == (str(interpreter), "pdm (.pdm-python)")


@posix_shell
def test_hatch_env_via_cli(repo, tmp_path, monkeypatch, no_poetry_cli):
    env_dir = tmp_path / "hatch-envs" / "proj-abc" / "default"
    interpreter = make_python(env_dir / "bin" / "python")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "hatch").write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'env find' ] && echo '{env_dir}' && exit 0\nexit 1\n")
    (fake_bin / "hatch").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    _python_project(repo, "[project]\nname = 'h'\nversion = '0'\n\n[tool.hatch.envs.default]\n")
    assert pyenvs.find_python(repo.root) == (str(interpreter), "hatch env")


def test_pyenv_version_file(repo, tmp_path, monkeypatch, no_poetry_cli):
    monkeypatch.setenv("PYENV_ROOT", str(tmp_path / "pyenv"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    interpreter = make_python(tmp_path / "pyenv" / "versions" / "3.12.4" / "bin" / "python")
    _python_project(repo, extra={".python-version": "3.11.9 3.12.4\n"})  # first missing, second installed
    assert pyenvs.find_python(repo.root) == (str(interpreter), "pyenv 3.12.4")


def test_pyenv_version_missing_is_reported(repo, tmp_path, monkeypatch, no_poetry_cli):
    monkeypatch.setenv("PYENV_ROOT", str(tmp_path / "pyenv"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _python_project(repo, extra={".python-version": "3.99.0\n"})
    assert ".python-version present but that pyenv version is not installed" in pyenvs.find_python(repo.root)[1]
