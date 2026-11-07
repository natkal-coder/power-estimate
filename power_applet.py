#!/usr/bin/env python3
"""Top-bar readout of power_log.csv: live watts, today's and month-to-date cost.
Needs the system Python (gi) and GNOME's appindicator extension."""
import time

from power import LOG, MAX_GAP_S, load_config, load_rows, summarize, wall_fn

REFRESH_S = 15
TODAY_BYTES = 512 * 1024  # more than a full day of rows
MONTH_BYTES = 8 * 1024 * 1024  # more than 31 days of rows
HEADER = "ts,gpu_w,cpu_w,cpu_src"
ICON_OK = "utilities-system-monitor-symbolic"
ICON_BAD = "dialog-warning-symbolic"


def tail_rows(path, n_bytes):
    with path.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - n_bytes))
        lines = f.read().decode(errors="ignore").split("\n")[1:]  # first may be partial
    return load_rows([HEADER] + lines)


def day_start(now):
    return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))


def month_start(now):
    return time.mktime(time.localtime(now)[:2] + (1, 0, 0, 0, 0, 0, -1))


def kwh_before_today(rows, now, cfg):
    """Energy this month up to last midnight. Changes once a day, so cacheable."""
    done = [r for r in rows if month_start(now) <= r[0] < day_start(now)]
    s = summarize(done, cfg["overhead_w"], wall_fn(cfg))
    return s["kwh"] if s else 0.0


def status(rows, now, cfg, month_kwh=0.0):
    """Return (big line, small line, ok). month_kwh excludes today."""
    if not rows or now - rows[-1][0] > MAX_GAP_S:
        return "logger down", "no fresh samples", False
    _, gpu_w, cpu_w, _ = rows[-1]
    overhead_w, cur, tariff = cfg["overhead_w"], cfg["currency"], cfg["tariff"]
    to_wall = wall_fn(cfg)
    wall_w = to_wall(gpu_w + cpu_w + overhead_w)
    today = summarize([r for r in rows if r[0] >= day_start(now)], overhead_w, to_wall)
    if today is None:
        return f"{wall_w:.0f} W", "collecting...", True
    month = (month_kwh + today["kwh"]) * tariff
    return f"{wall_w:.0f} W", f"today {cur}{today['kwh'] * tariff:.2f}  month {cur}{month:.2f}", True


def main():
    cfg = load_config()
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3, GLib, Gtk

    ind = AppIndicator3.Indicator.new(
        "power-applet", ICON_OK, AppIndicator3.IndicatorCategory.HARDWARE)
    ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    menu = Gtk.Menu()  # an indicator with no menu is not shown
    quit_item = Gtk.MenuItem(label="Quit")
    quit_item.connect("activate", Gtk.main_quit)
    menu.append(quit_item)
    menu.show_all()
    ind.set_menu(menu)

    cache = {"day": None, "kwh": 0.0}  # full-month read happens once a day

    def refresh():
        now = time.time()
        try:
            if cache["day"] != day_start(now):
                cache.update(day=day_start(now),
                             kwh=kwh_before_today(tail_rows(LOG, MONTH_BYTES), now, cfg))
            line1, line2, ok = status(tail_rows(LOG, TODAY_BYTES), now, cfg, cache["kwh"])
        except OSError as e:
            line1, line2, ok = "log unreadable", type(e).__name__, False
        ind.set_icon_full(ICON_OK if ok else ICON_BAD, "power")
        ind.set_label(f"{line1}  {line2}", "")
        return True  # keep the timer running

    refresh()
    GLib.timeout_add_seconds(REFRESH_S, refresh)
    Gtk.main()


if __name__ == "__main__":
    main()
