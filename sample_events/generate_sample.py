"""
Generate realistic sample events from the POS transaction data.
This seeds the API so /metrics and /funnel work immediately
without needing to run the full CV pipeline.

Strategy:
  - For each POS transaction, create a visitor session that flows through:
    ENTRY → ZONE_ENTER (browse zone) → ZONE_DWELL → ZONE_ENTER (CASH_COUNTER)
    → BILLING_QUEUE_JOIN → EXIT
  - Add non-converting visitors (walk-around only, no billing)
  - Add staff entries
  - Add one re-entry case
  - Add a billing queue abandon case
"""
from __future__ import annotations
import csv
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

random.seed(42)

STORE_ID = "STORE_BLR_002"
CAMERAS  = {
    "entry":   "CAM_ENTRY_01",
    "floor":   "CAM_FLOOR_01",
    "floor2":  "CAM_FLOOR_02",
    "billing": "CAM_BILLING_01",
    "floor3":  "CAM_FLOOR_03",
}
BROWSE_ZONES = [
    "DERMDOC", "GV", "MINIMALIST", "AQUALOGICA", "FOXTALE",
    "FACES", "LAKME", "MAYBELLINE", "ALPS", "LOREAL",
    "MAKEUP_UNIT", "FOH", "FRAGRANCE",
]

BASE_DIR   = os.path.dirname(os.path.dirname(__file__))
POS_CSV    = os.path.join(BASE_DIR, "data", "pos_transactions.csv")
OUTPUT     = os.path.join(BASE_DIR, "sample_events", "events.jsonl")


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def event(event_type, visitor_id, camera_id, zone_id, timestamp_dt,
          is_staff=False, dwell_ms=0, queue_depth=None, sku_zone=None,
          session_seq=0, confidence=0.88):
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE_ID,
        "camera_id":  camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  ts(timestamp_dt),
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone":    sku_zone,
            "session_seq": session_seq,
        },
    }


def make_converting_session(pos_ts: datetime) -> list:
    """Create a full converting visitor session ending just before a POS transaction."""
    events = []
    vid    = f"VIS_{uuid.uuid4().hex[:6]}"
    entry_dt = pos_ts - timedelta(minutes=random.randint(8, 25))
    seq = 0

    # ENTRY
    events.append(event("ENTRY", vid, CAMERAS["entry"], None, entry_dt,
                        session_seq=seq, confidence=random.uniform(0.82, 0.97)))
    seq += 1
    t = entry_dt + timedelta(seconds=random.randint(10, 30))

    # Browse 1–3 zones
    for i in range(random.randint(1, 3)):
        zone = random.choice(BROWSE_ZONES)
        cam  = random.choice([CAMERAS["floor"], CAMERAS["floor2"], CAMERAS["floor3"]])
        events.append(event("ZONE_ENTER", vid, cam, zone, t,
                            session_seq=seq, confidence=random.uniform(0.75, 0.95)))
        seq += 1
        dwell = random.randint(20_000, 120_000)  # 20s – 2min
        t += timedelta(milliseconds=dwell)
        if dwell > 30_000:
            events.append(event("ZONE_DWELL", vid, cam, zone, t,
                                dwell_ms=dwell,
                                session_seq=seq, confidence=random.uniform(0.75, 0.95)))
            seq += 1
        events.append(event("ZONE_EXIT", vid, cam, zone, t, dwell_ms=dwell,
                            session_seq=seq, confidence=random.uniform(0.75, 0.95)))
        seq += 1
        t += timedelta(seconds=random.randint(5, 15))

    # Head to billing
    t = pos_ts - timedelta(minutes=random.randint(2, 4))
    queue_d = random.randint(0, 3)
    events.append(event("ZONE_ENTER",          vid, CAMERAS["billing"], "CASH_COUNTER", t, session_seq=seq))
    seq += 1
    events.append(event("BILLING_QUEUE_JOIN",  vid, CAMERAS["billing"], "CASH_COUNTER", t,
                        queue_depth=queue_d, session_seq=seq))
    seq += 1
    t = pos_ts + timedelta(seconds=random.randint(30, 90))
    events.append(event("EXIT", vid, CAMERAS["entry"], None, t, session_seq=seq))

    return events


