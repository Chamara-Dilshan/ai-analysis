"""
One self-contained HTML dashboard of the forward record, built on the VM and
sent to Telegram as a file (Sundays with the scorecard, and on /dashboard).
Nothing is hosted: the file opens in the phone's browser, offline.

    - headline tiles: system record, real record, drawdown, scorecard verdict
    - cumulative R per trade, paper and real, inside the backtests' 5-95%
      range for that many trades (the scorecard's cone)
    - paper drawdown in dollars against the -$60 review / -$100 stop lines
    - each gate's record, and the latest trades

Pure function of the journals; inline SVG plus a few lines of JS for the
hover readout. No dependencies, no network.
"""
import html
import json
import math
from datetime import datetime

import scorecard

CAPITAL = 200.0
W, H = 400, 230
ML, MR, MT, MB = 40, 74, 12, 26          # room on the right for direct labels


def _closed_sorted(trades):
    done = [t for t in trades if t.get("status") in ("win", "loss")]
    return sorted(done, key=lambda t: t.get("closed") or t.get("opened") or "")


def _cum(values):
    out, run = [], 0.0
    for v in values:
        run += v
        out.append(round(run, 3))
    return out


def _usd(t, default_risk):
    if "usd" in t:
        return t["usd"]
    return t.get("r", 0) * CAPITAL * t.get("risk_pct_used", default_risk) / 100


class _Scale:
    def __init__(self, n, lo, hi):
        self.n = max(n, 1)
        pad = (hi - lo) * 0.08 or 1.0
        self.lo, self.hi = lo - pad, hi + pad

    def x(self, i):                      # i = trade number, 0 = start
        return ML + (W - ML - MR) * i / self.n

    def y(self, v):
        return MT + (H - MT - MB) * (self.hi - v) / (self.hi - self.lo)


def _path(scale, ys, start=0.0):
    pts = [(scale.x(0), scale.y(start))] + [(scale.x(i + 1), scale.y(v)) for i, v in enumerate(ys)]
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def _ticks(lo, hi, count=5):
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    out, v = [], start
    while v <= hi:
        if v >= lo:
            out.append(round(v, 6))
        v += step
    return out


def _axes(scale, unit):
    parts = []
    for v in _ticks(scale.lo, scale.hi):
        y = scale.y(v)
        cls = "zero" if v == 0 else "grid"
        label = (f"{'-' if v < 0 else ''}${abs(v):g}" if unit == "$" else f"{v:g}R")
        parts.append(f'<line class="{cls}" x1="{ML}" x2="{W - MR}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text class="tick" x="{ML - 6}" y="{y + 4:.1f}" text-anchor="end">{label}</text>')
    parts.append(f'<text class="tick" x="{ML}" y="{H - 8}">trade 0</text>'
                 f'<text class="tick" x="{W - MR}" y="{H - 8}" text-anchor="end">trade {scale.n}</text>')
    return "".join(parts)


def _equity_chart(paper_cum, real_cum, cone):
    n = max(len(paper_cum), len(real_cum), 1)
    vals = paper_cum + real_cum + [0.0] + [v for b in cone for v in (b[0], b[2])]
    s = _Scale(n, min(vals), max(vals))
    parts = [_axes(s, "R")]
    if cone:
        up = [(s.x(i + 1), s.y(b[2])) for i, b in enumerate(cone)]
        dn = [(s.x(i + 1), s.y(b[0])) for i, b in enumerate(cone)][::-1]
        poly = [(s.x(0), s.y(0))] + up + dn
        parts.append('<path class="cone" d="M' + " L".join(f"{x:.1f},{y:.1f}" for x, y in poly) + ' Z"/>')
        parts.append(f'<path class="median" d="{_path(s, [b[1] for b in cone])}"/>')
        last = cone[-1]
        parts.append(f'<text class="cone-label" x="{s.x(len(cone)) + 6:.1f}" y="{s.y(last[2]) + 4:.1f}">95%</text>'
                     f'<text class="cone-label" x="{s.x(len(cone)) + 6:.1f}" y="{s.y(last[0]) + 4:.1f}">5%</text>')
    for cls, ys, label in (("paper", paper_cum, "Paper"), ("real", real_cum, "Real")):
        if not ys:
            continue
        parts.append(f'<path class="line {cls}" d="{_path(s, ys)}"/>')
        x, y = s.x(len(ys)), s.y(ys[-1])
        parts.append(f'<circle class="dot {cls}" cx="{x:.1f}" cy="{y:.1f}" r="4"/>'
                     f'<text class="direct" x="{x + 8:.1f}" y="{y + 4:.1f}">{label} {ys[-1]:+.1f}R</text>')
    return s, "".join(parts)


