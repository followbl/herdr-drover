#!/usr/bin/env python3
"""Name unnamed agent tabs after what the session was asked to do.

Herdr labels a tab you never named with its position, so a row reads `6`, and
the agent's own terminal title is not always better: Claude writes a real task
title, but pi writes `π - metaintro`, which says only that pi is running
somewhere you already know about.

Both of them do keep a transcript on this machine, and its first human message
says exactly what the session is for. So that message, condensed to three words
by a small model, becomes the tab's name -- once, in the background, and only
for tabs nobody has named.

Rules it will not break:

* A tab you named is never touched. Neither is one named by another plugin.
* A name this plugin wrote is replaced only by this plugin, and only while it
  is still the name on the tab.
* One model call per session, and none at all without a transcript to read.

    python3 titles.py                 # one pass over every unnamed agent tab
    python3 titles.py --tab w0:t3     # wait for that tab's session, then name it
    python3 titles.py --dry-run       # print what it would name, change nothing

`DROVER_AI_TITLES=off` disables it. `DROVER_TITLE_CMD` replaces the model
command (default `claude -p --model haiku`); it is given the prompt as its last
argument and must print the title on stdout.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from typing import Any

import api
from lib import dlog, is_default_label, lead_pane, state_dir

DEFAULT_COMMAND = "claude -p --model haiku"
MAX_WORDS = 3
MAX_CHARS = 30
PROMPT_CHARS = 600
MODEL_TIMEOUT = 45.0
PASS_LIMIT = 8
RETRY_AFTER_MS = 60 * 60 * 1000
WAIT_SECONDS = 90
WAIT_STEP = 5.0

# Transcript lines that are not a person asking for something.
NOISE_MARKERS = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<system-reminder>",
    "caveat: the messages below",
)

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "so", "to", "of", "in", "on",
    "for", "with", "without", "into", "from", "at", "by", "as", "is", "are", "was", "were",
    "be", "been", "being", "do", "does", "did", "doing", "can", "could", "should", "would",
    "will", "shall", "may", "might", "must", "have", "has", "had", "i", "we", "you", "it",
    "this", "that", "these", "those", "please", "can", "let", "lets", "want", "need", "like",
    "help", "me", "my", "our", "your", "all", "any", "some", "go", "going", "get", "make",
    "take", "look", "see", "run", "use", "using", "through", "about", "also", "just", "now",
    "here", "there", "what", "why", "how", "when", "where", "who", "which",
}


# -- state --------------------------------------------------------------------


def titles_path() -> str:
    return os.path.join(state_dir(), "titles.json")


def lock_path() -> str:
    return os.path.join(state_dir(), "titles.lock")


def load_state() -> dict[str, Any]:
    try:
        with open(titles_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("tabs"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"tabs": {}}


def save_state(data: dict[str, Any]) -> None:
    path = titles_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


# -- transcripts ---------------------------------------------------------------


def transcript_path(session: dict[str, Any] | None) -> str:
    """Where this agent keeps the conversation, if we know how to find it."""
    if not isinstance(session, dict):
        return ""
    kind = str(session.get("kind") or "")
    value = str(session.get("value") or "")
    if not value:
        return ""
    if kind == "path":
        return value if os.path.isfile(value) else ""
    if kind != "id":
        return ""
    # Claude files its transcripts per project directory, named by session id.
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    projects = os.path.join(root, "projects")
    try:
        with os.scandir(projects) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                candidate = os.path.join(entry.path, f"{value}.jsonl")
                if os.path.isfile(candidate):
                    return candidate
    except OSError:
        return ""
    return ""


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in (None, "text")
        ]
        return " ".join(part for part in parts if part)
    return ""


def is_noise(text: str) -> bool:
    low = text.strip().lower()
    if not low:
        return True
    return any(marker in low for marker in NOISE_MARKERS)


def is_thin(text: str) -> bool:
    """A slash command or a grunt: true of the request, useless as a name."""
    stripped = text.strip()
    return stripped.startswith("/") or len(stripped) < 12


def early_prompts(path: str, want: int = 3, limit: int = 400) -> list[str]:
    """The first few things a person asked for, best ones first.

    One message is often not enough: `/effort ultracode` is a setting, and
    `Read 02.md and execute it` names a file rather than a task. Substantial
    requests come first, thin ones only if there is nothing else.
    """
    solid: list[str] = []
    thin: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= limit or len(solid) >= want:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("isMeta"):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                text = " ".join(_text_of(message.get("content")).split())
                if is_noise(text):
                    continue
                (thin if is_thin(text) else solid).append(text)
    except OSError:
        return []
    return (solid + thin)[:want]


def first_prompt(path: str, limit: int = 400) -> str:
    prompts = early_prompts(path, want=1, limit=limit)
    return prompts[0] if prompts else ""


def clean_prompt(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:PROMPT_CHARS]


# -- titles --------------------------------------------------------------------


def tidy(candidate: str) -> str:
    """Accept a model's answer only if it really is a name of two words."""
    text = " ".join(str(candidate or "").split())
    for quote in ('"', "'", "`", "*", "#"):
        text = text.replace(quote, "")
    text = text.strip().strip(".:;,-–—")
    if not text or "\n" in text:
        return ""
    words = text.split()
    if len(words) > MAX_WORDS + 1:
        return ""  # a sentence, not a name: the fallback does better
    words = words[:MAX_WORDS]
    text = " ".join(words)
    if len(text) > MAX_CHARS:
        return ""
    if text.lower().startswith(("here", "sure", "title", "i ")):
        return ""
    return text


