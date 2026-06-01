"""
Map bounding box center coordinates (normalized 0-1) in a camera frame
to store zone IDs, using the camera's zone_map from store_layout.json.
"""
from __future__ import annotations
from typing import Optional, List, Dict


class ZoneClassifier:
    def __init__(self, camera_config: Dict):
        """
        camera_config: single camera entry from store_layout.json
        """
        self.camera_id    = camera_config["camera_id"]
        self.zone_map     = camera_config.get("zone_map", [])
        self.camera_type  = camera_config.get("type", "floor")
        self.entry_line_y = camera_config.get("entry_line_y", 0.60)

    def classify(self, cx_norm: float, cy_norm: float) -> Optional[str]:
        """
        Given a normalized (0-1) center point, return the matching zone_id.
        Zones are checked in order; first match wins.
        """
        for zone_def in self.zone_map:
            if (zone_def["x_min"] <= cx_norm <= zone_def["x_max"] and
                    zone_def["y_min"] <= cy_norm <= zone_def["y_max"]):
                return zone_def["zone_id"]
        return None

    def is_entry_camera(self) -> bool:
        return self.camera_type == "entry_exit"

    def is_billing_camera(self) -> bool:
        return self.camera_type == "billing"

    def detect_crossing(
        self, prev_cy: float, curr_cy: float
    ) -> Optional[str]:
        """
        Detect if a tracked person crossed the entry line.
        Returns "IN" (entering), "OUT" (exiting), or None.

        Convention:
          - Camera is mounted above entry, looking inward.
          - Low cy  = near the door threshold (outside / on the threshold)
          - High cy = deeper inside the store
          - Crossing from low → high = ENTRY (going IN)
          - Crossing from high → low = EXIT  (going OUT)
        """
        line = self.entry_line_y
        if prev_cy < line and curr_cy >= line:
            return "IN"
        if prev_cy >= line and curr_cy < line:
            return "OUT"
        return None
