import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { createReadStream, existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { dirname, extname, join, resolve, sep } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const worker = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = process.env.PREVIEW_OUTPUT_DIR || join(tmpdir(), "opencode", "wso-preview");
const width = optionNumber("width", 1440, 320, 3840);
const height = optionNumber("height", 1000, 240, 2160);
const block = optionValue("block");
const view = optionValue("view");
if (view && !["mission", "overview", "follow"].includes(view)) throw new Error("--view must be mission, overview or follow.");
const runStrategy = process.argv.includes("--run-strategy") || block === "python";
const doRoom = process.argv.includes("--room");
const doStep = process.argv.includes("--step");
if (block && !["wasm", "python"].includes(block)) throw new Error("--block must be wasm or python.");
await mkdir(output, { recursive: true });
const profile = await mkdtemp(join(output, "browser-"));
const chrome = process.env.CHROME_BIN || [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
].find(existsSync);
if (!chrome) throw new Error("Set CHROME_BIN to an installed Chrome or Chromium browser.");
const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".wasm": "application/wasm", ".json": "application/json", ".txt": "text/plain" };
const publicDirectory = join(worker, "public");
const server = createServer(async (request, response) => {
  try {
    const path = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    const file = resolve(publicDirectory, `.${path === "/" ? "/index.html" : path}`);
    if (!file.startsWith(publicDirectory + sep)) throw new Error("Invalid path");
    const info = await stat(file);
    if (!info.isFile()) throw new Error("Not a file");
    response.writeHead(200, { "Content-Type": types[extname(file)] || "application/octet-stream", "Content-Length": info.size });
    createReadStream(file).pipe(response);
  } catch { response.writeHead(404); response.end("Not found"); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const supplied = optionValue("url");
const url = supplied || `http://127.0.0.1:${server.address().port}/`;
const child = spawn(chrome, ["--headless=new", `--user-data-dir=${profile}`, "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1", "--enable-unsafe-swiftshader", "--use-angle=swiftshader", "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
let browserError = "";
child.stderr.on("data", data => { browserError = (browserError + data).slice(-12_000); });
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
let socket;
let failed = false;
const messages = [];
const requests = [];
let blockObserved = false;
try {
  let port;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { port = Number((await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]); break; } catch { await delay(100); }
  }
  if (!port) throw new Error(`Browser did not start. ${browserError}`);
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = pages.find(item => item.type === "page");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let next = 0;
  const pending = new Map();
  const requestUrls = new Map();
  const targets = new Map();
  const targetSetups = [];
  const call = (method, params = {}, timeout = 260_000, sessionId) => new Promise((resolve, reject) => {
    const id = ++next;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, timeout);
    pending.set(id, message => { clearTimeout(timer); message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result); });
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    const sessionId = message.sessionId;
    const target = sessionId ? targets.get(sessionId) : undefined;
    if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
    if (message.method === "Target.attachedToTarget") {
      const { sessionId: childSession, targetInfo } = message.params;
      targets.set(childSession, { type: targetInfo.type, url: targetInfo.url });
      const setup = async () => {
        try {
          await Promise.allSettled([
            call("Network.enable", {}, 10_000, childSession),
            call("Runtime.enable", {}, 10_000, childSession),
            ...(block === "python" ? [call("Network.setBlockedURLs", { urls: ["*cdn.jsdelivr.net/pyodide/*"] }, 10_000, childSession)] : []),
          ]);
        } finally {
          try { await call("Runtime.runIfWaitingForDebugger", {}, 10_000, childSession); }
          catch (error) { addMessage({ type: "target-resume-error", text: `${targetInfo.type}: ${error.message}` }); }
        }
      };
      targetSetups.push(setup());
      return;
    }
    if (message.method === "Runtime.exceptionThrown") addMessage({ type: "exception", target: target?.type || "page", text: message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text });
    if (message.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(message.params.type)) addMessage({ type: message.params.type, target: target?.type || "page", text: message.params.args.map(item => item.value ?? item.description ?? "").join(" ") });
    if (message.method === "Network.requestWillBeSent") requestUrls.set(`${sessionId || "page"}:${message.params.requestId}`, message.params.request.url);
    if (message.method === "Network.loadingFailed") {
      const item = { type: "request-failed", target: target?.type || "page", url: requestUrls.get(`${sessionId || "page"}:${message.params.requestId}`), error: message.params.errorText, blockedReason: message.params.blockedReason };
      requests.push(item);
      if (requests.length > 200) requests.shift();
      if (item.blockedReason || (block === "python" && item.url?.includes("cdn.jsdelivr.net/pyodide/"))) blockObserved = true;
    }
    if (message.method === "Network.responseReceived" && message.params.response.status >= 400) {
      requests.push({ type: "http-error", target: target?.type || "page", url: message.params.response.url, status: message.params.response.status });
      if (requests.length > 200) requests.shift();
    }
  };
  const evaluate = async (expression, timeout = 260_000) => {
    const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, timeout);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result?.value;
  };
  const readRuntime = () => evaluate(`(async()=>{try{const m=await import('/sim/wsorb_bevy.js');const parse=x=>{try{return typeof x==='string'?JSON.parse(x):x}catch{return null}};const result=parse(m.command(JSON.stringify({op:'observe'})));const s=result?.state;const assets=Array.isArray(s?.assets)?s.assets:[];const byKind={};for(const a of assets)byKind[a.kind]=(byKind[a.kind]||0)+1;const pick=a=>a?{id:a.id,kind:a.kind,position:a.position,velocity:a.velocity,rotation:a.rotation,mass_kg:a.mass_kg}:null;const shuttle=assets.find(a=>a.id==='Shuttle');const target=assets.find(a=>a.id==='Target');const observation=parse(m.observation?.());return {renderer:parse(m.renderer_status?.()),snapshot:s?{time_s:s.time_s,seed:s.seed,frame:s.frame,shuttle:pick(shuttle),target:pick(target),mission:s.mission?{target_distance_m:s.mission.target_distance_m,relative_speed_mps:s.mission.relative_speed_mps,fuel_used_kg:s.mission.fuel_used_kg,damage:s.mission.damage,status:s.mission.status}:null,asset_counts:{total:assets.length,by_kind:byKind},assets:assets.slice(0,3).map(pick)}:null,radar_count:Array.isArray(observation?.radar)?observation.radar.length:Array.isArray(observation?.observations)?observation.observations.length:null};}catch(e){return {error:String(e)}}})()`);
  await call("Runtime.enable");
  await call("Page.enable");
  await call("Network.enable");
  await call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
  if (block === "wasm") await call("Network.setBlockedURLs", { urls: ["*/*.wasm*", "*.wasm*"] });
  if (block === "python") await call("Network.setBlockedURLs", { urls: ["*cdn.jsdelivr.net/pyodide/*"] });
  await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width <= 600 });
  await call("Page.navigate", { url });

  const stateExpression = `(()=>{const byId=id=>document.getElementById(id);const text=id=>byId(id)?.textContent?.trim()??null;const progress=id=>{const p=byId(id);return p?{value:p.value,max:p.max,percent:p.max?Math.round(p.value/p.max*100):null,indeterminate:p.hasAttribute('value')===false}:null};const c=byId('simulation-canvas');return {title:document.title,engine:text('sim-status'),worldMessage:text('world-message'),execution:text('execution-status'),console:text('console'),wasmProgressText:text('wasm-progress-text'),wasmProgress:progress('wasm-progress'),pythonProgressText:text('python-progress-text'),pythonProgress:progress('python-progress'),controls:Object.fromEntries(['run-strategy','step-button','reset-button','apply-thrust','cancel-thrust','export-button','stop-python'].map(id=>[id,{exists:!!byId(id),disabled:byId(id)?.disabled??null}])),canvas:c?{width:c.width,height:c.height,clientWidth:c.clientWidth,clientHeight:c.clientHeight,rect:{width:c.getBoundingClientRect().width,height:c.getBoundingClientRect().height}}:null,viewport:{width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,horizontalOverflow:document.documentElement.scrollWidth>innerWidth}};})()`;
  const capture = async path => {
    const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: process.argv.includes("--full-page") });
    await writeFile(join(output, path), Buffer.from(screenshot.data, "base64"));
  };
  const waitForEngine = async () => evaluate(`(async()=>{const ready=new Set(['3D ready','World ready']);const errors=new Set(['3D unavailable','World unavailable','Engine failed']);const end=Date.now()+60000;while(Date.now()<end){const s=document.getElementById('sim-status')?.textContent?.trim();if(ready.has(s)||errors.has(s))return s;await new Promise(r=>setTimeout(r,100));}return document.getElementById('sim-status')?.textContent?.trim()||'missing';})()`, 65_000);
  const engine = await waitForEngine();
  if (view && ["3D ready", "World ready"].includes(engine)) await evaluate(`document.getElementById(${JSON.stringify(`view-${view}`)})?.click()`);
  await delay(300);
  const before = await evaluate(stateExpression);
  const runtimeBefore = ["3D ready", "World ready"].includes(engine) ? await readRuntime() : null;
  await capture("before.png");
  let room = null;
  if (doRoom && ["3D ready", "World ready"].includes(engine)) {
    room = await verifyRoom(evaluate, delay, url);
    if (room.result !== "PASS") failed = true;
  }
  let step = { requested: doStep, changed: null, beforeTime: null, afterTime: null };
  if (doStep && ["3D ready", "World ready"].includes(engine)) {
    const timeBefore = await evaluate("document.getElementById('time-value')?.textContent?.trim() ?? null");
    await evaluate("document.getElementById('step-button')?.click()");
    const timeAfter = await evaluate(`(async()=>{const start=${JSON.stringify(timeBefore)};const end=Date.now()+15000;while(Date.now()<end){const now=document.getElementById('time-value')?.textContent?.trim()??null;if(now!==start)return now;await new Promise(r=>setTimeout(r,100));}return document.getElementById('time-value')?.textContent?.trim()??null;})()`, 17_000);
    step = { requested: true, beforeTime: timeBefore, afterTime: timeAfter, changed: timeAfter !== timeBefore };
  }
  let strategy = { requested: runStrategy, status: null, timedOut: false };
  if (runStrategy && ["3D ready", "World ready"].includes(engine)) {
    await evaluate("document.getElementById('run-strategy')?.click()");
    const outcome = await evaluate(`(async()=>{const end=Date.now()+240000;while(Date.now()<end){const s=document.getElementById('execution-status')?.textContent?.trim()||'';if(['Complete','Stopped with error','Stopped','Python unavailable'].includes(s))return {status:s,timedOut:false};await new Promise(r=>setTimeout(r,100));}return {status:document.getElementById('execution-status')?.textContent?.trim()||null,timedOut:true};})()`, 245_000);
    strategy = { requested: true, ...outcome };
  }
  await Promise.allSettled(targetSetups);
  await delay(250);
  const after = await evaluate(stateExpression);
  const runtimeAfter = ["3D ready", "World ready"].includes(engine) ? await readRuntime() : null;
  const poseBefore = runtimeBefore?.snapshot?.shuttle?.position;
  const poseAfter = runtimeAfter?.snapshot?.shuttle?.position;
  const poseChanged = Array.isArray(poseBefore) && Array.isArray(poseAfter) && poseBefore.some((value, index) => value !== poseAfter[index]);
  const simulationTimeChanged = runtimeBefore?.snapshot?.time_s !== undefined && runtimeAfter?.snapshot?.time_s !== undefined && runtimeBefore.snapshot.time_s !== runtimeAfter.snapshot.time_s;
  await capture("preview.png");
  if (block) blockObserved ||= requests.some(item => item.type === "request-failed" && (block === "wasm" ? item.url?.includes(".wasm") : item.url?.includes("cdn.jsdelivr.net/pyodide/")));
  const expectedBlockedOutcome = block === "wasm"
    ? blockObserved && ["3D unavailable", "World unavailable", "Engine failed"].includes(engine)
    : block === "python" && blockObserved && ["3D ready", "World ready"].includes(engine) && strategy.status === "Stopped with error";
  const errors = messages.filter(item => item.type === "exception" || item.type === "error");
  const unexpectedNetworkFailures = requests.filter(item => !blockObserved || !(block === "wasm" ? item.url?.includes(".wasm") : item.url?.includes("cdn.jsdelivr.net/pyodide/")));
  const runtimeReady = ["3D ready", "World ready"].includes(engine) && ["3D ready", "World ready"].includes(after.engine);
  if (block ? !expectedBlockedOutcome : !runtimeReady) failed = true;
  if (!block && (errors.length || unexpectedNetworkFailures.length)) failed = true;
  if (doStep && !step.changed) failed = true;
  if (runStrategy && !block && (strategy.timedOut || strategy.status !== "Complete")) failed = true;
  if (runStrategy && !block && (!simulationTimeChanged || !poseChanged)) failed = true;
  const report = { url, viewport: { width, height }, requested: { block, step: doStep, runStrategy, room: doRoom }, engine, state: { before, after }, runtime: { before: runtimeBefore, after: runtimeAfter, motion: { shuttlePoseChanged: poseChanged, simulationTimeChanged } }, room, step, strategy, blockedOutcomeObserved: block ? expectedBlockedOutcome : null, messages: messages.slice(-200), networkFailures: requests.slice(-200), result: failed ? "FAIL" : "PASS" };
  await writeFile(join(output, "preview.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  console.log(`Screenshots: ${join(output, "before.png")} and ${join(output, "preview.png")}`);
} finally {
  socket?.close();
  child.kill("SIGTERM");
  await new Promise(resolve => { if (child.exitCode !== null) resolve(); else { child.once("exit", resolve); setTimeout(resolve, 3000).unref(); } });
  server.close();
  await rm(profile, { recursive: true, force: true }).catch(() => {});
}
if (failed) process.exitCode = 1;

function optionValue(name) { return process.argv.find(arg => arg.startsWith(`--${name}=`))?.slice(name.length + 3); }
function optionNumber(name, fallback, min, max) {
  const raw = optionValue(name);
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) throw new Error(`--${name} must be an integer from ${min} to ${max}.`);
  return value;
}
function addMessage(item) { messages.push({ ...item, text: String(item.text || "").slice(0, 8000) }); if (messages.length > 200) messages.shift(); }

