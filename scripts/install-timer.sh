#!/usr/bin/env bash
# Install (or remove) the systemd user timer that keeps tab names current.
#
#   scripts/install-timer.sh            # install and start
#   scripts/install-timer.sh --remove   # stop and remove
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ "${1:-}" == "--remove" ]]; then
  systemctl --user disable --now drover-titles.timer 2>/dev/null || true
  rm -f "$units/drover-titles.timer" "$units/drover-titles.service"
  systemctl --user daemon-reload
  echo "removed drover-titles.timer"
  exit 0
fi

mkdir -p "$units"
sed "s|PLUGIN_ROOT|$root|g" "$root/systemd/drover-titles.service" > "$units/drover-titles.service"
cp "$root/systemd/drover-titles.timer" "$units/drover-titles.timer"
systemctl --user daemon-reload
systemctl --user enable --now drover-titles.timer
systemctl --user list-timers drover-titles.timer --no-pager
