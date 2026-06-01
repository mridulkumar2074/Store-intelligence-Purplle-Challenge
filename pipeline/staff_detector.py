"""
Classify whether a detected person is store staff based on uniform color.
Purplle store staff wear dark purple/magenta branded uniforms.

Approach:
  1. Extract the upper-body region (top 50% of bounding box).
  2. Convert to HSV.
  3. Check what fraction of pixels fall in the staff-uniform color range.
  4. If above threshold → is_staff = True.

The heuristic errs on the side of caution (prefer false negatives over
false positives) to avoid excluding real customers from metrics.
"""
from __future__ import annotations
import numpy as np

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

# HSV range for Purplle staff uniform (dark purple / magenta)
# Hue: 270-330 deg → in OpenCV (0-180): 135-165
# Saturation: moderate-to-high
# Value: medium-to-dark
_STAFF_HUE_LOW  = np.array([125, 60, 40],  dtype=np.uint8)
_STAFF_HUE_HIGH = np.array([165, 255, 200], dtype=np.uint8)

# Fallback secondary range (dark maroon / burgundy variants)
_STAFF_HUE_LOW2  = np.array([0,   80, 30],  dtype=np.uint8)
_STAFF_HUE_HIGH2 = np.array([15, 200, 160], dtype=np.uint8)

STAFF_COLOR_THRESHOLD = 0.20  # 20% of upper body pixels match → staff


def is_staff_by_color(frame: "np.ndarray", bbox) -> bool:
    """
    bbox: object with x1, y1, x2, y2 in pixel coordinates.
    Returns True if the person is likely staff based on uniform color.
    """
    if not _CV2_AVAILABLE:
        return False

    x1, y1, x2, y2 = int(bbox.x1), int(bbox.y1), int(bbox.x2), int(bbox.y2)
    h = y2 - y1
    if h < 30:
        return False

    # Upper half of the bounding box (shirt/top)
    upper_y2 = y1 + int(h * 0.50)
    roi = frame[y1:upper_y2, x1:x2]
    if roi.size == 0:
        return False

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask1 = cv2.inRange(hsv, _STAFF_HUE_LOW,  _STAFF_HUE_HIGH)
    mask2 = cv2.inRange(hsv, _STAFF_HUE_LOW2, _STAFF_HUE_HIGH2)
    combined = cv2.bitwise_or(mask1, mask2)

    total_px = roi.shape[0] * roi.shape[1]
    matched  = np.count_nonzero(combined)
    ratio    = matched / max(total_px, 1)

    return ratio >= STAFF_COLOR_THRESHOLD


def extract_appearance(frame: "np.ndarray", bbox) -> "np.ndarray | None":
    """
    Build a compact color histogram (64-dim) for Re-ID.
    Uses upper + lower body in HSV, 4 bins per channel.
    """
    if not _CV2_AVAILABLE:
        return None

    x1, y1, x2, y2 = int(bbox.x1), int(bbox.y1), int(bbox.x2), int(bbox.y2)
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return None

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [4, 4, 4],
                        [0, 180, 0, 256, 0, 256])
    hist = hist.flatten().astype(np.float32)
    norm = np.linalg.norm(hist)
    if norm > 0:
        hist /= norm
    return hist
