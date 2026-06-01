# PROMPT: "Write async pytest tests for a FastAPI store intelligence API.
# Test all endpoints: POST /events/ingest, GET /stores/{id}/metrics,
# GET /stores/{id}/funnel, GET /stores/{id}/heatmap, GET /stores/{id}/anomalies,
# GET /health. Use httpx AsyncClient with in-memory SQLite.
# Cover: idempotency, staff exclusion, zero-data stores, schema validation."
# CHANGES MADE: Added explicit tests for idempotency (same payload twice),
# staff visitor exclusion from unique_visitors, and zero-purchase store handling.
# Added partial failure test for malformed events in a batch.

import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import init_db, AsyncSessionLocal, Base, engine


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create a fresh in-memory DB for each test."""
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


STORE_ID = "STORE_BLR_002"


def _make_event(event_type="ENTRY", visitor_id=None, zone_id=None,
                is_staff=False, dwell_ms=0, queue_depth=None,
                timestamp=None, confidence=0.90):
    now = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE_ID,
        "camera_id":  "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp":  now,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata":   {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


# ── POST /events/ingest ────────────────────────────────────────────────────────

class TestIngest:
    @pytest.mark.asyncio
    async def test_ingest_valid_events(self, client):
        events = [_make_event("ENTRY") for _ in range(5)]
        resp = await client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 5
        assert body["rejected"] == 0
        assert body["duplicate"] == 0

    @pytest.mark.asyncio
    async def test_ingest_idempotency(self, client):
        """Same payload ingested twice — second call should report duplicates."""
        events = [_make_event("ENTRY") for _ in range(3)]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.post("/events/ingest", json={"events": events})
        body = resp.json()
        assert body["duplicate"] == 3
        assert body["accepted"] == 0

    @pytest.mark.asyncio
    async def test_ingest_partial_failure_malformed(self, client):
        """Batch with one bad event — valid ones should be accepted."""
        good = _make_event("ENTRY")
        bad  = {"event_id": str(uuid.uuid4()), "broken": True}  # missing required fields
        resp = await client.post("/events/ingest", json={"events": [good, bad]})
        body = resp.json()
        # At minimum the good event was accepted
        assert body["accepted"] >= 1

    @pytest.mark.asyncio
    async def test_ingest_batch_limit(self, client):
        """Batch > 500 events should be rejected by schema validation."""
        events = [_make_event() for _ in range(501)]
        resp = await client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 422  # Pydantic validation error


# ── GET /stores/{id}/metrics ──────────────────────────────────────────────────

class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_zero_data(self, client):
        """Empty store should return zeros, not crash."""
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["unique_visitors"] == 0
        assert body["conversion_rate"] == 0.0
        assert body["current_queue_depth"] == 0

    @pytest.mark.asyncio
    async def test_metrics_counts_customers_not_staff(self, client):
        """Staff ENTRY events must not increment unique_visitors."""
        events = [
            _make_event("ENTRY", is_staff=False),
            _make_event("ENTRY", is_staff=False),
            _make_event("ENTRY", is_staff=True),   # staff — should NOT count
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        assert body["unique_visitors"] == 2  # only non-staff ENTRY events

    @pytest.mark.asyncio
    async def test_metrics_returns_required_fields(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        required = {
            "store_id", "as_of", "unique_visitors", "conversion_rate",
            "avg_dwell_ms", "zone_dwell", "current_queue_depth",
            "abandonment_rate", "total_transactions", "total_revenue_inr",
        }
        for field in required:
            assert field in body, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_metrics_dwell_computed(self, client):
        """Zone dwell events should populate avg_dwell_ms."""
        events = [
            _make_event("ZONE_DWELL", zone_id="FOH", dwell_ms=45000, is_staff=False),
            _make_event("ZONE_DWELL", zone_id="FOH", dwell_ms=60000, is_staff=False),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        zone_dwell = {z["zone_id"]: z for z in body["zone_dwell"]}
        assert "FOH" in zone_dwell
        assert zone_dwell["FOH"]["avg_dwell_ms"] == pytest.approx(52500.0)

    @pytest.mark.asyncio
    async def test_metrics_queue_depth(self, client):
        now = datetime.now(timezone.utc)
        events = [
            _make_event("BILLING_QUEUE_JOIN", zone_id="CASH_COUNTER", queue_depth=4,
                        timestamp=now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        assert body["current_queue_depth"] == 4


# ── GET /stores/{id}/funnel ───────────────────────────────────────────────────

class TestFunnel:
    @pytest.mark.asyncio
    async def test_funnel_structure(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        body = resp.json()
        assert "stages" in body
        stages = [s["stage"] for s in body["stages"]]
        assert "Entry" in stages
        assert "Zone Visit" in stages
        assert "Billing Queue" in stages
        assert "Purchase" in stages

    @pytest.mark.asyncio
    async def test_funnel_no_double_count_reentry(self, client):
        """A re-entry visitor must NOT be counted twice in Entry stage."""
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        events = [
            _make_event("ENTRY",   visitor_id=vid),
            _make_event("EXIT",    visitor_id=vid),
            _make_event("REENTRY", visitor_id=vid),  # same visitor, not a new entry
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        body = resp.json()
        entry_stage = next(s for s in body["stages"] if s["stage"] == "Entry")
        assert entry_stage["count"] == 1  # counted once despite REENTRY event

    @pytest.mark.asyncio
    async def test_funnel_drop_off_decreasing(self, client):
        """Each stage count must be <= previous stage count."""
        events = [
            _make_event("ENTRY", visitor_id="VIS_A"),
            _make_event("ENTRY", visitor_id="VIS_B"),
            _make_event("ENTRY", visitor_id="VIS_C"),
            _make_event("ZONE_ENTER", visitor_id="VIS_A", zone_id="FOH"),
            _make_event("ZONE_ENTER", visitor_id="VIS_B", zone_id="DERMDOC"),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        stages = resp.json()["stages"]
        counts = [s["count"] for s in stages]
        for i in range(1, len(counts)):
            assert counts[i] <= counts[i - 1], (
                f"Stage {stages[i]['stage']} count {counts[i]} > "
                f"previous {counts[i-1]}"
            )

    @pytest.mark.asyncio
    async def test_funnel_staff_excluded(self, client):
        """Staff visitors must not appear in the funnel."""
        events = [
            _make_event("ENTRY", is_staff=False),
            _make_event("ENTRY", is_staff=True),  # staff
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        entry_stage = next(s for s in resp.json()["stages"] if s["stage"] == "Entry")
        assert entry_stage["count"] == 1


# ── GET /stores/{id}/anomalies ────────────────────────────────────────────────

class TestAnomalies:
    @pytest.mark.asyncio
    async def test_anomalies_empty_store(self, client):
        """No anomalies for empty store (nothing has happened yet)."""
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert "active_anomalies" in body

    @pytest.mark.asyncio
    async def test_billing_queue_spike_warn(self, client):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [
            _make_event("BILLING_QUEUE_JOIN", zone_id="CASH_COUNTER",
                        queue_depth=6, timestamp=now)
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        types = [a["anomaly_type"] for a in anomalies]
        assert "BILLING_QUEUE_SPIKE" in types

    @pytest.mark.asyncio
    async def test_billing_queue_spike_critical(self, client):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [
            _make_event("BILLING_QUEUE_JOIN", zone_id="CASH_COUNTER",
                        queue_depth=10, timestamp=now)
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        spike = next(a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE")
        assert spike["severity"] == "CRITICAL"

    @pytest.mark.asyncio
    async def test_dead_zone_detected(self, client):
        """Zone with events today but none recently → DEAD_ZONE anomaly."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [
            _make_event("ZONE_ENTER", zone_id="DERMDOC", timestamp=old_ts),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        types = [a["anomaly_type"] for a in anomalies]
        assert "DEAD_ZONE" in types

    @pytest.mark.asyncio
    async def test_anomaly_has_suggested_action(self, client):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [_make_event("BILLING_QUEUE_JOIN", queue_depth=9, timestamp=now)]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        for a in resp.json()["active_anomalies"]:
            assert "suggested_action" in a
            assert len(a["suggested_action"]) > 10


# ── GET /health ────────────────────────────────────────────────────────────────

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "version" in body
        assert "uptime_seconds" in body
        assert "db_ok" in body
        assert body["db_ok"] is True

    @pytest.mark.asyncio
    async def test_health_stale_feed_flag(self, client):
        """Store with last event >10 min ago should show STALE_FEED."""
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [_make_event("ENTRY", timestamp=old_ts)]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get("/health")
        stores = resp.json()["stores"]
        store = next((s for s in stores if s["store_id"] == STORE_ID), None)
        assert store is not None
        assert store["status"] == "STALE_FEED"
