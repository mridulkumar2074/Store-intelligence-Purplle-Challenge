from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Tuple, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .database import EventRecord
from .models import StoreEvent, IngestResponse
from pydantic import ValidationError


async def ingest_events(
    events: List, db: AsyncSession
) -> IngestResponse:
    accepted = 0
    rejected = 0
    duplicate = 0
    errors: List[Dict[str, Any]] = []

    now_iso = datetime.now(timezone.utc).isoformat()

    for raw_ev in events:
        try:
            # Per-event Pydantic validation for partial-success support
            if isinstance(raw_ev, dict):
                ev = StoreEvent.model_validate(raw_ev)
            elif isinstance(raw_ev, StoreEvent):
                ev = raw_ev
            else:
                rejected += 1
                errors.append({"event_id": "unknown", "error": "unsupported event type"})
                continue

            # Check for existing event_id (idempotency)
            existing = await db.get(EventRecord, ev.event_id)
            if existing is not None:
                duplicate += 1
                continue

            record = EventRecord(
                event_id    = ev.event_id,
                store_id    = ev.store_id,
                camera_id   = ev.camera_id,
                visitor_id  = ev.visitor_id,
                event_type  = ev.event_type.value,
                timestamp   = ev.timestamp,
                zone_id     = ev.zone_id,
                dwell_ms    = ev.dwell_ms,
                is_staff    = ev.is_staff,
                confidence  = ev.confidence,
                queue_depth = ev.metadata.queue_depth,
                sku_zone    = ev.metadata.sku_zone,
                session_seq = ev.metadata.session_seq,
                ingested_at = now_iso,
            )
            db.add(record)
            accepted += 1

        except (ValidationError, Exception) as exc:
            rejected += 1
            event_id = raw_ev.get("event_id", "unknown") if isinstance(raw_ev, dict) else "unknown"
            errors.append({"event_id": event_id, "error": str(exc)[:200]})

    await db.commit()
    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicate,
        errors=errors,
    )


async def load_pos_transactions(csv_path: str, db: AsyncSession) -> int:
    """Load POS transactions from CSV into the database (idempotent).
    Shifts timestamps to today so the daily metrics filter always matches.
    """
    import csv, os
    from datetime import timedelta
    if not os.path.exists(csv_path):
        return 0

    from .database import POSTransaction

    # Read all rows first to compute date shift
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        return 0

    # Shift dates to today
    try:
        first_dt = datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        orig_day = first_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        shift = today - orig_day
    except Exception:
        shift = timedelta(0)

    loaded = 0
    for row in rows:
        existing = await db.get(POSTransaction, row["transaction_id"])
        if existing:
            continue
        try:
            orig_ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            new_ts  = (orig_ts + shift).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            new_ts = row["timestamp"]

        db.add(POSTransaction(
            transaction_id   = row["transaction_id"],
            store_id         = row["store_id"],
            timestamp        = new_ts,
            basket_value_inr = float(row["basket_value_inr"]),
        ))
        loaded += 1

    await db.commit()
    return loaded
