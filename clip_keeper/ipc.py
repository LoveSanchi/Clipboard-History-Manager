"""Tiny line protocol between the popup / CLI and the running top-bar app."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from clip_keeper import paths


class TrayUnavailable(RuntimeError):
    pass


def request(cmd: str, timeout: float = 3.0, sock_path: Path | None = None, **args: Any) -> dict:
    sock_path = sock_path or paths.socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as exc:
        raise TrayUnavailable("clip-keeper is not running in the top bar (start: clip-keeper-tray)") from exc
    try:
        reply = json.loads(data)
    except ValueError as exc:
        raise TrayUnavailable("malformed reply from clip-keeper") from exc
    return reply if isinstance(reply, dict) else {"ok": False, "error": "malformed reply"}
