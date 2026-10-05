# Drover

A [Herdr](https://herdr.dev/) tab switcher named for cattle droving dogs. Hold Super, tap T to cycle recent tabs, release Super to land. Arrow up for **New Tab**.

Python 3, stdlib only. No build step.

## Features

- **Hold to cycle, release to land** on Linux when Super is a keyd `cmd` layer
- **MRU tabs** across every space in the session
- **`done` tabs first**, so finished agents are one tap away
- **Age is time since last prompt**, not last focus
- **New Tab** is a pinned row: arrow onto it, type a name, Enter
- **Fuzzy search** over tab name, the agent's task title, space, agent kind,
  working directory and tab number, from the `/` field as the overlay opens
- **Agent status in Herdr's own language** — `◉` needs you, `◐` working,
  `●` done, `✓` idle, colored like the sidebar
- **Agent marks** — Claude, Codex, Gemini, Cursor and the rest lead their row
  with the vendor logo when the icon font is installed, a colored two-letter
  tag when it is not. Either way the grid does not move.
- **Live pane preview** beside the list, so you can look before you leap
  (`^o` toggles; it hides itself below 104 columns)
- **Updates while open** — status, titles and new tabs land without the rows
  moving under the highlight
- **Costs nothing when idle** — a frame with nothing to say writes no bytes
- Overlay only — tabs do not focus while you cycle
- **User-owned keybindings** for both the Herdr chord and the overlay

## Install

Requires Herdr **≥ 0.9.0** (`herdr -V`) and **Python 3.10+** on `PATH`.

```bash
herdr plugin install followbl/herdr-drover
```

Or link a checkout while developing:

```bash
git clone https://github.com/followbl/herdr-drover
cd herdr-drover
herdr plugin link "$PWD"
python3 -m unittest discover -s tests -v
```

The marketplace lists public GitHub repos tagged `herdr-plugin`. After the first push it can take up to 30 minutes to appear at [herdr.dev/plugins](https://herdr.dev/plugins/).

## Herdr keybindings

These live in **your** `~/.config/herdr/config.toml`, not in the plugin. Reload with `herdr server reload-config`.

```toml
[[keys.command]]
key = "ctrl+shift+t"
type = "plugin_action"
command = "followbl.drover.open"
description = "Drover"

# Optional: cycle backward while the overlay is open
[[keys.command]]
key = "ctrl+shift+tab"
type = "plugin_action"
command = "followbl.drover.cycle-prev"
description = "Drover previous"
```

Bind whatever chord your terminal actually sends. On Omarchy-style setups, physical Super+T is often mapped to `Ctrl+Shift+T` via [keyd](https://github.com/rvaiya/keyd).

`prefix+c` (or your existing `new_tab` key) can stay as a direct create.

## Overlay keybindings

Defaults ship in `herdr-plugin.toml`. To remap without losing changes on plugin reinstall, copy the example into the plugin **config** dir:

```bash
cp keybindings.example.toml "$(herdr plugin config-dir followbl.drover)/keybindings.toml"
```

Then edit `[keybindings]`. Each action is a list of keys. A user file replaces the shipped list for that action only.

| Action | Default keys |
|---|---|
| `select` | Enter |
| `dismiss` | Esc |
| `force_quit` | C-c |
| `move_up` | Up, C-p |
| `move_down` | Down, C-n |
| `move_left` | Left |
| `move_right` | Right |
| `home` | Home, C-a |
| `end` | End, C-e |
| `backspace` | Backspace |
| `cycle` | C-t, C-S-t |
| `preview` | C-o |
| `cycle_prev` | C-S-Tab |
| `next` | Tab |
| `previous` | S-Tab |

Key syntax: `Enter`, `Esc`, `Up`, `Tab`, `S-Tab`, `C-p`, `C-S-t`, `C-S-Tab`. Printable characters still type into search or the New Tab name.

## Usage

| Gesture | Action |
|---|---|
| Super+T (your Herdr bind) | Open the overlay |
| T while Super is held | Cycle to the next tab (skips New Tab) |
| Release Super | Land on the highlighted tab (after at least one cycle) |
| Type | Fuzzy-filter tabs |
| ↑ / ↓ | Move, including onto New Tab |
| Enter | Focus the tab, or create New Tab with the typed name |
| ^o | Show or hide the pane preview |
| Esc | Close without changing focus |

### Super-release (Linux)

Release-to-land uses `keyd listen` and a layer named `cmd`. If keyd is missing, the overlay still works: pick with arrows or search, then Enter.

On macOS there is no keyd path. Use arrows / search / Enter.

## Short tab names, written for you

Herdr labels a tab you never named with its position, so a row reads `6`. The
agent's own title is not always better: Claude writes a real task title, but pi
writes `π - metaintro`, which says only that pi is running somewhere you already
know about.

Both keep a transcript on this machine, and its first human message says what
the session is for. Drover reads that, has a small model condense it to **three
words or fewer**, and names the tab:

| Session's first request | Name |
|---|---|
| can you please read f-twilio and come up with a full plan to move us over to telnyx | `Telnyx Migration` |
| read the herdr tab refactor and tell me what is going on | `Herdr Tab Refactor` |
| go through all core repos and audit the API layer | `API Audit` |
| Read .audit/…/prompts/03.md completely and execute | `Error Contracts` |
| look at the Cloudflare blog for everything they shipped last week | `Competitive Intelligence` |

### When it runs, and how often it asks a model

On Herdr's startup, whenever a new agent is detected, and when the overlay
opens on a tab that is still a number. Each of those is a detached pass that
reads the tab list first and exits in ~40ms unless a tab really is waiting for
a name, so the frequent trigger is cheap and the expensive one is rare.

A model is asked at most **once per session**, and only for a tab that has no
name of its own. A session whose transcript has nothing to read yet is retried
after an hour, not sooner. Name all your tabs and this never calls anything.

A tab whose agent already wrote a short, real title keeps it without a model
call -- though measured against a real session, that shortcut covered only 1
agent tab in 16: most agent titles run to four or five meaningful words, which
is past the limit, so the model still writes most names.

### Keeping them current

A tab opened for one thing is often spent on another. A scheduled sweep rewrites
the names **this plugin wrote** -- never the ones you typed -- when their session
has moved on, reading the most recent requests rather than the first:

```bash
scripts/install-timer.sh             # four sweeps a day, via systemd --user
scripts/install-timer.sh --remove    # stop and remove it
python3 titles.py --refresh --dry-run
```

The timer fires at 02:17, 08:17, 14:17 and 20:17 with `Persistent=true`, so a
machine that was asleep still gets its sweep. A session is only reconsidered
once its transcript has grown by 4KB since it was named, and a sweep asks at
most 12 times, so the ceiling is 48 calls a day and the floor -- a quiet
machine, or tabs you named yourself -- is none.

The tally lives in `titles.json` in the plugin's state directory
(`model_calls`, `named`, `refreshed`, `named_by_model`, `named_by_own_title`,
`named_by_heuristic`), so the question has an answer rather than an estimate. The rules it will not break:

- A tab **you** named is never touched, nor is one named by another plugin.
- A name Drover wrote is replaced only by Drover, and only while it is still
  the name on the tab. Rename it yourself and it stops.
- No transcript, no model, no name: the tab keeps its number.

```bash
herdr plugin action invoke followbl.drover.title-tabs   # name them now
python3 titles.py --dry-run                             # see what it would name
```

| | |
|---|---|
| `DROVER_AI_TITLES=off` | no model, no renames |
| `DROVER_TITLE_CMD` | the model command; default `claude -p --model haiku` |

Without a model command on PATH it falls back to two salient words from the
request itself, so the tab still beats a number.

### Showing them in Radar

[herdr-radar](https://github.com/hhdebb/herdr-radar) names an agent row after
the session title by default, which is where `π - metaintro` comes from. Set
`row_label = "tab"` in its config (`prefix+,`) and rows read the tab's name
instead, falling back to the session title only for tabs nobody has named --
which is exactly the set Drover names.

## Agent marks

Vendor logos come from the **Herdr Agent Icons Max** font published by
[herdr-radar](https://github.com/hhdebb/herdr-radar) (MIT; the vendor marks
belong to their owners). Drover uses that font's codepoints, so Radar,
[herdr-bar](https://github.com/jeffarese/herdr-bar) and Drover draw the same
glyph for the same agent. If Radar already shows logos, you are done.

Install it on its own:

```bash
mkdir -p ~/.local/share/fonts
curl -fsSL -o ~/.local/share/fonts/HerdrAgentIconsMax.ttf \
  https://github.com/hhdebb/herdr-radar/raw/main/fonts/HerdrAgentIconsMax.ttf
fc-cache -f
```

Then add the family as a fallback in your terminal's font list. Drover never
installs fonts or edits terminal configuration; without the font it draws a
colored two-letter tag of the same width. `DROVER_AGENT_ICONS=font` forces
logos, `off` forces tags.

## Performance

`python3 scripts/bench.py`, run inside Herdr, times everything on the path
between the key and the first frame. Measured on a 49-tab, 61-pane session:

| | before | after |
|---|---|---|
| Reading the MRU file | 12.9ms, 500KB, 2787 entries | 0.3ms, 11KB, 49 entries |
| An agent-status hook | 29ms | 4ms |
| Importing the overlay | 33ms | 16ms |
| Composing the first frame | 6.6ms (waited on a pane read) | 1.0ms |
| One cycle, on screen | the whole screen, ~1.4KB | the two rows that moved, ~355 bytes |
| A second idle, on screen | ~1.4KB | nothing |

The MRU file is the one that bit: it is rewritten on every tab focus and every
agent status change, and unpruned it keeps an entry for every tab ever seen.

What the server socket is for is *data*, not latency: one `session.snapshot`
carries tabs, spaces, panes and agents together, which is where each row's
agent, working directory and task title come from. Two `herdr` CLI list calls
answer a narrower question slightly faster when the binary is warm in cache
(~3ms against ~7ms for a 114KB snapshot), but they cannot answer this one, and
they pay two process starts for it. Pane reads for the preview are never on the
first frame; the column fills in on the next pass through the loop.

## How the list is ordered

1. New Tab (pinned)
2. Tabs whose agent status is `done`, newest focus first
3. Every other tab, newest focus first

The right-hand column is **time since the agent last went idle → working** (a user prompt). It stays put while the agent works and does not reset when you focus the tab. Tabs never prompted since install show `—`.

## Requirements

| | |
|---|---|
| Herdr | ≥ 0.9.0 (`session.snapshot`, `pane.read`, `pane.focus`) |
| Python | 3.10+ |
| Linux Super-release | `keyd` with a `cmd` layer |
| macOS | picker only |

State lives in `$XDG_STATE_HOME/herdr/plugins/followbl.drover/` (or `HERDR_PLUGIN_STATE_DIR`). Overlay key remaps live in `HERDR_PLUGIN_CONFIG_DIR` (`herdr plugin config-dir followbl.drover`). Do not put secrets in the plugin root; GitHub installs replace that checkout.

## Development

```bash
python3 -m unittest discover -s tests -v
```

CI runs the same command on every push.

## Thanks

[herdr-bar](https://github.com/jeffarese/herdr-bar) by @jeffarese is the better
command bar, and several ideas here are taken from reading it: the server
socket as the data path instead of the CLI, differential painting, column-aware
text measurement, status glyphs that mirror the sidebar, and the agent icon
font. Drover stays a hold-to-cycle switcher; if you want a Cmd+K bar with
automatic tab titles, install that one.

## License

MIT
