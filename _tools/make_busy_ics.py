#!/usr/bin/env python3
"""Build a busy-only .ics from a CalDAV calendar.

Every event in the window is expanded (recurrences, exceptions), converted to
UTC, stripped of everything except its time span and busy status, and
overlapping blocks of the same status are merged. The output contains no
titles, locations, attendees, UIDs or recurrence rules.

Output is deterministic: if the calendar hasn't changed, the file doesn't
either, so the cron wrapper only commits real changes.

Requires: pip install caldav icalendar recurring-ical-events
"""

import argparse
import datetime as dt
import hashlib
import inspect
import os
import sys
import tempfile

import caldav
import recurring_ical_events
from icalendar import Calendar, Event

UTC = dt.timezone.utc

# Exchange busy status (X-MICROSOFT-CDO-BUSYSTATUS) -> label in the feed.
# FREE is dropped entirely; anything unknown counts as busy.
STATUS_LABEL = {"OOF": "Away"}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="CalDAV server URL, e.g. http://localhost:1080/users/you@utu.fi/calendar/")
    p.add_argument("--user", required=True)
    p.add_argument("--password-file", help="file containing the password (chmod 600); default: $CALDAV_PASSWORD")
    p.add_argument("--calendar", help="calendar name or URL; default: the URL itself if it is a calendar, else the first one found")
    p.add_argument("--out", required=True, help="output .ics path")
    p.add_argument("--past-days", type=int, default=28)
    p.add_argument("--future-days", type=int, default=182)
    p.add_argument("--fetch-all", action="store_true",
                   help="download every event instead of a server-side time-range search "
                        "(use if recurring series are missing from the output)")
    return p.parse_args()


def get_password(args):
    if args.password_file:
        with open(args.password_file) as f:
            return f.read().strip()
    pw = os.environ.get("CALDAV_PASSWORD")
    if pw is None:
        sys.exit("No password: use --password-file or set CALDAV_PASSWORD")
    return pw


def open_calendar(args):
    kw = dict(url=args.url, username=args.user, password=get_password(args))
    # newer caldav versions refuse plain http unless told otherwise (local DavMail is http)
    if "require_tls" in inspect.signature(caldav.DAVClient.__init__).parameters:
        kw["require_tls"] = args.url.startswith("https")
    client = caldav.DAVClient(**kw)

    if args.calendar and args.calendar.startswith("http"):
        return client.calendar(url=args.calendar)
    cals = client.principal().calendars()
    if not cals:
        sys.exit("No calendars found")
    if args.calendar:
        for c in cals:
            if c.get_display_name() == args.calendar:
                return c
        sys.exit(f"Calendar {args.calendar!r} not found; have: {[c.get_display_name() for c in cals]}")
    return cals[0]


def to_utc(v):
    """datetime -> aware UTC datetime; date (all-day) stays a date."""
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:  # floating time: interpret in the local zone of the machine
            v = v.astimezone()
        return v.astimezone(UTC)
    return v


def busy_label(ev):
    if str(ev.get("STATUS", "")).upper() == "CANCELLED":
        return None
    ms = str(ev.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper()
    if ms == "FREE":
        return None
    if not ms and str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
        return None
    if ms in STATUS_LABEL:
        return STATUS_LABEL[ms]
    return STATUS_LABEL.get(ms, "Busy")


def collect_blocks(cal, start, end, fetch_all):
    if fetch_all:
        objs = cal.events()
    else:
        objs = cal.search(event=True, start=start, end=end)
    blocks = []
    for obj in objs:
        ical = Calendar.from_ical(obj.data)
        for ev in recurring_ical_events.of(ical).between(start, end):
            label = busy_label(ev)
            if label is None:
                continue
            s = to_utc(ev["DTSTART"].dt)
            e = to_utc(ev["DTEND"].dt) if "DTEND" in ev else None
            if e is None:  # DURATION or nothing
                e = s + (ev["DURATION"].dt if "DURATION" in ev else dt.timedelta(days=1) if not isinstance(s, dt.datetime) else dt.timedelta(0))
            if e <= s:
                continue
            blocks.append((isinstance(s, dt.datetime), label, s, e))
    return blocks


def merge(blocks):
    """Merge overlapping/adjacent blocks with the same kind and label."""
    out = []
    for timed, label, s, e in sorted(blocks, key=lambda b: (b[0], b[1], b[2], b[3])):
        if out and out[-1][0] == timed and out[-1][1] == label and s <= out[-1][3]:
            if e > out[-1][3]:
                out[-1] = (timed, label, out[-1][2], e)
        else:
            out.append((timed, label, s, e))
    return sorted(out, key=lambda b: (str(b[2]), b[1]))


def build_ics(blocks):
    cal = Calendar()
    cal.add("prodid", "-//fginter//busy-ics//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Filip Ginter — busy")
    for timed, label, s, e in blocks:
        ev = Event()
        key = f"{s.isoformat()}|{e.isoformat()}|{label}"
        ev.add("uid", hashlib.sha1(key.encode()).hexdigest()[:20] + "@busy")
        # fixed DTSTAMP keeps the file byte-identical between runs
        ev.add("dtstamp", dt.datetime(2000, 1, 1, tzinfo=UTC))
        ev.add("dtstart", s)
        ev.add("dtend", e)
        ev.add("summary", label)
        ev.add("transp", "OPAQUE")
        cal.add_component(ev)
    return cal.to_ical()


def main():
    args = parse_args()
    now = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = (now - dt.timedelta(days=args.past_days)).replace(hour=0)
    end = (now + dt.timedelta(days=args.future_days)).replace(hour=0)

    cal = open_calendar(args)
    blocks = merge(collect_blocks(cal, start, end, args.fetch_all))
    data = build_ics(blocks)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    with tempfile.NamedTemporaryFile("wb", dir=out_dir, delete=False) as f:
        f.write(data)
    os.replace(f.name, args.out)
    os.chmod(args.out, 0o644)
    print(f"{len(blocks)} blocks -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
