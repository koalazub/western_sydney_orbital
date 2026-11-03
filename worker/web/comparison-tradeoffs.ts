// Recorded trial values only. This widget has no flight or control capability.
import {
  formatMetric, getMetric, metricCatalog, metricValue, paretoRunIds, runsForState,
  type Metric, type WorkspaceActions, type WorkspaceState, type WorkspaceWidget,
} from "./comparison-model.ts";
import { finite, outcomeLabel, traceStyle, type PathRun } from "./path-comparison-data.ts";

const DEFAULT_X = "fuel_kg", DEFAULT_Y = "time_s";
const PLOT = { left: 100, right: 694, top: 38, bottom: 266 };
const TEXT = "var(--lab-text)", MUTED = "var(--lab-muted)", GRID = "var(--lab-border)";
const numbers = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const tickNumbers = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 4 });
let instances = 0;

type OutcomeSymbol = "circle" | "square" | "cross";
type FlightValues = { run: PathRun; x: number | null; y: number | null };
type MeasuredFlight = { run: PathRun; x: number; y: number };
type PlotFlight = MeasuredFlight & { px: number; py: number };
type SelectionRange = { xmin: number; xmax: number; ymin: number; ymax: number };
type PointView = {
  node: SVGGElement; symbol: SVGGElement; number: SVGTextElement;
  baseline: SVGCircleElement; selected: SVGCircleElement; frontier: SVGPathElement; title: SVGTitleElement;
};
type RowView = {
  node: HTMLTableRowElement; name: HTMLDivElement; identity: HTMLDivElement;
  outcome: HTMLTableCellElement; x: HTMLTableCellElement; y: HTMLTableCellElement;
  time: HTMLTableCellElement; placement: HTMLTableCellElement; button: HTMLButtonElement;
};

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

function metricLabel(metric: Metric) {
  return metric.unit ? `${metric.label}, ${metric.unit}` : metric.label;
}

function outcomeSymbol(outcome: string): OutcomeSymbol {
  switch (outcome) {
    case "success": return "circle";
    case "failed_damage":
    case "timeout": return "square";
    default: return "cross";
  }
}

function drawSymbol(parent: SVGElement, kind: OutcomeSymbol, color: string, size = 5) {
  switch (kind) {
    case "circle":
      parent.replaceChildren(svg("circle", { r: size, fill: color, stroke: "var(--lab-surface)", "stroke-width": 1 }));
      break;
    case "square":
      parent.replaceChildren(svg("rect", { x: -size, y: -size, width: size * 2, height: size * 2,
        fill: color, stroke: "var(--lab-surface)", "stroke-width": 1 }));
      break;
    case "cross":
      parent.replaceChildren(svg("path", { d: `M${-size} ${-size}L${size} ${size}M${-size} ${size}L${size} ${-size}`,
        fill: "none", stroke: color, "stroke-width": 2.5, "stroke-linecap": "round" }));
      break;
  }
}

function symbolSwatch(kind: OutcomeSymbol) {
  const image = svg("svg", { viewBox: "-16 -16 32 32", width: 24, height: 24, "aria-hidden": "true" });
  drawSymbol(image, kind, TEXT);
  return image;
}

function diamond() {
  return svg("path", { d: "M0 -9L9 0L0 9L-9 0Z", fill: "none", stroke: TEXT, "stroke-width": 1.8 });
}

function bounds(values: number[]): [number, number] {
  if (!values.length) return [0, 1];
  const low = Math.min(...values), high = Math.max(...values);
  const pad = low === high ? Math.max(Math.abs(low) * 0.05, 0.01) : (high / 2 - low / 2) * 0.12;
  const minimum = low - pad, maximum = high + pad;
  return [finite(minimum) ? minimum : low, finite(maximum) ? maximum : high];
}

function fraction(value: number, [low, high]: [number, number]) {
  const span = high - low;
  return finite(span) ? (value - low) / span : (value / 2 - low / 2) / (high / 2 - low / 2);
}

function interpolate([low, high]: [number, number], position: number) {
  return low * (1 - position) + high * position;
}

function tick(value: number) {
  return value !== 0 && (Math.abs(value) < 0.01 || Math.abs(value) >= 10000)
    ? value.toExponential(1) : tickNumbers.format(value === 0 ? 0 : value);
}

function orderChildren(parent: Element, nodes: Element[]) {
  let cursor = parent.firstElementChild;
  for (const node of nodes) {
    if (node === cursor) cursor = cursor.nextElementSibling;
    else parent.insertBefore(node, cursor);
  }
}

