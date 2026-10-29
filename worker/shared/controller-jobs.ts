import catalog from "../../src/wsorb/controllers/catalog.json" with { type: "json" };

export const CONTROLLER_HEARTBEAT_MS = 15_000;
export const MAX_CONTROLLER_RESULTS = 32;
export const CONTROLLER_BODY_LIMIT = 64 * 1024;
export const CONTROLLER_EXECUTION_KIND = "asynchronous_fenced_leased_API";
export const CONTROLLER_SCORE_FORMULA = "practice_normalized_time_damage_fuel_v1";
export const CONTROLLER_ANALYTICS_DEFINITION = "native_actuation_rotation_episodes_v1";
export const CONTROLLER_ANALYTICS_COLUMNS = [
  "time_s", "fuel_used_kg", "burn_duration_s", "angular_burn_duration_s", "turn_duration_s",
  "total_turn_angle_rad", "relative_speed_mps", "range_m", "damage_j",
] as const;
const MAX_ANALYTICS_SAMPLES = 48;

export const CONTROLLER_RESULT_STATES = [
  "duration_limit", "wall_budget_limit", "native_terminal", "stopped", "interrupted", "error",
] as const;
export const CONTROLLER_MEASUREMENT_STATUSES = ["running", "success", "failed_damage", "unknown"] as const;
export const CONTROLLER_RELEASE_STATES = [
  "not_claimed", "acknowledged", "skipped_after_takeover_no_fallback", "rejected_unapplied_no_fallback",
  "page_mismatch_undelivered_no_fallback", "unconfirmed_native_watchdog_no_fallback",
] as const;
// Never store exception messages, URLs or arbitrary error strings in job history.
// A service maps errors outside this list to controller_failed.
export const CONTROLLER_ERROR_CODES = [
  "controller_failed", "interrupted", "stopped", "context_mismatch", "stale_context", "command_expired",
  "sensor_unavailable", "sensor_no_sample", "page_mismatch", "local_busy", "owner_unavailable",
  "service_offline", "service_stalled", "transport_failed", "invalid_snapshot", "source_contract",
  "control_not_ready", "engine_build_mismatch", "comparison_mismatch", "stock_world_required",
  "fresh_trial_required", "external_rate", "mission_not_running", "manual_control", "consent_ended",
  "decision_expired", "lease_expired", "play_timeout", "policy_command_bounds", "policy_ineligible",
  "claim_unapplied", "claim_mismatch", "claim_failed", "command_failed", "play_failed", "rate_failed",
  "observation_failed", "decision_failed", "run_required", "invalid_scope", "invalid_policy",
  "shared_policy_unavailable", "owner_read_protocol",
  "invalid_arguments", "invalid_options", "invalid_job", "invalid_queue_response", "queue_unavailable",
  "queue_rejected", "external_scope_required", "owner_in_use", "heartbeat_lost", "execution_failed",
  "native_input_unavailable", "setup_failed", "invocation_failed",
] as const;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;

export const controllerCatalog = catalog;

export interface ControllerContext {
  trial_revision: number;
  tuning_revision: number;
  manual_interventions: number;
  seed: 100;
  running: false;
}

export interface ControllerJob {
  id: string;
  created_at: number;
  profile_id: string;
  seed: 100;
  duration_s: number;
  expected_context: ControllerContext;
  owner_page_id: string | null;
  status: "queued" | "running" | "stopping";
  stop_requested: boolean;
}

export interface ControllerScoreBreakdown {
  formula: typeof CONTROLLER_SCORE_FORMULA;
  progress_basis: "time_ratio" | "remaining_distance_ratio";
  progress_points: number;
  collision_penalty_points: number;
  fuel_penalty_points: number;
  offset_points: number;
  total: number;
  nominal_time_s: number;
  nominal_fuel_kg: number;
  impact_threshold_j: number;
}

interface ControllerMeasurement {
  time_s: number | null;
  range_m: number | null;
  relative_speed_mps: number | null;
  damage_j: number | null;
  fuel_used_kg: number | null;
  status: typeof CONTROLLER_MEASUREMENT_STATUSES[number];
  // Old persisted rows and older clients have neither score field.
  score?: number | null;
  score_breakdown?: ControllerScoreBreakdown | null;
}

