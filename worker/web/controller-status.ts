// Read-only presentation of supplied native reports. No wall-clock liveness or
// first-start time can be established from a latest accepted command receipt.
export type ControllerStatus = { state: string; eventKey: string; headline: string; evidence: string };

type Authority = {
  status: "idle" | "active" | "expired" | "revoked";
  epoch: number; term: number;
  run_id: string | null; controller_id: string | null;
  expires_at_sim_time_s: number | null;
};
type Fence = { epoch: number; term: number; run_id: string; controller_id: string };
type Watchdog = {
  status: "idle" | "active" | "expired" | "sensor_changed" | "restored" | "revoked";
  source_time_s: number | null; expires_at_sim_time_s: number | null;
  controller_fence: Fence | null;
};
type Presentation = Omit<ControllerStatus, "eventKey">;
type RequestedCommand = "zero" | "nonzero" | "unknown";

const record = (value: unknown): value is Record<string, unknown> => value !== null
  && typeof value === "object" && !Array.isArray(value)
  && [Object.prototype, null].includes(Object.getPrototypeOf(value));
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;
const unsigned = (value: unknown): value is number => finite(value) && Number.isInteger(value) && value <= 4294967295;
const controlId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value);

// Read own data properties only, without invoking accessors on unknown inputs.
function field(value: unknown, name: string): unknown {
  if (!record(value)) return undefined;
  const property = Object.getOwnPropertyDescriptor(value, name);
  return property?.enumerable && Object.hasOwn(property, "value") ? property.value : undefined;
}

function shape(value: unknown, required: string[], optional: string[] = []): value is Record<string, unknown> {
  return record(value) && required.every(key => Object.hasOwn(value, key))
    && Reflect.ownKeys(value).every(key => typeof key === "string" && (required.includes(key) || optional.includes(key)))
    && Object.values(Object.getOwnPropertyDescriptors(value)).every(property => property.enumerable && Object.hasOwn(property, "value"));
}

function readAuthority(value: unknown): Authority | null {
  if (!shape(value, ["status", "epoch", "term", "run_id", "controller_id", "expires_at_sim_time_s"])) return null;
  const { status, epoch, term, run_id, controller_id, expires_at_sim_time_s: expiry } = value;
  if (!unsigned(epoch) || !unsigned(term) || (run_id !== null && !controlId(run_id))
    || (controller_id !== null && !controlId(controller_id)) || (expiry !== null && !finite(expiry))) return null;
  switch (status) {
    case "idle":
      if (term !== 0 || run_id !== null || controller_id !== null || expiry !== null) return null;
      break;
    case "active":
    case "expired":
      if (term === 0 || epoch === 4294967295 || run_id === null || controller_id === null || expiry === null) return null;
      break;
    case "revoked":
      // Native revocation advances the epoch and removes the owner and deadline.
      if (controller_id !== null || expiry !== null) return null;
      break;
    default: return null;
  }
  return { status, epoch, term, run_id, controller_id, expires_at_sim_time_s: expiry };
}

function readFence(value: unknown): Fence | null {
  if (!shape(value, ["epoch", "term", "run_id", "controller_id"])) return null;
  const { epoch, term, run_id, controller_id } = value;
  if (!unsigned(epoch) || !unsigned(term) || term === 0 || !controlId(run_id) || !controlId(controller_id)) return null;
  return { epoch, term, run_id, controller_id };
}

function readWatchdog(value: unknown, time: number): Watchdog | null {
  if (!shape(value, ["status", "source_time_s", "expires_at_sim_time_s"], ["controller_fence"])) return null;
  const { status, source_time_s: source, expires_at_sim_time_s: expiry } = value;
  const rawFence = field(value, "controller_fence");
  const fence = rawFence === undefined || rawFence === null ? null : readFence(rawFence);
  if (rawFence !== undefined && rawFence !== null && fence === null) return null;
  if (status === "idle") {
    return source === null && expiry === null && fence === null
      ? { status, source_time_s: source, expires_at_sim_time_s: expiry, controller_fence: fence } : null;
  }
  if (status !== "active" && status !== "expired" && status !== "sensor_changed" && status !== "restored" && status !== "revoked") return null;
  if (!finite(source) || !finite(expiry) || source > time
    || expiry - source < 0.05 - 1e-9 || expiry - source > 1 + 1e-9) return null;
  return { status, source_time_s: source, expires_at_sim_time_s: expiry, controller_fence: fence };
}

