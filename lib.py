#!/usr/bin/env python3
"""Shared Drover helpers. Stdlib only."""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

PLUGIN_ID = "followbl.drover"
NEW_TAB_ID = "action:new-tab"


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


def open_lock_path() -> str:
    return os.path.join(state_dir(), "open.lock")


def sock_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime, "herdr-drover.sock")


def plugin_root() -> str:
    return os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(os.path.abspath(__file__))


def config_dir() -> str:
    path = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if path:
        return path
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "herdr", "plugins", "config", PLUGIN_ID)


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


def snapshot() -> dict[str, Any]:
    """The whole session in one round trip. See api.py for why not the CLI."""
    import api

    return api.client().snapshot()


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


def lead_pane(panes: list[dict[str, Any]], focused_pane_id: str | None) -> dict[str, Any]:
    """The pane a tab should be described by: the focused one, else an agent's."""
    if not panes:
        return {}
    for pane in panes:
        if pane.get("pane_id") and pane.get("pane_id") == focused_pane_id:
            return pane
    for pane in panes:
        if pane.get("agent"):
            return pane
    return panes[0]


def number_of(tab: dict[str, Any]) -> int:
    try:
        return int(tab.get("number") or 0)
    except (TypeError, ValueError):
        return 0


def is_default_label(label: str) -> bool:
    """True when Herdr is showing a position, not a name someone chose.

    An unnamed tab is labelled with its position, and positions shift as other
    tabs close -- so the number on the tab need not be its current one. Any
    all-digits label counts as unnamed; a tab deliberately named "7" is
    indistinguishable from one Herdr numbered 7, and this is the reading that
    helps more often.
    """
    text = (label or "").strip()
    return not text or text.isdigit()


def _squash(text: str) -> str:
    """Comparable core of a title: ASCII letters and digits only.

    Agents decorate their titles -- `π - Work`, `✳ Twilio` -- and the decoration
    is not part of what the title says.
    """
    return "".join(char for char in text.lower() if char.isascii() and char.isalnum())


def display_title(title: str, agent: str, cwd: str) -> str:
    """A name for an unnamed tab: the agent's task title, else its directory.

    Agents put several things in a terminal title. Codex appends ` | folder`.
    Before a task has a title, several of them show only their own name, the
    folder, or both -- `π - Work` -- and a row that says the agent twice says
    nothing, so those fall back to the directory.
    """
    text = (title or "").strip()
    if " | " in text:
        text = text.split(" | ", 1)[0].strip()
    base = _basename(cwd)
    noise = {"", _squash(agent), _squash(base), _squash(agent) + _squash(base)}
    if _squash(text) in noise:
        return base
    return text


def _basename(path: str) -> str:
    trimmed = (path or "").rstrip("/")
    return trimmed.rsplit("/", 1)[-1] if trimmed else ""


def build_items(mru: dict[str, Any] | None = None, snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One snapshot plus the MRU file: no subprocesses, no second round trip.

    Every row carries what the snapshot already knows about its tab -- agent
    kind, status, working directory, the agent's own task title -- because the
    fuzzy field matches on all of it and the row draws from it.
    """
    if snap is None:
        snap = snapshot()
    stored = ((mru if mru is not None else load_mru()).get("items")) or {}

    workspaces = {
        ws.get("workspace_id"): ws
        for ws in (snap.get("workspaces") or [])
        if isinstance(ws, dict) and ws.get("workspace_id")
    }
    panes_by_tab: dict[str, list[dict[str, Any]]] = {}
    for pane in snap.get("panes") or []:
        if isinstance(pane, dict) and pane.get("tab_id"):
            panes_by_tab.setdefault(str(pane["tab_id"]), []).append(pane)

    focused_tab = snap.get("focused_tab_id")
    focused_pane = snap.get("focused_pane_id")
    now = now_ms()
    items: list[dict[str, Any]] = []
    for tab in snap.get("tabs") or []:
        if not isinstance(tab, dict):
            continue
        tab_id = tab.get("tab_id")
        if not tab_id:
            continue
        ws = workspaces.get(tab.get("workspace_id")) or {}
        hist = stored.get(tab_id) or {}
        panes = panes_by_tab.get(str(tab_id)) or []
        pane = lead_pane(panes, focused_pane)
        current = bool(tab.get("focused")) or tab_id == focused_tab
        last_focused = int(hist.get("last_focused_ms") or 0)
        if current:
            last_focused = max(last_focused, now)
        agent_name = str(pane.get("agent") or "")
        cwd_path = str(pane.get("foreground_cwd") or pane.get("cwd") or "")
        title = str(pane.get("terminal_title_stripped") or "").strip()
        label = str(tab.get("label") or "").strip()
        number = number_of(tab)
        if is_default_label(label):
            # Display only: renaming tabs is another plugin's job.
            label = display_title(title, agent_name, cwd_path) or label or f"tab {number}"
        space = str(ws.get("label") or tab.get("workspace_id") or "")
        agent = agent_name
        cwd = cwd_path
        items.append(
            {
                "id": tab_id,
                "kind": "tab",
                "label": label,
                "space": space,
                "workspace_id": tab.get("workspace_id"),
                "pane_id": pane.get("pane_id") or "",
                "agent": agent,
                "cwd": cwd,
                "title": title,
                "agent_status": str(tab.get("agent_status") or "unknown"),
                "pane_count": int(tab.get("pane_count") or len(panes)),
                "number": number,
                "last_focused_ms": last_focused,
                "last_prompt_ms": int(hist.get("last_prompt_ms") or 0),
                "search": " ".join(
                    part for part in (label, title, space, agent, _basename(cwd), f"#{number}") if part
                ),
                "current": current,
            }
        )

    items.sort(key=tab_sort_key)
    return items


def prune_mru(snap: dict[str, Any], mru: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drop what the session no longer has.

    The MRU file is written on every tab focus and every agent status change,
    and read in full on every open. Left alone it keeps an entry per tab ever
    seen -- 2787 of them against 47 live tabs here, half a megabyte of JSON
    parsed and rewritten per event. Tabs the snapshot does not list are gone
    for good, so they go.
    """
    live_tabs = {str(tab.get("tab_id")) for tab in (snap.get("tabs") or []) if isinstance(tab, dict)}
    live_panes = {str(pane.get("pane_id")) for pane in (snap.get("panes") or []) if isinstance(pane, dict)}
    if not live_tabs:
        # An empty or failed snapshot is not evidence that the session is empty.
        return mru if mru is not None else load_mru()

    def apply(target: dict[str, Any]) -> bool | None:
        items = target.setdefault("items", {})
        panes = target.setdefault("panes", {})
        dead_items = [key for key in items if key not in live_tabs]
        dead_panes = [key for key, tab in panes.items() if key not in live_panes or str(tab) not in live_tabs]
        for key in dead_items:
            items.pop(key, None)
        for key in dead_panes:
            panes.pop(key, None)
        return True if (dead_items or dead_panes) else False

    if mru is not None:
        if apply(mru):
            save_mru(mru)
        return mru
    return mutate_mru(apply)


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


# --- temporary debug tracing (remove once the cycle bug is fixed) ---
def dlog(*parts: Any) -> None:
    """Append a line to the debug log when the flag file exists."""
    try:
        flag = os.path.join(state_dir(), "debug.on")
        if not os.path.exists(flag):
            return
        line = " ".join(str(p) for p in parts)
        with open(os.path.join(state_dir(), "debug.log"), "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} pid={os.getpid()} {line}\n")
    except OSError:
        pass
