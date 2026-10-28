// Browser responsibilities stop at loading and platform APIs. Camera movement,
// mission playback, telemetry, inspection and interaction are owned by Bevy.
import { engineManifest, loadVerifiedEngine, type EngineLoadTimings } from "./engine-loader.ts";
import planetaryAssets from "./assets/asset-manifest.json";
import { createEngineCanvas, graphicsPreflight } from "./graphics.ts";
import { diagnoseStartupFailure, showStartupFailure } from "./startup-errors.ts";
import { connectRoom, type RoomEngine } from "./room-control.ts";
import { installDiagnostics } from "./diagnostics.ts";
const diagnostics = installDiagnostics();
type Engine = RoomEngine & {
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
  fullscreen_shortcut_allowed?: () => boolean;
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
let roomConnection = readRoomConnection();
const canvas = createEngineCanvas();
const viewport = byId<WebKitElement>("simulation-viewport");
const boot = byId<HTMLElement>("boot-screen");
const progress = byId<HTMLProgressElement>("wasm-progress");
const statusText = byId<HTMLElement>("sim-status");
const progressText = byId<HTMLElement>("wasm-progress-text");
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
    engine_build: {bytes: number; sha256: string} | null;
    canvas: {
      width: number;
      height: number;
      css_width: number;
      css_height: number;
      device_pixel_ratio: number;
      connected: boolean;
      surface: string | null;
      owner: string | null;
    };
    error: string | null;
  };
  __wsorbGPU?: GPUPreflight;
  __wsorbLocalApi?: { page_id?: string; status?: string };
  __wsorbFullscreenFailure?: {
    activation: boolean | undefined;
    name: string;
    message: string;
  } | null;
  __wsorbPlatformAction?: typeof platformAction;
  __wsorbEngineError?: typeof fail;
  __wsorbLocalApi?: { status: string; error: string | null; page_id?: string };
};

let engine: Engine | undefined;
let engineBuild: { bytes: number; sha256: string } | undefined;
let ready = false;
let roomSessionActive = false;
let failed = false;
let platformMessage = "WebGPU · Click the scene to activate ship and debug controls";
let startupPhase = "checking_graphics";
let startupVisibleWait = 0;
let lastRendererStatus: RendererStatus | undefined;
let lastHudStatus: unknown = null;
let engineLoadTimings: EngineLoadTimings | null = null;
let wasmInstantiationMs: number | null = null;
let fullscreenPending = false;
let fullscreenShortcutInputSample: number | null | undefined;
let fullscreenShortcutInputSequence = 0;
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
let diagnosticsTimer: ReturnType<typeof setTimeout> | undefined;

function scheduleDiagnostics() {
  clearTimeout(diagnosticsTimer);
  if (!diagnostics.enabled || failed) return;
  diagnosticsTimer = setTimeout(() => {
    if (diagnostics.enabled && ready && !failed && engine) {
      diagnostics.observeNative(engine, () => ({
        local_page_id: browserGlobals.__wsorbLocalApi?.page_id,
        hidden: document.hidden, visibility: document.visibilityState, focused: document.hasFocus(),
        startup_stage: "ready",
      }));
    }
    scheduleDiagnostics();
  }, 1000);
}
diagnostics.onEnabledChange(scheduleDiagnostics);

function readRoomConnection(): { origin: string; id: string; key: string } | null {
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (!fragment.has("room") && !fragment.has("key")) return null;
  // The capability belongs only to this page's memory, not browser history,
  // HTTP requests, diagnostics, local storage or notebook navigation.
  history.replaceState(history.state, "", `${location.pathname}${location.search}`);
  const id = fragment.get("room") ?? "";
  const key = fragment.get("key") ?? "";
  if (location.protocol !== "https:" || fragment.getAll("room").length !== 1 || fragment.getAll("key").length !== 1
    || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(id)
    || !/^[A-Za-z0-9_-]{43}$/.test(key)) return { origin: location.origin, id: "", key: "" };
  return { origin: location.origin, id, key };
}

