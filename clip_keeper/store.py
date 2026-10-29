"""The clipboard history: SQLite in exactly copycat's format (copycat-core/src/store.rs and
model.rs), so the same history.db works with either tool. clip-keeper's extras live in
tables of their own (copycat ignores them):

  clip_keeper_meta  snippet names, rich text (HTML) and the "sealed" (encrypted) flag per clip
  clip_keeper_fts   full-text index of clip text (substring search beyond the preview)

Privacy: folder 0700, files 0600, secure_delete on, WAL emptied after every delete, sorts in
memory. With encryption on (crypto.py) every text field is stored encrypted and the
full-text index is dropped, because it would hold plain text. Whether a row is encrypted is
recorded in its own flag, never guessed from the text (a copied text may start with "ck1:")."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from clip_keeper.crypto import Cipher

DEFAULT_HISTORY_CAP = 500      # copycat-core DEFAULT_HISTORY_CAP
DEFAULT_IMAGE_CAP = 50         # screenshots are big: keep only the newest unpinned ones
KINDS = ("text", "image", "files")
PREVIEW_MAX_CHARS = 200        # copycat-core PREVIEW_MAX_CHARS
SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    content    TEXT    NOT NULL DEFAULT '',
    preview    TEXT    NOT NULL,
    hash       TEXT    NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_at);
CREATE TABLE IF NOT EXISTS clip_keeper_meta (
    clip_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL DEFAULT '',
    html    TEXT NOT NULL DEFAULT '',
    sealed  INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER IF NOT EXISTS clip_keeper_meta_gone AFTER DELETE ON clips BEGIN
    DELETE FROM clip_keeper_meta WHERE clip_id = old.id;
END;
"""
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS clip_keeper_fts USING fts5(content, tokenize = 'trigram');
CREATE TRIGGER IF NOT EXISTS clip_keeper_fts_add AFTER INSERT ON clips
    WHEN new.kind IN ('text', 'files') BEGIN
    INSERT INTO clip_keeper_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS clip_keeper_fts_gone AFTER DELETE ON clips BEGIN
    DELETE FROM clip_keeper_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS clip_keeper_fts_edit AFTER UPDATE OF content ON clips BEGIN
    DELETE FROM clip_keeper_fts WHERE rowid = old.id;
    INSERT INTO clip_keeper_fts(rowid, content)
        SELECT new.id, new.content WHERE new.kind IN ('text', 'files');
END;
"""
DROP_FTS = """
DROP TRIGGER IF EXISTS clip_keeper_fts_add; DROP TRIGGER IF EXISTS clip_keeper_fts_gone;
DROP TRIGGER IF EXISTS clip_keeper_fts_edit; DROP TABLE IF EXISTS clip_keeper_fts;
"""
LISTED = ("SELECT c.id, c.kind, '', c.preview, c.hash, c.created_at, c.pinned, COALESCE(m.name, ''), '', "
          "COALESCE(m.sealed, 0) "
          "FROM clips c LEFT JOIN clip_keeper_meta m ON m.clip_id = c.id WHERE c.kind IN ('text', 'image', 'files')")
FULL = ("SELECT c.id, c.kind, c.content, c.preview, c.hash, c.created_at, c.pinned, COALESCE(m.name, ''), "
        "COALESCE(m.html, ''), COALESCE(m.sealed, 0) FROM clips c LEFT JOIN clip_keeper_meta m ON m.clip_id = c.id "
        "WHERE c.kind IN ('text', 'image', 'files')")
NEWEST = "ORDER BY c.pinned DESC, c.created_at DESC, c.id DESC"
META_COLUMNS = ("name", "html")
LOCKED_TEXT = "🔒 encrypted"
UNREADABLE_TEXT = "🔒 unreadable (different key)"
SCAN_LIMIT_BYTES = 64 * 1024 * 1024  # encrypted search: stop decrypting after this much text


class StoreLocked(ValueError):
    """Encryption is on but the key is out of reach: writing would store plain text."""


@dataclass(frozen=True)
class Clip:
    id: int
    kind: str
    content: str
    preview: str
    hash: str
    created_at: int  # milliseconds since the epoch, like copycat
    pinned: bool
    name: str = ""   # snippet name (clip-keeper only)
    html: str = ""   # rich text, when the copy carried it (only loaded by get())
    sealed: bool = False  # stored encrypted
    locked: bool = False  # sealed and this store cannot open it: the text is a placeholder

    @property
    def text(self) -> str:
        """What copy-back restores; very old copycat rows only had a preview."""
        return self.content or self.preview

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def is_files(self) -> bool:
        return self.kind == "files"


def content_hash(kind: str, data: bytes) -> str:
    """copycat's content_hash: sha256(kind + NUL + bytes), hex."""
    return hashlib.sha256(kind.encode() + b"\0" + data).hexdigest()


