// Read-only presentation of published trials and genuine job receipts. This
// module has no engine, connection, command or controller-selection capability.
import profiles from "../../src/wsorb/controllers/catalog.json";
import definitions from "../../simulation/scenarios/flight_analytics.json";
import scoringReference from "../../src/wsorb/scoring_reference.json";
import { CONTROLLER_EXECUTION_KIND, CONTROLLER_SCORE_FORMULA } from "../shared/controller-jobs.ts";
import { installAnalyticsOverview } from "./analytics-overview.ts";
import { installChartInspector, type ChartInspectionPoint } from "./chart-inspector.ts";

const DEFINITION_ID = "native_actuation_rotation_episodes_v1";
const SOURCE_KIND = "native_physics_step_counters_with_sampled_public_series";
const SCORE_COMPATIBILITY = scoringReference.local_comparison.score_compatibility;
const MAX_PUBLIC_BYTES = 2 * 1024 * 1024;
const MAX_SCHEMA_SAMPLES = 256;
const MAX_PUBLIC_SAMPLES = 192;
const MAX_ROOM_SAMPLES = 48;
const COLUMNS = ["time_s", "fuel_used_kg", "burn_duration_s", "angular_burn_duration_s", "turn_duration_s",
  "total_turn_angle_rad", "relative_speed_mps", "range_m", "damage_j"] as const;
const EPISODES = ["burn", "angular_burn", "turn"] as const;
const COUNTERS = EPISODES.map(name => `${name}_count`);
const NATIVE_NUMBERS = ["observed_sim_s", ...EPISODES.flatMap(name => [`${name}_duration_s`, `longest_${name}_s`]),
  "total_turn_angle_rad", "peak_angular_speed_rad_s", "propellant_impulse_ns", "angular_impulse_nms",
  ...EPISODES.map(name => `current_${name}_s`)];
const FLAGS = EPISODES.map(name => `${name}_active`);
const METRICS = [
  { label: "Cumulative fuel used", unit: "kg", column: 1, guidance: "Use less fuel for a completed arrival. These are local fuel estimates." },
  { label: "Translation burn time", unit: "s", column: 2, guidance: "Less engine-on time to reach the station is better. Compare fuel and damage too." },
  { label: "Angular actuation time", unit: "s", column: 3, guidance: "Avoid unnecessary angular thrust. The lander can keep turning after thrust ends." },
  { label: "Physical turn time", unit: "s", column: 4, guidance: "Reduce unnecessary spin. Physical turning and angular thrust are different measurements." },
  { label: "Total orientation angle", unit: "rad", column: 5, guidance: "This is total rotation travelled, not remaining heading error. Avoid needless rotation." },
  { label: "Relative speed", unit: "m/s", column: 6, guidance: "A slower arrival is safer. Arrival speed is separate from the completion-time score term." },
  { label: "Target range", unit: "m", column: 7, guidance: "Smaller means closer to the station. Local arrival is entry within 50 m, not docking." },
  { label: "Cumulative damage", unit: "J", column: 8, guidance: "Less collision damage is better. Damage above 209,200 J ends the local flight." },
];
const TOTAL_LABELS: Record<string, string> = {
  observed_sim_s: "Observed simulation time, s", burn_count: "Translation burn episodes",
  burn_duration_s: "Translation burn time, s", longest_burn_s: "Longest translation burn, s",
  angular_burn_count: "Angular actuation episodes", angular_burn_duration_s: "Angular actuation time, s",
  longest_angular_burn_s: "Longest angular actuation, s", turn_count: "Physical turn episodes",
  turn_duration_s: "Physical turn time, s", longest_turn_s: "Longest physical turn, s",
  total_turn_angle_rad: "Total physical orientation angle, rad", peak_angular_speed_rad_s: "Peak angular speed, rad/s",
  propellant_impulse_ns: "Propellant force impulse, N s", angular_impulse_nms: "Actuator torque impulse, N m s",
  burn_active: "Translation burn active in last interval", angular_burn_active: "Angular actuation active in last interval",
  turn_active: "Physical turn active in last interval", current_burn_s: "Current translation burn, s",
  current_angular_burn_s: "Current angular actuation, s", current_turn_s: "Current physical turn, s",
};
const PROFILE_MEDIANS = ["median_time_s", "median_fuel_used_kg", "median_damage_j", "median_burn_count",
  "median_burn_duration_s", "median_angular_burn_count", "median_angular_burn_duration_s", "median_turn_count",
  "median_turn_duration_s", "median_total_turn_angle_rad"];
const OPTIONAL_PROFILE_STATS = ["median_score", "min_score", "max_score", "score_stddev", "success_rate",
  "median_range_m", "median_relative_speed_mps", "median_longest_burn_s", "median_longest_angular_burn_s",
  "median_longest_turn_s", "median_peak_angular_speed_rad_s", "median_propellant_impulse_ns", "median_angular_impulse_nms",
  "unscored_runs", "physical_runs", "analytics_runs", "paired_scores", "outcomes", "metric_runs"];
const PUBLIC_OUTCOMES = ["success", "failed_damage", "timeout", "interrupted", "engine_error", "policy_error"];
type Totals = Record<string, number | boolean | string>;
type RunAnalytics = { schema_version: 1; definition_id: string; source_kind: string; totals: Totals;
  columns: string[]; samples: Array<Array<number | null>>; sample_count: number };
type Measurement = Record<typeof MEASUREMENT_FIELDS[number], number | null>;
const MEASUREMENT_FIELDS = ["time_s", "range_m", "relative_speed_mps", "damage_j", "fuel_used_kg"] as const;
type ScoreTerms = Record<string, number | string>;
type PlotRun = { id: string; profileId: string; label: string; seed: number; repetition: number | null; outcome: string; score: number | null;
  terms: ScoreTerms | null; measurement: Measurement; analytics: RunAnalytics | null; engineHash: string | null;
  cohortKey: string; cohortLabel: string };
