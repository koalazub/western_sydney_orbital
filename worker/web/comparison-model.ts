// Pure views of one saved cohort. No engine, transport, commands or DOM.
import { finite, pairedRuns, type PathComparison, type PathRun, type PathSample } from "./path-comparison-data.ts";
import { estimateScore, type ScoreComponents, type ScoreEstimate } from "./score-estimate.ts";
export type { PathComparison, PathRun } from "./path-comparison-data.ts";

export type Alignment = "time" | "damage" | "arrival";
export type WorkspaceState = { data: PathComparison; supplement: MetricPublication | null; mode: "trials" | "cohort";
  seed: number; repetition: number; baseline: string; selectedIds: string[]; anchorId: string | null; outcome: "all" | "arrived" | "failed";
  xMetric: string; yMetric: string; visibleMetrics: string[]; cursor: number; interval: [number, number] | null;
  alignment: Alignment; difference: boolean; mapScale: "detail" | "equal" };
export type WorkspaceActions = { patch(value: Partial<WorkspaceState>): void; selectRuns(ids: string[]): void };
export type WorkspaceWidget = { update(state: WorkspaceState): void; dispose(): void };
export type Metric = { id: string; label: string; unit: string; group: string; description: string;
  source: "recorded" | "native" | "sampled" | "calculated"; direction: "min" | "max" | "context"; column?: number; nativeKey?: string };
export type MetricCoverage = { population: "arrivals" | "attempts"; eligible: number; pairedEligible: number;
  readings: Array<{ runId: string; seed: number; repetition: number; value: number }>;
  pairs: Array<{ runId: string; baselineId: string; seed: number; repetition: number; delta: number }>;
  timingCalls: number | null };
export type MetricRow = { id: string; algorithm: string; owner: string; label: string; runs: PathRun[]; outcome: string;
  attempts: number; arrivals: number; values: Record<string, number | null>; deltas: Record<string, number | null>;
  counts: Record<string, number>; spread: Record<string, number[]>; readonly coverage: Record<string, MetricCoverage>; score: ScoreSummary };
type ScoreReading = { runId: string; seed: number; repetition: number; estimate: ScoreEstimate };
type ScorePair = { runId: string; baselineId: string; seed: number; repetition: number; components: ScoreComponents };
export type ScoreSummary = {
  aggregation: "seed_pair_mean" | "terminal_run_mean" | "trial";
  components: ScoreComponents | null;
  deltas: ScoreComponents | null;
  readings: ScoreReading[];
  contributingRunIds: string[];
  eligibleRuns: number;
  eligibleSeeds: number;
  seedMeans: Array<{ seed: number; runIds: [string, string]; components: ScoreComponents }>;
  matchedSeedMeans: Array<{ seed: number; runIds: [string, string]; baselineIds: [string, string]; components: ScoreComponents }>;
  pairs: ScorePair[];
};

export const MAX_METRIC_BYTES = 2 * 1024 * 1024;
export const MAX_NATIVE_SAMPLES = 192;
export const DEFAULT_VISIBLE_METRICS = ["score_points", "time_s", "fuel_kg", "damage_j", "relative_speed_mps"];
export const SCORE_METRIC_FIELDS = {
  score_points: "total",
  score_progress_points: "progress_points",
  score_collision_penalty_points: "collision_penalty_points",
  score_fuel_penalty_points: "fuel_penalty_points",
  score_offset_points: "offset_points",
} as const satisfies Record<string, keyof ScoreComponents>;
export const COHORT_METRIC_NOTE = "Efficiency medians use arrivals only. Counts and spread are per metric. Failure readings remain in trial view.";
export const PAIRED_DELTA_NOTE = "Delta is the median of matched seed and repetition differences. Efficiency deltas require two arrivals.";
export const PARETO_LABEL = "Best observed trade-offs";
export const INTERVAL_NOTE = "Endpoints snap inward to saved readings. Coverage is their span divided by the requested window, not continuous recording. Gaps remain unknown.";
export const METRIC_LIMITATIONS = [
  "Saved local measurements, not STP results. No flights are run by this publisher.",
  "Native totals come only from validated physics-step receipts. Missing receipts stay null; commands never stand in for burns or turns.",
  "Native series contain actual retained readings, not interpolated motion. Thinning does not alter native episode totals.",
  "Policy wall times describe recorded calls on the source machine, not simulation time. Counts cover only calls with timings; missing timings are not zero.",
  "Source run and trace hashes, pairing and engine must match the path publication. Worlds and engine builds are never pooled.",
] as const;
export const NATIVE_COLUMNS = ["time_s", "fuel_used_kg", "burn_duration_s", "angular_burn_duration_s", "turn_duration_s",
  "total_turn_angle_rad", "relative_speed_mps", "range_m", "damage_j"] as const;
const EPISODES = ["burn", "angular_burn", "turn"] as const;
export const NATIVE_COUNTER_KEYS = ["burn_count", "angular_burn_count", "turn_count"] as const;
export const NATIVE_NUMBER_KEYS = ["observed_sim_s", "burn_duration_s", "longest_burn_s", "angular_burn_duration_s",
  "longest_angular_burn_s", "turn_duration_s", "longest_turn_s", "total_turn_angle_rad", "peak_angular_speed_rad_s",
  "propellant_impulse_ns", "angular_impulse_nms", "current_burn_s", "current_angular_burn_s", "current_turn_s"] as const;
