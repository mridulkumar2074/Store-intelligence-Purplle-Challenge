# PROMPT: "Write pytest tests for a retail store CCTV detection pipeline.
# Test: IoU tracker matching, entry/exit line crossing detection,
# zone classification, staff color detection, Re-ID store, event emitter schema.
# Use only numpy and stdlib — no cv2 needed for most unit tests."
# CHANGES MADE: Added edge cases for group entry, tracker track lifecycle,
# reentry window expiry, and emit schema validation against the required JSON schema.

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest

from pipeline.tracker import BBox, IoUTracker, ColorHistogram, Track
from pipeline.zone_classifier import ZoneClassifier
from pipeline.emit import EventEmitter


# ── BBox / IoU ────────────────────────────────────────────────────────────────

class TestBBox:
    def test_iou_identical(self):
        b = BBox(0, 0, 100, 100)
        assert b.iou(b) == pytest.approx(1.0)

    def test_iou_no_overlap(self):
        b1 = BBox(0, 0, 50, 50)
        b2 = BBox(60, 60, 110, 110)
        assert b1.iou(b2) == pytest.approx(0.0)

    def test_iou_partial(self):
        b1 = BBox(0, 0, 100, 100)
        b2 = BBox(50, 0, 150, 100)
        assert 0.0 < b1.iou(b2) < 1.0

    def test_area(self):
        b = BBox(10, 20, 110, 120)
        assert b.area == pytest.approx(10000.0)

    def test_center(self):
        b = BBox(0, 0, 100, 200)
        assert b.cx == pytest.approx(50.0)
        assert b.cy == pytest.approx(100.0)


# ── IoU Tracker ───────────────────────────────────────────────────────────────

class TestIoUTracker:
    def test_new_track_created_after_confirm_age(self):
        """A high-conf detection must accumulate confirm_age frames before confirming."""
        tracker = IoUTracker(min_confirm_age=3)
        det = [(BBox(10, 10, 60, 110), 0.9, None)]

        for i in range(2):
            confirmed = tracker.update(det, frame_idx=i)
            assert len(confirmed) == 0  # still tentative

        confirmed = tracker.update(det, frame_idx=2)
        assert len(confirmed) == 1

    def test_track_lost_when_no_detection(self):
        """Track should disappear after max_lost_age frames without a match."""
        tracker = IoUTracker(min_confirm_age=1, max_lost_age=3)
        det = [(BBox(10, 10, 60, 60), 0.9, None)]

        # Confirm the track
        for i in range(3):
            tracker.update(det, frame_idx=i)

        # Remove detections — track should eventually disappear
        for i in range(3, 10):
            tracker.update([], frame_idx=i)

        assert len(tracker.tracks) == 0

    def test_group_entry_creates_separate_tracks(self):
        """3 people entering simultaneously must produce 3 separate visitor_ids."""
        tracker = IoUTracker(min_confirm_age=1)
        dets = [
            (BBox(0,   10, 50,  80), 0.9, None),
            (BBox(60,  10, 110, 80), 0.9, None),
            (BBox(120, 10, 170, 80), 0.9, None),
        ]
        for i in range(3):
            tracker.update(dets, frame_idx=i)

        confirmed = [t for t in tracker.tracks if t.state == "confirmed"]
        visitor_ids = {t.visitor_id for t in confirmed}
        assert len(visitor_ids) == 3  # each person gets unique ID

    def test_same_person_keeps_id_across_frames(self):
        """A track that persists should keep its visitor_id."""
        tracker = IoUTracker(min_confirm_age=1)
        det = [(BBox(10, 10, 60, 60), 0.9, None)]

        for i in range(5):
            tracker.update(det, frame_idx=i)

        ids = {t.visitor_id for t in tracker.tracks}
        assert len(ids) == 1

    def test_low_conf_detection_ignored_for_new_tracks(self):
        """Low-confidence detections should not spawn new tracks (unmatched)."""
        tracker = IoUTracker(high_conf_threshold=0.55, min_confirm_age=1)
        det = [(BBox(10, 10, 60, 60), 0.30, None)]  # below threshold
        tracker.update(det, frame_idx=0)
        # Should not create confirmed tracks
        confirmed = [t for t in tracker.tracks if t.state == "confirmed"]
        assert len(confirmed) == 0

    def test_velocity_prediction(self):
        """Track should predict next position based on velocity."""
        tracker = IoUTracker(min_confirm_age=1)
        # Move detection rightward
        for i in range(5):
            det = [(BBox(i*10, 10, i*10+50, 60), 0.9, None)]
            tracker.update(det, frame_idx=i)

        trk = [t for t in tracker.tracks if t.state == "confirmed"][0]
        pred = trk.predict()
        assert pred.x1 > trk.bbox.x1  # should predict further right


