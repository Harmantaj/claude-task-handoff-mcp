"""Optional Playwright adapter.

Browser checks are proposed only for browser-relevant changes, only when the project
already has Playwright configured, and only against specs that relate to the change.
Playwright and its browsers are never installed by this tool.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .checks import Check, _bin
from .gitstate import FileChange

CONFIG_RE = re.compile(r"(^|/)playwright\.config\.(ts|js|mjs|cjs|mts)$")
SPEC_RE = re.compile(r"\.(spec|e2e|test)\.[cm]?[jt]sx?$")


def is_relevant(changes: list[FileChange]) -> bool:
    return any("frontend" in c.categories or CONFIG_RE.search(c.path) for c in changes
               if c.status not in ("deleted", "reverted"))


def _stem(path: str) -> str:
    name = PurePosixPath(path).name.split(".")[0]
    return re.sub(r"[^a-z0-9]", "", name.lower())


def playwright_checks(root: Path, changes: list[FileChange], all_files: list[str], config: dict) -> list[Check]:
    frontend = [c.path for c in changes if "frontend" in c.categories and c.status not in ("deleted", "reverted")]
    if not is_relevant(changes):
        return []
    if not config["playwright"]["enabled"]:
        return [Check("playwright", "browser", [], str(root), "browser-relevant files changed", frontend,
                      not_tested_reason="Playwright checks disabled in configuration")]

    configs = [f for f in all_files if CONFIG_RE.search(f)]
    if not configs:
        return [Check("playwright", "browser", [], str(root), "browser-relevant files changed", frontend,
                      not_tested_reason="no playwright.config found; browser behaviour is unverified")]

    checks: list[Check] = []
    for cfg in configs:
        pw_dir = (root / cfg).parent
        rel_dir = pw_dir.relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        specs = [f for f in all_files if f.startswith(prefix) and SPEC_RE.search(f)
                 and "node_modules/" not in f and re.search(r"(^|/)(e2e|tests?|playwright|specs?)/", f)]
        changed_specs = [c.path for c in changes if c.path in specs and c.status not in ("deleted", "reverted")]
        stems = {_stem(p) for p in frontend if len(_stem(p)) >= 3}
        related = sorted(set(changed_specs) | {s for s in specs if any(st and st in _stem(s) for st in stems)})
        label = "playwright" if not prefix else f"playwright[{rel_dir}]"
        binary = _bin(pw_dir, root, "playwright")

        if not related and not config["playwright"]["run_all_when_untargeted"]:
            checks.append(Check(label, "browser", [], str(pw_dir), "browser-relevant files changed", frontend,
                                not_tested_reason="no Playwright spec relates to the changed files "
                                                  "(set playwright.run_all_when_untargeted to run the full suite)"))
            continue
        argv = [binary or "playwright", "test", "--reporter=line",
                *[str((root / s).relative_to(pw_dir)) for s in related]]
        checks.append(Check(
            label, "browser", argv, str(pw_dir),
            f"{len(related)} related spec(s)" if related else "full e2e suite (configured)",
            frontend + changed_specs, scope="targeted" if related else "full", runner="playwright",
            blocked_reason=None if binary else "@playwright/test is not installed (not installing automatically)",
        ))
    return checks
