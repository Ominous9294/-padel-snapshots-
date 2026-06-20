#!/usr/bin/env python3
"""
Padel tracker — CLOUD-SIDE occupancy compute.

Runs in the GitHub Action immediately after fetch-ci.sh, at capture time.
Computes the point-in-time occupancy row for the relevant (date, window) for
all 7 Playtomic clubs and appends it — idempotently — to occupancy_rows.json
in the repo root. The Action then commits that file alongside the snapshots.

WHY cloud-side: the morning ("prev night / advance") and afternoon ("same day")
metrics are point-in-time — their value is the timing. The raw {date}_{club}.json
files are keyed by date only and get overwritten on every fetch, so they can ONLY
ever be re-derived as "realised end-of-day". Computing here, at the moment of
capture, is the only way to preserve the advance-vs-realised distinction. No LLM,
no laptop dependency — if the laptop is offline for days, rows still accrue here
and the local render catches up on the next pull.

Window selection (override with env WINDOW=morning|afternoon and DATE=YYYY-MM-DD):
  - evening run  (UTC hour >= 17): MORNING window for TOMORROW  -> "advance"
  - midday  run  (UTC hour <  17): AFTERNOON window for TODAY   -> "same day"

Idempotent: a given (date, club_id, window) is written once (first capture wins),
so re-runs and the manual workflow_dispatch never overwrite a real-time capture
or duplicate a row.

Usage:
    python3 compute_rows.py [--snapdir DIR] [--rows PATH] [--window W] [--date D]
Prints a one-line JSON summary to stdout.
"""
import argparse, glob, json, os, sys
from datetime import datetime, timezone, timedelta

# ---- Club config — MUST match the live tasks / catchup.py -----------------
# id: (courts, weekday_open, weekday_close, weekend_open, weekend_close, indoor)
CLUBS = {
    "harrogate-spa":          (2, 7, 22, 7, 19, True),
    "surge-harrogate":        (6, 6, 22, 8, 20, True),
    "city-padel-exeter":      (6, 6, 23, 6, 23, True),
    "centre-court-st-helens": (5, 7, 22, 7, 21, False),
    "wetherby":               (6, 6, 22, 6, 22, False),
    "padelhub-southampton":   (5, 8, 23, 8, 22, True),
    "east-dorset":            (3, 8, 21, 8, 21, False),
}
MORNING_END = 13   # morning window 06:00–13:00
MORNING_FLOOR = 6
AFTERNOON_START = 13  # afternoon window 13:00–23:00
AFTERNOON_CEIL = 23

LONDON = timezone(timedelta(hours=1))  # BST; snapshot_iso offset for display


def london_now():
    return datetime.now(timezone.utc).astimezone(LONDON)


def opening(club, d):
    courts, wo, wc, weo, wec, indoor = CLUBS[club]
    weekend = d.weekday() >= 5
    return courts, (weo if weekend else wo), (wec if weekend else wc), indoor


def count_free_hours(data, lo, hi):
    """Unique 60-min start hours that are FREE (present in the slots) in [lo, hi)."""
    free = 0
    if isinstance(data, list) and data:
        for res in data:
            hours = set()
            for s in res.get("slots", []):
                t = s.get("start_time", "")
                if len(t) >= 5 and t[3:5] == "00":
                    h = int(t[:2])
                    if lo <= h < hi:
                        hours.add(h)
            free += len(hours)
    # NOTE: empty list [] -> free stays 0 -> fully booked (occ 100). Per spec.
    return free


def compute(club, d, window, snap_path):
    courts, open_h, close_h, indoor = opening(club, d)
    with open(snap_path) as fh:
        data = json.load(fh)
    if window == "morning":
        lo = max(open_h, MORNING_FLOOR)
        hi = MORNING_END
    else:  # afternoon
        lo = AFTERNOON_START
        hi = min(close_h, AFTERNOON_CEIL)
    win_hours = max(hi - lo, 0)
    capacity = win_hours * courts
    free = count_free_hours(data, lo, hi)
    booked = capacity - free
    occ = round(100 * booked / capacity, 1) if capacity else None
    return booked, capacity, occ, free, indoor


def load_events(snapdir, club, d):
    """Pull social_events / counts from {date}_{club}_events.json if present."""
    p = os.path.join(snapdir, f"{d}_{club}_events.json")
    if not os.path.exists(p):
        return None, None, None
    try:
        e = json.load(open(p))
        return e.get("social_events"), e.get("tournament_count"), e.get("academy_count")
    except Exception:
        return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapdir", default=os.path.join(os.getcwd(), "snapshots"))
    ap.add_argument("--rows", default=os.path.join(os.getcwd(), "occupancy_rows.json"))
    ap.add_argument("--window", default=os.environ.get("WINDOW"))
    ap.add_argument("--date", default=os.environ.get("DATE"))
    args = ap.parse_args()

    now = london_now()
    # Decide window + target date if not explicitly given.
    if args.window in ("morning", "afternoon") and args.date:
        window, target = args.window, args.date
    else:
        utc_hour = datetime.now(timezone.utc).hour
        if utc_hour >= 17:                       # evening run
            window = "morning"
            target = (now + timedelta(days=1)).date().isoformat()
        else:                                    # midday run
            window = "afternoon"
            target = now.date().isoformat()
    label = "23:30 prev night" if window == "morning" else "12:30 same day"

    # Load existing rows (the committed history). Create if absent.
    rows = []
    if os.path.exists(args.rows):
        try:
            rows = json.load(open(args.rows))
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []
    have = {(r.get("date"), r.get("club_id"), r.get("window")) for r in rows}

    td = datetime.strptime(target, "%Y-%m-%d").date()
    added, missing, fully_booked = [], [], []
    for club in CLUBS:
        key = (target, club, window)
        if key in have:                          # first capture wins — never overwrite
            continue
        snap = os.path.join(args.snapdir, f"{target}_{club}.json")
        if not os.path.exists(snap):
            missing.append(club)
            continue
        try:
            booked, capacity, occ, free, indoor = compute(club, td, window, snap)
        except Exception as e:
            missing.append(f"{club}(parse:{e})")
            continue
        if occ == 100.0:
            fully_booked.append(club)
        soc, tour, acad = load_events(args.snapdir, club, target)
        rows.append({
            "club_id": club,
            "date": target,
            "window": window,
            "snapshot_iso": now.isoformat(),
            "snapshot_label": label,
            "booked_hours": booked,
            "capacity_hours": capacity,
            "occupancy_pct": occ,
            "free_slots_count": free,
            "indoor_booked": booked if indoor else 0,
            "outdoor_booked": 0 if indoor else booked,
            "social_events": soc,
            "tournament_count": tour,
            "academy_count": acad,
            "source": "cloud-compute",
        })
        have.add(key)
        added.append(club)

    if added:
        tmp = args.rows + ".partial"
        with open(tmp, "w") as fh:
            json.dump(rows, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, args.rows)

    print(json.dumps({
        "window": window, "date": target, "rows_added": len(added),
        "added": added, "missing": missing, "fully_booked": fully_booked,
        "total_rows": len(rows),
    }))


if __name__ == "__main__":
    main()
