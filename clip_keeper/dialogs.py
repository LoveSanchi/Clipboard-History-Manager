"""Small modal dialogs for the dropdown: edit before paste, name a snippet, errors."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

PASTE, COPY = 1, 2


def edit_text(parent: Gtk.Window | None, text: str) -> tuple[str, int] | None:
    """(edited text, PASTE or COPY), or None when cancelled. Ctrl+Enter pastes."""
    dialog = Gtk.Dialog(title="Edit before pasting", transient_for=parent, modal=True)
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Copy only", COPY, "Paste", PASTE)
    dialog.set_default_response(PASTE)
    dialog.set_default_size(560, 320)
    dialog.set_keep_above(True)
    view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True, left_margin=6, right_margin=6)
    view.get_buffer().set_text(text)
    view.connect("key-press-event", lambda _w, e: _ctrl_enter(dialog, e))
    scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    scroller.add(view)
    box = dialog.get_content_area()
    box.pack_start(Gtk.Label(label="Ctrl+Enter pastes · the edited text is not saved to the history", xalign=0),
                   False, False, 4)
    box.pack_start(scroller, True, True, 0)
    dialog.show_all()
    view.grab_focus()
    response = dialog.run()
    buffer = view.get_buffer()
    edited = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    dialog.destroy()
    return (edited, response) if response in (PASTE, COPY) else None


def _ctrl_enter(dialog: Gtk.Dialog, event) -> bool:
    from gi.repository import Gdk
    if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and event.state & Gdk.ModifierType.CONTROL_MASK:
        dialog.response(PASTE)
        return True
    return False


def ask_name(parent: Gtk.Window | None, current: str) -> str | None:
    """A snippet name ("" removes it), or None when cancelled."""
    dialog = Gtk.Dialog(title="Name this snippet", transient_for=parent, modal=True)
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    dialog.set_keep_above(True)
    entry = Gtk.Entry(text=current, activates_default=True, placeholder_text="e.g. home address")
    box = dialog.get_content_area()
    box.set_spacing(6)
    box.pack_start(Gtk.Label(label="Named clips are pinned; snippets 1–7 paste with Super+Alt+3…9.\n"
                                   "Leave empty to remove the name.", xalign=0), False, False, 0)
    box.pack_start(entry, False, False, 0)
    dialog.show_all()
    response = dialog.run()
    name = entry.get_text()
    dialog.destroy()
    return name[:80] if response == Gtk.ResponseType.OK else None


def error(title: str, body: str) -> None:
    dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=title)
    dialog.format_secondary_text(body)
    dialog.set_keep_above(True)
    dialog.run()
    dialog.destroy()
