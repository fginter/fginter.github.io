#!/bin/bash
# Hourly cron job: regenerate busy.ics from local CalDAV, push only if it changed.
#
# crontab -e:
#   7 * * * * $HOME/src/fginter.github.io/_tools/update_busy.sh >> $HOME/.cache/busy-ics.log 2>&1
#
# Settings come from ~/.config/busy-ics.env (chmod 600), e.g.:
#   CALDAV_URL=http://localhost:1080/users/figint@utu.fi/calendar/
#   CALDAV_USER=figint@utu.fi
#   CALDAV_PASSWORD=...
#   CALDAV_CALENDAR=          # optional: name or URL
#   PYTHON=/usr/bin/python3   # optional: python with caldav, icalendar, recurring-ical-events

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "${BUSY_ICS_ENV:-$HOME/.config/busy-ics.env}"
export CALDAV_PASSWORD

cd "$REPO"
git pull --quiet --rebase

$REPO/venv/bin/python3 _tools/make_busy_ics.py \
    --url "$CALDAV_URL" --user "$CALDAV_USER" \
    ${CALDAV_CALENDAR:+--calendar "$CALDAV_CALENDAR"} \
    --out busy.ics

exit 0

git add busy.ics
if git diff --cached --quiet; then
    exit 0
fi
git commit --quiet -m "Update busy.ics"
git push --quiet
echo "$(date -Is) pushed new busy.ics"
