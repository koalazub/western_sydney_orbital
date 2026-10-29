// Presentation only. The caller supplies validated measurements and native
// receipts; this module never loads data or controls a flight.
import { installChartInspector, type ChartInspectionPoint } from "./chart-inspector.ts";
import { deriveMetricInsight } from "./analytics-insights.ts";
export type OverviewRun = {
  id: string;
  profileId: string;
  label: string;
  seed: number;
  repetition: number | null;
  score: number | null;
  outcome?: string;
  measurement: {
    time_s: number | null;
    range_m: number | null;
    relative_speed_mps: number | null;
    damage_j: number | null;
    fuel_used_kg: number | null;
  };
  analytics: {
    totals: Record<string, number | string | boolean>;
    columns: string[];
    samples: Array<Array<number | null>>;
    sample_count: number;
  } | null;
};

// Extra publisher statistics remain structurally compatible. Median fields
// are optional because the existing publication type exposes them as a record.
export type OverviewProfile = {
  id: string;
  owner: string;
  label: string;
  mean_score: number | null;
  attempts: number;
  successes: number;
  scored_pairs: number;
  median_time_s?: unknown;
  median_fuel_used_kg?: unknown;
  median_damage_j?: unknown;
} & Record<string, unknown>;

type Team = { color: string; dash: string; repeatDash: string };
type Group = {
  id: string;
  owner: string;
  label: string;
  profile?: OverviewProfile;
  runs: OverviewRun[];
  first: OverviewRun;
  team: Team;
};
type Chart = {
  panel: HTMLElement;
  plot: SVGSVGElement;
  subtitle: HTMLElement;
  legend: HTMLUListElement;
  readouts: HTMLUListElement;
  note: HTMLElement;
  guidance: HTMLElement;
  insight: HTMLElement;
  insightScope: HTMLElement;
  data: HTMLDetailsElement;
  table: HTMLDivElement;
  key: string;
  title: string;
  disposeInspection?: () => void;
};
type Point = { time: number; value: number };
type FuelTrace = { group: Group; run: OverviewRun; points: Array<Point | null>; secondary: boolean };
type BarMetric = { key: string; label: string; count: string };
type DurationRow = { group: Group; value: number | null; scope: string; outcome: string };

const TEAMS: Record<string, Team> = {
  bui: { color: "var(--team-bui, #4dd6c1)", dash: "none", repeatDash: "5 3" },
  tom: { color: "var(--team-tom, #8aa7ff)", dash: "8 4", repeatDash: "8 3 2 3" },
  harry: { color: "var(--team-harry, #ffb477)", dash: "2 4", repeatDash: "2 3 2 6" },
  wso: { color: "var(--team-wso, #b8adff)", dash: "10 3 2 3", repeatDash: "10 3 2 3 2 3" },
};
const TEXT = "var(--overview-text, #edf1f7)";
const MUTED = "var(--overview-muted, #9ba7ba)";
const GRID = "var(--overview-border, #303846)";
const LABEL_SIZE = "var(--overview-svg-label, 15px)";
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const numeric = (value: unknown) => finite(value) ? value : null;
const exact = (value: unknown) => finite(value) ? String(value) : "n/a";
const decimal = (value: unknown, places = 1) => finite(value)
  ? new Intl.NumberFormat(undefined, { maximumFractionDigits: places }).format(value) : "n/a";
let instanceCount = 0;

