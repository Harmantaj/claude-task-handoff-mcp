"""Secret detection and redaction so summaries never carry credentials."""

from __future__ import annotations

import re

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("API key (sk-)", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "hard-coded credential",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|passw(?:or)?d|pwd|auth[_-]?key)\b[\"']?\s*[:=]\s*[\"'](?![{$<%])[^\"'\s{}<>]{8,}[\"']"
        ),
    ),
]


def find_secrets(text: str) -> list[str]:
    """Return the names of secret patterns present in text (never the values)."""
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def redact(text: str) -> str:
    for name, pattern in SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED {name}]", text)
    return text