export const NATIVE_FLAG_KEYS = ["burn_active", "angular_burn_active", "turn_active"] as const;
const DEFINITION_ID = "native_actuation_rotation_episodes_v1";
const SOURCE_KIND = "native_physics_step_counters_with_sampled_public_series";
type NativeKey = typeof NATIVE_COUNTER_KEYS[number] | typeof NATIVE_NUMBER_KEYS[number];
export type NativeTotals = { schema_version: 1; definition_id: typeof DEFINITION_ID }
  & Record<NativeKey, number> & Record<typeof NATIVE_FLAG_KEYS[number], boolean>;
export type NativeAnalytics = { schema_version: 1; definition_id: typeof DEFINITION_ID; source_kind: typeof SOURCE_KIND;
  totals: NativeTotals; columns: string[]; samples: Array<Array<number | null>>; sample_count: number };
export type PolicyMetrics = { sample_count: number; median_policy_wall_s: number | null; max_policy_wall_s: number | null;
  policy_calls_over_step_budget: number | null };
export type MetricRun = { id: string; algorithm: string; seed: number; repetition: number; outcome: string;
  engine_sha256: string; source_sha256: { run: string; trace: string }; analytics: NativeAnalytics | null; policy: PolicyMetrics | null };
export type MetricPublication = { schema: "wsorb.comparison_metrics"; schema_version: 1; id: PathComparison["id"];
  source: { path_sha256: string; engine_sha256: string; step_s: number }; runs: MetricRun[]; limitations: string[] };

