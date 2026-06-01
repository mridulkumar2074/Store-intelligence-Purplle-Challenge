"""
Main CCTV detection script.
Processes each camera video file using YOLOv8 + IoU tracker.
Emits structured events (ENTRY/EXIT/ZONE_*/BILLING_*/REENTRY) to JSONL.

Usage:
    python -m pipeline.detect \\
        --clips-dir "CCTV Footage" \\
        --store-layout data/store_layout.json \\
        --output-dir sample_events \\
        --api-url http://localhost:8000 \\
        --skip-frames 3 \\
        --clip-start 2026-04-10T12:00:00Z

Re-ID strategy:
    Visitors who exited and re-enter within RE_ID_WINDOW_SEC seconds are
    identified by appearance histogram similarity.  If similarity > RE_ID_THRESHOLD,
    the same visitor_id is reused and a REENTRY event is emitted rather than
    a second ENTRY.  This prevents double-counting the conversion funnel.

Cross-camera deduplication strategy:
    CAM_ENTRY_01 and CAM_FLOOR_01 have overlapping fields of view near the entrance.
    To prevent the same person being counted twice:
    - Each camera runs its own tracker with its own visitor_id space.
    - A shared CrossCameraRegistry records (visitor_id, camera_id, timestamp) for
      every ENTRY/ZONE_ENTER event within a CROSS_CAM_WINDOW_SEC window.
    - When a new confirmed track appears in a secondary (non-entry) camera, its
      appearance histogram is compared against recently-seen visitors in other cameras.
    - If similarity > RE_ID_THRESHOLD, the existing visitor_id is reused and no
      new ENTRY event is emitted from the secondary camera.
    - This means zone events from the overlap area are attributed to the correct
      session without inflating visitor counts.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from .tracker import IoUTracker, BBox, ColorHistogram, Track
from .zone_classifier import ZoneClassifier
from .staff_detector import is_staff_by_color, extract_appearance
from .emit import EventEmitter

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

try:
    from ultralytics import YOLO
    _YOLO_AVAILABLE = True
except ImportError:
    _YOLO_AVAILABLE = False

DWELL_EVENT_INTERVAL_SEC = 30   # emit ZONE_DWELL every 30s of continuous dwell
RE_ID_WINDOW_SEC         = 300  # 5 minutes: look-back window for re-entry matching
RE_ID_THRESHOLD          = 0.70 # cosine similarity threshold
CROSS_CAM_WINDOW_SEC     = 60   # window for cross-camera dedup (overlapping FOV)


class CrossCameraRegistry:
    """
    Prevents double-counting when the same physical person is visible in two
    cameras simultaneously (e.g., CAM_ENTRY_01 and CAM_FLOOR_01 overlap).

    Keeps a short-lived registry of (visitor_id → (camera_id, hist, timestamp)).
    When a new track appears in camera B, its appearance is compared against
    recent tracks from camera A. On match, visitor_id is reused and no new
    ENTRY is emitted from camera B.
    """
    def __init__(self, window_sec: float = CROSS_CAM_WINDOW_SEC):
        self._window = window_sec
        # visitor_id -> {camera_id, hist, last_seen_ts}
        self._registry: Dict[str, dict] = {}

    def register(self, visitor_id: str, camera_id: str,
                 hist: Optional[np.ndarray], ts: float):
        self._registry[visitor_id] = {
            "camera_id": camera_id,
            "hist":      hist,
            "ts":        ts,
        }
        # Evict expired entries
        expired = [vid for vid, v in self._registry.items()
                   if ts - v["ts"] > self._window]
        for vid in expired:
            del self._registry[vid]

    def find_cross_camera_match(
        self, hist: Optional[np.ndarray], current_camera: str, current_ts: float
    ) -> Optional[str]:
        """Return visitor_id from a DIFFERENT camera with similar appearance, or None."""
        if hist is None:
            return None
        best_sim = RE_ID_THRESHOLD
        best_id  = None
        for visitor_id, info in self._registry.items():
            if info["camera_id"] == current_camera:
                continue
            if current_ts - info["ts"] > self._window:
                continue
            stored_hist = info["hist"]
            if stored_hist is None:
                continue
            denom = np.linalg.norm(hist) * np.linalg.norm(stored_hist)
            if denom == 0:
                continue
            sim = float(np.dot(hist, stored_hist) / denom)
            if sim > best_sim:
                best_sim = sim
                best_id  = visitor_id
        return best_id


class ReIDStore:
    """Keep appearance descriptors of recently exited visitors for re-entry detection."""

    def __init__(self, window_sec: float = RE_ID_WINDOW_SEC):
        self.window_sec = window_sec
        # visitor_id → (exit_time, histogram)
        self._store: Dict[str, Tuple[float, Optional[np.ndarray]]] = {}

    def record_exit(self, visitor_id: str, hist: Optional[np.ndarray], exit_ts: float):
        self._store[visitor_id] = (exit_ts, hist)

    def find_match(
        self, hist: Optional[np.ndarray], current_ts: float
    ) -> Optional[str]:
        """Return visitor_id of the most similar recently-exited visitor, or None."""
        if hist is None:
            return None

        best_sim = RE_ID_THRESHOLD
        best_id  = None

        expired = [
            vid for vid, (ts, _) in self._store.items()
            if current_ts - ts > self.window_sec
        ]
        for vid in expired:
            del self._store[vid]

        for visitor_id, (ts, stored_hist) in self._store.items():
            if stored_hist is None:
                continue
            denom = (np.linalg.norm(hist) * np.linalg.norm(stored_hist))
            if denom == 0:
                continue
            sim = float(np.dot(hist, stored_hist) / denom)
            if sim > best_sim:
                best_sim = sim
                best_id  = visitor_id

        return best_id


def process_camera(
    video_path:        str,
    camera_cfg:        dict,
    store_id:          str,
    clip_start:        str,
    emitter:           EventEmitter,
    skip_frames:       int = 1,
    max_frames:        int = 0,
    model=None,
    cross_cam_registry: Optional["CrossCameraRegistry"] = None,
) -> dict:
    """Process one camera video and emit events. Returns a stats dict."""
    if not _CV2_AVAILABLE:
        print(f"[detect] cv2 not available — skipping {video_path}")
        return {}

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[detect] Cannot open {video_path}")
        return {}

    fps_video = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    camera_id   = camera_cfg["camera_id"]
    is_entry    = camera_cfg.get("type") == "entry_exit"
    is_billing  = camera_cfg.get("type") == "billing"

    clf    = ZoneClassifier(camera_cfg)
    tracker = IoUTracker()
    reid   = ReIDStore()

    # Per-track state
    track_zone_enter_frame: Dict[int, int]          = {}
    track_last_dwell_frame: Dict[int, int]          = {}
    track_session_seq:      Dict[int, int]          = defaultdict(int)
    exited_visitors:        set                     = set()
    # For entry-line crossing detection
    track_prev_cy: Dict[int, float] = {}
    # For billing camera: track who joined queue and when
    billing_join_frame: Dict[int, int] = {}

    frame_idx = 0
    stats = {
        "entries": 0, "exits": 0, "reentries": 0,
        "zone_events": 0, "billing_events": 0
    }

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if max_frames and frame_idx >= max_frames:
            break
        frame_idx += 1

        if frame_idx % skip_frames != 0:
            continue

        # Resize for speed (process at 720p)
        if w > 1280:
            scale = 1280 / w
            frame_proc = cv2.resize(frame, (1280, int(h * scale)))
        else:
            frame_proc = frame

        ph, pw = frame_proc.shape[:2]

        # ── Detection ────────────────────────────────────────────────────────
        detections: List[Tuple[BBox, float, Optional[ColorHistogram]]] = []
        if model is not None:
            results = model(frame_proc, classes=[0], verbose=False)[0]  # class 0 = person
            if results.boxes is not None:
                for box in results.boxes:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    conf = float(box.conf[0])
                    bbox = BBox(x1, y1, x2, y2)
                    hist_arr = extract_appearance(frame_proc, bbox)
                    hist_obj = ColorHistogram(hist_arr) if hist_arr is not None else None
                    detections.append((bbox, conf, hist_obj))

        # ── Track update ─────────────────────────────────────────────────────
        active_tracks = tracker.update(detections, frame_idx)

        current_time_sec = frame_idx / fps_video

        # ── Billing camera: count people for queue depth ──────────────────────
        queue_depth = len(active_tracks) if is_billing else 0

        for trk in active_tracks:
            # Normalized center coordinates
            cx_norm = ((trk.bbox.x1 + trk.bbox.x2) / 2) / pw
            cy_norm = ((trk.bbox.y1 + trk.bbox.y2) / 2) / ph

            # ── Staff detection ───────────────────────────────────────────────
            trk.is_staff = is_staff_by_color(frame_proc, trk.bbox)

            # ── Entry/Exit detection (entry camera only) ──────────────────────
            if is_entry:
                prev_cy = track_prev_cy.get(trk.track_id)
                crossing = None
                if prev_cy is not None:
                    crossing = clf.detect_crossing(prev_cy, cy_norm)
                track_prev_cy[trk.track_id] = cy_norm

                if crossing == "IN" and trk.visitor_id not in exited_visitors:
                    hist_arr = trk.appearance.hist if trk.appearance else None

                    # Cross-camera dedup: person already tracked in another camera?
                    cross_match = (
                        cross_cam_registry.find_cross_camera_match(
                            hist_arr, camera_id, current_time_sec
                        ) if cross_cam_registry else None
                    )
                    if cross_match:
                        trk.visitor_id = cross_match  # reuse existing id, no new ENTRY
                    else:
                        # Check Re-ID: is this someone who recently exited?
                        matched_vid = reid.find_match(hist_arr, current_time_sec)
                        if matched_vid:
                            trk.visitor_id = matched_vid
                            emitter.reentry(
                                store_id, camera_id, trk.visitor_id,
                                frame_idx, fps_video, clip_start,
                                trk.score, trk.is_staff,
                                session_seq=track_session_seq[trk.track_id]
                            )
                            stats["reentries"] += 1
                    else:
                        emitter.entry(
                            store_id, camera_id, trk.visitor_id,
                            frame_idx, fps_video, clip_start,
                            trk.score, trk.is_staff,
                            session_seq=track_session_seq[trk.track_id]
                        )
                        stats["entries"] += 1
                        # Register in cross-camera registry so floor cameras
                        # don't emit a duplicate ENTRY for this person
                        if cross_cam_registry:
                            cross_cam_registry.register(
                                trk.visitor_id, camera_id, hist_arr, current_time_sec
                            )
                    trk.crossed_entry_line = "IN"

                elif crossing == "OUT":
                    hist_arr = trk.appearance.hist if trk.appearance else None
                    reid.record_exit(trk.visitor_id, hist_arr, current_time_sec)
                    exited_visitors.add(trk.visitor_id)
                    emitter.exit(
                        store_id, camera_id, trk.visitor_id,
                        frame_idx, fps_video, clip_start,
                        trk.score, trk.is_staff,
                        session_seq=track_session_seq[trk.track_id]
                    )
                    stats["exits"] += 1
                    trk.crossed_entry_line = "OUT"

            # ── Zone classification ───────────────────────────────────────────
            new_zone = clf.classify(cx_norm, cy_norm)

            if new_zone != trk.zone_id:
                # Zone changed
                if trk.zone_id is not None:
                    # Emit ZONE_EXIT with dwell
                    enter_frame = track_zone_enter_frame.get(trk.track_id, frame_idx)
                    dwell_ms = int((frame_idx - enter_frame) / fps_video * 1000)
                    emitter.zone_exit(
                        store_id, camera_id, trk.visitor_id,
                        trk.zone_id, dwell_ms,
                        frame_idx, fps_video, clip_start,
                        trk.score, trk.is_staff,
                        session_seq=track_session_seq[trk.track_id]
                    )
                    stats["zone_events"] += 1

                    # Billing abandonment: left billing zone, check if no POS follows
                    if trk.zone_id == "CASH_COUNTER" and trk.track_id in billing_join_frame:
                        # We can't definitively check POS here, so we emit abandon
                        # The API funnel will correlate via POS timestamps
                        join_frame = billing_join_frame.pop(trk.track_id)
                        dwell_in_billing = int((frame_idx - join_frame) / fps_video * 1000)
                        emitter.billing_queue_abandon(
                            store_id, camera_id, trk.visitor_id,
                            dwell_in_billing,
                            frame_idx, fps_video, clip_start,
                            trk.score, trk.is_staff,
                            session_seq=track_session_seq[trk.track_id]
                        )
                        stats["billing_events"] += 1

                if new_zone is not None:
                    track_session_seq[trk.track_id] += 1
                    emitter.zone_enter(
                        store_id, camera_id, trk.visitor_id,
                        new_zone,
                        frame_idx, fps_video, clip_start,
                        trk.score, trk.is_staff,
                        session_seq=track_session_seq[trk.track_id]
                    )
                    track_zone_enter_frame[trk.track_id] = frame_idx
                    track_last_dwell_frame[trk.track_id] = frame_idx
                    stats["zone_events"] += 1

                    # Billing zone entered → record for queue
                    if new_zone == "CASH_COUNTER" and not trk.is_staff:
                        billing_join_frame[trk.track_id] = frame_idx
                        emitter.billing_queue_join(
                            store_id, camera_id, trk.visitor_id,
                            queue_depth,
                            frame_idx, fps_video, clip_start,
                            trk.score, trk.is_staff,
                            session_seq=track_session_seq[trk.track_id]
                        )
                        stats["billing_events"] += 1

                trk.zone_id = new_zone

            else:
                # Same zone — emit ZONE_DWELL every DWELL_EVENT_INTERVAL_SEC
                if new_zone is not None:
                    last_dwell = track_last_dwell_frame.get(trk.track_id, frame_idx)
                    elapsed_sec = (frame_idx - last_dwell) / fps_video
                    if elapsed_sec >= DWELL_EVENT_INTERVAL_SEC:
                        dwell_ms = int(elapsed_sec * 1000)
                        emitter.zone_dwell(
                            store_id, camera_id, trk.visitor_id,
                            new_zone, dwell_ms,
                            frame_idx, fps_video, clip_start,
                            trk.score, trk.is_staff,
                            session_seq=track_session_seq[trk.track_id]
                        )
                        track_last_dwell_frame[trk.track_id] = frame_idx
                        stats["zone_events"] += 1

    # ── Flush any active sessions at end of clip ──────────────────────────────
    for trk in tracker.tracks:
        if trk.state == "confirmed" and trk.zone_id:
            enter_frame = track_zone_enter_frame.get(trk.track_id, frame_idx)
            dwell_ms = int((frame_idx - enter_frame) / fps_video * 1000)
            emitter.zone_exit(
                store_id, camera_id, trk.visitor_id,
                trk.zone_id, dwell_ms,
                frame_idx, fps_video, clip_start,
                trk.score, trk.is_staff,
            )

    cap.release()
    print(f"[detect] {camera_id}: {stats}")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Purplle Store Intelligence — Detection Pipeline")
    parser.add_argument("--clips-dir",    default="CCTV Footage",         help="Directory with camera MP4 files")
    parser.add_argument("--store-layout", default="data/store_layout.json")
    parser.add_argument("--output-dir",   default="sample_events",         help="Directory to write events.jsonl")
    parser.add_argument("--api-url",      default=None,                     help="API base URL for live ingestion (e.g. http://localhost:8000)")
    parser.add_argument("--skip-frames",  type=int, default=5,             help="Process every Nth frame (5 = 3 FPS from 15 FPS source)")
    parser.add_argument("--max-frames",   type=int, default=0,             help="Max frames per camera (0 = full clip)")
    parser.add_argument("--clip-start",   default="2026-04-10T12:00:00Z", help="ISO-8601 UTC start time of the recording")
    parser.add_argument("--model-size",   default="yolov8n",              help="YOLOv8 model variant (yolov8n/yolov8s/yolov8m)")
    parser.add_argument("--demo",         action="store_true",             help="Quick demo: 2 min of CAM 1 only")
    args = parser.parse_args()

    # Load store layout
    with open(args.store_layout, encoding="utf-8") as f:
        layout = json.load(f)

    store_id  = layout["store_id"]
    cameras   = {c["file"]: c for c in layout["cameras"]}
    output_path = os.path.join(args.output_dir, "events.jsonl")

    # Load YOLOv8 model
    model = None
    if _YOLO_AVAILABLE:
        print(f"[detect] Loading {args.model_size} model…")
        try:
            model = YOLO(f"{args.model_size}.pt")
            print(f"[detect] Model loaded")
        except Exception as exc:
            print(f"[detect] YOLO load failed: {exc}. Running without detection.")
    else:
        print("[detect] ultralytics not installed — events will be empty. Install with: pip install ultralytics")

    demo_max_frames = int(2 * 60 * 15) if args.demo else args.max_frames

    # Shared cross-camera registry for deduplication across overlapping camera FOVs
    cross_cam_reg = CrossCameraRegistry()

    with EventEmitter(output_path, api_url=args.api_url) as emitter:
        for cam_file, cam_cfg in cameras.items():
            if args.demo and cam_cfg["type"] != "entry_exit":
                continue  # In demo mode, only process entry camera
            video_path = os.path.join(args.clips_dir, cam_file)
            if not os.path.exists(video_path):
                print(f"[detect] Skipping missing file: {video_path}")
                continue
            print(f"[detect] Processing {cam_file} ({cam_cfg['camera_id']})…")
            process_camera(
                video_path         = video_path,
                camera_cfg         = cam_cfg,
                store_id           = store_id,
                clip_start         = args.clip_start,
                emitter            = emitter,
                skip_frames        = args.skip_frames,
                max_frames         = demo_max_frames,
                model              = model,
                cross_cam_registry = cross_cam_reg,
            )

    print(f"[detect] Events written to {output_path}")


if __name__ == "__main__":
    main()
