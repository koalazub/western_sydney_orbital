import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { createReadStream, existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { dirname, extname, join, resolve, sep } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const worker = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = resolve(process.env.PREVIEW_OUTPUT_DIR || join(tmpdir(), "opencode", "wso-preview"));
if (output === dirname(worker) || output.startsWith(dirname(worker) + sep)) throw new Error("PREVIEW_OUTPUT_DIR must be outside the repository.");
const width = optionNumber("width", 1440, 320, 3840);
const height = optionNumber("height", 1000, 240, 2160);
const block = optionValue("block") || (process.argv.includes("--blockwasm") || process.argv.includes("--block-wasm") ? "wasm" : undefined);
const view = optionValue("view");
if (view && !["1", "2", "3", "4", "5", "6", "7", "orbit", "first_person", "third_person", "free", "overview", "ship_top", "ship_bottom"].includes(view)) throw new Error("--view must select debug view 1-5 or ship sensor 6/7.");
const runMission = process.argv.includes("--run-mission") || process.argv.includes("--run-strategy");
const doControls = process.argv.includes("--controls");
const doSensors = process.argv.includes("--sensors");
const doPlatform = process.argv.includes("--platform");
const doStep = process.argv.includes("--step");
const missionTimeout = optionNumber("mission-timeout", 120, 1, 600) * 1000;
if (process.argv.includes("--room")) throw new Error("--room is unsupported: the share-room UI was removed.");
if (block && block !== "wasm") throw new Error("--block must be wasm.");
await mkdir(output, { recursive: true });
await writeFile(join(output, "preview.json"), JSON.stringify({ result: "FAIL", error: "Browser verification has not completed." }, null, 2));
const chrome = process.env.CHROME_BIN || [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
].find(existsSync);
if (!chrome) throw new Error("Set CHROME_BIN to an installed Chrome or Chromium browser.");
const profile = await mkdtemp(join(output, "browser-"));
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
const graphicsFlags = process.argv.includes("--hardware") ? [] : ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"];
const child = spawn(chrome, [...(process.argv.includes("--headed") ? [] : ["--headless=new"]), "--force-device-scale-factor=1", `--user-data-dir=${profile}`, "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1", "--enable-unsafe-webgpu", "--enable-dawn-features=allow_unsafe_apis", ...graphicsFlags, "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
process.once("SIGTERM", () => { child.kill("SIGKILL"); process.exit(143); });
let browserError = "";
child.stderr.on("data", data => { browserError = (browserError + data).slice(-12_000); });
child.on("error", error => { browserError = `${browserError}\n${error.message}`; });
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
let socket;
let failed = false;
let report;
let capture;
let closeBrowser;
const commandTimers = new Set();
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
  const call = (method, params = {}, timeout = 30_000, sessionId) => new Promise((resolve, reject) => {
    const id = ++next;
    const timer = setTimeout(() => { commandTimers.delete(timer); pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, timeout);
    commandTimers.add(timer);
    pending.set(id, message => { clearTimeout(timer); commandTimers.delete(timer); message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result); });
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
      if (block === "wasm" && item.url?.includes(".wasm") && item.blockedReason) blockObserved = true;
    }
    if (message.method === "Network.responseReceived" && message.params.response.status >= 400) {
      requests.push({ type: "http-error", target: target?.type || "page", url: message.params.response.url, status: message.params.response.status });
      if (requests.length > 200) requests.shift();
    }
  };
  closeBrowser = () => call("Browser.close", {}, 5000);
  const evaluate = async (expression, timeout = 30_000) => {
    const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, timeout);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result?.value;
  };
  const readRuntime = (options = {}) => evaluate(`(${runtimeSnapshot})(${JSON.stringify(options)})`);
  const input = createInput(call, evaluate, readRuntime, delay);
  await call("Runtime.enable");
  await call("Page.enable");
  await call("Page.bringToFront");
  await call("Emulation.setFocusEmulationEnabled", { enabled: true });
  await call("Network.enable");
  await call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
  if (block === "wasm") await call("Network.setBlockedURLs", { urls: ["*/*.wasm*", "*.wasm*"] });
  await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: process.argv.includes("--headed") ? 0 : 1, mobile: width <= 600 });
  await call("Page.addScriptToEvaluateOnNewDocument", { source: `globalThis.__wsoGpuPreflight=(()=>{if(!navigator.gpu)return Promise.resolve({available:false,error:'navigator.gpu unavailable'});return navigator.gpu.requestAdapter().then(a=>a?({available:true,isFallbackAdapter:a.isFallbackAdapter??a.info?.isFallbackAdapter??null,info:a.info?{vendor:a.info.vendor,architecture:a.info.architecture,device:a.info.device,description:a.info.description}:null}):({available:false,error:'requestAdapter returned null'}),e=>({available:false,error:String(e)}))})()` });
  await call("Page.navigate", { url });

  const stateExpression = `(()=>{const byId=id=>document.getElementById(id);const text=id=>byId(id)?.textContent?.trim()??null;const c=byId('simulation-canvas');const visible=e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');return {title:document.title,engine:text('sim-status'),worldMessage:text('world-message'),bootVisible:!!byId('boot-screen')&&visible(byId('boot-screen')),externalPanels:Object.fromEntries(['editor','console','metrics','telemetry','shuttle-controls','python-console'].map(id=>[id,!!byId(id)])),externalControls:[...document.querySelectorAll('input,textarea,select,button,[contenteditable=true]')].filter(e=>!e.closest('#boot-screen')&&visible(e)).map(e=>({tag:e.tagName,id:e.id})),canvas:c?{width:c.width,height:c.height,clientWidth:c.clientWidth,clientHeight:c.clientHeight,rect:{width:c.getBoundingClientRect().width,height:c.getBoundingClientRect().height}}:null,viewport:{width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,horizontalOverflow:document.documentElement.scrollWidth>innerWidth}};})()`;
  capture = async path => {
    const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: process.argv.includes("--full-page") });
    await writeFile(join(output, path), Buffer.from(screenshot.data, "base64"));
  };
  const waitForEngine = async () => evaluate(`(async()=>{const ready=new Set(['3D ready','World ready']);const errors=new Set(['3D unavailable','World unavailable','Engine failed']);const end=Date.now()+60000;while(Date.now()<end){const s=document.getElementById('sim-status')?.textContent?.trim();if(ready.has(s)||errors.has(s))return s;await new Promise(r=>setTimeout(r,100));}return document.getElementById('sim-status')?.textContent?.trim()||'missing';})()`, 65_000);
  const engine = await waitForEngine();
  const ready = ["3D ready", "World ready"].includes(engine);
  if (ready) await input.waitFrames();
  const gpu = await evaluate(`globalThis.__wsoGpuPreflight||{available:false,error:'preflight missing'}`, 15_000);
  const before = await evaluate(stateExpression);
  const runtimeBefore = ready ? await readRuntime({ physics: true }) : null;
  await capture("before.png");
  let controls = null;
  if (doControls && ready) controls = await exerciseControls({ evaluate, call, readRuntime, input, capture });
  const platform = doPlatform && ready ? await exerciseViewport({ evaluate, readRuntime, input, capture }) : null;
  let step = { requested: doStep, changed: null, beforeTime: null, afterTime: null };
  if (doStep && ready) {
    await input.focusScene();
    const stateBefore = await readRuntime();
    await input.pressKey("Period");
    const stateAfter = await readRuntime();
    await input.waitFrames(3);
    const settled = await readRuntime();
    const timeBefore = stateBefore?.snapshot?.time_s, timeAfter = stateAfter?.snapshot?.time_s;
    step = { requested: true, beforeTime: timeBefore, afterTime: timeAfter, changed: Number.isFinite(timeBefore) && Number.isFinite(timeAfter) && timeAfter > timeBefore, paused: stateBefore?.runtime?.running === false && stateAfter?.runtime?.running === false, stableAfterStep: timeAfter === settled?.snapshot?.time_s };
    step.passed = step.changed && step.paused && step.stableAfterStep;
  }
  let mission = { requested: runMission, status: null, timedOut: false, passed: false };
  if (runMission && ready) mission = await exerciseMission({ evaluate, readRuntime, input, delay, timeout: missionTimeout });
  const sensors = doSensors && ready ? await exerciseSensors({ evaluate, readRuntime, input, capture }) : null;
  let selectedView = null;
  if (view && ready) {
    const viewKey = ({ orbit:"1", first_person:"2", third_person:"3", free:"4", overview:"5", ship_top:"6", ship_bottom:"7" })[view] || view;
    const expected = ({ "1":"orbit", "2":"first_person", "3":"third_person", "4":"free", "5":"overview", "6":"ship_top", "7":"ship_bottom" })[viewKey];
    await input.focusScene();
    await input.pressKey(viewKey);
    const actual = (await readRuntime())?.camera?.mode;
    selectedView = { expected, actual, passed: actual === expected };
  }
  await Promise.allSettled(targetSetups);
  await delay(250);
  const after = await evaluate(stateExpression);
  const runtimeAfter = ready ? await readRuntime({ physics: true }) : null;
  const poseBefore = runtimeBefore?.snapshot?.shuttle?.position;
  const poseAfter = runtimeAfter?.snapshot?.shuttle?.position;
  const poseChanged = vectorDistance(poseBefore, poseAfter) > 0.000001;
  const simulationTimeChanged = runtimeBefore?.snapshot?.time_s !== undefined && runtimeAfter?.snapshot?.time_s !== undefined && runtimeBefore.snapshot.time_s !== runtimeAfter.snapshot.time_s;
  await capture("preview.png");
  if (block === "wasm") blockObserved ||= requests.some(item => item.type === "request-failed" && item.url?.includes(".wasm") && (item.blockedReason || /BLOCKED_BY_CLIENT/.test(item.error)));
  const expectedBlockedOutcome = block === "wasm" && blockObserved && ["3D unavailable", "World unavailable", "Engine failed"].includes(engine) && after.bootVisible && !!after.worldMessage;
  const errors = messages.filter(item => item.type === "exception" || item.type === "error");
  const unexpectedNetworkFailures = requests.filter(item => !(block === "wasm" && blockObserved && item.url?.includes(".wasm")));
  const runtimeReady = ["3D ready", "World ready"].includes(engine) && ["3D ready", "World ready"].includes(after.engine);
  if (block ? !expectedBlockedOutcome : !runtimeReady || gpu?.available !== true) failed = true;
  if (unexpectedNetworkFailures.length || (!block && errors.length)) failed = true;
  const canvasOnly = [before, after].every(state => !Object.values(state.externalPanels).some(Boolean) && !state.externalControls.length && !state.viewport.horizontalOverflow && state.canvas?.width > 0 && state.canvas?.height > 0);
  const runtimeReadable = [runtimeBefore, runtimeAfter].every(state => !state?.error && state?.renderer?.ready === true && Number.isFinite(state.snapshot?.time_s) && vectorDistance(state.snapshot?.shuttle?.position, [0, 0, 0]) !== null && vectorDistance(state.snapshot?.shuttle?.velocity, [0, 0, 0]) !== null && vectorDistance(state.snapshot?.landing_pad?.position, [0, 0, 0]) !== null && ["running", "success", "failed_damage"].includes(state.snapshot?.mission?.status));
  if (!canvasOnly || (!block && (before.bootVisible || after.bootVisible || !runtimeReadable))) failed = true;
  if (doStep && !step.passed) failed = true;
  if (runMission && !mission.passed) failed = true;
  if (view && !selectedView?.passed) failed = true;
  if (doControls && (!controls || controls.result !== "PASS")) failed = true;
  if (doSensors && sensors?.result !== "PASS") failed = true;
  if (doPlatform && (!platform?.maximize?.passed || !platform?.fullscreen?.passed)) failed = true;
  report = { url: redactUrl(url), viewport: { width, height }, requested: { block, view, step: doStep, runMission, controls: doControls, sensors: doSensors, platform: doPlatform }, engine, gpu, softwareRenderer: gpu?.isFallbackAdapter === true || /swiftshader|software|llvmpipe/i.test(JSON.stringify(gpu?.info)), startup: await evaluate("globalThis.__wsorbStartup || null"), state: { before, after }, canvasOnly, runtime: { readable: runtimeReadable, before: runtimeBefore, after: runtimeAfter, motion: { shuttlePoseChanged: poseChanged, simulationTimeChanged } }, selectedView, controls, sensors, platform, mission, step, blockedOutcomeObserved: block ? expectedBlockedOutcome : null, messages: messages.slice(-200), networkFailures: requests.slice(-200), result: failed ? "FAIL" : "PASS" };
  await writeFile(join(output, "preview.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  console.log(`Screenshots: ${join(output, "before.png")} and ${join(output, "preview.png")}`);
} catch (error) {
  failed = true;
  await capture?.("failure.png").catch(() => {});
  report = { ...report, url: redactUrl(url), viewport: { width, height }, error: error.stack || String(error), messages, networkFailures: requests, browserError, result: "FAIL" };
  await writeFile(join(output, "preview.json"), JSON.stringify(report, null, 2));
  console.error(JSON.stringify(report, null, 2));
} finally {
  await closeBrowser?.().catch(() => {});
  for (const timer of commandTimers) clearTimeout(timer);
  commandTimers.clear();
  socket?.close();
  child.kill("SIGTERM");
  await new Promise(resolve => { if (child.exitCode !== null) resolve(); else { child.once("exit", resolve); setTimeout(() => { child.kill("SIGKILL"); resolve(); }, 3000).unref(); } });
  server.close();
  await rm(profile, { recursive: true, force: true }).catch(() => {});
}
// This is a one-shot CLI. Chrome/CDP can leave idle HTTP handles after all
// owned browser/server resources are closed; flush the report before exiting.
await Promise.all([
  new Promise(resolve => process.stdout.write("", resolve)),
  new Promise(resolve => process.stderr.write("", resolve)),
]);
process.exit(failed ? 1 : 0);

function optionValue(name) {
  const inline = process.argv.find(arg => arg.startsWith(`--${name}=`));
  if (inline !== undefined) {
    const value = inline.slice(name.length + 3);
    if (!value) throw new Error(`--${name} requires a value.`);
    return value;
  }
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1) return undefined;
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) throw new Error(`--${name} requires a value.`);
  return value;
}
function optionNumber(name, fallback, min, max) {
  const raw = optionValue(name);
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) throw new Error(`--${name} must be an integer from ${min} to ${max}.`);
  return value;
}
function addMessage(item) { messages.push({ ...item, text: String(item.text || "").slice(0, 8000) }); if (messages.length > 200) messages.shift(); }

