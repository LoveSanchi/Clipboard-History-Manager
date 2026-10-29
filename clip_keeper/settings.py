"""User settings (~/.config/clip-keeper/settings.json), written atomically and owner-only."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clip_keeper import paths
from clip_keeper.store import DEFAULT_HISTORY_CAP


@dataclass(frozen=True)
class Settings:
    paused: bool = False
    history_limit: int = DEFAULT_HISTORY_CAP
    skip_secrets: bool = True
    tray_items: int = 10
    record_images: bool = True
    image_limit: int = 50
    auto_paste: bool = True
    expire_days: int = 7           # unpinned clips older than this are deleted (0 = never)
    record_files: bool = True      # file copies in Files (Nautilus)
    record_html: bool = True       # keep rich text alongside the plain text
    private_menu: bool = False     # the top-bar menu shows "Clip 1, 2…" instead of clip text
    encrypt: bool = False          # history encrypted at rest, key in GNOME Keyring
    saved_tray_shortcut: str = ""  # GNOME's message-tray keys before we took Super+V


def load(path: Path | None = None) -> Settings:
    path = path or paths.settings_file()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    defaults = Settings()
    values = {}
    for field in dataclasses.fields(Settings):
        value = raw.get(field.name, getattr(defaults, field.name))
        if type(value) is not type(getattr(defaults, field.name)):
            value = getattr(defaults, field.name)
        values[field.name] = value
    values["history_limit"] = max(10, min(values["history_limit"], 10_000))
    values["tray_items"] = max(0, min(values["tray_items"], 30))
    values["image_limit"] = max(0, min(values["image_limit"], 1000))
    values["expire_days"] = max(0, min(values["expire_days"], 3650))
    return Settings(**values)


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or paths.settings_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(dataclasses.asdict(settings), fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(**changes) -> Settings:
    new = dataclasses.replace(load(), **changes)
    save(new)
    return new