function matches(authority: Authority, fence: Fence | null): boolean {
  return fence !== null && fence.epoch === authority.epoch && fence.term === authority.term
    && fence.run_id === authority.run_id && fence.controller_id === authority.controller_id;
}

function vector(value: unknown): value is number[] {
  return Array.isArray(value) && Object.getPrototypeOf(value) === Array.prototype && value.length === 3
    && Reflect.ownKeys(value).every(key => ["0", "1", "2", "length"].includes(String(key)))
    && ["0", "1", "2"].every(key => {
      const property = Object.getOwnPropertyDescriptor(value, key);
      return property !== undefined && Object.hasOwn(property, "value")
        && typeof property.value === "number" && Number.isFinite(property.value);
    });
}

function requestedCommand(observation: unknown, time: unknown): RequestedCommand {
  const physics = field(observation, "applied_physics");
  // Navigation faults can leave an older physics readback in the observation.
  if (!finite(time) || field(physics, "source_time_s") !== time) return "unknown";
  const requested = field(physics, "requested");
  if (!shape(requested, ["application", "accel_body_mps2", "angular_accel_body_rad_s2"])
    || typeof requested.application !== "string"
    || !["center_of_mass", "manual_nozzles", "practice_boost"].includes(requested.application)) return "unknown";
  const linear = requested.accel_body_mps2, angular = requested.angular_accel_body_rad_s2;
  if (!vector(linear) || !vector(angular)) return "unknown";
  return [...linear, ...angular].some(component => component !== 0) ? "nonzero" : "zero";
}

function commandCaveat(command: RequestedCommand): string {
  switch (command) {
    case "zero": return "Native requested command is zero; motion may continue.";
    case "nonzero": return "Native requested command is nonzero; this does not verify leased controller activity.";
    case "unknown": return "Current command output is unverified. A native stop is not confirmed.";
  }
}

function identity(authority: Authority): string {
  const run = authority.run_id === null ? "" : `run ${authority.run_id.slice(-8)}`;
  return authority.controller_id === null ? run : `owner ${authority.controller_id.slice(-8)} · ${run}`;
}

