# ruff: noqa: E501
"""Build an interactive explorer for the Tom polyfit_dodge local review.

The page embeds ownship telemetry from saved fuzz reports. It does not publish
radar rows, connection data or hidden debris state. Local points are the
published formula on local counters, not STP scores.

Example:
    uv run --locked python experiments/tom_polyfit_dodge_explorer.py \
        --series "6 m/s source=runs/.../official-cruise6-seeds100-104.json" \
        --series "7 m/s=runs/.../official-cruise7-seeds100-104.json" \
        --series "8 m/s benchmark=runs/.../official-cruise8-seeds100-104.json" \
        --output docs/analytics/tom-polyfit-dodge/explorer.html
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from tom_polyfit_dodge_review import PALETTE, Run, load_series, summarize

from wsorb.control_contract import body_to_reference

MAX_X_M = 1305.8
MAX_ALT_M = 76.0
MAX_LAT_M = 18.0
MAX_T_S = 300.0


def _round(value: float, places: int = 2) -> float:
    return round(float(value), places)


def _compact_run(run: Run) -> dict:
    start = np.array(run.samples[0][1], dtype=float)
    target = np.array(run.samples[0][4], dtype=float)
    along = target - start
    along /= np.linalg.norm(along)
    indices = list(range(0, len(run.samples), 2))
    if indices[-1] != len(run.samples) - 1:
        indices.append(len(run.samples) - 1)
    samples = []
    for index in indices:
        sample = run.samples[index]
        position = np.array(sample[1], dtype=float)
        velocity = np.array(sample[2], dtype=float)
        command = np.array(sample[7], dtype=float)
        world_command = body_to_reference(command, sample[3], "active_xyzw")
        along_command = float(world_command @ along)
        lateral_command = float(np.linalg.norm(world_command - along_command * along))
        samples.append(
            [
                _round(sample[0], 1),
                _round(np.linalg.norm(velocity), 2),
                _round(sample[5], 2),
                _round(sample[6], 2),
                _round(np.linalg.norm(target - position), 1),
                _round(along_command, 3),
                _round(lateral_command, 3),
            ]
        )
    path = [
        [_round(entry[0], 1), _round(entry[1], 1), _round(entry[2], 1), _round(entry[3], 1)]
        for index, entry in enumerate(run.path)
        if index in set(indices)
    ]
    return {
        "seed": run.seed,
        "state": run.state,
        "status": run.status,
        "points": run.points,
        "time_s": _round(run.time_s, 1),
        "fuel_kg": _round(run.fuel_kg, 2),
        "damage_j": _round(run.damage_j, 2),
        "range_m": _round(run.range_m, 1),
        "speed_mps": _round(run.speed_mps, 2),
        "initial_range_m": _round(float(np.linalg.norm(target - start)), 1),
        "extra_distance_m": _round(run.extra_distance_m, 2),
        "correction_episodes": run.correction_episodes,
        "max_speed_mps": _round(run.max_speed_mps, 2),
        "samples": samples,
        "path": path,
    }


def build_dataset(collections: dict[str, list[Run]]) -> dict:
    series = []
    for index, (label, runs) in enumerate(collections.items()):
        series.append(
            {
                "label": label,
                "color": PALETTE[index % len(PALETTE)],
                "summary": summarize(runs),
                "runs": [_compact_run(run) for run in runs],
            }
        )
    return {
        "schema": "wsorb.tom_polyfit_dodge_explorer",
        "schema_version": 1,
        "duration_s": MAX_T_S,
        "max_x_m": MAX_X_M,
        "max_alt_m": MAX_ALT_M,
        "max_lat_m": MAX_LAT_M,
        "series": series,
    }


HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tom polyfit_dodge explorer</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#475569;--line:#dbe3ee;--bg:#f8fafc;--card:#fff;--amber:#b45309;--blue:#1d4ed8;--red:#b91c1c;--green:#15803d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
header{position:sticky;top:0;z-index:3;padding:14px 20px 12px;background:#ffffffee;border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}
h1{margin:0;font-size:21px;font-weight:650}
h2{margin:0 0 8px;font-size:15px;font-weight:650}
h3{margin:0 0 6px;font-size:14px;font-weight:650}
p{margin:4px 0;color:var(--muted)}
a{color:var(--blue)}
.controls{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin-top:10px}
.group{display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.group>span{font-size:12px;color:var(--muted);margin-right:2px}
button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:5px 9px;font:inherit;cursor:pointer}
button[aria-pressed="true"]{background:var(--ink);color:#fff;border-color:var(--ink)}
button.small{padding:3px 7px;font-size:12px}
input[type=range]{width:min(420px,50vw);accent-color:var(--blue)}
main{padding:16px 20px 36px;max-width:1500px;margin:0 auto}
.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.card svg{display:block;width:100%;height:auto;background:#fff;border-radius:6px}
.cardHead{display:flex;align-items:baseline;gap:8px}
.cardHead .pill{margin-left:auto;border:1px solid var(--line);border-radius:999px;padding:1px 8px;font-size:12px;color:var(--muted)}
.hint{font-size:12px;color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:10px 0 0;padding:0;list-style:none}
.stats div{background:#f8fafc;border:1px solid var(--line);border-radius:7px;padding:6px 8px}
.stats b{display:block;font-size:16px}.stats span{font-size:11px;color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:var(--muted);margin:2px 0 8px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left;color:var(--muted)}
th{font-weight:650;color:var(--muted)}
tr.selected td{background:#eef2ff}
tr.clickable{cursor:pointer}
.insights{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}
.insight{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.insight p{font-size:13px}
.insight .tag{display:inline-block;font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 7px;margin-bottom:6px}
.warn{border-color:#fecaca;background:#fff7f7}
.notice{border-left:3px solid var(--amber);padding-left:10px}
.callout{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:8px}
.note{font-size:12px;color:var(--muted);margin-top:10px}
@media (max-width:1100px){.grid4{grid-template-columns:repeat(2,minmax(0,1fr))}.insights{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:700px){.grid4,.grid2,.insights,.stats{grid-template-columns:1fr}}
</style>
<body>
<header>
  <h1>Tom polyfit_dodge explorer</h1>
  <p>Local official cloud, seeds 100–104, 4× native playback. Ownship telemetry only. Local points, not STP scores.</p>
  <div class="controls">
    <div class="group" id="series"></div>
    <div class="group" id="seeds"></div>
    <button id="play" type="button">Play</button>
    <button id="reset" type="button">Reset</button>
    <label>t <output id="clock">0.0 s</output></label>
    <input id="scrub" type="range" min="0" max="300" step="0.5" value="0" aria-label="Flight time">
    <span class="group" id="speeds"></span>
  </div>
</header>
<main>
  <section class="grid4">
    <div class="card"><div class="cardHead"><h2>Top-down path</h2><span class="pill" id="topPill">seed 100</span></div><svg id="top" viewBox="0 0 420 280" role="img" aria-label="Top-down path"></svg><p class="hint">Launch to target, lateral axis.</p></div>
    <div class="card"><div class="cardHead"><h2>Altitude</h2><span class="pill" id="altPill">seed 100</span></div><svg id="alt" viewBox="0 0 420 280" role="img" aria-label="Altitude profile"></svg><p class="hint">World vertical relative to launch.</p></div>
    <div class="card"><div class="cardHead"><h2>Speed</h2></div><svg id="speed" viewBox="0 0 420 280" role="img" aria-label="Speed against time"></svg><p class="hint">Hover or drag the chart to scrub.</p></div>
    <div class="card"><div class="cardHead"><h2>Fuel</h2></div><svg id="fuel" viewBox="0 0 420 280" role="img" aria-label="Fuel against time"></svg><p class="hint">Each kg is 1.33 local points.</p></div>
  </section>

  <section class="grid2">
    <div class="card">
      <div class="cardHead"><h2>Control effort</h2><span class="pill" id="effortPill">selected</span></div>
      <div class="legend"><span><i style="background:#1d4ed8"></i>along target</span><span><i style="background:#b45309"></i>lateral magnitude</span></div>
      <svg id="effort" viewBox="0 0 640 260" role="img" aria-label="Commanded acceleration along and across the target line"></svg>
      <p class="hint">Full command is 0.5 m/s². The calibrated plant delivers about 0.11 m/s². A long lateral block is a dodge. A long zero block is a stop.</p>
    </div>
    <div class="card">
      <div class="cardHead"><h2>Running points if the run ended now</h2><span class="pill" id="pointsPill">local estimate</span></div>
      <svg id="points" viewBox="0 0 640 260" role="img" aria-label="Running local points against time"></svg>
      <p class="hint">Success formula inside 50 m. Otherwise the failure formula uses current range and initial range. Terminal dots are actual local points.</p>
    </div>
  </section>

  <section class="card" style="margin-top:12px">
    <div class="cardHead"><h2>Selected flight</h2><span class="pill" id="statusPill">—</span></div>
    <ul class="stats" id="stats"></ul>
    <div id="diagnosis"></div>
  </section>

  <section class="card" style="margin-top:12px">
    <h2>Runs in this series</h2>
    <div id="runsTable"></div>
    <p class="note">Click a row to inspect that seed. Timeout and error rows keep their measured fuel and range; missing terminal points stay missing, not zero.</p>
  </section>

  <section class="card" style="margin-top:12px">
    <h2>What to try next, without a rewrite</h2>
    <p>These are patches to the existing tracker, candidate sampler and governor. None of them requires a new planner or a new transport.</p>
    <div class="insights">
      <article class="insight">
        <span class="tag">DWA objective</span>
        <h3>Add the progress term</h3>
        <p>Your avoider is close to the dynamic window approach: sample reachable velocities, then score clearance, heading and speed. The published DWA objective has a heading term and a velocity term, not only distance. Add <code>lambda * max(0, cruise - along)</code> to the candidate cost. That removes the zero-speed equilibrium that breaks seed 101.</p>
        <p><a href="https://doi.org/10.1109/100.580977" target="_blank" rel="noreferrer">Fox, Burgard and Thrun, 1997</a></p>
      </article>
      <article class="insight">
        <span class="tag">Velocity obstacle</span>
        <h3>Use the collision cone</h3>
        <p>A rock maps to a cone of forbidden velocities in your velocity space. Instead of scoring 216 samples, project the current goal velocity onto the nearest point outside the union of cones. Keep the sampler as a fallback for clutter. This is a drop-in replacement for <code>clearance</code>, not a new guidance stack.</p>
        <p><a href="https://journals.sagepub.com/doi/10.1177/027836499801700706" target="_blank" rel="noreferrer">Fiorini and Shiller, 1998</a></p>
      </article>
      <article class="insight">
        <span class="tag">Safety filter</span>
        <h3>Check the next state, not just now</h3>
        <p>Your clearance assumes the new velocity applies instantly. A control barrier function safety filter checks that the next state keeps a positive margin: <code>h(next) >= (1-gamma) h(now)</code>. With a 0.11 m/s² plant, a 0.3 m/s dodge takes 2.7 s. Inflate the keep-out by the distance travelled during that ramp.</p>
        <p><a href="https://arxiv.org/abs/1903.11199" target="_blank" rel="noreferrer">Ames et al., 2019</a></p>
      </article>
      <article class="insight">
        <span class="tag">Zero-effort miss</span>
        <h3>First-sighting TTC</h3>
        <p>Each radar row already has surface range and range rate. Zero-effort miss is the miss distance if neither vehicle manoeuvres. Use <code>range / closing</code> for time-to-go and <code>range * sin(angle)</code> for the miss. That gives a usable TTC before three polyfit samples exist, which matters at a 4.17 s revisit.</p>
        <p><a href="https://secwww.jhuapl.edu/techdigest/content/techdigest/pdf/V29-N01/29-01-Palumbo_Homing.pdf" target="_blank" rel="noreferrer">Palumbo et al., 2010</a></p>
      </article>
      <article class="insight">
        <span class="tag">Tangent escape</span>
        <h3>Never settle on zero</h3>
        <p>When every candidate looks unsafe, a tangent-bug style fallback follows the obstacle boundary instead of stopping. In 3D, choose the tangent direction around the keep-out sphere that has the smallest angle to the goal. This is a local-minimum escape, not a global replanner.</p>
        <p><a href="https://journals.sagepub.com/doi/10.1177/027836499801700903" target="_blank" rel="noreferrer">Kamon and Rivlin, 1998</a></p>
      </article>
      <article class="insight">
        <span class="tag">Reachability</span>
        <h3>Reject inevitable states earlier</h3>
        <p>An inevitable collision state is a state where no future trajectory avoids collision. Use your actuation limit to reject candidates before the rock is close: if the lateral displacement available before TTC is less than the required miss, the candidate is not safe. This is the same idea as a reaction-time margin, with a hard threshold.</p>
        <p><a href="https://inria.hal.science/inria-00182082" target="_blank" rel="noreferrer">Fraichard and Asama, 2004</a></p>
      </article>
    </div>
    <p class="note">Keep the direct beeline. For a 1.3 km hop with a local cloud, a global A* or RRT* layer would add latency and fuel without addressing the stop-and-go failure. The fixes above stay inside your current tracker, avoider and governor.</p>
  </section>

  <p class="note">This page is local practice evidence, not an STP receipt. The raw fuzz reports are not published. The static review is <a href="README.md">README.md</a>; the graph is <a href="review.svg">review.svg</a>.</p>
</main>
<script id="flight-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById("flight-data").textContent);
const NS = "http://www.w3.org/2000/svg";
const state = { series: 0, seed: 100, t: 0, playing: false, speed: 4 };
const el = (name, attrs = {}) => { const node = document.createElementNS(NS, name); for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value); return node; };
const clear = svg => { while (svg.firstChild) svg.removeChild(svg.firstChild); };
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const fmt = (value, places = 1) => value === null || value === undefined || !Number.isFinite(value) ? "—" : value.toFixed(places);
const color = index => DATA.series[index].color;
const currentSeries = () => DATA.series[state.series];
const currentRuns = () => state.seed === "all" ? currentSeries().runs : currentSeries().runs.filter(run => run.seed === state.seed);
const currentRun = () => state.seed === "all" ? null : currentRunSafe();
function currentRunSafe() { return currentSeries().runs.find(run => run.seed === state.seed) || null; }
function runEnd(run) { return run.samples[run.samples.length - 1][0]; }
function maxTime() { return state.seed === "all" ? DATA.duration_s : Math.min(DATA.duration_s, runEnd(currentRun())); }
function sampleAt(run, t) { const samples = run.samples; if (!samples.length) return null; let lo = 0, hi = samples.length - 1; if (t <= samples[0][0]) return samples[0]; if (t >= samples[hi][0]) return samples[hi]; while (lo + 1 < hi) { const mid = (lo + hi) >> 1; if (samples[mid][0] <= t) lo = mid; else hi = mid; } return samples[lo]; }
function pathAt(run, t) { const path = run.path; if (!path.length) return null; let lo = 0, hi = path.length - 1; if (t <= path[0][0]) return path[0]; if (t >= path[hi][0]) return path[hi]; while (lo + 1 < hi) { const mid = (lo + hi) >> 1; if (path[mid][0] <= t) lo = mid; else hi = mid; } return path[lo]; }
function pointsAt(run, sample) {
  if (!sample) return null;
  const t = sample[0], fuel = sample[2], damage = sample[3], range = sample[4];
  if (t <= 0) return 0;
  const damageTerm = 2 * damage / 209200;
  const fuelTerm = fuel / 75;
  if (range <= 50) return 100 * (120 / t - damageTerm - fuelTerm + 0.5);
  if (!run.initial_range_m) return null;
  return 100 * (-range / run.initial_range_m - damageTerm - fuelTerm + 0.5);
}
function addText(svg, x, y, text, attrs = {}) { const node = el("text", { x, y, ...attrs }); node.textContent = text; svg.appendChild(node); return node; }
function grid(svg, width, height, pad, xMax, xLabel, yLabel, yMin, yMax) {
  const g = el("g", { stroke: "#e2e8f0", "stroke-width": "1" });
  for (let i = 0; i <= 4; i += 1) { const x = pad.l + (width - pad.l - pad.r) * i / 4; g.appendChild(el("line", { x1: x, y1: pad.t, x2: x, y2: height - pad.b })); addText(svg, x, height - 6, `${Math.round(xMax * i / 4)}`, { "font-size": 10, fill: "#64748b", "text-anchor": "middle" }); }
  for (let i = 0; i <= 4; i += 1) { const y = pad.t + (height - pad.t - pad.b) * i / 4; g.appendChild(el("line", { x1: pad.l, y1: y, x2: width - pad.r, y2: y })); const value = yMax - (yMax - yMin) * i / 4; addText(svg, pad.l - 6, y + 4, `${Math.round(value)}`, { "font-size": 10, fill: "#64748b", "text-anchor": "end" }); }
  svg.appendChild(g);
  addText(svg, (pad.l + width - pad.r) / 2, height - 22, xLabel, { "font-size": 11, fill: "#475569", "text-anchor": "middle" });
  addText(svg, 20, (pad.t + height - pad.b) / 2, yLabel, { "font-size": 11, fill: "#475569", "text-anchor": "middle", transform: `rotate(-90 20 ${(pad.t + height - pad.b) / 2})` });
}
function axes(svg, viewWidth, viewHeight, xMax, yMin, yMax, xLabel, yLabel) {
  const pad = { l: 42, r: 12, t: 12, b: 34 };
  grid(svg, viewWidth, viewHeight, pad, xMax, xLabel, yLabel, yMin, yMax);
  return { pad, x: value => pad.l + value / xMax * (viewWidth - pad.l - pad.r), y: value => viewHeight - pad.b - (value - yMin) / (yMax - yMin) * (viewHeight - pad.t - pad.b) };
}
function drawPath(svg, mode) {
  clear(svg);
  const width = 420, height = 280;
  const yMin = mode === "alt" ? 0 : -DATA.max_lat_m, yMax = mode === "alt" ? DATA.max_alt_m : DATA.max_lat_m;
  const map = axes(svg, width, height, DATA.max_x_m, yMin, yMax, "downrange, m", mode === "alt" ? "altitude, m" : "lateral, m");
  const runs = currentRuns();
  for (const run of runs) {
    const seriesIndex = DATA.series.indexOf(currentSeries());
    const selected = state.seed !== "all" && run.seed === state.seed;
    const points = run.path.filter(point => point[0] <= state.t).map(point => `${map.x(point[1]).toFixed(1)},${map.y(mode === "alt" ? point[3] : point[2]).toFixed(1)}`);
    if (points.length > 1) svg.appendChild(el("polyline", { points: points.join(" "), fill: "none", stroke: color(seriesIndex), "stroke-width": selected ? 2.6 : 1.1, opacity: selected ? 0.95 : 0.35 }));
    const current = pathAt(run, state.t);
    if (current) svg.appendChild(el("circle", { cx: map.x(current[1]), cy: map.y(mode === "alt" ? current[3] : current[2]), r: selected ? 4.5 : 2.2, fill: color(seriesIndex) }));
  }
  svg.appendChild(el("circle", { cx: map.x(DATA.max_x_m), cy: map.y(mode === "alt" ? 69 : 0), r: 6, fill: "none", stroke: "#64748b", "stroke-dasharray": "4 4" }));
}
function drawSeries(svg, field, yMin, yMax, label) {
  clear(svg);
  const width = 420, height = 280;
  const map = axes(svg, width, height, DATA.duration_s, yMin, yMax, "time, s", label);
  const seriesIndex = DATA.series.indexOf(currentSeries());
  for (const run of currentRuns()) {
    const selected = state.seed !== "all" && run.seed === state.seed;
    const points = run.samples.filter(sample => sample[0] <= state.t).map(sample => `${map.x(sample[0]).toFixed(1)},${map.y(sample[field]).toFixed(1)}`);
    if (points.length > 1) svg.appendChild(el("polyline", { points: points.join(" "), fill: "none", stroke: color(seriesIndex), "stroke-width": selected ? 2.4 : 1.1, opacity: selected ? 0.95 : 0.35 }));
    const current = sampleAt(run, state.t);
    if (current) svg.appendChild(el("circle", { cx: map.x(current[0]), cy: map.y(current[field]), r: selected ? 3.5 : 1.8, fill: color(seriesIndex) }));
  }
  svg.appendChild(el("line", { x1: map.x(state.t), y1: map.pad.t, x2: map.x(state.t), y2: height - map.pad.b, stroke: "#94a3b8", "stroke-dasharray": "3 3" }));
}
function drawEffort(svg) {
  clear(svg);
  const width = 640, height = 260;
  const map = axes(svg, width, height, DATA.duration_s, -0.55, 0.55, "time, s", "command, m/s²");
  const seriesIndex = DATA.series.indexOf(currentSeries());
  for (const run of currentRuns()) {
    const selected = state.seed !== "all" && run.seed === state.seed;
    for (const [field, stroke] of [[5, "#1d4ed8"], [6, "#b45309"]]) {
      const points = run.samples.filter(sample => sample[0] <= state.t).map(sample => `${map.x(sample[0]).toFixed(1)},${map.y(sample[field]).toFixed(1)}`);
      if (points.length > 1) svg.appendChild(el("polyline", { points: points.join(" "), fill: "none", stroke, "stroke-width": 1.7, opacity: 0.9 }));
    }
    const current = sampleAt(run, state.t);
    if (current) { svg.appendChild(el("circle", { cx: map.x(current[0]), cy: map.y(current[5]), r: 3, fill: "#1d4ed8" })); svg.appendChild(el("circle", { cx: map.x(current[0]), cy: map.y(current[6]), r: 3, fill: "#b45309" })); }
  }
  svg.appendChild(el("line", { x1: map.x(state.t), y1: map.pad.t, x2: map.x(state.t), y2: height - map.pad.b, stroke: "#94a3b8", "stroke-dasharray": "3 3" }));
}
function drawPoints(svg) {
  clear(svg);
  const width = 640, height = 260;
  const map = axes(svg, width, height, DATA.duration_s, -100, 70, "time, s", "local points");
  const seriesIndex = DATA.series.indexOf(currentSeries());
  for (const run of currentRuns()) {
    const selected = state.seed !== "all" && run.seed === state.seed;
    const points = run.samples.filter(sample => sample[0] <= state.t).map(sample => [sample, pointsAt(run, sample)]).filter(([, value]) => value !== null).map(([sample, value]) => `${map.x(sample[0]).toFixed(1)},${map.y(value).toFixed(1)}`);
    if (points.length > 1) svg.appendChild(el("polyline", { points: points.join(" "), fill: "none", stroke: color(seriesIndex), "stroke-width": selected ? 2.4 : 1.1, opacity: selected ? 0.95 : 0.35 }));
    if (run.points !== null) { const end = run.samples[run.samples.length - 1]; svg.appendChild(el("circle", { cx: map.x(end[0]), cy: map.y(run.points), r: selected ? 4.5 : 2.2, fill: color(seriesIndex), stroke: "#fff", "stroke-width": 1.2 })); }
  }
  svg.appendChild(el("line", { x1: map.x(state.t), y1: map.pad.t, x2: map.x(state.t), y2: height - map.pad.b, stroke: "#94a3b8", "stroke-dasharray": "3 3" }));
  svg.appendChild(el("line", { x1: map.pad.l, y1: map.y(0), x2: width - map.pad.r, y2: map.y(0), stroke: "#cbd5e1" }));
}
function updateStats() {
  const stats = document.getElementById("stats");
  const status = document.getElementById("statusPill");
  const diagnosis = document.getElementById("diagnosis");
  const series = currentSeries();
  if (state.seed === "all") {
    const summary = series.summary;
    status.textContent = `${summary.arrivals}/${summary.runs} arrivals`;
    stats.innerHTML = [["mean points", fmt(summary.mean_points, 2)], ["mean time", `${fmt(summary.mean_time_s, 1)} s`], ["mean fuel", `${fmt(summary.mean_fuel_kg, 2)} kg`], ["mean damage", `${fmt(summary.mean_damage_j, 2)} J`]].map(([name, value]) => `<div><b>${value}</b><span>${name}</span></div>`).join("");
    diagnosis.innerHTML = `<p class="notice">This is the series summary. Select a seed above to inspect its path, fuel and control effort. Seed 101 is the deadlock case.</p>`;
    return;
  }
  const run = currentRun();
  status.textContent = run.status === "success" ? "arrived" : run.state;
  stats.innerHTML = [
    ["points", fmt(run.points, 2)],
    ["time", `${fmt(run.time_s, 1)} s`],
    ["fuel", `${fmt(run.fuel_kg, 2)} kg`],
    ["damage", `${fmt(run.damage_j, 2)} J`],
    ["range", `${fmt(run.range_m, 1)} m`],
    ["max speed", `${fmt(run.max_speed_mps, 2)} m/s`],
    ["extra path", `${fmt(run.extra_distance_m, 2)} m`],
    ["later burns", String(run.correction_episodes)],
  ].map(([name, value]) => `<div><b>${value}</b><span>${name}</span></div>`).join("");
  if (run.seed === 101) {
    diagnosis.innerHTML = `<div class="insight warn"><h3>Seed 101: the avoidance deadlock</h3><p>At 6 m/s the shuttle holds 1.4 m/s, brakes to zero at 98 s, stays near zero until 169 s, then restarts. At 8 m/s it sits at zero for the first 80 s. The candidate cost has no progress term, so zero is a stable safe choice.</p><div class="callout"><button class="small" onclick="jumpTo(98)">Jump to 98 s</button><button class="small" onclick="jumpTo(169)">Jump to 169 s</button><button class="small" onclick="jumpTo(200)">Jump to 200 s</button></div></div>`;
  } else {
    diagnosis.innerHTML = `<p class="notice">The terminal points use the published formula on local time, fuel and damage. A running score is an estimate, not a terminal receipt.</p>`;
  }
}
function updateTable() {
  const series = currentSeries();
  const rows = series.runs.map(run => `<tr class="clickable ${run.seed === state.seed ? "selected" : ""}" onclick="selectSeed(${run.seed})"><td>${run.seed}</td><td>${run.status}</td><td>${fmt(run.points, 2)}</td><td>${fmt(run.time_s, 1)}</td><td>${fmt(run.fuel_kg, 2)}</td><td>${fmt(run.damage_j, 2)}</td><td>${fmt(run.range_m, 1)}</td><td>${run.correction_episodes}</td></tr>`).join("");
  document.getElementById("runsTable").innerHTML = `<table><thead><tr><th>seed</th><th>status</th><th>points</th><th>time s</th><th>fuel kg</th><th>damage J</th><th>range m</th><th>later burns</th></tr></thead><tbody>${rows}</tbody></table>`;
}
function render() {
  const maxT = maxTime();
  state.t = clamp(state.t, 0, maxT);
  document.getElementById("clock").textContent = `${state.t.toFixed(1)} s`;
  document.getElementById("scrub").max = String(maxT);
  document.getElementById("scrub").value = String(state.t);
  document.getElementById("topPill").textContent = state.seed === "all" ? "all seeds" : `seed ${state.seed}`;
  document.getElementById("altPill").textContent = state.seed === "all" ? "all seeds" : `seed ${state.seed}`;
  drawPath(document.getElementById("top"), "top");
  drawPath(document.getElementById("alt"), "alt");
  drawSeries(document.getElementById("speed"), 1, 0, 9, "speed, m/s");
  drawSeries(document.getElementById("fuel"), 2, 0, 55, "fuel, kg");
  drawEffort(document.getElementById("effort"));
  drawPoints(document.getElementById("points"));
  updateStats();
  updateTable();
}
function selectSeries(index) { state.series = index; state.seed = 100; state.t = 0; state.playing = false; buildButtons(); render(); }
function selectSeed(seed) { state.seed = seed; state.t = 0; state.playing = false; buildButtons(); render(); }
function jumpTo(t) { state.t = t; state.playing = false; render(); }
function buildButtons() {
  const seriesBox = document.getElementById("series");
  seriesBox.innerHTML = `<span>cruise</span>`;
  DATA.series.forEach((series, index) => { const button = document.createElement("button"); button.textContent = series.label; button.setAttribute("aria-pressed", String(index === state.series)); button.onclick = () => selectSeries(index); seriesBox.appendChild(button); });
  const seedBox = document.getElementById("seeds");
  seedBox.innerHTML = `<span>seed</span>`;
  const all = document.createElement("button"); all.textContent = "all"; all.setAttribute("aria-pressed", String(state.seed === "all")); all.onclick = () => selectSeed("all"); seedBox.appendChild(all);
  for (const run of currentSeries().runs) { const button = document.createElement("button"); button.textContent = run.seed; button.setAttribute("aria-pressed", String(run.seed === state.seed)); button.onclick = () => selectSeed(run.seed); seedBox.appendChild(button); }
}
document.getElementById("play").onclick = () => { state.playing = !state.playing; document.getElementById("play").textContent = state.playing ? "Pause" : "Play"; };
document.getElementById("reset").onclick = () => { state.t = 0; state.playing = false; document.getElementById("play").textContent = "Play"; render(); };
document.getElementById("scrub").oninput = event => { state.t = Number(event.target.value); state.playing = false; document.getElementById("play").textContent = "Play"; render(); };
const speedBox = document.getElementById("speeds");
speedBox.innerHTML = `<span>playback</span>`;
for (const speed of [1, 2, 4, 8]) { const button = document.createElement("button"); button.textContent = `${speed}×`; button.setAttribute("aria-pressed", String(speed === state.speed)); button.onclick = () => { state.speed = speed; for (const node of speedBox.querySelectorAll("button")) node.setAttribute("aria-pressed", String(node === button)); }; speedBox.appendChild(button); }
function chartTime(event, svg) { const rect = svg.getBoundingClientRect(); const x = (event.clientX - rect.left) / rect.width * 420; return clamp((x - 42) / (420 - 42 - 12) * DATA.duration_s, 0, DATA.duration_s); }
for (const id of ["speed", "fuel"]) { const svg = document.getElementById(id); svg.addEventListener("pointermove", event => { state.t = clamp(chartTime(event, svg), 0, maxTime()); state.playing = false; document.getElementById("play").textContent = "Play"; render(); }); }
window.addEventListener("keydown", event => { if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return; event.preventDefault(); state.t = clamp(state.t + (event.key === "ArrowRight" ? 1 : -1), 0, maxTime()); state.playing = false; document.getElementById("play").textContent = "Play"; render(); });
let last = 0;
function frame(now) { const dt = Math.min(0.1, (now - last) / 1000); last = now; if (state.playing) { state.t += dt * state.speed; if (state.t >= maxTime()) { state.t = maxTime(); state.playing = false; document.getElementById("play").textContent = "Play"; } render(); } requestAnimationFrame(frame); }
buildButtons();
render();
requestAnimationFrame(frame);
</script>
</body>
</html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="fuzz report label and path; repeat for multiple cruise settings",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/analytics/tom-polyfit-dodge/explorer.html"),
    )
    args = parser.parse_args(argv)

    collections: dict[str, list[Run]] = {}
    for item in args.series:
        if "=" not in item:
            raise ValueError("--series must be LABEL=PATH")
        label, path = item.rsplit("=", 1)
        collections[label] = load_series(label, Path(path))

    dataset = build_dataset(collections)
    encoded = json.dumps(dataset, separators=(",", ":"), allow_nan=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(HTML.replace("__DATA__", encoded), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
