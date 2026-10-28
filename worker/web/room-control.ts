// The room transports public reports and bounded native commands. It never owns
// the simulation clock, renderer, camera acquisition or controller watchdog.
export type RoomEngine = {
  observation: () => string;
  telemetry_status: () => string;
  navigation_status: () => string;
  runtime_status: () => string;
  renderer_status: () => string;
  ship_camera_status: () => string;
  control: (json: string) => string;
};
type Build = { bytes: number; sha256: string };
type NativeErrorCode = "stale_context" | "command_expired" | "sensor_unavailable" | "sensor_no_sample";
type Result = { ok: true; value: Record<string, unknown> } | { ok: false; error: string; code?: NativeErrorCode };
type Connection = { origin: string; id: string; key: string };
type Execution = { commandInvoked: boolean; engineFailed: boolean };

const CALL_LIMIT = 64 * 1024;
const REPORT_LIMIT = 512 * 1024;
const MAX_DEADLINE_MS = 10_000;
const MAX_IDS = 2048;
const encoder = new TextEncoder();
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const unsigned = (value: unknown) => typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= 4294967295;
const controlId = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value);
const messageId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_-]{1,100}$/.test(value);

class CallError extends Error {}
function invalid(message: string): never { throw new CallError(`${message}; no command was invoked.`); }

function shape(value: unknown, required: string[], optional: string[] = []): asserts value is Record<string, unknown> {
  if (!record(value) || required.some(key => !Object.hasOwn(value, key))
    || ![Object.prototype, null].includes(Object.getPrototypeOf(value))
    || Reflect.ownKeys(value).some(key => typeof key !== "string" || !required.includes(key) && !optional.includes(key))
    || Object.values(Object.getOwnPropertyDescriptors(value)).some(field => !field.enumerable || !Object.hasOwn(field, "value"))) {
    invalid("Unexpected JSON fields or missing required fields");
  }
}

function context(value: unknown) {
  shape(value, ["trial_revision", "tuning_revision", "manual_interventions", "seed", "running"]);
  if (![value.trial_revision, value.tuning_revision, value.manual_interventions, value.seed].every(unsigned)
    || typeof value.running !== "boolean") invalid("Invalid expected_context");
}

function lease(command: Record<string, unknown>) {
  if (typeof command.source_time_s !== "number" || !Number.isFinite(command.source_time_s) || command.source_time_s < 0
    || typeof command.lease_sim_s !== "number" || !Number.isFinite(command.lease_sim_s)
    || command.lease_sim_s < 0.05 || command.lease_sim_s > 1) invalid("Invalid source timestamp or lease");
  // Do not compare with a replacement observation, advance time or renew source.
  // The native simulation independently checks source_time_s + lease_sim_s.
}

function fence(value: unknown) {
  shape(value, ["epoch", "term", "run_id", "controller_id"]);
  if (!unsigned(value.epoch) || !unsigned(value.term) || !controlId(value.run_id) || !controlId(value.controller_id)) {
    invalid("Invalid controller_fence");
  }
}

function vector(value: unknown, limit: number) {
  if (!Array.isArray(value) || value.length !== 3
    || Object.getPrototypeOf(value) !== Array.prototype
    || Reflect.ownKeys(value).some(key => !["0", "1", "2", "length"].includes(String(key)))
    || [0, 1, 2].some(index => !Object.hasOwn(Object.getOwnPropertyDescriptor(value, String(index)) ?? {}, "value"))
    || !value.every(x => typeof x === "number" && Number.isFinite(x))
    || Math.hypot(...value) > limit) invalid("Command vector exceeds the room limits or is invalid");
}

