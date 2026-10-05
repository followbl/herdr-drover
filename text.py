#!/usr/bin/env python3
"""Measure and cut text by the columns it occupies, not by characters.

Agent tabs are named from terminal titles, and those arrive with emoji and CJK
in them. `len()` says one, the terminal spends two, and every column after it
on the row lands a cell to the left -- so the status glyph drifts and the
selection bar ends mid-glyph. Everything that lays out a row goes through here.

The hard part is not CJK, which the Unicode tables answer directly, but the
symbols either side of the emoji line: `✓` and `✳` are one column, `😀` is
two, and `✳️` -- the same `✳` with a variation selector, which is what Claude
puts in a terminal title -- is two. So characters are measured in segments:
a base plus whatever modifiers ride along with it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterator

# Everything a composed row can contain that the terminal does not draw.
ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC
    r"|\x1b[@-Z\\-_]"  # two-character escapes
)

ELLIPSIS = "…"

VARIATION_EMOJI = "️"
VARIATION_TEXT = "︎"
ZWJ = "‍"

# Combining marks, joiners and format characters ride along with the character
# before them.
_ZERO = {"Mn", "Me", "Cf"}

# Codepoints below U+1F000 that terminals draw double-wide on their own
# (Unicode's Emoji_Presentation=Yes). The rest of the symbol blocks -- ✓, ✳,
# ▸, ◉ -- are one column unless a variation selector asks for emoji form.
_WIDE_SYMBOLS = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3),
    (0x25FD, 0x25FE), (0x2614, 0x2615), (0x2648, 0x2653), (0x267F, 0x267F),
    (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
    (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA),
    (0x26F2, 0x26F3), (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
    (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
    (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797),
    (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
)


def char_width(char: str) -> int:
    """Columns one character occupies, ignoring any modifiers after it."""
    if char in ("", ZWJ):
        return 0
    code = ord(char)
    if code < 0x7F:
        return 1 if code >= 0x20 else 0
    if 0xFE00 <= code <= 0xFE0F:
        return 0
    if unicodedata.category(char) in _ZERO:
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    for low, high in _WIDE_SYMBOLS:
        if low <= code <= high:
            return 2
    if 0x1F000 <= code <= 0x1FAFF:
        return 2
    return 1


def segments(text: str) -> Iterator[tuple[str, int]]:
    """Walk the text as drawn: each base character with its modifiers."""
    index = 0
    length = len(text)
    while index < length:
        base = text[index]
        size = char_width(base)
        end = index + 1
        while end < length:
            follower = text[end]
            if follower == VARIATION_EMOJI:
                if size:
                    size = 2  # emoji presentation: ✳ -> ✳️
                end += 1
                continue
            if follower == VARIATION_TEXT:
                if size:
                    size = 1
                end += 1
                continue
            if follower == ZWJ and end + 1 < length:
                end += 2  # one glyph built from several characters
                continue
            if unicodedata.combining(follower) or unicodedata.category(follower) in _ZERO:
                end += 1
                continue
            break
        yield text[index:end], size
        index = end


def width(text: str) -> int:
    """Columns the text occupies."""
    if text.isascii():
        return sum(1 for char in text if char >= " ")
    return sum(size for _, size in segments(text))


def truncate(text: str, limit: int) -> str:
    """Cut to exactly `limit` columns, marking the cut with an ellipsis."""
    if limit <= 0:
        return ""
    if width(text) <= limit:
        return text
    if limit == 1:
        return ELLIPSIS
    out: list[str] = []
    used = 0
    for chunk, size in segments(text):
        if used + size > limit - 1:
            break
        out.append(chunk)
        used += size
    return "".join(out) + ELLIPSIS + " " * max(0, limit - used - 1)


def pad(text: str, limit: int) -> str:
    """Exactly `limit` columns wide: padded with spaces, or truncated."""
    if limit <= 0:
        return ""
    size = width(text)
    if size == limit:
        return text
    if size < limit:
        return text + " " * (limit - size)
    return truncate(text, limit)


def strip(text: str) -> str:
    """The row as the terminal draws it, without the sequences that style it."""
    return ANSI.sub("", text)


def visible(text: str) -> int:
    """Columns an already-colored row occupies."""
    return width(strip(text)) if "\x1b" in text else width(text)


def pad_visible(text: str, limit: int) -> str:
    """Pad a colored row out to `limit` columns.

    Never truncates: cutting a row that carries escape sequences could cut one
    in half, and whatever composed the row already fitted its own text.
    """
    return text + " " * max(0, limit - visible(text))
