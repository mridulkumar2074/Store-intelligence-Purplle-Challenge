# PROMPT: "Write direct async unit tests for metrics.py, funnel.py, heatmap.py,
# anomalies.py, and health.py — calling the functions directly with a real
# in-memory DB session so that code coverage tools can track them properly."
# CHANGES MADE: Seeded DB with realistic data including POS transactions,
# billing events, zone dwell events to exercise all computation branches.

import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio

from app.database import Base, engine, AsyncSessionLocal, EventRecord, POSTransaction
from app.metrics import get_store_metrics
from app.funnel import get_store_funnel
from app.heatmap import get_store_heatmap
from app.anomalies import get_store_anomalies
from app.health import get_health

STORE_ID = "STORE_BLR_002"
NOW      = datetime.now(timezone.utc)
TODAY    = NOW.replace(hour=10, minute=0, second=0, microsecond=0)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _ev(event_type, visitor_id=None, zone_id=None, is_staff=False,
        dwell_ms=0, queue_depth=None, dt=None, confidence=0.88) -> EventRecord:
    now_str = _ts(dt or TODAY)
    return EventRecord(
        event_id    = str(uuid.uuid4()),
        store_id    = STORE_ID,
        camera_id   = "CAM_ENTRY_01",
        visitor_id  = visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        event_type  = event_type,
        timestamp   = now_str,
        zone_id     = zone_id,
        dwell_ms    = dwell_ms,
        is_staff    = is_staff,
        confidence  = confidence,
        queue_depth = queue_depth,
        sku_zone    = None,
        session_seq = 1,
        ingested_at = now_str,
    )


# ── Metrics direct tests ──────────────────────────────────────────────────────

class TestMetricsDirect:
    @pytest.mark.asyncio
    async def test_empty_store_zero_metrics(self):
        async with AsyncSessionLocal() as db:
            result = await get_store_metrics(STORE_ID, db)
        assert result.unique_visitors == 0
        assert result.conversion_rate == 0.0
        assert result.total_transactions == 0

    @pytest.mark.asyncio
    async def test_visitors_counted_excluding_staff(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("ENTRY", visitor_id="VIS_A", is_staff=False))
            db.add(_ev("ENTRY", visitor_id="VIS_B", is_staff=False))
            db.add(_ev("ENTRY", visitor_id="VIS_STAFF", is_staff=True))
            await db.commit()
            result = await get_store_metrics(STORE_ID, db)
        assert result.unique_visitors == 2

    @pytest.mark.asyncio
    async def test_zone_dwell_aggregated(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("ZONE_DWELL", zone_id="DERMDOC", dwell_ms=30000))
            db.add(_ev("ZONE_DWELL", zone_id="DERMDOC", dwell_ms=60000))
            db.add(_ev("ZONE_DWELL", zone_id="FOH",     dwell_ms=45000))
            await db.commit()
            result = await get_store_metrics(STORE_ID, db)

        zone_map = {z.zone_id: z for z in result.zone_dwell}
        assert "DERMDOC" in zone_map
        assert zone_map["DERMDOC"].avg_dwell_ms == pytest.approx(45000.0)
        assert "FOH" in zone_map

    @pytest.mark.asyncio
    async def test_conversion_with_pos_correlation(self):
        billing_dt = TODAY + timedelta(hours=1)
        async with AsyncSessionLocal() as db:
            db.add(_ev("ENTRY",      visitor_id="VIS_X", dt=TODAY))
            db.add(_ev("ZONE_ENTER", visitor_id="VIS_X", zone_id="CASH_COUNTER",
                       dt=billing_dt))
            db.add(POSTransaction(
                transaction_id="TXN_DIRECT_001",
                store_id=STORE_ID,
                timestamp=_ts(billing_dt + timedelta(minutes=2)),
                basket_value_inr=999.0,
            ))
            await db.commit()
            result = await get_store_metrics(STORE_ID, db)

        assert result.conversion_rate == pytest.approx(1.0)
        assert result.total_revenue_inr == pytest.approx(999.0)
        assert result.total_transactions == 1

    @pytest.mark.asyncio
    async def test_queue_depth_from_recent_events(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("BILLING_QUEUE_JOIN", zone_id="CASH_COUNTER",
                       queue_depth=7, dt=NOW - timedelta(minutes=5)))
            await db.commit()
            result = await get_store_metrics(STORE_ID, db)
        assert result.current_queue_depth == 7

    @pytest.mark.asyncio
    async def test_abandonment_rate_calculated(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("BILLING_QUEUE_JOIN",    visitor_id="VIS_1",
                       zone_id="CASH_COUNTER", queue_depth=2))
            db.add(_ev("BILLING_QUEUE_JOIN",    visitor_id="VIS_2",
                       zone_id="CASH_COUNTER", queue_depth=2))
            db.add(_ev("BILLING_QUEUE_ABANDON", visitor_id="VIS_1",
                       zone_id="CASH_COUNTER"))
            await db.commit()
            result = await get_store_metrics(STORE_ID, db)
        assert result.abandonment_rate == pytest.approx(0.5)