const recorded: Metric[] = [
  { id: "time_s", label: "End time", unit: "s", group: "Efficiency", source: "recorded", direction: "min", column: 0,
    description: "Final observed time. An arrival time only for successful runs; shorter failures are not more efficient." },
  { id: "range_m", label: "End range", unit: "m", group: "Efficiency", source: "recorded", direction: "min", column: 4,
    description: "Final measured target range. Local arrival-region entry is not docking." },
  { id: "relative_speed_mps", label: "End speed", unit: "m/s", group: "Efficiency", source: "recorded", direction: "min", column: 5,
    description: "Final relative speed, not a safe rendezvous claim." },
  { id: "fuel_kg", label: "Fuel used", unit: "kg", group: "Efficiency", source: "recorded", direction: "min", column: 6,
    description: "Cumulative local physical fuel-use estimate, not remaining fuel or the STP fuel counter." },
  { id: "damage_j", label: "Damage", unit: "J", group: "Efficiency", source: "recorded", direction: "min", column: 7,
    description: "Final cumulative local damage. Failure readings remain absolute measurements, not efficiency ranks." },
  { id: "max_cross_track_m", label: "Peak cross-track", unit: "m", group: "Path", source: "sampled", direction: "context",
    description: "Largest retained-sample lateral/normal distance from the fixed route line. Unsaved extrema are unknown." },
  { id: "min_radar_m", label: "Min radar range", unit: "m", group: "Observations", source: "sampled", direction: "context", column: 10,
    description: "Smallest retained noisy radar range, including negative diagnostics. Not debris or hull clearance." },
  { id: "max_radar_age_s", label: "Max radar age", unit: "s", group: "Observations", source: "sampled", direction: "context", column: 11,
    description: "Oldest retained radar row on the simulation clock. Negative ages mean future timestamps." },
  { id: "min_terrain_m", label: "Min terrain clearance", unit: "m", group: "Observations", source: "sampled", direction: "context", column: 12,
    description: "Minimum retained shuttle-centre clearance above reported terrain, not full-hull clearance." },
  { id: "max_accel_mps2", label: "Peak thrust request", unit: "m/s²", group: "Requests", source: "sampled", direction: "context", column: 8,
    description: "Largest retained translation request. A request is not a measured acceleration or burn episode." },
  { id: "max_angular_accel_rad_s2", label: "Peak angular request", unit: "rad/s²", group: "Requests", source: "sampled", direction: "context", column: 9,
    description: "Largest retained angular request, not physical angular speed or a turn count." },
  { id: "retained_samples", label: "Retained samples", unit: "samples", group: "Coverage", source: "recorded", direction: "context",
    description: "Actual path readings retained in this publication." },
  { id: "source_samples", label: "Source samples", unit: "samples", group: "Coverage", source: "recorded", direction: "context",
    description: "Actual saved observations before publication thinning." },
  { id: "along_m", label: "Along route", unit: "m", group: "Path", source: "sampled", direction: "context", column: 1,
    description: "Final recorded progress along the fixed launch-to-initial-target line. Missing endpoint positions stay unavailable." },
  { id: "lateral_m", label: "Lateral offset", unit: "m", group: "Path", source: "sampled", direction: "context", column: 2,
    description: "Final signed lateral offset in the fixed route frame, not a geographic direction." },
  { id: "normal_m", label: "Normal offset", unit: "m", group: "Path", source: "sampled", direction: "context", column: 3,
    description: "Final signed normal offset in the fixed route frame, not terrain altitude." },
  ...(["ax_mps2", "ay_mps2", "az_mps2"] as const).map((id, index): Metric => ({ id, label: `Body ${["X", "Y", "Z"][index]} request`,
    unit: "m/s²", group: "Requests", source: "sampled", direction: "context", column: 13 + index,
    description: "Signed body-axis request with the largest retained magnitude. Series rows describe actual preceding command holds, not route-frame acceleration." })),
  { id: "accel_mps2", label: "Thrust request", unit: "m/s²", group: "Requests", source: "sampled", direction: "context", column: 8,
    description: "Largest retained translation request. Series rows describe actual preceding holds, not measured thrust or burns." },
  { id: "angular_accel_rad_s2", label: "Angular request", unit: "rad/s²", group: "Requests", source: "sampled", direction: "context", column: 9,
    description: "Largest retained angular request. Series rows describe actual preceding holds, not physical turning." },
  { id: "nearest_radar_m", label: "Radar range", unit: "m", group: "Observations", source: "sampled", direction: "context", column: 10,
    description: "Smallest retained noisy public radar range, not hull clearance. Negative diagnostics are retained." },
  { id: "terrain_clearance_m", label: "Terrain clearance", unit: "m", group: "Observations", source: "sampled", direction: "context", column: 12,
    description: "Minimum retained centre clearance above the reported terrain radius, not full-hull clearance." },
];
const scoreMetrics: Metric[] = [
  { id: "score_points", label: "Points", unit: "pts", group: "Score", source: "calculated", direction: "max",
    description: "Local estimate from the current published STP formula and saved local terminal measurements. Includes damage failures, not timeouts or software errors. Not an official STP result or a historical score receipt." },
  { id: "score_progress_points", label: "Score progress", unit: "pts", group: "Score", source: "calculated", direction: "max",
    description: "Time-ratio points for arrivals, or negative remaining-distance-ratio points for damage failures. Failure distance uses the recorded time-zero target range." },
  { id: "score_collision_penalty_points", label: "Score damage penalty", unit: "pts", group: "Score", source: "calculated", direction: "min",
    description: "Positive damage penalty subtracted from points. Uses the local final damage estimate, not an official STP damage counter." },
  { id: "score_fuel_penalty_points", label: "Score fuel penalty", unit: "pts", group: "Score", source: "calculated", direction: "min",
    description: "Positive fuel penalty subtracted from points. Uses saved local fuel consumption and the current formula's nominal fuel, not an official STP fuel counter." },
  { id: "score_offset_points", label: "Score offset", unit: "pts", group: "Score", source: "calculated", direction: "context",
    description: "Constant offset added to each valid terminal estimate. Missing or unsupported terminal estimates stay unavailable, not zero." },
];
const nativeDefinitions: Array<[string, string, string, Metric["direction"], string]> = [
  ["observed_sim_s", "Native observed time", "s", "context", "Integrated native observation horizon, not wall time."],
  ["burn_count", "Burns", "episodes", "min", "Native translation actuation episodes, independent of retained commands."],
  ["burn_duration_s", "Burn time", "s", "min", "Native integrated translation engine-on time."],
  ["longest_burn_s", "Longest burn", "s", "context", "Longest native translation episode, including an active final episode."],
  ["angular_burn_count", "Angular burns", "episodes", "min", "Native angular actuation episodes, not physical turns."],
  ["angular_burn_duration_s", "Angular burn time", "s", "min", "Native integrated angular engine-on time."],
  ["longest_angular_burn_s", "Longest angular burn", "s", "context", "Longest native angular actuation episode."],
  ["turn_count", "Turns", "episodes", "min", "Native physical rotation episodes; rotation can continue without angular thrust."],
  ["turn_duration_s", "Turn time", "s", "min", "Native integrated physical rotation time."],
  ["longest_turn_s", "Longest turn", "s", "context", "Longest native physical rotation episode."],
  ["total_turn_angle_rad", "Turn angle", "rad", "min", "Total native orientation angle travelled, not heading error."],
  ["peak_angular_speed_rad_s", "Peak angular speed", "rad/s", "context", "Peak physical angular speed recorded by native physics steps."],
  ["propellant_impulse_ns", "Propellant impulse", "N·s", "min", "Native integrated propellant force impulse."],
  ["angular_impulse_nms", "Angular impulse", "N·m·s", "min", "Native integrated actuator torque impulse."],
  ["current_burn_s", "Current burn", "s", "context", "Duration of the active final translation episode, zero when inactive."],
  ["current_angular_burn_s", "Current angular burn", "s", "context", "Duration of the active final angular actuation episode."],
  ["current_turn_s", "Current turn", "s", "context", "Duration of the active final physical rotation episode."],
  ["burn_active", "Burn active", "0/1", "context", "Native final translation activity flag, represented as zero or one."],
  ["angular_burn_active", "Angular burn active", "0/1", "context", "Native final angular actuation flag, represented as zero or one."],
  ["turn_active", "Turn active", "0/1", "context", "Native final physical rotation flag, represented as zero or one."],
];
export const metricCatalog: Metric[] = [...scoreMetrics, ...recorded, ...nativeDefinitions.map(([id, label, unit, direction, description]) =>
  ({ id, label, unit, direction, description, source: "native" as const, group: direction === "context" ? "Native context" : "Efficiency", nativeKey: id })),
  { id: "median_policy_wall_s", label: "Median policy time", unit: "s", group: "Runtime", source: "recorded", direction: "context",
    description: "Median recorded policy-call wall time on the source machine. Missing calls are excluded, not zero-filled." },
  { id: "max_policy_wall_s", label: "Max policy time", unit: "s", group: "Runtime", source: "recorded", direction: "context",
    description: "Largest recorded policy-call wall time on the source machine." },
  { id: "policy_sample_count", label: "Timed policy calls", unit: "calls", group: "Runtime", source: "recorded", direction: "context",
    description: "Number of calls with recorded policy wall times. Zero means zero timed calls in a source policy receipt; missing receipts stay unavailable." },
  { id: "policy_calls_over_step_budget", label: "Calls over step", unit: "calls", group: "Runtime", source: "recorded", direction: "context",
    description: "Recorded policy calls taking longer than the controller step. No timings means unavailable, not zero calls." },
];
export const getMetric = (id: string): Metric | undefined => metricCatalog.find(metric => metric.id === id);
export const isEfficiencyMetric = (metric: Metric | string) => (typeof metric === "string" ? getMetric(metric) : metric)?.group === "Efficiency";
export const isScoreMetric = (metric: Metric | string) => Object.hasOwn(SCORE_METRIC_FIELDS, typeof metric === "string" ? metric : metric.id);

