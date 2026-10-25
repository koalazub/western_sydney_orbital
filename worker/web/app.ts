// Browser responsibilities stop at loading and platform APIs. Camera movement,
// mission playback, telemetry, inspection and interaction are owned by Bevy.
import { engineManifest, loadVerifiedEngine, type EngineLoadTimings } from "./engine-loader.ts";
import planetaryAssets from "./assets/asset-manifest.json";
type Engine = {
  default: (options: unknown) => Promise<unknown>;
  start_simulation: (selector: string) => void;
  renderer_status: () => string;
  camera_status: () => string;
  hud_status: () => string;
  runtime_status: () => string;
  telemetry_status: () => string;
  command: (json: string) => string;
  set_viewport_input: (active: boolean, locked: boolean) => void;
  set_movement_keys: (mask: number) => void;
  key_bindings_status: () => string;
  set_key_bindings: (json: string) => string;
  pinch_zoom: (logDistanceDelta: number) => void;
  fullscreen_button_at: (x: number, y: number) => boolean;
  fullscreen_button_focused: () => boolean;
  notebook_button_at: (x: number, y: number) => boolean;
  notebook_button_focused: () => boolean;
  platform_state: (fullscreen: boolean, maximized: boolean, message: string) => void;
};
type StartupStage =
  | "initializing"
  | "preparing_view"
  | "drawing_world"
  | "presenting"
  | "waiting_for_gpu"
  | "ready"
  | "failed";
type RendererStatus = {
  ready: boolean;
  stage: StartupStage;
  rendered_frames: number;
  submitted_primary_frames: number;
  completed_primary_frame: boolean;
  primary_world_draws: number;
  primary_planetary_draw_mask: number;
  primary_viewport: [number, number] | null;
  primary_target_size: [number, number] | null;
  adapter: { name: string; backend: string; device_type: string; driver: string } | null;
  compiled_pipelines: number;
  pending_pipelines: number;
  pending_pipeline_labels: string[];
  surface_maps_ready: boolean;
  surface_maps_gpu_ready: boolean;
  error: string | null;
};
type WebKitDocument = Document & {
  webkitFullscreenElement?: Element | null;
  webkitExitFullscreen?: () => void | Promise<void>;
};
type WebKitElement = HTMLElement & {
  webkitRequestFullscreen?: () => void | Promise<void>;
};
type ScaleGesture = Event & { scale: number };
type GPUPreflight = Awaited<ReturnType<typeof graphicsPreflight>>;

const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const canvas = byId<HTMLCanvasElement>("simulation-canvas");
const viewport = byId<WebKitElement>("simulation-viewport");
const boot = byId<HTMLElement>("boot-screen");
const progress = byId<HTMLProgressElement>("wasm-progress");
const statusText = byId<HTMLElement>("sim-status");
const progressText = byId<HTMLElement>("wasm-progress-text");
const retryButton = byId<HTMLButtonElement>("retry-world");
const webkitDocument = document as WebKitDocument;
const browserGlobals = globalThis as typeof globalThis & {
  __wsorbStartup?: {
    stage: string;
    renderer: RendererStatus | null;
    hud: unknown;
    gpu_preflight: GPUPreflight | null;
    browser: string;
    secure_context: boolean;
    visibility: DocumentVisibilityState;
    elapsed_visible_ms: number;
    engine_load: EngineLoadTimings | null;
    wasm_instantiation_ms: number | null;
    canvas: {
      width: number;
      height: number;
      css_width: number;
      css_height: number;
      device_pixel_ratio: number;
    };
    error: string | null;
  };
  __wsorbGPU?: GPUPreflight;
  __wsorbFullscreenFailure?: {
    activation: boolean | undefined;
    name: string;
    message: string;
  } | null;
  __wsorbPlatformAction?: typeof platformAction;
  __wsorbEngineError?: typeof fail;
};

