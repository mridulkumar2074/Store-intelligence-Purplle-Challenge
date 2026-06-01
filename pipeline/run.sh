#!/usr/bin/env bash
# One command to process all CCTV clips and emit events to the API.
# Usage: bash pipeline/run.sh [--demo] [--api-url http://localhost:8000]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

API_URL="${API_URL:-http://localhost:8000}"
DEMO_FLAG=""
SKIP_FRAMES=5
MAX_FRAMES=0
CLIP_START="2026-04-10T12:00:00Z"

for arg in "$@"; do
  case "$arg" in
    --demo)        DEMO_FLAG="--demo"; SKIP_FRAMES=3 ;;
    --api-url=*)   API_URL="${arg#*=}" ;;
    --skip=*)      SKIP_FRAMES="${arg#*=}" ;;
  esac
done

echo "==> Installing pipeline dependencies…"
pip install -r requirements-pipeline.txt -q

echo "==> Running detection pipeline (skip-frames=$SKIP_FRAMES)…"
python -m pipeline.detect \
  --clips-dir    "CCTV Footage" \
  --store-layout "data/store_layout.json" \
  --output-dir   "sample_events" \
  --api-url      "$API_URL" \
  --skip-frames  "$SKIP_FRAMES" \
  --max-frames   "$MAX_FRAMES" \
  --clip-start   "$CLIP_START" \
  $DEMO_FLAG

echo "==> Events written to sample_events/events.jsonl"
echo "==> Ingesting into API at $API_URL …"
python - <<'PYEOF'
import json, sys, requests, os
path = os.path.join("sample_events", "events.jsonl")
api  = os.getenv("API_URL", "http://localhost:8000")
if not os.path.exists(path):
    print("No events file found.")
    sys.exit(0)
events = []
with open(path) as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))
BATCH = 500
ingested = 0
for i in range(0, len(events), BATCH):
    batch = events[i:i+BATCH]
    r = requests.post(f"{api}/events/ingest", json={"events": batch}, timeout=30)
    if r.ok:
        ingested += r.json().get("accepted", 0)
    else:
        print(f"Ingest error: {r.status_code} {r.text[:200]}")
print(f"Ingested {ingested}/{len(events)} events")
PYEOF