function html<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", content?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}
function svg<K extends keyof SVGElementTagNameMap>(tag: K, attributes: Record<string, string | number> = {}, content?: string): SVGElementTagNameMap[K] {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (content !== undefined) node.textContent = content;
  return node;
}
function label(plot: SVGSVGElement, x: number, y: number, value: string, attributes: Record<string, string | number> = {}) {
  plot.append(svg("text", { x, y, fill: MUTED, "font-size": LABEL_SIZE, class: "overview-tick", ...attributes }, value));
}
function tick(value: number) {
  if (value !== 0 && (Math.abs(value) >= 100000 || Math.abs(value) < 0.01)) return value.toExponential(1);
  return new Intl.NumberFormat(undefined, { maximumSignificantDigits: 3 }).format(value);
}
function runScope(run: OverviewRun) {
  return `seed ${run.seed} · ${run.repetition === null ? "selected run" : `rep ${run.repetition}`}`;
}
function total(run: OverviewRun, key: string) {
  return numeric(run.analytics?.totals[key]);
}
function groupsOf(runs: OverviewRun[], profiles?: OverviewProfile[]): Group[] {
  const grouped = new Map<string, OverviewRun[]>();
  for (const run of runs) {
    const existing = grouped.get(run.profileId);
    if (existing) existing.push(run); else grouped.set(run.profileId, [run]);
  }
  const groups = Array.from(grouped, ([id, selected]) => {
    const profile = profiles?.find(entry => entry.id === id);
    // Use an actual selected repetition, without ranking the measurements.
    const first = selected.find(run => run.repetition === 1) ?? selected[0];
    const parts = first.label.split(" / ");
    const owner = profile?.owner ?? (parts.length > 1 ? parts[0] : first.label);
    const name = profile?.label ?? (parts.length > 1 ? parts.slice(1).join(" / ").replace(/, repetition \d+$/, "") : id);
    return { id, owner, label: name, profile, runs: selected, first, team: TEAMS[owner.toLowerCase()] ?? TEAMS.wso };
  });
  if (profiles) groups.sort((left, right) => profiles.findIndex(profile => profile.id === left.id) - profiles.findIndex(profile => profile.id === right.id));
  return groups;
}
function stat(list: HTMLDListElement, name: string, value: string, source?: unknown) {
  const item = html("div", "pilot-card__stat");
  const result = html("dd", "", value);
  if (finite(source)) result.title = String(source);
  item.append(html("dt", "", name), result);
  list.append(item);
}
function arrivals(runs: OverviewRun[]) {
  const known = ["Station region reached", "Collision damage failure", "Time limit", "success", "failed_damage", "timeout"];
  if (!runs.every(run => run.outcome !== undefined && known.includes(run.outcome))) return "n/a";
  return `${runs.filter(run => run.outcome === "Station region reached" || run.outcome === "success").length}/${runs.length}`;
}
function pilotCard(group: Group) {
  const card = html("article", "pilot-card");
  card.style.setProperty("--pilot-color", group.team.color);
  card.dataset.owner = group.owner.toLowerCase();
  card.append(html("h4", "pilot-card__name", group.owner), html("p", "pilot-card__profile", group.label));
  const profile = group.profile;
  const time = profile ? profile.median_time_s : group.first.measurement.time_s;
  const value = html("p", "pilot-card__value", finite(time) ? `${decimal(time)} s` : "n/a");
  value.title = `Observed flight duration: ${exact(time)} s`;
  card.append(value, html("p", "pilot-card__scope", profile ? "Full cohort · median flight duration" : `Selected run · ${runScope(group.first)}`));
  const stats = html("dl", "pilot-card__stats");
  const fuel = profile ? profile.median_fuel_used_kg : group.first.measurement.fuel_used_kg;
  const damage = profile ? profile.median_damage_j : group.first.measurement.damage_j;
  stat(stats, profile ? "Median fuel · local est." : "Run fuel · local est.", finite(fuel) ? `${decimal(fuel, 2)} kg` : "n/a", fuel);
  stat(stats, profile ? "Median damage" : "Run damage", finite(damage) ? `${decimal(damage)} J` : "n/a", damage);
  stat(stats, "Arrivals", profile ? `${profile.successes}/${profile.attempts}` : arrivals(group.runs));
  card.append(stats);
  return card;
}

