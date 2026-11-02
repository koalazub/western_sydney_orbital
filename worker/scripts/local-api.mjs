import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, readFile, realpath, rename, unlink } from "node:fs/promises";
import { basename, dirname, relative, resolve, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { resolveNotebook } from "./notebook-launch.mjs";
import { emitDiagnostic, validTraceId } from "../shared/diagnostics.ts";
import { CONTROLLER_BODY_LIMIT, ControllerJobsError, controllerJobsAction, controllerJobsView, createControllerJobsState } from "../shared/controller-jobs.ts";

const BODY_LIMIT = 1024 * 1024;
const RESPONSE_LIMIT = 2 * BODY_LIMIT;
const BODY_TIMEOUT_MS = 3000;
const CALL_TIMEOUT_MS = 5000;
const WORLD_CALL_TIMEOUT_MS = 180000;
const CAPTURE_TIMEOUT_MS = 20000;
const PAGE_LEASE_MS = 6000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
const INVALID_CONTROL_ID = /[^A-Za-z0-9_-]/;
const CAMERA_MODES = new Set([
  "orbit", "mission", "first_person", "third_person", "follow", "free", "overview", "top", "ship_top", "ship_bottom",
]);
const FLIGHT_PRESETS = new Set(["mars_facing_l1", "far_side_l2", "competition_reference"]);
const WORLD_COMMANDS = new Set(["configure_experiment", "configure_flight", "reset"]);
const ERROR_CODES = new Set(["stale_context", "command_expired", "sensor_unavailable", "sensor_no_sample"]);
const worldSetup = call => call.method === "command" && WORLD_COMMANDS.has(call.command?.op);

class HttpError extends Error {
  constructor(status, message, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
const finiteBetween = (value, min, max) => typeof value === "number" && Number.isFinite(value) && value >= min && value <= max;
const invalid = message => { throw new HttpError(400, message); };

function shape(value, required, optional = []) {
  if (!record(value) || required.some(key => !Object.hasOwn(value, key))
    || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) {
    invalid("Unexpected JSON fields or missing required fields");
  }
}

function validateContext(context, required = false) {
  if (context == null && !required) return;
  shape(context, ["trial_revision", "tuning_revision", "manual_interventions", "seed", "running"]);
  for (const field of ["trial_revision", "tuning_revision", "manual_interventions", "seed"]) {
    if (!Number.isInteger(context[field]) || !finiteBetween(context[field], 0, 4294967295)) {
      invalid("Command context revisions and seed must be unsigned 32-bit integers");
    }
  }
  if (typeof context.running !== "boolean") invalid("Command context running must be a boolean");
}

function validateUnsigned(value, name) {
  if (!Number.isInteger(value) || !finiteBetween(value, 0, 4294967295)) {
    invalid(`${name} must be an unsigned 32-bit integer`);
  }
}

function validateId(value, name) {
  if (typeof value !== "string" || value.length < 1 || value.length > 64 || INVALID_CONTROL_ID.test(value)) {
    invalid(`${name} must contain 1 to 64 ASCII letters, digits, underscores or hyphens`);
  }
}

function validateLease(command) {
  if (!finiteBetween(command.lease_sim_s, 0.05, 1)) invalid("lease_sim_s must be finite and between 0.05 and 1");
  if (typeof command.source_time_s !== "number" || !Number.isFinite(command.source_time_s) || command.source_time_s < 0) {
    invalid("source_time_s must be a finite non-negative number");
  }
}

function validateFence(fence) {
  shape(fence, ["epoch", "term", "run_id", "controller_id"]);
  validateUnsigned(fence.epoch, "controller_fence.epoch");
  validateUnsigned(fence.term, "controller_fence.term");
  validateId(fence.run_id, "run_id");
  validateId(fence.controller_id, "controller_id");
}

function validateCall(value) {
  if (value?.method === "snapshot") {
    shape(value, ["method"]);
    return;
  }
  if (value?.method === "preview") {
    shape(value, ["method", "preview"]);
    if (value.preview !== null && !record(value.preview)) invalid("preview must be a report or null");
    finiteJson(value.preview);
    return;
  }
  if (value?.method === "pan_tilt") {
    shape(value, ["method", "camera", "yaw_deg", "pitch_deg"]);
    if (!["top", "bottom"].includes(value.camera)
      || !finiteBetween(value.yaw_deg, -1e6, 1e6)
      || !finiteBetween(value.pitch_deg, -1e6, 1e6)) invalid("Invalid camera or pan/tilt angles");
    return;
  }
  if (value?.method === "capture") {
    shape(value, ["method", "camera"]);
    if (!["top", "bottom"].includes(value.camera)) invalid("Unknown ship camera");
    return;
  }
  if (value?.method === "raycast") {
    shape(value, ["method", "query"]);
    shape(value.query, ["target_world_m", "max_distance_m"]);
    if (!Array.isArray(value.query.target_world_m) || value.query.target_world_m.length !== 3
      || !value.query.target_world_m.every(x => finiteBetween(x, -1e7, 1e7))
      || !finiteBetween(value.query.max_distance_m, 1, 100000)) invalid("Choose a finite world target and ray length from 1 to 100000 m");
    return;
  }
  shape(value, ["method", "command"]);
  if (value.method !== "command" || !record(value.command)) invalid("Expected a public snapshot, preview, camera operation or command");
  const command = value.command;
  switch (command.op) {
    case "pause":
    case "stop":
    case "debug_upright":
    case "clear_sensor_faults":
      shape(command, ["op"]);
      break;
    case "play":
      shape(command, ["op"], ["rate", "expected_context"]);
      if (command.rate != null && ![1, 5, 10].includes(command.rate)) invalid("rate must be 1, 5 or 10");
      validateContext(command.expected_context);
      break;
    case "reset":
      shape(command, ["op"], ["seed"]);
      if (command.seed != null && (!Number.isInteger(command.seed) || !finiteBetween(command.seed, 0, 4294967295))) {
        invalid("seed must be an integer from 0 to 4294967295");
      }
      break;
    case "configure_experiment":
    case "configure_flight":
      shape(command, command.op === "configure_flight" ? ["op", "settings", "preset"] : ["op", "settings"], ["seed"]);
      if (command.op === "configure_flight" && !FLIGHT_PRESETS.has(command.preset)) invalid("Choose a Mars-facing L1, far-side L2 or competition reference launch setup");
      if (!record(command.settings)) invalid("Experiment settings must be an object");
      finiteJson(command.settings);
      if (command.seed != null && (!Number.isInteger(command.seed) || !finiteBetween(command.seed, 0, 4294967295))) {
        invalid("seed must be an integer from 0 to 4294967295");
      }
      break;
    case "place_lander":
      shape(command, ["op", "position_m"]);
      if (!Array.isArray(command.position_m) || command.position_m.length !== 3
        || !command.position_m.every(value => finiteBetween(value, -1e7, 1e7))) {
        invalid("position_m must contain three finite world coordinates between -10000000 and 10000000 m");
      }
      break;
    case "move_launchpad":
      shape(command, ["op", "surface_direction"], ["expected_context"]);
      validateContext(command.expected_context);
      if (!Array.isArray(command.surface_direction) || command.surface_direction.length !== 3
        || !command.surface_direction.every(value => finiteBetween(value, -1e6, 1e6))
        || command.surface_direction.every(value => value === 0)) {
        invalid("Choose a nonzero, finite surface direction with three components");
      }
      break;
    case "tune_ship":
      shape(command, ["op", "settings"]);
      shape(command.settings, ["mass_kg", "inertia_scale", "thrust_scale"]);
      finiteJson(command.settings);
      break;
    case "tune_debris_flow":
      shape(command, ["op", "settings"]);
      shape(command.settings, ["speed_mps", "speed_spread_mps", "stream_axis", "stream_sign"]);
      finiteJson(command.settings);
      break;
    case "step":
      shape(command, ["op", "seconds"]);
      if (!finiteBetween(command.seconds, 0, 30)) invalid("seconds must be finite and between 0 and 30");
      break;
    case "set_command":
      shape(command, ["op", "accel", "angular_accel"], ["expected_context"]);
      validateContext(command.expected_context);
      // This is a transport bound, not the engine's much smaller thrust limit.
      for (const vector of [command.accel, command.angular_accel]) {
        if (!Array.isArray(vector) || vector.length !== 3 || !vector.every(x => finiteBetween(x, -1e6, 1e6))) {
          invalid("accel and angular_accel must contain three finite numbers with magnitude at most 1000000");
        }
      }
      break;
    case "set_leased_command":
      shape(command, ["op", "accel", "angular_accel", "lease_sim_s", "source_time_s", "expected_context", "expected_sensor_revision"], ["controller_fence"]);
      validateContext(command.expected_context, true);
      validateUnsigned(command.expected_sensor_revision, "expected_sensor_revision");
      validateLease(command);
      if (Object.hasOwn(command, "controller_fence")) validateFence(command.controller_fence);
      for (const [name, limit] of [["accel", 0.5], ["angular_accel", 0.05]]) {
        const vector = command[name];
        if (!Array.isArray(vector) || vector.length !== 3 || !vector.every(x => finiteBetween(x, -limit, limit))
          || Math.hypot(...vector) > limit) invalid(`${name} must contain three finite numbers with vector norm at most ${limit}`);
      }
      break;
    case "claim_control":
      shape(command, ["op", "run_id", "controller_id", "expected_epoch", "expected_term", "source_time_s", "lease_sim_s", "expected_context"]);
      validateId(command.run_id, "run_id");
      validateId(command.controller_id, "controller_id");
      validateUnsigned(command.expected_epoch, "expected_epoch");
      validateUnsigned(command.expected_term, "expected_term");
      validateContext(command.expected_context, true);
      validateLease(command);
      break;
    case "renew_control":
      shape(command, ["op", "controller_fence", "source_time_s", "lease_sim_s", "expected_context", "expected_sensor_revision"]);
      validateFence(command.controller_fence);
      validateContext(command.expected_context, true);
      validateUnsigned(command.expected_sensor_revision, "expected_sensor_revision");
      validateLease(command);
      if (command.lease_sim_s > 0.75) invalid("Ownership renewal lease_sim_s must be at most 0.75");
      break;
    case "release_control":
      shape(command, ["op", "run_id", "expected_epoch", "expected_context"]);
      validateId(command.run_id, "run_id");
      validateUnsigned(command.expected_epoch, "expected_epoch");
      validateContext(command.expected_context, true);
      break;
    case "set_sensor_fault":
      shape(command, ["op", "target", "mode"], ["duration_sim_s"]);
      if (!["radar_primary", "radar_backup", "camera_top", "camera_bottom", "radar_bus", "navigation", "all_avionics"].includes(command.target)) {
        invalid("Unknown sensor fault target");
      }
      if (!["nominal", "unavailable", "frozen"].includes(command.mode)) invalid("Sensor fault mode must be nominal, unavailable or frozen");
      if (Object.hasOwn(command, "duration_sim_s") ? !finiteBetween(command.duration_sim_s, 0.05, 300) : command.mode !== "nominal") {
        invalid("Non-nominal sensor faults require duration_sim_s; any supplied duration must be finite and between 0.05 and 300");
      }
      break;
    case "view":
      shape(command, ["op"], ["mode", "focus", "zoom"]);
      if (command.mode != null && !CAMERA_MODES.has(command.mode)) invalid("Unknown camera mode");
      if (command.zoom != null && !finiteBetween(command.zoom, 0.05, 20)) invalid("zoom must be between 0.05 and 20");
      if (command.focus != null) {
        const focus = command.focus;
        const debris = typeof focus === "string" && /^Debris[1-9][0-9]{0,5}$/.test(focus) && Number(focus.slice(6)) <= 100000;
        if (!["Shuttle", "Lander", "Target", "LandingPad", "Phobos", "Mars", "L1", "L2"].includes(focus) && !debris) {
          invalid("Unknown focus identifier");
        }
      }
      break;
    default:
      invalid("Allowed command ops: pause, play, step, reset, set_command, set_leased_command, claim_control, renew_control, release_control, set_sensor_fault, clear_sensor_faults, stop, view, debug_upright, configure_experiment, configure_flight, place_lander, move_launchpad, tune_ship, tune_debris_flow");
  }
}

function finiteJson(value, depth = 0) {
  if (depth > 32) invalid("Result exceeds the JSON nesting limit");
  if (typeof value === "number" && !Number.isFinite(value)) invalid("Result contains a non-finite number");
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) finiteJson(child, depth + 1);
  }
}

function readJson(request, response, limit = BODY_LIMIT) {
  if (request.headers["content-type"]?.split(";")[0].trim().toLowerCase() !== "application/json") {
    throw new HttpError(415, "Content-Type must be application/json");
  }
  if (Number(request.headers["content-length"] ?? 0) > limit) throw new HttpError(413, "Body exceeds the request limit");
  return new Promise((resolveBody, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      request.off("data", data);
      request.off("end", end);
      request.off("error", interrupted);
      request.off("aborted", interrupted);
      response.off("close", interrupted);
      if (error) {
        request.pause();
        reject(error);
      } else resolveBody(value);
    };
    const interrupted = () => finish(new HttpError(400, "Request disconnected while reading JSON"));
    const data = chunk => {
      size += chunk.length;
      if (size > limit) finish(new HttpError(413, "Body exceeds the request limit"));
      else chunks.push(chunk);
    };
    const end = () => {
      try { finish(null, JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
      catch { finish(new HttpError(400, "Invalid JSON body")); }
    };
    const timer = setTimeout(() => finish(new HttpError(408, "JSON body timed out")), BODY_TIMEOUT_MS);
    request.on("data", data);
    request.once("end", end);
    request.once("error", interrupted);
    request.once("aborted", interrupted);
    response.once("close", interrupted);
  });
}

const inside = (root, path) => {
  const rel = relative(root, path);
  return !rel || (rel !== ".." && !rel.startsWith(`..${sep}`) && !rel.startsWith(sep));
};

export async function createLocalApi({ worker, publicRoot, rootRealPath, headless = false }) {
  const assetDiagnostics = process.env.WSORB_DIAGNOSTICS === "1";
  const token = randomBytes(32).toString("base64url");
  const authorization = Buffer.from(`Bearer ${token}`);
  const requestedPath = resolve(process.env.WSORB_LOCAL_CONNECTION || resolve(worker, "..", ".devenv/wsorb-local.json"));
  if (inside(publicRoot, requestedPath)) throw new Error("The local connection descriptor must be outside public assets");
  await mkdir(dirname(requestedPath), { recursive: true, mode: 0o700 });
  const descriptorPath = resolve(await realpath(dirname(requestedPath)), basename(requestedPath));
  if (inside(rootRealPath, descriptorPath)) throw new Error("The local connection descriptor must be outside public assets");

  // Both publication and removal take this short lock. A token check followed by
  // an unlocked unlink could otherwise remove a newer server's descriptor.
  async function withDescriptorLock(action) {
    const lockPath = `${descriptorPath}.lock`;
    const deadline = Date.now() + 1000;
    let lock;
    while (!lock) {
      try { lock = await open(lockPath, "wx", 0o600); }
      catch (error) {
        if (error.code !== "EEXIST") throw error;
        if (Date.now() >= deadline) throw new Error("Local connection descriptor is locked; check for a stopped server's .lock file");
        await delay(25);
      }
    }
    try { return await action(); }
    finally {
      try { await lock.close(); }
      finally { await unlink(lockPath); }
    }
  }

  async function removeDescriptor() {
    await withDescriptorLock(async () => {
      let file;
      try {
        const info = await lstat(descriptorPath);
        if (!info.isFile() || info.size > 4096) return;
        file = await open(descriptorPath, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        const opened = await file.stat();
        if (!opened.isFile() || opened.size > 4096) return;
        const current = JSON.parse(await file.readFile("utf8"));
        if (current?.token === token) await unlink(descriptorPath);
      } catch (error) {
        if (error.code !== "ENOENT" && error.code !== "ELOOP" && !(error instanceof SyntaxError)) throw error;
      } finally { await file?.close(); }
    });
  }

  const manifestPath = resolve(publicRoot, "sim/manifest.json");
  if ((await lstat(resolve(publicRoot, "sim"))).isSymbolicLink() || (await lstat(manifestPath)).isSymbolicLink()) {
    throw new Error("The local engine manifest must not use symlinks");
  }
  const engineBuild = JSON.parse(await readFile(manifestPath, "utf8"));
  shape(engineBuild, ["bytes", "sha256"]);
  if (!Number.isSafeInteger(engineBuild.bytes) || !finiteBetween(engineBuild.bytes, 1, 64 * BODY_LIMIT) || !SHA256.test(engineBuild.sha256)) {
    throw new Error("Invalid local engine manifest");
  }
  const clientSource = await readFile(resolve(worker, "web/local-api.js"), "utf8");
  const alternateConnection = requestedPath !== resolve(worker, "..", ".devenv/wsorb-local.json");
  const relativeConnection = relative(resolve(worker, ".."), descriptorPath);
  const connectionLabel = inside(resolve(worker, ".."), descriptorPath)
    ? relativeConnection : "PATH_TO_THIS_GAME_CONNECTION";
  const escapedConnection = connectionLabel.replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
  const notebookHelp = alternateConnection
    ? `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect Python to this game</title><style>:root{color-scheme:dark;font:1rem/1.6 system-ui,sans-serif;background:#101923;color:#edf3f7}body{max-width:42rem;margin:2rem auto;padding:0 1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><main><h1>Separate game connection</h1><p>This game uses a separate Python API connection. The existing notebook may control another flight, so this page will not redirect you to that editor.</p><p>Inspect this game's public state with its explicit connection file:</p><pre>devenv shell uv run --locked wsorb checkpoint --connection-file ${escapedConnection}</pre><p>Add <code>--run --progress</code> only when you want to fly this game. If a controller is already running, do not start a second one.</p><p>Close this help tab to return to your existing game window. Do not reload or reset it.</p></main></html>`
    : await readFile(resolve(worker, "web/notebook.html"), "utf8");
  const native = headless
    ? await (await import("./headless.mjs")).createHeadless({ publicRoot, rootRealPath, engineBuild })
    : null;
  let origin;
  let closed = false;
  let publication = Promise.resolve();
  let page = null;
  let pageTimer;
  // One control request and one GPU readback may coexist. A capture must not
  // hold the command channel while Python is controlling a flying vehicle.
  const pending = new Map();
  const nativePending = new Map();
  let controllerJobs = createControllerJobsState();

  function validateResult(result, call, browser = false, diagnostics = false) {
    const id = browser ? ["id"] : [];
    shape(result, result.ok === true ? [...id, "ok", "value"] : [...id, "ok", "error"], result.ok === true ? [] : ["code"]);
    if (result.ok !== true && result.ok !== false) invalid("Result ok must be a boolean");
    if (result.ok === false && (typeof result.error !== "string" || !result.error || result.error.length > 2048)) {
      invalid("Result error must be a nonempty string of at most 2048 characters");
    }
    if (result.ok === false && result.code !== undefined && !ERROR_CODES.has(result.code)) invalid("Unknown engine rejection code");
    if (result.ok !== true) return;
    if (call.method === "capture") {
      shape(result.value, ["metadata", "rgb_base64"]);
      if (!record(result.value.metadata) || result.value.metadata.width !== 512
        || result.value.metadata.height !== 512 || result.value.metadata.channels !== 3
        || result.value.metadata.bits_per_channel !== 8
        || typeof result.value.rgb_base64 !== "string" || result.value.rgb_base64.length !== 1048576
        || !/^[A-Za-z0-9+/]+$/.test(result.value.rgb_base64)) invalid("Invalid RGB8 camera capture");
    } else if (call.method === "raycast") {
      shape(result.value, ["source_seed", "source_time_s", "frame", "origin_world_m", "direction_world", "max_distance_m", "hit"]);
      if (result.value.hit !== null && !record(result.value.hit)) invalid("Raycast hit must be a real hit report or null");
    } else if (call.method === "command" && call.command.op === "renew_control") {
      shape(result.value, ["ok", "ownership_renewal"]);
      if (result.value.ok !== true) invalid("Ownership renewal requires a native acknowledgement");
      const receipt = result.value.ownership_renewal;
      shape(receipt, ["source_time_s", "lease_sim_s", "sensor_revision", "control_authority"]);
      validateLease(receipt);
      validateUnsigned(receipt.sensor_revision, "sensor_revision");
      const authority = receipt.control_authority;
      shape(authority, ["epoch", "term", "run_id", "controller_id", "status", "expires_at_sim_time_s"]);
      validateUnsigned(authority.epoch, "epoch");
      validateUnsigned(authority.term, "term");
      validateId(authority.run_id, "run_id");
      validateId(authority.controller_id, "controller_id");
      if (receipt.source_time_s !== call.command.source_time_s || receipt.lease_sim_s !== call.command.lease_sim_s
        || receipt.sensor_revision !== call.command.expected_sensor_revision || authority.status !== "active"
        || ["epoch", "term", "run_id", "controller_id"].some(name => authority[name] !== call.command.controller_fence[name])
        || authority.expires_at_sim_time_s !== receipt.source_time_s + receipt.lease_sim_s) {
        invalid("Ownership renewal receipt must match the original source, sensor revision and active fence");
      }
    } else if (call.method !== "snapshot") {
      shape(result.value, ["ok"]);
      if (result.value.ok !== true) invalid("Commands return an acknowledgement only");
    } else {
      shape(result.value, ["observation", "telemetry", "navigation", "runtime", "renderer", "engine_build", "cameras"], diagnostics ? ["diagnostics"] : []);
      if (result.value.diagnostics !== undefined && !record(result.value.diagnostics)) invalid("Diagnostics must be a JSON object");
      if (![result.value.observation, result.value.telemetry, result.value.navigation, result.value.runtime, result.value.renderer].every(record)) {
        invalid("Snapshot reports must be JSON objects");
      }
      shape(result.value.engine_build, ["bytes", "sha256"]);
      if (result.value.engine_build.bytes !== engineBuild.bytes || result.value.engine_build.sha256 !== engineBuild.sha256) {
        invalid("Snapshot engine build does not match this lab server");
      }
    }
    finiteJson(result.value);
  }

  function send(request, response, status, value, type = "application/json; charset=utf-8") {
    if (response.destroyed || response.writableEnded) return;
    let body = type.startsWith("application/json") ? JSON.stringify(value) : value;
    if (Buffer.byteLength(body) > RESPONSE_LIMIT) {
      status = 502;
      type = "application/json; charset=utf-8";
      body = JSON.stringify({ ok: false, error: "Local bridge response exceeds 2 MiB" });
    }
    response.writeHead(status, {
      "Content-Type": type,
      "Content-Length": Buffer.byteLength(body),
      "Cache-Control": "no-store",
      "Cross-Origin-Resource-Policy": "same-origin",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
      "X-Frame-Options": "DENY",
      ...(request.wsorbTraceId ? { "X-WSORB-Request-ID": request.wsorbTraceId } : {}),
      ...(!request.complete ? { Connection: "close" } : {}),
    });
    response.end(request.method === "HEAD" ? undefined : body);
  }

  function settle(job, status, envelope) {
    if (pending.get(job.id) !== job) return;
    pending.delete(job.id);
    clearTimeout(job.timer);
    if (job.previousSocketTimeout !== undefined && !job.request.socket.destroyed) {
      job.request.socket.setTimeout(job.previousSocketTimeout);
    }
    if (worldSetup(job.call) && job.delivered && page?.id === job.page) {
      // A completed or cancelled build no longer holds an extended lease.
      page.expires = Math.min(page.expires, Date.now() + PAGE_LEASE_MS);
      clearTimeout(pageTimer);
      pageTimer = setTimeout(expire, Math.max(1, page.expires - Date.now()));
    }
    job.response.off("close", job.disconnected);
    emitDiagnostic(job.diagnostics, "local_relay", status === 504 ? "call_timeout" : "call_finished", {
      trace_id: job.trace_id, job_id: job.id, page_id: job.page, method: job.call.method, op: job.call.command?.op,
      channel: job.channel, delivered: job.delivered, ok: envelope?.ok === true, http_status: status,
      elapsed_ms: job.started_at === undefined ? undefined : Date.now() - job.started_at,
      code: envelope?.code, reason: status === 504 ? "call_deadline_expired" : !envelope ? "client_disconnected" : undefined,
    });
    if (envelope && !job.response.destroyed && !job.response.writableEnded) {
      job.response.setHeader("X-WSORB-Page", job.page);
      send(job.request, job.response, status, envelope);
    }
  }

  function lost(job, reason) {
    return { ok: false, error: job.delivered
      ? `${reason}. Browser execution may have occurred; outcome is indeterminate. Inspect live state before further flight control.`
      : `${reason}. The job was not delivered to the browser.` };
  }

  function expire() {
    if (page && page.expires <= Date.now()) {
      emitDiagnostic(process.env.WSORB_DIAGNOSTICS === "1" || [...pending.values()].some(job => job.diagnostics), "local_relay", "page_expired", { page_id: page.id });
      page = null;
      clearTimeout(pageTimer);
      for (const job of [...pending.values()]) settle(job, 503, lost(job, "Browser page lease expired"));
    }
    for (const job of [...pending.values()]) {
      if (job.deadline <= Date.now()) settle(job, 504, lost(job, "Local call deadline expired"));
    }
  }

  function claimPage(id, diagnostics = false, traceId, visible) {
    expire();
    if (page && page.id !== id) {
      emitDiagnostic(diagnostics, "local_relay", "page_conflict", { trace_id: traceId, owner_page_id: page.id, requester_page_id: id, reason: "owner_conflict", http_status: 409 });
      throw new HttpError(409, "Another browser page holds the local simulation lease");
    }
    if (!page) emitDiagnostic(diagnostics, "local_relay", "page_claimed", { trace_id: traceId, page_id: id });
    const expires = Math.max(Date.now() + PAGE_LEASE_MS,
      ...[...pending.values()]
        .filter(job => job.page === id && job.delivered && worldSetup(job.call))
        .map(job => job.deadline));
    page = { id, expires, visible };
    clearTimeout(pageTimer);
    pageTimer = setTimeout(expire, Math.max(1, expires - Date.now()));
  }

  function authenticated(request) {
    const supplied = Buffer.from(request.headers.authorization ?? "");
    return supplied.length === authorization.length && timingSafeEqual(supplied, authorization);
  }

  function pageId(request) {
    const id = request.headers["x-wsorb-page"];
    if (typeof id !== "string" || !UUID.test(id)) invalid("X-WSORB-Page must be a UUID");
    return id.toLowerCase();
  }

  function targetPageId(request) {
    const id = request.headers["x-wsorb-target-page"];
    if (id === undefined) return;
    if (typeof id !== "string" || !UUID.test(id) || id !== id.toLowerCase()) {
      invalid("X-WSORB-Target-Page must be a canonical UUID");
    }
    return id;
  }

  function pageVisibility(request) {
    const visible = request.headers["x-wsorb-page-visible"];
    if (visible === undefined) return;
    if (visible === "1") return true;
    if (visible === "0") return false;
    invalid("X-WSORB-Page-Visible must be 1 or 0");
  }

  function diagnosticCall(channel) {
    const job = [...pending.values()].find(job => job.channel === channel);
    if (job) return {
      job_id: job.id, trace_id: job.trace_id ?? null, method: job.call.method,
      op: job.call.command?.op ?? null, delivered: job.delivered,
      deadline_remaining_ms: Math.max(0, job.deadline - Date.now()),
    };
    const call = nativePending.get(channel);
    return call ? { method: call.method, op: call.command?.op ?? null, delivered: true } : null;
  }

  async function handle(request, response, pathname) {
    let diagnostics = process.env.WSORB_DIAGNOSTICS === "1";
    let traceId;
    try {
      if (!origin || closed) throw new HttpError(503, "Local lab is not running");
      if (request.headers.host !== new URL(origin).host) throw new HttpError(403, "Local API requires its exact loopback Host");
      if (request.headers.origin !== undefined && request.headers.origin !== origin) throw new HttpError(403, "Foreign Origin is not allowed");
      const site = request.headers["sec-fetch-site"];
      if (site !== undefined && site !== "same-origin" && site !== "none") throw new HttpError(403, "Cross-origin requests are not allowed");
      if (new URL(request.url, origin).search) invalid("Local API routes do not accept query parameters");
      const moduleRoute = pathname === "/__local/bootstrap.js" || pathname === "/__local/client.js";
      const notebookRoute = pathname === "/__local/notebook";
      const controllersRoute = pathname === "/__local/controllers";
      const allowedMethod = moduleRoute || notebookRoute || pathname === "/__local/status" || pathname === "/__local/poll" ? "GET" : "POST";
      if (!["/__local/bootstrap.js", "/__local/client.js", "/__local/notebook", "/__local/status", "/__local/call", "/__local/poll", "/__local/result", "/__local/controllers"].includes(pathname)) {
        throw new HttpError(404, "Unknown local API route");
      }
      if (controllersRoute ? !["GET", "POST"].includes(request.method) : request.method !== allowedMethod) {
        response.setHeader("Allow", controllersRoute ? "GET, POST" : allowedMethod);
        throw new HttpError(405, "Method not allowed");
      }
      const authorized = authenticated(request);
      if (notebookRoute) {
        // A separately selected backend must never open an editor whose default
        // descriptor points at another game. Keep the current editor/flight intact.
        if (alternateConnection) {
          send(request, response, 200, notebookHelp, "text/html; charset=utf-8");
          return;
        }
        const activatedNavigation = site === "none" && request.headers["sec-fetch-mode"] === "navigate"
          && request.headers["sec-fetch-dest"] === "document" && request.headers["sec-fetch-user"] === "?1";
        if (site !== "same-origin" && !activatedNavigation && !authorized) {
          send(request, response, 200, notebookHelp, "text/html; charset=utf-8");
          return;
        }
        const notebook = await resolveNotebook({ root: resolve(worker, "..") });
        if (!notebook) {
          send(request, response, 200, notebookHelp, "text/html; charset=utf-8");
          return;
        }
        response.writeHead(302, {
          Location: notebook.url,
          "Cache-Control": "no-store",
          "Referrer-Policy": "no-referrer",
          "Cross-Origin-Resource-Policy": "same-origin",
          "X-Content-Type-Options": "nosniff",
          "X-Frame-Options": "DENY",
        });
        response.end();
        return;
      }
      if (moduleRoute) {
        // Browsers supply Fetch Metadata on module imports. Native clients must
        // authenticate instead; an unadorned GET must not reveal the bootstrap.
        if (site !== "same-origin" && !authorized) throw new HttpError(403, "Local modules require a same-origin browser request");
        const source = pathname === "/__local/client.js" ? clientSource
          : native ? "export {};\n" : `import { connect } from "/__local/client.js";\nconnect(${JSON.stringify(token)});\n`;
        if (assetDiagnostics) response.once("finish", () => emitDiagnostic(true, "local_relay", "asset_finished", {
          asset: pathname === "/__local/client.js" ? "local_client" : "local_bootstrap",
          http_method: request.method, http_status: response.statusCode, bytes: Buffer.byteLength(source),
        }));
        send(request, response, 200, source, "text/javascript; charset=utf-8");
        return;
      }
      if (!authorized) {
        response.setHeader("WWW-Authenticate", "Bearer");
        throw new HttpError(401, "A local lab Bearer token is required");
      }
      diagnostics ||= request.headers["x-wsorb-diagnostics"] === "1";
      if (diagnostics) {
        traceId = validTraceId(request.headers["x-wsorb-request-id"]) ? request.headers["x-wsorb-request-id"] : randomUUID();
        request.wsorbTraceId = traceId;
      }
      expire();
      const visible = pathname === "/__local/poll" || pathname === "/__local/result"
        ? pageVisibility(request) : undefined;
      if (controllersRoute) {
        if (request.method === "GET") {
          send(request, response, 200, controllerJobsView(controllerJobs, Date.now()));
        } else {
          const action = await readJson(request, response, CONTROLLER_BODY_LIMIT);
          expire();
          // Owner identity comes only from the actual bridge lease. A native
          // headless instance deliberately has no browser identity to invent.
          const transition = controllerJobsAction(controllerJobs, action, {
            now: Date.now(), jobId: randomUUID(), ownerPageId: page?.id ?? null,
            ownerAvailable: native !== null || page !== null,
          });
          controllerJobs = transition.state;
          send(request, response, 200, transition.response);
        }
      } else if (pathname === "/__local/status") {
        const active = [...pending.values()].map(job => job.call).concat([...nativePending.values()]);
        send(request, response, 200, { enabled: true, connected: page !== null, busy: active.length !== 0,
          page_id: page?.id ?? null, page_binding_supported: !native, page_visible: page?.visible,
          busy_operation: active.some(worldSetup) ? "world_setup" : null,
          engine_build: engineBuild,
          ...(diagnostics ? { diagnostics: {
            enabled: true, page_id: page?.id ?? null,
            page_lease_remaining_ms: page ? Math.max(0, page.expires - Date.now()) : null,
            active_control_call: diagnosticCall("control"), active_capture_call: diagnosticCall("capture"),
            active_calls: [...pending.values()].map(job => ({
              job_id: job.id, trace_id: job.trace_id ?? null, method: job.call.method,
              op: job.call.command?.op ?? null, channel: job.channel, delivered: job.delivered,
              deadline_remaining_ms: Math.max(0, job.deadline - Date.now()),
            })),
          } } : {}),
          ...(native ? { backend: "native_headless", ...native.status() } : {}) });
      } else if (pathname === "/__local/poll") {
        if (native) throw new HttpError(409, "The native headless backend does not lease a browser page");
        const id = pageId(request);
        if (request.headers["x-wsorb-build"] !== engineBuild.sha256) {
          throw new HttpError(409, "Browser engine build differs from the lab server; restart the local lab and reload the simulation");
        }
        claimPage(id, diagnostics, traceId, visible);
        const job = [...pending.values()].find(job => job.page === id && !job.delivered);
        if (job) {
          job.delivered = true;
          emitDiagnostic(job.diagnostics, "local_relay", "call_dispatched", { trace_id: job.trace_id, job_id: job.id, page_id: id, method: job.call.method, op: job.call.command?.op, deadline_ms: job.deadline });
          if (worldSetup(job.call)) {
            // A real delivered, bounded world-build job may block the WASM
            // thread, preventing polls. This is a compute lease, not a heartbeat.
            page.expires = job.deadline;
            clearTimeout(pageTimer);
            pageTimer = setTimeout(expire, Math.max(1, job.deadline - Date.now()));
          }
        }
        send(request, response, 200, { job: job ? {
          id: job.id, ...job.call, deadline_ms: job.deadline,
          ...(job.diagnostics ? { diagnostics: true, trace_id: job.trace_id } : {}),
        } : null });
      } else if (pathname === "/__local/call") {
        const targetPage = targetPageId(request);
        const call = await readJson(request, response);
        validateCall(call);
        emitDiagnostic(diagnostics, "local_relay", "call_received", {
          trace_id: traceId, method: call.method, op: call.command?.op,
          source_time_s: call.command?.source_time_s, lease_sim_s: call.command?.lease_sim_s,
          epoch: call.command?.controller_fence?.epoch, term: call.command?.controller_fence?.term,
        });
        expire();
        if (targetPage !== undefined && (native || !page || page.id !== targetPage)) {
          throw new HttpError(409, "The target browser page is not connected to this local simulation", "page_mismatch");
        }
        const channel = call.method === "capture" ? "capture" : "control";
        if (native) {
          if (nativePending.has(channel)) throw new HttpError(409, `A local ${channel} call is already in flight`, "local_busy");
          if (response.destroyed || closed) return;
          const timeout = channel === "capture" ? CAPTURE_TIMEOUT_MS
            : worldSetup(call) ? WORLD_CALL_TIMEOUT_MS : CALL_TIMEOUT_MS;
          const previousSocketTimeout = request.socket.timeout ?? 30000;
          if (worldSetup(call)) request.socket.setTimeout(timeout + 5000);
          nativePending.set(channel, call);
          try {
            const result = await native.invoke(call, Date.now() + timeout, diagnostics);
            validateResult(result, call, false, diagnostics);
            emitDiagnostic(diagnostics, "local_relay", "call_finished", { trace_id: traceId, method: call.method, op: call.command?.op, ok: result.ok === true, code: result.code, http_status: 200 });
            if (result.ok === false) result.error = result.error.replaceAll(token, "[redacted]");
            send(request, response, 200, result);
          } finally {
            nativePending.delete(channel);
            if (worldSetup(call) && !request.socket.destroyed) request.socket.setTimeout(previousSocketTimeout);
          }
          return;
        }
        if (!page) throw new HttpError(503, "No ready browser is connected to the local simulation");
        if ([...pending.values()].some(job => job.channel === channel)) {
          throw new HttpError(409, `A local ${channel} call is already in flight`, "local_busy");
        }
        if (response.destroyed || closed) return;
        const timeout = channel === "capture" ? CAPTURE_TIMEOUT_MS
          : worldSetup(call) ? WORLD_CALL_TIMEOUT_MS : CALL_TIMEOUT_MS;
        const job = { id: randomUUID(), call, channel, page: page.id, request, response, deadline: Date.now() + timeout, delivered: false,
          diagnostics, trace_id: traceId, started_at: diagnostics ? Date.now() : undefined };
        if (worldSetup(call)) {
          job.previousSocketTimeout = request.socket.timeout ?? 30000;
          request.socket.setTimeout(timeout + 5000);
        }
        job.disconnected = () => settle(job);
        job.timer = setTimeout(() => settle(job, 504, lost(job, "Local call deadline expired")), timeout);
        pending.set(job.id, job);
        response.once("close", job.disconnected);
      } else {
        if (native) throw new HttpError(409, "The native headless backend does not accept browser results");
        const id = pageId(request);
        if (!page || page.id !== id || ![...pending.values()].some(job => job.page === id && job.delivered)) {
          throw new HttpError(409, "No delivered call belongs to this browser page");
        }
        const result = await readJson(request, response, RESPONSE_LIMIT);
        expire();
        const job = pending.get(result?.id);
        if (!page || page.id !== id || !job || job.page !== id || !job.delivered || result?.id !== job.id || job.deadline <= Date.now()) {
          throw new HttpError(409, "Stale or expired browser result");
        }
        validateResult(result, job.call, true, job.diagnostics);
        claimPage(id, diagnostics || job.diagnostics, job.trace_id, visible);
        const envelope = result.ok ? { ok: true, value: result.value }
          : { ok: false, error: result.error, ...(result.code ? { code: result.code } : {}) };
        settle(job, 200, envelope);
        send(request, response, 200, { ok: true });
      }
    } catch (error) {
      const httpError = error instanceof HttpError || error instanceof ControllerJobsError;
      emitDiagnostic(diagnostics, "local_relay", "call_rejected", {
        trace_id: traceId, http_status: httpError ? error.status : 500,
        code: error instanceof HttpError ? error.code : undefined,
        reason: error instanceof HttpError && error.code === "local_busy" ? "in_flight" : "invalid_request",
      });
      send(request, response, httpError ? error.status : 500, {
        ok: false, error: httpError ? error.message : "Local bridge failed",
        ...(error instanceof HttpError && error.code ? { code: error.code } : {}),
      });
    }
  }

  return {
    handle,
    acceptsHost: request => origin && request.headers.host === new URL(origin).host,
    indexScript: native ? "" : '<script type="module" src="/__local/bootstrap.js"></script>',
    indexPage: native ? '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Headless simulation</title><body><main><h1>Headless simulation</h1><p>The native physics runtime is running without a browser renderer. This page is not a live telemetry view.</p><p>Use the authenticated local API or <a href="/notebook.html">open the notebook</a> to inspect the actual flight.</p></main></body></html>' : null,
    async listen(port) {
      const url = `http://127.0.0.1:${port}`;
      origin = new URL(url).origin;
      publication = withDescriptorLock(async () => {
        const temporary = `${descriptorPath}.${randomUUID()}.tmp`;
        let file;
        let created = false;
        try {
          file = await open(temporary, "wx", 0o600);
          created = true;
          await file.chmod(0o600);
          await file.writeFile(`${JSON.stringify({ version: 1, url, token })}\n`);
          await file.sync();
          await file.close();
          file = null;
          await rename(temporary, descriptorPath);
        } finally {
          try { await file?.close(); }
          finally {
            if (created) await unlink(temporary).catch(error => { if (error.code !== "ENOENT") throw error; });
          }
        }
      });
      await publication;
    },
    async close() {
      closed = true;
      native?.close();
      clearTimeout(pageTimer);
      page = null;
      for (const job of [...pending.values()]) settle(job, 503, lost(job, "Local lab is shutting down"));
      await publication.catch(() => {});
      await removeDescriptor();
    },
  };
}
