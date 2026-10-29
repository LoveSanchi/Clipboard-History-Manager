import base64
from unittest import mock

import pytest

from clip_keeper import crypto
from clip_keeper.crypto import Cipher, KeyUnavailable, is_encrypted


def test_round_trip_and_fresh_nonces():
    c = Cipher(b"k" * 32)
    a, b = c.encrypt("héllo"), c.encrypt("héllo")
    assert a != b and is_encrypted(a) and c.decrypt(a) == "héllo"
    assert c.decrypt("plain") == "plain"
    assert c.decrypt_bytes(c.encrypt_bytes(b"\x00\x01")) == b"\x00\x01"


def test_keyed_hash_is_stable_and_key_bound():
    a, b = Cipher(b"a" * 32), Cipher(b"b" * 32)
    assert a.keyed_hash("text", b"x") == a.keyed_hash("text", b"x") != b.keyed_hash("text", b"x")
    assert a.keyed_hash("text", b"x") != a.keyed_hash("image", b"x")


def test_tampering_and_bad_keys_are_rejected():
    c = Cipher(b"k" * 32)
    blob = bytearray(c.encrypt_bytes(b"data"))
    blob[-1] ^= 1
    with pytest.raises(ValueError, match="wrong key"):
        c.decrypt_bytes(bytes(blob))
    with pytest.raises(ValueError):
        Cipher(b"short")
    assert not is_encrypted(None)


class FakeSecret:
    COLLECTION_DEFAULT = "default"

    def __init__(self, stored=None, fail=False):
        self.stored, self.fail = stored, fail

    def password_lookup_sync(self, schema, attrs, cancel):
        if self.fail:
            raise RuntimeError("keyring locked")
        return self.stored

    def password_store_sync(self, schema, attrs, collection, label, value, cancel):
        self.stored = value
        return True

    def password_clear_sync(self, schema, attrs, cancel):
        self.stored = None


def test_load_key_paths():
    fake = FakeSecret()
    with mock.patch.object(crypto, "_secret_schema", return_value=(fake, "schema")):
        with pytest.raises(KeyUnavailable):
            crypto.load_key()
        key = crypto.load_key(create=True)
        assert len(key) == 32 and base64.b64decode(fake.stored) == key
        assert crypto.load_key() == key
        crypto.delete_key()
        assert fake.stored is None
    with mock.patch.object(crypto, "_secret_schema", return_value=(FakeSecret(fail=True), "schema")):
        with pytest.raises(KeyUnavailable, match="unavailable"):
            crypto.load_key()