function makeChart(prefix: string, key: string, title: string): Chart {
  const panel = html("article", "metric-chart");
  panel.dataset.chart = key;
  const heading = html("h4", "", title);
  heading.id = `${prefix}-${key}-heading`;
  panel.setAttribute("aria-labelledby", heading.id);
  const header = html("header", "metric-chart__header");
  const subtitle = html("p", "metric-chart__subtitle");
  header.append(heading, subtitle);
  const plot = svg("svg", { class: "metric-chart__plot", viewBox: "0 0 640 260" });
  const legend = html("ul", "metric-chart__legend");
  const readouts = html("ul", "metric-chart__readouts");
  const note = html("p", "metric-chart__note");
  const guidance = html("p", "metric-chart__guidance");
  const insight = html("p", "metric-chart__insight");
  const insightScope = html("p", "metric-chart__insight-scope");
  guidance.id = `${prefix}-${key}-guidance`;
  insight.id = `${prefix}-${key}-insight`;
  panel.setAttribute("aria-describedby", `${guidance.id} ${insight.id}`);
  const data = html("details", "metric-chart__data");
  const summary = html("summary", "", "View data");
  const table = html("div", "overview-data-scroll");
  table.tabIndex = 0;
  table.setAttribute("role", "region");
  table.setAttribute("aria-label", `${title} data`);
  data.append(summary, table);
  panel.append(header, guidance, plot, legend, readouts, insight, insightScope, note, data);
  return { panel, plot, subtitle, legend, readouts, note, guidance, insight, insightScope, data, table, key: `${prefix}-${key}`, title };
}
function newPlot(chart: Chart, description: string, height = 260) {
  const plot = svg("svg", {
    class: "metric-chart__plot", viewBox: `0 0 640 ${height}`, role: "img",
    "aria-labelledby": `${chart.key}-title ${chart.key}-description`,
    "font-family": "inherit",
  });
  plot.style.fontVariantNumeric = "tabular-nums";
  plot.append(svg("title", { id: `${chart.key}-title` }, chart.title), svg("desc", { id: `${chart.key}-description` }, description));
  return plot;
}
function replacePlot(chart: Chart, plot: SVGSVGElement, points: ChartInspectionPoint[] = []) {
  chart.disposeInspection?.();
  chart.plot.replaceWith(plot);
  chart.plot = plot;
  chart.disposeInspection = points.length ? installChartInspector(chart.panel, plot, points,
    "Hover for a recorded value. Focus this chart and use the arrow keys to inspect entries.") : undefined;
}
function renderInsight(chart: Chart, groups: Group[]) {
  const metric = chart.panel.dataset.chart as "fuel" | "time" | "thrust" | "turn";
  const insight = deriveMetricInsight(metric, groups);
  chart.guidance.textContent = insight.guidance;
  chart.insight.textContent = insight.observation;
  chart.insightScope.textContent = insight.scope;
}
function plotGeometry(chart: Chart, bodyHeight: (labelUnits: number) => number) {
  const measuredWidth = chart.plot.getBoundingClientRect().width;
  const width = measuredWidth > 0 ? measuredWidth : 640;
  const labelUnits = Math.max(15, 12 * 640 / width);
  chart.panel.style.setProperty("--overview-svg-label", `${labelUnits}px`);
  const top = Math.max(30, 1.8 * labelUnits);
  const bottom = top + bodyHeight(labelUnits);
  return { labelUnits, top, bottom, height: bottom + 3.5 * labelUnits,
    tickBaseline: bottom + 1.4 * labelUnits, titleBaseline: bottom + 3.1 * labelUnits };
}
function emptyPlot(chart: Chart, message: string) {
  const geometry = plotGeometry(chart, units => Math.max(182, 6 * units));
  const plot = newPlot(chart, message, geometry.height);
  const suffix = " for this selection";
  const lines = message.endsWith(suffix) ? [message.slice(0, -suffix.length), "for this selection"] : [message];
  lines.forEach((line, index) => {
    const y = (geometry.top + geometry.bottom) / 2 + (index - (lines.length - 1) / 2) * 1.7 * geometry.labelUnits;
    label(plot, 320, y, line, { "text-anchor": "middle", class: "overview-empty-label" });
  });
  replacePlot(chart, plot);
}
function legendItem(list: HTMLUListElement, text: string, color: string, dash = "none", secondary = false) {
  const item = html("li", "overview-legend-item");
  const swatch = svg("svg", { viewBox: "0 0 30 12", class: "overview-legend-swatch", "aria-hidden": "true" });
  swatch.append(svg("line", { x1: 1, x2: 29, y1: 6, y2: 6, stroke: color, "stroke-width": 3, "stroke-dasharray": dash, opacity: secondary ? 0.65 : 1 }));
  item.append(swatch, html("span", "overview-legend-label", text));
  list.append(item);
}
function dataTable(chart: Chart, caption: string, columns: string[], rows: string[][]) {
  const table = html("table", "overview-data-table");
  table.append(html("caption", "", caption));
  const head = table.createTHead().insertRow();
  for (const name of columns) {
    const cell = html("th", "", name); cell.scope = "col"; head.append(cell);
  }
  const body = table.createTBody();
  for (const values of rows) {
    const row = body.insertRow();
    values.forEach((value, index) => {
      const cell = html(index === 0 ? "th" : "td", "", value);
      if (index === 0) (cell as HTMLTableCellElement).scope = "row";
      row.append(cell);
    });
  }
  chart.table.replaceChildren(table);
}
function selectionCaption(groups: Group[]) {
  const selected = groups.map(group => group.first);
  const seeds = [...new Set(selected.map(run => run.seed))];
  const repetitions = [...new Set(selected.map(run => run.repetition))];
  if (seeds.length === 1 && repetitions.length === 1) return runScope(selected[0]);
  return "First selected repetition per profile; seeds and run IDs in data";
}

