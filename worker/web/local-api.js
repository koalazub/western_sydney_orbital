// Served directly by the opt-in local server. Never bundled into hosted assets.
const POLL_MS = 50;
const REQUEST_TIMEOUT_MS = 2500;
const BODY_LIMIT = 1024 * 1024;
const RESPONSE_LIMIT = 2 * BODY_LIMIT;
const COMMANDS = new Set(["pause", "play", "step", "reset", "set_command", "set_leased_command", "claim_control", "renew_control", "release_control", "set_sensor_fault", "clear_sensor_faults", "stop", "view", "debug_upright", "configure_experiment", "configure_flight", "place_lander", "move_launchpad", "tune_ship", "tune_debris_flow"]);
const ERROR_CODES = new Set(["stale_context", "command_expired", "sensor_unavailable", "sensor_no_sample"]);
// Only these native rejections establish that an invoked command was unapplied.
const UNAPPLIED_COMMAND_CODES = new Set(["stale_context", "command_expired"]);
let connected = false;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const diagnostic = (event, input) => {
  try { globalThis.__wsorbDiagnostics?.record(event, input); }
  catch { /* Optional diagnostics cannot affect flight or delivery. */ }
};
const diagnosticsEnabled = forced => {
  try { return forced === true || globalThis.__wsorbDiagnostics?.enabled === true; }
  catch { return forced === true; }
};

// Keep the actual transport policy inspectable without a substitute browser or
// engine. The private fetch call below uses these exact Request options.
export function bridgeRequestOptions(token, page, path, body, build, signal, trace) {
  return {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "X-WSORB-Page": page,
      ...(typeof document === "undefined" ? {} : { "X-WSORB-Page-Visible": document.hidden ? "0" : "1" }),
      ...(path === "poll" ? { "X-WSORB-Build": build.sha256 } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(diagnosticsEnabled(trace?.diagnostics) ? { "X-WSORB-Diagnostics": "1" } : {}),
      ...(trace?.diagnostics === true && typeof trace.trace_id === "string" && UUID.test(trace.trace_id)
        ? { "X-WSORB-Request-ID": trace.trace_id } : {}),
    },
    body,
    cache: "no-store",
    mode: "same-origin",
    // no-referrer makes same-origin POSTs send Origin: null in browsers.
    // Keep the API's strict Origin checks rather than weakening the server.
    referrerPolicy: "same-origin",
    credentials: "omit",
    redirect: "error",
    signal,
  };
}

// The production snapshot path, also usable with the real native headless
// adapter. URL logging opt-in never changes the legacy seven-field payload.
export function bridgeSnapshot(api, manifest, page, diagnostics = false, helper = globalThis.__wsorbDiagnostics) {
  const value = {
    observation: JSON.parse(api.observation()),
    telemetry: JSON.parse(api.telemetry_status()),
    navigation: JSON.parse(api.navigation_status()),
    runtime: JSON.parse(api.runtime_status()),
    renderer: JSON.parse(api.renderer_status()),
    engine_build: manifest,
    cameras: JSON.parse(api.ship_camera_status()),
  };
  if (diagnostics === true) {
    let details = { page_id: page, diagnostics_helper_available: false };
    try { details = helper?.snapshot(api, page) ?? details; }
    catch { /* Keep the actual snapshot even if an optional hook fails. */ }
    value.diagnostics = details;
  }
  return value;
}

