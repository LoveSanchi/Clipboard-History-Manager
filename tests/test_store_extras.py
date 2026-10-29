"""Full-text search, expiry, snippets, rich text, files and encryption in the store."""

import os

import pytest

from clip_keeper.crypto import Cipher, is_encrypted
from clip_keeper.store import Store, StoreLocked, content_hash, files_preview


def cipher(seed: int = 1) -> Cipher:
    return Cipher(bytes([seed]) * 32)


def test_full_text_search_finds_text_beyond_the_preview():
    store = Store(memory=True)
    deep = store.insert_text("a" * 500 + " needle-in-haystack")
    store.insert_text("nothing here")
    assert store.full_text_matches("NEEDLE-in") == [deep]
    assert store.full_text_matches("ne") == []              # short query: previews only (fuzzy search)
    assert Store(memory=True, cipher=cipher()).full_text_matches("x") == []
    assert store.full_text_matches("   ") == []
    assert store.full_text_matches('say "hi"') == []        # quotes cannot break the FTS query


def test_full_text_index_follows_deletes_and_edits():
    store = Store(memory=True)
    clip_id = store.insert_text("findable words")
    store._conn.execute("UPDATE clips SET content = 'changed text' WHERE id = ?", (clip_id,))
    assert store.full_text_matches("findable") == [] and store.full_text_matches("changed") == [clip_id]
    store.delete(clip_id)
    assert store.full_text_matches("changed") == []


def test_existing_rows_are_indexed_when_search_is_set_up(tmp_path):
    path = tmp_path / "h.db"
    first = Store(path)
    clip_id = first.insert_text("older clip text")
    first._conn.executescript("DROP TABLE clip_keeper_fts")
    first.close()
    assert Store(path).full_text_matches("older") == [clip_id]


def test_expire_keeps_pinned_and_recent():
    store = Store(memory=True)
    day = 86_400_000
    old = store.insert_text("old", created_at=1)
    pinned_old = store.insert_text("pinned old", created_at=1)
    store.set_pinned(pinned_old, True)
    fresh = store.insert_text("fresh", created_at=10 * day)
    assert store.expire(0, now_ms=10 * day) == 0
    assert store.expire(7, now_ms=10 * day) == 1
    assert store.get(old) is None and store.get(pinned_old) and store.get(fresh)


def test_snippet_names_pin_and_sort():
    store = Store(memory=True)
    a, b, c = (store.insert_text(t, created_at=i) for i, t in enumerate(["a", "b", "c"]))
    assert store.set_name(b, " zeta ") and store.set_name(c, "alpha")
    store.set_pinned(a, True)
    assert [s.id for s in store.snippets()] == [c, b, a]
    assert store.get(b).name == "zeta" and store.get(b).pinned
    assert not store.set_name(999, "x")
    store.delete(b)
    assert store._conn.execute("SELECT COUNT(*) FROM clip_keeper_meta WHERE clip_id = ?", (b,)).fetchone()[0] == 0


def test_html_is_kept_with_the_text():
    store = Store(memory=True)
    clip_id = store.insert_text("bold", html="<b>bold</b>")
    assert store.get(clip_id).html == "<b>bold</b>"
    assert store.recent()[0].html == ""  # lists stay light


def test_files_kind():
    store = Store(memory=True)
    uris = ["file:///home/u/a%20b.txt", "file:///home/u/dir/"]
    clip_id = store.insert_files(uris)
    clip = store.get(clip_id)
    assert clip.is_files and clip.text.splitlines() == uris and clip.preview == "2 files: a b.txt, dir"
    assert store.full_text_matches("b.txt") == [clip_id]
    assert files_preview(["file:///x"]) == "1 file: x"
    assert files_preview([f"file:///f{i}" for i in range(5)]).endswith("…")


def test_latest_ignores_bad_n():
    assert Store(memory=True).latest(0) is None


