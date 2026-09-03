#!/usr/bin/env python3
"""Record MRU timestamps from Herdr plugin events. Stay cheap: no snapshot except startup."""

from __future__ import annotations

import json
import os
import sys

from lib import drop_item, mutate_mru, now_ms, remember_live_state, snapshot, touch_item


def event_payload() -> tuple[str, dict]:
    kind = (os.environ.get("HERDR_PLUGIN_EVENT") or "").strip()
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON") or ""
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            kind = kind or str(parsed.get("event") or "")
            inner = parsed.get("data")
            data = inner if isinstance(inner, dict) else parsed
    return kind.replace("-", "_").replace(".", "_"), data


def main() -> int:
    kind = (os.environ.get("HERDR_PLUGIN_EVENT") or "").strip()
    if kind == "startup":
        remember_live_state(snapshot())
        return 0

    event, data = event_payload()
    tab = data.get("tab") if isinstance(data.get("tab"), dict) else {}
    pane = data.get("pane") if isinstance(data.get("pane"), dict) else {}
    tab_id = data.get("tab_id") or tab.get("tab_id") or os.environ.get("HERDR_TAB_ID")
    workspace_id = data.get("workspace_id") or tab.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    pane_id = data.get("pane_id") or pane.get("pane_id") or os.environ.get("HERDR_PANE_ID")
    if pane.get("tab_id"):
        tab_id = tab_id or pane.get("tab_id")

    if event in {"tab_focused"} and tab_id:
        mutate_mru(
            lambda mru: touch_item(mru, tab_id, kind="tab", focused=True, extra={"workspace_id": workspace_id})
        )
        return 0
    if event in {"tab_created"}:
        created_id = tab.get("tab_id") or tab_id
        if created_id:
            mutate_mru(
                lambda mru: touch_item(
                    mru,
                    created_id,
                    kind="tab",
                    activity=True,
                    extra={"workspace_id": workspace_id or tab.get("workspace_id"), "label": tab.get("label")},
                )
            )
        return 0
    if event in {"tab_closed"} and tab_id:
        mutate_mru(lambda mru: drop_item(mru, tab_id))
        return 0
    if event in {"tab_renamed"} and tab_id:
        mutate_mru(
            lambda mru: touch_item(mru, tab_id, kind="tab", extra={"label": data.get("label"), "workspace_id": workspace_id})
        )
        return 0
    if event in {"pane_created", "pane_moved"}:
        mapped = pane.get("tab_id") or tab_id
        mapped_pane = pane.get("pane_id") or pane_id
        if mapped and mapped_pane:

            def edit(mru: dict) -> bool | None:
                panes = mru.setdefault("panes", {})
                if panes.get(mapped_pane) == mapped:
                    return False
                panes[mapped_pane] = mapped
                return True

            mutate_mru(edit)
        return 0
    if event in {"pane_agent_status_changed"}:

        def edit(mru: dict) -> bool | None:
            resolved = tab_id or (mru.get("panes") or {}).get(pane_id)
            if not resolved:
                return False
            status = str(data.get("agent_status") or "")
            if status not in {"working", "blocked", "done", "idle"}:
                return False
            entry = (mru.get("items") or {}).get(resolved) or {}
            prev = str(entry.get("agent_status") or "")
            if status == prev:
                return False
            extra = {"workspace_id": workspace_id, "agent_status": status}
            if status == "working" and prev != "working":
                extra["last_prompt_ms"] = now_ms()
            touch_item(mru, resolved, kind="tab", extra=extra)
            return True

        mutate_mru(edit)
        return 0
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - hooks must never crash the server
        sys.stderr.write(f"drover hook: {exc}\n")
        raise SystemExit(0)