def short_name(prompt: str) -> str:
    """A name from the request itself, for when no model answers."""
    words: list[str] = []
    for raw in prompt.split():
        word = "".join(char for char in raw if char.isalnum() or char in "-_/.").strip("-_/.")
        if not word or word.lower() in STOPWORDS or len(word) < 3:
            continue
        if "/" in word or word.endswith((".py", ".ts", ".js", ".md")):
            word = word.rsplit("/", 1)[-1]
        words.append(word)
        if len(words) == MAX_WORDS:
            break
    if not words:
        return ""
    name = " ".join(word if word[:1].isupper() else word.capitalize() for word in words)
    return name[:MAX_CHARS]


def shorten(title: str) -> str:
    """Cut a title to the word limit without asking anyone.

    Claude already writes a real task title -- `Twilio to Telnyx migration` --
    so the only thing missing is length. Dropping the words that carry no
    meaning usually gets there, and when it does not, the model still can.
    """
    words = [word for word in title.split() if word]
    if not words:
        return ""
    if len(words) <= MAX_WORDS:
        return title if len(title) <= MAX_CHARS else ""
    kept = [word for word in words if word.lower() not in STOPWORDS]
    if not kept or len(kept) > MAX_WORDS:
        return ""
    name = " ".join(kept)
    if len(name) > MAX_CHARS:
        return ""
    # Casing is left exactly as the agent wrote it: `iCIMS` and `JobDiva` lose
    # their shape to any rule simple enough to apply here.
    return name


def own_title(context: dict[str, Any]) -> str:
    """The agent's own title, when it says something and fits."""
    title = " ".join(str(context.get("title") or "").split())
    if " | " in title:
        title = title.split(" | ", 1)[0].strip()
    if not title:
        return ""
    import lib

    # The same reading build_items uses for a row: a title that is only the
    # agent's name or its folder is not a title.
    if lib.display_title(title, str(context.get("agent") or ""), str(context.get("cwd") or "")) != title:
        return ""
    return shorten(title)


def model_command() -> list[str]:
    return shlex.split(os.environ.get("DROVER_TITLE_CMD") or DEFAULT_COMMAND)


def compose_prompt(context: dict[str, Any]) -> str:
    """What the model is shown: the session's own title, what was asked, where."""
    parts: list[str] = []
    title = clean_prompt(str(context.get("title") or ""))
    if title:
        parts.append(f"Session title: {title}")
    where = str(context.get("cwd") or "").rstrip("/").rsplit("/", 1)[-1]
    if where:
        parts.append(f"Working directory: {where}")
    requests = [clean_prompt(text) for text in (context.get("prompts") or []) if text]
    for index, text in enumerate(requests, start=1):
        parts.append(f"Request {index}: {text}")
    joined = "\n".join(parts)
    return joined[: PROMPT_CHARS * 2]