function offerPythonConnection() {
  if (!roomConnection || !engine || !engineBuild) return;
  const connection = roomConnection;
  const panel = byId("python-room");
  const text = byId("python-room-status");
  const disconnect = byId<HTMLButtonElement>("disconnect-python");
  const permission = byId<HTMLDialogElement>("python-permission");
  panel.hidden = false;
  if (!connection.id || !connection.key) {
    text.textContent = "This Python room link is invalid. Open the private HTTPS link printed by wsorb room create.";
    return;
  }
  if (permission.open || roomSessionActive) return;
  text.textContent = "Python is not connected. Choose whether to allow this private room.";
  permission.addEventListener("close", () => {
    canvas.focus({ preventScroll: true });
    if (permission.returnValue !== "allow") {
      text.textContent = "Python connection declined. The game stays under manual control.";
      return;
    }
    const loaded = engine;
    const build = engineBuild;
    if (!loaded || !build || failed) {
      text.textContent = "The game is no longer ready. No Python connection was opened.";
      return;
    }
    const sessionEvents = new AbortController();
    roomSessionActive = true;
    try {
      const session = connectRoom(connection, loaded, build, (status, error) => {
        const messages: Record<string, string> = {
          connecting: "Connecting to your private Python room.",
          claiming: "Requesting this room's browser ownership. An existing owner will not be replaced.",
          connected: "Python room connected. Start wsorb checkpoint with your private room file to fly. Shift+Esc disconnects.",
          conflict: "Another browser owns this room. It was not replaced.",
          error: "Python room stopped. No call is retried.",
          indeterminate: "Python room stopped. A command's outcome is unknown. Inspect the actual flight before reconnecting.",
          disconnected: "Python room disconnected. No commands are retried. Native thrust expires on its original simulation-time lease.",
        };
        text.textContent = messages[status] ?? "Python room stopped. Inspect the game before reconnecting.";
        if (error) text.textContent += ` ${error}`;
        const ended = ["disconnected", "error", "conflict", "indeterminate"].includes(status);
        disconnect.hidden = ended;
        roomSessionActive = !ended;
        if (ended) sessionEvents.abort();
      }, () => ready && !failed && engine === loaded && !document.hidden && JSON.parse(loaded.renderer_status()).ready === true);
      const disconnectRoom = () => {
        session.disconnect();
        disconnect.hidden = true;
        canvas.focus({ preventScroll: true });
      };
      disconnect.addEventListener("click", disconnectRoom, { once: true, signal: sessionEvents.signal });
      // Tab belongs to the native HUD while the canvas has focus. Keep the
      // platform disconnect available without trapping keyboard-only users.
      window.addEventListener("keydown", event => {
        if (disconnect.hidden || event.repeat || event.code !== "Escape" || !event.shiftKey
          || event.ctrlKey || event.metaKey || event.altKey) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        disconnectRoom();
      }, { capture: true, signal: sessionEvents.signal });
      addEventListener("pagehide", () => session.disconnect(), { once: true, signal: sessionEvents.signal });
    } catch {
      sessionEvents.abort();
      roomSessionActive = false;
      text.textContent = "The private Python room could not connect. No flight command was sent.";
      disconnect.hidden = true;
    }
  }, { once: true });
  // Escape must not reuse a previous dialog's "allow" return value.
  permission.returnValue = "";
  permission.showModal();
}

addEventListener("hashchange", () => {
  const requested = readRoomConnection();
  if (!requested) return;
  const permission = byId<HTMLDialogElement>("python-permission");
  if (roomSessionActive || permission.open) {
    byId("python-room").hidden = false;
    byId("python-room-status").textContent = "A Python room is already connected or awaiting your choice. Finish that connection before opening another room. No existing flight was changed.";
    return;
  }
  // Pairing an already-loaded game must not initialize another WASM instance
  // or reset its physics. A fresh explicit permission choice is still required.
  roomConnection = requested;
  if (ready && !failed) offerPythonConnection();
});