export function scoreForRun(run: PathRun): ScoreEstimate | null {
  return estimateScore({ status: run.outcome, time_s: run.final.time_s, damage_j: run.final.damage_j,
    fuel_used_kg: run.final.fuel_kg, range_m: run.final.range_m,
    initial_range_m: run.samples.find(row => row[0] === 0)?.[4] ?? null });
}

function joinedRun(run: PathRun, supplement?: MetricPublication | null): MetricRun | undefined {
  return supplement?.runs.find(item => item.id === run.id && item.algorithm === run.algorithm && item.seed === run.seed
    && item.repetition === run.repetition && item.outcome === run.outcome && item.source_sha256.run === run.source_sha256.run
    && item.source_sha256.trace === run.source_sha256.trace && item.engine_sha256 === supplement.source.engine_sha256);
}
const median = (values: number[]) => {
  const sorted = values.filter(finite).slice().sort((a, b) => a - b), middle = Math.floor(sorted.length / 2);
  return sorted.length ? sorted.length % 2 ? sorted[middle] : sorted[middle - 1] / 2 + sorted[middle] / 2 : null;
};
const extentValue = (values: number[], minimum = false) => values.length ? values.reduce((a, b) => minimum ? Math.min(a, b) : Math.max(a, b)) : null;

export function metricValue(run: PathRun, id: string, supplement?: MetricPublication | null): number | null {
  const metric = getMetric(id);
  if (!metric) return null;
  if (isScoreMetric(id)) return scoreForRun(run)?.[SCORE_METRIC_FIELDS[id as keyof typeof SCORE_METRIC_FIELDS]] ?? null;
  if (Object.hasOwn(run.final, id)) return run.final[id as keyof PathRun["final"]];
  if (id === "retained_samples") return run.samples.length;
  if (id === "source_samples") return run.source_sample_count;
  if (id === "max_cross_track_m") return extentValue(run.samples
    .filter(row => finite(row[2]) && finite(row[3])).map(row => Math.hypot(row[2]!, row[3]!)).filter(finite));
  if (metric.nativeKey) {
    const totals = joinedRun(run, supplement)?.analytics?.totals;
    const value = totals?.[metric.nativeKey as keyof NativeTotals];
    return typeof value === "boolean" ? Number(value) : finite(value) ? value : null;
  }
  if (metric.group === "Runtime") {
    const key = id === "policy_sample_count" ? "sample_count" : id as keyof PolicyMetrics;
    const value = joinedRun(run, supplement)?.policy?.[key];
    return finite(value) ? value : null;
  }
  if (metric.column !== undefined) {
    if (["along_m", "lateral_m", "normal_m"].includes(id)) return run.samples.at(-1)?.[metric.column] ?? null;
    const values = run.samples.map(row => row[metric.column!]).filter(finite);
    if (["ax_mps2", "ay_mps2", "az_mps2"].includes(id)) return values.length
      ? values.reduce((a, b) => Math.abs(a) >= Math.abs(b) ? a : b) : null;
    return extentValue(values, id.startsWith("min_") || id === "nearest_radar_m" || id === "terrain_clearance_m");
  }
  return null;
}

export function metricSeries(run: PathRun, id: string, supplement?: MetricPublication | null): Array<[number, number | null]> {
  const metric = getMetric(id);
  if (!metric) return [];
  if (metric.column !== undefined) return run.samples.map(row => [row[0]!, row[metric.column!]]);
  const nativeColumns: Record<string, number> = { observed_sim_s: 0, burn_duration_s: 2, angular_burn_duration_s: 3,
    turn_duration_s: 4, total_turn_angle_rad: 5 };
  const column = metric.nativeKey ? nativeColumns[metric.nativeKey] : undefined;
  if (column === undefined) return [];
  return joinedRun(run, supplement)?.analytics?.samples.map(row => [row[0]!, row[column]] as [number, number | null]) ?? [];
}

const matchesOutcome = (run: PathRun, outcome: WorkspaceState["outcome"]) => outcome === "all"
  || (run.outcome === "success") === (outcome === "arrived");
