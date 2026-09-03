#!/usr/bin/env python3
"""Overlay keybindings. Defaults ship in the plugin; users override in config dir."""

from __future__ import annotations

import os
import re

from lib import config_dir, plugin_root

DEFAULT_KEYBINDINGS: dict[str, list[str]] = {
    "select": ["Enter"],
    "dismiss": ["Esc"],
    "force_quit": ["C-c"],
    "move_up": ["Up", "C-p"],
    "move_down": ["Down", "C-n"],
    "move_left": ["Left"],
    "move_right": ["Right"],
    "home": ["Home", "C-a"],
    "end": ["End", "C-e"],
    "backspace": ["Backspace"],
    "cycle": ["C-t", "C-S-t"],
    "cycle_prev": ["C-S-Tab"],
    "next": ["Tab"],
    "previous": ["S-Tab"],
}

EVENT_TO_TOKEN = {
    "enter": "Enter",
    "esc": "Esc",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "backspace": "Backspace",
    "delete": "Delete",
    "tab": "Tab",
    "shift+tab": "S-Tab",
    "ctrl+t": "C-t",
    "ctrl+shift+t": "C-S-t",
    "ctrl+shift+tab": "C-S-Tab",
}

CHAR_TO_TOKEN = {
    "\r": "Enter",
    "\n": "Enter",
    "\x03": "C-c",
    "\x7f": "Backspace",
    "\x08": "Backspace",
    "\t": "Tab",
    "\x0e": "C-n",
    "\x10": "C-p",
    "\x14": "C-t",
    "\x01": "C-a",
    "\x05": "C-e",
}

_SPECIALS = {
    "enter": "Enter",
    "return": "Enter",
    "ret": "Enter",
    "esc": "Esc",
    "escape": "Esc",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "tab": "Tab",
    "backspace": "Backspace",
    "delete": "Delete",
    "backtab": "S-Tab",
    "space": "Space",
}


def normalize_key(key: str) -> str:
    text = str(key or "").strip()
    if not text:
        return ""
    special = _SPECIALS.get(text.lower())
    if special:
        return special
    pieces = [part for part in re.split(r"[-+]", text) if part]
    mods: list[str] = []
    rest: list[str] = []
    for index, piece in enumerate(pieces):
        low = piece.lower()
        last = index == len(pieces) - 1
        if not last and low in {"c", "ctrl", "control"}:
            mods.append("C")
        elif not last and low in {"s", "shift"}:
            mods.append("S")
        elif not last and low in {"m", "a", "alt", "meta"}:
            mods.append("M")
        else:
            rest.append(piece)
    if mods:
        tail = rest[-1] if rest else ""
        if tail.lower() == "tab":
            tail = "Tab"
        elif len(tail) == 1:
            tail = tail.lower()
        elif tail:
            tail = tail[0].upper() + tail[1:]
        ordered = [mod for mod in ("C", "S", "M") if mod in mods]
        return "-".join([*ordered, tail] if tail else ordered)
    return text


def _parse_string_list(raw: str) -> list[str]:
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    values = []
    for piece in inner.split(","):
        item = piece.strip().strip('"').strip("'")
        if item:
            values.append(item)
    return values


def _keybindings_from_toml_text(text: str) -> dict[str, list[str]]:
    try:
        import tomllib
    except ImportError:
        tomllib = None  # type: ignore[assignment]
    if tomllib is not None:
        data = tomllib.loads(text)
        section = data.get("keybindings")
        if not isinstance(section, dict):
            return {}
        parsed: dict[str, list[str]] = {}
        for action, keys in section.items():
            if isinstance(keys, str):
                parsed[str(action)] = [keys]
            elif isinstance(keys, list):
                parsed[str(action)] = [str(key) for key in keys if str(key).strip()]
        return parsed
    parsed: dict[str, list[str]] = {}
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == "[keybindings]"
            continue
        if not in_section or "=" not in stripped:
            continue
        action, _, rest = stripped.partition("=")
        parsed[action.strip()] = _parse_string_list(rest)
    return parsed


def _load_file(path: str) -> dict[str, list[str]]:
    try:
        with open(path, encoding="utf-8") as handle:
            return _keybindings_from_toml_text(handle.read())
    except (OSError, ValueError):
        return {}


def load_keybindings() -> dict[str, list[str]]:
    bindings = {action: list(keys) for action, keys in DEFAULT_KEYBINDINGS.items()}
    shipped = os.path.join(plugin_root(), "herdr-plugin.toml")
    override = os.path.join(config_dir(), "keybindings.toml")
    for path in (shipped, override):
        extra = _load_file(path)
        for action, keys in extra.items():
            if action in DEFAULT_KEYBINDINGS and keys:
                bindings[action] = keys
    return bindings


def keymap(bindings: dict[str, list[str]] | None = None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for action, keys in (bindings or load_keybindings()).items():
        if action not in DEFAULT_KEYBINDINGS:
            continue
        for key in keys:
            token = normalize_key(key)
            if token:
                mapping[token] = action
    return mapping


def action_for_event(event: str, mapping: dict[str, str] | None = None) -> str:
    token = EVENT_TO_TOKEN.get(event)
    if not token:
        return ""
    return (mapping or keymap()).get(token, "")


def action_for_char(char: str, mapping: dict[str, str] | None = None) -> str:
    token = CHAR_TO_TOKEN.get(char)
    if not token:
        return ""
    return (mapping or keymap()).get(token, "")