function monitorLocalPythonConnection() {
  const panel = byId("local-python-connection");
  const text = byId("local-python-status");
  let previous = "";
  const update = () => {
    const connection = browserGlobals.__wsorbLocalApi;
    if (!connection) return;
    const signature = `${connection.status}\n${connection.error ?? ""}\n${connection.page_id ?? ""}\n${diagnostics.enabled}`;
    if (signature === previous) return;
    previous = signature;
    panel.hidden = false;
    const otherOwner = connection.error?.includes("Another browser page holds the local simulation lease");
    const messages: Record<string, string> = {
      waiting_for_engine: "Local Python API is waiting for this game's renderer.",
      connected: "Local Python API is connected to this tab. This does not mean a controller has started.",
      disconnected: "Local Python API is disconnected from this tab.",
      error: "Local Python API stopped. Inspect its error before sending more flight commands.",
    };
    const message = otherOwner
      ? "The local Python API is attached to another tab. This tab is not receiving its calls. Switch to the connected game; do not reset either flight."
      : messages[connection.status] ?? "Local Python API is not connected to this tab.";
    text.textContent = `${message} ${location.origin}`;
    if (diagnostics.enabled && connection.page_id) text.textContent += ` · page ${connection.page_id.slice(0, 8)}`;
    if (connection.error && !otherOwner) text.textContent += ` ${connection.error}`;
    if (otherOwner || connection.status === "error") announce(message);
  };
  // Older already-running local backends serve a cached bridge without custom
  // events. Reading its token-free status also makes those conflicts visible.
  update();
  const timer = setInterval(update, 250);
  addEventListener("pagehide", () => clearInterval(timer), { once: true });
}

