"""The Super+V dropdown: type to search, Enter to paste, Esc to close.

Up/Down move · Enter paste into the window you came from · Alt+1…9 paste that row ·
Shift+Enter paste in a terminal (Ctrl+Shift+V) · Ctrl+Enter copy only · Ctrl+Space mark
several, Enter pastes them joined by newlines · Ctrl+T paste transformed · Ctrl+E edit before
paste · Ctrl+N name (snippet) · Ctrl+P pin · Ctrl+D delete · Ctrl+Shift+Delete clear history."""

from __future__ import annotations

import os

# X11 (XWayland) on purpose: a native Wayland app may not place its own window, and this one
# must sit right under the top bar like the tray menu. XWayland is core GNOME and upgrade-safe.
os.environ.setdefault("GDK_BACKEND", "x11")

import fcntl
import sqlite3
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from clip_keeper import dialogs, fuzzy, ipc, paths, settings, transforms  # noqa: E402
from clip_keeper.formats import paths_text  # noqa: E402
from clip_keeper.history import open_store  # noqa: E402
from clip_keeper.store import Clip, Store, StoreLocked, one_line  # noqa: E402

SHOWN = 200
WIDTH = 520
MAX_LIST_HEIGHT = 440
EDGE = 8  # gap from the screen's right edge, like the tray menu
TOP_BAR_HEIGHT = 32  # GNOME's top bar in logical pixels; Mutter also keeps windows below it
NUMBERED = 9
HINT = ("Enter paste · Alt+1…9 paste row · Shift+Enter terminal · Ctrl+Enter copy only\n"
        "Ctrl+Space mark several · Ctrl+T transform · Ctrl+E edit · Ctrl+N name · Ctrl+P pin · Ctrl+D delete")
CSS = b"""
.clip-dropdown { background-color: #2d2d2d; border: 1px solid #454545; }
.clip-dropdown list, .clip-dropdown list row { background-color: transparent; color: #f2f2f2; }
.clip-dropdown list row:selected { background-color: #4a4a4a; }
.clip-dropdown .title { color: #b5b5b5; font-weight: bold; }
.clip-dropdown .dim-label { color: #9a9a9a; }
.clip-dropdown .number { color: #8ab4f8; font-family: monospace; }
.clip-dropdown .marked { color: #81c995; }
.clip-dropdown entry { background-color: #3a3a3a; color: #f2f2f2; border-color: #555555; }
"""


def ago(created_ms: int, now: float | None = None) -> str:
    secs = int((now if now is not None else time.time()) - created_ms / 1000)
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def joined_text(clips: list[Clip]) -> str:
    """Several clips as one paste: text as is, files as paths; images can't be joined."""
    return "\n".join(paths_text(c.text.splitlines()) if c.is_files else c.text for c in clips if not c.is_image)


