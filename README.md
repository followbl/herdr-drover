# Hold T

A [Herdr](https://herdr.dev/) tab switcher with the Super+T gesture: hold Super, tap T to cycle recent tabs, release Super to land. Arrow up for **New Tab**.

Python 3, stdlib only. No build step.

## Features

- **Hold to cycle, release to land** on Linux when Super is a keyd `cmd` layer (Omarchy / similar)
- **MRU tabs** across every space in the session
- **`done` tabs first**, so finished agents are one tap away
- **Age is time since last prompt**, not last focus
- **New Tab** is a pinned row: arrow onto it, type a name, Enter
- **Fuzzy search** from the `/` field as soon as the overlay opens
- Overlay only — tabs do not focus while you cycle

## Install

Requires Herdr **≥ 0.7.5** (`herdr -V`) and **Python 3.10+** on `PATH`.

```bash
herdr plugin install followbl/herdr-hold-t
```

Or link a checkout while developing:

```bash
git clone https://github.com/followbl/herdr-hold-t
cd herdr-hold-t
herdr plugin link "$PWD"
python3 -m unittest discover -s tests -v
```

The marketplace lists public GitHub repos tagged `herdr-plugin`. After the first push it can take up to 30 minutes to appear at [herdr.dev/plugins](https://herdr.dev/plugins/).

## Bind Super+T

Add this to `~/.config/herdr/config.toml` and reload (`herdr server reload-config`):

```toml
[[keys.command]]
key = "ctrl+shift+t"
type = "plugin_action"
command = "followbl.hold-t.open"
description = "Hold T"
```

On this plugin's original setup, physical Super+T is mapped in the terminal to `Ctrl+Shift+T` via [keyd](https://github.com/rvaiya/keyd). Bind whatever chord your terminal actually sends.

`prefix+c` (or your existing `new_tab` key) can stay as a direct create.

## Usage

| Key | Action |
|---|---|
| Super+T | Open the overlay (search focused, first tab highlighted) |
| T while Super is held | Cycle to the next tab (skips New Tab) |
| Release Super | Land on the highlighted tab (after at least one cycle) |
| Type | Fuzzy-filter tabs |
| ↑ / ↓ | Move, including onto New Tab |
| Enter | Focus the tab, or create New Tab with the typed name |
| Esc | Close without changing focus |

### Super-release (Linux)

Release-to-land uses `keyd listen` and a layer named `cmd`. If keyd is missing, the overlay still works: pick with arrows or search, then Enter.

On macOS there is no keyd path. Use arrows / search / Enter. Hold-to-commit needs something that can see Super key-up (terminals cannot).

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

State lives in `$XDG_STATE_HOME/herdr/plugins/followbl.hold-t/` (or `HERDR_PLUGIN_STATE_DIR` when Herdr launches the plugin). Do not put secrets in the plugin root; GitHub installs replace that checkout.

## Development

```bash
python3 -m unittest discover -s tests -v
```

CI runs the same command on every push.

## License

MIT
