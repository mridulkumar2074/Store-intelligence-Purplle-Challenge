"""Real-time store metrics computation."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Set
from collections import defaultdict

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from .database import EventRecord, POSTransaction
from .models import MetricsResponse, ZoneDwellMetric


async def get_store_metrics(store_id: str, db: AsyncSession) -> MetricsResponse:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    now_iso = now.isoformat()

    # ── Unique non-staff customer visitors today ──────────────────────────────
    entry_q = await db.execute(
        select(func.count(func.distinct(EventRecord.visitor_id)))
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "ENTRY",
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start,
        )
    )
    unique_visitors: int = entry_q.scalar() or 0

    # ── Re-entries should NOT double-count as new visitors ────────────────────
    # REENTRY visitors are already tracked under the same visitor_id, no extra work needed.

    # ── POS transactions today ───────────────────────────────────────────────
    pos_q = await db.execute(
        select(
            func.count(POSTransaction.transaction_id),
            func.coalesce(func.sum(POSTransaction.basket_value_inr), 0.0),
        )
        .where(
            POSTransaction.store_id == store_id,
            POSTransaction.timestamp >= today_start,
        )
    )
    pos_row = pos_q.one()
    total_transactions: int  = pos_row[0] or 0
    total_revenue_inr: float = float(pos_row[1] or 0.0)

    # ── Conversion rate via billing zone correlation ──────────────────────────
    # Visitors who were in a billing zone within 5 min before a POS transaction
    converted_visitors: Set[str] = set()
    if total_transactions > 0:
        billing_q = await db.execute(
            select(EventRecord.visitor_id, EventRecord.timestamp)
            .where(
                EventRecord.store_id == store_id,
                EventRecord.zone_id == "CASH_COUNTER",
                EventRecord.is_staff == False,
                EventRecord.timestamp >= today_start,
            )
        )
        billing_rows = billing_q.all()

        pos_ts_q = await db.execute(
            select(POSTransaction.timestamp)
            .where(
                POSTransaction.store_id == store_id,
                POSTransaction.timestamp >= today_start,
            )
        )
        pos_timestamps = [r[0] for r in pos_ts_q.all()]

        for vis_id, vis_ts_str in billing_rows:
            try:
                vis_ts = datetime.fromisoformat(vis_ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            for pos_ts_str in pos_timestamps:
                try:
                    pos_ts = datetime.fromisoformat(pos_ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                diff = (pos_ts - vis_ts).total_seconds()
                if 0 <= diff <= 300:  # within 5 minutes
                    converted_visitors.add(vis_id)
                    break

    conversion_rate = len(converted_visitors) / max(unique_visitors, 1)

    # ── Average dwell per zone ────────────────────────────────────────────────
    dwell_q = await db.execute(
        select(EventRecord.zone_id, EventRecord.dwell_ms)
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type.in_(["ZONE_DWELL", "ZONE_EXIT"]),
            EventRecord.is_staff == False,
            EventRecord.dwell_ms > 0,
            EventRecord.timestamp >= today_start,
        )
    )
    dwell_rows = dwell_q.all()

    zone_dwell_map: Dict[str, List[int]] = defaultdict(list)
    for zone_id, dwell_ms in dwell_rows:
        if zone_id:
            zone_dwell_map[zone_id].append(dwell_ms)

    zone_dwell: List[ZoneDwellMetric] = []
    total_dwell_ms = 0
    total_dwell_count = 0
    for zone_id, dwell_list in zone_dwell_map.items():
        avg = sum(dwell_list) / len(dwell_list)
        zone_dwell.append(ZoneDwellMetric(
            zone_id=zone_id,
            avg_dwell_ms=round(avg, 1),
            visit_count=len(dwell_list),
        ))
        total_dwell_ms += sum(dwell_list)
        total_dwell_count += len(dwell_list)

    avg_dwell_ms = total_dwell_ms / max(total_dwell_count, 1)

    # ── Current queue depth ───────────────────────────────────────────────────
    # Max queue_depth from billing events in the last 30 min
    thirty_min_ago = (now - timedelta(minutes=30)).isoformat()
    queue_q = await db.execute(
        select(func.max(EventRecord.queue_depth))
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
            EventRecord.timestamp >= thirty_min_ago,
        )
    )
    current_queue_depth: int = queue_q.scalar() or 0

    # ── Abandonment rate ──────────────────────────────────────────────────────
    abandon_q = await db.execute(
        select(func.count(func.distinct(EventRecord.visitor_id)))
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "BILLING_QUEUE_ABANDON",
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start,
        )
    )
    abandon_count: int = abandon_q.scalar() or 0

    queue_join_q = await db.execute(
        select(func.count(func.distinct(EventRecord.visitor_id)))
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start,
        )
    )
    queue_join_count: int = queue_join_q.scalar() or 0

    abandonment_rate = abandon_count / max(queue_join_count, 1)

    return MetricsResponse(
        store_id=store_id,
        as_of=now_iso,
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_ms=round(avg_dwell_ms, 1),
        zone_dwell=zone_dwell,
        current_queue_depth=current_queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
        total_transactions=total_transactions,
        total_revenue_inr=round(total_revenue_inr, 2),
    )