let engine: Engine | undefined;
let engineBuild: { bytes: number; sha256: string } | undefined;
let ready = false;
let failed = false;
let platformMessage = "WebGPU · Click the scene to activate ship and debug controls";
let startupPhase = "checking_graphics";
let startupVisibleWait = 0;
let lastRendererStatus: RendererStatus | undefined;
let engineLoadTimings: EngineLoadTimings | null = null;
let wasmInstantiationMs: number | null = null;
let fullscreenPending = false;
type TrustedAction = "fullscreen" | "notebook";
let platformPress: { pointerId: number; action: TrustedAction } | null = null;
let gestureScale: number | null = null;
let pointerLockExitPending = false;
const movementBits = new Map([
  ["KeyW", 1 << 0],
  ["KeyA", 1 << 1],
  ["KeyS", 1 << 2],
  ["KeyD", 1 << 3],
  ["KeyQ", 1 << 4],
  ["KeyE", 1 << 5],
  ["Space", 1 << 9],
]);
const movementHeld = new Set<string>();
const BINDINGS_STORAGE = "wsorb.ship-bindings.v1";
let bindingRevision: number | undefined;
let bindingsCapturing = false;

function syncBindings(persist = true) {
  if (!engine) return;
  const status = JSON.parse(engine.key_bindings_status());
  bindingsCapturing = status.capture.active;
  if (status.revision === bindingRevision) return;
  clearMovementKeys();
  movementBits.clear();
  for (const [action, code] of Object.entries(status.bindings)) {
    movementBits.set(code as string, status.action_bits[action]);
  }
  bindingRevision = status.revision;
  if (persist) {
    try {
      localStorage.setItem(BINDINGS_STORAGE, JSON.stringify({ version: 1, bindings: status.bindings }));
    } catch {
      // Restricted browser storage must not disable this page's working keys.
    }
  }
}

function restoreBindings() {
  if (!engine) return;
  try {
    const stored = localStorage.getItem(BINDINGS_STORAGE);
    if (stored && JSON.parse(engine.set_key_bindings(stored)).ok !== true) {
      localStorage.removeItem(BINDINGS_STORAGE);
    }
  } catch {
    // The native defaults remain available when storage is unavailable.
  }
  syncBindings(false);
}

function scheduleBindingsSync() {
  // Native widgets apply in Bevy's frame, not in the earlier DOM handler.
  requestAnimationFrame(() => requestAnimationFrame(() => syncBindings()));
}

function pageBackgroundFocused() {
  return document.activeElement === document.body || document.activeElement === document.documentElement;
}

function viewportInputActive() {
  // Pointer lock does not give the game keys belonging to an HTML control.
  return ready && document.hasFocus() && !document.hidden && !pointerLockExitPending
    && (document.activeElement === canvas || (document.pointerLockElement === canvas && pageBackgroundFocused()));
}

function clearMovementKeys() {
  movementHeld.clear();
  engine?.set_movement_keys(0);
}

function movementKey(event: KeyboardEvent, down: boolean) {
  if (!ready || !engine) return;
  syncBindings();
  scheduleBindingsSync();
  if (bindingsCapturing) {
    clearMovementKeys();
    if (!event.ctrlKey && !event.metaKey) event.preventDefault();
    inputState();
    return;
  }
  if (!viewportInputActive() || event.code === "Escape") {
    clearMovementKeys();
    return;
  }
  const movement = movementBits.has(event.code);
  const ownsMovement = movement && !event.ctrlKey && !event.metaKey;
  if (event.ctrlKey || event.metaKey) {
    // Browser and assistive shortcuts must not turn into delayed ship input.
    movementHeld.clear();
  } else if (movement) {
    // A clear requires a fresh press, never an autorepeat after focus or a chord.
    if (!down) movementHeld.delete(event.code);
    else if (!event.repeat) movementHeld.add(event.code);
  }
  let mask = 0;
  // Use physical codes: Option+W may have a printable non-W character on macOS.
  for (const code of movementHeld) mask |= movementBits.get(code) ?? 0;
  if (event.altKey) mask |= 1 << 6;
  if (event.shiftKey) mask |= 1 << 7;
  if (event.ctrlKey || event.metaKey) mask |= 1 << 8;
  engine.set_movement_keys(mask);
  inputState();
  if (ownsMovement) {
    event.preventDefault();
    // Publish the raw source first, then keep Space out of Winit's button input.
    // A captured Space must still reach Bevy on key-up, even after capture
    // completed. Otherwise its native held state could never rearm safely.
    if (down && event.code === "Space") event.stopImmediatePropagation();
  }
}

