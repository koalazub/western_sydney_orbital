// Failure presentation uses cached data and browser APIs, never the failed WASM instance.
export type StartupFailure = {
  code: string;
  title: string;
  message: string;
  recovery: string;
  detail: string;
};

const surfaceCodes = new Set(["canvas_context_unavailable", "canvas_context_error"]);
const ownerCodes = new Set(["canvas_owned", "renderer_already_started"]);
const canvasCodes = new Set(["canvas_missing", "canvas_invalid", "canvas_detached"]);

function errorText(value: unknown): string {
  try {
    const messages: string[] = [];
    const seen = new Set<unknown>();
    while (value !== undefined && value !== null && !seen.has(value) && messages.length < 4) {
      seen.add(value);
      if (value && typeof value === "object" && "message" in value && typeof value.message === "string") {
        const name = "name" in value && typeof value.name === "string" ? value.name : "Error";
        messages.push(`${name}: ${value.message}`);
        value = "cause" in value ? value.cause : undefined;
      } else {
        messages.push(String(value));
        break;
      }
    }
    return messages.join("\nCaused by: ") || "Unknown startup failure";
  } catch {
    return "The browser could not describe the startup error.";
  }
}

function redactDetail(value: string): string {
  return value
    .replace(/\/(Users|home)\/[^/\s]+\//g, "/$1/<user>/")
    .replace(/([?&](?:token|access_token|auth|key)=)[^\s&#]+/gi, "$1[redacted]");
}

export function diagnoseStartupFailure(cause: unknown, browser = ""): StartupFailure {
  const detail = redactDetail(errorText(cause)).slice(0, 12_000);
  let code = "startup_failed";
  try {
    if (cause && typeof cause === "object" && "code" in cause && typeof cause.code === "string") {
      code = cause.code.slice(0, 80);
    }
  } catch { /* A thrown getter must not break the error screen. */ }
  if (/canvas\.getContext\(\) returned null|Failed to create wgpu surface/.test(detail)) {
    code = "canvas_context_unavailable";
  } else if (code === "startup_failed" && /unreachable|Bevy stopped:|panicked at/.test(detail)) {
    code = "renderer_stopped";
  }
  const safari = /Safari|AppleWebKit/.test(browser) && !/Chrome|Chromium|Edg\//.test(browser);
  const browserRecovery = safari
    ? "Restart Safari Technology Preview or Safari and reopen this page. If it still fails, update the browser or try a current Chromium browser with WebGPU support."
    : "Restart or update your browser and reopen this page. Check that hardware acceleration and WebGPU are available.";
  if (surfaceCodes.has(code)) {
    return { code, title: "WebGPU canvas unavailable",
      message: "The browser did not provide the WebGPU drawing surface. The renderer cannot start.",
      recovery: browserRecovery + " Copy the diagnostics below if the problem continues.", detail };
  }
  if (ownerCodes.has(code)) {
    return { code, title: "This canvas already has a renderer",
      message: "A second renderer was blocked so it cannot take over the existing simulation.",
      recovery: "Use the existing simulation. Reload only if you intend to start a fresh flight.", detail };
  }
  if (canvasCodes.has(code) || code === "document_unavailable") {
    return { code, title: "Simulation canvas unavailable",
      message: "The game page could not provide a connected drawing canvas.",
      recovery: "Open the simulation directly in a browser tab. Reload the page, then copy the diagnostics if it still fails.", detail };
  }
  if (code === "insecure_context") {
    return { code, title: "WebGPU needs a secure page",
      message: "This page cannot access browser graphics from its current address.",
      recovery: "Open the HTTPS simulation address, or use the localhost address printed by the local server.", detail };
  }
  if (code === "webgpu_unavailable" || code === "adapter_unavailable" || code === "adapter_request_error") {
    return { code, title: "WebGPU unavailable",
      message: code === "webgpu_unavailable"
        ? "The browser has not exposed WebGPU to this page."
        : "The browser could not provide a WebGPU graphics adapter.",
      recovery: browserRecovery + " The simulation has not started.", detail };
  }
  return { code, title: code === "renderer_stopped" ? "The renderer stopped" : "Simulation could not start",
    message: "Startup did not complete. The technical error has been kept in the diagnostics below.",
    recovery: "Reload to start a fresh simulation. If it fails again, copy the diagnostics so the failure can be investigated.", detail };
}

const snapshotKeys = ["stage", "renderer", "gpu_preflight", "browser", "secure_context", "visibility",
  "elapsed_visible_ms", "engine_load", "wasm_instantiation_ms", "engine_build", "canvas"];

function cachedSnapshot(snapshot: unknown): Record<string, unknown> {
  const cached: Record<string, unknown> = {};
  if (snapshot && typeof snapshot === "object") {
    for (const key of snapshotKeys) {
      try {
        if (Object.hasOwn(snapshot, key)) cached[key] = (snapshot as Record<string, unknown>)[key];
      } catch { /* Preserve the original error even if a diagnostic field is unreadable. */ }
    }
  }
  return cached;
}

export function startupDiagnostics(failure: StartupFailure, snapshot?: unknown): string {
  const cached = cachedSnapshot(snapshot);
  try {
    return redactDetail(JSON.stringify({ schema: "wsorb.startup_failure.v1", failure,
      readback_scope: "Cached startup observations; no calls into the failed renderer.", ...cached }, null, 2));
  } catch {
    return JSON.stringify({ schema: "wsorb.startup_failure.v1", failure,
      readback_scope: "Startup observations could not be serialized. No renderer call was attempted." }, null, 2);
  }
}

let bound = false;
let latestDiagnostics = "";

export function initializeStartupRecovery() {
  if (bound) return;
  bound = true;
  document.getElementById("retry-world")?.addEventListener("click", () => location.reload());
  document.getElementById("copy-startup-diagnostics")?.addEventListener("click", async () => {
    const status = document.getElementById("diagnostics-status");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(latestDiagnostics);
      if (status) status.textContent = "Startup diagnostics copied.";
    } catch {
      const details = document.getElementById("startup-details") as HTMLDetailsElement | null;
      const report = document.getElementById("startup-diagnostics");
      if (details) details.open = true;
      report?.focus();
      if (report) window.getSelection()?.selectAllChildren(report);
      if (status) status.textContent = "Clipboard access is unavailable. Copy the selected technical details below.";
    }
  });
}

function browserSnapshot(): Record<string, unknown> {
  const canvas = document.getElementById("simulation-canvas");
  const rect = canvas?.getBoundingClientRect();
  return { stage: "failed", renderer: null, gpu_preflight: null,
    browser: navigator.userAgent, secure_context: isSecureContext, visibility: document.visibilityState,
    canvas: canvas instanceof HTMLCanvasElement ? {
      width: canvas.width, height: canvas.height, css_width: rect?.width, css_height: rect?.height,
      device_pixel_ratio: devicePixelRatio, connected: canvas.isConnected,
      surface: canvas.getAttribute("data-wsorb-surface"),
      owner: canvas.getAttribute("data-wsorb-renderer-owner"),
    } : null };
}

export function showStartupFailure(cause: unknown, snapshot?: unknown) {
  const failure = diagnoseStartupFailure(cause, navigator.userAgent);
  const observed = snapshot && typeof snapshot === "object" ? cachedSnapshot(snapshot) : browserSnapshot();
  const globals = globalThis as typeof globalThis & { __wsorbStartup?: Record<string, unknown> };
  globals.__wsorbStartup = { ...observed, stage: "failed", error: failure.detail, failure };
  latestDiagnostics = startupDiagnostics(failure, observed);
  const boot = document.getElementById("boot-screen");
  if (boot) boot.hidden = false;
  const set = (id: string, value: string) => {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
  };
  set("sim-status", failure.title);
  set("world-message", failure.message);
  set("startup-recovery", failure.recovery);
  set("wasm-progress-text", "Startup stopped. No automatic reload or renderer retry will run.");
  set("startup-diagnostics", latestDiagnostics);
  document.getElementById("wasm-progress")?.removeAttribute("value");
  for (const id of ["retry-world", "startup-recovery", "copy-startup-diagnostics", "startup-details", "reload-consequence"]) {
    const element = document.getElementById(id);
    if (element) element.hidden = false;
  }
  initializeStartupRecovery();
}
