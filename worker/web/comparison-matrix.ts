import { DEFAULT_VISIBLE_METRICS, formatMetric, getMetric, isEfficiencyMetric, isScoreMetric, metricCatalog, rowsForState, type Metric, type MetricCoverage, type MetricRow,
  type WorkspaceActions, type WorkspaceState, type WorkspaceWidget } from "./comparison-model.ts";
import { finite, outcomeLabel, traceStyle, type PathRun } from "./path-comparison-data.ts";
import { SCORE_CONSTANTS, SCORE_REFERENCE, type ScoreComponents } from "./score-estimate.ts";

const sources: Record<Metric["source"], string> = { recorded: "Recorded", native: "Native", sampled: "Sampled", calculated: "Calculated" };
let instances = 0;

function html<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text?: string) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function svg<K extends keyof SVGElementTagNameMap>(tag: K, attributes: Record<string, string | number>) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  return element;
}

function reconcile(parent: Element, children: Element[]) {
  const wanted = new Set(children);
  for (const child of Array.from(parent.children)) if (!wanted.has(child)) child.remove();
  children.forEach((child, index) => {
    const previous = parent.children.item(index);
    if (previous !== child) parent.insertBefore(child, previous);
  });
}

const sameIds = (a: string[], b: string[]) => a.length === b.length && a.every((id, index) => id === b[index]);
const sameRowInputs = (a: WorkspaceState, b: WorkspaceState) => a.data === b.data && a.supplement === b.supplement
  && a.mode === b.mode && a.seed === b.seed && a.repetition === b.repetition && a.baseline === b.baseline
  && a.outcome === b.outcome;
const readableOutcome = (value: string) => outcomeLabel(value) === "Unavailable" ? value || "Unavailable" : outcomeLabel(value);
const metricName = (metric: Metric) => metric.unit ? `${metric.label}, ${metric.unit}` : metric.label;
const columnName = (metric: Metric) => {
  const label = ({ time_s: "Time", fuel_kg: "Fuel", damage_j: "Damage", relative_speed_mps: "Speed" } as Record<string, string>)[metric.id] ?? metric.label;
  return metric.unit ? `${label} · ${metric.unit}` : label;
};

type MetricHeader = { element: HTMLTableCellElement; button: HTMLButtonElement; arrow: HTMLSpanElement };
type MetricCell = { element: HTMLTableCellElement; value: HTMLElement; delta: HTMLElement; coverage: HTMLElement };
type RowView = {
  row: MetricRow; element: HTMLTableRowElement; identity: HTMLTableCellElement; outcomeCell: HTMLTableCellElement;
  select: HTMLInputElement; inspect: HTMLButtonElement; name: HTMLSpanElement; swatch: SVGSVGElement;
  outcome: HTMLElement; arrivals: HTMLElement;
  cells: Map<string, MetricCell>;
};