function redactUrl(raw) { const u = new URL(raw); if (u.hash) u.hash = "[redacted]"; return u.toString(); }

// This function is serialized into the page. Keep all module reads here; input
// checks below interact with the game through CDP, never command/view setters.
async function runtimeSnapshot({ statusOnly = false, physics = false } = {}) {
  try {
    const m = await import("/sim/wsorb_bevy.js");
    const parse = value => typeof value === "string" ? JSON.parse(value) : value;
    const status = { renderer: parse(m.renderer_status()), camera: parse(m.camera_status()), runtime: parse(m.runtime_status()) };
    if (statusOnly) return status;
    const response = parse(m.command(JSON.stringify({ op: "observe" })));
    if (!response?.ok || !response.state) throw new Error(response?.error || "observe returned no state");
    const s = response.state, assets = s.assets;
    if (!Array.isArray(assets)) throw new Error("observe returned no assets");
    const byKind = {};
    for (const asset of assets) byKind[asset.kind] = (byKind[asset.kind] || 0) + 1;
    const pick = a => a ? { id: a.id, kind: a.kind, status: a.status, position: a.position, velocity: a.velocity, rotation: a.rotation, angular_velocity_rad_s: a.angular_velocity_rad_s, commanded_accel_mps2: a.commanded_accel_mps2, mass_kg: a.mass_kg, fuel_kg: a.fuel_kg } : null;
    const result = { ...status, snapshot: { time_s: s.time_s, seed: s.seed, frame: s.frame, environment: s.environment || null, shuttle: pick(assets.find(a => a.id === "Lander" || a.id === "Shuttle")), target: pick(assets.find(a => a.id === s.mission?.target_id || a.id === "ReturnShip" || a.id === "Target")), landing_pad: pick(assets.find(a => a.id === "LandingPad")), mission: s.mission || null, asset_counts: { total: assets.length, by_kind: byKind }, assets: assets.slice(0, 3).map(pick) }, radar_count: Array.isArray(s.observations) ? s.observations.length : null };
    if (physics) {
      // Compare every physical entity and mission field, not the camera's frame
      // counter or just the lander's first coordinate.
      const bytes = new TextEncoder().encode(JSON.stringify(s));
      const hash = await crypto.subtle.digest("SHA-256", bytes);
      result.physics = { time_s: s.time_s, asset_count: assets.length, sha256: Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, "0")).join("") };
    }
    return result;
  } catch (error) { return { error: String(error) }; }
}

