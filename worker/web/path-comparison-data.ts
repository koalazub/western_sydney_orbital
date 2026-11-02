// Public recorded measurements only. No flight, sensor or command capability.
export const PATH_COLUMNS = ["time_s", "along_m", "lateral_m", "normal_m", "range_m", "relative_speed_mps",
  "fuel_kg", "damage_j", "accel_mps2", "angular_accel_rad_s2", "nearest_radar_m", "max_radar_age_s",
  "terrain_clearance_m", "ax_mps2", "ay_mps2", "az_mps2", "command_from_s"] as const;
export const MAX_PATH_BYTES = 4 * 1024 * 1024;
export type PathSample = Array<number | null>;
export type PathEvent = { kind: "damage" | "terminal"; after_s: number | null; by_s: number; value: number | null };
export type PathRun = { id: string; algorithm: string; owner: string; label: string; seed: number; repetition: number;
  outcome: string; source_sample_count: number; samples: PathSample[]; events: PathEvent[];
  final: { time_s: number | null; range_m: number | null; relative_speed_mps: number | null; fuel_kg: number | null; damage_j: number | null };
  source_sha256: { run: string; trace: string } };
export type PathComparison = { schema: "wsorb.path_comparison"; schema_version: 1; id: "official" | "team"; label: string;
  comparison: { preset: string; distribution: string; debris_count: number; seeds: number[]; repetitions: number;
    duration_s: number; step_s: number; engine_sha256: string; arrival_radius_m: number; frame: "launch_route_reference";
    route_length_m: number; basis: Record<"origin_reference_m" | "target_initial_reference_m" | "along_axis" | "lateral_axis" | "normal_axis", number[]>;
    limitations: string[] }; columns: string[]; runs: PathRun[] };
export type TraceStyle = { color: string; dash: string; number: number };

