import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, readFile, realpath, rename, unlink } from "node:fs/promises";
import { basename, dirname, relative, resolve, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { resolveNotebook } from "./notebook-launch.mjs";

const BODY_LIMIT = 1024 * 1024;
const RESPONSE_LIMIT = 2 * BODY_LIMIT;
const BODY_TIMEOUT_MS = 3000;
const CALL_TIMEOUT_MS = 5000;
const WORLD_CALL_TIMEOUT_MS = 180000;
const CAPTURE_TIMEOUT_MS = 20000;
const PAGE_LEASE_MS = 6000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
const CAMERA_MODES = new Set([
  "orbit", "mission", "first_person", "third_person", "follow", "free", "overview", "top", "ship_top", "ship_bottom",
]);
const WORLD_COMMANDS = new Set(["configure_experiment", "configure_flight", "reset"]);
const worldSetup = call => call.method === "command" && WORLD_COMMANDS.has(call.command?.op);

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
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

function validateContext(context) {
  if (context == null) return;
  shape(context, ["trial_revision", "tuning_revision", "manual_interventions", "seed", "running"]);
  for (const field of ["trial_revision", "tuning_revision", "manual_interventions", "seed"]) {
    if (!Number.isInteger(context[field]) || !finiteBetween(context[field], 0, 4294967295)) {
      invalid("Command context revisions and seed must be unsigned 32-bit integers");
    }
  }
  if (typeof context.running !== "boolean") invalid("Command context running must be a boolean");
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
      if (command.op === "configure_flight" && !["mars_facing_l1", "far_side_l2"].includes(command.preset)) invalid("Choose a Mars-facing L1 or far-side L2 launch setup");
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
      invalid("Allowed command ops: pause, play, step, reset, set_command, stop, view, debug_upright, configure_experiment, configure_flight, place_lander, move_launchpad, tune_ship, tune_debris_flow");
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

export async function createLocalApi({ worker, publicRoot, rootRealPath }) {
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
  const notebookHelp = await readFile(resolve(worker, "web/notebook.html"), "utf8");
  let origin;
  let closed = false;
  let publication = Promise.resolve();
  let page = null;
  let pageTimer;
  // One control request and one GPU readback may coexist. A capture must not
  // hold the command channel while Python is controlling a flying vehicle.
  const pending = new Map();

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
    if (envelope) send(job.request, job.response, status, envelope);
  }

  function lost(job, reason) {
    return { ok: false, error: job.delivered
      ? `${reason}. Browser execution may have occurred; outcome is indeterminate. Inspect live state before further flight control.`
      : `${reason}. The job was not delivered to the browser.` };
  }

  function expire() {
    if (page && page.expires <= Date.now()) {
      page = null;
      clearTimeout(pageTimer);
      for (const job of [...pending.values()]) settle(job, 503, lost(job, "Browser page lease expired"));
    }
    for (const job of [...pending.values()]) {
      if (job.deadline <= Date.now()) settle(job, 504, lost(job, "Local call deadline expired"));
    }
  }

  function claimPage(id) {
    expire();
    if (page && page.id !== id) throw new HttpError(409, "Another browser page holds the local simulation lease");
    const expires = Math.max(Date.now() + PAGE_LEASE_MS,
      ...[...pending.values()]
        .filter(job => job.page === id && job.delivered && worldSetup(job.call))
        .map(job => job.deadline));
    page = { id, expires };
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

  async function handle(request, response, pathname) {
    try {
      if (!origin || closed) throw new HttpError(503, "Local lab is not running");
      if (request.headers.host !== new URL(origin).host) throw new HttpError(403, "Local API requires its exact loopback Host");
      if (request.headers.origin !== undefined && request.headers.origin !== origin) throw new HttpError(403, "Foreign Origin is not allowed");
      const site = request.headers["sec-fetch-site"];
      if (site !== undefined && site !== "same-origin" && site !== "none") throw new HttpError(403, "Cross-origin requests are not allowed");
      if (new URL(request.url, origin).search) invalid("Local API routes do not accept query parameters");
      const moduleRoute = pathname === "/__local/bootstrap.js" || pathname === "/__local/client.js";
      const notebookRoute = pathname === "/__local/notebook";
      const allowedMethod = moduleRoute || notebookRoute || pathname === "/__local/status" || pathname === "/__local/poll" ? "GET" : "POST";
      if (!["/__local/bootstrap.js", "/__local/client.js", "/__local/notebook", "/__local/status", "/__local/call", "/__local/poll", "/__local/result"].includes(pathname)) {
        throw new HttpError(404, "Unknown local API route");
      }
      if (request.method !== allowedMethod) {
        response.setHeader("Allow", allowedMethod);
        throw new HttpError(405, "Method not allowed");
      }
      const authorized = authenticated(request);
      if (notebookRoute) {
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
          : `import { connect } from "/__local/client.js";\nconnect(${JSON.stringify(token)});\n`;
        send(request, response, 200, source, "text/javascript; charset=utf-8");
        return;
      }
      if (!authorized) {
        response.setHeader("WWW-Authenticate", "Bearer");
        throw new HttpError(401, "A local lab Bearer token is required");
      }
      expire();
      if (pathname === "/__local/status") {
        send(request, response, 200, { enabled: true, connected: page !== null, busy: pending.size !== 0,
          busy_operation: [...pending.values()].some(job => worldSetup(job.call)) ? "world_setup" : null,
          engine_build: engineBuild });
      } else if (pathname === "/__local/poll") {
        const id = pageId(request);
        if (request.headers["x-wsorb-build"] !== engineBuild.sha256) {
          throw new HttpError(409, "Browser engine build differs from the lab server; restart the local lab and reload the simulation");
        }
        claimPage(id);
        const job = [...pending.values()].find(job => !job.delivered);
        if (job) {
          job.delivered = true;
          if (worldSetup(job.call)) {
            // A real delivered, bounded world-build job may block the WASM
            // thread, preventing polls. This is a compute lease, not a heartbeat.
            page.expires = job.deadline;
            clearTimeout(pageTimer);
            pageTimer = setTimeout(expire, Math.max(1, job.deadline - Date.now()));
          }
        }
        send(request, response, 200, { job: job ? { id: job.id, ...job.call, deadline_ms: job.deadline } : null });
      } else if (pathname === "/__local/call") {
        const call = await readJson(request, response);
        validateCall(call);
        expire();
        if (!page) throw new HttpError(503, "No ready browser is connected to the local simulation");
        const channel = call.method === "capture" ? "capture" : "control";
        if ([...pending.values()].some(job => job.channel === channel)) {
          throw new HttpError(409, `A local ${channel} call is already in flight`);
        }
        if (response.destroyed || closed) return;
        const timeout = channel === "capture" ? CAPTURE_TIMEOUT_MS
          : worldSetup(call) ? WORLD_CALL_TIMEOUT_MS : CALL_TIMEOUT_MS;
        const job = { id: randomUUID(), call, channel, page: page.id, request, response, deadline: Date.now() + timeout, delivered: false };
        if (worldSetup(call)) {
          job.previousSocketTimeout = request.socket.timeout ?? 30000;
          request.socket.setTimeout(timeout + 5000);
        }
        job.disconnected = () => settle(job);
        job.timer = setTimeout(() => settle(job, 504, lost(job, "Local call deadline expired")), timeout);
        pending.set(job.id, job);
        response.once("close", job.disconnected);
      } else {
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
        shape(result, result.ok === true ? ["id", "ok", "value"] : ["id", "ok", "error"], result.ok === true ? [] : ["code"]);
        if (result.ok !== true && result.ok !== false) invalid("Result ok must be a boolean");
        if (result.ok === false && (typeof result.error !== "string" || !result.error || result.error.length > 2048)) {
          invalid("Result error must be a nonempty string of at most 2048 characters");
        }
        if (result.ok === false && result.code !== undefined && result.code !== "stale_context") invalid("Unknown command rejection code");
        if (result.ok === true) {
          if (job.call.method === "capture") {
            shape(result.value, ["metadata", "rgb_base64"]);
            if (!record(result.value.metadata) || result.value.metadata.width !== 512
              || result.value.metadata.height !== 512 || result.value.metadata.channels !== 3
              || result.value.metadata.bits_per_channel !== 8
              || typeof result.value.rgb_base64 !== "string" || result.value.rgb_base64.length !== 1048576
              || !/^[A-Za-z0-9+/]+$/.test(result.value.rgb_base64)) invalid("Invalid RGB8 camera capture");
          } else if (job.call.method === "raycast") {
            shape(result.value, ["source_seed", "source_time_s", "frame", "origin_world_m", "direction_world", "max_distance_m", "hit"]);
            if (result.value.hit !== null && !record(result.value.hit)) invalid("Raycast hit must be a real hit report or null");
          } else if (job.call.method !== "snapshot") {
            shape(result.value, ["ok"]);
            if (result.value.ok !== true) invalid("Commands return an acknowledgement only");
          } else {
            shape(result.value, ["observation", "telemetry", "navigation", "runtime", "renderer", "engine_build", "cameras"]);
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
        claimPage(id);
        const envelope = result.ok ? { ok: true, value: result.value }
          : { ok: false, error: result.error, ...(result.code ? { code: result.code } : {}) };
        settle(job, 200, envelope);
        send(request, response, 200, { ok: true });
      }
    } catch (error) {
      send(request, response, error instanceof HttpError ? error.status : 500, {
        ok: false, error: error instanceof HttpError ? error.message : "Local bridge failed",
      });
    }
  }

  return {
    handle,
    acceptsHost: request => origin && request.headers.host === new URL(origin).host,
    indexScript: '<script type="module" src="/__local/bootstrap.js"></script>',
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
      clearTimeout(pageTimer);
      page = null;
      for (const job of [...pending.values()]) settle(job, 503, lost(job, "Local lab is shutting down"));
      await publication.catch(() => {});
      await removeDescriptor();
    },
  };
}
