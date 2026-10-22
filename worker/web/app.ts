import { basicSetup } from "codemirror";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { python } from "@codemirror/lang-python";
import { oneDark } from "@codemirror/theme-one-dark";
import { yCollab } from "y-codemirror.next";
import { Awareness } from "y-protocols/awareness";
import * as Y from "yjs";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const initialCode = `# Collaborative rover strategy — edit this plan together.
# Commands are async; this strategy advances the Bevy world in deliberate steps.
await sim.reset(seed=42)
state = await sim.observe()
print(f"Starting mission at t={state['time_s']:.1f}s")

rover = "Scout1"
waypoints = [(15, 15), (45, 15), (85, 15), (85, 45), (45, 45), (15, 45), (15, 85), (45, 85), (85, 85)]
for x, y in waypoints:
    await sim.move_to(rover, x, y)
    for _ in range(40):
        await sim.step(5)
        state = await sim.observe()
        asset = next(asset for asset in state["assets"] if asset["id"] == rover)
        print(f"t={state['time_s']:.0f}s · {rover}=({asset['x']:.1f}, {asset['y']:.1f}) · {asset['status']}")
        if state["found"] or asset["status"] != "moving":
            break
    if state["found"]:
        await sim.report()
        print("Target reported.")
        break
await sim.stop(rover)
print("Mission strategy complete.")
`;
const room = new URLSearchParams(location.hash.slice(1));
let savedNotebook = "https://notebooklm.google.com";
try {
  const candidate = localStorage.getItem("wsorb:notebook");
  if (candidate) {
    const url = new URL(candidate);
    if (url.protocol === "https:" && !url.username && !url.password && ["notebooklm.google.com", "docs.google.com"].includes(url.hostname)) savedNotebook = url.href;
  }
} catch { /* Browser storage is optional. */ }
let roomId = room.get("room"), roomKey = room.get("key");
let socket: WebSocket | undefined;
let doc = new Y.Doc(), strategy = doc.getText("strategy"), awareness = new Awareness(doc), editor: EditorView;
let hostId: string | null = null, clientId: string | null = null, isHost = false, seedNewRoom = false, hostRestoreFailed = false;
let world: any, worldReady = false, worldState: any, selectedAsset = "", running = false, lastTick = 0, accumulator = 0;
let pythonWorker: Worker | undefined, pythonReady = false, pythonActive = false, queuedRun = false, executionTimer: number | undefined;
let pendingCommands = new Map<string, {resolve:(v:any)=>void;reject:(e:Error)=>void}>();

if (!roomId) strategy.insert(0, initialCode);
editor = new EditorView({ parent: $("editor"), state: EditorState.create({ doc: roomId ? "" : initialCode, extensions: [basicSetup, python(), oneDark, EditorView.lineWrapping, EditorView.contentAttributes.of({"aria-label":"Shared Python strategy"}), yCollab(strategy, awareness)] }) });

