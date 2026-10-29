"""Render README screenshots from the real popup and menu labels, with made-up clips.

    python3 docs/screenshots/generate.py   (needs a desktop session; writes PNGs here)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from clip_keeper.popup import Popup  # noqa: E402
from clip_keeper.store import Store, now_millis, one_line  # noqa: E402

CLIPS = ["git stash pop", "docker compose logs -f api", "https://github.com/example/project/pull/42",
         "SELECT id, status FROM orders WHERE created_at > NOW() - INTERVAL 1 DAY;",
         "kubectl get pods -n staging", "Meeting moved to 4:30 — same room", "git status",
         "npm run build && npm test", "ssh deploy@bastion.example.com", "Thanks, merged!"]
CSS = b"""
.popup { background: #2d2d2d; border-radius: 12px; padding: 8px 0; }
.popup label { color: #f2f2f2; padding: 6px 18px; font-size: 11pt; }
.popup .info label { color: #9a9a9a; }
.popup separator { background: #4a4a4a; min-height: 1px; margin: 4px 0; }
.canvas { background: #1a1a1a; padding: 16px; }
.frame { background-color: @theme_bg_color; padding: 6px; }
"""


def spin(ms):
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def snapshot(widget, name, framed=True, show_all=True):
    off = Gtk.OffscreenWindow()
    if framed:
        frame = Gtk.EventBox()
        frame.get_style_context().add_class("frame")
        frame.add(widget)
        widget = frame
    off.add(widget)
    off.show_all() if show_all else (widget.show(), off.show())
    spin(500)
    off.get_surface().write_to_png(str(OUT / name))
    print("wrote", OUT / name)
    off.destroy()


def store():
    s = Store(memory=True)
    base = now_millis()
    for i, text in enumerate(CLIPS):
        s.insert_text(text, base - (len(CLIPS) - i) * 7 * 60_000)
    s.set_pinned(s.recent()[-2].id, True)  # pin "docker compose logs -f api"
    s.insert_files(["file:///home/me/Documents/report-q3.pdf", "file:///home/me/Pictures/diagram.png"],
                   base - 3 * 60_000)
    signature = s.insert_text("Best regards,\nAlex", base - 9 * 86_400_000)
    s.set_name(signature, "email signature")
    return s


def popup_png():
    original = Gtk.Window.show_all
    Gtk.Window.show_all = lambda self: None
    demo = store()
    popup = Popup(store=demo, copy=lambda cmd, args, paste=None: None)
    Gtk.Window.show_all = original
    popup._marked = [c.id for c in demo.recent() if c.text.startswith(("git stash", "npm run"))]
    popup._refill()
    body = popup.get_child()
    popup.remove(body)
    body.set_size_request(640, 420)
    snapshot(body, "popup.png", show_all=True)


def menu_png():
    items = [("info", "Clipboard history · 12 items · recording"), ("sep", "")]
    for clip in store().recent(8):
        badge = ("📌 " if clip.pinned else "") + ("📁 " if clip.is_files else "")
        name = f"{clip.name}: " if clip.name else ""
        items.append(("item", badge + (name + one_line(clip.text))[:58]))
    items += [("sep", ""), ("item", "Search history…    Super+V"),
              ("info", "Paste last / 2nd last:  Super+Alt+1 / 2 · snippets:  Super+Alt+3…9"),
              ("item", "Pause for 5 minutes"), ("item", "Skip the next copy"), ("item", "Pause recording"),
              ("item", "☐ Hide clip text in this menu"), ("item", "Clear history (keeps pinned)…"), ("sep", ""),
              ("item", "Quit (stops recording until next login)")]
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.get_style_context().add_class("popup")
    box.set_size_request(520, -1)
    for kind, text in items:
        if kind == "sep":
            box.pack_start(Gtk.Separator(), False, False, 0)
            continue
        line = Gtk.Box()
        line.get_style_context().add_class(kind)
        line.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
        box.pack_start(line, False, False, 0)
    canvas = Gtk.Box()
    canvas.get_style_context().add_class("canvas")
    canvas.pack_start(box, False, False, 0)
    snapshot(canvas, "menu.png", framed=False)


if __name__ == "__main__":
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    menu_png()
    popup_png()