def make_non_converting_session(base_dt: datetime) -> list:
    """Visitor who browses but does not purchase."""
    events = []
    vid    = f"VIS_{uuid.uuid4().hex[:6]}"
    entry_dt = base_dt + timedelta(minutes=random.randint(0, 30))
    seq = 0

    events.append(event("ENTRY", vid, CAMERAS["entry"], None, entry_dt,
                        session_seq=seq, confidence=random.uniform(0.80, 0.95)))
    seq += 1
    t = entry_dt + timedelta(seconds=20)

    for _ in range(random.randint(1, 4)):
        zone = random.choice(BROWSE_ZONES)
        cam  = random.choice([CAMERAS["floor"], CAMERAS["floor2"]])
        events.append(event("ZONE_ENTER", vid, cam, zone, t,
                            session_seq=seq, confidence=random.uniform(0.72, 0.92)))
        seq += 1
        dwell = random.randint(15_000, 90_000)
        t += timedelta(milliseconds=dwell)
        if dwell > 30_000:
            events.append(event("ZONE_DWELL", vid, cam, zone, t, dwell_ms=dwell,
                                session_seq=seq))
            seq += 1
        events.append(event("ZONE_EXIT", vid, cam, zone, t, dwell_ms=dwell,
                            session_seq=seq))
        seq += 1
        t += timedelta(seconds=10)

    events.append(event("EXIT", vid, CAMERAS["entry"], None, t, session_seq=seq))
    return events


def make_staff_session(base_dt: datetime) -> list:
    """Staff entry moving through multiple zones repeatedly."""
    events = []
    vid = f"VIS_STAFF_{uuid.uuid4().hex[:4]}"
    t   = base_dt
    seq = 0

    events.append(event("ENTRY", vid, CAMERAS["entry"], None, t,
                        is_staff=True, session_seq=seq, confidence=0.91))
    seq += 1

    zones_to_visit = random.sample(BROWSE_ZONES, 5)
    for zone in zones_to_visit:
        t += timedelta(seconds=random.randint(30, 120))
        events.append(event("ZONE_ENTER", vid, CAMERAS["floor"], zone, t,
                            is_staff=True, session_seq=seq))
        seq += 1
        t += timedelta(seconds=random.randint(20, 60))
        events.append(event("ZONE_EXIT",  vid, CAMERAS["floor"], zone, t,
                            dwell_ms=random.randint(20_000, 60_000),
                            is_staff=True, session_seq=seq))
        seq += 1

    return events


def make_reentry_session(base_dt: datetime) -> list:
    """Visitor exits and comes back — re-entry detection test case."""
    events = []
    vid = f"VIS_{uuid.uuid4().hex[:6]}"
    t   = base_dt
    seq = 0

    # First visit
    events.append(event("ENTRY", vid, CAMERAS["entry"], None, t, session_seq=seq))
    seq += 1
    t += timedelta(minutes=5)
    events.append(event("ZONE_ENTER", vid, CAMERAS["floor"], "FOH", t, session_seq=seq))
    seq += 1
    t += timedelta(minutes=3)
    events.append(event("EXIT", vid, CAMERAS["entry"], None, t, session_seq=seq))
    seq += 1

    # Re-entry
    t += timedelta(minutes=2)
    events.append(event("REENTRY", vid, CAMERAS["entry"], None, t, session_seq=seq,
                        confidence=0.78))
    seq += 1
    t += timedelta(minutes=4)
    events.append(event("ZONE_ENTER", vid, CAMERAS["billing"], "CASH_COUNTER", t, session_seq=seq))
    seq += 1
    events.append(event("BILLING_QUEUE_JOIN", vid, CAMERAS["billing"], "CASH_COUNTER", t,
                        queue_depth=2, session_seq=seq))
    seq += 1
    t += timedelta(minutes=3)
    events.append(event("EXIT", vid, CAMERAS["entry"], None, t, session_seq=seq))

    return events


