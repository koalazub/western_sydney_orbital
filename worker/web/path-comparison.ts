// Recorded ownship evidence only. No engine, room, runner or flight capability.
import { finite, MAX_PATH_BYTES, pairedRuns, selectedCSV, validatePathComparison,
  type PathComparison, type PathRun } from "./path-comparison-data.ts";
import { alignmentTime, DEFAULT_VISIBLE_METRICS, inspectionAnchor, MAX_METRIC_BYTES, metricCatalog, runsForState, validateMetricPublication, type WorkspaceActions,
  type WorkspaceState } from "./comparison-model.ts";
import { installMetricMatrix } from "./comparison-matrix.ts";
import { installTradeoffExplorer } from "./comparison-tradeoffs.ts";
import { installIntervalInspector } from "./comparison-inspector.ts";
import { SCORE_REFERENCE } from "./score-estimate.ts";

const files = { official: "/analytics/paths-official.json", team: "/analytics/paths-team.json" } as const;
const metricFiles = { official: "/analytics/metrics-official.json", team: "/analytics/metrics-team.json" } as const;
type World = keyof typeof files;
type PathPublication = { data: PathComparison; bytes: ArrayBuffer; sha256: string | null };
const numbers = new Intl.NumberFormat();
const plurals = new Intl.PluralRules();
const counted = (value: number, one: string, many: string) => `${numbers.format(value)} ${plurals.select(value) === "one" ? one : many}`;
let instances = 0;

function html<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text?: string) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

async function boundedJSON(path: string, maxBytes: number): Promise<{ value: unknown; bytes: ArrayBuffer }> {
  const response = await fetch(path, { method: "GET", mode: "same-origin", credentials: "omit", redirect: "error",
    referrerPolicy: "no-referrer", cache: "no-cache", signal: AbortSignal.timeout(15000) });
  const size = response.headers.get("content-length");
  if (!response.ok || !/^application\/json(?:;|$)/i.test(response.headers.get("content-type") ?? "")
    || (size !== null && (!/^\d+$/.test(size) || Number(size) > maxBytes))) throw new Error("Unavailable");
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Unavailable");
  const chunks: Uint8Array[] = []; let bytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      bytes += value.byteLength;
      if (bytes > maxBytes) { await reader.cancel(); throw new Error("Unavailable"); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const raw = new ArrayBuffer(bytes), buffer = new Uint8Array(raw);
  let offset = 0;
  for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.byteLength; }
  return { value: JSON.parse(new TextDecoder().decode(buffer)), bytes: raw };
}

async function loadPaths(id: World): Promise<PathPublication> {
  const publication = await boundedJSON(files[id], MAX_PATH_BYTES);
  const data = validatePathComparison(publication.value);
  if (!data || data.id !== id) throw new Error("Unavailable");
  let sha256: string | null = null;
  try {
    const digest = await crypto.subtle.digest("SHA-256", publication.bytes);
    sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  } catch { /* Saved paths remain usable when the optional metric binding is unavailable. */ }
  return { data, bytes: publication.bytes, sha256 };
}

function eligibleSelection(state: WorkspaceState): PathRun[] {
  const runById = new Map(state.data.runs.map(run => [run.id, run]));
  return state.selectedIds.flatMap(id => {
    const run = runById.get(id);
    return run && (state.outcome === "all" || (run.outcome === "success") === (state.outcome === "arrived")) ? [run] : [];
  });
}

function initialState(data: PathComparison): WorkspaceState {
  const seed = data.comparison.seeds[0], repetition = 1;
  const preferred = data.id === "official" ? ["ali_official_profile", "wso_official_profile"] : ["basic_pd_radar"];
  const baseline = preferred.find(algorithm => data.runs.some(run => run.algorithm === algorithm)) ?? data.runs[0].algorithm;
  const selected = data.runs.filter(run => run.algorithm === baseline).sort((a, b) =>
    data.comparison.seeds.indexOf(a.seed) - data.comparison.seeds.indexOf(b.seed) || a.repetition - b.repetition);
  const anchor = selected.find(run => run.seed === seed && run.repetition === repetition);
  return { data, supplement: null, mode: "cohort", seed, repetition, baseline,
    selectedIds: selected.map(run => run.id), anchorId: anchor?.id ?? null, outcome: "all",
    xMetric: "fuel_kg", yMetric: "time_s", visibleMetrics: [...DEFAULT_VISIBLE_METRICS],
    cursor: 0, interval: null, alignment: "time", difference: false, mapScale: "detail" };
}