export function runsForState(state: WorkspaceState): PathRun[] {
  const runs = state.mode === "trials" ? pairedRuns(state.data, state.seed, state.repetition) : state.data.runs;
  return runs.filter(run => matchesOutcome(run, state.outcome));
}
export function baselineRun(state: WorkspaceState, run: PathRun): PathRun | undefined {
  const algorithm = state.data.runs.find(item => item.id === state.baseline)?.algorithm ?? state.baseline;
  return state.data.runs.find(item => item.algorithm === algorithm && item.seed === run.seed && item.repetition === run.repetition);
}
function selectedInspectionRuns(state: WorkspaceState): PathRun[] {
  return pairedRuns(state.data, state.seed, state.repetition)
    .filter(run => state.selectedIds.includes(run.id) && matchesOutcome(run, state.outcome));
}
export function inspectionRuns(state: WorkspaceState): PathRun[] {
  const selected = selectedInspectionRuns(state);
  if (!selected.length) return [];
  const pair = pairedRuns(state.data, state.seed, state.repetition);
  const baseline = baselineRun(state, selected[0]);
  return pair.filter(run => run === baseline || selected.includes(run));
}
export function inspectionAnchor(state: WorkspaceState): PathRun | undefined {
  const selected = selectedInspectionRuns(state);
  if (!selected.length) return undefined;
  const explicit = selected.find(run => run.id === state.anchorId);
  if (explicit) return explicit;
  const aligned = selected.find(run => alignmentTime(run, state.alignment) !== null);
  if (aligned) return aligned;
  // An aligned reference is a fallback only for a genuine selected pair.
  const baseline = baselineRun(state, selected[0]);
  return baseline && alignmentTime(baseline, state.alignment) !== null ? baseline : selected[0];
}

function scoreComponents(progress: number, damage: number, fuel: number, offset: number): ScoreComponents | null {
  const total = progress - damage - fuel + offset;
  return [progress, damage, fuel, offset, total].every(finite)
    ? { total, progress_points: progress, collision_penalty_points: damage, fuel_penalty_points: fuel, offset_points: offset } : null;
}
function meanScoreComponents(values: ScoreComponents[]): ScoreComponents | null {
  if (!values.length) return null;
  const mean = (key: keyof ScoreComponents) => values.reduce((sum, value) => sum + value[key] / values.length, 0);
  return scoreComponents(mean("progress_points"), mean("collision_penalty_points"), mean("fuel_penalty_points"), mean("offset_points"));
}
function scoreDifference(candidate: ScoreComponents, baseline: ScoreComponents): ScoreComponents | null {
  return scoreComponents(candidate.progress_points - baseline.progress_points,
    candidate.collision_penalty_points - baseline.collision_penalty_points,
    candidate.fuel_penalty_points - baseline.fuel_penalty_points, candidate.offset_points - baseline.offset_points);
}
function summarizeScore(state: WorkspaceState, runs: PathRun[]): ScoreSummary {
  const readings: ScoreReading[] = runs.flatMap(run => {
    const estimate = scoreForRun(run);
    return estimate ? [{ runId: run.id, seed: run.seed, repetition: run.repetition, estimate }] : [];
  });
  const aggregation: ScoreSummary["aggregation"] = state.mode === "trials" ? "trial"
    : state.data.comparison.repetitions === 2 ? "seed_pair_mean" : "terminal_run_mean";
  const runById = new Map(runs.map(run => [run.id, run]));
  const pairs: ScorePair[] = [], seedMeans: ScoreSummary["seedMeans"] = [], matchedSeedMeans: ScoreSummary["matchedSeedMeans"] = [];
  const match = (reading: ScoreReading): ScorePair | null => {
    const run = runById.get(reading.runId), baseline = run && baselineRun(state, run);
    const reference = baseline && scoreForRun(baseline);
    const components = reference && scoreDifference(reading.estimate, reference);
    return baseline && components ? { runId: reading.runId, baselineId: baseline.id, seed: reading.seed,
      repetition: reading.repetition, components } : null;
  };
  if (aggregation === "seed_pair_mean") {
    for (const seed of state.data.comparison.seeds) {
      const first = readings.find(reading => reading.seed === seed && reading.repetition === 1);
      const second = readings.find(reading => reading.seed === seed && reading.repetition === 2);
      if (!first || !second) continue;
      const components = meanScoreComponents([first.estimate, second.estimate]);
      if (!components) continue;
      const runIds: [string, string] = [first.runId, second.runId];
      seedMeans.push({ seed, runIds, components });
      const a = match(first), b = match(second);
      if (!a || !b) continue;
      const difference = meanScoreComponents([a.components, b.components]);
      if (!difference) continue;
      pairs.push(a, b);
      matchedSeedMeans.push({ seed, runIds, baselineIds: [a.baselineId, b.baselineId], components: difference });
    }
  } else {
    for (const reading of readings) { const pair = match(reading); if (pair) pairs.push(pair); }
  }
  return {
    aggregation, readings, pairs, seedMeans, matchedSeedMeans,
    eligibleRuns: runs.length, eligibleSeeds: state.data.comparison.seeds.length,
    contributingRunIds: aggregation === "seed_pair_mean" ? seedMeans.flatMap(pair => pair.runIds) : readings.map(reading => reading.runId),
    components: meanScoreComponents(aggregation === "seed_pair_mean" ? seedMeans.map(pair => pair.components) : readings.map(reading => reading.estimate)),
    deltas: meanScoreComponents(aggregation === "seed_pair_mean" ? matchedSeedMeans.map(pair => pair.components) : pairs.map(pair => pair.components)),
  };
}

