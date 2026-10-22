// Browser responsibilities stop at loading and platform APIs. Camera movement,
// mission playback, telemetry, inspection and interaction are owned by Bevy.
type Engine = {
  default: (options: unknown) => Promise<unknown>;
  start_simulation: (selector: string) => void;
  renderer_status: () => string;
  camera_status: () => string;
  runtime_status: () => string;
  command: (json: string) => string;
  set_viewport_input: (active: boolean, locked: boolean) => void;
  platform_state: (fullscreen: boolean, maximized: boolean, message: string) => void;
};
type StartupStage = "initializing" | "preparing_view" | "drawing_world" | "presenting" | "waiting_for_gpu" | "ready" | "failed";
type RendererStatus = {
  ready: boolean;
  stage: StartupStage;
  rendered_frames: number;
  submitted_primary_frames: number;
  completed_primary_frame: boolean;
  primary_world_draws: number;
  primary_viewport: [number, number] | null;
  primary_target_size: [number, number] | null;
  adapter: { name: string; backend: string; device_type: string; driver: string } | null;
  compiled_pipelines: number;
  pending_pipelines: number;
  pending_pipeline_labels: string[];
  error: string | null;
};
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const canvas = byId<HTMLCanvasElement>("simulation-canvas");
const viewport = byId<HTMLElement>("simulation-viewport");
const boot = byId<HTMLElement>("boot-screen");
const progress = byId<HTMLProgressElement>("wasm-progress");
let engine: Engine | undefined;
let ready = false;
let failed = false;
let platformMessage = "WebGPU · Click the scene to activate camera controls";
let startupPhase = "checking_graphics";
let startupVisibleWait = 0;
let lastRendererStatus: RendererStatus | undefined;

