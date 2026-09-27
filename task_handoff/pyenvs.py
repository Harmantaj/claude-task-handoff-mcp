"""Locate the Python interpreter a project actually uses.

Order: in-repo virtualenv, Poetry environment, conda environment (from environment.yml),
then python3 on PATH. Detection only reads files or asks the tool (`poetry env info`);
it never creates, activates or installs an environment.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IN_REPO_VENVS = (".venv", "venv", "env")


def _bin_python(env_dir: Path) -> Path | None:
    for rel in ("bin/python", "bin/python3"):
        candidate = env_dir / rel
        if candidate.exists():
            return candidate
    return None


def _toml_value(text: str, section: str, key: str) -> str | None:
    """Tiny TOML reader for `key = "value"` inside `[section]` (tomllib needs Python 3.11)."""
    match = re.search(rf"(?m)^\[{re.escape(section)}\][^\n]*\n(.*?)(?=^\[|\Z)", text, re.S)
    if not match:
        return None
    value = re.search(rf"(?m)^{re.escape(key)}\s*=\s*[\"']([^\"']+)[\"']", match.group(1))
    return value.group(1) if value else None


# --------------------------------------------------------------------------- poetry


def is_poetry_project(root: Path) -> bool:
    pyproject = root / "pyproject.toml"
    if (root / "poetry.lock").exists():
        return True
    return pyproject.is_file() and "[tool.poetry" in pyproject.read_text(errors="replace")


def poetry_env_name(name: str, project_dir: Path) -> str:
    """Mirror of poetry's EnvManager.generate_env_name (poetry 1.2 - 2.x)."""
    sanitized = re.sub(r'[ $`!*@"\\\r\n\t]', "_", name.lower())[:42]
    normalized = os.path.normcase(os.path.realpath(str(project_dir)))
    digest = base64.urlsafe_b64encode(hashlib.sha256(normalized.encode()).digest()).decode()[:8]
    return f"{sanitized}-{digest}"


def poetry_virtualenvs_dir() -> Path:
    if os.environ.get("POETRY_VIRTUALENVS_PATH"):
        return Path(os.environ["POETRY_VIRTUALENVS_PATH"]).expanduser()
    if os.environ.get("POETRY_CACHE_DIR"):
        cache = Path(os.environ["POETRY_CACHE_DIR"]).expanduser()
    elif sys.platform == "darwin":
        cache = Path.home() / "Library" / "Caches" / "pypoetry"
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pypoetry"
    return cache / "virtualenvs"


def _poetry_via_cli(root: Path) -> Path | None:
    poetry = shutil.which("poetry")
    if not poetry:
        return None
    try:
        proc = subprocess.run([poetry, "env", "info", "--executable", "--no-interaction"], cwd=root,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = proc.stdout.strip().splitlines()[-1].strip() if proc.returncode == 0 and proc.stdout.strip() else ""
    return Path(path) if path and path != "NA" and Path(path).exists() else None


def _poetry_via_cache(root: Path) -> Path | None:
    text = (root / "pyproject.toml").read_text(errors="replace") if (root / "pyproject.toml").is_file() else ""
    name = _toml_value(text, "tool.poetry", "name") or _toml_value(text, "project", "name")
    if not name:
        return None
    prefix = poetry_env_name(name, root) + "-py"
    base = poetry_virtualenvs_dir()
    if not base.is_dir():
        return None

    def version_key(p: Path):
        return tuple(int(x) for x in re.findall(r"\d+", p.name[len(prefix):]))

    envs = sorted((p for p in base.iterdir() if p.name.startswith(prefix)), key=version_key, reverse=True)
    for env in envs:  # newest Python first, as poetry would typically have activated it
        python = _bin_python(env)
        if python:
            return python
    return None


def poetry_python(root: Path) -> Path | None:
    if not is_poetry_project(root):
        return None
    return _poetry_via_cli(root) or _poetry_via_cache(root)


# --------------------------------------------------------------------------- conda


def conda_env_spec(root: Path) -> tuple[str | None, str | None]:
    """(name, prefix) declared in environment.yml / environment.yaml, if any."""
    for filename in ("environment.yml", "environment.yaml"):
        spec = root / filename
        if spec.is_file():
            text = spec.read_text(errors="replace")
            name = re.search(r"(?m)^name:\s*[\"']?([^\"'#\s]+)", text)
            prefix = re.search(r"(?m)^prefix:\s*[\"']?([^\"'#\n]+?)[\"']?\s*$", text)
            return (name.group(1) if name else None, prefix.group(1).strip() if prefix else None)
    return None, None


def conda_env_dirs() -> list[Path]:
    """Candidate environment directories from conda's registry and the usual install roots."""
    home = Path.home()
    envs: list[Path] = []
    registry = home / ".conda" / "environments.txt"  # conda/mamba record every env they create here
    if registry.is_file():
        envs += [Path(line.strip()) for line in registry.read_text(errors="replace").splitlines() if line.strip()]
    roots = [os.environ.get("CONDA_ENVS_PATH", ""), os.environ.get("CONDA_ENVS_DIRS", "")]
    env_parents = [Path(p).expanduser() for r in roots for p in r.split(os.pathsep) if p]
    for base in ("miniconda3", "anaconda3", "miniforge3", "mambaforge", "micromamba", ".micromamba",
                 "opt/miniconda3", "opt/anaconda3"):
        env_parents.append(home / base / "envs")
    env_parents += [Path("/opt/homebrew/Caskroom/miniconda/base/envs"), Path("/opt/miniconda3/envs"),
                    Path("/opt/conda/envs"), Path("/usr/local/Caskroom/miniconda/base/envs")]
    if os.environ.get("MAMBA_ROOT_PREFIX"):
        env_parents.append(Path(os.environ["MAMBA_ROOT_PREFIX"]) / "envs")
    for parent in env_parents:
        if parent.is_dir():
            envs += [p for p in parent.iterdir() if p.is_dir()]
    return envs


def conda_python(root: Path) -> Path | None:
    name, prefix = conda_env_spec(root)
    if prefix:
        python = _bin_python(Path(prefix).expanduser())
        if python:
            return python
    if not name:
        return None
    for env in conda_env_dirs():
        if env.name == name:
            python = _bin_python(env)
            if python:
                return python
    return None


# --------------------------------------------------------------------------- entry


def find_python(root: Path) -> tuple[str | None, str]:
    """Return (interpreter path, human-readable source)."""
    for name in IN_REPO_VENVS:
        python = _bin_python(root / name)
        if python:
            return str(python), f"in-repo {name}/"
    python = poetry_python(root)
    if python:
        return str(python), "poetry env"
    python = conda_python(root)
    if python:
        return str(python), f"conda env '{conda_env_spec(root)[0] or python.parent.parent.name}'"
    fallback = shutil.which("python3") or shutil.which("python")
    hints = []
    if is_poetry_project(root):
        hints.append("poetry project but no poetry env found")
    if conda_env_spec(root) != (None, None):
        hints.append("environment.yml present but its conda env was not found")
    source = "python3 on PATH" + (f" ({'; '.join(hints)})" if hints else "")
    return fallback, source
