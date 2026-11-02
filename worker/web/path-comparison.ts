// Offline flight evidence. Deliberately independent of engine, rooms and runners.
import { finite, MAX_PATH_BYTES, outcomeLabel, pairedRuns, sampleAtTime, segments, selectedCSV,
  timeline, traceStyle, validatePathComparison, type PathComparison, type PathRun } from "./path-comparison-data.ts";

const files = { official: "/analytics/paths-official.json", team: "/analytics/paths-team.json" } as const;
const format = (value: unknown, places = 2) => finite(value)
  ? new Intl.NumberFormat(undefined, { maximumFractionDigits: places }).format(value) : "n/a";
const exact = (value: unknown) => finite(value) ? String(value) : "n/a";
const tick = (value: number) => value !== 0 && (Math.abs(value) < 0.01 || Math.abs(value) >= 100000)
  ? value.toExponential(1) : format(value, 2);
const TEXT = "var(--lab-text)", MUTED = "var(--lab-muted)", GRID = "var(--lab-border)";
const metrics = [
  { column: 4, title: "Target range", unit: "m", note: "Watch progress and stalls. The 50 m line is arrival-region entry, not docking.", threshold: "arrival" },
  { column: 5, title: "Relative speed", unit: "m/s", note: "Compare speed as range closes. Entering the region quickly is not a safe rendezvous." },
  { column: 6, title: "Cumulative local fuel", unit: "kg", note: "A steep section spends fuel faster. Compare completed flights and damage, not fuel alone." },
  { column: 7, title: "Cumulative local damage", unit: "J", note: "A jump brackets an observed damage interval. It does not identify a rock or prove the cause." },
  { column: 8, title: "Requested translation acceleration", unit: "m/s²", note: "Horizontal segments show the actual preceding command interval. Gaps omit unsaved requests. Near the cap, raising a gain may only extend saturation.", threshold: 0.5 },
  { column: 9, title: "Requested angular acceleration", unit: "rad/s²", note: "Horizontal segments show actual command holds, not physical turn rate. A ship can keep spinning after a zero angular request.", threshold: 0.05 },
  { column: 10, title: "Nearest observed radar range", unit: "m", note: "This is a noisy sensor range to an observed body, not hull clearance. Anonymous rolling returns do not track the same rock." },
  { column: 11, title: "Oldest radar row age", unit: "s", note: "Read this with radar range. Negative age indicates a future timestamp, not an unusually fresh measurement." },
  { column: 12, title: "Terrain-centre clearance", unit: "m", note: "Shuttle centre above the sampled terrain radius. It is not full-hull or asteroid clearance." },
] as const;
let instances = 0;
function html<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text?: string) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function svg<K extends keyof SVGElementTagNameMap>(tag: K, attributes: Record<string, string | number> = {}, text?: string) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}
function line(plot: SVGSVGElement, x1: number, y1: number, x2: number, y2: number, attributes = {}) {
  plot.append(svg("line", { x1, y1, x2, y2, stroke: GRID, ...attributes }));
}
function label(plot: SVGSVGElement, x: number, y: number, text: string, attributes = {}) {
  plot.append(svg("text", { x, y, fill: MUTED, "font-size": 15, ...attributes }, text));
}
function table(parent: HTMLElement, caption: string, headings: string[], rows: string[][]) {
  const element = html("table", "path-table");
  element.append(html("caption", "", caption));
  const head = element.createTHead().insertRow();
  for (const title of headings) { const cell = html("th", "", title); cell.scope = "col"; head.append(cell); }
  const body = element.createTBody();
  for (const values of rows) {
    const row = body.insertRow();
    values.forEach((value, i) => { const cell = html(i === 0 ? "th" : "td", "", value);
      if (i === 0) (cell as HTMLTableCellElement).scope = "row"; row.append(cell); });
  }
  parent.replaceChildren(element);
}
function scrollRegion(name: string) {
  const region = html("div", "path-scroll"); region.tabIndex = 0;
  region.setAttribute("role", "region"); region.setAttribute("aria-label", name);
  return region;
}
function bounds(values: number[], zero = false): [number, number] {
  if (!values.length) return [0, 1];
  let low = Math.min(...values), high = Math.max(...values);
  if (zero) { low = Math.min(0, low); high = Math.max(0, high); }
  const pad = low === high ? Math.max(Math.abs(low) * 0.03, 0.01) : (high - low) * 0.06;
  return [low - pad, high + pad];
}
const pathD = (points: Array<[number, number]>, x: (n: number) => number, y: (n: number) => number) => points
  .map(([a, b], i) => `${i ? "L" : "M"}${x(a).toFixed(3)} ${y(b).toFixed(3)}`).join(" ");

