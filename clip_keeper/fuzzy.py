"""Fuzzy search for the popup: every query character must appear in order; consecutive
matches and matches at word starts rank higher; recency breaks ties."""

from __future__ import annotations

from clip_keeper.store import Clip, one_line


def score(query: str, text: str) -> int | None:
    """Higher is better; None when `query` is not a subsequence of `text` (case-insensitive)."""
    if not query:
        return 0
    q, t = query.lower(), text.lower()
    exact = t.find(q)
    if exact >= 0:
        return 10_000 - min(exact, 9_000)  # a plain substring beats any scattered match
    total, pos, previous = 0, 0, -2
    for ch in q:
        found = t.find(ch, pos)
        if found < 0:
            return None
        total += 10
        if found == previous + 1:
            total += 15
        if found == 0 or not t[found - 1].isalnum():
            total += 8
        total -= min(found - pos, 20)
        previous, pos = found, found + 1
    return total


def search(clips: list[Clip], query: str, limit: int = 200) -> list[Clip]:
    query = query.strip()
    if not query:
        return clips[:limit]
    ranked = []
    for order, clip in enumerate(clips):  # clips arrive pinned-first, newest-first
        s = score(query, one_line(f"{clip.name} {clip.text}" if clip.name else clip.text))
        if s is not None:
            ranked.append((-int(clip.pinned), -s, order, clip))
    return [item[3] for item in sorted(ranked, key=lambda r: r[:3])][:limit]


def search_all(clips: list[Clip], query: str, full_text_ids: list[int], limit: int = 200) -> list[Clip]:
    """Fuzzy matches on names and previews first, then clips whose full text (beyond the
    200-character preview) contains the query."""
    found = search(clips, query, limit)
    seen = {c.id for c in found}
    by_id = {c.id: c for c in clips}
    deeper = [by_id[i] for i in full_text_ids if i in by_id and i not in seen]
    return (found + deeper)[:limit]
