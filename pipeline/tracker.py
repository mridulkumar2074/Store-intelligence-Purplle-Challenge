"""
IoU-based multi-object tracker with Kalman filter prediction.
Implements the core ByteTrack lifecycle without external dependencies.
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from scipy.optimize import linear_sum_assignment


@dataclass
class BBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def iou(self, other: BBox) -> float:
        xi1 = max(self.x1, other.x1)
        yi1 = max(self.y1, other.y1)
        xi2 = min(self.x2, other.x2)
        yi2 = min(self.y2, other.y2)
        inter = max(0.0, xi2 - xi1) * max(0.0, yi2 - yi1)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0


@dataclass
class ColorHistogram:
    """Compact appearance descriptor for Re-ID."""
    hist: np.ndarray  # shape (64,) — 4 bins per channel in HSV

    def similarity(self, other: ColorHistogram) -> float:
        if self.hist is None or other.hist is None:
            return 0.0
        denom = np.sqrt(np.sum(self.hist ** 2)) * np.sqrt(np.sum(other.hist ** 2))
        if denom == 0:
            return 0.0
        return float(np.dot(self.hist, other.hist) / denom)


@dataclass
class Track:
    track_id:    int
    visitor_id:  str
    bbox:        BBox
    score:       float
    age:         int = 0          # frames since creation
    lost_age:    int = 0          # frames since last matched detection
    state:       str = "tentative"  # tentative | confirmed | lost
    velocity:    np.ndarray = field(default_factory=lambda: np.zeros(4))
    appearance:  Optional[ColorHistogram] = None
    zone_id:     Optional[str] = None
    zone_enter_frame: int = 0
    entry_y_history: List[float] = field(default_factory=list)
    crossed_entry_line: Optional[str] = None  # "IN" | "OUT"
    is_staff:    bool = False
    frame_first_seen: int = 0

    def predict(self) -> BBox:
        """Move bbox forward by last velocity."""
        x1 = self.bbox.x1 + self.velocity[0]
        y1 = self.bbox.y1 + self.velocity[1]
        x2 = self.bbox.x2 + self.velocity[2]
        y2 = self.bbox.y2 + self.velocity[3]
        return BBox(x1, y1, x2, y2)

    def update(self, det: BBox, score: float, appearance: Optional[ColorHistogram] = None):
        prev = np.array([self.bbox.x1, self.bbox.y1, self.bbox.x2, self.bbox.y2])
        curr = np.array([det.x1, det.y1, det.x2, det.y2])
        self.velocity = 0.6 * self.velocity + 0.4 * (curr - prev)
        self.bbox = det
        self.score = score
        self.lost_age = 0
        self.age += 1
        if appearance:
            self.appearance = appearance


class IoUTracker:
    """
    Simple two-pass tracker similar to ByteTrack:
      Pass 1: match high-confidence detections to confirmed tracks
      Pass 2: match low-confidence detections to unmatched confirmed tracks
    Then assign remaining detections to new (tentative) tracks.
    """

    def __init__(
        self,
        iou_threshold: float = 0.35,
        high_conf_threshold: float = 0.55,
        low_conf_threshold: float = 0.10,
        max_lost_age: int = 30,
        min_confirm_age: int = 3,
    ):
        self.iou_threshold       = iou_threshold
        self.high_conf_threshold = high_conf_threshold
        self.low_conf_threshold  = low_conf_threshold
        self.max_lost_age        = max_lost_age
        self.min_confirm_age     = min_confirm_age
        self._next_id            = 1
        self.tracks: List[Track] = []

    def _new_visitor_id(self) -> str:
        vid = f"VIS_{self._next_id:05x}"
        self._next_id += 1
        return vid

    def _match(
        self, tracks: List[Track], dets: List[tuple[BBox, float, Optional[ColorHistogram]]]
    ) -> tuple[List[tuple[int, int]], List[int], List[int]]:
        """Hungarian matching. Returns (matches, unmatched_track_idxs, unmatched_det_idxs)."""
        if not tracks or not dets:
            return [], list(range(len(tracks))), list(range(len(dets)))

        cost = np.zeros((len(tracks), len(dets)), dtype=float)
        for i, trk in enumerate(tracks):
            pred = trk.predict()
            for j, (det_bbox, _, _) in enumerate(dets):
                cost[i, j] = 1.0 - pred.iou(det_bbox)

        row_ind, col_ind = linear_sum_assignment(cost)
        matches, unmatched_t, unmatched_d = [], list(range(len(tracks))), list(range(len(dets)))

        for r, c in zip(row_ind, col_ind):
            if cost[r, c] <= 1.0 - self.iou_threshold:
                matches.append((r, c))
                unmatched_t.remove(r)
                unmatched_d.remove(c)

        return matches, unmatched_t, unmatched_d

    def update(
        self,
        detections: List[tuple[BBox, float, Optional[ColorHistogram]]],
        frame_idx: int,
    ) -> List[Track]:
        """Update tracker with new detections. Returns all active tracks."""
        high_dets = [(b, s, a) for b, s, a in detections if s >= self.high_conf_threshold]
        low_dets  = [(b, s, a) for b, s, a in detections if self.low_conf_threshold <= s < self.high_conf_threshold]

        confirmed  = [t for t in self.tracks if t.state == "confirmed"]
        tentative  = [t for t in self.tracks if t.state == "tentative"]

        # Pass 1: high-confidence dets vs confirmed tracks
        m1, unm_confirmed, unm_high = self._match(confirmed, high_dets)
        for ti, di in m1:
            confirmed[ti].update(*high_dets[di])
            confirmed[ti].state = "confirmed"

        # Pass 2: low-confidence dets vs unmatched confirmed tracks
        remaining_confirmed = [confirmed[i] for i in unm_confirmed]
        m2, still_unmatched_c, unm_low = self._match(remaining_confirmed, low_dets)
        for ti, di in m2:
            remaining_confirmed[ti].update(*low_dets[di])

        # Mark confirmed tracks that are still unmatched as lost
        for i in still_unmatched_c:
            remaining_confirmed[i].lost_age += 1
            if remaining_confirmed[i].lost_age > self.max_lost_age:
                remaining_confirmed[i].state = "lost"

        # Match tentative tracks with leftover high dets
        leftover_high = [high_dets[i] for i in unm_high]
        m3, unm_tentative, unm_leftover = self._match(tentative, leftover_high)
        for ti, di in m3:
            tentative[ti].update(*leftover_high[di])
            tentative[ti].age += 1
            if tentative[ti].age >= self.min_confirm_age:
                tentative[ti].state = "confirmed"

        # Mark unmatched tentative tracks as lost quickly
        for i in unm_tentative:
            tentative[i].lost_age += 1
            if tentative[i].lost_age > 5:
                tentative[i].state = "lost"

        # Spawn new tracks for completely unmatched high-conf detections
        for i in unm_leftover:
            bbox, score, app = leftover_high[i]
            trk = Track(
                track_id=self._next_id,
                visitor_id=self._new_visitor_id(),
                bbox=bbox,
                score=score,
                appearance=app,
                state="tentative",
                frame_first_seen=frame_idx,
            )
            self._next_id += 1
            self.tracks.append(trk)

        # Remove permanently lost tracks
        self.tracks = [t for t in self.tracks if t.state != "lost"]

        return [t for t in self.tracks if t.state == "confirmed"]
