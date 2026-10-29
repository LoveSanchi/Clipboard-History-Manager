"""The top-bar process's clipboard side: watch changes, and own the clipboard when a
clip is copied back. It talks X11 through XWayland on purpose: GNOME relays the Wayland
clipboard to X11 clients (core Mutter behaviour, stable across versions), while plain
Wayland apps may only touch the clipboard when they have keyboard focus.

A password manager can take the clipboard between our "what kinds of data?" and "give me
the text" requests. Every change bumps a generation counter; data is only handed on if no
change happened meanwhile AND a second look at the targets still shows no secret flag."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from clip_keeper import formats  # noqa: E402
from clip_keeper.filters import PASSWORD_HINT_TARGETS  # noqa: E402

OnText = Callable[[str | None, bool, str], None]  # text, password hint, html
OnImage = Callable[[bytes, int, int], None]
OnFiles = Callable[[list[str]], None]
TEXT_TARGETS = ("UTF8_STRING", "STRING", "TEXT", "text/plain", "text/plain;charset=utf-8")
IMAGE_TARGET = "image/png"


def classify(target_names: set[str]) -> str:
    """"secret", "files", "text", "image" or "other" for a set of clipboard target names.
    Text wins over images (a copied web image usually also carries a URL or alt text)."""
    if any(t in target_names for t in PASSWORD_HINT_TARGETS):
        return "secret"
    if formats.GNOME_FILES in target_names:
        return "files"
    if any(t in target_names for t in TEXT_TARGETS):
        return "text"
    return "image" if IMAGE_TARGET in target_names else "other"


def _names(atoms) -> set[str]:
    return {a.name() for a in (atoms or [])}


def _atom(name: str):
    return Gdk.Atom.intern(name, False)


class ClipboardWatch:
    def __init__(self, on_text: OnText, on_image: OnImage | None = None, on_files: OnFiles | None = None):
        self._cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self._on_text = on_text
        self._on_image = on_image
        self._on_files = on_files
        self._generation = 0
        self._owner: _Owner | None = None
        self._cb.connect("owner-change", self._changed)

    def _changed(self, *_args) -> None:
        self._generation += 1
        generation = self._generation
        self._cb.request_targets(lambda _c, atoms, *_r: self._got_targets(generation, atoms))

    def _got_targets(self, generation: int, atoms) -> None:
        if generation != self._generation:
            return  # the clipboard changed again: that change brings its own targets
        names = _names(atoms)
        kind = classify(names)
        if kind == "secret":
            self._on_text(None, True, "")  # never even read the secret
        elif kind == "files" and self._on_files is not None:
            self._cb.request_contents(_atom(formats.GNOME_FILES), lambda _c, data: self._got_files(generation, data))
        elif kind in ("text", "files"):
            wants_html = formats.HTML in names
            self._cb.request_text(lambda _c, text: self._got_text(generation, text, wants_html))
        elif kind == "image" and self._on_image is not None:
            self._cb.request_contents(_atom(IMAGE_TARGET), lambda _c, data: self._got_image(generation, data))

    def _got_text(self, generation: int, text: str | None, wants_html: bool = False) -> None:
        if generation != self._generation:
            return  # someone (maybe a password manager) took the clipboard meanwhile
        if wants_html:
            self._cb.request_contents(_atom(formats.HTML), lambda _c, data: self._got_html(generation, text, data))
        else:
            self._cb.request_targets(lambda _c, atoms, *_r: self._confirm(generation, text, atoms))

    def _got_html(self, generation: int, text: str | None, data) -> None:
        if generation != self._generation:
            return
        html = formats.decode_html(data.get_data() if data is not None else None)
        self._cb.request_targets(lambda _c, atoms, *_r: self._confirm(generation, text, atoms, html))

    def _confirm(self, generation: int, text: str | None, atoms, html: str = "") -> None:
        if generation != self._generation or classify(_names(atoms)) == "secret":
            return
        self._on_text(text, False, html)

    def _got_files(self, generation: int, data) -> None:
        if generation != self._generation:
            return
        uris = formats.parse_gnome_files(data.get_data() if data is not None else None)
        if uris:
            self._cb.request_targets(lambda _c, atoms, *_r: self._confirm_files(generation, uris, atoms))

    def _confirm_files(self, generation: int, uris: list[str], atoms) -> None:
        if generation == self._generation and classify(_names(atoms)) == "files":
            self._on_files(uris)

    def _got_image(self, generation: int, data) -> None:
        if generation != self._generation or data is None:
            return
        png = bytes(data.get_data() or b"")
        if not png or formats.image_too_big(png):
            return  # never decode an image that declares itself huge
        width, height = formats.png_size(png)
        self._on_image(png, width, height)

    # ── copying back ──────────────────────────────────────────────────────────
    def set_image(self, png: bytes) -> None:
        from gi.repository import GdkPixbuf
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(png)
        loader.close()
        self._cb.set_image(loader.get_pixbuf())
        self._cb.store()

    def set_text(self, text: str, html: str = "") -> None:
        """Become the clipboard owner (this process lives on, so the content stays pasteable)."""
        if html:
            self._own({formats.HTML: html.encode("utf-8")}, text)
            return
        self._cb.set_text(text, -1)
        self._cb.store()

    def set_files(self, uris: list[str]) -> None:
        """Files as Nautilus offers them: paste them in Files, attach them in a browser, or
        get their paths in a text field."""
        self._own({formats.GNOME_FILES: formats.gnome_files(uris), formats.URI_LIST: formats.uri_list(uris)},
                  formats.paths_text(uris))

    def _own(self, extra: dict[str, bytes], text: str) -> None:
        if self._owner is None:
            self._owner = _Owner()
        self._owner.take(extra, text)


class _Owner:
    """Owns the clipboard with several formats at once (Gtk.Clipboard's own multi-format
    call is not available from Python): a hidden widget answers each paste request."""

    def __init__(self) -> None:
        self._widget = Gtk.Invisible()
        self._formats: list[tuple[str, bytes | None]] = []
        self._text = ""
        self._widget.connect("selection-get", self._get)

    def take(self, extra: dict[str, bytes], text: str) -> None:
        self._formats = [*extra.items(), *((t, None) for t in TEXT_TARGETS)]
        self._text = text
        selection = Gdk.SELECTION_CLIPBOARD
        Gtk.selection_clear_targets(self._widget, selection)
        for info, (name, _) in enumerate(self._formats):
            Gtk.selection_add_target(self._widget, selection, _atom(name), info)
        Gtk.selection_owner_set(self._widget, selection, Gdk.CURRENT_TIME)

    def _get(self, _widget, selection_data, info: int, _time) -> None:
        if not 0 <= info < len(self._formats):
            return
        name, data = self._formats[info]
        if data is None:
            selection_data.set_text(self._text, -1)
        else:
            selection_data.set(_atom(name), 8, data)