def _drawdown_chart(dd):
    lo = min(dd + [-105.0])
    s = _Scale(len(dd), lo, 0.0)
    parts = [_axes(s, "$")]
    for level, name in ((-60, "review"), (-100, "stop")):
        y = s.y(level)
        parts.append(f'<line class="limit" x1="{ML}" x2="{W - MR}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text class="direct" x="{W - MR + 6}" y="{y + 4:.1f}">{name} ${-level}</text>')
    if dd:
        area = _path(s, dd) + f" L{s.x(len(dd)):.1f},{s.y(0):.1f} Z"
        parts.append(f'<path class="dd-area" d="{area}"/>'
                     f'<path class="line paper" d="{_path(s, dd)}"/>')
    return s, "".join(parts)


def _gate_rows(paper):
    rows = []
    for mode in ("reversal", "continuation"):
        ts = [t for t in _closed_sorted(paper) if t.get("mode", "reversal") == mode]
        if not ts:
            continue
        wins = sum(1 for t in ts if t["status"] == "win")
        r = sum(t.get("r", 0) for t in ts)
        rows.append(f"<tr><td>{mode}</td><td class='num'>{len(ts)}</td>"
                    f"<td class='num'>{100 * wins / len(ts):.0f}%</td>"
                    f"<td class='num'>{r:+.1f}R</td><td class='num'>{r / len(ts):+.2f}R</td></tr>")
    return "".join(rows)


def _recent_rows(paper, real, limit=15, public=False):
    taken = {(t["symbol"], t.get("alert_entry")) for t in real}
    items = [("Paper", t, "" if public else
              ("taken" if (t["symbol"], t["entry"]) in taken else "skipped"))
             for t in _closed_sorted(paper)]
    items += [("Real", t, t.get("source", "")) for t in _closed_sorted(real)]
    items.sort(key=lambda it: it[1].get("closed", ""), reverse=True)
    rows = []
    for kind, t, note in items[:limit]:
        r = t.get("r", 0)
        rows.append(f"<tr><td>{html.escape(t.get('closed', '')[:10])}</td><td>{kind}</td>"
                    f"<td>{html.escape(t['symbol'].replace('USDT', ''))}</td>"
                    f"<td>{html.escape(t.get('mode', ''))}</td><td>{t['direction']}</td>"
                    f"<td class='num {'pos' if r > 0 else 'neg'}'>{r:+.2f}R</td>"
                    f"<td>{html.escape(note)}</td></tr>")
    return "".join(rows) or "<tr><td colspan='7'>No closed trades yet.</td></tr>"


REPO_URL = "https://github.com/Chamara-Dilshan/ai-analysis"

PUBLIC_INTRO = (
    '<p class="note" style="margin-top:10px">The live forward record of an automated crypto '
    'futures setup monitor: every setup it has fired since August 2026, scored as paper trades '
    '(fees included) against the range its backtests predicted for that many trades. Rebuilt '
    'daily by the monitor itself. <a href="' + REPO_URL + '">Source and case study</a>.</p>')

PUBLIC_FOOTER = (
    '<p class="note" style="margin-top:18px">Not financial advice. This is a research and '
    'engineering project; it places no orders and recommends no trades. Paper results assume '
    'fills at the alert price with fees and no slippage. '
    '<a href="' + REPO_URL + '">github.com/Chamara-Dilshan/ai-analysis</a></p>')


