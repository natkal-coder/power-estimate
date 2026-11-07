#!/usr/bin/env python3
"""Estimate a desktop PC's wall power and electricity cost on Linux.

  power.py setup --psu-w 1300 [--tariff 0.31] [--currency S$]
  power.py log      sample forever, append raw watts to power_log.csv
  power.py report   energy, cost, month estimate (--tariff --psu-eff --overhead)

GPU (NVIDIA, AMD) and CPU (RAPL) are measured. Board, RAM, disks, fans and PSU
losses are estimated. Raw readings are logged and the estimates apply at report
time, so changing them reprices all history. Monitors are not included.
"""
import argparse
import calendar
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "power_log.csv"
CONFIG = HERE / "power_config.json"
POWERCAP = Path("/sys/class/powercap")
DRM = Path("/sys/class/drm")
BLOCK = Path("/sys/block")
LOCALTIME = Path("/etc/localtime")

INTERVAL_S = 15
MAX_GAP_S = INTERVAL_S * 3  # longer gap = PC off or logger down: not billed
DAY_FMT, MONTH_FMT = "%Y-%m-%d", "%Y-%m"
WEEK_DAYS = 7

SG_TARIFF = 0.3116  # S$/kWh, SP Group with GST, 1 Oct-31 Dec 2026
MAX_PSU_W = 5000

# Typical draw of parts nothing measures. Calibrate with a wall meter.
BOARD_W, FANS_W, RAM_W_PER_GB = 15, 10, 0.2
NVME_W, SSD_W, HDD_W = 4, 2, 6

# (load fraction, efficiency). 80 Plus Gold at 230 V; the tier cannot be read
# from the PC. Below 20% load is outside the spec, so those points are typical.
# ponytail: one generic curve, pass --psu-eff if you know your unit's real figure
PSU_CURVE = ((0.0, 0.70), (0.05, 0.80), (0.10, 0.86), (0.20, 0.90), (0.50, 0.92), (1.0, 0.89))


def psu_efficiency(dc_w, psu_w):
    load = dc_w / psu_w
    for (x0, y0), (x1, y1) in zip(PSU_CURVE, PSU_CURVE[1:]):
        if load <= x1:
            return y0 + (y1 - y0) * (load - x0) / (x1 - x0)
    return PSU_CURVE[-1][1]


def wall_fn(cfg, psu_eff=None):
    """Return a function converting DC watts to wall watts."""
    if psu_eff:
        return lambda dc_w: dc_w / psu_eff
    return lambda dc_w: dc_w / psu_efficiency(dc_w, cfg["psu_w"])


def detect_overhead_w(block=BLOCK, mem_gb=None):
    """Estimate unmeasured DC draw from RAM size and internal disks. USB is skipped."""
    if mem_gb is None:
        mem_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    total = BOARD_W + FANS_W + RAM_W_PER_GB * mem_gb
    for dev in block.iterdir():
        internal = (dev / "device").exists() and "/usb" not in str(dev.resolve())
        if not internal or (dev / "removable").read_text().strip() == "1":
            continue
        if dev.name.startswith("nvme"):
            total += NVME_W
        elif (dev / "queue/rotational").read_text().strip() == "1":
            total += HDD_W
        else:
            total += SSD_W
    return round(total)


def physical_cores(cpuinfo=None):
    text = Path("/proc/cpuinfo").read_text() if cpuinfo is None else cpuinfo
    cores = set()
    for block in text.split("\n\n"):
        pkg = re.search(r"^physical id\s*:\s*(\d+)", block, re.M)
        core = re.search(r"^core id\s*:\s*(\d+)", block, re.M)
        if pkg and core:
            cores.add((pkg.group(1), core.group(1)))
    return len(cores) or os.cpu_count() or 1  # ARM has no core id


def cpu_limits(cores):
    """(idle, max) watts for the utilisation model, used only without RAPL."""
    max_w = max(65, 15 * cores)
    return max_w / 4, max_w


def read_gpu_watts(drm=DRM):
    total = 0.0
    if shutil.which("nvidia-smi"):
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        # ponytail: cards with no power sensor report N/A and count as 0 W
        total += sum(float(x) for x in out.split("\n") if x.strip() and "N/A" not in x)
    for card in drm.glob("card*"):
        if re.fullmatch(r"card\d+", card.name):  # card0-HDMI-A-1 links to the same GPU
            sensors = sorted(card.glob("device/hwmon/hwmon*/power1_*"))
            total += sum(int(s.read_text()) / 1e6 for s in sensors[:1])
    return total


def rapl_domains(root=POWERCAP):
    """One domain per CPU package; sub-domains like intel-rapl:0:0 would double count."""
    return sorted(p for p in root.glob("intel-rapl:*") if re.fullmatch(r"intel-rapl:\d+", p.name))


