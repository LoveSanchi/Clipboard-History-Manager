"""clip-keeper's top-bar app: records the clipboard, shows the latest clips, and serves
the popup and CLI over a small socket. Recording only happens while this runs."""

from __future__ import annotations

import os

# Must be X11 (XWayland): see clipboard.py. Set before GTK loads.
os.environ["GDK_BACKEND"] = "x11"

import fcntl  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

from clip_keeper import paths, settings, transforms, vkbd  # noqa: E402
from clip_keeper.capture import Capturer  # noqa: E402
from clip_keeper.clipboard import ClipboardWatch  # noqa: E402
from clip_keeper.filters import MAX_CLIP_BYTES  # noqa: E402
from clip_keeper.history import open_store  # noqa: E402
from clip_keeper.store import Clip, StoreLocked, one_line  # noqa: E402

ICON_DIR = Path(__file__).resolve().parent / "icons"
REFRESH_SECONDS = 5  # the popup can change history (pin, delete, clear) behind our back
EXPIRE_EVERY_SECONDS = 3600
MAX_REQUEST_BYTES = 8 * 1024 * 1024  # an edited clip travels in the request (JSON-escaped)
PASTE_DELAY_MS = 180  # after the popup is gone, before Ctrl+V: focus must be back on the target
SHORT_PAUSE_SECONDS = 300
POPUP = "/usr/bin/clip-keeper-popup"
MAX_CONNECTIONS = 16
REQUEST_DEADLINE_SECONDS = 5.0