export interface ControllerResult {
  id: string;
  profile_id: string;
  seed: 100;
  duration_s: number;
  state: typeof CONTROLLER_RESULT_STATES[number];
  native_success: boolean;
  measurement: ControllerMeasurement;
  release_state: typeof CONTROLLER_RELEASE_STATES[number];
  world_metadata_unchanged: boolean;
  error_code: typeof CONTROLLER_ERROR_CODES[number] | null;
  execution_kind: typeof CONTROLLER_EXECUTION_KIND;
  engine_sha256: string | null;
  elapsed_s: number;
  analytics?: ControllerFlightAnalytics | null;
}

export interface ControllerFlightAnalytics {
  schema_version: 1;
  definition_id: typeof CONTROLLER_ANALYTICS_DEFINITION;
  source_kind: "native_physics_step_counters_with_sampled_public_series";
  totals: Record<string, number | string | boolean>;
  columns: typeof CONTROLLER_ANALYTICS_COLUMNS;
  samples: Array<Array<number | null>>;
  sample_count: number;
}

export interface ControllerJobsState {
  service: { worker_id: string; last_seen_at: number } | null;
  active: { job: ControllerJob; worker_id: string | null; stalled: boolean } | null;
  results: ControllerResult[];
}

interface ActionContext {
  now: number;
  jobId: string;
  ownerPageId: string | null;
  ownerAvailable: boolean;
}

export class ControllerJobsError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function invalid(message: string): never { throw new ControllerJobsError(400, message); }
function conflict(message: string): never { throw new ControllerJobsError(409, message); }
function shape(value: unknown, required: string[], optional: string[] = []): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)
    || required.some(key => !Object.hasOwn(value, key))
    || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) {
    return invalid("Unexpected controller fields or missing required fields");
  }
  return value as Record<string, unknown>;
}
function uuid(value: unknown): string {
  if (typeof value !== "string" || !UUID.test(value)) return invalid("Controller IDs must be canonical UUIDs");
  return value;
}
function choice<T extends string>(value: unknown, values: readonly T[], name: string): T {
  if (typeof value !== "string" || !values.includes(value as T)) return invalid(`Invalid controller ${name}`);
  return value as T;
}
function boolean(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") return invalid(`Controller ${name} must be a boolean`);
  return value;
}
function measurementNumber(value: unknown): number | null {
  if (value === null || (typeof value === "number" && Number.isFinite(value))) return value;
  return invalid("Controller measurements must be finite numbers or null");
}
function scoreBreakdown(value: unknown, score: number | null): ControllerScoreBreakdown | null {
  if (value === null) return null;
  const numbers = [
    "progress_points", "collision_penalty_points", "fuel_penalty_points", "offset_points", "total",
    "nominal_time_s", "nominal_fuel_kg", "impact_threshold_j",
  ] as const;
  const fields = shape(value, ["formula", "progress_basis", ...numbers]);
  if (fields.formula !== CONTROLLER_SCORE_FORMULA) invalid("Invalid controller score formula");
  for (const name of numbers) {
    if (typeof fields[name] !== "number" || !Number.isFinite(fields[name])) invalid("Controller score terms must be finite numbers");
  }
  for (const name of ["collision_penalty_points", "fuel_penalty_points"] as const) {
    if ((fields[name] as number) < 0) invalid("Controller score penalties must be non-negative");
  }
  for (const name of ["nominal_time_s", "nominal_fuel_kg", "impact_threshold_j"] as const) {
    if ((fields[name] as number) <= 0) invalid("Controller score normalization values must be positive");
  }
  if (score === null || fields.total !== score) invalid("Controller score total must match the native score");
  return {
    formula: CONTROLLER_SCORE_FORMULA,
    progress_basis: choice(fields.progress_basis, ["time_ratio", "remaining_distance_ratio"], "score progress basis"),
    progress_points: fields.progress_points as number,
    collision_penalty_points: fields.collision_penalty_points as number,
    fuel_penalty_points: fields.fuel_penalty_points as number,
    offset_points: fields.offset_points as number,
    total: fields.total as number,
    nominal_time_s: fields.nominal_time_s as number,
    nominal_fuel_kg: fields.nominal_fuel_kg as number,
    impact_threshold_j: fields.impact_threshold_j as number,
  };
}

