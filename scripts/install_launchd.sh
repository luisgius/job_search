#!/usr/bin/env bash
# One-shot installer for the weekday-08:00 launchd job. Idempotent: re-running
# replaces the installed agent (paths moved? repo cloned elsewhere? run again).
#
#     bash scripts/install_launchd.sh
#
# Undo:
#     launchctl bootout "gui/$(id -u)/com.job-hunter.daily"
#     rm ~/Library/LaunchAgents/com.job-hunter.daily.plist

set -euo pipefail

if [ "$(uname)" != "Darwin" ]; then
    echo "launchd is macOS-only. On Linux, use the cron line in the README" >&2
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.job-hunter.daily"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"

mkdir -p "$AGENTS" "$REPO/output/logs"
# sed with | as the delimiter: $REPO contains slashes. The replacement side
# of s||| still treats &, \ and the delimiter as special, and the plist is
# XML where & must be &amp; — escape for both layers, in that order, so a
# repo under "My Repos" or "Sync & Docs" lands in the plist intact.
SED_REPO="$(printf '%s' "$REPO" | sed -e 's/&/\&amp;/g' -e 's/[|&\\]/\\&/g')"
sed "s|__REPO__|$SED_REPO|g" "$REPO/scripts/$LABEL.plist.template" >"$PLIST"

# Replace any previously loaded copy; the bootout failing just means there
# was none, which is fine. bootstrap straight after bootout can lose a race
# with the old instance still draining (errno 5, EIO) — retry briefly before
# calling it a real failure.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for attempt in 1 2 3; do
    if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
        break
    fi
    if [ "$attempt" = 3 ]; then
        echo "bootstrap failed three times — log out and back in, then rerun" >&2
        exit 1
    fi
    sleep 1
done

echo "Installed: $PLIST"
echo "Schedule:  weekdays 08:00 (fires once on wake if the Mac was asleep)"
echo
launchctl print "gui/$(id -u)/$LABEL" | sed -n '1,6p'
echo
echo "Fire a test run right now with:"
echo "    launchctl kickstart \"gui/$(id -u)/$LABEL\""
echo "then:  tail -f \"$REPO/output/logs/daily-$(date +%Y-%m-%d).log\""