type PublishedProfile = { id: string; owner: string; label: string; attempts: number; successes: number; scored_runs: number;
  scored_pairs: number; mean_score: number | null } & Record<string, unknown>;
type Publication = { comparison: Record<string, unknown>; profiles: PublishedProfile[]; runs: PlotRun[]; rankable: boolean };
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const nonnegative = (value: unknown): value is number => finite(value) && value >= 0;
const integer = (value: unknown, max = Number.MAX_SAFE_INTEGER): value is number => nonnegative(value) && Number.isSafeInteger(value) && value <= max;
const text = (value: unknown, max = 300): value is string => typeof value === "string" && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value);
const sha256 = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const identifier = (value: unknown): value is string => typeof value === "string" && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$/.test(value);
const same = (left: number, right: number) => Math.abs(left - right) <= 1e-7 * Math.max(1, left, right);
const exact = (value: unknown) => finite(value) ? String(value) : typeof value === "boolean" ? (value ? "Yes" : "No") : "n/a";
const rounded = (value: unknown, digits = 2) => finite(value) ? value.toFixed(digits) : "n/a";
function shape(value: unknown, required: readonly string[], optional: readonly string[] = []): value is Record<string, unknown> {
  return record(value) && required.every(key => Object.hasOwn(value, key))
    && Object.keys(value).every(key => required.includes(key) || optional.includes(key));
}

function validateAnalytics(value: unknown, maximum: number): RunAnalytics | null {
  if (!shape(value, ["schema_version", "definition_id", "source_kind", "totals", "columns", "samples", "sample_count"])
    || value.schema_version !== 1 || value.definition_id !== DEFINITION_ID || value.source_kind !== SOURCE_KIND
    || !Array.isArray(value.columns) || value.columns.length !== COLUMNS.length || value.columns.some((name, i) => name !== COLUMNS[i])
    || !Array.isArray(value.samples) || value.samples.length < 1 || value.samples.length > Math.min(maximum, MAX_SCHEMA_SAMPLES)
    || value.sample_count !== value.samples.length) return null;
  const totals = value.totals;
  if (!shape(totals, ["schema_version", "definition_id", ...COUNTERS, ...NATIVE_NUMBERS, ...FLAGS])
    || totals.schema_version !== 1 || totals.definition_id !== DEFINITION_ID
    || COUNTERS.some(key => !integer(totals[key])) || NATIVE_NUMBERS.some(key => !nonnegative(totals[key]))
    || FLAGS.some(key => typeof totals[key] !== "boolean")) return null;
  const observed = totals.observed_sim_s as number;
  for (const name of EPISODES) {
    const count = totals[`${name}_count`] as number, duration = totals[`${name}_duration_s`] as number;
    const longest = totals[`longest_${name}_s`] as number, current = totals[`current_${name}_s`] as number;
    const tolerance = 1e-7 * Math.max(1, observed, duration);
    if (duration > observed + tolerance || longest > duration + tolerance || current > longest + tolerance
      || (!totals[`${name}_active`] && current !== 0) || (totals[`${name}_active`] && (current <= 0 || count === 0))
      || (count === 0 && (duration !== 0 || longest !== 0)) || (count > 0 && (duration <= 0 || longest <= 0 || longest + tolerance < duration / count))) return null;
  }
  if (observed === 0 && [...COUNTERS, ...NATIVE_NUMBERS].some(key => totals[key] !== 0)) return null;
  const rows: Array<Array<number | null>> = [];
  const previous = new Map<number, { time: number; value: number }>();
  const cumulativeTotals: Record<number, string> = { 2: "burn_duration_s", 3: "angular_burn_duration_s", 4: "turn_duration_s", 5: "total_turn_angle_rad" };
  for (const raw of value.samples) {
    if (!Array.isArray(raw) || raw.length !== COLUMNS.length || !nonnegative(raw[0])
      || raw.some((cell, i) => i > 0 && cell !== null && !nonnegative(cell))) return null;
    const time = raw[0] as number;
    if ((rows.length && time <= rows[rows.length - 1][0]!) || (time > observed && !same(time, observed))) return null;
    for (const column of [1, 2, 3, 4, 5, 8]) {
      const cell = raw[column];
      if (cell === null) continue;
      const old = previous.get(column);
      if (old && cell < old.value && !same(cell, old.value)) return null;
      if (Object.hasOwn(cumulativeTotals, column)) {
        const total = totals[cumulativeTotals[column]] as number;
        if ((cell > total && !same(cell, total)) || (!rows.length && cell !== 0)
          || (column < 5 && cell > time && !same(cell, time))
          || (column < 5 && old && cell - old.value > time - old.time && !same(cell - old.value, time - old.time))) return null;
      }
      previous.set(column, { time, value: cell });
    }
    rows.push(raw.slice());
  }
  if (rows[0][0] !== 0 || !same(rows[rows.length - 1][0]!, observed)) return null;
  for (const [column, key] of Object.entries(cumulativeTotals)) {
    const cell = rows[rows.length - 1][Number(column)];
    if (cell !== null && !same(cell, totals[key] as number)) return null;
  }
  return { schema_version: 1, definition_id: DEFINITION_ID, source_kind: SOURCE_KIND,
    totals: { ...totals } as Totals, columns: [...COLUMNS], samples: rows, sample_count: rows.length };
}

function validateTerms(value: unknown, score: number | null): ScoreTerms | null {
  const numbers = ["progress_points", "collision_penalty_points", "fuel_penalty_points", "offset_points", "total",
    "nominal_time_s", "nominal_fuel_kg", "impact_threshold_j"];
  if (!shape(value, ["formula", "progress_basis", ...numbers]) || value.formula !== CONTROLLER_SCORE_FORMULA
    || !["time_ratio", "remaining_distance_ratio"].includes(value.progress_basis as string)
    || numbers.some(key => !finite(value[key])) || score === null || value.total !== score
    || ["collision_penalty_points", "fuel_penalty_points"].some(key => (value[key] as number) < 0)
    || ["nominal_time_s", "nominal_fuel_kg", "impact_threshold_j"].some(key => (value[key] as number) <= 0)) return null;
  return { ...value } as ScoreTerms;
}