function flightAnalytics(value: unknown): ControllerFlightAnalytics | null {
  if (value === null) return null;
  const data = shape(value, ["schema_version", "definition_id", "source_kind", "totals", "columns", "samples", "sample_count"]);
  if (data.schema_version !== 1 || data.definition_id !== CONTROLLER_ANALYTICS_DEFINITION
    || data.source_kind !== "native_physics_step_counters_with_sampled_public_series") invalid("Unsupported flight analytics definition");
  const numbers = [
    "observed_sim_s", "burn_count", "burn_duration_s", "longest_burn_s", "angular_burn_count",
    "angular_burn_duration_s", "longest_angular_burn_s", "turn_count", "turn_duration_s", "longest_turn_s",
    "total_turn_angle_rad", "peak_angular_speed_rad_s", "propellant_impulse_ns", "angular_impulse_nms",
    "current_burn_s", "current_angular_burn_s", "current_turn_s",
  ] as const;
  const flags = ["burn_active", "angular_burn_active", "turn_active"] as const;
  const native = shape(data.totals, ["schema_version", "definition_id", ...numbers, ...flags]);
  if (native.schema_version !== 1 || native.definition_id !== CONTROLLER_ANALYTICS_DEFINITION) invalid("Unsupported native analytics receipt");
  const totals: Record<string, number | string | boolean> = { schema_version: 1, definition_id: CONTROLLER_ANALYTICS_DEFINITION };
  for (const name of numbers) {
    if (typeof native[name] !== "number" || !Number.isFinite(native[name]) || (native[name] as number) < 0) invalid("Analytics measurements must be finite and non-negative");
    totals[name] = native[name] as number;
  }
  for (const name of ["burn_count", "angular_burn_count", "turn_count"] as const) {
    if (!Number.isSafeInteger(totals[name])) invalid("Analytics counts must be safe integers");
  }
  for (const name of flags) totals[name] = boolean(native[name], "analytics activity");
  for (const [duration, longest, current] of [
    ["burn_duration_s", "longest_burn_s", "current_burn_s"],
    ["angular_burn_duration_s", "longest_angular_burn_s", "current_angular_burn_s"],
    ["turn_duration_s", "longest_turn_s", "current_turn_s"],
  ]) {
    if ((totals[duration] as number) > (totals.observed_sim_s as number) + 1e-6
      || (totals[longest] as number) > (totals[duration] as number) + 1e-6
      || (totals[current] as number) > (totals[longest] as number) + 1e-6) invalid("Analytics durations exceed their observed interval");
  }
  if (!Array.isArray(data.columns) || data.columns.length !== CONTROLLER_ANALYTICS_COLUMNS.length
    || data.columns.some((name, index) => name !== CONTROLLER_ANALYTICS_COLUMNS[index])) invalid("Invalid analytics columns");
  if (!Array.isArray(data.samples) || data.samples.length === 0 || data.samples.length > MAX_ANALYTICS_SAMPLES
    || !Number.isSafeInteger(data.sample_count) || (data.sample_count as number) < data.samples.length) invalid("Invalid bounded analytics series");
  let previous = -1;
  const samples: Array<Array<number | null>> = [];
  for (const row of data.samples) {
    if (!Array.isArray(row) || row.length !== CONTROLLER_ANALYTICS_COLUMNS.length
      || row.some(value => value !== null && (typeof value !== "number" || !Number.isFinite(value) || value < 0))
      || typeof row[0] !== "number" || row[0] <= previous || row[0] > (totals.observed_sim_s as number) + 1e-6) invalid("Invalid analytics sample interval");
    previous = row[0];
    samples.push([...row]);
  }
  return {
    schema_version: 1, definition_id: CONTROLLER_ANALYTICS_DEFINITION,
    source_kind: "native_physics_step_counters_with_sampled_public_series", totals,
    columns: CONTROLLER_ANALYTICS_COLUMNS, samples, sample_count: data.sample_count as number,
  };
}
function context(value: unknown): ControllerContext {
  const fields = shape(value, ["trial_revision", "tuning_revision", "manual_interventions", "seed", "running"]);
  for (const name of ["trial_revision", "tuning_revision", "manual_interventions", "seed"]) {
    const number = fields[name];
    if (typeof number !== "number" || !Number.isInteger(number) || number < 0 || number > 0xffffffff) {
      invalid("Controller context revisions and seed must be unsigned 32-bit integers");
    }
  }
  if (fields.seed !== 100 || fields.running !== false) invalid("A controller job requires paused seed 100 context");
  return {
    trial_revision: fields.trial_revision as number, tuning_revision: fields.tuning_revision as number,
    manual_interventions: fields.manual_interventions as number, seed: 100, running: false,
  };
}