export function rowsForState(state: WorkspaceState): MetricRow[] {
  const groups = new Map<string, PathRun[]>();
  for (const run of runsForState(state)) {
    const key = state.mode === "trials" ? run.id : run.algorithm;
    groups.set(key, [...(groups.get(key) ?? []), run]);
  }
  return [...groups].map(([id, runs]) => {
    const first = runs[0], values: MetricRow["values"] = {}, deltas: MetricRow["deltas"] = {};
    const counts: MetricRow["counts"] = {}, spread: MetricRow["spread"] = {}, coverage: MetricRow["coverage"] = {};
    const score = summarizeScore(state, runs);
    for (const metric of metricCatalog) {
      if (isScoreMetric(metric)) {
        const field = SCORE_METRIC_FIELDS[metric.id as keyof typeof SCORE_METRIC_FIELDS];
        const readings = score.readings.map(reading => Object.freeze({ runId: reading.runId, seed: reading.seed,
          repetition: reading.repetition, value: reading.estimate[field] }));
        const pairs = score.pairs.map(pair => Object.freeze({ runId: pair.runId, baselineId: pair.baselineId,
          seed: pair.seed, repetition: pair.repetition, delta: pair.components[field] }));
        values[metric.id] = score.components?.[field] ?? null; deltas[metric.id] = score.deltas?.[field] ?? null;
        spread[metric.id] = readings.map(reading => reading.value); counts[metric.id] = readings.length;
        Object.freeze(readings); Object.freeze(pairs);
        coverage[metric.id] = Object.freeze({ population: "attempts", eligible: runs.length, pairedEligible: runs.length,
          readings, pairs, timingCalls: null });
        continue;
      }
      const efficiency = isEfficiencyMetric(metric), arrivals = runs.filter(run => run.outcome === "success");
      const population = state.mode === "cohort" && efficiency ? "arrivals" : "attempts";
      const eligible = population === "arrivals" ? arrivals : runs, pairedEligible = efficiency ? arrivals : runs;
      const readings: MetricCoverage["readings"] = [], pairs: MetricCoverage["pairs"] = [];
      let timingCalls: number | null = null;
      for (const run of eligible) {
        const value = metricValue(run, metric.id, state.supplement);
        if (finite(value)) readings.push(Object.freeze({ runId: run.id, seed: run.seed, repetition: run.repetition, value }));
        const policy = joinedRun(run, state.supplement)?.policy;
        if (policy) timingCalls = (timingCalls ?? 0) + policy.sample_count;
      }
      for (const run of pairedEligible) {
        const baseline = baselineRun(state, run);
        if (!baseline || (efficiency && baseline.outcome !== "success")) continue;
        const value = metricValue(run, metric.id, state.supplement), reference = metricValue(baseline, metric.id, state.supplement);
        if (finite(value) && finite(reference) && finite(value - reference)) pairs.push(Object.freeze({ runId: run.id,
          baselineId: baseline.id, seed: run.seed, repetition: run.repetition, delta: value - reference }));
      }
      spread[metric.id] = readings.map(reading => reading.value); counts[metric.id] = readings.length;
      values[metric.id] = median(spread[metric.id]); deltas[metric.id] = median(pairs.map(pair => pair.delta));
      Object.freeze(readings); Object.freeze(pairs);
      coverage[metric.id] = Object.freeze({ population, eligible: eligible.length, pairedEligible: pairedEligible.length,
        readings, pairs, timingCalls });
    }
    const outcomes = new Set(runs.map(run => run.outcome));
    return { id, algorithm: first.algorithm, owner: first.owner, label: first.label, runs,
      outcome: outcomes.size === 1 ? first.outcome : "mixed", attempts: runs.length,
      arrivals: runs.filter(run => run.outcome === "success").length, values, deltas, counts, spread, coverage: Object.freeze(coverage), score };
  });
}

export function alignmentTime(run: PathRun, alignment: Alignment): number | null {
  switch (alignment) {
    case "time": return 0;
    case "damage": return extentValue(run.events.filter(event => event.kind === "damage").map(event => event.by_s), true);
    case "arrival": return run.outcome === "success" ? run.final.time_s : null;
  }
}

/** Coverage is endpoint span / requested duration, zero with fewer than two
 * readings. It says nothing about retained interior observations or commands.
 * Each cumulative difference needs two actual endpoint values; no filling.
 */