def read_rapl_uj(domains):
    try:
        return [int((d / "energy_uj").read_text()) for d in domains] or None
    except OSError:  # root-only unless the udev rule is installed
        return None


def read_cpu_times():
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n")[0].split()[1:9]]
    return sum(fields), fields[3] + fields[4]  # total, idle + iowait


def rapl_watts(prev_uj, now_uj, dt, max_uj):
    delta = now_uj - prev_uj
    return (delta + max_uj if delta < 0 else delta) / 1e6 / dt


def model_watts(prev, now, limits):
    # ponytail: linear in utilisation, ignores clocks; install the udev rule for RAPL
    idle_w, max_w = limits
    total, idle = now[0] - prev[0], now[1] - prev[1]
    busy = 1 - idle / total if total > 0 else 0
    return idle_w + (max_w - idle_w) * busy


def cpu_reading(prev, now, max_uj, limits):
    """Prev and now are (rapl list or None, cpu times, monotonic). Returns (watts, source)."""
    if prev[0] and now[0]:
        dt = now[2] - prev[2]
        return sum(rapl_watts(a, b, dt, m) for a, b, m in zip(prev[0], now[0], max_uj)), "rapl"
    return model_watts(prev[1], now[1], limits), "model"


def log_forever():
    domains = rapl_domains()
    max_uj = [int((d / "max_energy_range_uj").read_text()) for d in domains]
    limits = cpu_limits(physical_cores())
    errors = rows = 0

    def snapshot():
        return read_rapl_uj(domains), read_cpu_times(), time.monotonic()

    prev = snapshot()
    is_new = not LOG.exists()
    with LOG.open("a", newline="", buffering=1) as f:
        if is_new:
            f.write("ts,gpu_w,cpu_w,cpu_src\n")
        while True:
            time.sleep(INTERVAL_S)
            now = snapshot()
            cpu_w, src = cpu_reading(prev, now, max_uj, limits)
            prev = now
            try:
                gpu_w = read_gpu_watts()
            except (subprocess.SubprocessError, ValueError, OSError) as e:
                errors += 1  # no row written: shows up as lost coverage in report
                print(f"[power] errors={errors} gpu read failed: {e}", flush=True)
                continue
            f.write(f"{time.time():.0f},{gpu_w:.1f},{cpu_w:.1f},{src}\n")
            rows += 1
            if rows % 40 == 0:  # every 10 min
                print(f"[power] errors={errors} rows={rows} gpu={gpu_w:.0f}W "
                      f"cpu={cpu_w:.0f}W ({src})", flush=True)


def load_rows(lines):
    """Parse CSV lines, dropping a torn tail or any other malformed row."""
    rows = []
    for r in csv.DictReader(lines):
        try:
            rows.append((float(r["ts"]), float(r["gpu_w"]), float(r["cpu_w"]), r["cpu_src"]))
        except (TypeError, ValueError):
            continue
    return rows


def summarize(rows, overhead_w, to_wall):
    """Each row bills the interval before it, unless that interval is a gap."""
    covered_s = gpu_ws = cpu_ws = wall_ws = 0.0
    for (t0, *_), (t1, gpu_w, cpu_w, _) in zip(rows, rows[1:]):
        dt = t1 - t0
        if 0 < dt <= MAX_GAP_S:
            covered_s += dt
            gpu_ws += gpu_w * dt
            cpu_ws += cpu_w * dt
            wall_ws += to_wall(gpu_w + cpu_w + overhead_w) * dt
    if not covered_s:
        return None
    return {
        "covered_h": covered_s / 3600,
        "span_h": (rows[-1][0] - rows[0][0]) / 3600,
        "gpu_w": gpu_ws / covered_s,
        "cpu_w": cpu_ws / covered_s,
        "wall_w": wall_ws / covered_s,
        "kwh": wall_ws / 3.6e6,
    }


def totals(rows, overhead_w, to_wall, fmt):
    """Summaries per local period (fmt is a strftime key), oldest first."""
    groups = {}
    for r in rows:
        groups.setdefault(time.strftime(fmt, time.localtime(r[0])), []).append(r)
    summaries = ((k, summarize(g, overhead_w, to_wall)) for k, g in groups.items())
    return {k: s for k, s in summaries if s is not None}


def week_kwh_per_day(days, today):
    """Mean kWh/day over the last WEEK_DAYS full days, or None if there are none.

    Today and the first logged day are partial, so neither counts. A day with
    no samples counts as 0 kWh: the PC was off, or the logger was down.
    """
    if not days:
        return None
    first = date.fromisoformat(min(days))
    window = [today - timedelta(n) for n in range(1, WEEK_DAYS + 1)]
    full = [d for d in window if d > first]
    if not full:
        return None
    kwh = sum(days[str(d)]["kwh"] for d in full if str(d) in days)
    return kwh / len(full), len(full)


