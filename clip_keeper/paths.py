"""Where things live. The history database is copycat's own location, so either tool
can read the other's history."""

import os
from pathlib import Path

APP = "clip-keeper"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "").strip()
    path = Path(value) if value else None
    return path if path and path.is_absolute() else Path.home() / fallback


def db_path() -> Path:
    """Shared with copycat: $XDG_DATA_HOME/copycat/history.db."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "copycat" / "history.db"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def settings_file() -> Path:
    return config_dir() / "settings.json"


def runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    return Path(runtime) / APP if runtime else _xdg("XDG_STATE_HOME", ".local/state") / APP / "run"


def socket_path() -> Path:
    return Path(os.environ.get("CLIP_KEEPER_SOCKET", "") or runtime_dir() / "tray.sock")
