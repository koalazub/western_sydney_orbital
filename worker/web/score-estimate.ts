// Current published algebra applied to local counters, not an official score receipt.
import scoringReference from "../../src/wsorb/scoring_reference.json" with { type: "json" };

export const SCORE_REFERENCE = scoringReference.current_main;
export const SCORE_CONSTANTS = SCORE_REFERENCE.constants;

export type ScoreInputs = {
  status: string;
  time_s: number | null;
  damage_j: number | null;
  fuel_used_kg: number | null;
  range_m?: number | null;
  initial_range_m?: number | null;
};
export type ScoreComponents = {
  total: number;
  progress_points: number;
  collision_penalty_points: number;
  fuel_penalty_points: number;
  offset_points: number;
};
export type ScoreEstimate = ScoreComponents & {
  progress_basis: "time_ratio" | "remaining_distance_ratio";
};

const nonnegative = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;

export function estimateScore(input: ScoreInputs): ScoreEstimate | null {
  if (!input || (input.status !== "success" && input.status !== "failed_damage")) return null;
  const { time_s: time, damage_j: damage, fuel_used_kg: fuel, range_m: range, initial_range_m: initialRange } = input;
  if (!nonnegative(damage) || !nonnegative(fuel)) return null;
  for (const optional of [time, range, initialRange]) {
    if (optional !== null && optional !== undefined && !nonnegative(optional)) return null;
  }
  const constants = SCORE_CONSTANTS;
  let progress: number, progress_basis: ScoreEstimate["progress_basis"];
  switch (input.status) {
    case "success":
      if (!nonnegative(time) || damage > constants.impact_threshold_j
        || (range !== null && range !== undefined && range > constants.arrival_radius_m)) return null;
      progress = time === 0 ? constants.zero_time_bonus : constants.nominal_time_s / time;
      progress_basis = "time_ratio";
      break;
    case "failed_damage":
      if (damage <= constants.impact_threshold_j || !nonnegative(range) || !nonnegative(initialRange) || initialRange === 0) return null;
      progress = -range / initialRange;
      progress_basis = "remaining_distance_ratio";
      break;
    default: return null;
  }
  const progress_points = constants.points_scale * progress;
  const collision_penalty_points = constants.points_scale * (damage / constants.impact_threshold_j) * constants.damage_multiplier;
  const fuel_penalty_points = constants.points_scale * Math.max(fuel / constants.nominal_fuel_kg, 0);
  const offset_points = constants.points_scale * constants.score_offset;
  const total = progress_points - collision_penalty_points - fuel_penalty_points + offset_points;
  if (![progress_points, collision_penalty_points, fuel_penalty_points, offset_points, total].every(Number.isFinite)) return null;
  return { total, progress_points, collision_penalty_points, fuel_penalty_points, offset_points, progress_basis };
}