# ── Funnel direct tests ───────────────────────────────────────────────────────

class TestFunnelDirect:
    @pytest.mark.asyncio
    async def test_empty_funnel(self):
        async with AsyncSessionLocal() as db:
            result = await get_store_funnel(STORE_ID, db)
        stages = {s.stage: s for s in result.stages}
        assert stages["Entry"].count == 0

    @pytest.mark.asyncio
    async def test_full_conversion_funnel(self):
        vid = "VIS_FULL"
        async with AsyncSessionLocal() as db:
            db.add(_ev("ENTRY",               visitor_id=vid, dt=TODAY))
            db.add(_ev("ZONE_ENTER",          visitor_id=vid, zone_id="FACES",
                       dt=TODAY + timedelta(minutes=3)))
            db.add(_ev("ZONE_ENTER",          visitor_id=vid, zone_id="CASH_COUNTER",
                       dt=TODAY + timedelta(minutes=8)))
            db.add(_ev("BILLING_QUEUE_JOIN",  visitor_id=vid, zone_id="CASH_COUNTER",
                       queue_depth=1, dt=TODAY + timedelta(minutes=8)))
            db.add(POSTransaction(
                transaction_id="TXN_FUNNEL_001",
                store_id=STORE_ID,
                timestamp=_ts(TODAY + timedelta(minutes=11)),
                basket_value_inr=600.0,
            ))
            await db.commit()
            result = await get_store_funnel(STORE_ID, db)

        stages = {s.stage: s.count for s in result.stages}
        assert stages["Entry"] == 1
        assert stages["Zone Visit"] == 1
        assert stages["Billing Queue"] == 1
        assert stages["Purchase"] == 1
        assert result.conversion_rate == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_reentry_not_double_counted(self):
        vid = "VIS_REENTRY"
        async with AsyncSessionLocal() as db:
            db.add(_ev("ENTRY",   visitor_id=vid, dt=TODAY))
            db.add(_ev("EXIT",    visitor_id=vid, dt=TODAY + timedelta(minutes=5)))
            db.add(_ev("REENTRY", visitor_id=vid, dt=TODAY + timedelta(minutes=8)))
            await db.commit()
            result = await get_store_funnel(STORE_ID, db)

        stages = {s.stage: s.count for s in result.stages}
        assert stages["Entry"] == 1  # not 2

    @pytest.mark.asyncio
    async def test_funnel_drop_off_percentages(self):
        async with AsyncSessionLocal() as db:
            for i in range(10):
                db.add(_ev("ENTRY", visitor_id=f"VIS_{i:03d}", dt=TODAY))
            for i in range(6):
                db.add(_ev("ZONE_ENTER", visitor_id=f"VIS_{i:03d}",
                           zone_id="FOH", dt=TODAY + timedelta(minutes=2)))
            for i in range(3):
                db.add(_ev("BILLING_QUEUE_JOIN", visitor_id=f"VIS_{i:03d}",
                           zone_id="CASH_COUNTER", queue_depth=1,
                           dt=TODAY + timedelta(minutes=5)))
            await db.commit()
            result = await get_store_funnel(STORE_ID, db)

        stages = {s.stage: s for s in result.stages}
        assert stages["Zone Visit"].drop_off_pct == pytest.approx(40.0, abs=1.0)
        assert stages["Billing Queue"].drop_off_pct == pytest.approx(50.0, abs=1.0)


# ── Heatmap direct tests ──────────────────────────────────────────────────────

class TestHeatmapDirect:
    @pytest.mark.asyncio
    async def test_empty_heatmap(self):
        async with AsyncSessionLocal() as db:
            result = await get_store_heatmap(STORE_ID, db)
        assert result.zones == []

    @pytest.mark.asyncio
    async def test_heatmap_max_zone_scores_100(self):
        async with AsyncSessionLocal() as db:
            # 10 visits to FRAGRANCE, 5 to MINIMALIST
            for _ in range(10):
                db.add(_ev("ZONE_ENTER", zone_id="FRAGRANCE"))
            for _ in range(5):
                db.add(_ev("ZONE_ENTER", zone_id="MINIMALIST"))
            # 20 unique entry sessions for data_confidence
            for i in range(20):
                db.add(_ev("ENTRY", visitor_id=f"VIS_{i:03d}"))
            await db.commit()
            result = await get_store_heatmap(STORE_ID, db)

        zone_map = {z.zone_id: z for z in result.zones}
        assert zone_map["FRAGRANCE"].normalised_score == 100.0
        assert zone_map["MINIMALIST"].normalised_score < 100.0
        assert all(z.data_confidence for z in result.zones)

    @pytest.mark.asyncio
    async def test_heatmap_low_sessions_no_confidence(self):
        async with AsyncSessionLocal() as db:
            for _ in range(5):
                db.add(_ev("ZONE_ENTER", zone_id="FOH"))
            for i in range(5):  # only 5 sessions, below threshold of 20
                db.add(_ev("ENTRY", visitor_id=f"VIS_{i:03d}"))
            await db.commit()
            result = await get_store_heatmap(STORE_ID, db)

        assert all(not z.data_confidence for z in result.zones)