def in_singapore(localtime=LOCALTIME):
    return str(localtime.resolve()).endswith("/Singapore")


def write_config(psu_w, tariff, currency, overhead_w, path=None, singapore=None):
    """Validate setup input and save it. Exits with a message on bad input."""
    singapore = in_singapore() if singapore is None else singapore
    if not 0 < psu_w <= MAX_PSU_W:
        sys.exit(f"--psu-w must be between 1 and {MAX_PSU_W}")
    if tariff is None and not singapore:
        sys.exit("no default rate outside Singapore: pass --tariff, your price per kWh")
    tariff = SG_TARIFF if tariff is None else tariff
    if tariff <= 0:
        sys.exit("--tariff must be above 0")
    cfg = {"psu_w": psu_w, "overhead_w": overhead_w, "tariff": tariff,
           "currency": currency or ("S$" if singapore else "$")}
    (path or CONFIG).write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def load_config(path=None):
    try:
        return json.loads((path or CONFIG).read_text())
    except (OSError, ValueError):
        sys.exit("no valid power_config.json: run install.sh --psu-w <your PSU watts>")


def report(args, log=None, config=None):
    cfg = load_config(config)
    path = log or LOG
    if not path.exists():
        sys.exit(f"no log at {path}: is the logger running?")
    with path.open(newline="") as f:
        rows = load_rows(f)
    overhead = cfg["overhead_w"] if args.overhead is None else args.overhead
    tariff = cfg["tariff"] if args.tariff is None else args.tariff
    cur = cfg["currency"]
    to_wall = wall_fn(cfg, args.psu_eff)
    s = summarize(rows, overhead, to_wall)
    if s is None:
        sys.exit("not enough samples yet")
    dc_w = s["gpu_w"] + s["cpu_w"] + overhead
    rapl_pct = 100 * sum(r[3] == "rapl" for r in rows) / len(rows)
    print(f"logged   {s['covered_h']:.2f} h of {s['span_h']:.2f} h span, "
          f"cpu via RAPL {rapl_pct:.0f}% of samples")
    print(f"average  {s['wall_w']:.0f} W at wall = (gpu {s['gpu_w']:.0f} + cpu {s['cpu_w']:.0f} "
          f"+ other {overhead:.0f}) / {dc_w / s['wall_w']:.2f} PSU")
    print(f"so far   {s['kwh']:.2f} kWh, {cur}{s['kwh'] * tariff:.2f} at {cur}{tariff}/kWh")
    days = totals(rows, overhead, to_wall, DAY_FMT)
    for day, d in list(days.items())[-(WEEK_DAYS + 1):]:
        print(f"{day}  {d['kwh']:.1f} kWh, {cur}{d['kwh'] * tariff:.2f}, "
              f"{d['covered_h']:.1f} h logged")
    today = date.today()
    week = week_kwh_per_day(days, today)
    if week is None:
        print("estimate needs one full day of logging")
    else:
        kwh_day, n = week
        month_days = calendar.monthrange(today.year, today.month)[1]
        print(f"estimate {kwh_day:.1f} kWh/day over last {n} of {WEEK_DAYS} full days = "
              f"{cur}{kwh_day * month_days * tariff:.2f} for a full "
              f"{today:%B} ({month_days} days)")
    this_month = time.strftime(MONTH_FMT)
    for month, m in totals(rows, overhead, to_wall, MONTH_FMT).items():
        state = "to date" if month == this_month else "final"
        print(f"{month}  {m['kwh']:.1f} kWh, {cur}{m['kwh'] * tariff:.2f}, "
              f"{m['covered_h']:.0f} h logged ({state})")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["setup", "log", "report"])
    p.add_argument("--psu-w", type=float, help="PSU rating in watts (setup)")
    p.add_argument("--currency", help="symbol for reports (setup)")
    p.add_argument("--tariff", type=float, help="price per kWh")
    p.add_argument("--psu-eff", type=float, help="fixed efficiency, replaces the curve")
    p.add_argument("--overhead", type=float, help="DC watts for board, RAM, disks, fans")
    args = p.parse_args(argv)
    if args.psu_eff is not None and not 0 < args.psu_eff <= 1:
        p.error("--psu-eff must be in (0, 1]")
    if args.cmd == "setup" and args.psu_w is None:
        p.error("setup needs --psu-w, the watt rating printed on your PSU")
    return args


def main():
    args = parse_args()
    if args.cmd == "setup":
        overhead = detect_overhead_w() if args.overhead is None else args.overhead
        print(json.dumps(write_config(args.psu_w, args.tariff, args.currency, overhead)))
    elif args.cmd == "log":
        log_forever()
    else:
        report(args)


if __name__ == "__main__":
    main()
