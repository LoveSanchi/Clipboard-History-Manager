import json

import pytest

from clip_keeper import filters, fuzzy, settings, shortcut
from clip_keeper.capture import Capturer
from clip_keeper.settings import Settings
from clip_keeper.store import Store


@pytest.mark.parametrize("text, kind", [
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc", "private key"),
    ("ghp_" + "a" * 36, "token"),
    ("AKIAABCDEFGHIJKLMNOP", "token"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "JWT"),
    ("mysql://reader:s3cret@db:3306/orders", "connection string with a password"),
    ('{"api_key": "abcd1234efgh"}', "password assignment"),
    ("export PASSWORD=hunter22", "password assignment"),
])
def test_secrets_are_recognised(text, kind):
    assert filters.secret_kind(text) == kind


@pytest.mark.parametrize("text", ["hello world", "SELECT * FROM orders WHERE id = 3", "https://example.com/path",
                                  "the password is in the vault", "git commit -m 'fix'", "a" * 40])
def test_ordinary_text_is_kept(text):
    assert filters.secret_kind(text) is None


def test_skip_reasons():
    assert filters.skip_reason("x", True, True) == "marked secret by a password manager"
    assert filters.skip_reason("  ", False, True) == "empty"
    assert filters.skip_reason(None, False, True) == "empty"
    assert filters.skip_reason("a" * (filters.MAX_CLIP_BYTES + 1), False, True) == "larger than 1 MiB"
    assert filters.skip_reason("PASSWORD=hunter22", False, False) is None   # filter off
    assert filters.skip_reason("hello", False, True) is None


def test_fuzzy_ranks_substrings_then_scattered_and_keeps_pins_first():
    store = Store(memory=True)
    for i, text in enumerate(["git status", "grep -r todo src", "docker ps", "git stash pop"]):
        store.insert_text(text, i)
    clips = store.recent()
    assert [c.text for c in fuzzy.search(clips, "git st")] == ["git stash pop", "git status"]
    assert [c.text for c in fuzzy.search(clips, "gsp")][0] == "git stash pop"
    assert fuzzy.search(clips, "zzz") == []
    assert len(fuzzy.search(clips, "")) == 4
    store.set_pinned(store.recent()[-1].id, True)  # pin "git status"
    assert [c.text for c in fuzzy.search(store.recent(), "git")][0] == "git status"
    assert fuzzy.score("", "x") == 0 and fuzzy.score("ab", "a-b") > fuzzy.score("ab", "axxxxb")


def test_settings_roundtrip_and_validation(tmp_path):
    path = tmp_path / "s.json"
    assert settings.load(path) == Settings()
    settings.save(Settings(paused=True, history_limit=50), path)
    assert settings.load(path).paused and settings.load(path).history_limit == 50
    path.write_text(json.dumps({"paused": "yes", "history_limit": 999999, "tray_items": -3}))
    loaded = settings.load(path)
    assert loaded.paused is False and loaded.history_limit == 10_000 and loaded.tray_items == 0
    path.write_text("[1]")
    assert settings.load(path) == Settings()
    assert settings.update(paused=True).paused and settings.load().paused


def test_capture_stores_skips_pauses_and_collapses_bursts():
    store = Store(memory=True)
    now = [100.0]
    cap = Capturer(store, clock=lambda: now[0])
    on = Settings()
    assert cap.handle("hello", False, on) == "stored"
    assert cap.handle("hello", False, on) == "repeat"          # the same burst
    now[0] += 5
    assert cap.handle("hello", False, on) == "stored"          # copied again later: moves to the top
    assert store.count() == 1
    assert cap.handle("PASSWORD=hunter22", False, on) == "looks like a password assignment"
    assert cap.handle("PASSWORD=hunter22", False, on) == "repeat"          # counted once per burst
    assert cap.handle("anything", True, on) == "marked secret by a password manager"
    assert cap.stats.skipped["looks like a password assignment"] == 1
    assert cap.handle("x", False, Settings(paused=True)) == "paused"
    assert store.count() == 1 and cap.stats.stored == 2
    assert cap.stats.skipped["marked secret by a password manager"] == 1
    for i in range(15):
        cap.handle(f"clip {i}", False, Settings(history_limit=10))
    assert store.count() == 10


def _fake_gsettings(state, calls):
    def run(args):
        calls.append(args)
        if args[0] == "get":
            return state[(args[1], args[2])]
        if args[0] == "set" and (args[1], args[2]) in state:
            state[(args[1], args[2])] = args[3]
        return ""
    return run


def test_shortcut_bind_and_unbind_keep_other_bindings():
    tray = ("org.gnome.shell.keybindings", "toggle-message-tray")
    state = {tray: "['<Super>v', '<Super>m']", (shortcut.MEDIA_KEYS, "custom-keybindings"): "['/org/gnome/.../custom0/']"}
    calls = []
    run = _fake_gsettings(state, calls)
    saved = shortcut.bind("/usr/bin/clip-keeper-popup", run)
    assert saved == "['<Super>v', '<Super>m']"
    assert shortcut.parse_list(state[tray]) == ["<Super>m"]
    ours = [s.path for s in shortcut.shortcuts()]
    assert shortcut.parse_list(state[(shortcut.MEDIA_KEYS, "custom-keybindings")]) == ["/org/gnome/.../custom0/", *ours]
    assert ["set", shortcut.CUSTOM_SCHEMA, "binding", "<Super>v"] in calls
    paste_1 = shortcut.shortcuts()[1]
    assert ["set", paste_1.schema, "binding", "<Super><Alt>1"] in calls
    assert ["set", paste_1.schema, "command", "/usr/bin/clip-keeper paste 1"] in calls
    shortcut.bind("/usr/bin/clip-keeper-popup", run)  # idempotent
    assert shortcut.parse_list(state[(shortcut.MEDIA_KEYS, "custom-keybindings")]).count(shortcut.CUSTOM_PATH) == 1
    state[tray] = "['<Super>m', '<Super>n']"          # the user changed it meanwhile
    shortcut.unbind(saved, run)
    assert shortcut.parse_list(state[(shortcut.MEDIA_KEYS, "custom-keybindings")]) == ["/org/gnome/.../custom0/"]
    assert shortcut.parse_list(state[tray]) == ["<Super>v", "<Super>m", "<Super>n"]  # V back, their N kept


