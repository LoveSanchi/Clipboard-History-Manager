"""Keyboard shortcuts through GNOME's documented custom-keybinding settings (stable for
years and across GNOME versions):

  Super+V      → the clipboard dropdown
  Super+Alt+1  → paste the last copied clip
  Super+Alt+2  → paste the second-last copied clip
  Super+Alt+3…9 → paste snippets 1…7 (pinned clips, named ones first)

GNOME uses Super+V for the message tray, which also has Super+M: we take V, keep M, and on
unbind give V back only if it had it."""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from typing import Callable

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
TRAY = ("org.gnome.shell.keybindings", "toggle-message-tray")
BINDING = "<Super>v"
SNIPPET_KEYS = 7

Run = Callable[[list[str]], str]


@dataclass(frozen=True)
class Shortcut:
    slug: str
    name: str
    command: str
    binding: str

    @property
    def path(self) -> str:
        return f"{BASE}{self.slug}/"

    @property
    def schema(self) -> str:
        return f"{MEDIA_KEYS}.custom-keybinding:{self.path}"


def shortcuts(popup: str = "/usr/bin/clip-keeper-popup", cli: str = "/usr/bin/clip-keeper") -> list[Shortcut]:
    return [
        Shortcut("clip-keeper", "clip-keeper", popup, BINDING),
        Shortcut("clip-keeper-paste-1", "clip-keeper: paste last copied", f"{cli} paste 1", "<Super><Alt>1"),
        Shortcut("clip-keeper-paste-2", "clip-keeper: paste second-last copied", f"{cli} paste 2", "<Super><Alt>2"),
        *(Shortcut(f"clip-keeper-snippet-{n}", f"clip-keeper: paste snippet {n}", f"{cli} paste-snippet {n}",
                   f"<Super><Alt>{n + 2}") for n in range(1, SNIPPET_KEYS + 1)),
    ]


# Kept for callers and tests that refer to the main (Super+V) binding.
CUSTOM_PATH = shortcuts()[0].path
CUSTOM_SCHEMA = shortcuts()[0].schema


def _gsettings(args: list[str]) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, check=True, timeout=10).stdout.strip()


def parse_list(value: str) -> list[str]:
    """Parse a GVariant string array. Raises ValueError rather than guessing: writing back
    a wrong guess would wipe the user's other shortcuts."""
    text = value.strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    if not text or text == "[]":
        return []
    try:
        items = ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f"cannot understand the GNOME setting {value!r}; leaving shortcuts untouched") from exc
    if not isinstance(items, (list, tuple)):
        raise ValueError(f"unexpected GNOME setting {value!r}; leaving shortcuts untouched")
    return [str(i) for i in items]


def format_list(items: list[str]) -> str:
    return "@as []" if not items else "[" + ", ".join(repr(i) for i in items) + "]"


def bind(popup_command: str, run: Run = _gsettings, cli: str = "/usr/bin/clip-keeper") -> str:
    """Install all clip-keeper shortcuts; returns the message-tray value to restore later."""
    wanted = shortcuts(popup_command, cli)
    customs = parse_list(run(["get", MEDIA_KEYS, "custom-keybindings"]))  # parse before changing anything
    tray_before = run(["get", *TRAY])
    tray_keys = parse_list(tray_before)
    if BINDING.lower() in [k.lower() for k in tray_keys]:
        run(["set", *TRAY, format_list([k for k in tray_keys if k.lower() != BINDING.lower()])])
    missing = [s.path for s in wanted if s.path not in customs]
    if missing:
        run(["set", MEDIA_KEYS, "custom-keybindings", format_list([*customs, *missing])])
    for s in wanted:
        run(["set", s.schema, "name", s.name])
        run(["set", s.schema, "command", s.command])
        run(["set", s.schema, "binding", s.binding])
    return tray_before


def unbind(saved_tray: str, run: Run = _gsettings) -> None:
    """Remove our shortcuts and give Super+V back to the message tray — but only if it had it
    before, and without undoing other changes the user made to that setting since."""
    ours = {s.path for s in shortcuts()}
    customs = parse_list(run(["get", MEDIA_KEYS, "custom-keybindings"]))
    run(["set", MEDIA_KEYS, "custom-keybindings", format_list([c for c in customs if c not in ours])])
    for s in shortcuts():
        run(["reset-recursively", s.schema])
    had_it = any(k.lower() == BINDING.lower() for k in parse_list(saved_tray)) if saved_tray else False
    current = parse_list(run(["get", *TRAY]))
    if had_it and not any(k.lower() == BINDING.lower() for k in current):
        run(["set", *TRAY, format_list([BINDING, *current])])
