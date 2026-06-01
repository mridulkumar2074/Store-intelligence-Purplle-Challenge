# Purplle Store Intelligence

End-to-end CCTV analytics pipeline for the Brigade Road, Bangalore store.  
Converts raw camera footage into live business metrics via a containerised REST API.

**North Star**: Offline Store Conversion Rate = unique purchasing visitors / total unique visitors

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