export function intervalStats(run: PathRun, interval: [number, number] | null, supplement?: MetricPublication | null): {
  start_s: number | null; end_s: number | null; fuel_kg: number | null; damage_j: number | null;
  progress_m: number | null; duration_s: number | null; coverage: number;
} {
  const empty = { start_s: null, end_s: null, fuel_kg: null, damage_j: null, progress_m: null, duration_s: null, coverage: 0 };
  const window = interval ?? [0, run.final.time_s ?? run.samples.at(-1)?.[0] ?? 0];
  if (!window.every(finite)) return empty;
  const low = Math.min(...window), high = Math.max(...window);
  // Supplementary readings are real observations too. A position is unavailable
  // at a supplementary-only endpoint; do not borrow a nearby path position.
  const rows: PathSample[] = run.samples.map(row => row.slice());
  for (const native of joinedRun(run, supplement)?.analytics?.samples ?? []) {
    const existing = rows.find(row => row[0] === native[0]);
    if (existing) {
      if (existing[6] === null) existing[6] = native[1];
      if (existing[7] === null) existing[7] = native[8];
    } else {
      const row: PathSample = Array(17).fill(null); row[0] = native[0]; row[6] = native[1]; row[7] = native[8]; rows.push(row);
    }
  }
  const selected = rows.filter(row => row[0]! >= low && row[0]! <= high).sort((a, b) => a[0]! - b[0]!);
  if (!selected.length) return empty;
  const first = selected[0], last = selected[selected.length - 1], duration = last[0]! - first[0]!;
  const difference = (column: number) => duration > 0 && finite(first[column]) && finite(last[column]) && finite(last[column]! - first[column]!)
    ? last[column]! - first[column]! : null;
  const fuel = difference(6), damage = difference(7);
  const positions = [first, last].every(row => [1, 2, 3].every(column => finite(row[column])));
  return { start_s: first[0], end_s: last[0], fuel_kg: fuel !== null && fuel >= 0 ? fuel : null,
    damage_j: damage !== null && damage >= 0 ? damage : null, progress_m: positions ? difference(1) : null,
    duration_s: duration, coverage: high > low ? Math.min(1, duration / (high - low)) : 0 };
}