async function load(id: keyof typeof files): Promise<PathComparison> {
  const response = await fetch(files[id], { method: "GET", mode: "same-origin", credentials: "omit", redirect: "error",
    referrerPolicy: "no-referrer", cache: "no-cache", signal: AbortSignal.timeout(15000) });
  const size = response.headers.get("content-length");
  if (!response.ok || !/^application\/json(?:;|$)/i.test(response.headers.get("content-type") ?? "")
    || (size !== null && (!/^\d+$/.test(size) || Number(size) > MAX_PATH_BYTES))) throw new Error("Unavailable");
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Unavailable");
  const chunks: Uint8Array[] = []; let bytes = 0;
  while (true) {
    const { done, value } = await reader.read(); if (done) break;
    bytes += value.byteLength;
    if (bytes > MAX_PATH_BYTES) { await reader.cancel(); throw new Error("Unavailable"); }
    chunks.push(value);
  }
  const data = validatePathComparison(JSON.parse(await new Blob(chunks).text()));
  if (!data || data.id !== id) throw new Error("Unavailable");
  return data;
}

export function installPathComparison(container: HTMLElement) {
  const prefix = `path-comparison-${++instances}`;
  container.classList.add("path-comparison");
  const heading = html("h3", "", "Compare paths and timing"); heading.id = `${prefix}-heading`;
  container.setAttribute("aria-labelledby", heading.id);
  const intro = html("p", "path-intro", "Choose one world, seed and repetition. The same numbered trace follows each controller across the maps and time graphs. These are saved flights, not a live replay.");
  const controls = html("div", "path-controls");
  function select(name: string, key: string, options: Array<[string, string]>) {
    const field = html("label", "", name); const input = html("select");
    input.id = input.name = `${prefix}-${key}`; field.htmlFor = input.id;
    input.append(...options.map(([value, text]) => new Option(text, value))); field.append(input); controls.append(field);
    return input;
  }
  const cohort = select("Recorded world", "world", [["official", "Competition hop · official cloud"], ["team", "Historical L2 · directed-stream practice"]]);
  const seed = select("Seed", "seed", []), repetition = select("Repetition", "repetition", []);
  const scale = select("Map scale", "scale", [["detail", "Expand cross-track detail"], ["equal", "Equal metres on both axes"]]);
  seed.disabled = repetition.disabled = true;
  const status = html("p", "path-status", "Open the comparison to load the saved flight measurements."); status.setAttribute("role", "status");
  const retry = html("button", "", "Retry flight measurements"); retry.type = "button"; retry.hidden = true;
  const body = html("div", "path-body"); body.hidden = true;
  const scope = html("p", "path-scope");
  const filters = html("fieldset", "path-profiles"); filters.append(html("legend", "", "Visible controllers"));
  const cards = html("div", "path-cards");
  const cursor = html("div", "path-cursor");
  const cursorLabel = html("label", "", "Inspect simulation time");
  const slider = html("input"); slider.type = "range"; slider.min = "0"; slider.step = "1"; slider.value = "0";
  slider.id = `${prefix}-time`; slider.disabled = true; cursorLabel.htmlFor = slider.id;
  const time = html("output"); time.htmlFor = slider.id; time.id = `${prefix}-time-value`;
  slider.setAttribute("aria-describedby", `${prefix}-cursor-help`);
  const previous = html("button", "", "Previous sample"), next = html("button", "", "Next sample");
  previous.type = next.type = "button";
  cursor.append(cursorLabel, time, slider, previous, next);
  const help = html("p", "path-note", "Move the time control or select a graph to inspect the same saved timestamp everywhere. Arrow keys work on focused graphs. No measurements are interpolated; a run without that timestamp shows n/a. Scroll inside graphs or tables on narrow screens."); help.id = `${prefix}-cursor-help`;
  const readout = scrollRegion("Controller measurements at the selected time, scroll for all columns");
  const maps = html("div", "path-chart-grid"), charts = html("div", "path-chart-grid");
  const componentDetails = html("details", "lab-disclosure");
  const componentCharts = html("div", "path-chart-grid");
  componentDetails.append(html("summary", "", "Inspect signed body-axis requests for reversals"), componentCharts);
  const events = html("div", "path-events");
  const observations = html("div", "path-observations");
  const details = html("details", "lab-disclosure"), provenance = html("div", "path-provenance");
  details.append(html("summary", "", "Recorded data, coordinates and limitations"), provenance);
  const downloads = html("nav", "analytics-downloads"); downloads.setAttribute("aria-label", "Download path comparison evidence");
  const json = html("a", "", "Download this world JSON"); json.download = "";
  const csv = html("a", "", "Download visible paired samples CSV"); csv.download = "paired-flight-samples.csv";
  const trialCSV = html("a", "", "Download trace-derived official trial metrics CSV");
  trialCSV.href = "/analytics/paths-official-trials.csv"; trialCSV.download = "";
  const theory = html("a", "", "Read the Python theory review, opens in a new tab");
  theory.href = "/analytics/python-flight-theory.txt"; theory.target = "_blank"; theory.rel = "noopener noreferrer";
  downloads.append(json, csv, trialCSV, theory);
  const explainers = html("details", "lab-disclosure"); explainers.append(html("summary", "", "How to use these graphs to adjust a path"));
  const explanation = html("div", "path-explainers");
  for (const [title, text] of [
    ["Locate the manoeuvre", "The maps show two projections of the same launch-to-initial-target frame. Along is progress on that fixed line. Lateral and normal are two perpendicular offsets, not altitude or ship-body axes. Select the detour time, then read the speed and requests below. Expanded cross-track detail uses unequal axis scales; it changes the drawing, not the measurements."],
    ["Check braking before gains", "The ideal stopping distance is v²/(2a). At 10 m/s and 0.5 m/s² it is 100 m, before latency, uncertainty, gravity or thrust loss. The constant 10 m/s beeline does not brake for arrival. Ali's local profile lowers its speed reference near the arrival region. A lower arrival time can therefore come with a much higher entry speed."],
    ["Separate request, motion and cost", "A requested acceleration is not the measured force, a burn episode or a safe corridor. Saved command row t describes the interval ending at t. Body-axis components change meaning as attitude changes. Look for repeated request reversals alongside slow progress and fuel growth, then test a gain or steering change on the same seeds."],
    ["Treat damage as an inspection window", "Triangles mark increases between saved damage readings. Radar range is noisy and incomplete, with 200 rolling returns from 500 bodies. It is not closest hull clearance and cannot identify a collider. A coincident path turn and damage increase is a lead to inspect, not proof that the turn caused the collision."],
    ["Keep comparisons paired", "Compare one world, seed and repetition at a time. Check all paired seeds before adopting a change. The historical L2 world is different from the competition hop. Repeating a seed does not create a new independent environment. Software errors stay visible and do not win on short time or low fuel."],
    ["Know which theory is flying", "The PID, fixed-gain filters, association, weighted objective and attitude-bias aids are reference modules. They are not wired into the published Ali flights. Updating a filter for 300 seconds is not a 300-second forecast without new measurements. The STP source port also differs from the local controller and still needs licensed-runtime validation."],
  ]) {
    const section = html("section"); section.append(html("h4", "", title), html("p", "", text)); explanation.append(section);
  }
  explainers.append(explanation);
  body.append(scope, filters, cards, cursor, help, readout, maps, observations, events, charts, componentDetails, explainers, details, downloads);
  container.replaceChildren(heading, intro, controls, status, retry, body);
  const cache = new Map<string, PathComparison>();
  let data: PathComparison | undefined, selected: PathRun[] = [], times: number[] = [], index = 0;
  let request = 0, loaded = false, loading = false, csvURL: string | undefined;
  const hidden = new Set<string>();
  const cursorUpdates: Array<() => void> = [];
  let plotCount = 0;

  function updateCursor(value: number) {
    index = Math.max(0, Math.min(times.length - 1, value));
    const t = times[index]; slider.value = String(index);
    time.textContent = finite(t) ? `${format(t, 3)} s` : "No saved times";
    slider.setAttribute("aria-valuetext", finite(t) ? `${format(t, 3)} simulation seconds` : "No saved times");
    previous.disabled = !times.length || index === 0; next.disabled = !times.length || index === times.length - 1;
    const rows = selected.map(run => {
      const sample = sampleAtTime(run, t);
      const lastTime = run.samples.at(-1)?.[0];
      const state = !sample ? finite(lastTime) && t > lastTime + 1e-7 ? `Ended at ${format(lastTime)} s` : "No saved sample" : "Recorded sample";
      return [`${traceStyle(run.algorithm).number}. ${run.label}`, state, sample ? exact(sample[0]) : "n/a",
        ...[1, 2, 3, 4, 5, 6, 7, 8].map(column => exact(sample?.[column]))];
    });
    table(readout, "Exact supplied values at the time control. A blank timestamp is not filled with a nearby sample.",
      ["Controller", "Sample state", "Sample time, s", "Along, m", "Lateral, m", "Normal, m", "Range, m", "Relative speed, m/s", "Fuel, kg", "Damage, J", "Request, m/s²"], rows);
    for (const update of cursorUpdates) update();
  }
  function selectTime(t: number) {
    if (!times.length) return;
    let closest = 0; for (let i = 1; i < times.length; i++) if (Math.abs(times[i] - t) < Math.abs(times[closest] - t)) closest = i;
    updateCursor(closest);
  }
  slider.addEventListener("input", () => updateCursor(Number(slider.value)));
  previous.addEventListener("click", () => updateCursor(index - 1)); next.addEventListener("click", () => updateCursor(index + 1));

  function figure(title: string, note: string, height = 280) {
    const element = html("figure", "path-chart"); const name = `${prefix}-plot-${++plotCount}`;
    const header = html("h4", "", title);
    const region = scrollRegion(`${title}. Scroll horizontally, or focus the graph and use arrow keys to inspect time.`);
    const plot = svg("svg", { viewBox: `0 0 720 ${height}`, role: "img", tabindex: "0", class: "path-plot",
      "aria-labelledby": `${name}-title`, "aria-describedby": `${name}-description` });
    plot.append(svg("title", { id: `${name}-title` }, title), svg("desc", { id: `${name}-description` }, `${note} Trace numbers match the controller legend. Full numeric values are available in the paired samples download.`));
    const caption = html("figcaption", "", note); element.append(header, region, caption); region.append(plot);
    plot.addEventListener("keydown", event => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.key === "ArrowRight" || event.key === "ArrowLeft" || event.key === "Home" || event.key === "End") {
        event.preventDefault(); event.stopPropagation();
        updateCursor(event.key === "Home" ? 0 : event.key === "End" ? times.length - 1 : index + (event.key === "ArrowRight" ? 1 : -1));
      }
    });
    return { element, plot };
  }
  function clickPlot(plot: SVGSVGElement, points: Array<{ x: number; y: number; time: number }>) {
    plot.addEventListener("click", event => {
      const matrix = plot.getScreenCTM(); if (!matrix || !points.length) return;
      const p = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
      let best = points[0], distance = Infinity;
      for (const point of points) { const d = Math.hypot(point.x - p.x, point.y - p.y); if (d < distance) { distance = d; best = point; } }
      selectTime(best.time);
    });
  }
  function drawTrace(plot: SVGSVGElement, run: PathRun, xColumn: number, yColumn: number, x: (n: number) => number, y: (n: number) => number) {
    const style = traceStyle(run.algorithm);
    const command = xColumn === 0 && [8, 9, 13, 14, 15].includes(yColumn);
    const lines = command ? run.samples.filter(row => finite(row[16]) && finite(row[yColumn]))
      .map(row => [[row[16]!, row[yColumn]!], [row[0]!, row[yColumn]!]] as Array<[number, number]>)
      : segments(run.samples, xColumn, yColumn);
    const trace = lines.map(part => pathD(part, x, y)).join(" ");
    if (trace) plot.append(svg("path", {
      d: trace, fill: "none", stroke: style.color, "stroke-width": 2.5, "stroke-dasharray": style.dash,
      "vector-effect": "non-scaling-stroke", "data-trace": run.id,
    }));
    const final = run.samples.at(-1);
    if (final && finite(final[xColumn]) && finite(final[yColumn])) {
      const cx = x(final[xColumn]!), cy = y(final[yColumn]!);
      plot.append(svg(run.outcome === "success" ? "circle" : "rect", run.outcome === "success"
        ? { cx, cy, r: 4, fill: style.color } : { x: cx - 4, y: cy - 4, width: 8, height: 8, fill: "var(--lab-surface)", stroke: style.color, "stroke-width": 2 }));
      label(plot, cx + 7, cy - 7, String(style.number), { fill: style.color, "font-weight": 700, "paint-order": "stroke", stroke: "var(--lab-surface)", "stroke-width": 3 });
    }
    for (const event of run.events.filter(event => event.kind === "damage")) {
      const row = sampleAtTime(run, event.by_s);
      if (!row || !finite(row[xColumn]) || !finite(row[yColumn])) continue;
      const cx = x(row[xColumn]!), cy = y(row[yColumn]!);
      plot.append(svg("path", { d: `M${cx} ${cy - 6}l-5 10h10Z`, fill: "var(--lab-surface)", stroke: style.color, "stroke-width": 2 }));
    }
    const marker = svg("circle", { r: 6, fill: "var(--lab-surface)", stroke: style.color, "stroke-width": 3, visibility: "hidden" }); plot.append(marker);
    cursorUpdates.push(() => {
      const row = sampleAtTime(run, times[index]); const show = row && finite(row[xColumn]) && finite(row[yColumn]);
      marker.setAttribute("visibility", show ? "visible" : "hidden");
      if (show) { marker.setAttribute("cx", String(x(row![xColumn]!))); marker.setAttribute("cy", String(y(row![yColumn]!))); }
    });
  }
  function map(column: 2 | 3) {
    const name = column === 2 ? "Lateral" : "Normal";
    const equal = scale.value === "equal";
    const note = `${name} offset against fixed along-route progress, in metres. ${equal ? "Equal distance scale on both axes." : "Cross-track detail: vertical scale is expanded. Slopes and angles are distorted."} Circle = arrival endpoint, square = other endpoint, triangle = damage increase. Hollow ring = selected time.`;
    const { element, plot } = figure(`${name} path overlay`, note, 310);
    const rows = selected.flatMap(run => run.samples);
    let [xmin, xmax] = bounds([0, data!.comparison.route_length_m, ...rows.map(row => row[1]).filter(finite)]);
    let [ymin, ymax] = bounds([0, ...rows.map(row => row[column]).filter(finite)]);
    if (equal) {
      const unit = Math.max((xmax - xmin) / 590, (ymax - ymin) / 216);
      const midx = (xmin + xmax) / 2, midy = (ymin + ymax) / 2;
      [xmin, xmax] = [midx - unit * 295, midx + unit * 295]; [ymin, ymax] = [midy - unit * 108, midy + unit * 108];
    }
    const x = (n: number) => 96 + (n - xmin) / (xmax - xmin) * 590;
    const y = (n: number) => 252 - (n - ymin) / (ymax - ymin) * 216;
    for (let i = 0; i <= 4; i++) {
      const a = xmin + (xmax - xmin) * i / 4, b = ymin + (ymax - ymin) * i / 4;
      line(plot, 96, y(b), 686, y(b)); label(plot, 84, y(b) + 5, tick(b), { "text-anchor": "end" });
      label(plot, x(a), 275, tick(a), { "text-anchor": "middle" });
    }
    line(plot, x(0), y(0), x(data!.comparison.route_length_m), y(0), { stroke: MUTED, "stroke-dasharray": "2 5" });
    label(plot, 96, 22, `${name} offset, m`); label(plot, 390, 301, "Along launch-to-initial-target line, m", { "text-anchor": "middle" });
    plot.append(svg("circle", { cx: x(0), cy: y(0), r: 4, fill: TEXT }));
    const goal = x(data!.comparison.route_length_m);
    line(plot, goal, 36, goal, 252, { stroke: MUTED, "stroke-dasharray": "4 4" });
    label(plot, goal - 4, 49, "Initial target", { "text-anchor": "end" });
    for (const run of selected) drawTrace(plot, run, 1, column, x, y);
    clickPlot(plot, rows.filter(row => finite(row[1]) && finite(row[column])).map(row => ({ x: x(row[1]!), y: y(row[column]!), time: row[0]! })));
    maps.append(element);
  }
  function metricChart(metric: { column: number; title: string; unit: string; note: string; threshold?: string | number }, destination = charts) {
    const { element, plot } = figure(`${metric.title} over time`, metric.note);
    const rows = selected.flatMap(run => run.samples);
    const tmax = Math.max(1, times.at(-1) ?? 0);
    const threshold = metric.threshold === "arrival" ? data!.comparison.arrival_radius_m : metric.threshold;
    const values = rows.map(row => row[metric.column]).filter(finite);
    let [low, high] = bounds([...values, ...(finite(threshold) ? [threshold] : [])], true);
    if ([4, 5, 6, 7, 8, 9].includes(metric.column)) low = 0;
    const x = (n: number) => 96 + n / tmax * 590, y = (n: number) => 224 - (n - low) / (high - low) * 188;
    for (let i = 0; i <= 4; i++) {
      const v = low + (high - low) * i / 4;
      line(plot, 96, y(v), 686, y(v)); label(plot, 84, y(v) + 5, tick(v), { "text-anchor": "end" });
      label(plot, x(tmax * i / 4), 247, format(tmax * i / 4, 1), { "text-anchor": "middle" });
    }
    label(plot, 96, 22, `${metric.title}, ${metric.unit}`); label(plot, 390, 272, "Simulation time, s", { "text-anchor": "middle" });
    if (finite(threshold)) {
      line(plot, 96, y(threshold), 686, y(threshold), { stroke: MUTED, "stroke-dasharray": "4 4" });
      label(plot, 681, y(threshold) - 6, `${format(threshold)} ${metric.unit} ${metric.threshold === "arrival" ? "arrival region" : "request cap"}`, { "text-anchor": "end" });
    }
    for (const run of selected) drawTrace(plot, run, 0, metric.column, x, y);
    const cursorLine = svg("line", { y1: 36, y2: 224, stroke: TEXT, "stroke-width": 1, "stroke-dasharray": "2 3" }); plot.append(cursorLine);
    cursorUpdates.push(() => { const position = String(x(times[index] ?? 0)); cursorLine.setAttribute("x1", position); cursorLine.setAttribute("x2", position); });
    if (!values.length) label(plot, 390, 120, "No saved values for this metric", { "text-anchor": "middle", fill: TEXT });
    // Time graphs choose the nearest actual saved time, regardless of y value.
    plot.addEventListener("click", event => { const matrix = plot.getScreenCTM(); if (!matrix) return;
      const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse()); selectTime((point.x - 96) / 590 * tmax); });
    destination.append(element);
  }
  function summary() {
    observations.replaceChildren(html("h4", "", "What this paired selection shows"));
    const arrivals = selected.filter(run => run.outcome === "success" && finite(run.final.time_s));
    if (arrivals.length > 1) {
      const fastest = arrivals.reduce((a, b) => a.final.time_s! <= b.final.time_s! ? a : b);
      const fueled = arrivals.filter(run => finite(run.final.fuel_kg));
      const leastFuel = fueled.length ? fueled.reduce((a, b) => a.final.fuel_kg! <= b.final.fuel_kg! ? a : b) : null;
      observations.append(html("p", "", `${fastest.label} has the shortest recorded arrival here, ${format(fastest.final.time_s)} s, with ${format(fastest.final.relative_speed_mps)} m/s entry speed and ${format(fastest.final.damage_j)} J damage. ${leastFuel ? `${leastFuel.label} used the least local fuel among arrivals with a fuel reading, ${format(leastFuel.final.fuel_kg)} kg.` : "Fuel readings are unavailable."} This is one matched pair, not a cohort ranking.`));
    } else observations.append(html("p", "", `${arrivals.length} recorded arrival in this selection. Non-arrivals remain visible but are not ranked as efficient flights.`));
    const list = html("ul");
    for (const run of selected) {
      const positions = run.samples.filter(row => finite(row[2]) && finite(row[3]));
      const maximum = positions.length ? positions.reduce((a, b) => Math.hypot(a[2]!, a[3]!) >= Math.hypot(b[2]!, b[3]!) ? a : b) : null;
      list.append(html("li", "", `${traceStyle(run.algorithm).number}. ${run.label}: ${outcomeLabel(run.outcome)} at ${format(run.final.time_s)} s. ${maximum ? `Largest sampled cross-track offset ${format(Math.hypot(maximum[2]!, maximum[3]!))} m at ${format(maximum[0])} s.` : "Cross-track samples unavailable."}`));
    }
    observations.append(list);
  }
  function eventList() {
    events.replaceChildren(html("h4", "", "Inspect damage and endpoints")); const list = html("ul");
    for (const run of selected) {
      for (const event of run.events) {
        const item = html("li"); const button = html("button"); button.type = "button";
        button.textContent = `${traceStyle(run.algorithm).number}. ${run.label} · ${event.kind === "damage"
          ? `damage ${event.after_s === null ? "first seen by" : `after ${format(event.after_s)} s, by`} ${format(event.by_s)} s, ${format(event.value)} J`
          : `${outcomeLabel(run.outcome)} at ${format(event.by_s)} s`}`;
        button.addEventListener("click", () => selectTime(event.by_s)); item.append(button); list.append(item);
      }
    }
    if (!list.children.length) list.append(html("li", "", "No saved events in the visible traces."));
    events.append(html("p", "path-note", "Select an event to move every graph and the maps to its saved timestamp. Damage is a sampled interval, not an exact contact time."), list);
  }
  function render() {
    if (!data) return;
    const pair = pairedRuns(data, Number(seed.value), Number(repetition.value));
    selected = pair.filter(run => !hidden.has(run.algorithm));
    const oldTime = times[index] ?? 0; times = timeline(selected); index = 0;
    slider.max = String(Math.max(0, times.length - 1)); slider.disabled = times.length === 0;
    cursorUpdates.length = 0; maps.replaceChildren(); charts.replaceChildren(); componentCharts.replaceChildren(); cards.replaceChildren();
    scope.textContent = `${data.label} · ${data.comparison.debris_count} bodies · seed ${seed.value} · repetition ${repetition.value} · ${selected.length}/${pair.length} controllers visible. One line per actual flight. No averaging.`;
    for (const run of selected) {
      const card = html("article", "path-card"); const style = traceStyle(run.algorithm); card.style.setProperty("--trace-color", style.color);
      card.append(html("h4", "", `${style.number}. ${run.label}`), html("p", "", outcomeLabel(run.outcome)),
        html("p", "path-card-values", `${format(run.final.time_s)} s · ${format(run.final.fuel_kg)} kg · ${format(run.final.damage_j)} J`),
        html("p", "path-note", `${format(run.final.relative_speed_mps)} m/s final speed · ${run.samples.length}/${run.source_sample_count} retained samples`)); cards.append(card);
    }
    if (selected.length) {
      map(2); map(3); for (const metric of metrics) metricChart(metric);
      for (const [i, axis] of ["X", "Y", "Z"].entries()) metricChart({ column: 13 + i, title: `Body ${axis} acceleration request`, unit: "m/s²",
        note: "Signed component held over its recorded preceding interval. Gaps omit unsaved requests. Compare reversals with progress and fuel. Body axes rotate with the ship; these are not fixed map directions." }, componentCharts);
    }
    summary(); eventList();
    const info = data.comparison;
    provenance.replaceChildren(html("p", "", `Engine SHA-256: ${info.engine_sha256}`),
      html("p", "", `Frame: ${info.frame}. Route length ${exact(info.route_length_m)} m. Physics world ${info.preset}; debris ${info.distribution}; controller step ${exact(info.step_s)} s; time limit ${exact(info.duration_s)} s.`),
      html("p", "", "Ownship positions use a fixed launch-to-initial-target basis. Normal is along-axis cross lateral-axis. Body command components are not these map axes. Source run and trace SHA-256 values are in the JSON download. No raw radar returns, images, hidden debris states or private connection data are published."));
    const limitations = html("ul"); for (const note of info.limitations) limitations.append(html("li", "", note)); provenance.append(limitations);
    json.href = files[data.id];
    trialCSV.hidden = data.id !== "official";
    if (csvURL) URL.revokeObjectURL(csvURL);
    csvURL = URL.createObjectURL(new Blob([selectedCSV(selected)], { type: "text/csv;charset=utf-8" })); csv.href = csvURL;
    csv.hidden = !selected.length;
    if (times.length) selectTime(oldTime); else updateCursor(0);
  }
  async function changeWorld() {
    const token = ++request, id = cohort.value as keyof typeof files;
    loading = true;
    status.textContent = "Loading saved paired flights. This does not start a flight."; retry.hidden = true; body.hidden = true;
    seed.disabled = repetition.disabled = true;
    try {
      const next = cache.get(id) ?? await load(id); if (token !== request) return;
      cache.set(id, next); data = next; loaded = true; hidden.clear();
      seed.replaceChildren(...next.comparison.seeds.map(n => new Option(String(n), String(n))));
      repetition.replaceChildren(...Array.from({ length: next.comparison.repetitions }, (_, i) => new Option(String(i + 1), String(i + 1))));
      filters.replaceChildren(html("legend", "", "Visible controllers · line pattern and number stay fixed across graphs"));
      for (const run of pairedRuns(next, next.comparison.seeds[0], 1)) {
        const style = traceStyle(run.algorithm); const field = html("label"); const check = html("input"); check.type = "checkbox"; check.checked = true;
        const swatch = svg("svg", { viewBox: "0 0 32 10", "aria-hidden": "true", class: "path-swatch" });
        swatch.append(svg("line", { x1: 0, x2: 32, y1: 5, y2: 5, stroke: style.color, "stroke-width": 3, "stroke-dasharray": style.dash }));
        field.append(check, swatch, document.createTextNode(`${style.number}. ${run.label}`)); filters.append(field);
        check.addEventListener("change", () => { if (check.checked) hidden.delete(run.algorithm); else hidden.add(run.algorithm); render(); });
      }
      times = []; seed.disabled = repetition.disabled = false; body.hidden = false;
      status.textContent = `${next.runs.length} saved trials loaded. Local measurements, not STP points. L2 and competition-hop trials are never pooled.`;
      render();
    } catch {
      if (token !== request) return;
      status.textContent = "Flight measurements could not be loaded or validated. Check your connection and choose Retry flight measurements. No flight was started.";
      retry.hidden = false;
    } finally { if (token === request) loading = false; }
  }
  cohort.addEventListener("change", () => { void changeWorld(); }); retry.addEventListener("click", () => { void changeWorld(); });
  seed.addEventListener("change", render); repetition.addEventListener("change", render); scale.addEventListener("change", render);
  addEventListener("pagehide", () => { if (csvURL) URL.revokeObjectURL(csvURL); }, { once: true });
  return { open() { if (!loaded && !loading) void changeWorld(); } };
}
