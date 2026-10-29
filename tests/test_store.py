import hashlib
import sqlite3
import stat

from clip_keeper import paths
from clip_keeper.store import PREVIEW_MAX_CHARS, Store, content_hash, one_line, preview_of

COPYCAT_DDL = """PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS clips (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '', preview TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL, pinned INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_at);"""


def test_hash_matches_copycats_formula():
    # copycat-core model.rs: sha256(kind.as_str() ++ [0u8] ++ bytes), lower-case hex
    assert content_hash("text", b"hello") == hashlib.sha256(b"text\x00hello").hexdigest()
    assert content_hash("text", b"x") != content_hash("image", b"x")


def test_preview_and_one_line_match_copycat():
    assert preview_of("  hi  ") == "hi"
    long = "a" * (PREVIEW_MAX_CHARS + 50)
    assert preview_of(long) == "a" * PREVIEW_MAX_CHARS + "…"
    assert one_line(" a\n\tb   c ") == "a b c"


def test_lives_where_copycat_looks_and_is_private():
    store = Store()
    store.insert_text("secret-ish")
    db = paths.db_path()
    assert db.parts[-2:] == ("copycat", "history.db")
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    for suffix in ("", "-wal", "-shm"):
        f = db.with_name(db.name + suffix)
        if f.exists():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_reads_and_writes_a_database_copycat_created():
    db = paths.db_path()
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(COPYCAT_DDL)
    conn.execute("INSERT INTO clips (kind, content, preview, hash, created_at, pinned) VALUES "
                 "('text', 'from copycat', 'from copycat', ?, 1000, 1)", (content_hash("text", b"from copycat"),))
    conn.commit()
    conn.close()
    store = Store()
    assert [c.text for c in store.recent()] == ["from copycat"]
    store.insert_text("from copycat", created_at=5000)          # same content → bumped, not duplicated
    assert store.count() == 1 and store.recent()[0].created_at == 5000


def test_migrates_a_pre_content_copycat_database():
    db = paths.db_path()
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE clips (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, preview TEXT NOT NULL,"
                 " hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, pinned INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO clips (kind, preview, hash, created_at) VALUES ('text', 'old', 'h', 1)")
    conn.commit()
    conn.close()
    (clip,) = Store().recent()
    assert clip.content == "" and clip.text == "old"  # falls back to the preview, like copycat


def test_order_pins_cap_and_delete():
    store = Store(memory=True)
    a = store.insert_text("a", 1)
    store.insert_text("b", 2)
    c = store.insert_text("c", 3)
    assert [x.text for x in store.recent()] == ["c", "b", "a"]
    store.set_pinned(a, True)
    assert [x.text for x in store.recent()] == ["a", "c", "b"]   # pinned first
    assert store.enforce_cap(1) == 1                              # keeps newest unpinned + all pinned
    assert [x.text for x in store.recent()] == ["a", "c"]
    assert store.delete(c) and not store.delete(c)
    assert store.clear_unpinned() == 0 and store.count() == 1
    assert store.get(a).pinned and store.get(999) is None
    assert store.recent(limit=0) == []


def test_deleted_clips_are_overwritten_on_disk():
    store = Store()
    store.insert_text("hunter2-very-unique-marker")
    store.clear_unpinned()
    store.close()
    db = paths.db_path()
    raw = b"".join(f.read_bytes() for f in db.parent.iterdir())
    assert b"hunter2-very-unique-marker" not in raw


def test_a_single_delete_leaves_nothing_in_the_database_or_wal():
    store = Store()
    clip = store.insert_text("ZZTOPSECRET-marker")
    store.insert_text("keep me")
    assert store.delete(clip)
    db = paths.db_path()
    for f in db.parent.iterdir():          # the tray keeps its connection open: check while it is
        assert b"ZZTOPSECRET-marker" not in f.read_bytes(), f.name


def test_cap_trimming_scrubs_too():
    store = Store()
    store.insert_text("OLD-SECRET-marker", 1)
    store.insert_text("newer", 2)
    assert store.enforce_cap(1) == 1
    for f in paths.db_path().parent.iterdir():
        assert b"OLD-SECRET-marker" not in f.read_bytes()


def test_lists_hold_previews_only_and_skip_non_text_rows():
    store = Store(memory=True)
    big = "x" * 5000
    clip_id = store.insert_text(big)
    store._conn.execute("INSERT INTO clips (kind, content, preview, hash, created_at) VALUES ('rtf', '', '[rtf]', 'h', 99)")
    (listed,) = store.recent()
    assert listed.content == "" and len(listed.text) == 201        # preview (200 chars + …)
    assert store.get(clip_id).text == big                           # full text on demand
    assert store.get(int(store._conn.execute("SELECT id FROM clips WHERE kind='rtf'").fetchone()[0])) is None


def test_images_are_private_files_with_copycat_rows():
    store = Store()
    png = b"\x89PNG fake image bytes"
    clip_id = store.insert_image(png, 1920, 1080, created_at=10)
    clip = store.get(clip_id)
    assert clip.is_image and clip.preview == "image 1920×1080" and clip.hash == content_hash("image", png)
    path = store.image_path(clip)
    assert path.read_bytes() == png and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert store.insert_image(png, 1920, 1080, created_at=20) == clip_id      # same image: bumped
    assert store.count() == 1 and store.get(clip_id).created_at == 20
    assert store.delete(clip_id) and not path.exists()


def test_image_cap_and_clear_remove_files():
    store = Store(memory=True)
    ids = [store.insert_image(f"img{i}".encode(), 10, 10, created_at=i) for i in range(5)]
    store.insert_text("text stays", 100)
    store.set_pinned(ids[0], True)
    paths_before = [store.image_path(store.get(i)) for i in ids]
    assert store.enforce_cap(cap=500, image_cap=2) == 2                       # 4 unpinned images → newest 2
    assert [p.exists() for p in paths_before] == [True, False, False, True, True]
    assert [c.preview for c in store.recent()][0] == "image 10×10"            # the pinned one first
    store.clear_unpinned()
    assert [p.exists() for p in paths_before] == [True, False, False, False, False]
    assert [c.text for c in store.recent()] == ["image 10×10"]
