#!/usr/bin/env python3
"""Popup TUI: MRU tab switcher with Super-release commit via keyd listen."""

from __future__ import annotations

import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import termios
import time
import tty
from typing import Any

from lib import (
    NEW_TAB_ID,
    build_items,
    filter_items,
    fuzzy_score,
    herdr,
    herdr_bin,
    new_tab_item,
    prompt_ms,
    relative_age,
    sock_path,
)
from keys import action_for_char, action_for_event, keymap

ESC = "\x1b"
CSI = ESC + "["
RESET = CSI + "0m"
DIM = CSI + "2m"
BOLD = CSI + "1m"
SHOW = CSI + "?25h"
HIDE = CSI + "?25l"
CLEAR = CSI + "2J" + CSI + "H"
BG_SEL = CSI + "48;5;237m"
FG_SEL = CSI + "38;5;255m"
FG_MUTED = CSI + "38;5;245m"
FG_LABEL = CSI + "38;5;252m"
STATUS_COLOR = {
    "working": CSI + "38;5;178m",
    "blocked": CSI + "38;5;203m",
    "done": CSI + "38;5;75m",
    "idle": CSI + "38;5;244m",
    "unknown": CSI + "38;5;240m",
}
FG_RULE = CSI + "38;5;238m"
KITTY_KEYS = {
    9: "tab",
    13: "enter",
    27: "esc",
    127: "backspace",
    57350: "left",
    57351: "right",
    57352: "up",
    57353: "down",
    57354: "home",
    57355: "end",
    116: "t",
    84: "t",
}

