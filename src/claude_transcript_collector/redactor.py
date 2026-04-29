"""Redact secrets from transcript text.

Design principles:
- High precision, low recall: only match things that are almost certainly secrets.
- Each pattern is anchored to a distinctive prefix or structure so we don't
  accidentally nuke normal prose, hex colors, base64 snippets in code, etc.
"""

import re

REDACTION_PLACEHOLDER = "[REDACTED]"

PATTERNS: list[tuple[str, re.Pattern]] = [
    # AWS access key IDs (always start with AKIA/ASIA)
    ("AWS Access Key", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")),

    # AWS secret keys — 40 chars of base64, preceded by known key names
    ("AWS Secret Key", re.compile(
        r"(?i)(?:aws_secret_access_key|secret_access_key|aws_secret)\s*[=:]\s*"
        r"['\"]?([A-Za-z0-9/+=]{40})['\"]?"
    )),

    # OpenAI / Anthropic style keys: sk-... (20+ chars)
    ("API Key (sk-)", re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}\b")),

    # Anthropic keys with prefix
    ("Anthropic Key", re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b")),

    # GitHub tokens
    ("GitHub Token", re.compile(r"\b(ghp|gho|ghs|ghu|ghr)_[a-zA-Z0-9]{36,}\b")),

    # GitHub fine-grained PATs
    ("GitHub PAT", re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{20,}\b")),

    # Slack tokens
    ("Slack Token", re.compile(r"\bxox[baprs]-[a-zA-Z0-9\-]{10,}\b")),

    # Stripe keys
    ("Stripe Key", re.compile(r"\b[sr]k_live_[a-zA-Z0-9]{20,}\b")),

    # JWTs (three base64url segments separated by dots)
    ("JWT", re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    )),

    # PEM private keys
    ("Private Key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    )),

    # Passwords in URLs: ://user:pass@host
    ("URL Password", re.compile(
        r"(://[^:/?#\s]+):([^@/?#\s]{3,})(@)"
    )),

    # Generic secret/password/token assignments (key = "value" or key: "value")
    ("Secret Assignment", re.compile(
        r'(?i)(?:password|passwd|secret|token|api_key|apikey|access_key)'
        r'\s*[=:]\s*["\']([^"\']{8,})["\']'
    )),
]


def redact(text: str) -> tuple[str, list[dict]]:
    """Redact secrets from text.

    Returns (redacted_text, list_of_redaction_records).
    Each record has: {"pattern_name": str, "start": int, "end": int, "original_length": int}
    """
    records = []

    for pattern_name, pattern in PATTERNS:
        for match in pattern.finditer(text):
            records.append({
                "pattern_name": pattern_name,
                "start": match.start(),
                "end": match.end(),
                "original_length": match.end() - match.start(),
            })

    if not records:
        return text, []

    # Sort by start position, merge overlapping ranges
    records.sort(key=lambda r: r["start"])
    merged = [records[0]]
    for r in records[1:]:
        if r["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], r["end"])
            merged[-1]["pattern_name"] += f", {r['pattern_name']}"
        else:
            merged.append(r)

    # Apply redactions from end to preserve offsets
    result = text
    for r in reversed(merged):
        result = result[:r["start"]] + REDACTION_PLACEHOLDER + result[r["end"]:]

    return result, merged


def redact_jsonl_content(raw_jsonl: str) -> tuple[str, int]:
    """Redact secrets from raw JSONL content.

    Returns (redacted_content, total_redaction_count).
    """
    redacted_text, records = redact(raw_jsonl)
    return redacted_text, len(records)