function resultSummary(value: unknown, job: ControllerJob): ControllerResult {
  const summary = shape(value, [
    "state", "native_success", "measurement", "release_state", "world_metadata_unchanged", "error_code",
    "execution_kind", "engine_sha256", "elapsed_s",
  ], ["id", "profile_id", "seed", "duration_s", "analytics"]);
  for (const name of ["id", "profile_id", "seed", "duration_s"] as const) {
    if (Object.hasOwn(summary, name) && summary[name] !== job[name]) invalid("Controller result identity differs from its job");
  }
  const measurement = shape(summary.measurement, ["time_s", "range_m", "relative_speed_mps", "damage_j", "fuel_used_kg", "status"], ["score", "score_breakdown"]);
  const score = Object.hasOwn(measurement, "score") ? measurementNumber(measurement.score) : null;
  const breakdown = scoreBreakdown(Object.hasOwn(measurement, "score_breakdown") ? measurement.score_breakdown : null, score);
  if (summary.execution_kind !== CONTROLLER_EXECUTION_KIND) invalid("Invalid controller execution kind");
  if (summary.engine_sha256 !== null && (typeof summary.engine_sha256 !== "string" || !SHA256.test(summary.engine_sha256))) {
    invalid("Controller engine_sha256 must be a lowercase SHA-256 or null");
  }
  if (typeof summary.elapsed_s !== "number" || !Number.isFinite(summary.elapsed_s) || summary.elapsed_s < 0) {
    invalid("Controller elapsed_s must be a finite non-negative number");
  }
  return {
    id: job.id, profile_id: job.profile_id, seed: job.seed, duration_s: job.duration_s,
    state: choice(summary.state, CONTROLLER_RESULT_STATES, "result state"),
    native_success: boolean(summary.native_success, "native_success"),
    measurement: {
      time_s: measurementNumber(measurement.time_s), range_m: measurementNumber(measurement.range_m),
      relative_speed_mps: measurementNumber(measurement.relative_speed_mps), damage_j: measurementNumber(measurement.damage_j),
      fuel_used_kg: measurementNumber(measurement.fuel_used_kg),
      status: choice(measurement.status, CONTROLLER_MEASUREMENT_STATUSES, "measurement status"),
      score, score_breakdown: breakdown,
    },
    release_state: choice(summary.release_state, CONTROLLER_RELEASE_STATES, "release state"),
    world_metadata_unchanged: boolean(summary.world_metadata_unchanged, "world_metadata_unchanged"),
    error_code: summary.error_code === null ? null : choice(summary.error_code, CONTROLLER_ERROR_CODES, "error code"),
    execution_kind: CONTROLLER_EXECUTION_KIND, engine_sha256: summary.engine_sha256 as string | null,
    elapsed_s: summary.elapsed_s,
    analytics: flightAnalytics(Object.hasOwn(summary, "analytics") ? summary.analytics : null),
  };
}

export function createControllerJobsState(): ControllerJobsState {
  return { service: null, active: null, results: [] };
}

function heartbeatOnline(state: ControllerJobsState, now: number): boolean {
  return state.service !== null && now >= state.service.last_seen_at && now - state.service.last_seen_at <= CONTROLLER_HEARTBEAT_MS;
}
function stalled(state: ControllerJobsState, now: number): boolean {
  return state.active !== null && (state.active.stalled || !heartbeatOnline(state, now));
}

export function controllerJobsView(state: ControllerJobsState, now: number) {
  const isStalled = stalled(state, now);
  return {
    catalog: controllerCatalog,
    service: { online: heartbeatOnline(state, now) && !isStalled, busy: state.active !== null },
    active: state.active ? {
      ...state.active.job, expected_context: { ...state.active.job.expected_context },
      status: isStalled ? "stalled" : state.active.job.status,
    } : null,
    results: state.results.map(result => ({
      ...result,
      ...(result.analytics ? { analytics: structuredClone(result.analytics) } : {}),
      measurement: {
        ...result.measurement,
        ...(result.measurement.score_breakdown ? { score_breakdown: { ...result.measurement.score_breakdown } } : {}),
      },
    })),
  };
}

