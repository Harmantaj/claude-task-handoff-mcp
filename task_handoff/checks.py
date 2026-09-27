"""Impact-aware check selection.

Checks are chosen from project metadata and the changed files. Nothing is installed:
a missing tool or missing dependencies produces a BLOCKED check, never a silent skip.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .gitstate import FileChange
from .impact import language
from .pyenvs import find_python

KIND_STRENGTH = {"syntax": 1, "lint": 2, "typecheck": 2, "build": 2, "test": 3, "browser": 3}


@dataclass
class Check:
    id: str
    kind: str  # syntax | lint | typecheck | build | test | browser
    argv: list[str]
    cwd: str
    reason: str
    covers: list[str] = field(default_factory=list)  # changed paths this check provides evidence for
    scope: str = "targeted"  # targeted | full
    runner: str = "generic"  # used to interpret exit codes (pytest, playwright, ...)
    blocked_reason: str | None = None
    not_tested_reason: str | None = None

    def display(self) -> str:
        return " ".join(self.argv) if self.argv else self.id

    def to_dict(self) -> dict:
        return asdict(self)


def _exists_any(root: Path, names: list[str]) -> Path | None:
    for name in names:
        if (root / name).exists():
            return root / name
    return None


# --------------------------------------------------------------------------- python


def _has_module(python: str, module: str) -> bool:
    try:
        return subprocess.run(
            [python, "-c", f"import {module}"], capture_output=True, stdin=subprocess.DEVNULL, timeout=30
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pyproject_has(root: Path, section: str) -> bool:
    py = root / "pyproject.toml"
    return py.is_file() and f"[{section}" in py.read_text(errors="replace")


def _pytest_configured(root: Path, files: list[str]) -> bool:
    if _exists_any(root, ["pytest.ini", "conftest.py", "tox.ini"]) or _pyproject_has(root, "tool.pytest"):
        return True
    cfg = root / "setup.cfg"
    if cfg.is_file() and "[tool:pytest]" in cfg.read_text(errors="replace"):
        return True
    return any(re.search(r"(^|/)(test_[^/]+|[^/]+_test)\.py$", f) for f in files)


def _python_checks(root: Path, changes: list[FileChange], all_files: list[str], config: dict) -> list[Check]:
    live = [c for c in changes if c.status != "deleted" and c.status != "reverted"]
    py_changed = [c.path for c in live if c.path.endswith(".py")]
    deps_changed = any(
        "dependency" in c.categories and PurePosixPath(c.path).name in
        {"pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock"}
        or re.match(r"requirements.*\.(txt|in)$", PurePosixPath(c.path).name)
        for c in changes
    )
    conftest_changed = any(PurePosixPath(c.path).name == "conftest.py" for c in changes)
    if not py_changed and not deps_changed:
        return []

    python, source = find_python(root)
    checks: list[Check] = []
    if python is None:
        return [Check("python-syntax", "syntax", [], str(root), "Python files changed", py_changed,
                      blocked_reason="no python interpreter found on PATH")]
    via = f" · python: {source}"

    if py_changed:
        checks.append(Check(
            "python-syntax", "syntax",
            [python, "-c", "import ast,sys\nfor f in sys.argv[1:]: ast.parse(open(f,'rb').read(), f)", *py_changed],
            str(root), f"{len(py_changed)} Python file(s) changed (read-only parse){via}", py_changed,
        ))

    py_all = [f for f in all_files if f.endswith(".py")]
    if _pytest_configured(root, py_all):
        test_files = set(f for f in py_all if re.search(r"(^|/)(test_[^/]+|[^/]+_test)\.py$", f))
        targets: set[str] = set()
        covered: set[str] = set()
        unmapped: list[str] = []
        for path in py_changed:
            if path in test_files:
                targets.add(path)
                covered.add(path)
                continue
            stem = PurePosixPath(path).stem
            mapped = [t for t in test_files if PurePosixPath(t).name in (f"test_{stem}.py", f"{stem}_test.py")]
            if mapped:
                targets.update(mapped)
                covered.add(path)
            elif PurePosixPath(path).name not in ("conftest.py", "__init__.py", "setup.py"):
                unmapped.append(path)

        argv_base = [python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
        full_needed = deps_changed or conftest_changed or (unmapped and config["verification"]["full_suite_fallback"])
        blocked = None if _has_module(python, "pytest") else f"pytest is not installed for {python} ({source})"
        if full_needed and test_files:
            why = (
                "dependencies changed" if deps_changed else
                "conftest.py changed" if conftest_changed else
                f"no targeted tests found for {len(unmapped)} changed file(s); running full suite as fallback"
            )
            checks.append(Check("pytest", "test", argv_base, str(root), why + via, py_changed, scope="full",
                                runner="pytest", blocked_reason=blocked))
        elif targets:
            checks.append(Check("pytest", "test", [*argv_base, *sorted(targets)], str(root),
                                f"tests mapped to changed files ({len(targets)} test file(s)){via}",
                                sorted(covered), runner="pytest", blocked_reason=blocked))
        elif not test_files and py_changed:
            checks.append(Check("pytest", "test", [], str(root), "pytest configured", py_changed,
                                not_tested_reason="no Python test files exist in the repository"))

    if py_changed and (_exists_any(root, ["ruff.toml", ".ruff.toml"]) or _pyproject_has(root, "tool.ruff")):
        ruff = str(Path(python).parent / "ruff") if (Path(python).parent / "ruff").exists() else shutil.which("ruff")
        checks.append(Check("ruff", "lint", [ruff or "ruff", "check", "--no-cache", *py_changed], str(root),
                            "ruff is configured", py_changed,
                            blocked_reason=None if ruff else "ruff configured but not installed"))
    if py_changed and (_exists_any(root, ["mypy.ini", ".mypy.ini"]) or _pyproject_has(root, "tool.mypy")):
        blocked = None if _has_module(python, "mypy") else f"mypy configured but not installed for {python} ({source})"
        checks.append(Check("mypy", "typecheck", [python, "-m", "mypy", "--no-incremental", *py_changed],
                            str(root), "mypy is configured", py_changed, blocked_reason=blocked))
    return checks


# --------------------------------------------------------------------------- javascript


def _package_dir(root: Path, path: str) -> Path | None:
    current = (root / path).parent
    while True:
        if (current / "package.json").is_file():
            return current
        if current == root or root not in current.parents:
            return None
        current = current.parent


def _package_manager(pkg_dir: Path, root: Path) -> str:
    for directory in (pkg_dir, root):
        if (directory / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (directory / "yarn.lock").exists():
            return "yarn"
        if (directory / "bun.lockb").exists() or (directory / "bun.lock").exists():
            return "bun"
    return "npm"


def _bin(pkg_dir: Path, root: Path, name: str) -> str | None:
    for directory in (pkg_dir, root):
        candidate = directory / "node_modules" / ".bin" / name
        if candidate.exists():
            return str(candidate)
    return None


def _js_checks(root: Path, changes: list[FileChange], config: dict) -> list[Check]:
    groups: dict[Path, list[FileChange]] = {}
    for change in changes:
        name = PurePosixPath(change.path).name
        if language(change.path) == "javascript" or name in {"package.json", "package-lock.json", "yarn.lock",
                                                                "pnpm-lock.yaml", "tsconfig.json"} \
                or ("frontend" in change.categories and change.status != "deleted"):
            pkg = _package_dir(root, change.path)
            if pkg:
                groups.setdefault(pkg, []).append(change)

    checks: list[Check] = []
    for pkg_dir, pkg_changes in groups.items():
        rel = pkg_dir.relative_to(root).as_posix()
        label = "" if rel == "." else f"[{rel}]"
        manifest = json.loads((pkg_dir / "package.json").read_text() or "{}")
        scripts: dict[str, str] = manifest.get("scripts", {})
        pm = _package_manager(pkg_dir, root)
        covers = [c.path for c in pkg_changes]
        code = [c.path for c in pkg_changes if language(c.path) == "javascript" and c.status not in ("deleted", "reverted")]
        deps_or_config = any({"dependency", "config"} & set(c.categories) for c in pkg_changes)

        blocked = None
        if not shutil.which(pm):
            blocked = f"{pm} is not installed"
        elif not ((pkg_dir / "node_modules").is_dir() or (root / "node_modules").is_dir()):
            blocked = f"dependencies not installed (no node_modules in {rel}); not installing automatically"

        def run_script(name: str) -> list[str]:
            return [pm, "run", name] if pm != "yarn" else [pm, name]

        # Type checking
        type_script = next((s for s in ("typecheck", "type-check", "tsc", "check-types") if s in scripts), None)
        if type_script:
            checks.append(Check(f"typecheck{label}", "typecheck", run_script(type_script), str(pkg_dir),
                                f"'{type_script}' script", covers, scope="full", blocked_reason=blocked))
        elif (pkg_dir / "tsconfig.json").exists() and any(p.endswith((".ts", ".tsx")) for p in code):
            tsc = _bin(pkg_dir, root, "tsc")
            checks.append(Check(f"tsc{label}", "typecheck", [tsc or "tsc", "--noEmit"], str(pkg_dir),
                                "TypeScript files changed", covers, scope="full",
                                blocked_reason=blocked or (None if tsc else "typescript not installed locally")))

        if "lint" in scripts and code:
            checks.append(Check(f"lint{label}", "lint", run_script("lint"), str(pkg_dir), "'lint' script",
                                covers, scope="full", blocked_reason=blocked))

        test_script = scripts.get("test", "")
        placeholder = "no test specified" in test_script
        if test_script and not placeholder and (code or deps_or_config):
            rel_code = [str((root / p).relative_to(pkg_dir)) for p in code]
            if "vitest" in test_script and code and _bin(pkg_dir, root, "vitest"):
                argv, scope, why = [_bin(pkg_dir, root, "vitest"), "related", "--run", *rel_code], "targeted", \
                    "vitest tests related to changed files"
            elif "jest" in test_script and code and _bin(pkg_dir, root, "jest"):
                argv, scope, why = [_bin(pkg_dir, root, "jest"), "--findRelatedTests", "--passWithNoTests", *rel_code], \
                    "targeted", "jest tests related to changed files"
            else:
                argv, scope, why = run_script("test"), "full", "'test' script (runner cannot be targeted)"
            checks.append(Check(f"test{label}", "test", argv, str(pkg_dir), why, covers, scope=scope,
                                blocked_reason=blocked))
        elif code and (not test_script or placeholder):
            checks.append(Check(f"test{label}", "test", [], str(pkg_dir), "JS/TS files changed", covers,
                                not_tested_reason="package.json has no real 'test' script"))

        if "build" in scripts and deps_or_config:
            checks.append(Check(f"build{label}", "build", run_script("build"), str(pkg_dir),
                                "dependencies/build config changed", covers, scope="full", blocked_reason=blocked))
    return checks


# --------------------------------------------------------------------------- go


def _go_checks(root: Path, changes: list[FileChange]) -> list[Check]:
    go_files = [c.path for c in changes if c.path.endswith(".go") and c.status not in ("deleted", "reverted")]
    mod_changed = any(PurePosixPath(c.path).name in ("go.mod", "go.sum") for c in changes)
    if not (go_files or mod_changed) or not (root / "go.mod").exists():
        return []
    packages = sorted({"./" + str(PurePosixPath(f).parent) if str(PurePosixPath(f).parent) != "." else "."
                       for f in go_files}) or ["./..."]
    blocked = None if shutil.which("go") else "go toolchain not installed"
    return [
        Check("go-vet", "lint", ["go", "vet", *packages], str(root), "Go files changed", go_files, blocked_reason=blocked),
        Check("go-test", "test", ["go", "test", *packages], str(root), "tests in changed Go packages",
              go_files, scope="full" if packages == ["./..."] else "targeted", blocked_reason=blocked),
    ]


# --------------------------------------------------------------------------- shell


def _shell_checks(root: Path, changes: list[FileChange]) -> list[Check]:
    scripts = [c.path for c in changes if language(c.path) == "shell" and c.status not in ("deleted", "reverted")]
    if not scripts:
        return []
    bash = shutil.which("bash")
    checks = [Check("bash-syntax", "syntax",
                    [bash or "bash", "-c", 'for f; do bash -n "$f" || exit 1; done', "_", *scripts], str(root),
                    f"{len(scripts)} shell script(s) changed (parse only, not executed)", scripts,
                    blocked_reason=None if bash else "bash not found")]
    if shutil.which("shellcheck"):
        checks.append(Check("shellcheck", "lint", ["shellcheck", *scripts], str(root), "shellcheck is installed",
                            scripts))
    return checks


# --------------------------------------------------------------------------- entry


def select_checks(root: Path, changes: list[FileChange], all_files: list[str], config: dict) -> list[Check]:
    from .playwright_adapter import playwright_checks

    checks = _python_checks(root, changes, all_files, config)
    checks += _js_checks(root, changes, config)
    checks += _go_checks(root, changes)
    checks += _shell_checks(root, changes)
    checks += playwright_checks(root, changes, all_files, config)
    return checks
