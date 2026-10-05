#!/usr/bin/env python3
"""Status glyphs, and optional vendor marks for agents.

The status glyphs are Herdr's own (`◉` needs you, `◐` working, `●` done,
`✓` idle), so a Drover row reads the same as the sidebar row it came from.

The vendor logos come from the **Herdr Agent Icons Max** font published by
[herdr-radar](https://github.com/hhdebb/herdr-radar) (MIT; the marks belong to
their owners). Codepoints and vendor order match that font's `codepoints.toml`,
so Drover, Radar and herdr-bar all draw the same glyph for the same agent. The
font is never installed or required: without it, rows fall back to a colored
two-letter tag, and nothing shifts, because both are two columns wide.
"""

from __future__ import annotations

import os
import sys

FAMILY = "Herdr Agent Icons Max"
FONT_PREFIX = "HerdrAgentIconsMax"

# Order is the font's, so index == codepoint offset. Do not reorder.
VENDORS = (
    "claude", "codex", "opencode", "omp", "cline", "mastracode", "kimi", "kilo",
    "maki", "pi", "hermes", "cursor", "copilot", "deepseek", "gemini", "gpt",
    "qwen", "grok", "agy", "kiro", "amp", "devin", "qodercli", "glm",
)
LOGOS = {name: chr(0xE1A0 + index) for index, name in enumerate(VENDORS)}

ALIASES = {
    "cursor-agent": "cursor",
    "open-code": "opencode",
    "claude-code": "claude",
    "github-copilot": "copilot",
    "gemini-cli": "gemini",
}

# Two columns, so the list keeps its grid when the font is missing.
TAGS = {
    "claude": "cl",
    "codex": "cx",
    "opencode": "oc",
    "omp": "op",
    "cline": "ci",
    "kimi": "km",
    "pi": "pi",
    "cursor": "cu",
    "copilot": "co",
    "deepseek": "ds",
    "gemini": "gm",
    "gpt": "gp",
    "qwen": "qw",
    "grok": "gk",
    "amp": "am",
    "devin": "dv",
    "glm": "gl",
}

STATUS_GLYPHS = {
    "blocked": "◉",
    "working": "◐",
    "done": "●",
    "idle": "✓",
    "unknown": "·",
    "": "·",
}

# 256-color SGR parameters. Terminal palettes vary; these are the vendor hues
# rounded to the cube so they survive a theme change.
AGENT_COLORS = {
    "claude": "38;5;173",
    "codex": "38;5;36",
    "kimi": "38;5;141",
    "gemini": "38;5;33",
    "cursor": "38;5;140",
    "opencode": "38;5;39",
    "copilot": "38;5;250",
    "grok": "38;5;244",
    "pi": "38;5;244",
    "deepseek": "38;5;63",
    "gpt": "38;5;36",
}


def normalize(agent: str | None) -> str:
    kind = (agent or "").strip().lower().split(":", 1)[0]
    if kind.endswith(" code"):
        kind = kind[:-5]
    kind = kind.replace("_", "-")
    return ALIASES.get(kind, kind)


_FONT_FOUND: bool | None = None


def _font_dirs() -> list[str]:
    if sys.platform == "darwin":
        return [os.path.expanduser("~/Library/Fonts"), "/Library/Fonts"]
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return [os.path.join(data, "fonts"), os.path.expanduser("~/.fonts"), "/usr/share/fonts"]


def _installed_in(directory: str) -> bool:
    """Look one level down, not a full walk.

    /usr/share/fonts holds thousands of files and this runs while someone is
    waiting for the popup to appear.
    """
    try:
        with os.scandir(directory) as entries:
            children = []
            for entry in entries:
                if entry.is_file() and entry.name.startswith(FONT_PREFIX):
                    return True
                if entry.is_dir():
                    children.append(entry.path)
    except OSError:
        return False
    for child in children:
        try:
            with os.scandir(child) as entries:
                if any(entry.is_file() and entry.name.startswith(FONT_PREFIX) for entry in entries):
                    return True
        except OSError:
            continue
    return False


def font_available() -> bool:
    """Whether this machine's terminal can draw the vendor marks.

    Cached for the life of the process: a font that appears mid-popup would
    reflow every row. A font the terminal cannot see is worse than no font --
    the row would spend two columns on a replacement box -- so this only says
    yes for a font installed where the terminal would look.
    """
    global _FONT_FOUND
    if _FONT_FOUND is not None:
        return _FONT_FOUND
    choice = os.environ.get("DROVER_AGENT_ICONS", "").strip().lower()
    if choice == "font":
        _FONT_FOUND = True
        return True
    if choice == "off":
        _FONT_FOUND = False
        return False
    found = any(_installed_in(directory) for directory in _font_dirs())
    if not found and sys.platform.startswith("linux"):
        import subprocess

        try:
            result = subprocess.run(
                ["fc-match", "--format", "%{family}", FAMILY],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
            families = [name.strip() for name in (result.stdout or "").split(",")]
            found = result.returncode == 0 and FAMILY in families
        except (OSError, subprocess.TimeoutExpired):
            found = False
    _FONT_FOUND = found
    return found


def reset_font_cache() -> None:
    global _FONT_FOUND
    _FONT_FOUND = None


def agent_mark(agent: str | None, *, font: bool | None = None) -> str:
    """Two columns identifying the agent, or two spaces for a plain tab."""
    kind = normalize(agent)
    if not kind:
        return "  "
    if font_available() if font is None else font:
        logo = LOGOS.get(kind)
        if logo:
            return logo + " "
    tag = TAGS.get(kind)
    if tag:
        return tag
    return (kind[:2] if len(kind) >= 2 else kind + " ").ljust(2)


def agent_color(agent: str | None) -> str:
    return AGENT_COLORS.get(normalize(agent), "")


def status_glyph(status: str | None) -> str:
    return STATUS_GLYPHS.get((status or "").strip().lower(), "·")
