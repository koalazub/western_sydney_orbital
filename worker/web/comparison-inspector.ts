// Read-only linked views of retained observations. No interpolation or command integration.
import { alignmentTime, baselineRun, formatMetric, getMetric, inspectionAnchor, inspectionRuns, intervalStats, metricSeries,
  type WorkspaceActions, type WorkspaceState, type WorkspaceWidget } from "./comparison-model.ts";
import { finite, outcomeLabel, pairedRuns, sampleAtTime, segments, timeline, traceStyle,
  type PathRun } from "./path-comparison-data.ts";

type Track = { id: string; label: string; unit: string; note: string; command?: boolean; pointsOnly?: boolean; native?: boolean };
type Reading = [number, number | null];
type SeriesRow = { time: number; value: number | null; from: number | null };
type Series = { run: PathRun; origin: number; rows: SeriesRow[] };
type HitPoint = { x: number; y: number; time: number; run: PathRun };
type Chart = { key: string; kind: "route" | "time"; element: HTMLElement; heading: HTMLElement; badge: HTMLElement;
  plot: SVGSVGElement; caption: HTMLElement; titleId: string; descriptionId: string; clipId: string;
  points: HitPoint[]; overlay: SVGGElement; brush: SVGRectElement; fromX: (x: number) => number;
  x: (value: number) => number; y: (value: number) => number; domain: [number, number];
  series: Series[]; column?: 2 | 3; track?: Track; drag: { id: number; x: number; y: number; lastX: number; lastY: number } | null };

const EPSILON = 1e-7;
const MAX_TRACKS = 6;
const LEFT = 88, RIGHT = 696, TOP = 34, BOTTOM = 208, WIDTH = RIGHT - LEFT;
const TEXT = "var(--lab-text)", MUTED = "var(--lab-muted)", GRID = "var(--lab-border)", SURFACE = "var(--lab-surface)";
const tracks: Track[] = [
  { id: "time_s", label: "Recorded time", unit: "s", note: "Actual simulation timestamps", pointsOnly: true },
  { id: "along_m", label: "Along route", unit: "m", note: "Fixed launch-to-initial-target frame" },
  { id: "lateral_m", label: "Lateral offset", unit: "m", note: "Fixed route frame, not body axes" },
  { id: "normal_m", label: "Normal offset", unit: "m", note: "Fixed route frame, not altitude" },
  { id: "range_m", label: "Target range", unit: "m", note: "Recorded target range" },
  { id: "relative_speed_mps", label: "Relative speed", unit: "m/s", note: "Recorded relative speed" },
  { id: "fuel_kg", label: "Cumulative fuel", unit: "kg", note: "Local cumulative fuel used, not a rate" },
  { id: "damage_j", label: "Cumulative damage", unit: "J", note: "Local cumulative damage" },
  { id: "accel_mps2", label: "Translation request", unit: "m/s²", note: "Recorded command windows only", command: true },
  { id: "angular_accel_rad_s2", label: "Angular request", unit: "rad/s²", note: "Requested actuation, not physical turning", command: true },
  { id: "nearest_radar_m", label: "Radar range", unit: "m", note: "Noisy observed range, not hull clearance" },
  { id: "max_radar_age_s", label: "Oldest radar age", unit: "s", note: "Negative values are future timestamps" },
  { id: "terrain_clearance_m", label: "Terrain clearance", unit: "m", note: "Sampled centre clearance, not hull clearance" },
  { id: "ax_mps2", label: "Body X request", unit: "m/s²", note: "Signed body-axis command windows", command: true },
  { id: "ay_mps2", label: "Body Y request", unit: "m/s²", note: "Signed body-axis command windows", command: true },
  { id: "az_mps2", label: "Body Z request", unit: "m/s²", note: "Signed body-axis command windows", command: true },
  { id: "command_from_s", label: "Command start", unit: "s", note: "Start of the recorded command window", pointsOnly: true },
  { id: "burn_duration_s", label: "Cumulative burn time", unit: "s", note: "Native translation engine-on counter", native: true },
  { id: "angular_burn_duration_s", label: "Cumulative angular burn", unit: "s", note: "Native angular engine-on counter", native: true },
  { id: "turn_duration_s", label: "Cumulative turn time", unit: "s", note: "Native physical rotation counter", native: true },
  { id: "total_turn_angle_rad", label: "Cumulative turn angle", unit: "rad", note: "Native orientation travelled, not heading error", native: true },
];
const byTrackId = new Map(tracks.map(track => [track.id, track]));
let instances = 0;

function extent(values: number[], zero = false): [number, number] {
  let low = zero ? 0 : Infinity, high = zero ? 0 : -Infinity;
  for (const value of values) { if (finite(value)) { low = Math.min(low, value); high = Math.max(high, value); } }
  if (!finite(low) || !finite(high)) return [0, 1];
  const pad = low === high ? Math.max(Math.abs(low) * 0.04, 0.01) : (high - low) * 0.05;
  return [low - pad, high + pad];
}
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));
const sameTime = (a: number, b: number) => Math.abs(a - b) <= EPSILON;
const number = (value: number | null, id = "time_s", signed = false) => formatMetric(value, getMetric(id) ?? id, signed);
const tick = (value: number, signed = false) => number(value, "", signed);
function nearestIndex(times: number[], value: number): number {
  if (!times.length) return -1;
  let low = 0, high = times.length;
  while (low < high) { const middle = Math.floor((low + high) / 2); if (times[middle] < value) low = middle + 1; else high = middle; }
  if (low === 0) return 0;
  if (low === times.length) return times.length - 1;
  return value - times[low - 1] <= times[low] - value ? low - 1 : low;
}
function readingAt(readings: Reading[], time: number): Reading | null {
  let low = 0, high = readings.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2), reading = readings[middle];
    if (sameTime(reading[0], time)) return reading;
    if (reading[0] < time) low = middle + 1; else high = middle;
  }
  return null;
}
function pathData(points: Array<[number, number]>, x: (n: number) => number, y: (n: number) => number): string {
  return points.map(([a, b], index) => `${index ? "L" : "M"}${x(a).toFixed(3)} ${y(b).toFixed(3)}`).join(" ");
}

