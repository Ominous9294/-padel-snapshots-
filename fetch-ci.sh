#!/usr/bin/env bash
# Padel Tracker — Playtomic availability + events fetcher (CLOUD / GitHub Actions version)
# Ubuntu-runner port. GNU coreutils (date -d, stat -c).
# Writes raw JSON to ./snapshots and ./manifest.json in the repo root.
# Captures SOCIAL EVENTS per club per day -> {date}_{id}_events.json:
#   - tournaments      (api.playtomic.io/v1/tournaments)
#   - academy classes  (api.playtomic.io/v1/lessons)
# Zero external dependencies beyond curl + python3.

set -euo pipefail
IFS=$'\n\t'

BASE_DIR="${BASE_DIR:-$(pwd)}"
SNAP_DIR="$BASE_DIR/snapshots"
mkdir -p "$SNAP_DIR"

CLUBS=(
  "harrogate-spa:d6d04c01-6101-455f-8968-8de9f75bf384:harrogate-spa-tennis-centre"
  "surge-harrogate:18fda907-f989-4d40-b124-b8bf98ecbbd2:surge-padel-harrogate"
  "city-padel-exeter:3198f517-efdd-44ff-94cc-50dd13491da0:city-padel-exeter"
  "centre-court-st-helens:35c5611d-7146-48f0-bc33-06b5f19be611:centre-court-padel"
  "wetherby:d009033b-b7ff-4a4a-a935-36b2561fbd6f:wetherby-padel-club"
  "padelhub-southampton:ea27a502-a158-4079-8681-63bd6d091f45:the-padel-hub-so16-southampton"
  "east-dorset:da25ad46-c6d2-4266-b9da-2a00863b1919:east-dorset-padel"
)

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
log "Run started (cloud) — TZ=${TZ:-unset}"

TODAY=$(date "+%Y-%m-%d")
TOMORROW=$(date -d "+1 day" "+%Y-%m-%d")

is_uuid() { [[ "$1" =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$ ]]; }
is_slug() { [[ "$1" =~ ^[a-z0-9-]+$ ]]; }

# Refreshed 2026-07-15 after Cloudflare 403'd every request under the previous
# Chrome-126 UA (all 14 availability + 14 events calls failed on Jul 15 midday).
# Chrome 138 is current at time of writing. Also: added `sleep 1` between requests
# so we look less like a bot burst (14 requests in <2s = classic fingerprint).
# Bump this UA again next time Cloudflare updates its rules.
BROWSER_UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

# ---- Availability fetch loop ----------------------------------------------
SUCCESS=0
FAILURE=0

for d in "$TODAY" "$TOMORROW"; do
  for entry in "${CLUBS[@]}"; do
    id="${entry%%:*}"
    rest="${entry#*:}"
    tid="${rest%%:*}"
    slug="${rest##*:}"

    if ! is_uuid "$tid"; then
      log "SKIP $id — tenant_id failed UUID validation"
      FAILURE=$((FAILURE+1))
      continue
    fi

    URL="https://api.playtomic.io/v1/availability?sport_id=PADEL&tenant_id=${tid}&local_start_min=${d}T00:00:00&local_start_max=${d}T23:59:59"
    OUT="$SNAP_DIR/${d}_${id}.json"
    TMP="${OUT}.partial"

    HTTP_CODE=$(curl \
      --proto "=https" --tlsv1.2 --max-time 20 --retry 2 --retry-delay 3 -sS \
      --user-agent "$BROWSER_UA" \
      -H "Accept: application/json, text/plain, */*" \
      -H "Accept-Language: en-GB,en;q=0.9" \
      -H "Origin: https://playtomic.com" \
      -H "Referer: https://playtomic.com/" \
      -w "%{http_code}" -o "$TMP" "$URL" 2>&1 || echo "000")

    if [ "$HTTP_CODE" != "200" ]; then
      rm -f "$TMP"
      log "FAIL $d $id — HTTP $HTTP_CODE"
      FAILURE=$((FAILURE+1))
      sleep 1
      continue
    fi

    if ! python3 -c "import json,sys; json.load(open('$TMP'))" 2>/dev/null; then
      rm -f "$TMP"
      log "FAIL $d $id — response was not valid JSON"
      FAILURE=$((FAILURE+1))
      sleep 1
      continue
    fi

    mv "$TMP" "$OUT"
    bytes=$(stat -c%s "$OUT")
    log "OK   $d $id (${bytes} bytes)"
    SUCCESS=$((SUCCESS+1))
    sleep 1
  done
done

# ---- Events fetch loop ----------------------------------------------------
EV_OK=0
EV_WARN=0

