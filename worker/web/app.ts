import { basicSetup } from "codemirror";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { python } from "@codemirror/lang-python";
import { yCollab } from "y-codemirror.next";
import { Awareness } from "y-protocols/awareness";
import * as Y from "yjs";

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const button = (id: string) => $<HTMLButtonElement>(id);
const input = (id: string) => $<HTMLInputElement>(id);
const initialCode = `# Browser practice controller. Uses state estimates and noisy radar only.
# Port the guidance logic into STP's Controller.py for official runs.
import math

def body_vector(vector, quaternion):
    # Inverse quaternion rotation: Phobos-frame vector to shuttle body frame.
    x, y, z, w = quaternion
    q = [-x, -y, -z]
    cross = lambda a, b: [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
    t = [2*v for v in cross(q, vector)]
    c = cross(q, t)
    return [vector[i] + w*t[i] + c[i] for i in range(3)]

state = await sim.reset(seed=100)
print("Starting sensor-guided approach. This is a practice controller, not an optimized submission.")
for tick in range(180):
    state = await sim.observe()
    if state["mission"]["status"] not in ("running", "in_progress", "ready"):
        break
    shuttle, target = state["shuttle"], state["target"]
    delta = [target["position"][i] - shuttle["position"][i] for i in range(3)]
    # Position/velocity feedback, with a short-range radar repulsion term.
    accel = [0.0012*delta[i] - 0.07*shuttle["velocity"][i] for i in range(3)]
    for measurement in (await sim.radar(20)):
        relative = measurement[0]
        clearance = measurement[1][0]
        radius = max(math.sqrt(sum(x*x for x in relative)), 1)
        if clearance < 65:
            weight = 0.7 * max(0, 1 - clearance/65)
            accel = [accel[i] - weight*relative[i]/radius for i in range(3)]
    magnitude = math.sqrt(sum(x*x for x in accel))
    if magnitude > 0.5:
        accel = [x*0.5/magnitude for x in accel]
    await sim.set_command(body_vector(accel, shuttle["rotation"]))
    state = await sim.step(1)
    if tick % 10 == 0:
        m = state["mission"]
        print(f"t={state['time_s']:.0f}s  range={m['target_distance_m']:.1f}m  speed={m['relative_speed_mps']:.2f}m/s  fuel={m['fuel_used_kg']:.3f}kg  damage={m['damage']:.0f}J")
await sim.stop()
print("Controller complete:", state["mission"]["status"])
`;

type Entity = { id: string; name: string; kind: string; position: number[]; velocity: number[]; rotation: number[]; mass_kg: number; radius_m: number; thrust_n: number[]; status: string; angular_velocity_rad_s?: number[]; commanded_accel_mps2?: number[] };
type Snapshot = { model: string; time_s: number; seed: number; frame: string; assets: Entity[]; events: { t_s: number; asset: string; message: string }[]; collisions: number; mission: { shuttle_id: string; target_id: string; target_distance_m: number; relative_speed_mps: number; fuel_used_kg: number; damage: number; status: string; score?: number | null } };
type FlightSample = { time_s: number; mission: Snapshot["mission"]; spacecraft: Entity[] };
type Engine = { default: (options: unknown) => Promise<unknown>; start_simulation: (selector: string) => void; command: (json: string) => string; observation: () => string; renderer_status: () => string };

const fragment = new URLSearchParams(location.hash.slice(1));
let roomId = fragment.get("room"), roomKey = fragment.get("key");
let socket: WebSocket | undefined;
let clientId: string | null = null, hostId: string | null = null, isHost = false, seedNewRoom = false, restoreFailed = false;
let world: Engine | undefined, worldReady = false, engineFailed = false, worldState: Snapshot | undefined;
let selectedAsset = "Shuttle", entitiesSignature = "", sampleSeed: number | undefined;
let flightSamples: FlightSample[] = [], lastPublished = 0, publishTimer: number | undefined;
let pythonWorker: Worker | undefined, pythonReady = false, pythonActive = false, queuedRun = false, executionTimer: number | undefined;
let commandQueue: Promise<unknown> = Promise.resolve(), commandBusy = false;
let cameraMode = "mission", cameraZoom = 1;
const pendingCommands = new Map<string, { resolve: (value: Snapshot) => void; reject: (error: Error) => void; timer: number }>();
const doc = new Y.Doc(), strategy = doc.getText("strategy"), awareness = new Awareness(doc);