export function installIntervalInspector(container: HTMLElement, actions: WorkspaceActions): WorkspaceWidget {
  const doc = container.ownerDocument, prefix = `comparison-inspector-${++instances}`;
  const events = new AbortController(), charts = new Map<string, Chart>();
  const selectedTracks = new Set(["range_m", "relative_speed_mps", "fuel_kg"]);
  const rawSeries = new Map<string, Reading[]>();
  const legendItems = new Map<string, { run: PathRun; item: HTMLLIElement; toggle: HTMLButtonElement; note: HTMLElement }>();
  const inputTimes = new Map<HTMLInputElement, { text: string; actual: number }>();
  const originalLabel = container.getAttribute("aria-labelledby"), addedClass = !container.classList.contains("comparison-inspector");
  let state: WorkspaceState | undefined, previousState: WorkspaceState | undefined;
  let runs: PathRun[] = [], aligned: PathRun[] = [], legendRuns: PathRun[] = [], anchor: PathRun | undefined, anchorOrigin: number | null = null;
  let times: number[] = [], origins = new Map<string, number>(), activeCharts: Chart[] = [];
  let timeDomain: [number, number] = [0, 1], cursorBounds: [number, number] = [0, 1];
  let structureKey = "", intervalKey = "", eventTableDirty = true, disposed = false;

  function html<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text?: string): HTMLElementTagNameMap[K] {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function svg<K extends keyof SVGElementTagNameMap>(tag: K, attributes: Record<string, string | number> = {}, text?: string): SVGElementTagNameMap[K] {
    const element = doc.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function line(parent: SVGElement, x1: number, y1: number, x2: number, y2: number, attributes: Record<string, string | number> = {}) {
    parent.append(svg("line", { x1, y1, x2, y2, stroke: GRID, "vector-effect": "non-scaling-stroke", ...attributes }));
  }
  function label(parent: SVGElement, x: number, y: number, text: string, attributes: Record<string, string | number> = {}) {
    parent.append(svg("text", { x, y, fill: MUTED, "font-size": 14, ...attributes }, text));
  }
  function button(text: string) { const element = html("button", "", text); element.type = "button"; return element; }
  function field<T extends HTMLInputElement | HTMLSelectElement>(parent: HTMLElement, text: string, key: string, input: T): T {
    const wrapper = html("label", "", text); input.id = input.name = `${prefix}-${key}`;
    wrapper.htmlFor = input.id; wrapper.append(input); parent.append(wrapper); return input;
  }
  function scrollRegion(className: string, name: string) {
    const region = html("div", className); region.tabIndex = 0;
    region.setAttribute("role", "region"); region.setAttribute("aria-label", name);
    region.style.minWidth = "0"; region.style.maxWidth = "100%"; region.style.overflowX = "auto";
    return region;
  }
  function table(parent: HTMLElement, caption: string, headings: string[], rows: Array<Array<string | number | null>>, exact = false) {
    const element = html("table"); element.append(html("caption", "comparison-muted", caption));
    const head = element.createTHead().insertRow();
    for (const text of headings) { const cell = html("th", "", text); cell.scope = "col"; head.append(cell); }
    const body = element.createTBody();
    for (const values of rows) {
      const row = body.insertRow();
      values.forEach((value, index) => {
        const cell = html(index === 0 ? "th" : "td", "", typeof value === "number" ? exact ? String(value) : number(value) : value ?? "n/a");
        if (index === 0) cell.scope = "row";
        if (typeof value === "number") cell.title = String(value);
        row.append(cell);
      });
    }
    // Keep the scroll region, and therefore its focus and scroll position, in place.
    parent.replaceChildren(element);
  }

  container.classList.add("comparison-inspector");
  const header = html("div", "comparison-section-header"), heading = html("h3", "", "Flight path");
  heading.id = `${prefix}-heading`; heading.tabIndex = -1; container.setAttribute("aria-labelledby", heading.id);
  const chartTools = html("div", "comparison-toolbar comparison-chart-tools");
  const equal = html("input"); equal.type = "checkbox";
  field(chartTools, "Equal metres", "equal-metres", equal); header.append(heading, chartTools);
  const pathLegend = html("ol", "comparison-path-legend"); pathLegend.setAttribute("aria-label", "Inspected flights");
  const maps = html("div", "comparison-chart-grid");
  const status = html("p", "comparison-muted"); status.setAttribute("role", "status"); status.setAttribute("aria-atomic", "true");
  const empty = html("p", "comparison-empty"); empty.hidden = true;
  const inspection = html("details", "comparison-time-inspection"), inspectionSummary = html("summary", "", "Inspect time");
  const toolbar = html("div", "comparison-toolbar");
  const alignment = field(toolbar, "Align by", "alignment", html("select"));
  alignment.append(new Option("Time", "time"), new Option("First damage", "damage"), new Option("Arrival", "arrival"));
  const picker = html("details", "comparison-track-picker"), trackCount = html("span", "comparison-muted");
  const pickerSummary = html("summary", "", "Choose tracks"); pickerSummary.append(doc.createTextNode(" "), trackCount);
  const trackFields = html("fieldset"); trackFields.append(html("legend", "", `Timeline tracks, up to ${MAX_TRACKS}`));
  const trackInputs = new Map<string, { input: HTMLInputElement; label: HTMLLabelElement; note: HTMLElement }>();
  for (const track of tracks) {
    const wrapper = html("label"), check = html("input"), note = html("span", "comparison-muted"); check.type = "checkbox";
    check.id = check.name = `${prefix}-track-${track.id}`; check.value = track.id; wrapper.htmlFor = check.id;
    wrapper.title = track.note;
    wrapper.append(check, doc.createTextNode(`${track.label}, ${track.unit}${track.native ? " · native" : ""}`), note);
    trackFields.append(wrapper); trackInputs.set(track.id, { input: check, label: wrapper, note });
    check.addEventListener("change", () => {
      if (check.checked && selectedTracks.size < MAX_TRACKS) selectedTracks.add(track.id);
      else { selectedTracks.delete(track.id); check.checked = false; }
      if (state) render(state, true);
    }, { signal: events.signal });
  }
  picker.append(pickerSummary, trackFields);
  const flightPicker = html("details", "comparison-flight-picker"), flightPickerSummary = html("summary", "", "Flights");
  const legend = html("ol", "comparison-toolbar"); legend.setAttribute("aria-label", "Select paired flights");
  flightPicker.append(flightPickerSummary, legend);

  const cursorControls = html("div", "comparison-toolbar");
  const cursorNumber = html("input"); cursorNumber.type = "number"; cursorNumber.step = "any"; cursorNumber.inputMode = "decimal";
  field(cursorControls, "Cursor, s", "cursor-number", cursorNumber);
  const cursorRange = html("input"); cursorRange.type = "range"; cursorRange.min = "0"; cursorRange.step = "1";
  field(cursorControls, "Recorded sample", "cursor-range", cursorRange);
  const previous = button("Previous"), next = button("Next"), cursorOutput = html("output", "comparison-muted");
  previous.setAttribute("aria-label", "Previous recorded sample"); next.setAttribute("aria-label", "Next recorded sample");
  cursorOutput.htmlFor = `${cursorNumber.id} ${cursorRange.id}`;
  cursorOutput.setAttribute("aria-live", "polite"); cursorOutput.setAttribute("aria-atomic", "true");
  cursorControls.append(previous, next, cursorOutput);

  const intervalDisclosure = html("details", "comparison-interval-disclosure"), intervalDisclosureSummary = html("summary", "", "Measure interval");
  const intervalControls = html("form", "comparison-interval-controls");
  const intervalAnchor = html("p", "comparison-muted"); intervalAnchor.id = `${prefix}-interval-anchor`;
  intervalAnchor.style.flexBasis = "100%"; intervalControls.append(intervalAnchor);
  const from = html("input"), to = html("input");
  for (const input of [from, to]) { input.type = "number"; input.step = "any"; input.inputMode = "decimal"; input.required = true; }
  field(intervalControls, "From, s", "interval-from", from); field(intervalControls, "To, s", "interval-to", to);
  const apply = button("Apply interval"), clear = button("Clear interval"); apply.type = "submit";
  intervalControls.append(apply, clear);
  const intervalSummary = scrollRegion("comparison-interval-stats comparison-table-scroll", "Interval endpoint totals, scroll for all columns");
  intervalSummary.hidden = true; intervalDisclosure.append(intervalDisclosureSummary, intervalControls, intervalSummary);
  const timelines = html("div", "comparison-chart-grid");
  const interactionHelp = html("p", "comparison-muted", "Touch gestures scroll each chart. From and To set an interval without dragging.");
  interactionHelp.id = `${prefix}-interaction-help`;
  const readoutDetails = html("details", "comparison-cursor-readout");
  readoutDetails.append(html("summary", "", "Exact cursor values"));
  const readout = scrollRegion("comparison-table-scroll", "Original recorded values at the cursor, scroll for all columns");
  readoutDetails.append(readout);
  const help = html("details", "comparison-inline-help"); help.append(html("summary", "", "Inspection help"));
  const alignmentHelp = html("p"); alignmentHelp.id = `${prefix}-alignment-help`;
  help.append(interactionHelp,
    html("p", "", "Along, lateral and normal are offsets in the fixed launch-to-initial-target frame, not body axes."),
    html("p", "", "Flight buttons toggle the linked selection. The baseline stays plotted while any flight is selected. Selecting a point on a selected flight moves only the cursor; selecting the pinned baseline adds it without replacing the other flights."),
    html("p", "", "Graph arrow keys move between retained sample times; Home and End choose the first and last. Focus the scroll region to pan with arrow keys. Mouse and pen timeline drags select the same interval as From and To; touch gestures pan the graph."),
    html("p", "", "Endpoints snap inward to retained readings; cumulative endpoint deltas survive thinning, but gaps do not establish continuous commands or exact reversal counts."),
    html("p", "", "Endpoint coverage is the recorded endpoint span divided by the requested duration, not continuous sample coverage. A single endpoint has 0% coverage and no fuel, damage or progress delta."),
    html("p", "", "Damage brackets retain after_s and by_s without inventing a start when after_s is missing. Triangles mark by_s where a metric value is available. Circles mark arrived endpoints; squares mark other recorded ends. Vertical end-time annotations remain visible without a metric value."),
    html("p", "", "Retained observations use local cumulative counters. No interpolation or command integration is applied. Difference traces require actual readings at matching timestamps or event offsets; either run's missing readings break the trace."),
    html("p", "", "Time inputs round to milliseconds for display. Unedited inputs keep their actual times. Exact cursor values and the sample CSV retain the original precision."));
  const eventDetails = html("details", "comparison-inline-help"), eventTable = scrollRegion("comparison-table-scroll", "Original recorded damage brackets and terminal times");
  eventDetails.append(html("summary", "", "Recorded events"), eventTable);
  cursorNumber.setAttribute("aria-describedby", alignmentHelp.id); cursorRange.setAttribute("aria-describedby", alignmentHelp.id);
  from.setAttribute("aria-describedby", `${intervalAnchor.id} ${alignmentHelp.id}`); to.setAttribute("aria-describedby", `${intervalAnchor.id} ${alignmentHelp.id}`);
  inspection.append(inspectionSummary, cursorControls, toolbar, alignmentHelp, intervalDisclosure,
    flightPicker, picker, timelines, readoutDetails, eventDetails, help);
  container.replaceChildren(header, pathLegend, maps, status, empty, inspection);

  function seriesFor(run: PathRun, track: Track): Reading[] {
    const key = `${run.id}:${track.id}`;
    const cached = rawSeries.get(key); if (cached) return cached;
    const readings: Reading[] = track.id === "command_from_s" ? run.samples.map(row => [row[0]!, row[16]])
      : metricSeries(run, track.id, state?.supplement);
    rawSeries.set(key, readings); return readings;
  }
  function activeTracks(): Track[] { return tracks.filter(track => selectedTracks.has(track.id)); }
  function runName(run: PathRun): string { return `${traceStyle(run.algorithm).number}. ${run.label}`; }
  const inputTimeText = (value: number) => String(Number(value.toFixed(3)));
  function setInputTime(input: HTMLInputElement, actual: number) {
    const text = inputTimeText(actual);
    input.value = text; input.title = `Actual time ${String(actual)} s`;
    inputTimes.set(input, { text, actual });
  }
  function inputTime(input: HTMLInputElement): number {
    const saved = inputTimes.get(input);
    return saved?.text === input.value ? saved.actual : input.valueAsNumber;
  }
  function editedTime(input: HTMLInputElement) { inputTimes.delete(input); input.removeAttribute("title"); }
  function validateTime(input: HTMLInputElement, missing: string) {
    const value = inputTime(input);
    input.setCustomValidity(!finite(value) ? missing : value < cursorBounds[0] || value > cursorBounds[1]
      ? `Enter a time between ${String(cursorBounds[0])} and ${String(cursorBounds[1])} seconds.` : "");
  }
  function visibleFocusTarget(target: HTMLElement): HTMLElement {
    let visible = target;
    for (let parent = target.parentElement; parent && parent !== container; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS" && !(parent as HTMLDetailsElement).open) {
        visible = parent.querySelector<HTMLElement>(":scope > summary") ?? heading;
      }
    }
    return visible;
  }
  function missingEvent(): string { return state?.alignment === "damage" ? "No first damage" : "No arrival"; }
  function mappedInterval(run: PathRun): [number, number] | null {
    if (!state?.interval || anchorOrigin === null) return null;
    const origin = origins.get(run.id); if (origin === undefined) return null;
    return [state.interval[0] - anchorOrigin + origin, state.interval[1] - anchorOrigin + origin];
  }
  function cursorTime(run: PathRun): number | null {
    const origin = origins.get(run.id);
    return state && anchorOrigin !== null && origin !== undefined ? state.cursor - anchorOrigin + origin : null;
  }
  function absoluteAnchorTime(run: PathRun, time: number): number | null {
    const origin = origins.get(run.id);
    if (origin === undefined || anchorOrigin === null || !state) return null;
    const absolute = time - origin + anchorOrigin;
    // An event offset before the anchor's simulation began has no absolute anchor time.
    return absolute >= -EPSILON && absolute <= state.data.comparison.duration_s + EPSILON ? Math.max(0, absolute) : null;
  }
  function choosePoint(point: HitPoint | undefined) {
    if (!point || !state) return;
    inspection.open = true;
    const absolute = absoluteAnchorTime(point.run, point.time);
    if (state.selectedIds.includes(point.run.id)) {
      if (absolute !== null) actions.patch({ cursor: absolute });
      else cursorOutput.textContent = "This sample has no aligned time in the anchor. Choose Time to move the cursor.";
      return;
    }
    const pinnedBaseline = baselineRun(state, point.run)?.id === point.run.id;
    if (pinnedBaseline && anchor) {
      // Adding the pinned comparison must not change the meaning of an existing interval.
      actions.patch({ selectedIds: [...state.selectedIds, point.run.id], anchorId: anchor.id,
        ...(absolute !== null ? { cursor: absolute } : {}) });
      if (absolute === null) cursorOutput.textContent = "Baseline added. This sample has no aligned time in the anchor; choose Time to move the cursor.";
    } else actions.patch({ selectedIds: [point.run.id], anchorId: point.run.id, cursor: point.time, interval: null });
  }
  function chooseTime(value: number) {
    if (!finite(value) || !times.length || anchorOrigin === null) return;
    actions.patch({ cursor: times[nearestIndex(times, value)] });
  }
  function stepCursor(direction: -1 | 1) {
    if (!state || !times.length || anchorOrigin === null) return;
    const candidate = direction < 0 ? times.findLast(time => time < state!.cursor - EPSILON)
      : times.find(time => time > state!.cursor + EPSILON);
    if (candidate !== undefined) actions.patch({ cursor: candidate });
  }
  function graphKey(event: KeyboardEvent, chart: Chart) {
    if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.key === "Escape" && chart.drag) { cancelDrag(chart); event.preventDefault(); event.stopPropagation(); return; }
    if (!times.length || anchorOrigin === null) return;
    switch (event.key) {
      case "ArrowLeft": case "ArrowDown": stepCursor(-1); break;
      case "ArrowRight": case "ArrowUp": stepCursor(1); break;
      case "Home": actions.patch({ cursor: times[0] }); break;
      case "End": actions.patch({ cursor: times[times.length - 1] }); break;
      default: return;
    }
    inspection.open = true;
    event.preventDefault(); event.stopPropagation();
  }
  function plotPoint(chart: Chart, event: PointerEvent): { x: number; y: number } | null {
    try {
      const matrix = chart.plot.getScreenCTM(); if (!matrix || !finite(event.clientX) || !finite(event.clientY)) return null;
      const point = chart.plot.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
      const transformed = point.matrixTransform(matrix.inverse());
      return finite(transformed.x) && finite(transformed.y) ? transformed : null;
    } catch { return null; }
  }
  function nearestPoint(chart: Chart, x: number, y: number, target: EventTarget | null): HitPoint | undefined {
    const targetElement = target && "nodeType" in target ? target as Element : null;
    const targetRun = typeof targetElement?.closest === "function" ? targetElement.closest("[data-run]")?.getAttribute("data-run") : null;
    const annotation = typeof targetElement?.closest === "function" ? targetElement.closest("[data-time-s]") : null;
    if (annotation && targetRun) {
      const time = Number(annotation.getAttribute("data-time-s")), run = aligned.find(run => run.id === targetRun);
      if (run && finite(time)) return { x, y, time, run };
    }
    let best: HitPoint | undefined, distance = Infinity;
    for (const point of chart.points) {
      if (targetRun && point.run.id !== targetRun) continue;
      const d = chart.kind === "time" && !targetRun ? Math.abs(point.x - x) : Math.hypot(point.x - x, point.y - y);
      if (d < distance) { distance = d; best = point; }
    }
    return best;
  }
  function cancelDrag(chart: Chart) {
    const pointer = chart.drag?.id; chart.drag = null; chart.brush.setAttribute("visibility", "hidden");
    if (pointer !== undefined && chart.plot.hasPointerCapture(pointer)) chart.plot.releasePointerCapture(pointer);
  }
  function chartFor(key: string, kind: Chart["kind"]): Chart {
    const existing = charts.get(key); if (existing) return existing;
    const element = html("figure"), chartHeader = html("div", "comparison-section-header"), title = html("h4"), badge = html("strong", "comparison-muted");
    chartHeader.append(title, badge);
    const scroll = scrollRegion("comparison-plot-scroll", "Scrollable recorded graph");
    scroll.setAttribute("aria-describedby", interactionHelp.id);
    const titleId = `${prefix}-${key}-title`, descriptionId = `${prefix}-${key}-description`, clipId = `${prefix}-${key}-clip`;
    const plot = svg("svg", { viewBox: "0 0 720 268", width: 720, role: "img", tabindex: 0,
      class: kind === "route" ? "comparison-route-plot" : "comparison-time-plot", "aria-labelledby": titleId, "aria-describedby": descriptionId });
    plot.style.width = "100%"; plot.style.minWidth = "34rem"; plot.style.display = "block";
    // Touch belongs to the local scroller. Numeric endpoints provide the same interval action.
    plot.style.touchAction = "auto";
    const overlay = svg("g", { "aria-hidden": "true", "pointer-events": "none", "clip-path": `url(#${clipId})` });
    const brush = svg("rect", { class: "comparison-brush", y: TOP, height: BOTTOM - TOP, visibility: "hidden",
      fill: GRID, "fill-opacity": 0.35, stroke: TEXT, "stroke-dasharray": "4 3", "pointer-events": "none" });
    const caption = html("figcaption", "comparison-muted"); scroll.append(plot); element.append(chartHeader, scroll, caption);
    const chart: Chart = { key, kind, element, heading: title, badge, plot, caption, titleId, descriptionId, clipId,
      points: [], overlay, brush, fromX: value => value, x: value => value, y: value => value, domain: [0, 1], series: [], drag: null };
    charts.set(key, chart);
    plot.addEventListener("keydown", event => graphKey(event, chart), { signal: events.signal });
    plot.addEventListener("pointerdown", event => {
      if (event.pointerType === "touch" || event.button !== 0 || event.isPrimary === false) return;
      const point = plotPoint(chart, event); if (!point || point.x < LEFT || point.x > RIGHT || point.y < TOP || point.y > BOTTOM) return;
      plot.focus();
      chart.drag = { id: event.pointerId, x: point.x, y: point.y, lastX: point.x, lastY: point.y };
      if (chart.kind === "time" && anchorOrigin !== null && times.length) {
        try { plot.setPointerCapture(event.pointerId); } catch { cancelDrag(chart); }
      }
    }, { signal: events.signal });
    plot.addEventListener("pointermove", event => {
      const drag = chart.drag; if (!drag || drag.id !== event.pointerId) return;
      const point = plotPoint(chart, event); if (!point) { cancelDrag(chart); return; }
      drag.lastX = point.x; drag.lastY = point.y;
      if (chart.kind !== "time" || anchorOrigin === null || !times.length || Math.abs(point.x - drag.x) < 4) return;
      const end = clamp(point.x, LEFT, RIGHT);
      chart.brush.setAttribute("x", String(Math.min(drag.x, end)));
      chart.brush.setAttribute("width", String(Math.abs(drag.x - end)));
      chart.brush.setAttribute("visibility", "visible");
    }, { signal: events.signal });
    plot.addEventListener("pointerup", event => {
      const drag = chart.drag; if (!drag || drag.id !== event.pointerId) return;
      const point = plotPoint(chart, event); cancelDrag(chart); if (!point) return;
      if (chart.kind === "time" && anchorOrigin !== null && times.length && Math.abs(point.x - drag.x) >= 4) {
        const a = clamp(chart.fromX(clamp(drag.x, LEFT, RIGHT)) + anchorOrigin, cursorBounds[0], cursorBounds[1]);
        const b = clamp(chart.fromX(clamp(point.x, LEFT, RIGHT)) + anchorOrigin, cursorBounds[0], cursorBounds[1]);
        inspection.open = intervalDisclosure.open = true;
        actions.patch({ interval: [Math.min(a, b), Math.max(a, b)] });
      } else if (Math.hypot(point.x - drag.x, point.y - drag.y) < 8) choosePoint(nearestPoint(chart, point.x, point.y, event.target));
    }, { signal: events.signal });
    plot.addEventListener("pointercancel", () => cancelDrag(chart), { signal: events.signal });
    plot.addEventListener("lostpointercapture", () => { chart.drag = null; chart.brush.setAttribute("visibility", "hidden"); }, { signal: events.signal });
    plot.addEventListener("pointerleave", () => { if (chart.kind === "route") cancelDrag(chart); }, { signal: events.signal });
    return chart;
  }
  function resetChart(chart: Chart, title: string, description: string) {
    cancelDrag(chart); chart.heading.textContent = title; chart.points = []; chart.series = [];
    const definitions = svg("defs"), clip = svg("clipPath", { id: chart.clipId });
    clip.append(svg("rect", { x: LEFT, y: TOP, width: WIDTH, height: BOTTOM - TOP })); definitions.append(clip);
    chart.overlay.replaceChildren();
    chart.plot.replaceChildren(svg("title", { id: chart.titleId }, title), svg("desc", { id: chart.descriptionId }, description), definitions);
    chart.plot.parentElement?.setAttribute("aria-label", `${title}. Scroll horizontally for the full graph.`);
  }
  function chartLayer(chart: Chart) { const layer = svg("g", { "clip-path": `url(#${chart.clipId})` }); chart.plot.append(layer); return layer; }
  function stroke(run: PathRun, width = 2.3): Record<string, string | number> {
    const style = traceStyle(run.algorithm);
    return { fill: "none", stroke: style.color, "stroke-width": width, "stroke-dasharray": style.dash,
      "vector-effect": "non-scaling-stroke", "data-run": run.id };
  }
  function marker(parent: SVGElement, run: PathRun, x: number, y: number, title: string, damage = false) {
    const style = traceStyle(run.algorithm), attributes = { fill: SURFACE, stroke: style.color, "stroke-width": 2,
      "vector-effect": "non-scaling-stroke", "data-run": run.id };
    const node = damage ? svg("path", { ...attributes, d: `M${x} ${y - 6}l-5 9h10Z` })
      : run.outcome === "success" ? svg("circle", { ...attributes, cx: x, cy: y, r: 4 })
        : svg("rect", { ...attributes, x: x - 4, y: y - 4, width: 8, height: 8 });
    node.append(svg("title", {}, title)); parent.append(node);
  }
  function eventTitle(run: PathRun, event: PathRun["events"][number]): string {
    return event.kind === "damage" ? `${runName(run)}: damage ${event.after_s === null ? "first observed" : `after ${number(event.after_s)} s`} by ${number(event.by_s)} s, ${number(event.value, "damage_j")} J`
      : `${runName(run)}: ${outcomeLabel(run.outcome)} at ${number(event.by_s)} s`;
  }
  function axes(chart: Chart, xDomain: [number, number], yDomain: [number, number], xTitle: string, yTitle: string, signed = false) {
    chart.domain = xDomain;
    chart.x = value => LEFT + (value - xDomain[0]) / (xDomain[1] - xDomain[0]) * WIDTH;
    chart.y = value => BOTTOM - (value - yDomain[0]) / (yDomain[1] - yDomain[0]) * (BOTTOM - TOP);
    chart.fromX = value => xDomain[0] + (value - LEFT) / WIDTH * (xDomain[1] - xDomain[0]);
    for (let i = 0; i <= 4; i++) {
      const xv = xDomain[0] + (xDomain[1] - xDomain[0]) * i / 4, yv = yDomain[0] + (yDomain[1] - yDomain[0]) * i / 4;
      line(chart.plot, LEFT, chart.y(yv), RIGHT, chart.y(yv));
      label(chart.plot, LEFT - 10, chart.y(yv) + 5, tick(yv, signed), { "text-anchor": "end" });
      label(chart.plot, chart.x(xv), BOTTOM + 23, tick(xv), { "text-anchor": "middle" });
    }
    line(chart.plot, LEFT, TOP, LEFT, BOTTOM, { stroke: MUTED }); line(chart.plot, LEFT, BOTTOM, RIGHT, BOTTOM, { stroke: MUTED });
    label(chart.plot, LEFT, 20, yTitle); label(chart.plot, (LEFT + RIGHT) / 2, 259, xTitle, { "text-anchor": "middle" });
    if (signed) line(chart.plot, LEFT, chart.y(0), RIGHT, chart.y(0), { stroke: TEXT, "stroke-width": 1.5 });
  }
  function addPoints(chart: Chart, parent: SVGElement, run: PathRun, points: HitPoint[]) {
    const style = traceStyle(run.algorithm);
    for (const point of points) {
      if (point.x < LEFT - EPSILON || point.x > RIGHT + EPSILON) continue;
      chart.points.push(point);
      parent.append(svg("circle", { cx: point.x, cy: point.y, r: 1.8, fill: style.color, "data-run": run.id }));
    }
  }
  function drawMap(column: 2 | 3, mapX: [number, number], mapY: [number, number]): Chart {
    const chart = chartFor(column === 2 ? "lateral" : "normal", "route"), name = column === 2 ? "Lateral" : "Normal";
    chart.column = column;
    resetChart(chart, `Along / ${name.toLowerCase()}`, "Retained route-frame positions in metres. Trace numbers and patterns match the legend. Thick segments show the selected interval; hollow rings show actual cursor samples. Select a point or use arrow keys, Home and End.");
    chart.badge.textContent = state!.mapScale === "detail" ? "Expanded cross-track" : "Equal metres";
    chart.badge.dataset.mapScale = state!.mapScale;
    chart.caption.textContent = "Route offsets, m · recorded positions";
    axes(chart, mapX, mapY, "Along route, m", `${name} offset, m`);
    const layer = chartLayer(chart), x = chart.x, y = chart.y, goal = state!.data.comparison.route_length_m;
    line(layer, x(0), y(0), x(goal), y(0), { stroke: MUTED, "stroke-dasharray": "2 5" });
    line(layer, x(goal), TOP, x(goal), BOTTOM, { stroke: MUTED, "stroke-dasharray": "4 4" });
    label(layer, x(goal) - 5, TOP + 15, "Initial target", { "text-anchor": "end" });
    layer.append(svg("circle", { cx: x(0), cy: y(0), r: 3, fill: TEXT }));
    for (const run of aligned) {
      const parts = segments(run.samples, 1, column);
      const d = parts.map(part => pathData(part, x, y)).join(" ");
      if (d) layer.append(svg("path", { d, ...stroke(run) }));
      const interval = mappedInterval(run);
      if (interval) {
        const selected = run.samples.filter(row => row[0]! >= interval[0] && row[0]! <= interval[1]);
        const highlight = segments(selected, 1, column).map(part => pathData(part, x, y)).join(" ");
        if (highlight) layer.append(svg("path", { d: highlight, ...stroke(run, 6), class: "comparison-interval" }));
        for (const row of selected) if (finite(row[1]) && finite(row[column])) layer.append(svg("circle", {
          cx: x(row[1]!), cy: y(row[column]!), r: 3, fill: traceStyle(run.algorithm).color, "pointer-events": "none" }));
      }
      addPoints(chart, layer, run, run.samples.filter(row => finite(row[1]) && finite(row[column]))
        .map(row => ({ x: x(row[1]!), y: y(row[column]!), time: row[0]!, run })));
      for (const event of run.events.filter(event => event.kind === "damage")) {
        const row = sampleAtTime(run, event.by_s);
        if (row && finite(row[1]) && finite(row[column])) marker(layer, run, x(row[1]!), y(row[column]!), eventTitle(run, event), true);
      }
      const terminal = run.events.findLast(event => event.kind === "terminal"), last = terminal ? sampleAtTime(run, terminal.by_s) : run.samples.at(-1);
      if (last && finite(last[1]) && finite(last[column])) {
        const cx = x(last[1]!), cy = y(last[column]!);
        marker(layer, run, cx, cy, terminal ? eventTitle(run, terminal) : `${runName(run)}: last recorded sample at ${number(last[0])} s`);
        label(layer, cx + 7, cy - 7, String(traceStyle(run.algorithm).number), { fill: traceStyle(run.algorithm).color,
          "font-weight": 700, "paint-order": "stroke", stroke: SURFACE, "stroke-width": 3 });
      }
    }
    if (!aligned.some(run => run.samples.some(row => finite(row[1]) && finite(row[column])))) {
      label(chart.plot, (LEFT + RIGHT) / 2, (TOP + BOTTOM) / 2, `No recorded ${name.toLowerCase()} positions`, { "text-anchor": "middle", fill: TEXT });
    }
    chart.plot.append(chart.overlay, chart.brush); return chart;
  }
  function displayedSeries(run: PathRun, track: Track): Series {
    const origin = origins.get(run.id)!, reference = state!.difference ? baselineRun(state!, run) : undefined;
    const referenceOrigin = reference ? alignmentTime(reference, state!.alignment) : null;
    const referenceReadings = reference && referenceOrigin !== null ? seriesFor(reference, track) : [];
    const readings = seriesFor(run, track);
    // A reference-only reading must break a difference trace too. Iterating only
    // this run's timestamps would connect across that missing counterpart.
    const timestamps = [...readings.map(row => row[0]), ...(state!.difference && referenceOrigin !== null
      ? referenceReadings.map(row => row[0] - referenceOrigin + origin) : [])].sort((a, b) => a - b);
    const union = timestamps.filter((time, index) => index === 0 || !sameTime(time, timestamps[index - 1]));
    const rows = union.map((timestamp): SeriesRow => {
      const reading = readingAt(readings, timestamp), time = reading?.[0] ?? timestamp, raw = reading?.[1] ?? null;
      let value = raw, fromTime: number | null = track.command ? sampleAtTime(run, time)?.[16] ?? null : null;
      if (track.command && (!finite(fromTime) || fromTime >= time)) value = null;
      if (state!.difference) {
        const referenceTime = referenceOrigin === null ? null : time - origin + referenceOrigin;
        const counterpart = referenceTime === null ? null : readingAt(referenceReadings, referenceTime);
        value = finite(raw) && counterpart && finite(counterpart[1]) && finite(raw - counterpart[1]) ? raw - counterpart[1] : null;
        if (track.command && reference && referenceTime !== null && referenceOrigin !== null) {
          const referenceFrom = sampleAtTime(reference, referenceTime)?.[16];
          fromTime = finite(fromTime) && finite(referenceFrom) ? Math.max(fromTime, referenceFrom - referenceOrigin + origin) : null;
          if (!finite(fromTime) || fromTime >= time) value = null;
        }
      }
      return { time, value, from: fromTime };
    });
    if (track.command && state!.difference) {
      let lastGap = -Infinity;
      for (const row of rows) {
        if (!finite(row.value)) lastGap = row.time;
        // Keep the matched endpoint, but never draw a hold across an unmatched
        // retained timestamp or invent a shortened command start after that gap.
        else if (finite(row.from) && lastGap > row.from && lastGap < row.time) row.from = null;
      }
    }
    return { run, origin, rows };
  }
  function seriesPaths(series: Series, track: Track, x: (n: number) => number, y: (n: number) => number,
    window: [number, number] | null = null): string {
    if (track.pointsOnly) return "";
    if (track.command) return series.rows.filter(row => finite(row.value) && finite(row.from)
      && (!window || row.time >= window[0] && row.from! <= window[1]))
      .map(row => pathData([[row.from! - series.origin, row.value!], [row.time - series.origin, row.value!]], x, y)).join(" ");
    let d = "", connected = false;
    for (const row of series.rows) {
      if (!finite(row.value) || (window && (row.time < window[0] || row.time > window[1]))) { connected = false; continue; }
      d += `${connected ? "L" : "M"}${x(row.time - series.origin).toFixed(3)} ${y(row.value).toFixed(3)} `; connected = true;
    }
    return d;
  }
  function endTimeAnnotation(chart: Chart, parent: SVGElement, run: PathRun, time: number, lane: number, title: string, terminal = true) {
    const origin = origins.get(run.id); if (origin === undefined) return;
    const offset = time - origin; if (offset < chart.domain[0] || offset > chart.domain[1]) return;
    const position = chart.x(offset), style = traceStyle(run.algorithm);
    const annotation = svg("g", { class: "comparison-end-time", "data-run": run.id,
      "data-event-kind": terminal ? "terminal" : "last-sample", "data-time-s": time });
    annotation.append(svg("title", {}, `${title}. Time annotation only; no metric value is implied.`));
    line(annotation, position, TOP, position, BOTTOM, { stroke: style.color, "stroke-dasharray": "2 5", "stroke-opacity": 0.7 });
    const atRight = position > RIGHT - 120;
    label(annotation, position + (atRight ? -5 : 5), lane, `${style.number} ${terminal ? "end" : "last"} ${number(time)} s`, {
      "text-anchor": atRight ? "end" : "start", fill: style.color, "paint-order": "stroke", stroke: SURFACE, "stroke-width": 3 });
    parent.append(annotation);
  }
  function drawTimeline(track: Track): Chart {
    const chart = chartFor(`track-${track.id}`, "time"); chart.track = track;
    const title = `${state!.difference ? "Δ " : ""}${track.label}`;
    resetChart(chart, title, `${track.note}. ${state!.difference ? "Signed differences require actual paired readings at the same time or aligned event offset; missing counterparts on either run break the trace. Original values remain in Exact cursor values." : "Points are retained readings and lines connect adjacent available readings."} Command segments use only their recorded start and end. End-time annotations do not imply a metric value. Mouse or pen drags select an interval; touch pans. Arrow keys, Home and End move the cursor.`);
    chart.plot.dataset.track = track.id; chart.plot.dataset.difference = String(state!.difference);
    chart.badge.textContent = track.native ? "Native counter" : ""; chart.badge.hidden = !track.native;
    chart.caption.textContent = `${track.note}${state!.difference ? state!.alignment === "time" ? " · matched timestamps" : " · matched event offsets" : ""}`;
    chart.series = aligned.map(run => displayedSeries(run, track));
    const visible = chart.series.flatMap(series => series.rows.filter(row => finite(row.value)
      && row.time - series.origin >= timeDomain[0] && (row.from ?? row.time) - series.origin <= timeDomain[1]).map(row => row.value!));
    let yDomain: [number, number];
    if (state!.difference) {
      const maximum = visible.reduce((max, value) => Math.max(max, Math.abs(value)), 0);
      const span = maximum > 0 ? maximum * 1.08 : 1; yDomain = [-span, span];
    } else {
      yDomain = extent(visible, true);
      if (visible.every(value => value >= 0)) yDomain[0] = 0;
    }
    axes(chart, timeDomain, yDomain, state!.alignment === "time" ? "Simulation time, s"
      : state!.alignment === "damage" ? "From first damage observation, s" : "From arrival, s", `${title}, ${track.unit}`, state!.difference);
    const layer = chartLayer(chart), x = chart.x, y = chart.y;
    if (state!.interval && anchorOrigin !== null) layer.append(svg("rect", { class: "comparison-interval", x: x(state!.interval[0] - anchorOrigin), y: TOP,
      width: Math.max(0, x(state!.interval[1] - anchorOrigin) - x(state!.interval[0] - anchorOrigin)), height: BOTTOM - TOP,
      fill: GRID, "fill-opacity": 0.18, "pointer-events": "none" }));
    if (!state!.difference && track.id === "range_m") {
      const radius = state!.data.comparison.arrival_radius_m;
      if (radius >= yDomain[0] && radius <= yDomain[1]) {
        line(layer, LEFT, y(radius), RIGHT, y(radius), { stroke: MUTED, "stroke-dasharray": "4 4" });
        label(layer, RIGHT - 4, y(radius) - 6, `${number(radius, "range_m")} m arrival region`, { "text-anchor": "end" });
      }
    }
    chart.series.forEach((series, index) => {
      const { run, origin } = series, d = seriesPaths(series, track, x, y);
      if (d) layer.append(svg("path", { d, ...stroke(run) }));
      const interval = mappedInterval(run);
      if (interval) {
        const highlighted = seriesPaths(series, track, x, y, interval);
        if (highlighted) layer.append(svg("path", { d: highlighted, ...stroke(run, 5), class: "comparison-interval" }));
      }
      addPoints(chart, layer, run, series.rows.filter(row => finite(row.value)).map(row => ({ x: x(row.time - origin), y: y(row.value!), time: row.time, run })));
      const lane = TOP + 8 + index * 7;
      for (const event of run.events) {
        const position = x(event.by_s - origin);
        if (event.kind === "damage") {
          const bracket = svg("g", { "data-run": run.id, "data-event-kind": "damage", "data-time-s": event.by_s, "data-by-s": event.by_s,
            ...(event.after_s !== null ? { "data-after-s": event.after_s } : {}) });
          bracket.append(svg("title", {}, eventTitle(run, event)));
          if (event.after_s !== null) {
            const start = x(event.after_s - origin);
            line(bracket, start, lane, position, lane, { stroke: traceStyle(run.algorithm).color });
            line(bracket, start, lane - 3, start, lane + 3, { stroke: traceStyle(run.algorithm).color });
          }
          line(bracket, position, lane - 3, position, lane + 3, { stroke: traceStyle(run.algorithm).color });
          layer.append(bracket);
        } else endTimeAnnotation(chart, layer, run, event.by_s, TOP + 16 + index * 18, eventTitle(run, event));
        const reading = series.rows.find(row => sameTime(row.time, event.by_s));
        // Source brackets are time annotations, not substitute metric values.
        if (reading && finite(reading.value)) marker(layer, run, position, y(reading.value), eventTitle(run, event), event.kind === "damage");
      }
      if (!run.events.some(event => event.kind === "terminal")) {
        const lastTime = run.samples.at(-1)?.[0], last = finite(lastTime) ? series.rows.find(row => sameTime(row.time, lastTime)) : undefined;
        if (finite(lastTime)) {
          const title = `${runName(run)}: last recorded sample at ${number(lastTime)} s`;
          endTimeAnnotation(chart, layer, run, lastTime, TOP + 16 + index * 18, title, false);
          if (last && finite(last.value)) marker(layer, run, x(lastTime - origin), y(last.value), title);
        }
      }
    });
    if (!visible.length) label(chart.plot, (LEFT + RIGHT) / 2, (TOP + BOTTOM) / 2,
      state!.difference ? "No matched readings in this interval" : "No recorded readings in this interval", { "text-anchor": "middle", fill: TEXT });
    chart.plot.append(chart.overlay, chart.brush); return chart;
  }
  function syncCharts(parent: HTMLElement, nextCharts: Chart[]) {
    const wanted = new Set(nextCharts.map(chart => chart.element));
    for (const child of Array.from(parent.children)) if (!wanted.has(child as HTMLElement)) child.remove();
    nextCharts.forEach((chart, index) => {
      const current = parent.children.item(index);
      if (current === chart.element) return;
      if (chart.element.parentElement === parent && chart.element.contains(doc.activeElement)) {
        // Reorder siblings instead of detaching the focused graph and losing focus.
        const after = chart.element.nextSibling;
        let displaced = parent.children.item(index);
        while (displaced && displaced !== chart.element) {
          parent.insertBefore(displaced, after); displaced = parent.children.item(index);
        }
      } else parent.insertBefore(chart.element, current);
    });
  }
  function drawCharts() {
    const rows = aligned.flatMap(run => run.samples);
    let xDomain = extent([0, state!.data.comparison.route_length_m, ...rows.map(row => row[1]).filter(finite)]);
    let lateral = extent([0, ...rows.map(row => row[2]).filter(finite)]), normal = extent([0, ...rows.map(row => row[3]).filter(finite)]);
    if (state!.mapScale === "equal") {
      const unit = Math.max((xDomain[1] - xDomain[0]) / WIDTH, (lateral[1] - lateral[0]) / (BOTTOM - TOP), (normal[1] - normal[0]) / (BOTTOM - TOP));
      const equalDomain = (domain: [number, number], pixels: number): [number, number] => {
        const middle = domain[0] / 2 + domain[1] / 2; return [middle - unit * pixels / 2, middle + unit * pixels / 2];
      };
      xDomain = equalDomain(xDomain, WIDTH); lateral = equalDomain(lateral, BOTTOM - TOP); normal = equalDomain(normal, BOTTOM - TOP);
    }
    const routeCharts = aligned.length ? [drawMap(2, xDomain, lateral), drawMap(3, xDomain, normal)] : [];
    const timeCharts = aligned.length && inspection.open ? activeTracks().map(drawTimeline) : [];
    activeCharts = [...routeCharts, ...timeCharts]; syncCharts(maps, routeCharts); syncCharts(timelines, timeCharts);
    if (aligned.length && inspection.open && !selectedTracks.size) timelines.replaceChildren(html("p", "comparison-empty", "Choose a track to show timelines."));
  }
  function drawPathLegend() {
    pathLegend.hidden = !runs.length;
    pathLegend.replaceChildren(...runs.map(run => {
      const style = traceStyle(run.algorithm), item = html("li"); item.dataset.runId = run.id;
      item.title = `${run.id} · seed ${run.seed} · repetition ${run.repetition}`;
      const swatch = svg("svg", { viewBox: "0 0 36 12", width: 36, height: 12, "aria-hidden": "true", focusable: "false" });
      line(swatch, 1, 6, 35, 6, { stroke: style.color, "stroke-width": 3, "stroke-dasharray": style.dash });
      item.append(swatch, html("span", "", runName(run)));
      if (baselineRun(state!, run)?.id === run.id) item.append(html("small", "comparison-baseline-badge", "Baseline"));
      if (run.outcome !== "success") item.append(html("span", "comparison-muted", outcomeLabel(run.outcome)));
      return item;
    }));
  }
  function drawLegend() {
    const wanted = new Set(legendRuns.map(run => run.id));
    for (const [id, view] of legendItems) if (!wanted.has(id)) { view.item.remove(); legendItems.delete(id); }
    for (const [index, run] of legendRuns.entries()) {
      let view = legendItems.get(run.id);
      if (!view) {
        const item = html("li"), toggle = button(`Select ${runName(run)}`), note = html("span", "comparison-muted");
        const style = traceStyle(run.algorithm);
        const swatch = svg("svg", { viewBox: "0 0 36 12", width: 36, height: 12, "aria-hidden": "true", focusable: "false" });
        line(swatch, 1, 6, 35, 6, { stroke: style.color, "stroke-width": 3, "stroke-dasharray": style.dash });
        toggle.prepend(swatch); toggle.classList.add("comparison-run-toggle"); toggle.dataset.runId = run.id;
        toggle.style.display = "inline-flex"; toggle.style.alignItems = "center"; toggle.style.gap = ".375rem";
        item.style.flexWrap = "wrap"; item.style.overflowWrap = "anywhere";
        item.dataset.run = run.id; item.append(toggle, note);
        view = { run, item, toggle, note }; legendItems.set(run.id, view);
        const created = view;
        toggle.addEventListener("click", () => {
          if (!state) return;
          const id = created.run.id;
          const selectedIds = state.selectedIds.includes(id) ? state.selectedIds.filter(selected => selected !== id) : [...state.selectedIds, id];
          actions.patch({ selectedIds });
        }, { signal: events.signal });
      }
      view.run = run;
      const reference = baselineRun(state!, run)?.id === run.id;
      view.toggle.setAttribute("aria-pressed", String(state!.selectedIds.includes(run.id)));
      const eligible = state!.outcome === "all" || (run.outcome === "success") === (state!.outcome === "arrived");
      view.toggle.disabled = !eligible;
      const notes = [reference ? "Baseline, pinned" : "", run.outcome !== "success" ? outcomeLabel(run.outcome) : "",
        !eligible ? "Outside outcome filter" : "",
        alignmentTime(run, state!.alignment) === null ? missingEvent() : ""].filter(Boolean);
      view.note.textContent = notes.join(" · "); view.note.hidden = !notes.length;
      view.note.id = `${prefix}-run-note-${run.id}`;
      if (notes.length) view.toggle.setAttribute("aria-describedby", view.note.id); else view.toggle.removeAttribute("aria-describedby");
      if (legend.children.item(index) !== view.item) legend.insertBefore(view.item, legend.children.item(index));
    }
  }
  function drawIntervalSummary() {
    const selected = state!.interval, haveAnchor = anchorOrigin !== null;
    intervalSummary.hidden = !selected || !runs.length;
    if (!selected) { intervalSummary.replaceChildren(); return; }
    const rows = runs.map(run => {
      const excluded = !origins.has(run.id) || (selected && !haveAnchor);
      const interval = selected ? mappedInterval(run) : null;
      const stats = intervalStats(run, interval, state!.supplement);
      if (excluded) return [runName(run), null, null, null, null, null, null, !origins.has(run.id) ? missingEvent() : "Anchor has no alignment event"];
      const percent = `${number(stats.coverage * 100, "")}%`;
      const coverage = stats.start_s === null || stats.end_s === null ? `${percent} · no endpoints`
        : stats.start_s === stats.end_s ? `${percent} · one endpoint` : percent;
      return [runName(run), stats.start_s, stats.end_s, stats.duration_s,
        number(stats.fuel_kg, "fuel_kg"), number(stats.damage_j, "damage_j"), number(stats.progress_m, "along_m", true), coverage];
    });
    table(intervalSummary, "Interval endpoint deltas · actual per-run times",
      ["Controller", "From, s", "To, s", "Duration, s", "Fuel spent, kg", "Damage added, J", "Along progress, m", "Endpoint coverage"], rows);
  }
  function drawCursorReadout() {
    if (!state || !inspection.open || !readoutDetails.open) return;
    const ids = [...new Set(["along_m", "lateral_m", "normal_m", "range_m", "relative_speed_mps", "fuel_kg", "damage_j",
      ...activeTracks().map(track => track.id)])].filter(id => id !== "time_s");
    const rows = runs.map(run => {
      const time = cursorTime(run), sample = time === null ? null : sampleAtTime(run, time);
      const values = ids.map(id => time === null ? null : readingAt(seriesFor(run, byTrackId.get(id)!), time)?.[1] ?? null);
      const nativeReading = time !== null && activeTracks().some(track => track.native && readingAt(seriesFor(run, track), time));
      const last = run.samples.at(-1)?.[0];
      const sampleState = !origins.has(run.id) ? missingEvent() : anchorOrigin === null ? "Anchor has no alignment event"
        : sample ? "Path sample" : nativeReading ? "Native sample" : time !== null && finite(last) && time > last + EPSILON
          ? `Ended at ${number(last)} s` : "No sample at cursor";
      return [runName(run), sample ? sample[0] : nativeReading ? time : null, sampleState, ...values];
    });
    table(readout, state.difference ? "Original values · differences are drawn above" : "Original values · actual retained readings",
      ["Controller", "Actual time, s", "Reading", ...ids.map(id => { const track = byTrackId.get(id)!; return `${track.label}, ${track.unit}`; })], rows, true);
  }
  function updateCursorOverlays() {
    if (!state) return;
    const offset = anchorOrigin === null ? null : state.cursor - anchorOrigin;
    for (const chart of activeCharts) {
      chart.overlay.replaceChildren();
      if (chart.kind === "time" && offset !== null && offset >= chart.domain[0] && offset <= chart.domain[1]) {
        line(chart.overlay, chart.x(offset), TOP, chart.x(offset), BOTTOM, { stroke: TEXT, "stroke-dasharray": "2 3" });
      }
      for (const run of aligned) {
        const time = cursorTime(run); if (time === null) continue;
        let cx: number | null = null, cy: number | null = null;
        if (chart.kind === "route" && chart.column) {
          const row = sampleAtTime(run, time);
          if (row && finite(row[1]) && finite(row[chart.column])) { cx = chart.x(row[1]!); cy = chart.y(row[chart.column]!); }
        } else if (chart.kind === "time") {
          const series = chart.series.find(series => series.run.id === run.id), row = series?.rows.find(row => sameTime(row.time, time));
          if (series && row && finite(row.value)) { cx = chart.x(row.time - series.origin); cy = chart.y(row.value); }
        }
        if (cx !== null && cy !== null) chart.overlay.append(svg("circle", { cx, cy, r: 6, fill: SURFACE,
          stroke: traceStyle(run.algorithm).color, "stroke-width": 3, "vector-effect": "non-scaling-stroke", "data-run": run.id }));
      }
    }
  }
  function drawEvents() {
    if (!eventDetails.open || !eventTableDirty) return; eventTableDirty = false;
    table(eventTable, "Original source brackets and endpoints · simulation seconds",
      ["Controller", "Observation", "After, s", "By, s", "Damage, J"], runs.flatMap(run => run.events.map(event => [
        runName(run), event.kind === "damage" ? "Damage observed" : outcomeLabel(run.outcome), event.after_s, event.by_s,
        event.kind === "damage" ? number(event.value, "damage_j") : "n/a",
      ])));
  }
  function syncControls() {
    if (!state) return;
    alignment.value = state.alignment; equal.checked = state.mapScale === "equal";
    const canInspect = times.length > 0 && anchorOrigin !== null;
    cursorNumber.disabled = cursorRange.disabled = !canInspect;
    from.disabled = to.disabled = apply.disabled = !canInspect;
    clear.disabled = state.interval === null;
    for (const input of [cursorNumber, from, to]) {
      // Outward display bounds keep a rounded endpoint valid. validateTime checks its actual value.
      input.min = String(Math.floor(cursorBounds[0] * 1000) / 1000); input.max = String(Math.ceil(cursorBounds[1] * 1000) / 1000);
    }
    if (finite(state.cursor)) setInputTime(cursorNumber, state.cursor); else { cursorNumber.value = ""; inputTimes.delete(cursorNumber); }
    cursorRange.max = String(Math.max(0, times.length - 1)); cursorRange.value = String(Math.max(0, nearestIndex(times, state.cursor)));
    cursorRange.setAttribute("aria-valuetext", canInspect ? `${number(state.cursor)} simulation seconds in ${anchor?.label ?? "the anchor run"}` : "No aligned samples");
    previous.disabled = !canInspect || !times.some(time => time < state!.cursor - EPSILON);
    next.disabled = !canInspect || !times.some(time => time > state!.cursor + EPSILON);
    const output = canInspect ? `${number(state.cursor)} s` : "No aligned cursor";
    if (cursorOutput.textContent !== output) cursorOutput.textContent = output;
    const key = `${state.data.id}:${state.seed}:${state.repetition}:${anchor?.id}:${state.alignment}:${state.interval?.join(",") ?? "all"}`;
    if (key !== intervalKey) {
      intervalKey = key; setInputTime(from, state.interval?.[0] ?? cursorBounds[0]); setInputTime(to, state.interval?.[1] ?? cursorBounds[1]);
      from.setCustomValidity(""); to.setCustomValidity("");
    }
    trackCount.textContent = `${selectedTracks.size}/${MAX_TRACKS}`;
    for (const track of tracks) {
      const controls = trackInputs.get(track.id)!, checked = selectedTracks.has(track.id);
      const available = !track.native || runs.some(run => seriesFor(run, track).length > 0);
      controls.input.checked = checked;
      controls.note.textContent = available ? "" : " · not recorded";
      controls.input.disabled = !checked && (!available || selectedTracks.size >= MAX_TRACKS);
    }
  }
  function render(nextState: WorkspaceState, localChange = false) {
    if (disposed) return;
    const focused = doc.activeElement && container.contains(doc.activeElement) ? doc.activeElement : null;
    const focusedChart = focused ? activeCharts.find(chart => chart.element.contains(focused)) : undefined;
    state = nextState;
    const nextKey = `${nextState.seed}:${nextState.repetition}:${nextState.baseline}:${nextState.outcome}:${nextState.selectedIds.join(",")}:${nextState.anchorId}:${nextState.alignment}:${nextState.difference}:${nextState.mapScale}:${nextState.interval?.join(",") ?? "all"}`;
    const changed = localChange || previousState?.data !== nextState.data || previousState?.supplement !== nextState.supplement || nextKey !== structureKey;
    if (changed) {
      rawSeries.clear(); structureKey = nextKey; runs = inspectionRuns(nextState);
      anchor = inspectionAnchor(nextState);
      legendRuns = pairedRuns(nextState.data, nextState.seed, nextState.repetition);
      anchorOrigin = anchor ? alignmentTime(anchor, nextState.alignment) : null;
      origins = new Map();
      for (const run of runs) { const origin = alignmentTime(run, nextState.alignment); if (origin !== null) origins.set(run.id, origin); }
      aligned = runs.filter(run => origins.has(run.id));
      const offsets = aligned.flatMap(run => {
        const pathTimes = timeline([run]), nativeTimes = activeTracks().filter(track => track.native).flatMap(track => seriesFor(run, track).map(row => row[0]));
        return [...pathTimes, ...nativeTimes].map(time => time - origins.get(run.id)!);
      }).filter(finite).sort((a, b) => a - b);
      const uniqueOffsets = offsets.filter((time, index) => index === 0 || !sameTime(time, offsets[index - 1]));
      times = anchorOrigin === null ? [] : uniqueOffsets.map(time => time + anchorOrigin!).filter(time => time >= -EPSILON && time <= nextState.data.comparison.duration_s + EPSILON).map(time => Math.max(0, time));
      cursorBounds = [Math.min(times[0] ?? 0, 0), Math.max(times.at(-1) ?? 0, 0)];
      if (cursorBounds[0] === cursorBounds[1]) cursorBounds[1] = Math.min(nextState.data.comparison.duration_s, cursorBounds[0] + 1);
      if (nextState.interval && anchorOrigin !== null) timeDomain = [nextState.interval[0] - anchorOrigin, nextState.interval[1] - anchorOrigin];
      else timeDomain = [uniqueOffsets[0] ?? 0, uniqueOffsets.at(-1) ?? 1];
      if (timeDomain[0] === timeDomain[1]) timeDomain = [timeDomain[0] - 0.5, timeDomain[1] + 0.5];
      intervalAnchor.hidden = nextState.alignment === "time";
      intervalAnchor.textContent = "Bounds use the anchor's simulation seconds. Each flight uses the same event offset.";
      alignmentHelp.textContent = nextState.alignment === "time"
        ? "Cursor and interval inputs use simulation seconds. Every flight uses the same absolute time."
        : `${anchor ? `Anchor · ${runName(anchor)}. ` : ""}Inputs use its absolute simulation seconds. Each flight uses the same offset from its own ${nextState.alignment === "damage" ? "first damage by_s observation, not exact contact" : "arrival"}, without stretching durations.`;
      const excluded = runs.length - aligned.length;
      let message = excluded ? `${excluded} ${excluded === 1 ? "flight excluded" : "flights excluded"}, ${missingEvent().toLowerCase()}. Choose Time in Inspect time to include ${excluded === 1 ? "it" : "them"}.` : "";
      if (anchor && anchorOrigin === null) message += `${message ? " " : ""}The anchor has ${missingEvent().toLowerCase()}. Choose Time or another flight in Inspect time.`;
      if (nextState.difference && aligned.some(run => { const reference = baselineRun(nextState, run); return !reference || alignmentTime(reference, nextState.alignment) === null; }))
        message += `${message ? " " : ""}Baseline alignment unavailable. Differences remain gaps.`;
      if (status.textContent !== message) status.textContent = message;
      status.hidden = !message;
      empty.hidden = runs.length > 0;
      empty.textContent = "Select a flight above to inspect its path.";
      maps.hidden = timelines.hidden = !aligned.length; readoutDetails.hidden = !runs.length;
      drawPathLegend(); drawLegend(); drawCharts(); drawIntervalSummary(); eventTableDirty = true; drawEvents();
    }
    syncControls(); drawCursorReadout(); updateCursorOverlays(); previousState = nextState;
    if (focused && (!focused.isConnected || focused.closest("[hidden]") || focused.matches(":disabled"))) {
      const active = doc.activeElement;
      // A surviving control, including an external Clear selection button, owns its focus.
      if (active && active !== focused && active !== doc.body && active !== doc.documentElement) return;
      const recovery = focused === clear ? !from.disabled ? from : intervalDisclosureSummary
        : !runs.length ? [...legendItems.values()].find(view => !view.toggle.disabled)?.toggle ?? heading
        : !aligned.length ? alignment : focusedChart?.kind === "time" || picker.contains(focused) ? pickerSummary : alignment;
      visibleFocusTarget(recovery).focus();
    }
  }

  alignment.addEventListener("change", () => {
    if (alignment.value === "time" || alignment.value === "damage" || alignment.value === "arrival") actions.patch({ alignment: alignment.value });
  }, { signal: events.signal });
  equal.addEventListener("change", () => actions.patch({ mapScale: equal.checked ? "equal" : "detail" }), { signal: events.signal });
  cursorRange.addEventListener("input", () => { const time = times[Number(cursorRange.value)]; if (finite(time)) actions.patch({ cursor: time }); }, { signal: events.signal });
  function applyCursor() {
    validateTime(cursorNumber, "Enter a simulation time in seconds.");
    if (!cursorNumber.reportValidity()) return;
    chooseTime(inputTime(cursorNumber));
  }
  cursorNumber.addEventListener("input", () => { editedTime(cursorNumber); cursorNumber.setCustomValidity(""); }, { signal: events.signal });
  cursorNumber.addEventListener("change", applyCursor, { signal: events.signal });
  cursorNumber.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); applyCursor(); } }, { signal: events.signal });
  previous.addEventListener("click", () => stepCursor(-1), { signal: events.signal }); next.addEventListener("click", () => stepCursor(1), { signal: events.signal });
  for (const input of [from, to]) input.addEventListener("input", () => { editedTime(input); from.setCustomValidity(""); to.setCustomValidity(""); }, { signal: events.signal });
  intervalControls.addEventListener("submit", event => {
    event.preventDefault(); if (!state || anchorOrigin === null || !times.length) return;
    validateTime(from, "Enter the interval start in simulation seconds.");
    validateTime(to, "Enter the interval end in simulation seconds.");
    const start = inputTime(from), end = inputTime(to);
    if (finite(start) && finite(end) && end < start) to.setCustomValidity("Set To at or after From.");
    if (!intervalControls.reportValidity()) return;
    actions.patch({ interval: [start, end] });
  }, { signal: events.signal });
  clear.addEventListener("click", () => actions.patch({ interval: null }), { signal: events.signal });
  inspection.addEventListener("toggle", () => {
    if (!state || disposed) return;
    drawCharts(); drawCursorReadout(); updateCursorOverlays(); drawEvents();
  }, { signal: events.signal });
  readoutDetails.addEventListener("toggle", drawCursorReadout, { signal: events.signal });
  eventDetails.addEventListener("toggle", drawEvents, { signal: events.signal });

  // The shell owns normalization when data or selection changes. Rendering never patches shared state.
  return {
    update(nextState) { render(nextState); },
    dispose() {
      if (disposed) return; disposed = true;
      for (const chart of charts.values()) cancelDrag(chart);
      events.abort(); rawSeries.clear(); charts.clear(); legendItems.clear(); inputTimes.clear(); container.replaceChildren();
      if (addedClass) container.classList.remove("comparison-inspector");
      if (container.getAttribute("aria-labelledby") === heading.id) {
        if (originalLabel === null) container.removeAttribute("aria-labelledby"); else container.setAttribute("aria-labelledby", originalLabel);
      }
    },
  };
}
