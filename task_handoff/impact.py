"""Change-impact analysis: classify changed paths into categories and risk flags."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

FRONTEND_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".astro", ".css", ".scss", ".sass", ".less", ".html"}
FRONTEND_DIRS = {"frontend", "web", "client", "ui", "components", "pages", "views", "public", "static", "app"}
JS_EXT = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}
BACKEND_EXT = {".py", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".h", ".swift"}
SHELL_EXT = {".sh", ".bash"}
DOC_EXT = {".md", ".rst", ".txt", ".adoc"}

DEPENDENCY_FILES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
    "pyproject.toml", "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock", "composer.json",
    "composer.lock", "pom.xml", "build.gradle", "build.gradle.kts",
}
CONFIG_NAMES = re.compile(
    r"(^|/)(Dockerfile[^/]*|docker-compose[^/]*|Makefile|tsconfig[^/]*\.json|\.eslintrc[^/]*|"
    r"eslint\.config\.[^/]+|(vite|webpack|next|nuxt|rollup|babel|jest|vitest|playwright|tailwind|postcss)"
    r"\.config\.[^/]+|\.github/.+|\.gitlab-ci\.yml|[^/]+\.ya?ml|[^/]+\.ini|conftest\.py|\.task-handoff\.json)$"
)
SCHEMA = re.compile(r"(^|/)(migrations?|alembic|schema|prisma|db/migrate)(/|$)|\.sql$|schema\.(prisma|graphql|rb)$|(^|/)models?\.py$")
AUTH = re.compile(r"(?i)(auth|login|logout|session|permission|jwt|oauth|passw|rbac|acl|credential|token|security|crypto)")
TEST = re.compile(
    r"(^|/)(tests?|__tests__|spec|e2e|playwright)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|"
    r"\.(test|spec|e2e)\.[cm]?[jt]sx?$"
)
SECRET_FILE = re.compile(r"(^|/)(\.env(\.[^/]*)?|[^/]+\.(pem|key|p12|pfx)|id_(rsa|ed25519|ecdsa)|credentials\.json|secrets?\.(json|ya?ml))$")
SAFE_ENV = re.compile(r"\.env\.(example|sample|template|dist)$")


def language(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix
    if suffix == ".py":
        return "python"
    if suffix in JS_EXT | {".tsx", ".jsx", ".vue", ".svelte"}:
        return "javascript"
    if suffix == ".go":
        return "go"
    if suffix in SHELL_EXT:
        return "shell"
    return None


def categorize(path: str) -> list[str]:
    p = PurePosixPath(path)
    name, suffix = p.name, p.suffix
    parts = {part.lower() for part in p.parts[:-1]}
    cats: list[str] = []

    if TEST.search(path):
        cats.append("test")
    if suffix in FRONTEND_EXT or (suffix in JS_EXT and parts & FRONTEND_DIRS):
        cats.append("frontend")
    elif suffix in BACKEND_EXT or suffix in JS_EXT:
        cats.append("backend")
    elif suffix in SHELL_EXT:
        cats.append("script")
    if name in DEPENDENCY_FILES or re.match(r"requirements.*\.(txt|in)$", name):
        cats.append("dependency")
    if CONFIG_NAMES.search(path):
        cats.append("config")
    is_doc = suffix in DOC_EXT or "docs" in parts
    if SCHEMA.search(path) and not is_doc:
        cats.append("schema")
    if AUTH.search(path) and not is_doc:
        cats.append("auth")
    if SECRET_FILE.search(path) and not SAFE_ENV.search(path):
        cats.append("secret-file")
    if is_doc:
        if not cats or cats == ["test"]:
            cats.append("docs")
    return cats or ["other"]


RISK_TEXT = {
    "auth": "Authentication/authorization code changed - needs human review even if checks pass.",
    "schema": "Schema/migration changed - verify migration safety and rollback; not executed here.",
    "dependency": "Dependencies changed - lockfile/install consistency not verified unless a build/install check passed.",
    "secret-file": "A credential-like file changed - make sure it is not committed.",
    "config": "Build/CI/tooling configuration changed - behaviour outside tested paths may differ.",
}


def risk_flags(changes) -> list[str]:
    """Human-readable risk notes derived purely from categories and change shape."""
    flags: list[str] = []
    for category, text in RISK_TEXT.items():
        paths = [c.path for c in changes if category in c.categories]
        if paths:
            shown = ", ".join(paths[:3]) + (f" (+{len(paths) - 3} more)" if len(paths) > 3 else "")
            flags.append(f"{text} [{shown}]")
    deleted = [c.path for c in changes if c.status == "deleted"]
    if deleted:
        flags.append(f"{len(deleted)} file(s) deleted: {', '.join(deleted[:3])}{' ...' if len(deleted) > 3 else ''}")
    reverted = [c.path for c in changes if c.status == "reverted"]
    if reverted:
        flags.append(
            f"Pre-existing uncommitted work disappeared during the task: {', '.join(reverted[:3])}"
        )
    return flags