def make_abandon_session(base_dt: datetime) -> list:
    """Visitor joins billing queue but leaves without purchasing."""
    events = []
    vid = f"VIS_{uuid.uuid4().hex[:6]}"
    t   = base_dt
    seq = 0

    events.append(event("ENTRY", vid, CAMERAS["entry"], None, t, session_seq=seq))
    seq += 1
    t += timedelta(minutes=random.randint(3, 8))
    events.append(event("ZONE_ENTER",         vid, CAMERAS["billing"], "CASH_COUNTER", t, session_seq=seq))
    seq += 1
    events.append(event("BILLING_QUEUE_JOIN", vid, CAMERAS["billing"], "CASH_COUNTER", t,
                        queue_depth=6, session_seq=seq))
    seq += 1
    t += timedelta(minutes=random.randint(3, 7))
    events.append(event("BILLING_QUEUE_ABANDON", vid, CAMERAS["billing"], "CASH_COUNTER", t,
                        dwell_ms=random.randint(120_000, 420_000), session_seq=seq))
    seq += 1
    t += timedelta(minutes=1)
    events.append(event("EXIT", vid, CAMERAS["entry"], None, t, session_seq=seq))

    return events


def generate():
    # Read POS transactions and shift them to TODAY for demo purposes
    transactions_raw = []
    with open(POS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            transactions_raw.append(dt)

    # Shift all timestamps to today so the API's "today" filter returns real data
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if transactions_raw:
        orig_date = transactions_raw[0].replace(hour=0, minute=0, second=0, microsecond=0)
        date_shift = today - orig_date
    else:
        date_shift = timedelta(0)
    transactions = sorted([t + date_shift for t in transactions_raw])

    # Also update POS CSV to today's timestamps for metric correlation
    if date_shift.days != 0:
        rows_updated = []
        with open(POS_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                orig_dt = datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                new_dt = orig_dt + date_shift
                row["timestamp"] = new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                rows_updated.append(row)
        with open(POS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows_updated)
        print(f"  POS CSV timestamps shifted by {date_shift.days} days")

    all_events = []

    # 1. One converting session per POS transaction
    for pos_ts in transactions:
        all_events.extend(make_converting_session(pos_ts))

    # 2. Non-converting visitors (1.5x of converting, typical retail ~30% CR)
    n_non_converting = int(len(transactions) * 2.0)
    first_txn = transactions[0] if transactions else datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    for i in range(n_non_converting):
        offset = timedelta(minutes=random.randint(0, 9 * 60))
        all_events.extend(make_non_converting_session(first_txn + offset))

    # 3. Staff sessions (4 staff members, 2 rotations each)
    for i in range(4):
        staff_dt = first_txn + timedelta(hours=random.uniform(0, 3))
        all_events.extend(make_staff_session(staff_dt))
        all_events.extend(make_staff_session(staff_dt + timedelta(hours=4)))

    # 4. One re-entry case
    all_events.extend(make_reentry_session(first_txn + timedelta(hours=1)))

    # 5. Two queue-abandon cases
    all_events.extend(make_abandon_session(first_txn + timedelta(hours=2)))
    all_events.extend(make_abandon_session(first_txn + timedelta(hours=5)))

    # Sort by timestamp
    all_events.sort(key=lambda e: e["timestamp"])

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        for ev in all_events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    print(f"Generated {len(all_events)} sample events -> {OUTPUT}")
    n_entries = sum(1 for e in all_events if e["event_type"] == "ENTRY")
    n_convert = sum(1 for e in all_events if e["event_type"] == "BILLING_QUEUE_JOIN")
    print(f"  ENTRY: {n_entries}, BILLING_QUEUE_JOIN: {n_convert}")


if __name__ == "__main__":
    generate()