function renderFuel(chart: Chart, groups: Group[]) {
  chart.subtitle.textContent = "Cumulative fuel · local estimate, kg";
  chart.legend.replaceChildren(); chart.readouts.replaceChildren();
  const traces: FuelTrace[] = [];
  const rows: string[][] = [];
  for (const group of groups) {
    for (const run of group.runs) {
      const analytics = run.analytics;
      if (!analytics) continue;
      const timeColumn = analytics.columns.indexOf("time_s");
      const fuelColumn = analytics.columns.indexOf("fuel_used_kg");
      if (timeColumn < 0 || fuelColumn < 0) continue;
      const points = analytics.samples.map(row => {
        const time = row[timeColumn], value = row[fuelColumn];
        rows.push([`${group.owner} · ${run.id} · ${runScope(run)}`, exact(time), exact(value)]);
        return finite(time) && finite(value) ? { time, value } : null;
      });
      if (points.some(point => point !== null)) traces.push({ group, run, points, secondary: run !== group.first });
    }
  }
  dataTable(chart, "Actual recorded fuel samples. Null values remain unavailable.", ["Run", "Time, s", "Fuel, kg"], rows);
  const seeds = [...new Set(groups.flatMap(group => group.runs.map(run => run.seed)))];
  chart.note.textContent = `Seed ${seeds.join(", ")} · recorded samples. Repeats stay separate; gaps stay open.`;
  if (!traces.length) { emptyPlot(chart, "Fuel samples unavailable for this selection"); return; }
  const points = traces.flatMap(trace => trace.points.filter((point): point is Point => point !== null));
  const minimumTime = Math.min(...points.map(point => point.time));
  const maximumTime = Math.max(...points.map(point => point.time));
  const maximumFuel = Math.max(...points.map(point => point.value));
  // Only the axis gets a fallback span. No observation is added at zero.
  const timeSpan = maximumTime - minimumTime || 1;
  const fuelSpan = maximumFuel > 0 ? maximumFuel * 1.08 : 1;
  const geometry = plotGeometry(chart, units => Math.max(182, 9 * units));
  const { top, bottom, labelUnits } = geometry;
  const x = (value: number) => 68 + (value - minimumTime) / timeSpan * 550;
  const y = (value: number) => bottom - value / fuelSpan * (bottom - top);
  const plot = newPlot(chart, "Cumulative fuel in kilograms against actual simulation time in seconds. Each repetition is a separate labeled trace. Straight segments connect adjacent recorded points only; nulls break lines. Exact samples are available in View data.", geometry.height);
  const inspection = traces.flatMap(trace => trace.points.filter((point): point is Point => point !== null).map(point => ({
    x: x(point.time), y: y(point.value), series: trace.run.id, title: `${trace.group.owner} / ${trace.group.label} · ${runScope(trace.run)}`,
    values: [{ label: "Time", value: point.time, unit: "s" }, { label: "Fuel used", value: point.value, unit: "kg" }],
  })));
  for (let i = 0; i <= 3; i++) {
    const fuel = fuelSpan * i / 3, time = minimumTime + timeSpan * i / 3;
    plot.append(svg("line", { x1: 68, x2: 618, y1: y(fuel), y2: y(fuel), stroke: GRID, class: "overview-grid-line" }));
    label(plot, 58, y(fuel) + 0.32 * labelUnits, tick(fuel), { "text-anchor": "end" });
    label(plot, x(time), geometry.tickBaseline, tick(time), { "text-anchor": "middle" });
  }
  label(plot, 68, top - 0.8 * labelUnits, "kg", { class: "overview-axis-label" });
  label(plot, 618, geometry.titleBaseline, "Simulation time, s", { "text-anchor": "end", class: "overview-axis-label" });
  // Secondary traces are drawn first so a coincident primary stays visible.
  const drawingOrder = [...traces.filter(trace => trace.secondary), ...traces.filter(trace => !trace.secondary)];
  for (const trace of drawingOrder) {
    const dash = trace.secondary ? trace.group.team.repeatDash : trace.group.team.dash;
    let path = "";
    let segment: Point[] = [];
    const areaSegments: Point[][] = [];
    let connected = false;
    for (const point of trace.points) {
      if (point === null) {
        connected = false;
        if (segment.length) areaSegments.push(segment);
        segment = [];
        continue;
      }
      path += `${connected ? "L" : "M"}${x(point.time)} ${y(point.value)} `;
      segment.push(point);
      connected = true;
    }
    if (segment.length) areaSegments.push(segment);
    if (!trace.secondary) for (const area of areaSegments) {
      const first = area[0], last = area[area.length - 1];
      if (area.length < 2) continue;
      const d = `M${x(first.time)} ${y(0)} ` + area.map(point => `L${x(point.time)} ${y(point.value)}`).join(" ") + ` L${x(last.time)} ${y(0)} Z`;
      plot.append(svg("path", { d, fill: trace.group.team.color, opacity: 0.07, class: "overview-area" }));
    }
    const line = svg("path", { d: path, fill: "none", stroke: trace.group.team.color, "stroke-width": trace.secondary ? 2 : 3, "stroke-dasharray": dash, opacity: trace.secondary ? 0.65 : 1, "vector-effect": "non-scaling-stroke", class: "overview-series", "data-chart-inspection-series": trace.run.id });
    line.append(svg("title", {}, `${trace.group.owner} · ${trace.run.id} · ${runScope(trace.run)}`));
    plot.append(line);
    const available = trace.points.filter((point): point is Point => point !== null);
    available.forEach((point, index) => {
      const endpoint = index === 0 || index === available.length - 1;
      const dot = svg("circle", { cx: x(point.time), cy: y(point.value), r: endpoint ? 3.5 : 1.5, fill: trace.group.team.color, opacity: trace.secondary ? 0.65 : 1, class: "overview-point", "data-chart-inspection-series": trace.run.id });
      dot.append(svg("title", {}, `${trace.group.owner}, ${trace.run.id}: ${exact(point.time)} s, ${exact(point.value)} kg`));
      plot.append(dot);
    });
  }
  for (const trace of traces) legendItem(chart.legend, `${trace.group.owner} · ${trace.run.repetition === null ? trace.run.id : `rep ${trace.run.repetition}`}`, trace.group.team.color,
    trace.secondary ? trace.group.team.repeatDash : trace.group.team.dash, trace.secondary);
  replacePlot(chart, plot, inspection);
}