// Supply actual runs from one PathComparison, normally runsForState(state).
// A supplement additionally rejects accidental runs from another source cohort.
export function paretoRunIds(runs: PathRun[], xMetric: string, yMetric: string, supplement?: MetricPublication | null): string[] {
  const x = getMetric(xMetric), y = getMetric(yMetric);
  if (!x || !y || x.direction === "context" || y.direction === "context"
    || (supplement && runs.some(run => !joinedRun(run, supplement)))) return [];
  const points = runs.filter(run => run.outcome === "success").map(run =>
    ({ id: run.id, x: metricValue(run, xMetric, supplement), y: metricValue(run, yMetric, supplement) }))
    .filter((point): point is { id: string; x: number; y: number } => finite(point.x) && finite(point.y));
  const better = (a: number, b: number, direction: Metric["direction"]) => direction === "min" ? a <= b : a >= b;
  return points.filter(point => !points.some(other => better(other.x, point.x, x.direction) && better(other.y, point.y, y.direction)
    && (other.x !== point.x || other.y !== point.y))).map(point => point.id);
}
export function formatMetric(value: number | null, metricOrId: Metric | string, delta = false): string {
  if (!finite(value)) return "n/a";
  const metric = typeof metricOrId === "string" ? getMetric(metricOrId) : metricOrId;
  const number = Object.is(value, -0) ? 0 : value;
  const small = number !== 0 && Math.abs(number) < 0.001, large = Math.abs(number) >= 1_000_000;
  const exactInteger = metric?.unit === "samples" || metric?.unit === "episodes" || metric?.unit === "calls" || metric?.unit === "0/1";
  const text = (small || large) && !exactInteger ? number.toExponential(2).replace(/\.?(0+)e/, "e")
    : new Intl.NumberFormat(undefined, exactInteger ? { maximumFractionDigits: 2 } : { maximumSignificantDigits: 4 }).format(number);
  return delta && number > 0 ? `+${text}` : text;
}

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const shape = (value: unknown, keys: readonly string[]): value is Record<string, unknown> => record(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const nonnegative = (value: unknown): value is number => finite(value) && value >= 0;
const integer = (value: unknown, max = Number.MAX_SAFE_INTEGER): value is number => nonnegative(value) && Number.isSafeInteger(value) && value <= max;
const hash = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const same = (a: number, b: number) => Math.abs(a - b) <= 1e-7 * Math.max(1, Math.abs(a), Math.abs(b));

function validateAnalytics(value: unknown): value is NativeAnalytics {
  if (!shape(value, ["schema_version", "definition_id", "source_kind", "totals", "columns", "samples", "sample_count"])
    || value.schema_version !== 1 || value.definition_id !== DEFINITION_ID || value.source_kind !== SOURCE_KIND
    || !Array.isArray(value.columns) || value.columns.length !== NATIVE_COLUMNS.length || value.columns.some((key, i) => key !== NATIVE_COLUMNS[i])
    || !Array.isArray(value.samples) || !value.samples.length || value.samples.length > MAX_NATIVE_SAMPLES || value.sample_count !== value.samples.length) return false;
  const totals = value.totals;
  if (!shape(totals, ["schema_version", "definition_id", ...NATIVE_COUNTER_KEYS, ...NATIVE_NUMBER_KEYS, ...NATIVE_FLAG_KEYS])
    || totals.schema_version !== 1 || totals.definition_id !== DEFINITION_ID || NATIVE_COUNTER_KEYS.some(key => !integer(totals[key]))
    || NATIVE_NUMBER_KEYS.some(key => !nonnegative(totals[key])) || NATIVE_FLAG_KEYS.some(key => typeof totals[key] !== "boolean")) return false;
  const native = totals as NativeTotals, observed = native.observed_sim_s;
  for (const name of EPISODES) {
    const count = native[`${name}_count`], duration = native[`${name}_duration_s`], longest = native[`longest_${name}_s`], current = native[`current_${name}_s`];
    const tolerance = 1e-7 * Math.max(1, observed, duration);
    if (duration > observed + tolerance || longest > duration + tolerance || current > longest + tolerance
      || (!native[`${name}_active`] && current !== 0) || (native[`${name}_active`] && (current <= 0 || count === 0))
      || (count === 0 && (duration !== 0 || longest !== 0)) || (count > 0 && (duration <= 0 || longest <= 0 || longest + tolerance < duration / count))) return false;
  }
  if (observed === 0 && [...NATIVE_COUNTER_KEYS, ...NATIVE_NUMBER_KEYS].some(key => native[key] !== 0)) return false;
  const cumulative: Record<number, NativeKey> = { 2: "burn_duration_s", 3: "angular_burn_duration_s", 4: "turn_duration_s", 5: "total_turn_angle_rad" };
  const previous = new Map<number, { time: number; value: number }>(); let lastTime: number | null = null;
  for (const row of value.samples) {
    if (!Array.isArray(row) || row.length !== NATIVE_COLUMNS.length || !nonnegative(row[0])
      || row.some(cell => cell !== null && !nonnegative(cell))) return false;
    const now = row[0] as number;
    if ((lastTime === null ? now !== 0 : now <= lastTime) || (now > observed && !same(now, observed))) return false;
    for (const column of [1, 2, 3, 4, 5, 8]) {
      const cell = row[column]; if (cell === null) continue;
      const old = previous.get(column);
      if (old && cell < old.value) return false;
      if (Object.hasOwn(cumulative, column) && (cell > native[cumulative[column]] + 1e-7 * Math.max(1, cell, native[cumulative[column]])
        || (lastTime === null && cell !== 0) || (column < 5 && cell > now + 1e-7 * Math.max(1, cell, now))
        || (column < 5 && old && cell - old.value > now - old.time + 1e-7 * Math.max(1, cell, now)))) return false;
      previous.set(column, { time: now, value: cell });
    }
    lastTime = now;
  }
  if (lastTime === null || !same(lastTime, observed)) return false;
  const last = value.samples.at(-1)!;
  return Object.entries(cumulative).every(([column, key]) => last[Number(column)] === null || same(last[Number(column)], native[key]));
}

export function validateMetricPublication(value: unknown, data: PathComparison, pathSha256: string): MetricPublication | null {
  if (!hash(pathSha256) || !shape(value, ["schema", "schema_version", "id", "source", "runs", "limitations"])
    || value.schema !== "wsorb.comparison_metrics" || value.schema_version !== 1 || value.id !== data.id
    || !shape(value.source, ["path_sha256", "engine_sha256", "step_s"]) || value.source.path_sha256 !== pathSha256
    || value.source.engine_sha256 !== data.comparison.engine_sha256 || value.source.step_s !== data.comparison.step_s
    || !Array.isArray(value.runs) || value.runs.length !== data.runs.length || !value.runs.length || value.runs.length > 128
    || !Array.isArray(value.limitations) || value.limitations.length !== METRIC_LIMITATIONS.length
    || value.limitations.some((note, i) => note !== METRIC_LIMITATIONS[i])) return null;
  const seen = new Set<string>();
  for (const item of value.runs) {
    if (!shape(item, ["id", "algorithm", "seed", "repetition", "outcome", "engine_sha256", "source_sha256", "analytics", "policy"])) return null;
    const run = data.runs.find(run => run.id === item.id);
    if (!run || seen.has(run.id) || item.algorithm !== run.algorithm || item.seed !== run.seed || item.repetition !== run.repetition
      || item.outcome !== run.outcome || item.engine_sha256 !== data.comparison.engine_sha256
      || !shape(item.source_sha256, ["run", "trace"]) || item.source_sha256.run !== run.source_sha256.run || item.source_sha256.trace !== run.source_sha256.trace) return null;
    seen.add(run.id);
    if (item.analytics !== null) {
      if (!validateAnalytics(item.analytics) || !finite(run.final.time_s) || !same(item.analytics.totals.observed_sim_s, run.final.time_s)
        || item.analytics.sample_count > run.source_sample_count) return null;
      // Public readings at shared timestamps must agree with the path source.
      for (const row of item.analytics.samples) {
        const path = run.samples.find(sample => same(sample[0]!, row[0]!));
        if (path && [[1, 6], [6, 5], [7, 4], [8, 7]].some(([native, column]) => row[native] !== null && path[column] !== null
          && !same(row[native]!, path[column]!))) return null;
      }
    }
    const policy = item.policy;
    if (policy === null) continue;
    if (!shape(policy, ["sample_count", "median_policy_wall_s", "max_policy_wall_s", "policy_calls_over_step_budget"])
      || !integer(policy.sample_count, Math.max(0, run.source_sample_count - 1))) return null;
    if (policy.sample_count === 0) {
      if ([policy.median_policy_wall_s, policy.max_policy_wall_s, policy.policy_calls_over_step_budget].some(value => value !== null)) return null;
    } else if (!nonnegative(policy.median_policy_wall_s) || !nonnegative(policy.max_policy_wall_s)
      || policy.median_policy_wall_s > policy.max_policy_wall_s || !integer(policy.policy_calls_over_step_budget, policy.sample_count)
      || (policy.max_policy_wall_s > data.comparison.step_s) !== (policy.policy_calls_over_step_budget > 0)) return null;
  }
  try {
    if (new TextEncoder().encode(JSON.stringify(value)).length > MAX_METRIC_BYTES) return null;
  } catch { return null; }
  return value as unknown as MetricPublication;
}