export function installPathComparison(container: HTMLElement) {
  const prefix = `path-comparison-${++instances}`;
  container.classList.add("path-comparison", "comparison-workspace");
  const existingHeading = (container.getAttribute("aria-labelledby")?.trim().split(/\s+/) ?? [])
    .map(id => container.ownerDocument.getElementById(id))
    .find(element => element && !container.contains(element) && element.matches("h1, h2, h3, h4, h5, h6, [role=heading]")
      && element.textContent?.trim() && !element.closest("[hidden], [aria-hidden=true]"));
  const heading = existingHeading ? null : html("h3", "", "Compare flights");
  if (heading) { heading.id = `${prefix}-heading`; container.setAttribute("aria-labelledby", heading.id); }
  const toolbar = html("div", "comparison-workspace-toolbar comparison-summary-header");
  const filters = html("details", "comparison-filters");
  filters.append(html("summary", "", "Filters"));
  const filterFields = html("div", "comparison-filter-fields"); filters.append(filterFields);
  const controlLabels = new Map<string, HTMLSpanElement>();
  function select(parent: HTMLElement, name: string, key: string, options: Array<[string, string]> = []) {
    const label = html("label"), caption = html("span", "", name), input = html("select");
    input.id = input.name = `${prefix}-${key}`; label.htmlFor = input.id;
    input.append(...options.map(([value, text]) => new Option(text, value)));
    controlLabels.set(key, caption); label.append(caption, input); parent.append(label); return input;
  }
  const world = select(toolbar, "World", "world", [["official", "Competition hop"], ["team", "Historical L2 practice"]]);
  const outcome = select(filterFields, "Outcome", "outcome", [["all", "All outcomes"], ["arrived", "Arrived"], ["failed", "Not arrived"]]);
  const seed = select(filterFields, "Inspect seed", "seed"), repetition = select(filterFields, "Inspect repetition", "repetition");
  const baseline = select(filterFields, "Baseline", "baseline");
  const differenceLabel = html("label", "comparison-difference-toggle"), difference = html("input");
  difference.type = "checkbox"; difference.id = difference.name = `${prefix}-difference`;
  differenceLabel.htmlFor = difference.id;
  differenceLabel.append(difference, document.createTextNode("Show differences from baseline")); filterFields.append(differenceLabel);
  toolbar.append(filters);
  const scope = html("p", "comparison-workspace-scope"); scope.hidden = true;
  const statusArea = html("div", "comparison-workspace-status");
  const status = html("p", "", "Open the comparison to load saved flights."); status.setAttribute("role", "status"); status.tabIndex = -1;
  const retry = html("button", "", "Retry flight measurements"); retry.type = "button"; retry.hidden = true;
  const metricStatus = html("p"); metricStatus.setAttribute("role", "status"); metricStatus.tabIndex = -1;
  const metricRetry = html("button", "", "Retry extra metrics"); metricRetry.type = "button"; metricRetry.hidden = true;
  metricStatus.hidden = true;
  const announcement = html("p", "comparison-live"), inspectionStatus = html("p", "comparison-live");
  for (const live of [announcement, inspectionStatus]) { live.setAttribute("role", "status"); live.setAttribute("aria-atomic", "true"); }
  statusArea.append(status, retry, metricStatus, metricRetry, announcement, inspectionStatus);
  const body = html("div", "comparison-workspace-body"); body.hidden = true;
  const matrixHost = html("section", "comparison-matrix-host");
  const tradeoffsHost = html("section", "comparison-tradeoffs-host");
  tradeoffsHost.hidden = true;
  const tradeoffsDisclosure = html("details", "comparison-tradeoffs-disclosure");
  tradeoffsDisclosure.append(html("summary", "", "Explore trade-offs"), tradeoffsHost);
  const inspectorHost = html("section", "comparison-inspector-host");
  const evidence = html("div", "comparison-evidence");
  const method = html("details"), provenance = html("div", "path-provenance");
  method.append(html("summary", "", "Data & method"), provenance);
  const exports = html("details"), downloads = html("nav", "analytics-downloads");
  downloads.setAttribute("aria-label", "Comparison data downloads");
  const json = html("a", "", "Download world paths JSON"); json.download = "";
  const metrics = html("a", "", "Download extra metrics JSON"); metrics.download = ""; metrics.hidden = true;
  const csv = html("a", "", "Download selected flight samples CSV"); csv.download = "selected-flight-samples.csv";
  const csvHelp = html("p", "path-note", "Select flights in the matrix or scatter plot to export their saved samples.");
  const trials = html("a", "", "Download historical official trial metrics CSV");
  trials.href = "/analytics/paths-official-trials.csv"; trials.download = "";
  const theory = html("a", "", "Read the Python theory review, opens in a new tab");
  theory.href = "/analytics/python-flight-theory.txt"; theory.target = "_blank"; theory.rel = "noopener noreferrer";
  downloads.append(json, metrics, csv, trials, theory);
  exports.append(html("summary", "", "Export data"), downloads, csvHelp);
  evidence.append(method, exports); body.append(matrixHost, inspectorHost, tradeoffsDisclosure, evidence);
  container.replaceChildren(...(heading ? [heading] : []), toolbar, scope, statusArea, body);

  let state: WorkspaceState | undefined, loading = false, loaded = false, disposed = false;
  let request = 0, metricRequest = 0, csvURL: string | undefined;
  const cache = new Map<World, PathPublication>();
  const metricCache = new Map<World, { pathSha256: string; supplement: NonNullable<WorkspaceState["supplement"]> }>();
  const actions: WorkspaceActions = { patch, selectRuns(ids) { patch({ selectedIds: ids }); } };
  const matrix = installMetricMatrix(matrixHost, actions);
  let tradeoffs: ReturnType<typeof installTradeoffExplorer> | undefined;
  const inspector = installIntervalInspector(inspectorHost, actions);
  const metricIds = new Set(metricCatalog.map(metric => metric.id));
  const sharedKeys = new Set(["data", "supplement", "mode", "seed", "repetition", "baseline", "selectedIds", "outcome"]);
  const inspectorKeys = new Set(["anchorId", "cursor", "interval", "alignment", "difference", "mapScale"]);
  setControlsDisabled(true);

  function retainRetryFocus(button: HTMLButtonElement, retained: HTMLElement) {
    if (document.activeElement !== button) return undefined;
    let moved = false;
    const onBlur = () => { moved = true; };
    retained.focus(); retained.addEventListener("blur", onBlur);
    return {
      finish(target: HTMLElement) {
        retained.removeEventListener("blur", onBlur);
        if (!moved && !disposed && document.activeElement === retained) target.focus();
      },
      cancel() { retained.removeEventListener("blur", onBlur); },
    };
  }
  function setControlsDisabled(disabled: boolean) {
    seed.disabled = repetition.disabled = baseline.disabled = outcome.disabled = difference.disabled = disabled;
  }
  function syncToolbar() {
    if (!state) return;
    world.value = state.data.id; seed.value = String(state.seed); repetition.value = String(state.repetition);
    baseline.value = state.baseline; outcome.value = state.outcome; difference.checked = state.difference;
    const summary = state.mode === "cohort";
    controlLabels.get("seed")!.textContent = summary ? "Inspect seed" : "Seed";
    controlLabels.get("repetition")!.textContent = summary ? "Inspect repetition" : "Repetition";
    const inspected = eligibleSelection(state).filter(run => run.seed === state!.seed && run.repetition === state!.repetition).length;
    const cohort = summary ? `All ${counted(state.data.comparison.seeds.length, "seed", "seeds")} × ${counted(state.data.comparison.repetitions, "repetition", "repetitions")}`
      : `Trials · seed ${numbers.format(state.seed)} · repetition ${numbers.format(state.repetition)}`;
    const outcomeScope = state.outcome === "all" ? "All outcomes" : state.outcome === "arrived" ? "Arrived only" : "Not arrived only";
    const pair = `Path: seed ${numbers.format(state.seed)} · repetition ${numbers.format(state.repetition)}${inspected ? "" : " · no selected flight"}`;
    const baselineName = state.data.runs.find(run => run.algorithm === state!.baseline)?.label ?? state.baseline;
    scope.textContent = `${state.data.id === "official" ? "Competition hop" : "Historical L2 stress/practice"} · ${cohort} · ${outcomeScope} · ${pair}${state.difference ? ` · Δ from ${baselineName}` : ""} · Local simulation`;
  }
  function selectedRuns() {
    if (!state) return [];
    const selected = new Set(state.selectedIds);
    return runsForState(state).filter(run => selected.has(run.id));
  }
  function updateEvidence() {
    if (!state) return;
    const info = state.data.comparison, runs = selectedRuns();
    provenance.replaceChildren(
      html("p", "", `Engine SHA-256: ${info.engine_sha256}`),
      html("p", "", `World ${info.preset}; debris ${info.distribution}, ${info.debris_count} bodies; step ${info.step_s} s; limit ${info.duration_s} s; route ${info.route_length_m} m.`),
      html("p", "", `Points re-evaluate the saved local final counters with current published formula ${SCORE_REFERENCE.formula_id}, source ${SCORE_REFERENCE.source_commit}. They are local estimates, not new flights, historical score receipts or official STP results. Metric details includes the formula, component breakdown and scoring population.`),
      html("p", "", "Other cohort efficiency summaries use arrivals and matched seed/repetition pairs. Points also include scorable damage failures. Repetitions are not independent environments. Fuel and damage are local estimates, not verified STP counters; arrival-region entry is not docking."),
      html("p", "", "No interpolation or stopped-trace extension. Commands describe preceding holds, not measured force. Damage brackets are sampled intervals, not exact contacts. Radar range is not hull clearance or collider identity."),
      html("p", "", "Paths use a fixed launch-to-initial-target frame, not altitude or body axes. Detail scale distorts angles; equal scale uses equal metres. Only curated ownship data is published, without raw radar, images, private descriptors or hidden debris states."),
      html("p", "", "The theory reference's PID and filtering aids are not wired into the published Ali flights. The STP port needs licensed-runtime validation. See Export data for the full theory review."));
    const limits = html("ul");
    for (const note of info.limitations) limits.append(html("li", "", note));
    const hashes = html("ul");
    for (const run of runs) hashes.append(html("li", "", `${run.label} · ${run.id} · seed ${run.seed} · rep ${run.repetition}. Run SHA-256 ${run.source_sha256.run}. Trace SHA-256 ${run.source_sha256.trace}.`));
    provenance.append(html("h4", "", "Historical source limitations"), limits, html("h4", "", "Selected source hashes"), hashes,
      html("p", "", "The world JSON includes source hashes for every published run. The selected CSV contains actual saved timestamps and values, with blank cells for missing measurements."));
    json.href = files[state.data.id]; metrics.href = metricFiles[state.data.id]; metrics.hidden = state.supplement === null;
    trials.hidden = state.data.id !== "official";
    if (csvURL) URL.revokeObjectURL(csvURL);
    csvURL = undefined; csv.removeAttribute("href"); csv.hidden = !runs.length; csvHelp.hidden = !!runs.length;
    if (runs.length) {
      csvURL = URL.createObjectURL(new Blob([selectedCSV(runs)], { type: "text/csv;charset=utf-8" })); csv.href = csvURL;
    }
  }
  function update(changed?: string[]) {
    if (!state || disposed) return;
    const shared = !changed || changed.some(key => sharedKeys.has(key));
    if (shared || changed?.includes("difference")) syncToolbar();
    if (shared || changed?.some(key => key === "visibleMetrics" || key === "difference")) matrix.update(state);
    if (tradeoffsDisclosure.open && (shared || changed?.some(key => key === "xMetric" || key === "yMetric"))) updateTradeoffs();
    if (shared || changed?.some(key => inspectorKeys.has(key))) inspector.update(state);
    if (!changed || changed.some(key => sharedKeys.has(key) && key !== "baseline")) updateEvidence();
  }
  function updateTradeoffs() {
    if (!state || disposed || !tradeoffsDisclosure.open) return;
    tradeoffs ??= installTradeoffExplorer(tradeoffsHost, actions);
    tradeoffs.update(state);
  }
  function patch(value: Partial<WorkspaceState>) {
    if (!state || loading || disposed || !value || typeof value !== "object" || Array.isArray(value)) return;
    const previous = state, next = { ...previous };
    const runById = new Map(previous.data.runs.map(run => [run.id, run]));
    const algorithms = new Set(previous.data.runs.map(run => run.algorithm));
    const duration = previous.data.comparison.duration_s;
    let selectionChanged = false, pairRequested = false, cursorPatched = false, intervalPatched = false;
    let requestedAnchorId: string | null | undefined;
    for (const [key, item] of Object.entries(value)) {
      switch (key) {
        case "mode": if (item === "trials" || item === "cohort") next.mode = item; break;
        case "seed":
          if (finite(item) && previous.data.comparison.seeds.includes(item)) { next.seed = item; pairRequested = true; }
          break;
        case "repetition":
          if (finite(item) && Number.isInteger(item) && item >= 1 && item <= previous.data.comparison.repetitions) {
            next.repetition = item; pairRequested = true;
          }
          break;
        case "baseline": if (typeof item === "string" && algorithms.has(item)) next.baseline = item; break;
        case "outcome": if (item === "all" || item === "arrived" || item === "failed") next.outcome = item; break;
        case "xMetric": case "yMetric": if (typeof item === "string" && metricIds.has(item)) next[key] = item; break;
        case "visibleMetrics":
          if (Array.isArray(item) && item.length <= metricIds.size && item.every(id => typeof id === "string" && metricIds.has(id))
            && new Set(item).size === item.length) next.visibleMetrics = ["score_points", ...item.filter(id => id !== "score_points")];
          break;
        case "selectedIds":
          if (Array.isArray(item) && item.length <= runById.size && item.every(id => typeof id === "string" && runById.has(id))
            && new Set(item).size === item.length) { next.selectedIds = [...item]; selectionChanged = true; }
          break;
        case "anchorId":
          if (item === null || (typeof item === "string" && runById.has(item))) requestedAnchorId = item;
          break;
        case "cursor":
          if (finite(item) && item >= 0 && item <= duration + 1e-7) { next.cursor = item; cursorPatched = true; }
          break;
        case "interval":
          if (item === null) { next.interval = null; intervalPatched = true; }
          else if (Array.isArray(item) && item.length === 2 && item.every(n => finite(n) && n >= 0 && n <= duration + 1e-7)
            && item[0] <= item[1]) { next.interval = [item[0], item[1]]; intervalPatched = true; }
          break;
        case "alignment": if (item === "time" || item === "damage" || item === "arrival") next.alignment = item; break;
        case "difference": if (typeof item === "boolean") next.difference = item; break;
        case "mapScale": if (item === "detail" || item === "equal") next.mapScale = item; break;
        // Loaded data and supplementary publications cannot be patched by widgets.
        default: break;
      }
    }
    const explicitPairChange = pairRequested && (next.seed !== previous.seed || next.repetition !== previous.repetition);
    if (explicitPairChange && !selectionChanged && next.mode === "trials") {
      const profiles = new Set(previous.selectedIds.map(id => runById.get(id)!.algorithm));
      next.selectedIds = pairedRuns(previous.data, next.seed, next.repetition)
        .filter(run => profiles.has(run.algorithm)).map(run => run.id);
    }
    const reconcilePair = selectionChanged || (!explicitPairChange && (next.mode !== previous.mode || next.outcome !== previous.outcome));
    const eligible = eligibleSelection(next);
    if (reconcilePair && !eligible.some(run => run.seed === next.seed && run.repetition === next.repetition)) {
      const first = eligible[0];
      if (first) { next.seed = first.seed; next.repetition = first.repetition; }
    }
    const selectedPair = eligible.filter(run => run.seed === next.seed && run.repetition === next.repetition);
    const requestedAnchor = selectedPair.find(run => run.id === requestedAnchorId);
    const stableAnchor = selectedPair.find(run => run.id === previous.anchorId);
    const hasOrigin = (run: PathRun | undefined): run is PathRun => !!run && alignmentTime(run, next.alignment) !== null;
    const anchor = hasOrigin(requestedAnchor) ? requestedAnchor : hasOrigin(stableAnchor) ? stableAnchor
      : selectedPair.find(hasOrigin) ?? requestedAnchor ?? stableAnchor ?? selectedPair[0];
    next.anchorId = anchor?.id ?? null;
    const pairChanged = next.seed !== previous.seed || next.repetition !== previous.repetition;
    if (selectionChanged || pairChanged || next.outcome !== previous.outcome || next.alignment !== previous.alignment
      || next.anchorId !== previous.anchorId) inspectionStatus.textContent = "";
    if (pairChanged) {
      next.cursor = 0; next.interval = null;
    } else if (next.anchorId !== previous.anchorId) {
      const oldAnchor = inspectionAnchor(previous), newAnchor = inspectionAnchor(next);
      const oldOrigin = oldAnchor ? alignmentTime(oldAnchor, previous.alignment) : null;
      const newOrigin = newAnchor ? alignmentTime(newAnchor, next.alignment) : null;
      const within = (time: number) => finite(time) && time >= -1e-7 && time <= duration + 1e-7;
      if (oldOrigin !== null && newOrigin !== null) {
        // Explicit times in an anchor patch already use the new anchor's clock.
        const cursor = cursorPatched ? next.cursor : previous.cursor - oldOrigin + newOrigin;
        const interval: [number, number] | null = intervalPatched || previous.interval === null ? next.interval
          : [previous.interval[0] - oldOrigin + newOrigin, previous.interval[1] - oldOrigin + newOrigin];
        if (within(cursor) && (!interval || interval.every(within))) {
          next.cursor = Math.max(0, Math.min(duration, cursor));
          next.interval = interval ? [Math.max(0, Math.min(duration, interval[0])), Math.max(0, Math.min(duration, interval[1]))] : null;
        } else {
          next.cursor = 0; next.interval = null;
          inspectionStatus.textContent = "Anchor changed. Cursor reset and interval cleared.";
        }
      } else {
        next.cursor = 0; next.interval = null;
        inspectionStatus.textContent = "Anchor changed. Cursor reset and interval cleared.";
      }
    }
    const changed = (Object.keys(next) as Array<keyof WorkspaceState>).filter(key => {
      const a = previous[key], b = next[key];
      return Array.isArray(a) && Array.isArray(b) ? a.length !== b.length || a.some((item, i) => item !== b[i]) : a !== b;
    });
    if (!changed.length) return;
    state = next; update(changed);
  }
  async function loadMetrics(publication: PathPublication, token: number) {
    const { data, sha256 } = publication;
    const metricToken = ++metricRequest;
    metricStatus.hidden = false; metricStatus.textContent = "Loading extra metrics.";
    const focus = retainRetryFocus(metricRetry, metricStatus); metricRetry.hidden = true;
    try {
      if (sha256 === null) throw new Error("Unavailable");
      const cached = metricCache.get(data.id);
      const supplement = cached?.pathSha256 === sha256 ? cached.supplement
        : validateMetricPublication((await boundedJSON(metricFiles[data.id], MAX_METRIC_BYTES)).value, data, sha256);
      if (disposed || token !== request || metricToken !== metricRequest) return;
      if (!supplement) throw new Error("Unavailable");
      metricCache.set(data.id, { pathSha256: sha256, supplement });
      state = { ...state!, supplement }; update();
      announcement.textContent = "Extra metrics loaded.";
      focus?.finish(matrixHost.querySelector<HTMLSelectElement>("select:not(:disabled)") ?? world);
      metricStatus.textContent = ""; metricStatus.hidden = true;
    } catch {
      if (disposed || token !== request || metricToken !== metricRequest) return;
      metricStatus.textContent = "Extra metrics are unavailable or do not match these paths. Retry to load them. Saved paths remain available."; metricRetry.hidden = false;
      focus?.finish(metricRetry);
    } finally { focus?.cancel(); }
  }
  async function changeWorld() {
    const id = world.value;
    if (id !== "official" && id !== "team") return;
    const token = ++request; ++metricRequest; loading = true; loaded = false;
    status.hidden = false; status.textContent = "Loading saved flights."; announcement.textContent = "";
    const focus = retainRetryFocus(retry, status); retry.hidden = true; body.hidden = scope.hidden = true;
    metricStatus.textContent = ""; metricStatus.hidden = true; metricRetry.hidden = true; inspectionStatus.textContent = ""; setControlsDisabled(true);
    try {
      const publication = cache.get(id) ?? await loadPaths(id);
      if (disposed || token !== request) return;
      const { data } = publication;
      cache.set(id, publication); state = initialState(data);
      seed.replaceChildren(...data.comparison.seeds.map(n => new Option(String(n), String(n))));
      repetition.replaceChildren(...Array.from({ length: data.comparison.repetitions }, (_, i) => new Option(String(i + 1), String(i + 1))));
      baseline.replaceChildren(...pairedRuns(data, state.seed, state.repetition).map(run => new Option(run.label, run.algorithm)));
      loaded = true; setControlsDisabled(false); body.hidden = scope.hidden = false;
      update(); announcement.textContent = "Saved flights loaded.";
      focus?.finish(matrixHost.querySelector<HTMLSelectElement>("select:not(:disabled)") ?? world);
      status.textContent = ""; status.hidden = true; void loadMetrics(publication, token);
    } catch {
      if (disposed || token !== request) return;
      status.textContent = "Flight measurements could not be loaded or validated. Check your connection and retry."; retry.hidden = false;
      focus?.finish(retry);
    } finally { focus?.cancel(); if (token === request) loading = false; }
  }
  world.addEventListener("change", () => { void changeWorld(); });
  seed.addEventListener("change", () => patch({ seed: Number(seed.value) }));
  repetition.addEventListener("change", () => patch({ repetition: Number(repetition.value) }));
  baseline.addEventListener("change", () => patch({ baseline: baseline.value }));
  outcome.addEventListener("change", () => patch({ outcome: outcome.value as WorkspaceState["outcome"] }));
  difference.addEventListener("change", () => patch({ difference: difference.checked }));
  tradeoffsDisclosure.addEventListener("toggle", () => {
    tradeoffsHost.hidden = !tradeoffsDisclosure.open;
    if (tradeoffsDisclosure.open) updateTradeoffs();
  });
  retry.addEventListener("click", () => { void changeWorld(); });
  metricRetry.addEventListener("click", () => {
    const publication = state && cache.get(state.data.id);
    if (publication && state?.data === publication.data && !loading) void loadMetrics(publication, request);
  });
  addEventListener("pagehide", event => {
    if (event.persisted) return;
    disposed = true; ++request; ++metricRequest;
    if (csvURL) URL.revokeObjectURL(csvURL);
    matrix.dispose(); inspector.dispose(); tradeoffs?.dispose();
  });
  return { open() { if (!disposed && !loaded && !loading) void changeWorld(); } };
}
