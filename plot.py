"""Write power_report.html: stat tiles, daily cost bars, 7 days of stacked power.

Stdlib only. Static inline SVG plus a small script for tooltips; values are also
in the table, so nothing depends on hover.
"""
import html
import json
import math
import time

BUCKET_S = 1800  # 30-minute points: finer is noise at this width
POWER_DAYS = 7
COST_DAYS = 31
W, H = 760, 240
LEFT, RIGHT, TOP, BOTTOM = 56, 20, 16, 30
GUTTER = 56  # right-hand room for direct labels on the power chart
SERIES = (("gpu", "GPU", "GPU"), ("cpu", "CPU", "CPU"), ("other", "Board, RAM, disks, PSU loss", "Other"))
MIN_LABEL_PX = 14  # direct label only where the band is tall enough to hold it


def esc(text):
    return html.escape(str(text), quote=True)


def ticks(vmax, n=4):
    """Round axis ticks from 0 up past vmax."""
    raw = max(vmax, 1e-9) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw - 1e-12)
    count = max(1, math.ceil(vmax / step - 1e-9))
    return [round(i * step, 10) for i in range(count + 1)]


def buckets(rows, overhead_w, to_wall, since):
    """Mean GPU, CPU and remaining wall watts per BUCKET_S, from `since` on."""
    groups = {}
    for t, gpu_w, cpu_w, _ in rows:
        if t >= since:
            groups.setdefault(int(t // BUCKET_S), []).append((gpu_w, cpu_w))
    out = []
    for key in sorted(groups):
        g = groups[key]
        gpu = sum(x[0] for x in g) / len(g)
        cpu = sum(x[1] for x in g) / len(g)
        wall = sum(to_wall(a + b + overhead_w) for a, b in g) / len(g)
        out.append({"t": key * BUCKET_S, "gpu": gpu, "cpu": cpu, "other": wall - gpu - cpu})
    return out


def runs(points):
    """Split points wherever a bucket is missing (PC off or logger down)."""
    chunks = []
    for p in points:
        if chunks and p["t"] - chunks[-1][-1]["t"] <= BUCKET_S:
            chunks[-1].append(p)
        else:
            chunks.append([p])
    return chunks


def scale(lo, hi, out_lo, out_hi):
    span = (hi - lo) or 1
    return lambda v: out_lo + (v - lo) * (out_hi - out_lo) / span


def y_axis(tick_vals, y, fmt):
    parts = []
    for v in tick_vals:
        parts.append(f'<line class="grid" x1="{LEFT}" x2="{W - RIGHT}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                     f'<text class="tick" x="{LEFT - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{esc(fmt(v))}</text>')
    parts.append(f'<line class="axis" x1="{LEFT}" x2="{W - RIGHT}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    return "".join(parts)


def bar_path(x, w, top, base, r=4):
    r = min(r, w / 2, max(base - top, 0))
    return (f"M{x:.1f},{base:.1f}V{top + r:.1f}Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f}V{base:.1f}Z")


def spread(mids, gap=MIN_LABEL_PX):
    """Push label centres apart so no two are closer than gap, keeping their order."""
    out = []
    for m in sorted(mids):
        out.append(max(m, out[-1] + gap) if out else m)
    return out


def cost_chart(days, tariff, cur, per_day_kwh, today):
    """Bars of cost per day; today and the first logged day are partial and faded."""
    items = list(days.items())[-COST_DAYS:]
    partial_days = {min(days), today}
    costs = [d["kwh"] * tariff for _, d in items]
    ref = per_day_kwh * tariff if per_day_kwh else None
    tv = ticks(max(costs + [ref or 0]))
    y = scale(0, tv[-1], H - BOTTOM, TOP)
    slot = (W - LEFT - RIGHT) / max(len(items), 1)
    bw = max(2.0, slot - 2)  # 2px surface gap between bars
    parts = [y_axis(tv, y, lambda v: f"{cur}{v:g}")]
    every = max(1, math.ceil(len(items) / 10))
    for i, ((day, d), c) in enumerate(zip(items, costs)):
        x = LEFT + i * slot + 1
        cls = "bar partial" if day in partial_days else "bar"
        parts.append(f'<path class="{cls}" tabindex="0" data-i="{i}" d="{bar_path(x, bw, y(c), y(0))}">'
                     f'<title>{esc(day)}: {esc(cur)}{c:.2f}</title></path>')
        if i % every == 0:
            parts.append(f'<text class="tick" x="{x + bw / 2:.1f}" y="{H - 10}" '
                         f'text-anchor="middle">{esc(day[5:])}</text>')
    if ref:
        parts.append(f'<line class="ref" x1="{LEFT}" x2="{W - RIGHT}" y1="{y(ref):.1f}" y2="{y(ref):.1f}"/>'
                     f'<text class="label" x="{W - RIGHT}" y="{y(ref) - 6:.1f}" text-anchor="end">'
                     f'7-day average {esc(cur)}{ref:.2f}/day</text>')
    return f'<svg id="cost" viewBox="0 0 {W} {H}" role="img" aria-label="Cost per day">{"".join(parts)}</svg>'


def band_polygon(run, x, y, lower_keys, upper_keys):
    top = [(x(p["t"]), y(sum(p[k] for k in upper_keys))) for p in run]
    bottom = [(x(p["t"]), y(sum(p[k] for k in lower_keys))) for p in reversed(run)]
    pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in top + bottom)
    return pts


def power_chart(points, start, end):
    """Stacked areas: GPU, then CPU, then everything else, in wall watts."""
    tv = ticks(max([p["gpu"] + p["cpu"] + p["other"] for p in points] or [1]))
    x = scale(start, end, LEFT, W - GUTTER)
    y = scale(0, tv[-1], H - BOTTOM, TOP)
    parts = [y_axis(tv, y, lambda v: f"{v:g} W")]
    day = time.mktime(time.localtime(start)[:3] + (0, 0, 0, 0, 0, -1)) + 86400
    while day < end:
        parts.append(f'<text class="tick" x="{x(day):.1f}" y="{H - 10}" text-anchor="middle">'
                     f'{esc(time.strftime("%a %d", time.localtime(day)))}</text>')
        day += 86400
    keys = [k for k, _, _ in SERIES]
    for n, key in enumerate(keys):
        for run in runs(points):
            pts = band_polygon(run, x, y, keys[:n], keys[:n + 1])
            parts.append(f'<polygon class="area s{n + 1}" points="{pts}"/>')
    if points:  # labels in the gutter beside the latest point, never over the data
        last, base, mids = points[-1], 0.0, []
        for key in keys:
            mids.append((y(base) + y(base + last[key])) / 2)
            base += last[key]
        order = sorted(range(len(keys)), key=lambda i: mids[i])
        for i, mid in zip(order, spread([mids[i] for i in order])):
            parts.append(f'<text class="label" x="{W - GUTTER + 6}" y="{mid + 4:.1f}">'
                         f'{esc(SERIES[i][2])}</text>')
    parts.append(f'<line id="xh" class="crosshair" y1="{TOP}" y2="{H - BOTTOM}" visibility="hidden"/>'
                 f'<rect id="hit" x="{LEFT}" y="{TOP}" width="{W - LEFT - GUTTER}" '
                 f'height="{H - TOP - BOTTOM}" fill="transparent"/>')
    return f'<svg id="power" viewBox="0 0 {W} {H}" role="img" aria-label="Power over 7 days">{"".join(parts)}</svg>'


CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;
--text-secondary:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
color-scheme:dark;--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70}}
body{margin:0}
.viz-root{background:var(--page);color:var(--text-primary);min-height:100vh;
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px}
.wrap{max-width:820px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 8px}
.sub{color:var(--text-secondary);margin:0 0 20px}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.tile,.card{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.tile{flex:1 1 180px}.tile b{display:block;font-size:26px;font-weight:600}
.tile span{color:var(--text-secondary)}
.card{margin-bottom:16px}svg{width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.label{fill:var(--text-secondary);font-size:12px}
.bar{fill:var(--s1)}.bar.partial{opacity:.45}.bar:hover,.bar:focus{opacity:.8;outline:none}
.ref{stroke:var(--text-secondary);stroke-width:1.5}
.area{stroke:var(--surface-1);stroke-width:2;stroke-linejoin:round}
.s1{fill:var(--s1)}.s2{fill:var(--s2)}.s3{fill:var(--s3)}
.crosshair{stroke:var(--text-secondary);stroke-width:1}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--text-secondary);margin-bottom:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.note{color:var(--text-secondary);font-size:12px;margin:6px 0 0}
#tip{position:absolute;pointer-events:none;background:var(--surface-1);color:var(--text-primary);
border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:12px;
box-shadow:0 2px 8px rgba(0,0,0,.15)}
#tip .key{display:inline-block;width:12px;height:2px;vertical-align:middle;margin-right:6px}
#tip span{color:var(--text-secondary)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:4px 8px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}th{color:var(--text-secondary);font-weight:500}
summary{cursor:pointer;color:var(--text-secondary)}
"""

JS = """
const D = JSON.parse(document.getElementById('data').textContent);
const tip = document.getElementById('tip');
function show(rows, px, py) {
  tip.replaceChildren();
  for (const [value, label, color] of rows) {
    const row = document.createElement('div');
    if (color) { const k = document.createElement('i'); k.className = 'key';
      k.style.background = color; row.append(k); }
    const b = document.createElement('b'); b.textContent = value; row.append(b);
    if (label) { const s = document.createElement('span'); s.textContent = ' ' + label; row.append(s); }
    tip.append(row);
  }
  tip.hidden = false;
  tip.style.left = Math.min(px + 14, window.scrollX + innerWidth - tip.offsetWidth - 8) + 'px';
  tip.style.top = (py + 14) + 'px';
}
function hide() { tip.hidden = true; }
for (const bar of document.querySelectorAll('.bar')) {
  const [day, cost, kwh, hours] = D.days[bar.dataset.i];
  const rows = [[cost, day], [kwh, ''], [hours, '']];
  bar.addEventListener('pointermove', e => show(rows, e.pageX, e.pageY));
  bar.addEventListener('focus', () => { const r = bar.getBoundingClientRect();
    show(rows, r.right + scrollX, r.top + scrollY); });
  bar.addEventListener('pointerleave', hide); bar.addEventListener('blur', hide);
}
const svg = document.getElementById('power'), hit = document.getElementById('hit'),
      xh = document.getElementById('xh');
if (D.power.length) {
  hit.addEventListener('pointermove', e => {
    const pt = new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
    let best = D.power[0];
    for (const p of D.power) if (Math.abs(p[0] - pt.x) < Math.abs(best[0] - pt.x)) best = p;
    xh.setAttribute('x1', best[0]); xh.setAttribute('x2', best[0]); xh.setAttribute('visibility', 'visible');
    show([[best[2], best[1]], [best[3], 'GPU', 'var(--s1)'], [best[4], 'CPU', 'var(--s2)'],
          [best[5], 'board, RAM, disks, PSU loss', 'var(--s3)']], e.pageX, e.pageY);
  });
  hit.addEventListener('pointerleave', () => { xh.setAttribute('visibility', 'hidden'); hide(); });
}
"""


def safe_json(obj):
    """JSON that cannot close the <script> element it sits in."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def tooltip_data(items, tariff, cur, points, start, end):
    x = scale(start, end, LEFT, W - GUTTER)
    days = [[day, f"{cur}{d['kwh'] * tariff:.2f}", f"{d['kwh']:.1f} kWh", f"{d['covered_h']:.1f} h logged"]
            for day, d in items]
    power = [[round(x(p["t"]), 1), time.strftime("%a %d %b %H:%M", time.localtime(p["t"])),
              f"{p['gpu'] + p['cpu'] + p['other']:.0f} W total", f"{p['gpu']:.0f} W",
              f"{p['cpu']:.0f} W", f"{p['other']:.0f} W"] for p in points]
    return {"days": days, "power": power}


def tiles(stats):
    return "".join(f'<div class="tile"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for k, v in stats)


def table(items, tariff, cur):
    head = "<tr><th>Day</th><th>GPU W</th><th>CPU W</th><th>Wall W</th><th>kWh</th><th>Cost</th><th>Hours logged</th></tr>"
    body = "".join(
        f"<tr><td>{esc(day)}</td><td>{d['gpu_w']:.0f}</td><td>{d['cpu_w']:.0f}</td><td>{d['wall_w']:.0f}</td>"
        f"<td>{d['kwh']:.1f}</td><td>{esc(cur)}{d['kwh'] * tariff:.2f}</td><td>{d['covered_h']:.1f}</td></tr>"
        for day, d in reversed(items))
    return f"<details><summary>Table of every day</summary><table>{head}{body}</table></details>"


def page(stats, days, rows, overhead_w, to_wall, tariff, cur, per_day_kwh, now):
    """Return the full HTML document."""
    items = list(days.items())[-COST_DAYS:]
    start = max(now - POWER_DAYS * 86400, rows[0][0] if rows else now)
    points = buckets(rows, overhead_w, to_wall, start)
    legend = "".join(f'<span><i class="s{n + 1}" style="background:var(--s{n + 1})"></i>{esc(label)}</span>'
                     for n, (_, label, _) in enumerate(SERIES))
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC power and cost</title><style>{CSS}</style></head>
<body><div class="viz-root"><div class="wrap">
<h1>PC power and cost</h1>
<p class="sub">Generated {esc(time.strftime("%d %b %Y %H:%M", time.localtime(now)))}. GPU and CPU are measured;
board, RAM, disks and PSU losses are estimated.</p>
<div class="tiles">{tiles(stats)}</div>
<div class="card"><h2>Cost per day</h2>{cost_chart(days, tariff, cur, per_day_kwh, today)}
<p class="note">Faded bars are partial days: the first day logged and today.</p></div>
<div class="card"><h2>Power at the wall, last {POWER_DAYS} days</h2><div class="legend">{legend}</div>
{power_chart(points, start, now)}<p class="note">Gaps are times the PC was off or the logger was down.</p></div>
<div class="card">{table(items, tariff, cur)}</div>
</div><div id="tip" hidden></div></div>
<script type="application/json" id="data">{safe_json(tooltip_data(items, tariff, cur, points, start, now))}</script>
<script>{JS}</script></body></html>
"""