class Tray:
    def __init__(self) -> None:
        self._settings = settings.load()
        self._store, self._locked = open_store(self._settings)
        self._capturer = Capturer(self._store)
        self._watch = ClipboardWatch(self._on_clipboard, self._on_image, self._on_files)
        self._keyboard, self._keyboard_problem = _open_keyboard()
        self._ind = AppIndicator.Indicator.new("clip-keeper", "clip-keeper-on",
                                               AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self._ind.set_icon_theme_path(str(ICON_DIR))
        self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._ind.set_title("Clipboard history")
        self._signature: Any = None
        self._last_expiry = 0.0
        self._tick()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)
        threading.Thread(target=self._serve, daemon=True).start()

    # ── capture ───────────────────────────────────────────────────────────────
    def _capture(self, record: Callable[[], str]) -> None:
        self._settings = settings.load()
        if self._needs_reopen():
            self._reopen()  # encryption was switched: never record with the old cipher
        if self._locked:
            return  # never write plain text while encryption is on but the key is out of reach
        try:
            record()
        except StoreLocked:
            return
        self._render()

    def _on_clipboard(self, text: str | None, password_hint: bool, html: str = "") -> None:
        self._capture(lambda: self._capturer.handle(text, password_hint, self._settings, html))

    def _on_image(self, png: bytes, width: int, height: int) -> None:
        self._capture(lambda: self._capturer.handle_image(png, width, height, self._settings))

    def _on_files(self, uris: list[str]) -> None:
        self._capture(lambda: self._capturer.handle_files(uris, self._settings))

    # ── periodic work ─────────────────────────────────────────────────────────
    def _tick(self) -> bool:
        self._settings = settings.load()
        if self._needs_reopen():
            self._reopen()  # keyring unlocked after login, or encryption switched from the CLI
        if not self._locked and time.monotonic() - self._last_expiry > EXPIRE_EVERY_SECONDS:
            self._last_expiry = time.monotonic()
            self._store.expire(self._settings.expire_days)
        self._render()
        return True

    def _needs_reopen(self) -> bool:
        return bool(self._locked) or self._store.encrypted != self._settings.encrypt

    def _reopen(self) -> None:
        store, locked = open_store(self._settings)
        if locked and self._locked:
            store.close()  # still locked: keep what we have instead of churning every tick
            return
        self._store.close()
        self._store, self._locked = store, locked
        self._capturer.use(store)
        self._signature = None

    # ── menu ──────────────────────────────────────────────────────────────────
    def _render(self) -> None:
        clips = self._store.recent(self._settings.tray_items)
        quiet = self._settings.paused or bool(self._capturer.paused_seconds_left) or bool(self._locked)
        self._ind.set_icon_full("clip-keeper-paused" if quiet else "clip-keeper-on",
                                "Clipboard history (paused)" if quiet else "Clipboard history")
        signature = (tuple((c.id, c.pinned, c.preview, c.name) for c in clips), self._settings,
                     self._capturer.stats.last_skip, self._capturer.skip_next,
                     (self._capturer.paused_seconds_left + 59) // 60, self._locked, self._store.count())
        if signature != self._signature:
            self._signature = signature
            self._ind.set_menu(self._build_menu(clips))

    def _state(self) -> str:
        if self._locked:
            return "locked — unlock GNOME Keyring to record"
        if self._settings.paused:
            return "paused — nothing is being recorded"
        left = self._capturer.paused_seconds_left
        if left:
            return f"paused for {(left + 59) // 60} more min"
        return "skipping the next copy" if self._capturer.skip_next else "recording"

    def _build_menu(self, clips: list[Clip]) -> Gtk.Menu:
        menu = Gtk.Menu()
        lock = " · 🔒 encrypted" if self._store.encrypted else ""
        _label(menu, f"Clipboard history · {self._store.count()} items · {self._state()}{lock}")
        if self._capturer.stats.last_skip:
            _label(menu, f"last skipped: {self._capturer.stats.last_skip}")
        if self._keyboard_problem and self._settings.auto_paste:
            _label(menu, "auto-paste unavailable — Super+V only copies (see: clip-keeper status)")
        menu.append(Gtk.SeparatorMenuItem())
        for number, clip in enumerate(clips, 1):
            _item(menu, self._menu_title(number, clip), lambda *_, c=clip.id: self.copy_clip(c))
        if not clips:
            _label(menu, "nothing copied yet")
        menu.append(Gtk.SeparatorMenuItem())
        _item(menu, "Search history…    Super+V", lambda *_: _spawn_popup())
        _label(menu, "Paste last / 2nd last:  Super+Alt+1 / 2 · snippets:  Super+Alt+3…9")
        self._recording_items(menu)
        _check(menu, "Hide clip text in this menu", self._settings.private_menu,
               lambda on: self._change(private_menu=on))
        _item(menu, "Clear history (keeps pinned)…", self._confirm_clear)
        menu.append(Gtk.SeparatorMenuItem())
        _item(menu, "Quit (stops recording until next login)", lambda *_: Gtk.main_quit())
        menu.show_all()
        return menu

    def _recording_items(self, menu: Gtk.Menu) -> None:
        if self._settings.paused:
            _item(menu, "Resume recording", lambda *_: self.set_paused(False))
            return
        if self._capturer.paused_seconds_left:
            _item(menu, "Resume recording now", lambda *_: self.pause_for(0))
        else:
            _item(menu, "Pause for 5 minutes", lambda *_: self.pause_for(SHORT_PAUSE_SECONDS))
            if not self._capturer.skip_next:
                _item(menu, "Skip the next copy", lambda *_: self.skip_next())
        _item(menu, "Pause recording", lambda *_: self.set_paused(True))

    def _menu_title(self, number: int, clip: Clip) -> str:
        badge = ("📌 " if clip.pinned else "") + ("🖼 " if clip.is_image else "📁 " if clip.is_files else "")
        if self._settings.private_menu:
            kind = "image" if clip.is_image else "files" if clip.is_files else "text"
            return f"{badge}{clip.name or f'Clip {number}'} ({kind})"
        name = f"{clip.name}: " if clip.name else ""
        return badge + _shorten(name + one_line(clip.text), 60)

    # ── actions ───────────────────────────────────────────────────────────────
    def copy_clip(self, clip_id: int, transform: str | None = None) -> bool:
        clip = self._store.get(clip_id)
        if clip is None:
            return False
        if clip.locked:
            raise ValueError("this clip is encrypted and the key is unavailable")
        if clip.is_image:
            try:
                png = self._store.image_bytes(clip)
            except (OSError, ValueError):
                return False
            self._capturer.expect_own("image")  # GTK re-encodes it: don't store a twin
            self._watch.set_image(png)
        elif clip.is_files and transform is None:
            self._watch.set_files(clip.text.splitlines())
        elif transform is None:
            self._watch.set_text(clip.text, clip.html)
        else:
            return self.copy_text(transforms.apply(transform, clip.text))
        return True

    def copy_text(self, text: str) -> bool:
        """Text that is not (yet) a clip: transformed, edited or several joined. Not recorded."""
        if len(text.encode("utf-8")) > MAX_CLIP_BYTES:
            raise ValueError("the text is larger than 1 MiB")
        self._capturer.expect_own("text", text.encode("utf-8"))
        self._watch.set_text(text)
        return True

    def paste(self, combo: str, delay_ms: int = 0) -> dict:
        """Press the paste keys a moment after the popup has closed and focus is back."""
        if not self._settings.auto_paste:
            return {"ok": False, "error": "auto-paste is off"}
        if self._keyboard is None:
            return {"ok": False, "error": self._keyboard_problem}
        if combo not in vkbd.COMBOS:
            return {"ok": False, "error": f"unknown paste keys {combo!r}"}

        def press() -> bool:
            try:
                self._keyboard.press(combo)
            except OSError as exc:
                print(f"clip-keeper: auto-paste failed: {exc}", file=sys.stderr)
            return False

        GLib.timeout_add(max(PASTE_DELAY_MS, min(delay_ms, 2000)), press)
        return {"ok": True}

    def set_paused(self, paused: bool) -> None:
        self._change(paused=paused)
        self._capturer.resume()

    def pause_for(self, seconds: int) -> None:
        self._capturer.pause_for(max(0, min(seconds, 86_400)))
        self._render()

    def skip_next(self) -> None:
        self._capturer.skip_next = True
        self._render()

    def _change(self, **changes: Any) -> None:
        self._settings = settings.update(**changes)
        self._render()

    def _confirm_clear(self, *_: Any) -> None:
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                                   text="Clear the clipboard history?")
        dialog.format_secondary_text("Every clip except pinned ones is deleted and overwritten on disk.")
        dialog.set_keep_above(True)
        if dialog.run() == Gtk.ResponseType.OK:
            self._store.clear_unpinned()
        dialog.destroy()
        self._render()

    # ── socket for the popup and the CLI ──────────────────────────────────────
    def _serve(self) -> None:
        path = paths.socket_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            if path.parent == paths.runtime_dir():  # never chmod a directory we don't own (CLIP_KEEPER_SOCKET)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(path.parent, 0o700)
            path.unlink(missing_ok=True)
            server.bind(str(path))
        except OSError as exc:
            print(f"clip-keeper: cannot listen on {path}: {exc} — the popup will copy by itself", file=sys.stderr)
            return
        finally:
            os.umask(old)
        server.listen(8)
        slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                time.sleep(1)  # e.g. out of file descriptors: keep serving once it clears
                continue
            if not slots.acquire(blocking=False):
                conn.close()  # too many slow clients at once
                continue
            threading.Thread(target=self._answer_then_release, args=(conn, slots), daemon=True).start()

    def _answer_then_release(self, conn: socket.socket, slots: threading.BoundedSemaphore) -> None:
        try:
            self._answer(conn)
        finally:
            slots.release()

    def _answer(self, conn: socket.socket) -> None:
        """One request per connection; any failure ends only this connection."""
        with conn:
            try:
                message = json.loads(_read_line(conn))
                reply = self._on_main_thread(lambda: self.handle(message))
            except Exception as exc:  # noqa: BLE001 - bad input must never stop the server
                reply = {"ok": False, "error": f"bad request: {type(exc).__name__}"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass

    @staticmethod
    def _on_main_thread(fn: Callable[[], dict]) -> dict:
        """GTK and the clipboard belong to the main thread: run there, wait for the answer."""
        done, box = threading.Event(), {}

        def run() -> bool:
            try:
                box["reply"] = fn()
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                box["reply"] = {"ok": False, "error": str(exc)}
            done.set()
            return False

        GLib.idle_add(run)
        done.wait(5)
        return box.get("reply", {"ok": False, "error": "timed out"})

    def handle(self, message: Any) -> dict:
        if not isinstance(message, dict):
            return {"ok": False, "error": "bad request"}
        cmd = message.get("cmd")
        if cmd in ("copy", "copy_text"):
            return self._handle_copy(message)
        if cmd in ("pause", "resume"):
            self.set_paused(cmd == "pause")
            return {"ok": True}
        if cmd == "pause_for" and isinstance(message.get("seconds"), int):
            self.pause_for(message["seconds"])
            return {"ok": True}
        if cmd == "skip_next":
            self.skip_next()
            return {"ok": True}
        if cmd == "status":
            return self._status()
        if cmd == "reopen":  # the CLI switched encryption: switch now, not on the next tick
            self._settings = settings.load()
            self._reopen()
            self._render()
            return {"ok": True, "encrypted": self._store.encrypted, "locked": self._locked}
        if cmd == "refresh":
            self._signature = None
            self._render()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _handle_copy(self, message: dict) -> dict:
        transform = message.get("transform")
        if transform is not None and transform not in transforms.BY_KEY:
            return {"ok": False, "error": f"unknown transform {transform!r}"}
        try:
            if message["cmd"] == "copy_text" and isinstance(message.get("text"), str):
                copied = self.copy_text(message["text"])
            elif message["cmd"] == "copy" and isinstance(message.get("id"), int):
                copied = self.copy_clip(message["id"], transform)
            else:
                return {"ok": False, "error": "bad copy request"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        paste = message.get("paste")
        delay = message.get("delay_ms") if isinstance(message.get("delay_ms"), int) else 0
        if copied and isinstance(paste, str):
            return {"ok": True, "paste": self.paste(paste, delay)}
        return {"ok": copied} if copied else {"ok": False, "error": "the clip is gone"}

    def _status(self) -> dict:
        stats = self._capturer.stats
        return {"ok": True, "paused": self._settings.paused, "items": self._store.count(),
                "paused_seconds_left": self._capturer.paused_seconds_left, "skip_next": self._capturer.skip_next,
                "encrypted": self._store.encrypted, "locked": self._locked,
                "stored": stats.stored, "skipped": dict(stats.skipped),
                "auto_paste": "off" if not self._settings.auto_paste else
                ("ready" if self._keyboard else self._keyboard_problem)}


def _read_line(conn: socket.socket) -> bytes:
    """One request line, within a size limit and an overall deadline (a client that drips
    bytes cannot hold a thread for long)."""
    deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
    data = b""
    while b"\n" not in data:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("request too slow")
        conn.settimeout(left)
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
    return data.split(b"\n", 1)[0]


def _open_keyboard():
    try:
        return vkbd.VirtualKeyboard(), ""
    except vkbd.Unavailable as exc:
        return None, str(exc)


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _spawn_popup() -> None:
    subprocess.Popen([POPUP if os.path.exists(POPUP) else "clip-keeper-popup"], start_new_session=True)


def _label(menu: Gtk.Menu, text: str) -> None:
    item = Gtk.MenuItem(label=text)
    item.set_sensitive(False)
    menu.append(item)


def _item(menu: Gtk.Menu, text: str, handler: Callable) -> None:
    item = Gtk.MenuItem(label=text)
    item.connect("activate", handler)
    menu.append(item)


def _check(menu: Gtk.Menu, text: str, active: bool, handler: Callable[[bool], None]) -> None:
    item = Gtk.CheckMenuItem(label=text)
    item.set_active(active)
    item.connect("toggled", lambda w: handler(w.get_active()))
    menu.append(item)


def _single_instance():
    lock = paths.runtime_dir() / "tray.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(lock, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    lock = _single_instance()
    if lock is None:
        print("clip-keeper is already running", file=sys.stderr)
        return 0
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
