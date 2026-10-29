"""Logic inside the GTK modules that runs without a display."""

import json
import types

import pytest

gi = pytest.importorskip("gi")

from clip_keeper import clipboard  # noqa: E402


def test_classify():
    assert clipboard.classify({"UTF8_STRING", "x-kde-passwordManagerHint"}) == "secret"
    assert clipboard.classify({"TARGETS", "UTF8_STRING"}) == "text"
    assert clipboard.classify({"image/png"}) == "image"
    assert clipboard.classify({"image/png", "UTF8_STRING"}) == "text"
    assert clipboard.classify({"application/x-thing"}) == "other"


class FakeClipboard:
    def __init__(self):
        self.text_callbacks, self.target_callbacks = [], []

    def request_targets(self, cb):
        self.target_callbacks.append(cb)

    def request_text(self, cb):
        self.text_callbacks.append(cb)


def atoms(*names):
    return [types.SimpleNamespace(name=lambda n=n: n) for n in names]


def make_watch():
    seen = []
    watch = clipboard.ClipboardWatch.__new__(clipboard.ClipboardWatch)
    watch._cb, watch._on_text, watch._generation = FakeClipboard(), lambda t, h, html="": seen.append((t, h) + ((html,) if html else ())), 0
    return watch, seen


def test_text_is_confirmed_before_use():
    watch, seen = make_watch()
    watch._changed()
    watch._cb.target_callbacks.pop(0)(None, atoms("UTF8_STRING"))
    watch._cb.text_callbacks.pop(0)(None, "hello")
    watch._cb.target_callbacks.pop(0)(None, atoms("UTF8_STRING"))
    assert seen == [("hello", False)]


def test_a_password_manager_taking_the_clipboard_mid_read_is_never_stored():
    watch, seen = make_watch()
    watch._changed()                                              # app A copies
    watch._cb.target_callbacks.pop(0)(None, atoms("UTF8_STRING"))  # A: plain text
    watch._changed()                                              # KeePassXC takes the clipboard
    watch._cb.text_callbacks.pop(0)(None, "the-password")          # A's text request returns B's secret
    assert seen == []                                             # dropped: the generation moved on
    watch._cb.target_callbacks.pop(0)(None, atoms("UTF8_STRING", "x-kde-passwordManagerHint"))
    assert seen == [(None, True)]                                 # B is recognised as a secret, never read


def test_secret_flag_appearing_at_confirmation_drops_the_text():
    watch, seen = make_watch()
    watch._changed()
    watch._cb.target_callbacks.pop(0)(None, atoms("UTF8_STRING"))
    watch._cb.text_callbacks.pop(0)(None, "x")
    watch._cb.target_callbacks.pop(0)(None, atoms("UTF8_STRING", "x-kde-passwordManagerHint"))
    assert seen == []


def test_tray_socket_survives_bad_requests(monkeypatch):
    from clip_keeper import tray as tray_mod
    t = tray_mod.Tray.__new__(tray_mod.Tray)
    t._on_main_thread = lambda fn: fn()
    t.handle = lambda message: {"ok": True, "echo": message}  # noqa

    class Conn:
        def __init__(self, payload):
            self.payload, self.sent = payload, b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, s):
            pass

        def recv(self, n):
            chunk, self.payload = self.payload[:n], self.payload[n:]
            return chunk

        def sendall(self, data):
            self.sent += data

    for payload in (b"[" * 100_000 + b"\n", b"not json\n", b"\xff\xfe\n", b"x" * 200_000, b"y" * (9 << 20)):
        conn = Conn(payload)
        t._answer(conn)
        assert json.loads(conn.sent)["ok"] is False
    good = Conn(b'{"cmd": "status"}\n')
    t._answer(good)
    assert json.loads(good.sent) == {"ok": True, "echo": {"cmd": "status"}}


class FakeWatch:
    def __init__(self):
        self.calls = []

    def set_text(self, text, html=""):
        self.calls.append(("text", text, html))

    def set_files(self, uris):
        self.calls.append(("files", uris))

    def set_image(self, png):
        self.calls.append(("image", png))


def make_tray():
    from clip_keeper import tray as tray_mod
    from clip_keeper.capture import Capturer
    from clip_keeper.settings import Settings
    from clip_keeper.store import Store
    t = tray_mod.Tray.__new__(tray_mod.Tray)
    t._store = Store(memory=True)
    t._capturer, t._watch, t._settings = Capturer(t._store), FakeWatch(), Settings(auto_paste=False)
    t._locked, t._keyboard, t._keyboard_problem = "", None, ""
    t._render = lambda: None
    return t


