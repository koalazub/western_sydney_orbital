import { flightWorlds, type FlightSelection } from "./flight-selection.ts";
import type { RoomEngine } from "./room-control.ts";

const sameData = (left: unknown, right: unknown): boolean => {
  if (left === right) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  const a = left as Record<string, unknown>, b = right as Record<string, unknown>;
  return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(key => Object.hasOwn(b, key) && sameData(a[key], b[key]));
};

const nativeSettings = (value: unknown): unknown => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  const settings = value as Record<string, unknown>, debris = settings.debris;
  if (!debris || typeof debris !== "object" || Array.isArray(debris)) return value;
  // Native serde omits only this default distribution variant. Do not default
  // null, accept a different mode, or fill any missing numeric setup fields.
  if (Object.hasOwn(debris, "distribution")) return value;
  return { ...settings, debris: { ...debris, distribution: { kind: "directed_stream" } } };
};

export async function freshTrial(api: RoomEngine, selection: FlightSelection, signal: AbortSignal) {
  signal.throwIfAborted();
  const world = flightWorlds.find(world => world.id === selection.worldId);
  if (!world) throw new Error("Choose a supported flight world.");
  const before = JSON.parse(api.observation()), runtime = JSON.parse(api.runtime_status());
  if (runtime.keyboard_thrust === true || before.control_authority?.status === "active") {
    throw new Error("Stop the active controller and release manual thrust before replacing this flight.");
  }
  const result = JSON.parse(api.control(JSON.stringify({
    op: "configure_flight", preset: world.preset, seed: selection.seed, settings: world.settings,
  })));
  if (result.ok !== true) throw new Error("Fresh flight was rejected. No request was retried.");
  const end = performance.now() + 10_000;
  let unconfirmed = ["native_readback"];
  while (performance.now() < end) {
    signal.throwIfAborted();
    const actual = JSON.parse(api.observation()), playback = JSON.parse(api.runtime_status());
    const checks = {
      trial_revision: actual.trial_revision !== before.trial_revision,
      time_s: actual.time_s === 0,
      running: playback.running === false,
      seed: actual.seed === selection.seed,
      flight_preset: actual.flight_preset === world.preset,
      experiment_settings: sameData(nativeSettings(actual.experiment_settings), world.settings),
      launch_site_direction: actual.launch_site_direction === null,
      debris_count: actual.geometry?.debris_count === world.settings.debris.count,
      arrival_radius_m: actual.geometry?.arrival_radius_m === 50,
      ship_tuning: sameData(actual.ship_tuning, { ...world.settings.ship, thrust_scale: 1 }),
      debris_flow: sameData(actual.debris_flow, Object.fromEntries(["speed_mps", "speed_spread_mps", "stream_axis", "stream_sign"]
        .map(key => [key, world.settings.debris[key]]))),
      max_accel_mps2: actual.max_accel_mps2 === 0.5,
      max_angular_accel_rad_s2: actual.max_angular_accel_rad_s2 === 0.05,
    };
    unconfirmed = Object.entries(checks).filter(([, confirmed]) => !confirmed).map(([field]) => field);
    if (unconfirmed.length === 0) {
      const fields = ["trial_revision", "tuning_revision", "manual_interventions", "seed"];
      if (fields.some(field => !Number.isSafeInteger(actual[field]) || actual[field] < 0)) {
        unconfirmed = ["consent_context"];
        break;
      }
      return Object.fromEntries([...fields.map(field => [field, actual[field]]), ["running", false]]);
    }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`The selected fresh world was not confirmed. Unconfirmed fields: ${unconfirmed.join(", ")}. No reset or run was retried.`);
}
