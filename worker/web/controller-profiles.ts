// Named Python job selection only. Autonomy stays in Python; world creation,
// measurements and control authority stay in the real native engine.
import profiles from "../../src/wsorb/controllers/catalog.json";
import scenario from "../../simulation/scenarios/phobos_escape.json";
import { CONTROLLER_SCORE_FORMULA } from "../shared/controller-jobs.ts";
import { installFlightAnalytics } from "./flight-analytics.ts";
import type { RoomEngine } from "./room-control.ts";

type Connection = { origin: string; id: string; key: string };
type QueueRequest = (payload?: Record<string, unknown>) => Promise<Record<string, unknown>>;
type Job = { id: string; profile_id: string; status: string; stop_requested: boolean };
type View = {
  service: { online: boolean; busy: boolean };
  active: Job | null;
  results: Array<Record<string, unknown>>;
};
type LocalBridge = { status?: string; controllers?: QueueRequest };
type Options = {
  engine: () => RoomEngine | undefined;
  ready: () => boolean;
  room: () => Connection | null;
  local: () => LocalBridge | undefined;
};
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const number = (value: unknown, places = 2) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(places) : "n/a";
const practiceScoreLabel = "STP-formula score estimate, not an official runtime score";
const numericScore = (result: Record<string, unknown>) => {
  if (result.world_metadata_unchanged === false) return null;
  const score = record(result.measurement) ? result.measurement.score : null;
  return typeof score === "number" && Number.isFinite(score) ? score : null;
};
const rankingScore = (result: Record<string, unknown>) => result.state === "native_terminal"
  && result.world_metadata_unchanged === true && result.release_state === "acknowledged" && !result.error_code
  ? numericScore(result) : null;

function comparisonGroups(results: Array<Record<string, unknown>>) {
  const groups = new Map<string, { label: string; comparable: boolean; results: Array<Record<string, unknown>> }>();
  for (const [index, result] of results.slice(-32).reverse().entries()) {
    const build = typeof result.engine_sha256 === "string" && /^[0-9a-f]{64}$/.test(result.engine_sha256) ? result.engine_sha256 : null;
    const comparable = build !== null && result.seed === 100 && typeof result.duration_s === "number"
      && Number.isFinite(result.duration_s) && result.duration_s > 0 && result.duration_s <= 1800;
    // A missing build cannot establish a comparison cohort, even if a score exists.
    const key = comparable ? `${build}:${result.seed}:${result.duration_s}` : `unknown:${index}`;
    let group = groups.get(key);
    if (!group) {
      group = {
        label: comparable ? `Engine ${build}, seed 100, time limit ${result.duration_s} s`
          : "Comparison unavailable: engine build, seed or time limit not recorded.",
        comparable, results: [],
      };
      groups.set(key, group);
    }
    group.results.push(result);
  }
  return groups.values();
}

