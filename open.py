#!/usr/bin/env python3
"""Open the switcher, or cycle the one already up.

Deliberately standalone: this runs on every press of the chord, so it imports
nothing but what a datagram and a lock need. Importing the plugin's lib module
pulls in json, subprocess and threading and roughly doubles the time from key
to popup.
"""

from __future__ import annotations

import fcntl
import os
import socket
import subprocess
import time

PLUGIN_ID = "followbl.drover"


def sock_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime, "herdr-drover.sock")


def state_dir() -> str:
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if path:
        return path
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    for candidate in (
        os.path.join(xdg, "herdr", "plugins", PLUGIN_ID),
        os.path.join(xdg, "herdr", PLUGIN_ID),
    ):
        if os.path.isdir(candidate):
            return candidate
    path = os.path.join(xdg, "herdr", "plugins", PLUGIN_ID)
    os.makedirs(path, exist_ok=True)
    return path


def open_lock_path() -> str:
    return os.path.join(state_dir(), "open.lock")


def send_ipc(message: str) -> bool:
    path = sock_path()
    if not os.path.exists(path):
        return False
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.settimeout(0.02)
        sock.sendto(message.encode("utf-8"), path)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            sock.close()


def herdr_bin() -> str:
    candidate = os.environ.get("HERDR_BIN_PATH")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return "herdr"


def open_pane() -> int:
    plugin = os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID
    result = subprocess.run(
        [herdr_bin(), "plugin", "pane", "open", "--plugin", plugin, "--entrypoint", "switcher"],
        check=False,
    )
    return result.returncode or 0


def wait_for_ipc(message: str, attempts: int = 40, delay: float = 0.03) -> bool:
    for _ in range(attempts):
        time.sleep(delay)
        if send_ipc(message):
            return True
    return False


def main() -> int:
    action = (os.environ.get("HERDR_PLUGIN_ACTION_ID") or "open").strip()
    message = "cycle-prev" if action in {"cycle-prev", "prev"} else "cycle"
    if send_ipc(message):
        return 0

    # No socket yet. Either the overlay is not up, or a previous tap is still
    # spawning it. Only the lock holder opens a pane; everyone else waits for
    # that overlay and cycles it, so a fast double tap cannot stack popups.
    handle = open(open_lock_path(), "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            wait_for_ipc(message)
            return 0
        code = open_pane()
        if code != 0:
            return 0 if wait_for_ipc(message, attempts=15) else code
        # Hold the lock until the overlay is listening, so the next tap queues
        # behind us instead of opening a second one.
        for _ in range(40):
            if os.path.exists(sock_path()):
                break
            time.sleep(0.03)
        return 0
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
