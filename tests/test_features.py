"""Formats, transforms, capture gates, snippets and the new CLI commands."""

import base64
import json

import pytest

from clip_keeper import cli, crypto, formats, history, settings, shortcut, transforms
from clip_keeper.capture import SKIPPED_ONCE, Capturer
from clip_keeper.settings import Settings
from clip_keeper.store import Store


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ── formats ───────────────────────────────────────────────────────────────────
def test_gnome_files_round_trip():
    uris = ["file:///home/u/My%20File.txt", "file:///tmp/x"]
    assert formats.parse_gnome_files(formats.gnome_files(uris)) == uris
    assert formats.parse_gnome_files(b"cut\r\nfile:///a\r\n") == ["file:///a"]
    assert formats.parse_gnome_files(b"hello\nfile:///a") is None
    assert formats.parse_gnome_files(b"copy\n") is None and formats.parse_gnome_files(None) is None
    assert formats.uri_list(uris) == b"file:///home/u/My%20File.txt\r\nfile:///tmp/x\r\n"
    assert formats.paths_text(uris + ["sftp://h/x"]) == "/home/u/My File.txt\n/tmp/x\nsftp://h/x"


def test_decode_html():
    assert formats.decode_html("<b>é</b>".encode()) == "<b>é</b>"
    assert formats.decode_html("<i>x</i>".encode("utf-16")) == "<i>x</i>"
    assert formats.decode_html(b"a\x00b") == "ab"
    assert formats.decode_html(b"x" * (formats.MAX_HTML_BYTES + 1)) == "" and formats.decode_html(None) == ""


# ── transforms ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key, text, out", [
    ("plain", "x", "x"),
    ("trim", "  a  \n b \n\n", "a\n b"),
    ("one_line", "a\n  b\tc", "a b c"),
    ("json_pretty", '{"a":[1]}', '{\n  "a": [\n    1\n  ]\n}'),
    ("json_compact", '{ "a" : "é" }', '{"a":"é"}'),
    ("url_encode", "a b/&", "a%20b%2F%26"),
    ("url_decode", "a%20b", "a b"),
    ("base64_encode", "hi", "aGk="),
    ("base64_decode", "aGk", "hi"),
    ("base64_decode", base64.urlsafe_b64encode("ü?>".encode()).decode(), "ü?>"),
    ("upper", "aB", "AB"), ("lower", "aB", "ab"), ("title", "a b", "A B"),
])
def test_transforms(key, text, out):
    assert transforms.apply(key, text) == out


@pytest.mark.parametrize("key, text, why", [
    ("json_pretty", "{", "not JSON"), ("base64_decode", "@@@", "not base64"),
    ("base64_decode", base64.b64encode(b"\xff\xfe\x00").decode(), "binary"), ("nope", "x", "unknown"),
])
def test_transform_errors(key, text, why):
    with pytest.raises(ValueError, match=why):
        transforms.apply(key, text)


# ── capture ───────────────────────────────────────────────────────────────────
def test_skip_next_and_timed_pause():
    store, clock = Store(memory=True), Clock()
    cap, s = Capturer(store, clock), Settings()
    cap.skip_next = True
    assert cap.handle("first", False, s) == SKIPPED_ONCE and not cap.skip_next
    clock.t += 3
    assert cap.handle("first", False, s) == "stored"
    cap.pause_for(300)
    assert cap.paused_seconds_left == 300 and cap.handle("during", False, s) == "paused"
    clock.t += 301
    assert cap.paused_seconds_left == 0 and cap.handle("after", False, s) == "stored"
    cap.pause_for(60)
    cap.resume()
    assert cap.handle("resumed", False, s) == "stored"
    assert cap.stats.skipped[SKIPPED_ONCE] == 1


