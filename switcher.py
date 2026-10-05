#!/usr/bin/env python3
"""Popup TUI: MRU tab switcher with Super-release commit via keyd listen."""

from __future__ import annotations

import os
import re
import select
import signal
import socket
import sys
import termios
import time
import tty
from typing import Any

import api
import icons
import text as textutil
from paint import Painter
from lib import (
    NEW_TAB_ID,
    age_ms,
    build_items,
    dlog,
    filter_items,
    fuzzy_score,
    new_tab_item,
    prompt_ms,
    prune_mru,
    relative_age,
    sock_path,
    tab_sort_key,
)
from keys import action_for_char, action_for_event, herdr_chord, keymap

ESC = "\x1b"
CSI = ESC + "["
RESET = CSI + "0m"
DIM = CSI + "2m"
BOLD = CSI + "1m"
SHOW = CSI + "?25h"
HIDE = CSI + "?25l"
CLEAR = CSI + "2J" + CSI + "H"
ALT_ON = CSI + "?1049h"
ALT_OFF = CSI + "?1049l"
WRAP_OFF = CSI + "?7l"
WRAP_ON = CSI + "?7h"
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
PLAIN = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC
    r"|\x1b[@-Z\\-_]"  # two-character escapes
    r"|[\x00-\x08\x0b-\x1f\x7f]"  # stray control bytes
)
CSI_ANY = re.compile(r"^\x1b\[([?=>])?([\d;]*)([\x20-\x2f]*)([\x40-\x7e])")
SS3 = re.compile(r"^\x1bO([A-Za-z])")
OSC = re.compile(r"^\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)


def spawn_keyd() -> Any:
    """Start `keyd listen`, or None when keyd is not installed.

    The import lives here, not at the top: this runs after the first frame is
    already on screen, and `subprocess` is ~4ms of it.
    """
    import subprocess

    try:
        return subprocess.Popen(
            ["keyd", "listen"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError:
        return None


def spawn_detached(args: list[str]) -> None:
    """Run a herdr command that has to outlive this process."""
    import shlex
    import subprocess

    quoted = " ".join(shlex.quote(part) for part in [api.herdr_bin(), *args])
    subprocess.Popen(
        ["bash", "-lc", f"sleep 0.07; {quoted}"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def terminal_size() -> tuple[int, int]:
    """Columns and rows of this popup, without importing shutil for it."""
    for stream in (sys.stdout, sys.stdin):
        try:
            size = os.get_terminal_size(stream.fileno())
        except (OSError, ValueError, AttributeError):
            continue
        if size.columns and size.lines:
            return size.columns, size.lines
    return 80, 24


def pad(text: str, width: int) -> str:
    """Exactly `width` terminal columns. See text.py: titles carry emoji."""
    return textutil.pad(text, width)


class Switcher:
    REFRESH_SECONDS = 2.0
    PREVIEW_MIN_COLS = 104
    PREVIEW_TTL = 1.5

    def __init__(self, snap: dict[str, Any] | None = None) -> None:
        self.snap: dict[str, Any] = snap or {}
        self.all_items = [new_tab_item(), *build_items(snap=snap)]
        self.query = ""
        self.cursor = 0
        self.name = ""
        self.index = 0
        self.scroll = 0
        self.cycles = 0
        self.queued: list[int] = []
        self.last_cycle_at = 0.0
        self.last_cycle_source = ""
        self.chords = herdr_chord()
        self.chord_leads = {chord[0] for chord in self.chords}
        self.sock_ino: int | None = None
        self.painter = Painter()
        self.refreshed_at = time.monotonic()
        self.pruned = False
        self.note = ""
        self.preview_on = os.environ.get("DROVER_PREVIEW", "") != "off"
        self.preview_text: dict[str, tuple[float, list[str], int]] = {}
        self.preview_want = ""
        self.preview_rows = 0
        self.preview_cols = 0
        self.icons_on = icons.font_available()
        self.cmd_active = False
        self.cmd_seen = False
        self.ready = False
        self.ready_deadline = 0.0
        self.pending = ""
        self.dirty = True
        self.listen: Any = None  # a `keyd listen` child, when keyd is there
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

    def cycle(self, delta: int = 1, *, source: str = "ipc") -> None:
        dlog("cycle delta=", delta, "source=", source, "ready=", self.ready, "cycles=", self.cycles)
        now = time.monotonic()
        if source != self.last_cycle_source and now - self.last_cycle_at < 0.15:
            # One physical tap can arrive twice: as the plugin action over IPC and
            # as the forwarded prefix chord on stdin. Count it once.
            dlog("cycle deduped against", self.last_cycle_source)
            return
        self.last_cycle_at = now
        self.last_cycle_source = source
        if not self.ready:
            self.queued.append(delta)
            return
        self.cycles += 1
        self.move(delta, skip_new_tab=True)

    def refresh(self) -> None:
        """Pick up status, title and label changes without moving any row.

        Reordering under the highlight would make a hold-and-tap land somewhere
        the eye never chose, so a refresh updates rows in place, appends tabs
        that appeared, and drops the ones that are gone.
        """
        try:
            snap = api.client().snapshot()
            fresh = build_items(snap=snap)
        except (api.ApiError, OSError, ValueError) as exc:
            dlog("refresh failed", repr(exc))
            return
        self.snap = snap
        by_id = {item["id"]: item for item in fresh}
        kept: list[dict[str, Any]] = []
        for item in self.all_items:
            if item["kind"] != "tab":
                kept.append(item)
                continue
            update = by_id.pop(item["id"], None)
            if update is None:
                continue  # closed while we were open
            update["last_focused_ms"] = max(
                int(update.get("last_focused_ms") or 0), int(item.get("last_focused_ms") or 0)
            )
            kept.append(update)
        kept.extend(sorted(by_id.values(), key=tab_sort_key))
        selected = self.current()
        self.all_items = kept
        if selected is not None:
            for position, item in enumerate(self.filtered()):
                if item["id"] == selected["id"]:
                    self.index = position
                    break
        self.dirty = True

    def prune(self) -> None:
        """Trim dead tabs out of the MRU file, once per overlay."""
        self.pruned = True
        snap = self.snap
        try:
            if not snap:
                snap = api.client().snapshot()
                self.snap = snap
            prune_mru(snap)
        except (api.ApiError, OSError, ValueError) as exc:
            dlog("prune failed", repr(exc))

    def arm(self) -> None:
        """Super state is known: start honoring cycles, including any taps that beat us here."""
        if self.ready:
            return
        self.ready = True
        queued, self.queued = self.queued, []
        for delta in queued:
            self.cycles += 1
            self.move(delta, skip_new_tab=True)
        dlog("armed; replayed", len(queued), "queued cycles")

    def setup_terminal(self) -> None:
        self.old_term = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        # The alternate screen keeps the overlay out of the pane's scrollback,
        # and no-wrap stops a wide row from scrolling the frame we just painted.
        sys.stdout.write(ALT_ON + WRAP_OFF + CLEAR + SHOW)
        sys.stdout.flush()

    def restore_terminal(self) -> None:
        sys.stdout.write(RESET + WRAP_ON + SHOW + ALT_OFF)
        if self.old_term is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_term)
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
        try:
            self.sock_ino = os.stat(path).st_ino
        except OSError:
            self.sock_ino = None
        dlog("switcher setup_ipc bound", path)

    def setup_keyd(self) -> None:
        self.listen = spawn_keyd()
        if self.listen is None:
            self.arm()
            dlog("keyd listen unavailable -> armed")
            return
        fd = self.listen.stdout.fileno() if self.listen.stdout else None
        if fd is None:
            self.arm()
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
                # A newer instance may own the path by now; never unlink its socket.
                if self.sock_ino is not None and os.stat(sock_path()).st_ino == self.sock_ino:
                    os.unlink(sock_path())
            except OSError:
                pass
        self.restore_terminal()

    def cols_rows(self) -> tuple[int, int]:
        columns, lines = terminal_size()
        return max(48, columns), max(8, lines)

    def preview_width(self, cols: int) -> int:
        """Columns for the preview, or 0 when it is off or there is no room."""
        if not self.preview_on or cols < self.PREVIEW_MIN_COLS:
            return 0
        return max(32, min(56, cols // 3))

    # -- preview ------------------------------------------------------------

    def cached_preview(self, pane_id: str, width: int) -> list[str] | None:
        """What we already have for this pane, if it is still fresh enough."""
        cached = self.preview_text.get(pane_id)
        if cached and time.monotonic() - cached[0] < self.PREVIEW_TTL and cached[2] == width:
            return cached[1]
        return None

    def preview_lines(self, pane_id: str, rows: int, width: int) -> list[str]:
        """The tail of a pane, cached briefly so cycling does not re-read it."""
        cached = self.cached_preview(pane_id, width)
        if cached is not None:
            return cached
        now = time.monotonic()
        try:
            raw = api.client().read_pane(pane_id, max(4, rows))
        except api.ApiError:
            raw = ""
        lines: list[str] = []
        for line in raw.splitlines():
            plain = PLAIN.sub("", line).rstrip()
            # A pane's visible screen is mostly empty rows; one blank is enough
            # to keep the shape, and the column is only a few dozen wide.
            if not plain and (not lines or not lines[-1]):
                continue
            lines.append(plain)
        while lines and not lines[-1].strip():
            lines.pop()
        trimmed = [pad(line, width) for line in lines[-rows:]]
        self.preview_text[pane_id] = (now, trimmed, width)
        return trimmed

    def take_preview(self, selected: dict[str, Any] | None, rows: int, width: int) -> list[str]:
        """Only what is already read. A pane read is a round trip, and this
        runs while the frame -- including the very first one -- is composing,
        so a miss leaves the column empty and asks the loop to fetch it."""
        self.preview_want = ""
        if width <= 0 or not selected or selected.get("kind") != "tab":
            return []
        pane_id = str(selected.get("pane_id") or "")
        if not pane_id:
            return []
        cached = self.cached_preview(pane_id, width)
        if cached is not None:
            return cached
        self.preview_want = pane_id
        self.preview_rows = rows
        self.preview_cols = width
        return []

    def fetch_preview(self) -> None:
        """Read the pane the last frame wanted, then ask for a repaint."""
        pane_id, self.preview_want = self.preview_want, ""
        if not pane_id:
            return
        self.preview_lines(pane_id, self.preview_rows, self.preview_cols)
        self.dirty = True

    # -- composition --------------------------------------------------------

    def compose(self) -> tuple[list[str], int]:
        """Every row of the frame as a finished string, and the frame width."""
        cols, height = self.cols_rows()
        items = self.filtered()
        if items:
            self.index = max(0, min(self.index, len(items) - 1))
        preview_w = self.preview_width(cols)
        list_w = cols - preview_w - (3 if preview_w else 0)

        header = 2
        footer = 1
        new_item = next((item for item in items if item["kind"] == "new-tab"), None)
        tabs = [item for item in items if item["kind"] == "tab"]
        pinned = 2 if new_item else 0
        body = max(1, height - header - footer - pinned)

        selected = items[self.index] if items else None
        tab_index = next(
            (i for i, tab in enumerate(tabs) if selected is not None and tab["id"] == selected["id"]), 0
        )
        if selected and selected["kind"] == "tab":
            if tab_index < self.scroll:
                self.scroll = tab_index
            if tab_index >= self.scroll + body:
                self.scroll = max(0, tab_index - body + 1)
        self.scroll = max(0, min(self.scroll, max(0, len(tabs) - body)))

        rows: list[str] = [self.render_search(list_w), self.rule(list_w)]
        if new_item:
            rows.append(self.render_row(new_item, selected is new_item, list_w))
            rows.append(self.rule(list_w))
        visible = tabs[self.scroll : self.scroll + body]
        for tab in visible:
            rows.append(self.render_row(tab, selected is tab, list_w))
        if not visible and not new_item:
            rows.append(f"  {FG_MUTED}" + pad("no matches", max(0, list_w - 2)) + RESET)
        while len(rows) < height - footer:
            rows.append("")
        rows = rows[: height - footer]
        rows.append(self.render_footer(list_w, len(tabs)))

        if preview_w:
            lines = self.take_preview(selected, height - header, preview_w)
            for index in range(header, height):
                line = lines[index - header] if index - header < len(lines) else ""
                rows[index] = (
                    textutil.pad_visible(rows[index], list_w)
                    + f" {FG_RULE}│{RESET} "
                    + (f"{FG_MUTED}{pad(line, preview_w)}{RESET}" if line.strip() else "")
                )
        return rows, cols

    def render(self) -> None:
        rows, cols = self.compose()
        _, height = self.cols_rows()
        frame = self.painter.frame(rows, cols, height)
        if frame:
            sys.stdout.write(HIDE + frame + self.search_cursor_seq(cols) + SHOW)
            sys.stdout.flush()

    def rule(self, width: int) -> str:
        return FG_RULE + "  " + "─" * max(8, width - 4) + RESET

    def render_footer(self, width: int, count: int) -> str:
        hints = "⏎ land · ^o preview · esc close"
        if self.note:
            left = self.note
        else:
            left = f"{count} tab{'' if count == 1 else 's'}"
        line = pad(f"  {left}", max(0, width - textutil.width(hints) - 2)) + hints
        return FG_MUTED + pad(line, width) + RESET

    def render_search(self, cols: int) -> str:
        prefix = "  /  "
        if self.query:
            field, style = self.query, ""
        else:
            field, style = "search tabs, titles, dirs, agents", DIM
        inner = pad(field, max(8, cols - len(prefix) - 1))
        return f"{FG_MUTED}{prefix}{RESET}{style}{inner}{RESET}"

    def search_cursor_seq(self, cols: int) -> str:
        if self.on_new_tab():
            prefix_len = 6  # " ▸ +  "
            col = min(cols, prefix_len + textutil.width(self.name) + 1)
            return CSI + f"3;{col}H"
        prefix_len = 5  # "  /  "
        col = min(cols, prefix_len + textutil.width(self.query[: self.cursor]) + 1)
        return CSI + f"1;{col}H"

    def render_row(self, item: dict[str, Any], selected: bool, width: int) -> str:
        mark = "▸" if selected else " "
        if item["kind"] == "new-tab":
            label = self.name if selected else (self.name or "New Tab")
            if selected:
                return BG_SEL + FG_SEL + pad(f" {mark} +  {label}", width) + RESET
            return f" {mark} {FG_MUTED}" + pad(f"+  {label}", max(0, width - 4)) + RESET

        status = item.get("agent_status") or ""
        glyph = icons.status_glyph(status)
        agent = icons.agent_mark(item.get("agent"), font=self.icons_on)
        age = relative_age(prompt_ms(item) or age_ms(item))
        # " ▸ " + mark(2) + " " + glyph(1) + "  " = 9 columns before the name.
        lead_w = 9
        age_w = 5
        space_w = 0 if width < 72 else min(18, max(8, width // 6))
        gaps = 2 + (2 if space_w else 0)
        name_w = width - lead_w - age_w - space_w - gaps
        if name_w < 10:
            space_w = 0
            gaps = 2
            name_w = max(1, width - lead_w - age_w - gaps)
        name = pad(str(item.get("label") or ""), name_w)
        space = pad(str(item.get("space") or ""), space_w) if space_w else ""
        age_cell = pad(age, age_w)

        status_sgr = STATUS_COLOR.get(status, FG_MUTED)
        agent_sgr = icons.agent_color(item.get("agent"))
        agent_sgr = CSI + agent_sgr + "m" if agent_sgr else FG_MUTED
        if selected:
            # Keep the selection background while still coloring the glyphs:
            # 39m restores the default foreground without dropping the bg.
            back_to_fg = FG_SEL
            out = [BG_SEL, FG_SEL, f" {mark} ", agent_sgr, agent, back_to_fg, " ", status_sgr, glyph, back_to_fg, "  ", name]
            if space_w:
                out.extend(["  ", space])
            out.extend(["  ", age_cell, RESET])
            return "".join(out)
        out = [
            f" {mark} ", agent_sgr, agent, RESET, " ", status_sgr, glyph, RESET, "  ",
            FG_LABEL, name, RESET,
        ]
        if space_w:
            out.extend(["  ", FG_MUTED, space, RESET])
        out.extend(["  ", FG_MUTED, age_cell, RESET])
        return "".join(out)

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

    def handle_action(self, action: str, *, source: str = "key") -> None:
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
        if action == "preview":
            self.preview_on = not self.preview_on
            self.painter.reset()
            self.dirty = True
            return
        if action == "cycle":
            self.cycle(1, source=source)
            return
        if action == "cycle_prev":
            self.cycle(-1, source=source)
            return
        if action in {"next", "tab"}:
            self.move(1)
            return
        if action in {"previous", "shift+tab"}:
            self.move(-1)

    def handle_keys(self, data: str, *, flush: bool = False) -> None:
        self.pending += data
        while self.pending:
            first = self.pending[0]
            if first in self.chord_leads:
                # Herdr's own chord (prefix+t) forwarded to this popup instead of
                # firing the plugin action. Two bytes, so wait for the second one.
                if len(self.pending) == 1 and not flush:
                    return
                chord = self.chords.get(self.pending[:2])
                if chord:
                    self.pending = self.pending[2:]
                    self.handle_action(chord, source="chord")
                    continue
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
        dlog("ipc msg=", repr(msg))
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
        dlog("keyd line=", repr(line), "state=", state, "ready=", self.ready, "cycles=", self.cycles)
        if state is None:
            return
        was = self.cmd_active
        self.cmd_seen = True
        self.cmd_active = state
        if was and not self.cmd_active:
            if not self.ready:
                self.arm()
                dlog("cmd released -> armed")
                return
            if self.cycles > 0 and not self.on_new_tab():
                self.confirm()

    def confirm(self) -> None:
        dlog("confirm() ready=", self.ready, "cycles=", self.cycles, "index=", self.index)
        item = self.current()
        if item is None:
            self.done = {"op": "cancel"}
            return
        if item["kind"] == "new-tab":
            self.done = {"op": "new-tab", "label": self.name.strip()}
            return
        self.done = {"op": "focus", "item": item}

    def loop(self) -> dict[str, Any]:
        dlog("switcher start items=", len(self.all_items))
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
                    self.arm()
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
                elif not ready and self.pending and self.pending[0] in self.chord_leads:
                    self.handle_keys("", flush=True)
                for fd in ready:
                    if fd is sys.stdin:
                        _chunk = os.read(sys.stdin.fileno(), 128)
                        dlog("stdin bytes=", repr(_chunk))
                        self.handle_keys(_chunk.decode("utf-8", "ignore"))
                    elif fd is self.server:
                        self.handle_ipc()
                    else:
                        self.handle_keyd()
                now = time.monotonic()
                if now - last_age >= 1.0:
                    self.dirty = True  # the age column moves on its own
                    last_age = now
                if now - self.refreshed_at >= self.REFRESH_SECONDS:
                    self.refresh()
                    self.refreshed_at = now
                if self.dirty:
                    self.render()
                    self.dirty = False
                if self.preview_want:
                    self.fetch_preview()
                if not self.pruned:
                    # After the first frame: the open path never waits on this.
                    self.prune()
        finally:
            dlog("switcher exiting done=", self.done)
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
        """Last resort: retry through the CLI once this popup is gone.

        Herdr refuses some focus changes while a popup still owns the screen,
        and by then this process is exiting, so the retry has to outlive it.
        """
        spawn_detached(args)

    client = api.client()
    if op == "new-tab":
        label = (result.get("label") or "").strip()
        args = ["tab", "create", "--focus"] + (["--label", label] if label else [])
        try:
            client.create_tab(label)
        except api.ApiError:
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
                client.focus_workspace(workspace_id)
            except api.ApiError:
                pass
        try:
            client.focus_tab(tab_id)
        except api.ApiError:
            later(["tab", "focus", tab_id])
            return 0
        pane_id = item.get("pane_id")
        if pane_id:
            # Land on the pane the row described, not just its tab.
            client.focus_pane(str(pane_id))
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
