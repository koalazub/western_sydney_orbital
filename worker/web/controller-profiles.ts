// Named Python job selection only. Autonomy stays in Python; world creation,
// measurements and control authority stay in the real native engine.
import profiles from "../../src/wsorb/controllers/catalog.json";
import { flightSelection, flightWorlds, type FlightSelection } from "./flight-selection.ts";
import { freshTrial } from "./flight-launch.ts";
import { CONTROLLER_ANALYTICS_COLUMNS, CONTROLLER_ANALYTICS_DEFINITION, CONTROLLER_SCORE_FORMULA } from "../shared/controller-jobs.ts";
import { installFlightAnalytics } from "./flight-analytics.ts";
import { mountDashboard } from "./dashboard-shell.ts";
import { installPathComparison } from "./path-comparison.ts";
import type { RoomEngine } from "./room-control.ts";
import { estimateScore } from "./score-estimate.ts";

type Connection = { origin: string; id: string; key: string };
type QueueRequest = (payload?: Record<string, unknown>) => Promise<Record<string, unknown>>;
type Job = { id: string; profile_id: string; status: string; stop_requested: boolean };
type View = {
  service: { online: boolean; busy: boolean };
  active: Job | null;
  results: Array<Record<string, unknown>>;
};
type LocalBridge = { status?: string; controllers?: QueueRequest };
type ResultsSource = { kind: "room"; connection: Connection } | { kind: "local"; bridge: LocalBridge } | null;
type Options = {
  engine: () => RoomEngine | undefined;
  ready: () => boolean;
  room: () => Connection | null;
  local: () => LocalBridge | undefined;
};
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const number = (value: unknown, places = 2) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(places) : "n/a";
const practiceScoreLabel = "Points · current STP formula, local estimate";
const counter = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : null;
function terminalStatus(result: Record<string, unknown>) {
  if (result.state !== "native_terminal" || result.world_metadata_unchanged !== true || result.error_code !== null
    || !record(result.measurement)) return null;
  if (result.measurement.status === "success" && result.native_success === true) return "success";
  if (result.measurement.status === "failed_damage" && result.native_success === false) return "failed_damage";
  return null;
}
function recordedInitialRange(result: Record<string, unknown>) {
  const analytics = result.analytics;
  if (result.world_metadata_unchanged !== true || !record(analytics) || analytics.schema_version !== 1
    || analytics.definition_id !== CONTROLLER_ANALYTICS_DEFINITION
    || analytics.source_kind !== "native_physics_step_counters_with_sampled_public_series"
    || !Array.isArray(analytics.columns) || analytics.columns.length !== CONTROLLER_ANALYTICS_COLUMNS.length
    || !analytics.columns.every((name, index) => name === CONTROLLER_ANALYTICS_COLUMNS[index])
    || !Array.isArray(analytics.samples) || analytics.samples.length === 0 || analytics.samples.length > 48
    || !Number.isSafeInteger(analytics.sample_count) || (analytics.sample_count as number) < analytics.samples.length
    || !record(analytics.totals) || analytics.totals.schema_version !== 1
    || analytics.totals.definition_id !== CONTROLLER_ANALYTICS_DEFINITION) return null;
  const observedTime = counter(analytics.totals.observed_sim_s);
  if (observedTime === null || observedTime < 0) return null;
  const timeColumn = CONTROLLER_ANALYTICS_COLUMNS.indexOf("time_s");
  const rangeColumn = CONTROLLER_ANALYTICS_COLUMNS.indexOf("range_m");
  let previousTime = -1, initialRange: number | null = null;
  for (const row of analytics.samples) {
    if (!Array.isArray(row) || row.length !== CONTROLLER_ANALYTICS_COLUMNS.length
      || row.some(value => value !== null && (typeof value !== "number" || !Number.isFinite(value) || value < 0))) return null;
    const time = counter(row[timeColumn]);
    if (time === null || time <= previousTime || time > observedTime + 1e-6) return null;
    previousTime = time;
    // A later retained sample is not the scoring system's initial range.
    if (time === 0) initialRange = counter(row[rangeColumn]);
  }
  return initialRange !== null && initialRange > 0 ? initialRange : null;
}
function currentScore(result: Record<string, unknown>) {
  const status = terminalStatus(result);
  if (status === null || !record(result.measurement)) return null;
  const measurement = result.measurement;
  return estimateScore({ status, time_s: counter(measurement.time_s), damage_j: counter(measurement.damage_j),
    fuel_used_kg: counter(measurement.fuel_used_kg), range_m: counter(measurement.range_m),
    // Never borrow a current-world range, route length or legacy score.
    initial_range_m: recordedInitialRange(result) });
}
function scoreUnavailable(result: Record<string, unknown>) {
  if (result.world_metadata_unchanged !== true) return "Flight context was not preserved.";
  if (result.state !== "native_terminal") return "No confirmed native terminal result.";
  if (terminalStatus(result) === "failed_damage" && recordedInitialRange(result) === null) return "No recorded time-zero target range.";
  if (terminalStatus(result) === null) return "Terminal outcome was not confirmed.";
  return "A required final counter is missing or invalid.";
}
const numericScore = (result: Record<string, unknown>) => {
  if (result.world_metadata_unchanged === false) return null;
  const score = record(result.measurement) ? result.measurement.score : null;
  return typeof score === "number" && Number.isFinite(score) ? score : null;
};