function command(value: unknown): asserts value is Record<string, unknown> {
  if (!record(value)) invalid("Expected a command object");
  switch (value.op) {
    case "claim_control":
      shape(value, ["op", "run_id", "controller_id", "expected_epoch", "expected_term", "source_time_s", "lease_sim_s", "expected_context"]);
      if (!controlId(value.run_id) || !controlId(value.controller_id) || !unsigned(value.expected_epoch) || !unsigned(value.expected_term)) {
        invalid("Invalid controller claim");
      }
      lease(value);
      break;
    case "release_control":
      shape(value, ["op", "run_id", "expected_epoch", "expected_context"]);
      if (!controlId(value.run_id) || !unsigned(value.expected_epoch)) invalid("Invalid controller release");
      break;
    case "set_leased_command":
      shape(value, ["op", "accel", "angular_accel", "source_time_s", "lease_sim_s", "expected_context", "expected_sensor_revision", "controller_fence"]);
      lease(value);
      if (!unsigned(value.expected_sensor_revision)) invalid("Invalid expected_sensor_revision");
      fence(value.controller_fence);
      vector(value.accel, 0.5);
      vector(value.angular_accel, 0.05);
      break;
    case "play":
      shape(value, ["op", "rate", "expected_context"]);
      if (value.rate !== 1) invalid("Room playback requires rate 1");
      break;
    default:
      invalid("Unsupported room command");
  }
  context(value.expected_context);
}

function deadline(value: unknown): asserts value is number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) invalid("Invalid room call deadline");
  const remaining = value - Date.now();
  if (remaining <= 0) invalid("Room call expired before engine invocation");
  if (remaining > MAX_DEADLINE_MS) invalid("Room call deadline is too far in the future");
}

function nativeReport(json: string): Record<string, unknown> {
  if (typeof json !== "string" || json.length > REPORT_LIMIT || encoder.encode(json).byteLength > REPORT_LIMIT) {
    throw new Error("Native report unavailable");
  }
  const value: unknown = JSON.parse(json);
  if (!record(value)) throw new Error("Native report unavailable");
  return value;
}

function nativeRejection(value: Record<string, unknown>): Result {
  switch (value.code) {
    case "stale_context":
      return { ok: false, code: value.code, error: "Native engine rejected a stale flight context; no command was applied." };
    case "command_expired":
      return { ok: false, code: value.code, error: "Native engine rejected an expired decision; no command was applied." };
    case "sensor_unavailable":
    case "sensor_no_sample":
      return { ok: false, code: value.code, error: "Native engine rejected unavailable sensor data." };
    default:
      // Never forward native parse errors, payloads, identifiers or debug state.
      return { ok: false, error: "Native engine rejected the room command." };
  }
}

// The public executor and the socket use this same synchronous path. Invocation
// tracking is private so an ACK never gains diagnostic or hidden state fields.
function runCall(
  api: RoomEngine,
  build: Build,
  call: Record<string, unknown>,
  execution: Execution = { commandInvoked: false, engineFailed: false },
): Result {
  try {
    if (!record(call) || call.version !== 1) invalid("Unsupported room call version");
    if (call.method === "snapshot") shape(call, ["version", "method", "deadline_ms"]);
    else if (call.method === "command") {
      shape(call, ["version", "method", "deadline_ms", "command"]);
      command(call.command);
    } else invalid("Unsupported room call method");
    deadline(call.deadline_ms);
    if (!Number.isSafeInteger(build.bytes) || build.bytes <= 0 || build.bytes > 64 * 1024 * 1024
      || typeof build.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(build.sha256)) invalid("Verified engine build is unavailable");
    const json = JSON.stringify(call);
    if (encoder.encode(json).byteLength > CALL_LIMIT) invalid("Room call exceeds the input limit");
    if (call.method === "snapshot") {
      const read = (getter: () => string) => {
        if (Date.now() >= (call.deadline_ms as number)) invalid("Room snapshot expired before its next report");
        return nativeReport(getter());
      };
      const value = {
        observation: read(() => api.observation()),
        telemetry: read(() => api.telemetry_status()),
        navigation: read(() => api.navigation_status()),
        runtime: read(() => api.runtime_status()),
        renderer: read(() => api.renderer_status()),
        engine_build: { bytes: build.bytes, sha256: build.sha256 },
        cameras: read(() => api.ship_camera_status()),
      };
      if (encoder.encode(JSON.stringify(value)).byteLength > REPORT_LIMIT) throw new Error("Native report unavailable");
      return { ok: true, value };
    }
    const request = JSON.stringify(call.command);
    if (Date.now() >= (call.deadline_ms as number)) invalid("Room call expired before engine invocation");
    execution.commandInvoked = true;
    const result = nativeReport(api.control(request));
    if (result.ok === false) return nativeRejection(result);
    if (result.ok !== true) throw new Error("Native acknowledgement unavailable");
    // Play is queued by the real engine. This ACK does not assert it has run.
    return { ok: true, value: { ok: true } };
  } catch (error) {
    if (execution.commandInvoked) {
      execution.engineFailed = true;
      return { ok: false, error: "Native engine command failed. Outcome is indeterminate. Inspect live state before further control." };
    }
    if (error instanceof CallError) return { ok: false, error: error.message };
    execution.engineFailed = true;
    return { ok: false, error: "Room request failed before command invocation; no command was invoked." };
  }
}

