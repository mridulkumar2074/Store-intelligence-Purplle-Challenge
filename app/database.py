from __future__ import annotations
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import (
    Column, String, Boolean, Float, Integer, Text, Index, event
)
from sqlalchemy.orm import DeclarativeBase

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{DATA_DIR}/store_intelligence.db"
)


class Base(DeclarativeBase):
    pass


class EventRecord(Base):
    __tablename__ = "events"

    event_id     = Column(String(36), primary_key=True)
    store_id     = Column(String(50), nullable=False)
    camera_id    = Column(String(50), nullable=False)
    visitor_id   = Column(String(50), nullable=False)
    event_type   = Column(String(30), nullable=False)
    timestamp    = Column(String(30), nullable=False)
    zone_id      = Column(String(50))
    dwell_ms     = Column(Integer, default=0)
    is_staff     = Column(Boolean, default=False)
    confidence   = Column(Float, default=0.0)
    queue_depth  = Column(Integer)
    sku_zone     = Column(String(50))
    session_seq  = Column(Integer, default=0)
    ingested_at  = Column(String(30))

    __table_args__ = (
        Index("ix_events_store_ts",   "store_id", "timestamp"),
        Index("ix_events_visitor",    "visitor_id"),
        Index("ix_events_type",       "event_type"),
        Index("ix_events_staff",      "is_staff"),
        Index("ix_events_zone",       "zone_id"),
    )


class POSTransaction(Base):
    __tablename__ = "pos_transactions"

    transaction_id    = Column(String(80), primary_key=True)
    store_id          = Column(String(50), nullable=False)
    timestamp         = Column(String(30), nullable=False)
    basket_value_inr  = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_pos_store_ts", "store_id", "timestamp"),
    )


engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