function log(text: string) { const consoleView = $("console"); consoleView.textContent = `${consoleView.textContent === "# Strategy output will appear here\n" ? "" : consoleView.textContent}${text}\n`.slice(-16_000); consoleView.scrollTop = consoleView.scrollHeight; }
function showError(message: string) { $("world-message").textContent = message; $("execution-status").textContent = "Needs attention"; }
function encodeBase64(bytes: Uint8Array) { let binary = ""; for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000)); return btoa(binary); }
function connectRoom(id: string, key: string) {
  if (socket) {
    socket.onmessage = socket.onclose = socket.onerror = socket.onopen = null;
    socket.close();
  }
  for (const pending of pendingCommands.values()) pending.reject(new Error("Switched rooms"));
  pendingCommands.clear();
  hostId = clientId = null;
  isHost = false;
  roomId = id; roomKey = key; history.replaceState(null, "", `#room=${encodeURIComponent(id)}&key=${encodeURIComponent(key)}`);
  socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/rooms/${encodeURIComponent(id)}/ws`, ["wsorb", `auth.${key}`]);
  socket.onopen = () => { $("collab-state").textContent = "● Shared room"; $("collab-state").classList.add("connected"); $("room-status").textContent = "Shared room connected"; };
  socket.onclose = () => { $("collab-state").textContent = "Offline"; $("collab-state").classList.remove("connected"); $("room-status").textContent = "Room connection lost — refresh to reconnect. Unsynced edits may not be shared."; };
  socket.onerror = () => { $("room-status").textContent = "Room unavailable — check the link and try again"; };
  socket.onmessage = ({data}) => {
    try {
      const message = JSON.parse(data);
      if (message.type === "welcome") {
        clientId = message.clientId; hostId = message.hostId;
        if (message.document) Y.applyUpdate(doc, Uint8Array.from(atob(message.document), c => c.charCodeAt(0)), "remote");
        if (seedNewRoom) {
          if (strategy.length === 0) strategy.insert(0, initialCode);
          if (socket?.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({type:"document",update:encodeBase64(Y.encodeStateAsUpdate(doc))}));
            socket.send(JSON.stringify({type:"claim-host"}));
            socket.send(JSON.stringify({type:"notebook",url:savedNotebook}));
          }
        }
        seedNewRoom = false;
        if (message.state) updateState(message.state);
        updateHostUI();
        if (message.state && !isHost) restoreViewer(message.state);
        setNotebook(message.notebookUrl || savedNotebook);
      } else if (message.type === "document") {
        Y.applyUpdate(doc, Uint8Array.from(atob(message.update), c => c.charCodeAt(0)), "remote");
      } else if (message.type === "host") { hostId = message.hostId; updateHostUI(); }
      else if (message.type === "state") { updateState(message.state); restoreViewer(message.state); }
      else if (message.type === "execute" && isHost) executeHostCommand(message);
      else if (message.type === "result") {
        const wait = pendingCommands.get(message.id); if (wait) { pendingCommands.delete(message.id); message.ok ? wait.resolve(message.state) : wait.reject(new Error(message.error || "Simulation command failed")); }
      } else if (message.type === "notebook") setNotebook(message.url);
    } catch { $("room-status").textContent = "Could not read room update"; }
  };
}
doc.on("update", (update: Uint8Array, origin: unknown) => { if (origin === "remote" || socket?.readyState !== WebSocket.OPEN) return; socket.send(JSON.stringify({type:"document", update:encodeBase64(update)})); });
function updateHostUI() {
  const wasHost = isHost;
  isHost = !!clientId && hostId === clientId;
  $("claim-host").textContent = isHost ? "Release simulation host" : "Claim simulation host";
  $("claim-host").classList.toggle("primary", isHost);
  if (isHost && !wasHost) {
    hostRestoreFailed = false;
    if (worldState) restoreHostState();
    else if (worldReady) void callWorld({op:"observe"}).catch(error => showError(String(error)));
  }
  if (wasHost && !isHost) {
    running = false;
    $("run-button").disabled = false;
    $("pause-button").disabled = true;
    stopPython();
  }
}
$("claim-host").addEventListener("click", () => {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    $("room-status").textContent = "Create or join a room to claim the simulation host.";
    return;
  }
  socket.send(JSON.stringify({type: isHost ? "release-host" : "claim-host"}));
});

async function callWorld(command: Record<string, unknown>, publishState = true): Promise<any> {
  if (!worldReady || !world) throw new Error("The Bevy simulation is not ready yet.");
  if (roomId && !isHost) {
    if (socket?.readyState !== WebSocket.OPEN) throw new Error("Room is reconnecting. Wait for the shared connection before issuing commands.");
    if (!hostId) throw new Error("Claim the simulation host before issuing commands.");
    const id = crypto.randomUUID();
    return new Promise((resolve,reject) => { pendingCommands.set(id,{resolve,reject}); socket!.send(JSON.stringify({type:"command",id,command})); setTimeout(() => { const pending = pendingCommands.get(id); if (pending) { pendingCommands.delete(id); pending.reject(new Error("Host did not respond. Claim the simulation host or try again later.")); } }, 15_000); });
  }
  if (isHost && hostRestoreFailed) throw new Error("The room state could not be restored. Reload before controlling this mission.");
  const result = JSON.parse(await world.command(JSON.stringify(command)));
  if (!result.ok) throw new Error(result.error || "Simulation command failed");
  if (result.state) {
    updateState(result.state);
    if (publishState && roomId && isHost && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type:"state",state:result.state}));
  }
  return result.state;
}
async function executeHostCommand(message: any) {
  try { const state = await callWorld(message.command, false); socket?.send(JSON.stringify({type:"result",id:message.id,ok:true,state})); }
  catch (error) { socket?.send(JSON.stringify({type:"result",id:message.id,ok:false,error:error instanceof Error ? error.message : String(error)})); }
}

function updateState(state: any) {
  if (!state || typeof state !== "object") return; worldState = state;
  $("time-value").textContent = `${Math.floor((state.time_s||0)/60).toString().padStart(2,"0")}:${Math.floor((state.time_s||0)%60).toString().padStart(2,"0")}`;
  $("found-value").textContent = String(state.found ?? 0); $("reported-value").textContent = String(state.reported ?? 0); $("seed-value").textContent = String(state.seed ?? "—");
  const assets = Array.isArray(state.assets) ? state.assets : []; $("asset-count").textContent = String(assets.length);
  if (!assets.some((asset: any) => asset.id === selectedAsset)) selectedAsset = assets[0]?.id || "";
  const list = $("asset-list"); list.replaceChildren();
  if (!assets.length) list.innerHTML = '<div class="empty-note">No field assets in this world.</div>';
  for (const asset of assets) {
    const button = document.createElement("button"); button.className = `asset-row${asset.id === selectedAsset ? " selected" : ""}`; button.type = "button";
    button.innerHTML = `<span class="asset-icon" aria-hidden="true">⌖</span><span class="asset-info"><strong>${escapeHtml(asset.name || asset.id)}</strong><small>${Number(asset.x||0).toFixed(1)}, ${Number(asset.y||0).toFixed(1)} · ${Number(asset.speed_mps||0).toFixed(1)} m/s</small></span><span class="battery-value">${Math.round(asset.battery_percent||0)}%</span>`;
    button.setAttribute("aria-pressed", String(asset.id === selectedAsset)); button.addEventListener("click", () => { selectedAsset = asset.id; updateState(worldState); }); list.append(button);
  }
  const selected = assets.find((asset:any) => asset.id === selectedAsset);
  if (selected) { $("sensor-radius").value = String(selected.sensor_radius_m ?? 30); $("sensor-value").textContent = `${selected.sensor_radius_m ?? 30} m`; $("battery").value = String(selected.battery_percent ?? 80); $("battery-value").textContent = `${Math.round(selected.battery_percent ?? 80)}%`; }
}
function escapeHtml(value: string) { return value.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]!)); }

async function setupWorld() {
  try {
    const module = await import("/sim/wsorb_bevy.js");
    await module.default(); world = module; world.start_simulation("#simulation-canvas"); worldReady = true;
    $("sim-status").innerHTML = "<i></i> World ready"; $("sim-status").classList.add("ready"); $("world-message").textContent = "";
    if (isHost && worldState) restoreHostState();
    else if (isHost || !roomId) updateState(await callWorld({op:"observe"}));
    else if (worldState) restoreViewer(worldState);
  } catch (error) { $("sim-status").textContent = "World unavailable"; showError(`Could not load the Bevy world. Check that /sim assets were built and try refreshing. ${error instanceof Error ? error.message : ""}`); }
}
function restoreViewer(state: any) {
  if (!worldReady || isHost || !state) return;
  try {
    const parsed = JSON.parse(world.command(JSON.stringify({op:"restore",state})));
    if (parsed.ok && parsed.state) updateState(parsed.state);
    else if (!parsed.ok) $("room-status").textContent = `Could not restore shared state: ${parsed.error || "unknown error"}`;
  } catch { $("room-status").textContent = "Could not restore the host simulation state"; }
}
function restoreHostState() {
  if (!isHost) return;
  if (!worldReady) return;
  if (!worldState) { hostRestoreFailed = false; return; }
  try {
    const parsed = JSON.parse(world.command(JSON.stringify({op:"restore",state:worldState})));
    if (!parsed.ok) throw new Error(parsed.error || "WASM rejected the saved state");
    hostRestoreFailed = false;
    if (parsed.state) updateState(parsed.state);
    if (socket?.readyState === WebSocket.OPEN && parsed.state) socket.send(JSON.stringify({type:"state",state:parsed.state}));
    $("room-status").textContent = "Simulation host connected · previous mission restored";
  } catch (error) {
    hostRestoreFailed = true;
    showError(`Could not restore the saved room mission: ${error instanceof Error ? error.message : String(error)}`);
  }
}
function startWorker() {
  if (pythonWorker) return;
  const instance = new Worker("/python-worker.js");
  pythonWorker = instance; pythonReady = false; $("python-version").textContent = "loading runtime"; $("execution-status").textContent = "Loading Python…";
  instance.onmessage = ({data}) => {
    if (pythonWorker !== instance) return;
    if (data.type === "ready") { pythonReady = true; $("python-version").textContent = data.version; $("execution-status").textContent = "Ready"; if (queuedRun) runStrategy(); }
    else if (data.type === "stdout") log(data.text);
    else if (data.type === "stderr") log(data.text);
    else if (data.type === "status") $("execution-status").textContent = data.status;
    else if (data.type === "done") finishExecution("Complete");
    else if (data.type === "error") { log(`Error: ${data.error}`); if (!pythonReady) { pythonWorker?.terminate(); pythonWorker = undefined; queuedRun = false; } finishExecution("Stopped with error"); }
    else if (data.type === "command") {
      void callWorld(data.command).then(state => instance.postMessage({type:"result",id:data.id,ok:true,state})).catch(error => instance.postMessage({type:"result",id:data.id,ok:false,error:error instanceof Error?error.message:String(error)}));
    }
  };
  instance.onerror = event => { if (pythonWorker !== instance) return; log(`Python worker error: ${event.message}`); stopPython(); };
  instance.postMessage({type:"init"});
}
function finishExecution(status: string) { pythonActive = false; if (executionTimer) clearTimeout(executionTimer); executionTimer = undefined; $("execution-status").textContent = status; $("run-strategy").disabled = false; }
function stopPython() { pythonWorker?.terminate(); pythonWorker = undefined; pythonReady = false; queuedRun = false; finishExecution("Stopped"); }
$("run-button").addEventListener("click", () => {
  if (pythonActive) { showError("Stop the Python strategy before starting continuous playback."); return; }
  if (!worldReady) { showError("The Bevy world is still loading. Try again when it is ready."); return; }
  if (roomId && !isHost) { showError("Claim the simulation host before running the shared world."); return; }
  if (hostRestoreFailed) { showError("The saved room mission could not be restored. Reload before controlling it."); return; }
  running = true; lastTick = 0; $("run-button").disabled = true; $("pause-button").disabled = false; $("pause-button").innerHTML = "Ⅱ <span>Pause</span>";
});
$("run-strategy").addEventListener("click", () => {
  if (roomId && !isHost) { log("Only the simulation host can run strategy commands. Claim the host role first."); return; }
  if (!pythonWorker) { queuedRun = true; $("run-strategy").disabled = true; startWorker(); return; }
  if (!pythonReady) { queuedRun = true; $("run-strategy").disabled = true; $("execution-status").textContent = "Python is still loading…"; return; }
  runStrategy();
});
function runStrategy() {
  queuedRun = false;
  running = false; $("run-button").disabled = false; $("pause-button").disabled = true;
  $("pause-button").innerHTML = "Ⅱ <span>Pause</span>";
  $("console").textContent = ""; pythonActive = true; $("run-strategy").disabled = true; $("execution-status").textContent = "Running strategy…";
  pythonWorker!.postMessage({type:"run",code:editor.state.doc.toString()}); executionTimer = window.setTimeout(() => { log("Execution limit reached (60 seconds). Stop and simplify your strategy."); stopPython(); },60_000);
}
$("stop-python").addEventListener("click", stopPython);
$("pause-button").addEventListener("click", () => { running = false; $("run-button").disabled = false; $("pause-button").disabled = true; $("pause-button").innerHTML = "Ⅱ <span>Pause</span>"; });
$("step-button").addEventListener("click", async () => { try { await callWorld({op:"step",seconds:Number($("speed").value)}); } catch(e) { showError(String(e)); } });
$("reset-button").addEventListener("click", async () => { try { running = false; $("run-button").disabled = false; $("pause-button").disabled = true; await callWorld({op:"reset",seed:Number($("seed-input").value)||42}); } catch(e) { showError(String(e)); } });
$("speed").addEventListener("input", () => { $("speed-value").textContent = `${$("speed").value}×`; });
$("sensor-radius").addEventListener("input", async () => { $("sensor-value").textContent = `${$("sensor-radius").value} m`; if (selectedAsset) try { await callWorld({op:"set_attribute",entity:selectedAsset,attribute:"sensor_radius_m",value:Number($("sensor-radius").value)}); } catch(e) { showError(String(e)); } });
$("battery").addEventListener("input", async () => { $("battery-value").textContent = `${$("battery").value}%`; if (selectedAsset) try { await callWorld({op:"set_attribute",entity:selectedAsset,attribute:"battery_percent",value:Number($("battery").value)}); } catch(e) { showError(String(e)); } });
function animationFrame(time: number) { requestAnimationFrame(animationFrame); if (!running || !worldReady || (roomId && !isHost)) return; if (!lastTick) lastTick = time; accumulator += Math.min((time-lastTick)/1000,0.25)*Number($("speed").value); lastTick = time; if (accumulator >= .25) { const steps = Math.min(4,Math.floor(accumulator/.25)); accumulator -= steps*.25; void callWorld({op:"step",seconds:steps*.25}).catch(e => { running=false; showError(String(e)); }); } }
requestAnimationFrame(animationFrame);

$("export-button").addEventListener("click", () => {
  if (!worldState) return;
  const record = { simulator: "bevy-wasm", exported_at: new Date().toISOString(), state: worldState, strategy: editor.state.doc.toString(), notebook_url: savedNotebook };
  const blob = new Blob([JSON.stringify(record, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `orbital-mission-${worldState.seed ?? "results"}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
});
$("clear-console").addEventListener("click", () => { $("console").textContent = ""; });
function setNotebook(url: string) {
  savedNotebook = url;
  try { localStorage.setItem("wsorb:notebook", url); } catch { /* Browser storage is optional. */ }
  const input = $("notebook-url") as HTMLInputElement;
  input.value = url;
  const anchor = $("notebook-open") as HTMLAnchorElement;
  anchor.href = url;
  anchor.textContent = `Open ${url.includes("docs.google.com") ? "Google Docs" : "NotebookLM"} ↗`;
}
$("notebook-form").addEventListener("submit", event => { event.preventDefault(); const input = $("notebook-url") as HTMLInputElement; let parsed: URL; try { parsed = new URL(input.value); } catch { input.setCustomValidity("Enter a valid Google Docs or NotebookLM URL."); input.reportValidity(); return; } input.setCustomValidity(""); if (parsed.protocol !== "https:" || !["notebooklm.google.com","docs.google.com"].includes(parsed.hostname)) { input.setCustomValidity("Use a secure NotebookLM or Google Docs URL."); input.reportValidity(); return; } input.value = parsed.href; setNotebook(parsed.href); if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type:"notebook",url:parsed.href})); });
$("notebook-url").addEventListener("input", () => ($("notebook-url") as HTMLInputElement).setCustomValidity(""));

