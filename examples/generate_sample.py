"""Generate a deterministic 3-day CGM sample (15-minute readings, Asia/Dubai).

Produces examples/glucose_sample.csv with columns: subject, measured_at, glucose_mgdl.
Deterministic (no randomness) so the example output is reproducible. Run:

    python examples/generate_sample.py
"""

from __future__ import annotations

import math
import pathlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DUBAI = ZoneInfo("Asia/Dubai")
SUBJECT = "patient-001"
START = datetime(2026, 6, 26, 0, 0, tzinfo=DUBAI)
DAYS = 3
STEP = timedelta(minutes=15)

# meal spikes: (hour, amplitude, spread_hours)
MEALS = [(8.0, 70.0, 1.2), (13.0, 85.0, 1.4), (19.5, 95.0, 1.6)]


def glucose_at(t: datetime) -> float:
    hod = t.hour + t.minute / 60.0
    base = 108.0
    # dawn phenomenon: gentle rise 03:00-08:00
    dawn = 22.0 * math.exp(-((hod - 6.5) ** 2) / 4.0)
    # post-meal excursions
    meals = sum(amp * math.exp(-((hod - h) ** 2) / (2 * s * s)) for h, amp, s in MEALS)
    # slow circadian wobble, varied per day so AGP bands have spread
    wobble = 8.0 * math.sin((hod / 24.0) * 2 * math.pi + t.day)
    return round(base + dawn + meals + wobble, 1)


def main() -> str:
    rows = ["subject,measured_at,glucose_mgdl"]
    t = START
    end = START + timedelta(days=DAYS)
    while t < end:
        rows.append(f"{SUBJECT},{t.isoformat()},{glucose_at(t)}")
        t += STEP
    out = pathlib.Path(__file__).resolve().parent / "glucose_sample.csv"
    out.write_text("\n".join(rows) + "\n")
    print(f"wrote {out} ({len(rows) - 1} readings)")
    return str(out)


if __name__ == "__main__":
    main()