function renderDuration(chart: Chart, groups: Group[]) {
  const rows: DurationRow[] = groups.map(group => ({ group,
    value: numeric(group.profile ? group.profile.median_time_s : group.first.measurement.time_s),
    scope: group.profile ? "Full cohort · median flight duration" : `Selected run · ${runScope(group.first)}`,
    outcome: group.profile ? `${group.profile.successes}/${group.profile.attempts} arrivals` : group.first.outcome ?? "Outcome unavailable" }));
  const allCohort = groups.every(group => group.profile !== undefined);
  chart.subtitle.textContent = "Observed duration · seconds";
  chart.legend.replaceChildren(); chart.readouts.replaceChildren();
  chart.note.textContent = `${allCohort ? "Full-cohort medians" : "Selected-run observations"} · seconds. Durations may include failures and timeouts, not just arrivals.`;
  dataTable(chart, "Supplied observed flight durations, not completion-time rankings. Cohort medians keep their full-cohort scope; selected runs retain their outcomes.",
    ["Profile", "Scope", "Duration, s", "Arrivals / outcome"], rows.map(row => [`${row.group.owner} / ${row.group.label}`, row.scope, exact(row.value), row.outcome]));
  for (const row of rows) {
    const readout = html("li", "overview-readout");
    readout.style.setProperty("--pilot-color", row.group.team.color);
    readout.append(html("strong", "overview-readout__name", row.group.owner), html("span", "overview-readout__value", row.outcome));
    chart.readouts.append(readout);
  }
  const values = rows.map(row => row.value).filter(finite);
  if (!values.length) { emptyPlot(chart, "Flight durations unavailable for this selection"); return; }
  const largest = Math.max(...values), maximum = largest > 0 ? largest * 1.06 : 1;
  const x = (value: number) => 112 + value / maximum * 374;
  const geometry = plotGeometry(chart, units => Math.max(182, rows.length * Math.max(54, 2.6 * units)));
  const { top, bottom, height, labelUnits } = geometry;
  const rowStep = Math.max(54, 2.6 * labelUnits);
  const barHeight = Math.max(20, 0.8 * labelUnits);
  const plot = newPlot(chart, "Observed flight duration in seconds on a horizontal axis starting at zero. Bars show supplied full-cohort medians or an actual selected run, without recomputing aggregates. Shorter duration does not establish a better outcome: failures and timeouts may be included. Arrivals, outcomes, scope and exact values are available below and in View data. Zero is measured; n/a means unavailable.", height);
  const inspection: ChartInspectionPoint[] = [];
  for (let i = 0; i <= 3; i++) {
    const value = maximum * i / 3;
    plot.append(svg("line", { x1: x(value), x2: x(value), y1: top, y2: bottom, stroke: GRID, class: "overview-grid-line" }));
    label(plot, x(value), geometry.tickBaseline, tick(value), { "text-anchor": "middle" });
  }
  plot.append(svg("line", { x1: x(0), x2: x(0), y1: top, y2: bottom, stroke: MUTED, "stroke-width": 1.5, class: "overview-zero-line" }));
  label(plot, 112, geometry.titleBaseline, "Observed flight duration, s", { class: "overview-axis-label" });
  rows.forEach((row, index) => {
    const y = top + (index + 0.5) * rowStep;
    label(plot, 12, y + 0.32 * labelUnits, row.group.owner, { fill: TEXT, class: "overview-row-label" });
    if (finite(row.value)) {
      const bar = svg("rect", { x: x(0), y: y - barHeight / 2, width: x(row.value) - x(0), height: barHeight, rx: 3, fill: row.group.team.color, opacity: 0.8, class: "overview-duration-bar", "data-chart-inspection-index": inspection.length, "pointer-events": "all" });
      bar.append(svg("title", {}, `${row.group.owner}: ${exact(row.value)} s; ${row.scope}; ${row.outcome}`));
      plot.append(bar);
      plot.append(svg("line", { x1: x(row.value), x2: x(row.value), y1: y - barHeight / 2 - 3, y2: y + barHeight / 2 + 3, stroke: row.group.team.color, "stroke-width": 3, class: "overview-duration-marker" }));
      const marker = svg("circle", { cx: x(row.value), cy: y, r: 4, fill: row.group.team.color, "data-chart-inspection-index": inspection.length });
      marker.append(svg("title", {}, `${exact(row.value)} s`)); plot.append(marker);
      inspection.push({ x: x(row.value), y, title: `${row.group.owner} / ${row.group.label} · ${row.scope}`,
        values: [{ label: "Flight duration", value: row.value, unit: "s" }, { label: "Arrivals / outcome", value: row.outcome }] });
    }
    label(plot, 626, y + 0.32 * labelUnits, finite(row.value) ? `${decimal(row.value)} s` : "n/a", { fill: TEXT, "text-anchor": "end", class: "overview-value" });
  });
  replacePlot(chart, plot, inspection);
}

