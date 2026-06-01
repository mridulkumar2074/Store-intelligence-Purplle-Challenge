# Engineering Choices

This document explains three key design decisions made during the build, including the options considered, what AI suggested, what I chose, and why.

---

## Decision 1: Detection Model Selection

### The question
Which person detection model to use given the constraints: CPU-only Docker, 1080p retail CCTV footage, 15 FPS source, need for real-time processing.

### Options considered

| Model | mAP50 (COCO) | CPU FPS (1080p) | Notes |
|---|---|---|---|
| YOLOv8n | ~53% | ~12 FPS | Nano — fast, small |
| YOLOv8s | ~64% | ~6 FPS | Better accuracy, slower |
| YOLOv8m | ~73% | ~2 FPS | Too slow for CPU |
| RT-DETR | ~73% | ~1 FPS | Transformer, GPU needed |
| MediaPipe Pose | N/A | ~20 FPS | Person only, no OD bounding boxes |

### What AI suggested
Claude initially suggested YOLOv8s as a "balanced choice" citing better accuracy for handling partial occlusion in dense retail environments. It also proposed using a VLM (e.g., GPT-4V sampled at 0.5 FPS) to classify zones and detect staff by analyzing full frame patches — arguing this would eliminate the need for HSV-based staff detection.

### What I chose and why
**YOLOv8n with `--skip-frames=5`** (effective 3 FPS analysis rate).

I disagreed with YOLOv8s for two reasons:
1. At 6 FPS on CPU, YOLOv8s would require a 2× skip-frames setting to keep up with real-time input, eliminating its accuracy advantage.
2. The retail footage has relatively slow-moving subjects. A person browsing a shelf is in the same bounding box region for 10–15 seconds — we do not need sub-second precision to track them through zones.

I also rejected the VLM zone classification idea for the production pipeline. Calling GPT-4V at 0.5 FPS for a 20-minute video = 600 API calls per camera × 5 cameras = 3,000 API calls per recording session. The latency, cost, and dependency on an external API would make this unacceptable in a production retail deployment. I kept the VLM option documented as a potential enhancement for ambiguous zone boundaries, but the default path uses the coordinate-based zone map — which is deterministic, zero-cost, and easy to update.

**What actually happened**: YOLOv8n with `--skip-frames=5` processes the full 5-camera dataset in approximately 40 minutes on a CPU. With `--skip-frames=3` (5 FPS effective), it takes ~65 minutes but catches more fast-moving entries. For the demo mode (`--demo`), only the entry camera is processed at 3 FPS for the first 2 minutes, completing in under 5 minutes.

---

## Decision 2: Event Schema Design

### The question
How to design the event schema to support all analytics queries while keeping the pipeline's output format simple enough to validate and debug.

### Options considered

**Option A (chosen)**: Flat schema with a small `metadata` object for optional fields.
```json
{
  "event_id": "uuid",
  "store_id": "STORE_BLR_002",
  "visitor_id": "VIS_c8a2f1",
  "event_type": "ZONE_DWELL",
  "timestamp": "2026-04-10T14:22:10Z",
  "zone_id": "SKINCARE_BACK",
  "dwell_ms": 32000,
  "is_staff": false,
  "confidence": 0.88,
  "metadata": { "queue_depth": null, "sku_zone": "DERMDOC", "session_seq": 5 }
}
```

**Option B**: Separate event types with different schemas (ENTRY event has `entry_direction` field, BILLING_QUEUE_JOIN has `queue_depth` at top level, etc.).

**Option C**: One flat table with all possible fields, NULLs for irrelevant ones. Simple to store, verbose on the wire.

### What AI suggested
The AI suggested Option B — typed sub-schemas for each event type, arguing it produces "cleaner Pydantic models with no optional fields". It generated a draft with seven separate Pydantic models inheriting from a `BaseEvent`.

### What I chose and why
**Option A** — the problem statement's required schema. The challenge specifies an exact JSON structure with `metadata` as a nested object containing `queue_depth`, `sku_zone`, and `session_seq`. Deviating from this would fail the automated schema compliance checks.

Beyond compliance, Option A has a practical advantage: any downstream consumer (the API, test harness, or future analytics) only needs to know one schema. Option B's polymorphism would require the API ingest endpoint to inspect `event_type` and instantiate different models — complexity that pays off in larger systems but is unnecessary overhead here.

The AI's concern about optional fields is addressed by keeping `metadata.queue_depth` as `null` except for `BILLING_QUEUE_JOIN` events — a clearly documented convention, not a hidden surprise.

---

## Decision 3: API Storage and Scaling Architecture

### The question
Which database to use for the Intelligence API — SQLite, PostgreSQL, or an in-memory store with Redis for real-time queries.

### Options considered

| Option | Pros | Cons |
|---|---|---|
| SQLite (WAL mode) | Zero setup, works in single Docker container, simple backup | No horizontal scaling, write lock contention at very high event rates |
| PostgreSQL | Production-grade, horizontal read replicas, mature async driver | Requires separate service, adds setup complexity for reviewers |
| Redis + TimeSeries module | Sub-millisecond metric queries, built for time series | High memory usage, complex data model, license concerns (BUSL-2.1) |
| DuckDB | Excellent for analytics queries, columnar storage | Still experimental for high-frequency writes, no async Python driver |

### What AI suggested
Claude recommended PostgreSQL with `asyncpg`, citing production robustness and the ability to use `MATERIALIZED VIEW` for pre-computing metrics. It also suggested Redis as a caching layer for frequently-queried metrics.

### What I chose and why
**SQLite with WAL mode** for this submission.

I overrode the AI's recommendation for three specific reasons:

1. **Acceptance gate reliability**: The grading environment runs `docker compose up` on a clean machine. Adding a PostgreSQL service creates two failure modes (DB startup race, authentication) that SQLite avoids entirely. If the API container starts and `./data/store_intelligence.db` doesn't exist, SQLite creates it on first connection. PostgreSQL has no equivalent zero-config path.

2. **Scale mismatch**: The Brigade Bangalore store generates ~800–1,200 events per day of footage. At this rate, SQLite can handle the write load easily — WAL mode allows concurrent reads while a write is in progress, which is all we need for a single API worker serving analytics dashboards.

3. **Operational simplicity**: A single SQLite file is trivially backed up (`cp store_intelligence.db backup.db`) and inspectable (`sqlite3 store_intelligence.db .dump`). For a store operations team, this is more maintainable than a PostgreSQL instance.

**When I would change this decision**: If the system scales to all 40 Apex Retail stores sending events in real time (~40–48k events/hour at peak), the write bottleneck in SQLite's WAL mode would become real. I would migrate to PostgreSQL with a connection pool (e.g., `pgBouncer`), partition the `events` table by `(store_id, date)`, and add a Redis layer for the `current_queue_depth` metric (which is the only one requiring sub-second freshness). The event schema and all business logic would remain unchanged — only the database backend and connection layer would be swapped.

---

## AI Disagreements Summary

| Decision | AI Suggested | I Chose | Reason for Override |
|---|---|---|---|
| Detection model | YOLOv8s | YOLOv8n + skip-frames | CPU speed; accuracy advantage disappears at required skip rate |
| Zone classification | VLM (GPT-4V) | Coordinate-based zone map | Cost, latency, external API dependency unacceptable in production |
| Event schema | Typed sub-schemas per event type | Single flat schema with metadata | Required by problem spec; complexity not justified at this scale |
| Storage | PostgreSQL + Redis cache | SQLite WAL | Acceptance gate reliability; scale mismatch; operational simplicity |
