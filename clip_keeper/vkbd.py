"""A tiny virtual keyboard through the kernel's uinput device, used to press Ctrl+V after a
clip is chosen. Wayland does not let apps type into each other, but a (virtual) keyboard is
just a keyboard: this works in every app, and the uinput API has been stable since Linux 4.5,
so neither GNOME nor Ubuntu upgrades affect it.

Access to /dev/uinput comes from the `input` group or the package's udev `uaccess` rule.
Key codes are physical positions (KEY_V is where V sits on a US layout)."""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
import time

EV_SYN, EV_KEY, SYN_REPORT = 0x00, 0x01, 0
KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V, KEY_INSERT = 29, 42, 47, 110
KEYS = (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V, KEY_INSERT)

# ioctl numbers from <linux/uinput.h> (64-bit and 32-bit Linux agree on these)
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
BUS_VIRTUAL = 0x06
KEY_DELAY = 0.012
SETTLE_SECONDS = 0.35  # the compositor needs a moment to adopt a new input device

COMBOS = {
    "ctrl_v": (KEY_LEFTCTRL, KEY_V),
    "ctrl_shift_v": (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V),  # terminals
    "shift_insert": (KEY_LEFTSHIFT, KEY_INSERT),
}


class Unavailable(RuntimeError):
    pass


class _Setup(ctypes.Structure):  # struct uinput_setup
    _fields_ = [("bustype", ctypes.c_uint16), ("vendor", ctypes.c_uint16), ("product", ctypes.c_uint16),
                ("version", ctypes.c_uint16), ("name", ctypes.c_char * 80), ("ff_effects_max", ctypes.c_uint32)]


def _event(kind: int, code: int, value: int) -> bytes:
    """struct input_event: timeval (zero: the kernel stamps it), type, code, value."""
    return struct.pack("llHHi", 0, 0, kind, code, value)


class VirtualKeyboard:
    def __init__(self, path: str = "/dev/uinput", opener=os.open):
        try:
            self._fd = opener(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise Unavailable(f"cannot open {path}: {exc.strerror} — add yourself to the 'input' group "
                              f"or reinstall clip-keeper (its udev rule grants access)") from exc
        try:
            fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
            for key in KEYS:
                fcntl.ioctl(self._fd, UI_SET_KEYBIT, key)
            setup = _Setup(BUS_VIRTUAL, 0x1209, 0xC11B, 1, b"clip-keeper virtual keyboard", 0)
            fcntl.ioctl(self._fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self._fd, UI_DEV_CREATE)
        except OSError as exc:
            os.close(self._fd)
            raise Unavailable(f"uinput refused the virtual keyboard: {exc.strerror}") from exc
        self._ready_at = time.monotonic() + SETTLE_SECONDS

    def press(self, combo: str = "ctrl_v", sleep=time.sleep) -> None:
        keys = COMBOS[combo]
        wait = self._ready_at - time.monotonic()
        if wait > 0:
            sleep(wait)
        for key in keys:
            self._emit(EV_KEY, key, 1, sleep)
        for key in reversed(keys):
            self._emit(EV_KEY, key, 0, sleep)

    def _emit(self, kind: int, code: int, value: int, sleep) -> None:
        os.write(self._fd, _event(kind, code, value) + _event(EV_SYN, SYN_REPORT, 0))
        sleep(KEY_DELAY)

    def close(self) -> None:
        try:
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        finally:
            os.close(self._fd)
