"""Session-based conversion funnel logic."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, Set
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import EventRecord, POSTransaction
from .models import FunnelResponse, FunnelStage


async def get_store_funnel(store_id: str, db: AsyncSession) -> FunnelResponse:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    now_iso = now.isoformat()

    # ── Step 1: All non-staff visitors who entered today ─────────────────────
    # Re-entries must NOT double-count — use unique visitor_ids from ENTRY/REENTRY
    entry_q = await db.execute(
        select(EventRecord.visitor_id)
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type.in_(["ENTRY"]),
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start,
        )
    )
    entered_visitors: Set[str] = {r[0] for r in entry_q.all()}
    total_entered = len(entered_visitors)

    # ── Step 2: Visitors who visited at least one named zone ─────────────────
    zone_q = await db.execute(
        select(EventRecord.visitor_id)
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start,
            EventRecord.zone_id.notin_(["ENTRY_EXIT", "ENTRY_LOBBY"]),
        )
    )
    zone_visitors: Set[str] = {r[0] for r in zone_q.all()} & entered_visitors
    total_zone_visit = len(zone_visitors)

    # ── Step 3: Visitors who entered the billing queue ────────────────────────
    billing_q = await db.execute(
        select(EventRecord.visitor_id)
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
            EventRecord.zone_id == "CASH_COUNTER",
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start,
        )
    )
    billing_visitors: Set[str] = {r[0] for r in billing_q.all()} & entered_visitors
    total_billing = len(billing_visitors)

    # ── Step 4: Visitors who completed a purchase (POS correlation) ───────────
    pos_q = await db.execute(
        select(POSTransaction.timestamp)
        .where(
            POSTransaction.store_id == store_id,
            POSTransaction.timestamp >= today_start,
        )
    )
    pos_timestamps = [r[0] for r in pos_q.all()]

    # Map billing visitor events to timestamps for correlation
    billing_events_q = await db.execute(
        select(EventRecord.visitor_id, EventRecord.timestamp)
        .where(
            EventRecord.store_id == store_id,
            EventRecord.zone_id == "CASH_COUNTER",
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start,
        )
    )

    purchased_visitors: Set[str] = set()
    for vis_id, vis_ts_str in billing_events_q.all():
        if vis_id not in entered_visitors:
            continue
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
            if 0 <= diff <= 300:
                purchased_visitors.add(vis_id)
                break

    total_purchased = len(purchased_visitors)

    # ── Build funnel stages ───────────────────────────────────────────────────
    def drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0
        return round((1.0 - current / previous) * 100, 2)

    stages = [
        FunnelStage(stage="Entry",        count=total_entered,    drop_off_pct=0.0),
        FunnelStage(stage="Zone Visit",   count=total_zone_visit, drop_off_pct=drop_off(total_zone_visit, total_entered)),
        FunnelStage(stage="Billing Queue",count=total_billing,    drop_off_pct=drop_off(total_billing, total_zone_visit)),
        FunnelStage(stage="Purchase",     count=total_purchased,  drop_off_pct=drop_off(total_purchased, total_billing)),
    ]

    conversion_rate = total_purchased / max(total_entered, 1)

    return FunnelResponse(
        store_id=store_id,
        as_of=now_iso,
        stages=stages,
        conversion_rate=round(conversion_rate, 4),
    )