function timestamp(value: number): string {
  return new Intl.NumberFormat("en", { useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value === 0 ? 0 : value);
}

const presentation = (state: string, headline: string, evidence: string): Presentation => ({ state, headline, evidence });
const unknownStatus = (): Presentation => presentation("unknown", "Controller status unknown",
  "Native controller status is missing or invalid. Inspect the flight before sending more commands.");
const claimedStatus = (authority: Authority): Presentation => presentation("claimed", "Control claimed; waiting for a command",
  `Native control authority: active; ${identity(authority)}. No current matching leased command is verified.`);

function nativeStatus(observation: unknown, runtime: unknown, authority: Authority | null): Presentation {
  const time = field(observation, "time_s"), mission = field(field(observation, "mission"), "status");
  const running = field(runtime, "running"), keyboard = field(runtime, "keyboard_thrust");
  const revision = field(runtime, "trial_revision"), observedRevision = field(observation, "trial_revision");
  const command = requestedCommand(observation, time);
  const sameTrial = observedRevision === undefined || unsigned(observedRevision) && unsigned(revision) && observedRevision === revision;
  // A terminal mission outranks a leftover active lease, even without a receipt.
  if (sameTrial && finite(time) && (mission === "success" || mission === "failed_damage")) {
    return presentation("ended", mission === "success" ? "Flight ended · arrived" : "Flight ended · impact damage", mission === "success"
      ? `Native mission reports arrival. Observation T+${timestamp(time)} s.`
      : `Native mission reports failure from impact damage. Observation T+${timestamp(time)} s.`);
  }
  if (keyboard === true) return presentation("manual", "Manual keyboard control",
    `Native runtime reports keyboard thrust. ${commandCaveat(sameTrial ? command : "unknown")}`);
  if (!finite(time) || mission !== "running" || typeof running !== "boolean" || keyboard !== false
    || !unsigned(revision) || !sameTrial || authority === null) return unknownStatus();
  const expiry = authority.expires_at_sim_time_s;
  if (expiry !== null && expiry > time + 1 + 1e-9) return unknownStatus();
  // The engine also has a private runtime deadline. Reported expiry is decisive
  // even if paused simulation time has not reached the reported sim-time value.
  if (authority.status === "expired") return presentation("expired", "Controller lease expired",
    `Native control authority: expired; ${identity(authority)}. ${commandCaveat(command)}`);
  if (authority.status === "revoked") return presentation("revoked", "Controller released control",
    `Native control authority: revoked${authority.run_id === null ? "." : `; ${identity(authority)}.`} ${commandCaveat(command)}`);
  if (authority.status === "active" && (expiry === null || time >= expiry)) return unknownStatus();
  const watchdog = readWatchdog(field(observation, "command_watchdog"), time);
  if (authority.status === "idle") {
    if (watchdog?.status !== "idle") return unknownStatus();
    if (command === "nonzero") return presentation("unleased", "Unleased API command",
      "Native control authority: idle. No leased controller active. Native requested command is nonzero.");
    return presentation("idle", "Controller idle", "Native control authority: idle. No leased command accepted.");
  }
  if (watchdog === null || !matches(authority, watchdog.controller_fence)) return claimedStatus(authority);
  switch (watchdog.status) {
    case "active": {
      const source = watchdog.source_time_s, deadline = watchdog.expires_at_sim_time_s;
      if (source === null || deadline === null || source > time || time >= deadline || expiry !== deadline) return claimedStatus(authority);
      return presentation(running ? "running" : "paused", running ? "Controller running" : "Controller owns control · flight paused",
        `Latest accepted command: T+${timestamp(source)} s. Command expiry: T+${timestamp(deadline)} s. Verified ${identity(authority)}.`);
    }
    case "expired": return presentation("expired", "Controller lease expired",
      `Native command watchdog: expired; ${identity(authority)}. ${commandCaveat(command)}`);
    case "sensor_changed": return presentation("sensor_changed", "Controller command cleared by sensor change",
      `Native command watchdog: sensor_changed; ${identity(authority)}. ${commandCaveat(command)}`);
    case "restored": return presentation("restored", "Controller command cleared after restore",
      `Native command watchdog: restored; ${identity(authority)}. ${commandCaveat(command)}`);
    case "revoked": return presentation("revoked", "Controller released control",
      `Native command watchdog: revoked; ${identity(authority)}. ${commandCaveat(command)}`);
    case "idle": return claimedStatus(authority);
  }
}

/** Use eventKey only to detect lifecycle changes; evidence may update separately. */
export function describeControllerStatus(connection: unknown, observation: unknown, runtime: unknown): ControllerStatus {
  const authority = readAuthority(field(observation, "control_authority"));
  const revision = field(runtime, "trial_revision");
  const native = nativeStatus(observation, runtime, authority);
  const status = field(connection, "status"), error = field(connection, "error");
  let view: Presentation;
  if (typeof error === "string" && error.includes("Another browser page holds the local simulation lease")) {
    view = presentation("other_page", "Python API attached to another page",
      "This page is not receiving local Python API calls. Switch to the connected game; do not reset either flight.");
  } else if (status === "disconnected" || status === "error") {
    const previous = native.state === "unknown" ? "Native controller state is unverified."
      : `Last native observation: ${native.headline}. ${native.evidence}`;
    view = status === "disconnected" ? presentation("disconnected", "Python API disconnected",
      `${previous} Disconnection alone does not confirm a native stop. Reconnect the local Python API and inspect the flight before sending more commands.`)
      : presentation("error", "Python API connection error",
        `${previous} A connection error does not confirm a native stop. Check the local Python service and reconnect before sending more commands.`);
  } else if (status === "waiting_for_engine") {
    view = presentation("waiting_for_engine", "Python API waiting for engine",
      "Wait for the connected game's renderer. No controller activity is verified.");
  } else {
    view = status === "connected" ? native : presentation("unknown", "Controller status unknown",
      "Local Python API connection is not verified. Check the local Python service and inspect the flight before sending commands.");
  }
  // Native IDs identify a consent cohort and owner, never a queue job or profile.
  // Keep complete validated IDs in this comparison-only key, shortened in copy.
  const run = view.state === "ended" ? "terminal" : authority === null ? "unknown" : `${authority.epoch}:${authority.term}:${authority.run_id ?? "-"}:${authority.controller_id ?? "-"}`;
  const outcome = view.state === "ended" ? field(field(observation, "mission"), "status") : "-";
  return { ...view, eventKey: `${view.state}:${run}:${unsigned(revision) ? revision : "unknown"}:${outcome}` };
}
