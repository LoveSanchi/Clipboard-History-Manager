"""clip-keeper — a private clipboard history for the GNOME top bar."""

from __future__ import annotations

import argparse
import sys

from clip_keeper import __version__, crypto, fuzzy, ipc, settings, shortcut
from clip_keeper.history import open_store
from clip_keeper.store import Store, one_line

POPUP_COMMAND = "/usr/bin/clip-keeper-popup"


def _store() -> Store:
    store, problem = open_store(settings.load())
    if problem:
        print(f"clip-keeper: {problem}", file=sys.stderr)
    return store


def _on(flag: bool) -> str:
    return "on" if flag else "off"


def cmd_status(args) -> int:
    s = settings.load()
    store = _store()
    expiry = f"unpinned clips expire after {s.expire_days} days" if s.expire_days else "clips never expire"
    print(f"history: {store.count()} clips (limit {s.history_limit}) · "
          f"{'PAUSED' if s.paused else 'recording'} · secret filter {_on(s.skip_secrets)}")
    print(f"{expiry} · encryption {_on(s.encrypt)} · images {_on(s.record_images)} · "
          f"files {_on(s.record_files)} · rich text {_on(s.record_html)} · menu text hidden {_on(s.private_menu)}")
    try:
        reply = ipc.request("status")
        skipped = ", ".join(f"{n}× {why}" for why, n in reply.get("skipped", {}).items()) or "none"
        print(f"top-bar app: running · stored this session {reply.get('stored', 0)} · skipped: {skipped}")
        if reply.get("locked"):
            print(f"top-bar app: {reply['locked']}")
        if reply.get("paused_seconds_left"):
            print(f"paused for another {reply['paused_seconds_left']} s")
        if reply.get("skip_next"):
            print("the next copy will be skipped")
        print(f"auto-paste: {reply.get('auto_paste', 'unknown')}")
    except ipc.TrayUnavailable:
        print("top-bar app: NOT running — nothing is being recorded (start: clip-keeper-tray)")
    return 0


def cmd_list(args) -> int:
    store = _store()
    clips = fuzzy.search(store.recent(), args.query or "", args.number)
    for clip in clips:
        print(_line(clip))
    return 0


def _line(clip) -> str:
    badge = "🖼 " if clip.is_image else "📁 " if clip.is_files else ""
    name = f"[{clip.name}] " if clip.name else ""
    return f"{clip.id:>6}  {'📌' if clip.pinned else '  '} {badge}{name}{one_line(clip.text)[:110]}"


def cmd_snippets(args) -> int:
    snippets = _store().snippets()
    for number, clip in enumerate(snippets[:7], 3):
        print(f"Super+Alt+{number}  {_line(clip)}")
    for clip in snippets[7:]:
        print(f"             {_line(clip)}")
    if not snippets:
        print("no snippets yet: pin a clip (Ctrl+P in Super+V) or name one: clip-keeper name ID NAME")
    return 0


def cmd_name(args) -> int:
    if not _store().set_name(args.id, " ".join(args.name)):
        print(f"there is no clip {args.id}", file=sys.stderr)
        return 1
    _refresh()
    print(f"clip {args.id} named and pinned" if args.name else f"clip {args.id}: name removed")
    return 0


SHORTCUT_PASTE_DELAY_MS = 450  # a shortcut fires on key press: give the user time to let go of Super+Alt


def cmd_paste(args) -> int:
    """Paste the n-th most recently copied clip into the focused window (Super+Alt+1/2)."""
    return _paste(_store().latest(args.n), args, f"there is no clip number {args.n} yet")


def cmd_paste_snippet(args) -> int:
    """Paste the n-th snippet (pinned clips, named ones first) — Super+Alt+3…9 are snippets 1…7."""
    snippets = _store().snippets()
    clip = snippets[args.n - 1] if 1 <= args.n <= len(snippets) else None
    return _paste(clip, args, f"there is no snippet {args.n}: pin or name a clip first")