function validateProfileStats(entry: Record<string, unknown>, seeds: unknown[]): boolean {
  const structured = ["paired_scores", "outcomes", "metric_runs"];
  if (OPTIONAL_PROFILE_STATS.filter(key => !structured.includes(key)).some(key => Object.hasOwn(entry, key) && entry[key] !== null && !finite(entry[key]))) return false;
  for (const key of ["unscored_runs", "physical_runs", "analytics_runs"]) {
    if (Object.hasOwn(entry, key) && !integer(entry[key], entry.attempts as number)) return false;
  }
  if (Object.hasOwn(entry, "outcomes") && (!record(entry.outcomes)
    || Object.entries(entry.outcomes).some(([key, count]) => !PUBLIC_OUTCOMES.includes(key) || !integer(count, entry.attempts as number))
    || Object.values(entry.outcomes).reduce<number>((sum, count) => sum + (count as number), 0) !== entry.attempts)) return false;
  if (Object.hasOwn(entry, "metric_runs") && (!shape(entry.metric_runs, MEASUREMENT_FIELDS)
    || Object.values(entry.metric_runs).some(count => !integer(count, entry.attempts as number)))) return false;
  if (Object.hasOwn(entry, "paired_scores")) {
    if (!Array.isArray(entry.paired_scores) || entry.paired_scores.length !== seeds.length) return false;
    const seen = new Set();
    for (const pair of entry.paired_scores) {
      if (!shape(pair, ["seed", "run_ids", "mean_score", "outcomes"]) || !seeds.includes(pair.seed) || seen.has(pair.seed)
        || !Array.isArray(pair.run_ids) || pair.run_ids.length !== 2 || pair.run_ids.some(id => !identifier(id))
        || (pair.mean_score !== null && !finite(pair.mean_score)) || !Array.isArray(pair.outcomes) || pair.outcomes.length !== 2
        || pair.outcomes.some(outcome => !PUBLIC_OUTCOMES.includes(outcome))) return false;
      seen.add(pair.seed);
    }
  }
  return true;
}

