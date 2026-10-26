// The private Node adapter for the same built WASM/glue used by the browser.
// No DOM, renderer, sensor samples or playback state are supplied by this adapter.
import { createHash } from "node:crypto";
import { lstat, readFile, realpath } from "node:fs/promises";
import { resolve, sep } from "node:path";
import { performance } from "node:perf_hooks";
import { pathToFileURL } from "node:url";

const TICK_MS = 20;
const ERROR_CODES = new Set(["stale_context", "command_expired", "sensor_unavailable", "sensor_no_sample"]);
const UNAPPLIED_COMMAND_CODES = new Set(["stale_context", "command_expired"]);
const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
const message = error => typeof error?.message === "string" && error.message ? error.message
  : typeof error?.error === "string" && error.error ? error.error
    : typeof error === "string" && error ? error : "Unrecognized native headless engine error";

export async function createHeadless({ publicRoot, rootRealPath, engineBuild }) {
  async function engineFile(name, expectedSize) {
    const path = resolve(publicRoot, "sim", name);
    const info = await lstat(path);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error("Headless engine assets must be regular files, not symlinks");
    if (expectedSize !== undefined && info.size !== expectedSize) throw new Error("Headless engine length does not match the actual build manifest");
    const actual = await realpath(path);
    if (!actual.startsWith(`${rootRealPath}${sep}`)) throw new Error("Headless engine asset is outside public assets");
    return actual;
  }

  const gluePath = await engineFile("wsorb_bevy.js");
  const wasm = await readFile(await engineFile("wsorb_bevy_bg.wasm", engineBuild.bytes));
  if (wasm.length !== engineBuild.bytes || createHash("sha256").update(wasm).digest("hex") !== engineBuild.sha256) {
    throw new Error("Headless engine bytes do not match the actual build manifest; rebuild before starting the lab");
  }
  const moduleUrl = pathToFileURL(gluePath);
  moduleUrl.searchParams.set("v", engineBuild.sha256);
  const api = await import(moduleUrl.href);
  const methods = [
    "default", "headless_start", "headless_tick", "headless_status", "observation", "runtime_status",
    "telemetry_status", "navigation_status", "ship_camera_status", "renderer_status", "control",
    "set_navigation_preview", "pan_tilt_camera", "ship_camera_capture", "lookahead_raycast",
  ];
  if (methods.some(name => typeof api[name] !== "function")) {
    throw new Error("The built engine lacks the native headless API. Rebuild before starting the headless lab");
  }
  await api.default({ module_or_path: wasm });

  function checkNativeResult(result) {
    if (typeof result === "string") {
      const parsed = JSON.parse(result);
      if (parsed?.ok === false) throw new Error(message(parsed));
    }
  }

  function physicsStatus() {
    const value = JSON.parse(api.headless_status());
    if (!record(value) || typeof value.started !== "boolean"
      || !["unstarted", "starting", "idle", "updating", "failed"].includes(value.phase)) {
      throw new Error("The native headless lifecycle report is invalid");
    }
    return value;
  }

  const ready = status => status.started && status.phase === "idle" && status.error == null;
  checkNativeResult(api.headless_start());
  // Run the actual initial Runtime/Reports schedule without advancing its clock.
  checkNativeResult(api.headless_tick(0));
  let lastPhysics = physicsStatus();
  if (!ready(lastPhysics)) throw new Error("The actual native headless physics runtime is not ready");
  let previous = performance.now();
  let timer;
  let closed = false;
  let tickError = null;

  function fail(error) {
    tickError ??= new Error(message(error));
    clearTimeout(timer);
    return tickError;
  }

  function tick() {
    if (closed) throw new Error("The native headless runtime is closed");
    if (tickError) throw tickError;
    const now = performance.now();
    const delta = Math.min(1, (now - previous) / 1000);
    previous = now;
    try {
      // One bounded real elapsed sample, never catch-up ticks or a sim-time step.
      // Native Runtime separately bounds physical work after a stalled callback.
      checkNativeResult(api.headless_tick(delta));
      lastPhysics = physicsStatus();
      if (!ready(lastPhysics)) throw new Error(lastPhysics.error || "The native headless scheduler is no longer idle");
    } catch (error) {
      throw fail(error);
    }
  }

  function schedule() {
    timer = setTimeout(() => {
      if (closed) return;
      try { tick(); }
      catch { return; } // Fail closed; status exposes the failure, calls cannot write.
      schedule();
    }, TICK_MS);
    timer.unref();
  }
  schedule();

  return {
    status() {
      if (!closed && !tickError) {
        try { tick(); }
        catch { /* Never re-enter a trapped engine to assemble diagnostics. */ }
      }
      return {
        physics: lastPhysics,
        physics_ready: !closed && !tickError && ready(lastPhysics),
        physics_status_cached: tickError !== null,
        headless_error: tickError ? message(tickError) : null,
      };
    },
    async invoke(call, deadline) {
      let commandInvoked = false;
      const failure = (error, code) => ({
        ok: false, error: message(error).slice(0, 2048), ...(ERROR_CODES.has(code) ? { code } : {}),
      });
      try {
        if (Date.now() >= deadline) return failure("Local call expired before native engine invocation; no command was invoked");
        tick();
        let value;
        if (call.method === "snapshot") {
          value = {
            observation: JSON.parse(api.observation()),
            telemetry: JSON.parse(api.telemetry_status()),
            navigation: JSON.parse(api.navigation_status()),
            runtime: JSON.parse(api.runtime_status()),
            renderer: JSON.parse(api.renderer_status()),
            engine_build: engineBuild,
            cameras: JSON.parse(api.ship_camera_status()),
          };
        } else if (call.method === "command") {
          if (Date.now() >= deadline) return failure("Local call expired before native engine invocation; no command was invoked");
          commandInvoked = true;
          const result = JSON.parse(api.control(JSON.stringify(call.command)));
          if (result.ok === false) return failure(result, result.code);
          if (result.ok !== true) throw new Error("Native engine did not return a valid command acknowledgement");
          // This is the native ACK only. Queued playback is read on a later tick.
          value = { ok: true };
        } else if (call.method === "preview") {
          const result = JSON.parse(api.set_navigation_preview(JSON.stringify(call.preview)));
          if (result.ok !== true) return failure(result, result.code);
          value = { ok: true };
        } else if (call.method === "raycast") {
          const result = JSON.parse(api.lookahead_raycast(JSON.stringify(call.query)));
          if (result.ok !== true) return failure(result, result.code);
          value = result.report;
        } else if (call.method === "pan_tilt") {
          const result = JSON.parse(api.pan_tilt_camera(call.camera, call.yaw_deg, call.pitch_deg));
          if (result.ok !== true) return failure(result, result.code);
          value = { ok: true };
        } else if (call.method === "capture") {
          let captureTimer;
          try {
            // The actual uninitialized native cameras reject with sensor_no_sample.
            // Do not manufacture that rejection, metadata or replacement pixels.
            const frame = await Promise.race([
              api.ship_camera_capture(call.camera),
              new Promise((_, reject) => {
                captureTimer = setTimeout(() => reject(new Error("Camera capture deadline expired")), Math.max(0, deadline - Date.now()));
              }),
            ]);
            const { rgb, ...metadata } = frame;
            if (!(rgb instanceof Uint8Array) || rgb.length !== 512 * 512 * 3) {
              throw new Error("Native camera did not return a genuine 512×512 RGB8 frame");
            }
            value = { metadata, rgb_base64: Buffer.from(rgb).toString("base64") };
          } finally { clearTimeout(captureTimer); }
        } else return failure("Unsupported native headless call");
        return { ok: true, value };
      } catch (error) {
        if (error instanceof WebAssembly.RuntimeError) fail(error);
        if (UNAPPLIED_COMMAND_CODES.has(error?.code)) commandInvoked = false;
        return failure(commandInvoked
          ? `Native engine command failed: ${message(error).slice(0, 1600)}. Outcome is indeterminate. Inspect live state before further flight control.`
          : error, error?.code);
      }
    },
    close() {
      closed = true;
      clearTimeout(timer);
    },
  };
}
