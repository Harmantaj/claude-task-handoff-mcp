"""Configuration: built-in defaults, optionally overridden by <repo>/.task-handoff.json."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "verification": {
        "max_parallel_checks": 3,
        "timeout_seconds": 120,
        # When changed source files have no targeted tests, run the whole suite
        # (clearly labelled as a fallback) instead of leaving them untested.
        "full_suite_fallback": True,
    },
    "playwright": {
        "enabled": True,
        "install_automatically": False,  # never honoured as True: we do not install
        "run_all_when_untargeted": False,
    },
    "safety": {"allow_destructive_operations": False},
    "report": {"max_files_listed": 25, "max_output_lines": 12},
}

CONFIG_FILENAME = ".task-handoff.json"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(repo_root: Path) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULTS)
    for candidate in (os.environ.get("TASK_HANDOFF_CONFIG"), repo_root / CONFIG_FILENAME):
        if candidate and Path(candidate).is_file():
            config = _merge(config, json.loads(Path(candidate).read_text()))
    # Installing browsers/deps and destructive operations are never performed,
    # regardless of configuration.
    config["playwright"]["install_automatically"] = False
    config["safety"]["allow_destructive_operations"] = False
    return config
