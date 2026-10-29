"""Optional encryption of the history at rest (off by default).

When on, clip text, previews, names, rich text, file lists and image files are encrypted
with AES-256-GCM; duplicate detection uses a keyed hash, so the hashes reveal nothing either.
The key is random and lives in GNOME Keyring (the Secret Service API, stable for a decade),
which unlocks with your login — a copied backup of your home folder alone is unreadable.

Trade-off: copycat cannot read an encrypted history, and search runs over decrypted text in
memory instead of the full-text index (which would hold plain text)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "ck1:"
KEY_LABEL = "clip-keeper history key"
ATTRIBUTES = {"application": "clip-keeper", "purpose": "history-encryption"}


class KeyUnavailable(RuntimeError):
    pass


class Cipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        self._aead = AESGCM(key)
        self._mac_key = hashlib.sha256(b"clip-keeper hash key" + key).digest()

    def encrypt(self, text: str) -> str:
        return PREFIX + base64.b64encode(self.encrypt_bytes(text.encode("utf-8"))).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not is_encrypted(value):
            return value  # rows written before encryption was switched on
        return self.decrypt_bytes(base64.b64decode(value[len(PREFIX):])).decode("utf-8")

    def encrypt_bytes(self, data: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, data, None)

    def decrypt_bytes(self, blob: bytes) -> bytes:
        """Raises ValueError for a wrong key or damaged data (AES-GCM authenticates both)."""
        try:
            return self._aead.decrypt(blob[:12], blob[12:], None)
        except InvalidTag as exc:
            raise ValueError("cannot decrypt: wrong key or damaged data") from exc

    def keyed_hash(self, kind: str, data: bytes) -> str:
        return PREFIX + hmac.new(self._mac_key, kind.encode() + b"\0" + data, hashlib.sha256).hexdigest()


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _secret_schema():
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import Secret
    schema = Secret.Schema.new("org.clipkeeper.HistoryKey", Secret.SchemaFlags.NONE,
                               {"application": Secret.SchemaAttributeType.STRING,
                                "purpose": Secret.SchemaAttributeType.STRING})
    return Secret, schema


def load_key(create: bool = False) -> bytes:
    """Fetch the key from GNOME Keyring (creating it when asked). Raises KeyUnavailable."""
    try:
        Secret, schema = _secret_schema()
        stored = Secret.password_lookup_sync(schema, ATTRIBUTES, None)
        if stored:
            return base64.b64decode(stored)
        if not create:
            raise KeyUnavailable("no clip-keeper key in GNOME Keyring")
        key = os.urandom(32)
        Secret.password_store_sync(schema, ATTRIBUTES, Secret.COLLECTION_DEFAULT, KEY_LABEL,
                                   base64.b64encode(key).decode("ascii"), None)
        return key
    except KeyUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - keyring locked, no session bus, …
        raise KeyUnavailable(f"GNOME Keyring is unavailable: {exc}") from exc


def delete_key() -> None:
    Secret, schema = _secret_schema()
    Secret.password_clear_sync(schema, ATTRIBUTES, None)