def test_encrypted_store_hides_everything_on_disk(tmp_path):
    path = tmp_path / "h.db"
    store = Store(path, cipher=cipher())
    clip_id = store.insert_text("top secret text", html="<i>top secret</i>")
    store.set_name(clip_id, "secret name")
    img = store.insert_image(b"PNGDATA", 2, 3)
    assert store.encrypted
    raw = store._conn.execute("SELECT content, preview, hash FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert all(is_encrypted(v) for v in raw) and raw[2] != content_hash("text", b"top secret text")
    meta = store._conn.execute("SELECT name, html FROM clip_keeper_meta").fetchone()
    assert all(is_encrypted(v) for v in meta)
    assert store._conn.execute("SELECT name FROM sqlite_master WHERE name = 'clip_keeper_fts'").fetchone() is None
    clip = store.get(clip_id)
    assert (clip.text, clip.html, clip.name) == ("top secret text", "<i>top secret</i>", "secret name")
    image = store.get(img)
    assert store.image_path(image).name.endswith(".png.enc") and b"PNGDATA" not in store.image_path(image).read_bytes()
    assert store.image_bytes(image) == b"PNGDATA"
    assert store.full_text_matches("SECRET text") == [clip_id]
    assert store.insert_text("top secret text") == clip_id  # keyed hash still collapses duplicates
    store.close()
    assert b"top secret" not in path.read_bytes()


def test_reading_encrypted_rows_without_the_key_shows_a_lock(tmp_path):
    path = tmp_path / "h.db"
    Store(path, cipher=cipher()).insert_text("hidden")
    assert Store(path).recent()[0].text == "🔒 encrypted"


def test_reencrypt_round_trip(tmp_path):
    path = tmp_path / "h.db"
    store = Store(path)
    text_id = store.insert_text("plain words", html="<b>x</b>")
    store.set_name(text_id, "nm")
    img_id = store.insert_image(b"IMG", 1, 1)
    plain_image = store.image_path(store.get(img_id))
    assert store.reencrypt(cipher()) == 2
    assert not plain_image.exists() and store.encrypted
    assert store.get(text_id).text == "plain words" and store.get(img_id) and store.image_bytes(store.get(img_id)) == b"IMG"
    store.close()
    assert b"plain words" not in path.read_bytes()
    store = Store(path, cipher=cipher())
    assert store.reencrypt(cipher(2)) == 2                      # key rotation
    assert store.reencrypt(None) == 2                           # back to plain, copycat-readable
    clip = store.get(text_id)
    assert (clip.text, clip.html, clip.name, clip.hash) == ("plain words", "<b>x</b>", "nm",
                                                            content_hash("text", b"plain words"))
    assert store.full_text_matches("plain") == [text_id]
    assert store.image_bytes(store.get(img_id)) == b"IMG"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_wrong_key_fails_loudly(tmp_path):
    path = tmp_path / "h.db"
    Store(path, cipher=cipher(1)).insert_text("x")
    store = Store(path, cipher=cipher(2))
    assert store.recent()[0].text == "🔒 unreadable (different key)"
    with pytest.raises(ValueError, match="cannot open"):
        store.reencrypt(None)


def test_reencrypt_refuses_to_lose_unreadable_rows(tmp_path):
    path = tmp_path / "h.db"
    Store(path, cipher=cipher()).insert_text("x")
    with pytest.raises(ValueError, match="cannot open"):
        Store(path).reencrypt(cipher())


def test_text_that_looks_encrypted_is_still_plain(tmp_path):
    path = tmp_path / "h.db"
    store = Store(path)
    spoof = store.insert_text("ck1:not really encrypted")
    assert store.get(spoof).text == "ck1:not really encrypted" and not store.get(spoof).locked
    assert store.full_text_matches("really") == [spoof]
    assert store.reencrypt(cipher()) == 1 and store.get(spoof).text == "ck1:not really encrypted"
    assert store.reencrypt(None) == 1 and store.get(spoof).text == "ck1:not really encrypted"


def test_plain_rows_added_by_others_are_sealed_on_open(tmp_path):
    path = tmp_path / "h.db"
    Store(path, cipher=cipher()).insert_text("sealed one")
    plain = Store(path)                       # e.g. copycat, which knows nothing of encryption
    straggler = plain.insert_text("plain straggler")
    img = plain.insert_image(b"RAWPNG", 1, 1)
    raw_image = plain.image_path(plain.get(img))
    plain.close()
    store = Store(path, cipher=cipher())
    assert store.get(straggler).sealed and store.get(straggler).text == "plain straggler"
    assert not raw_image.exists() and store.image_bytes(store.get(img)) == b"RAWPNG"
    store.close()
    assert b"plain straggler" not in path.read_bytes() and b"RAWPNG" not in b"".join(
        p.read_bytes() for p in (tmp_path / "images").iterdir())


def test_locked_store_never_writes_text(tmp_path):
    path = tmp_path / "h.db"
    clip_id = Store(path, cipher=cipher()).insert_text("x")
    locked = Store(path, locked=True)
    assert locked.locked and locked.get(clip_id).locked and locked.get(clip_id).text == "🔒 encrypted"
    for write in (lambda: locked.insert_text("leak"), lambda: locked.set_name(clip_id, "leak"),
                  lambda: locked.reencrypt(None)):
        with pytest.raises(StoreLocked):
            write()
    assert locked.full_text_matches("anything") == []
    assert locked.set_pinned(clip_id, True) and locked.delete(clip_id)   # harmless edits still work


def test_meta_fields_are_whitelisted():
    store = Store(memory=True)
    with pytest.raises(ValueError, match="unknown field"):
        store._set_meta(store.insert_text("x"), **{"name = 'x' --": "y"})


def test_decrypting_duplicates_keeps_one(tmp_path):
    path = tmp_path / "h.db"
    Store(path, cipher=cipher()).insert_text("same")
    Store(path).insert_text("same")           # plain copy of the same text; no key to collapse it
    store = Store(path, cipher=cipher())      # opening seals it: identical keyed hash -> one row kept
    assert store.count() == 1 and store.reencrypt(None) == 1


def test_deleted_images_are_wiped(tmp_path):
    store = Store(tmp_path / "h.db")
    clip_id = store.insert_image(b"SECRETPNG", 1, 1)
    path = store.image_path(store.get(clip_id))
    store.delete(clip_id)
    assert not path.exists()
