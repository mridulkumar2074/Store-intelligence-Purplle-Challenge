# System Architecture — Purplle Store Intelligence

## Overview

The system converts raw CCTV footage from the Brigade Road, Bangalore Purplle store into real-time business analytics. It is structured as two loosely-coupled stages:

1. **Detection Pipeline** — an offline (or near-real-time) batch process that reads video frames, runs person detection and tracking, and emits structured JSON events.
2. **Intelligence API** — a stateless FastAPI service that ingests those events, computes metrics, and serves queryable endpoints.

The North Star metric is **offline store conversion rate**: unique visitors who completed a POS purchase divided by total unique customer visitors in the session window.

---

## Component Architecture

```
CCTV Footage (5 MP4 files)
    │
    ▼
┌─────────────────────────────────┐
│  Detection Pipeline             │
│  ┌─────────┐  ┌──────────────┐ │
│  │ YOLOv8n │→ │ IoU Tracker  │ │
│  └─────────┘  └──────────────┘ │
│       │               │        │
│  ┌────▼────┐   ┌──────▼─────┐ │
│  │  Staff  │   │   Zone     │ │
│  │Detector │   │Classifier  │ │
│  └────┬────┘   └──────┬─────┘ │
│       └───────┬────────┘       │
│           ┌───▼────┐           │
│           │ Re-ID  │           │
│           │ Store  │           │
│           └───┬────┘           │
│           ┌───▼──────────┐     │
│           │ EventEmitter │     │
│           └───┬──────────┘     │
└───────────────┼────────────────┘
                │ JSONL + POST /events/ingest
                ▼
┌──────────────────────────────────┐
│  Intelligence API (FastAPI)      │
│  POST /events/ingest             │
│  GET  /stores/{id}/metrics       │
│  GET  /stores/{id}/funnel        │
│  GET  /stores/{id}/heatmap       │
│  GET  /stores/{id}/anomalies     │
│  GET  /health                    │
│              │                   │
│         SQLite (WAL)             │
└──────────────────────────────────┘
                │
                ▼
┌─────────────────────────┐
│  Live Dashboard (Rich)  │
│  Terminal metrics view  │
└─────────────────────────┘
```

---

## Detection Pipeline

### Stage 1: Person Detection

**Model**: YOLOv8n (nano variant). Chosen for its balance between CPU inference speed (~12 FPS on x86 without GPU) and detection accuracy (mAP50 ~53% on COCO). The nano model was sufficient given that we are detecting the `person` class only — not requiring fine-grained classification.

**Frame subsampling**: Configurable `--skip-frames` (default: 5) processes every 5th frame, giving an effective 3 FPS analysis rate from the 15 FPS source. This reduces CPU load by 5× with minimal loss to tracking quality since people move slowly in a retail environment.

**Resolution**: Frames are downscaled to 1280px width before inference. This keeps memory footprint low while preserving enough resolution for person detection.

### Stage 2: Tracking

**Tracker**: Custom two-pass IoU-based tracker (`pipeline/tracker.py`) inspired by ByteTrack. Two passes:
- Pass 1: Match high-confidence detections (>0.55) to confirmed tracks using Hungarian assignment
- Pass 2: Match low-confidence detections (0.10–0.55) to unmatched confirmed tracks — this handles partial occlusion cases without silently dropping detections

Tracks become confirmed after `min_confirm_age=3` consecutive detections. This suppresses false positives (e.g., reflections in glass). Lost tracks are retained for `max_lost_age=30` frames before being discarded — long enough to survive brief occlusions behind displays.

**Why not ByteTrack directly?** The `boxmot` library (which ships ByteTrack) requires CUDA for the full feature set. The custom tracker is pure Python + NumPy + SciPy, making it portable across CPU-only Docker environments.

### Stage 3: Entry / Exit Detection

The entry camera (CAM_ENTRY_01) defines a virtual **line** at `entry_line_y = 0.60` (normalized y-coordinate from top). Each tracked person's center y-coordinate is compared between consecutive frames:
- `prev_cy < line AND curr_cy >= line` → **ENTRY** (moving inward)
- `prev_cy >= line AND curr_cy < line` → **EXIT** (moving outward)

This directional line-crossing approach is robust to people who linger at the entrance without crossing — they do not generate false ENTRY/EXIT events.

### Stage 4: Staff Detection

Staff detection uses HSV color-range matching on the upper-body region of each bounding box. Purplle staff wear dark purple/magenta uniforms (HSV hue: 125–165°). If >20% of upper-body pixels fall in this range, `is_staff = true`. This heuristic is fast (no model required) and conservative — it defaults to `is_staff = false` on ambiguous cases, which is the safer direction (we prefer including a confused staff member in customer metrics over excluding real customers).

