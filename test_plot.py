#!/usr/bin/env python3
"""Run: python3 test_plot.py"""
import json
import re
import tempfile
import time
from pathlib import Path

import plot as C
import power as P

# Axis ticks: round steps from 0 that reach past the maximum
assert C.ticks(5.3) == [0, 2, 4, 6]
assert C.ticks(612) == [0, 200, 400, 600, 800]
assert C.ticks(0)[0] == 0  # no data must not crash the axis

# Buckets: means per 30 minutes, "other" is wall minus GPU and CPU
flat = P.wall_fn({"psu_w": 1000}, 0.8)
rows = [(t, 100.0, 50.0, "rapl") for t in range(0, 3600, 15)]
b = C.buckets(rows, 50, flat, 0)
assert [p["t"] for p in b] == [0, 1800]
assert b[0]["gpu"] == 100 and b[0]["cpu"] == 50 and abs(b[0]["other"] - 100) < 1e-9
assert C.buckets(rows, 50, flat, 1800)[0]["t"] == 1800  # since filters older rows

# Runs break at a missing bucket so the area never bridges an outage
pts = [{"t": t} for t in (0, 1800, 3600, 9000, 10800)]
assert [len(r) for r in C.runs(pts)] == [3, 2]

# Direct labels never closer than the minimum spacing, order kept
assert C.spread([100, 105, 106]) == [100, 114, 128]
assert C.spread([10, 50]) == [10, 50]

# Data embedded in <script> cannot close it; user text is escaped in markup
assert "</script>" not in C.safe_json({"x": "</script><b>"})
assert json.loads(C.safe_json({"x": "</script>"}))["x"] == "</script>"
assert C.esc('<b a="1">') == "&lt;b a=&quot;1&quot;&gt;"

# Bars: today and the first logged day fade, even once the window has scrolled
days = {f"2026-11-{n:02d}": {"kwh": float(n), "covered_h": 24.0} for n in range(1, 41 - 10)}
svg = C.cost_chart(days, 0.5, "S$", 10.0, "2026-11-30")
assert svg.count('class="bar partial"') == 2  # all 30 days shown: first logged day and today
assert "7-day average S$5.00/day" in svg
# 56 days logged: the first logged day has scrolled out, only today fades
many = {f"2026-{m:02d}-{d:02d}": {"kwh": 1.0, "covered_h": 24.0} for m in (10, 11) for d in range(1, 29)}
assert C.cost_chart(many, 0.5, "S$", None, "2026-11-28").count('class="bar partial"') == 1
assert "7-day average" not in C.cost_chart(many, 0.5, "S$", None, "2026-11-28")

# End to end: plot writes a page with tiles, both charts, tooltip data and a table
tmpdir = tempfile.TemporaryDirectory()
tmp = Path(tmpdir.name)
now = time.time()
log_rows = [(now - 3 * 86400 + t, 300.0, 90.0, "rapl") for t in range(0, 3 * 86400, 15)]
(tmp / "log.csv").write_text("ts,gpu_w,cpu_w,cpu_src\n" + "".join(f"{t:.0f},{g},{c},{s}\n" for t, g, c, s in log_rows))
(tmp / "cfg.json").write_text(json.dumps({"psu_w": 1300, "overhead_w": 61, "tariff": 0.3, "currency": "<€>"}))
out = P.plot(P.parse_args(["plot", "--no-open"]), tmp / "log.csv", tmp / "cfg.json", tmp / "r.html")
page = out.read_text()
assert page.count("<svg") == 2 and "Table of every day" in page and 'id="data"' in page
assert "<€>" not in page.split('<script type="application/json"')[0]  # escaped in markup
data = json.loads(re.search(r'id="data">(.*?)</script>', page, re.S).group(1))
assert data["days"] and data["power"] and data["days"][0][1].startswith("<€>")
assert "estimate for the full month" in page
print("ok")
