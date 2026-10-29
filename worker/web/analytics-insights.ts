// Supplied measurements only. This module does not load data or control flights.
export type InsightRun = {
  label: string;
  outcome?: string;
  seed: number;
  repetition: number | null;
  measurement: {
    time_s: number | null;
    fuel_used_kg: number | null;
    damage_j: number | null;
  };
  analytics: { totals: Record<string, number | string | boolean> } | null;
};

export type InsightProfile = {
  attempts: number;
  successes: number;
  median_time_s?: unknown;
  median_fuel_used_kg?: unknown;
  median_damage_j?: unknown;
};

export type InsightGroup = {
  owner: string;
  label?: string;
  first: InsightRun;
  profile?: InsightProfile;
};

export type MetricInsight = { guidance: string; observation: string; scope: string };

type Metric = "fuel" | "time" | "thrust" | "turn";
type Reading = { group: InsightGroup; value: number };
type Counts = { attempts: number; successes: number };
type Selection = { completed: InsightGroup[]; comparable: boolean; scope: string };

const GUIDANCE: Record<Metric, string> = {
  fuel: "Use less fuel for a completed flight.",
  time: "Reach the station sooner, without adding damage.",
  thrust: "Less engine-on time to reach the station is better. Check fuel and damage too.",
  turn: "Avoid needless spin; turns can still be needed for avoidance.",
};

