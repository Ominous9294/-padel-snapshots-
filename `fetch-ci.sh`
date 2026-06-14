#!/usr/bin/env bash
# Padel Tracker — Playtomic availability fetcher (CLOUD / GitHub Actions version)
# Ubuntu-runner port of the local fetch.sh. GNU coreutils (date -d, stat -c).
# Writes raw JSON to ./snapshots and ./manifest.json in the repo root.
# Zero external dependencies beyond curl + python3 (both on ubuntu-latest).

set -euo pipefail
IFS=$'\n\t'

# Repo root by default; in Actions this is $GITHUB_WORKSPACE (the checkout).
BASE_DIR="${BASE_DIR:-$(pwd)}"
SNAP_DIR="$BASE_DIR/snapshots"
mkdir -p "$SNAP_DIR"

# Tenant IDs — hardcoded literals from the tracking set, never user input.
CLUBS=(
  "harrogate-spa:d6d04c01-6101-455f-8968-8de9f75bf384"
  "surge-harrogate:18fda907-f989-4d40-b124-b8bf98ecbbd2"
  "city-padel-exeter:3198f517-efdd-44ff-94cc-50dd13491da0"
  "centre-court-st-helens:35c5611d-7146-48f0-bc33-06b5f19be611"
  "wetherby:d009033b-b7ff-4a4a-a935-36b2561fbd6f"
  "padelhub-southampton:ea27a502-a158-4079-8681-63bd6d091f45"
  "east-dorset:da25ad46-c6d2-4266-b9da-2a00863b1919"
)

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }   # -> stdout, captured by Actions
log "Run started (cloud) — TZ=${TZ:-unset}"

# Dates in the workflow's TZ (set to Europe/London in the YAML).
TODAY=$(date "+%Y-%m-%d")
TOMORROW=$(date -d "+1 day" "+%Y-%m-%d")   # GNU date syntax (was -v+1d on macOS)

SUCCESS=0
FAILURE=0

for d in "$TODAY" "$TOMORROW"; do
  for entry in "${CLUBS[@]}"; do
    id="${entry%%:*}"
    tid="${entry##*:}"

    if ! [[ "$tid" =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$ ]]; then
      log "SKIP $id — tenant_id failed UUID validation"
      FAILURE=$((FAILURE+1))
      continue
    fi

    URL="https://api.playtomic.io/v1/availability?sport_id=PADEL&tenant_id=${tid}&local_start_min=${d}T00:00:00&local_start_max=${d}T23:59:59"
    OUT="$SNAP_DIR/${d}_${id}.json"
    TMP="${OUT}.partial"

    HTTP_CODE=$(curl \
      --proto "=https" \
      --tlsv1.2 \
      --max-time 20 \
      --retry 2 --retry-delay 3 \
      -sS \
      --user-agent "PadelGardenTracker/1.0" \
      -w "%{http_code}" \
      -o "$TMP" \
      "$URL" 2>&1 || echo "000")

    if [ "$HTTP_CODE" != "200" ]; then
      rm -f "$TMP"
      log "FAIL $d $id — HTTP $HTTP_CODE"
      FAILURE=$((FAILURE+1))
      continue
    fi

    if ! python3 -c "import json,sys; json.load(open('$TMP'))" 2>/dev/null; then
      rm -f "$TMP"
      log "FAIL $d $id — response was not valid JSON"
      FAILURE=$((FAILURE+1))
      continue
    fi

    mv "$TMP" "$OUT"
    bytes=$(stat -c%s "$OUT")   # GNU stat (was stat -f%z on macOS)
    log "OK   $d $id (${bytes} bytes)"
    SUCCESS=$((SUCCESS+1))
  done
done

# ---- Manifest (same shape the tracker already reads) ----------------------
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

log "Run complete — success=$SUCCESS failure=$FAILURE"

# Fail the job loudly only if EVERYTHING failed (network/endpoint down),
# so a single club outage doesn't spam you with red builds.
if [ "$SUCCESS" -eq 0 ]; then
  log "ERROR — zero successful fetches"
  exit 1
fi
