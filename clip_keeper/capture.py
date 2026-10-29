"""Deciding what to do with one clipboard change (no GTK here, so it is testable)."""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field

from clip_keeper import filters
from clip_keeper.formats import MAX_FILES, MAX_IMAGE_BYTES
from clip_keeper.settings import Settings
from clip_keeper.store import Store, content_hash

REPEAT_WINDOW_SECONDS = 2.0    # apps often re-announce the same clipboard many times in a burst
OWN_WINDOW_SECONDS = 10.0      # how long a clip we set ourselves is recognised when it comes back
OWN_ANY_WINDOW_SECONDS = 2.0   # ... when matched by kind only (an image GTK re-encodes): keep it short
SKIPPED_ONCE = "skipped (skip next copy)"


@dataclass
class Stats:
    stored: int = 0
    skipped: collections.Counter = field(default_factory=collections.Counter)
    last_skip: str = ""


class Capturer:
    def __init__(self, store: Store, clock=time.monotonic):
        self._store = store
        self._clock = clock
        self._last: tuple[str, float] = ("", 0.0)
        self._own: tuple[str, str | None, float] = ("", None, 0.0)
        self._own_window = OWN_WINDOW_SECONDS
        self._paused_until = 0.0
        self.skip_next = False
        self.stats = Stats()

    def use(self, store: Store) -> None:
        """Switch to a reopened store, keeping skip-next, the timed pause and the stats."""
        self._store = store

    # ── runtime switches (not saved: a restart forgets them, on purpose) ──────
    def pause_for(self, seconds: float) -> None:
        self._paused_until = self._clock() + seconds

    def resume(self) -> None:
        self._paused_until = 0.0

    @property
    def paused_seconds_left(self) -> int:
        return max(0, int(self._paused_until - self._clock() + 0.999))

    def expect_own(self, kind: str, data: bytes | None = None) -> None:
        """We are about to put this on the clipboard (a transformed or edited paste): when the
        change comes back, don't record it as a new copy. data=None matches any clip of the
        kind (GTK re-encodes an image we copy back, so its bytes differ)."""
        self._own = (kind, None if data is None else content_hash(kind, data), self._clock())
        self._own_window = OWN_WINDOW_SECONDS if data is not None else OWN_ANY_WINDOW_SECONDS

    # ── the gate every change passes ──────────────────────────────────────────
    def _gate(self, kind: str, digest: str, settings: Settings) -> str | None:
        if settings.paused or self.paused_seconds_left:
            return "paused"
        now = self._clock()
        if digest == self._last[0] and now - self._last[1] < REPEAT_WINDOW_SECONDS:
            return "repeat"
        self._last = (digest, now)
        own_kind, own_digest, own_at = self._own
        if kind == own_kind and own_digest in (None, digest) and now - own_at < self._own_window:
            self._own = ("", None, 0.0)
            return "own"
        if self.skip_next:
            self.skip_next = False
            return self._skipped(SKIPPED_ONCE)
        return None

    def _skipped(self, reason: str) -> str:
        self.stats.skipped[reason] += 1
        self.stats.last_skip = reason
        return reason

    def _stored(self, settings: Settings) -> str:
        self._store.enforce_cap(settings.history_limit, settings.image_limit)
        self.stats.stored += 1
        return "stored"

    # ── kinds ─────────────────────────────────────────────────────────────────
    def handle(self, text: str | None, password_hint: bool, settings: Settings, html: str = "") -> str:
        """Returns "stored", "repeat", "paused", "own" or the skip reason."""
        digest = "password-hint" if password_hint else content_hash("text", (text or "").encode("utf-8"))
        verdict = self._gate("text", digest, settings)
        if verdict:
            return verdict
        reason = filters.skip_reason(text, password_hint, settings.skip_secrets)
        if reason:
            return self._skipped(reason)
        self._store.insert_text(text, html=html if settings.record_html else "")
        return self._stored(settings)

    def handle_files(self, uris: list[str] | None, settings: Settings) -> str:
        """Files copied in Files (Nautilus): the list of URIs, not the files themselves."""
        if not uris:
            return "empty"
        verdict = self._gate("files", content_hash("files", "\n".join(uris).encode()), settings)
        if verdict:
            return verdict
        if not settings.record_files:
            return "files are off"
        self._store.insert_files(uris[:MAX_FILES])
        return self._stored(settings)

    def handle_image(self, png: bytes | None, width: int, height: int, settings: Settings) -> str:
        """Screenshots and copied images. Password managers never copy images, so only the
        size limit and the pause switches apply."""
        if not png:
            return "empty"
        verdict = self._gate("image", content_hash("image", png), settings)
        if verdict:
            return verdict
        if not settings.record_images:
            return "images are off"
        if len(png) > MAX_IMAGE_BYTES:
            return self._skipped("image larger than 25 MiB")
        self._store.insert_image(png, width, height)
        return self._stored(settings)
