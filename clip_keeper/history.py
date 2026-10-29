"""Opening the history the way the settings ask: encrypted (key from GNOME Keyring) or plain."""

from __future__ import annotations

from clip_keeper.crypto import Cipher, KeyUnavailable, load_key
from clip_keeper.settings import Settings
from clip_keeper.store import Store


def open_store(settings: Settings, path=None, key_loader=None) -> tuple[Store, str]:
    """(store, problem). With encryption on but the key unreachable (keyring locked, or a
    damaged key) the store opens locked: encrypted rows show as locked and anything that
    would write clip text raises — writing plain text would defeat the setting."""
    if not settings.encrypt:
        return Store(path), ""
    try:
        return Store(path, cipher=Cipher((key_loader or load_key)())), ""
    except (KeyUnavailable, ValueError) as exc:
        return Store(path, locked=True), f"history is locked: {exc}"
