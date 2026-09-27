"""Evidence classification, verdicts, and the compact handoff.

Every statement in a report carries one evidence label:
  VERIFIED   - observed directly (git state, or a check we ran and saw pass/fail)
  CLAIMED    - asserted by the agent/user, not independently checked
  INFERRED   - follows from verified facts by a stated rule
  NOT TESTED - relevant, but no check exercised it
  BLOCKED    - a check was attempted/needed but could not run
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path, PurePosixPath

from . import gitstate
from .checks import KIND_STRENGTH, select_checks
from .impact import categorize, language, risk_flags
from .runner import BLOCKED, FAILED, NOT_TESTED, PASSED, CheckResult, run_checks

VERIFIED, CLAIMED, INFERRED, NOT_TESTED_L, BLOCKED_L = "VERIFIED", "CLAIMED", "INFERRED", "NOT TESTED", "BLOCKED"
CODE_CATEGORIES = {"backend", "frontend", "test", "script"}


def _finding(label: str, text: str) -> dict:
    return {"evidence": label, "text": text}


def inspect(repo_path: str) -> tuple[Path, dict, "gitstate.ChangeSet"]:
    root = gitstate.repo_root(repo_path)
    from .config import load_config

    config = load_config(root)
    baseline = gitstate.load_baseline(root)
    changes = gitstate.collect_changes(root, baseline)
    for change in changes.attributed:
        change.categories = categorize(change.path)
    return root, config, changes


def _is_code(change) -> bool:
    return bool(CODE_CATEGORIES & set(change.categories)) and change.status not in ("deleted", "reverted") \
        and language(change.path) is not None


def build_report(repo_path: str, *, run: bool = True, claims: list[str] | None = None,
                 notes: str = "") -> dict:
    root, config, changes = inspect(repo_path)
    baseline = gitstate.load_baseline(root)
    attributed = changes.attributed
    findings: list[dict] = []

    # --- facts from git -------------------------------------------------------------
    for note in changes.notes:
        findings.append(_finding(INFERRED, note))
    for secret in changes.secrets:
        findings.append(_finding(VERIFIED, f"Possible secret added in {secret['path']} "
                                           f"({', '.join(secret['patterns'])}); value withheld."))

    # --- checks ---------------------------------------------------------------------
    results: list[CheckResult] = []
    if attributed:
        selected = select_checks(root, attributed, gitstate.tracked_files(root), config)
        if run:
            results = run_checks(selected, config)
        else:
            results = [CheckResult(c.id, c.kind, c.display(), c.reason, c.scope, c.covers,
                                   BLOCKED if c.blocked_reason else NOT_TESTED,
                                   c.blocked_reason or c.not_tested_reason or "selected, not run (dry run)")
                       for c in selected]

    for r in results:
        if r.outcome == PASSED:
            findings.append(_finding(VERIFIED, f"{r.id} passed ({r.detail}; {r.scope})"))
        elif r.outcome == FAILED:
            findings.append(_finding(VERIFIED, f"{r.id} FAILED ({r.detail})"))
        elif r.outcome == BLOCKED:
            findings.append(_finding(BLOCKED_L, f"{r.id}: {r.detail}"))
        else:
            findings.append(_finding(NOT_TESTED_L, f"{r.id}: {r.detail}"))

    # --- coverage: which changed code has behavioural evidence ---------------------
    strongest: dict[str, int] = {}
    for r in results:
        if r.outcome == PASSED:
            for path in r.covers:
                strongest[path] = max(strongest.get(path, 0), KIND_STRENGTH.get(r.kind, 0))
    code_changes = [c for c in attributed if _is_code(c)]
    untested = [c.path for c in code_changes if strongest.get(c.path, 0) < KIND_STRENGTH["test"]]
    if untested and run:
        shown = ", ".join(untested[:6]) + (f" (+{len(untested) - 6} more)" if len(untested) > 6 else "")
        findings.append(_finding(NOT_TESTED_L, f"No passing test exercised: {shown}"))
    full_only = [r.id for r in results if r.outcome == PASSED and r.kind == "test" and r.scope == "full"]
    if full_only:
        findings.append(_finding(INFERRED, f"Coverage via full-suite run ({', '.join(full_only)}): passing does not "
                                           "prove the changed lines are exercised."))
    frontend = [c for c in attributed if "frontend" in c.categories and c.status != "deleted"]
    if frontend and not any(r.kind == "browser" and r.outcome == PASSED for r in results):
        if not any(r.kind == "browser" for r in results):
            findings.append(_finding(NOT_TESTED_L, "UI changes have no browser verification."))

    # --- claims ---------------------------------------------------------------------
    changed_paths = {c.path for c in attributed}
    for claim in claims or []:
        findings.append(_finding(CLAIMED, claim))
        mentioned = set(re.findall(r"[\w./-]+\.[A-Za-z0-9]{1,6}\b", claim))
        for path in mentioned:
            hits = [p for p in changed_paths if p == path or p.endswith("/" + path)]
            if hits:
                findings.append(_finding(INFERRED, f"'{path}' from the claim was modified ({hits[0]})."))
            elif "/" in path or PurePosixPath(path).suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".md"}:
                findings.append(_finding(INFERRED, f"Claim mentions '{path}' but it is not among the changed files."))
        if re.search(r"(?i)\ball tests pass|tests? (are )?passing|\bverified\b", claim):
            if not any(r.kind == "test" and r.outcome == PASSED for r in results):
                findings.append(_finding(INFERRED, "Claim of passing tests is not supported by any test run here."))

    risks = risk_flags(attributed)
    if len(attributed) > config["report"]["max_files_listed"] or sum(c.added + c.removed for c in attributed) > 2000:
        risks.append(f"Large change ({len(attributed)} files, "
                     f"+{sum(c.added for c in attributed)}/-{sum(c.removed for c in attributed)} lines) - review in pieces.")
    if changes.secrets:
        risks.insert(0, "Possible secrets in added content - do not commit until reviewed.")

    verdict = _verdict(attributed, results, code_changes, untested, run)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo": str(root),
        "task": (baseline or {}).get("task"),
        "baseline": {"present": baseline is not None, "created_at": (baseline or {}).get("created_at"),
                     "base_ref": (changes.base_ref or "")[:12] or None,
                     "commits_since": changes.commits_since_baseline},
        "state_fingerprint": gitstate.state_fingerprint(root),
        "checks_ran": run,
        "verdict": verdict,
        "changes": [
            {"path": c.path, "status": c.status, "added": c.added, "removed": c.removed,
             "categories": c.categories, **({"note": c.note} if c.note else {})}
            for c in attributed
        ],
        "pre_existing_unrelated": changes.pre_existing,
        "checks": [r.to_dict() for r in results],
        "findings": findings,
        "risks": risks,
        "notes": notes,
    }
    report["next_prompt"] = next_prompt(report)
    save_report(root, report)
    return report


def _verdict(attributed, results, code_changes, untested, run) -> str:
    if not attributed:
        return "NO CHANGES"
    if any(r.outcome == FAILED for r in results):
        return "FAILED"
    if not run:
        return "NOT VERIFIED (inspection only)"
    if not code_changes and not any(r.outcome in (BLOCKED, NOT_TESTED) for r in results):
        return "NO EXECUTABLE CHANGES"
    if any(r.outcome in (BLOCKED, NOT_TESTED) for r in results) or untested:
        return "PARTIALLY VERIFIED" if any(r.outcome == PASSED for r in results) else "NOT VERIFIED"
    return "VERIFIED"


def next_prompt(report: dict) -> str:
    checks = report["checks"]
    if not report["checks_ran"] and report["changes"]:
        runnable = [c["id"] for c in checks if c["outcome"] == NOT_TESTED and c["detail"].endswith("(dry run)")]
        blocked_ids = [f"{c['id']} ({c['detail']})" for c in checks if c["outcome"] == BLOCKED]
        text = (f"Call verify_task to run {len(runnable)} selected check(s): {', '.join(runnable)}."
                if runnable else "No runnable checks apply; review the changes manually.")
        return text + (f" Already blocked: {'; '.join(blocked_ids)}." if blocked_ids else "")
    failed = [c for c in checks if c["outcome"] == FAILED]
    blocked = [c for c in checks if c["outcome"] == BLOCKED]
    parts: list[str] = []
    if failed:
        first = failed[0]
        detail = "; ".join((first.get("excerpt") or [])[-3:])
        parts.append(f"Fix the failing check(s): {', '.join(c['id'] for c in failed)}. "
                     f"Run `{first['command']}` to reproduce" + (f" - key output: {detail}" if detail else "") + ".")
    if blocked:
        parts.append("Unblock verification: " + "; ".join(f"{c['id']} ({c['detail']})" for c in blocked) + ".")
    untested = [f["text"] for f in report["findings"] if f["evidence"] == NOT_TESTED_L and f["text"].startswith("No passing")]
    if untested:
        parts.append(untested[0].replace("No passing test exercised:", "Add or run tests covering") + ".")
    if any("disappeared" in r for r in report["risks"]):
        parts.append("Confirm whether the discarded pre-existing changes should be restored.")
    if report["verdict"] in ("VERIFIED", "NO EXECUTABLE CHANGES") and not parts:
        tail = " Review the risk flags before merging." if report["risks"] else ""
        return "Verification passed for the changed files." + tail
    if report["verdict"] == "NO CHANGES":
        return "No changes attributed to this task; confirm the work was saved in this repository."
    parts.append("Then call verify_task again.")
    return " ".join(parts)


# --------------------------------------------------------------------------- persistence


def save_report(root: Path, report: dict) -> None:
    directory = gitstate.state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "last_report.json").write_text(json.dumps(report, indent=2))


def load_report(root: Path) -> dict | None:
    path = gitstate.state_dir(root) / "last_report.json"
    return json.loads(path.read_text()) if path.is_file() else None


# --------------------------------------------------------------------------- rendering

COLLAPSE_AT = 5
ICON = {PASSED: "PASS", FAILED: "FAIL", BLOCKED: "BLOCKED", NOT_TESTED: "NOT TESTED"}


def render_markdown(report: dict, max_files: int = 25) -> str:
    lines: list[str] = []
    title = report.get("task") or "Task handoff"
    lines.append(f"## {title} - {report['verdict']}")
    b = report["baseline"]
    base_txt = (f"baseline {b['created_at']} @ {b['base_ref']}" if b["present"] else "no baseline (attribution vs HEAD)")
    if b.get("commits_since"):
        base_txt += f", {b['commits_since']} commit(s) since"
    lines.append(f"_{report['repo']} · {base_txt}_")

    changes = report["changes"]
    if changes:
        added = sum(c["added"] for c in changes)
        removed = sum(c["removed"] for c in changes)
        lines.append(f"\n**Changes** ({len(changes)} files, +{added}/-{removed}) [VERIFIED]")
        if len(changes) <= max_files:
            def collapsible(c):
                return set(c["categories"]) <= {"other", "docs"} and not c.get("note")
            groups = Counter(str(PurePosixPath(c["path"]).parent) for c in changes if collapsible(c))
            emitted: set[str] = set()
            for c in changes:
                directory = str(PurePosixPath(c["path"]).parent)
                if collapsible(c) and groups[directory] >= COLLAPSE_AT:
                    if directory not in emitted:
                        emitted.add(directory)
                        members = [m for m in changes if collapsible(m) and str(PurePosixPath(m["path"]).parent) == directory]
                        statuses = ", ".join(sorted({m["status"] for m in members}))
                        lines.append(f"- `{directory}/` {len(members)} non-code files ({statuses}) "
                                     f"+{sum(m['added'] for m in members)}/-{sum(m['removed'] for m in members)}")
                    continue
                note = f" - {c['note']}" if c.get("note") else ""
                lines.append(f"- `{c['path']}` {c['status']} +{c['added']}/-{c['removed']} "
                             f"({', '.join(c['categories'])}){note}")
        else:
            by_dir = Counter(str(PurePosixPath(c["path"]).parent) for c in changes)
            for directory, count in by_dir.most_common(10):
                lines.append(f"- `{directory}/` {count} files")
            if len(by_dir) > 10:
                lines.append(f"- ... {len(by_dir) - 10} more directories")
    if report["pre_existing_unrelated"]:
        pre = report["pre_existing_unrelated"]
        lines.append(f"- Excluded {len(pre)} pre-existing uncommitted file(s): "
                     + ", ".join(f"`{p}`" for p in pre[:5]) + (" ..." if len(pre) > 5 else ""))

    if report["checks"]:
        lines.append("\n**Checks**")
        for c in report["checks"]:
            lines.append(f"- {ICON[c['outcome']]} `{c['id']}` - {c['detail']}"
                         + (f" ({c['duration_s']}s)" if c.get("duration_s") else "") + f" · why: {c['reason']}")
            if c["outcome"] == FAILED and c.get("excerpt"):
                for ln in c["excerpt"][-5:]:
                    lines.append(f"    > {ln}")

    others = [f for f in report["findings"] if not (f["evidence"] == VERIFIED and
                                                    re.match(r"^\S+ (passed|FAILED) \(", f["text"]))]
    others = [f for f in others if not (f["evidence"] in (BLOCKED_L, NOT_TESTED_L) and
                                        any(f["text"].startswith(c["id"] + ":") for c in report["checks"]))]
    if others:
        lines.append("\n**Evidence**")
        for f in others:
            lines.append(f"- [{f['evidence']}] {f['text']}")
    if report["risks"]:
        lines.append("\n**Risks**")
        lines.extend(f"- {r}" for r in report["risks"])
    if report.get("notes"):
        lines.append(f"\n**Notes** {report['notes']}")
    lines.append(f"\n**Next prompt:** {report['next_prompt']}")
    return "\n".join(lines)
