#!/usr/bin/env python3
"""Shared Hold T helpers. Stdlib only."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

PLUGIN_ID = "followbl.hold-t"
NEW_TAB_ID = "action:new-tab"


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def state_dir() -> str:
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if path:
        return path
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    for candidate in (
        os.path.join(xdg, "herdr", "plugins", PLUGIN_ID),
        os.path.join(xdg, "herdr", PLUGIN_ID),
    ):
        if os.path.isdir(candidate):
            return candidate
    path = os.path.join(xdg, "herdr", "plugins", PLUGIN_ID)
    os.makedirs(path, exist_ok=True)
    return path


def mru_path() -> str:
    return os.path.join(state_dir(), "mru.json")


def lock_path() -> str:
    return os.path.join(state_dir(), "mru.lock")


def sock_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime, "herdr-hold-t.sock")


def now_ms() -> int:
    return int(time.time() * 1000)


@contextmanager
def _mru_lock():
    os.makedirs(state_dir(), exist_ok=True)
    handle = open(lock_path(), "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def herdr(*args: str) -> dict[str, Any]:
    result = subprocess.run(
        [herdr_bin(), *args],
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        err = ((result.stderr or result.stdout or b"").decode("utf-8", "ignore")).strip()
        raise RuntimeError(err or f"herdr {' '.join(args)} failed ({result.returncode})")
    raw = result.stdout or b""
    if not raw.strip():
        return {}
    return json.loads(raw)


def snapshot() -> dict[str, Any]:
    payload = herdr("api", "snapshot")
    return (payload.get("result") or {}).get("snapshot") or payload.get("snapshot") or {}


def _result_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    result = payload.get("result")
    source = result if isinstance(result, dict) else payload
    value = source.get(key) if isinstance(source, dict) else None
    return value if isinstance(value, list) else []


def _read_mru() -> dict[str, Any]:
    path = mru_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": {}}


def _write_mru(data: dict[str, Any]) -> None:
    path = mru_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    encoded = json.dumps(data, separators=(",", ":"))
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
    os.replace(tmp, path)


def load_mru() -> dict[str, Any]:
    with _mru_lock():
        return _read_mru()


def save_mru(data: dict[str, Any]) -> None:
    with _mru_lock():
        _write_mru(data)


def mutate_mru(mutator: Callable[[dict[str, Any]], bool | None]) -> dict[str, Any]:
    """Load, mutate, and write MRU under one exclusive lock. Return False to skip the write."""
    with _mru_lock():
        data = _read_mru()
        if mutator(data) is False:
            return data
        _write_mru(data)
        return data


def touch_item(
    data: dict[str, Any],
    item_id: str,
    *,
    kind: str,
    focused: bool = False,
    activity: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    items = data.setdefault("items", {})
    entry = items.get(item_id) or {"id": item_id, "kind": kind, "last_focused_ms": 0, "last_activity_ms": 0}
    entry["kind"] = kind
    stamp = now_ms()
    if focused:
        entry["last_focused_ms"] = stamp
        entry["last_activity_ms"] = max(int(entry.get("last_activity_ms") or 0), stamp)
    if activity:
        entry["last_activity_ms"] = stamp
    if extra:
        entry.update(extra)
    if not int(entry.get("last_focused_ms") or 0) and not int(entry.get("last_activity_ms") or 0):
        entry["last_activity_ms"] = stamp
    items[item_id] = entry


def drop_item(data: dict[str, Any], item_id: str) -> None:
    data.setdefault("items", {}).pop(item_id, None)
    panes = data.get("panes")
    if isinstance(panes, dict):
        dead = [key for key, value in panes.items() if value == item_id]
        for key in dead:
            panes.pop(key, None)


def remember_live_state(snap: dict[str, Any], mru: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stamp first-seen times and pane→tab map. One write if anything changed."""

    def apply(target: dict[str, Any]) -> bool | None:
        items = target.setdefault("items", {})
        panes = target.setdefault("panes", {})
        now = now_ms()
        dirty = False
        for pane in snap.get("panes") or []:
            pane_id = pane.get("pane_id")
            tab_id = pane.get("tab_id")
            if pane_id and tab_id and panes.get(pane_id) != tab_id:
                panes[pane_id] = tab_id
                dirty = True
        focused_tab = snap.get("focused_tab_id")
        for tab in snap.get("tabs") or []:
            tab_id = tab.get("tab_id")
            if not tab_id:
                continue
            entry = items.get(tab_id)
            if entry is None:
                entry = {"id": tab_id, "kind": "tab", "last_focused_ms": 0, "last_activity_ms": 0}
                items[tab_id] = entry
                dirty = True
            if tab.get("workspace_id") and entry.get("workspace_id") != tab.get("workspace_id"):
                entry["workspace_id"] = tab.get("workspace_id")
                dirty = True
            if tab.get("label") and entry.get("label") != tab.get("label"):
                entry["label"] = tab.get("label")
                dirty = True
            if tab.get("focused") or tab_id == focused_tab:
                if int(entry.get("last_focused_ms") or 0) != now:
                    entry["last_focused_ms"] = now
                    entry["last_activity_ms"] = max(int(entry.get("last_activity_ms") or 0), now)
                    dirty = True
            elif not int(entry.get("last_focused_ms") or 0) and not int(entry.get("last_activity_ms") or 0):
                entry["last_activity_ms"] = now
                dirty = True
        return True if dirty else False

    if mru is not None:
        if apply(mru):
            save_mru(mru)
        return mru
    return mutate_mru(apply)


