# PROMPT: "Write pytest tests targeting the computation paths in funnel.py,
# heatmap.py, metrics.py, and health.py. Focus on exercising the SQL queries
# with real data — ingest events first, then verify the computed analytics.
# Test edge cases: all-staff store, zero purchases, re-entry not double counted."
# CHANGES MADE: Structured fixtures that seed data via the ingest endpoint,
# then test each analytics endpoint response. Added POS correlation test and
# heatmap data_confidence flag threshold test.

import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import Base, engine, AsyncSessionLocal
from app.ingestion import load_pos_transactions

STORE_ID = "STORE_BLR_002"
TODAY = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(event_type, visitor_id=None, zone_id=None, is_staff=False,
       dwell_ms=0, queue_depth=None, dt=None, confidence=0.88):
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE_ID,
        "camera_id":  "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp":  ts(dt or TODAY),
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata":   {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── Funnel tests with rich data ───────────────────────────────────────────────

class TestFunnelComputation:
    @pytest.mark.asyncio
    async def test_full_funnel_flow(self, client):
        """Seed a complete conversion session and verify all 4 stages."""
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        events = [
            ev("ENTRY",              visitor_id=vid, dt=TODAY),
            ev("ZONE_ENTER",         visitor_id=vid, zone_id="DERMDOC", dt=TODAY + timedelta(minutes=2)),
            ev("ZONE_DWELL",         visitor_id=vid, zone_id="DERMDOC", dwell_ms=45000,
               dt=TODAY + timedelta(minutes=3)),
            ev("ZONE_EXIT",          visitor_id=vid, zone_id="DERMDOC", dwell_ms=60000,
               dt=TODAY + timedelta(minutes=4)),
            ev("ZONE_ENTER",         visitor_id=vid, zone_id="CASH_COUNTER",
               dt=TODAY + timedelta(minutes=6)),
            ev("BILLING_QUEUE_JOIN", visitor_id=vid, zone_id="CASH_COUNTER",
               queue_depth=2, dt=TODAY + timedelta(minutes=6)),
        ]
        await client.post("/events/ingest", json={"events": events})

        # Load a POS transaction that happens 3 min after billing entry
        async with AsyncSessionLocal() as db:
            from app.database import POSTransaction
            db.add(POSTransaction(
                transaction_id="TXN_TEST_001",
                store_id=STORE_ID,
                timestamp=ts(TODAY + timedelta(minutes=9)),
                basket_value_inr=850.0,
            ))
            await db.commit()

        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["Entry"]["count"] == 1
        assert stages["Zone Visit"]["count"] == 1
        assert stages["Billing Queue"]["count"] == 1
        assert stages["Purchase"]["count"] == 1

    @pytest.mark.asyncio
    async def test_funnel_all_staff_store(self, client):
        """All-staff store should have empty funnel (all excluded)."""
        events = [
            ev("ENTRY",      is_staff=True),
            ev("ZONE_ENTER", is_staff=True, zone_id="FOH"),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        entry_stage = next(s for s in resp.json()["stages"] if s["stage"] == "Entry")
        assert entry_stage["count"] == 0

    @pytest.mark.asyncio
    async def test_funnel_zero_purchase_no_crash(self, client):
        """Store with visitors but zero POS transactions must not crash."""
        events = [
            ev("ENTRY",      visitor_id="VIS_A"),
            ev("ZONE_ENTER", visitor_id="VIS_A", zone_id="FOH"),
            ev("EXIT",       visitor_id="VIS_A"),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        assert resp.status_code == 200
        assert resp.json()["conversion_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_funnel_excludes_entry_lobby_from_zone_visit(self, client):
        """ENTRY_LOBBY zone should not count as a zone visit."""
        vid = "VIS_X"
        events = [
            ev("ENTRY",      visitor_id=vid),
            ev("ZONE_ENTER", visitor_id=vid, zone_id="ENTRY_LOBBY"),  # excluded zone
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        zone_stage = next(s for s in resp.json()["stages"] if s["stage"] == "Zone Visit")
        assert zone_stage["count"] == 0

    @pytest.mark.asyncio
    async def test_funnel_reentry_not_double_counted(self, client):
        """REENTRY event must not add a second entry to the funnel."""
        vid = "VIS_REENTRY"
        events = [
            ev("ENTRY",   visitor_id=vid, dt=TODAY),
            ev("EXIT",    visitor_id=vid, dt=TODAY + timedelta(minutes=10)),
            ev("REENTRY", visitor_id=vid, dt=TODAY + timedelta(minutes=15)),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        entry_stage = next(s for s in resp.json()["stages"] if s["stage"] == "Entry")
        assert entry_stage["count"] == 1


# ── Heatmap tests ──────────────────────────────────────────────────────────────

class TestHeatmap:
    @pytest.mark.asyncio
    async def test_heatmap_normalised_score(self, client):
        """Most visited zone should have score 100."""
        events = []
        for _ in range(10):
            vid = f"VIS_{uuid.uuid4().hex[:6]}"
            events.append(ev("ZONE_ENTER", visitor_id=vid, zone_id="FOH"))
        for _ in range(5):
            vid = f"VIS_{uuid.uuid4().hex[:6]}"
            events.append(ev("ZONE_ENTER", visitor_id=vid, zone_id="DERMDOC"))

        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert zones["FOH"]["normalised_score"] == 100.0
        assert 0 < zones["DERMDOC"]["normalised_score"] < 100

    @pytest.mark.asyncio
    async def test_heatmap_data_confidence_below_20_sessions(self, client):
        """With fewer than 20 unique sessions, data_confidence should be False."""
        events = [ev("ZONE_ENTER", zone_id="FOH") for _ in range(3)]
        # Inject 3 entry events to create 3 sessions
        entry_events = [
            ev("ENTRY", visitor_id=f"VIS_{uuid.uuid4().hex[:6]}")
            for _ in range(3)
        ]
        await client.post("/events/ingest", json={"events": events + entry_events})
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        zones = resp.json()["zones"]
        if zones:
            assert all(not z["data_confidence"] for z in zones)

    @pytest.mark.asyncio
    async def test_heatmap_data_confidence_above_20_sessions(self, client):
        """With 20+ unique visitor sessions, data_confidence should be True."""
        events = []
        for i in range(25):
            vid = f"VIS_{i:03d}"
            events.append(ev("ENTRY",      visitor_id=vid))
            events.append(ev("ZONE_ENTER", visitor_id=vid, zone_id="MAYBELLINE"))

        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        zones = resp.json()["zones"]
        assert any(z["data_confidence"] for z in zones)

    @pytest.mark.asyncio
    async def test_heatmap_empty_store(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        assert resp.status_code == 200
        assert resp.json()["zones"] == []

    @pytest.mark.asyncio
    async def test_heatmap_avg_dwell_computed(self, client):
        vid = "VIS_DWELL"
        events = [
            ev("ZONE_ENTER", visitor_id=vid, zone_id="MINIMALIST",
               dt=TODAY),
            ev("ZONE_DWELL", visitor_id=vid, zone_id="MINIMALIST",
               dwell_ms=30000, dt=TODAY + timedelta(seconds=30)),
            ev("ZONE_DWELL", visitor_id=vid, zone_id="MINIMALIST",
               dwell_ms=30000, dt=TODAY + timedelta(seconds=60)),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert zones["MINIMALIST"]["avg_dwell_ms"] == pytest.approx(30000.0)


# ── Metrics computation tests ─────────────────────────────────────────────────

class TestMetricsComputation:
    @pytest.mark.asyncio
    async def test_conversion_rate_via_billing_correlation(self, client):
        """Visitor in billing zone → POS within 5 min → converted."""
        vid = "VIS_CONVERT"
        billing_time = TODAY + timedelta(hours=2)

        events = [
            ev("ENTRY",      visitor_id=vid, dt=TODAY + timedelta(hours=1)),
            ev("ZONE_ENTER", visitor_id=vid, zone_id="CASH_COUNTER", dt=billing_time),
        ]
        await client.post("/events/ingest", json={"events": events})

        # POS transaction 3 minutes after billing zone entry
        async with AsyncSessionLocal() as db:
            from app.database import POSTransaction
            db.add(POSTransaction(
                transaction_id="TXN_CONV_001",
                store_id=STORE_ID,
                timestamp=ts(billing_time + timedelta(minutes=3)),
                basket_value_inr=1200.0,
            ))
            await db.commit()

        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        assert body["conversion_rate"] > 0
        assert body["total_transactions"] == 1
        assert body["total_revenue_inr"] == pytest.approx(1200.0)

    @pytest.mark.asyncio
    async def test_no_conversion_outside_window(self, client):
        """POS transaction >5 min after billing zone entry should NOT count."""
        vid = "VIS_NO_CONV"
        billing_time = TODAY + timedelta(hours=3)

        events = [
            ev("ENTRY",      visitor_id=vid, dt=TODAY + timedelta(hours=2)),
            ev("ZONE_ENTER", visitor_id=vid, zone_id="CASH_COUNTER", dt=billing_time),
        ]
        await client.post("/events/ingest", json={"events": events})

        async with AsyncSessionLocal() as db:
            from app.database import POSTransaction
            db.add(POSTransaction(
                transaction_id="TXN_LATE_001",
                store_id=STORE_ID,
                timestamp=ts(billing_time + timedelta(minutes=10)),  # too late
                basket_value_inr=500.0,
            ))
            await db.commit()

        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        assert resp.json()["conversion_rate"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_abandonment_rate_computed(self, client):
        events = [
            ev("BILLING_QUEUE_JOIN",    visitor_id="VIS_A", zone_id="CASH_COUNTER", queue_depth=3),
            ev("BILLING_QUEUE_JOIN",    visitor_id="VIS_B", zone_id="CASH_COUNTER", queue_depth=3),
            ev("BILLING_QUEUE_ABANDON", visitor_id="VIS_A", zone_id="CASH_COUNTER"),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        assert body["abandonment_rate"] == pytest.approx(0.5, abs=0.01)


# ── Ingestion edge cases ──────────────────────────────────────────────────────

class TestIngestionEdgeCases:
    @pytest.mark.asyncio
    async def test_ingest_empty_batch(self, client):
        resp = await client.post("/events/ingest", json={"events": []})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 0

    @pytest.mark.asyncio
    async def test_ingest_returns_structured_errors(self, client):
        bad_events = [{"broken": "payload"}]
        resp = await client.post("/events/ingest", json={"events": bad_events})
        assert resp.status_code == 200
        body = resp.json()
        assert body["rejected"] >= 1
        assert isinstance(body["errors"], list)

    @pytest.mark.asyncio
    async def test_ingest_low_confidence_not_suppressed(self, client):
        """Low-confidence detections must be stored, not filtered."""
        event = ev("ENTRY", confidence=0.12)
        resp = await client.post("/events/ingest", json={"events": [event]})
        assert resp.json()["accepted"] == 1