function renderActivity(chart: Chart, groups: Group[], turning: boolean) {
  const metrics: BarMetric[] = turning ? [
    { key: "turn_duration_s", label: "Physical turning", count: "turn_count" },
    { key: "angular_burn_duration_s", label: "Angular actuation", count: "angular_burn_count" },
  ] : [
    { key: "burn_duration_s", label: "Translation burn", count: "burn_count" },
    { key: "angular_burn_duration_s", label: "Angular actuation", count: "angular_burn_count" },
  ];
  chart.subtitle.textContent = turning ? "Physical motion vs angular thrust · seconds" : "Translation and angular thrust · seconds";
  chart.legend.replaceChildren(); chart.readouts.replaceChildren();
  legendItem(chart.legend, `${metrics[0].label} · solid`, MUTED);
  legendItem(chart.legend, `${metrics[1].label} · dashed outline`, MUTED, "4 3");
  chart.note.textContent = `${selectionCaption(groups)} · native physics-step totals. ${turning ? "Turning can continue without angular thrust." : "Durations overlap; bars are not stacked."}`;
  const tableRows: string[][] = [];
  for (const group of groups) {
    for (const metric of metrics) tableRows.push([`${group.owner} · ${group.first.id} · ${runScope(group.first)}`, `${metric.label} · ${metric.key}`, exact(total(group.first, metric.key)), exact(total(group.first, metric.count))]);
    if (turning) tableRows.push([`${group.owner} · ${group.first.id} · ${runScope(group.first)}`, "Orientation angle · total_turn_angle_rad", exact(total(group.first, "total_turn_angle_rad")), "n/a"]);
    const readout = html("li", "overview-readout");
    readout.style.setProperty("--pilot-color", group.team.color);
    const counts = turning
      ? `${exact(total(group.first, "turn_count"))} turns · ${decimal(total(group.first, "total_turn_angle_rad"), 2)} rad`
      : `${exact(total(group.first, "burn_count"))} translation / ${exact(total(group.first, "angular_burn_count"))} angular episodes`;
    const detail = html("span", "overview-readout__value", counts);
    detail.title = turning ? `turn_count: ${exact(total(group.first, "turn_count"))}; total_turn_angle_rad: ${exact(total(group.first, "total_turn_angle_rad"))}; angular_burn_count: ${exact(total(group.first, "angular_burn_count"))}` : counts;
    readout.append(html("strong", "overview-readout__name", group.owner), detail);
    chart.readouts.append(readout);
  }
  dataTable(chart, turning ? "First selected repetition per profile. Native physical-turn and angular-actuation totals; angle is in radians, durations in seconds." : "First selected repetition per profile. Native actuation durations in seconds and contiguous episode counts.",
    ["Run", "Native metric", "Value", "Episodes"], tableRows);
  const values = groups.flatMap(group => metrics.map(metric => total(group.first, metric.key))).filter(finite);
  if (!values.length) { emptyPlot(chart, "Native activity totals unavailable for this selection"); return; }
  const largest = Math.max(...values), maximum = largest > 0 ? largest * 1.06 : 1;
  const x = (value: number) => 112 + value / maximum * 374;
  const geometry = plotGeometry(chart, units => Math.max(182, groups.length * (2 * Math.max(24, 1.7 * units) + 1.2 * units)));
  const { top, bottom, height, labelUnits } = geometry;
  const pairGap = Math.max(24, 1.7 * labelUnits);
  const groupHeight = 2 * pairGap + 1.2 * labelUnits;
  const barHeight = Math.max(14, 0.75 * labelUnits);
  const plot = newPlot(chart, `${metrics[0].label} and ${metrics[1].label} durations in seconds, grouped rather than stacked. The solid and dashed-outline bars represent distinct native counters, whose durations may overlap. Zero is a measured zero; n/a means unavailable. Native episode counts and ${turning ? "physical orientation angles" : "run identifiers"} follow the plot.`, height);
  const inspection: ChartInspectionPoint[] = [];
  for (let i = 0; i <= 3; i++) {
    const value = maximum * i / 3;
    plot.append(svg("line", { x1: x(value), x2: x(value), y1: top, y2: bottom, stroke: GRID, class: "overview-grid-line" }));
    label(plot, x(value), geometry.tickBaseline, tick(value), { "text-anchor": "middle" });
  }
  label(plot, 112, geometry.titleBaseline, "Duration, s", { class: "overview-axis-label" });
  groups.forEach((group, index) => {
    const rowY = top + (index + 0.5) * groupHeight;
    label(plot, 12, rowY + 0.32 * labelUnits, group.owner, { fill: TEXT, class: "overview-row-label" });
    metrics.forEach((metric, metricIndex) => {
      const value = total(group.first, metric.key);
      const centerY = rowY + (metricIndex - 0.5) * pairGap;
      const y = centerY - barHeight / 2;
      if (finite(value)) {
        const bar = svg("rect", { x: x(0), y, width: x(value) - x(0), height: barHeight, rx: 3,
          fill: metricIndex === 0 ? group.team.color : "none", stroke: group.team.color,
          "stroke-width": 1.5, "stroke-dasharray": metricIndex === 0 ? "none" : "4 3",
          class: metricIndex === 0 ? "overview-activity-bar" : "overview-activity-bar overview-activity-bar--secondary",
          "data-chart-inspection-index": inspection.length, "pointer-events": "all" });
        bar.append(svg("title", {}, `${group.owner}, ${group.first.id}, ${metric.key}: ${exact(value)} s; ${metric.count}: ${exact(total(group.first, metric.count))}`));
        plot.append(bar);
        if (value === 0) plot.append(svg("line", { x1: x(0), x2: x(0), y1: y, y2: y + barHeight, stroke: group.team.color, "stroke-width": 2, "data-chart-inspection-index": inspection.length }));
        inspection.push({ x: x(value), y: centerY, title: `${group.owner} / ${group.label} · ${metric.label} · ${runScope(group.first)}`,
          values: [{ label: metric.label, value, unit: "s" }, { label: "Episodes", value: total(group.first, metric.count) },
            ...(turning ? [{ label: "Orientation angle", value: total(group.first, "total_turn_angle_rad"), unit: "rad" }] : [])] });
      }
      label(plot, 626, centerY + 0.32 * labelUnits, finite(value) ? `${decimal(value)} s` : "n/a", { fill: TEXT, "text-anchor": "end", class: "overview-value" });
    });
  });
  replacePlot(chart, plot, inspection);
}