def preview_of(text: str) -> str:
    trimmed = text.strip()
    return trimmed if len(trimmed) <= PREVIEW_MAX_CHARS else trimmed[:PREVIEW_MAX_CHARS] + "…"


def files_preview(uris: list[str]) -> str:
    names = [unquote(u.rstrip("/").rsplit("/", 1)[-1]) for u in uris]
    shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
    return preview_of(f"{len(names)} file{'s' if len(names) != 1 else ''}: {shown}")


def one_line(text: str) -> str:
    return " ".join(text.split())


def now_millis() -> int:
    return int(time.time() * 1000)


def _wipe(path: Path) -> None:
    """Overwrite a file with zeros before deleting it (best effort: SSDs and filesystem
    journals may still hold old blocks)."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(b"\0" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


class Store:
    def __init__(self, path: Path | None = None, memory: bool = False, cipher: Cipher | None = None,
                 locked: bool = False):
        """locked=True: encryption is on but the key is unavailable — reading shows locked
        placeholders, deleting works, and anything that would write clip text raises."""
        self._cipher = cipher
        self._locked = locked and cipher is None
        self.scrubbed = True  # False when the last WAL cleanup could not finish
        if memory:
            self._path = None
            self._images = Path(tempfile.mkdtemp(prefix="clip-keeper-images-"))
            self._conn = sqlite3.connect(":memory:")
        else:
            from clip_keeper import paths
            self._path = path or paths.db_path()
            self._images = self._path.parent / "images"
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._path.parent, 0o700)
            old = os.umask(0o077)  # the history holds whatever was copied: owner-only from birth
            try:
                self._conn = sqlite3.connect(str(self._path), timeout=5)
            finally:
                os.umask(old)
        # temp_store=MEMORY: sorts never spill clip text to /tmp files.
        self._conn.executescript("PRAGMA journal_mode = WAL; PRAGMA secure_delete = ON; PRAGMA temp_store = MEMORY;"
                                 + SCHEMA)
        self._migrate()
        if not self._locked:
            self._set_up_search()
            if self._cipher is not None and self._has_plain_rows():
                self._rewrite(self._cipher, only_plain=True)  # e.g. rows copycat added meanwhile
        self._lock_down()

    @property
    def encrypted(self) -> bool:
        return self._cipher is not None

    @property
    def locked(self) -> bool:
        return self._locked

    def close(self) -> None:
        self._conn.close()

    # ── housekeeping ──────────────────────────────────────────────────────────
    def _lock_down(self) -> None:
        if self._path is None:
            return
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._path) + suffix)
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def _add_column(self, table: str, column: str, ddl: str) -> None:
        if column in {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}:
            return
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):  # another process migrated first
                raise

    def _migrate(self) -> None:
        self._add_column("clips", "content", "content TEXT NOT NULL DEFAULT ''")
        self._add_column("clip_keeper_meta", "sealed", "sealed INTEGER NOT NULL DEFAULT 0")

    def _set_up_search(self) -> None:
        if self._cipher is not None:
            self._conn.executescript(DROP_FTS)  # an index of plain text must not exist then
            return
        self._conn.executescript(FTS_SCHEMA)
        self._conn.execute("""INSERT INTO clip_keeper_fts(rowid, content)
                              SELECT id, content FROM clips WHERE kind IN ('text', 'files')
                              AND id NOT IN (SELECT clip_id FROM clip_keeper_meta WHERE sealed = 1)
                              AND id NOT IN (SELECT rowid FROM clip_keeper_fts)""")
        self._conn.commit()

    def _scrub(self) -> bool:
        """Deleted text also sits in the WAL until a checkpoint: fold it back into the
        (secure_delete) database and empty the WAL. Retries while another process reads;
        False if a reader kept it from finishing (the next delete tries again)."""
        for _ in range(40):
            busy = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            if not busy:
                self.scrubbed = True
                return True
            time.sleep(0.05)
        self.scrubbed = False
        return False

    def _guard(self) -> None:
        if self._locked:
            raise StoreLocked("the history is encrypted and GNOME Keyring is locked: nothing was written")

    # ── encryption helpers ────────────────────────────────────────────────────
    def _seal(self, text: str, cipher: Cipher | None = None) -> str:
        cipher = cipher or self._cipher
        return cipher.encrypt(text) if cipher and text else text

    def _hash(self, kind: str, data: bytes, cipher: Cipher | None = None) -> str:
        cipher = cipher or self._cipher
        return cipher.keyed_hash(kind, data) if cipher else content_hash(kind, data)

    def _clip(self, row) -> Clip:
        sealed = bool(row[9])
        fields = [str(row[i]) for i in (2, 3, 7, 8)]
        locked = False
        if sealed:
            try:
                if self._cipher is None:
                    raise ValueError("no key")
                fields = [self._cipher.decrypt(f) if f else f for f in fields]
            except ValueError:
                locked = True
                placeholder = LOCKED_TEXT if self._cipher is None else UNREADABLE_TEXT
                fields = ["", placeholder, "", ""]
        content, preview, name, html = fields
        return Clip(int(row[0]), str(row[1]), content, preview, str(row[4]), int(row[5]), bool(row[6]),
                    name, html, sealed, locked)

    # ── writes ────────────────────────────────────────────────────────────────
    def _insert(self, kind: str, content: str, preview: str, digest: str, created_at: int | None) -> tuple[int, bool]:
        """(id, created). An identical clip is moved back to the top (copycat's duplicate collapsing)."""
        self._guard()
        created_at = created_at if created_at is not None else now_millis()
        with self._conn:
            row = self._conn.execute("SELECT id FROM clips WHERE hash = ?", (digest,)).fetchone()
            if row:
                self._conn.execute("UPDATE clips SET created_at = ? WHERE id = ?", (created_at, row[0]))
                return int(row[0]), False
            cur = self._conn.execute(
                "INSERT INTO clips (kind, content, preview, hash, created_at, pinned) VALUES (?, ?, ?, ?, ?, 0)",
                (kind, self._seal(content), self._seal(preview), digest, created_at))
            if self._cipher is not None:
                self._conn.execute("INSERT INTO clip_keeper_meta (clip_id, sealed) VALUES (?, 1)", (cur.lastrowid,))
        self._lock_down()
        return int(cur.lastrowid), True

    def insert_text(self, text: str, created_at: int | None = None, html: str = "") -> int:
        clip_id, _ = self._insert("text", text, preview_of(text), self._hash("text", text.encode("utf-8")), created_at)
        if html:
            self._set_meta(clip_id, html=html)
        return clip_id

    def insert_files(self, uris: list[str], created_at: int | None = None) -> int:
        content = "\n".join(uris)
        clip_id, _ = self._insert("files", content, files_preview(uris), self._hash("files", content.encode()),
                                  created_at)
        return clip_id

    def _image_file(self, digest: str, sealed: bool) -> Path:
        return self._images / f"{digest.replace(':', '_')}.png{'.enc' if sealed else ''}"

    def image_path(self, clip: Clip) -> Path:
        """Images live as files named by their content hash (the row keeps copycat's format)."""
        return self._image_file(clip.hash, clip.sealed)

    def image_bytes(self, clip: Clip) -> bytes:
        """The PNG; raises OSError if the file is gone, ValueError if it cannot be decrypted."""
        data = self.image_path(clip).read_bytes()
        if not clip.sealed:
            return data
        if self._cipher is None:
            raise ValueError("the image is encrypted and the key is unavailable")
        return self._cipher.decrypt_bytes(data)

    def insert_image(self, png: bytes, width: int, height: int, created_at: int | None = None) -> int:
        digest = self._hash("image", png)
        clip_id, created = self._insert("image", "", f"image {width}×{height}", digest, created_at)
        if created:
            self._images.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_private(self._image_file(digest, self.encrypted),
                           self._cipher.encrypt_bytes(png) if self._cipher else png)
        return clip_id

    def _set_meta(self, clip_id: int, **fields: str) -> None:
        self._guard()
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO clip_keeper_meta (clip_id) VALUES (?)", (clip_id,))
            for column, value in fields.items():
                if column not in META_COLUMNS:
                    raise ValueError(f"unknown field {column!r}")
                self._conn.execute(f"UPDATE clip_keeper_meta SET {column} = ? WHERE clip_id = ?",  # noqa: S608
                                   (self._seal(value), clip_id))

    def set_name(self, clip_id: int, name: str) -> bool:
        """Name a clip (a snippet); naming also pins it so it is never trimmed or expired."""
        if self.get(clip_id) is None:
            return False
        self._set_meta(clip_id, name=name.strip())
        if name.strip():
            self.set_pinned(clip_id, True)
        return True

    def set_pinned(self, clip_id: int, pinned: bool) -> bool:
        with self._conn:
            return self._conn.execute("UPDATE clips SET pinned = ? WHERE id = ?", (int(pinned), clip_id)).rowcount > 0

    def _delete_where(self, where: str, params: tuple = ()) -> int:
        """`where` is always a constant from this class; values go in `params`."""
        with self._conn:
            doomed = self._conn.execute(f"SELECT id, kind, hash FROM clips WHERE {where}", params).fetchall()  # noqa: S608
            for clip_id, _, _ in doomed:
                self._conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        if doomed:
            for _, kind, digest in doomed:
                if kind == "image":
                    for sealed in (False, True):
                        _wipe(self._image_file(digest, sealed))
            self._scrub()
        return len(doomed)

    def delete(self, clip_id: int) -> bool:
        return self._delete_where("id = ?", (clip_id,)) > 0

    def clear_unpinned(self) -> int:
        return self._delete_where("pinned = 0")

    def expire(self, older_than_days: int, now_ms: int | None = None) -> int:
        """Delete unpinned clips older than this many days (0 = keep forever)."""
        if older_than_days <= 0:
            return 0
        cutoff = (now_ms if now_ms is not None else now_millis()) - older_than_days * 86_400_000
        return self._delete_where("pinned = 0 AND created_at < ?", (cutoff,))

    def enforce_cap(self, cap: int = DEFAULT_HISTORY_CAP, image_cap: int = DEFAULT_IMAGE_CAP) -> int:
        """Keep the newest `cap` unpinned clips, of which at most `image_cap` images."""
        newest_any = "SELECT id FROM clips WHERE pinned = 0 ORDER BY created_at DESC, id DESC LIMIT ?"
        newest_images = "SELECT id FROM clips WHERE pinned = 0 AND kind = 'image' ORDER BY created_at DESC, id DESC LIMIT ?"
        return self._delete_where(f"pinned = 0 AND (id NOT IN ({newest_any}) OR "
                                  f"(kind = 'image' AND id NOT IN ({newest_images})))", (cap, image_cap))

    # ── reads ─────────────────────────────────────────────────────────────────
    def recent(self, limit: int = DEFAULT_HISTORY_CAP) -> list[Clip]:
        """Pinned first, then newest. Only previews are loaded (content=""), so lists stay
        light even with 1 MiB clips; get() fetches the full clip when needed."""
        return [self._clip(r) for r in self._conn.execute(f"{LISTED} {NEWEST} LIMIT ?", (limit,))]

    def snippets(self) -> list[Clip]:
        """Pinned clips in snippet order: named ones by name, then the rest newest first."""
        rows = self._conn.execute(f"{LISTED} AND c.pinned = 1 ORDER BY c.created_at DESC, c.id DESC")
        clips = [self._clip(r) for r in rows]
        return sorted(clips, key=lambda c: (c.name == "", c.name.lower()))

    def latest(self, n: int) -> Clip | None:
        """The n-th most recently copied clip (1 = last), by copy time only — pins don't count."""
        if n < 1:
            return None
        row = self._conn.execute(f"{LISTED} ORDER BY c.created_at DESC, c.id DESC LIMIT 1 OFFSET ?",
                                 (n - 1,)).fetchone()
        return self.get(int(row[0])) if row else None

    def get(self, clip_id: int) -> Clip | None:
        row = self._conn.execute(f"{FULL} AND c.id = ?", (clip_id,)).fetchone()
        return self._clip(row) if row else None

    def full_text_matches(self, query: str, limit: int = 200) -> list[int]:
        """Ids whose full text contains `query` (case-insensitive), newest first. Plain
        histories use the trigram index (3+ characters; shorter queries only match previews,
        which the fuzzy search covers); encrypted ones decrypt and scan, up to a budget."""
        query = query.strip()
        if not query:
            return []
        if self._cipher is None:
            if len(query) < 3 or self._locked:
                return []
            phrase = '"' + query.replace('"', '""') + '"'
            rows = self._conn.execute(
                "SELECT c.id FROM clip_keeper_fts f JOIN clips c ON c.id = f.rowid WHERE clip_keeper_fts MATCH ? "
                "ORDER BY c.pinned DESC, c.created_at DESC LIMIT ?", (phrase, limit))
            return [int(r[0]) for r in rows]
        needle, found, scanned = query.lower(), [], 0
        for row in self._conn.execute(f"{FULL} AND c.kind IN ('text', 'files') {NEWEST}"):
            clip = self._clip(row)
            scanned += len(clip.text)
            if needle in clip.text.lower():
                found.append(clip.id)
            if len(found) >= limit or scanned > SCAN_LIMIT_BYTES:
                break
        return found

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0])

    # ── encryption switch ─────────────────────────────────────────────────────
    def reencrypt(self, new_cipher: Cipher | None) -> int:
        """Rewrite every row (and image) with `new_cipher` (None = decrypt to plain). Every
        encrypted row must open with the current key, or nothing changes. Returns rows kept."""
        self._guard()
        return self._rewrite(new_cipher, only_plain=False)

    def _has_plain_rows(self) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM clips WHERE id NOT IN (SELECT clip_id FROM clip_keeper_meta WHERE sealed = 1) LIMIT 1"
        ).fetchone() is not None

    def _rewrite(self, new_cipher: Cipher | None, only_plain: bool) -> int:
        """One transaction that blocks other writers (the tray, copycat) from the first read
        to the last write, so no row can slip in between in the old format."""
        self._conn.execute("BEGIN IMMEDIATE")
        files: list[tuple[Path | None, Path | None, Path]] = []  # (temporary, final, old); None: dropped
        try:
            where = " AND COALESCE(m.sealed, 0) = 0" if only_plain else ""
            clips = [self._clip_strict(r) for r in self._conn.execute(f"{FULL}{where}").fetchall()]
            kept = sum(self._rewrite_one(c, data, new_cipher, files) for c, data in clips)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            for temporary, _, _ in files:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            raise
        finals = {final for _, final, _ in files if final is not None}
        for temporary, final, _ in files:
            if temporary is not None:
                os.replace(temporary, final)
        for _, _, old in files:
            if old not in finals:
                _wipe(old)
        self._cipher = new_cipher
        self._set_up_search()
        self._conn.execute("VACUUM")  # rewrite the file: no page keeps the old form
        self._scrub()
        self._lock_down()
        return kept

    def _clip_strict(self, row) -> tuple[Clip, bytes | None]:
        """The clip in plain form plus its image bytes; raises ValueError if it can't be opened."""
        clip = self._clip(row)
        if clip.locked:
            raise ValueError("the history holds clips this key cannot open: nothing changed")
        image = None
        if clip.is_image and self.image_path(clip).exists():
            image = self.image_bytes(clip)
        return clip, image

    def _rewrite_one(self, clip: Clip, image: bytes | None, cipher: Cipher | None, files: list) -> int:
        data = image if image is not None else clip.text.encode("utf-8")
        digest = self._hash(clip.kind, data, cipher) if cipher else content_hash(clip.kind, data)
        seal = (lambda t: self._seal(t, cipher)) if cipher else (lambda t: t)
        try:
            self._conn.execute("UPDATE clips SET content = ?, preview = ?, hash = ? WHERE id = ?",
                               (seal(clip.content), seal(clip.preview), digest, clip.id))
        except sqlite3.IntegrityError:  # the same clip exists twice (old and new form): keep one
            self._conn.execute("DELETE FROM clips WHERE id = ?", (clip.id,))
            if clip.is_image:
                files.append((None, None, self.image_path(clip)))
            return 0
        self._conn.execute("INSERT OR IGNORE INTO clip_keeper_meta (clip_id) VALUES (?)", (clip.id,))
        self._conn.execute("UPDATE clip_keeper_meta SET name = ?, html = ?, sealed = ? WHERE clip_id = ?",
                           (seal(clip.name), seal(clip.html), int(cipher is not None), clip.id))
        if image is not None:
            self._images.mkdir(mode=0o700, parents=True, exist_ok=True)
            final = self._image_file(digest, cipher is not None)
            temporary = final.with_name(final.name + ".new")
            _write_private(temporary, cipher.encrypt_bytes(image) if cipher else image)
            files.append((temporary, final, self.image_path(clip)))
        return 1
