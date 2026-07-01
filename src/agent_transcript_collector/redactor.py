"""Redact secrets from transcript text.

Design principles:
- High precision, low recall: only match things that are almost certainly secrets.
- Each pattern is anchored to a distinctive prefix or structure so we don't
  accidentally nuke normal prose, hex colors, base64 snippets in code, etc.
"""

import functools
import getpass
import hashlib
import hmac
import os
import re
import string
import subprocess
from pathlib import Path

USERNAME_PLACEHOLDER = "[USER]"
EMAIL_PLACEHOLDER = "[EMAIL]"

# Secrets are replaced with type-preserving MOCKS rather than a blanket
# [REDACTED], so an investigator can still see *which* kind of credential was
# present and trace one secret's flow through a transcript (env -> tool arg ->
# file write -> echoed output). The real value never survives.
#
# Mapping is run-local and irreversible: the same real secret maps to the same
# mock everywhere within a single process (so flow tracing works across every
# file in an upload), but the per-process salt is random and discarded on exit,
# so a guessed secret can't be confirmed and nothing reverses mock -> original.
_MOCK_SALT = os.urandom(16)

# Every mock embeds this constant near the start of its random portion: it is
# hex("MOCK"), so it reads as ordinary key material but is a fixed,
# salt-independent marker. Anyone who knows the scheme can grep `(?i)4d4f434b`
# to enumerate/confirm synthetic values. Uppercase variant for [A-Z0-9]-only
# token types (e.g. AWS access keys).
_MOCK_TAG = "4d4f434b"
_MOCK_TAG_UP = _MOCK_TAG.upper()

_ALNUM = string.ascii_letters + string.digits
_LOWER_ALNUM = string.ascii_lowercase + string.digits
_UPPER_ALNUM = string.ascii_uppercase + string.digits
_HEX = "0123456789abcdef"
_B64 = string.ascii_letters + string.digits + "+/"
_B64URL = string.ascii_letters + string.digits + "-_"

# Bare-token username redaction only applies to names at least this long, so a
# short/common login can't nuke unrelated words. Path redaction is anchored and
# not subject to this.
MIN_USERNAME_LEN = 4

# System/cloud default logins we never redact, in paths or as tokens — there's no
# personal info in "ubuntu". Extend at runtime via CTC_USERNAME_STOPLIST.
_BASE_DEFAULT_USERS = {
    "ubuntu", "ec2-user", "admin", "administrator", "root", "user", "users",
    "shared", "dev", "node", "app", "runner", "vagrant", "pi", "centos",
    "debian", "fedora", "azureuser", "cloud-user", "opc", "git", "guest",
}

_HOMEPATH_RE = re.compile(r"(/(?:home|Users)/)([A-Za-z0-9][A-Za-z0-9._-]*)")

# Emails: fail safe — redact any plausible address (alphabetic TLD) rather than
# allowlisting TLDs (which silently leaks uncommon ccTLDs like .it/.es). Guards
# keep code/hosts intact:
#  - the local part must START with an alphanumeric and be preceded by a real
#    boundary (not + - . \ @ or another local-part char). This drops web-framework
#    decorators written in diffs/code — `+@app.post`, `-@app.get`, `\n@app.route`
#    — whose "local part" is only the diff marker or escaped-newline char.
#  - a small denylist excludes internal-host pseudo-TLDs (...ec2.internal).
_EMAIL_NON_TLDS = {"internal", "local", "localdomain", "lan", "arpa"}
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-\\@])[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+\.([A-Za-z]{2,24})"
)

