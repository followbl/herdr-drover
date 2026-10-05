#!/usr/bin/env python3
"""Unit tests for Drover. Stdlib unittest only."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
import hook  # noqa: E402
import icons  # noqa: E402
import paint  # noqa: E402
import titles  # noqa: E402
import text as textutil  # noqa: E402
import keys  # noqa: E402
import lib  # noqa: E402
import switcher  # noqa: E402


class RelativeAgeTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(lib.relative_age(0, now=1_000_000), "—")

    def test_buckets(self) -> None:
        now = 1_700_000_000_000
        self.assertEqual(lib.relative_age(now - 3_000, now=now), "now")
        self.assertEqual(lib.relative_age(now - 45_000, now=now), "45s")
        self.assertEqual(lib.relative_age(now - 5 * 60_000, now=now), "5m")
        self.assertEqual(lib.relative_age(now - 3 * 3_600_000, now=now), "3h")
        self.assertEqual(lib.relative_age(now - 4 * 86_400_000, now=now), "4d")


class FuzzyTests(unittest.TestCase):
    def test_empty_query_matches(self) -> None:
        self.assertEqual(lib.fuzzy_score("", "hello"), 0)

    def test_substring_beats_miss(self) -> None:
        self.assertIsNotNone(lib.fuzzy_score("chat", "product chat"))
        self.assertIsNone(lib.fuzzy_score("zzz", "product chat"))

    def test_filter_keeps_order_by_score(self) -> None:
        items = [
            {"label": "email-warmup", "space": "marketing", "agent_status": "idle", "search": "", "kind": "tab"},
            {"label": "chat", "space": "product", "agent_status": "done", "search": "", "kind": "tab"},
        ]
        filtered = lib.filter_items(items, "chat")
        self.assertEqual([item["label"] for item in filtered], ["chat"])


class SortTests(unittest.TestCase):
    def test_done_tabs_sort_first(self) -> None:
        items = [
            {"agent_status": "idle", "last_focused_ms": 200, "number": 1, "space": "a", "label": "old"},
            {"agent_status": "done", "last_focused_ms": 50, "number": 2, "space": "b", "label": "finished"},
            {"agent_status": "working", "last_focused_ms": 300, "number": 3, "space": "c", "label": "busy"},
            {"agent_status": "done", "last_focused_ms": 80, "number": 4, "space": "b", "label": "newer-done"},
        ]
        items.sort(key=lib.tab_sort_key)
        self.assertEqual(
            [item["label"] for item in items],
            ["newer-done", "finished", "busy", "old"],
        )


class MruTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_touch_and_drop(self) -> None:
        data: dict = {"items": {}}
        lib.touch_item(data, "w0:t1", kind="tab", focused=True, extra={"label": "chat"})
        self.assertGreater(data["items"]["w0:t1"]["last_focused_ms"], 0)
        data.setdefault("panes", {})["w0:p1"] = "w0:t1"
        lib.drop_item(data, "w0:t1")
        self.assertNotIn("w0:t1", data["items"])
        self.assertNotIn("w0:p1", data["panes"])

    def test_mutate_skips_write_on_false(self) -> None:
        lib.save_mru({"items": {"keep": {"id": "keep"}}})

        def skip(_mru: dict) -> bool:
            return False

        lib.mutate_mru(skip)
        self.assertEqual(lib.load_mru()["items"]["keep"]["id"], "keep")

    def test_prompt_ms(self) -> None:
        self.assertEqual(lib.prompt_ms({"last_prompt_ms": 12}), 12)
        self.assertEqual(lib.prompt_ms({}), 0)


class BuildItemsTests(unittest.TestCase):
    SNAP = {
        "focused_tab_id": "w0:t2",
        "focused_pane_id": "w0:p2",
        "workspaces": [{"workspace_id": "w0", "label": "metaintro"}],
        "tabs": [
            {"tab_id": "w0:t1", "workspace_id": "w0", "label": "chat", "agent_status": "done",
             "focused": False, "pane_count": 1, "number": 2},
            {"tab_id": "w0:t2", "workspace_id": "w0", "label": "", "agent_status": "idle",
             "focused": True, "pane_count": 1, "number": 1},
        ],
        "panes": [
            {"pane_id": "w0:p1", "tab_id": "w0:t1", "agent": "claude",
             "foreground_cwd": "/home/b/Work/metaintro", "terminal_title_stripped": "Twilio migration"},
            {"pane_id": "w0:p2", "tab_id": "w0:t2", "agent": "",
             "cwd": "/home/b/Work/herdr-drover", "terminal_title_stripped": "drover"},
        ],
    }

    def test_builds_from_one_snapshot(self) -> None:
        mru = {"items": {"w0:t1": {"last_focused_ms": 10, "last_prompt_ms": 5}}}
        items = lib.build_items(mru, snap=self.SNAP)
        self.assertEqual([item["id"] for item in items], ["w0:t1", "w0:t2"])
        self.assertEqual(items[0]["space"], "metaintro")
        self.assertEqual(items[0]["last_prompt_ms"], 5)
        self.assertEqual(items[0]["agent"], "claude")
        self.assertEqual(items[0]["pane_id"], "w0:p1")
        self.assertTrue(items[1]["current"])

    def test_unnamed_tab_takes_the_agent_title(self) -> None:
        items = lib.build_items({"items": {}}, snap=self.SNAP)
        self.assertEqual([item["label"] for item in items], ["chat", "drover"])

    def test_search_field_covers_title_dir_and_agent(self) -> None:
        items = lib.build_items({"items": {}}, snap=self.SNAP)
        field = items[0]["search"]
        for part in ("chat", "Twilio migration", "metaintro", "claude", "#2"):
            self.assertIn(part, field)

    def test_lead_pane_prefers_focused_then_agent(self) -> None:
        panes = [{"pane_id": "a"}, {"pane_id": "b", "agent": "codex"}, {"pane_id": "c"}]
        self.assertEqual(lib.lead_pane(panes, "c")["pane_id"], "c")
        self.assertEqual(lib.lead_pane(panes, None)["pane_id"], "b")
        self.assertEqual(lib.lead_pane([{"pane_id": "z"}], None)["pane_id"], "z")
        self.assertEqual(lib.lead_pane([], None), {})


class DisplayTitleTests(unittest.TestCase):
    def test_default_labels_are_positions_not_names(self) -> None:
        self.assertTrue(lib.is_default_label(""))
        self.assertTrue(lib.is_default_label("6"))
        self.assertFalse(lib.is_default_label("core-sms"))
        self.assertFalse(lib.is_default_label("API 03"))

    def test_task_title_wins(self) -> None:
        self.assertEqual(lib.display_title("Clarifying patch notes", "claude", "/w/x"), "Clarifying patch notes")

    def test_codex_folder_suffix_is_dropped(self) -> None:
        self.assertEqual(lib.display_title("Twilio migration | metaintro", "codex", "/w/metaintro"), "Twilio migration")

    def test_agent_and_folder_noise_falls_back_to_the_directory(self) -> None:
        self.assertEqual(lib.display_title("\u03c0 - Work", "pi", "/home/b/Work"), "Work")
        self.assertEqual(lib.display_title("claude", "claude", "/home/b/Work/herdr"), "herdr")
        self.assertEqual(lib.display_title("", "", "/home/b/Work/tsk/ae1275"), "ae1275")

    def test_unnamed_tabs_take_the_title_in_build_items(self) -> None:
        snap = {
            "workspaces": [{"workspace_id": "w0", "label": "space"}],
            "tabs": [{"tab_id": "w0:t1", "workspace_id": "w0", "label": "6", "number": 33}],
            "panes": [{"pane_id": "w0:p1", "tab_id": "w0:t1", "agent": "claude",
                       "cwd": "/w/x", "terminal_title_stripped": "Patch notes"}],
        }
        self.assertEqual(lib.build_items({"items": {}}, snap=snap)[0]["label"], "Patch notes")

    def test_named_tabs_are_left_alone(self) -> None:
        snap = {
            "workspaces": [],
            "tabs": [{"tab_id": "w0:t1", "workspace_id": "w0", "label": "core-sms", "number": 1}],
            "panes": [{"pane_id": "w0:p1", "tab_id": "w0:t1", "agent": "claude",
                       "cwd": "/w/x", "terminal_title_stripped": "Patch notes"}],
        }
        self.assertEqual(lib.build_items({"items": {}}, snap=snap)[0]["label"], "core-sms")


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_drops_dead_tabs_and_panes(self) -> None:
        lib.save_mru({
            "items": {"live": {"id": "live"}, "dead": {"id": "dead"}},
            "panes": {"p-live": "live", "p-dead": "dead", "p-gone": "live"},
        })
        snap = {"tabs": [{"tab_id": "live"}], "panes": [{"pane_id": "p-live"}]}
        lib.prune_mru(snap)
        data = lib.load_mru()
        self.assertEqual(list(data["items"]), ["live"])
        self.assertEqual(list(data["panes"]), ["p-live"])

    def test_empty_snapshot_is_not_evidence_of_an_empty_session(self) -> None:
        lib.save_mru({"items": {"keep": {"id": "keep"}}, "panes": {}})
        lib.prune_mru({"tabs": [], "panes": []})
        self.assertIn("keep", lib.load_mru()["items"])


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HERDR_PLUGIN_STATE_DIR": self.tmp.name,
                "HERDR_PLUGIN_EVENT": "",
                "HERDR_PLUGIN_EVENT_JSON": "",
                # Running the suite inside a Herdr pane would otherwise leak the
                # real tab/pane ids into events that deliberately omit them.
                "HERDR_TAB_ID": "",
                "HERDR_PANE_ID": "",
                "HERDR_WORKSPACE_ID": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, event: str, data: dict) -> int:
        os.environ["HERDR_PLUGIN_EVENT"] = event
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps({"event": event, "data": data})
        return hook.main()

    def test_focus_then_prompt_transition(self) -> None:
        self.assertEqual(self._run("tab.focused", {"tab_id": "w0:t1", "workspace_id": "w0"}), 0)
        self.assertEqual(
            self._run("pane.agent_status_changed", {"pane_id": "w0:p1", "tab_id": "w0:t1", "agent_status": "working"}),
            0,
        )
        entry = lib.load_mru()["items"]["w0:t1"]
        self.assertEqual(entry["agent_status"], "working")
        self.assertGreater(entry["last_prompt_ms"], 0)
        first = entry["last_prompt_ms"]
        self.assertEqual(
            self._run("pane.agent_status_changed", {"tab_id": "w0:t1", "agent_status": "working"}),
            0,
        )
        self.assertEqual(lib.load_mru()["items"]["w0:t1"]["last_prompt_ms"], first)

    def test_pane_map_resolves_status_without_tab_id(self) -> None:
        self._run("pane.created", {"pane": {"pane_id": "w0:p9", "tab_id": "w0:t9"}})
        self._run("pane.agent_status_changed", {"pane_id": "w0:p9", "agent_status": "done"})
        self.assertEqual(lib.load_mru()["items"]["w0:t9"]["agent_status"], "done")

    def test_closed_drops_item(self) -> None:
        self._run("tab.created", {"tab": {"tab_id": "w0:t3", "label": "tmp"}})
        self._run("tab.closed", {"tab_id": "w0:t3"})
        self.assertNotIn("w0:t3", lib.load_mru()["items"])

    def test_unknown_event_is_ok(self) -> None:
        self.assertEqual(self._run("workspace.focused", {}), 0)

    def test_hook_never_raises(self) -> None:
        os.environ["HERDR_PLUGIN_EVENT"] = "tab.focused"
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = "{"
        self.assertEqual(hook.main(), 0)


class SwitcherParserTests(unittest.TestCase):
    def test_cmd_layer_state(self) -> None:
        self.assertTrue(switcher.cmd_layer_state("+cmd"))
        self.assertFalse(switcher.cmd_layer_state("-cmd"))
        self.assertIsNone(switcher.cmd_layer_state("+shift"))
        self.assertTrue(switcher.cmd_layer_state("cmd"))
        self.assertIsNone(switcher.cmd_layer_state(""))

    def test_escape_arrows_and_esc(self) -> None:
        self.assertEqual(switcher.parse_escape("\x1b[A")[0], "up")
        self.assertEqual(switcher.parse_escape("\x1b[B")[0], "down")
        self.assertEqual(switcher.parse_escape("\x1b")[0], "pending")
        self.assertEqual(switcher.parse_escape("\x1b[Z")[0], "shift+tab")

    def test_kitty_ctrl_shift_t(self) -> None:
        action, _used = switcher.parse_escape("\x1b[116;6u")
        self.assertEqual(action, "ctrl+shift+t")

    def test_apply_cancel(self) -> None:
        self.assertEqual(switcher.apply({"op": "cancel"}), 0)

    def test_apply_new_tab_calls_the_api_once(self) -> None:
        client = mock.Mock()
        with mock.patch.object(switcher.api, "client", return_value=client), mock.patch.object(
            switcher, "spawn_detached"
        ) as popen:
            self.assertEqual(switcher.apply({"op": "new-tab", "label": "notes"}), 0)
            client.create_tab.assert_called_once_with("notes")
            popen.assert_not_called()

    def test_apply_new_tab_retries_only_on_failure(self) -> None:
        client = mock.Mock()
        client.create_tab.side_effect = switcher.api.ApiError("busy")
        with mock.patch.object(switcher.api, "client", return_value=client), mock.patch.object(
            switcher, "spawn_detached"
        ) as popen:
            self.assertEqual(switcher.apply({"op": "new-tab", "label": ""}), 0)
            popen.assert_called_once()

    def test_apply_focus_lands_on_the_pane_the_row_named(self) -> None:
        client = mock.Mock()
        with mock.patch.object(switcher.api, "client", return_value=client):
            switcher.apply({"op": "focus", "item": {"id": "w0:t1", "workspace_id": "w0", "pane_id": "w0:p1"}})
        client.focus_workspace.assert_called_once_with("w0")
        client.focus_tab.assert_called_once_with("w0:t1")
        client.focus_pane.assert_called_once_with("w0:p1")

    def test_apply_focus_retries_through_the_cli_when_the_api_fails(self) -> None:
        client = mock.Mock()
        client.focus_tab.side_effect = switcher.api.ApiError("popup owns the screen")
        with mock.patch.object(switcher.api, "client", return_value=client), mock.patch.object(
            switcher, "spawn_detached"
        ) as popen:
            switcher.apply({"op": "focus", "item": {"id": "w0:t1"}})
        popen.assert_called_once()
        client.focus_pane.assert_not_called()



class ChordTests(unittest.TestCase):
    CONFIG = """
