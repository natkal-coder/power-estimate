#!/usr/bin/env python3
"""Run: python3 test_applet.py"""
import time

from power import wall_fn
from power_applet import kwh_before_today, status

cfg = {"psu_w": 1000, "overhead_w": 50, "tariff": 0.3, "currency": "S$"}
now = time.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1))
rows = [(now - 3600 + t, 100.0, 50.0, "rapl") for t in range(0, 3601, 15)]
wall = wall_fn(cfg)(200)
kwh = wall / 1000

big, small, ok = status(rows, now, cfg)
assert ok and big == f"{wall:.0f} W" and f"today S${kwh * 0.3:.2f}" in small, (big, small)
assert status(rows, now + 60, cfg)[2] is False  # stale log
assert status([], now, cfg)[2] is False  # empty log
assert status(rows[-1:], now, cfg)[1] == "collecting..."  # one sample: no energy yet

# Month-to-date = kWh before today + today, and last month is excluded
day = 86400
old = [(now - 8 * day + t, 100.0, 50.0, "rapl") for t in range(0, 3601, 15)]  # 28 Oct
this = [(now - 2 * day + t, 100.0, 50.0, "rapl") for t in range(0, 3601, 15)]  # 3 Nov
assert abs(kwh_before_today(old + this + rows, now, cfg) - kwh) < 1e-9
assert kwh_before_today(rows, now, cfg) == 0.0
assert f"month S${kwh * 2 * 0.3:.2f}" in status(rows, now, cfg, kwh)[1]
print("ok")