function createInput(call, evaluate, readRuntime, delay) {
  // Waiting for rendered Bevy frames also works when software WebGPU is much
  // slower than Chrome's animation callbacks. Never release in the same frame.
  const waitFrames = async (count = 2, minimumMs = 80) => {
    const before = await readRuntime({ statusOnly: true });
    const frame = before?.renderer?.rendered_frames;
    if (!Number.isFinite(frame)) throw new Error(`Cannot synchronize input with Bevy frames: ${before?.error || "rendered_frames missing"}`);
    const deadline = Date.now() + 15_000;
    await delay(minimumMs);
    while (Date.now() < deadline) {
      const state = await readRuntime({ statusOnly: true });
      if (state?.renderer?.error || state?.error) throw new Error(state.error || state.renderer.error);
      if (state?.renderer?.rendered_frames >= frame + count) return;
      await delay(25);
    }
    throw new Error(`Bevy did not render ${count} frames after frame ${frame}; input verification stopped.`);
  };
  const keyEvent = async (type, name, synchronize = true) => {
    const { code, key, vk } = keyDescriptor(name);
    await call("Input.dispatchKeyEvent", { type, code, key, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    if (synchronize) await waitFrames();
  };
  const keyDown = (name, synchronize) => keyEvent("keyDown", name, synchronize);
  const keyUp = (name, synchronize) => keyEvent("keyUp", name, synchronize);
  const pressKey = async (name, synchronize) => {
    try { await keyDown(name, synchronize); } finally { await keyUp(name, synchronize); }
  };
  const mouse = async (type, x, y, { button = "none", buttons = 0, deltaY = 0 } = {}) => {
    await call("Input.dispatchMouseEvent", { type, x, y, button, buttons, pointerType: "mouse", ...(type === "mouseWheel" ? { deltaX: 0, deltaY } : {}), ...(type === "mousePressed" || type === "mouseReleased" ? { clickCount: 1 } : {}) });
    await waitFrames(2, 50);
  };
  const click = async (x, y, button = "left") => {
    const buttons = button === "right" ? 2 : button === "middle" ? 4 : 1;
    await mouse("mouseMoved", x, y);
    try { await mouse("mousePressed", x, y, { button, buttons }); }
    finally { await mouse("mouseReleased", x, y, { button }); }
  };
  const canvasBox = () => evaluate(`(()=>{const c=document.getElementById('simulation-canvas');if(!c)return null;const r=c.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})()`);
  const scenePoint = async () => {
    const box = await canvasBox();
    if (!box?.width || !box?.height) throw new Error("Canvas missing or has no rendered dimensions");
    return { x: Math.round(box.x + box.width * .35), y: Math.round(box.y + box.height * .45) };
  };
  const moveToScene = async () => { const p = await scenePoint(); await mouse("mouseMoved", p.x, p.y); return p; };
  const focusScene = async ({ preserveFocus = false } = {}) => {
    await evaluate("document.getElementById('simulation-canvas')?.focus()");
    const p = await scenePoint();
    // Right-click activates the DOM canvas without left-click object picking.
    await click(p.x, p.y, preserveFocus ? "right" : "left");
    if ((await readRuntime({ statusOnly: true }))?.camera?.active !== true) throw new Error("A real canvas click did not activate the camera");
    return p;
  };
  const drag = async (button, dx, dy) => {
    const p = await moveToScene(), buttons = button === "right" ? 2 : button === "middle" ? 4 : 1;
    try {
      await mouse("mousePressed", p.x, p.y, { button, buttons });
      for (let i = 1; i <= 6; i++) await mouse("mouseMoved", p.x + dx * i / 6, p.y + dy * i / 6, { button, buttons });
    } finally { await mouse("mouseReleased", p.x + dx, p.y + dy, { button }); }
  };
  return { waitFrames, keyDown, keyUp, pressKey, mouse, click, canvasBox, moveToScene, focusScene, drag };
}

function keyDescriptor(name) {
  const code = /^[0-9]$/.test(name) ? `Digit${name}` : /^[a-z]$/i.test(name) ? `Key${name.toUpperCase()}` : name;
  const special = { Space: [" ", 32], Period: [".", 190], Comma: [",", 188], Slash: ["/", 191], Escape: ["Escape", 27], Enter: ["Enter", 13], Tab: ["Tab", 9], ShiftLeft: ["Shift", 16] };
  const [key, vk] = special[code] || (code.startsWith("Digit") ? [code.slice(5), Number(code.slice(5)) + 48] : code.startsWith("Key") ? [code.slice(3).toLowerCase(), code.charCodeAt(3)] : []);
  if (!key || !vk) throw new Error(`Unsupported preview key: ${name}`);
  return { code, key, vk };
}

async function exerciseControls({ evaluate, call, readRuntime, input, capture }) {
  const result = { modes: {}, orbitDrag: false, panDrag: false, wheelZoom: false, freeMove: false, focusGuard: false, mousePicking: false, focusCycle: false, keyboardOverHud: false, worldUnchangedDuringCameraInput: false, fullscreen: null, maximize: null, nativeHud: null, evidence: {} };
  try {
    // Take the physical baseline before focusing, selecting a mode or moving
    // the camera. Native mission-control clicks are checked separately below.
    const baseline = await readRuntime({ physics: true });
    if (baseline.runtime?.running !== false || !baseline.physics?.sha256) throw new Error("Camera verification requires a readable, paused mission");
    result.evidence.physicsBeforeCamera = baseline.physics;
    const pose = async () => (await readRuntime({ statusOnly: true }))?.camera;
    await evaluate(`(()=>{const i=document.createElement('input');i.id='__preview_focus_guard';i.setAttribute('aria-label','temporary preview focus guard');i.style.cssText='position:fixed;left:0;top:0;width:2px;height:2px;z-index:99999';document.body.append(i)})()`);
    try {
      const { root } = await call("DOM.getDocument");
      const { nodeId } = await call("DOM.querySelector", { nodeId: root.nodeId, selector: "#__preview_focus_guard" });
      await call("DOM.focus", { nodeId });
      await delay(150);
      const beforeGuard = await readRuntime({ physics: true });
      // Canvas blur may suspend Bevy redraws. Test real DOM input without
      // demanding game frames until focus returns to the canvas.
      await input.keyDown("KeyW", false);
      await delay(300);
      await input.keyUp("KeyW", false);
      for (const key of ["5", "Space", "Period", "KeyT"]) await input.pressKey(key, false);
      await call("Input.insertText", { text: "WASD" });
      await delay(150);
      const guarded = await evaluate(`(()=>{const i=document.getElementById('__preview_focus_guard');return {focused:document.activeElement===i,value:i?.value}})()`);
      const afterGuard = await readRuntime({ physics: true });
      result.focusGuard = guarded.focused && guarded.value === "WASD" && samePhysics(beforeGuard, afterGuard) && sameCamera(beforeGuard.camera, afterGuard.camera) && beforeGuard.runtime.running === afterGuard.runtime.running && beforeGuard.runtime.rate === afterGuard.runtime.rate;
      result.evidence.focusGuard = { input: guarded, before: beforeGuard.camera, after: afterGuard.camera, physicsBefore: beforeGuard.physics, physicsAfter: afterGuard.physics };
    } finally { await evaluate("document.getElementById('__preview_focus_guard')?.remove()"); }

    await input.focusScene({ preserveFocus: true });
    for (const [key, name] of [["1", "orbit"], ["2", "first_person"], ["3", "third_person"], ["4", "free"], ["5", "overview"]]) {
      await input.pressKey(key);
      const actual = await pose();
      result.modes[name] = { ...actual, passed: actual?.mode === name };
      await capture(`controls-${name}.png`);
    }
    await capture("controls-overview.png");
    await input.pressKey("1");
    const orbitBefore = await pose();
    await input.drag("left", 52, 8);
    const orbitAfter = await pose();
    result.evidence.orbit = cameraDelta(orbitBefore, orbitAfter);
    result.orbitDrag = orbitAfter?.mode === "orbit" && orbitAfter?.focus === orbitBefore?.focus && result.evidence.orbit.positionDeltaM > .01 && result.evidence.orbit.rotationDelta > .000001;

    const panBefore = await pose();
    await input.drag("right", 38, 24);
    const panAfter = await pose();
    await input.waitFrames(3);
    const panSettled = await pose();
    result.evidence.pan = { ...cameraDelta(panBefore, panAfter), settled: panSettled };
    result.panDrag = panAfter?.mode === "orbit" && panAfter?.focus === panBefore?.focus && result.evidence.pan.positionDeltaM > .01 && Number.isFinite(result.evidence.pan.rotationDelta) && result.evidence.pan.rotationDelta < .03 && sameCamera(panAfter, panSettled);

    const zoomBefore = await pose(), wheelPoint = await input.moveToScene();
    await input.mouse("mouseWheel", wheelPoint.x, wheelPoint.y, { deltaY: -120 });
    const zoomAfter = await pose();
    result.evidence.zoom = cameraDelta(zoomBefore, zoomAfter);
    result.wheelZoom = zoomAfter?.mode === "orbit" && zoomAfter?.focus === zoomBefore?.focus && Number.isFinite(zoomBefore?.distance_m) && Number.isFinite(zoomAfter?.distance_m) && zoomAfter.distance_m < zoomBefore.distance_m - .01 && result.evidence.zoom.positionDeltaM > .01;

    await input.pressKey("4");
    const moveBefore = await pose();
    try { await input.keyDown("KeyW"); await input.waitFrames(3, 300); }
    finally { await input.keyUp("KeyW"); }
    const moveAfter = await pose();
    result.evidence.freeMove = cameraDelta(moveBefore, moveAfter);
    const forwardDistance = forwardTravel(moveBefore, moveAfter);
    result.evidence.freeMove.forwardDistanceM = forwardDistance;
    result.freeMove = moveBefore?.mode === "free" && moveAfter?.mode === "free" && moveAfter?.focus === moveBefore?.focus && result.evidence.freeMove.positionDeltaM > .01 && forwardDistance > .01;

    await input.pressKey("KeyF");
    const pickBefore = await pose(), pickBox = await input.canvasBox();
    await input.click(pickBox.x + pickBox.width * .5, pickBox.y + pickBox.height * .5);
    const pickedLander = await pose();
    await input.click(pickBox.x + pickBox.width * .98, pickBox.y + pickBox.height * .4);
    const picked = await pose();
    for (let i = 0; i < 8 && (await pose())?.focus !== "Shuttle"; i++) await input.pressKey("Comma");
    const pickRestored = await pose();
    result.mousePicking = pickBefore?.focus === "Shuttle" && pickedLander?.focus === "Shuttle" && ["Phobos", "LandingPad"].includes(picked?.focus) && pickRestored?.focus === "Shuttle";
    result.evidence.mousePicking = { before: pickBefore, pickedLander, picked, restored: pickRestored };
    const focusBefore = await pose();
    await input.pressKey("Slash");
    const focusNext = await pose();
    await input.pressKey("Comma");
    const focusRestored = await pose();
    result.focusCycle = focusBefore?.focus === "Shuttle" && focusNext?.focus === "Target" && focusRestored?.focus === "Shuttle" && [focusBefore, focusNext, focusRestored].every(p => p?.mode === "orbit");
    result.evidence.focusCycle = { before: focusBefore, next: focusNext, restored: focusRestored };
    const viewport = await exerciseViewport({ evaluate, readRuntime, input, capture });
    result.maximize = viewport.maximize;
    result.fullscreen = viewport.fullscreen;
    const hoveredButton = (await locateHudButtons({ call, evaluate, input }))[0];
    if (!hoveredButton) throw new Error("No HUD button available for keyboard hover verification");
    await input.mouse("mouseMoved", hoveredButton.x + hoveredButton.width / 2, hoveredButton.y + hoveredButton.height / 2);
    await input.pressKey("4");
    result.keyboardOverHud = (await pose())?.mode === "free";
    await input.pressKey("1");

    const afterCamera = await readRuntime({ physics: true });
    result.evidence.physicsAfterCamera = afterCamera.physics;
    result.worldUnchangedDuringCameraInput = samePhysics(baseline, afterCamera) && afterCamera.runtime?.running === false;
    result.nativeHud = await exerciseNativeHud({ evaluate, call, readRuntime, input, capture, baseline });
    result.result = Object.keys(result.modes).length === 5 && Object.values(result.modes).every(p => p.passed) && result.orbitDrag && result.panDrag && result.wheelZoom && result.freeMove && result.focusGuard && result.mousePicking && result.focusCycle && result.keyboardOverHud && result.worldUnchangedDuringCameraInput && result.maximize?.passed && (result.fullscreen?.supported === false || result.fullscreen?.passed) && result.nativeHud?.result === "PASS" ? "PASS" : "FAIL";
  } catch (error) { result.error = error.stack || String(error); result.result = "FAIL"; }
  return result;
}

async function exerciseViewport({ evaluate, readRuntime, input, capture }) {
  const read = async () => {
    const dom = await evaluate(`(()=>{const c=document.getElementById('simulation-canvas'),v=document.getElementById('simulation-viewport'),r=c.getBoundingClientRect();return {fullscreenEnabled:document.fullscreenEnabled===true&&typeof v.requestFullscreen==='function',fullscreenElement:document.fullscreenElement?.id||null,windowed:v.classList.contains('windowed'),width:innerWidth,height:innerHeight,canvas:{x:r.x,y:r.y,width:r.width,height:r.height,bufferWidth:c.width,bufferHeight:c.height},dpr:devicePixelRatio}})()`);
    return { ...dom, diagnostic: await evaluate("globalThis.__wsorbFullscreenFailure || null"), runtime: (await readRuntime({ statusOnly: true }))?.runtime };
  };
  await input.focusScene({ preserveFocus: true });
  const before = await read();
  // Normalize by real input if the hosted page was already windowed.
  if (before.windowed) await input.pressKey("KeyM");
  const maximizedBefore = await read();
  await input.pressKey("KeyM");
  const windowed = await read();
  await capture("windowed.png");
  await input.pressKey("KeyM");
  const maximizedAfter = await read();
  const maximize = { before: maximizedBefore, windowed, after: maximizedAfter, passed: windowed.windowed && windowed.runtime?.maximized === false && windowed.canvas.width < maximizedBefore.canvas.width - 1 && windowed.canvas.height < maximizedBefore.canvas.height - 1 && fillsViewport(maximizedAfter) && maximizedAfter.runtime?.maximized === true && !maximizedAfter.windowed && bufferMatches(windowed) && bufferMatches(maximizedAfter) };
  const fullBefore = await read();
  if (!fullBefore.fullscreenEnabled) return { maximize, fullscreen: { supported: false, before: fullBefore, passed: null, status: "unsupported_browser_api" } };
  await input.pressKey("KeyX");
  await input.waitFrames(3, 200);
  const fullAfter = await read();
  const entered = fullAfter.fullscreenElement === "simulation-viewport" && fullAfter.runtime?.fullscreen === true;
  let exited = null;
  if (entered) {
    await capture("gamefull.png");
    await input.pressKey("KeyX");
    await input.waitFrames(3, 200);
    exited = await read();
  }
  const fullscreen = { supported: true, before: fullBefore, after: fullAfter, exited, apiEntered: entered, passed: entered && fillsViewport(fullAfter) && bufferMatches(fullAfter) && exited?.fullscreenElement === null && exited?.runtime?.fullscreen === false && fillsViewport(exited), status: entered ? "entered" : "failed_to_enter" };
  if (fullscreen.passed) fullscreen.status = "verified_enter_and_exit";
  return { maximize, fullscreen };
}

async function exerciseNativeHud({ evaluate, call, readRuntime, input, capture, baseline }) {
  const result = { locationMethod: "screenshot button rectangles in native HUD source order", buttons: null, step: null, playback: null, rates: [], reset: null, result: "FAIL" };
  try {
    const actions = async (allowDisabledStep = false) => {
      await input.moveToScene();
      const buttons = await locateHudButtons({ call, evaluate, input });
      const running = (await readRuntime({ statusOnly: true }))?.runtime?.running;
      if (buttons.length !== 8 && !(allowDisabledStep && running && buttons.length === 7)) throw new Error(`Expected eight native mission buttons, found ${buttons.length}: ${JSON.stringify(buttons)}`);
      return buttons;
    };
    const clickAction = async index => {
      const buttons = await actions(index === 0), b = buttons[index];
      await input.click(b.x + b.width / 2, b.y + b.height / 2);
      return b;
    };
    result.buttons = await actions();
    await capture("native-hud.png");
    const stepBefore = await readRuntime();
    const stepButton = await clickAction(1);
    const stepAfter = await readRuntime();
    await input.waitFrames(3);
    const stepSettled = await readRuntime();
    result.step = { button: stepButton, beforeTime: stepBefore.snapshot?.time_s, afterTime: stepAfter.snapshot?.time_s, settledTime: stepSettled.snapshot?.time_s, passed: stepBefore.runtime?.running === false && stepAfter.runtime?.running === false && Number.isFinite(stepBefore.snapshot?.time_s) && stepAfter.snapshot?.time_s > stepBefore.snapshot.time_s && stepSettled.snapshot?.time_s === stepAfter.snapshot.time_s };

    const runButton = await clickAction(0);
    await input.waitFrames(4, 300);
    const running = await readRuntime();
    const pauseButton = await clickAction(0);
    const paused = await readRuntime();
    await input.waitFrames(3);
    const pausedSettled = await readRuntime();
    result.playback = { runButton, pauseButton, running: { runtime: running.runtime, time_s: running.snapshot?.time_s }, paused: { runtime: paused.runtime, time_s: paused.snapshot?.time_s }, passed: running.runtime?.running === true && running.snapshot?.time_s > stepAfter.snapshot?.time_s && paused.runtime?.running === false && pausedSettled.snapshot?.time_s === paused.snapshot?.time_s };

    let previousRate = paused.runtime?.rate;
    for (let i = 0; i < 3; i++) {
      const expected = ({ 1: 5, 5: 10, 10: 1 })[previousRate];
      const button = await clickAction(4), after = await readRuntime();
      result.rates.push({ button, before: previousRate, expected, actual: after.runtime?.rate, passed: Number.isFinite(expected) && after.runtime?.rate === expected && after.runtime?.running === false && after.snapshot?.time_s === paused.snapshot?.time_s });
      previousRate = after.runtime?.rate;
    }

    const beforeReset = await readRuntime({ physics: true });
    const resetButton = await clickAction(2);
    const promptState = await readRuntime({ physics: true });
    const box = await input.canvasBox();
    const promptButtons = await locateHudButtons({ call, evaluate, input, region: { x: box.x + box.width * .05, y: box.y + box.height * .35, width: box.width * .9, height: Math.min(190, box.height * .65) } });
    // Reset and Cancel are the two buttons in the native modal, not DOM buttons.
    const left = box.x + box.width * .05 + 10;
    const confirmation = promptButtons.filter(b => Math.abs(b.x - left) < 8);
    await capture("native-reset.png");
    if (confirmation.length !== 2) throw new Error(`Could not identify both native reset confirmation buttons: ${JSON.stringify(promptButtons)}`);
    const confirmButton = confirmation[0];
    await input.click(confirmButton.x + confirmButton.width / 2, confirmButton.y + confirmButton.height / 2);
    const reset = await readRuntime({ physics: true });
    result.reset = { button: resetButton, confirmButton, promptButtons: confirmation, promptDidNotReset: samePhysics(beforeReset, promptState), beforeTime: beforeReset.snapshot?.time_s, afterTime: reset.snapshot?.time_s, phase: reset.snapshot?.mission?.phase, shuttle: reset.snapshot?.shuttle, passed: beforeReset.snapshot?.time_s > 0 && samePhysics(beforeReset, promptState) && reset.runtime?.running === false && reset.snapshot?.time_s === 0 && reset.snapshot?.mission?.phase === "on_pad" && reset.snapshot?.mission?.status === "running" && samePhysics(baseline, reset) };
    await input.focusScene();
    await input.pressKey("Tab");
    await input.pressKey("Tab");
    const keyboardBefore = await readRuntime();
    await input.pressKey("Enter");
    const keyboardAfter = await readRuntime();
    await input.waitFrames(3);
    const keyboardSettled = await readRuntime();
    result.keyboardStep = { beforeTime: keyboardBefore.snapshot?.time_s, afterTime: keyboardAfter.snapshot?.time_s, passed: keyboardBefore.runtime?.running === false && keyboardAfter.runtime?.running === false && keyboardAfter.snapshot?.time_s > keyboardBefore.snapshot?.time_s && keyboardAfter.snapshot?.time_s === keyboardSettled.snapshot?.time_s };
    result.result = result.step.passed && result.playback.passed && result.rates.length === 3 && result.rates.every(rate => rate.passed) && result.reset.passed && result.keyboardStep.passed ? "PASS" : "FAIL";
  } catch (error) { result.error = error.stack || String(error); }
  return result;
}

async function locateHudButtons({ call, evaluate, input, region }) {
  const box = await input.canvasBox();
  const viewport = await evaluate("({width:innerWidth,height:innerHeight})");
  const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  return evaluate(`(${screenshotButtons})(${JSON.stringify(screenshot.data)},${JSON.stringify({ viewport, region: region || { x: box.x, y: box.y, width: box.width, height: box.height * .48 } })})`);
}

// Decode CDP's screenshot offscreen. This reads rendered button backgrounds;
// it neither changes the WebGPU canvas nor adds an HTML testing interface.
async function screenshotButtons(base64, { viewport, region }) {
  const image = await createImageBitmap(new Blob([Uint8Array.from(atob(base64), c => c.charCodeAt(0))], { type: "image/png" }));
  try {
    const canvas = new OffscreenCanvas(image.width, image.height), context = canvas.getContext("2d");
    context.drawImage(image, 0, 0);
    const { data } = context.getImageData(0, 0, image.width, image.height);
    const sx = image.width / viewport.width, sy = image.height / viewport.height;
    const x0 = Math.max(0, Math.floor(region.x * sx)), y0 = Math.max(0, Math.floor(region.y * sy));
    const x1 = Math.min(image.width, Math.ceil((region.x + region.width) * sx)), y1 = Math.min(image.height, Math.ceil((region.y + region.height) * sy));
    const w = x1 - x0, h = y1 - y0, mask = new Uint8Array(w * h), queue = new Int32Array(w * h);
    // Enabled BUTTON, SELECTED, FOCUSED and HOVERED colors. Disabled controls
    // can blend into a translucent panel and must not join its pixel region.
    const palette = [[31, 48, 64], [31, 87, 107], [48, 117, 145], [46, 79, 97]];
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const i = ((y + y0) * image.width + x + x0) * 4;
      if (palette.some(c => Math.abs(data[i] - c[0]) <= 5 && Math.abs(data[i + 1] - c[1]) <= 5 && Math.abs(data[i + 2] - c[2]) <= 5)) mask[y * w + x] = 1;
    }
    const buttons = [];
    for (let start = 0; start < mask.length; start++) {
      if (!mask[start]) continue;
      let head = 0, tail = 1, minX = w, minY = h, maxX = 0, maxY = 0;
      queue[0] = start; mask[start] = 0;
      while (head < tail) {
        const i = queue[head++], x = i % w, y = Math.floor(i / w);
        minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y);
        for (const n of [x > 0 ? i - 1 : -1, x + 1 < w ? i + 1 : -1, y > 0 ? i - w : -1, y + 1 < h ? i + w : -1]) {
          if (n >= 0 && mask[n]) { mask[n] = 0; queue[tail++] = n; }
        }
      }
      const width = (maxX - minX + 1) / sx, height = (maxY - minY + 1) / sy;
      const fill = tail / ((maxX - minX + 1) * (maxY - minY + 1));
      if (width >= 12 && height >= 30 && height <= 90 && fill >= .55) buttons.push({ x: (minX + x0) / sx, y: (minY + y0) / sy, width, height, backgroundFill: fill });
    }
    const rows = [];
    for (const button of buttons.sort((a, b) => a.y - b.y || a.x - b.x)) {
      let row = rows.find(r => Math.abs(r.y - button.y) < 4);
      if (!row) { row = { y: button.y, buttons: [] }; rows.push(row); }
      row.buttons.push(button);
    }
    return rows.flatMap(row => row.buttons.sort((a, b) => a.x - b.x));
  } finally { image.close(); }
}