function comparisonGroups(results: Array<Record<string, unknown>>) {
  const groups = new Map<string, { label: string; comparable: boolean; results: Array<Record<string, unknown>> }>();
  for (const [index, result] of results.slice(-32).reverse().entries()) {
    const build = typeof result.engine_sha256 === "string" && /^[0-9a-f]{64}$/.test(result.engine_sha256) ? result.engine_sha256 : null;
    const world = flightWorlds.find(world => world.id === result.world_id);
    const comparable = build !== null && world !== undefined && Number.isSafeInteger(result.seed) && typeof result.duration_s === "number"
      && Number.isFinite(result.duration_s) && result.duration_s > 0 && result.duration_s <= 1800;
    // A missing build cannot establish a comparison cohort, even if a score exists.
    const key = comparable ? `${build}:${world!.id}:${result.seed}:${result.duration_s}` : `unknown:${index}`;
    let group = groups.get(key);
    if (!group) {
      group = {
        label: comparable ? `${world!.label}, seed ${result.seed}, time limit ${result.duration_s} s · Engine ${build}`
          : "Comparison unavailable: world, engine build, seed or time limit not recorded.",
        comparable, results: [],
      };
      groups.set(key, group);
    }
    group.results.push(result);
  }
  return groups.values();
}

async function hostedRequest(connection: Connection, payload?: Record<string, unknown>) {
  const response = await fetch(`/api/rooms/${connection.id}/controllers`, {
    method: payload === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${connection.key}`, ...(payload ? { "Content-Type": "application/json" } : {}) },
    body: payload ? JSON.stringify(payload) : undefined,
    mode: "same-origin", credentials: "omit", redirect: "error", referrerPolicy: "same-origin", cache: "no-store",
    signal: AbortSignal.timeout(5000),
  });
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Controller service is unavailable.");
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 512 * 1024) { await reader.cancel(); throw new Error("Controller response exceeds its limit."); }
    chunks.push(value);
  }
  const result: unknown = JSON.parse(await new Blob(chunks).text());
  // Never display arbitrary server text or capability-bearing request details.
  if (!response.ok || !record(result)) throw new Error("Controller request was not confirmed. Refresh before trying a new action.");
  return result;
}

function confirmFreshTrial(profileLabel: string, selection: FlightSelection): Promise<boolean> {
  const dialog = byId<HTMLDialogElement>("controller-fresh-confirm");
  const world = flightWorlds.find(world => world.id === selection.worldId)!;
  byId("controller-fresh-chosen").textContent = `Replace this tab's flight with ${profileLabel} in ${world.label}, seed ${selection.seed}? The current flight cannot be recovered here.`;
  // window.confirm blocks the bridge's page-lease polling. A native HTML dialog
  // keeps that authenticated connection alive while the person decides.
  dialog.returnValue = "cancel";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "run"), { once: true });
    dialog.showModal();
  });
}