# ── Anomalies direct tests ────────────────────────────────────────────────────

class TestAnomaliesDirect:
    @pytest.mark.asyncio
    async def test_no_anomalies_empty_store(self):
        async with AsyncSessionLocal() as db:
            result = await get_store_anomalies(STORE_ID, db)
        assert result.active_anomalies == []

    @pytest.mark.asyncio
    async def test_queue_spike_warn_threshold(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("BILLING_QUEUE_JOIN", zone_id="CASH_COUNTER",
                       queue_depth=5, dt=NOW - timedelta(minutes=5)))
            await db.commit()
            result = await get_store_anomalies(STORE_ID, db)

        types = [a.anomaly_type for a in result.active_anomalies]
        assert "BILLING_QUEUE_SPIKE" in types

    @pytest.mark.asyncio
    async def test_queue_spike_critical_threshold(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("BILLING_QUEUE_JOIN", zone_id="CASH_COUNTER",
                       queue_depth=9, dt=NOW - timedelta(minutes=2)))
            await db.commit()
            result = await get_store_anomalies(STORE_ID, db)

        spike = next(a for a in result.active_anomalies
                     if a.anomaly_type == "BILLING_QUEUE_SPIKE")
        assert spike.severity == "CRITICAL"
        assert len(spike.suggested_action) > 0

    @pytest.mark.asyncio
    async def test_dead_zone_anomaly(self):
        old_ts = NOW - timedelta(hours=1)
        async with AsyncSessionLocal() as db:
            db.add(_ev("ZONE_ENTER", zone_id="DERMDOC", dt=old_ts))
            await db.commit()
            result = await get_store_anomalies(STORE_ID, db)

        types = [a.anomaly_type for a in result.active_anomalies]
        assert "DEAD_ZONE" in types
        dead = next(a for a in result.active_anomalies if a.anomaly_type == "DEAD_ZONE")
        assert "DERMDOC" in dead.description

    @pytest.mark.asyncio
    async def test_anomaly_has_all_required_fields(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("BILLING_QUEUE_JOIN", zone_id="CASH_COUNTER",
                       queue_depth=8, dt=NOW - timedelta(minutes=3)))
            await db.commit()
            result = await get_store_anomalies(STORE_ID, db)

        for a in result.active_anomalies:
            assert a.anomaly_id
            assert a.anomaly_type
            assert a.severity
            assert a.detected_at
            assert a.description
            assert a.suggested_action


# ── Health direct tests ───────────────────────────────────────────────────────

class TestHealthDirect:
    @pytest.mark.asyncio
    async def test_health_no_data(self):
        async with AsyncSessionLocal() as db:
            result = await get_health(db)
        assert result.db_ok is True
        assert result.version == "1.0.0"
        assert result.uptime_seconds >= 0

    @pytest.mark.asyncio
    async def test_health_recent_events_ok(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("ENTRY", dt=NOW - timedelta(minutes=2)))
            await db.commit()
            result = await get_health(db)

        store = next((s for s in result.stores if s.store_id == STORE_ID), None)
        assert store is not None
        assert store.status == "OK"

    @pytest.mark.asyncio
    async def test_health_stale_feed_warning(self):
        async with AsyncSessionLocal() as db:
            db.add(_ev("ENTRY", dt=NOW - timedelta(minutes=15)))
            await db.commit()
            result = await get_health(db)

        store = next((s for s in result.stores if s.store_id == STORE_ID), None)
        assert store.status == "STALE_FEED"

    @pytest.mark.asyncio
    async def test_health_events_last_hour_count(self):
        async with AsyncSessionLocal() as db:
            for i in range(7):
                db.add(_ev("ENTRY", dt=NOW - timedelta(minutes=i * 5)))
            db.add(_ev("ENTRY", dt=NOW - timedelta(hours=2)))  # older, shouldn't count
            await db.commit()
            result = await get_health(db)

        store = next(s for s in result.stores if s.store_id == STORE_ID)
        assert store.events_last_hour == 7