async function exerciseMission({ evaluate, readRuntime, input, delay, timeout }) {
  const command = async request => {
    const response = await evaluate(`(async()=>{const m=await import('/sim/wsorb_bevy.js');return JSON.parse(m.command(${JSON.stringify(JSON.stringify(request))}))})()`);
    if (response?.ok !== true) throw new Error(`Mission ${request.op} failed: ${response?.error || "no acknowledgement"}`);
  };
  await command({ op: "reset" });
  await input.waitFrames(3);
  const initial = await readRuntime();
  await command({ op: "play", rate: 10 });
  const start = Date.now(), deadline = start + timeout;
  let latest = initial, samples = 0, maximumDisplacementM = 0, pathLengthM = 0, previousPosition = initial.snapshot?.shuttle?.position;
  const phases = [{ phase: initial.snapshot?.mission?.phase, time_s: initial.snapshot?.time_s }];
  while (Date.now() < deadline) {
    latest = await readRuntime();
    samples++;
    const position = latest.snapshot?.shuttle?.position, phase = latest.snapshot?.mission?.phase;
    maximumDisplacementM = Math.max(maximumDisplacementM, vectorDistance(initial.snapshot?.shuttle?.position, position) || 0);
    pathLengthM += vectorDistance(previousPosition, position) || 0;
    previousPosition = position;
    if (phase && phases.at(-1)?.phase !== phase) phases.push({ phase, time_s: latest.snapshot.time_s });
    if (latest.error || (latest.snapshot?.mission?.status && latest.snapshot.mission.status !== "running")) break;
    await delay(250);
  }
  const terminal = latest.snapshot?.mission, status = terminal?.status || null;
  const timedOut = status === "running" && Date.now() >= deadline;
  await input.waitFrames(3, 300);
  const settled = await readRuntime();
  const simulatedElapsedSeconds = latest.snapshot?.time_s - initial.snapshot?.time_s;
  const pausedAfter = latest.runtime?.running === false && settled.runtime?.running === false && settled.snapshot?.time_s === latest.snapshot?.time_s && vectorDistance(latest.snapshot?.shuttle?.position, settled.snapshot?.shuttle?.position) === 0;
  const result = { requested: true, status, phase: terminal?.phase, score: terminal?.score, timedOut, timeoutSeconds: timeout / 1000, wallElapsedSeconds: (Date.now() - start) / 1000, simulatedElapsedSeconds, samples, phases, initial: { runtime: initial.runtime, time_s: initial.snapshot?.time_s, mission: initial.snapshot?.mission, shuttle: initial.snapshot?.shuttle }, terminal: { runtime: latest.runtime, time_s: latest.snapshot?.time_s, mission: terminal, shuttle: latest.snapshot?.shuttle, target: latest.snapshot?.target }, motion: { maximumDisplacementM, sampledPathLengthM: pathLengthM, shuttlePoseChanged: maximumDisplacementM > .01 }, pausedAfter, error: latest.error || null };
  result.passed = !timedOut && !latest.error && initial.snapshot?.mission?.phase === "on_pad" && initial.runtime?.autopilot === true && status === "success" && terminal?.phase === "arrived" && Number.isFinite(terminal?.score) && simulatedElapsedSeconds > 0 && maximumDisplacementM > .01 && pausedAfter;
  return result;
}

