# Changelog

## Unreleased

### 0.4.0

Fixes for cycling, and a faster, denser overlay. Ideas taken from reading
[herdr-bar](https://github.com/jeffarese/herdr-bar); see Thanks in the README.

**Cycling**

- A second tap now always cycles. Herdr's own chord (`prefix+t`) is recognized
  by the overlay itself, so a tap forwarded to the focused popup instead of
  firing the plugin action cycles the list rather than typing into the search
  field.
- A tap delivered twice -- as the plugin action over IPC and as the forwarded
  chord -- counts once.
- A fast double tap no longer stacks popups: opens serialize behind a lock, and
  the second tap waits for the first overlay and cycles it.
- Socket cleanup no longer deletes a newer instance's socket.
- Taps that arrive before the overlay knows the Super state are replayed
  instead of dropped.

**Performance**

- Tabs, spaces, panes and agents come from one `session.snapshot` over the
  Herdr socket instead of two `herdr` CLI processes. That is for the data -- it
  is where each row's agent, working directory and task title come from -- not
  for latency: a warm CLI list pair answers its narrower question in ~3ms
  against ~7ms for a 114KB snapshot, while spawning two processes to do it.
- Pane reads for the preview never block a frame, including the first one.
- The MRU file is pruned to live tabs on server start and on first open. It is
  rewritten on every tab focus and agent status change, and unpruned it grew to
  2787 entries and 500KB behind 49 live tabs; reading it went from 12.9ms to
  0.5ms, and a status-change hook from 29ms to 4ms.
- Only rows that changed are repainted. One cycle writes ~355 bytes instead of
  the whole screen, and the once-a-second age tick writes nothing at all when
  nothing moved.
- `subprocess`, `shlex` and the icon-font probe no longer load before the first
  frame; importing the overlay dropped from 33ms to 16ms.

**Overlay**

- Agent status reads in Herdr's own glyphs (`◉` needs you, `◐` working,
  `●` done, `✓` idle) with sidebar colors.
- Agent rows lead with the vendor logo from the Herdr Agent Icons Max font when
  it is installed, and a colored two-letter tag of the same width when it is
  not. `DROVER_AGENT_ICONS=font|off` overrides the probe.
- A live preview of the selected pane sits beside the list. `^o` toggles it; it
  hides itself below 104 columns. `DROVER_PREVIEW=off` starts without it.
- Fuzzy search also matches the agent's task title, the agent kind, the working
  directory and the tab number.
- The list updates while it is open -- status, titles, tabs that appeared or
  closed -- without reordering under the highlight.
- Rows are measured in terminal columns, so emoji and CJK in agent titles no
  longer shift the status column or cut the selection mid-glyph.
- Unnamed tabs show their agent's task title instead of a bare number.
- Landing focuses the pane the row described, not just its tab.
- The overlay draws on the alternate screen, leaving the pane's scrollback
  untouched.
- Requires Herdr 0.9.0.

## 0.3.0

- Renamed the plugin to Drover; overlay keys became user-owned.

## 0.2.0

- Published Hold T as a Herdr plugin.