def build_dashboard(paper: list, real: list, now: datetime, version: str = "",
                    sequences: list = None, public: bool = False) -> str:
    """The private dashboard (sent to Telegram) shows both journals. With
    public=True it is the page published on the web: the paper record only.
    Real trades, their fills and which alerts were taken never appear."""
    if public:
        real = []
    sequences = sequences if sequences is not None else scorecard.load_reference()
    p_done, r_done = _closed_sorted(paper), _closed_sorted(real)
    paper_cum = _cum([t.get("r", 0) for t in p_done])
    real_cum = _cum([t.get("r", 0) for t in r_done])
    cone = [b for b in (scorecard.expected_band(i, sequences)
                        for i in range(1, len(paper_cum) + 1)) if b]

    equity_usd = _cum([_usd(t, 2.0) for t in p_done])
    peak, dd = 0.0, []
    for e in equity_usd:
        peak = max(peak, e)
        dd.append(round(e - peak, 2))

    eq_scale, eq_svg = _equity_chart(paper_cum, real_cum, cone)
    dd_scale, dd_svg = _drawdown_chart(dd)

    n, total = len(p_done), (paper_cum[-1] if paper_cum else 0.0)
    band = scorecard.expected_band(n, sequences) if n else None
    verdict = scorecard.verdict(n, total, band) if n else "no closed trades yet."
    wins = sum(1 for t in p_done if t["status"] == "win")
    r_total = real_cum[-1] if real_cum else 0.0
    r_usd = sum(_usd(t, 2.0) for t in r_done)
    dd_now = dd[-1] if dd else 0.0

    hover = {
        "eq": {"x": [eq_scale.x(i + 1) for i in range(max(len(paper_cum), len(real_cum)))],
               "paper": paper_cum, "real": real_cum,
               "lo": [b[0] for b in cone], "hi": [b[2] for b in cone]},
        "dd": {"x": [dd_scale.x(i + 1) for i in range(len(dd))], "dd": dd},
    }

    real_tile = "" if public else (
        f'<div class="tile"><div class="k">You (real)</div><div class="v">{r_total:+.2f}R</div>'
        f'<div class="d">{len(r_done)} trades · {"+" if r_usd >= 0 else "-"}${abs(r_usd):.2f} '
        f'before fees</div></div>')
    real_legend = "" if public else (
        '<span><b style="background:var(--series-2)"></b>Real (trades you took)</span>')

    return TEMPLATE.format(
        heading="Crypto setup monitor: live record" if public else "Trading dashboard",
        intro=PUBLIC_INTRO if public else "",
        footer=PUBLIC_FOOTER if public else "",
        real_tile=real_tile, real_legend=real_legend,
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        version=html.escape(version or "unknown version"),
        p_n=n, p_wins=f"{100 * wins / n:.0f}%" if n else "-",
        p_r=f"{total:+.1f}R",
        dd=f"${-dd_now:.2f}", dd_pct=f"{min(100, -dd_now):.0f}",
        verdict=html.escape(verdict),
        band=(f"{band[0]:+.1f}R to {band[2]:+.1f}R" if band else "n/a"),
        eq_svg=eq_svg, dd_svg=dd_svg, W=W, H=H,
        gate_rows=_gate_rows(paper) or "<tr><td colspan='5'>No closed trades yet.</td></tr>",
        recent_rows=_recent_rows(paper, real, public=public),
        data=json.dumps(hover),
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Dashboard</title>
<style>
:root {{
  color-scheme: light;
  --surface: #fcfcfb; --card: #ffffff; --border: #e4e3df;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
  --series-1: #2a78d6; --series-2: #eb6834;
  --cone: rgba(82, 81, 78, 0.12); --grid: #ecebe7; --limit: #a3a29c;
  --pos: #1d7a3a; --neg: #b3312f;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --surface: #1a1a19; --card: #222220; --border: #34332f;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
    --series-1: #3987e5; --series-2: #d95926;
    --cone: rgba(195, 194, 183, 0.14); --grid: #2c2b28; --limit: #6f6e67;
    --pos: #5cc27c; --neg: #e66767;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--surface); color: var(--text-primary);
  font: 15px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 20px 16px 40px; }}
h1 {{ font-size: 22px; margin: 0 0 2px; }}
h2 {{ font-size: 16px; margin: 0 0 4px; }}
.sub, .note {{ color: var(--text-secondary); font-size: 13px; margin: 0; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px; margin: 18px 0; }}
.tile, .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }}
.tile .k {{ color: var(--text-secondary); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
.tile .v {{ font-size: 24px; font-weight: 650; font-variant-numeric: tabular-nums; }}
.tile .d {{ color: var(--text-secondary); font-size: 12px; }}
.meter {{ height: 6px; background: var(--grid); border-radius: 3px; margin-top: 6px; position: relative; }}
.meter span {{ display: block; height: 100%; background: var(--text-secondary); border-radius: 3px; }}
.meter i {{ position: absolute; top: -3px; width: 2px; height: 12px; background: var(--limit); }}
.card {{ margin-bottom: 14px; position: relative; }}
svg {{ width: 100%; height: auto; display: block; margin-top: 8px; overflow: visible; }}
.grid {{ stroke: var(--grid); stroke-width: 1; }}
.zero {{ stroke: var(--limit); stroke-width: 1; }}
.limit {{ stroke: var(--limit); stroke-width: 1.5; stroke-dasharray: 5 4; }}
.tick, .cone-label {{ fill: var(--text-muted); font-size: 11px; }}
svg {{ max-width: 560px; }}
.direct {{ fill: var(--text-secondary); font-size: 11.5px; font-weight: 600; }}
.cone {{ fill: var(--cone); stroke: none; }}
.median {{ fill: none; stroke: var(--limit); stroke-width: 1; stroke-dasharray: 2 3; }}
.line {{ fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }}
.line.paper {{ stroke: var(--series-1); }} .line.real {{ stroke: var(--series-2); }}
.dot {{ stroke: var(--card); stroke-width: 2; }}
.dot.paper {{ fill: var(--series-1); }} .dot.real {{ fill: var(--series-2); }}
.dd-area {{ fill: var(--series-1); opacity: .14; }}
.legend {{ display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); margin-top: 6px; }}
.legend b {{ display: inline-block; width: 14px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 5px; }}
.cross {{ stroke: var(--text-muted); stroke-width: 1; visibility: hidden; }}
.tip {{ position: absolute; pointer-events: none; background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 9px; font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.12);
  white-space: nowrap; display: none; font-variant-numeric: tabular-nums; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }}