function nonnegative(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}
function integerCount(value: unknown): number | null {
  const number = nonnegative(value);
  return number !== null && Number.isInteger(number) ? number : null;
}
function decimal(value: number, places = 1): string {
  return new Intl.NumberFormat("en", {
    minimumFractionDigits: places, maximumFractionDigits: places,
  }).format(value);
}
function name(group: InsightGroup): string {
  return group.owner.trim() || group.label?.trim() || group.first.label.trim() || "Unnamed run";
}
function names(rows: Reading[]): string {
  return new Intl.ListFormat("en", { type: "conjunction" }).format(rows.map(row => name(row.group)));
}
function total(run: InsightRun, key: string): number | null {
  return nonnegative(run.analytics?.totals[key]);
}
function arrived(run: InsightRun): boolean {
  return run.outcome === "Station region reached" || run.outcome === "success";
}
function profileCounts(profile: InsightProfile): Counts | null {
  const attempts = integerCount(profile.attempts), successes = integerCount(profile.successes);
  return attempts !== null && successes !== null && successes <= attempts ? { attempts, successes } : null;
}
function completedCohort(profile: InsightProfile): boolean {
  const counts = profileCounts(profile);
  return counts !== null && counts.attempts > 0 && counts.successes === counts.attempts;
}
function countText(counts: Counts | null): string {
  return counts === null ? "arrival counts unavailable" : `${decimal(counts.successes, 0)}/${decimal(counts.attempts, 0)} arrivals`;
}
function readings(groups: InsightGroup[], measure: (group: InsightGroup) => unknown): Reading[] {
  return groups.flatMap(group => {
    const value = nonnegative(measure(group));
    return value === null ? [] : [{ group, value }];
  });
}
function minimum(rows: Reading[]): Reading[] {
  if (!rows.length) return [];
  const value = Math.min(...rows.map(row => row.value));
  return rows.filter(row => row.value === value);
}
function damageWarning(groups: InsightGroup[], cohort = false): string | null {
  const rows = readings(groups, group => cohort ? group.profile?.median_damage_j : group.first.measurement.damage_j);
  if (new Set(rows.map(row => row.value)).size > 1) return cohort ? "median damage differs" : "arrival damage differs";
  if (rows.length < groups.length) return cohort ? "some median damage is unavailable" : "some arrival damage is unavailable";
  return null;
}
function selectedScope(groups: InsightGroup[]): Selection {
  const completed = groups.filter(group => arrived(group.first));
  const seeds = new Set(groups.map(group => group.first.seed));
  const repetitions = new Set(groups.map(group => group.first.repetition));
  const valid = groups.every(({ first }) => integerCount(first.seed) !== null
    && (first.repetition === null || integerCount(first.repetition) !== null));
  const comparable = valid && seeds.size === 1 && repetitions.size === 1;
  const first = groups[0].first;
  const source = seeds.size > 1 ? "Mixed seeds · first selected runs are unranked"
    : repetitions.size > 1 ? "Mixed repetitions · first selected runs are unranked"
    : !valid ? "Selected-run scope unavailable · unranked"
    : `First selected runs · seed ${decimal(first.seed, 0)}${first.repetition === null ? "" : `, rep ${decimal(first.repetition, 0)}`}`;
  const excluded = completed.length > 0 && completed.length < groups.length ? " · non-arrivals excluded" : "";
  return { completed, comparable, scope: `${source} · ${completed.length}/${groups.length} recorded arrivals${excluded}` };
}
function noArrivals(): string {
  return "No recorded arrivals in these selected runs. Reach the station before comparing efficiency; stopped, failed, and unknown-outcome runs stay unranked.";
}
function quantity(metric: Exclude<Metric, "turn">, value: number): string {
  if (metric === "fuel") return `${decimal(value, 2)} kg of estimated fuel`;
  if (metric === "thrust") return `${decimal(value)} s of translation thrust`;
  return `${decimal(value)} s to reach the station`;
}
function selectedValue(metric: Exclude<Metric, "turn">, group: InsightGroup): number | null {
  if (metric === "fuel") return nonnegative(group.first.measurement.fuel_used_kg);
  if (metric === "time") return nonnegative(group.first.measurement.time_s);
  return total(group.first, "burn_duration_s");
}
function fuelContext(winner: Reading, next: Reading | undefined, completed: InsightGroup[]): string {
  const damage = damageWarning(completed) ?? "compare damage too";
  if (next) {
    const winnerTime = nonnegative(winner.group.first.measurement.time_s);
    const nextTime = nonnegative(next.group.first.measurement.time_s);
    if (winnerTime !== null && nextTime !== null) {
      const difference = winnerTime - nextTime;
      if (difference > 0) return `${name(winner.group)} took ${decimal(difference)} s longer than ${name(next.group)}; ${damage}.`;
      if (difference < 0) return `${name(winner.group)} also arrived ${decimal(-difference)} s sooner than ${name(next.group)}; ${damage}.`;
      return `Their recorded arrival times are equal; ${damage}.`;
    }
  }
  return `Compare arrival time and damage too${damageWarning(completed) ? `; ${damage}` : ""}.`;
}
function thrustContext(winners: Reading[], completed: InsightGroup[]): string {
  const fuels = readings(completed, group => group.first.measurement.fuel_used_kg);
  const lowerFuel = minimum(fuels)[0];
  const winnerFuel = winners.length === 1 ? nonnegative(winners[0].group.first.measurement.fuel_used_kg) : null;
  const damage = damageWarning(completed);
  if (winnerFuel !== null && lowerFuel && lowerFuel.value < winnerFuel) {
    return `${name(lowerFuel.group)} used less estimated fuel, ${decimal(lowerFuel.value, 2)} kg versus ${name(winners[0].group)}'s ${decimal(winnerFuel, 2)} kg; engine-on time alone is not fuel efficiency${damage ? `, and ${damage}` : ""}.`;
  }
  if (fuels.length < completed.length) return `Estimated fuel is unavailable for some completed flights; engine-on time alone is not fuel efficiency${damage ? `, and ${damage}` : ""}.`;
  return damage ? `Check estimated fuel and damage too; ${damage}.` : "Check estimated fuel and arrival damage alongside engine-on time.";
}
function selectedObservation(metric: Exclude<Metric, "turn">, selection: Selection): string {
  const { completed, comparable } = selection;
  if (!completed.length) return noArrivals();
  const rows = readings(completed, group => selectedValue(metric, group));
  if (!rows.length) return `Completed flights have no available ${metric === "thrust" ? "native translation-thrust" : metric} measurements. Record those measurements before comparing completed flights.`;
  if (!comparable) {
    const data = rows.slice(0, 2).map(row => `${name(row.group)} recorded ${quantity(metric, row.value)} on a completed flight`).join("; ");
    const single = completed.length === 1 ? `${name(rows[0].group)} has the only completed flight, with ${quantity(metric, rows[0].value)}` : data;
    return `${single}. Different or unavailable seed/repetition scopes leave these readings unranked.`;
  }
  if (rows.length === 1) {
    const only = completed.length === 1 ? "the only completed flight" : `the only available ${metric} reading among completed flights`;
    return `${name(rows[0].group)} has ${only}, with ${quantity(metric, rows[0].value)}. More completed-flight measurements are needed for a comparison.`;
  }
  const winners = minimum(rows);
  const next = winners.length === 1 ? minimum(rows.filter(row => row.value > winners[0].value))[0] : undefined;
  const among = rows.length < completed.length ? "completed flights with available data" : "these completed flights";
  const tie = winners.length > 1 ? " each" : "";
  let summary = metric === "time"
    ? `${names(winners)}${tie} reached the station in ${decimal(winners[0].value)} s, the shortest measured time among ${among}`
    : `${names(winners)}${tie} used ${quantity(metric, winners[0].value)}, the least among ${among}`;
  if (metric === "fuel" && next) summary += `; ${name(next.group)} used ${decimal(next.value, 2)} kg`;
  const damage = damageWarning(completed);
  const context = metric === "fuel" ? fuelContext(winners[0], next, completed)
    : metric === "thrust" ? thrustContext(winners, completed)
    : `Compare estimated fuel and arrival damage alongside duration${damage ? `; ${damage}` : ""}.`;
  return `${summary}. ${context}`;
}
function turnObservation(selection: Selection): string {
  const { completed, comparable } = selection;
  if (!completed.length) return noArrivals();
  const rows = readings(completed, group => total(group.first, "turn_duration_s"));
  if (!rows.length) return "Physical-turn totals are unavailable for completed flights in this selection. Record native turning and angular-actuation totals to compare spin.";
  // Describe an actual turning example, not an efficiency ranking of turns.
  const row = (comparable ? rows.find(entry => {
    const angular = total(entry.group.first, "angular_burn_duration_s");
    return angular !== null && entry.value > angular;
  }) ?? rows.find(entry => entry.value > 0) : undefined) ?? rows[0];
  const turns = integerCount(total(row.group.first, "turn_count"));
  const angle = total(row.group.first, "total_turn_angle_rad");
  const angular = total(row.group.first, "angular_burn_duration_s");
  const turnUnit = turns !== null && new Intl.PluralRules("en").select(turns) === "one" ? "turn" : "turns";
  const count = turns === null ? "" : ` across ${decimal(turns, 0)} ${turnUnit}`;
  const rotation = angle === null ? "" : `${turns === null ? ", through" : " and"} ${decimal(angle, 2)} rad`;
  const actuation = angular === null ? "; angular-thrust duration is unavailable" : `, versus ${decimal(angular)} s of angular thrust`;
  const lead = completed.length === 1 ? `${name(row.group)} has the only completed flight, recording` : `${name(row.group)} recorded`;
  const summary = `${lead} ${decimal(row.value)} s of physical turning${count}${rotation}${actuation}.`;
  const damage = damageWarning(completed);
  const caution = damage ? `; compare fuel and damage too, since ${damage}` : "";
  if (!comparable) return `${summary} Different or unavailable seed/repetition scopes leave these totals unranked; physical turning can continue without angular thrust${caution}.`;
  if (angular !== null && row.value > angular) return `${summary} Physical turning can outlast angular thrust through coasting; the totals do not locate those intervals${caution}.`;
  return `${summary} Physical turning measures motion; angular actuation measures thrust, and their durations may overlap${caution}.`;
}
function cohortScope(groups: InsightGroup[]): string {
  return `Full-cohort medians, not the selected seed · ${groups.map(group => `${name(group)} ${countText(profileCounts(group.profile!))}`).join("; ")}`;
}
function cohortContext(winners: Reading[], completed: InsightGroup[], excluded: boolean): string {
  const prefix = excluded ? "Cohorts without all arrivals are excluded; " : "";
  const damage = damageWarning(completed, true);
  const fuels = readings(completed, group => group.profile?.median_fuel_used_kg);
  const lowerFuel = minimum(fuels)[0];
  const winnerFuel = winners.length === 1 ? nonnegative(winners[0].group.profile?.median_fuel_used_kg) : null;
  if (winnerFuel !== null && lowerFuel && lowerFuel.value < winnerFuel) {
    return `${prefix}${name(lowerFuel.group)} has a lower median fuel use, ${decimal(lowerFuel.value, 2)} kg versus ${name(winners[0].group)}'s ${decimal(winnerFuel, 2)} kg; ${damage ?? "compare median damage too"}.`;
  }
  return `${prefix}${excluded ? "compare" : "Compare"} median fuel and damage alongside duration${damage ? `; ${damage}` : ""}.`;
}
function cohortTime(groups: InsightGroup[]): string {
  const completed = groups.filter(group => completedCohort(group.profile!));
  // Never substitute a selected-seed duration for a missing full-cohort median.
  const rows = readings(completed, group => group.profile?.median_time_s);
  if (!rows.length) {
    const recorded = readings(groups, group => group.profile?.median_time_s)[0];
    const detail = recorded ? `${name(recorded.group)}'s full-cohort median is ${decimal(recorded.value)} s, with ${countText(profileCounts(recorded.group.profile!))}; no all-arrival cohort has an available median duration.`
      : "No all-arrival cohort has an available median duration.";
    return `${detail} Compare arrival times only for cohorts whose recorded attempts all reached the station.`;
  }
  if (rows.length === 1) {
    const row = rows[0];
    return `${name(row.group)} is the only all-arrival cohort with an available median duration, ${decimal(row.value)} s, with ${countText(profileCounts(row.group.profile!))}. More all-arrival cohorts with duration data are needed for a comparison.`;
  }
  const winners = minimum(rows);
  const among = rows.length < completed.length ? "all-arrival cohorts with available duration data" : "all-arrival cohorts";
  const summary = winners.length === 1
    ? `${name(winners[0].group)} has the shortest full-cohort median among ${among}, ${decimal(winners[0].value)} s, with ${countText(profileCounts(winners[0].group.profile!))}`
    : `${names(winners)} share the shortest full-cohort median among ${among}, ${decimal(winners[0].value)} s`;
  return `${summary}. ${cohortContext(winners, completed, completed.length < groups.length)}`;
}
function mixedTime(groups: InsightGroup[]): MetricInsight {
  const data = groups.flatMap(group => {
    const value = nonnegative(group.profile ? group.profile.median_time_s : group.first.measurement.time_s);
    if (value === null) return [];
    return [group.profile
      ? `${name(group)}'s full-cohort median is ${decimal(value)} s, with ${countText(profileCounts(group.profile))}`
      : `${name(group)}'s selected-run duration is ${decimal(value)} s, ${arrived(group.first) ? "with a recorded arrival" : "without a recorded arrival"}`];
  });
  return {
    guidance: GUIDANCE.time,
    observation: `${data.length ? data.slice(0, 2).join("; ") : "Duration data are unavailable for these mixed scopes"}. Full-cohort medians and selected-run durations have different scopes and are unranked.`,
    scope: "Mixed full-cohort and selected-run scopes · unranked",
  };
}

export function deriveMetricInsight(metric: "fuel" | "time" | "thrust" | "turn", groups: InsightGroup[]): MetricInsight {
  const guidance = GUIDANCE[metric];
  if (!groups.length) return { guidance, observation: "No measured runs in this selection. Choose a seed or run a test to compare completed flights.", scope: "No selected runs." };
  if (metric === "time") {
    const profiled = groups.filter(group => group.profile !== undefined);
    if (profiled.length === groups.length) return { guidance, observation: cohortTime(groups), scope: cohortScope(groups) };
    if (profiled.length) return mixedTime(groups);
  }
  const selection = selectedScope(groups);
  const meaning = metric === "thrust" ? " · Engine-on time, not burn count, fuel use, or an official score"
    : metric === "fuel" ? " · Local fuel estimate"
    : metric === "turn" ? " · Native physical turning and angular actuation" : "";
  return {
    guidance,
    observation: metric === "turn" ? turnObservation(selection) : selectedObservation(metric, selection),
    scope: selection.scope + meaning,
  };
}