def test_own_copies_are_not_recorded():
    store, clock = Store(memory=True), Clock()
    cap, s = Capturer(store, clock), Settings()
    cap.expect_own("text", b"edited")
    assert cap.handle("edited", False, s) == "own"
    clock.t += 3
    assert cap.handle("edited", False, s) == "stored"          # once only
    cap.expect_own("image")
    assert cap.handle_image(b"re-encoded", 1, 1, s) == "own"     # any image
    cap.expect_own("text", b"old")
    clock.t += 11
    assert cap.handle("old", False, s) == "stored"               # too late: a real copy


def test_capture_html_and_files():
    store, clock = Store(memory=True), Clock()
    cap = Capturer(store, clock)
    assert cap.handle("bold", False, Settings(), html="<b>bold</b>") == "stored"
    assert store.get(store.latest(1).id).html == "<b>bold</b>"
    clock.t += 3
    assert cap.handle("plain", False, Settings(record_html=False), html="<i>x</i>") == "stored"
    assert store.latest(1).html == ""
    assert cap.handle_files(["file:///a"], Settings()) == "stored" and store.latest(1).is_files
    assert cap.handle_files(["file:///b"], Settings(record_files=False)) == "files are off"
    assert cap.handle_files([], Settings()) == "empty"
    assert cap.handle_image(b"x", 1, 1, Settings(record_images=False)) == "images are off"


def test_new_settings_are_validated(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"expire_days": 99999, "private_menu": "yes", "encrypt": True}))
    loaded = settings.load(path)
    assert loaded.expire_days == 3650 and loaded.private_menu is False and loaded.encrypt is True


# ── history / encryption switch ───────────────────────────────────────────────
def test_open_store_with_and_without_key():
    store, problem = history.open_store(Settings())
    assert not store.encrypted and problem == ""
    store, problem = history.open_store(Settings(encrypt=True), key_loader=lambda: b"k" * 32)
    assert store.encrypted and problem == ""

    def locked():
        raise crypto.KeyUnavailable("keyring locked")
    store, problem = history.open_store(Settings(encrypt=True), key_loader=locked)
    assert not store.encrypted and store.locked and "locked" in problem
    store, problem = history.open_store(Settings(encrypt=True), key_loader=lambda: b"short")
    assert store.locked and "32 bytes" in problem


def test_cli_encrypt_on_and_off(capsys, monkeypatch):
    keyring = {}
    monkeypatch.setattr(crypto, "load_key", lambda create=False: keyring.setdefault("k", b"z" * 32) if create
                        else keyring.get("k") or (_ for _ in ()).throw(crypto.KeyUnavailable("none")))
    monkeypatch.setattr(crypto, "delete_key", lambda: keyring.clear())
    monkeypatch.setattr(history, "load_key", lambda: crypto.load_key())
    clip_id = Store().insert_text("private words")
    assert cli.main(["encrypt", "on"]) == 0 and "1 clips rewritten" in capsys.readouterr().out
    assert settings.load().encrypt and Store().get(clip_id).text == "🔒 encrypted"
    assert cli.main(["encrypt", "on"]) == 0 and "already" in capsys.readouterr().out
    assert cli.main(["list"]) == 0 and "private words" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_tray_switched", lambda on: False)        # tray didn't confirm: key kept
    assert cli.main(["encrypt", "off"]) == 0 and keyring and "key was kept" in capsys.readouterr().out
    assert cli.main(["encrypt", "on"]) == 0
    monkeypatch.setattr(cli, "_tray_switched", lambda on: True)
    assert cli.main(["encrypt", "off"]) == 0 and not keyring
    assert Store().get(clip_id).text == "private words" and not settings.load().encrypt