export function validatePublication(value: unknown): Publication | null {
  if (!shape(value, ["schema", "schema_version", "comparison", "profiles", "trials"])
    || value.schema !== "wsorb.teammate_analytics" || ![1, 2].includes(value.schema_version as number)
    || !Array.isArray(value.profiles) || value.profiles.length < 1 || value.profiles.length > profiles.length
    || !Array.isArray(value.trials) || value.trials.length < 1 || value.trials.length > 256) return null;
  const comparison = value.comparison;
  if (!shape(comparison, ["preset", "seeds", "repetitions", "debris_count", "duration_limit_s", "controller_step_s", "physics_step_s",
    "execution_kind", "score_formula_id", "engine_build", "definitions", "source_hashes", "ranking_method", "limitations"], ["score_compatibility"])
    || (value.schema_version === 2 && !Object.hasOwn(comparison, "score_compatibility"))
    || (Object.hasOwn(comparison, "score_compatibility") && (!shape(comparison.score_compatibility, Object.keys(SCORE_COMPATIBILITY))
      || Object.entries(SCORE_COMPATIBILITY).some(([key, expected]) => (comparison.score_compatibility as Record<string, unknown>)[key] !== expected)))
    || !["far_side_l2", "mars_facing_l1", "competition_reference"].includes(comparison.preset as string)
    || !Array.isArray(comparison.seeds) || !comparison.seeds.length || comparison.seeds.length > 64
    || comparison.seeds.some(seed => !integer(seed, 0xffffffff)) || new Set(comparison.seeds).size !== comparison.seeds.length || comparison.repetitions !== 2
    || !integer(comparison.debris_count, 100000) || !nonnegative(comparison.duration_limit_s) || comparison.duration_limit_s === 0
    || comparison.duration_limit_s > 86400 || !nonnegative(comparison.controller_step_s) || comparison.controller_step_s === 0
    || comparison.controller_step_s > 30 || !nonnegative(comparison.physics_step_s) || comparison.physics_step_s === 0
    || comparison.physics_step_s > comparison.controller_step_s || !["owned_native_manual_step", CONTROLLER_EXECUTION_KIND].includes(comparison.execution_kind as string)
    || (comparison.score_formula_id !== null && comparison.score_formula_id !== CONTROLLER_SCORE_FORMULA) || !text(comparison.ranking_method, 300)
    || !shape(comparison.engine_build, ["bytes", "sha256"]) || !integer(comparison.engine_build.bytes, 100 * 1024 * 1024)
    || comparison.engine_build.bytes === 0 || !sha256(comparison.engine_build.sha256)
    || (comparison.definitions !== null && (!shape(comparison.definitions, Object.keys(definitions))
      || Object.entries(definitions).some(([key, expected]) => (comparison.definitions as Record<string, unknown>)[key] !== expected)))
    || !record(comparison.source_hashes) || Object.keys(comparison.source_hashes).length > 128
    || Object.entries(comparison.source_hashes).some(([path, hash]) => !/^[a-zA-Z0-9_][a-zA-Z0-9_./-]{0,199}$/.test(path)
      || path.split("/").includes("..") || !sha256(hash))
    || !Array.isArray(comparison.limitations) || comparison.limitations.length > 24 || comparison.limitations.some(item => !text(item, 1000))) return null;
  const summaries: PublishedProfile[] = [];
  for (const entry of value.profiles) {
    if (!shape(entry, ["id", "owner", "label", "attempts", "successes", "scored_runs", "scored_pairs", "mean_score", ...PROFILE_MEDIANS], OPTIONAL_PROFILE_STATS)) return null;
    const known = profiles.find(profile => profile.id === entry.id);
    if (!known || entry.owner !== known.owner || entry.label !== known.label || summaries.some(profile => profile.id === entry.id)
      || !integer(entry.attempts, 256) || !integer(entry.successes, entry.attempts) || !integer(entry.scored_runs, entry.attempts)
      || !integer(entry.scored_pairs, comparison.seeds.length) || (entry.mean_score !== null && !finite(entry.mean_score))
      || (entry.scored_pairs === 0) !== (entry.mean_score === null)
      || entry.attempts !== comparison.seeds.length * comparison.repetitions || PROFILE_MEDIANS.some(key => entry[key] !== null && !nonnegative(entry[key]))
      || !validateProfileStats(entry, comparison.seeds)) return null;
    summaries.push({ ...entry } as PublishedProfile);
  }
  const runs: PlotRun[] = [];
  const pairs = new Set<string>();
  const outcomes: Record<string, string> = { success: "Station region reached", failed_damage: "Collision damage failure",
    timeout: "Time limit", interrupted: "Interrupted", engine_error: "Engine error", policy_error: "Policy error" };
  for (const trial of value.trials) {
    if (!shape(trial, ["id", "profile_id", "owner", "label", "seed", "repetition", "outcome", "score", "score_breakdown", "measurement", "analytics", "engine_sha256"])) return null;
    const summary = summaries.find(entry => entry.id === trial.profile_id);
    const pair = `${trial.profile_id}:${trial.seed}:${trial.repetition}`;
    if (!summary || trial.owner !== summary.owner || trial.label !== summary.label || !identifier(trial.id)
      || runs.some(run => run.id === trial.id) || !comparison.seeds.includes(trial.seed) || ![1, 2].includes(trial.repetition as number) || pairs.has(pair)
      || typeof trial.outcome !== "string" || !Object.hasOwn(outcomes, trial.outcome)
      || (trial.score !== null && !finite(trial.score))
      || (trial.score !== null && !["success", "failed_damage"].includes(trial.outcome))
      || (trial.engine_sha256 !== comparison.engine_build.sha256 && !(trial.engine_sha256 === null
        && ["interrupted", "engine_error", "policy_error"].includes(trial.outcome) && trial.analytics === null && trial.score === null))
      || !shape(trial.measurement, MEASUREMENT_FIELDS)
      || MEASUREMENT_FIELDS.some(key => (trial.measurement as Record<string, unknown>)[key] !== null && !nonnegative((trial.measurement as Record<string, unknown>)[key]))) return null;
    const terms = validateTerms(trial.score_breakdown, trial.score as number | null);
    const analytics = validateAnalytics(trial.analytics, MAX_PUBLIC_SAMPLES);
    if ((trial.score_breakdown !== null && !terms) || (trial.analytics !== null && (!analytics || comparison.definitions === null))
      || (finite(trial.measurement.time_s) && trial.measurement.time_s > comparison.duration_limit_s && !same(trial.measurement.time_s, comparison.duration_limit_s))
      || (analytics && finite(trial.measurement.time_s) && !same(analytics.totals.observed_sim_s as number, trial.measurement.time_s))) return null;
    pairs.add(pair);
    runs.push({ id: trial.id, profileId: summary.id, label: `${summary.owner} / ${summary.label}, repetition ${trial.repetition}`, seed: trial.seed as number,
      repetition: trial.repetition as number,
      outcome: outcomes[trial.outcome], score: trial.score as number | null, terms, measurement: { ...trial.measurement } as Measurement,
      analytics, engineHash: trial.engine_sha256 as string | null, cohortKey: "publication", cohortLabel: "Published matched trial cohort" });
  }
  if (runs.length !== summaries.length * comparison.seeds.length * comparison.repetitions || summaries.some(summary => {
    const selected = runs.filter(run => run.profileId === summary.id);
    return selected.length !== summary.attempts || selected.filter(run => run.outcome === outcomes.success).length !== summary.successes
      || selected.filter(run => run.score !== null).length !== summary.scored_runs
      || (comparison.seeds as number[]).filter(seed => selected.filter(run => run.seed === seed && run.score !== null).length === 2).length !== summary.scored_pairs;
  })) return null;
  // Matching legacy formula IDs and seed coverage cannot repair incompatible
  // fuel counters. Version-1 archives are read-only legacy diagnostics too.
  return { comparison: { ...comparison, score_compatibility: { ...SCORE_COMPATIBILITY } }, profiles: summaries, runs, rankable: false };
}

async function loadPublication(): Promise<Publication> {
  // This fixed public request cannot receive a room key, URL parameters or a body.
  const response = await fetch("/analytics/team-comparison.json", {
    method: "GET", mode: "same-origin", credentials: "omit", redirect: "error", referrerPolicy: "no-referrer",
    cache: "no-cache", signal: AbortSignal.timeout(8000),
  });
  const declared = response.headers.get("content-length");
  if (!response.ok || !/^application\/json(?:;|$)/i.test(response.headers.get("content-type") ?? "")
    || (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_PUBLIC_BYTES))) throw new Error("Publication unavailable");
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Publication unavailable");
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_PUBLIC_BYTES) { await reader.cancel(); throw new Error("Publication unavailable"); }
    chunks.push(value);
  }
  const publication = validatePublication(JSON.parse(await new Blob(chunks).text()));
  if (!publication) throw new Error("Publication unavailable");
  return publication;
}