function fullscreenElement() {
  return document.fullscreenElement || webkitDocument.webkitFullscreenElement || null;
}

function errorMessage(value: unknown) {
  return value instanceof Error ? value.message : String(value);
}

function announce(message: string) {
  byId("viewport-announcement").textContent = message;
}

function recordStartup(error?: string) {
  const rect = canvas.getBoundingClientRect();
  // This stays in diagnostics, not an HTML telemetry panel. The preflight
  // adapter is not necessarily the adapter Bevy actually selects.
  browserGlobals.__wsorbStartup = {
    stage: failed ? "failed" : lastRendererStatus?.stage ?? startupPhase,
    renderer: lastRendererStatus ?? null,
    hud: engine ? JSON.parse(engine.hud_status()) : null,
    gpu_preflight: browserGlobals.__wsorbGPU ?? null,
    browser: navigator.userAgent,
    secure_context: isSecureContext,
    visibility: document.visibilityState,
    elapsed_visible_ms: Math.round(startupVisibleWait),
    engine_load: engineLoadTimings,
    wasm_instantiation_ms: wasmInstantiationMs,
    canvas: {
      width: canvas.width,
      height: canvas.height,
      css_width: rect.width,
      css_height: rect.height,
      device_pixel_ratio: devicePixelRatio,
    },
    error: error ?? null,
  };
}

function publishPlatform(message = platformMessage) {
  platformMessage = message;
  engine?.platform_state(!!fullscreenElement(), !viewport.classList.contains("windowed"), message);
}

function inputState() {
  const locked = document.pointerLockElement === canvas;
  const active = viewportInputActive();
  if (!active) clearMovementKeys();
  engine?.set_viewport_input(active, locked);
}

function resetViewportInput() {
  gestureScale = null;
  platformPress = null;
  clearMovementKeys();
  inputState();
}

function fail(message: string) {
  if (failed) return;
  ready = false;
  failed = true;
  recordStartup(message);
  boot.hidden = false;
  statusText.textContent = "3D unavailable";
  byId("world-message").textContent = message;
  progressText.textContent = "Simulation could not start.";
  progress.removeAttribute("value");
  retryButton.hidden = false;
  inputState();
}

function maximize() {
  viewport.classList.toggle("windowed");
  publishPlatform(viewport.classList.contains("windowed") ? "Windowed view" : "Maximized view");
  canvas.focus({ preventScroll: true });
  window.dispatchEvent(new Event("resize"));
}

async function fullscreen() {
  if (fullscreenPending) return;
  fullscreenPending = true;
  browserGlobals.__wsorbFullscreenFailure = null;
  const activation = navigator.userActivation?.isActive;
  const exiting = !!fullscreenElement();
  try {
    // The native API call must happen in this trusted DOM event, before any
    // await or Bevy animation-frame queue consumes Safari's user activation.
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else if (exiting) {
      if (!webkitDocument.webkitExitFullscreen) throw new Error("This browser does not expose fullscreen exit");
      await webkitDocument.webkitExitFullscreen();
    } else if (viewport.requestFullscreen) {
      await viewport.requestFullscreen();
    } else if (viewport.webkitRequestFullscreen) {
      await viewport.webkitRequestFullscreen();
    } else {
      throw new Error("This browser does not expose fullscreen");
    }
    canvas.focus({ preventScroll: true });
    publishPlatform(fullscreenElement() ? "Fullscreen · Escape exits" : exiting ? "Fullscreen closed" : "Fullscreen requested");
  } catch (error) {
    browserGlobals.__wsorbFullscreenFailure = {
      activation,
      name: error instanceof DOMException ? error.name : "Error",
      message: errorMessage(error),
    };
    publishPlatform(`Fullscreen could not be ${exiting ? "closed" : "entered"}: ${errorMessage(error)}. Click Fullscreen or press X in the game.`);
    announce(platformMessage);
  } finally {
    fullscreenPending = false;
  }
}