// Synchronous and free of I/O, clocks, ID generation and native engine calls.
// Adapters must load, transition and save without an intervening await.
export function controllerJobsAction(state: ControllerJobsState, input: unknown, env: ActionContext) {
  const action = shape(input, ["action"], ["profile_id", "seed", "duration_s", "fresh", "expected_context", "job_id", "worker_id", "result"]);
  if (action.action === "submit") {
    shape(input, ["action", "profile_id", "seed", "duration_s", "fresh", "expected_context"]);
    if (typeof action.profile_id !== "string" || !controllerCatalog.some(profile => profile.id === action.profile_id)) {
      invalid("Unknown controller profile");
    }
    if (action.seed !== 100 || action.fresh !== true) invalid("Controller jobs require seed 100 and fresh: true");
    if (typeof action.duration_s !== "number" || !Number.isFinite(action.duration_s) || action.duration_s <= 0 || action.duration_s > 1800) {
      invalid("Controller duration_s must be greater than zero and at most 1800");
    }
    const expectedContext = context(action.expected_context);
    if (state.active) conflict("A controller job is already active");
    if (!heartbeatOnline(state, env.now)) throw new ControllerJobsError(503, "Controller service is offline");
    if (!env.ownerAvailable) throw new ControllerJobsError(503, "No connected simulation owner");
    const next: ControllerJobsState = { ...state, active: {
      job: {
        id: env.jobId, created_at: env.now, profile_id: action.profile_id, seed: 100,
        duration_s: action.duration_s, expected_context: expectedContext, owner_page_id: env.ownerPageId,
        status: "queued", stop_requested: false,
      },
      worker_id: null, stalled: false,
    } };
    return { state: next, response: controllerJobsView(next, env.now) };
  }
  if (action.action === "stop") {
    shape(input, ["action", "job_id"]);
    const jobId = uuid(action.job_id);
    if (!state.active || state.active.job.id !== jobId) conflict("No matching active controller job");
    if (stalled(state, env.now)) {
      // Explicitly abandon a lost service's job, never replay it. No native
      // measurement or cleanup is invented. A later fresh test still has to
      // verify native ownership, context and watchdog state before it can run.
      const job = state.active.job;
      const summary: ControllerResult = {
        id: job.id, profile_id: job.profile_id, seed: job.seed, duration_s: job.duration_s,
        state: "interrupted", native_success: false,
        measurement: { time_s: null, range_m: null, relative_speed_mps: null, damage_j: null, fuel_used_kg: null, status: "unknown", score: null, score_breakdown: null },
        release_state: state.active.worker_id === null ? "not_claimed" : "unconfirmed_native_watchdog_no_fallback",
        world_metadata_unchanged: false, error_code: "service_stalled", execution_kind: CONTROLLER_EXECUTION_KIND,
        engine_sha256: null, elapsed_s: Math.max(0, (env.now - job.created_at) / 1000),
      };
      const next: ControllerJobsState = { ...state, active: null, results: [...state.results.slice(-(MAX_CONTROLLER_RESULTS - 1)), summary] };
      return { state: next, response: controllerJobsView(next, env.now) };
    }
    const next: ControllerJobsState = { ...state, active: {
      ...state.active, stalled: stalled(state, env.now),
      job: { ...state.active.job, stop_requested: true, status: "stopping" },
    } };
    return { state: next, response: controllerJobsView(next, env.now) };
  }
  if (action.action === "poll") {
    shape(input, ["action", "worker_id"]);
    const workerId = uuid(action.worker_id);
    if ((state.service && state.service.worker_id !== workerId && heartbeatOnline(state, env.now))
      || (state.active?.worker_id && state.active.worker_id !== workerId)) {
      conflict("Another controller service owns this lease");
    }
    const isStalled = stalled(state, env.now);
    const next: ControllerJobsState = { ...state, service: { worker_id: workerId, last_seen_at: env.now } };
    if (state.active) {
      next.active = {
        ...state.active, worker_id: workerId, stalled: isStalled,
        job: { ...state.active.job, status: state.active.job.stop_requested ? "stopping" : "running" },
      };
    }
    // A stale assigned job is never handed out again, even to its old worker.
    // That worker may still acknowledge failure via complete using its old IDs.
    return { state: next, response: {
      job: next.active && !isStalled ? { ...next.active.job, expected_context: { ...next.active.job.expected_context } } : null,
      stop_requested: isStalled || next.active?.job.stop_requested === true,
    } };
  }
  if (action.action === "complete") {
    shape(input, ["action", "worker_id", "job_id", "result"]);
    const workerId = uuid(action.worker_id), jobId = uuid(action.job_id);
    if (!state.active || state.active.job.id !== jobId || state.active.worker_id !== workerId) {
      conflict("No matching controller job assigned to this service");
    }
    const summary = resultSummary(action.result, state.active.job);
    const next: ControllerJobsState = { ...state, active: null, results: [...state.results.slice(-(MAX_CONTROLLER_RESULTS - 1)), summary] };
    return { state: next, response: controllerJobsView(next, env.now) };
  }
  return invalid("Unknown controller action");
}