function errorMessage(value: unknown) { return value instanceof Error ? value.message : String(value); }
function announce(message: string) { byId("viewport-announcement").textContent = message; }
function recordStartup(error?: string) {
  const rect = canvas.getBoundingClientRect();
  // This stays in diagnostics, not an HTML telemetry panel. The preflight
  // adapter is not necessarily the adapter Bevy actually selects.
  (globalThis as any).__wsorbStartup = {
    stage: failed ? "failed" : lastRendererStatus?.stage ?? startupPhase,
    renderer: lastRendererStatus ?? null,
    gpu_preflight: (globalThis as any).__wsorbGPU ?? null,
    browser: navigator.userAgent,
    secure_context: isSecureContext,
    visibility: document.visibilityState,
    elapsed_visible_ms: Math.round(startupVisibleWait),
    canvas: { width: canvas.width, height: canvas.height, css_width: rect.width, css_height: rect.height, device_pixel_ratio: devicePixelRatio },
    error: error ?? null,
  };
}
function publishPlatform(message = platformMessage) {
  platformMessage = message;
  engine?.platform_state(!!document.fullscreenElement, !viewport.classList.contains("windowed"), message);
}
function inputState() {
  const locked = document.pointerLockElement === canvas;
  const active = ready && document.hasFocus() && (document.activeElement === canvas || locked);
  engine?.set_viewport_input(active, locked);
}
function fail(message: string) {
  ready = false; failed = true;
  recordStartup(message);
  boot.hidden = false;
  byId("sim-status").textContent = "3D unavailable";
  byId("world-message").textContent = `${message} Check WebGPU support and graphics acceleration, then reload.`;
  byId("wasm-progress-text").textContent = "Simulation could not start.";
  progress.removeAttribute("value");
  byId("retry-world").hidden = false;
  inputState();
}
function maximize() {
  viewport.classList.toggle("windowed");
  publishPlatform(viewport.classList.contains("windowed") ? "Windowed view" : "Maximized view");
  canvas.focus({ preventScroll: true });
  window.dispatchEvent(new Event("resize"));
}
async function fullscreen() {
  const activation = navigator.userActivation?.isActive;
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else if (viewport.requestFullscreen) await viewport.requestFullscreen({ navigationUI: "hide" });
    else throw new Error("This browser does not expose fullscreen");
    canvas.focus({ preventScroll: true });
    publishPlatform(document.fullscreenElement ? "Fullscreen · Escape exits" : "Fullscreen closed");
  } catch (error) {
    (globalThis as any).__wsorbFullscreenFailure = { activation, name: error instanceof DOMException ? error.name : "Error", message: errorMessage(error) };
    viewport.classList.remove("windowed");
    publishPlatform(`Fullscreen unavailable: ${errorMessage(error)}. The game is maximized within this window.`);
    announce(platformMessage);
  }
}
async function captureMouse() {
  try { await canvas.requestPointerLock(); publishPlatform("Mouse captured · Escape releases it"); }
  catch { publishPlatform("Mouse capture unavailable. Drag to look instead."); announce(platformMessage); }
}
function releaseMouse() {
  if (document.pointerLockElement) document.exitPointerLock();
  canvas.blur(); inputState(); publishPlatform("Mouse released · Click the scene to resume camera controls");
}
function exportFlight() {
  if (!engine) return;
  const result = JSON.parse(engine.command(JSON.stringify({ op: "observe" })));
  if (!result.ok) { publishPlatform("Could not export the current mission"); return; }
  const record = { exported_at: new Date().toISOString(), state: result.state,
    camera: JSON.parse(engine.camera_status()), runtime: JSON.parse(engine.runtime_status()),
    model_limitations: result.state.model };
  const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = `phobos-flight-${result.state.seed}.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  publishPlatform("Flight state exported");
}
(globalThis as any).__wsorbPlatformAction = (action: string) => {
  // Fullscreen/pointer lock keep the original input's transient user activation.
  if (action === "fullscreen") void fullscreen();
  else if (action === "maximize") maximize();
  else if (action === "capture") void captureMouse();
  else if (action === "release") releaseMouse();
  else if (action === "export") queueMicrotask(exportFlight);
  else if (action === "notebook") window.open("https://notebooklm.google.com/", "_blank", "noopener,noreferrer");
};
(globalThis as any).__wsorbEngineError = (message: string) => fail(message);
canvas.addEventListener("pointerdown", () => { if (ready) { canvas.focus({ preventScroll: true }); inputState(); } });
canvas.addEventListener("focus", inputState); canvas.addEventListener("blur", inputState);
canvas.addEventListener("contextmenu", event => event.preventDefault());
canvas.addEventListener("wheel", event => { if (ready && document.activeElement === canvas && !event.ctrlKey && !event.metaKey) event.preventDefault(); }, { passive: false });
window.addEventListener("blur", inputState); window.addEventListener("focus", inputState);
document.addEventListener("visibilitychange", inputState);
document.addEventListener("pointerlockchange", () => { inputState(); publishPlatform(document.pointerLockElement === canvas ? "Mouse captured · Escape releases it" : "Mouse released"); });
document.addEventListener("fullscreenchange", () => { publishPlatform(document.fullscreenElement ? "Fullscreen · Escape exits" : "Fullscreen closed"); inputState(); window.dispatchEvent(new Event("resize")); });
window.addEventListener("keydown", event => {
  if (ready && document.activeElement === canvas && !event.ctrlKey && !event.metaKey && ["Tab", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.code)) event.preventDefault();
});
window.addEventListener("error", event => { if (/wasm|wsorb_bevy/.test(`${event.filename} ${event.error?.stack || ""}`)) fail(`The simulation stopped: ${event.message}`); });
window.addEventListener("unhandledrejection", event => { if (/wasm|wsorb_bevy/.test(String(event.reason?.stack || ""))) fail(`The simulation stopped: ${errorMessage(event.reason)}`); });
new ResizeObserver(() => { if (engine && !failed) window.dispatchEvent(new Event("resize")); }).observe(viewport);
byId("retry-world").addEventListener("click", () => location.reload());

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
  let furthestStage = 0, lastProgress = 0, lastTick = performance.now();
  let wasVisible = !document.hidden;
  await new Promise<void>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let settled = false;
    const finish = (error?: Error) => {
      settled = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", check);
      if (error) reject(error); else resolve();
    };
    const check = () => {
      if (settled) return;
      clearTimeout(timer);
      const now = performance.now();
      if (wasVisible) startupVisibleWait += now - lastTick;
      lastTick = now; wasVisible = !document.hidden;
      if (failed) { finish(new Error("Bevy reported a startup failure")); return; }
      try {
        const status = JSON.parse(engine!.renderer_status()) as RendererStatus;
        lastRendererStatus = status;
        recordStartup();
        if (status.error) { finish(new Error(status.error)); return; }
        if (status.ready) { finish(); return; }
        const stage = status.stage ?? "initializing";
        const milestone = stages.indexOf(stage);
        if (milestone > furthestStage) {
          furthestStage = milestone;
          lastProgress = startupVisibleWait;
        }
        const description = startupDescriptions[stage] ?? startupDescriptions.initializing;
        byId("wasm-progress-text").textContent = document.hidden
          ? "Startup paused while this tab is hidden. Return to the tab to continue."
          : `WebGPU: ${description}. ${status.compiled_pipelines} pipelines ready, ${status.pending_pipelines} pending.`;
        if (!document.hidden && startupVisibleWait - lastProgress > 60_000) {
          const size = status.primary_target_size?.join("x") ?? `${canvas.width}x${canvas.height}`;
          const adapter = status.adapter ? status.adapter.name || `${status.adapter.backend}/${status.adapter.device_type}` : "not initialized";
          finish(new Error(`WebGPU startup stalled while ${description}. No render milestone advanced for 60 seconds in the visible tab. ${status.rendered_frames} render updates, ${status.submitted_primary_frames ?? 0} game frames submitted; framebuffer ${size}; GPU ${adapter}.`));
          return;
        }
        timer = setTimeout(check, 100);
      } catch (error) { finish(new Error(errorMessage(error))); }
    };
    document.addEventListener("visibilitychange", check);
    check();
  });
}
async function download(url: string, total: number): Promise<Uint8Array> {
  let response: Response;
  try { response = await fetch(url); } catch { throw new Error("Engine download failed. Check your connection."); }
  if (!response.ok || !response.body) throw new Error(`Engine download returned HTTP ${response.status}`);
  const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let loaded = 0, lastReport = 0;
  progress.max = total; progress.value = 0;
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    loaded += value.byteLength;
    if (loaded > 64 * 1048576) { await reader.cancel(); throw new Error("Engine download exceeds the 64 MB limit"); }
    chunks.push(value);
    if (performance.now() - lastReport > 80) {
      progress.value = loaded;
      byId("wasm-progress-text").textContent = `Downloading engine: ${megabytes(loaded)} of ${megabytes(total)}`;
      lastReport = performance.now();
    }
  }
  if (loaded !== total) throw new Error("Incomplete engine download. Reload to get the current build.");
  const bytes = new Uint8Array(loaded); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}
async function start() {
  try {
    recordStartup();
    const gpu = (navigator as any).gpu;
    if (!gpu) throw new Error("WebGPU is required. Use a current browser with WebGPU enabled.");
    const adapter = await gpu.requestAdapter({ powerPreference: "high-performance" });
    if (!adapter) throw new Error("No WebGPU adapter is available. Enable graphics acceleration.");
    (globalThis as any).__wsorbGPU = { backend: "WebGPU", vendor: adapter.info?.vendor || "", architecture: adapter.info?.architecture || "", device: adapter.info?.device || "", description: adapter.info?.description || "", fallback: adapter.isFallbackAdapter ?? adapter.info?.isFallbackAdapter ?? false };
    startupPhase = "downloading_engine"; recordStartup();
    const manifest = await fetch("/sim/manifest.json", { cache: "no-cache" });
    if (!manifest.ok) throw new Error(`Engine manifest returned HTTP ${manifest.status}`);
    const metadata = await manifest.json();
    const [module, bytes] = await Promise.all([import("/sim/wsorb_bevy.js"), download(`/sim/wsorb_bevy_bg.wasm?v=${metadata.sha256}`, metadata.bytes)]);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    if (Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("") !== metadata.sha256) throw new Error("Engine integrity check failed. Reload to get the current build.");
    progress.removeAttribute("value");
    byId("sim-status").textContent = "Starting Bevy 0.20";
    byId("wasm-progress-text").textContent = `Downloaded ${megabytes(bytes.byteLength)}. Compiling WebAssembly and WebGPU pipelines.`;
    startupPhase = "compiling_webassembly"; recordStartup();
    engine = module as Engine;
    await engine.default({ module_or_path: bytes });
    engine.set_viewport_input(false, false);
    startupPhase = "starting_renderer"; recordStartup();
    engine.start_simulation("#simulation-canvas");
    await waitForRenderer();
    if (failed) return;
    ready = true; byId("sim-status").textContent = "3D ready"; boot.hidden = true;
    publishPlatform("WebGPU · Lander ready on Phobos. Space runs the mission; H opens controls.");
    canvas.focus({ preventScroll: true }); inputState();
  } catch (error) { if (!failed) fail(errorMessage(error)); }
}
void start();