export function installTradeoffExplorer(container: HTMLElement, actions: WorkspaceActions): WorkspaceWidget {
  const prefix = `comparison-tradeoffs-${++instances}`;
  const events = new AbortController();
  const listen = { signal: events.signal };
  const root = html("section", "comparison-tradeoffs");
  root.style.minWidth = "0";
  const header = html("div", "comparison-section-header");
  const heading = html("h3", "", "Whole-flight trade-offs"); heading.id = `${prefix}-heading`;
  root.setAttribute("aria-labelledby", heading.id);
  header.append(heading, html("span", "comparison-muted", "Actual trials"));

  const toolbar = html("div", "comparison-toolbar");
  function axisSelect(label: string, key: string, defaultMetric: string) {
    const field = html("label", "", label), select = html("select");
    select.id = select.name = `${prefix}-${key}`; field.htmlFor = select.id;
    select.setAttribute("aria-describedby", `${prefix}-${key}-help`);
    const groups = new Map<string, HTMLOptGroupElement>();
    for (const metric of metricCatalog) {
      let group = groups.get(metric.group);
      if (!group) {
        group = html("optgroup"); group.label = metric.group || "Other measurements";
        groups.set(metric.group, group); select.append(group);
      }
      group.append(new Option(metricLabel(metric), metric.id));
    }
    select.value = defaultMetric; select.disabled = true;
    field.append(select); toolbar.append(field);
    return select;
  }
  const xSelect = axisSelect("X axis", "x", DEFAULT_X), ySelect = axisSelect("Y axis", "y", DEFAULT_Y);
  const frontierToolbar = html("div", "comparison-toolbar");
  const frontierLabel = html("label"), frontierToggle = html("input"); frontierToggle.type = "checkbox";
  frontierToggle.id = `${prefix}-frontier`; frontierToggle.name = frontierToggle.id; frontierLabel.htmlFor = frontierToggle.id;
  frontierToggle.checked = true; frontierToggle.disabled = true;
  const frontierSwatch = svg("svg", { viewBox: "-16 -16 32 32", width: 24, height: 24, "aria-hidden": "true" });
  frontierSwatch.append(diamond());
  frontierLabel.append(frontierToggle, frontierSwatch, document.createTextNode("Best observed trade-offs"));
  const frontierScope = html("small", "comparison-muted", "Arrivals · selected axes"); frontierScope.id = `${prefix}-frontier-scope`;
  const frontierReason = html("span", "comparison-inline-help", "Choose two objectives");
  frontierReason.id = `${prefix}-frontier-reason`; frontierReason.hidden = true;
  frontierToggle.setAttribute("aria-describedby", `${frontierScope.id} ${frontierReason.id} ${prefix}-frontier-help`);
  frontierToolbar.append(frontierLabel, frontierScope, frontierReason);

  const legend = html("div", "comparison-legend");
  const controllers = html("div", "comparison-controller-legend"), controllerItems = html("ul");
  controllerItems.setAttribute("aria-label", "Controllers"); controllers.append(controllerItems);
  const outcomes = html("div", "comparison-outcome-legend"), outcomeItems = html("ul");
  outcomeItems.setAttribute("aria-label", "Outcomes"); outcomes.append(outcomeItems);
  for (const [kind, label] of [["circle", "Arrived"], ["square", "Failed or timed out"], ["cross", "Error or interrupted"]] as const) {
    const item = html("li"); item.append(symbolSwatch(kind), document.createTextNode(label)); outcomeItems.append(item);
  }
  const highlights = html("div", "comparison-highlight-legend");
  for (const kind of ["baseline", "selected"] as const) {
    const item = html("span"), image = svg("svg", { viewBox: "-16 -16 32 32", width: 24, height: 24, "aria-hidden": "true" });
    image.append(svg("circle", { r: kind === "baseline" ? 10 : 13, fill: "none", stroke: TEXT,
      "stroke-width": kind === "baseline" ? 1.6 : 2.5, "stroke-dasharray": kind === "baseline" ? "2 2" : "none" }));
    item.append(image, document.createTextNode(kind === "baseline" ? "Baseline" : "Selected")); highlights.append(item);
  }
  legend.append(controllers, outcomes);

  const status = html("p", "comparison-muted"); status.id = `${prefix}-status`;
  status.setAttribute("role", "status"); status.setAttribute("aria-atomic", "true");
  const chartHelp = html("p", "comparison-inline-help"); chartHelp.id = `${prefix}-chart-help`;
  const helpDetails = html("details", "comparison-inline-help");
  const flightsLink = html("a", "", "Flight values"), rangeLink = html("a", "", "Select range");
  flightsLink.href = `#${prefix}-flight-list`; rangeLink.href = `#${prefix}-range-summary`;
  chartHelp.append(document.createTextNode("Select a point, or open "), flightsLink,
    document.createTextNode(". Drag a range with a mouse or pen, or use "), rangeLink,
    document.createTextNode(". Arrow keys move between points; Enter or Space selects."));
  helpDetails.append(html("summary", "", "Plot help"), chartHelp, highlights,
    html("p", "comparison-muted", "Touch gestures scroll the plot. Select range provides the same selection without dragging."));

  const plotScroll = html("div", "comparison-plot-scroll");
  plotScroll.tabIndex = 0; plotScroll.setAttribute("role", "region");
  plotScroll.setAttribute("aria-label", "Flight trade-offs, scroll horizontally for the full plot");
  plotScroll.setAttribute("aria-describedby", chartHelp.id);
  plotScroll.style.minWidth = "0"; plotScroll.style.maxWidth = "100%"; plotScroll.style.overflowX = "auto";
  const plot = svg("svg", { viewBox: "0 0 720 330", width: "100%", class: "comparison-scatter", role: "group",
    "aria-labelledby": `${prefix}-plot-title`, "aria-describedby": `${status.id} ${chartHelp.id}` });
  plot.style.width = "100%"; plot.style.minWidth = "34rem"; plot.style.height = "auto"; plot.style.display = "block";
  plot.style.touchAction = "auto"; plot.style.userSelect = "none";
  plot.append(svg("title", { id: `${prefix}-plot-title` }, "Whole-flight trade-offs"),
    svg("desc", {}, "Each point is an actual trial. Controller numbers match the legend. Non-arrivals do not enter the best observed trade-offs."));
  const plotBackground = svg("rect", { x: PLOT.left, y: PLOT.top, width: PLOT.right - PLOT.left,
    height: PLOT.bottom - PLOT.top, fill: "transparent", "aria-hidden": "true" });
  const axisLayer = svg("g", { "aria-hidden": "true", "pointer-events": "none" });
  const pointLayer = svg("g");
  const brushRectangle = svg("rect", { class: "comparison-brush", visibility: "hidden", "aria-hidden": "true",
    "pointer-events": "none", fill: TEXT, "fill-opacity": 0.08, stroke: TEXT, "stroke-width": 1.5,
    "vector-effect": "non-scaling-stroke" });
  plot.append(plotBackground, axisLayer, pointLayer, brushRectangle); plotScroll.append(plot);

  const empty = html("div", "comparison-empty"), emptyText = html("p"); empty.hidden = true;
  const resetAxes = html("button", "", "Reset axes"); resetAxes.type = "button";
  empty.append(emptyText, resetAxes);

  const rangeDetails = html("details"), rangeSummary = html("summary", "", "Select range");
  rangeSummary.id = `${prefix}-range-summary`;
  const rangeForm = html("form"); rangeForm.noValidate = true;
  const rangeFields = html("div", "comparison-range-fields");
  const rangeFeedback = html("p", "comparison-inline-help"); rangeFeedback.id = `${prefix}-range-feedback`;
  rangeFeedback.setAttribute("role", "status"); rangeFeedback.setAttribute("aria-atomic", "true");
  const rangeHelp = html("p", "comparison-muted", "Bounds use the selected axis units. Only plotted flights are selected.");
  rangeHelp.id = `${prefix}-range-help`;
  const fields = (["x-min", "x-max", "y-min", "y-max"] as const).map(key => {
    const label = html("label"), text = html("span"), input = html("input");
    input.id = input.name = `${prefix}-${key}`; input.type = "number"; input.step = "any";
    input.inputMode = "decimal"; input.required = true; label.htmlFor = input.id;
    input.setAttribute("aria-describedby", `${rangeHelp.id} ${rangeFeedback.id}`);
    label.append(text, input); rangeFields.append(label);
    return { input, text };
  });
  const rangeActions = html("div", "comparison-toolbar");
  const selectRange = html("button", "", "Select range"); selectRange.type = "submit"; selectRange.disabled = true;
  const resetRange = html("button", "", "Use plotted bounds"); resetRange.type = "button"; resetRange.disabled = true;
  rangeActions.append(selectRange, resetRange);
  rangeForm.append(rangeFields, rangeActions, rangeHelp, rangeFeedback); rangeDetails.append(rangeSummary, rangeForm);

  const flights = html("details", "comparison-flight-picker"), flightsSummary = html("summary", "", "Flight values");
  const flightScroll = html("div", "comparison-plot-scroll"); flightScroll.id = `${prefix}-flight-list`;
  flightScroll.tabIndex = 0; flightScroll.setAttribute("role", "region");
  flightScroll.setAttribute("aria-label", "Flight values. Scroll for all columns.");
  flightScroll.style.minWidth = "0"; flightScroll.style.maxWidth = "100%"; flightScroll.style.overflowX = "auto";
  const flightTable = html("table", "comparison-point-list");
  flightTable.append(html("caption", "", "All flights in the current scope, including missing axis values."));
  const tableHead = flightTable.createTHead().insertRow();
  const columnHeads = ["Flight", "Outcome", "X", "Y", "Recorded time, s", "Plot status", "Select"].map(title => {
    const cell = html("th", "", title); cell.scope = "col"; tableHead.append(cell); return cell;
  });
  const tableBody = flightTable.createTBody(); flightScroll.append(flightTable); flights.append(flightsSummary, flightScroll);

  const metricDetails = html("details"), xHelp = html("p", "comparison-muted"), yHelp = html("p", "comparison-muted");
  xHelp.id = `${prefix}-x-help`; yHelp.id = `${prefix}-y-help`;
  const frontierHelp = html("p", "comparison-muted",
    "Best observed trade-offs compares arrived flights on these two objectives. It is not a safety ranking.");
  frontierHelp.id = `${prefix}-frontier-help`;
  metricDetails.append(html("summary", "", "Axis settings"), frontierToolbar, xHelp, yHelp, frontierHelp);
  root.append(header, toolbar, legend, status, plotScroll, empty, rangeDetails, flights, metricDetails, helpDetails);
  container.append(root);

  const pointViews = new Map<string, PointView>(), rowViews = new Map<string, RowView>();
  let current: WorkspaceState | null = null, disposed = false, frontierEnabled = true;
  let activePointId: string | null = null, visiblePoints: PlotFlight[] = [];
  let xBounds: [number, number] = [0, 1], yBounds: [number, number] = [0, 1];
  let rangeDirty = false, rangeValidationShown = false, rangeContext = "", plotKey = "";
  let renderedData: WorkspaceState["data"] | null = null;
  let renderedSupplement: WorkspaceState["supplement"] = null, renderKey = "";
  let brush: { pointerId: number; x: number; y: number; clientX: number; clientY: number; dragged: boolean } | null = null;

  function clearRangeFeedback() {
    rangeFeedback.textContent = "";
    for (const field of fields) field.input.removeAttribute("aria-invalid");
  }

  function setRangeValues(value: SelectionRange) {
    [value.xmin, value.xmax, value.ymin, value.ymax].forEach((number, index) => { fields[index].input.value = String(number); });
  }

  function usePlotBounds() {
    setRangeValues({ xmin: xBounds[0], xmax: xBounds[1], ymin: yBounds[0], ymax: yBounds[1] });
    rangeDirty = false; rangeValidationShown = false; clearRangeFeedback();
  }

  function validateRange(focusInvalid: boolean): SelectionRange | null {
    clearRangeFeedback();
    const values = fields.map(field => field.input.valueAsNumber);
    const invalid = fields.filter((field, index) => !finite(values[index]));
    let message = "";
    if (invalid.length) message = "Enter a number for each bound.";
    else {
      const errors: string[] = [];
      if (values[0] > values[1]) { invalid.push(fields[0], fields[1]); errors.push("Set X min no higher than X max."); }
      if (values[2] > values[3]) { invalid.push(fields[2], fields[3]); errors.push("Set Y min no higher than Y max."); }
      message = errors.join(" ");
    }
    if (invalid.length) {
      for (const field of invalid) field.input.setAttribute("aria-invalid", "true");
      rangeFeedback.textContent = message;
      if (focusInvalid) invalid[0].input.focus();
      return null;
    }
    if (values[0] < xBounds[0] || values[1] > xBounds[1] || values[2] < yBounds[0] || values[3] > yBounds[1]) {
      rangeFeedback.textContent = "Part of this range is outside the plot. Use plotted bounds to match it.";
    }
    return { xmin: values[0], xmax: values[1], ymin: values[2], ymax: values[3] };
  }

  function cancelBrush() {
    const pointerId = brush?.pointerId;
    brush = null; brushRectangle.setAttribute("visibility", "hidden");
    if (pointerId !== undefined && plot.hasPointerCapture(pointerId)) plot.releasePointerCapture(pointerId);
  }

  function pointerPosition(event: PointerEvent): [number, number] | null {
    const matrix = plot.getScreenCTM();
    if (!matrix) return null;
    const point = plot.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
    const position = point.matrixTransform(matrix.inverse());
    return finite(position.x) && finite(position.y) ? [position.x, position.y] : null;
  }

  function pointTarget(target: EventTarget | null) {
    const node = target instanceof Element ? target.closest<SVGGElement>("[data-flight-point]") : null;
    return node && pointLayer.contains(node) ? node : null;
  }

  function setActivePoint(id: string) {
    activePointId = id;
    for (const [pointId, view] of pointViews) view.node.setAttribute("tabindex", pointId === id ? "0" : "-1");
  }

  function revealPoint(node: SVGGElement) {
    const point = node.getBoundingClientRect(), viewport = plotScroll.getBoundingClientRect();
    const left = viewport.left + plotScroll.clientLeft, right = left + plotScroll.clientWidth;
    const top = viewport.top + plotScroll.clientTop, bottom = top + plotScroll.clientHeight;
    // Reveal within this plot only, without scrolling the rest of the workspace.
    if (point.left < left) plotScroll.scrollLeft += point.left - left;
    else if (point.right > right) plotScroll.scrollLeft += point.right - right;
    if (point.top < top) plotScroll.scrollTop += point.top - top;
    else if (point.bottom > bottom) plotScroll.scrollTop += point.bottom - bottom;
  }

  function focusPoint(id: string) {
    setActivePoint(id);
    const node = pointViews.get(id)?.node;
    if (node) { node.focus({ preventScroll: true }); revealPoint(node); }
  }

  function createPoint(run: PathRun): PointView {
    const node = svg("g", { role: "button", tabindex: "-1", class: "comparison-point",
      "data-flight-point": "", "data-run-id": run.id, "aria-describedby": chartHelp.id });
    const title = svg("title"), frontier = diamond(); frontier.classList.add("comparison-frontier");
    const baseline = svg("circle", { r: 11, fill: "none", stroke: TEXT, "stroke-width": 1.6, "stroke-dasharray": "2 2" });
    const selected = svg("circle", { r: 14, fill: "none", stroke: TEXT, "stroke-width": 2.5 });
    const symbol = svg("g", { "aria-hidden": "true", "pointer-events": "none" });
    const number = svg("text", { x: 9, y: -10, fill: TEXT, "font-size": 13, "aria-hidden": "true",
      "pointer-events": "none", "paint-order": "stroke", stroke: "var(--lab-surface)", "stroke-width": 3 });
    const hit = svg("circle", { r: 15, fill: "transparent", "aria-hidden": "true" });
    node.append(title, hit, frontier, baseline, selected, symbol, number); pointLayer.append(node);
    const view = { node, title, frontier, baseline, selected, symbol, number };
    pointViews.set(run.id, view); return view;
  }

  function createRow(run: PathRun): RowView {
    const node = html("tr"); node.dataset.runId = run.id; node.id = `${prefix}-flight-${run.id}`;
    const head = html("th"); head.scope = "row";
    const name = html("div"), identity = html("div", "comparison-muted"); head.append(name, identity);
    const outcome = html("td"), x = html("td"), y = html("td"), time = html("td"), placement = html("td");
    const actionCell = html("td"), button = html("button", "", "Select"); button.type = "button";
    button.dataset.selectRun = run.id; actionCell.append(button);
    node.append(head, outcome, x, y, time, placement, actionCell); tableBody.append(node);
    const view = { node, name, identity, outcome, x, y, time, placement, button };
    rowViews.set(run.id, view); return view;
  }

  function drawAxes(xMetric: Metric, yMetric: Metric) {
    const content: SVGElement[] = [];
    for (let index = 0; index <= 4; index++) {
      const position = index / 4, x = PLOT.left + position * (PLOT.right - PLOT.left);
      const y = PLOT.bottom - position * (PLOT.bottom - PLOT.top);
      content.push(svg("line", { x1: PLOT.left, x2: PLOT.right, y1: y, y2: y, stroke: GRID }),
        svg("line", { x1: x, x2: x, y1: PLOT.top, y2: PLOT.bottom, stroke: GRID, "stroke-dasharray": "2 4" }),
        svg("text", { x: PLOT.left - 10, y: y + 5, fill: MUTED, "font-size": 14, "text-anchor": "end" }, tick(interpolate(yBounds, position))),
        svg("text", { x, y: 289, fill: MUTED, "font-size": 14, "text-anchor": "middle" }, tick(interpolate(xBounds, position))));
    }
    content.push(svg("text", { x: PLOT.left, y: 21, fill: TEXT, "font-size": 15 }, metricLabel(yMetric)),
      svg("text", { x: (PLOT.left + PLOT.right) / 2, y: 320, fill: TEXT, "font-size": 15, "text-anchor": "middle" }, metricLabel(xMetric)));
    axisLayer.replaceChildren(...content);
  }

  function describeMetric(metric: Metric, state: WorkspaceState) {
    const field = metric.nativeKey ?? (metric.column === undefined ? metric.id : state.data.columns[metric.column] ?? metric.id);
    return `${metricLabel(metric)}. ${metric.description} Source · ${metric.source} · ${field}`;
  }

  function render() {
    const state = current;
    if (!state || disposed) return;
    const focused = document.activeElement;
    const hadFocus = focused !== null && root.contains(focused);
    const focusedPoint = hadFocus ? pointTarget(focused)?.getAttribute("data-run-id") : null;
    const focusedRow = hadFocus ? focused?.closest("tr[data-run-id]")?.getAttribute("data-run-id") : null;
    const xMetric = getMetric(state.xMetric) ?? getMetric(DEFAULT_X)!;
    const yMetric = getMetric(state.yMetric) ?? getMetric(DEFAULT_Y)!;
    const runs = [...runsForState(state)].sort((a, b) => traceStyle(a.algorithm).number - traceStyle(b.algorithm).number
      || a.seed - b.seed || a.repetition - b.repetition || a.id.localeCompare(b.id));
    const values: FlightValues[] = runs.map(run => {
      const x = metricValue(run, xMetric.id, state.supplement ?? undefined);
      const y = metricValue(run, yMetric.id, state.supplement ?? undefined);
      return { run, x: finite(x) ? x : null, y: finite(y) ? y : null };
    });
    const measured = values.filter((flight): flight is MeasuredFlight => flight.x !== null && flight.y !== null);
    const nextPlotKey = JSON.stringify([state.data.id, xMetric.id, yMetric.id, values.map(value => [value.run.id, value.x, value.y])]);
    const geometryChanged = nextPlotKey !== plotKey;
    if (geometryChanged) cancelBrush();
    plotKey = nextPlotKey;
    xBounds = bounds(measured.map(value => value.x)); yBounds = bounds(measured.map(value => value.y));
    visiblePoints = measured.map(value => ({ ...value,
      px: PLOT.left + fraction(value.x, xBounds) * (PLOT.right - PLOT.left),
      py: PLOT.bottom - fraction(value.y, yBounds) * (PLOT.bottom - PLOT.top) }));
    const selected = new Set(state.selectedIds);
    const objectives = xMetric.direction !== "context" && yMetric.direction !== "context" && xMetric.id !== yMetric.id;
    const arrivals = measured.filter(value => value.run.outcome === "success").map(value => value.run);
    const frontier = new Set(objectives && frontierEnabled && arrivals.length
      ? paretoRunIds(arrivals, xMetric.id, yMetric.id, state.supplement ?? undefined) : []);
    const isBaseline = (run: PathRun) => run.algorithm === state.baseline || run.id === state.baseline;

    xSelect.disabled = ySelect.disabled = false;
    xSelect.value = xMetric.id; ySelect.value = yMetric.id;
    frontierToggle.disabled = !objectives; frontierToggle.checked = objectives && frontierEnabled;
    frontierReason.hidden = objectives;
    frontierToggle.setAttribute("aria-describedby", `${frontierScope.id} ${frontierHelp.id}${objectives ? "" : ` ${frontierReason.id}`}`);
    status.textContent = `${numbers.format(measured.length)}/${numbers.format(runs.length)} plotted · ${numbers.format(runs.length - measured.length)} missing axis values · ${numbers.format(runs.filter(run => run.outcome !== "success").length)} non-arrivals · ${numbers.format(selected.size)} selected`;
    xHelp.textContent = describeMetric(xMetric, state); yHelp.textContent = describeMetric(yMetric, state);
    columnHeads[2].textContent = metricLabel(xMetric); columnHeads[3].textContent = metricLabel(yMetric);
    fields.forEach((field, index) => {
      const axis = index < 2 ? "X" : "Y", metric = index < 2 ? xMetric : yMetric;
      field.text.textContent = `${axis} ${index % 2 === 0 ? "min" : "max"}${metric.unit ? `, ${metric.unit}` : ""}`;
      field.input.disabled = measured.length === 0;
    });
    selectRange.disabled = resetRange.disabled = measured.length === 0;
    const nextRangeContext = JSON.stringify([state.data.id, xMetric.id, yMetric.id]);
    if (rangeContext !== nextRangeContext || !rangeDirty) { rangeContext = nextRangeContext; usePlotBounds(); }
    else if (geometryChanged) validateRange(false);
    drawAxes(xMetric, yMetric);

    const legendRuns = [...new Map(state.data.runs.map(run => [run.algorithm, run])).values()]
      .sort((a, b) => traceStyle(a.algorithm).number - traceStyle(b.algorithm).number);
    controllerItems.replaceChildren(...legendRuns.map(run => {
      const style = traceStyle(run.algorithm), item = html("li");
      const baseline = run.algorithm === state.baseline
        || state.data.runs.some(candidate => candidate.algorithm === run.algorithm && candidate.id === state.baseline);
      item.classList.toggle("is-baseline", baseline);
      const swatch = svg("svg", { viewBox: "0 0 32 10", width: 32, height: 10, "aria-hidden": "true" });
      swatch.append(svg("line", { x1: 0, x2: 32, y1: 5, y2: 5, stroke: style.color, "stroke-width": 3, "stroke-dasharray": style.dash }));
      item.append(swatch, document.createTextNode(`${style.number}. ${run.label}${baseline ? " · baseline" : ""}`)); return item;
    }));

    const pointIds = new Set(visiblePoints.map(point => point.run.id));
    for (const [id, view] of pointViews) if (!pointIds.has(id)) { view.node.remove(); pointViews.delete(id); }
    if (!activePointId || !pointIds.has(activePointId)) {
      activePointId = visiblePoints.find(point => selected.has(point.run.id))?.run.id ?? visiblePoints[0]?.run.id ?? null;
    }
    for (const point of visiblePoints) {
      const run = point.run, style = traceStyle(run.algorithm), view = pointViews.get(run.id) ?? createPoint(run);
      const baseline = isBaseline(run), chosen = selected.has(run.id), best = frontier.has(run.id);
      const timestamp = finite(run.final.time_s) ? `Recorded time ${String(run.final.time_s)} s.` : "Time not recorded.";
      const name = `Select ${style.number}. ${run.label}, flight ${run.id}, seed ${run.seed}, repetition ${run.repetition}. ${outcomeLabel(run.outcome)}. ${metricLabel(xMetric)}: ${formatMetric(point.x, xMetric)}. ${metricLabel(yMetric)}: ${formatMetric(point.y, yMetric)}. ${timestamp}${baseline ? " Baseline." : ""}${best ? " Best observed trade-offs." : ""}`;
      view.node.setAttribute("transform", `translate(${point.px} ${point.py})`);
      view.node.setAttribute("aria-label", name); view.node.setAttribute("aria-pressed", String(chosen));
      view.node.setAttribute("tabindex", run.id === activePointId ? "0" : "-1");
      view.node.classList.toggle("is-selected", chosen); view.node.classList.toggle("is-baseline", baseline);
      view.node.style.setProperty("--trace-color", style.color);
      view.title.textContent = name;
      view.baseline.setAttribute("visibility", baseline ? "visible" : "hidden");
      view.selected.setAttribute("visibility", chosen ? "visible" : "hidden");
      view.frontier.setAttribute("visibility", best ? "visible" : "hidden");
      drawSymbol(view.symbol, outcomeSymbol(run.outcome), style.color, chosen ? 6 : 5);
      view.number.textContent = String(style.number); view.number.setAttribute("font-weight", chosen ? "750" : "500");
    }
    orderChildren(pointLayer, visiblePoints.map(point => pointViews.get(point.run.id)!.node));

    const rowIds = new Set(runs.map(run => run.id));
    for (const [id, view] of rowViews) if (!rowIds.has(id)) { view.node.remove(); rowViews.delete(id); }
    for (const value of values) {
      const run = value.run, style = traceStyle(run.algorithm), view = rowViews.get(run.id) ?? createRow(run);
      const chosen = selected.has(run.id), baseline = isBaseline(run), plotted = value.x !== null && value.y !== null;
      view.node.classList.toggle("is-selected", chosen); view.node.classList.toggle("is-baseline", baseline);
      view.name.textContent = `${style.number}. ${run.label}`;
      view.identity.textContent = `${run.id} · seed ${run.seed} · repetition ${run.repetition}`;
      view.outcome.textContent = outcomeLabel(run.outcome);
      for (const [cell, number, metric] of [[view.x, value.x, xMetric], [view.y, value.y, yMetric]] as const) {
        cell.textContent = number === null ? "Not recorded" : formatMetric(number, metric);
        cell.title = number === null ? "Not recorded" : `${String(number)} ${metric.unit}`.trim();
      }
      view.time.textContent = finite(run.final.time_s) ? String(run.final.time_s) : "Not recorded";
      const placement = plotted ? ["Plotted"] : [`Not plotted · ${value.x === null && value.y === null ? "both axis values missing" : value.x === null ? "X value missing" : "Y value missing"}`];
      if (baseline) placement.push("Baseline");
      if (frontier.has(run.id)) placement.push("Best observed trade-offs");
      if (chosen) placement.push("Selected");
      view.placement.textContent = placement.join(" · ");
      view.button.setAttribute("aria-label", `Select ${style.number}. ${run.label}, flight ${run.id}, seed ${run.seed}, repetition ${run.repetition}`);
      view.button.setAttribute("aria-pressed", String(chosen));
    }
    orderChildren(tableBody, runs.map(run => rowViews.get(run.id)!.node));
    empty.hidden = measured.length !== 0;
    resetAxes.hidden = runs.length === 0;
    emptyText.textContent = runs.length ? "No flights have both axis values. Choose other metrics or reset axes."
      : state.outcome !== "all" ? "No flights match this outcome. Change Outcome in Filters."
        : "No recorded flights in this scope. Choose another seed in Filters.";

    if (hadFocus && focused) {
      let target: Element | undefined = focused;
      if (!root.contains(focused) || focused.closest("[hidden]") || focused.matches(":disabled")) {
        target = focusedPoint ? pointViews.get(focusedPoint)?.node ?? pointViews.get(activePointId ?? "")?.node
          : focusedRow ? rowViews.get(focusedRow)?.button ?? rowViews.values().next().value?.button : flightsSummary;
      }
      if (!(target instanceof HTMLElement || target instanceof SVGElement)) target = flightsSummary;
      if (target !== document.activeElement && (target instanceof HTMLElement || target instanceof SVGElement)) target.focus({ preventScroll: true });
      const point = pointTarget(target);
      if (point) revealPoint(point);
    }
  }

  xSelect.addEventListener("change", () => { if (current) actions.patch({ xMetric: xSelect.value }); }, listen);
  ySelect.addEventListener("change", () => { if (current) actions.patch({ yMetric: ySelect.value }); }, listen);
  frontierToggle.addEventListener("change", () => { frontierEnabled = frontierToggle.checked; render(); }, listen);
  resetAxes.addEventListener("click", () => { if (current) actions.patch({ xMetric: DEFAULT_X, yMetric: DEFAULT_Y }); }, listen);
  for (const [link, details] of [[flightsLink, flights], [rangeLink, rangeDetails]] as const) {
    link.addEventListener("click", event => {
      if (event.button === 0 && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey) details.open = true;
    }, listen);
  }
  tableBody.addEventListener("click", event => {
    const button = event.target instanceof Element ? event.target.closest<HTMLButtonElement>("button[data-select-run]") : null;
    const id = button?.dataset.selectRun;
    if (current && id && rowViews.has(id)) actions.selectRuns([id]);
  }, listen);
  for (const field of fields) field.input.addEventListener("input", () => {
    rangeDirty = true;
    if (rangeValidationShown) validateRange(false);
  }, listen);
  resetRange.addEventListener("click", usePlotBounds, listen);
  rangeForm.addEventListener("submit", event => {
    event.preventDefault();
    if (!current || !visiblePoints.length) return;
    rangeValidationShown = true; rangeDirty = true;
    const range = validateRange(true);
    if (!range) return;
    const ids = visiblePoints.filter(point => point.x >= range.xmin && point.x <= range.xmax
      && point.y >= range.ymin && point.y <= range.ymax).map(point => point.run.id);
    rangeFeedback.textContent = ids.length ? `Selected flights · ${numbers.format(ids.length)}`
      : "No plotted flights in this range. Widen the bounds or use plotted bounds.";
    actions.selectRuns(ids);
  }, listen);

  plot.addEventListener("focusin", event => {
    const point = pointTarget(event.target), id = point?.getAttribute("data-run-id");
    if (point && id) { setActivePoint(id); revealPoint(point); }
  }, listen);
  plot.addEventListener("click", event => {
    const id = pointTarget(event.target)?.getAttribute("data-run-id");
    if (current && event.button === 0 && id && pointViews.has(id)) actions.selectRuns([id]);
  }, listen);
  plot.addEventListener("keydown", event => {
    if (event.key === "Escape" && brush) { event.preventDefault(); cancelBrush(); return; }
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const id = pointTarget(event.target)?.getAttribute("data-run-id");
    if (!current || !id) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault(); event.stopPropagation(); actions.selectRuns([id]); return;
    }
    const horizontal = event.key === "ArrowLeft" || event.key === "ArrowRight";
    const vertical = event.key === "ArrowUp" || event.key === "ArrowDown";
    if (!horizontal && !vertical && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault(); event.stopPropagation();
    const ordered = [...visiblePoints].sort((a, b) => horizontal ? a.px - b.px || a.py - b.py || a.run.id.localeCompare(b.run.id)
      : b.py - a.py || a.px - b.px || a.run.id.localeCompare(b.run.id));
    const index = ordered.findIndex(point => point.run.id === id);
    const step = event.key === "ArrowLeft" || event.key === "ArrowDown" ? -1 : 1;
    const next = event.key === "Home" ? ordered[0] : event.key === "End" ? ordered.at(-1)
      : ordered[(index + step + ordered.length) % ordered.length];
    if (next) focusPoint(next.run.id);
  }, listen);

  plot.addEventListener("pointerdown", event => {
    if (!current || !visiblePoints.length || brush || event.pointerType === "touch" || !event.isPrimary || event.button !== 0 || pointTarget(event.target)) return;
    const position = pointerPosition(event);
    if (!position || position[0] < PLOT.left || position[0] > PLOT.right || position[1] < PLOT.top || position[1] > PLOT.bottom) return;
    try { plot.setPointerCapture(event.pointerId); } catch { return; }
    brush = { pointerId: event.pointerId, x: position[0], y: position[1], clientX: event.clientX, clientY: event.clientY, dragged: false };
    event.preventDefault();
  }, listen);
  plot.addEventListener("pointermove", event => {
    if (!brush || brush.pointerId !== event.pointerId) return;
    const position = pointerPosition(event); if (!position) return;
    brush.dragged ||= Math.hypot(event.clientX - brush.clientX, event.clientY - brush.clientY) >= 4;
    if (!brush.dragged) return;
    const x = Math.max(PLOT.left, Math.min(PLOT.right, position[0]));
    const y = Math.max(PLOT.top, Math.min(PLOT.bottom, position[1]));
    brushRectangle.setAttribute("x", String(Math.min(brush.x, x))); brushRectangle.setAttribute("y", String(Math.min(brush.y, y)));
    brushRectangle.setAttribute("width", String(Math.abs(x - brush.x))); brushRectangle.setAttribute("height", String(Math.abs(y - brush.y)));
    brushRectangle.setAttribute("visibility", "visible"); event.preventDefault();
  }, listen);
  plot.addEventListener("pointerup", event => {
    if (!brush || brush.pointerId !== event.pointerId) return;
    const start = brush, position = pointerPosition(event);
    const dragged = start.dragged || Math.hypot(event.clientX - start.clientX, event.clientY - start.clientY) >= 4;
    cancelBrush();
    if (!position || !dragged || !current) return;
    const x = Math.max(PLOT.left, Math.min(PLOT.right, position[0])), y = Math.max(PLOT.top, Math.min(PLOT.bottom, position[1]));
    const left = Math.min(start.x, x), right = Math.max(start.x, x), top = Math.min(start.y, y), bottom = Math.max(start.y, y);
    const ids = visiblePoints.filter(point => point.px >= left && point.px <= right && point.py >= top && point.py <= bottom)
      .map(point => point.run.id);
    setRangeValues({ xmin: interpolate(xBounds, (left - PLOT.left) / (PLOT.right - PLOT.left)),
      xmax: interpolate(xBounds, (right - PLOT.left) / (PLOT.right - PLOT.left)),
      ymin: interpolate(yBounds, (PLOT.bottom - bottom) / (PLOT.bottom - PLOT.top)),
      ymax: interpolate(yBounds, (PLOT.bottom - top) / (PLOT.bottom - PLOT.top)) });
    rangeDirty = true; rangeValidationShown = false; clearRangeFeedback();
    rangeDetails.open = true;
    rangeFeedback.textContent = ids.length ? `Selected flights · ${numbers.format(ids.length)}`
      : "No plotted flights in this range. Widen the bounds or use plotted bounds.";
    actions.selectRuns(ids);
  }, listen);
  plot.addEventListener("pointercancel", event => { if (brush?.pointerId === event.pointerId) cancelBrush(); }, listen);
  plot.addEventListener("lostpointercapture", event => { if (brush?.pointerId === event.pointerId) cancelBrush(); }, listen);

  return {
    update(state) {
      if (disposed) return;
      const nextKey = JSON.stringify([state.mode, state.seed, state.repetition, state.baseline,
        state.selectedIds, state.outcome, state.xMetric, state.yMetric]);
      const changed = renderedData !== state.data || renderedSupplement !== state.supplement || renderKey !== nextKey;
      current = state;
      renderedData = state.data; renderedSupplement = state.supplement; renderKey = nextKey;
      if (changed) render();
    },
    dispose() {
      if (disposed) return;
      disposed = true; cancelBrush(); events.abort(); pointViews.clear(); rowViews.clear();
      current = null; visiblePoints = []; root.remove();
    },
  };
}