class Popup(Gtk.Window):
    def __init__(self, store: Store | None = None, copy=None):
        super().__init__(title="Clipboard history")
        self._store = store or open_store(settings.load())[0]
        self._copy = copy or _copy_via_tray
        self._all: list[Clip] = []
        self._shown: list[Clip] = []
        self._marked: list[int] = []  # ids, in the order they were marked
        self._had_focus = False
        self._modal = False
        # A dropdown, not a window: no title bar, no taskbar entry, under the top bar.
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_resizable(False)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)  # dialogs get keyboard focus; menus may not
        self.set_size_request(WIDTH, -1)
        self.get_style_context().add_class("clip-dropdown")
        _install_css()
        self._build()
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", lambda *_: Gtk.main_quit())
        self.connect("focus-in-event", lambda *_: setattr(self, "_had_focus", True))
        self.connect("focus-out-event", self._on_focus_out)
        self._reload()
        self._place()
        self.show_all()
        self._entry.grab_focus()

    def _build(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        self.add(box)
        title = Gtk.Label(label="Clipboard history" + ("  🔒" if self._store.encrypted else ""), xalign=0)
        title.get_style_context().add_class("title")
        box.pack_start(title, False, False, 0)
        self._entry = Gtk.SearchEntry(placeholder_text="Search names, previews and full text…")
        self._entry.connect("search-changed", lambda *_: self._refill())
        self._entry.connect("activate", lambda *_: self._enter("ctrl_v"))
        box.pack_start(self._entry, False, False, 0)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self._list.connect("row-activated", lambda *_: self._enter("ctrl_v"))
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(MAX_LIST_HEIGHT)
        scroller.add(self._list)
        box.pack_start(scroller, True, True, 0)
        self._status = Gtk.Label(xalign=0)
        self._status.get_style_context().add_class("dim-label")
        box.pack_start(self._status, False, False, 0)
        hint = Gtk.Label(label=HINT, xalign=0)
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)

    def _place(self) -> None:
        """Top-right, directly under the top bar (the work area starts below it)."""
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        area, screen = monitor.get_workarea(), monitor.get_geometry()
        # Under XWayland the work area may not exclude GNOME's top bar: never start inside it.
        top = area.y if area.y > screen.y else screen.y + TOP_BAR_HEIGHT
        self.move(area.x + area.width - WIDTH - EDGE, top)

    def _on_focus_out(self, *_args) -> bool:
        if self._had_focus and not self._modal:  # clicked elsewhere: close, like a menu
            self.destroy()
        return False

    # ── data ──────────────────────────────────────────────────────────────────
    def _reload(self) -> None:
        self._all = self._store.recent()
        live = {c.id for c in self._all}
        self._marked = [i for i in self._marked if i in live]
        self._refill()

    def _refill(self) -> None:
        for child in self._list.get_children():
            self._list.remove(child)
        query = self._entry.get_text()
        deep = self._store.full_text_matches(query, SHOWN) if query.strip() else []
        self._shown = fuzzy.search_all(self._all, query, deep, SHOWN)
        now = time.time()
        for index, clip in enumerate(self._shown):
            self._list.add(self._row(index, clip, now))
        self._list.show_all()
        first = self._list.get_row_at_index(0)
        if first:
            self._list.select_row(first)
        self._update_status()

    def _row(self, index: int, clip: Clip, now: float) -> Gtk.Box:
        row = Gtk.Box(spacing=8, margin=4)
        number = Gtk.Label(label=str(index + 1) if index < NUMBERED else " ", width_chars=1)
        number.get_style_context().add_class("number")
        row.pack_start(number, False, False, 0)
        if clip.id in self._marked:
            mark = Gtk.Label(label=f"✓{self._marked.index(clip.id) + 1}")
            mark.get_style_context().add_class("marked")
            row.pack_start(mark, False, False, 0)
        if clip.is_image:
            thumb = self._thumbnail(clip)
            if thumb is not None:
                row.pack_start(thumb, False, False, 0)
        badge = ("📌 " if clip.pinned else "") + ("📁 " if clip.is_files else "")
        name = f"{clip.name} — " if clip.name else ""
        text = Gtk.Label(label=badge + name + one_line(clip.text)[:160], xalign=0)
        text.set_ellipsize(3)  # Pango.EllipsizeMode.END
        row.pack_start(text, True, True, 0)
        when = Gtk.Label(label=ago(clip.created_at, now))
        when.get_style_context().add_class("dim-label")
        row.pack_end(when, False, False, 0)
        return row

    def _update_status(self, note: str = "") -> None:
        marked = f" · {len(self._marked)} marked — Enter pastes them joined" if self._marked else ""
        self._status.set_text(note or f"{len(self._shown)} of {len(self._all)}{marked}")

    def _thumbnail(self, clip: Clip):
        from gi.repository import GdkPixbuf
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            loader.write(self._store.image_bytes(clip))
            loader.close()
            pixbuf = loader.get_pixbuf()
        except (GLib.Error, OSError, ValueError):
            return None
        scale = min(96 / pixbuf.get_width(), 54 / pixbuf.get_height(), 1.0)
        small = pixbuf.scale_simple(max(1, int(pixbuf.get_width() * scale)), max(1, int(pixbuf.get_height() * scale)),
                                    GdkPixbuf.InterpType.BILINEAR)
        return Gtk.Image.new_from_pixbuf(small)

    def _selected(self) -> Clip | None:
        row = self._list.get_selected_row()
        return self._shown[row.get_index()] if row is not None and row.get_index() < len(self._shown) else None

    # ── keys ──────────────────────────────────────────────────────────────────
    def _on_key(self, _w, event) -> bool:
        key, state = event.keyval, event.state
        ctrl, shift = bool(state & Gdk.ModifierType.CONTROL_MASK), bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        if key == Gdk.KEY_Escape:
            self.destroy()
        elif alt and Gdk.KEY_1 <= key <= Gdk.KEY_9:
            self._paste_row(key - Gdk.KEY_1)
        elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._enter(None if ctrl else "ctrl_shift_v" if shift else "ctrl_v")
        elif key in (Gdk.KEY_Down, Gdk.KEY_Up):
            self._move(1 if key == Gdk.KEY_Down else -1)
        elif ctrl and key == Gdk.KEY_space:
            self._toggle_mark()
        elif ctrl and shift and key == Gdk.KEY_Delete:
            self._clear()
        elif ctrl and key in _CTRL_ACTIONS:
            getattr(self, _CTRL_ACTIONS[key])()
        else:
            return False
        return True

    def _move(self, step: int) -> None:
        row = self._list.get_selected_row()
        index = (row.get_index() if row else -1) + step
        target = self._list.get_row_at_index(max(0, min(index, len(self._shown) - 1)))
        if target:
            self._list.select_row(target)
            target.grab_focus()
            self._entry.grab_focus_without_selecting()

    def _paste_row(self, index: int) -> None:
        if index < len(self._shown):
            self._list.select_row(self._list.get_row_at_index(index))
            self._marked = []
            self._enter("ctrl_v")

    # ── actions ───────────────────────────────────────────────────────────────
    def _enter(self, paste: str | None) -> None:
        if self._marked:
            clips = [c for c in (self._store.get(i) for i in self._marked) if c is not None]
            text = joined_text(clips)
            if not text:
                self._update_status("images can't be joined — unmark them, or paste one at a time")
                return
            self._finish(lambda: self._copy("copy_text", {"text": text}, paste))
            return
        clip = self._selected()
        if clip is not None:
            self._finish(lambda: self._copy("copy", {"id": clip.id}, paste))

    def _finish(self, copy) -> None:
        """Close first so focus goes back to the window you came from, then copy (and
        let the top-bar app press the paste keys there)."""
        self.hide()
        while Gtk.events_pending():
            Gtk.main_iteration()
        copy()
        self.destroy()

    def _toggle_mark(self) -> None:
        clip = self._selected()
        if clip is None:
            return
        row = self._list.get_selected_row().get_index()
        self._marked = [i for i in self._marked if i != clip.id] if clip.id in self._marked \
            else [*self._marked, clip.id]
        self._refill()
        self._list.select_row(self._list.get_row_at_index(min(row + 1, len(self._shown) - 1)))

    def _transform(self) -> None:
        clip = self._selected()
        if clip is None or clip.is_image:
            self._update_status("pick a text clip to transform")
            return
        menu = Gtk.Menu()
        for key, label, _ in transforms.TRANSFORMS:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _i, k=key: self._apply_transform(clip, k))
            menu.append(item)
        menu.show_all()
        self._modal = True
        menu.connect("deactivate", lambda *_: GLib.idle_add(self._end_modal))
        row = self._list.get_selected_row()
        menu.popup_at_widget(row, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def _apply_transform(self, clip: Clip, key: str) -> None:
        full = self._store.get(clip.id)
        try:
            transforms.apply(key, full.text if full else "")
        except ValueError as exc:  # say why here, instead of closing and pasting nothing
            self._update_status(f"{key.replace('_', ' ')}: {exc}")
            return
        self._finish(lambda: self._copy("copy", {"id": clip.id, "transform": key}, "ctrl_v"))

    def _end_modal(self) -> bool:
        self._modal = False
        if not self.is_active():
            self.present()
        return False

    def _edit(self) -> None:
        clip = self._selected()
        full = self._store.get(clip.id) if clip is not None and not clip.is_image else None
        if full is None:
            self._update_status("pick a text clip to edit")
            return
        self._modal = True
        result = dialogs.edit_text(self, paths_text(full.text.splitlines()) if full.is_files else full.text)
        self._modal = False
        if result is None:
            self.present()
            return
        text, how = result
        self._finish(lambda: self._copy("copy_text", {"text": text}, "ctrl_v" if how == dialogs.PASTE else None))

    def _name(self) -> None:
        clip = self._selected()
        if clip is None:
            return
        self._modal = True
        name = dialogs.ask_name(self, clip.name)
        self._modal = False
        if name is not None:
            try:
                self._store.set_name(clip.id, name)
            except StoreLocked as exc:
                self._update_status(str(exc))
                self.present()
                return
            _notify_tray()
            self._reload()
        self.present()

    def _pin(self) -> None:
        clip = self._selected()
        if clip:
            self._store.set_pinned(clip.id, not clip.pinned)
            self._reload()

    def _delete(self) -> None:
        clip = self._selected()
        if clip:
            self._store.delete(clip.id)
            self._reload()

    def _clear(self) -> None:
        self._modal = True  # the question takes focus; that must not close the dropdown
        dialog = Gtk.MessageDialog(transient_for=self, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text="Clear the clipboard history?")
        dialog.format_secondary_text("Every clip except pinned ones is deleted and overwritten on disk.")
        if dialog.run() == Gtk.ResponseType.OK:
            self._store.clear_unpinned()
            self._reload()
        dialog.destroy()
        self._modal = False
        self.present()


_CTRL_ACTIONS = {Gdk.KEY_p: "_pin", Gdk.KEY_P: "_pin", Gdk.KEY_d: "_delete", Gdk.KEY_D: "_delete",
                 Gdk.KEY_t: "_transform", Gdk.KEY_T: "_transform", Gdk.KEY_e: "_edit", Gdk.KEY_E: "_edit",
                 Gdk.KEY_n: "_name", Gdk.KEY_N: "_name"}
TRAY = "/usr/bin/clip-keeper-tray"
SERVICE = "/usr/lib/systemd/user/clip-keeper.service"


def _install_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _notify_tray() -> None:
    try:
        ipc.request("refresh", timeout=1)
    except ipc.TrayUnavailable:
        pass


def _copy_via_tray(cmd: str, args: dict, paste: str | None = None) -> None:
    """The top-bar app owns the clipboard: it outlives this popup, so the content stays
    pasteable. If it isn't running, start it and retry — a copy made by this short-lived
    window would vanish the moment it closes."""
    for attempt in range(2):
        try:
            reply = ipc.request(cmd, **args, **({"paste": paste} if paste else {}))
            if reply.get("ok"):
                _notify_tray()
                return
            raise ipc.TrayUnavailable(reply.get("error") or "the clip is gone")
        except ipc.TrayUnavailable as exc:
            if attempt == 0 and os.path.exists(TRAY) and "not running" in str(exc):
                _start_tray()
                _wait_for(paths.socket_path(), 4.0)
                continue
            dialogs.error("Could not copy the clip", str(exc))
            return


def _start_tray() -> None:
    """Through its systemd user service when installed (so crashes restart it), else directly."""
    command = ["systemctl", "--user", "start", "--no-block", "clip-keeper.service"] if os.path.exists(SERVICE) \
        else [TRAY]
    subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for(path, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.1)
    time.sleep(0.3)  # the socket file appears just before the server is listening


def _single_instance():
    lock = paths.runtime_dir() / "popup.lock"
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
        return 0  # already open (Super+V pressed twice)
    try:
        Popup()
    except sqlite3.Error as exc:
        dialogs.error("Clipboard history is unavailable", str(exc))
        return 1
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