[keys]
prefix = "ctrl+e"

[[keys.command]]
key = "prefix+t"
type = "plugin_action"
command = "followbl.drover.open"

[[keys.command]]
key = "prefix+y"
type = "plugin_action"
command = "followbl.drover.cycle-prev"

[[keys.command]]
key = "prefix+z"
type = "plugin_action"
command = "other.plugin.thing"
"""

    def test_parses_prefix_chords(self) -> None:
        self.assertEqual(
            keys.herdr_chord(self.CONFIG),
            {"\x05t": "cycle", "\x05y": "cycle_prev"},
        )

    def test_no_chords_without_config(self) -> None:
        self.assertEqual(keys.herdr_chord(""), {})

    def test_non_ctrl_prefix_is_ignored(self) -> None:
        self.assertEqual(keys.herdr_chord('[keys]\nprefix = "f1"\n'), {})


def _switcher(items: int = 3) -> "switcher.Switcher":
    built = [
        {
            "id": f"t{i}",
            "kind": "tab",
            "label": f"tab {i}",
            "space": "",
            "workspace_id": "w0",
            "agent_status": "idle",
            "pane_count": 1,
            "number": i,
            "last_focused_ms": 1000 - i,
            "last_prompt_ms": 0,
            "search": f"tab {i}",
            "current": False,
        }
        for i in range(items)
    ]
    with mock.patch.object(switcher, "build_items", return_value=built), mock.patch.object(
        switcher, "herdr_chord", return_value={"\x05t": "cycle"}
    ):
        return switcher.Switcher()


class CycleTests(unittest.TestCase):
    def test_forwarded_prefix_chord_cycles_instead_of_typing(self) -> None:
        sw = _switcher()
        sw.arm()
        start = sw.index
        sw.handle_keys("\x05t")
        self.assertEqual(sw.query, "")
        self.assertEqual(sw.cycles, 1)
        self.assertNotEqual(sw.index, start)

    def test_split_chord_waits_for_second_byte(self) -> None:
        sw = _switcher()
        sw.arm()
        sw.handle_keys("\x05")
        self.assertEqual(sw.cycles, 0)
        self.assertEqual(sw.pending, "\x05")
        sw.handle_keys("t")
        self.assertEqual(sw.cycles, 1)
        self.assertEqual(sw.query, "")

    def test_lead_byte_without_chord_falls_through(self) -> None:
        sw = _switcher()
        sw.arm()
        sw.query = "tab"
        sw.cursor = 0
        sw.handle_keys("\x05a")  # C-e (end) then a literal "a"
        self.assertEqual(sw.cycles, 0)
        self.assertEqual(sw.query, "taba")
        self.assertEqual(sw.cursor, len("taba"))

    def test_one_tap_delivered_twice_counts_once(self) -> None:
        sw = _switcher()
        sw.arm()
        sw.cycle(1, source="ipc")
        sw.cycle(1, source="chord")
        self.assertEqual(sw.cycles, 1)

    def test_repeat_taps_from_one_source_all_count(self) -> None:
        sw = _switcher()
        sw.arm()
        sw.cycle(1, source="ipc")
        sw.cycle(1, source="ipc")
        sw.cycle(1, source="ipc")
        self.assertEqual(sw.cycles, 3)

    def test_cycle_before_arming_is_replayed(self) -> None:
        sw = _switcher()
        start = sw.index
        sw.cycle(1, source="ipc")
        self.assertEqual(sw.cycles, 0)
        sw.arm()
        self.assertEqual(sw.cycles, 1)
        self.assertNotEqual(sw.index, start)

    def test_cleanup_keeps_a_newer_instance_socket(self) -> None:
        sw = _switcher()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "herdr-drover.sock")
            Path(path).write_text("newer", encoding="utf-8")
            sw.sock_ino = os.stat(path).st_ino + 1  # someone else owns it now
            sw.server = mock.Mock()
            sw.old_term = None
            with mock.patch.object(switcher, "sock_path", return_value=path), mock.patch.object(
                switcher.sys, "stdout", io.StringIO()
            ):
                sw.cleanup()
            self.assertTrue(os.path.exists(path))

    def test_cleanup_removes_its_own_socket(self) -> None:
        sw = _switcher()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "herdr-drover.sock")
            Path(path).write_text("mine", encoding="utf-8")
            sw.sock_ino = os.stat(path).st_ino
            sw.server = mock.Mock()
            sw.old_term = None
            with mock.patch.object(switcher, "sock_path", return_value=path), mock.patch.object(
                switcher.sys, "stdout", io.StringIO()
            ):
                sw.cleanup()
            self.assertFalse(os.path.exists(path))

class ManifestTests(unittest.TestCase):
    def test_required_fields(self) -> None:
        text = (ROOT / "herdr-plugin.toml").read_text(encoding="utf-8")
        self.assertIn('id = "followbl.drover"', text)
        self.assertIn('name = "Drover"', text)
        self.assertIn("[[actions]]", text)
        self.assertIn("[[panes]]", text)
        self.assertIn('id = "open"', text)
        self.assertIn('id = "cycle-prev"', text)
        self.assertIn('id = "switcher"', text)
        self.assertIn("[keybindings]", text)
        self.assertIn("min_herdr_version", text)


class KeybindingTests(unittest.TestCase):
    def test_normalize_chords(self) -> None:
        self.assertEqual(keys.normalize_key("ctrl+shift+t"), "C-S-t")
        self.assertEqual(keys.normalize_key("C-S-t"), "C-S-t")
        self.assertEqual(keys.normalize_key("S-Tab"), "S-Tab")
        self.assertEqual(keys.normalize_key("escape"), "Esc")
        self.assertEqual(keys.normalize_key("C-p"), "C-p")

    def test_default_keymap_select_and_cycle(self) -> None:
        mapping = keys.keymap(keys.DEFAULT_KEYBINDINGS)
        self.assertEqual(mapping["Enter"], "select")
        self.assertEqual(mapping["C-S-t"], "cycle")
        self.assertEqual(mapping["Up"], "move_up")

    def test_user_override_replaces_action_keys(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = Path(tmp.name) / "keybindings.toml"
        override.write_text('[keybindings]\nmove_up = ["k"]\n', encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"HERDR_PLUGIN_ROOT": str(ROOT), "HERDR_PLUGIN_CONFIG_DIR": tmp.name},
        ):
            mapping = keys.keymap()
        self.assertEqual(mapping.get("k"), "move_up")
        self.assertNotEqual(mapping.get("Up"), "move_up")
        self.assertEqual(mapping["Enter"], "select")

    def test_action_for_event(self) -> None:
        mapping = keys.keymap(keys.DEFAULT_KEYBINDINGS)
        self.assertEqual(keys.action_for_event("enter", mapping), "select")
        self.assertEqual(keys.action_for_event("ctrl+shift+t", mapping), "cycle")
        self.assertEqual(keys.action_for_char("\x03", mapping), "force_quit")



class TextWidthTests(unittest.TestCase):
    def test_wide_and_zero_width_characters(self) -> None:
        self.assertEqual(textutil.width("abc"), 3)
        self.assertEqual(textutil.width("日本語"), 6)
        self.assertEqual(textutil.width("e\u0301"), 1)  # combining accent
        self.assertEqual(textutil.width("\u2733\ufe0f"), 2)  # emoji + variation selector

    def test_pad_counts_columns_not_characters(self) -> None:
        self.assertEqual(textutil.width(textutil.pad("日本語", 10)), 10)
        self.assertEqual(textutil.width(textutil.pad("plain", 10)), 10)
        self.assertEqual(textutil.pad("", 3), "   ")
        self.assertEqual(textutil.pad("abc", 0), "")

    def test_truncation_never_overflows_the_cell(self) -> None:
        for source in ("abcdefgh", "日本語テスト", "mixed 日本 text"):
            for limit in range(1, 12):
                self.assertLessEqual(textutil.width(textutil.pad(source, limit)), limit)
                self.assertEqual(textutil.width(textutil.pad(source, limit)), limit)


class IconTests(unittest.TestCase):
    def test_status_glyphs_mirror_herdr(self) -> None:
        self.assertEqual(icons.status_glyph("blocked"), "◉")
        self.assertEqual(icons.status_glyph("working"), "◐")
        self.assertEqual(icons.status_glyph("done"), "●")
        self.assertEqual(icons.status_glyph("idle"), "✓")
        self.assertEqual(icons.status_glyph("nonsense"), "·")

    def test_logo_codepoints_match_the_font(self) -> None:
        # Herdr Agent Icons Max: vendor order is the codepoint order.
        self.assertEqual(icons.LOGOS["claude"], chr(0xE1A0))
        self.assertEqual(icons.LOGOS["codex"], chr(0xE1A1))
        self.assertEqual(len(icons.VENDORS), len(set(icons.VENDORS)))

    def test_marks_are_two_columns_with_or_without_the_font(self) -> None:
        for agent in ("claude", "codex", "cursor-agent", "unheard-of", "", None):
            for font in (True, False):
                mark = icons.agent_mark(agent, font=font)
                self.assertEqual(textutil.width(mark), 2, (agent, font))

    def test_aliases_and_colors(self) -> None:
        self.assertEqual(icons.normalize("Claude Code"), "claude")
        self.assertEqual(icons.normalize("cursor-agent"), "cursor")
        self.assertEqual(icons.agent_mark("cursor-agent", font=True), icons.LOGOS["cursor"] + " ")
        self.assertTrue(icons.agent_color("claude"))
        self.assertEqual(icons.agent_color("nothing-here"), "")


class PaintTests(unittest.TestCase):
    def test_first_frame_paints_everything(self) -> None:
        painter = paint.Painter()
        frame = painter.frame(["one", "two"], 20, 2)
        self.assertIn("one", frame)
        self.assertIn("two", frame)

    def test_unchanged_frame_costs_nothing(self) -> None:
        painter = paint.Painter()
        painter.frame(["one", "two"], 20, 2)
        self.assertEqual(painter.frame(["one", "two"], 20, 2), "")

    def test_only_the_changed_row_is_written(self) -> None:
        painter = paint.Painter()
        painter.frame(["one", "two", "three"], 20, 3)
        frame = painter.frame(["one", "TWO", "three"], 20, 3)
        self.assertIn("TWO", frame)
        self.assertNotIn("three", frame)
        self.assertIn("\x1b[2;1H", frame)

    def test_resize_and_reset_force_a_repaint(self) -> None:
        painter = paint.Painter()
        painter.frame(["one"], 20, 1)
        self.assertIn("one", painter.frame(["one"], 30, 1))
        painter.frame(["one"], 30, 1)
        painter.reset()
        self.assertIn("one", painter.frame(["one"], 30, 1))

    def test_shorter_frame_clears_the_tail(self) -> None:
        painter = paint.Painter()
        painter.frame(["one", "two"], 20, 2)
        frame = painter.frame(["one"], 20, 2)
        self.assertIn("\x1b[2;1H", frame)
        self.assertIn("\x1b[J", frame)


class ApiTests(unittest.TestCase):
    def _server(self, directory: str, response: dict, requests: list) -> str:
        import socket as socketlib
        import threading

        path = os.path.join(directory, "api.sock")
        listener = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        listener.bind(path)
        listener.listen(4)

        def serve() -> None:
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                with conn:
                    data = conn.recv(65536)
                    if not data:
                        continue
                    requests.append(json.loads(data.decode().splitlines()[0]))
                    conn.sendall((json.dumps(response) + "\n").encode())

        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(listener.close)
        return path

    def test_snapshot_over_the_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            requests: list = []
            path = self._server(tmp, {"result": {"snapshot": {"tabs": [{"tab_id": "w0:t1"}]}}}, requests)
            client = api.Client(path=path)
            self.assertEqual(client.snapshot()["tabs"][0]["tab_id"], "w0:t1")
            self.assertEqual(requests[0]["method"], "session.snapshot")

    def test_server_error_is_reported_not_retried_on_the_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._server(tmp, {"error": {"message": "no such tab"}}, [])
            client = api.Client(path=path)
            with mock.patch.object(api, "herdr_bin", side_effect=AssertionError("must not shell out")):
                with self.assertRaises(api.ApiError):
                    client.focus_tab("gone")

    def test_missing_socket_falls_back_to_the_cli(self) -> None:
        client = api.Client(path="/nonexistent/herdr.sock")
        self.assertFalse(client.socket_ok)
        payload = json.dumps({"result": {"snapshot": {"tabs": []}}}).encode()
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr=b"")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            self.assertEqual(client.snapshot(), {"tabs": []})
        self.assertEqual(run.call_args[0][0][1:], ["api", "snapshot"])

    def test_transport_failure_switches_to_the_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dead.sock")
            open(path, "w").close()  # exists, but nothing is listening
            client = api.Client(path=path)
            self.assertTrue(client.socket_ok)
            payload = json.dumps({"result": {"snapshot": {"tabs": []}}}).encode()
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr=b"")
            with mock.patch.object(subprocess, "run", return_value=completed):
                client.snapshot()
            self.assertFalse(client.socket_ok)


def _fake_switcher(count: int = 4, statuses: tuple = ("idle", "working", "done", "blocked")) -> "switcher.Switcher":
    items = [
        {
            "id": f"t{i}", "kind": "tab", "label": f"tab {i} ✳ 日本語", "space": "metaintro",
            "workspace_id": "w0", "pane_id": f"p{i}", "agent": ["claude", "codex", "", "pi"][i % 4],
            "cwd": "/home/b/Work/metaintro", "title": "task title",
            "agent_status": statuses[i % len(statuses)], "pane_count": 1, "number": i,
            "last_focused_ms": 1_700_000_000_000 - i, "last_prompt_ms": 1_700_000_000_000 - i,
            "search": f"tab {i} metaintro", "current": False,
        }
        for i in range(count)
    ]
    with mock.patch.object(switcher, "build_items", return_value=items), mock.patch.object(
        switcher, "herdr_chord", return_value={}
    ):
        return switcher.Switcher()


class ComposeTests(unittest.TestCase):
    def _compose(self, sw: "switcher.Switcher", cols: int, rows: int) -> list:
        with mock.patch.object(switcher, "terminal_size", return_value=(cols, rows)):
            composed, width = sw.compose()
        self.assertEqual(width, cols)
        return composed

    def test_no_row_overflows_the_frame(self) -> None:
        sw = _fake_switcher()
        for cols in (48, 60, 80, 104, 160):
            for row in self._compose(sw, cols, 20):
                plain = switcher.PLAIN.sub("", row)
                self.assertLessEqual(textutil.width(plain), cols, (cols, repr(plain)))

    def test_rows_carry_status_glyphs(self) -> None:
        sw = _fake_switcher()
        body = "".join(self._compose(sw, 120, 20))
        for glyph in ("✓", "◐", "●", "◉"):
            self.assertIn(glyph, body)

    def test_agent_marks_follow_the_font(self) -> None:
        sw = _fake_switcher()
        sw.icons_on = False
        plain = switcher.PLAIN.sub("", "".join(self._compose(sw, 120, 20)))
        self.assertIn("cl ", plain)  # claude's tag, no font
        self.assertNotIn(icons.LOGOS["claude"], plain)
        sw.icons_on = True
        sw.painter.reset()
        with_font = switcher.PLAIN.sub("", "".join(self._compose(sw, 120, 20)))
        self.assertIn(icons.LOGOS["claude"], with_font)
        self.assertIn(icons.LOGOS["codex"], with_font)

    def test_selection_is_marked_once(self) -> None:
        sw = _fake_switcher()
        rows = [switcher.PLAIN.sub("", row) for row in self._compose(sw, 100, 20)]
        self.assertEqual(sum(1 for row in rows if row.startswith(" ▸")), 1)

    def test_preview_column_appears_only_when_there_is_room(self) -> None:
        sw = _fake_switcher()
        sw.index = 1
        with mock.patch.object(switcher.api, "client") as client:
            client.return_value.read_pane.return_value = "preview line"
            self._compose(sw, 160, 20)
            sw.fetch_preview()
        wide = "".join(self._compose(sw, 160, 20))
        self.assertIn("│", wide)
        narrow = "".join(self._compose(sw, 80, 20))
        self.assertNotIn("│", narrow)

    def test_preview_toggle_turns_the_column_off(self) -> None:
        sw = _fake_switcher()
        sw.handle_action("preview")
        self.assertFalse(sw.preview_on)
        self.assertNotIn("│", "".join(self._compose(sw, 160, 20)))

    def test_footer_counts_the_tabs(self) -> None:
        sw = _fake_switcher(count=3)
        self.assertIn("3 tabs", switcher.PLAIN.sub("", self._compose(sw, 100, 20)[-1]))

    def test_preview_strips_escapes_and_control_bytes(self) -> None:
        sw = _fake_switcher()
        client = mock.Mock()
        client.read_pane.return_value = "\x1b[31mred\x1b[0m line\r\n\x1b]0;title\x07tail\n\n"
        with mock.patch.object(switcher.api, "client", return_value=client):
            lines = sw.preview_lines("p0", 4, 20)
        self.assertEqual([line.strip() for line in lines], ["red line", "tail"])

    def test_preview_failure_is_not_fatal(self) -> None:
        sw = _fake_switcher()
        client = mock.Mock()
        client.read_pane.side_effect = switcher.api.ApiError("gone")
        with mock.patch.object(switcher.api, "client", return_value=client):
            self.assertEqual(sw.preview_lines("p0", 4, 20), [])


class PreviewSchedulingTests(unittest.TestCase):
    def test_first_frame_does_not_read_a_pane(self) -> None:
        sw = _fake_switcher()
        client = mock.Mock()
        with mock.patch.object(switcher.api, "client", return_value=client), mock.patch.object(
            switcher, "terminal_size", return_value=(160, 20)
        ):
            sw.compose()
        client.read_pane.assert_not_called()
        self.assertTrue(sw.preview_want)

    def test_the_wanted_pane_is_read_after_the_frame(self) -> None:
        sw = _fake_switcher()
        client = mock.Mock()
        client.read_pane.return_value = "tail line"
        with mock.patch.object(switcher.api, "client", return_value=client), mock.patch.object(
            switcher, "terminal_size", return_value=(160, 20)
        ):
            sw.compose()
            sw.fetch_preview()
            self.assertTrue(sw.dirty)
            rows, _ = sw.compose()
        client.read_pane.assert_called_once()
        self.assertIn("tail line", switcher.PLAIN.sub("", "".join(rows)))
        self.assertFalse(sw.preview_want)

    def test_a_narrower_frame_re_reads_at_the_new_width(self) -> None:
        sw = _fake_switcher()
        client = mock.Mock()
        client.read_pane.return_value = "tail line"
        with mock.patch.object(switcher.api, "client", return_value=client), mock.patch.object(
            switcher, "terminal_size", return_value=(160, 20)
        ):
            sw.compose(); sw.fetch_preview()
        with mock.patch.object(switcher.api, "client", return_value=client), mock.patch.object(
            switcher, "terminal_size", return_value=(200, 20)
        ):
            sw.compose()
        self.assertTrue(sw.preview_want)


class TitlerTriggerTests(unittest.TestCase):
    def _snap(self, labels: list) -> dict:
        return {"tabs": [{"tab_id": f"t{i}", "label": label} for i, label in enumerate(labels)]}

    def test_unnamed_tabs_start_a_pass(self) -> None:
        sw = _fake_switcher()
        with mock.patch.object(switcher, "spawn_titler") as spawn:
            sw.start_titler(self._snap(["core-sms", "6"]))
        spawn.assert_called_once()

    def test_nothing_to_name_starts_nothing(self) -> None:
        sw = _fake_switcher()
        with mock.patch.object(switcher, "spawn_titler") as spawn:
            sw.start_titler(self._snap(["core-sms", "ship"]))
        spawn.assert_not_called()

    def test_the_off_switch_is_honored(self) -> None:
        sw = _fake_switcher()
        with mock.patch.dict(os.environ, {"DROVER_AI_TITLES": "off"}), mock.patch.object(
            switcher, "spawn_titler"
        ) as spawn:
            sw.start_titler(self._snap(["6"]))
        spawn.assert_not_called()


class RefreshTests(unittest.TestCase):
    SNAP = {
        "workspaces": [{"workspace_id": "w0", "label": "metaintro"}],
        "tabs": [
            {"tab_id": "t0", "workspace_id": "w0", "label": "tab 0", "agent_status": "done", "number": 0},
            {"tab_id": "t9", "workspace_id": "w0", "label": "new one", "agent_status": "idle", "number": 9},
        ],
        "panes": [],
    }

    def test_refresh_updates_in_place_and_keeps_the_highlight(self) -> None:
        sw = _fake_switcher(count=3)
        sw.index = 2  # the second tab, with the New Tab row at 0
        selected = sw.current()["id"]
        client = mock.Mock()
        client.snapshot.return_value = self.SNAP
        with mock.patch.object(switcher.api, "client", return_value=client):
            sw.refresh()
        ids = [item["id"] for item in sw.all_items if item["kind"] == "tab"]
        self.assertEqual(ids, ["t0", "t9"])  # t1/t2 closed, t9 appended last
        self.assertEqual(sw.all_items[0]["kind"], "new-tab")
        self.assertNotEqual(sw.current()["id"], selected)  # that tab is gone
        self.assertEqual(sw.all_items[1]["agent_status"], "done")

    def test_refresh_keeps_order_when_nothing_closed(self) -> None:
        sw = _fake_switcher(count=2)
        before = [item["id"] for item in sw.all_items]
        snap = dict(self.SNAP)
        snap["tabs"] = [
            {"tab_id": "t1", "workspace_id": "w0", "label": "tab 1", "agent_status": "working", "number": 1},
            {"tab_id": "t0", "workspace_id": "w0", "label": "tab 0", "agent_status": "idle", "number": 0},
        ]
        client = mock.Mock()
        client.snapshot.return_value = snap
        with mock.patch.object(switcher.api, "client", return_value=client):
            sw.refresh()
        self.assertEqual([item["id"] for item in sw.all_items], before)

    def test_refresh_failure_leaves_the_list_alone(self) -> None:
        sw = _fake_switcher(count=2)
        before = list(sw.all_items)
        client = mock.Mock()
        client.snapshot.side_effect = switcher.api.ApiError("server restarting")
        with mock.patch.object(switcher.api, "client", return_value=client):
            sw.refresh()
        self.assertEqual(sw.all_items, before)


class TitleTextTests(unittest.TestCase):
    def test_tidy_accepts_a_name(self) -> None:
        self.assertEqual(titles.tidy("Telnyx Migration"), "Telnyx Migration")
        self.assertEqual(titles.tidy('  "API Audit"  '), "API Audit")
        self.assertEqual(titles.tidy("Error Contracts."), "Error Contracts")

    def test_tidy_rejects_an_answer_that_is_not_a_name(self) -> None:
        for answer in (
            "",
            "Here is a good title for your session",
            "Sure! API Audit",
            "I think this session is about the API",
            "Supercalifragilistic Expialidocious Naming",
        ):
            self.assertEqual(titles.tidy(answer), "", answer)

    def test_tidy_keeps_at_most_three_words(self) -> None:
        self.assertEqual(titles.tidy("Telnyx Migration Plan"), "Telnyx Migration Plan")
        self.assertEqual(titles.tidy("Plan The Telnyx Migration Now"), "")

    def test_heuristic_names_without_a_model(self) -> None:
        self.assertEqual(titles.short_name("can you please migrate us from twilio to telnyx"), "Migrate Twilio Telnyx")
        self.assertEqual(titles.short_name("the and or if"), "")

    def test_thin_requests_are_recognized(self) -> None:
        self.assertTrue(titles.is_thin("/effort ultracode"))
        self.assertTrue(titles.is_thin("go on"))
        self.assertFalse(titles.is_thin("audit the API layer for url encoding"))

    def test_noise_is_not_a_request(self) -> None:
        self.assertTrue(titles.is_noise("<local-command-caveat>Caveat: the messages below"))
        self.assertTrue(titles.is_noise("<command-name>/clear</command-name>"))
        self.assertFalse(titles.is_noise("fix the retry policy"))

    def test_model_prompt_carries_title_directory_and_requests(self) -> None:
        shown = titles.compose_prompt(
            {"title": "π - metaintro", "cwd": "/home/b/Work/metaintro", "prompts": ["audit the api layer"]}
        )
        self.assertIn("Session title: π - metaintro", shown)
        self.assertIn("Working directory: metaintro", shown)
        self.assertIn("Request 1: audit the api layer", shown)

    def test_falls_back_to_the_heuristic_when_the_model_declines(self) -> None:
        with mock.patch.object(titles, "ask_model", return_value=""):
            name = titles.title_for({"prompts": ["migrate us from twilio to telnyx"], "title": "", "cwd": ""})
        self.assertEqual(name, "Migrate Twilio Telnyx")


class TranscriptTests(unittest.TestCase):
    def _write(self, directory: str, lines: list) -> str:
        path = os.path.join(directory, "session.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        return path

    def test_reads_pi_and_claude_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "session", "id": "x"},
                {"type": "message", "message": {"role": "system", "content": "you are"}},
                {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "audit the api layer"}]}},
            ])
            self.assertEqual(titles.first_prompt(path), "audit the api layer")
            claude = self._write(tmp, [
                {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<system-reminder>x"}},
                {"type": "user", "message": {"role": "user", "content": "<local-command-caveat>Caveat: the messages below"}},
                {"type": "user", "message": {"role": "user", "content": "plan the telnyx move"}},
            ])
            self.assertEqual(titles.first_prompt(claude), "plan the telnyx move")

    def test_thin_requests_come_after_substantial_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "message", "message": {"role": "user", "content": "/effort ultracode"}},
                {"type": "message", "message": {"role": "user", "content": "rewrite the retry policy for job reads"}},
            ])
            self.assertEqual(
                titles.early_prompts(path),
                ["rewrite the retry policy for job reads", "/effort ultracode"],
            )

    def test_transcript_path_for_each_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = os.path.join(tmp, "s.jsonl")
            open(session, "w").close()
            self.assertEqual(titles.transcript_path({"kind": "path", "value": session}), session)
            self.assertEqual(titles.transcript_path({"kind": "path", "value": "/nope.jsonl"}), "")
            projects = os.path.join(tmp, "projects", "-home-b-Work")
            os.makedirs(projects)
            claude = os.path.join(projects, "abc-123.jsonl")
            open(claude, "w").close()
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertEqual(titles.transcript_path({"kind": "id", "value": "abc-123"}), claude)
                self.assertEqual(titles.transcript_path({"kind": "id", "value": "missing"}), "")
            self.assertEqual(titles.transcript_path(None), "")


class TitlePassTests(unittest.TestCase):
    SNAP = {
        "focused_pane_id": "w0:p1",
        "tabs": [
            {"tab_id": "w0:t1", "label": "6", "number": 6},
            {"tab_id": "w0:t2", "label": "core-sms", "number": 7},
            {"tab_id": "w0:t3", "label": "8", "number": 8},
        ],
        "panes": [
            {"pane_id": "w0:p1", "tab_id": "w0:t1", "agent": "pi", "cwd": "/w/metaintro",
             "agent_session": {"kind": "path", "value": "SESSION"}},
            {"pane_id": "w0:p2", "tab_id": "w0:t2", "agent": "claude", "cwd": "/w/x",
             "agent_session": {"kind": "path", "value": "SESSION"}},
            {"pane_id": "w0:p3", "tab_id": "w0:t3", "agent": "", "cwd": "/w/y"},
        ],
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.tmp.name, "DROVER_AI_TITLES": ""})
        self.env.start()
        self.session = os.path.join(self.tmp.name, "session.jsonl")
        with open(self.session, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "message", "message": {"role": "user", "content": "migrate us to telnyx"}}) + "\n")
        self.snap = json.loads(json.dumps(self.SNAP).replace("SESSION", self.session))

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _client(self, snap=None):
        client = mock.Mock()
        client.snapshot.return_value = snap or self.snap
        return client

    def test_only_unnamed_tabs_with_a_transcript_are_candidates(self) -> None:
        found = titles.candidates(self.snap)
        self.assertEqual([item["tab_id"] for item in found], ["w0:t1"])

    def test_a_pass_names_the_unnamed_tab(self) -> None:
        client = self._client()
        with mock.patch.object(titles.api, "client", return_value=client), mock.patch.object(
            titles, "ask_model", return_value="Telnyx Migration"
        ):
            written = titles.run()
        self.assertEqual(written, [("w0:t1", "Telnyx Migration")])
        client.call.assert_called_once()
        self.assertEqual(client.call.call_args[0][0], "tab.rename")
        self.assertEqual(client.call.call_args[0][1]["label"], "Telnyx Migration")

    def test_a_name_typed_while_the_model_thought_wins(self) -> None:
        client = self._client()
        renamed = json.loads(json.dumps(self.snap))
        renamed["tabs"][0]["label"] = "brad's name"
        client.snapshot.side_effect = [self.snap, renamed]
        with mock.patch.object(titles.api, "client", return_value=client), mock.patch.object(
            titles, "ask_model", return_value="Telnyx Migration"
        ):
            self.assertEqual(titles.run(), [])
        client.call.assert_not_called()

    def test_the_same_session_is_not_named_twice(self) -> None:
        client = self._client()
        with mock.patch.object(titles.api, "client", return_value=client), mock.patch.object(
            titles, "ask_model", return_value="Telnyx Migration"
        ) as ask:
            titles.run()
            named = json.loads(json.dumps(self.snap))
            named["tabs"][0]["label"] = "Telnyx Migration"
            client.snapshot.return_value = named
            titles.run()
        ask.assert_called_once()

    def test_dry_run_changes_nothing(self) -> None:
        client = self._client()
        with mock.patch.object(titles.api, "client", return_value=client), mock.patch.object(
            titles, "ask_model", return_value="Telnyx Migration"
        ):
            written = titles.run(dry_run=True)
        self.assertEqual(written, [("w0:t1", "Telnyx Migration")])
        client.call.assert_not_called()
        self.assertFalse(os.path.exists(titles.titles_path()))

    def test_off_switch(self) -> None:
        with mock.patch.dict(os.environ, {"DROVER_AI_TITLES": "off"}):
            self.assertEqual(titles.run(), [])

    def test_state_forgets_closed_tabs(self) -> None:
        titles.save_state({"tabs": {"w0:t9": {"session": "x", "title": "Old Name"}}})
        client = self._client()
        with mock.patch.object(titles.api, "client", return_value=client), mock.patch.object(
            titles, "ask_model", return_value="Telnyx Migration"
        ):
            titles.run()
        self.assertNotIn("w0:t9", titles.load_state()["tabs"])


if __name__ == "__main__":
    unittest.main()
