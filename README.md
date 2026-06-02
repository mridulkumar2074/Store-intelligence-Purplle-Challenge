# STORE INTELLIGENCE-Purplle Challenge

End-to-end CCTV analytics pipeline for the Brigade Road, Bangalore store.  
Converts raw camera footage into live business metrics via a containerised REST API.

**North Star**: Offline Store Conversion Rate = unique purchasing visitors / total unique visitors

---

## Live Demo

> **Base URL:** `https://store-intelligence-purplle-challenge.onrender.com`  
> **Interactive Docs:** [https://store-intelligence-purplle-challenge.onrender.com/docs](https://store-intelligence-purplle-challenge.onrender.com/docs)

---

## Sample API Outputs (Live — Brigade Road, Bangalore)

### Output 1 — Health Check `/health`
> Confirms the system is running, database is connected, and the store feed is active.

```
GET https://store-intelligence-purplle-challenge.onrender.com/health
```

```json
{
  "status": "OK",                          // <-- System is fully running
  "version": "1.0.0",
  "uptime_seconds": 210.8,                 // <-- Running for 3.5 minutes
  "stores": [
    {
      "store_id": "STORE_BLR_002",         // <-- Brigade Road, Bangalore store
      "last_event_at": "2026-06-01T21:40:43Z",
      "events_last_hour": 819,             // <-- 819 visitor events loaded
      "status": "OK"                       // <-- Feed is live, not stale
    }
  ],
  "db_ok": true                            // <-- Database connected and working
}
```

---

### Output 2 — Store Metrics `/stores/STORE_BLR_002/metrics`
> Today's key business numbers: visitors, revenue, conversion rate, queue depth.

```
GET https://store-intelligence-purplle-challenge.onrender.com/stores/STORE_BLR_002/metrics
```

```json
{
  "store_id": "STORE_BLR_002",
  "as_of": "2026-06-01T10:56:38Z",
  "unique_visitors": 75,                   // <-- 75 customers walked in today
  "conversion_rate": 0.3333,              // <-- 33.3% bought something (1 in 3)
  "avg_dwell_ms": 61587.5,               // <-- Average 61 seconds spent per zone
  "zone_dwell": [
    { "zone_id": "FRAGRANCE",  "avg_dwell_ms": 64821.9, "visit_count": 37 },  // <-- Most visited zone
    { "zone_id": "MAYBELLINE", "avg_dwell_ms": 53510.4, "visit_count": 36 },
    { "zone_id": "MINIMALIST", "avg_dwell_ms": 80634.7, "visit_count": 21 },  // <-- Longest dwell time
    { "zone_id": "ALPS",       "avg_dwell_ms": 62321.2, "visit_count": 34 },
    { "zone_id": "FOXTALE",    "avg_dwell_ms": 76061.7, "visit_count": 27 }
  ],
  "current_queue_depth": 6,              // <-- 6 people waiting at billing right now
  "abandonment_rate": 0.0741,            // <-- 7.4% left the queue without buying
  "total_transactions": 24,              // <-- 24 purchases completed today
  "total_revenue_inr": 34331.71          // <-- Rs. 34,331 revenue today
}
```

---

### Output 3 — Conversion Funnel `/stores/STORE_BLR_002/funnel`
> Shows exactly where customers drop off — from walking in to making a purchase.

```
GET https://store-intelligence-purplle-challenge.onrender.com/stores/STORE_BLR_002/funnel
```

```json
{
  "store_id": "STORE_BLR_002",
  "stages": [
    {
      "stage": "Entry",            // STAGE 1: Walked into the store
      "count": 75,                 // <-- 75 customers entered
      "drop_off_pct": 0.0          // <-- Nobody left immediately
    },
    {
      "stage": "Zone Visit",       // STAGE 2: Browsed at least one shelf
      "count": 75,                 // <-- All 75 browsed products
      "drop_off_pct": 0.0          // <-- 0% drop-off here
    },
    {
      "stage": "Billing Queue",    // STAGE 3: Went to the billing counter
      "count": 27,                 // <-- Only 27 out of 75 went to pay
      "drop_off_pct": 64.0         // <-- 64% browsed but NEVER went to billing
    },
    {
      "stage": "Purchase",         // STAGE 4: Actually completed the payment
      "count": 25,                 // <-- 25 customers paid
      "drop_off_pct": 7.41         // <-- 2 people left the queue without paying
    }
  ],
  "conversion_rate": 0.3333        // <-- 25 buyers / 75 visitors = 33.3% conversion
}
```

```
Funnel Visualization:

  75 Entered      [===================] 100%
  75 Browsed      [===================] 100%
  27 At Billing   [=======            ]  36%   <-- BIG DROP: 48 people never tried to buy
  25 Purchased    [======             ]  33%
```

---

### Output 4 — Zone Heatmap `/stores/STORE_BLR_002/heatmap`
> Which sections of the store get the most foot traffic and customer attention.

```
GET https://store-intelligence-purplle-challenge.onrender.com/stores/STORE_BLR_002/heatmap
```

```json
{
  "store_id": "STORE_BLR_002",
  "zones": [
    {
      "zone_id": "FRAGRANCE",       // <-- HOTTEST ZONE in the entire store
      "visit_frequency": 37,        // <-- 37 customers visited
      "avg_dwell_ms": 64821.9,      // <-- Spent ~65 seconds on average
      "normalised_score": 100.0,    // <-- Score 100/100 (highest)
      "data_confidence": true       // <-- 20+ sessions, high confidence
    },
    {
      "zone_id": "MAYBELLINE",
      "visit_frequency": 36,        // <-- 2nd busiest
      "normalised_score": 97.3
    },
    {
      "zone_id": "ALPS",
      "visit_frequency": 34,
      "normalised_score": 91.9
    },
    {
      "zone_id": "MINIMALIST",
      "visit_frequency": 21,
      "avg_dwell_ms": 83217.9,      // <-- Longest dwell: 83 seconds! High interest
      "normalised_score": 56.8
    },
    {
      "zone_id": "MAKEUP_UNIT",
      "visit_frequency": 10,
      "normalised_score": 27.0      // <-- COLDEST ZONE — needs attention
    }
  ]
}
```

```
Zone Heat Map (score out of 100):

  FRAGRANCE    ████████████████████  100
  MAYBELLINE   ███████████████████▌   97
  ALPS         ██████████████████▌    92
  FOH          █████████████████▏     87
  LAKME        ████████████████▊      84
  AQUALOGICA   ████████████████▏      81
  MINIMALIST   ███████████▍           57  <-- Low visits but HIGH dwell
  MAKEUP_UNIT  █████▍                 27  <-- Needs store promotion
```

---

### Output 5 — Active Anomalies `/stores/STORE_BLR_002/anomalies`
> Real-time alerts the store manager should act on RIGHT NOW.

```
GET https://store-intelligence-purplle-challenge.onrender.com/stores/STORE_BLR_002/anomalies
```

```json
{
  "store_id": "STORE_BLR_002",
  "active_anomalies": [
    {
      "anomaly_id": "2b71f1ad-7ae3-4995-8008-7f773236442b",
      "anomaly_type": "BILLING_QUEUE_SPIKE",   // <-- Queue is getting too long
      "severity": "WARN",                       // <-- WARNING level (CRITICAL = 8+ people)
      "detected_at": "2026-06-01T10:56:39Z",
      "description": "Billing queue depth is 6 — approaching threshold.",
                                                // ^^ 6 people waiting right now
      "suggested_action": "Monitor queue; consider deploying an additional cashier if depth increases.",
                                                // ^^ System tells manager what to DO
      "metadata": {
        "queue_depth": 6                        // <-- Exact queue count
      }
    }
  ]
}
```

> **Anomaly Types the system detects:**
> - `BILLING_QUEUE_SPIKE` — Queue too long, customers may abandon
> - `CONVERSION_DROP` — Today's sales rate worse than 7-day average
> - `DEAD_ZONE` — A shelf area with no visitors for 30+ minutes

---

## Quick Start (5 commands)

```bash
# 1. Clone / enter the project
cd /path/to/Purpelle

# 2. Generate sample events from the real POS data (seeds the API for demo)
python sample_events/generate_sample.py

# 3. Start the Intelligence API
docker compose up --build -d

# 4. Verify the API is live
curl http://localhost:8000/stores/STORE_BLR_002/metrics

# 5. (Optional) Open the live terminal dashboard
pip install rich httpx && python dashboard/live_dashboard.py
```

The API is available at **http://localhost:8000** and serves data immediately from the pre-generated sample events.

---

## Running the Detection Pipeline

The pipeline processes the actual CCTV footage and emits structured events:

```bash
# Install pipeline dependencies
pip install -r requirements-pipeline.txt

# Quick demo: 2 minutes of entry camera only (~5 minutes on CPU)
python -m pipeline.detect --demo --api-url http://localhost:8000

# Full run: all 5 cameras (40-65 minutes on CPU, skip every 5th frame)
python -m pipeline.detect \
  --clips-dir    "CCTV Footage" \
  --store-layout data/store_layout.json \
  --output-dir   sample_events \
  --api-url      http://localhost:8000 \
  --skip-frames  5 \
  --clip-start   2026-04-10T12:00:00Z

# Or using the shell script
bash pipeline/run.sh --demo
bash pipeline/run.sh                    # full run
```

Output is written to `sample_events/events.jsonl` and simultaneously POSTed to the API.

### Using Docker for the pipeline

```bash
# Build and run pipeline against the local footage
docker compose --profile pipeline up pipeline
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events/ingest` | Ingest up to 500 events (idempotent by event_id) |
| `GET` | `/stores/{id}/metrics` | Visitors, conversion rate, dwell, queue, abandonment |
| `GET` | `/stores/{id}/funnel` | Entry → Zone Visit → Billing → Purchase with drop-off % |
| `GET` | `/stores/{id}/heatmap` | Zone visit frequency and dwell, normalised 0-100 |
| `GET` | `/stores/{id}/anomalies` | Active anomalies with severity and suggested actions |
| `GET` | `/health` | Service status, per-store last-event timestamps |

**Store ID**: `STORE_BLR_002` (Brigade Road, Bangalore)

### Example API calls

```bash
# Real-time metrics
curl http://localhost:8000/stores/STORE_BLR_002/metrics | python -m json.tool

# Conversion funnel
curl http://localhost:8000/stores/STORE_BLR_002/funnel

# Zone heatmap
curl http://localhost:8000/stores/STORE_BLR_002/heatmap

# Active anomalies
curl http://localhost:8000/stores/STORE_BLR_002/anomalies

# Health check
curl http://localhost:8000/health

# Ingest custom events
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"events": [{"event_id":"...","store_id":"STORE_BLR_002",...}]}'
```

Interactive API docs: **http://localhost:8000/docs**

---

## Running Tests

```bash
# Install test dependencies
pip install -r requirements.txt

# Run all tests with coverage
pytest --cov=app --cov=pipeline --cov-report=term-missing

# Run specific test file
pytest tests/test_pipeline.py -v
pytest tests/test_metrics.py -v
```

---

## Project Structure

```
.
├── CCTV Footage/           # Input: 5 camera MP4 files (CAM 1-5.mp4)
├── app/
│   ├── main.py             # FastAPI entrypoint + structured logging
│   ├── models.py           # Pydantic event + response schemas
│   ├── database.py         # SQLite (aiosqlite + SQLAlchemy async)
│   ├── ingestion.py        # Idempotent event ingest
│   ├── metrics.py          # Real-time store metrics
│   ├── funnel.py           # Session-based conversion funnel
│   ├── heatmap.py          # Zone visit heatmap
│   ├── anomalies.py        # Anomaly detection engine
│   └── health.py           # Health check endpoint
├── pipeline/
│   ├── detect.py           # Main YOLOv8 + tracking script
│   ├── tracker.py          # IoU tracker (ByteTrack-inspired)
│   ├── zone_classifier.py  # Frame position → zone ID mapping
│   ├── staff_detector.py   # Uniform color-based staff detection
│   ├── emit.py             # Structured event builder + JSONL/API emitter
│   └── run.sh              # One-command pipeline runner
├── dashboard/
│   └── live_dashboard.py   # Rich terminal live metrics dashboard
├── sample_events/
│   ├── generate_sample.py  # Generate realistic events from POS data
│   └── events.jsonl        # Pre-generated events (seeded from real POS CSV)
├── data/
│   ├── store_layout.json   # Zone definitions, camera calibration
│   └── pos_transactions.csv# POS transactions (converted from sales CSV)
├── tests/
│   ├── test_pipeline.py    # IoU tracker, zone classifier, emitter tests
│   └── test_metrics.py     # Full API endpoint tests (all 6 endpoints)
├── docs/
│   ├── DESIGN.md           # Architecture + AI-assisted decisions
│   └── CHOICES.md          # 3 engineering decisions with full reasoning
├── docker-compose.yml
├── Dockerfile              # API service image
├── Dockerfile.pipeline     # Pipeline image (includes OpenCV + ultralytics)
├── requirements.txt        # API dependencies
├── requirements-pipeline.txt
└── README.md
```

---

## Architecture Decisions

See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture and AI-assisted design decisions.  
See [docs/CHOICES.md](docs/CHOICES.md) for the three key engineering choices with trade-off reasoning.

**Key choices**:
- **YOLOv8n** for person detection (CPU-friendly, ~12 FPS without GPU)
- **Custom IoU tracker** — ByteTrack-inspired, no GPU dependency
- **HSV color matching** for staff detection (Purplle purple uniforms)
- **Appearance histogram Re-ID** for re-entry detection (prevents double-counting)
- **SQLite WAL** as storage (zero-setup, sufficient for single-store scale)
- **5-minute POS correlation window** for conversion attribution

---

## Live Dashboard

The terminal dashboard polls the API every 5 seconds and displays:
- Store metrics (conversion rate, queue depth, revenue)
- Conversion funnel with stage drop-offs
- Zone heatmap (visit frequency + dwell time)
- Active anomalies with severity

```bash
# Start dashboard (API must be running)
python dashboard/live_dashboard.py --store STORE_BLR_002
```

---

## Camera Mapping

| Docker file | Camera ID | Coverage |
|---|---|---|
| CAM 1.mp4 | CAM_ENTRY_01 | Entry/Exit threshold — ENTRY/EXIT detection |
| CAM 2.mp4 | CAM_FLOOR_01 | Main floor (FOH, Fragrance, Makeup Units) |
| CAM 3.mp4 | CAM_FLOOR_02 | Back wall brands (GV, DermDoc, Minimalist, etc.) |
| CAM 4.mp4 | CAM_BILLING_01 | Cash counter — billing queue depth tracking |
| CAM 5.mp4 | CAM_FLOOR_03 | Front wall brands (Maybelline, Faces, Lakme, etc.) |
