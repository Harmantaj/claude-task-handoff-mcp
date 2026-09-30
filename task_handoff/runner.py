"""Run selected checks with timeouts and turn raw output into compact, redacted evidence."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from .checks import Check
from .redact import redact

# Outcomes. A check that did not demonstrably pass is never reported as passed.
PASSED, FAILED, BLOCKED, NOT_TESTED = "passed", "failed", "blocked", "not_tested"
WINDOWS = sys.platform == "win32"


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a timed-out check together with everything it spawned (test workers, browsers)."""
    if WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       stdin=subprocess.DEVNULL)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

SIGNAL_LINE = re.compile(
    r"(?i)(\bfail(ed|ure|ing)?\b|\berror\b|assert|exception|traceback|✘|✗|×|\bpassed\b|\btests?\b.*\b\d+\b|"
    r"\bok\b\s+\S+|panic:)"
)
PW_BROWSERS_MISSING = re.compile(r"Executable doesn't exist|npx playwright install")


@dataclass
class CheckResult:
    id: str
    kind: str
    command: str
    reason: str
    scope: str
    covers: list[str]
    outcome: str
    detail: str  # one-line explanation
    exit_code: int | None = None
    duration_s: float = 0.0
    excerpt: list[str] | None = None  # a few redacted, relevant output lines

    def to_dict(self) -> dict:
        return asdict(self)


def _excerpt(output: str, max_lines: int) -> list[str]:
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    picked = [ln for ln in lines if SIGNAL_LINE.search(ln)]
    chosen = (picked[-max_lines:] if picked else lines[-max_lines:])
    return [redact(ln[:200]) for ln in chosen]


def _summary_line(output: str) -> str:
    for line in reversed(output.splitlines()):
        line = line.strip().strip("=").strip()
        if re.search(r"\d+ (passed|failed|error)|Tests?:|\bpassed\b|\bfailed\b", line):
            return redact(line[:160])
    return ""


def run_check(check: Check, timeout: int, max_lines: int) -> CheckResult:
    base = dict(id=check.id, kind=check.kind, command=redact(check.display()), reason=check.reason,
                scope=check.scope, covers=check.covers)
    if check.blocked_reason:
        return CheckResult(**base, outcome=BLOCKED, detail=check.blocked_reason)
    if check.not_tested_reason or not check.argv:
        return CheckResult(**base, outcome=NOT_TESTED, detail=check.not_tested_reason or "no command available")

    env = {**os.environ, "CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    argv = list(check.argv)
    if WINDOWS:  # npm, npx, yarn... are .cmd shims that CreateProcess only finds by full path
        argv[0] = shutil.which(argv[0]) or argv[0]
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, cwd=check.cwd, env=env, stdin=subprocess.DEVNULL,  # never touch MCP stdio
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}),
        )
    except FileNotFoundError:
        return CheckResult(**base, outcome=BLOCKED, detail=f"executable not found: {check.argv[0]}")
    except PermissionError:
        return CheckResult(**base, outcome=BLOCKED, detail=f"executable not runnable: {check.argv[0]}")
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        output, _ = proc.communicate()
        return CheckResult(**base, outcome=BLOCKED, detail=f"timed out after {timeout}s (result unknown)",
                           duration_s=round(time.monotonic() - start, 1), excerpt=_excerpt(output or "", max_lines))
    duration = round(time.monotonic() - start, 1)
    code = proc.returncode
    excerpt = _excerpt(output, max_lines)
    summary = _summary_line(output)

    if code == 0:
        if check.runner in ("playwright", "vitest", "jest") and re.search(r"(?i)no tests? (files )?found", output):
            return CheckResult(**base, outcome=NOT_TESTED, detail="no tests matched", exit_code=code,
                               duration_s=duration, excerpt=excerpt)
        return CheckResult(**base, outcome=PASSED, detail=summary or "exit code 0", exit_code=code,
                           duration_s=duration, excerpt=None)
    if check.runner == "pytest":
        if code == 5:
            return CheckResult(**base, outcome=NOT_TESTED, detail="pytest collected no tests", exit_code=code,
                               duration_s=duration, excerpt=excerpt)
        if code in (3, 4):
            return CheckResult(**base, outcome=BLOCKED, detail=f"pytest could not run (exit {code})",
                               exit_code=code, duration_s=duration, excerpt=excerpt)
    if code in (126, 127):  # shell: command not found / not executable
        return CheckResult(**base, outcome=BLOCKED, detail="a required executable is missing", exit_code=code,
                           duration_s=duration, excerpt=excerpt)
    if check.runner == "playwright" and PW_BROWSERS_MISSING.search(output):
        return CheckResult(**base, outcome=BLOCKED, detail="Playwright browsers are not installed", exit_code=code,
                           duration_s=duration, excerpt=excerpt)
    return CheckResult(**base, outcome=FAILED, detail=summary or f"exit code {code}", exit_code=code,
                       duration_s=duration, excerpt=excerpt)


def run_checks(checks: list[Check], config: dict) -> list[CheckResult]:
    verification = config["verification"]
    max_lines = config["report"]["max_output_lines"]
    workers = max(1, int(verification["max_parallel_checks"]))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_check, c, int(verification["timeout_seconds"]), max_lines) for c in checks]
        return [f.result() for f in futures]
