"""Anomaly detection engine."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import List
from collections import defaultdict

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .database import EventRecord, POSTransaction
from .models import Anomaly, AnomalyType, AnomalySeverity, AnomaliesResponse

# Thresholds
QUEUE_SPIKE_THRESHOLD      = 5    # people in queue = WARN, >8 = CRITICAL
DEAD_ZONE_MINUTES          = 30   # no visits in N minutes
CONVERSION_DROP_THRESHOLD  = 0.30 # 30% below 7-day avg → WARN


async def get_store_anomalies(store_id: str, db: AsyncSession) -> AnomaliesResponse:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    anomalies: List[Anomaly] = []

    # ── 1. Billing queue spike ────────────────────────────────────────────────
    thirty_min_ago = (now - timedelta(minutes=30)).isoformat()
    queue_q = await db.execute(
        select(func.max(EventRecord.queue_depth))
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
            EventRecord.timestamp >= thirty_min_ago,
        )
    )
    max_queue: int = queue_q.scalar() or 0

    if max_queue >= 8:
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
            severity=AnomalySeverity.CRITICAL,
            detected_at=now_iso,
            description=f"Billing queue depth reached {max_queue} in the last 30 minutes.",
            suggested_action="Open additional billing counters or redirect floor staff to expedite checkout.",
            metadata={"queue_depth": max_queue},
        ))
    elif max_queue >= QUEUE_SPIKE_THRESHOLD:
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
            severity=AnomalySeverity.WARN,
            detected_at=now_iso,
            description=f"Billing queue depth is {max_queue} — approaching threshold.",
            suggested_action="Monitor queue; consider deploying an additional cashier if depth increases.",
            metadata={"queue_depth": max_queue},
        ))

    # ── 2. Conversion drop vs 7-day rolling average ───────────────────────────
    # Today's conversion
    entries_q = await db.execute(
        select(func.count(func.distinct(EventRecord.visitor_id)))
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "ENTRY",
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start.isoformat(),
        )
    )
    today_entries: int = entries_q.scalar() or 0

    pos_today_q = await db.execute(
        select(func.count(POSTransaction.transaction_id))
        .where(
            POSTransaction.store_id == store_id,
            POSTransaction.timestamp >= today_start.isoformat(),
        )
    )
    today_pos: int = pos_today_q.scalar() or 0
    today_cr = today_pos / max(today_entries, 1)

    # Historical 7-day average (days 1-7 before today)
    seven_days_ago = (today_start - timedelta(days=7)).isoformat()
    hist_entries_q = await db.execute(
        select(func.count(func.distinct(EventRecord.visitor_id)))
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "ENTRY",
            EventRecord.is_staff == False,
            EventRecord.timestamp >= seven_days_ago,
            EventRecord.timestamp < today_start.isoformat(),
        )
    )
    hist_entries: int = hist_entries_q.scalar() or 0

    hist_pos_q = await db.execute(
        select(func.count(POSTransaction.transaction_id))
        .where(
            POSTransaction.store_id == store_id,
            POSTransaction.timestamp >= seven_days_ago,
            POSTransaction.timestamp < today_start.isoformat(),
        )
    )
    hist_pos: int = hist_pos_q.scalar() or 0

    if hist_entries > 0:
        hist_cr = hist_pos / hist_entries
        if hist_cr > 0 and today_cr < hist_cr * (1 - CONVERSION_DROP_THRESHOLD):
            drop_pct = round((hist_cr - today_cr) / hist_cr * 100, 1)
            severity = (
                AnomalySeverity.CRITICAL if drop_pct > 50
                else AnomalySeverity.WARN
            )
            anomalies.append(Anomaly(
                anomaly_type=AnomalyType.CONVERSION_DROP,
                severity=severity,
                detected_at=now_iso,
                description=(
                    f"Conversion rate today ({today_cr:.1%}) is {drop_pct}% below "
                    f"the 7-day average ({hist_cr:.1%})."
                ),
                suggested_action=(
                    "Review staff engagement, promotional offer visibility, "
                    "and billing queue abandonment. Check if any display zones are empty."
                ),
                metadata={"today_cr": today_cr, "hist_cr": hist_cr, "drop_pct": drop_pct},
            ))

    # ── 3. Dead zones (no visits in last 30 min) ─────────────────────────────
    dead_zone_cutoff = (now - timedelta(minutes=DEAD_ZONE_MINUTES)).isoformat()

    active_zones_q = await db.execute(
        select(EventRecord.zone_id)
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
            EventRecord.is_staff == False,
            EventRecord.timestamp >= dead_zone_cutoff,
            EventRecord.zone_id.isnot(None),
        )
        .distinct()
    )
    active_zones = {r[0] for r in active_zones_q.all()}

    # Zones that had visits today but not in the last 30 min
    all_visited_q = await db.execute(
        select(EventRecord.zone_id)
        .where(
            EventRecord.store_id == store_id,
            EventRecord.event_type.in_(["ZONE_ENTER"]),
            EventRecord.is_staff == False,
            EventRecord.timestamp >= today_start.isoformat(),
            EventRecord.zone_id.isnot(None),
            EventRecord.zone_id.notin_(["ENTRY_EXIT", "ENTRY_LOBBY"]),
        )
        .distinct()
    )
    all_visited = {r[0] for r in all_visited_q.all()}
    dead_zones = all_visited - active_zones

    for zone_id in sorted(dead_zones):
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.DEAD_ZONE,
            severity=AnomalySeverity.INFO,
            detected_at=now_iso,
            description=f"Zone '{zone_id}' has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
            suggested_action=(
                f"Check display stock and signage for zone '{zone_id}'. "
                "Consider redirecting foot traffic with promotional activity."
            ),
            metadata={"zone_id": zone_id, "no_visit_minutes": DEAD_ZONE_MINUTES},
        ))

    # ── 4. Low traffic warning ────────────────────────────────────────────────
    # If it's mid-day and very few visitors
    hour_now = now.hour
    if 12 <= hour_now <= 20 and today_entries < 3 and today_entries > 0:
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.LOW_TRAFFIC,
            severity=AnomalySeverity.INFO,
            detected_at=now_iso,
            description=f"Only {today_entries} visitors today during peak hours.",
            suggested_action="Consider reviewing store visibility or check for entry camera issues.",
            metadata={"visitor_count": today_entries},
        ))

    return AnomaliesResponse(
        store_id=store_id,
        as_of=now_iso,
        active_anomalies=anomalies,
    )
