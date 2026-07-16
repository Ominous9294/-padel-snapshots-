#!/usr/bin/env python3
"""
Padel Tracker — Playtomic availability + events fetcher (CLOUD / GHA version)

Python + curl_cffi rewrite deployed 2026-07-16 after the previous curl-based
fetcher started getting 403'd by Cloudflare's TLS fingerprinting (JA3/JA4).
curl_cffi impersonates Chrome's TLS handshake at the byte level, defeating
that check while keeping the same tiny footprint as curl.

Writes:
  - snapshots/{date}_{id}.json         raw Playtomic availability
  - snapshots/{date}_{id}_events.json  tournament + academy counts
  - manifest.json                      index of all snapshot files

Requires: pip install curl_cffi
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curl_cffi import requests

BASE_DIR = Path(os.environ.get("BASE_DIR", os.getcwd()))
SNAP_DIR = BASE_DIR / "snapshots"
SNAP_DIR.mkdir(exist_ok=True)

# (id, tenant_id, slug) — hardcoded from the tracking set, never user input.
CLUBS = [
    ("harrogate-spa",           "d6d04c01-6101-455f-8968-8de9f75bf384", "harrogate-spa-tennis-centre"),
    ("surge-harrogate",         "18fda907-f989-4d40-b124-b8bf98ecbbd2", "surge-padel-harrogate"),
    ("city-padel-exeter",       "3198f517-efdd-44ff-94cc-50dd13491da0", "city-padel-exeter"),
    ("centre-court-st-helens",  "35c5611d-7146-48f0-bc33-06b5f19be611", "centre-court-padel"),
    ("wetherby",                "d009033b-b7ff-4a4a-a935-36b2561fbd6f", "wetherby-padel-club"),
    ("padelhub-southampton",    "ea27a502-a158-4079-8681-63bd6d091f45", "the-padel-hub-so16-southampton"),
    ("east-dorset",             "da25ad46-c6d2-4266-b9da-2a00863b1919", "east-dorset-padel"),
]

UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
SLUG_RE = re.compile(r"^[a-z0-9-]+$")

# "chrome" pins to curl_cffi's latest supported Chrome fingerprint. Bump to a
# specific version like "chrome131" only if Cloudflare rolls out new detection
# that requires an exact version match.
IMPERSONATE = "chrome"

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": "https://playtomic.com",
    "Referer": "https://playtomic.com/",
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def http_get(url: str, timeout: int = 20):
    """GET with Chrome TLS impersonation. Returns (status_code, body_bytes)."""
    try:
        r = requests.get(url, impersonate=IMPERSONATE, headers=BROWSER_HEADERS, timeout=timeout)
        return r.status_code, r.content
    except Exception as exc:
        log(f"    (network error: {exc})")
        return 0, b""


def parse_json_or_none(body: bytes):
    try:
        return json.loads(body)
    except Exception:
        return None


def coerce_list(data, extra_keys=()):
    """Playtomic responses are usually bare arrays but sometimes wrapped."""
    if data is None:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("tournaments", "lessons", "classes", "data", "results", "items", *extra_keys):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def item_start(item: dict) -> str:
    for k in ("start_date", "start_datetime", "starts_at", "start", "from"):
        v = item.get(k)
        if isinstance(v, str):
            return v
    return ""


def main() -> None:
    tz = os.environ.get("TZ", "unset")
    log(f"Run started (cloud, python+curl_cffi) — TZ={tz}")

    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # ---- Availability fetch loop --------------------------------------
    success = 0
    failure = 0

    for d in (today, tomorrow):
        for cid, tid, _slug in CLUBS:
            if not UUID_RE.match(tid):
                log(f"SKIP {cid} — tenant_id failed UUID validation")
                failure += 1
                continue

            url = (
                "https://api.playtomic.io/v1/availability"
                f"?sport_id=PADEL&tenant_id={tid}"
                f"&local_start_min={d}T00:00:00&local_start_max={d}T23:59:59"
            )
            out = SNAP_DIR / f"{d}_{cid}.json"
            tmp = SNAP_DIR / f"{d}_{cid}.json.partial"

            status, body = http_get(url)
            if status != 200:
                log(f"FAIL {d} {cid} — HTTP {status}")
                failure += 1
                time.sleep(1)
                continue
            if parse_json_or_none(body) is None:
                log(f"FAIL {d} {cid} — response was not valid JSON")
                failure += 1
                time.sleep(1)
                continue

            tmp.write_bytes(body)
            tmp.replace(out)
            log(f"OK   {d} {cid} ({len(body)} bytes)")
            success += 1
            time.sleep(1)

    # ---- Events fetch loop (tournaments + academy) ---------------------
    ev_ok = 0
    ev_warn = 0

    for cid, tid, slug in CLUBS:
        if not UUID_RE.match(tid):
            log(f"SKIP events {cid} — bad tenant_id"); ev_warn += 1; continue
        if not SLUG_RE.match(slug):
            log(f"SKIP events {cid} — bad slug"); ev_warn += 1; continue

        # Tournaments
        turl = (
            "https://api.playtomic.io/v1/tournaments"
            f"?tenant_id={tid}"
            f"&local_start_min={today}T00:00:00&local_start_max={tomorrow}T23:59:59"
        )
        t_status, t_body = http_get(turl)
        tours = coerce_list(parse_json_or_none(t_body)) if t_status == 200 else None
        if tours is None:
            log(f"WARN events {cid} — tournaments fetch failed (HTTP {t_status})")
        time.sleep(1)

        # Academy classes via /v1/lessons (date-range params are ignored server-side;
        # we filter client-side by start_date prefix).
        aurl = f"https://api.playtomic.io/v1/lessons?tenant_id={tid}&size=500&sort=start_date,DESC"
        a_status, a_body = http_get(aurl)
        lessons = coerce_list(parse_json_or_none(a_body)) if a_status == 200 else None
        if lessons is None:
            log(f"WARN events {cid} — academy (lessons) fetch failed (HTTP {a_status})")
        time.sleep(1)

        now_iso = datetime.now().astimezone().isoformat()
        try:
            for date_str in (today, tomorrow):
                t_count = None if tours is None else sum(
                    1 for t in tours if isinstance(t, dict) and item_start(t).startswith(date_str)
                )
                a_count = None if lessons is None else sum(
                    1 for c in lessons
                    if isinstance(c, dict)
                    and item_start(c).startswith(date_str)
                    and str(c.get("tournament_status", "")).upper() != "CANCELLED"
                )
                social = None if (t_count is None and a_count is None) else (t_count or 0) + (a_count or 0)

                rec = {
                    "club_id": cid, "date": date_str, "fetched_iso": now_iso,
                    "tournament_count": t_count, "academy_count": a_count, "social_events": social,
                    "tournament_ok": tours is not None, "academy_ok": lessons is not None,
                }
                out = SNAP_DIR / f"{date_str}_{cid}_events.json"
                tmp = SNAP_DIR / f"{date_str}_{cid}_events.json.partial"
                tmp.write_text(json.dumps(rec, separators=(",", ":")))
                tmp.replace(out)
            log(f"OK   events {cid} (tournaments={'y' if tours is not None else 'n'} academy={'y' if lessons is not None else 'n'})")
            ev_ok += 1
        except Exception as exc:
            log(f"WARN events {cid} — could not write event files: {exc}")
            ev_warn += 1

    # ---- Manifest ------------------------------------------------------
    files = []
    for f in sorted(SNAP_DIR.glob("*.json")):
        st = f.stat()
        files.append({
            "file": f.name,
            "size_bytes": st.st_size,
            "modified_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
    (BASE_DIR / "manifest.json").write_text(
        json.dumps({
            "generated_iso": datetime.now(timezone.utc).isoformat(),
            "snapshot_count": len(files),
            "snapshots": files,
        }, indent=2)
    )

    log(f"Run complete — availability success={success} failure={failure} | events ok={ev_ok} warn={ev_warn}")
    if success == 0:
        log("ERROR — zero successful fetches")
        sys.exit(1)


if __name__ == "__main__":
    main()