function roomRuns(results: Array<Record<string, unknown>>): PlotRun[] {
  const runs: PlotRun[] = [];
  const outcomes: Record<string, string> = { duration_limit: "Time limit", wall_budget_limit: "Wall-time limit",
    native_terminal: "Native terminal outcome", stopped: "Stopped", interrupted: "Interrupted", error: "Controller error" };
  for (const result of results.slice(-32).reverse()) {
    if (!record(result)) continue;
    const profile = profiles.find(entry => entry.id === result.profile_id);
    if (!profile || !identifier(result.id) || !integer(result.seed, 0xffffffff)) continue;
    const raw = record(result.measurement) ? result.measurement : {};
    const measurement = Object.fromEntries(MEASUREMENT_FIELDS.map(key => [key, nonnegative(raw[key]) ? raw[key] : null])) as Measurement;
    const score = finite(raw.score) && result.world_metadata_unchanged !== false ? raw.score : null;
    const hash = sha256(result.engine_sha256) ? result.engine_sha256 : null;
    const duration = finite(result.duration_s) && result.duration_s > 0 && result.duration_s <= 1800 ? result.duration_s : null;
    const comparable = hash !== null && duration !== null && result.world_metadata_unchanged === true && result.execution_kind === CONTROLLER_EXECUTION_KIND;
    const analytics = validateAnalytics(result.analytics, MAX_ROOM_SAMPLES);
    runs.push({ id: result.id, profileId: profile.id, label: `${profile.owner} / ${profile.label}`, seed: result.seed, repetition: null,
      outcome: result.native_success === true && result.world_metadata_unchanged === true ? "Station region reached"
        : raw.status === "failed_damage" ? "Collision damage failure" : typeof result.state === "string" && Object.hasOwn(outcomes, result.state) ? outcomes[result.state] : "Unknown outcome",
      score, terms: validateTerms(raw.score_breakdown, score), measurement,
      analytics: analytics && (measurement.time_s === null || same(analytics.totals.observed_sim_s as number, measurement.time_s)) ? analytics : null,
      engineHash: hash, cohortKey: comparable ? `${hash}:${result.seed}:${duration}:${CONTROLLER_EXECUTION_KIND}` : `unverified:${result.id}`,
      cohortLabel: comparable ? `Engine ${hash}; seed ${result.seed}; time limit ${duration} s; ${CONTROLLER_EXECUTION_KIND}.`
        : `Run ${result.id}. Comparison unavailable: matching build, seed, time limit and unchanged world were not verified.` });
  }
  return runs;
}

function cell(row: HTMLTableRowElement, value: string, header = false) {
  const item = document.createElement(header ? "th" : "td");
  if (header) (item as HTMLTableCellElement).scope = "row";
  item.textContent = value;
  row.append(item);
}
function bodyOf(table: HTMLTableElement) {
  for (const body of Array.from(table.tBodies)) body.remove();
  return table.createTBody();
}
function renderOutcomes(table: HTMLTableElement, runs: PlotRun[]) {
  const body = bodyOf(table);
  for (const run of runs) {
    const row = body.insertRow();
    [run.label, run.id, String(run.seed), run.outcome, exact(run.score), ...MEASUREMENT_FIELDS.map(key => exact(run.measurement[key])),
      run.analytics ? `${run.analytics.sample_count} actual samples` : "Unavailable, legacy or unsupported receipt",
      exact(run.terms?.progress_points), typeof run.terms?.progress_basis === "string" ? run.terms.progress_basis : "n/a",
      exact(run.terms?.collision_penalty_points), exact(run.terms?.fuel_penalty_points), exact(run.terms?.offset_points)]
      .forEach((value, i) => cell(row, value, i === 0));
  }
}
function renderTotals(table: HTMLTableElement, runs: PlotRun[]) {
  const head = table.tHead ?? table.createTHead();
  head.replaceChildren();
  const heading = head.insertRow();
  for (const value of ["Native field / unit", ...runs.map(run => `${run.label}, ${run.id}`)]) {
    const th = document.createElement("th"); th.scope = "col"; th.textContent = value; heading.append(th);
  }
  const body = bodyOf(table);
  for (const [key, label] of Object.entries(TOTAL_LABELS)) {
    const row = body.insertRow();
    cell(row, label, true);
    for (const run of runs) cell(row, exact(run.analytics?.totals[key]));
  }
}
const svgNode = (tag: string, attributes: Record<string, string> = {}, content?: string) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (content !== undefined) node.textContent = content;
  return node;
};
function extent(values: number[]): [number, number] {
  const low = Math.min(...values), high = Math.max(...values);
  // Axis padding is not a fabricated observation or a zero-valued baseline.
  const padding = low === high ? Math.max(Math.abs(low) * 0.05, 0.01) : (high - low) * 0.04;
  const upper = high + padding;
  return [Math.max(0, low - padding), Number.isFinite(upper) ? upper : high];
}
function tick(value: number) {
  return value !== 0 && (Math.abs(value) >= 100000 || Math.abs(value) < 0.01) ? value.toExponential(1) : String(Number(value.toPrecision(4)));
}
const customInspections = new Map<string, () => void>();
function renderGraph(prefix: string, runs: PlotRun[], metric: typeof METRICS[number]) {
  customInspections.get(prefix)?.(); customInspections.delete(prefix);
  const svg = document.getElementById(`${prefix}-plot`) as unknown as SVGSVGElement;
  const caption = svg.closest("figure")?.querySelector("figcaption");
  if (caption) caption.textContent = `${metric.guidance} Scroll horizontally on narrow screens.`;
  const legend = byId<HTMLOListElement>(`${prefix}-legend`);
  const description = `${metric.label} in ${metric.unit} against actual simulation time in seconds. Points are recorded samples; lines only connect adjacent available samples. Missing values break lines. Full numeric data follows.`;
  svg.replaceChildren(svgNode("title", { id: `${prefix}-plot-title` }, `${metric.label} by simulation time`),
    svgNode("desc", { id: `${prefix}-plot-description` }, description));
  legend.replaceChildren();
  const selected = runs.filter(run => run.analytics?.samples.some(row => row[metric.column] !== null));
  if (!selected.length) {
    svg.append(svgNode("text", { x: "24", y: "64", class: "analytics-empty" }, "No recorded samples for this selection."));
    return;
  }
  const points = selected.flatMap(run => run.analytics!.samples.filter(row => row[metric.column] !== null));
  const [xmin, xmax] = extent(points.map(row => row[0]!)), [ymin, ymax] = extent(points.map(row => row[metric.column]!));
  svg.querySelector("desc")!.textContent = `${selected.length} recorded runs at seed ${selected[0].seed}. ${description} Trace numbers identify runs in the legend. Coincident repetitions can overlap.`;
  const x = (value: number) => 88 + (value - xmin) / (xmax - xmin) * 608;
  const y = (value: number) => 260 - (value - ymin) / (ymax - ymin) * 220;
  for (let i = 0; i <= 4; i++) {
    const xv = xmin + (xmax - xmin) * (i / 4), yv = ymin + (ymax - ymin) * (i / 4);
    svg.append(svgNode("line", { x1: "88", y1: String(y(yv)), x2: "696", y2: String(y(yv)), class: "analytics-grid" }),
      svgNode("text", { x: "76", y: String(y(yv) + 5), "text-anchor": "end", class: "analytics-tick" }, tick(yv)),
      svgNode("text", { x: String(x(xv)), y: "284", "text-anchor": "middle", class: "analytics-tick" }, tick(xv)));
  }
  svg.append(svgNode("path", { d: "M88 40 V260 H696", class: "analytics-axis" }),
    svgNode("text", { x: "88", y: "23", class: "analytics-axis-label" }, `${metric.label}, ${metric.unit}`),
    svgNode("text", { x: "392", y: "314", "text-anchor": "middle", class: "analytics-axis-label" }, "Simulation time, s"));
  const inspection: ChartInspectionPoint[] = [];
  selected.forEach((run, index) => {
    const style = `analytics-series-${index % 7}`;
    const entry = document.createElement("li");
    entry.className = style;
    const swatch = document.createElement("span"); swatch.className = "analytics-swatch"; swatch.setAttribute("aria-hidden", "true");
    entry.append(swatch, document.createTextNode(`${run.label}, seed ${run.seed}, ${run.id}. ${run.outcome}.`)); legend.append(entry);
    let path = "", connected = false;
    let last: Array<number | null> | undefined;
    for (const row of run.analytics!.samples) {
      if (row[metric.column] === null) { connected = false; continue; }
      path += `${connected ? "L" : "M"}${x(row[0]!).toFixed(3)} ${y(row[metric.column]!).toFixed(3)} `;
      connected = true;
      last = row;
      const pointIndex = inspection.length;
      inspection.push({ x: x(row[0]!), y: y(row[metric.column]!), series: run.id, title: `${run.label} · seed ${run.seed}`,
        values: [{ label: "Time", value: row[0], unit: "s" }, { label: metric.label, value: row[metric.column], unit: metric.unit }, { label: "Outcome", value: run.outcome }] });
      svg.append(svgNode("circle", { cx: String(x(row[0]!)), cy: String(y(row[metric.column]!)), r: "2.5", class: `analytics-point ${style}`, "data-chart-inspection-index": String(pointIndex), "data-chart-inspection-series": run.id }));
    }
    svg.append(svgNode("path", { d: path, class: `analytics-line ${style}`, "data-chart-inspection-series": run.id }));
    if (last) svg.append(svgNode("text", { x: String(x(last[0]!) + 7), y: String(y(last[metric.column]!) + 5), class: `analytics-series-label ${style}` }, String(index + 1)));
  });
  const container = svg.closest<HTMLElement>(".analytics-chart-scroll");
  if (container && inspection.length) customInspections.set(prefix, installChartInspector(container, svg, inspection));
}

