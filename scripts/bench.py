#!/usr/bin/env python3
"""Time the open path against the live Herdr session.

Run it inside Herdr: `python3 scripts/bench.py`. Everything measured here is
on the path between the key press and the first frame, except the repaint
numbers, which are what cycling and the age tick cost afterwards.

    python3 scripts/bench.py --json   # machine-readable, for comparing runs
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from typing import Any, Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import api  # noqa: E402
import lib  # noqa: E402
import paint  # noqa: E402
import switcher  # noqa: E402


def timed(label: str, work: Callable[[], Any], runs: int = 5) -> dict[str, Any]:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        work()
        samples.append((time.perf_counter() - start) * 1000.0)
    return {
        "label": label,
        "runs": runs,
        # The open path runs each of these once per press, so the first sample
        # -- cold caches, binary not resident -- is the honest one. Repeating a
        # subprocess call in a loop flatters it.
        "first_ms": round(samples[0], 2),
        "min_ms": round(min(samples), 2),
        "median_ms": round(statistics.median(samples), 2),
        "max_ms": round(max(samples), 2),
    }


def cli_pair() -> None:
    """What the old data path cost: two CLI calls, one process each."""
    for args in (["tab", "list"], ["workspace", "list"]):
        subprocess.run([api.herdr_bin(), *args], capture_output=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("--runs", type=int, default=5)
    options = parser.parse_args()

    client = api.client()
    if not client.socket_ok:
        print("warning: HERDR_SOCKET_PATH is not usable; socket rows will show CLI timings")

    snap = client.snapshot()
    mru = lib.load_mru()
    results = [
        timed("session.snapshot over the socket", client.snapshot, options.runs),
        timed("herdr tab list + workspace list (CLI)", cli_pair, options.runs),
        timed("build_items from a snapshot", lambda: lib.build_items(mru, snap=snap), options.runs),
        timed("load_mru", lib.load_mru, options.runs),
    ]

    items = [lib.new_tab_item(), *lib.build_items(mru, snap=snap)]
    overlay = switcher.Switcher.__new__(switcher.Switcher)
    switcher.Switcher.__init__(overlay, snap=snap)
    overlay.all_items = items

    def compose_once() -> None:
        overlay.painter = paint.Painter()
        overlay.compose()

    results.append(timed("compose one frame", compose_once, options.runs))

    rows, cols = overlay.compose()
    painter = paint.Painter()
    first = painter.frame(rows, cols, len(rows))
    overlay.index = min(overlay.index + 1, len(overlay.filtered()) - 1)
    moved_rows, _ = overlay.compose()
    moved = painter.frame(moved_rows, cols, len(rows))
    idle = painter.frame(moved_rows, cols, len(rows))

    sizes = {
        "tabs": len(snap.get("tabs") or []),
        "panes": len(snap.get("panes") or []),
        "mru_items": len((mru.get("items") or {})),
        "mru_bytes": os.path.getsize(lib.mru_path()) if os.path.exists(lib.mru_path()) else 0,
        "first_frame_bytes": len(first),
        "one_cycle_bytes": len(moved),
        "idle_tick_bytes": len(idle),
    }

    if options.json:
        print(json.dumps({"timings": results, "sizes": sizes}, indent=2))
        return 0

    width = max(len(row["label"]) for row in results)
    for row in results:
        print(
            f"{row['label']:<{width}}  first {row['first_ms']:>7.2f}ms"
            f"   min {row['min_ms']:>7.2f}ms   median {row['median_ms']:>7.2f}ms"
        )
    print()
    for key, value in sizes.items():
        print(f"{key:<20} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
