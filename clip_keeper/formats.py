"""Clipboard data formats beyond plain text, kept free of GTK so they are testable.

  x-special/gnome-copied-files  what Nautilus (Files) offers: "copy\\n" + one URI per line
  text/uri-list                 the standard list (RFC 2483): CRLF-separated, # comments
  text/html                     rich text; Firefox and Chromium may send UTF-16 with a BOM"""

from __future__ import annotations

import struct
from urllib.parse import unquote, urlparse

GNOME_FILES = "x-special/gnome-copied-files"
URI_LIST = "text/uri-list"
HTML = "text/html"
MAX_FILES = 10_000
MAX_HTML_BYTES = 1024 * 1024
MAX_FILES_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000  # 8K × 5K; decoding is ~4 bytes per pixel
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def parse_gnome_files(data: bytes | None) -> list[str] | None:
    """URIs from Nautilus's format; None when it isn't one ("cut" counts too: the files
    are still there until pasted)."""
    if not data or len(data) > MAX_FILES_BYTES:
        return None
    lines = [ln.strip() for ln in data.decode("utf-8", "replace").replace("\r", "").split("\n")]
    if not lines or lines[0] not in ("copy", "cut"):
        return None
    uris = [ln for ln in lines[1:] if ln and "://" in ln]
    return uris[:MAX_FILES] or None


def gnome_files(uris: list[str]) -> bytes:
    return ("copy\n" + "\n".join(uris)).encode("utf-8")


def uri_list(uris: list[str]) -> bytes:
    return ("\r\n".join(uris) + "\r\n").encode("utf-8")


def paths_text(uris: list[str]) -> str:
    """The plain-text form of a file copy: local paths, one per line (what Nautilus offers)."""
    out = []
    for uri in uris:
        parsed = urlparse(uri)
        out.append(unquote(parsed.path) if parsed.scheme == "file" else uri)
    return "\n".join(out)


def decode_html(data: bytes | None) -> str:
    """HTML as text, or "" when absent or too big to be worth keeping."""
    if not data or len(data) > MAX_HTML_BYTES:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", "replace")
    else:
        text = data.decode("utf-8", "replace")
    return text.replace("\x00", "").strip()


def png_size(data: bytes | None) -> tuple[int, int] | None:
    """Width and height from a PNG header, without decoding it; None if it isn't a PNG."""
    if not data or len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def image_too_big(data: bytes) -> bool:
    """Checked before decoding: a tiny file can declare a huge image."""
    size = png_size(data)
    return len(data) > MAX_IMAGE_BYTES or size is None or size[0] * size[1] > MAX_IMAGE_PIXELS