function platformButtonAt(event: PointerEvent): TrustedAction | null {
  const rect = canvas.getBoundingClientRect();
  if (!ready || !engine || rect.width <= 0 || rect.height <= 0) return null;
  const x = (event.clientX - rect.left) * canvas.clientWidth / rect.width;
  const y = (event.clientY - rect.top) * canvas.clientHeight / rect.height;
  if (engine.fullscreen_button_at(x, y)) return "fullscreen";
  if (engine.notebook_button_at(x, y)) return "notebook";
  return null;
}

function openNotebook() {
  // Open a stable same-origin page in this trusted event. The local server can
  // resolve private Marimo authentication after navigation, without exposing a
  // token in the hosted app or awaiting a fetch before Safari opens the tab.
  if (navigator.userActivation?.isActive === false) {
    publishPlatform("Use Open notebook in the game to open the flight lab.");
    announce(platformMessage);
    return;
  }
  try {
    window.open("/notebook.html", "_blank", "noopener,noreferrer");
    publishPlatform("Opening the flight lab in a new tab. If it does not appear, allow popups for this game.");
  } catch {
    publishPlatform("The notebook could not open. Allow popups for this game and use Open notebook again.");
  }
  announce(platformMessage);
}

function trustedAction(action: TrustedAction) {
  if (action === "fullscreen") void fullscreen();
  else openNotebook();
}

function pinch(logDistanceDelta: number) {
  if (!ready || !document.hasFocus() || !Number.isFinite(logDistanceDelta)) return;
  canvas.focus({ preventScroll: true });
  inputState();
  engine!.pinch_zoom(logDistanceDelta);
}

function changeGesture(event: Event) {
  const scale = (event as ScaleGesture).scale;
  if (!ready || gestureScale === null || !Number.isFinite(scale) || scale <= 0) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  pinch(Math.log(gestureScale / scale));
  gestureScale = scale;
}

async function captureMouse() {
  try {
    await canvas.requestPointerLock();
    publishPlatform("Mouse captured · Escape releases it");
  } catch {
    publishPlatform("Mouse capture unavailable. Drag to look instead.");
    announce(platformMessage);
  }
}

function releaseMouse() {
  // exitPointerLock is asynchronous; stop input before its change event arrives.
  pointerLockExitPending = document.pointerLockElement === canvas;
  clearMovementKeys();
  if (document.pointerLockElement) document.exitPointerLock();
  canvas.blur();
  inputState();
  publishPlatform("Mouse released · Click the scene to resume ship and debug controls");
}

