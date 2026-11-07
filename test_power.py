#!/usr/bin/env python3
"""Run: python3 test_power.py"""
import contextlib
import io
import json
import tempfile
import time
from datetime import date
from pathlib import Path

import power as P

SG = {"psu_w": 1000, "overhead_w": 50, "tariff": 0.5, "currency": "S$"}
flat = P.wall_fn(SG, 0.8)  # 200 W DC -> 250 W wall
tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# RAPL: plain delta, counter wrap, one domain per package
assert P.rapl_watts(0, 100_000_000, 10, 1000) == 10
assert P.rapl_watts(900_000_000, 100_000_000, 10, 1_000_000_000) == 20
for name in ("intel-rapl:0", "intel-rapl:0:0", "intel-rapl:1"):
    write(root / "cap" / name / "energy_uj", "5\n")
dom = P.rapl_domains(root / "cap")
assert [d.name for d in dom] == ["intel-rapl:0", "intel-rapl:1"]
assert P.read_rapl_uj(dom) == [5, 5]
assert P.read_rapl_uj([]) is None and P.read_rapl_uj([root / "nope"]) is None

# Two packages sum; without RAPL the utilisation model takes over
prev, now = ([0, 0], (0, 0), 0.0), ([10_000_000, 20_000_000], (100, 0), 1.0)
assert P.cpu_reading(prev, now, [1, 1], (30, 120)) == (30, "rapl")
assert P.cpu_reading((None,) + prev[1:], now, [], (30, 120)) == (120, "model")

# CPU model: idle, full load, no clock movement
limits = (30, 142)
assert P.model_watts((0, 0), (100, 100), limits) == 30
assert P.model_watts((0, 0), (100, 0), limits) == 142
assert P.model_watts((5, 5), (5, 5), limits) == 30

# Cores: SMT siblings collapse, packages stay apart, missing ids fall back
info = "\n\n".join(f"processor : {i}\nphysical id : {p}\nsiblings : 2\ncore id : {c}"
                   for i, (p, c) in enumerate([(0, 0), (0, 0), (0, 1), (1, 0)]))
assert P.physical_cores(info) == 3
assert P.physical_cores("processor : 0") >= 1
assert P.cpu_limits(8) == (30, 120) and P.cpu_limits(2) == (16.25, 65)

# PSU curve: exact points, interpolation, past the rating, fixed override
assert abs(P.psu_efficiency(500, 1000) - 0.92) < 1e-9
assert abs(P.psu_efficiency(350, 1000) - 0.91) < 1e-9
assert P.psu_efficiency(1500, 1000) == 0.89
assert abs(P.psu_efficiency(0, 1000) - 0.70) < 1e-9
assert flat(200) == 250

# Overhead: disks by type; USB, removable and virtual devices skipped
block = root / "block"
for name, rot, rem in (("nvme0n1", "0", "0"), ("sda", "1", "0"), ("sdb", "0", "0"), ("sdz", "0", "1")):
    write(block / name / "queue/rotational", rot + "\n")
    write(block / name / "removable", rem + "\n")
    (block / name / "device").mkdir()
write(block / "loop0/removable", "0\n")
real = root / "x/usb1/sdu"
write(real / "removable", "0\n")
write(real / "queue/rotational", "1\n")
(real / "device").mkdir()
(block / "sdu").symlink_to(real)
assert P.detect_overhead_w(block, 100) == 15 + 10 + 20 + 4 + 6 + 2

# AMD GPU hwmon: connector entries never double count
drm = root / "drm"
write(drm / "card0/device/hwmon/hwmon3/power1_average", "45000000\n")
(drm / "card0-HDMI-A-1").symlink_to(drm / "card0")
real_which = P.shutil.which
P.shutil.which = lambda _: None
assert P.read_gpu_watts(drm) == 45
P.shutil.which = real_which

# Torn tail and garbage rows dropped
lines = ["ts,gpu_w,cpu_w,cpu_src", "1,2,3,rapl", "bad,2,3,rapl", "9,2"]
assert P.load_rows(lines) == [(1.0, 2.0, 3.0, "rapl")]

# One hour at 100 + 50 + 50 W DC at 0.8 = 250 W wall, 0.25 kWh
rows = [(float(t), 100.0, 50.0, "rapl") for t in range(0, 3601, 15)]
s = P.summarize(rows, 50, flat)
assert abs(s["wall_w"] - 250) < 1e-9 and abs(s["kwh"] - 0.25) < 1e-9

