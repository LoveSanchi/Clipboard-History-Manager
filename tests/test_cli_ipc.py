import json
import socket
import threading

import pytest

from clip_keeper import cli, ipc, paths, settings, shortcut
from clip_keeper.store import Store


@pytest.fixture
def fake_tray():
    """A stand-in for the top-bar app's socket that records requests."""
    path = paths.socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)
    seen = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                msg = json.loads(conn.makefile("rb").readline())
                seen.append(msg)
                reply = {"ok": True, "stored": 3, "skipped": {"empty": 2}} if msg["cmd"] == "status" else {"ok": True}
                conn.sendall((json.dumps(reply) + "\n").encode())

    threading.Thread(target=serve, daemon=True).start()
    yield seen
    server.close()


def test_status_with_and_without_tray(capsys, fake_tray):
    Store().insert_text("one")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "history: 1 clips" in out and "stored this session 3" in out and "2× empty" in out


def test_status_without_tray(capsys):
    assert cli.main(["status"]) == 0
    assert "NOT running" in capsys.readouterr().out


def test_list_clear_pause_secret_filter(capsys, fake_tray):
    store = Store()
    store.insert_text("git status", 1)
    store.insert_text("docker ps", 2)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert out.index("docker ps") < out.index("git status")
    cli.main(["list", "git"])
    assert "docker" not in capsys.readouterr().out
    assert cli.main(["clear"]) == 0 and Store().count() == 0
    cli.main(["pause"])
    assert settings.load().paused
    cli.main(["resume"])
    assert not settings.load().paused
    cli.main(["secret-filter", "off"])
    assert settings.load().skip_secrets is False
    assert {"cmd": "refresh"} in fake_tray


def test_bind_and_unbind_remember_gnome_default(capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(shortcut, "bind", lambda command: calls.append(("bind", command)) or "['<Super>v', '<Super>m']")
    monkeypatch.setattr(shortcut, "unbind", lambda saved: calls.append(("unbind", saved)))
    cli.main(["bind-shortcut"])
    assert settings.load().saved_tray_shortcut == "['<Super>v', '<Super>m']"
    cli.main(["unbind-shortcut"])
    assert calls == [("bind", cli.POPUP_COMMAND), ("unbind", "['<Super>v', '<Super>m']")]
    assert settings.load().saved_tray_shortcut == ""


def test_ipc_errors():
    with pytest.raises(ipc.TrayUnavailable, match="not running"):
        ipc.request("status")