def _paste(clip, args, missing: str) -> int:
    if clip is None:
        print(missing, file=sys.stderr)
        return 1
    reply = ipc.request("copy", id=clip.id, paste="ctrl_shift_v" if args.terminal else "ctrl_v",
                        delay_ms=SHORTCUT_PASTE_DELAY_MS)
    if not reply.get("ok"):
        print(f"clip-keeper: {reply.get('error', 'could not copy')}", file=sys.stderr)
        return 1
    return 0


def cmd_clear(args) -> int:
    print(f"cleared {_store().clear_unpinned()} clips (pinned ones kept)")
    _refresh()
    return 0


def cmd_pause(args, paused: bool) -> int:
    minutes = getattr(args, "minutes", None)
    if paused and minutes:
        ipc.request("pause_for", seconds=int(minutes * 60))
        print(f"recording paused for {minutes:g} minutes")
        return 0
    settings.update(paused=paused)
    if not paused:
        _try("pause_for", seconds=0)
    _refresh()
    print("recording paused" if paused else "recording resumed")
    return 0


def cmd_skip_next(args) -> int:
    ipc.request("skip_next")
    print("the next copy will not be recorded")
    return 0


def cmd_expire(args) -> int:
    settings.update(expire_days=args.days)
    removed = _store().expire(args.days)
    print(f"unpinned clips now expire after {args.days} days (removed {removed} now)" if args.days
          else "clips never expire")
    _refresh()
    return 0


def cmd_switch(args, field: str, label: str) -> int:
    settings.update(**{field: args.state == "on"})
    print(f"{label} {args.state}")
    _refresh()
    return 0


def cmd_encrypt(args) -> int:
    """Re-write the whole history encrypted (key into GNOME Keyring) or back to plain. The
    top-bar app is paused meanwhile and must confirm the switch before the old key goes."""
    on = args.state == "on"
    if settings.load().encrypt == on:
        print(f"encryption is already {args.state}")
        return 0
    key = crypto.load_key(create=on)
    _try("pause_for", seconds=120)
    try:
        store = Store(cipher=None if on else crypto.Cipher(key))
        rewritten = store.reencrypt(crypto.Cipher(key) if on else None)
        scrubbed = store.scrubbed
        store.close()
        settings.update(encrypt=on)
        switched = _tray_switched(on)
    finally:
        _try("pause_for", seconds=0)
    print(f"encryption {args.state}: {rewritten} clips rewritten"
          + (" · key stored in GNOME Keyring (\"clip-keeper history key\")" if on else ""))
    if not scrubbed:
        print("note: another program was reading the history, so its write-ahead log could not be emptied "
              "yet; it is emptied on the next delete")
    if not on:
        if switched:
            crypto.delete_key()
            print("key removed from GNOME Keyring")
        else:
            print("the top-bar app did not confirm the switch, so the key was kept in GNOME Keyring; "
                  "restart it (systemctl --user restart clip-keeper) and run this again to remove the key")
    return 0


def _tray_switched(on: bool) -> bool:
    """True when the top-bar app now uses the new form (or isn't running at all)."""
    try:
        reply = ipc.request("reopen")
    except ipc.TrayUnavailable:
        return True
    return bool(reply.get("ok")) and reply.get("encrypted") == on and not reply.get("locked")


def cmd_bind(args) -> int:
    saved = shortcut.bind(args.command)
    if not settings.load().saved_tray_shortcut:
        settings.update(saved_tray_shortcut=saved)
    print("Super+V now opens the clipboard history (GNOME's message tray stays on Super+M); "
          "Super+Alt+1/2 paste the last clips, Super+Alt+3…9 your snippets")
    return 0


def cmd_unbind(args) -> int:
    shortcut.unbind(settings.load().saved_tray_shortcut)
    settings.update(saved_tray_shortcut="")
    print("Super+V restored to GNOME's default")
    return 0


def cmd_auto_paste(args) -> int:
    settings.update(auto_paste=args.state == "on")
    print(f"auto-paste {args.state} (Super+V {'pastes' if args.state == 'on' else 'only copies'})")
    _refresh()
    return 0