# ── snippets, names and switches through the CLI ──────────────────────────────
def test_snippets_name_and_paste_snippet(capsys, monkeypatch):
    store = Store()
    a, b = store.insert_text("alpha text", created_at=1), store.insert_text("beta text", created_at=2)
    assert cli.main(["snippets"]) == 0 and "no snippets" in capsys.readouterr().out
    assert cli.main(["name", str(a), "my", "sig"]) == 0
    assert cli.main(["name", "999", "x"]) == 1
    Store().set_pinned(b, True)
    capsys.readouterr()
    assert cli.main(["snippets"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Super+Alt+3") and "[my sig]" in out[0] and "beta" in out[1]
    sent = []
    monkeypatch.setattr(cli.ipc, "request", lambda cmd, **kw: sent.append((cmd, kw)) or {"ok": True})
    assert cli.main(["paste-snippet", "2", "--terminal"]) == 0
    assert sent[-1] == ("copy", {"id": b, "paste": "ctrl_shift_v", "delay_ms": cli.SHORTCUT_PASTE_DELAY_MS})
    assert cli.main(["paste-snippet", "9"]) == 1
    assert cli.main(["name", str(a)]) == 0 and Store().get(a).name == ""


def test_skip_pause_expire_and_switches(capsys, monkeypatch):
    sent = []
    monkeypatch.setattr(cli.ipc, "request", lambda cmd, **kw: sent.append((cmd, kw)) or {"ok": True})
    assert cli.main(["skip-next"]) == 0 and sent[-1] == ("skip_next", {})
    assert cli.main(["pause", "--minutes", "5"]) == 0 and sent[-1] == ("pause_for", {"seconds": 300})
    assert not settings.load().paused
    assert cli.main(["resume"]) == 0 and ("pause_for", {"seconds": 0}) in sent
    Store().insert_text("ancient", created_at=1)
    assert cli.main(["expire", "3"]) == 0 and "removed 1" in capsys.readouterr().out
    assert settings.load().expire_days == 3
    assert cli.main(["expire", "0"]) == 0 and "never" in capsys.readouterr().out
    for name, field in (("files", "record_files"), ("rich-text", "record_html"), ("private-menu", "private_menu")):
        cli.main([name, "off" if field != "private_menu" else "on"])
        assert getattr(settings.load(), field) == (field == "private_menu")
    assert cli.main(["status"]) == 0
    assert "menu text hidden on" in capsys.readouterr().out


def test_snippet_shortcuts():
    all_shortcuts = shortcut.shortcuts()
    assert len(all_shortcuts) == 3 + shortcut.SNIPPET_KEYS
    last = all_shortcuts[-1]
    assert last.binding == "<Super><Alt>9" and last.command.endswith("paste-snippet 7")
    assert len({s.path for s in all_shortcuts}) == len(all_shortcuts)


def test_search_all_merges_names_and_full_text():
    from clip_keeper import fuzzy
    store = Store(memory=True)
    deep = store.insert_text("x" * 300 + " zebra", created_at=1)
    named = store.insert_text("some text", created_at=2)
    store.set_name(named, "zebra notes")
    clips = store.recent()
    found = fuzzy.search_all(clips, "zebra", store.full_text_matches("zebra"))
    assert [c.id for c in found] == [named, deep]
    assert fuzzy.search_all(clips, "zebra", [deep, 999], limit=1)[0].id == named


def test_tray_switch_confirmation(monkeypatch):
    replies = iter([{"ok": True, "encrypted": True, "locked": ""}, {"ok": True, "encrypted": False, "locked": ""}])
    monkeypatch.setattr(cli.ipc, "request", lambda cmd, **kw: next(replies))
    assert cli._tray_switched(True) and not cli._tray_switched(True)

    def down(cmd, **kw):
        raise cli.ipc.TrayUnavailable("not running")
    monkeypatch.setattr(cli.ipc, "request", down)
    assert cli._tray_switched(False)


def test_png_header_checks():
    import struct as st
    header = formats.PNG_SIGNATURE + b"\0\0\0\rIHDR" + st.pack(">II", 30_000, 30_000) + b"rest"
    assert formats.png_size(header) == (30_000, 30_000) and formats.image_too_big(header)
    small = formats.PNG_SIGNATURE + b"\0\0\0\rIHDR" + st.pack(">II", 1920, 1080)
    assert not formats.image_too_big(small)
    assert formats.png_size(b"GIF89a....") is None and formats.image_too_big(b"not png")
    assert formats.parse_gnome_files(b"copy\n" + b"file:///x" * 200_000) is None
