"""Event schema construction and emission (JSONL file + optional API POST)."""
from __future__ import annotations
import json
import uuid
import os
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, IO


def _ts(clip_start_utc: str, frame_idx: int, fps: float) -> str:
    """Compute ISO-8601 UTC timestamp from clip start + frame offset."""
    try:
        base = datetime.fromisoformat(clip_start_utc.replace("Z", "+00:00"))
    except ValueError:
        base = datetime.now(timezone.utc)
    offset = timedelta(seconds=frame_idx / max(fps, 1))
    return (base + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventEmitter:
    def __init__(
        self,
        output_path: str,
        api_url: Optional[str] = None,
        batch_size: int = 50,
    ):
        self.output_path = output_path
        self.api_url     = api_url
        self.batch_size  = batch_size
        self._buffer: list = []
        self._fh: Optional[IO] = None
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def open(self):
        self._fh = open(self.output_path, "a", encoding="utf-8")

    def close(self):
        self._flush()
        if self._fh:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def emit(self, event: dict):
        if not event.get("event_id"):
            event["event_id"] = str(uuid.uuid4())
        line = json.dumps(event, ensure_ascii=False)
        if self._fh:
            self._fh.write(line + "\n")
        self._buffer.append(event)
        if len(self._buffer) >= self.batch_size:
            self._flush()

    def _flush(self):
        if not self._buffer or not self.api_url:
            self._buffer.clear()
            return
        try:
            resp = requests.post(
                f"{self.api_url}/events/ingest",
                json={"events": self._buffer},
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                print(f"[emit] API ingest warning: {resp.status_code}")
        except Exception as exc:
            print(f"[emit] API ingest failed: {exc}")
        finally:
            self._buffer.clear()

    # ── Event builders ────────────────────────────────────────────────────────

    def entry(self, store_id, camera_id, visitor_id, frame_idx, fps, clip_start,
              confidence, is_staff, session_seq=0):
        self.emit({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera_id,
            "visitor_id": visitor_id,
            "event_type": "ENTRY",
            "timestamp":  _ts(clip_start, frame_idx, fps),
            "zone_id":    None,
            "dwell_ms":   0,
            "is_staff":   is_staff,
            "confidence": round(confidence, 3),
            "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": session_seq},
        })

    def exit(self, store_id, camera_id, visitor_id, frame_idx, fps, clip_start,
             confidence, is_staff, session_seq=0):
        self.emit({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera_id,
            "visitor_id": visitor_id,
            "event_type": "EXIT",
            "timestamp":  _ts(clip_start, frame_idx, fps),
            "zone_id":    None,
            "dwell_ms":   0,
            "is_staff":   is_staff,
            "confidence": round(confidence, 3),
            "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": session_seq},
        })

    def reentry(self, store_id, camera_id, visitor_id, frame_idx, fps, clip_start,
                confidence, is_staff, session_seq=0):
        self.emit({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera_id,
            "visitor_id": visitor_id,
            "event_type": "REENTRY",
            "timestamp":  _ts(clip_start, frame_idx, fps),
            "zone_id":    None,
            "dwell_ms":   0,
            "is_staff":   is_staff,
            "confidence": round(confidence, 3),
            "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": session_seq},
        })

    def zone_enter(self, store_id, camera_id, visitor_id, zone_id, frame_idx, fps, clip_start,
                   confidence, is_staff, sku_zone=None, session_seq=0):
        self.emit({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera_id,
            "visitor_id": visitor_id,
            "event_type": "ZONE_ENTER",
            "timestamp":  _ts(clip_start, frame_idx, fps),
            "zone_id":    zone_id,
            "dwell_ms":   0,
            "is_staff":   is_staff,
            "confidence": round(confidence, 3),
            "metadata":   {"queue_depth": None, "sku_zone": sku_zone, "session_seq": session_seq},
        })

    def zone_exit(self, store_id, camera_id, visitor_id, zone_id, dwell_ms,
                  frame_idx, fps, clip_start, confidence, is_staff, session_seq=0):
        self.emit({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera_id,
            "visitor_id": visitor_id,
            "event_type": "ZONE_EXIT",
            "timestamp":  _ts(clip_start, frame_idx, fps),
            "zone_id":    zone_id,
            "dwell_ms":   dwell_ms,
            "is_staff":   is_staff,
            "confidence": round(confidence, 3),
            "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": session_seq},
        })

    def zone_dwell(self, store_id, camera_id, visitor_id, zone_id, dwell_ms,
                   frame_idx, fps, clip_start, confidence, is_staff, session_seq=0):
        self.emit({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera_id,
            "visitor_id": visitor_id,
            "event_type": "ZONE_DWELL",
            "timestamp":  _ts(clip_start, frame_idx, fps),
            "zone_id":    zone_id,
            "dwell_ms":   dwell_ms,
            "is_staff":   is_staff,
            "confidence": round(confidence, 3),
            "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": session_seq},
        })

    def billing_queue_join(self, store_id, camera_id, visitor_id, queue_depth,
                           frame_idx, fps, clip_start, confidence, is_staff, session_seq=0):
        self.emit({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera_id,
            "visitor_id": visitor_id,
            "event_type": "BILLING_QUEUE_JOIN",
            "timestamp":  _ts(clip_start, frame_idx, fps),
            "zone_id":    "CASH_COUNTER",
            "dwell_ms":   0,
            "is_staff":   is_staff,
            "confidence": round(confidence, 3),
            "metadata":   {"queue_depth": queue_depth, "sku_zone": None, "session_seq": session_seq},
        })

    def billing_queue_abandon(self, store_id, camera_id, visitor_id, dwell_ms,
                              frame_idx, fps, clip_start, confidence, is_staff, session_seq=0):
        self.emit({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera_id,
            "visitor_id": visitor_id,
            "event_type": "BILLING_QUEUE_ABANDON",
            "timestamp":  _ts(clip_start, frame_idx, fps),
            "zone_id":    "CASH_COUNTER",
            "dwell_ms":   dwell_ms,
            "is_staff":   is_staff,
            "confidence": round(confidence, 3),
            "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": session_seq},
        })