async function exerciseSensors({ evaluate, readRuntime, input, capture }) {
  const result = { views: {}, captures: {}, fixedPose: false, debugIsolation: false, lighting: null, result: "FAIL" };
  const pixels = async (id, name) => {
    const frame = await evaluate(`(${sensorPixels})(${JSON.stringify(id)})`, 30_000);
    if (frame.error) throw new Error(frame.error);
    await writeFile(join(output, `${name}.png`), Buffer.from(frame.png, "base64"));
    delete frame.png;
    result.captures[name] = frame;
    return frame;
  };
  const restore = async (kind, occluded) => {
    await evaluate(`(${sensorScene})(${JSON.stringify(kind)},${JSON.stringify(occluded)})`);
    await input.waitFrames(4, 350);
  };
  try {
    await input.focusScene({ preserveFocus: true });
    await evaluate(`(async()=>{const m=await import('/sim/wsorb_bevy.js');globalThis.__previewSensorOriginal=JSON.parse(m.command(JSON.stringify({op:'observe'}))).state;return true})()`);
    await input.waitFrames(4, 350);
    const before = await readRuntime({ physics: true });
    for (const [key, id, mode] of [["6", "top", "ship_top"], ["7", "bottom", "ship_bottom"]]) {
      await input.pressKey(key);
      const state = await readRuntime({ physics: true });
      result.views[id] = { mode: state.camera?.mode, position_m: state.camera?.position_m, passed: state.camera?.mode === mode && samePhysics(before, state) && state.renderer?.ready === true };
      await capture(`ship-${id}-view.png`);
      const frame = await pixels(id, `ship-${id}-raw`);
      if (frame.width !== 512 || frame.height !== 512 || frame.channels !== 3 || frame.bits_per_channel !== 8 || frame.byteLength !== 512 * 512 * 3 || frame.debug_overlays !== false) throw new Error(`Invalid ${id} sensor format: ${JSON.stringify(frame)}`);
    }
    await input.pressKey("6");
    const mounted = (await readRuntime({ statusOnly: true })).camera;
    await input.drag("left", 36, 8);
    const point = await input.moveToScene();
    await input.mouse("mouseWheel", point.x, point.y, { deltaY: -120 });
    const afterDrag = (await readRuntime({ statusOnly: true })).camera;
    result.fixedPose = sameCamera(mounted, afterDrag);
    const beforeDebug = await pixels("top", "ship-top-before-debug");
    await input.pressKey("4");
    const debugBefore = (await readRuntime({ statusOnly: true })).camera;
    await input.keyDown("KeyW");
    await delay(200);
    await input.keyUp("KeyW");
    const debugAfter = (await readRuntime({ statusOnly: true })).camera;
    const afterDebug = await pixels("top", "ship-top-after-debug");
    result.debugIsolation = beforeDebug.sha256 === afterDebug.sha256 && debugAfter?.mode === "free" && vectorDistance(debugBefore?.position_m, debugAfter?.position_m) > .01 && samePhysics(before, await readRuntime({ physics: true }));
    await restore("mars", false);
    const marsBackground = await pixels("top", "mars-background");
    await restore("mars", true);
    const marsDebris = await pixels("top", "mars-debris");
    await restore("sun", false);
    const sunBackground = await pixels("top", "sun-background");
    await restore("sun", true);
    const sunDebris = await pixels("top", "sun-silhouette");
    const contrast = Math.abs(marsBackground.centerMean - marsDebris.centerMean) / Math.max(1, marsBackground.centerMean + marsDebris.centerMean);
    result.mountFollowsShip = vectorDistance(marsBackground.position_m, [2, 0, 60000]) < .01 && vectorDistance(sunBackground.position_m, [-2, 0, 60000]) < .01;
    result.lighting = { marsBackground, marsDebris, sunBackground, sunDebris, marsContrast: contrast,
      passed: sunBackground.centerPeak > 200 && sunDebris.centerPeak < 10 && marsBackground.centerMean > 10 && marsDebris.centerMean > 10 && contrast < .45 };
    result.rgb8ColorDiversity = Math.max(...Object.values(result.captures).map(frame => frame.uniqueColors)) > 256;
    result.result = Object.values(result.views).every(view => view.passed) && result.fixedPose && result.mountFollowsShip && result.debugIsolation && result.rgb8ColorDiversity && result.lighting.passed ? "PASS" : "FAIL";
  } catch (error) { result.error = error.stack || String(error); }
  finally {
    await evaluate(`(async()=>{if(!globalThis.__previewSensorOriginal)return;const m=await import('/sim/wsorb_bevy.js');const r=JSON.parse(m.command(JSON.stringify({op:'restore',state:globalThis.__previewSensorOriginal})));delete globalThis.__previewSensorOriginal;if(!r.ok)throw new Error(r.error)})()`).catch(error => { result.restoreError = String(error); result.result = "FAIL"; });
    await input.focusScene({ preserveFocus: true }).catch(() => {});
    await input.pressKey("1").catch(() => {});
  }
  return result;
}