def cmd_images(args) -> int:
    settings.update(record_images=args.state == "on")
    print(f"recording images {args.state}")
    return 0


def cmd_secrets(args) -> int:
    settings.update(skip_secrets=args.state == "on")
    print(f"secret filter {args.state}")
    return 0


def _refresh() -> None:
    _try("refresh")


def _try(cmd: str, **args) -> None:
    """Tell the top-bar app, if it runs; settings changes reach it on its next tick anyway."""
    try:
        ipc.request(cmd, **args)
    except ipc.TrayUnavailable:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clip-keeper", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="is it recording, and how much is stored").set_defaults(run=cmd_status)
    s = sub.add_parser("list", help="recent clips (optionally fuzzy-filtered)")
    s.add_argument("query", nargs="?")
    s.add_argument("-n", "--number", type=int, default=20)
    s.set_defaults(run=cmd_list)
    sub.add_parser("clear", help="delete every unpinned clip").set_defaults(run=cmd_clear)
    for name, run, what in (("paste", cmd_paste, "the n-th most recently copied clip (1 = last)"),
                            ("paste-snippet", cmd_paste_snippet, "the n-th snippet (Super+Alt+3 = 1)")):
        s = sub.add_parser(name, help=f"paste {what}")
        s.add_argument("n", type=int)
        s.add_argument("--terminal", action="store_true", help="press Ctrl+Shift+V instead of Ctrl+V")
        s.set_defaults(run=run)
    sub.add_parser("snippets", help="pinned clips and their Super+Alt keys").set_defaults(run=cmd_snippets)
    s = sub.add_parser("name", help="name a clip (makes it a pinned snippet); no name removes it")
    s.add_argument("id", type=int)
    s.add_argument("name", nargs="*")
    s.set_defaults(run=cmd_name)
    s = sub.add_parser("pause", help="stop recording until resumed (or for --minutes)")
    s.add_argument("--minutes", type=float)
    s.set_defaults(run=lambda a: cmd_pause(a, True))
    sub.add_parser("resume", help="record again").set_defaults(run=lambda a: cmd_pause(a, False))
    sub.add_parser("skip-next", help="don't record the next copy").set_defaults(run=cmd_skip_next)
    s = sub.add_parser("expire", help="delete unpinned clips older than DAYS (0 = never; default 7)")
    s.add_argument("days", type=int, choices=range(0, 3651), metavar="DAYS")
    s.set_defaults(run=cmd_expire)
    for name, field, label, what in (
            ("files", "record_files", "recording file copies", "record files copied in Files (default on)"),
            ("rich-text", "record_html", "keeping rich text", "keep formatting (HTML) with text (default on)"),
            ("private-menu", "private_menu", "hiding clip text in the top-bar menu",
             "show 'Clip 1, 2…' instead of clip text in the top-bar menu")):
        s = sub.add_parser(name, help=what)
        s.add_argument("state", choices=["on", "off"])
        s.set_defaults(run=lambda a, f=field, lb=label: cmd_switch(a, f, lb))
    s = sub.add_parser("encrypt", help="encrypt the history at rest, key in GNOME Keyring (default off)")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(run=cmd_encrypt)
    s = sub.add_parser("secret-filter", help="skip clips that look like passwords or tokens (default on)")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(run=cmd_secrets)
    s = sub.add_parser("auto-paste", help="paste the chosen clip into the window you came from (default on)")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(run=cmd_auto_paste)
    s = sub.add_parser("images", help="also record screenshots and copied images (default on)")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(run=cmd_images)
    s = sub.add_parser("bind-shortcut", help="make Super+V open the popup")
    s.add_argument("--command", default=POPUP_COMMAND)
    s.set_defaults(run=cmd_bind)
    sub.add_parser("unbind-shortcut", help="give Super+V back to GNOME").set_defaults(run=cmd_unbind)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except ipc.TrayUnavailable as exc:
        print(f"clip-keeper: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, crypto.KeyUnavailable) as exc:
        print(f"clip-keeper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
