import subprocess
import sys
from pathlib import Path

import pytest


def make_python(path: Path) -> Path:
    """Create an executable that behaves like an environment's python (delegates to the test runner's)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n")
    path.chmod(0o755)
    return path


class Repo:
    def __init__(self, root: Path):
        self.root = root

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True).stdout

    def write(self, path: str, content: str) -> None:
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    def commit(self, message: str = "commit") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path / "proj")
    r.root.mkdir()
    r.git("init", "-q")
    r.git("config", "user.email", "test@example.com")
    r.git("config", "user.name", "Test")
    r.git("config", "commit.gpgsign", "false")
    r.write(".gitignore", ".venv/\n__pycache__/\nnode_modules/\n")
    r.write("README.md", "# fixture\n")
    r.commit("init")
    return r


@pytest.fixture
def py_repo(repo: Repo) -> Repo:
    """Python project whose interpreter (the test runner's) has pytest available."""
    make_python(repo.root / ".venv" / "bin" / "python")
    repo.write("pyproject.toml", "[project]\nname='fx'\nversion='0'\n\n[tool.pytest.ini_options]\n")
    repo.write("app/__init__.py", "")
    repo.write("app/calc.py", "def add(a, b):\n    return a + b\n")
    repo.write("app/util.py", "def ident(x):\n    return x\n")
    repo.write("tests/test_calc.py", "from app.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    repo.write("tests/test_util.py", "from app.util import ident\n\ndef test_ident():\n    assert ident(1) == 1\n")
    repo.write("conftest.py", "")
    repo.commit("python project")
    return repo