# Efficiency follows each sample, not the average load
mixed = [(float(t), 0.0 if t % 30 else 400.0, 0.0, "rapl") for t in range(0, 3601, 15)]
curve = P.wall_fn(SG)
assert P.summarize(mixed, 100, curve)["wall_w"] != curve(300)

# A gap (PC off) is not billed, but still counts toward the span
gap = rows + [(3600.0 + P.MAX_GAP_S + 1, 100.0, 50.0, "rapl")]
g = P.summarize(gap, 50, flat)
assert abs(g["kwh"] - 0.25) < 1e-9 and g["span_h"] > g["covered_h"]

# Too little data is None, not 0 kWh
assert P.summarize(rows[:1], 50, flat) is None

# Months are totalled separately, each with its own energy
oct_t = time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))
nov_t = time.mktime((2026, 11, 1, 12, 0, 0, 0, 0, -1))
two = [(oct_t + t, 100.0, 50.0, "rapl") for t in range(0, 3601, 15)]
two += [(nov_t + t, 100.0, 50.0, "rapl") for t in range(0, 1801, 15)]
m = P.totals(two, 50, flat, P.MONTH_FMT)
assert list(m) == ["2026-10", "2026-11"]
assert abs(m["2026-10"]["kwh"] - 0.25) < 1e-9 and abs(m["2026-11"]["kwh"] - 0.125) < 1e-9


# Week estimate: first logged day and today never count; a missing day is 0 kWh
def day(k):
    return {"kwh": k}


d0 = date(2026, 11, 10)
assert P.week_kwh_per_day({}, d0) is None
assert P.week_kwh_per_day({"2026-11-09": day(5), "2026-11-10": day(5)}, d0) is None
seen = {"2026-11-08": day(99), "2026-11-09": day(10), "2026-11-10": day(99)}
assert P.week_kwh_per_day(seen, d0) == (10, 1)
off = {"2026-11-07": day(99), "2026-11-08": day(10), "2026-11-10": day(99)}
assert P.week_kwh_per_day(off, d0) == (5, 2)  # 9 Nov had no samples
long_run = {f"2026-11-{n:02d}": day(n) for n in range(1, 21)}
assert P.week_kwh_per_day(long_run, date(2026, 11, 20)) == (16, 7)  # mean of 13..19


# Setup: rate defaults only in Singapore, bad input refused with a message
def refused(**kw):
    try:
        P.write_config(path=root / "bad.json", **kw)
    except SystemExit as e:
        return "--" in str(e)
    return False


cfg_path = root / "cfg.json"
c = P.write_config(1300, None, None, 61, cfg_path, singapore=True)
assert c == {"psu_w": 1300, "overhead_w": 61, "tariff": P.SG_TARIFF, "currency": "S$"}
assert json.loads(cfg_path.read_text()) == c == P.load_config(cfg_path)
assert P.write_config(850, 0.2, None, 50, cfg_path, singapore=False)["currency"] == "$"
assert refused(psu_w=850, tariff=None, currency=None, overhead_w=50, singapore=False)
assert refused(psu_w=0, tariff=0.2, currency=None, overhead_w=50, singapore=False)
assert refused(psu_w=850, tariff=-1, currency=None, overhead_w=50, singapore=False)
try:
    P.load_config(root / "missing.json")
    raise AssertionError("missing config accepted")
except SystemExit as e:
    assert "install.sh" in str(e)

# Report end to end, including a month that has closed
log = root / "log.csv"
log.write_text("ts,gpu_w,cpu_w,cpu_src\n"
               + "".join(f"{t:.0f},{g},{c},{src}\n" for t, g, c, src in two))
write(root / "s.json", json.dumps(SG))
out = io.StringIO()
with contextlib.redirect_stdout(out):
    P.report(P.parse_args(["report"]), log, root / "s.json")
text = out.getvalue()
assert "cpu via RAPL 100%" in text and "2026-10" in text and "(final)" in text
assert "S$" in text and "estimate" in text

# CLI validation
for bad in (["setup"], ["report", "--psu-eff", "1.5"]):
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            P.parse_args(bad)
            raise AssertionError(bad)
        except SystemExit:
            pass
print("ok")
