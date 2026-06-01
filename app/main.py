"""Store Intelligence API — FastAPI entrypoint."""
from __future__ import annotations
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, Request, Response, HTTPException
from fastapi.responses import JSONResponse

from fastapi.responses import PlainTextResponse

from .database import init_db, get_db, AsyncSession
from .ingestion import ingest_events, load_pos_transactions
from .metrics import get_store_metrics
from .funnel import get_store_funnel
from .heatmap import get_store_heatmap
from .anomalies import get_store_anomalies
from .health import get_health
from .models import IngestRequest, IngestResponse
from .telemetry import metrics as _telemetry


# ── Structured JSON logging ────────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        log: dict = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "level":    record.levelname,
            "logger":   record.name,
            "message":  record.getMessage(),
        }
        for key in ("trace_id", "store_id", "endpoint", "latency_ms",
                    "event_count", "status_code"):
            val = getattr(record, key, None)
            if val is not None:
                log[key] = val
        if record.exc_info:
            log["exc"] = self.formatException(record.exc_info)
        return json.dumps(log)


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return logging.getLogger("store_api")


logger = _setup_logging()

# ── Application lifecycle ──────────────────────────────────────────────────

POS_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pos_transactions.csv")
SAMPLE_EVENTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "sample_events", "events.jsonl"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialised")

    # Load POS transactions
    from .database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        n = await load_pos_transactions(POS_CSV, db)
        if n:
            logger.info(f"Loaded {n} POS transactions")

        # Pre-load sample events if present (for demo / acceptance gate).
        # Timestamps are shifted to today so the "today" filter always returns data.
        if os.path.exists(SAMPLE_EVENTS_PATH):
            from .models import StoreEvent
            from datetime import timedelta
            events = []
            with open(SAMPLE_EVENTS_PATH, encoding="utf-8") as f:
                raw_lines = [l.strip() for l in f if l.strip()]

            # Compute shift: move all events to today
            if raw_lines:
                try:
                    first = json.loads(raw_lines[0])
                    orig_dt = datetime.fromisoformat(
                        first["timestamp"].replace("Z", "+00:00")
                    )
                    today = datetime.now(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    orig_day = orig_dt.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    shift = today - orig_day
                except Exception:
                    shift = timedelta(0)

                for line in raw_lines:
                    try:
                        ev_dict = json.loads(line)
                        if shift.days != 0:
                            orig_ts = datetime.fromisoformat(
                                ev_dict["timestamp"].replace("Z", "+00:00")
                            )
                            ev_dict["timestamp"] = (orig_ts + shift).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            )
                        events.append(StoreEvent(**ev_dict))
                    except Exception:
                        pass

            if events:
                result = await ingest_events(events, db)
                logger.info(
                    "Pre-loaded sample events",
                    extra={"event_count": result.accepted}
                )

    yield
    logger.info("Shutting down Store Intelligence API")


# ── FastAPI app ────────────────────────────────────────────────────────────

app = FastAPI(
    title="Store Intelligence API",
    description="Real-time retail store analytics from CCTV event streams.",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Request logging middleware ─────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    start = time.perf_counter()

    response: Response = await call_next(request)

    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    store_id = request.path_params.get("store_id", "")

    logger.info(
        f"{request.method} {request.url.path}",
        extra={
            "trace_id":   trace_id,
            "store_id":   store_id,
            "endpoint":   request.url.path,
            "latency_ms": latency_ms,
            "status_code": response.status_code,
        },
    )
    _telemetry.record_request(request.url.path, response.status_code, latency_ms)
    response.headers["X-Request-ID"] = trace_id
    return response


# ── Error handler — no raw stack traces in responses ──────────────────────

@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": "An unexpected error occurred."},
    )


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.post("/events/ingest", response_model=IngestResponse, status_code=200)
async def ingest(
    payload: IngestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Ingest a batch of up to 500 events. Idempotent by event_id."""
    result = await ingest_events(payload.events, db)  # events is List[Any]
    _telemetry.record_ingest(result.accepted, result.rejected, result.duplicate)
    logger.info(
        "Ingest completed",
        extra={"event_count": result.accepted, "store_id": "batch"},
    )
    return result


@app.get("/stores/{store_id}/metrics")
async def metrics(store_id: str, db: AsyncSession = Depends(get_db)):
    """Real-time store metrics: visitors, conversion, dwell, queue, abandonment."""
    return await get_store_metrics(store_id, db)


@app.get("/stores/{store_id}/funnel")
async def funnel(store_id: str, db: AsyncSession = Depends(get_db)):
    """Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase."""
    return await get_store_funnel(store_id, db)


@app.get("/stores/{store_id}/heatmap")
async def heatmap(store_id: str, db: AsyncSession = Depends(get_db)):
    """Zone visit frequency and dwell heatmap, normalised 0-100."""
    return await get_store_heatmap(store_id, db)


@app.get("/stores/{store_id}/anomalies")
async def anomalies(store_id: str, db: AsyncSession = Depends(get_db)):
    """Active operational anomalies: queue spikes, conversion drops, dead zones."""
    return await get_store_anomalies(store_id, db)


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """Service health: uptime, last event per store, STALE_FEED warnings."""
    return await get_health(db)


@app.get("/api/metrics", response_class=PlainTextResponse,
         summary="Prometheus-compatible API telemetry metrics")
async def api_metrics():
    """Exposes request counts, error rates, latency percentiles, and event ingestion totals
    in Prometheus text format. Also available as JSON at /api/metrics?format=json."""
    return PlainTextResponse(_telemetry.to_prometheus_text(), media_type="text/plain")


@app.get("/api/metrics/json", summary="API telemetry metrics as JSON")
async def api_metrics_json():
    """JSON view of the same observability metrics."""
    return _telemetry.to_json()


# ── Graceful degradation: DB unavailable ──────────────────────────────────

from sqlalchemy.exc import OperationalError

@app.exception_handler(OperationalError)
async def db_error_handler(request: Request, exc: OperationalError):
    logger.error("Database unavailable", exc_info=True)
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_unavailable",
            "detail": "The database is temporarily unavailable. Please retry.",
        },
    )
