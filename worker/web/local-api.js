// Served directly by the opt-in local server. Never bundled into hosted assets.
const POLL_MS = 50;
const REQUEST_TIMEOUT_MS = 2500;
const BODY_LIMIT = 1024 * 1024;
const RESPONSE_LIMIT = 2 * BODY_LIMIT;
const COMMANDS = new Set(["pause", "play", "step", "reset", "set_command", "stop", "view", "debug_upright", "configure_experiment"]);
let connected = false;

export function connect(token) {
  if (connected) return;
  connected = true;
  const status = { status: "waiting_for_engine", error: null };
  globalThis.__wsorbLocalApi = status;
  const page = crypto.randomUUID();
  let stopped = false;
  const activeRequests = new Set();
  let capturing = false;
  let api;
  let manifest;
  const message = error => error instanceof Error ? error.message : String(error);
  const report = (state, error = null) => {
    status.status = state;
    status.error = error;
  };

  addEventListener("pagehide", () => {
    stopped = true;
    for (const request of activeRequests) request.abort();
    report("disconnected", "This page left the local lab. Reload the simulation to reconnect.");
  }, { once: true });

  async function request(path, body) {
    const controller = new AbortController();
    activeRequests.add(controller);
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(`/__local/${path}`, {
        method: body === undefined ? "GET" : "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "X-WSORB-Page": page,
          ...(path === "poll" ? { "X-WSORB-Build": manifest.sha256 } : {}),
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body,
        cache: "no-store",
        mode: "same-origin",
        credentials: "omit",
        redirect: "error",
        signal: controller.signal,
      });
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
      if (!response.ok) throw new Error(value.error || `Local bridge returned HTTP ${response.status}`);
      return value;
    } finally {
      clearTimeout(timer);
      activeRequests.delete(controller);
    }
  }

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
    const methods = ["observation", "telemetry_status", "navigation_status", "runtime_status", "renderer_status", "command", "set_navigation_preview", "ship_camera_status", "pan_tilt_camera", "ship_camera_capture"];
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
  }

  async function invoke(job) {
    let commandInvoked = false;
    const failure = error => ({ envelope: { id: job.id, ok: false, error }, commandInvoked });
    if (!Number.isSafeInteger(job.deadline_ms) || Date.now() >= job.deadline_ms) {
      return failure("Local call expired before engine invocation; no command was invoked");
    }
    if (globalThis.__wsorbStartup?.stage !== "ready" || !api) return failure("The browser engine is no longer ready");
    try {
      let value;
      if (job.method === "snapshot") {
        value = {
          observation: JSON.parse(api.observation()),
          telemetry: JSON.parse(api.telemetry_status()),
          navigation: JSON.parse(api.navigation_status()),
          runtime: JSON.parse(api.runtime_status()),
          engine_build: manifest,
          cameras: JSON.parse(api.ship_camera_status()),
        };
      } else if (job.method === "pan_tilt") {
        const result = JSON.parse(api.pan_tilt_camera(job.camera, job.yaw_deg, job.pitch_deg));
        if (result.ok !== true) return failure(result.error || "Camera aim was rejected");
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
        if (result.ok !== true) return failure(result.error || "Python preview was rejected");
        value = { ok: true };
      } else if (job.method === "command" && COMMANDS.has(job.command?.op)) {
        const json = JSON.stringify(job.command);
        if (Date.now() >= job.deadline_ms) return failure("Local call expired before engine invocation; no command was invoked");
        commandInvoked = true;
        const result = JSON.parse(api.command(json));
        if (result.ok === false && typeof result.error === "string") return failure(result.error.slice(0, 2048));
        if (result.ok !== true) throw new Error("Engine did not return a valid command acknowledgement");
        // Pause/play/view can be queued by the engine. This is only an ACK,
        // never a claim that the next Bevy frame has applied their effects.
        value = { ok: true };
      } else return failure("Unsupported local browser job");
      return { envelope: { id: job.id, ok: true, value }, commandInvoked };
    } catch (error) {
      return failure(commandInvoked
        ? "Engine command did not return a valid acknowledgement; outcome is indeterminate. Inspect live state before further flight control."
        : `Browser engine request failed: ${message(error).slice(0, 1800)}`);
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
      const acknowledgement = await request("result", body);
      if (acknowledgement.ok !== true) throw new Error("Invalid bridge acknowledgement");
    } catch {
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
            if (job.method === "capture") {
              if (capturing) {
                // The server can expire a capture before this page finishes its
                // result POST. A replacement must not start another readback or
                // block polling and flight control behind the old one.
                await request("result", JSON.stringify({
                  id: job.id, ok: false, error: "A camera readback is still finishing. Wait before capturing again.",
                }));
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