export function installMetricMatrix(container: HTMLElement, actions: WorkspaceActions): WorkspaceWidget {
  const prefix = `comparison-matrix-${++instances}`;
  const lifecycle = new AbortController(), listen = { signal: lifecycle.signal };
  const number = new Intl.NumberFormat(), plurals = new Intl.PluralRules();
  const counted = (value: number, one: string, many: string) => `${number.format(value)} ${plurals.select(value) === "one" ? one : many}`;
  const fraction = (count: number, eligible: number) => `${number.format(count)}/${number.format(eligible)}`;
  const runIdentity = (reading: Pick<MetricCoverage["readings"][number], "runId" | "seed" | "repetition">) => `run ${reading.runId} · seed ${number.format(reading.seed)} · repetition ${number.format(reading.repetition)}`;
  const rawValue = (value: number, metric: Metric, delta = false) => `${delta && value > 0 ? "+" : ""}${String(value)}${metric.unit ? ` ${metric.unit}` : ""}`;
  const catalogIds = new Set(metricCatalog.map(metric => metric.id));
  const root = html("section", "comparison-matrix");
  const heading = html("h2", "", "Results"); heading.id = `${prefix}-heading`;
  root.setAttribute("aria-labelledby", heading.id);
  const header = html("div", "comparison-section-header comparison-summary-header");
  const toolbar = html("div", "comparison-toolbar");
  const modeLabel = html("label", "", "View");
  const mode = html("select"); mode.id = mode.name = `${prefix}-mode`; mode.disabled = true;
  modeLabel.htmlFor = mode.id;
  for (const [value, label] of [["cohort", "Summary"], ["trials", "Trials"]]) {
    const option = html("option", "", label); option.value = value; mode.append(option);
  }
  modeLabel.append(mode);

  const picker = html("details", "comparison-metric-picker comparison-details");
  picker.append(html("summary", "", "Columns"));
  const searchLabel = html("label", "", "Find metrics"), search = html("input");
  search.type = "search"; search.id = search.name = `${prefix}-search`; search.placeholder = "e.g. fuel";
  searchLabel.htmlFor = search.id; searchLabel.append(search);
  const useDefaults = html("button", "", "Use default metrics"); useDefaults.type = "button"; useDefaults.disabled = true;
  const pickerStatus = html("p", "comparison-muted"); pickerStatus.setAttribute("role", "status");
  const pickerGroups = html("div", "comparison-metric-options");
  const groups = new Map<string, { element: HTMLFieldSetElement; items: Array<{ metric: Metric; label: HTMLLabelElement }> }>();
  const checks = new Map<string, HTMLInputElement>();
  for (const metric of metricCatalog) {
    let group = groups.get(metric.group);
    if (!group) {
      const element = html("fieldset"); element.append(html("legend", "", metric.group));
      group = { element, items: [] }; groups.set(metric.group, group); pickerGroups.append(element);
    }
    const label = html("label"), check = html("input"); check.type = "checkbox";
    check.id = `${prefix}-metric-${metric.id}`; check.name = `${prefix}-metrics`; check.value = metric.id; check.disabled = true;
    label.htmlFor = check.id; label.append(check, document.createTextNode(metricName(metric)));
    if (metric.id === "score_points") label.append(html("small", "comparison-muted", "Always shown"));
    group.element.append(label); group.items.push({ metric, label }); checks.set(metric.id, check);
    check.addEventListener("change", () => {
      if (!current || metric.id === "score_points") return;
      const visible = Array.from(new Set(current.visibleMetrics)).filter(id => catalogIds.has(id) && id !== metric.id);
      if (check.checked) {
        const previousIndex = current.visibleMetrics.indexOf(metric.id);
        visible.splice(previousIndex < 0 ? visible.length : previousIndex, 0, metric.id);
      }
      actions.patch({ visibleMetrics: visible });
    }, listen);
  }
  const noMatches = html("div", "comparison-empty"); noMatches.hidden = true;
  const noMatchesText = html("p"), clearSearch = html("button", "", "Clear search"); clearSearch.type = "button";
  noMatches.append(noMatchesText, clearSearch);
  const clearSelection = html("button", "", "Clear selection"); clearSelection.type = "button"; clearSelection.disabled = true;
  const pickerActions = html("div", "comparison-picker-actions"); pickerActions.append(useDefaults, clearSelection);
  picker.append(searchLabel, pickerStatus, pickerGroups, noMatches, pickerActions);
  toolbar.append(modeLabel, picker); header.append(heading, toolbar);
  const scope = html("p", "comparison-visually-hidden"); scope.id = `${prefix}-scope`;
  const scoreNote = html("p", "comparison-score-note", "Local estimate · STP formula, local measurements");
  const scoreAggregation = html("span", "comparison-score-aggregation"); scoreNote.append(scoreAggregation);
  scoreNote.id = `${prefix}-score-note`;
  const status = html("p", "comparison-live"); status.setAttribute("role", "status"); status.setAttribute("aria-atomic", "true");

  const scroll = html("div", "comparison-table-scroll"); scroll.tabIndex = 0;
  scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", "Metrics, scroll for all columns");
  const table = html("table", "comparison-metric-table"); table.setAttribute("aria-describedby", `${scope.id} ${scoreNote.id}`);
  const caption = table.createCaption(); caption.className = "comparison-visually-hidden";
  const head = table.createTHead().insertRow(), body = table.createTBody();
  const controllerHead = html("th"), outcomeHead = html("th", "", "Arrived");
  const selectAllLabel = html("label", "comparison-row-select"), selectAll = html("input");
  selectAll.type = "checkbox"; selectAll.id = selectAll.name = `${prefix}-select-all`; selectAll.disabled = true;
  selectAll.dataset.selectAll = ""; selectAllLabel.htmlFor = selectAll.id;
  selectAllLabel.append(selectAll, html("span", "comparison-visually-hidden", "Compare all controllers in this view"));
  const controllerHeading = html("div", "comparison-row-identity"); controllerHeading.append(selectAllLabel, document.createTextNode("Controller"));
  controllerHead.append(controllerHeading);
  controllerHead.scope = outcomeHead.scope = "col"; head.append(controllerHead, outcomeHead); scroll.append(table);
  const noMetrics = html("div", "comparison-empty"); noMetrics.hidden = true;
  const restoreMetrics = html("button", "", "Use default metrics"); restoreMetrics.type = "button";
  noMetrics.append(html("p", "", "No metrics selected."), restoreMetrics);
  const empty = html("div", "comparison-empty"); empty.hidden = true;
  const emptyText = html("p"), showAll = html("button", "", "Show all outcomes"); showAll.type = "button";
  empty.append(emptyText, showAll);

  const coverage = html("details", "comparison-inline-help comparison-metric-coverage comparison-details");
  coverage.append(html("summary", "", "Metric details"));
  const coverageIntro = html("p", "comparison-muted"), nativeNote = html("p", "comparison-muted"), timingNote = html("p", "comparison-muted");
  const coverageScroll = html("div", "comparison-table-scroll"); coverageScroll.tabIndex = 0;
  coverageScroll.setAttribute("role", "region"); coverageScroll.setAttribute("aria-label", "Metric coverage, source run readings and paired differences, scroll for all columns");
  const coverageTable = html("table", "comparison-metric-table"), coverageCaption = coverageTable.createCaption();
  const coverageHead = coverageTable.createTHead().insertRow(), coverageBody = coverageTable.createTBody();
  coverageScroll.append(coverageTable);
  const aboutNote = html("p", "comparison-muted", "Points use means of per-run terminal estimates, including damage failures. Other summary values are medians: efficiency uses arrivals only, context uses attempts in the outcome filter. Other metric Δ values are medians of matched seed/repetition differences; efficiency pairs need two arrivals. Whole-flight values ignore the path interval. n/a means unavailable.");
  const selectionNote = html("p", "comparison-muted", "Checkboxes add or remove source runs. A controller name selects only that controller. The baseline stays unchanged.");
  const definitions = html("dl"), outcomeDetails = html("ul", "comparison-outcome-details");
  const scoreMethod = html("div", "comparison-score-method");
  const scorePopulation = html("p", "comparison-muted");
  const constants = SCORE_CONSTANTS;
  const formula = html("p", "", `Success points = ${constants.points_scale} × (${constants.nominal_time_s}/T − ${constants.damage_multiplier}D/${constants.impact_threshold_j} − max(F/${constants.nominal_fuel_kg}, 0) + ${constants.score_offset}). At T = 0, the time term is ${constants.zero_time_bonus}. Damage failure replaces the time term with −r/r₀.`);
  const formulaSource = html("a", "", `Published formula ${SCORE_REFERENCE.source_commit.slice(0, 7)}, opens in a new tab`);
  formulaSource.href = `https://github.com/SimDynamX/STU4-Sim/blob/${SCORE_REFERENCE.source_commit}/${SCORE_REFERENCE.source_file}`;
  formulaSource.target = "_blank"; formulaSource.rel = "noopener noreferrer";
  scoreMethod.append(html("h3", "", "Local score estimate"),
    html("p", "", "Current-formula re-evaluation of saved local final counters. These are not new flights, historical score receipts, official STP results or a certified leaderboard. Historical L2 is a stress/practice world, not the competition hop."),
    formula, html("p", "", `Success requires damage ≤ ${constants.impact_threshold_j} J and any supplied range ≤ ${constants.arrival_radius_m} m. Damage failures require damage > ${constants.impact_threshold_j} J and a positive recorded time-zero range for r₀. Timeouts, errors and missing required inputs stay n/a. No route-length substitute or zero filling.`),
    scorePopulation, html("p", "", `Formula ${SCORE_REFERENCE.formula_id} · reviewed ${SCORE_REFERENCE.reviewed_on}`), formulaSource);
  const scoreBreakdown = html("div", "comparison-table-scroll comparison-score-breakdown"); scoreBreakdown.tabIndex = 0;
  scoreBreakdown.setAttribute("role", "region"); scoreBreakdown.setAttribute("aria-label", "Local score breakdown in points, scroll for all columns");
  const scoreTable = html("table", "comparison-metric-table"); scoreTable.createCaption().textContent = "Local estimates and their scoring population, in points";
  const scoreHead = scoreTable.createTHead().insertRow(), scoreBody = scoreTable.createTBody();
  for (const title of ["Controller or source", "Progress basis", "Progress", "Damage", "Fuel", "Offset", "Points", "Population"]) {
    const cell = html("th", "", title); cell.scope = "col"; scoreHead.append(cell);
  }
  scoreBreakdown.append(scoreTable);
  coverage.append(scoreMethod, scoreBreakdown, aboutNote, coverageIntro, nativeNote, timingNote, coverageScroll, definitions,
    html("h3", "", "Outcomes and sources"), outcomeDetails, selectionNote);
  root.append(header, scoreNote, scope, status, noMetrics, scroll, empty, coverage); container.append(root);

  let current: WorkspaceState | null = null, rendered: WorkspaceState | null = null, disposed = false;
  let modelRows: MetricRow[] = [], displayedRows: MetricRow[] = [], visibleMetrics: Metric[] = [];
  let sortMetric: string | null = "score_points", sortDirection: "ascending" | "descending" = "descending";
  const headers = new Map<string, MetricHeader>(), views = new Map<string, RowView>();

  function filterPicker() {
    const query = search.value.trim().toLocaleLowerCase();
    let matching = 0;
    for (const group of groups.values()) {
      let groupMatches = 0;
      for (const { metric, label } of group.items) {
        const matches = !query || [metric.label, metric.id, metric.unit, metric.group, metric.description]
          .some(value => value.toLocaleLowerCase().includes(query));
        label.hidden = !matches; if (matches) groupMatches++;
      }
      group.element.hidden = groupMatches === 0; matching += groupMatches;
    }
    const text = query ? counted(matching, "matching metric", "matching metrics")
      : `${number.format(visibleMetrics.length)}/${number.format(metricCatalog.length)} metrics selected`;
    if (pickerStatus.textContent !== text) pickerStatus.textContent = text;
    noMatches.hidden = matching !== 0;
    noMatchesText.textContent = matching ? "" : `No metrics match "${search.value.trim()}".`;
  }

  function defaults() { if (current) actions.patch({ visibleMetrics: DEFAULT_VISIBLE_METRICS.filter(id => catalogIds.has(id)) }); }
  mode.addEventListener("change", () => { if (current) actions.patch({ mode: mode.value === "cohort" ? "cohort" : "trials" }); }, listen);
  search.addEventListener("input", filterPicker, listen);
  clearSearch.addEventListener("click", () => { search.value = ""; filterPicker(); search.focus(); }, listen);
  useDefaults.addEventListener("click", defaults, listen); restoreMetrics.addEventListener("click", defaults, listen);
  clearSelection.addEventListener("click", () => { if (current) actions.selectRuns([]); }, listen);
  selectAll.addEventListener("change", () => {
    if (!current) return;
    const selected = new Set(current.selectedIds);
    for (const row of displayedRows) for (const run of row.runs) {
      if (selectAll.checked) selected.add(run.id); else selected.delete(run.id);
    }
    actions.selectRuns([...selected]);
  }, listen);
  showAll.addEventListener("click", () => { if (current) actions.patch({ outcome: "all" }); }, listen);
  coverage.addEventListener("toggle", () => { if (coverage.open && current) renderCoverage(); }, listen);

  function keepFocus(change: () => void) {
    const active = document.activeElement;
    const focused = active instanceof HTMLElement && root.contains(active) ? active : null;
    const rowId = focused?.closest<HTMLElement>(".comparison-row")?.dataset.rowId;
    const left = scroll.scrollLeft, top = scroll.scrollTop;
    change();
    if (focused) {
      const usable = focused.isConnected && !focused.matches(":disabled") && !focused.closest("[hidden]");
      if (!usable || document.activeElement !== focused) {
        const fallback = rowId ? views.get(rowId)?.select : undefined;
        const target = usable ? focused : fallback ?? (!empty.hidden && !showAll.hidden ? showAll : mode);
        target.focus({ preventScroll: true });
      }
    }
    scroll.scrollLeft = left; scroll.scrollTop = top;
  }

  function metricHeader(metric: Metric) {
    let view = headers.get(metric.id);
    if (!view) {
      const element = html("th"); element.scope = "col";
      element.classList.toggle("comparison-score-cell", metric.id === "score_points");
      const button = html("button"), arrow = html("span"); button.type = "button"; arrow.setAttribute("aria-hidden", "true");
      button.append(document.createTextNode(`${columnName(metric)} `), arrow); element.append(button);
      view = { element, button, arrow }; headers.set(metric.id, view);
      button.addEventListener("click", () => {
        if (!current) return;
        sortDirection = sortMetric === metric.id ? sortDirection === "ascending" ? "descending" : "ascending"
          : metric.direction === "max" ? "descending" : "ascending";
        sortMetric = metric.id; render();
      }, listen);
    }
    const sorted = sortMetric === metric.id;
    if (sorted) view.element.setAttribute("aria-sort", sortDirection); else view.element.removeAttribute("aria-sort");
    view.arrow.textContent = sorted ? sortDirection === "ascending" ? "↑" : "↓" : "↕";
    const next = sorted && sortDirection === "ascending" ? "descending" : "ascending";
    view.button.setAttribute("aria-label", `${columnName(metric)}, sort ${next}${isEfficiencyMetric(metric) ? ", arrivals first" : ""}`);
    return view.element;
  }

  function rowView(row: MetricRow) {
    let view = views.get(row.id);
    if (view) { view.row = row; return view; }
    const element = html("tr", "comparison-row"); element.dataset.rowId = row.id;
    const identity = html("th"); identity.scope = "row";
    const identityContent = html("div", "comparison-row-identity");
    const selectLabel = html("label", "comparison-row-select"), select = html("input"); select.type = "checkbox";
    select.id = `${prefix}-select-${row.id}`; select.name = `${prefix}-controllers`; select.value = row.id;
    select.dataset.selectController = row.algorithm; selectLabel.htmlFor = select.id;
    selectLabel.append(select, html("span", "comparison-visually-hidden", `Compare ${row.label}`));
    const inspect = html("button", "comparison-row-name"), name = html("span"); inspect.type = "button";
    const swatch = svg("svg", { viewBox: "0 0 32 10", width: 32, height: 10, class: "comparison-swatch", "aria-hidden": "true", focusable: "false" });
    inspect.append(swatch, name); identityContent.append(selectLabel, inspect); identity.append(identityContent);
    const outcomeCell = html("td"), outcome = html("small", "comparison-trial-outcome"), arrivals = html("span", "comparison-arrival-count");
    outcomeCell.append(arrivals, outcome); element.append(identity, outcomeCell);
    view = { row, element, identity, outcomeCell, select, inspect, name, swatch, outcome, arrivals, cells: new Map() };
    views.set(row.id, view);
    const created = view;
    select.addEventListener("change", () => {
      if (!current || !created.row.runs.length) return;
      const selected = new Set(current.selectedIds);
      for (const run of created.row.runs) {
        if (select.checked) selected.add(run.id); else selected.delete(run.id);
      }
      actions.selectRuns([...selected]);
    }, listen);
    inspect.addEventListener("click", () => {
      if (!current || !created.row.runs.length) return;
      const pair = created.row.runs.find(run => run.seed === current!.seed && run.repetition === current!.repetition)
        ?? created.row.runs[0];
      actions.patch({ selectedIds: created.row.runs.map(run => run.id), seed: pair.seed, repetition: pair.repetition, anchorId: pair.id });
    }, listen);
    return view;
  }

  function updateSelection() {
    if (!current) return;
    const selected = new Set(current.selectedIds);
    for (const view of views.values()) {
      const count = view.row.runs.filter(run => selected.has(run.id)).length;
      const all = count > 0 && count === view.row.runs.length;
      view.element.classList.toggle("is-selected", count > 0);
      view.select.checked = all; view.select.indeterminate = count > 0 && !all;
      view.select.disabled = !view.row.runs.length;
    }
    const visibleIds = new Set(displayedRows.flatMap(row => row.runs.map(run => run.id)));
    const visibleSelected = [...visibleIds].filter(id => selected.has(id)).length;
    selectAll.checked = visibleIds.size > 0 && visibleSelected === visibleIds.size;
    selectAll.indeterminate = visibleSelected > 0 && visibleSelected < visibleIds.size;
    selectAll.disabled = visibleIds.size === 0;
    const sorted = visibleMetrics.find(metric => metric.id === sortMetric);
    const partitioned = !!sorted && isEfficiencyMetric(sorted);
    const stoppedRows = partitioned && displayedRows.some(row => row.arrivals === 0 && row.attempts > 0);
    const text = `${counted(displayedRows.length, "controller", "controllers")} · ${counted(current.selectedIds.length, "selected trial", "selected trials")}`
      + (sorted ? ` · ${sorted.label}: ${sortDirection}, n/a last${partitioned ? " within outcome groups" : ""}` : "")
      + (stoppedRows ? " · Arrivals first; stopped runs follow" : "");
    if (status.textContent !== text) status.textContent = text;
  }

  function scoreCoverageText(row: MetricRow) {
    const score = row.score;
    return score.aggregation === "seed_pair_mean" ? `${fraction(score.seedMeans.length, score.eligibleSeeds)} seed pairs`
      : `${fraction(score.readings.length, score.eligibleRuns)} scored`;
  }
  function scoreDeltaCoverageText(row: MetricRow) {
    const score = row.score;
    return score.aggregation === "seed_pair_mean" ? `${fraction(score.matchedSeedMeans.length, score.eligibleSeeds)} seed pairs`
      : `${fraction(score.pairs.length, score.eligibleRuns)} paired`;
  }
  function unscoredReason(run: PathRun) {
    if (run.outcome !== "success" && run.outcome !== "failed_damage") return `No terminal formula for ${readableOutcome(run.outcome).toLocaleLowerCase()}`;
    if (!finite(run.final.damage_j)) return "Missing final damage";
    if (!finite(run.final.fuel_kg)) return "Missing final fuel";
    if (run.outcome === "success") {
      if (!finite(run.final.time_s)) return "Missing final time";
      if (run.final.damage_j > constants.impact_threshold_j) return "Recorded success exceeds the current damage threshold";
      if (finite(run.final.range_m) && run.final.range_m > constants.arrival_radius_m) return "Recorded arrival is outside the current arrival radius";
    } else {
      if (run.final.damage_j <= constants.impact_threshold_j) return "Recorded damage failure does not exceed the current threshold";
      if (!finite(run.final.range_m)) return "Missing final range";
      const initial = run.samples.find(sample => sample[0] === 0)?.[4];
      if (!finite(initial) || initial <= 0) return "Missing positive recorded time-zero range";
    }
    return "Invalid inputs or non-finite score arithmetic";
  }
  function renderScoreBreakdown() {
    if (!current) return;
    const elements: HTMLTableRowElement[] = [];
    function append(row: MetricRow, scope: string, identity: string, basis: string, components: ScoreComponents | null, population: string, runId?: string) {
      const element = html("tr"); element.dataset.rowId = row.id; element.dataset.scoreScope = scope;
      if (runId) element.dataset.runId = runId;
      const name = html("th", "", identity); name.scope = "row";
      element.append(name, html("td", "", basis));
      for (const [field, sign] of [["progress_points", 1], ["collision_penalty_points", -1], ["fuel_penalty_points", -1], ["offset_points", 1], ["total", 1]] as const) {
        const cell = html("td"); cell.dataset.scoreComponent = field;
        const value = components ? sign * components[field] : null;
        const text = formatMetric(value, "score_points", field !== "total");
        cell.append(html(field === "total" ? "strong" : "span", "", text));
        if (components) { cell.dataset.scoreValue = String(components[field]); cell.title = String(value); }
        element.append(cell);
      }
      element.append(html("td", "", population)); elements.push(element);
    }
    for (const row of displayedRows) {
      const score = row.score, included = new Set(score.contributingRunIds);
      const summary = score.aggregation === "seed_pair_mean" ? "Local cohort mean of complete seed-pair means"
        : score.aggregation === "terminal_run_mean" ? "Local mean of scored terminal runs" : "Actual terminal trial";
      append(row, "summary", row.label, summary, score.components,
        `${scoreCoverageText(row)} · ${fraction(included.size, score.eligibleRuns)} runs used`);
      for (const pair of score.seedMeans) append(row, "seed-pair", `${row.label} · seed ${number.format(pair.seed)}`, "Two-run mean", pair.components,
        `Repetitions 1 and 2 · ${pair.runIds.join(" · ")}`);
      for (const run of row.runs) {
        const reading = score.readings.find(reading => reading.runId === run.id);
        const basis = reading ? reading.estimate.progress_basis === "time_ratio" ? "Time ratio" : "Remaining-distance ratio" : "Not scored";
        const use = reading ? included.has(run.id) ? "Used" : "Excluded: incomplete seed pair" : unscoredReason(run);
        append(row, "trial", `${row.label} · ${runIdentity({ runId: run.id, seed: run.seed, repetition: run.repetition })}`, basis,
          reading?.estimate ?? null, `${readableOutcome(run.outcome)} · ${use}`, run.id);
      }
      for (const pair of score.matchedSeedMeans) append(row, "seed-pair-delta", `${row.label} · seed ${number.format(pair.seed)} · Δ`, "Matched two-run mean difference", pair.components,
        `${pair.runIds.join(" · ")} vs baseline ${pair.baselineIds.join(" · ")}`);
    }
    scoreBody.replaceChildren(...elements);
  }

  function renderCoverage() {
    if (!current) return;
    const summary = current.mode === "cohort";
    coverageCaption.textContent = summary ? "Coverage of whole-flight summary metrics" : "Coverage of whole-flight trial metrics";
    const headings = ["Controller", "Metric", "Runs with value", "Paired runs", "Source", "Measured timing calls", "Source run readings", "Paired differences"];
    if (summary) headings.push("Run spread");
    coverageHead.replaceChildren(...headings.map(text => {
      const cell = html("th", "", text); cell.scope = "col"; return cell;
    }));
    const domains = new Map<string, [number, number]>();
    if (summary) for (const metric of visibleMetrics) {
      const values = displayedRows.flatMap(row => row.coverage[metric.id].readings.map(reading => reading.value)).filter(finite);
      if (values.length) domains.set(metric.id, [Math.min(...values), Math.max(...values)]);
    }
    const rows: HTMLTableRowElement[] = [];
    for (const row of displayedRows) for (const metric of visibleMetrics) {
      const element = html("tr", "comparison-coverage-row"), style = traceStyle(row.algorithm);
      element.dataset.rowId = row.id; element.dataset.metricId = metric.id;
      const controller = html("th", "", `${style ? `${style.number}. ` : ""}${row.label}`); controller.scope = "row";
      const supplied = row.coverage[metric.id];
      const coverageText = `${fraction(supplied.readings.length, supplied.eligible)} ${supplied.population}`
        + (isScoreMetric(metric) ? ` · ${fraction(row.score.contributingRunIds.length, row.score.eligibleRuns)} used` : "");
      const pairedText = isScoreMetric(metric) ? scoreDeltaCoverageText(row)
        : `${fraction(supplied.pairs.length, supplied.pairedEligible)} eligible runs paired`;
      const source = (metric.source === "native" || metric.group === "Runtime") && !current.supplement
        ? `${sources[metric.source]} · not published` : sources[metric.source];
      const timingText = metric.group === "Runtime"
        ? finite(supplied.timingCalls) ? number.format(supplied.timingCalls) : "n/a" : "Not applicable";
      const readings = html("td", "comparison-coverage-readings"), differences = html("td", "comparison-coverage-differences");
      for (const reading of supplied.readings) readings.append(html("div", "", `${runIdentity(reading)}: ${rawValue(reading.value, metric)}`));
      if (!supplied.readings.length) readings.textContent = supplied.eligible === 0 ? `No eligible ${supplied.population}` : "No supplied readings";
      for (const pair of supplied.pairs) differences.append(html("div", "", `${runIdentity(pair)} vs baseline run ${pair.baselineId}: Δ ${rawValue(pair.delta, metric, true)}`));
      if (!supplied.pairs.length) differences.textContent = "No valid paired differences";
      for (const cell of [readings, differences]) {
        cell.style.whiteSpace = "normal"; cell.style.maxWidth = "56ch"; cell.style.overflowWrap = "anywhere"; cell.style.textAlign = "start";
      }
      element.append(controller, html("td", "", metricName(metric)), html("td", "comparison-coverage-runs", coverageText),
        html("td", "comparison-coverage-pairs", pairedText), html("td", "", source), html("td", "comparison-coverage-timing-calls", timingText), readings, differences);
      if (summary) {
        const cell = html("td", "comparison-coverage-spread"), domain = domains.get(metric.id);
        const values = supplied.readings.map(reading => reading.value);
        if (style && domain && domain[1] > domain[0] && values.length > 1 && values.some(value => value !== values[0])) {
          const spread = svg("svg", { viewBox: "0 0 84 14", width: 84, height: 14, class: "comparison-metric-spread", role: "img", focusable: "false",
            "aria-label": `${metricName(metric)} by source run. ${supplied.readings.map(reading => `${runIdentity(reading)}: ${rawValue(reading.value, metric)}`).join("; ")}` });
          spread.append(svg("line", { x1: 4, x2: 80, y1: 7, y2: 7, stroke: "var(--lab-border)", "stroke-width": 1 }),
            ...values.map(value => svg("circle", { cx: 4 + 76 * (value - domain[0]) / (domain[1] - domain[0]), cy: 7, r: 2, fill: style.color })));
          cell.append(spread);
        } else cell.textContent = "n/a";
        element.append(cell);
      }
      rows.push(element);
    }
    coverageBody.replaceChildren(...rows);
    renderScoreBreakdown();
    outcomeDetails.replaceChildren(...displayedRows.map(row => {
      const item = html("li"), outcomes = new Map<string, number>();
      for (const run of row.runs) outcomes.set(run.outcome, (outcomes.get(run.outcome) ?? 0) + 1);
      const breakdown = Array.from(outcomes, ([value, count]) => `${number.format(count)} ${readableOutcome(value).toLocaleLowerCase()}`).join(" · ");
      item.append(html("p", "", `${row.label}${row.owner ? ` · ${row.owner}` : ""} · ${breakdown}`));
      const runs = html("ul", "comparison-source-runs");
      for (const run of row.runs) runs.append(html("li", "", `${runIdentity({ runId: run.id, seed: run.seed, repetition: run.repetition })} · ${readableOutcome(run.outcome)}. Run SHA-256 ${run.source_sha256.run}. Trace SHA-256 ${run.source_sha256.trace}.`));
      item.append(runs); return item;
    }));
  }

  function render() {
    if (!current || disposed) return;
    const state = current;
    keepFocus(() => {
      visibleMetrics = Array.from(new Set(["score_points", ...state.visibleMetrics])).filter(id => catalogIds.has(id)).map(id => getMetric(id)!);
      if (!visibleMetrics.some(metric => metric.id === sortMetric)) sortMetric = null;
      mode.value = state.mode; mode.disabled = useDefaults.disabled = clearSelection.disabled = false;
      const chosen = new Set(visibleMetrics.map(metric => metric.id));
      for (const [id, check] of checks) { check.checked = chosen.has(id); check.disabled = id === "score_points"; }
      filterPicker();
      const baselineRun = state.data.runs.find(run => run.algorithm === state.baseline), baselineStyle = traceStyle(state.baseline);
      const baselineName = `${baselineStyle ? `${baselineStyle.number}. ` : ""}${baselineRun?.label ?? state.baseline}`;
      const pairSummary = state.mode === "cohort" && state.data.comparison.repetitions === 2;
      const aggregation = state.mode === "trials" ? "Actual terminal trial"
        : pairSummary ? "Local cohort mean · complete two-run seed pairs" : "Terminal-run mean · not a two-run result";
      scoreAggregation.textContent = ` · ${aggregation}${state.data.id === "team" ? " · Historical L2 stress/practice" : ""}`;
      scorePopulation.textContent = pairSummary
        ? "Score each run first, then average repetitions 1 and 2 of the same controller and seed. Both runs must be scorable and present in the outcome filter; damage failures count. The local cohort summary is the mean of complete seed-pair means. Δ uses only complete candidate and baseline pairs from the same seeds. Component means use exactly the same population as total points. This is not an official submission aggregate."
        : state.mode === "cohort"
          ? `This publication has ${number.format(state.data.comparison.repetitions)} repetitions per seed. Points are the arithmetic mean of scorable terminal runs, including damage failures, not an official two-run result. Each run is scored before averaging. Δ is the mean of actual matched trial differences. Component means use exactly the same scoring population. Unscored attempts remain in the coverage denominator.`
          : "Each estimate uses this actual trial's saved terminal measurements. Δ requires a scorable baseline trial with the same seed and repetition. Damage failures count; errors and timeouts remain unavailable.";
      scope.textContent = `${state.mode === "cohort" ? "Whole-flight summary across all seeds and repetitions" : `Whole-flight trials for seed ${number.format(state.seed)}, repetition ${number.format(state.repetition)}`}. Baseline: ${baselineName}.${state.difference ? " Differences use matched seed/repetition pairs." : ""}`;
      caption.textContent = state.mode === "cohort" ? "Summary of whole-flight metrics" : "Whole-flight trial metrics";
      reconcile(head, [controllerHead, metricHeader(visibleMetrics[0]), outcomeHead, ...visibleMetrics.slice(1).map(metricHeader)]);

      displayedRows = modelRows.slice();
      if (sortMetric) {
        const key = sortMetric, direction = sortDirection === "ascending" ? 1 : -1;
        displayedRows.sort((a, b) => {
          if (isEfficiencyMetric(key) && (a.arrivals > 0) !== (b.arrivals > 0)) return a.arrivals > 0 ? -1 : 1;
          const av = a.values[key], bv = b.values[key];
          if (!finite(av)) return finite(bv) ? 1 : 0;
          if (!finite(bv)) return -1;
          return direction * (av - bv);
        });
      }
      const liveRows = new Set(displayedRows.map(row => row.id));
      for (const [id, view] of views) if (!liveRows.has(id)) { view.element.remove(); views.delete(id); }
      const elements = displayedRows.map(row => {
        const view = rowView(row), style = traceStyle(row.algorithm), isBaseline = row.algorithm === state.baseline;
        const name = `${style ? `${style.number}. ` : ""}${row.label}`;
        view.name.textContent = name;
        view.inspect.setAttribute("aria-label", `${name}, inspect only this controller's saved path`);
        view.swatch.toggleAttribute("hidden", !style);
        if (style) {
          view.element.style.setProperty("--trace-color", style.color);
          view.swatch.replaceChildren(svg("line", { x1: 0, x2: 32, y1: 5, y2: 5, stroke: style.color, "stroke-width": 3, "stroke-dasharray": style.dash }));
        } else view.element.style.removeProperty("--trace-color");
        view.element.classList.toggle("is-baseline", isBaseline);
        view.outcome.hidden = state.mode === "cohort" || row.outcome === "success";
        view.outcome.textContent = readableOutcome(row.outcome);
        const arrivalValue = html("span", "comparison-arrival-value", fraction(row.arrivals, row.attempts));
        arrivalValue.setAttribute("aria-hidden", "true");
        view.arrivals.replaceChildren(arrivalValue,
          html("span", "comparison-visually-hidden", `${number.format(row.arrivals)} of ${number.format(row.attempts)} arrived`));
        const cells = visibleMetrics.map(metric => {
          let cell = view.cells.get(metric.id);
          if (!cell) {
            const element = html("td"), value = html("strong", "comparison-metric-value"), delta = html("small", "comparison-delta");
            const coverage = html("small", "comparison-score-coverage");
            element.dataset.metricId = metric.id;
            element.classList.toggle("comparison-score-cell", metric.id === "score_points");
            element.append(value, coverage, delta); cell = { element, value, delta, coverage }; view.cells.set(metric.id, cell);
          }
          const supplied = row.coverage[metric.id];
          cell.value.textContent = formatMetric(finite(row.values[metric.id]) ? row.values[metric.id] : null, metric);
          cell.value.title = finite(row.values[metric.id]) ? rawValue(row.values[metric.id]!, metric) : "Unavailable";
          cell.coverage.hidden = metric.id !== "score_points" || state.mode !== "cohort";
          cell.coverage.textContent = scoreCoverageText(row);
          cell.coverage.title = `${fraction(row.score.readings.length, row.score.eligibleRuns)} source runs scored; ${fraction(row.score.contributingRunIds.length, row.score.eligibleRuns)} runs used in this estimate`;
          if (metric.id === "score_points" && state.mode === "cohort") cell.coverage.append(
            html("span", "comparison-visually-hidden", ` · ${fraction(row.score.readings.length, row.score.eligibleRuns)} source runs scored · ${fraction(row.score.contributingRunIds.length, row.score.eligibleRuns)} runs used`));
          cell.delta.textContent = `Δ ${formatMetric(finite(row.deltas[metric.id]) ? row.deltas[metric.id] : null, metric, true)}`
            + `${metric.unit && finite(row.deltas[metric.id]) ? ` ${metric.unit}` : ""} · ${isScoreMetric(metric) ? scoreDeltaCoverageText(row) : `${fraction(supplied.pairs.length, supplied.pairedEligible)} paired`}`;
          cell.delta.title = `Difference from ${baselineName}, matched pairs only`;
          cell.delta.hidden = !state.difference;
          return cell.element;
        });
        reconcile(view.element, [view.identity, cells[0], view.outcomeCell, ...cells.slice(1)]);
        return view.element;
      });
      reconcile(body, elements);
      scroll.hidden = displayedRows.length === 0; empty.hidden = displayedRows.length !== 0;
      showAll.hidden = state.outcome === "all";
      emptyText.textContent = state.outcome === "arrived" ? "No arrivals match this selection."
        : state.outcome === "failed" ? "No failed runs match this selection." : "No recorded trials for this selection. Choose another seed or Summary.";
      noMetrics.hidden = visibleMetrics.length !== 0;
      coverage.hidden = !displayedRows.length;
      coverageScroll.hidden = !visibleMetrics.length;
      coverageIntro.textContent = "Coverage shows supplied readings / eligible runs and valid pairs / eligible candidate runs. Missing counterparts and values stay missing. Repetitions are not independent environments. Summary dots show actual run readings on one shared scale per metric.";
      nativeNote.hidden = !!state.supplement || !visibleMetrics.some(metric => metric.source === "native");
      nativeNote.textContent = "Native metrics are not published for this world.";
      timingNote.hidden = !visibleMetrics.some(metric => metric.group === "Runtime");
      timingNote.textContent = "Measured timing calls count recorded wall-time measurements across eligible runs. Run coverage is separate. Unrecorded call counts are unknown. Missing timings are not zero.";
      if (coverage.open) renderCoverage();
      definitions.replaceChildren(...visibleMetrics.flatMap(metric => {
        const term = html("dt", "", `${metricName(metric)} · ${sources[metric.source]}`);
        return [term, html("dd", "", metric.description)];
      }));
      updateSelection();
    });
  }

  return {
    update(state) {
      if (disposed) return;
      current = state;
      if (!rendered || rendered.data !== state.data) { sortMetric = "score_points"; sortDirection = "descending"; }
      const rowInputsChanged = !rendered || !sameRowInputs(rendered, state);
      if (rowInputsChanged) modelRows = rowsForState(state);
      if (rowInputsChanged || !rendered || rendered.difference !== state.difference || !sameIds(rendered.visibleMetrics, state.visibleMetrics)) render();
      else if (!sameIds(rendered.selectedIds, state.selectedIds)) updateSelection();
      rendered = state;
    },
    dispose() {
      if (disposed) return;
      disposed = true; lifecycle.abort(); root.remove(); views.clear(); headers.clear();
      current = rendered = null; modelRows = []; displayedRows = []; visibleMetrics = [];
    },
  };
}
