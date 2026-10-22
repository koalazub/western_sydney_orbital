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
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const canvas = byId<HTMLCanvasElement>("simulation-canvas");
const viewport = byId<HTMLElement>("simulation-viewport");
const boot = byId<HTMLElement>("boot-screen");
const progress = byId<HTMLProgressElement>("wasm-progress");
let engine: Engine | undefined;
let ready = false;
let failed = false;
let platformMessage = "WebGPU · Click the scene to activate camera controls";

function errorMessage(value: unknown) { return value instanceof Error ? value.message : String(value); }
function announce(message: string) { byId("viewport-announcement").textContent = message; }
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
new ResizeObserver(() => { if (ready) window.dispatchEvent(new Event("resize")); }).observe(viewport);
byId("retry-world").addEventListener("click", () => location.reload());

const megabytes = (bytes: number) => `${(bytes / 1048576).toFixed(2)} MB`;
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
    const gpu = (navigator as any).gpu;
    if (!gpu) throw new Error("WebGPU is required. Use a current browser with WebGPU enabled.");
    const adapter = await gpu.requestAdapter({ powerPreference: "high-performance" });
    if (!adapter) throw new Error("No WebGPU adapter is available. Enable graphics acceleration.");
    (globalThis as any).__wsorbGPU = { backend: "WebGPU", vendor: adapter.info?.vendor || "", architecture: adapter.info?.architecture || "", fallback: adapter.info?.isFallbackAdapter ?? false };
    const manifest = await fetch("/sim/manifest.json", { cache: "no-cache" });
    if (!manifest.ok) throw new Error(`Engine manifest returned HTTP ${manifest.status}`);
    const metadata = await manifest.json();
    const [module, bytes] = await Promise.all([import("/sim/wsorb_bevy.js"), download(`/sim/wsorb_bevy_bg.wasm?v=${metadata.sha256}`, metadata.bytes)]);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    if (Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("") !== metadata.sha256) throw new Error("Engine integrity check failed. Reload to get the current build.");
    progress.removeAttribute("value");
    byId("sim-status").textContent = "Starting Bevy 0.20";
    byId("wasm-progress-text").textContent = `Downloaded ${megabytes(bytes.byteLength)}. Compiling WebAssembly and WebGPU pipelines.`;
    engine = module as Engine;
    await engine.default({ module_or_path: bytes });
    engine.set_viewport_input(false, false);
    engine.start_simulation("#simulation-canvas");
    const deadline = performance.now() + 60_000;
    await new Promise<void>((resolve, reject) => {
      const check = () => {
        if (failed) { reject(new Error("Bevy reported a startup failure")); return; }
        try {
          const status = JSON.parse(engine!.renderer_status());
          if (status.error) { reject(new Error(status.error)); return; }
          if (status.ready) { resolve(); return; }
          byId("wasm-progress-text").textContent = `WebGPU: ${status.compiled_pipelines} pipelines ready, ${status.pending_pipelines} pending. Waiting for the first game frame.`;
          if (performance.now() > deadline) { reject(new Error("Bevy did not produce a ready game frame within 60 seconds")); return; }
          setTimeout(check, 100);
        } catch (error) { reject(error); }
      }; check();
    });
    if (failed) return;
    ready = true; byId("sim-status").textContent = "3D ready"; boot.hidden = true;
    publishPlatform("WebGPU · Lander ready on Phobos. Space runs the mission; H opens controls.");
    canvas.focus({ preventScroll: true }); inputState();
  } catch (error) { if (!failed) fail(errorMessage(error)); }
}
void start();