if (!roomId) strategy.insert(0, initialCode);
const editor = new EditorView({ parent: $("editor"), state: EditorState.create({
  doc: roomId ? "" : initialCode,
  extensions: [basicSetup, python(), EditorView.lineWrapping, EditorView.contentAttributes.of({ "aria-label": "Shared Python strategy" }), yCollab(strategy, awareness)],
}) });

function errorText(error: unknown) { return error instanceof Error ? error.message : String(error); }
function log(text: string) {
  const output = $("console");
  if (output.textContent === "Run a strategy to see its output.") output.textContent = "";
  output.textContent = `${output.textContent}${text}\n`.slice(-32_000);
  output.scrollTop = output.scrollHeight;
}
function showError(message: string) { $("world-message").textContent = message; $("world-message").classList.add("error"); }
function clearError() { if (!engineFailed && !restoreFailed) { $("world-message").textContent = ""; $("world-message").classList.remove("error"); } }
function canControl() { return worldReady && !restoreFailed && (!roomId || (isHost && socket?.readyState === WebSocket.OPEN)); }
function refreshControls() {
  const busy = pythonActive || queuedRun || commandBusy;
  for (const id of ["run-strategy", "step-button", "reset-button"]) button(id).disabled = !canControl() || busy;
  if (worldState?.mission.status !== "running") button("step-button").disabled = true;
  for (const id of ["apply-thrust", "cancel-thrust"]) button(id).disabled = !canControl() || busy || worldState?.mission.status !== "running" || selectedAsset !== (worldState?.mission.shuttle_id || "Shuttle");
  button("stop-python").disabled = !pythonActive && !queuedRun;
  button("export-button").disabled = !worldReady || !worldState;
  for (const id of ["view-mission", "view-overview", "view-follow", "zoom-in", "zoom-out"]) button(id).disabled = !worldReady;
  button("claim-host").hidden = !roomId;
  button("claim-host").disabled = !worldReady || socket?.readyState !== WebSocket.OPEN;
  $("control-help").textContent = !canControl() && roomId ? "Claim the simulation host to send commands. Each viewer can change their own camera." : selectedAsset !== (worldState?.mission.shuttle_id || "Shuttle") ? "Select the shuttle to set its acceleration. Other entities are read-only." : "The command stays active until changed or zeroed. Simulation time advances only through Step or Python.";
}

