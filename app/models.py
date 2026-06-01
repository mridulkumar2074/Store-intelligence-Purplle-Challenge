from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
from enum import Enum
import uuid


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class StoreEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: str
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class IngestRequest(BaseModel):
    # Accept raw dicts so per-event validation can produce partial success
    events: List[Any] = Field(max_length=500)


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: List[Dict[str, Any]] = []


# ── Metrics ──────────────────────────────────────────────────────────────────

class ZoneDwellMetric(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    as_of: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_ms: float
    zone_dwell: List[ZoneDwellMetric]
    current_queue_depth: int
    abandonment_rate: float
    total_transactions: int
    total_revenue_inr: float


# ── Funnel ────────────────────────────────────────────────────────────────────

class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    as_of: str
    stages: List[FunnelStage]
    conversion_rate: float


# ── Heatmap ───────────────────────────────────────────────────────────────────

class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_ms: float
    normalised_score: float
    data_confidence: bool


class HeatmapResponse(BaseModel):
    store_id: str
    as_of: str
    zones: List[HeatmapZone]


# ── Anomalies ─────────────────────────────────────────────────────────────────

class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"
    STALE_FEED = "STALE_FEED"
    LOW_TRAFFIC = "LOW_TRAFFIC"


class Anomaly(BaseModel):
    anomaly_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    detected_at: str
    description: str
    suggested_action: str
    metadata: Dict[str, Any] = {}


class AnomaliesResponse(BaseModel):
    store_id: str
    as_of: str
    active_anomalies: List[Anomaly]


# ── Health ────────────────────────────────────────────────────────────────────

class StoreHealth(BaseModel):
    store_id: str
    last_event_at: Optional[str]
    events_last_hour: int
    status: str  # "OK" | "STALE_FEED" | "NO_DATA"


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    stores: List[StoreHealth]
    db_ok: bool