th, td {{ text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--border); }}
th {{ color: var(--text-secondary); font-weight: 600; font-size: 12px; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: var(--pos); }} .neg {{ color: var(--neg); }}
.scroll {{ overflow-x: auto; }}
</style></head>
<body><main>
<h1>{heading}</h1>
<p class="sub">Generated {generated} · {version}</p>
{intro}
<div class="tiles">
  <div class="tile"><div class="k">System (paper)</div><div class="v">{p_r}</div>
    <div class="d">{p_n} trades · {p_wins} wins</div></div>
  {real_tile}
  <div class="tile"><div class="k">Drawdown</div><div class="v">{dd}</div>
    <div class="meter"><span style="width:{dd_pct}%"></span><i style="left:60%"></i><i style="left:calc(100% - 2px)"></i></div>
    <div class="d">review at $60 · stop at $100</div></div>
</div>

<div class="card"><h2>Scorecard verdict</h2>
  <p class="note">{verdict} Normal range for this many trades: {band}.</p></div>

<div class="card" id="eq"><h2>Cumulative R per trade</h2>
  <p class="note">Shaded: the range 90% of backtest stretches of the same length landed in.</p>
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Cumulative R, paper and real, against the backtest range">{eq_svg}
    <line class="cross" y1="14" y2="{H}" x1="-10" x2="-10"/></svg>
  <div class="legend"><span><b style="background:var(--series-1)"></b>Paper (every alert)</span>
    {real_legend}
    <span><b style="background:var(--cone);height:8px"></b>Backtest 5–95%</span></div>
  <div class="tip"></div></div>

<div class="card" id="dd"><h2>Drawdown from the high</h2>
  <p class="note">Paper record, in dollars on $200.</p>
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Paper drawdown in dollars with review and stop lines">{dd_svg}
    <line class="cross" y1="14" y2="{H}" x1="-10" x2="-10"/></svg>
  <div class="tip"></div></div>

<div class="card"><h2>By setup</h2><div class="scroll"><table>
  <tr><th>Gate</th><th class="num">Trades</th><th class="num">Wins</th><th class="num">Total</th><th class="num">Per trade</th></tr>
  {gate_rows}</table></div></div>

<div class="card"><h2>Latest trades</h2><div class="scroll"><table>
  <tr><th>Closed</th><th>Record</th><th>Coin</th><th>Gate</th><th>Side</th><th class="num">Result</th><th></th></tr>
  {recent_rows}</table></div></div>
{footer}
</main>
<script>
const D = {data};
function hook(id, rows) {{
  const card = document.getElementById(id), svg = card.querySelector("svg"),
        tip = card.querySelector(".tip"), cross = svg.querySelector(".cross"), xs = D[id].x;
  if (!xs.length) return;
  function show(ev) {{
    const r = svg.getBoundingClientRect(), k = {W} / r.width;
    const vx = (ev.clientX - r.left) * k;
    let i = 0;
    for (let j = 1; j < xs.length; j++) if (Math.abs(xs[j] - vx) < Math.abs(xs[i] - vx)) i = j;
    cross.setAttribute("x1", xs[i]); cross.setAttribute("x2", xs[i]); cross.style.visibility = "visible";
    tip.innerHTML = "<b>Trade " + (i + 1) + "</b><br>" + rows(i).join("<br>");
    tip.style.display = "block";
    const left = xs[i] / k + svg.offsetLeft + 12;
    tip.style.left = Math.min(left, card.clientWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = (svg.offsetTop + 8) + "px";
  }}
  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", () => {{ tip.style.display = "none"; cross.style.visibility = "hidden"; }});
}}
const f = v => (v >= 0 ? "+" : "") + v.toFixed(2);
hook("eq", i => [
  D.eq.paper[i] !== undefined ? "Paper " + f(D.eq.paper[i]) + "R" : null,
  D.eq.real[i] !== undefined ? "Real " + f(D.eq.real[i]) + "R" : null,
  D.eq.lo[i] !== undefined ? "Range " + f(D.eq.lo[i]) + " to " + f(D.eq.hi[i]) + "R" : null
].filter(Boolean));
hook("dd", i => ["Drawdown $" + (-D.dd.dd[i]).toFixed(2)]);
</script>
</body></html>
"""