const styles: Record<string, TraceStyle> = {
  official_basic_beeline: { color: "var(--team-reference)", dash: "3 4", number: 1 },
  wso_official_profile: { color: "var(--team-ali)", dash: "none", number: 2 },
  ali_official_profile: { color: "var(--team-ali)", dash: "none", number: 2 },
  baseline: { color: "var(--team-wso)", dash: "12 4", number: 3 },
  "body-axis": { color: "var(--team-wso)", dash: "12 3 2 3", number: 4 },
  basic_pd_radar: { color: "var(--team-harry)", dash: "7 3", number: 5 },
  bui_alpha_beta_mvp: { color: "var(--team-bui)", dash: "2 3", number: 6 },
  tom_kalman_velocity_mvp: { color: "var(--team-tom)", dash: "9 3 2 3", number: 7 },
};
export const traceStyle = (algorithm: string) => styles[algorithm];
export const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max = 300): value is string => typeof value === "string" && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value);
const integer = (value: unknown, max: number) => finite(value) && Number.isSafeInteger(value) && value >= 0 && value <= max;
const hash = (value: unknown) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const shape = (value: unknown, keys: string[]): value is Record<string, unknown> => record(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const close = (a: number, b: number) => Math.abs(a - b) <= 1e-7 * Math.max(1, Math.abs(a), Math.abs(b));
// Noisy radar range and row age may be negative; retain those diagnostics.
const nonnegativeColumns = [0, 4, 5, 6, 7, 8, 9, 16];
const outcomes = ["success", "failed_damage", "timeout", "policy_error", "engine_error", "interrupted"];
const finalColumns: Record<string, number> = { time_s: 0, range_m: 4, relative_speed_mps: 5, fuel_kg: 6, damage_j: 7 };

export function validatePathComparison(value: unknown): PathComparison | null {
  if (!shape(value, ["schema", "schema_version", "id", "label", "comparison", "columns", "runs"])
    || value.schema !== "wsorb.path_comparison" || value.schema_version !== 1 || !["official", "team"].includes(value.id as string)
    || !text(value.label) || !Array.isArray(value.columns) || value.columns.length !== PATH_COLUMNS.length
    || value.columns.some((column, i) => column !== PATH_COLUMNS[i])
    || !Array.isArray(value.runs) || !value.runs.length || value.runs.length > 128) return null;
  const c = value.comparison;
  if (!shape(c, ["preset", "distribution", "debris_count", "seeds", "repetitions", "duration_s", "step_s", "engine_sha256",
    "arrival_radius_m", "frame", "route_length_m", "basis", "limitations"])
    || !text(c.preset) || !text(c.distribution) || c.debris_count !== 500 || !Array.isArray(c.seeds) || !c.seeds.length || c.seeds.length > 64
    || c.seeds.some(seed => !integer(seed, 0xffffffff)) || new Set(c.seeds).size !== c.seeds.length
    || !integer(c.repetitions, 10) || c.repetitions === 0 || !finite(c.duration_s) || c.duration_s <= 0 || c.duration_s > 86400
    || !finite(c.step_s) || c.step_s <= 0 || c.step_s > 30 || !hash(c.engine_sha256)
    || !finite(c.arrival_radius_m) || c.arrival_radius_m <= 0 || !finite(c.route_length_m) || c.route_length_m <= 0
    || c.frame !== "launch_route_reference" || !Array.isArray(c.limitations) || c.limitations.length > 32 || c.limitations.some(item => !text(item, 2000))
    || !shape(c.basis, ["origin_reference_m", "target_initial_reference_m", "along_axis", "lateral_axis", "normal_axis"])
    || Object.values(c.basis).some(vector => !Array.isArray(vector) || vector.length !== 3 || !vector.every(finite))) return null;
  if (value.id === "official" && (c.preset !== "competition_reference" || c.distribution !== "official_cloud")) return null;
  if (value.id === "team" && (c.preset !== "far_side_l2" || c.distribution !== "directed_stream")) return null;
  const basis = c.basis as PathComparison["comparison"]["basis"];
  const axes = [basis.along_axis, basis.lateral_axis, basis.normal_axis];
  if (axes.some(axis => !close(Math.hypot(...axis), 1)) || !close(axes[0].reduce((sum, v, i) => sum + v * axes[1][i], 0), 0)
    || !close(axes[0].reduce((sum, v, i) => sum + v * axes[2][i], 0), 0)
    || !close(axes[1].reduce((sum, v, i) => sum + v * axes[2][i], 0), 0)) return null;
  const delta = basis.target_initial_reference_m.map((n, i) => n - basis.origin_reference_m[i]);
  if (!close(Math.hypot(...delta), c.route_length_m)
    || delta.some((n, i) => !close(n / c.route_length_m, axes[0][i]))) return null;
  const cross = [axes[0][1] * axes[1][2] - axes[0][2] * axes[1][1],
    axes[0][2] * axes[1][0] - axes[0][0] * axes[1][2], axes[0][0] * axes[1][1] - axes[0][1] * axes[1][0]];
  if (cross.some((n, i) => !close(n, axes[2][i]))) return null;
  const ids = new Set(), pairs = new Set(), algorithms = new Set<string>();
  for (const raw of value.runs) {
    if (!shape(raw, ["id", "algorithm", "owner", "label", "seed", "repetition", "outcome", "source_sample_count", "samples", "events", "final", "source_sha256"])
      || !text(raw.id, 96) || !/^[a-zA-Z0-9_.-]+$/.test(raw.id) || ids.has(raw.id) || !Object.hasOwn(styles, raw.algorithm as string)
      || !text(raw.owner, 80) || !text(raw.label) || !c.seeds.includes(raw.seed) || !integer(raw.repetition, c.repetitions) || raw.repetition === 0
      || !outcomes.includes(raw.outcome as string) || !integer(raw.source_sample_count, 1_000_000)
      || !Array.isArray(raw.samples) || raw.samples.length > 512 || raw.samples.length > raw.source_sample_count
      || !Array.isArray(raw.events) || raw.events.length > 1024
      || !shape(raw.final, Object.keys(finalColumns)) || !shape(raw.source_sha256, ["run", "trace"])
      || !hash(raw.source_sha256.run) || !hash(raw.source_sha256.trace)) return null;
    const pair = `${raw.algorithm}:${raw.seed}:${raw.repetition}`;
    if (pairs.has(pair)) return null;
    ids.add(raw.id); pairs.add(pair); algorithms.add(raw.algorithm as string);
    let previous: PathSample | undefined;
    for (const row of raw.samples) {
      if (!Array.isArray(row) || row.length !== PATH_COLUMNS.length || !finite(row[0]) || row[0] < 0 || row[0] > c.duration_s + 1e-7
        || row.some(cell => cell !== null && !finite(cell)) || nonnegativeColumns.some(column => row[column] !== null && row[column] < 0)
        || (previous && row[0] <= previous[0]!)) return null;
      if (previous && [6, 7].some(column => row[column] !== null && previous![column] !== null && row[column] < previous![column]! - 1e-7)) return null;
      const command = row[8] !== null || row[9] !== null;
      if (command ? !finite(row[16]) || row[16] >= row[0] || row[0] - row[16] > c.step_s + 1e-6 : row[16] !== null) return null;
      previous = row;
    }
    for (const [key, column] of Object.entries(finalColumns)) {
      const number = raw.final[key];
      if (number !== null && (!finite(number) || number < 0)) return null;
      const last = raw.samples.at(-1)?.[column] ?? null;
      if ((last === null) !== (number === null) || (finite(last) && finite(number) && !close(last, number))) return null;
    }
    for (const event of raw.events) {
      if (!shape(event, ["kind", "after_s", "by_s", "value"]) || !["damage", "terminal"].includes(event.kind as string)
        || !finite(event.by_s) || event.by_s < 0 || event.by_s > c.duration_s + 1e-7
        || (event.after_s !== null && (!finite(event.after_s) || event.after_s < 0 || event.after_s > event.by_s))
        || (event.value !== null && (!finite(event.value) || event.value < 0))
        || !raw.samples.some(row => close(row[0]!, event.by_s))) return null;
    }
  }
  const allowed = value.id === "official" ? ["official_basic_beeline", "wso_official_profile", "ali_official_profile", "baseline", "body-axis", "basic_pd_radar"]
    : ["basic_pd_radar", "bui_alpha_beta_mvp", "tom_kalman_velocity_mvp"];
  if ([...algorithms].some(algorithm => !allowed.includes(algorithm))
    || algorithms.size !== (value.id === "official" ? 5 : 3)
    || (algorithms.has("wso_official_profile") && algorithms.has("ali_official_profile"))
    || value.runs.length !== algorithms.size * c.seeds.length * c.repetitions) return null;
  return value as unknown as PathComparison;
}

export function pairedRuns(data: PathComparison, seed: number, repetition: number): PathRun[] {
  return data.runs.filter(run => run.seed === seed && run.repetition === repetition)
    .sort((a, b) => traceStyle(a.algorithm).number - traceStyle(b.algorithm).number);
}
export function timeline(runs: PathRun[]): number[] {
  const times = runs.flatMap(run => run.samples.map(row => row[0]!)).sort((a, b) => a - b);
  return times.filter((time, i) => i === 0 || Math.abs(time - times[i - 1]) > 1e-7);
}
export function sampleAtTime(run: PathRun, time: number): PathSample | null {
  // No interpolation, extrapolation, last-value fill or invented common sample.
  return run.samples.find(row => Math.abs(row[0]! - time) <= 1e-7) ?? null;
}
export function segments(samples: PathSample[], xColumn: number, yColumn: number): Array<Array<[number, number]>> {
  const result: Array<Array<[number, number]>> = [];
  let current: Array<[number, number]> = [];
  for (const row of samples) {
    if (row[xColumn] === null || row[yColumn] === null) { if (current.length) result.push(current); current = []; }
    else current.push([row[xColumn]!, row[yColumn]!]);
  }
  if (current.length) result.push(current);
  return result;
}
export function outcomeLabel(outcome: string) {
  return ({ success: "Arrived", failed_damage: "Damage failure", timeout: "Time limit", policy_error: "Policy error",
    engine_error: "Engine error", interrupted: "Interrupted" } as Record<string, string>)[outcome] ?? "Unavailable";
}
export function selectedCSV(runs: PathRun[]): string {
  const quote = (value: unknown) => `"${String(value ?? "").replaceAll('"', '""')}"`;
  return [["trial", "algorithm", "seed", "repetition", "outcome", ...PATH_COLUMNS].map(quote).join(","),
    ...runs.flatMap(run => run.samples.map(row => [run.id, run.algorithm, run.seed, run.repetition, run.outcome, ...row].map(quote).join(",")))].join("\r\n") + "\r\n";
}