export function installAnalyticsOverview(container: HTMLElement): { update(runs: OverviewRun[], profiles?: OverviewProfile[]): void } {
  const prefix = `analytics-overview-${++instanceCount}`;
  container.classList.add("analytics-overview");
  const cards = html("div", "overview-cards");
  cards.tabIndex = 0;
  cards.setAttribute("role", "region");
  cards.setAttribute("aria-label", "Controller summaries, scroll horizontally on narrow screens");
  const grid = html("div", "overview-chart-grid");
  const empty = html("div", "overview-empty");
  empty.append(html("h4", "overview-empty__title", "No measured runs in this selection"), html("p", "overview-empty__hint", "Choose another seed or teammate, or run a test."));
  const fuel = makeChart(prefix, "fuel", "Fuel over time");
  const time = makeChart(prefix, "time", "Flight duration");
  const thrust = makeChart(prefix, "thrust", "Thrust activity");
  const turns = makeChart(prefix, "turn", "Turning vs actuation");
  const charts = [fuel, time, thrust, turns];
  grid.append(...charts.map(chart => chart.panel));
  cards.hidden = grid.hidden = true;
  container.replaceChildren(empty, cards, grid);
  let groups: Group[] = [];
  let lastWidthKey = "";
  let rendering = false;
  function renderCharts(force = false) {
    if (rendering || !groups.length) return;
    const widths = charts.map(chart => chart.plot.getBoundingClientRect().width);
    const widthKey = widths.every(width => width > 0) ? widths.map(width => width.toFixed(2)).join(":") : "";
    // A height-only ResizeObserver notification must not rebuild the plots.
    // Hidden updates use fallback geometry; opening the panel supplies a new
    // positive width key and redraws that cached selection at its real width.
    if (!force && (!widthKey || widthKey === lastWidthKey)) return;
    lastWidthKey = widthKey;
    rendering = true;
    try {
      renderFuel(fuel, groups);
      renderDuration(time, groups);
      renderActivity(thrust, groups, false);
      renderActivity(turns, groups, true);
      for (const chart of charts) renderInsight(chart, groups);
    } finally {
      rendering = false;
    }
  }
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => renderCharts()).observe(container);
  return {
    update(runs, profiles) {
      groups = groupsOf(runs, profiles);
      empty.hidden = groups.length > 0;
      cards.hidden = grid.hidden = groups.length === 0;
      cards.replaceChildren(...groups.map(pilotCard));
      if (!groups.length) {
        lastWidthKey = "";
        for (const chart of charts) { chart.disposeInspection?.(); chart.disposeInspection = undefined; }
        return;
      }
      // Selection changes must render even when the width has not changed.
      renderCharts(true);
    },
  };
}