def age_ms(item: dict[str, Any]) -> int:
    return int(item.get("last_focused_ms") or 0)


def prompt_ms(item: dict[str, Any]) -> int:
    return int(item.get("last_prompt_ms") or 0)


def relative_age(ms: int, *, now: int | None = None) -> str:
    if not ms:
        return "—"
    now = now or now_ms()
    seconds = max(0, (now - ms) // 1000)
    if seconds < 10:
        return "now"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    if days < 14:
        return f"{days}d"
    weeks = days // 7
    return f"{weeks}w"


def fuzzy_score(query: str, *fields: str) -> int | None:
    q = query.strip().lower()
    if not q:
        return 0
    haystack = " ".join(f for f in fields if f).lower()
    if not haystack:
        return None
    if q in haystack:
        return 10_000 - haystack.find(q) + min(len(q) * 20, 400)
    score = 0
    idx = 0
    consecutive = 0
    first = True
    for ch in q:
        found = haystack.find(ch, idx)
        if found < 0:
            return None
        if found == idx:
            consecutive += 1
            score += 12 + consecutive * 4
        else:
            consecutive = 0
            score += 3
        if first or haystack[found - 1] in " /-_.:":
            score += 8
        first = False
        idx = found + 1
    score -= idx
    return score


def tab_sort_key(item: dict[str, Any]) -> tuple:
    return (
        0 if item.get("agent_status") == "done" else 1,
        -age_ms(item),
        -int(item.get("number") or 0),
        str(item.get("space") or ""),
        str(item.get("label") or ""),
    )


def build_items(mru: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Cheap open path: tab list + workspace list + MRU. No snapshot, no disk write."""
    bundle: dict[str, Any] = {}

    def load_tabs() -> None:
        bundle["tabs"] = _result_list(herdr("tab", "list"), "tabs")

    def load_spaces() -> None:
        bundle["workspaces"] = _result_list(herdr("workspace", "list"), "workspaces")

    def load_state() -> None:
        bundle["mru"] = mru if mru is not None else load_mru()

    workers = [threading.Thread(target=fn) for fn in (load_tabs, load_spaces, load_state)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    stored = (bundle.get("mru") or {}).get("items") or {}
    workspaces = {
        ws.get("workspace_id"): ws
        for ws in (bundle.get("workspaces") or [])
        if isinstance(ws, dict) and ws.get("workspace_id")
    }
    now = now_ms()
    items: list[dict[str, Any]] = []
    for tab in bundle.get("tabs") or []:
        if not isinstance(tab, dict):
            continue
        tab_id = tab.get("tab_id")
        if not tab_id:
            continue
        ws_id = tab.get("workspace_id")
        ws = workspaces.get(ws_id) or {}
        hist = stored.get(tab_id) or {}
        current = bool(tab.get("focused"))
        last_focused = int(hist.get("last_focused_ms") or 0)
        if current:
            last_focused = max(last_focused, now)
        label = str(tab.get("label") or tab_id)
        space = str(ws.get("label") or ws_id or "")
        items.append(
            {
                "id": tab_id,
                "kind": "tab",
                "label": label,
                "space": space,
                "workspace_id": ws_id,
                "agent_status": str(tab.get("agent_status") or "unknown"),
                "pane_count": int(tab.get("pane_count") or 0),
                "number": int(tab.get("number") or 0),
                "last_focused_ms": last_focused,
                "last_prompt_ms": int(hist.get("last_prompt_ms") or 0),
                "search": f"{label} {space}",
                "current": current,
            }
        )

    items.sort(key=tab_sort_key)
    return items


def new_tab_item() -> dict[str, Any]:
    return {
        "id": NEW_TAB_ID,
        "kind": "new-tab",
        "label": "New Tab",
        "space": "",
        "workspace_id": None,
        "agent_status": "",
        "pane_count": 0,
        "last_focused_ms": 0,
        "last_prompt_ms": 0,
        "search": "new tab create",
        "current": False,
    }


def filter_items(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    if not query.strip():
        return items
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        score = fuzzy_score(
            query,
            item.get("label") or "",
            item.get("space") or "",
            item.get("agent_status") or "",
            item.get("search") or "",
            item.get("kind") or "",
        )
        if score is None:
            continue
        scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored]


def send_ipc(message: str) -> bool:
    path = sock_path()
    if not os.path.exists(path):
        return False
    sock = None
    try:
        sock = __import__("socket").socket(__import__("socket").AF_UNIX, __import__("socket").SOCK_DGRAM)
        sock.settimeout(0.02)
        sock.sendto(message.encode("utf-8"), path)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            sock.close()