def record(state: dict[str, Any], event: str) -> None:
    """Keep a tally in the state file; how often this runs should be a fact."""
    stats = state.setdefault("stats", {})
    stats[event] = int(stats.get(event) or 0) + 1
    stats[f"last_{event}_ms"] = int(time.time() * 1000)


def ask_model(prompt: str) -> str:
    """A short name from a small model, or an empty string if it will not."""
    instruction = (
        "Name this coding session so its owner recognizes it in a tab list.\n"
        f"Rules: at most {MAX_WORDS} words, fewer when two say it, Title Case, "
        "no punctuation, no quotes.\n"
        "Name the work, not the tool, the agent, the model or a file path. "
        "If the request only points at a file or a command, name what that work is about, "
        "using the session title and directory for context.\n"
        "Answer with the words only.\n\n" + prompt
    )
    argv = model_command()
    if not argv:
        return ""
    try:
        result = subprocess.run(
            [*argv, instruction],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=MODEL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        dlog("title model unavailable", repr(exc))
        return ""
    if result.returncode != 0:
        dlog("title model failed", result.stderr[-200:])
        return ""
    return tidy((result.stdout or b"").decode("utf-8", "replace"))


def title_for(context: dict[str, Any], *, use_model: bool = True) -> tuple[str, str]:
    """A name for this session, and where it came from.

    The source matters: a name the agent already wrote costs nothing, and the
    tally of how often a model was needed is the only honest answer to "how
    often does this run".
    """
    ready = own_title(context)
    if ready:
        return ready, "own_title"
    shown = compose_prompt(context)
    if not shown:
        return "", "none"
    if use_model and os.environ.get("DROVER_AI_TITLES", "").strip().lower() != "off":
        name = ask_model(shown)
        if name:
            return name, "model"
    fallback = " ".join(context.get("prompts") or []) or str(context.get("title") or "")
    name = short_name(clean_prompt(fallback))
    return (name, "heuristic") if name else ("", "none")


# -- the pass ------------------------------------------------------------------


def candidates(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Unnamed tabs whose agent keeps a transcript we can read."""
    panes: dict[str, list[dict[str, Any]]] = {}
    for pane in snap.get("panes") or []:
        if isinstance(pane, dict) and pane.get("tab_id"):
            panes.setdefault(str(pane["tab_id"]), []).append(pane)
    focused = snap.get("focused_pane_id")
    found = []
    for tab in snap.get("tabs") or []:
        if not isinstance(tab, dict) or not tab.get("tab_id"):
            continue
        if not is_default_label(str(tab.get("label") or "")):
            continue
        pane = lead_pane(panes.get(str(tab["tab_id"])) or [], focused)
        session = pane.get("agent_session")
        path = transcript_path(session)
        if not path:
            continue
        found.append(
            {
                "tab_id": str(tab["tab_id"]),
                "label": str(tab.get("label") or ""),
                "agent": str(pane.get("agent") or ""),
                "title": str(pane.get("terminal_title_stripped") or ""),
                "cwd": str(pane.get("foreground_cwd") or pane.get("cwd") or ""),
                "session": str((session or {}).get("value") or ""),
                "path": path,
            }
        )
    return found


def owned(state: dict[str, Any], tab_id: str, label: str) -> bool:
    """True when the name on this tab is one we wrote and may replace."""
    record = (state.get("tabs") or {}).get(tab_id)
    return bool(record) and record.get("title") == label


def should_skip(state: dict[str, Any], item: dict[str, Any], now_ms: int) -> bool:
    record = (state.get("tabs") or {}).get(item["tab_id"])
    if not record:
        return False
    if record.get("session") != item["session"]:
        return False  # a new session in the same tab deserves a new name
    if record.get("title"):
        return True  # already named it; the tab is unnamed again only by hand
    return now_ms - int(record.get("tried_ms") or 0) < RETRY_AFTER_MS


def rename(client: Any, tab_id: str, title: str) -> bool:
    try:
        client.call("tab.rename", {"tab_id": tab_id, "label": title}, ["tab", "rename", tab_id, title])
        return True
    except api.ApiError as exc:
        dlog("rename failed", tab_id, repr(exc))
        return False


def run(tab_id: str = "", *, dry_run: bool = False, limit: int = PASS_LIMIT) -> list[tuple[str, str]]:
    """Name what needs naming. Returns the (tab, title) pairs it wrote."""
    if os.environ.get("DROVER_AI_TITLES", "").strip().lower() == "off" and not dry_run:
        return []
    client = api.client()
    try:
        snap = client.snapshot()
    except api.ApiError as exc:
        dlog("titles: no snapshot", repr(exc))
        return []

    state = load_state()
    now = int(time.time() * 1000)
    items = candidates(snap)
    if tab_id:
        items = [item for item in items if item["tab_id"] == tab_id]
    written: list[tuple[str, str]] = []
    for item in items[:limit]:
        if should_skip(state, item, now):
            continue
        prompts = early_prompts(item["path"])
        if not prompts:
            state.setdefault("tabs", {})[item["tab_id"]] = {
                "session": item["session"],
                "tried_ms": now,
            }
            continue
        title, source = title_for(
            {
                "prompts": prompts,
                "title": item.get("title", ""),
                "cwd": item.get("cwd", ""),
                "agent": item.get("agent", ""),
            }
        )
        if source == "model":
            record(state, "model_calls")
        if not title:
            state.setdefault("tabs", {})[item["tab_id"]] = {
                "session": item["session"],
                "tried_ms": now,
            }
            continue
        if dry_run:
            written.append((item["tab_id"], title))
            continue
        # Re-read the label: the pass takes seconds, and a name typed in the
        # meantime wins.
        fresh = next(
            (tab for tab in client.snapshot().get("tabs") or [] if tab.get("tab_id") == item["tab_id"]),
            None,
        )
        if fresh is None:
            continue
        current = str(fresh.get("label") or "")
        if not is_default_label(current) and not owned(state, item["tab_id"], current):
            continue
        if not rename(client, item["tab_id"], title):
            continue
        record(state, "named")
        record(state, f"named_by_{source}")
        state.setdefault("tabs", {})[item["tab_id"]] = {
            "session": item["session"],
            "title": title,
            "at_ms": now,
        }
        written.append((item["tab_id"], title))
    if not dry_run:
        live = {str(tab.get("tab_id")) for tab in snap.get("tabs") or []}
        state["tabs"] = {key: value for key, value in (state.get("tabs") or {}).items() if key in live}
        save_state(state)
    return written


def tab_label(client: Any, tab_id: str) -> str | None:
    """This tab's current label, or None if it is gone."""
    try:
        result = client.call("tab.list", {}, ["tab", "list"])
    except api.ApiError:
        return None
    for tab in result.get("tabs") or []:
        if isinstance(tab, dict) and tab.get("tab_id") == tab_id:
            return str(tab.get("label") or "")
    return None


def wait_for_tab(tab_id: str, dry_run: bool = False) -> list[tuple[str, str]]:
    """A session has just started; give it time to be asked for something.

    Detection fires for every agent, including all of them at once when the
    server restarts, so this leaves immediately unless the tab really is
    waiting for a name -- and asks for the tab list, not a whole snapshot,
    while it waits.
    """
    client = api.client()
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        label = tab_label(client, tab_id)
        if label is None or not is_default_label(label):
            return []  # named, or closed: nothing here to do
        written = run(tab_id, dry_run=dry_run)
        if written or time.monotonic() >= deadline:
            return written
        time.sleep(WAIT_STEP)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tab", default="", help="only this tab, waiting for its first prompt")
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument("--limit", type=int, default=PASS_LIMIT)
    options = parser.parse_args(argv)

    # One pass at a time: every new agent fires this, and they would otherwise
    # ask the model the same questions at once.
    os.makedirs(state_dir(), exist_ok=True)
    handle = open(lock_path(), "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not options.tab:
                return 0
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if options.tab:
            written = wait_for_tab(options.tab, dry_run=options.dry_run)
        else:
            written = run(dry_run=options.dry_run, limit=options.limit)
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    for tab_id, title in written:
        print(f"{tab_id}\t{title}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a hook must never crash the server
        sys.stderr.write(f"drover titles: {exc}\n")
        raise SystemExit(0)