function scoreOrder(left: Record<string, unknown>, right: Record<string, unknown>) {
  const a = rankingScore(left), b = rankingScore(right);
  if (a === b) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return a > b ? -1 : 1;
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

function context(observation: Record<string, unknown>, runtime: Record<string, unknown>) {
  const fields = ["trial_revision", "tuning_revision", "manual_interventions", "seed"];
  if (fields.some(field => !Number.isSafeInteger(observation[field]) || (observation[field] as number) < 0)
    || runtime.running !== false) throw new Error("The fresh paused flight could not be verified.");
  return Object.fromEntries([...fields.map(field => [field, observation[field]]), ["running", false]]);
}

async function freshTrial(api: RoomEngine) {
  const before = JSON.parse(api.observation());
  const runtime = JSON.parse(api.runtime_status());
  if (runtime.keyboard_thrust === true || before.control_authority?.status === "active") {
    throw new Error("Stop the active controller and release manual thrust before replacing this flight.");
  }
  const result = JSON.parse(api.control(JSON.stringify({
    op: "configure_flight", preset: "far_side_l2", seed: scenario.default_seed, settings: scenario.experiment_settings,
  })));
  if (result.ok !== true) throw new Error("Fresh flight was rejected. No request was retried.");
  const end = performance.now() + 10_000;
  while (performance.now() < end) {
    const actual = JSON.parse(api.observation());
    const playback = JSON.parse(api.runtime_status());
    if (actual.trial_revision !== before.trial_revision && actual.time_s === 0 && playback.running === false
      && actual.seed === 100 && actual.flight_preset === "far_side_l2" && actual.geometry?.debris_count === 500
      && actual.ship_tuning?.mass_kg === scenario.experiment_settings.ship.mass_kg
      && actual.ship_tuning?.thrust_scale === 1 && actual.max_accel_mps2 === 0.5 && actual.max_angular_accel_rad_s2 === 0.05) {
      return context(actual, playback);
    }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error("Fresh flight was not confirmed. No reset or run was retried.");
}

export function installControllerProfiles(options: Options) {
  const panel = byId<HTMLDialogElement>("controller-panel");
  const open = byId<HTMLButtonElement>("open-controllers");
  const owner = byId<HTMLSelectElement>("controller-owner");
  const profile = byId<HTMLSelectElement>("controller-profile");
  const run = byId<HTMLButtonElement>("run-controller");
  run.disabled = true;
  const stop = byId<HTMLButtonElement>("stop-controller");
  const status = byId("controller-service-status");
  const error = byId("controller-error");
  const table = byId<HTMLTableElement>("controller-results-table");
  const sort = byId<HTMLSelectElement>("controller-result-sort");
  const analytics = installFlightAnalytics();
  let view: View | undefined;
  let busy = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let autoUpdate = true;
  const request = (): QueueRequest => {
    const room = options.room();
    if (room) return payload => hostedRequest(room, payload);
    const local = options.local();
    if (local?.status === "connected" && local.controllers) return local.controllers;
    throw new Error("No Python connection for this tab. Connect a local runner or a private hosted room.");
  };
  const selected = () => profiles.find(item => item.id === profile.value)!;
  const label = (id: unknown) => {
    const entry = profiles.find(item => item.id === id);
    return entry ? `${entry.owner} / ${entry.label}` : "Unknown profile";
  };
  function description() {
    const entry = selected();
    byId("controller-description").textContent = `${entry.description} ${entry.experimental ? "Adapted experimental profile; avoidance is not certified." : ""}`;
  }
  function selectOwner() {
    profile.replaceChildren(...profiles.filter(entry => entry.owner === owner.value).map(entry => new Option(entry.label, entry.id)));
    description();
  }
  for (const name of ["Tom", "Harry", "Bui", "WSO"]) {
    if (profiles.some(entry => entry.owner === name)) owner.add(new Option(name, name));
  }
  selectOwner();
  function renderResults(results: Array<Record<string, unknown>>) {
    const bodies = Array.from(comparisonGroups(results), group => {
      const body = document.createElement("tbody");
      const heading = body.insertRow();
      const name = document.createElement("th");
      name.scope = "rowgroup";
      name.colSpan = 13;
      name.textContent = group.label;
      heading.append(name);
      if (sort.value === "score" && group.comparable) group.results.sort(scoreOrder);
      for (const result of group.results) {
        const row = body.insertRow();
        const measurement = record(result.measurement) ? result.measurement : {};
        const terms = record(measurement.score_breakdown) && measurement.score_breakdown.formula === CONTROLLER_SCORE_FORMULA
          && numericScore(result) !== null && measurement.score_breakdown.total === numericScore(result)
          ? measurement.score_breakdown : {};
        const outcome = result.native_success === true && result.world_metadata_unchanged === true
          ? `Station region reached${result.error_code ? `; ${result.error_code}` : ""}`
          : result.error_code ? `${result.state ?? "error"}: ${result.error_code}`
            : measurement.status === "failed_damage" ? "Collision damage failure" : String(result.state ?? "Unknown");
        const score = numericScore(result);
        const scoreText = score !== null && rankingScore(result) === null ? `${number(score)} (not ranked)` : number(score);
        const values = [label(result.profile_id), scoreText, number(result.duration_s, 0), outcome,
          number(measurement.time_s, 1), number(measurement.range_m), number(measurement.relative_speed_mps), number(measurement.damage_j), number(measurement.fuel_used_kg, 3),
          number(terms.progress_points), number(terms.collision_penalty_points), number(terms.fuel_penalty_points), number(terms.offset_points)];
        values.forEach((value, index) => {
          const cell = document.createElement(index === 0 ? "th" : "td");
          if (cell instanceof HTMLTableCellElement && index === 0) cell.scope = "row";
          if (index === 1) { const score = document.createElement("strong"); score.textContent = value; cell.append(score); }
          else cell.textContent = value;
          row.append(cell);
        });
      }
      return body;
    });
    for (const body of Array.from(table.tBodies)) body.remove();
    table.append(...bodies);
  }
  function render(value: Record<string, unknown>) {
    if (!record(value.service) || typeof value.service.online !== "boolean" || !Array.isArray(value.results)) {
      throw new Error("Controller service returned an unsupported status.");
    }
    view = value as unknown as View;
    const active = view.active;
    const message = !view.service.online ? "Python runner offline. Profiles are available; start the runner to fly."
      : active ? `${label(active.profile_id)}: ${active.stop_requested ? "stopping" : active.status}.`
        : "Python runner ready. Select a teammate and profile, then run a fresh test.";
    if (status.textContent !== message) status.textContent = message;
    run.disabled = busy || !view.service.online || !!active || !options.ready();
    stop.disabled = busy || !active || active.stop_requested;
    renderResults(view.results);
    analytics.updateRoomResults(view.results);
  }
  byId("controller-score-label").textContent = practiceScoreLabel;
  sort.addEventListener("change", () => { if (view) renderResults(view.results); });
  async function refresh() {
    try { render(await request()()); }
    catch (cause) {
      view = undefined;
      analytics.updateRoomResults([]);
      run.disabled = true;
      stop.disabled = true;
      status.textContent = cause instanceof Error ? cause.message : "Controller service is unavailable.";
    }
  }
  async function requestStop() {
    if (!view?.active || busy) return;
    busy = true;
    error.textContent = "";
    try { render(await request()({ action: "stop", job_id: view.active.id })); }
    catch { error.textContent = "Stop was not confirmed. Do not start another controller; inspect the live flight and refresh."; }
    finally { busy = false; await refresh(); }
  }
  const changedProfile = async () => { description(); await requestStop(); };
  owner.addEventListener("change", () => { selectOwner(); void changedProfile(); });
  profile.addEventListener("change", () => { void changedProfile(); });
  stop.addEventListener("click", () => { void requestStop(); });
  run.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    owner.disabled = profile.disabled = true;
    run.disabled = true;
    error.textContent = "";
    try {
      const send = request();
      render(await send());
      if (!view?.service.online || view.active || !options.ready()) throw new Error("Wait for a ready game and idle Python runner before starting.");
      if (!window.confirm(`Replace this tab's flight and run ${label(profile.value)}? The new test uses seed 100 and 500 debris bodies.`)) return;
      const api = options.engine();
      if (!api) throw new Error("The real engine is unavailable. No run was started.");
      const id = selected().id;
      const expected = await freshTrial(api);
      render(await send({ action: "submit", profile_id: id, seed: 100, duration_s: Number(byId<HTMLSelectElement>("controller-duration").value), fresh: true, expected_context: expected }));
    } catch (cause) {
      error.textContent = cause instanceof Error ? cause.message : "The test was not confirmed. Refresh before sending another action.";
    } finally { busy = false; owner.disabled = profile.disabled = false; await refresh(); }
  });
  async function schedule() {
    clearTimeout(timer);
    if (!panel.open || !autoUpdate) return;
    if (!busy) await refresh();
    if (panel.open && autoUpdate) timer = setTimeout(() => { void schedule(); }, 2000);
  }
  byId("refresh-controller-results").addEventListener("click", () => { void refresh(); });
  byId<HTMLButtonElement>("pause-controller-updates").addEventListener("click", event => {
    autoUpdate = !autoUpdate;
    (event.currentTarget as HTMLButtonElement).textContent = autoUpdate ? "Pause updates" : "Resume updates";
    if (autoUpdate) void schedule(); else clearTimeout(timer);
  });
  const show = () => { if (!panel.open) { panel.showModal(); owner.focus(); void refresh(); void schedule(); } };
  open.hidden = false;
  open.addEventListener("click", show);
  byId("close-controllers").addEventListener("click", () => panel.close());
  panel.addEventListener("close", () => { clearTimeout(timer); open.focus({ preventScroll: true }); });
  window.addEventListener("keydown", event => {
    if (event.code !== "KeyC" || !event.shiftKey || event.ctrlKey || event.metaKey || event.altKey || event.repeat
      || document.activeElement?.tagName !== "CANVAS") return;
    event.preventDefault(); event.stopImmediatePropagation(); show();
  }, { capture: true });
  addEventListener("pagehide", () => clearTimeout(timer), { once: true });
}