// A paused, browser-local optical fixture. It only moves existing dynamic
// bodies; scenario geometry, material, lighting and physical parameters stay fixed.
async function sensorScene(kind, occluded) {
  const m = await import("/sim/wsorb_bevy.js");
  const state = structuredClone(globalThis.__previewSensorOriginal);
  if (!state) throw new Error("Original optical fixture state is missing");
  const ship = state.assets.find(asset => asset.id === "Shuttle");
  const debris = state.assets.filter(asset => asset.kind === "asteroid");
  const largest = debris.reduce((a, b) => a.radius_m > b.radius_m ? a : b);
  for (let i = 0; i < debris.length; i++) {
    debris[i].position = [50000 + i % 25 * 2000, 200000 + Math.floor(i / 25) * 2000, 300000];
    debris[i].velocity = [0, 0, 0]; debris[i].angular_velocity_rad_s = [0, 0, 0];
  }
  const sign = kind === "mars" ? 1 : -1;
  ship.position = [0, 0, 60000]; ship.velocity = [0, 0, 0]; ship.angular_velocity_rad_s = [0, 0, 0];
  ship.rotation = [0, sign * Math.SQRT1_2, 0, Math.SQRT1_2];
  ship.commanded_accel_mps2 = [0, 0, 0]; ship.commanded_angular_accel_rad_s2 = [0, 0, 0];
  if (occluded) largest.position = [sign * 1000, 0, 60000];
  const response = JSON.parse(m.command(JSON.stringify({ op: "restore", state })));
  if (!response.ok) throw new Error(`Optical fixture restore failed: ${response.error}`);
}

