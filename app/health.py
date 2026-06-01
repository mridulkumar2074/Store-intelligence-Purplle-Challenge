"""Health check endpoint."""
from __future__ import annotations
import time
from datetime import datetime, timezone, timedelta
from typing import List

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from .database import EventRecord
from .models import HealthResponse, StoreHealth

_START_TIME = time.time()
VERSION = "1.0.0"
STALE_FEED_MINUTES = 10


async def get_health(db: AsyncSession) -> HealthResponse:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    uptime = time.time() - _START_TIME

    db_ok = True
    stores_health: List[StoreHealth] = []

    try:
        # Get all known store IDs from events
        stores_q = await db.execute(
            select(EventRecord.store_id).distinct()
        )
        store_ids = [r[0] for r in stores_q.all()]

        one_hour_ago = (now - timedelta(hours=1)).isoformat()
        stale_cutoff  = (now - timedelta(minutes=STALE_FEED_MINUTES)).isoformat()

        for store_id in store_ids:
            # Last event timestamp
            last_q = await db.execute(
                select(func.max(EventRecord.timestamp))
                .where(EventRecord.store_id == store_id)
            )
            last_event_at: str | None = last_q.scalar()

            # Events in last hour
            count_q = await db.execute(
                select(func.count(EventRecord.event_id))
                .where(
                    EventRecord.store_id == store_id,
                    EventRecord.timestamp >= one_hour_ago,
                )
            )
            events_last_hour: int = count_q.scalar() or 0

            # Determine status
            if last_event_at is None:
                status = "NO_DATA"
            elif last_event_at < stale_cutoff:
                status = "STALE_FEED"
            else:
                status = "OK"

            stores_health.append(StoreHealth(
                store_id=store_id,
                last_event_at=last_event_at,
                events_last_hour=events_last_hour,
                status=status,
            ))

        # If no stores found, add a placeholder for the default store
        if not stores_health:
            stores_health.append(StoreHealth(
                store_id="STORE_BLR_002",
                last_event_at=None,
                events_last_hour=0,
                status="NO_DATA",
            ))

        overall = (
            "OK" if all(s.status == "OK" for s in stores_health)
            else ("DEGRADED" if any(s.status != "NO_DATA" for s in stores_health)
                  else "NO_DATA")
        )

    except Exception as exc:
        db_ok = False
        overall = "UNHEALTHY"
        stores_health = []

    return HealthResponse(
        status=overall,
        version=VERSION,
        uptime_seconds=round(uptime, 1),
        stores=stores_health,
        db_ok=db_ok,
    )