export function installControllerProfiles(options: Options) {
  const panel = byId<HTMLDialogElement>("controller-panel");
  const dashboard = mountDashboard(panel);
  const open = byId<HTMLButtonElement>("open-controllers");
  const owner = byId<HTMLSelectElement>("controller-owner");
  const profile = byId<HTMLSelectElement>("controller-profile");
  const world = byId<HTMLSelectElement>("controller-world");
  world.setAttribute("aria-describedby", "controller-world-description");
  const seed = byId<HTMLInputElement>("controller-seed");
  const duration = byId<HTMLSelectElement>("controller-duration");
  world.replaceChildren(...flightWorlds.map(world => new Option(world.label, world.id)));
  world.value = "official";
  const run = byId<HTMLButtonElement>("run-controller");
  run.disabled = true;
  const quickProfile = byId<HTMLSelectElement>("quick-profile");
  const quickRun = byId<HTMLButtonElement>("quick-run");
  const quickStatus = byId("quick-status");
  const stop = byId<HTMLButtonElement>("stop-controller");
  const outsideStop = byId<HTMLButtonElement>("stop-controller-flight");
  const watch = byId<HTMLButtonElement>("watch-controller-flight");
  const setup = byId<HTMLDetailsElement>("controller-setup");
  const setupButton = byId<HTMLButtonElement>("setup-controller");
  const status = byId("controller-service-status");
  const error = byId("controller-error");
  const table = byId<HTMLTableElement>("controller-results-table");
  const breakdownTable = byId<HTMLTableElement>("controller-score-breakdown-table");
  const legacyTable = byId<HTMLTableElement>("controller-legacy-results-table");
  const analytics = installFlightAnalytics();
  const pathContainer = byId("dashboard-path-comparison");
  const paths = installPathComparison(pathContainer);
  // This caller already has a visible title. Do not leave a second, hidden
  // heading in the comparison's heading navigation.
  pathContainer.querySelector(":scope > h3")?.remove();
  pathContainer.setAttribute("aria-labelledby", "lab-title");
  let view: View | undefined;
  let viewSource: ResultsSource = null;
  let lastJob: { job: Job; source: ResultsSource } | null = null;
  let trackingJob = false;
  let busy = false;
  let starting: AbortController | null = null;
  let startupMessage = "";
  let refreshing = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let autoUpdate = true;
  let disposed = false;
  let openingControl: HTMLElement = open;
  let refreshVersion = 0;
  let savedSource: ResultsSource = null;
  let savedCount: number | undefined;
  let latestResult: Record<string, unknown> | undefined;
  let dataState: "waiting" | "loaded" | "stale" | "unavailable" = "waiting";
  const resultsSource = (): ResultsSource => {
    const room = options.room();
    if (room) return { kind: "room", connection: { ...room } };
    const local = options.local();
    return local ? { kind: "local", bridge: local } : null;
  };
  const sameSource = (a: ResultsSource, b: ResultsSource) => {
    if (a === null || b === null) return a === b;
    if (a.kind === "room" && b.kind === "room") {
      return a.connection.origin === b.connection.origin && a.connection.id === b.connection.id && a.connection.key === b.connection.key;
    }
    return a.kind === "local" && b.kind === "local" && a.bridge === b.bridge;
  };
  const sourceAvailable = (source: ResultsSource) => source?.kind === "room"
    || source?.kind === "local" && source.bridge.status === "connected" && typeof source.bridge.controllers === "function";
  const confirmedView = () => sameSource(viewSource, resultsSource()) && sourceAvailable(viewSource) ? view : undefined;
  const label = (id: unknown) => {
    const entry = profiles.find(item => item.id === id);
    return entry ? `${entry.owner} / ${entry.label}` : "Unknown profile";
  };
  const jobState = (job: Job) => job.stop_requested ? "Stop requested"
    : job.status === "running" ? "Running" : job.status === "queued" ? "Queued"
      : job.status === "stopping" ? "Stopping" : "Runner stalled";
  function renderRunnerControls() {
    const current = confirmedView();
    const active = current?.active;
    run.disabled = busy || !current?.service.online || current.service.busy || !!active || !options.ready();
    quickRun.disabled = run.disabled;
    quickProfile.disabled = busy || !!active || !!starting;
    const quickWorld = flightWorlds.find(item => item.id === world.value);
    const quickText = starting ? startupMessage
      : !options.ready() ? "Waiting for the game to be ready…"
        : !resultsSource() ? "Open More options to connect Python."
          : !current ? "Connecting to the Python runner…"
            : !current.service.online ? "Runner offline. Use More options to start it."
              : active ? `${label(active.profile_id)} · ${jobState(active)}`
                : current.service.busy ? "Runner busy. Wait for the current test."
                  : `Ready · ${quickWorld?.label ?? "Official competition reference"} · seed ${seed.value} · ${duration.value} s`;
    if (quickStatus.textContent !== quickText) quickStatus.textContent = quickText;
    stop.disabled = outsideStop.disabled = starting ? starting.signal.aborted : busy || !active || active.stop_requested;
    stop.textContent = outsideStop.textContent = starting ? "Cancel startup" : "Stop controller";
    watch.disabled = busy || !options.ready();
    setupButton.hidden = !!current?.service.online;
    byId("controller-run-reason").textContent = starting ? startupMessage : busy ? "Waiting for the current request."
      : !options.ready() ? "Wait for the game to be ready and keep this tab visible."
      : !current ? "Start Python on your machine and check the connection before running."
        : !current.service.online ? "Python runner offline. Start it on your machine, then check the connection."
          : active || current.service.busy ? "Wait for Stop to finish before running the next test."
             : "";
    const flight = byId("controller-flight");
    const flightScore = byId("controller-flight-score");
    flightScore.hidden = true;
    flight.hidden = lastJob === null && !starting;
    if (starting) {
      flight.dataset.state = "starting";
      byId("controller-flight-status").textContent = startupMessage;
      byId("controller-flight-measurements").textContent = "";
      byId("refresh-controller-flight").hidden = true;
      return;
    }
    if (!lastJob) return;
    let state: string;
    if (!current || !sameSource(lastJob.source, resultsSource())) {
      state = "Last confirmed profile. Status unavailable; refresh the connection.";
      flight.dataset.state = "unavailable";
    } else if (active) {
      state = `${jobState(active)}${current.service.online ? "" : " · Python runner offline"}${autoUpdate ? "" : " · last confirmed, updates paused"}`;
      flight.dataset.state = active.status;
    } else {
      const result = current.results.find(result => result.id === lastJob!.job.id);
      state = result && terminalStatus(result) === "success" ? "Station region reached"
        : result && terminalStatus(result) === "failed_damage" ? "Collision damage failure"
        : result?.state === "stopped" ? "Stopped"
          : result?.state === "duration_limit" ? "Time budget reached"
            : result?.state === "wall_budget_limit" ? "Runner time budget reached"
              : result?.state === "interrupted" ? "Interrupted"
                : result?.state === "error" ? "Run ended with an error. See receipts."
                  : result ? "Run ended. See receipts." : "No active job. See receipts for the outcome.";
      flight.dataset.state = "finished";
      const estimate = result ? currentScore(result) : null;
      if (estimate) {
        flightScore.textContent = `${number(estimate.total)} pts · Local estimate. Progress ${number(estimate.progress_points)} − damage ${number(estimate.collision_penalty_points)} − fuel ${number(estimate.fuel_penalty_points)} + ${number(estimate.offset_points)}.`;
        flightScore.hidden = false;
      }
    }
    const message = `${label(lastJob.job.profile_id)} · ${state}`;
    const target = byId("controller-flight-status");
    if (target.textContent !== message) target.textContent = message;
    byId("refresh-controller-flight").hidden = !!current && autoUpdate;
  }
  function renderLatestResult(state: string, previousConnection: boolean) {
    const card = byId("controller-latest-result");
    card.hidden = latestResult === undefined;
    if (!latestResult) return;
    const score = currentScore(latestResult);
    const title = state === "loaded" ? "Latest result" : previousConnection ? "Previous connection result" : "Last saved result";
    byId("controller-latest-result-heading").textContent = title;
    byId("controller-latest-result-profile").textContent = label(latestResult.profile_id);
    byId("controller-latest-result-points").textContent = score ? number(score.total) : "n/a";
    const note = score ? terminalStatus(latestResult) === "success" ? "Station region reached." : "Damage failure."
      : scoreUnavailable(latestResult);
    const connectionNote = state === "loaded" ? "" : " Connection data is not current; refresh results.";
    byId("controller-latest-result-note").textContent = note + connectionNote;
    card.dataset.dataState = state;
    const message = `${title} · ${label(latestResult.profile_id)} · ${score ? `${number(score.total)} points, local estimate.` : `Points unavailable. ${note}`}${connectionNote}`;
    const announcement = byId("controller-latest-result-announcement");
    if (announcement.textContent !== message) announcement.textContent = message;
  }
  function renderDataStatus() {
    const previousConnection = savedCount !== undefined && !sameSource(savedSource, resultsSource());
    const displayedState = previousConnection && dataState === "loaded" ? "stale" : dataState;
    const message = displayedState === "waiting" ? "Connection data not loaded."
      : displayedState === "loaded" ? `${savedCount} saved connection ${savedCount === 1 ? "receipt" : "receipts"} loaded. Native analytics stay n/a where unrecorded.`
        : previousConnection ? "Previous connection data retained, not current. This connection's receipts and native analytics are unavailable."
          : displayedState === "stale" ? "Connection data stale. Showing the last successful receipts and traces. Native analytics stay n/a where unrecorded."
            : "Connection data unavailable. Receipts and native analytics have not loaded.";
    const recovery = displayedState === "stale" || displayedState === "unavailable"
      ? " Check the Python connection, then use Refresh results. Refresh only reads saved data."
      : " Refresh results only reads saved data.";
    const updates = autoUpdate ? "" : " Automatic updates paused.";
    for (const id of ["controller-results-status", "controller-recent-data-status"]) {
      const target = byId(id), text = message + recovery + updates;
      if (target.textContent !== text) target.textContent = text;
      target.dataset.dataState = displayedState;
    }
    renderLatestResult(displayedState, previousConnection);
  }
  function unavailableResults() {
    view = undefined;
    renderRunnerControls();
    dataState = savedCount === undefined ? "unavailable" : "stale";
    // The existing charts and receipts belong to the last accepted source.
    // A failed read is not evidence of an empty connection or an empty flight.
    if (savedCount === undefined) {
      byId("lab-room-empty").hidden = true;
      byId("room-analytics-cohort-detail").textContent = "Connection receipts are unavailable.";
      byId("room-analytics-status").textContent = "Native analytics unavailable until connection receipts load.";
    }
    renderDataStatus();
  }
  const request = (): QueueRequest => {
    const room = options.room();
    if (room) return payload => hostedRequest(room, payload);
    const local = options.local();
    if (local?.status === "connected" && local.controllers) return local.controllers;
    throw new Error("No Python connection for this tab. Start the runner on your machine using the local connection file or a private room. Set up Python runner shows the commands.");
  };
  const selected = () => profiles.find(item => item.id === profile.value)!;
  function selection(): FlightSelection {
    const result = flightSelection(new URLSearchParams({ profile: profile.value, world: world.value, seed: seed.value, duration: duration.value }));
    if (!result) throw new Error("Choose a profile, world, whole-number seed from 0 to 4294967295, and a time budget.");
    return result;
  }
  function worldDescription() {
    const chosen = flightWorlds.find(item => item.id === world.value)!;
    byId("controller-world-summary").textContent = `${chosen.label} · seed ${seed.value} · ${chosen.settings.debris.count} debris bodies`;
    byId("controller-world-description").textContent = chosen.id === "official"
      ? "Competition hop with the published debris-cloud setup. A fresh run uses the current local engine and live Python timing. It can differ from saved runs."
      : chosen.id === "team" ? "Historical L2 practice settings from the saved teammate comparison. A fresh run uses the current local engine and live Python timing."
        : "Far-side L2 practice with 20 m/s Across Phobos crossflow and light attraction off.";
  }
  function description() {
    const entry = selected();
    byId("controller-description").textContent = entry.description;
    byId("controller-implementation").textContent = entry.profile;
    byId("controller-profile-kind").textContent = entry.target_seeking
      ? `${entry.experimental ? "Experimental · " : ""}Target-seeking` : "Not target-seeking";
    byId("controller-profile-source").textContent = entry.source;
    byId("controller-profile-id").textContent = entry.id;
    byId("controller-profile-contributor").textContent = entry.contributor;
  }
  function selectOwner() {
    profile.replaceChildren(...profiles.filter(entry => entry.owner === owner.value)
      .sort((a, b) => Number(b.target_seeking) - Number(a.target_seeking))
      .map(entry => new Option(`${entry.label}${entry.target_seeking ? "" : " · not target-seeking"}`, entry.id)));
    description();
  }
  function chooseQuickProfile(identifier: string) {
    const entry = profiles.find(item => item.id === identifier);
    if (!entry) return;
    owner.value = entry.owner;
    selectOwner();
    profile.value = entry.id;
    quickProfile.value = entry.id;
    description();
  }
  const owners = [...new Set(profiles.map(entry => entry.owner))].sort((a, b) => a.localeCompare(b));
  owner.replaceChildren(...owners.map(name => new Option(name, name)));
  quickProfile.replaceChildren(...owners.map(name => {
    const group = document.createElement("optgroup");
    group.label = name;
    group.append(...profiles.filter(entry => entry.owner === name)
      .sort((a, b) => Number(b.target_seeking) - Number(a.target_seeking))
      .map(entry => new Option(`${entry.label}${entry.target_seeking ? "" : " · not target-seeking"}`, entry.id)));
    return group;
  }));
  chooseQuickProfile(profiles.some(entry => entry.id === "ali_mppi") ? "ali_mppi" : profiles[0].id);
  worldDescription();
  function renderResults(results: Array<Record<string, unknown>>) {
    const outcome = (result: Record<string, unknown>) => terminalStatus(result) === "success" ? "Station region reached"
      : terminalStatus(result) === "failed_damage" ? "Collision damage failure"
        : result.state === "duration_limit" ? "Time budget reached"
          : result.state === "wall_budget_limit" ? "Runner time budget reached"
            : result.error_code ? `${result.state ?? "error"}: ${result.error_code}` : String(result.state ?? "Unknown");
    function populate(target: HTMLTableElement, columns: number, valuesFor: (result: Record<string, unknown>) => string[]) {
      const bodies = Array.from(comparisonGroups(results), group => {
        const body = document.createElement("tbody");
        const heading = body.insertRow();
        const name = document.createElement("th");
        name.scope = "rowgroup";
        name.colSpan = columns;
        name.textContent = group.label;
        heading.append(name);
        for (const result of group.results) {
          const row = body.insertRow();
          valuesFor(result).forEach((value, index) => {
            const cell = document.createElement(index === 0 ? "th" : "td");
            if (index === 0) cell.scope = "row";
            if (index === 1 && target !== legacyTable) cell.className = "controller-score-points";
            cell.textContent = value;
            row.append(cell);
          });
        }
        return body;
      });
      for (const body of Array.from(target.tBodies)) body.remove();
      target.append(...bodies);
    }
    populate(table, 6, result => {
      const measurement = record(result.measurement) ? result.measurement : {};
      const score = currentScore(result);
      return [label(result.profile_id), score ? number(score.total) : "n/a", outcome(result),
        number(measurement.time_s, 1), number(measurement.fuel_used_kg, 3), number(measurement.damage_j)];
    });
    populate(breakdownTable, 7, result => {
      const score = currentScore(result);
      return [label(result.profile_id), ...[score?.total, score?.progress_points, score?.collision_penalty_points,
        score?.fuel_penalty_points, score?.offset_points].map(value => value === undefined ? "n/a" : String(value)),
        score ? score.progress_basis === "time_ratio" ? "Terminal completion time" : "Recorded remaining-distance ratio" : scoreUnavailable(result)];
    });
    populate(legacyTable, 13, result => {
      const measurement = record(result.measurement) ? result.measurement : {};
      const terms = record(measurement.score_breakdown) && measurement.score_breakdown.formula === CONTROLLER_SCORE_FORMULA
        && numericScore(result) !== null && measurement.score_breakdown.total === numericScore(result)
        ? measurement.score_breakdown : {};
      const score = numericScore(result);
      const scoreText = score === null ? "n/a" : `${number(score)} (legacy, not STP)`;
      return [label(result.profile_id), scoreText, number(result.duration_s, 0), outcome(result),
        number(measurement.time_s, 1), number(measurement.range_m), number(measurement.relative_speed_mps), number(measurement.damage_j), number(measurement.fuel_used_kg, 3),
        number(terms.progress_points), number(terms.collision_penalty_points), number(terms.fuel_penalty_points), number(terms.offset_points)];
    });
    latestResult = results.at(-1);
    byId("lab-room-results").hidden = results.length === 0;
  }
  function render(value: Record<string, unknown>, source: ResultsSource) {
    if (!sameSource(source, resultsSource())) {
      ++refreshVersion;
      unavailableResults();
      status.textContent = "Connection changed. Refresh results before using the runner controls.";
      return;
    }
    if (!record(value.service) || typeof value.service.online !== "boolean" || typeof value.service.busy !== "boolean"
      || !Array.isArray(value.results) || !value.results.every(record)
      || value.active !== null && (!record(value.active) || typeof value.active.id !== "string" || typeof value.active.profile_id !== "string"
        || typeof value.active.stop_requested !== "boolean" || !["queued", "running", "stopping", "stalled"].includes(String(value.active.status)))) {
      throw new Error("Controller service returned an unsupported status.");
    }
    view = value as unknown as View;
    viewSource = source;
    const active = view.active;
    trackingJob = active !== null;
    if (active) lastJob = { job: { ...active }, source };
    const message = active ? `Current job · ${label(active.profile_id)} · ${jobState(active)}.${view.service.online ? "" : " Python runner offline."}`
      : !view.service.online ? "Python runner offline. Start it on your machine, then check the connection."
        : view.service.busy ? "Python runner busy. Refresh status before starting another flight."
          : "Python runner on your machine connected and idle. Ready to run.";
    if (status.textContent !== message) status.textContent = message;
    renderRunnerControls();
    renderResults(view.results);
    analytics.updateRoomResults(view.results);
    savedSource = source;
    savedCount = view.results.length;
    dataState = "loaded";
    ++refreshVersion;
    renderDataStatus();
  }
  byId("controller-score-label").textContent = practiceScoreLabel;
  async function refresh() {
    if (refreshing || disposed) return;
    refreshing = true;
    const version = ++refreshVersion, source = resultsSource();
    try {
      const value = await request()();
      if (version !== refreshVersion) return;
      render(value, source);
    }
    catch (cause) {
      if (version !== refreshVersion) return;
      unavailableResults();
      status.textContent = cause instanceof Error ? cause.message : "Controller service is unavailable.";
    }
    finally { refreshing = false; schedule(); }
  }
  function showError(message: string) {
    error.textContent = message;
    byId("controller-flight-error").textContent = message;
  }
  async function requestStop() {
    if (starting) {
      starting.abort();
      startupMessage = "Cancelling startup.";
      const confirmation = byId<HTMLDialogElement>("controller-fresh-confirm");
      if (confirmation.open) confirmation.close("cancel");
      renderRunnerControls(); return;
    }
    const current = confirmedView();
    if (!current?.active || busy) return;
    busy = true;
    ++refreshVersion;
    renderRunnerControls();
    showError("");
    const source = resultsSource();
    try { render(await request()({ action: "stop", job_id: current.active.id }), source); }
    catch { unavailableResults(); showError("Stop was not confirmed. Inspect the live flight and refresh before another action."); }
    finally { busy = false; renderRunnerControls(); await refresh(); }
  }
  const changedProfile = async () => { description(); await requestStop(); };
  owner.addEventListener("change", () => { selectOwner(); quickProfile.value = profile.value; void changedProfile(); });
  profile.addEventListener("change", () => { quickProfile.value = profile.value; void changedProfile(); });
  world.addEventListener("change", () => { worldDescription(); renderRunnerControls(); void requestStop(); });
  seed.addEventListener("change", () => { worldDescription(); renderRunnerControls(); void requestStop(); });
  duration.addEventListener("change", () => { renderRunnerControls(); void requestStop(); });
  stop.addEventListener("click", () => { void requestStop(); });
  outsideStop.addEventListener("click", () => { void requestStop(); });
  async function startRun() {
    if (busy || run.disabled) return;
    busy = true;
    starting = new AbortController();
    startupMessage = "Checking the Python runner on your machine.";
    const signal = starting.signal;
    ++refreshVersion;
    owner.disabled = profile.disabled = world.disabled = seed.disabled = duration.disabled = true;
    run.disabled = true;
    renderRunnerControls();
    showError("");
    try {
      const chosen = selection();
      if (!options.ready()) throw new Error("Wait for the game to be ready and keep this tab visible.");
      const api = options.engine();
      if (!api) throw new Error("The real engine is unavailable. No run was started.");
      const before = JSON.parse(api.observation());
      if (before.time_s > 0 && !await confirmFreshTrial(label(chosen.profileId), chosen)) return;
      signal.throwIfAborted();
      const send = request();
      const source = resultsSource();
      render(await send(), source);
      const current = confirmedView();
      if (!current?.service.online || current.service.busy || current.active || !options.ready()) throw new Error("Wait for a ready game and idle Python runner before starting.");
      startupMessage = "Preparing the selected world."; renderRunnerControls();
      const expected = await freshTrial(api, chosen, signal);
      signal.throwIfAborted();
      if (!sameSource(source, resultsSource()) || !options.ready()) throw new Error("The flight connection changed during startup.");
      const camera = JSON.parse(api.control(JSON.stringify({ op: "view", mode: "orbit", focus: "Shuttle", zoom: 1 })));
      if (camera.ok !== true) throw new Error("The lander view could not be opened.");
      startupMessage = "Starting the selected Python profile."; renderRunnerControls();
      const submitted = await send({ action: "submit", profile_id: chosen.profileId, world_id: chosen.worldId, seed: chosen.seed,
        duration_s: chosen.duration, fresh: true, expected_context: expected });
      render(submitted, source);
      // Cancellation can arrive while the submit is in flight. Stop the exact
      // confirmed job, never resend the submit or infer that a timeout cancelled it.
      if (signal.aborted && record(submitted.active) && typeof submitted.active.id === "string") {
        render(await send({ action: "stop", job_id: submitted.active.id }), source);
      }
      autoUpdate = true;
      for (const id of ["pause-controller-updates", "pause-controller-flight-updates"]) byId(id).textContent = "Pause updates";
      if (panel.open) panel.close("watch");
    } catch (cause) {
      showError(signal.aborted ? "Flight startup cancelled. Check status for any submitted job."
        : cause instanceof Error ? cause.message : "The flight was not confirmed. Refresh before sending another action.");
    } finally {
      starting = null; busy = false;
      owner.disabled = profile.disabled = world.disabled = seed.disabled = duration.disabled = false;
      renderRunnerControls(); await refresh();
    }
  }
  run.addEventListener("click", () => { void startRun(); });
  quickRun.addEventListener("click", () => { void startRun(); });
  quickProfile.addEventListener("change", () => {
    chooseQuickProfile(quickProfile.value);
    renderRunnerControls();
    void requestStop();
  });
  function schedule() {
    clearTimeout(timer);
    // The quick launcher must discover a ready runner without opening the full
    // panel, so keep polling while this tab still has a Python connection.
    if (disposed || document.hidden || !autoUpdate
      || !panel.open && !confirmedView()?.active && !resultsSource()) return;
    // One existing status schedule serves both the dialog and a watched job.
    // It ends on completion, a failed connection read, page hide or Pause updates.
    timer = setTimeout(() => {
      if (!busy) void refresh(); else schedule();
    }, 2000);
  }
  byId("refresh-controller-results").addEventListener("click", () => { void refresh(); });
  byId("refresh-controller-flight").addEventListener("click", () => { void refresh(); });
  byId("reconnect-controller").addEventListener("click", () => { void refresh(); });
  setupButton.addEventListener("click", () => {
    setup.open = true;
    setup.querySelector("summary")?.focus({ preventScroll: true });
    setup.scrollIntoView({ block: "nearest" });
  });
  setup.addEventListener("toggle", () => setupButton.setAttribute("aria-expanded", String(setup.open)));
  addEventListener("wsorb:controller-status", () => {
    if (disposed) return;
    if (!sourceAvailable(resultsSource()) || !sameSource(viewSource, resultsSource())) {
      unavailableResults();
      status.textContent = "Python connection unavailable. Reconnect the runner, then check the connection.";
    } else renderRunnerControls();
    if (!document.hidden && (panel.open || trackingJob) && autoUpdate && !busy) void refresh();
  });
  const toggleUpdates = () => {
    autoUpdate = !autoUpdate;
    for (const id of ["pause-controller-updates", "pause-controller-flight-updates"]) byId(id).textContent = autoUpdate ? "Pause updates" : "Resume updates";
    renderDataStatus();
    renderRunnerControls();
    if (autoUpdate) void refresh();
    schedule();
  };
  byId("pause-controller-updates").addEventListener("click", toggleUpdates);
  byId("pause-controller-flight-updates").addEventListener("click", toggleUpdates);
  const show = (trigger: HTMLElement) => {
    if (!panel.open) {
      openingControl = trigger;
      panel.returnValue = "";
      dashboard.showView("run");
      panel.showModal();
      panel.querySelector<HTMLButtonElement>('.lab-navigation [data-lab-view="run"]')?.focus();
      renderRunnerControls();
      analytics.openPublished();
      paths.open();
      void refresh(); schedule();
    }
  };
  open.hidden = false;
  // The engine, room and local bridge announce lifecycle changes. Use them to
  // enable the quick launcher without a session that only polls forever.
  addEventListener("wsorb:controller-status", () => {
    if (disposed) return;
    void refresh(); schedule();
  });
  renderRunnerControls();
  void refresh(); schedule();
  open.addEventListener("click", () => show(open));
  function chooseFlight(linkedSelection: FlightSelection) {
    const entry = profiles.find(entry => entry.id === linkedSelection.profileId)!;
    owner.value = entry.owner; selectOwner(); profile.value = entry.id;
    quickProfile.value = entry.id;
    world.value = linkedSelection.worldId; seed.value = String(linkedSelection.seed);
    if (!Array.from(duration.options).some(option => Number(option.value) === linkedSelection.duration)) {
      duration.add(new Option(`${linkedSelection.duration} seconds`, String(linkedSelection.duration)));
    }
    duration.value = String(linkedSelection.duration); description(); worldDescription(); show(open);
    dashboard.showView("run"); run.focus();
  }
  const linkedSelection = flightSelection(new URLSearchParams(location.search));
  if (linkedSelection) chooseFlight(linkedSelection);
  pathContainer.addEventListener("click", event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const link = (event.target as Element).closest<HTMLAnchorElement>("a.comparison-fly");
    if (!link) return;
    const chosen = flightSelection(new URL(link.href).searchParams);
    if (!chosen) return;
    event.preventDefault();
    if (busy || confirmedView()?.active) { showError("Stop the current flight before choosing another code profile."); return; }
    chooseFlight(chosen);
  });
  byId("close-controllers").addEventListener("click", () => panel.close());
  watch.addEventListener("click", () => { if (!watch.disabled) panel.close("watch"); });
  byId<HTMLButtonElement>("disconnect-python").addEventListener("click", event => {
    if (!(event.currentTarget as HTMLButtonElement).hidden && panel.open) panel.close("watch");
  });
  panel.addEventListener("close", () => {
    schedule();
    const target = panel.returnValue === "watch" ? byId("simulation-canvas") : openingControl;
    target.focus({ preventScroll: true });
  });
  window.addEventListener("keydown", event => {
    if (event.code !== "KeyC" || !event.shiftKey || event.ctrlKey || event.metaKey || event.altKey || event.repeat
      || document.activeElement?.tagName !== "CANVAS") return;
    event.preventDefault(); event.stopImmediatePropagation(); show(byId("simulation-canvas"));
  }, { capture: true });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    renderRunnerControls();
    if (!document.hidden && (panel.open || trackingJob)) {
      unavailableResults();
      void refresh();
    }
  });
  const liveTimer = setInterval(() => {
    const target = byId("controller-flight-measurements");
    if (disposed || starting || document.hidden || !lastJob || !options.ready()) return;
    try {
      const observation = JSON.parse(options.engine()!.observation());
      const mission = observation.mission ?? {};
      target.textContent = `Live tab · T+${number(observation.time_s, 1)} s · Range ${number(mission.target_distance_m, 1)} m · Speed ${number(mission.relative_speed_mps, 2)} m/s · Fuel ${number(mission.fuel_used_kg, 2)} kg · Damage ${number(mission.damage, 1)} J`;
    } catch { target.textContent = "Live measurements unavailable."; }
  }, 250);
  addEventListener("pagehide", () => { disposed = true; starting?.abort(); ++refreshVersion; clearTimeout(timer); clearInterval(liveTimer); }, { once: true });
}