### Stage 5: Re-ID and Re-Entry Handling

When a visitor exits, their appearance histogram (64-dimensional HSV color histogram, L2-normalised) is stored in a `ReIDStore` with a 5-minute TTL. When a new confirmed track appears in the entry camera, its histogram is compared against all recently-exited visitors using cosine similarity. If similarity > 0.70, the same `visitor_id` is reused and a **REENTRY** event is emitted instead of a new ENTRY. This prevents re-entry inflation in the conversion funnel.

### Stage 6: Zone Classification

Each camera's `zone_map` in `store_layout.json` defines a list of rectangular regions, ordered by priority (first match wins). The normalized center of each tracked bounding box is tested against these regions to determine the zone. This approach is calibration-free and easy to update for store rearrangements.

**Edge cases handled**:
- **Group entry**: Each bounding box becomes its own track → 3 people entering together produce 3 ENTRY events
- **Partial occlusion**: Low-confidence detections still produce events (not suppressed) with accurate `confidence` field
- **Billing queue**: The billing camera counts all active tracks as queue depth
- **Empty periods**: The API returns valid zero values for all metrics when no events exist

---

## Intelligence API

### Data Model

Events are stored in a SQLite database (WAL mode) with indexes on `(store_id, timestamp)`, `visitor_id`, `event_type`, `is_staff`, and `zone_id`. This supports all real-time query patterns without an external data store.

POS transactions are loaded from `data/pos_transactions.csv` at startup and persisted in the `pos_transactions` table.

### Conversion Rate Computation

The system uses time-window correlation to link visitors to purchases. A visitor who was observed in the `CASH_COUNTER` zone in the **5-minute window before** a POS transaction timestamp is counted as a converted visitor. This threshold was chosen based on typical retail checkout times and maps directly to the problem statement specification.

### Funnel Logic

The funnel is session-based, using unique `visitor_id`s from ENTRY events. REENTRY events do not create new sessions — the visitor was already counted at their initial ENTRY. This ensures no double-counting across the Entry → Zone Visit → Billing Queue → Purchase stages.

### Anomaly Detection

Three anomaly types are actively monitored:
1. **BILLING_QUEUE_SPIKE**: Max queue depth in the last 30 minutes exceeds threshold (WARN at 5, CRITICAL at 8)
2. **CONVERSION_DROP**: Today's conversion rate is >30% below the 7-day rolling average
3. **DEAD_ZONE**: A zone that received visits today has had zero visits in the last 30 minutes

---

## AI-Assisted Decisions

### 1. ByteTrack Reimplementation

I consulted Claude to evaluate whether to use the `boxmot` library (which ships ByteTrack) or implement the core algorithm myself. The AI correctly identified that `boxmot` requires CUDA support for the full feature set and would create a non-trivial Docker image with GPU dependencies. I agreed with this analysis and implemented the two-pass IoU tracker manually. The AI also suggested using `scipy.optimize.linear_sum_assignment` for the Hungarian matching step — I adopted this since it's already in the NumPy/SciPy stack.

### 2. Conversion Rate Methodology

I asked Claude to evaluate two approaches for computing conversion rate: (1) direct POS-to-session correlation using billing zone presence, and (2) a probabilistic model based on session length and dwell time in billing zones. The AI recommended approach (1) as more defensible and consistent with the problem statement's explicit 5-minute window specification. I agreed — the probabilistic model would require more historical data to calibrate and introduces model risk without meaningful gain for this use case.

### 3. SQLite vs PostgreSQL

The AI initially suggested PostgreSQL for production robustness. I overrode this for the challenge submission because: (a) SQLite with WAL mode supports concurrent reads/writes adequately for a single-store deployment, (b) it eliminates a PostgreSQL service dependency in `docker-compose.yml`, and (c) it makes the acceptance gate simpler — `docker compose up` starts everything without a separate database initialization step. The tradeoff is horizontal scalability, which is documented in CHOICES.md.

---

## Production Considerations

- **Structured logging**: Every request emits a JSON log line with `trace_id`, `store_id`, `endpoint`, `latency_ms`, `event_count`, `status_code`.
- **Idempotency**: `POST /events/ingest` is safe to call multiple times with the same payload — duplicate `event_id`s are silently skipped.
- **Graceful degradation**: Database unavailable → HTTP 503 with structured JSON body. No raw stack traces in API responses.
- **Health endpoint**: `/health` provides `STALE_FEED` warnings when a store's last event is >10 minutes old.
- **Test coverage**: All API endpoints, edge cases (empty store, all-staff, zero purchases, re-entry), and pipeline components are covered.
