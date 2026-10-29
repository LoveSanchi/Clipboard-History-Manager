import struct

import pytest

from clip_keeper import vkbd


@pytest.fixture
def fake_device(monkeypatch):
    calls, writes = [], []
    monkeypatch.setattr(vkbd.fcntl, "ioctl", lambda fd, req, arg=0: calls.append((req, arg)))
    monkeypatch.setattr(vkbd.os, "write", lambda fd, data: writes.append(data) or len(data))
    monkeypatch.setattr(vkbd.os, "close", lambda fd: calls.append(("close", fd)))
    return calls, writes


def events(writes):
    out = []
    for blob in writes:
        for i in range(0, len(blob), 24):
            _, _, kind, code, value = struct.unpack("llHHi", blob[i:i + 24])
            if kind == vkbd.EV_KEY:
                out.append((code, value))
    return out


def test_sets_up_a_keyboard_with_only_the_paste_keys(fake_device):
    calls, _ = fake_device
    kb = vkbd.VirtualKeyboard(opener=lambda path, flags: 7)
    requests = [c[0] for c in calls]
    assert requests[0] == vkbd.UI_SET_EVBIT and requests.count(vkbd.UI_SET_KEYBIT) == len(vkbd.KEYS)
    assert vkbd.UI_DEV_SETUP in requests and requests[-1] == vkbd.UI_DEV_CREATE
    kb.close()
    assert (vkbd.UI_DEV_DESTROY, 0) in calls and ("close", 7) in calls


@pytest.mark.parametrize("combo, expected", [
    ("ctrl_v", [(29, 1), (47, 1), (47, 0), (29, 0)]),
    ("ctrl_shift_v", [(29, 1), (42, 1), (47, 1), (47, 0), (42, 0), (29, 0)]),
    ("shift_insert", [(42, 1), (110, 1), (110, 0), (42, 0)]),
])
def test_presses_and_releases_in_order(fake_device, combo, expected):
    _, writes = fake_device
    kb = vkbd.VirtualKeyboard(opener=lambda path, flags: 7)
    slept = []
    kb.press(combo, sleep=slept.append)
    assert events(writes) == expected
    assert slept[0] > 0.2      # waits for the compositor to adopt the new device first


def test_unavailable_device_gives_a_helpful_error():
    def denied(path, flags):
        raise PermissionError(13, "Permission denied")
    with pytest.raises(vkbd.Unavailable, match="'input' group"):
        vkbd.VirtualKeyboard(opener=denied)


def test_refused_setup_closes_the_device(monkeypatch):
    closed = []
    monkeypatch.setattr(vkbd.os, "close", lambda fd: closed.append(fd))

    def refuse(fd, req, arg=0):
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(vkbd.fcntl, "ioctl", refuse)
    with pytest.raises(vkbd.Unavailable, match="refused"):
        vkbd.VirtualKeyboard(opener=lambda path, flags: 9)
    assert closed == [9]