def test_tray_copies_each_kind_and_transforms():
    t = make_tray()
    text = t._store.insert_text(' {"a": 1} ', html="<b>x</b>")
    files = t._store.insert_files(["file:///a", "file:///b"])
    image = t._store.insert_image(b"PNG", 1, 1)
    assert t.handle({"cmd": "copy", "id": text}) == {"ok": True}
    assert t._watch.calls[-1] == ("text", ' {"a": 1} ', "<b>x</b>")
    assert t.handle({"cmd": "copy", "id": files})["ok"] and t._watch.calls[-1] == ("files", ["file:///a", "file:///b"])
    assert t.handle({"cmd": "copy", "id": image})["ok"] and t._watch.calls[-1] == ("image", b"PNG")
    assert t._capturer.handle_image(b"other", 1, 1, t._settings) == "own"
    assert t.handle({"cmd": "copy", "id": text, "transform": "json_compact"})["ok"]
    assert t._watch.calls[-1] == ("text", '{"a":1}', "")
    assert t._capturer.handle('{"a":1}', False, t._settings) == "own"   # not recorded
    assert t.handle({"cmd": "copy", "id": text, "transform": "base64_decode"})["ok"] is False
    assert "unknown transform" in t.handle({"cmd": "copy", "id": text, "transform": "rm -rf"})["error"]
    assert t.handle({"cmd": "copy", "id": 999}) == {"ok": False, "error": "the clip is gone"}


def test_tray_copy_text_pause_skip_and_status():
    t = make_tray()
    assert t.handle({"cmd": "copy_text", "text": "a\nb", "paste": "ctrl_v"}) == {
        "ok": True, "paste": {"ok": False, "error": "auto-paste is off"}}
    assert t.handle({"cmd": "copy_text", "text": "x" * (1024 * 1024 + 1)})["ok"] is False
    assert t.handle({"cmd": "copy_text", "text": 5})["ok"] is False
    assert t.handle({"cmd": "skip_next"}) == {"ok": True} and t._capturer.skip_next
    assert t.handle({"cmd": "pause_for", "seconds": 10**9}) == {"ok": True}
    status = t.handle({"cmd": "status"})
    assert status["skip_next"] and status["paused_seconds_left"] == 86_400 and status["encrypted"] is False
    assert t.handle({"cmd": "pause_for", "seconds": "x"})["ok"] is False
    assert t.handle(["not", "a", "dict"])["ok"] is False


def test_tray_never_records_while_locked(monkeypatch):
    from clip_keeper import tray as tray_mod
    from clip_keeper.store import Store
    t = make_tray()
    monkeypatch.setattr(tray_mod, "open_store", lambda s: (Store(memory=True, locked=True), "history is locked"))
    t._reopen()
    assert t._locked
    t._on_clipboard("secret-ish", False)
    assert t._store.count() == 0
    t._capturer.skip_next = True
    t._reopen()                                   # still locked: nothing churns
    assert t._capturer.skip_next


def test_tray_reopens_before_recording_when_encryption_switched(monkeypatch):
    from clip_keeper import crypto, tray as tray_mod
    from clip_keeper.store import Store
    t = make_tray()
    sealed = Store(memory=True, cipher=crypto.Cipher(b"k" * 32))
    monkeypatch.setattr(tray_mod.settings, "load", lambda: t._settings.__class__(encrypt=True))
    monkeypatch.setattr(tray_mod, "open_store", lambda s: (sealed, ""))
    t._on_clipboard("after the switch", False)
    assert t._store is sealed and sealed.latest(1).sealed
    assert t.handle({"cmd": "reopen"}) == {"ok": True, "encrypted": True, "locked": ""}


def test_private_menu_titles():
    from clip_keeper.settings import Settings
    t = make_tray()
    clip = t._store.get(t._store.insert_text("visible words"))
    assert "visible words" in t._menu_title(1, clip)
    t._settings = Settings(private_menu=True)
    assert t._menu_title(4, clip) == "Clip 4 (text)"
    t._store.set_name(clip.id, "sig")
    assert t._menu_title(4, t._store.get(clip.id)) == "📌 sig (text)"


def test_joined_text_for_multi_select():
    from clip_keeper.popup import joined_text
    from clip_keeper.store import Store
    store = Store(memory=True)
    ids = [store.insert_text("one"), store.insert_files(["file:///tmp/a%20b"]), store.insert_image(b"P", 1, 1),
           store.insert_text("two")]
    assert joined_text([store.get(i) for i in ids]) == "one\n/tmp/a b\ntwo"
    assert joined_text([store.get(ids[2])]) == ""
