"""Zone heatmap computation."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import List
from collections import defaultdict

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .database import EventRecord
from .models import HeatmapResponse, HeatmapZone

_MIN_SESSIONS_FOR_CONFIDENCE = 20


async def get_store_heatmap(store_id: str, db: AsyncSession) -> HeatmapResponse:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    now_iso = now.isoformat()

    # Total unique sessions today
    sessions_q = await db.execute(
        select(func.count(func.distinct(EventRecord.visitor_id)))
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "ENTRY",
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start,
        )
    )
    total_sessions: int = sessions_q.scalar() or 0

    # Per-zone visit frequency and dwell
    zone_visits_q = await db.execute(
        select(EventRecord.zone_id, EventRecord.dwell_ms)
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
            EventRecord.is_staff == False,
            EventRecord.zone_id.isnot(None),
            EventRecord.zone_id.notin_(["ENTRY_EXIT", "ENTRY_LOBBY"]),
            EventRecord.timestamp >= today_start,
        )
    )
    zone_rows = zone_visits_q.all()

    zone_visit_counts: dict[str, int]         = defaultdict(int)
    zone_dwell_totals: dict[str, int]         = defaultdict(int)
    zone_dwell_counts: dict[str, int]         = defaultdict(int)

    for zone_id, dwell_ms in zone_rows:
        zone_visit_counts[zone_id] += 1
        if dwell_ms and dwell_ms > 0:
            zone_dwell_totals[zone_id] += dwell_ms
            zone_dwell_counts[zone_id] += 1

    if not zone_visit_counts:
        return HeatmapResponse(store_id=store_id, as_of=now_iso, zones=[])

    max_visits = max(zone_visit_counts.values(), default=1)

    zones: List[HeatmapZone] = []
    for zone_id, visit_count in sorted(zone_visit_counts.items(), key=lambda x: -x[1]):
        avg_dwell = (
            zone_dwell_totals[zone_id] / zone_dwell_counts[zone_id]
            if zone_dwell_counts[zone_id] > 0 else 0.0
        )
        # Normalise 0-100 based on visit frequency relative to max
        norm_score = round((visit_count / max_visits) * 100, 1)
        zones.append(HeatmapZone(
            zone_id=zone_id,
            visit_frequency=visit_count,
            avg_dwell_ms=round(avg_dwell, 1),
            normalised_score=norm_score,
            data_confidence=(total_sessions >= _MIN_SESSIONS_FOR_CONFIDENCE),
        ))

    return HeatmapResponse(store_id=store_id, as_of=now_iso, zones=zones)
