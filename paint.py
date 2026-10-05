#!/usr/bin/env python3
"""Send only the rows that changed.

The overlay recomposes every row on every frame, which keeps the layout code
in one place -- but it then used to *write* every row, once a second for the
age column and again on each keystroke. On a 60-row popup that is the whole
list repainted to move one highlight, and over SSH or inside a multiplexer
those bytes are the latency.

So this sits between the composed rows and the terminal, remembers what it
painted, and writes only the rows that differ. Row granularity, not cells:
one changed row costs a cursor address, the row, and an erase to end of line,
which is what the overlay was already paying per row anyway.
"""

from __future__ import annotations

from typing import Sequence

CSI = "\x1b["
RESET = CSI + "0m"


def compose(rows: Sequence[str]) -> str:
    """Paint every row from scratch, then erase whatever is below them."""
    out = [RESET + CSI + "H"]
    for index, row in enumerate(rows):
        out.append(f"{CSI}{index + 1};1H")
        out.append(row)
        out.append(RESET + CSI + "K")
    out.append(CSI + "J")
    return "".join(out)


class Painter:
    def __init__(self) -> None:
        self.cols = 0
        self.rows = 0
        self.painted: list[str] = []
        self.fresh = True

    def reset(self) -> None:
        """Forget the screen: the next frame repaints all of it."""
        self.fresh = True

    def frame(self, rows: Sequence[str], cols: int, height: int) -> str:
        """The bytes that turn the painted screen into these rows.

        Empty when nothing moved, which is the point: an idle second costs no
        output at all.
        """
        wanted = list(rows)[:height]
        if self.fresh or cols != self.cols or height != self.rows:
            self.cols = cols
            self.rows = height
            self.fresh = False
            self.painted = wanted
            return compose(wanted)

        out: list[str] = []
        for index in range(len(wanted)):
            previous = self.painted[index] if index < len(self.painted) else None
            row = wanted[index]
            if row == previous:
                continue
            out.append(f"{CSI}{index + 1};1H")
            out.append(row)
            out.append(RESET + CSI + "K")
        if len(wanted) < len(self.painted):
            # The frame got shorter: clear from the first row it no longer has.
            out.append(f"{CSI}{len(wanted) + 1};1H" + RESET + CSI + "J")
        self.painted = wanted
        return "".join(out)
