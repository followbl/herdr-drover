# Drover

A [Herdr](https://herdr.dev/) tab switcher named for cattle droving dogs. Hold Super, tap T to cycle recent tabs, release Super to land. Arrow up for **New Tab**.

Python 3, stdlib only. No build step.

## Features

- **Hold to cycle, release to land** on Linux when Super is a keyd `cmd` layer
- **MRU tabs** across every space in the session
- **`done` tabs first**, so finished agents are one tap away
- **Age is time since last prompt**, not last focus
- **New Tab** is a pinned row: arrow onto it, type a name, Enter
- **Fuzzy search** from the `/` field as soon as the overlay opens
- Overlay only — tabs do not focus while you cycle
- **User-owned keybindings** for both the Herdr chord and the overlay

## Install

Requires Herdr **≥ 0.7.5** (`herdr -V`) and **Python 3.10+** on `PATH`.

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
| Esc | Close without changing focus |

### Super-release (Linux)

Release-to-land uses `keyd listen` and a layer named `cmd`. If keyd is missing, the overlay still works: pick with arrows or search, then Enter.

On macOS there is no keyd path. Use arrows / search / Enter.

## How the list is ordered

1. New Tab (pinned)
2. Tabs whose agent status is `done`, newest focus first
3. Every other tab, newest focus first

The right-hand column is **time since the agent last went idle → working** (a user prompt). It stays put while the agent works and does not reset when you focus the tab. Tabs never prompted since install show `—`.

## Requirements

| | |
|---|---|
| Herdr | ≥ 0.7.5 |
| Python | 3.10+ |
| Linux Super-release | `keyd` with a `cmd` layer |
| macOS | picker only |

State lives in `$XDG_STATE_HOME/herdr/plugins/followbl.drover/` (or `HERDR_PLUGIN_STATE_DIR`). Overlay key remaps live in `HERDR_PLUGIN_CONFIG_DIR` (`herdr plugin config-dir followbl.drover`). Do not put secrets in the plugin root; GitHub installs replace that checkout.

## Development

```bash
python3 -m unittest discover -s tests -v
```

CI runs the same command on every push.

## License

MIT
