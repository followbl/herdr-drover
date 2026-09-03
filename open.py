#!/usr/bin/env python3
"""Open the switcher, or cycle it if it is already up."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from lib import herdr_bin, send_ipc


def open_pane() -> int:
    plugin = os.environ.get("HERDR_PLUGIN_ID") or "followbl.hold-t"
    result = subprocess.run(
        [
            herdr_bin(),
            "plugin",
            "pane",
            "open",
            "--plugin",
            plugin,
            "--entrypoint",
            "switcher",
        ],
        check=False,
    )
    return result.returncode or 0


def main() -> int:
    action = (os.environ.get("HERDR_PLUGIN_ACTION_ID") or "open").strip()
    message = "cycle-prev" if action in {"cycle-prev", "prev"} else "cycle"
    if send_ipc(message):
        return 0
    code = open_pane()
    if code == 0:
        return 0
    for _ in range(15):
        time.sleep(0.03)
        if send_ipc(message):
            return 0
    return code


if __name__ == "__main__":
    raise SystemExit(main())