async function verifyRoom(evaluate, delay, pageUrl) {
  const outcome = { requested: true, unauthorizedStatus: null, authorizedStatus: null, snapshotBytes: null, commandStatus: null, commandSnapshotBytes: null, viewerWelcome: false, viewerResult: false, result: "FAIL" };
  let viewer;
  try {
    const page = new URL(pageUrl);
    await evaluate("document.getElementById('share-button')?.click(); document.getElementById('create-room')?.click()");
    const link = await evaluate(`(async()=>{const end=Date.now()+20000;while(Date.now()<end){const value=document.getElementById('share-link')?.value;if(value)return value;await new Promise(r=>setTimeout(r,100));}throw new Error('Room creation timed out');})()`, 25_000);
    const privateLink = new URL(link);
    const params = new URLSearchParams(privateLink.hash.slice(1));
    const id = params.get("room");
    const key = params.get("key");
    if (!id || !key) throw new Error("Room link was incomplete");
    const hostReady = await evaluate(`(async()=>{const end=Date.now()+20000;while(Date.now()<end){const button=document.getElementById('claim-host');if(button&&!button.disabled&&button.textContent.trim()==='Release simulation host')return true;await new Promise(r=>setTimeout(r,100));}return false;})()`, 25_000);
    outcome.hostClaimed = hostReady;
    if (!hostReady) throw new Error("Browser did not connect and claim the simulation host");
    await evaluate("document.querySelector('#share-dialog .dialog-close')?.click()");
    const endpoint = new URL(`/api/rooms/${encodeURIComponent(id)}/state`, page.origin);
    const unauthorized = await fetch(endpoint);
    outcome.unauthorizedStatus = unauthorized.status;
    const stateUrl = new URL(`/api/rooms/${encodeURIComponent(id)}/state`, page.origin);
    const authorized = await fetch(stateUrl, { headers: { Authorization: `Bearer ${key}` } });
    outcome.authorizedStatus = authorized.status;
    if (authorized.ok) {
      const data = await authorized.json();
      outcome.snapshotBytes = new TextEncoder().encode(data.state ? JSON.stringify(data.state) : "").byteLength;
    }

    const wsUrl = new URL(`/api/rooms/${encodeURIComponent(id)}/ws`, page.origin);
    wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
    viewer = new WebSocket(wsUrl, ["wsorb", `auth.${key}`]);
    const received = [];
    viewer.addEventListener("message", event => {
      try { received.push(JSON.parse(event.data)); } catch { /* Ignore malformed frames. */ }
    });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Viewer WebSocket timed out")), 15000);
      viewer.addEventListener("open", () => { clearTimeout(timer); resolve(); }, { once: true });
      viewer.addEventListener("error", () => { clearTimeout(timer); reject(new Error("Viewer WebSocket failed")); }, { once: true });
    });
    const welcomeEnd = Date.now() + 15000;
    while (!received.some(item => item.type === "welcome") && Date.now() < welcomeEnd) await delay(100);
    outcome.viewerWelcome = received.some(item => item.type === "welcome");
    if (!outcome.viewerWelcome) throw new Error("Viewer did not receive a room welcome");

    const viewerCommandId = `preview-viewer-${Date.now()}`;
    viewer.send(JSON.stringify({ type: "command", id: viewerCommandId, command: { op: "observe" } }));
    const viewerResultEnd = Date.now() + 35000;
    while (!received.some(item => item.type === "result" && item.id === viewerCommandId) && Date.now() < viewerResultEnd) await delay(100);
    const viewerResult = received.find(item => item.type === "result" && item.id === viewerCommandId);
    outcome.viewerResult = !!viewerResult;
    outcome.viewerResultSnapshotBytes = viewerResult?.state ? new TextEncoder().encode(JSON.stringify(viewerResult.state)).byteLength : 0;

    const observeResponse = await fetch(new URL(`/api/rooms/${encodeURIComponent(id)}/commands`, page.origin), {
      method: "POST", headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({ id: `preview-observe-${Date.now()}`, command: { op: "observe" } }),
    });
    outcome.commandStatus = observeResponse.status;
    if (!observeResponse.ok) {
      const failure = await observeResponse.json().catch(() => ({}));
      outcome.commandError = typeof failure.error === "string" ? failure.error.slice(0, 300) : `HTTP ${observeResponse.status}`;
      throw new Error(`Command endpoint returned HTTP ${observeResponse.status}`);
    }
    if (observeResponse.ok) {
      const result = await observeResponse.json();
      outcome.commandSnapshotBytes = result?.state ? new TextEncoder().encode(JSON.stringify(result.state)).byteLength : 0;
    }
    const stepResponse = await fetch(new URL(`/api/rooms/${encodeURIComponent(id)}/commands`, page.origin), {
      method: "POST", headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({ id: `preview-step-${Date.now()}`, command: { op: "step", seconds: 1 } }),
    });
    const stepped = stepResponse.ok ? await stepResponse.json() : null;
    outcome.stepStatus = stepResponse.status;
    if (!stepResponse.ok) throw new Error(`Step endpoint returned HTTP ${stepResponse.status}`);
    outcome.simulationTimeAfterStep = stepped?.state?.time_s ?? null;
    const broadcastEnd = Date.now() + 5000;
    while (!received.some(item => item.type === "state" && item.state?.time_s === outcome.simulationTimeAfterStep) && Date.now() < broadcastEnd) await delay(100);
    const broadcastState = received.find(item => item.type === "state" && item.state?.time_s === outcome.simulationTimeAfterStep);
    outcome.viewerSawStepBroadcast = !!broadcastState;
    outcome.broadcastSnapshotBytes = broadcastState?.state ? new TextEncoder().encode(JSON.stringify(broadcastState.state)).byteLength : 0;
    const refresh = await fetch(stateUrl, { headers: { Authorization: `Bearer ${key}` } });
    if (refresh.ok) {
      const data = await refresh.json();
      outcome.persistedSnapshotBytes = data.state ? new TextEncoder().encode(JSON.stringify(data.state)).byteLength : 0;
      outcome.snapshotPersisted = outcome.persistedSnapshotBytes > 0;
    }
    outcome.result = outcome.unauthorizedStatus === 401 && outcome.authorizedStatus === 200 && outcome.commandStatus === 200 && outcome.stepStatus === 200 && outcome.viewerWelcome && outcome.viewerResult && outcome.viewerResultSnapshotBytes > 64 * 1024 && outcome.viewerSawStepBroadcast && outcome.snapshotPersisted ? "PASS" : "FAIL";
  } catch (error) {
    outcome.error = String(error?.message || error).slice(0, 500);
  } finally {
    if (viewer && viewer.readyState < WebSocket.CLOSING) viewer.close(1000, "preview complete");
  }
  return outcome;
}