def test_unbind_without_a_prior_bind_leaves_the_tray_alone():
    tray = ("org.gnome.shell.keybindings", "toggle-message-tray")
    state = {tray: "['<Super>m']", (shortcut.MEDIA_KEYS, "custom-keybindings"): "@as []"}
    calls = []
    shortcut.unbind("", _fake_gsettings(state, calls))
    assert state[tray] == "['<Super>m']" and not any(c[0] == "reset" and c[1] == tray[0] for c in calls)


def test_unreadable_settings_never_overwrite_other_shortcuts():
    tray = ("org.gnome.shell.keybindings", "toggle-message-tray")
    state = {tray: "['<Super>m']", (shortcut.MEDIA_KEYS, "custom-keybindings"): "garbage["}
    calls = []
    with pytest.raises(ValueError, match="leaving shortcuts untouched"):
        shortcut.bind("/x", _fake_gsettings(state, calls))
    assert not any(c[0] == "set" and c[2] == "custom-keybindings" for c in calls)


def test_gvariant_lists():
    assert shortcut.parse_list("@as []") == [] and shortcut.parse_list("") == []
    with pytest.raises(ValueError):
        shortcut.parse_list("'x'")
    assert shortcut.format_list([]) == "@as []" and shortcut.format_list(["a"]) == "['a']"


@pytest.mark.parametrize("blob", ["a." * 500_000, "a-" * 500_000, "a_" * 500_000, "x" * 1_000_000,
                                  "token" * 200_000, "://" * 330_000, "a:" * 500_000, "password " * 110_000,
                                  "eyJ" * 350_000, "x eyJ" * 200_000, "Bearer " * 150_000])
def test_filters_are_fast_on_1mib_worst_cases(blob):
    import time
    started = time.monotonic()
    filters.secret_kind(blob)
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("text, kind", [
    ("redis://:pass1234@host:6379", "connection string with a password"),
    ("sk_live_" + "a" * 24, "token"),
    ("Authorization: Bearer abcdefghijklmnopqrstu", "bearer/basic credential"),
    ("curl -u admin:hunter22 https://x", "curl -u credentials"),
    ("GITHUB_TOKEN=ghx1234567", "password assignment"),
    ("AWS_SECRET_ACCESS_KEY=abcd/efgh1234", "password assignment"),
    ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "private key"),
    ("the password is hunter2!", "password"),
])
def test_more_credential_shapes(text, kind):
    assert filters.secret_kind(text) == kind


@pytest.mark.parametrize("text", ["http://localhost:3000/@vite/client", "https://medium.com:443/@foo/bar",
                                  "https://host:8443/login?user=a@b.com", "def f(password: str)", "password: string,",
                                  "OLDPWD=/home/a/b", "secret: true", "Top secret: meeting at five", "tokens: 5"])
def test_no_false_positives(text):
    assert filters.secret_kind(text) is None


def test_capture_images():
    store = Store(memory=True)
    now = [0.0]
    cap = Capturer(store, clock=lambda: now[0])
    on = Settings()
    assert cap.handle_image(b"png-1", 800, 600, on) == "stored"
    assert cap.handle_image(b"png-1", 800, 600, on) == "repeat"
    assert cap.handle_image(b"png-2", 800, 600, Settings(paused=True)) == "paused"
    assert cap.handle_image(b"png-2", 800, 600, Settings(record_images=False)) == "images are off"
    assert cap.handle_image(b"", 1, 1, on) == "empty"
    import clip_keeper.capture as capture_mod
    capture_mod.MAX_IMAGE_BYTES, old = 3, capture_mod.MAX_IMAGE_BYTES
    try:
        assert cap.handle_image(b"toolarge", 1, 1, on) == "image larger than 25 MiB"
    finally:
        capture_mod.MAX_IMAGE_BYTES = old
    for i in range(5):
        now[0] += 10
        cap.handle_image(f"shot{i}".encode(), 10, 10, Settings(image_limit=2))
    assert sum(1 for c in store.recent() if c.is_image) == 2


def test_latest_ignores_pins_and_cli_paste_asks_the_tray(monkeypatch):
    store = Store()
    first = store.insert_text("older", 1)
    store.insert_text("newest", 2)
    store.set_pinned(first, True)
    assert store.latest(1).text == "newest" and store.latest(2).text == "older"
    assert store.latest(3) is None and store.latest(0) is None
    from clip_keeper import cli, ipc
    sent = []
    monkeypatch.setattr(ipc, "request", lambda cmd, **kw: sent.append((cmd, kw)) or {"ok": True})
    assert cli.main(["paste", "2"]) == 0
    assert sent == [("copy", {"id": first, "paste": "ctrl_v", "delay_ms": cli.SHORTCUT_PASTE_DELAY_MS})]
    assert cli.main(["paste", "1", "--terminal"]) == 0 and sent[-1][1]["paste"] == "ctrl_shift_v"
    assert cli.main(["paste", "9"]) == 1
    monkeypatch.setattr(ipc, "request", lambda cmd, **kw: {"ok": False, "error": "auto-paste is off"})
    assert cli.main(["paste", "1"]) == 1