export function executeRoomCall(api: RoomEngine, build: Build, call: Record<string, unknown>): Result {
  return runCall(api, build, call);
}

function socketUrl(connection: Connection, pageOrigin: string | undefined): URL {
  shape(connection, ["origin", "id", "key"]);
  if (typeof connection.origin !== "string" || connection.origin.length > 2048
    || typeof connection.id !== "string" || !/^[a-f0-9-]{1,100}$/.test(connection.id)
    || typeof connection.key !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(connection.key)) {
    throw new Error("Invalid room connection");
  }
  const origin = new URL(connection.origin);
  if (origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash
    || origin.protocol !== "https:" && !(origin.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(origin.hostname))
    || origin.origin !== pageOrigin) throw new Error("Invalid room origin");
  // Construct only this relative route against the current page, not a supplied
  // WebSocket URL. Authentication belongs exclusively in the subprotocol.
  const url = new URL(`/api/rooms/${connection.id}/ws`, pageOrigin);
  url.protocol = origin.protocol === "https:" ? "wss:" : "ws:";
  return url;
}

export function connectRoom(
  connection: Connection,
  api: RoomEngine,
  build: Build,
  report: (status: string, error?: string) => void,
  isReady: () => boolean,
  // Non-browser peers provide their actual endpoint origin explicitly. The
  // shipped page always uses its own location, never an origin from the link.
  pageOrigin: string | undefined = typeof location === "undefined" ? undefined : location.origin,
): { disconnect: () => void } {
  const notify = (status: string, error?: string) => {
    try { report(status, error); } catch { /* Status rendering cannot interrupt socket cleanup. */ }
  };
  let socket: WebSocket;
  try {
    const actualOrigin = typeof location === "undefined" ? pageOrigin : location.origin;
    socket = new WebSocket(socketUrl(connection, actualOrigin), ["wsorb", `auth.${connection.key}`]);
  } catch {
    notify("error", "Room connection could not open. Use a same-origin room on HTTPS or local loopback.");
    return { disconnect() {} };
  }
  let stopped = false;
  let clientId: string | undefined;
  let claiming = false;
  let ownsHost = false;
  let anyCommandInvoked = false;
  const seen = new Map<string, number>();
  const ready = () => { try { return isReady() === true; } catch { return false; } };
  // Browser close() accepts 1000 or application codes, not reserved 1008/1009.
  const stop = (status: string, error?: string, code: 1000 | 4003 | 4008 | 4009 = 1000) => {
    if (stopped) return;
    stopped = true;
    globalThis.removeEventListener?.("pagehide", pagehide);
    const release = ownsHost;
    ownsHost = false;
    if (release && socket.readyState === WebSocket.OPEN) {
      try { socket.send(JSON.stringify({ type: "release-host" })); } catch { /* Close still releases the room host. */ }
    }
    try { socket.close(code, "Room control stopped"); } catch { /* The socket may already be gone. */ }
    // Never release native control, pause, reset or otherwise change the flight.
    // The native watchdog, not this socket, expires abandoned actuation.
    notify(status, error);
  };
  const pagehide = () => stop("disconnected", "This page left the room. Reconnection requires explicit consent.");
  const send = (value: Record<string, unknown>) => {
    if (socket.readyState !== WebSocket.OPEN) throw new Error("Room socket unavailable");
    socket.send(JSON.stringify(value));
  };
  globalThis.addEventListener?.("pagehide", pagehide, { once: true });
  notify("connecting");
  socket.addEventListener("message", event => {
    if (stopped) return;
    if (typeof event.data !== "string" || event.data.length > REPORT_LIMIT || encoder.encode(event.data).byteLength > REPORT_LIMIT) {
      stop("error", "Room message exceeds the input limit or is not JSON text.", 4009);
      return;
    }
    let message: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(event.data);
      if (!record(parsed)) throw new Error("Invalid room message");
      message = parsed;
    } catch {
      stop("error", "Room sent an invalid JSON message.", 4003);
      return;
    }
    if (message.type === "welcome") {
      if (clientId || !messageId(message.clientId) || message.hostId !== null && !messageId(message.hostId)) {
        stop("error", "Room sent an invalid welcome.", 4008);
        return;
      }
      clientId = message.clientId;
      if (message.hostId !== null && message.hostId !== clientId) {
        stop("conflict", "Another browser already owns this room. Its host was not replaced.", 4008);
        return;
      }
      claiming = true;
      try { send({ type: "claim-host" }); }
      catch { stop("error", "Room host claim could not be sent."); return; }
      notify("claiming");
    } else if (message.type === "host") {
      if (!clientId || !claiming) {
        stop("error", "Room host grant arrived before this browser claimed it.", 4008);
      } else if (message.hostId !== clientId) {
        ownsHost = false;
        stop("conflict", "This browser does not own the room. No takeover was attempted.", 4008);
      } else {
        ownsHost = true;
        notify("connected");
      }
    } else if (message.type === "execute") {
      if (!ownsHost) {
        stop("error", "Room execution arrived without this browser's host grant.", 4008);
        return;
      }
      if (encoder.encode(event.data).byteLength > CALL_LIMIT || !messageId(message.id)) {
        stop("error", "Room call exceeds the input limit or has an invalid ID.", 4008);
        return;
      }
      if (seen.has(message.id)) {
        stop("error", "Duplicate room call ID. The repeated call was not invoked.", 4008);
        return;
      }
      if (seen.size >= MAX_IDS) {
        const now = Date.now();
        // Never evict an ID while its original bounded deadline could be live.
        // A replay after eviction has expired and the executor rejects it too.
        for (const [id, expires] of seen) if (expires <= now) seen.delete(id);
        if (seen.size >= MAX_IDS) {
          stop("error", "Room call history is full; this call was not invoked.", 4008);
          return;
        }
      }
      seen.set(message.id, Date.now() + MAX_DEADLINE_MS);
      const execution: Execution = { commandInvoked: false, engineFailed: false };
      const engineReady = ready();
      const result: Result = engineReady
        ? runCall(api, build, message.command as Record<string, unknown>, execution)
        : { ok: false, error: "Browser engine is not ready; no command was invoked." };
      anyCommandInvoked ||= execution.commandInvoked;
      seen.set(message.id, Date.now() + MAX_DEADLINE_MS);
      try { send({ type: "result", id: message.id, ...result }); }
      catch {
        stop(execution.commandInvoked ? "indeterminate" : "error", execution.commandInvoked
          ? "Command result could not be sent. Outcome is indeterminate. Inspect live state before further control."
          : "Room result could not be sent; no command was invoked for this call.");
        return;
      }
      if (!engineReady) stop("error", "Browser engine is no longer ready. Room control stopped without a flight command.");
      else if (execution.engineFailed && !result.ok) stop(execution.commandInvoked ? "indeterminate" : "error", result.error);
    } else if (message.type === "error") {
      stop("error", "Room server rejected the connection.");
    }
    // Notebook documents, saved state and other room traffic are not engine input.
  });
  socket.addEventListener("error", () => stop(anyCommandInvoked ? "indeterminate" : "error", anyCommandInvoked
    ? "Room connection failed. Command delivery may be indeterminate. Inspect live state before further control."
    : "Room connection failed; no command was invoked."));
  socket.addEventListener("close", () => stop("disconnected", "Room connection closed. No reconnect was attempted."));
  return { disconnect: () => stop("disconnected") };
}