async function sensorPixels(id) {
  try {
    const m = await import("/sim/wsorb_bevy.js");
    const { rgb, ...metadata } = await m.ship_camera_capture(id);
    if (!(rgb instanceof Uint8Array)) throw new Error("Ship capture did not return RGB bytes");
    const rgba = new Uint8ClampedArray(metadata.width * metadata.height * 4);
    const colors = new Set(); let black = 0, bright = 0, centerMean = 0, centerPeak = 0, count = 0;
    for (let i = 0; i < rgb.length / 3; i++) {
      const r = rgb[i * 3], g = rgb[i * 3 + 1], b = rgb[i * 3 + 2];
      rgba.set([r, g, b, 255], i * 4); colors.add(r << 16 | g << 8 | b);
      const luma = .2126 * r + .7152 * g + .0722 * b;
      if (Math.max(r, g, b) < 4) black++;
      if (Math.min(r, g, b) > 240) bright++;
      const x = i % metadata.width, y = Math.floor(i / metadata.width);
      if (Math.abs(x - metadata.width / 2) < 4 && Math.abs(y - metadata.height / 2) < 4) {
        centerMean += luma; centerPeak = Math.max(centerPeak, r, g, b); count++;
      }
    }
    const canvas = new OffscreenCanvas(metadata.width, metadata.height), context = canvas.getContext("2d", { colorSpace: "srgb" });
    context.putImageData(new ImageData(rgba, metadata.width, metadata.height), 0, 0);
    const png = new Uint8Array(await (await canvas.convertToBlob({ type: "image/png" })).arrayBuffer());
    let binary = ""; for (let i = 0; i < png.length; i += 32768) binary += String.fromCharCode(...png.subarray(i, i + 32768));
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", rgb));
    return { ...metadata, byteLength: rgb.length, uniqueColors: colors.size, blackPixels: black, brightPixels: bright,
      centerMean: centerMean / count, centerPeak, sha256: Array.from(hash, b => b.toString(16).padStart(2, "0")).join(""), png: btoa(binary) };
  } catch (error) { return { error: String(error) }; }
}

