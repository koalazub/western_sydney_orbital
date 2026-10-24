import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, readFile, realpath, rename, unlink } from "node:fs/promises";
import { basename, dirname, relative, resolve, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const BODY_LIMIT = 1024 * 1024;
const RESPONSE_LIMIT = 2 * BODY_LIMIT;
const BODY_TIMEOUT_MS = 3000;
const CALL_TIMEOUT_MS = 5000;
const PAGE_LEASE_MS = 6000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
const CAMERA_MODES = new Set([
  "orbit", "mission", "first_person", "third_person", "follow", "free", "overview", "top", "ship_top", "ship_bottom",
]);

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
  shape(value, ["method", "command"]);
  if (value.method !== "command" || !record(value.command)) invalid("Expected snapshot, preview or command");
  const command = value.command;
  switch (command.op) {
    case "pause":
    case "stop":
    case "debug_upright":
      shape(command, ["op"]);
      break;
    case "play":
      shape(command, ["op"], ["rate"]);
      if (command.rate != null && ![1, 5, 10].includes(command.rate)) invalid("rate must be 1, 5 or 10");
      break;
    case "reset":
      shape(command, ["op"], ["seed"]);
      if (command.seed != null && (!Number.isInteger(command.seed) || !finiteBetween(command.seed, 0, 4294967295))) {
        invalid("seed must be an integer from 0 to 4294967295");
      }
      break;
    case "step":
      shape(command, ["op", "seconds"]);
      if (!finiteBetween(command.seconds, 0, 30)) invalid("seconds must be finite and between 0 and 30");
      break;
    case "set_command":
      shape(command, ["op", "accel", "angular_accel"]);
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
        const debris = typeof focus === "string" && /^Debris[1-9][0-9]{0,4}$/.test(focus) && Number(focus.slice(6)) <= 65535;
        if (!["Shuttle", "Lander", "Target", "LandingPad", "Phobos", "Mars", "L1", "L2"].includes(focus) && !debris) {
          invalid("Unknown focus identifier");
        }
      }
      break;
    default:
      invalid("Allowed command ops: pause, play, step, reset, set_command, stop, view, debug_upright");
  }
}

function finiteJson(value, depth = 0) {
  if (depth > 32) invalid("Result exceeds the JSON nesting limit");
  if (typeof value === "number" && !Number.isFinite(value)) invalid("Result contains a non-finite number");
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) finiteJson(child, depth + 1);
  }
}

function readJson(request, response) {
  if (request.headers["content-type"]?.split(";")[0].trim().toLowerCase() !== "application/json") {
    throw new HttpError(415, "Content-Type must be application/json");
  }
  if (Number(request.headers["content-length"] ?? 0) > BODY_LIMIT) throw new HttpError(413, "Body exceeds 1 MiB");
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
      if (size > BODY_LIMIT) finish(new HttpError(413, "Body exceeds 1 MiB"));
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
  let origin;
  let closed = false;
  let publication = Promise.resolve();
  let page = null;
  let pageTimer;
  let pending = null;

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
    if (pending !== job) return;
    pending = null;
    clearTimeout(job.timer);
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
      if (pending) settle(pending, 503, lost(pending, "Browser page lease expired"));
    }
    if (pending && pending.deadline <= Date.now()) settle(pending, 504, lost(pending, "Local call deadline expired"));
  }

  function claimPage(id) {
    expire();
    if (page && page.id !== id) throw new HttpError(409, "Another browser page holds the local simulation lease");
    page = { id, expires: Date.now() + PAGE_LEASE_MS };
    clearTimeout(pageTimer);
    pageTimer = setTimeout(expire, PAGE_LEASE_MS);
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
      const allowedMethod = moduleRoute || pathname === "/__local/status" || pathname === "/__local/poll" ? "GET" : "POST";
      if (!["/__local/bootstrap.js", "/__local/client.js", "/__local/status", "/__local/call", "/__local/poll", "/__local/result"].includes(pathname)) {
        throw new HttpError(404, "Unknown local API route");
      }
      if (request.method !== allowedMethod) {
        response.setHeader("Allow", allowedMethod);
        throw new HttpError(405, "Method not allowed");
      }
      const authorized = authenticated(request);
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
        send(request, response, 200, { enabled: true, connected: page !== null, busy: pending !== null, engine_build: engineBuild });
      } else if (pathname === "/__local/poll") {
        const id = pageId(request);
        if (request.headers["x-wsorb-build"] !== engineBuild.sha256) {
          throw new HttpError(409, "Browser engine build differs from the lab server; restart the local lab and reload the simulation");
        }
        claimPage(id);
        const job = pending && !pending.delivered ? pending : null;
        if (job) job.delivered = true;
        send(request, response, 200, { job: job ? { id: job.id, ...job.call, deadline_ms: job.deadline } : null });
      } else if (pathname === "/__local/call") {
        const call = await readJson(request, response);
        validateCall(call);
        expire();
        if (!page) throw new HttpError(503, "No ready browser is connected to the local simulation");
        if (pending) throw new HttpError(409, "A local simulation call is already in flight");
        if (response.destroyed || closed) return;
        const job = { id: randomUUID(), call, page: page.id, request, response, deadline: Date.now() + CALL_TIMEOUT_MS, delivered: false };
        job.disconnected = () => settle(job);
        job.timer = setTimeout(() => settle(job, 504, lost(job, "Local call deadline expired")), CALL_TIMEOUT_MS);
        pending = job;
        response.once("close", job.disconnected);
      } else {
        const id = pageId(request);
        if (!page || page.id !== id || !pending || pending.page !== id || !pending.delivered) {
          throw new HttpError(409, "No delivered call belongs to this browser page");
        }
        const result = await readJson(request, response);
        expire();
        const job = pending;
        if (!page || page.id !== id || !job || job.page !== id || !job.delivered || result?.id !== job.id || job.deadline <= Date.now()) {
          throw new HttpError(409, "Stale or expired browser result");
        }
        shape(result, result.ok === true ? ["id", "ok", "value"] : ["id", "ok", "error"]);
        if (result.ok !== true && result.ok !== false) invalid("Result ok must be a boolean");
        if (result.ok === false && (typeof result.error !== "string" || !result.error || result.error.length > 2048)) {
          invalid("Result error must be a nonempty string of at most 2048 characters");
        }
        if (result.ok === true) {
          if (job.call.method !== "snapshot") {
            shape(result.value, ["ok"]);
            if (result.value.ok !== true) invalid("Commands return an acknowledgement only");
          } else {
            shape(result.value, ["observation", "telemetry", "navigation", "runtime", "engine_build"]);
            if (![result.value.observation, result.value.telemetry, result.value.navigation, result.value.runtime].every(record)) {
              invalid("Snapshot reports must be JSON objects");
            }
            shape(result.value.engine_build, ["bytes", "sha256"]);
            if (result.value.engine_build.bytes !== engineBuild.bytes || result.value.engine_build.sha256 !== engineBuild.sha256) {
              invalid("Snapshot engine build does not match this lab server");
            }
          }
          finiteJson(result.value);
        }
        const envelope = result.ok ? { ok: true, value: result.value } : { ok: false, error: result.error };
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
      if (pending) settle(pending, 503, lost(pending, "Local lab is shutting down"));
      await publication.catch(() => {});
      await removeDescriptor();
    },
  };
}