function encodeBase64(bytes: Uint8Array) {
  let value = "";
  for (let i = 0; i < bytes.length; i += 0x8000) value += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(value);
}
function send(message: unknown) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message)); }
function publishState(force = false) {
  if (!roomId || !isHost || !worldState || socket?.readyState !== WebSocket.OPEN) return;
  if (publishTimer) clearTimeout(publishTimer);
  if (!force && (performance.now() - lastPublished < 1000 || socket.bufferedAmount > 1024 * 1024)) {
    publishTimer = window.setTimeout(() => publishState(true), 1000);
    return;
  }
  lastPublished = performance.now();
  send({ type: "state", state: worldState });
}
function connectRoom(id: string, key: string) {
  if (pythonActive || queuedRun) {
    stopPython(false);
    if (canControl()) { updateState(localCommand({ op: "stop" })); publishState(true); }
  }
  if (publishTimer) clearTimeout(publishTimer);
  if (socket) { socket.onmessage = socket.onclose = socket.onerror = socket.onopen = null; socket.close(); }
  for (const pending of pendingCommands.values()) { clearTimeout(pending.timer); pending.reject(new Error("Switched rooms")); }
  pendingCommands.clear();
  clientId = hostId = null; isHost = false; restoreFailed = false; clearError();
  roomId = id; roomKey = key;
  history.replaceState(null, "", `#room=${encodeURIComponent(id)}&key=${encodeURIComponent(key)}`);
  socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/rooms/${encodeURIComponent(id)}/ws`, ["wsorb", `auth.${key}`]);
  socket.onopen = () => { $("collab-state").textContent = "Shared room"; $("room-status").textContent = "Shared room connected"; refreshControls(); };
  socket.onclose = () => {
    isHost = false; stopPython(false);
    $("collab-state").textContent = "Disconnected";
    $("room-status").textContent = "Room disconnected. Reload to reconnect. Unsynced edits remain in this browser.";
    refreshControls();
  };
  socket.onerror = () => { $("room-status").textContent = "Room unavailable. Check the private link and reload."; };
  socket.onmessage = ({ data }) => {
    try {
      const message = JSON.parse(data);
      if (message.type === "welcome") {
        clientId = message.clientId; hostId = message.hostId;
        if (message.document) Y.applyUpdate(doc, Uint8Array.from(atob(message.document), c => c.charCodeAt(0)), "remote");
        if (seedNewRoom) {
          if (!strategy.length) strategy.insert(0, initialCode);
          send({ type: "document", update: encodeBase64(Y.encodeStateAsUpdate(doc)) });
          send({ type: "claim-host" }); send({ type: "notebook", url: savedNotebook });
        }
        seedNewRoom = false;
        if (message.state) restoreState(message.state);
        updateHost();
        setNotebook(message.notebookUrl || savedNotebook);
      } else if (message.type === "document") {
        Y.applyUpdate(doc, Uint8Array.from(atob(message.update), c => c.charCodeAt(0)), "remote");
      } else if (message.type === "host") { hostId = message.hostId; updateHost(); }
      else if (message.type === "state" && !isHost) restoreState(message.state);
      else if (message.type === "execute" && isHost) void executeHostCommand(message);
      else if (message.type === "result") {
        const pending = pendingCommands.get(message.id);
        if (pending) { clearTimeout(pending.timer); pendingCommands.delete(message.id); message.ok ? pending.resolve(message.state) : pending.reject(new Error(message.error || "Simulation command failed")); }
      } else if (message.type === "notebook") setNotebook(message.url);
      else if (message.type === "error") $("room-status").textContent = `${message.error}. Check the room connection and try again.`;
    } catch (error) { $("room-status").textContent = `Could not read room update. Reload to reconnect. ${errorText(error)}`; }
  };
  refreshControls();
}
doc.on("update", (update: Uint8Array, origin: unknown) => { if (origin !== "remote") send({ type: "document", update: encodeBase64(update) }); });
function updateHost() {
  const wasHost = isHost;
  isHost = !!clientId && hostId === clientId;
  button("claim-host").textContent = isHost ? "Release simulation host" : "Claim simulation host";
  if (wasHost && !isHost) stopPython(false);
  if (isHost && !wasHost && worldReady) {
    if (worldState) restoreState(worldState);
    publishState(true);
  }
  refreshControls();
}
button("claim-host").addEventListener("click", () => {
  if (!worldReady || socket?.readyState !== WebSocket.OPEN) return;
  if (pythonActive || queuedRun) stopPython();
  send({ type: isHost ? "release-host" : "claim-host" });
});

function localCommand(command: Record<string, unknown>): Snapshot {
  const response = JSON.parse(world!.command(JSON.stringify(command)));
  if (!response.ok) throw new Error(response.error || "Simulation command failed");
  return response.state;
}
function restoreState(state: Snapshot) {
  if (!state || !state.model || !Array.isArray(state.assets) || !state.mission?.shuttle_id) {
    restoreFailed = true;
    showError("This room contains an older rover mission. Its strategy has been preserved. Create a new room for the Phobos model.");
    refreshControls(); return;
  }
  if (!worldReady) { worldState = state; return; }
  try { const restored = localCommand({ op: "restore", state }); restoreFailed = false; updateState(restored); }
  catch (error) { restoreFailed = true; showError(`Saved mission could not be restored. Reload or create a new room. ${errorText(error)}`); }
  refreshControls();
}
async function callWorld(command: Record<string, unknown>, sensorOnly = false, publish = true, sourceWorker?: Worker): Promise<any> {
  const operation = async () => {
    if (sourceWorker && pythonWorker !== sourceWorker) throw new Error("Strategy stopped before this command executed.");
    if (!worldReady || !world) throw new Error("The 3D engine is unavailable. Reload the engine before sending commands.");
    if (restoreFailed) throw new Error("The saved mission could not be restored. Create a new Phobos room.");
    if (roomId && !isHost) {
      if (!hostId || socket?.readyState !== WebSocket.OPEN) throw new Error("Claim the simulation host before sending commands.");
      if (sensorOnly) throw new Error("Claim the simulation host to run Python in this browser.");
      const id = crypto.randomUUID();
      return new Promise<Snapshot>((resolve, reject) => {
        const timer = window.setTimeout(() => { pendingCommands.delete(id); reject(new Error("The host did not respond. Reconnect or claim the host role.")); }, 30_000);
        pendingCommands.set(id, { resolve, reject, timer }); send({ type: "command", id, command });
      });
    }
    const state = localCommand(command);
    updateState(state);
    if (publish && command.op !== "observe") publishState();
    // Give Bevy a browser frame before Python can request the next integration step.
    if (command.op === "step" || command.op === "reset") await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
    return sensorOnly ? JSON.parse(world.observation()) : state;
  };
  const result = commandQueue.then(operation, operation);
  commandQueue = result.catch(() => undefined);
  return result;
}
async function executeHostCommand(message: any) {
  try {
    if (pythonActive || queuedRun) throw new Error("A Python strategy is running. Stop it before sending external commands.");
    const state = await callWorld(message.command, false, false);
    send({ type: "result", id: message.id, ok: true, state });
  } catch (error) { send({ type: "result", id: message.id, ok: false, error: errorText(error) }); }
}

const fmt = (value: unknown, digits = 1) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "Not available";
const vector = (value: number[] | undefined, digits = 2) => value?.length === 3 ? value.map(x => fmt(x, digits)).join(", ") : "Not available";
const length = (value: number[]) => Math.hypot(...value);
function updateState(state: Snapshot) {
  if (!state || !Array.isArray(state.assets)) return;
  const previous = worldState;
  worldState = state;
  const mission = state.mission;
  $("time-value").textContent = `${fmt(state.time_s, 2)} s`;
  $("seed-value").textContent = String(state.seed);
  $("mission-value").textContent = mission.status;
  if (mission.status !== "running" && !engineFailed && !restoreFailed) {
    $("world-message").textContent = mission.status === "success" ? "Shuttle arrived within 50 m of the return ship. Export this flight or reset for another run." : "The shuttle exceeded the practice damage limit. Export this flight to inspect the impact, then reset to try again.";
    $("world-message").classList.remove("error");
  }
  $("distance-value").textContent = `${fmt(mission.target_distance_m)} m`;
  $("relative-speed-value").textContent = `${fmt(mission.relative_speed_mps, 2)} m/s`;
  $("fuel-value").textContent = `${fmt(mission.fuel_used_kg, 3)} kg`;
  $("damage-value").textContent = `${fmt(mission.damage, 0)} J`;
  $("score-value").textContent = mission.score == null ? "Run not finished" : fmt(mission.score, 2);
  $("collision-value").textContent = String(state.collisions);
  $("ship-count").textContent = String(state.assets.filter(a => a.kind === "ship" || a.kind === "target").length);
  $("asteroid-count").textContent = String(state.assets.filter(a => a.kind === "asteroid").length);
  $("asset-count").textContent = `${state.assets.length} entities`;
  $("frame-value").textContent = state.frame;
  $("model-description").textContent = state.model;
  if (!state.assets.some(a => a.id === selectedAsset)) selectedAsset = mission.shuttle_id || state.assets[0]?.id;
  const signature = `${state.seed}:${state.assets.map(a => a.id).join("|")}`;
  const select = $<HTMLSelectElement>("asset-list");
  if (signature !== entitiesSignature) {
    select.replaceChildren(...state.assets.map(asset => new Option(`${asset.name || asset.id} · ${asset.kind}`, asset.id)));
    entitiesSignature = signature;
  }
  select.disabled = false;
  select.value = selectedAsset;
  const selected = state.assets.find(a => a.id === selectedAsset);
  if (selected) {
    const values = [
      ["Entity", selected.name || selected.id], ["Position, m", vector(selected.position)],
      ["Velocity, m/s", vector(selected.velocity)], ["Speed", `${fmt(length(selected.velocity), 3)} m/s`],
      ["Rotation, quaternion xyzw", selected.rotation.map(x => fmt(x, 4)).join(", ")],
      ["Angular velocity, rad/s", vector(selected.angular_velocity_rad_s, 4)],
      ["Mass", `${fmt(selected.mass_kg, 1)} kg`], ["Collision radius", `${fmt(selected.radius_m, 2)} m`],
      ["Body command, m/s²", vector(selected.commanded_accel_mps2, 3)], ["Status", selected.status],
    ];
    $("selected-details").replaceChildren(...values.map(([label, value]) => { const item = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = label; dd.textContent = value; item.append(dt, dd); return item; }));
  }
  if (sampleSeed !== state.seed || (previous && state.time_s < previous.time_s)) { flightSamples = []; sampleSeed = state.seed; }
  if (!flightSamples.length || flightSamples.at(-1)!.time_s !== state.time_s) {
    flightSamples.push({ time_s: state.time_s, mission: structuredClone(mission), spacecraft: structuredClone(state.assets.filter(a => a.kind === "ship" || a.kind === "target")) });
    if (flightSamples.length > 4096) flightSamples.splice(0, 1);
    drawChart();
  } else {
    flightSamples[flightSamples.length - 1] = { time_s: state.time_s, mission: structuredClone(mission), spacecraft: structuredClone(state.assets.filter(a => a.kind === "ship" || a.kind === "target")) };
  }
  $("sample-count").textContent = `${flightSamples.length} samples`;
  const events = state.events.slice(-30);
  $("event-count").textContent = state.events.length > 30 ? `Latest 30 of ${state.events.length} retained` : `${state.events.length} retained`;
  $("event-list").replaceChildren(...(events.length ? events.map(event => { const li = document.createElement("li"); li.textContent = `${fmt(event.t_s, 1)} s · ${event.asset}: ${event.message}`; return li; }) : [Object.assign(document.createElement("li"), { textContent: "No mission events yet." })]));
  refreshControls();
}
$("asset-list").addEventListener("change", () => { selectedAsset = $<HTMLSelectElement>("asset-list").value; if (worldState) updateState(worldState); });
function drawChart() {
  const canvas = $<HTMLCanvasElement>("analysis-chart"), context = canvas.getContext("2d");
  if (!context) return;
  const width = Math.max(260, canvas.clientWidth), height = 220, scale = devicePixelRatio || 1;
  canvas.width = width * scale; canvas.height = height * scale;
  context.scale(scale, scale); context.clearRect(0, 0, width, height);
  context.font = "12px system-ui"; context.fillStyle = "#50585b";
  if (!flightSamples.length) { context.fillText("Run or step the simulation to record a flight.", 12, 30); return; }
  const left = 65, top = 20, right = width - 18, bottom = height - 35;
  const maxTime = Math.max(1, flightSamples.at(-1)!.time_s), maxDistance = Math.max(50, ...flightSamples.map(s => s.mission.target_distance_m));
  context.strokeStyle = "#b8bec1"; context.lineWidth = 1; context.beginPath(); context.moveTo(left, top); context.lineTo(left, bottom); context.lineTo(right, bottom); context.stroke();
  context.fillText(`${fmt(maxDistance, 0)} m`, 2, top + 5); context.fillText("0 m", 25, bottom + 4); context.fillText("0 s", left, bottom + 23); context.textAlign = "right"; context.fillText(`${fmt(maxTime, 1)} s`, right, bottom + 23); context.textAlign = "left";
  context.strokeStyle = "#164b79"; context.lineWidth = 2; context.beginPath();
  for (const [index, sample] of flightSamples.entries()) { const x = left + sample.time_s / maxTime * (right - left), y = bottom - sample.mission.target_distance_m / maxDistance * (bottom - top); index ? context.lineTo(x, y) : context.moveTo(x, y); }
  context.stroke();
}
new ResizeObserver(drawChart).observe($("analysis-chart"));

function updateProgress(id: "wasm" | "python", message: string, loaded?: number, total?: number) {
  const progress = $<HTMLProgressElement>(`${id}-progress`);
  if (total && loaded !== undefined && total >= loaded) { progress.max = total; progress.value = loaded; }
  else progress.removeAttribute("value");
  $(`${id}-progress-text`).textContent = message;
}
const bytesLabel = (value: number) => `${(value / 1024 / 1024).toFixed(2)} MB`;
async function downloadWasm(url: string, expectedBytes?: number): Promise<Uint8Array> {
  let response: Response;
  try { response = await fetch(url); } catch { throw new Error("The 3D engine download failed. Check your connection."); }
  if (!response.ok) throw new Error(`Engine download returned HTTP ${response.status}`);
  const total = expectedBytes || (Number(response.headers.get("Content-Length")) || undefined);
  const reader = response.body?.getReader();
  if (!reader) return new Uint8Array(await response.arrayBuffer());
  const chunks: Uint8Array[] = []; let loaded = 0, lastReport = 0;
  while (true) {
    const { done, value } = await reader.read(); if (done) break;
    loaded += value.byteLength;
    if (loaded > 64 * 1024 * 1024) { await reader.cancel(); throw new Error("Engine download exceeds 64 MB"); }
    chunks.push(value);
    if (performance.now() - lastReport > 80) { updateProgress("wasm", `Downloading 3D engine: ${bytesLabel(loaded)}${total ? ` of ${bytesLabel(total)}` : ""}`, loaded, total); lastReport = performance.now(); }
  }
  if (expectedBytes && loaded !== expectedBytes) throw new Error("Engine download is incomplete. Reload to fetch the current build.");
  const bytes = new Uint8Array(loaded); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}
function failWorld(message: string) {
  engineFailed = true; worldReady = false;
  $("sim-status").textContent = "3D unavailable";
  updateProgress("wasm", "3D startup failed. Reload the engine to try again.");
  showError(`${message} Reload the engine to try again. If it still fails, check your connection and browser graphics support.`);
  button("retry-world").hidden = false;
  stopPython(false); refreshControls();
  $("execution-status").textContent = "3D unavailable";
}
(globalThis as any).__wsorbEngineError = (message: string) => failWorld(message);
window.addEventListener("error", event => {
  if (event.error instanceof WebAssembly.RuntimeError || /wasm|wsorb_bevy/.test(`${event.filename} ${event.error?.stack || ""}`)) failWorld(`The 3D engine stopped: ${event.message}`);
});
window.addEventListener("unhandledrejection", event => {
  if (event.reason instanceof WebAssembly.RuntimeError || /wasm|wsorb_bevy/.test(String(event.reason?.stack || ""))) failWorld(`The 3D engine stopped: ${errorText(event.reason)}`);
});
$("simulation-canvas").addEventListener("webglcontextlost", () => failWorld("The browser lost its graphics context."));
async function setupWorld() {
  try {
    const metadataResponse = await fetch("/sim/manifest.json", { cache: "no-cache" });
    if (!metadataResponse.ok) throw new Error(`Engine manifest returned HTTP ${metadataResponse.status}`);
    const metadata = await metadataResponse.json();
    const [module, bytes] = await Promise.all([import("/sim/wsorb_bevy.js"), downloadWasm(`/sim/wsorb_bevy_bg.wasm?v=${metadata.sha256}`, metadata.bytes)]);
    if (metadata.sha256) {
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      const actual = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
      if (actual !== metadata.sha256) throw new Error("Engine integrity check failed. Reload to fetch the current build.");
    }
    updateProgress("wasm", `Downloaded ${bytesLabel(bytes.byteLength)}. Compiling WebAssembly and initializing graphics.`);
    world = module as Engine;
    await world.default({ module_or_path: bytes });
    if (engineFailed) return;
    world.start_simulation("#simulation-canvas");
    const start = performance.now();
    await new Promise<void>((resolve, reject) => {
      const check = () => {
        if (engineFailed) { reject(new Error("Bevy reported a startup failure")); return; }
        try {
          const status = JSON.parse(world!.renderer_status());
          if (status.error) { reject(new Error(status.error)); return; }
          if (status.ready) { resolve(); return; }
          updateProgress("wasm", `Initializing graphics: ${status.compiled_pipelines} compiled pipelines, ${status.pending_pipelines} pending. Waiting for a rendered frame.`);
          if (performance.now() - start > 60_000) { reject(new Error("The renderer did not produce a ready frame within 60 seconds.")); return; }
          setTimeout(check, 100);
        } catch (error) { reject(error); }
      }; check();
    });
    if (engineFailed) return;
    worldReady = true;
    $("sim-status").textContent = "3D ready";
    updateProgress("wasm", `3D engine ready. ${bytesLabel(bytes.byteLength)} downloaded.`, bytes.byteLength, bytes.byteLength);
    clearError();
    $("execution-status").textContent = "Ready";
    if (worldState) restoreState(worldState);
    else updateState(localCommand({ op: "observe" }));
    if (isHost) publishState(true);
    refreshControls();
  } catch (error) { if (!engineFailed) failWorld(errorText(error)); }
}
button("retry-world").addEventListener("click", () => location.reload());

function startWorker() {
  if (pythonWorker) return;
  const instance = new Worker("/python-worker.js");
  pythonWorker = instance; pythonReady = false;
  $("python-version").textContent = "loading";
  $("execution-status").textContent = "Loading Python";
  updateProgress("python", "Connecting to the pinned Python runtime download.");
  instance.onmessage = ({ data }) => {
    if (pythonWorker !== instance) return;
    if (data.type === "progress") {
      const message = data.stage === "ready" ? "Python ready." : data.stage === "initialize" ? data.message || "Initializing Python runtime." : `${data.file}: ${bytesLabel(data.loaded || 0)}${data.total && data.total >= data.loaded ? ` of ${bytesLabel(data.total)}` : " downloaded"}`;
      updateProgress("python", message, data.stage === "ready" ? 1 : data.loaded, data.stage === "ready" ? 1 : data.total);
    } else if (data.type === "ready") {
      pythonReady = true; $("python-version").textContent = data.version;
      if (queuedRun) runStrategy(); else { $("execution-status").textContent = "Ready"; refreshControls(); }
    } else if (data.type === "stdout" || data.type === "stderr") log(data.text);
    else if (data.type === "status") $("execution-status").textContent = data.status;
    else if (data.type === "done") { void zeroCommand(); finishExecution("Complete"); }
    else if (data.type === "error") {
      log(`Python error: ${data.error}\nCorrect the strategy and run again. For download errors, check the connection and retry Run strategy.`);
      if (data.phase === "init" || data.phase === "fatal") { pythonWorker?.terminate(); pythonWorker = undefined; pythonReady = false; updateProgress("python", "Python runtime failed. Run strategy to reload it and retry."); }
      void zeroCommand(); finishExecution("Stopped with error");
    } else if (data.type === "command") {
      // Never send the analysis snapshot's debris truth back into Python.
      void callWorld(data.command, true, true, instance).then(state => { if (pythonWorker === instance) instance.postMessage({ type: "result", id: data.id, ok: true, state }); }).catch(error => { if (pythonWorker === instance) instance.postMessage({ type: "result", id: data.id, ok: false, error: errorText(error) }); });
    }
  };
  instance.onerror = event => { if (pythonWorker !== instance) return; log(`Python worker failed: ${event.message}. Run strategy to retry.`); stopPython(); $("execution-status").textContent = "Stopped with error"; };
  instance.postMessage({ type: "init" });
}
function finishExecution(status: string) {
  pythonActive = queuedRun = false;
  if (executionTimer) clearTimeout(executionTimer);
  executionTimer = undefined;
  $("execution-status").textContent = status;
  publishState(true); refreshControls();
}
async function zeroCommand() {
  if (canControl()) try { await callWorld({ op: "stop", entity: worldState?.mission.shuttle_id || "Shuttle" }); } catch (error) { showError(`Could not zero the shuttle command. Reload before resuming. ${errorText(error)}`); }
}
function stopPython(zero = true) {
  const wasRunning = pythonActive || queuedRun;
  pythonWorker?.terminate(); pythonWorker = undefined; pythonReady = false;
  if (zero && wasRunning) void zeroCommand();
  if (wasRunning) finishExecution("Stopped");
  else refreshControls();
}
function runStrategy() {
  if (!canControl() || !pythonReady || !pythonWorker) { queuedRun = false; refreshControls(); return; }
  queuedRun = false; pythonActive = true;
  $("console").textContent = "";
  $("execution-status").textContent = "Running strategy";
  clearError(); refreshControls();
  pythonWorker.postMessage({ type: "run", code: editor.state.doc.toString() });
  executionTimer = window.setTimeout(() => { log("Execution stopped after 180 seconds of browser time. Reduce the strategy workload or number of steps before running again."); stopPython(); }, 180_000);
}
button("run-strategy").addEventListener("click", () => {
  if (!canControl()) { showError("Wait for the 3D engine and claim the host role before running a shared strategy."); return; }
  if (!editor.state.doc.toString().trim()) { log("Write a Python strategy before running it."); editor.focus(); return; }
  if (!pythonReady) { queuedRun = true; refreshControls(); startWorker(); }
  else runStrategy();
});
button("stop-python").addEventListener("click", () => stopPython());
async function manualCommand(action: () => Promise<unknown>) {
  if (!canControl() || pythonActive || queuedRun) return;
  commandBusy = true; clearError(); refreshControls();
  try { await action(); } catch (error) { showError(`${errorText(error)} Check the command values and try again.`); }
  finally { commandBusy = false; refreshControls(); }
}
button("step-button").addEventListener("click", () => void manualCommand(async () => {
  if (!input("step-seconds").reportValidity()) return;
  const seconds = Number(input("step-seconds").value);
  if (!Number.isFinite(seconds) || seconds < .01 || seconds > 30) throw new Error("Step duration must be between 0.01 and 30 seconds.");
  await callWorld({ op: "step", seconds });
}));
button("reset-button").addEventListener("click", () => void manualCommand(async () => {
  if (!input("seed-input").reportValidity()) return;
  if (worldState && worldState.time_s > 0 && !confirm("Reset this simulation? The current flight and its recorded samples will be cleared. Export the flight first to keep it.")) return;
  const seed = Number(input("seed-input").value);
  if (!Number.isInteger(seed) || seed < 0 || seed > 4294967295) throw new Error("Use a whole-number seed between 0 and 4294967295.");
  await callWorld({ op: "reset", seed });
}));
button("apply-thrust").addEventListener("click", () => void manualCommand(async () => {
  const accel = ["thrust-x", "thrust-y", "thrust-z"].map(id => Number(input(id).value));
  if (accel.some(value => !Number.isFinite(value))) throw new Error("Enter finite acceleration values.");
  await callWorld({ op: "set_command", accel, angular_accel: [0, 0, 0] });
}));
button("cancel-thrust").addEventListener("click", () => void manualCommand(() => callWorld({ op: "stop", entity: worldState?.mission.shuttle_id || "Shuttle" })));
function setView(mode = cameraMode) {
  if (!worldReady) return;
  try {
    localCommand({ op: "view", mode, focus: selectedAsset, zoom: cameraZoom }); cameraMode = mode;
    for (const name of ["mission", "overview", "follow"]) button(`view-${name}`).setAttribute("aria-pressed", String(name === mode));
  } catch (error) { showError(`Could not change the view. ${errorText(error)} Select another view or reload.`); }
}
for (const mode of ["mission", "overview", "follow"]) button(`view-${mode}`).addEventListener("click", () => { cameraZoom = 1; setView(mode); });
button("zoom-in").addEventListener("click", () => { cameraZoom = Math.max(.25, cameraZoom / 1.35); setView(); });
button("zoom-out").addEventListener("click", () => { cameraZoom = Math.min(4, cameraZoom * 1.35); setView(); });
button("export-button").addEventListener("click", () => {
  if (!worldState) return;
  const record = { simulator: "Bevy + Rapier browser practice model", exported_at: new Date().toISOString(), model_limitations: worldState.model, state: worldState, samples: flightSamples, strategy: editor.state.doc.toString(), notebook_url: savedNotebook };
  const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = `phobos-flight-${worldState.seed}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
button("clear-console").addEventListener("click", () => { $("console").textContent = ""; });

function validNotebook(value: string) {
  try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password && ["notebooklm.google.com", "docs.google.com"].includes(url.hostname) ? url.href : null; } catch { return null; }
}
let savedNotebook = "https://notebooklm.google.com/";
try { savedNotebook = validNotebook(localStorage.getItem("wsorb:notebook") || "") || savedNotebook; } catch { /* Storage is optional. */ }
function setNotebook(value: string) {
  const url = validNotebook(value); if (!url) return;
  savedNotebook = url; input("notebook-url").value = url;
  const anchor = $<HTMLAnchorElement>("notebook-open"); anchor.href = url; anchor.textContent = url.includes("docs.google.com") ? "Open Google Docs" : "Open NotebookLM";
  try { localStorage.setItem("wsorb:notebook", url); } catch { /* Storage is optional. */ }
}
$("notebook-form").addEventListener("submit", event => {
  event.preventDefault(); const field = input("notebook-url"), url = validNotebook(field.value);
  field.setCustomValidity(url ? "" : "Use a secure NotebookLM or Google Docs URL, without a username or password.");
  field.setAttribute("aria-invalid", String(!url));
  if (!url) { field.reportValidity(); return; }
  setNotebook(url); send({ type: "notebook", url }); $("notebook-status").textContent = "Reference link saved.";
});
input("notebook-url").addEventListener("input", () => { input("notebook-url").setCustomValidity(""); input("notebook-url").removeAttribute("aria-invalid"); });
const dialog = $<HTMLDialogElement>("share-dialog");
button("share-button").addEventListener("click", () => {
  if (roomId && roomKey) { input("share-link").value = `${location.origin}/#room=${encodeURIComponent(roomId)}&key=${encodeURIComponent(roomKey)}`; $("share-result").hidden = false; }
  dialog.showModal();
});
button("create-room").addEventListener("click", async () => {
  const create = button("create-room"); create.disabled = true; $("share-status").textContent = "Creating private room.";
  try {
    const response = await fetch("/api/rooms", { method: "POST" });
    if (!response.ok) throw new Error(`Room service returned HTTP ${response.status}.`);
    const data = await response.json(); if (!data.id || !data.key) throw new Error("Room service returned an incomplete link.");
    seedNewRoom = true; connectRoom(data.id, data.key);
    input("share-link").value = `${location.origin}/#room=${encodeURIComponent(data.id)}&key=${encodeURIComponent(data.key)}`;
    $("share-result").hidden = false; $("share-status").textContent = "Room created. Keep this link private.";
  } catch (error) { $("share-status").textContent = `Could not create a room. Check your connection and try again. ${errorText(error)}`; }
  finally { create.disabled = false; }
});
button("copy-link").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(input("share-link").value); $("share-status").textContent = "Private link copied."; }
  catch { input("share-link").select(); $("share-status").textContent = "Copy the selected link with your browser's Copy command."; }
});
setNotebook(savedNotebook); refreshControls();
if (roomId && roomKey) connectRoom(roomId, roomKey);
else if (roomId || roomKey) { roomId = roomKey = null; history.replaceState(null, "", location.pathname + location.search); $("room-status").textContent = "Incomplete room link. Create a new private room."; }
void setupWorld();
