#!/usr/bin/env python3
"""Unit tests for Drover. Stdlib unittest only."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hook  # noqa: E402
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
    def test_builds_from_tab_and_workspace_lists(self) -> None:
        tabs = {
            "result": {
                "tabs": [
                    {
                        "tab_id": "w0:t1",
                        "workspace_id": "w0",
                        "label": "chat",
                        "agent_status": "done",
                        "focused": False,
                        "pane_count": 1,
                        "number": 2,
                    },
                    {
                        "tab_id": "w0:t2",
                        "workspace_id": "w0",
                        "label": "idle-one",
                        "agent_status": "idle",
                        "focused": True,
                        "pane_count": 1,
                        "number": 1,
                    },
                ]
            }
        }
        spaces = {"result": {"workspaces": [{"workspace_id": "w0", "label": "metaintro"}]}}

        def fake_herdr(*args: str) -> dict:
            if args == ("tab", "list"):
                return tabs
            if args == ("workspace", "list"):
                return spaces
            raise AssertionError(args)

        mru = {"items": {"w0:t1": {"last_focused_ms": 10, "last_prompt_ms": 5}}}
        with mock.patch.object(lib, "herdr", side_effect=fake_herdr):
            items = lib.build_items(mru)
        self.assertEqual([item["id"] for item in items], ["w0:t1", "w0:t2"])
        self.assertEqual(items[0]["space"], "metaintro")
        self.assertEqual(items[0]["last_prompt_ms"], 5)
        self.assertTrue(items[1]["current"])


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HERDR_PLUGIN_STATE_DIR": self.tmp.name,
                "HERDR_PLUGIN_EVENT": "",
                "HERDR_PLUGIN_EVENT_JSON": "",
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

    def test_apply_new_tab_calls_herdr_once(self) -> None:
        with mock.patch.object(switcher, "herdr") as herdr_fn, mock.patch.object(switcher.subprocess, "Popen") as popen:
            self.assertEqual(switcher.apply({"op": "new-tab", "label": "notes"}), 0)
            herdr_fn.assert_called_once_with("tab", "create", "--focus", "--label", "notes")
            popen.assert_not_called()

    def test_apply_new_tab_retries_only_on_failure(self) -> None:
        with mock.patch.object(switcher, "herdr", side_effect=RuntimeError("busy")), mock.patch.object(
            switcher.subprocess, "Popen"
        ) as popen:
            self.assertEqual(switcher.apply({"op": "new-tab", "label": ""}), 0)
            popen.assert_called_once()


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


if __name__ == "__main__":
    unittest.main()