const dialog = $("share-dialog") as HTMLDialogElement;
$("share-button").addEventListener("click", () => {
  if (roomId && roomKey) {
    ($("share-link") as HTMLInputElement).value = `${location.origin}/#room=${encodeURIComponent(roomId)}&key=${encodeURIComponent(roomKey)}`;
    $("share-result").hidden = false;
  }
  dialog.showModal();
});
$("create-room").addEventListener("click", async () => { const button = $("create-room") as HTMLButtonElement; button.disabled = true; button.textContent = "Creating room…"; try { const response = await fetch("/api/rooms",{method:"POST"}); if (!response.ok) throw new Error(response.status === 404 ? "Room service is not available yet. Please try again later." : "Could not create a room. Check your connection and try again."); const data = await response.json(); if (!data.id || !data.key) throw new Error("Room service returned an incomplete link."); seedNewRoom = true; connectRoom(data.id,data.key); const link = `${location.origin}/#room=${encodeURIComponent(data.id)}&key=${encodeURIComponent(data.key)}`; ($("share-link") as HTMLInputElement).value = link; $("share-result").hidden = false; button.textContent = "Room created"; } catch(error) { button.textContent = error instanceof Error ? error.message : "Could not create room"; } finally { button.disabled = false; } });
$("copy-link").addEventListener("click", async () => { const input = $("share-link") as HTMLInputElement; try { await navigator.clipboard.writeText(input.value); $("copy-link").textContent = "Copied"; } catch { input.select(); document.execCommand("copy"); $("copy-link").textContent = "Copied"; } });
setNotebook(savedNotebook);
if (roomId && roomKey) connectRoom(roomId,roomKey); else if (roomId || roomKey) { history.replaceState(null,"",location.pathname+location.search); $("room-status").textContent = "Invalid room link — create a new private room"; }
setupWorld();