function exportFlight() {
  if (!engine) return;
  const result = JSON.parse(engine.command(JSON.stringify({ op: "observe" })));
  if (!result.ok) {
    publishPlatform("Could not export the current mission");
    return;
  }
  const record = {
    exported_at: new Date().toISOString(),
    engine_build: engineBuild,
    state: result.state,
    camera: JSON.parse(engine.camera_status()),
    runtime: JSON.parse(engine.runtime_status()),
    telemetry: JSON.parse(engine.telemetry_status()),
    model_limitations: result.state.model,
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `phobos-flight-${result.state.seed}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  publishPlatform("Flight state exported");
}

function platformAction(action: string) {
  // Native HUD fullscreen input uses the trusted DOM handlers below. Other
  // platform commands can arrive from Bevy's simulation-frame action queue.
  if (action === "fullscreen") void fullscreen();
  else if (action === "maximize") maximize();
  else if (action === "capture") void captureMouse();
  else if (action === "release") releaseMouse();
  else if (action === "export") queueMicrotask(exportFlight);
  else if (action === "notebook") openNotebook();
}

function pressPlatformButton(event: PointerEvent) {
  if (!ready) return;
  canvas.focus({ preventScroll: true });
  inputState();
  const action = event.button === 0 ? platformButtonAt(event) : null;
  platformPress = action ? { pointerId: event.pointerId, action } : null;
}

function releasePlatformButton(event: PointerEvent) {
  if (ready) scheduleBindingsSync();
  const press = platformPress;
  platformPress = null;
  if (press && press.pointerId === event.pointerId && platformButtonAt(event) === press.action) {
    trustedAction(press.action);
  }
}

function wheelZoom(event: WheelEvent) {
  if (!ready || event.metaKey) return;
  if (event.ctrlKey) {
    // Trackpad pinch is ctrl-wheel in browsers without WebKit GestureEvents.
    // Keep it out of both page zoom and Winit's ordinary wheel accumulator.
    event.preventDefault();
    event.stopImmediatePropagation();
    if (gestureScale === null) {
      const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? canvas.clientHeight : 1;
      pinch(event.deltaY * unit * 0.01);
    }
  } else if (document.activeElement === canvas) event.preventDefault();
}

function startGesture(event: Event) {
  if (!ready || !document.hasFocus()) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const scale = (event as ScaleGesture).scale;
  gestureScale = Number.isFinite(scale) && scale > 0 ? scale : 1;
}

function endGesture(event: Event) {
  changeGesture(event);
  gestureScale = null;
}

function pointerLockChanged() {
  if (document.pointerLockElement !== canvas) pointerLockExitPending = false;
  clearMovementKeys();
  inputState();
  publishPlatform(document.pointerLockElement === canvas ? "Mouse captured · Escape releases it" : "Mouse released");
}

function fullscreenChanged() {
  clearMovementKeys();
  publishPlatform(fullscreenElement() ? "Fullscreen · Escape exits" : "Fullscreen closed");
  inputState();
  window.dispatchEvent(new Event("resize"));
}

function platformKey(event: KeyboardEvent) {
  if (!ready || !engine || event.isComposing || event.ctrlKey || event.metaKey || event.altKey
    || bindingsCapturing || document.activeElement !== canvas) return;
  let action: TrustedAction | null = event.code === "KeyX" ? "fullscreen" : null;
  if (event.code === "Enter") {
    if (engine.fullscreen_button_focused()) action = "fullscreen";
    else if (engine.notebook_button_focused()) action = "notebook";
  }
  if (!action) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (!event.repeat) trustedAction(action);
}

function preventViewportScroll(event: KeyboardEvent) {
  if (!ready || document.activeElement !== canvas || event.ctrlKey || event.metaKey) return;
  if (["Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.code)) event.preventDefault();
}

function windowError(event: ErrorEvent) {
  if (/wasm|wsorb_bevy/.test(`${event.filename} ${event.error?.stack || ""}`)) {
    fail(`The simulation stopped: ${event.message}`);
  }
}

function unhandledRejection(event: PromiseRejectionEvent) {
  if (/wasm|wsorb_bevy/.test(String(event.reason?.stack || ""))) {
    fail(`The simulation stopped: ${errorMessage(event.reason)}`);
  }
}

browserGlobals.__wsorbPlatformAction = platformAction;
browserGlobals.__wsorbEngineError = fail;
canvas.addEventListener("pointerdown", pressPlatformButton, { capture: true });
canvas.addEventListener("pointerup", releasePlatformButton, { capture: true });
canvas.addEventListener("pointercancel", () => {
  platformPress = null;
});
canvas.addEventListener("focus", inputState);
canvas.addEventListener("blur", resetViewportInput);
canvas.addEventListener("contextmenu", event => event.preventDefault());
canvas.addEventListener("wheel", wheelZoom, { passive: false, capture: true });
canvas.addEventListener("gesturestart", startGesture, { passive: false, capture: true });
canvas.addEventListener("gesturechange", changeGesture, { passive: false, capture: true });
canvas.addEventListener("gestureend", endGesture, { passive: false, capture: true });
window.addEventListener("blur", resetViewportInput);
window.addEventListener("focus", inputState);
document.addEventListener("focusin", inputState, { capture: true });
document.addEventListener("focusout", inputState, { capture: true });
document.addEventListener("visibilitychange", inputState);
document.addEventListener("pointerlockchange", pointerLockChanged);
for (const name of ["fullscreenchange", "webkitfullscreenchange"]) {
  document.addEventListener(name, fullscreenChanged);
}
window.addEventListener("keydown", event => movementKey(event, true), { capture: true });
window.addEventListener("keyup", event => movementKey(event, false), { capture: true });
window.addEventListener("keydown", platformKey, { capture: true });
window.addEventListener("keydown", preventViewportScroll);
window.addEventListener("error", windowError);
window.addEventListener("unhandledrejection", unhandledRejection);
new ResizeObserver(() => {
  if (engine && !failed) window.dispatchEvent(new Event("resize"));
}).observe(viewport);
retryButton.addEventListener("click", () => location.reload());

const megabytes = (bytes: number) => `${(bytes / 1048576).toFixed(2)} MB`;
const startupDescriptions: Record<StartupStage, string> = {
  initializing: "initializing the graphics device",
  preparing_view: "preparing the game camera and framebuffer",
  drawing_world: "drawing the first world frame",
  presenting: "presenting the game camera to the canvas",
  waiting_for_gpu: "waiting for the GPU to complete the submitted game frame",
  ready: "game frame complete",
  failed: "renderer stopped",
};

async function waitForRenderer() {
  // Only forward render milestones reset the stall budget. Pipeline-cache
  // churn and arbitrary render-loop counts neither prove readiness nor extend
  // this wait. Browsers may suspend animation frames in a hidden tab.
  const stages: StartupStage[] = ["initializing", "preparing_view", "drawing_world", "presenting", "waiting_for_gpu", "ready"];
  let furthestStage = 0;
  let lastProgress = 0;
  let lastTick = performance.now();
  let wasVisible = !document.hidden;
  await new Promise<void>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let settled = false;
    const finish = (error?: Error) => {
      settled = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", check);
      if (error) reject(error);
      else resolve();
    };
    const check = () => {
      if (settled) return;
      clearTimeout(timer);
      const now = performance.now();
      if (wasVisible) startupVisibleWait += now - lastTick;
      lastTick = now;
      wasVisible = !document.hidden;
      if (failed) {
        finish(new Error("Bevy reported a startup failure"));
        return;
      }
      try {
        const status = JSON.parse(engine!.renderer_status()) as RendererStatus;
        lastRendererStatus = status;
        recordStartup();
        if (status.error) {
          finish(new Error(status.error));
          return;
        }
        if (status.ready) {
          finish();
          return;
        }
        const stage = status.stage ?? "initializing";
        const milestone = stages.indexOf(stage);
        if (milestone > furthestStage) {
          furthestStage = milestone;
          lastProgress = startupVisibleWait;
        }
        const description = status.adapter && !status.surface_maps_gpu_ready
          ? status.surface_maps_ready ? "uploading the planetary and star maps" : "loading the planetary and star maps"
          : startupDescriptions[stage] ?? startupDescriptions.initializing;
        progressText.textContent = document.hidden
          ? "Startup paused while this tab is hidden. Return to the tab to continue."
          : `WebGPU: ${description}. ${status.compiled_pipelines} pipelines ready, ${status.pending_pipelines} pending.`;
        if (!document.hidden && startupVisibleWait - lastProgress > 60_000) {
          const size = status.primary_target_size?.join("x") ?? `${canvas.width}x${canvas.height}`;
          const adapter = status.adapter ? status.adapter.name || `${status.adapter.backend}/${status.adapter.device_type}` : "not initialized";
          finish(new Error(`WebGPU startup stalled while ${description}. No render milestone advanced for 60 seconds in the visible tab. ${status.rendered_frames} render updates, ${status.submitted_primary_frames ?? 0} game frames submitted; framebuffer ${size}; GPU ${adapter}.`));
          return;
        }
        timer = setTimeout(check, 100);
      } catch (error) {
        finish(new Error(errorMessage(error)));
      }
    };
    document.addEventListener("visibilitychange", check);
    check();
  });
}

function preloadPlanetaryMaps() {
  for (const asset of planetaryAssets.textures) {
    const link = document.createElement("link");
    link.rel = "preload";
    link.as = "fetch";
    link.href = `/assets/${asset.path}`;
    link.type = "image/png";
    link.crossOrigin = "anonymous";
    link.fetchPriority = "low";
    document.head.append(link);
  }
}

async function graphicsPreflight() {
  const gpu = (navigator as Navigator & {
    gpu?: {
      requestAdapter(options: { powerPreference: "high-performance" }): Promise<{
        info?: {
          vendor?: string;
          architecture?: string;
          device?: string;
          description?: string;
          isFallbackAdapter?: boolean;
        };
        isFallbackAdapter?: boolean;
      } | null>;
    };
  }).gpu;
  if (!gpu) throw new Error("WebGPU is required. Use a current browser with WebGPU enabled.");
  const adapter = await gpu.requestAdapter({ powerPreference: "high-performance" });
  if (!adapter) throw new Error("The browser did not provide a WebGPU adapter.");
  const info = adapter.info;
  return {
    backend: "WebGPU",
    vendor: info?.vendor || "",
    architecture: info?.architecture || "",
    device: info?.device || "",
    description: info?.description || "",
    fallback: adapter.isFallbackAdapter ?? info?.isFallbackAdapter ?? false,
  };
}

async function start() {
  try {
    recordStartup();
    const [gpu, manifestResponse] = await Promise.all([
      graphicsPreflight(),
      fetch("/sim/manifest.json", { cache: "no-cache" }),
    ]);
    browserGlobals.__wsorbGPU = gpu;
    startupPhase = "downloading_engine";
    recordStartup();
    if (!manifestResponse.ok) throw new Error(`Engine manifest returned HTTP ${manifestResponse.status}`);
    const metadata = engineManifest(await manifestResponse.json());
    preloadPlanetaryMaps();
    const moduleUrl = `/sim/wsorb_bevy.js?v=${metadata.sha256}`;
    const [module, verified] = await Promise.all([
      import(moduleUrl),
      loadVerifiedEngine(`/sim/wsorb_bevy_bg.wasm?v=${metadata.sha256}`, metadata, (loaded, total) => {
        progress.max = total;
        progress.value = loaded;
        progressText.textContent = `Loading engine: ${megabytes(loaded)} of ${megabytes(total)}. Compiling while downloading.`;
      }),
    ]);
    engineLoadTimings = verified.timings;
    engineBuild = { bytes: metadata.bytes, sha256: metadata.sha256 };
    progress.removeAttribute("value");
    statusText.textContent = "Starting Bevy 0.20";
    progressText.textContent = `Verified ${megabytes(metadata.bytes)}. Initializing WebAssembly and WebGPU pipelines.`;
    startupPhase = "initializing_webassembly";
    recordStartup();
    const loaded = module as Engine;
    const initializing = performance.now();
    await loaded.default({ module_or_path: verified.module });
    wasmInstantiationMs = performance.now() - initializing;
    engine = loaded;
    clearMovementKeys();
    engine.set_viewport_input(false, false);
    startupPhase = "starting_renderer";
    recordStartup();
    engine.start_simulation("#simulation-canvas");
    await waitForRenderer();
    if (failed) return;
    ready = true;
    restoreBindings();
    statusText.textContent = "3D ready";
    boot.hidden = true;
    publishPlatform("WebGPU · Lander ready on Phobos. P runs the mission; H opens controls.");
    // Loading may finish after the user has moved to another tab or HTML control.
    if (document.hasFocus() && !document.hidden && pageBackgroundFocused()) canvas.focus({ preventScroll: true });
    inputState();
  } catch (error) {
    if (!failed) fail(errorMessage(error));
  }
}

void start();