function vectorDistance(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length || !a.length || !a.every(Number.isFinite) || !b.every(Number.isFinite)) return null;
  return Math.hypot(...a.map((value, index) => value - b[index]));
}
function rotationDistance(a, b) {
  const direct = vectorDistance(a, b), negated = Array.isArray(b) ? vectorDistance(a, b.map(value => -value)) : null;
  return direct === null || negated === null ? null : Math.min(direct, negated);
}
function cameraDelta(before, after) { return { before, after, positionDeltaM: vectorDistance(before?.position_m, after?.position_m), rotationDelta: rotationDistance(before?.rotation, after?.rotation) }; }
function sameCamera(a, b) { return !!a && !!b && a.mode === b.mode && a.focus === b.focus && vectorDistance(a.position_m, b.position_m) !== null && vectorDistance(a.position_m, b.position_m) < .001 && rotationDistance(a.rotation, b.rotation) !== null && rotationDistance(a.rotation, b.rotation) < .000001 && Number.isFinite(a.distance_m) && Math.abs(a.distance_m - b.distance_m) < .001; }
function samePhysics(a, b) { return !!a?.physics?.sha256 && a.physics.sha256 === b?.physics?.sha256 && a.physics.time_s === b.physics.time_s && a.physics.asset_count === b.physics.asset_count; }
function forwardTravel(before, after) {
  const q = before?.rotation, a = before?.position_m, b = after?.position_m;
  if (!Array.isArray(q) || q.length !== 4 || vectorDistance(a, b) === null) return null;
  const [x, y, z, w] = q, forward = [-2 * (x * z + w * y), 2 * (w * x - y * z), -(1 - 2 * (x * x + y * y))];
  return forward.reduce((sum, value, index) => sum + value * (b[index] - a[index]), 0);
}
function fillsViewport(state) { return !!state?.canvas && Math.abs(state.canvas.width - state.width) <= 2 && Math.abs(state.canvas.height - state.height) <= 2 && Math.abs(state.canvas.x) <= 1 && Math.abs(state.canvas.y) <= 1; }
function bufferMatches(state) { return !!state?.canvas && Math.abs(state.canvas.bufferWidth - state.canvas.width * state.dpr) <= 2 && Math.abs(state.canvas.bufferHeight - state.canvas.height * state.dpr) <= 2; }