# Each entry is (name, compiled regex, secret_group, kind). `secret_group` is the
# capture group holding the actual secret to replace — 0 means the whole match.
# Anchoring affixes (key= , ://user: , @ssh.runpod.io , the npg_ prefix, ...) sit
# OUTSIDE that group so they survive into the mock, preserving how the secret was
# handled. `kind` selects the mock template.
PATTERNS: list[tuple[str, re.Pattern, int, str]] = [
    # AWS access key IDs (always start with AKIA/ASIA)
    ("AWS Access Key", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), 0, "aws_access"),

    # AWS secret keys — 40 chars of base64, preceded by known key names
    ("AWS Secret Key", re.compile(
        r"(?i)(?:aws_secret_access_key|secret_access_key|aws_secret)\s*[=:]\s*"
        r"['\"]?([A-Za-z0-9/+=]{40})['\"]?"
    ), 1, "aws_secret"),

    # OpenAI / Anthropic style keys: sk-... (20+ chars)
    ("API Key (sk-)", re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}\b"), 0, "openai"),

    # Anthropic keys with prefix
    ("Anthropic Key", re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b"), 0, "anthropic"),

    # GitHub tokens. No leading \b: these tokens are frequently concatenated to
    # surrounding chars in real transcripts (e.g. inside longer blobs), and a
    # leading word-boundary would miss those; the distinctive prefix + 36-char
    # run is specific enough on its own.
    ("GitHub Token", re.compile(r"(ghp|gho|ghs|ghu|ghr)_[a-zA-Z0-9]{36,}"), 0, "github_token"),

    # GitHub fine-grained PATs
    ("GitHub PAT", re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{20,}\b"), 0, "github_pat"),

    # Slack tokens
    ("Slack Token", re.compile(r"\bxox[baprs]-[a-zA-Z0-9\-]{10,}\b"), 0, "slack"),

    # Stripe keys
    ("Stripe Key", re.compile(r"\b[sr]k_live_[a-zA-Z0-9]{20,}\b"), 0, "stripe"),

    # JWTs (three base64url segments separated by dots)
    ("JWT", re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    ), 0, "jwt"),

    # PEM private keys
    ("Private Key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    ), 0, "pem"),

    # Basic-auth userinfo in URLs: ://user:pass@host — mock the WHOLE userinfo
    # (user AND pass), since the username slot routinely carries a token used as
    # the user. Same trigger as before (requires user:pass), but the username no
    # longer survives. For DB schemes this coincides with DB Connection URI below
    # (same span, same kind) and merges to one.
    ("URL Password", re.compile(
        r"(://)([^:/?#\s]+:[^@/?#\s]{3,})(@)"
    ), 2, "uri_userinfo"),

    # DB/messaging connection URIs with userinfo — covers the password-LESS /
    # token-as-user form (scheme://token@host) the URL Password pattern misses.
    # Replace only the userinfo, keeping the scheme and host.
    ("DB Connection URI", re.compile(
        r"(?i)\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)"
        r"://([^/\s@]+)@"
    ), 1, "uri_userinfo"),

    # Neon Postgres role/password token (distinctive npg_ prefix)
    ("Neon Credential", re.compile(r"\bnpg_[A-Za-z0-9]{8,}\b"), 0, "neon"),

    # RunPod SSH target: <pod-id>-<hex>@ssh.runpod.io — replace the id, keep the host.
    ("RunPod SSH", re.compile(r"\b([A-Za-z0-9]{8,}-[a-fA-F0-9]{6,})@ssh\.runpod\.io\b"), 1, "runpod"),

    # HuggingFace access tokens (hf_ + 34+ chars). Prefix + length is specific
    # enough; no leading anchor so embedded tokens are still caught.
    ("HuggingFace Token", re.compile(r"hf_[A-Za-z0-9]{34,}"), 0, "huggingface"),

    # Google API keys (AIza + 35 chars).
    ("GCP API Key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), 0, "gcp"),

    # Slack app-level tokens (xapp-…) — the xox… pattern above does not cover these.
    ("Slack App Token", re.compile(r"xapp-\d-[A-Za-z0-9-]{15,}"), 0, "slack_app"),

    # GitLab tokens: glrt- runner-authentication tokens and glpat- personal tokens.
    ("GitLab Token", re.compile(r"gl(?:rt|pat)-[A-Za-z0-9_\-]{20,}"), 0, "gitlab"),

    # Generic secret/password/token assignments (key = "value" or key: "value")
    ("Secret Assignment", re.compile(
        r'(?i)(?:password|passwd|secret|token|api_key|apikey|access_key)'
        r'\s*[=:]\s*["\']([^"\']{8,})["\']'
    ), 1, "generic"),
]

# When two secret spans overlap (e.g. sk-ant- matches both the sk- and Anthropic
# patterns, or a Neon token sits inside a DB URI's userinfo), the higher-ranked
# kind wins so the mock reflects the most specific type. Unlisted kinds default to 10.
_RANK = {
    "anthropic": 30, "neon": 25, "aws_secret": 20, "uri_userinfo": 20,
    "runpod": 18, "github_pat": 15, "password": 10, "generic": 5,
}


class _Det:
    """Deterministic char source seeded from (per-run salt, kind, secret).

    Same secret -> same byte stream within the process, so identical secrets map
    to identical mocks. The salt is random and never stored, so the stream can't
    be reproduced after the run and a guessed secret can't be confirmed.
    """

    def __init__(self, kind: str, secret: str):
        self._seed = f"{kind}\x00{secret}".encode("utf-8", "surrogatepass")
        self._block = hmac.new(_MOCK_SALT, self._seed, hashlib.sha256).digest()
        self._buf = self._block
        self._counter = 0

    def _byte(self) -> int:
        if not self._buf:
            self._counter += 1
            self._buf = hmac.new(
                _MOCK_SALT, self._block + self._counter.to_bytes(4, "big"), hashlib.sha256
            ).digest()
        b = self._buf[0]
        self._buf = self._buf[1:]
        return b

    def chars(self, alphabet: str, n: int) -> str:
        return "".join(alphabet[self._byte() % len(alphabet)] for _ in range(n))


def _mock(kind: str, original: str) -> str:
    """Build a fake, type-preserving replacement for a detected secret.

    The result matches the same detector (so it still classifies as that type)
    and embeds the _MOCK_TAG marker, but carries none of the original value.
    """
    r = _Det(kind, original)
    if kind == "aws_access":
        prefix = original[:4] if original[:4] in ("AKIA", "ASIA") else "AKIA"
        return prefix + _MOCK_TAG_UP + r.chars(_UPPER_ALNUM, 8)        # 4 + 16
    if kind == "aws_secret":
        return _MOCK_TAG + r.chars(_B64, 32)                           # 40
    if kind == "openai":
        return "sk-" + _MOCK_TAG + r.chars(_ALNUM, 16)
    if kind == "anthropic":
        return "sk-ant-" + _MOCK_TAG + r.chars(_ALNUM, 16)
    if kind == "github_token":
        prefix = original.split("_", 1)[0] if "_" in original else "ghp"
        return f"{prefix}_" + _MOCK_TAG + r.chars(_ALNUM, 28)          # >=36 after _
    if kind == "github_pat":
        return "github_pat_" + _MOCK_TAG + r.chars(_ALNUM, 16)
    if kind == "slack":
        prefix = original[:5] if re.match(r"xox[baprs]-", original) else "xoxb-"
        return prefix + _MOCK_TAG + r.chars(_ALNUM, 8)
    if kind == "stripe":
        prefix = original[:8] if re.match(r"[sr]k_live_", original) else "sk_live_"
        return prefix + _MOCK_TAG + r.chars(_ALNUM, 16)
    if kind == "jwt":
        return ("eyJ" + _MOCK_TAG + r.chars(_B64URL, 12) + "."
                + "eyJ" + r.chars(_B64URL, 20) + "."
                + r.chars(_B64URL, 24))
    if kind == "pem":
        m = re.search(r"BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY", original)
        algo = m.group(1) if m and m.group(1) else ""
        # Single line (no newlines) so it never breaks JSON-encoded transcript strings.
        body = _MOCK_TAG + r.chars(_B64, 64)
        return f"-----BEGIN {algo}PRIVATE KEY-----{body}-----END {algo}PRIVATE KEY-----"
    if kind == "neon":
        return "npg_" + _MOCK_TAG + r.chars(_ALNUM, 8)
    if kind == "runpod":
        return _MOCK_TAG + r.chars(_LOWER_ALNUM, 4) + "-" + r.chars(_HEX, 6)
    if kind == "password":
        return _MOCK_TAG + r.chars(_ALNUM, 6)
    if kind == "uri_userinfo":
        if ":" in original:                                            # user:pass form
            return "mockuser:" + _MOCK_TAG + r.chars(_ALNUM, 6)
        return _MOCK_TAG + r.chars(_ALNUM, 12)                         # token-as-user form
    if kind == "huggingface":
        return "hf_" + _MOCK_TAG + r.chars(_ALNUM, 26)                 # hf_ + 34
    if kind == "gcp":
        return "AIza" + _MOCK_TAG + r.chars(_ALNUM, 27)                # AIza + 35
    if kind == "slack_app":
        return "xapp-1-" + _MOCK_TAG + r.chars(_ALNUM, 12)
    if kind == "gitlab":
        prefix = original[:original.index("-") + 1] if "-" in original else "glrt-"
        return prefix + _MOCK_TAG + r.chars(_ALNUM, 16)
    return _MOCK_TAG + r.chars(_ALNUM, 8)                              # generic


def redact(text: str) -> tuple[str, list[dict]]:
    """Replace secrets in text with type-preserving mocks.

    Returns (redacted_text, list_of_redaction_records).
    Each record has: {"pattern_name": kind, "start": int, "end": int, "original_length": int}
    where start/end are offsets into the ORIGINAL text.
    """
    spans = []  # (start, end, kind) of the secret group
    for _name, pattern, group, kind in PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(group)
            if start < 0 or end <= start:
                continue
            spans.append((start, end, kind))

    if not spans:
        return text, []

    # Sort by start, longest-first on ties, then merge overlapping spans. The
    # surviving kind is the highest-ranked among the overlap (most specific type).
    spans.sort(key=lambda s: (s[0], -s[1]))
    merged: list[list] = []
    for start, end, kind in spans:
        if merged and start < merged[-1][1]:
            prev = merged[-1]
            prev[1] = max(prev[1], end)
            if _RANK.get(kind, 10) > _RANK.get(prev[2], 10):
                prev[2] = kind
        else:
            merged.append([start, end, kind])

    # Apply from end to preserve offsets.
    records = []
    result = text
    for start, end, kind in reversed(merged):
        result = result[:start] + _mock(kind, text[start:end]) + result[end:]
        records.append({
            "pattern_name": kind, "start": start, "end": end,
            "original_length": end - start,
        })
    records.reverse()
    return result, records


def redact_jsonl_content(raw_jsonl: str) -> tuple[str, int]:
    """Redact secrets from raw JSONL content.

    Returns (redacted_content, total_redaction_count).
    """
    redacted_text, records = redact(raw_jsonl)
    return redacted_text, len(records)


def default_usernames() -> set[str]:
    """System/default logins that are never redacted (stoplist, env-extendable)."""
    extra = os.environ.get("CTC_USERNAME_STOPLIST", "")
    return _BASE_DEFAULT_USERS | {u.strip().lower() for u in extra.split(",") if u.strip()}


@functools.lru_cache(maxsize=1)
def local_usernames() -> tuple[str, ...]:
    """The machine's own identity tokens worth redacting (computed once).

    Gathers the home-dir name, the login name, and git user.name; drops any that
    are default/system logins or shorter than MIN_USERNAME_LEN. Longest first so
    overlapping names redact greedily.
    """
    names = set()
    try:
        names.add(Path.home().name)
    except Exception:
        pass
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["git", "config", "--get", "user.name"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            names.add(out.stdout.strip())
    except Exception:
        pass
    defaults = default_usernames()
    keep = {n for n in names if n and len(n) >= MIN_USERNAME_LEN and n.lower() not in defaults}
    return tuple(sorted(keep, key=len, reverse=True))


def redact_identity(text: str, usernames: tuple[str, ...] | None = None) -> tuple[str, int]:
    """Redact home-path usernames, the local identity, and emails.

    - Home paths: /home/<u>/ and /Users/<u>/ -> /home/[USER]/, EXCEPT default
      logins (ubuntu, admin, ...), which are left untouched.
    - The machine's own non-default usernames are redacted as bare tokens too,
      so they don't leak outside paths.
    - Email addresses -> [EMAIL].

    Emails are redacted before the bare-token pass so that an address whose local
    part is the local username (e.g. nick@host.com) becomes [EMAIL] rather than
    being fragmented into [USER]@host.com.

    Returns (redacted_text, count).
    """
    if usernames is None:
        usernames = local_usernames()
    defaults = default_usernames()
    counts = {"n": 0}

    def _home(m):
        if m.group(2).lower() in defaults:
            return m.group(0)
        counts["n"] += 1
        return m.group(1) + USERNAME_PLACEHOLDER

    text = _HOMEPATH_RE.sub(_home, text)

    def _email(m):
        if m.group(1).lower() in _EMAIL_NON_TLDS:
            return m.group(0)
        counts["n"] += 1
        return EMAIL_PLACEHOLDER

    text = _EMAIL_RE.sub(_email, text)

    for name in usernames:
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        text, c = pat.subn(USERNAME_PLACEHOLDER, text)
        counts["n"] += c
    return text, counts["n"]


# Dash-encoded home paths used as project keys: `-home-<user>-...` (Claude) and
# `home-<user>-...` (Codex/Pi). Applied only to archive paths / manifest group
# fields, where the context is unambiguously a path — so no min-length guard,
# unlike bare-token redaction in free content.
_HOMEPATH_ENCODED_RE = re.compile(r"(^|-)(home|Users)-([^-]+)")


def redact_path_token(token: str, usernames: tuple[str, ...] | None = None) -> tuple[str, int]:
    """Redact usernames from an archive path / manifest group field.

    Covers the decoded slash form (/home/<u>/) via redact_identity AND the
    dash-encoded project-key form (-home-<u>-, home-<u>-), which the slash regex
    can't see. Default logins are still preserved.
    """
    token, n = redact_identity(token, usernames=usernames)
    defaults = default_usernames()
    counts = {"n": 0}

    def _enc(m):
        seg = m.group(3)
        if seg == USERNAME_PLACEHOLDER or seg.lower() in defaults:
            return m.group(0)
        counts["n"] += 1
        return f"{m.group(1)}{m.group(2)}-{USERNAME_PLACEHOLDER}"

    token = _HOMEPATH_ENCODED_RE.sub(_enc, token)
    return token, n + counts["n"]