for entry in "${CLUBS[@]}"; do
  id="${entry%%:*}"
  rest="${entry#*:}"
  tid="${rest%%:*}"
  slug="${rest##*:}"

  is_uuid "$tid" || { log "SKIP events $id — bad tenant_id"; EV_WARN=$((EV_WARN+1)); continue; }
  is_slug "$slug" || { log "SKIP events $id — bad slug"; EV_WARN=$((EV_WARN+1)); continue; }

  TOUR_TMP="$SNAP_DIR/.${id}_tournaments.tmp"
  ACAD_TMP="$SNAP_DIR/.${id}_academy.tmp"
  TOUR_ARG="NONE"
  ACAD_ARG="NONE"

  # --- Tournaments (JSON API) ---
  TURL="https://api.playtomic.io/v1/tournaments?tenant_id=${tid}&local_start_min=${TODAY}T00:00:00&local_start_max=${TOMORROW}T23:59:59"
  T_CODE=$(curl --proto "=https" --tlsv1.2 --max-time 20 --retry 2 --retry-delay 3 -sS \
    --user-agent "$BROWSER_UA" \
    -H "Accept: application/json, text/plain, */*" \
    -H "Accept-Language: en-GB,en;q=0.9" \
    -H "Origin: https://playtomic.com" \
    -H "Referer: https://playtomic.com/" \
    -w "%{http_code}" -o "$TOUR_TMP" "$TURL" 2>&1 || echo "000")
  if [ "$T_CODE" = "200" ] && python3 -c "import json; json.load(open('$TOUR_TMP'))" 2>/dev/null; then
    TOUR_ARG="$TOUR_TMP"
  else
    rm -f "$TOUR_TMP"
    log "WARN events $id — tournaments fetch failed (HTTP $T_CODE)"
  fi
  sleep 1

  # --- Academy classes (JSON API — /v1/lessons, confirmed 2026-06-18) ---
  AURL="https://api.playtomic.io/v1/lessons?tenant_id=${tid}&size=500&sort=start_date,DESC"
  A_CODE=$(curl --proto "=https" --tlsv1.2 --max-time 20 --retry 2 --retry-delay 3 -sS \
    --user-agent "$BROWSER_UA" \
    -H "Accept: application/json, text/plain, */*" \
    -H "Accept-Language: en-GB,en;q=0.9" \
    -H "Origin: https://playtomic.com" \
    -H "Referer: https://playtomic.com/" \
    -w "%{http_code}" -o "$ACAD_TMP" "$AURL" 2>&1 || echo "000")
  if [ "$A_CODE" = "200" ] && python3 -c "import json; json.load(open('$ACAD_TMP'))" 2>/dev/null; then
    ACAD_ARG="$ACAD_TMP"
  else
    rm -f "$ACAD_TMP"
    log "WARN events $id — academy (lessons) fetch failed (HTTP $A_CODE)"
  fi
  sleep 1

  # --- Derive per-date counts and write event files ---
  if python3 - "$id" "$SNAP_DIR" "$TODAY" "$TOMORROW" "$TOUR_ARG" "$ACAD_ARG" <<'PY'
import json, os, sys
from datetime import datetime

club_id, snap_dir, today, tomorrow, tour_path, acad_path = sys.argv[1:7]

def load_list(path, extra_keys=()):
    if path == "NONE":
        return None
    try:
        data = json.load(open(path))
    except Exception:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("tournaments", "lessons", "classes", "data", "results", "items", *extra_keys):
            if isinstance(data.get(k), list):
                return data[k]
    return []

def item_start(t):
    for k in ("start_date", "start_datetime", "starts_at", "start", "from"):
        v = t.get(k)
        if isinstance(v, str):
            return v
    return ""

tours = load_list(tour_path)
lessons = load_list(acad_path)

now_iso = datetime.now().astimezone().isoformat()

for d in (today, tomorrow):
    t_count = None if tours is None else sum(
        1 for t in tours if isinstance(t, dict) and item_start(t).startswith(d)
    )
    a_count = None if lessons is None else sum(
        1 for c in lessons
        if isinstance(c, dict)
        and item_start(c).startswith(d)
        and str(c.get("tournament_status", "")).upper() != "CANCELLED"
    )
    social = None if (t_count is None and a_count is None) else (t_count or 0) + (a_count or 0)

    rec = {
        "club_id": club_id, "date": d, "fetched_iso": now_iso,
        "tournament_count": t_count, "academy_count": a_count, "social_events": social,
        "tournament_ok": tours is not None, "academy_ok": lessons is not None,
    }
    out = os.path.join(snap_dir, f"{d}_{club_id}_events.json")
    tmp = out + ".partial"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, out)
PY
  then
    rm -f "$TOUR_TMP" "$ACAD_TMP"
    log "OK   events $id (tournaments=$([ "$TOUR_ARG" != NONE ] && echo y || echo n) academy=$([ "$ACAD_ARG" != NONE ] && echo y || echo n))"
    EV_OK=$((EV_OK+1))
  else
    rm -f "$TOUR_TMP" "$ACAD_TMP"
    log "WARN events $id — could not write event files"
    EV_WARN=$((EV_WARN+1))
  fi
done

# ---- Manifest ------------------------------------------------------------
python3 - "$SNAP_DIR" "$BASE_DIR/manifest.json" <<'PY'
import json, os, sys, glob
from datetime import datetime, timezone
snap_dir, manifest_path = sys.argv[1], sys.argv[2]
files = []
for f in sorted(glob.glob(os.path.join(snap_dir, "*.json"))):
    st = os.stat(f)
    files.append({
        "file": os.path.basename(f),
        "size_bytes": st.st_size,
        "modified_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    })
with open(manifest_path, "w") as out:
    json.dump({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "snapshot_count": len(files),
        "snapshots": files,
    }, out, indent=2)
PY

log "Run complete — availability success=$SUCCESS failure=$FAILURE | events ok=$EV_OK warn=$EV_WARN"

if [ "$SUCCESS" -eq 0 ]; then
  log "ERROR — zero successful fetches"
  exit 1
fi
