"""Paste transformed: small, predictable rewrites of a clip's text before it is pasted.
Each one either returns the new text or raises ValueError with a short reason."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Callable
from urllib.parse import quote, unquote

MAX_DECODED_BYTES = 4 * 1024 * 1024


def _json_pretty(text: str) -> str:
    return json.dumps(_json(text), indent=2, ensure_ascii=False)


def _json_compact(text: str) -> str:
    return json.dumps(_json(text), separators=(",", ":"), ensure_ascii=False)


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"not JSON ({exc.msg} at line {exc.lineno})") from exc


def _b64_decode(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    padded = compact + "=" * (-len(compact) % 4)
    try:
        data = base64.b64decode(padded, altchars=b"-_" if ("-" in compact or "_" in compact) else None,
                                validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64") from exc
    if len(data) > MAX_DECODED_BYTES:
        raise ValueError("decoded data is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("decodes to binary, not text") from exc


def _trim(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _one_line(text: str) -> str:
    return " ".join(text.split())


# (key, menu label, function). "plain" drops rich text and is handled by the caller.
TRANSFORMS: list[tuple[str, str, Callable[[str], str]]] = [
    ("plain", "Plain text (no formatting)", lambda t: t),
    ("trim", "Trim whitespace", _trim),
    ("one_line", "Join into one line", _one_line),
    ("json_pretty", "JSON: pretty", _json_pretty),
    ("json_compact", "JSON: compact", _json_compact),
    ("url_encode", "URL-encode", lambda t: quote(t, safe="")),
    ("url_decode", "URL-decode", unquote),
    ("base64_encode", "Base64-encode", lambda t: base64.b64encode(t.encode("utf-8")).decode("ascii")),
    ("base64_decode", "Base64-decode", _b64_decode),
    ("upper", "UPPER CASE", str.upper),
    ("lower", "lower case", str.lower),
    ("title", "Title Case", str.title),
]
BY_KEY = {key: fn for key, _, fn in TRANSFORMS}


def apply(key: str, text: str) -> str:
    if key not in BY_KEY:
        raise ValueError(f"unknown transform {key!r}")
    return BY_KEY[key](text)
