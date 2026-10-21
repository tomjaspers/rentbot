#!/bin/sh
# Turn the laptop's OpenRent worker on or off. It shares the Neon database with GitHub Actions and
# takes turns with it via a database lock, so both can run at the same time safely.
#   scripts/laptop.sh on | off | status
set -eu
LABEL=com.tjaspers.rentbot.openrent
SRC="$(cd "$(dirname "$0")/.." && pwd)/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/rentbot-openrent.log"

case "${1:-status}" in
  on)
    cp "$SRC" "$DEST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$DEST"
    echo "on: scanning OpenRent every 10 min while the laptop is awake (log: $LOG)"
    ;;
  off)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$DEST"
    echo "off"
    ;;
  status)
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then echo "on"; else echo "off"; fi
    [ -f "$LOG" ] && tail -3 "$LOG"
    ;;
  *)
    echo "usage: $0 on|off|status" >&2
    exit 2
    ;;
esac