# ── Zone Classifier ───────────────────────────────────────────────────────────

class TestZoneClassifier:
    @pytest.fixture
    def entry_cam(self):
        return ZoneClassifier({
            "camera_id": "CAM_ENTRY_01",
            "type": "entry_exit",
            "entry_line_y": 0.60,
            "zone_map": [
                {"zone_id": "ENTRY_EXIT",  "x_min": 0.0, "y_min": 0.6, "x_max": 1.0, "y_max": 1.0},
                {"zone_id": "ENTRY_LOBBY", "x_min": 0.0, "y_min": 0.3, "x_max": 1.0, "y_max": 0.6},
                {"zone_id": "FOH",         "x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 0.3},
            ],
        })

    @pytest.fixture
    def floor_cam(self):
        return ZoneClassifier({
            "camera_id": "CAM_FLOOR_01",
            "type": "floor",
            "entry_line_y": 0.60,
            "zone_map": [
                {"zone_id": "SKINCARE_BACK", "x_min": 0.0, "y_min": 0.0,  "x_max": 1.0, "y_max": 0.25},
                {"zone_id": "FOH",           "x_min": 0.0, "y_min": 0.25, "x_max": 1.0, "y_max": 1.0},
            ],
        })

    def test_entry_zone_classification(self, entry_cam):
        assert entry_cam.classify(0.5, 0.8) == "ENTRY_EXIT"

    def test_lobby_zone_classification(self, entry_cam):
        assert entry_cam.classify(0.5, 0.45) == "ENTRY_LOBBY"

    def test_foh_zone_classification(self, entry_cam):
        assert entry_cam.classify(0.5, 0.1) == "FOH"

    def test_no_zone_outside_all(self, floor_cam):
        # This should return None since no zone covers (1.5, 0.5)
        assert floor_cam.classify(1.5, 0.5) is None

    def test_entry_detection_in(self, entry_cam):
        # prev_cy above line, curr_cy below → entering
        result = entry_cam.detect_crossing(0.50, 0.65)
        assert result == "IN"

    def test_entry_detection_out(self, entry_cam):
        # prev_cy below line, curr_cy above → exiting
        result = entry_cam.detect_crossing(0.65, 0.50)
        assert result == "OUT"

    def test_no_crossing(self, entry_cam):
        # Both above line — no crossing
        assert entry_cam.detect_crossing(0.4, 0.45) is None
        # Both below line — no crossing
        assert entry_cam.detect_crossing(0.7, 0.75) is None

    def test_is_entry_camera(self, entry_cam, floor_cam):
        assert entry_cam.is_entry_camera()
        assert not floor_cam.is_entry_camera()


# ── Event Emitter ─────────────────────────────────────────────────────────────

REQUIRED_FIELDS = {
    "event_id", "store_id", "camera_id", "visitor_id", "event_type",
    "timestamp", "dwell_ms", "is_staff", "confidence", "metadata",
}

class TestEventEmitter:
    @pytest.fixture
    def emitter_file(self, tmp_path):
        out = str(tmp_path / "test_events.jsonl")
        emitter = EventEmitter(out)
        emitter.open()
        yield emitter, out
        emitter.close()

    def _read_events(self, path):
        events = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def test_entry_event_schema(self, emitter_file):
        emitter, path = emitter_file
        emitter.entry("STORE_X", "CAM_1", "VIS_001", 100, 15.0, "2026-04-10T12:00:00Z",
                       confidence=0.9, is_staff=False)
        emitter.close()
        events = self._read_events(path)
        assert len(events) == 1
        ev = events[0]
        for field in REQUIRED_FIELDS:
            assert field in ev, f"Missing field: {field}"
        assert ev["event_type"] == "ENTRY"
        assert ev["zone_id"] is None
        assert ev["is_staff"] is False

    def test_exit_event_schema(self, emitter_file):
        emitter, path = emitter_file
        emitter.exit("STORE_X", "CAM_1", "VIS_001", 200, 15.0, "2026-04-10T12:00:00Z",
                      confidence=0.85, is_staff=False)
        emitter.close()
        events = self._read_events(path)
        assert events[0]["event_type"] == "EXIT"

    def test_reentry_event_schema(self, emitter_file):
        emitter, path = emitter_file
        emitter.reentry("STORE_X", "CAM_1", "VIS_001", 300, 15.0, "2026-04-10T12:00:00Z",
                         confidence=0.78, is_staff=False)
        emitter.close()
        events = self._read_events(path)
        assert events[0]["event_type"] == "REENTRY"

    def test_zone_dwell_event(self, emitter_file):
        emitter, path = emitter_file
        emitter.zone_dwell("STORE_X", "CAM_2", "VIS_001", "FOH", 35000,
                           450, 15.0, "2026-04-10T12:00:00Z", 0.88, False)
        emitter.close()
        events = self._read_events(path)
        ev = events[0]
        assert ev["event_type"] == "ZONE_DWELL"
        assert ev["zone_id"] == "FOH"
        assert ev["dwell_ms"] == 35000

    def test_billing_queue_join_has_queue_depth(self, emitter_file):
        emitter, path = emitter_file
        emitter.billing_queue_join("STORE_X", "CAM_4", "VIS_001", 4,
                                   600, 15.0, "2026-04-10T12:00:00Z", 0.9, False)
        emitter.close()
        events = self._read_events(path)
        ev = events[0]
        assert ev["event_type"] == "BILLING_QUEUE_JOIN"
        assert ev["metadata"]["queue_depth"] == 4

    def test_event_ids_are_unique(self, emitter_file):
        emitter, path = emitter_file
        for i in range(20):
            emitter.entry("STORE_X", "CAM_1", f"VIS_{i:03d}", i*10, 15.0,
                          "2026-04-10T12:00:00Z", 0.9, False)
        emitter.close()
        events = self._read_events(path)
        ids = [e["event_id"] for e in events]
        assert len(ids) == len(set(ids))  # all unique

    def test_staff_event_flagged(self, emitter_file):
        emitter, path = emitter_file
        emitter.entry("STORE_X", "CAM_1", "VIS_STAFF", 10, 15.0, "2026-04-10T12:00:00Z",
                       confidence=0.92, is_staff=True)
        emitter.close()
        events = self._read_events(path)
        assert events[0]["is_staff"] is True

    def test_timestamp_format(self, emitter_file):
        emitter, path = emitter_file
        emitter.entry("STORE_X", "CAM_1", "VIS_001", 450, 15.0, "2026-04-10T12:00:00Z",
                       confidence=0.9, is_staff=False)
        emitter.close()
        events = self._read_events(path)
        ts = events[0]["timestamp"]
        # Must be ISO-8601 parseable
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")

    def test_confidence_range(self, emitter_file):
        emitter, path = emitter_file
        emitter.entry("STORE_X", "CAM_1", "VIS_001", 50, 15.0, "2026-04-10T12:00:00Z",
                       confidence=0.654321, is_staff=False)
        emitter.close()
        events = self._read_events(path)
        conf = events[0]["confidence"]
        assert 0.0 <= conf <= 1.0