function installPlot(prefix: string, onSelection?: (runs: PlotRun[]) => void, chooseRuns?: (runs: PlotRun[]) => PlotRun[]) {
  const metric = byId<HTMLSelectElement>(`${prefix}-metric`), seed = byId<HTMLSelectElement>(`${prefix}-seed`);
  const profile = byId<HTMLSelectElement>(`${prefix}-profile`), download = byId<HTMLAnchorElement>(`${prefix}-csv`);
  let runs: PlotRun[] = [], csvUrl: string | undefined;
  metric.replaceChildren(...METRICS.map((item, i) => new Option(`${item.label}, ${item.unit}`, String(i))));
  function render() {
    const matching = runs.filter(run => (seed.value === "all" || String(run.seed) === seed.value) && (profile.value === "all" || run.profileId === profile.value));
    const selected = chooseRuns ? chooseRuns(matching) : matching;
    onSelection?.(selected);
    const column = METRICS[Number(metric.value)] ?? METRICS[0];
    renderGraph(prefix, selected, column);
    renderOutcomes(byId<HTMLTableElement>(`${prefix}-outcomes`), selected);
    renderTotals(byId<HTMLTableElement>(`${prefix}-totals`), selected);
    const body = bodyOf(byId<HTMLTableElement>(`${prefix}-samples`));
    const csv = ["run_id,profile_id,seed,repetition," + COLUMNS.join(",")];
    let sampleCount = 0;
    for (const run of selected) {
      for (const sample of run.analytics?.samples ?? []) {
        const row = body.insertRow();
        [run.label, run.id, ...sample.map(exact)].forEach((value, i) => cell(row, value, i === 0));
        csv.push([run.id, run.profileId, run.seed, run.repetition ?? "", ...sample.map(value => value === null ? "" : String(value))].join(","));
        sampleCount++;
      }
    }
    const available = selected.filter(run => run.analytics).length;
    const message = `${selected.length} selected runs; ${available} with native analytics; ${sampleCount} actual samples. ${selected.length - available} unavailable. Metric: ${column.label}.`;
    const status = byId(`${prefix}-status`);
    if (status.textContent !== message) status.textContent = message;
    if (csvUrl) URL.revokeObjectURL(csvUrl);
    csvUrl = undefined;
    download.hidden = sampleCount === 0;
    download.removeAttribute("href");
    if (sampleCount) { csvUrl = URL.createObjectURL(new Blob([csv.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" })); download.href = csvUrl; }
  }
  for (const control of [metric, seed, profile]) control.addEventListener("change", render);
  addEventListener("pagehide", () => { if (csvUrl) URL.revokeObjectURL(csvUrl); }, { once: true });
  return {
    render,
    update(next: PlotRun[]) {
      runs = next;
      const oldSeed = seed.value, oldProfile = profile.value;
      const seeds = [...new Set(runs.map(run => run.seed))].sort((a, b) => a - b);
      seed.replaceChildren(...(prefix === "published-analytics" ? [new Option("All recorded seeds", "all")] : []), ...seeds.map(value => new Option(String(value), String(value))));
      seed.value = oldSeed === "all" || (oldSeed === "" && prefix === "published-analytics") ? "all"
        : seeds.includes(Number(oldSeed)) && oldSeed !== "" ? oldSeed : seeds.includes(100) ? "100" : String(seeds[0] ?? "");
      const available = profiles.filter(entry => runs.some(run => run.profileId === entry.id));
      profile.replaceChildren(new Option("Compare all profiles", "all"), ...available.map(entry => new Option(`${entry.owner} / ${entry.label}`, entry.id)));
      profile.value = available.some(entry => entry.id === oldProfile) ? oldProfile : "all";
      seed.disabled = runs.length === 0;
      profile.disabled = runs.length === 0;
      render();
    },
  };
}

function renderPublication(publication: Publication) {
  const comparison = publication.comparison, build = comparison.engine_build as Record<string, unknown>;
  const fields: Array<[string, string]> = [["Execution mode", String(comparison.execution_kind)], ["Preset", String(comparison.preset)],
    ["Seeds", (comparison.seeds as number[]).join(", ")], ["Consecutive repetitions per seed", String(comparison.repetitions)], ["Debris bodies", String(comparison.debris_count)],
    ["Time limit, s", String(comparison.duration_limit_s)], ["Controller step, s", String(comparison.controller_step_s)],
    ["Physics step, s", String(comparison.physics_step_s)], ["Engine bytes", String(build.bytes)], ["Engine SHA-256", String(build.sha256)],
    ["STP points", "Unavailable: official fuel and damage counters not recorded"],
    ["Documented success formula", scoringReference.documented_model.success_score],
    ["Legacy diagnostic formula", comparison.score_formula_id === null ? "Unavailable" : String(comparison.score_formula_id)], ["Local summary method", String(comparison.ranking_method)]];
  const manifest = byId<HTMLDListElement>("published-analytics-manifest");
  manifest.replaceChildren();
  for (const [name, value] of fields) {
    const term = document.createElement("dt"), detail = document.createElement("dd");
    term.textContent = name; detail.textContent = value; manifest.append(term, detail);
  }
  byId("published-analytics-definitions").textContent = comparison.definitions === null ? "Native counter definitions unavailable for this archive." : JSON.stringify(comparison.definitions, null, 2);
  const hashes = bodyOf(byId<HTMLTableElement>("published-analytics-hashes"));
  for (const [path, hash] of Object.entries(comparison.source_hashes as Record<string, string>)) {
    const row = hashes.insertRow(); cell(row, path, true); cell(row, hash);
  }
  const limitations = byId<HTMLUListElement>("published-analytics-limitations");
  limitations.replaceChildren(...(comparison.limitations as string[]).map(value => { const item = document.createElement("li"); item.textContent = value; return item; }));
  const body = bodyOf(byId<HTMLTableElement>("published-analytics-scoreboard"));
  byId("published-analytics-ranking").textContent = "Unranked measured profiles. Legacy SI totals are not comparable STP points, even with matching builds and seeds.";
  for (const entry of publication.profiles) {
    const row = body.insertRow();
    [`${entry.owner} / ${entry.label}`, `${entry.successes}/${entry.attempts}`,
      rounded(entry.median_time_s, 1), rounded(entry.median_fuel_used_kg, 3), rounded(entry.median_damage_j),
      rounded(entry.median_burn_count, 1), rounded(entry.median_burn_duration_s, 1),
      rounded(entry.median_angular_burn_count, 1), rounded(entry.median_angular_burn_duration_s, 1),
      rounded(entry.median_turn_count, 1), rounded(entry.median_turn_duration_s, 1), rounded(entry.median_total_turn_angle_rad)]
      .forEach((value, i) => cell(row, value, i === 0));
  }
}

export function installFlightAnalytics() {
  // Source links must not unload the game or its private controller connection.
  for (const link of document.querySelectorAll<HTMLAnchorElement>("#controller-score-sources a")) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.append(document.createTextNode(" (opens in a new tab)"));
  }
  // These are published documentation values, never substituted for flight data.
  const example = scoringReference.documented_model.worked_examples.find(entry => entry.id === "wiki_documented_example")!;
  for (const cell of document.querySelectorAll<HTMLElement>("#controller-score-example [data-score-term]")) {
    const value = example[cell.dataset.scoreTerm as keyof typeof example];
    cell.textContent = typeof value === "number" ? String(value) : "n/a";
  }
  const details = byId<HTMLDetailsElement>("flight-analytics");
  let publishedProfiles: PublishedProfile[] = [];
  const publishedOverview = installAnalyticsOverview(byId("dashboard-overview"));
  const roomOverview = installAnalyticsOverview(byId("dashboard-room-overview"));
  const runChoice = byId<HTMLSelectElement>("overview-run");
  const seedChoice = byId<HTMLSelectElement>("published-analytics-seed");
  const measurementButtons = document.querySelectorAll<HTMLButtonElement>("[data-measurement-view]");
  let measurementView: "aggregate" | "run" = "aggregate";
  let lastSingleSeed = "100";
  const choosePublishedRuns = (matching: PlotRun[]) => {
    const previous = runChoice.value;
    runChoice.replaceChildren(...matching.map(run => new Option(`Seed ${run.seed} · rep ${run.repetition ?? "n/a"} · ${run.id} · ${run.label}`, run.id)));
    runChoice.value = matching.some(run => run.id === previous) ? previous : matching[0]?.id ?? "";
    runChoice.disabled = measurementView !== "run" || matching.length === 0;
    seedChoice.disabled = measurementView === "aggregate" || matching.length === 0;
    return measurementView === "run" ? matching.filter(run => run.id === runChoice.value) : matching;
  };
  const published = installPlot("published-analytics", selected => {
    const aggregate = measurementView === "aggregate";
    const selectedProfiles = publishedProfiles.filter(profile => selected.some(run => run.profileId === profile.id));
    publishedOverview.update(selected, aggregate ? selectedProfiles : undefined);
    byId("lab-cohort-label").textContent = `${selected.length} recorded ${selected.length === 1 ? "flight" : "flights"}`;
    const summary = byId("overview-selection-status");
    if (aggregate) {
      const seeds = new Set(selected.map(run => run.seed));
      summary.textContent = `Aggregate · ${selected.length} recorded flights across ${seeds.size} seeds. Cards and charts show cohort medians, not summed fuel. Arrival rates count every attempt. Individual flights remain available in Single run.`;
    } else {
      const run = selected[0];
      summary.textContent = run ? `One recorded flight · ${run.label} · seed ${run.seed} · ${run.id}. ${run.outcome}. ${run.analytics?.sample_count ?? 0} recorded samples. No averaging with other runs.` : "No recorded flight matches this selection.";
    }
  }, choosePublishedRuns);
  for (const button of measurementButtons) button.addEventListener("click", () => {
    const next = button.dataset.measurementView;
    if ((next !== "aggregate" && next !== "run") || next === measurementView) return;
    if (measurementView === "run") lastSingleSeed = seedChoice.value;
    measurementView = next;
    for (const item of measurementButtons) item.setAttribute("aria-pressed", String(item === button));
    byId("overview-run-field").hidden = next !== "run";
    seedChoice.value = next === "aggregate" ? "all" : lastSingleSeed;
    if (seedChoice.selectedIndex < 0) seedChoice.selectedIndex = 0;
    published.render();
  });
  runChoice.addEventListener("change", published.render);
  const room = installPlot("room-analytics", selected => roomOverview.update(selected));
  const cohort = byId<HTMLSelectElement>("room-analytics-cohort");
  const status = byId("published-analytics-load-status"), retry = byId<HTMLButtonElement>("retry-published-analytics");
  let loaded = false, loading = false, runs: PlotRun[] = [], signature = "";
  function renderRoom() {
    const selected = runs.filter(run => run.cohortKey === cohort.value);
    byId("room-analytics-cohort-detail").textContent = selected[0]?.cohortLabel ?? "No completed jobs from this connection. Published trials above are not room jobs.";
    room.update(selected);
  }
  function updateCohorts() {
    const old = cohort.value;
    const groups = [...new Map(runs.map(run => [run.cohortKey, run])).values()];
    cohort.replaceChildren(...groups.map(run => new Option(run.cohortKey.startsWith("unverified:") ? `Unverified run ${run.id}`
      : `Engine ${run.engineHash!.slice(0, 10)}, seed ${run.seed}, ${runs.filter(item => item.cohortKey === run.cohortKey).length} runs`, run.cohortKey)));
    cohort.value = groups.some(run => run.cohortKey === old) ? old : groups[0]?.cohortKey ?? "";
    cohort.disabled = groups.length === 0;
    renderRoom();
  }
  async function load() {
    if (!byId<HTMLDialogElement>("controller-panel").open || loaded || loading) return;
    loading = true; retry.hidden = true;
    status.classList.remove("screen-reader-only");
    status.textContent = "Loading the public matched-trial publication.";
    try {
      const publication = await loadPublication();
      publishedProfiles = publication.profiles;
      byId("lab-cohort-label").textContent = `${publication.runs.length} measured runs`;
      const build = publication.comparison.engine_build as { sha256: string };
      const context = document.querySelector<HTMLElement>(".lab-toolbar__note");
      if (context) context.textContent = `Recorded build ${build.sha256.slice(0, 8)} · ${publication.comparison.debris_count} bodies`;
      renderPublication(publication);
      published.update(publication.runs);
      byId("published-analytics-content").hidden = false;
      loaded = true;
      status.textContent = `${publication.runs.length} measured flights loaded.`;
      status.classList.add("screen-reader-only");
    } catch {
      // Never expose server text, URLs, exception messages or private details.
      status.textContent = "Published analytics unavailable. No valid matched-trial publication is available here. Current connection jobs remain separate below.";
      retry.hidden = false;
      const empty = byId("dashboard-overview").querySelector<HTMLElement>(".overview-empty")!;
      empty.classList.add("lab-empty"); empty.hidden = false;
      const title = document.createElement("h4"); title.textContent = "Comparison unavailable";
      const hint = document.createElement("p"); hint.textContent = "Use Retry above to reload the recorded comparison.";
      empty.replaceChildren(title, hint);
    } finally { loading = false; }
  }
  cohort.addEventListener("change", renderRoom);
  details.addEventListener("toggle", () => { if (details.open) { updateCohorts(); void load(); } });
  retry.addEventListener("click", () => { void load(); });
  return {
    openPublished() { void load(); },
    updateRoomResults(results: Array<Record<string, unknown>>) {
      const next = roomRuns(results), nextSignature = JSON.stringify(next);
      if (nextSignature === signature) return;
      runs = next; signature = nextSignature;
      byId("lab-room-empty").hidden = runs.length !== 0;
      byId("dashboard-room-overview").hidden = runs.length === 0;
      if (details.open) updateCohorts();
    },
  };
}