function requestInitialFollowView() {
  if (!engine || new URLSearchParams(location.search).get("follow") !== "lander") return;
  try {
    const result = JSON.parse(engine.control(JSON.stringify({
      op: "view", mode: "orbit", focus: "Shuttle", zoom: 1,
    })));
    publishPlatform(result.ok === true
      ? "Follow lander view requested. Python connection status identifies the controlled tab."
      : "Follow view could not be requested. Press F in the game to follow the lander.");
  } catch {
    publishPlatform("Follow view could not be requested. Press F in the game to follow the lander.");
  }
}

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
    hud: lastHudStatus,
    gpu_preflight: browserGlobals.__wsorbGPU ?? null,
    browser: navigator.userAgent,
    secure_context: isSecureContext,
    visibility: document.visibilityState,
    elapsed_visible_ms: Math.round(startupVisibleWait),
    engine_load: engineLoadTimings,
    wasm_instantiation_ms: wasmInstantiationMs,
    engine_build: engineBuild ?? null,
    canvas: {
      width: canvas.width,
      height: canvas.height,
      css_width: rect.width,
      css_height: rect.height,
      device_pixel_ratio: devicePixelRatio,
      connected: canvas.isConnected,
      surface: canvas.getAttribute("data-wsorb-surface"),
      owner: canvas.getAttribute("data-wsorb-renderer-owner"),
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

function fail(cause: unknown) {
  if (failed) return;
  ready = false;
  failed = true;
  clearTimeout(diagnosticsTimer);
  diagnostics.record("engine.failed", { startup_stage: "failed" });
  // A panic callback can arrive while Rust is unwinding or already trapped.
  // Never call back into that instance to assemble an error report or input reset.
  engine = undefined;
  recordStartup(diagnoseStartupFailure(cause, navigator.userAgent).detail);
  showStartupFailure(cause, browserGlobals.__wsorbStartup);
  movementHeld.clear();
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

function nativeHudInputSample(): number | null {
  if (!engine) return null;
  try {
    const { samples } = JSON.parse(engine.hud_status());
    return Number.isSafeInteger(samples) && samples > 0 ? samples : null;
  } catch {
    return null;
  }
}

function rememberNativeShortcutInput(event: KeyboardEvent | PointerEvent) {
  if (!ready || event.target !== canvas) return;
  if (event instanceof KeyboardEvent && (event.code === "KeyX" || event.repeat)) return;
  // Winit can batch R, Tab and pointer focus changes until a redraw. Do not use
  // the preceding frame's permission before that native input has been handled.
  const sequence = ++fullscreenShortcutInputSequence;
  fullscreenShortcutInputSample = null;
  queueMicrotask(() => {
    // Sample after this event's handlers and any synchronous focus subevents.
    // Only a later native frame may clear the fence, never a fullscreen retry.
    if (sequence === fullscreenShortcutInputSequence) fullscreenShortcutInputSample = nativeHudInputSample();
  });
}

function fullscreenShortcutAllowed() {
  // Older or unavailable engines cannot authorize this shortcut. The native
  // getter returns a copied bool and releases Rust borrows before any DOM API.
  if (engine?.fullscreen_shortcut_allowed?.() !== true) return false;
  if (fullscreenShortcutInputSample === undefined) return true;
  const sample = nativeHudInputSample();
  if (sample === null || fullscreenShortcutInputSample === null
    || sample <= fullscreenShortcutInputSample) return false;
  fullscreenShortcutInputSample = undefined;
  return true;
}

function platformKey(event: KeyboardEvent) {
  if (!ready || !engine || event.isComposing || event.ctrlKey || event.metaKey || event.altKey
    || bindingsCapturing || document.activeElement !== canvas) return;
  let action: TrustedAction | null = event.code === "KeyX" && fullscreenShortcutAllowed() ? "fullscreen" : null;
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
// Record pending native input before Winit or movement-key propagation guards.
// This only suppresses a stale X press; it never queues a fullscreen request.
for (const name of ["pointerdown", "pointerup", "pointercancel"] as const) {
  window.addEventListener(name, rememberNativeShortcutInput, { capture: true });
}
window.addEventListener("keydown", rememberNativeShortcutInput, { capture: true });
window.addEventListener("keyup", rememberNativeShortcutInput, { capture: true });
window.addEventListener("keydown", event => movementKey(event, true), { capture: true });
window.addEventListener("keyup", event => movementKey(event, false), { capture: true });
window.addEventListener("keydown", platformKey, { capture: true });
window.addEventListener("keydown", preventViewportScroll);
window.addEventListener("error", windowError);
window.addEventListener("unhandledrejection", unhandledRejection);
new ResizeObserver(() => {
  if (engine && !failed) window.dispatchEvent(new Event("resize"));
}).observe(viewport);

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

async function start() {
  try {
    recordStartup();
    const [gpu, manifestResponse] = await Promise.all([
      graphicsPreflight(canvas),
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
    // Bind only the module the app already initialized and started. Diagnostics
    // never call default(), start_simulation(), commands or camera acquisition.
    diagnostics.bindEngine(loaded, () => ready && !failed && engine === loaded);
    diagnostics.record("engine.ready", { startup_stage: "ready" });
    scheduleDiagnostics();
    lastHudStatus = JSON.parse(engine.hud_status());
    restoreBindings();
    statusText.textContent = "3D ready";
    boot.hidden = true;
    publishPlatform("WebGPU · Lander ready on Phobos. P runs the mission; H opens controls.");
    // Loading may finish after the user has moved to another tab or HTML control.
    if (document.hasFocus() && !document.hidden && pageBackgroundFocused()) canvas.focus({ preventScroll: true });
    inputState();
    requestInitialFollowView();
    monitorLocalPythonConnection();
    offerPythonConnection();
  } catch (error) {
    fail(error);
  }
}

void start();