export function connect(token) {
  if (connected) return;
  connected = true;
  const page = crypto.randomUUID();
  const status = { status: "waiting_for_engine", error: null, page_id: page };
  globalThis.__wsorbLocalApi = status;
  diagnostic("local.attach", { page_id: page, status: "waiting_for_engine" });
  let stopped = false;
  const activeRequests = new Set();
  let capturing = false;
  let api;
  let manifest;
  const message = error => {
    const detail = typeof error?.message === "string" && error.message ? error.message
      : typeof error?.error === "string" && error.error ? error.error
        : typeof error === "string" && error ? error : "Unrecognized browser engine error";
    return detail.replaceAll(token, "[redacted]");
  };
  const errorCode = error => ERROR_CODES.has(error?.code) ? error.code : undefined;
  const report = (state, error = null) => {
    if (status.status !== state) diagnostic("local.status", { page_id: page, status: state });
    status.status = state;
    status.error = error;
  };

  addEventListener("pagehide", () => {
    stopped = true;
    for (const request of activeRequests) request.abort();
    report("disconnected", "This page left the local lab. Reload the simulation to reconnect.");
  }, { once: true });

  async function request(path, body, trace) {
    const controller = new AbortController();
    activeRequests.add(controller);
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(`/__local/${path}`,
        bridgeRequestOptions(token, page, path, body, manifest, controller.signal, trace));
      if (response.status === 409) diagnostic("local.lease_conflict", () => ({
        page_id: page, http_status: 409, id: trace?.id, trace_id: trace?.trace_id, diagnostics: trace?.diagnostics === true,
        request_id: response.headers.get("X-WSORB-Request-ID"),
      }));
      else if (!response.ok) diagnostic("local.request_failed", () => ({
        page_id: page, http_status: response.status, id: trace?.id, trace_id: trace?.trace_id, diagnostics: trace?.diagnostics === true,
      }));
      const reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > RESPONSE_LIMIT) {
          await reader.cancel();
          throw new Error("Local bridge response exceeds 2 MiB");
        }
        chunks.push(value);
      }
      const value = JSON.parse(await new Blob(chunks).text());
      if (!response.ok) {
        const error = new Error(value?.error || value?.message ? message(value) : `Local bridge returned HTTP ${response.status}`);
        const code = errorCode(value);
        if (code) error.code = code;
        throw error;
      }
      return value;
    } finally {
      clearTimeout(timer);
      activeRequests.delete(controller);
    }
  }

  // The panel receives a scoped authenticated operation, never the bearer token.
  status.controllers = payload => request("controllers", payload === undefined ? undefined : JSON.stringify(payload));

  async function attach() {
    const response = await fetch("/sim/manifest.json", {
      cache: "no-store", mode: "same-origin", credentials: "omit", redirect: "error", signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!response.ok) throw new Error(`Engine manifest returned HTTP ${response.status}`);
    const metadata = await response.json();
    if (!Number.isSafeInteger(metadata.bytes) || metadata.bytes <= 0 || metadata.bytes > 64 * BODY_LIMIT
      || typeof metadata.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(metadata.sha256)) {
      throw new Error("Invalid engine manifest. Rebuild and reload the local simulation.");
    }
    manifest = Object.freeze({ bytes: metadata.bytes, sha256: metadata.sha256 });
    const moduleUrl = `/sim/wsorb_bevy.js?v=${manifest.sha256}`;
    const loaded = performance.getEntriesByType("resource").filter(entry => new URL(entry.name).pathname === "/sim/wsorb_bevy.js");
    if (loaded.length && !loaded.some(entry => entry.name === new URL(moduleUrl, location.origin).href)) {
      throw new Error("Built assets changed after this page loaded. Reload the simulation before connecting the lab.");
    }
    // The app already initialized this exact module. Do not call default() or
    // start_simulation() here, even if rebuilt assets left it uninitialized.
    const module = await import(moduleUrl);
    const methods = ["observation", "telemetry_status", "navigation_status", "runtime_status", "renderer_status", "control", "set_navigation_preview", "ship_camera_status", "pan_tilt_camera", "ship_camera_capture", "lookahead_raycast"];
    if (methods.some(name => typeof module[name] !== "function")) {
      throw new Error("This engine build lacks the local practice API. Rebuild and reload the simulation.");
    }
    try {
      if (!JSON.parse(module.renderer_status()).ready) throw new Error("Renderer is not ready");
      JSON.parse(module.runtime_status());
    } catch {
      throw new Error("The loaded engine module is uninitialized or no longer ready. Restart the local lab and reload the simulation.");
    }
    api = module;
    diagnostic("local.ready", () => ({ page_id: page, status: "ready",
      observation: JSON.parse(api.observation()), renderer: JSON.parse(api.renderer_status()),
      camera: typeof api.camera_status === "function" ? JSON.parse(api.camera_status()) : undefined,
    }));
  }

  async function invoke(job) {
    let commandInvoked = false;
    const log = (event, fields = {}) => diagnostic(event, () => ({
      page_id: page, id: job.id, trace_id: job.trace_id, diagnostics: job.diagnostics === true,
      method: job.method, ...fields,
    }));
    const failure = (error, code) => ({
      envelope: { id: job.id, ok: false, error: message(error).slice(0, 2048), ...(ERROR_CODES.has(code) ? { code } : {}) },
      commandInvoked,
    });
    if (!Number.isSafeInteger(job.deadline_ms) || Date.now() >= job.deadline_ms) {
      return failure("Local call expired before engine invocation; no command was invoked");
    }
    if (globalThis.__wsorbStartup?.stage !== "ready" || !api) return failure("The browser engine is no longer ready");
    try { globalThis.__wsorbDiagnostics?.setNativeCollection(api, job.diagnostics === true); }
    catch { /* Collection is optional and never authorizes a command. */ }
    try {
      let value;
      if (job.method === "snapshot") {
        value = bridgeSnapshot(api, manifest, page, job.diagnostics === true);
        diagnostic("engine.sample", () => ({ page_id: page, id: job.id, trace_id: job.trace_id,
          diagnostics: job.diagnostics === true, observation: value.observation, runtime: value.runtime,
          renderer: value.renderer, ...globalThis.__wsorbDiagnostics?.snapshot(api, page),
        }));
      } else if (job.method === "raycast") {
        const result = JSON.parse(api.lookahead_raycast(JSON.stringify(job.query)));
        if (result.ok !== true) {
          log("local.native_rejection", { code: result.code, command_invoked: false });
          return failure(result.error || result.message || "Look-ahead ray was rejected", result.code);
        }
        value = result.report;
      } else if (job.method === "pan_tilt") {
        const result = JSON.parse(api.pan_tilt_camera(job.camera, job.yaw_deg, job.pitch_deg));
        if (result.ok !== true) {
          log("local.native_rejection", { code: result.code, command_invoked: false });
          return failure(result.error || result.message || "Camera aim was rejected", result.code);
        }
        value = { ok: true };
      } else if (job.method === "capture") {
        let timer;
        try {
          const frame = await Promise.race([
            api.ship_camera_capture(job.camera),
            new Promise((_, reject) => {
              timer = setTimeout(() => reject(new Error("Camera capture deadline expired")), Math.max(0, job.deadline_ms - Date.now()));
            }),
          ]);
          const { rgb, ...metadata } = frame;
          if (!(rgb instanceof Uint8Array) || rgb.length !== 512 * 512 * 3) {
            throw new Error("Camera did not return a genuine 512×512 RGB8 frame");
          }
          let binary = "";
          for (let start = 0; start < rgb.length; start += 16384) {
            binary += String.fromCharCode(...rgb.subarray(start, start + 16384));
          }
          value = { metadata, rgb_base64: btoa(binary) };
        } finally {
          clearTimeout(timer);
        }
      } else if (job.method === "preview") {
        const json = JSON.stringify(job.preview);
        if (Date.now() >= job.deadline_ms) return failure("Local call expired before preview publication");
        const result = JSON.parse(api.set_navigation_preview(json));
        if (result.ok !== true) {
          log("local.native_rejection", { code: result.code, command_invoked: false });
          return failure(result.error || result.message || "Python preview was rejected", result.code);
        }
        value = { ok: true };
      } else if (job.method === "command" && COMMANDS.has(job.command?.op)) {
        const json = JSON.stringify(job.command);
        if (Date.now() >= job.deadline_ms) return failure("Local call expired before engine invocation; no command was invoked");
        commandInvoked = true;
        log("local.invoked", { op: job.command.op, source_time_s: job.command.source_time_s, lease_sim_s: job.command.lease_sim_s });
        const result = JSON.parse(api.control(json));
        if (result.ok === false && (typeof result.error === "string" || typeof result.message === "string")) {
          if (UNAPPLIED_COMMAND_CODES.has(result.code)) commandInvoked = false;
          log("local.native_rejection", { code: result.code, command_invoked: commandInvoked });
          return failure(result, result.code);
        }
        if (result.ok !== true) throw new Error("Engine did not return a valid command acknowledgement");
        // Pause/play/view can be queued by the engine. This is only an ACK,
        // never a claim that the next Bevy frame has applied their effects.
        if (job.command.op === "renew_control") {
          if (!result.ownership_renewal || typeof result.ownership_renewal !== "object") {
            throw new Error("Native ownership renewal receipt unavailable");
          }
          value = { ok: true, ownership_renewal: result.ownership_renewal };
        } else value = { ok: true };
      } else return failure("Unsupported local browser job");
      log("local.ack", { ok: true, command_invoked: commandInvoked });
      return { envelope: { id: job.id, ok: true, value }, commandInvoked };
    } catch (error) {
      const code = errorCode(error);
      if (commandInvoked && UNAPPLIED_COMMAND_CODES.has(code)) commandInvoked = false;
      if (commandInvoked) log("local.indeterminate", { command_invoked: true, no_retry: true });
      return failure(commandInvoked
        ? `Engine command failed: ${message(error).slice(0, 1600)}. Outcome is indeterminate. Inspect live state before further flight control.`
        : `Browser engine request failed: ${message(error).slice(0, 1800)}`, code);
    }
  }

  async function complete(job) {
    const result = await invoke(job);
    let body = JSON.stringify(result.envelope);
    const limit = job.method === "capture" ? RESPONSE_LIMIT : BODY_LIMIT;
    if (new TextEncoder().encode(body).byteLength > limit) {
      body = JSON.stringify({ id: job.id, ok: false, error: "Browser report exceeds the result limit" });
    }
    try {
      if (stopped) return;
      diagnostic("local.result_sent", () => ({ page_id: page, id: job.id, trace_id: job.trace_id,
        diagnostics: job.diagnostics === true, ok: result.envelope.ok, command_invoked: result.commandInvoked,
      }));
      const acknowledgement = await request("result", body, job);
      if (acknowledgement.ok !== true) throw new Error("Invalid bridge acknowledgement");
      diagnostic("local.result_ack", () => ({ page_id: page, id: job.id, trace_id: job.trace_id,
        diagnostics: job.diagnostics === true, ok: true, command_invoked: result.commandInvoked,
      }));
    } catch {
      diagnostic(result.commandInvoked ? "local.indeterminate" : "local.no_retry", () => ({
        page_id: page, id: job.id, trace_id: job.trace_id, diagnostics: job.diagnostics === true,
        command_invoked: result.commandInvoked, no_retry: true,
      }));
      if (result.commandInvoked) {
        // An uncertain actuation result must never be resent automatically.
        stopped = true;
        report("error", "Command result was not acknowledged; outcome is indeterminate. Inspect live state before further flight control.");
      } else {
        report("error", "Browser result was not acknowledged. This job did not invoke a flight command.");
      }
    }
  }

  async function run() {
    while (!stopped) {
      const startup = globalThis.__wsorbStartup;
      if (startup?.stage === "failed") {
        report("error", `Simulation startup failed: ${startup.error || "browser engine unavailable"}`);
        return;
      }
      if (startup?.stage !== "ready") {
        report("waiting_for_engine", "Waiting for the real browser engine to become ready.");
      } else {
        if (!api) {
          try { await attach(); }
          catch (error) { report("error", message(error)); return; }
        }
        if (stopped || globalThis.__wsorbStartup?.stage !== "ready") continue;
        try {
          const { job } = await request("poll");
          if (stopped) return;
          report("connected");
          if (job) {
            diagnostic("local.received", () => ({ page_id: page, id: job.id, trace_id: job.trace_id,
              diagnostics: job.diagnostics === true, method: job.method, deadline_ms: job.deadline_ms,
              source_time_s: job.command?.source_time_s, lease_sim_s: job.command?.lease_sim_s,
            }));
            if (job.method === "capture") {
              if (capturing) {
                // The server can expire a capture before this page finishes its
                // result POST. A replacement must not start another readback or
                // block polling and flight control behind the old one.
                await request("result", JSON.stringify({
                  id: job.id, ok: false, error: "A camera readback is still finishing. Wait before capturing again.",
                }), job);
              } else {
                capturing = true;
                // Keep polling to renew the page lease and deliver flight commands
                // while the real GPU readback is pending. Never start it twice.
                void complete(job).catch(error => report("error", message(error))).finally(() => { capturing = false; });
              }
            } else await complete(job);
          }
        } catch (error) {
          if (!stopped) report("disconnected", message(error));
        }
      }
      await new Promise(resolve => setTimeout(resolve, POLL_MS));
    }
  }

  void run().catch(error => report("error", message(error)));
}