# Full CSI: ESC [ private? params inter? final(0x40-0x7E)
CSI_ANY = re.compile(r"^\x1b\[([?=>])?([\d;]*)([\x20-\x2f]*)([\x40-\x7e])")
SS3 = re.compile(r"^\x1bO([A-Za-z])")
OSC = re.compile(r"^\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)


def pad(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) == width:
        return text
    if len(text) < width:
        return text + " " * (width - len(text))
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


class Switcher:
    def __init__(self) -> None:
        self.all_items = [new_tab_item(), *build_items()]
        self.query = ""
        self.cursor = 0
        self.name = ""
        self.index = 0
        self.scroll = 0
        self.cycles = 0
        self.cmd_active = False
        self.cmd_seen = False
        self.ready = False
        self.ready_deadline = 0.0
        self.pending = ""
        self.dirty = True
        self.listen: subprocess.Popen[bytes] | None = None
        self.listen_buf = b""
        self.server: socket.socket | None = None
        self.old_term: list | None = None
        self.done: dict[str, Any] | None = None
        self.keymap = keymap()
        self._place_initial_highlight()

    def filtered(self) -> list[dict[str, Any]]:
        if self.query:
            items = filter_items(self.all_items, self.query)
            if not any(item["id"] == NEW_TAB_ID for item in items):
                if fuzzy_score(self.query, "new tab create") is not None:
                    items = [new_tab_item(), *items]
            return items
        return self.all_items

    def _place_initial_highlight(self) -> None:
        items = self.filtered()
        for i, item in enumerate(items):
            if item["kind"] != "new-tab":
                self.index = i
                return
        self.index = 0

    def current(self) -> dict[str, Any] | None:
        items = self.filtered()
        if not items:
            return None
        self.index = max(0, min(self.index, len(items) - 1))
        return items[self.index]

    def move(self, delta: int, *, skip_new_tab: bool = False) -> None:
        items = self.filtered()
        if not items:
            return
        n = len(items)
        idx = self.index
        for _ in range(n):
            idx = (idx + delta) % n
            if skip_new_tab and items[idx]["kind"] == "new-tab":
                continue
            self.index = idx
            self.dirty = True
            return

    def on_new_tab(self) -> bool:
        item = self.current()
        return bool(item and item["kind"] == "new-tab")

    def cycle(self, delta: int = 1) -> None:
        if not self.ready:
            return
        self.cycles += 1
        self.move(delta, skip_new_tab=True)

    def setup_terminal(self) -> None:
        self.old_term = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        sys.stdout.write(CLEAR + SHOW)
        sys.stdout.flush()

    def restore_terminal(self) -> None:
        if self.old_term is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_term)
        sys.stdout.write(SHOW + RESET)
        sys.stdout.flush()

    def setup_ipc(self) -> None:
        path = sock_path()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(path)
        sock.setblocking(False)
        self.server = sock

    def setup_keyd(self) -> None:
        try:
            self.listen = subprocess.Popen(
                ["keyd", "listen"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            self.listen = None
            self.ready = True
            return
        fd = self.listen.stdout.fileno() if self.listen.stdout else None
        if fd is None:
            self.ready = True
            return
        ready, _, _ = select.select([fd], [], [], 0)
        if ready:
            self.handle_keyd()

    def cleanup(self) -> None:
        if self.listen and self.listen.poll() is None:
            self.listen.terminate()
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            try:
                os.unlink(sock_path())
            except FileNotFoundError:
                pass
        self.restore_terminal()

    def cols_rows(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((80, 24))
        return max(48, size.columns), max(8, size.lines)

    def render(self) -> None:
        cols, rows = self.cols_rows()
        items = self.filtered()
        if items:
            self.index = max(0, min(self.index, len(items) - 1))
        header = 2
        footer = 1
        new_item = next((item for item in items if item["kind"] == "new-tab"), None)
        tabs = [item for item in items if item["kind"] == "tab"]
        pinned = 2 if new_item else 0
        body = max(1, rows - header - footer - pinned)

        selected = items[self.index] if items else None
        tab_index = next((i for i, tab in enumerate(tabs) if selected is not None and tab["id"] == selected["id"]), 0)
        if selected and selected["kind"] == "tab":
            if tab_index < self.scroll:
                self.scroll = tab_index
            if tab_index >= self.scroll + body:
                self.scroll = max(0, tab_index - body + 1)
        self.scroll = max(0, min(self.scroll, max(0, len(tabs) - body)))

        parts: list[str] = [CSI + "H" + HIDE]
        parts.append(CSI + "1;1H" + CSI + "2K" + self.render_search(cols))
        parts.append(CSI + "2;1H" + CSI + "2K" + FG_MUTED + "  " + "─" * max(8, cols - 4) + RESET)

        row = header + 1
        if new_item:
            parts.append(CSI + f"{row};1H" + CSI + "2K" + self.render_row(new_item, selected is new_item, cols))
            row += 1
            parts.append(CSI + f"{row};1H" + CSI + "2K" + FG_RULE + "  " + "─" * max(8, cols - 4) + RESET)
            row += 1
        visible = tabs[self.scroll : self.scroll + body]
        for offset, tab in enumerate(visible):
            parts.append(CSI + f"{row};1H" + CSI + "2K" + self.render_row(tab, selected is tab, cols))
            row += 1
            if row > rows - footer:
                break
        if not visible and not new_item:
            parts.append(CSI + f"{row};1H" + CSI + "2K" + f"  {FG_MUTED}no matches{RESET}")
            row += 1
        parts.append(CSI + f"{row};1H" + CSI + "J")
        parts.append(CSI + f"{rows};1H" + CSI + "2K" + f"{FG_MUTED}  {len(tabs)}{RESET}")
        parts.append(self.search_cursor_seq(cols))
        sys.stdout.write("".join(parts))
        sys.stdout.flush()

    def render_search(self, cols: int) -> str:
        prefix = "  /  "
        text = self.query
        placeholder = "search tabs"
        if text:
            field = text
            style = ""
        else:
            field = placeholder
            style = DIM
        inner = pad(field, max(8, cols - len(prefix) - 1))
        return f"{FG_MUTED}{prefix}{RESET}{style}{inner}{RESET}"

    def search_cursor_seq(self, cols: int) -> str:
        if self.on_new_tab():
            prefix_len = 6  # " ▸ +  "
            pos = len(self.name)
            col = min(cols, prefix_len + pos + 1)
            return CSI + f"3;{col}H" + SHOW
        prefix_len = 5  # "  /  "
        pos = self.cursor
        col = min(cols, prefix_len + pos + 1)
        return CSI + f"1;{col}H" + SHOW

    def render_row(self, item: dict[str, Any], selected: bool, cols: int) -> str:
        mark = "▸" if selected else " "
        status_w, age_w, space_w = 7, 4, 16
        gutter = 2 + 1 + 2 + 2 + 2
        name_w = max(12, cols - gutter - space_w - status_w - age_w)
        if item["kind"] == "new-tab":
            if selected:
                label = self.name
            else:
                label = self.name or "New Tab"
            plain = f" {mark} +  {label}"
            if selected:
                return BG_SEL + FG_SEL + pad(plain, cols) + RESET
            return f" {mark} {FG_MUTED}+  {label}{RESET}"
        space = pad(str(item.get("space") or ""), space_w)
        name = pad(str(item.get("label") or ""), name_w)
        status_text = str(item.get("agent_status") or "")
        if status_text == "unknown":
            status_text = ""
        status = pad(status_text, status_w)
        age = pad(relative_age(prompt_ms(item)), age_w)
        status_sgr = STATUS_COLOR.get(item.get("agent_status") or "", FG_MUTED)
        if selected:
            return BG_SEL + FG_SEL + pad(f" {mark} {space}  {name}  {status}  {age}", cols) + RESET
        return (
            f" {mark} {FG_MUTED}{space}{RESET}  "
            f"{FG_LABEL}{name}{RESET}  "
            f"{status_sgr}{status}{RESET}  "
            f"{FG_MUTED}{age}{RESET}"
        )

    def insert_text(self, text: str) -> None:
        if self.on_new_tab():
            self.name += text
            self.dirty = True
            return
        self.query = self.query[: self.cursor] + text + self.query[self.cursor :]
        self.cursor += len(text)
        self.dirty = True
        self._place_initial_highlight()

    def delete_back(self) -> None:
        if self.on_new_tab():
            if self.name:
                self.name = self.name[:-1]
                self.dirty = True
            return
        if self.cursor <= 0:
            return
        self.query = self.query[: self.cursor - 1] + self.query[self.cursor :]
        self.cursor -= 1
        self.dirty = True
        self._place_initial_highlight()

    def move_cursor(self, delta: int) -> None:
        if self.on_new_tab():
            return
        nxt = max(0, min(len(self.query), self.cursor + delta))
        if nxt != self.cursor:
            self.cursor = nxt
            self.dirty = True

    def handle_action(self, action: str) -> None:
        if action in {"", "noop", "pending"}:
            return
        if action in {"dismiss", "force_quit", "esc"}:
            self.done = {"op": "cancel"}
            return
        if action in {"select", "enter"}:
            self.confirm()
            return
        if action in {"move_up", "up"}:
            self.move(-1)
            return
        if action in {"move_down", "down"}:
            self.move(1)
            return
        if action in {"move_left", "left"}:
            self.move_cursor(-1)
            return
        if action in {"move_right", "right"}:
            self.move_cursor(1)
            return
        if action == "home":
            if not self.on_new_tab() and self.cursor != 0:
                self.cursor = 0
                self.dirty = True
            return
        if action == "end":
            end = len(self.query)
            if not self.on_new_tab() and self.cursor != end:
                self.cursor = end
                self.dirty = True
            return
        if action == "backspace":
            self.delete_back()
            return
        if action == "cycle":
            self.cycle(1)
            return
        if action == "cycle_prev":
            self.cycle(-1)
            return
        if action in {"next", "tab"}:
            self.move(1)
            return
        if action in {"previous", "shift+tab"}:
            self.move(-1)

    def handle_keys(self, data: str) -> None:
        self.pending += data
        while self.pending:
            first = self.pending[0]
            if first == ESC:
                event, used = parse_escape(self.pending)
                if event == "pending":
                    if len(self.pending) > 64:
                        self.pending = self.pending[1:]
                        continue
                    return
                if used <= 0:
                    self.pending = self.pending[1:]
                    continue
                self.pending = self.pending[used:]
                action = action_for_event(event, self.keymap)
                if action:
                    self.handle_action(action)
                continue
            ch = self.pending[0]
            self.pending = self.pending[1:]
            action = action_for_char(ch, self.keymap)
            if action:
                self.handle_action(action)
            elif ch.isprintable():
                self.insert_text(ch)

    def handle_ipc(self) -> None:
        if self.server is None:
            return
        try:
            payload, _ = self.server.recvfrom(256)
        except (BlockingIOError, OSError):
            return
        msg = payload.decode("utf-8", "ignore").strip()
        if msg == "cycle":
            self.cycle(1)
        elif msg == "cycle-prev":
            self.cycle(-1)
        elif msg == "commit":
            if self.ready and self.cycles > 0 and not self.on_new_tab():
                self.confirm()
        elif msg == "cancel":
            self.done = {"op": "cancel"}

    def handle_keyd(self) -> None:
        if not self.listen or not self.listen.stdout:
            return
        fd = self.listen.stdout.fileno()
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return
        if not chunk:
            return
        self.listen_buf += chunk
        while True:
            nl = self.listen_buf.find(b"\n")
            if nl < 0:
                break
            line = self.listen_buf[:nl].decode("utf-8", "ignore")
            self.listen_buf = self.listen_buf[nl + 1 :]
            self._keyd_line(line)

    def _keyd_line(self, line: str) -> None:
        state = cmd_layer_state(line)
        if state is None:
            return
        was = self.cmd_active
        self.cmd_seen = True
        self.cmd_active = state
        if was and not self.cmd_active:
            if not self.ready:
                self.ready = True
                return
            if self.cycles > 0 and not self.on_new_tab():
                self.confirm()

    def confirm(self) -> None:
        item = self.current()
        if item is None:
            self.done = {"op": "cancel"}
            return
        if item["kind"] == "new-tab":
            self.done = {"op": "new-tab", "label": self.name.strip()}
            return
        self.done = {"op": "focus", "item": item}

    def loop(self) -> dict[str, Any]:
        self.setup_terminal()
        self.setup_ipc()
        self.render()
        self.dirty = False
        self.setup_keyd()
        self.ready_deadline = time.monotonic() + 0.05
        last_age = time.monotonic()
        try:
            while self.done is None:
                now = time.monotonic()
                if not self.ready and not self.cmd_seen and now >= self.ready_deadline:
                    self.ready = True
                fds: list[Any] = [sys.stdin]
                if self.server:
                    fds.append(self.server)
                if self.listen and self.listen.stdout and self.listen.poll() is None:
                    fds.append(self.listen.stdout)
                if self.pending:
                    timeout = 0.05
                elif not self.ready:
                    timeout = max(0.0, self.ready_deadline - now)
                else:
                    timeout = 1.0
                ready, _, _ = select.select(fds, [], [], timeout)
                if not ready and self.pending.startswith(ESC):
                    if self.pending == ESC:
                        self.pending = ""
                        self.handle_action("esc")
                    else:
                        self.pending = self.pending[1:]
                for fd in ready:
                    if fd is sys.stdin:
                        self.handle_keys(os.read(sys.stdin.fileno(), 128).decode("utf-8", "ignore"))
                    elif fd is self.server:
                        self.handle_ipc()
                    else:
                        self.handle_keyd()
                now = time.monotonic()
                if now - last_age >= 1.0:
                    self.dirty = True
                    last_age = now
                if self.dirty:
                    self.render()
                    self.dirty = False
        finally:
            self.cleanup()
        return self.done or {"op": "cancel"}


def cmd_layer_state(line: str) -> bool | None:
    """True if cmd became active, False if it left, None if the line is unrelated."""
    text = line.strip()
    if not text:
        return None
    if text.startswith(("+", "-")):
        layers = [part for part in text[1:].split("/") if part]
        if "cmd" not in layers:
            return None
        return text.startswith("+")
    layers = [part for part in text.strip("/").split("/") if part]
    if "cmd" not in layers:
        return None
    return True


def parse_escape(data: str) -> tuple[str, int]:
    if not data.startswith(ESC):
        return "noop", 1
    if len(data) == 1:
        return "pending", 0

    osc = OSC.match(data)
    if osc:
        return "noop", osc.end()
    if data.startswith(ESC + "]"):
        return "pending", 0

    ss3 = SS3.match(data)
    if ss3:
        arrows = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
        return arrows.get(ss3.group(1), "noop"), ss3.end()
    if data.startswith(ESC + "O") and len(data) == 2:
        return "pending", 0

    # Mouse: ESC [ < ... M/m
    if data.startswith(CSI + "<"):
        end = re.search(r"[Mm]", data[3:])
        if not end:
            return "pending", 0
        return "noop", 3 + end.end()

    match = CSI_ANY.match(data)
    if not match:
        if data.startswith(CSI):
            return "pending", 0
        return "esc", 1

    _priv, params, _inter, final = match.groups()
    used = match.end()
    parts = [p for p in (params or "").split(";") if p != ""]
    nums = []
    for part in parts:
        try:
            nums.append(int(part))
        except ValueError:
            continue
    event = nums[2] if len(nums) >= 3 else 1
    mods = nums[1] if len(nums) >= 2 else 1
    if event == 3:
        return "noop", used

    ctrl = bool((mods - 1) & 4) if mods else False
    shift = bool((mods - 1) & 1) if mods else False

    if final == "u":
        keycode = nums[0] if nums else 0
        name = KITTY_KEYS.get(keycode)
        if name == "t" and ctrl:
            return "ctrl+shift+t" if shift else "ctrl+t", used
        if name == "tab" and shift:
            return "ctrl+shift+tab" if ctrl else "shift+tab", used
        if name:
            return name, used
        return "noop", used

    arrows = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
    if final in arrows:
        return arrows[final], used
    if final == "Z":
        return "shift+tab", used
    if final == "~" and nums:
        mapping = {1: "home", 3: "delete", 4: "end", 7: "home", 8: "end"}
        return mapping.get(nums[0], "noop"), used
    return "noop", used


def apply(result: dict[str, Any]) -> int:
    op = result.get("op")
    if op == "cancel":
        return 0

    def later(args: list[str]) -> None:
        quoted = " ".join(shlex.quote(part) for part in [herdr_bin(), *args])
        subprocess.Popen(
            ["bash", "-lc", f"sleep 0.07; {quoted}"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if op == "new-tab":
        args = ["tab", "create", "--focus"]
        label = (result.get("label") or "").strip()
        if label:
            args.extend(["--label", label])
        try:
            herdr(*args)
        except RuntimeError:
            later(args)
        return 0
    if op == "focus":
        item = result.get("item") or {}
        tab_id = item.get("id")
        if not tab_id:
            return 0
        workspace_id = item.get("workspace_id")
        if workspace_id:
            try:
                herdr("workspace", "focus", workspace_id)
            except RuntimeError:
                pass
        try:
            herdr("tab", "focus", tab_id)
        except RuntimeError:
            later(["tab", "focus", tab_id])
        return 0
    return 0


def main() -> int:
    def _die(signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _die)
    signal.signal(signal.SIGINT, _die)
    switcher = Switcher()
    result = switcher.loop()
    return apply(result)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            sys.stdout.write(SHOW + RESET)
            sys.stdout.flush()
        except OSError:
            pass
        sys.stderr.write(f"drover: {exc}\n")
        raise SystemExit(1)
