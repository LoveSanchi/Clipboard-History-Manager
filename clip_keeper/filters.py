"""What is never recorded.

1. Anything a password manager flags as secret (the `x-kde-passwordManagerHint` clipboard
   target, set by KeePassXC, 1Password and others).
2. Text that looks like a credential: private keys, provider tokens, bearer/basic auth,
   connection strings or `curl -u` with a password, `PASSWORD=…`-style assignments.
3. Empty text, and text too large to be a sensible clip.

Every pattern starts from a literal anchor (a token prefix, "://", a keyword) and only uses
bounded repeats around it, so scanning a 1 MiB clip stays linear — a slow regex here would
freeze the top-bar app.
"""

from __future__ import annotations

import re

MAX_CLIP_BYTES = 1024 * 1024
PASSWORD_HINT_TARGETS = ("x-kde-passwordManagerHint", "application/x-kde-passwordManagerHint")
_NOT_VALUES = {"string", "boolean", "number", "undefined", "required", "optional", "nullable", "default"}

_TOKEN = (
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|npm_[A-Za-z0-9]{36}|pypi-[A-Za-z0-9_-]{50,}"
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|ya29\.[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}"
    r"|hooks\.slack\.com/services/[A-Za-z0-9/]{20,}"
)
_KEYWORDS = r"password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|credential"

_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")),
    ("token", re.compile(rf"(?<![A-Za-z0-9_-])(?:{_TOKEN})")),
    ("JWT", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer/basic credential", re.compile(r"\b(?:Bearer|Basic) [A-Za-z0-9._~+/=-]{16,}")),
    # user:password@host right after the scheme — the password ends before any '/', so
    # http://host:3000/@vite or ?user=a@b.com are not matched.
    ("connection string with a password", re.compile(r"://[^\s:/@]{0,64}:[^\s@/]{1,256}@[^\s/]")),
    ("curl -u credentials", re.compile(r"(?:^|\s)(?:-u|--user)\s+[^\s:]{1,64}:[^\s]{3,}")),
]
_ASSIGNMENT = re.compile(
    rf"(?i)(?:{_KEYWORDS})[A-Za-z0-9_]{{0,20}}[\"']?[ \t]{{0,3}}[:=][ \t]{{0,3}}"
    r"[\"']?(?P<value>[^\s\"',;)}]{6,})")
_SPOKEN = re.compile(r"(?i)\bpassword (?:is|was|=) (?P<value>\S{6,})")


def _secret_like(value: str) -> bool:
    """A value worth hiding: not a type word, and has a digit or symbol, or is long."""
    if value.lower() in _NOT_VALUES:
        return False
    return len(value) >= 16 or any(not c.isalpha() for c in value)


def secret_kind(text: str) -> str | None:
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            return kind
    for pattern, kind in ((_ASSIGNMENT, "password assignment"), (_SPOKEN, "password")):
        for match in pattern.finditer(text):
            if _secret_like(match.group("value")):
                return kind
    return None


def skip_reason(text: str | None, password_hint: bool, skip_secrets: bool) -> str | None:
    """Why this clip must not be stored, or None to store it."""
    if password_hint:
        return "marked secret by a password manager"
    if text is None or not text.strip():
        return "empty"
    if len(text.encode("utf-8", "replace")) > MAX_CLIP_BYTES:
        return "larger than 1 MiB"
    if skip_secrets:
        kind = secret_kind(text)
        if kind:
            return f"looks like a {kind}"
    return None
