// Review the real built comparison page. No flight, GPU or Python service.
// Uses the installed Chrome and Node's CDP WebSocket, not a substitute DOM.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { once } from "node:events";
import { pathToFileURL } from "node:url";

const option = process.argv.indexOf("--output");
if (option < 0 || !process.argv[option + 1]) throw new Error("Use just comparison-review OUTPUT_DIRECTORY");
const output = resolve(process.argv[option + 1]);
const reportOption = process.argv.indexOf("--report-file");
const reportFile = reportOption >= 0 ? resolve(process.argv[reportOption + 1]) : null;
await mkdir(output, { recursive: true });
const profile = await mkdtemp(resolve(output, "chrome-"));
const chromePath = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const server = spawn(process.execPath, ["worker/scripts/local.mjs"], {
  env: { ...process.env, PORT: "0", WSORB_LAB: "0", WSORB_HEADLESS: "0" }, stdio: ["ignore", "pipe", "pipe"],
});
let chrome, ws;
function matchOutput(process, pattern, streams) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Review process startup timed out")), 15000);
    process.once("error", reject); process.once("exit", code => reject(new Error(`Review process exited ${code}`)));
    for (const stream of streams) stream.on("data", data => { const match = String(data).match(pattern); if (match) { clearTimeout(timeout); resolve(match[0]); } });
  });
}
try {
  const origin = await matchOutput(server, /http:\/\/127\.0\.0\.1:\d+\//, [server.stdout]);
  chrome = spawn(chromePath, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
  const endpoint = await matchOutput(chrome, /ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[a-z0-9-]+/, [chrome.stderr]);
  ws = new WebSocket(endpoint); await once(ws, "open");
  const pending = new Map(); let serial = 0;
  ws.addEventListener("message", event => {
    const message = JSON.parse(String(event.data)); const task = pending.get(message.id);
    if (task) { pending.delete(message.id); clearTimeout(task.timeout); if (message.error) task.reject(new Error(message.error.message)); else task.resolve(message.result); }
  });
  function call(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++serial;
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
      pending.set(id, { resolve, reject, timeout }); ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
  const { targetId } = await call("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await call("Target.attachToTarget", { targetId, flatten: true });
  const cdp = (method, params = {}) => call(method, params, sessionId);
  await cdp("Page.enable"); await cdp("Runtime.enable");
  const errors = [];
  ws.addEventListener("message", event => { const message = JSON.parse(String(event.data));
    if (message.sessionId === sessionId && message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.text); });
  async function evaluate(expression) {
    const response = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description ?? response.exceptionDetails.text);
    return response.result.value;
  }
  const waitLoaded = () => evaluate(`new Promise((resolve,reject)=>{let n=0;const check=()=>{const el=document.querySelector('.path-body');if(el&&!el.hidden)resolve(true);else if(++n>200)reject(Error(document.querySelector('.path-status')?.textContent||'not loaded'));else setTimeout(check,50)};check()})`);
  const results = [];
  const contrast = (foreground, background) => {
    const luminance = color => {
      const rgb = color.match(/[\d.]+/g).slice(0, 3).map(value => Number(value) / 255);
      return rgb.map(n => n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4).reduce((sum, n, i) => sum + n * [.2126, .7152, .0722][i], 0);
    };
    const a = luminance(foreground), b = luminance(background); return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
  };
  for (const [width, height, name] of [[1440, 1100, "desktop"], [720, 1100, "half-width"], [320, 900, "narrow"]]) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
    await cdp("Page.navigate", { url: new URL("compare.html", origin).href }); await waitLoaded();
    const geometry = await evaluate(`(()=>({width:innerWidth,pageWidth:document.documentElement.scrollWidth,plots:document.querySelectorAll('.path-plot').length,cards:document.querySelectorAll('.path-card').length,duplicateIds:[...document.querySelectorAll('[id]')].map(n=>n.id).filter((v,i,a)=>a.indexOf(v)!==i),unlabelled:[...document.querySelectorAll('input,select,button')].filter(n=>!(n.labels?.length||n.textContent.trim()||n.getAttribute('aria-label'))).length}))()`);
    assert.equal(geometry.pageWidth, geometry.width, `Page overflows at ${width}px`);
    assert.equal(geometry.cards, 5); assert(geometry.plots >= 11); assert.deepEqual(geometry.duplicateIds, []); assert.equal(geometry.unlabelled, 0);
    const pairs = await evaluate(`[...document.querySelectorAll('.path-card h4')].map(n=>({foreground:getComputedStyle(n).color,background:getComputedStyle(n.closest('.path-card')).backgroundColor}))`);
    const contrasts = pairs.map(pair => ({ ...pair, ratio: contrast(pair.foreground, pair.background) }));
    assert(contrasts.every(pair => pair.ratio >= 4.5), "Controller label contrast must meet WCAG AA");
    const before = await evaluate(`document.querySelector('.path-cursor output').textContent`);
    await evaluate(`document.querySelector('.path-plot').focus()`);
    await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "ArrowRight", code: "ArrowRight" });
    await cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "ArrowRight", code: "ArrowRight" });
    const after = await evaluate(`document.querySelector('.path-cursor output').textContent`); assert.notEqual(after, before);
    assert.equal(await evaluate(`getComputedStyle(document.activeElement).outlineStyle`), "solid");
    await evaluate(`document.querySelector('.path-profiles input').click()`);
    assert.equal(await evaluate(`document.querySelectorAll('.path-card').length`), 4);
    await evaluate(`document.querySelector('.path-profiles input').click()`);
    await evaluate(`scrollTo(0,0)`);
    const screenshot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `${name}.png`), Buffer.from(screenshot.data, "base64"));
    await evaluate(`document.querySelector('.path-chart-grid').scrollIntoView({block:'start'})`);
    const maps = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `${name}-maps.png`), Buffer.from(maps.data, "base64"));
    results.push({ viewport: name, ...geometry, keyboardCursor: { before, after }, measuredLabelContrast: contrasts });
  }
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await evaluate(`(()=>{const select=document.querySelector('select[id$="-world"]');select.value='team';select.dispatchEvent(new Event('change',{bubbles:true}))})()`); await waitLoaded();
  assert.equal(await evaluate(`document.querySelectorAll('.path-card').length`), 3);
  assert.match(await evaluate(`document.querySelector('.path-scope').textContent`), /Historical|L2|directed/i);
  await evaluate(`(()=>{const select=document.querySelector('select[id$="-repetition"]');select.value='2';select.dispatchEvent(new Event('change',{bubbles:true}))})()`);
  assert.match(await evaluate(`document.querySelector('.path-scope').textContent`), /repetition 2/);
  // Native checkboxes retain an explicit way out of the empty state.
  await evaluate(`[...document.querySelectorAll('.path-profiles input')].forEach(n=>n.click())`);
  assert.equal(await evaluate(`document.querySelectorAll('.path-card').length`), 0);
  assert.equal(await evaluate(`document.querySelector('.path-cursor input').disabled`), true);
  await evaluate(`document.querySelector('.path-profiles input').click()`);
  assert.equal(await evaluate(`document.querySelectorAll('.path-card').length`), 1);
  // Also walk the existing dashboard's charts and empty connection views. Block
  // engine assets rather than supply a fake engine. The real page explicitly
  // supports recorded analytics before GPU readiness.
  await cdp("Network.enable"); await cdp("Network.setBlockedURLs", { urls: ["*/sim/*"] });
  for (const width of [1440, 320]) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height: 1000, deviceScaleFactor: 1, mobile: false });
    await cdp("Page.navigate", { url: origin });
    await evaluate(`new Promise((resolve,reject)=>{let n=0;const check=()=>{const b=document.getElementById('open-controllers');if(b&&!b.hidden){b.click();resolve(true)}else if(++n>200)reject(Error('dashboard unavailable'));else setTimeout(check,50)};check()})`);
    await waitLoaded();
    assert.equal(await evaluate(`document.getElementById('controller-panel').open`), true);
    assert.equal(await evaluate(`document.getElementById('run-controller').disabled`), true);
    const dashboard = await evaluate(`(()=>{const p=document.getElementById('controller-panel');return {width:innerWidth,panelWidth:p.getBoundingClientRect().width,outerOverflow:p.scrollWidth>p.clientWidth,paths:p.querySelectorAll('.path-card').length}})()`);
    assert.equal(dashboard.outerOverflow, false); assert.equal(dashboard.paths, 5);
    const dashboardShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `dashboard-${width}.png`), Buffer.from(dashboardShot.data, "base64"));
    await evaluate(`document.querySelector('#historical-overview > summary').click()`);
    await evaluate(`new Promise((resolve,reject)=>{let n=0;const check=()=>{if(document.querySelectorAll('.pilot-card').length>=3)resolve(true);else if(++n>200)reject(Error('historical charts unavailable'));else setTimeout(check,50)};check()})`);
    await evaluate(`document.getElementById('dashboard-overview').scrollIntoView({block:'start'})`);
    const aggregateShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `historical-aggregate-${width}.png`), Buffer.from(aggregateShot.data, "base64"));
    await evaluate(`document.querySelector('[data-measurement-view="run"]').click()`);
    assert.match(await evaluate(`document.getElementById('overview-selection-status').textContent`), /One recorded flight/);
    for (const view of ["run", "data"]) {
      await evaluate(`document.querySelector('.lab-navigation [data-lab-view="${view}"]').click()`);
      assert.equal(await evaluate(`document.getElementById('lab-view-${view}').hidden`), false);
    }
    results.push({ dashboardWidth: width, ...dashboard, legacyViews: "Aggregate, single run, offline run and recorded-data views opened with real source modules" });
  }
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1500, deviceScaleFactor: 1, mobile: false });
  for (const name of ["official-comparison", "efficiency", "fuel-burns", "turns", "score-components"]) {
    await cdp("Page.navigate", { url: new URL(`analytics/${name}.svg`, origin).href });
    const labels = await evaluate(`(()=>{const root=document.documentElement;if(root.localName!=='svg')throw Error('SVG unavailable');const box=root.viewBox.baseVal;return {title:root.querySelector('title')?.textContent,clipped:[...root.querySelectorAll('text')].filter(n=>{const b=n.getBBox();return b.x < box.x-1||b.y < box.y-1||b.x+b.width > box.x+box.width+1||b.y+b.height > box.y+box.height+1}).map(n=>n.textContent)}})()`);
    assert(labels.title, `${name} needs a title`);
    assert.deepEqual(labels.clipped, [], `${name} has text outside its SVG viewport`);
    const shot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `${name}.png`), Buffer.from(shot.data, "base64"));
    results.push({ svg: name, ...labels });
  }
  if (reportFile) {
    await cdp("Page.navigate", { url: pathToFileURL(reportFile).href });
    await evaluate(`new Promise((resolve,reject)=>{let n=0;const check=()=>{if(document.querySelector('#charts svg'))resolve(true);else if(++n>300)reject(Error(document.getElementById('cursor-status')?.textContent||'Python report unavailable'));else setTimeout(check,100)};check()})`);
    const report = await evaluate(`(()=>{const s=JSON.parse(document.getElementById('report-spec').textContent);return {trials:[...new Set(s.datasets.trace.map(r=>r.series_id))],repetitions:[...new Set(s.datasets.trace.map(r=>r.repetition))],svg:document.querySelectorAll('#charts svg').length}})()`);
    assert.equal(report.trials.length, 5); assert.deepEqual(report.repetitions, [1]); assert(report.svg > 0);
    await evaluate(`(()=>{const n=document.getElementById('cursor-number');n.value='33';n.dispatchEvent(new Event('input',{bubbles:true}))})()`);
    assert.match(await evaluate(`document.getElementById('cursor-status').textContent`), /Cursor 33 s/);
    const reportShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, "python-report.png"), Buffer.from(reportShot.data, "base64"));
    results.push({ pythonReport: reportFile, ...report, cursor: "Actual Vega rendered from saved trial data" });
  }
  assert.deepEqual(errors, []);
  const result = { result: "PASS", page: "Actual Chrome, read-only comparison, no engine or flight", viewports: results,
    checked: ["distinct scenarios", "repetition switch", "empty selection recovery", "keyboard cursor", "visible focus", "outer reflow", "native control labels"],
    not_verified: ["Screen-reader speech", "GPU rendering", "licensed STP execution"], screenshots: output };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n"); console.log(JSON.stringify(result, null, 2));
} finally {
  ws?.close(); chrome?.kill("SIGTERM"); server.kill("SIGTERM");
}
