// Review the real built pages. No owner flight, GPU or Python service.
// A separate source-module fixture checks runner consent and queue UI only.
// Uses the installed Chrome and Node's CDP WebSocket, not a substitute DOM.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { once } from "node:events";
import { pathToFileURL } from "node:url";
import { formatMetric, getMetric, NATIVE_COLUMNS, NATIVE_COUNTER_KEYS, NATIVE_NUMBER_KEYS, NATIVE_FLAG_KEYS } from "../web/comparison-model.ts";
import { build } from "esbuild";

const option = process.argv.indexOf("--output");
if (option < 0 || !process.argv[option + 1]) throw new Error("Use node worker/scripts/review-comparison.mjs --output OUTPUT_DIRECTORY");
const output = resolve(process.argv[option + 1]);
const reportOption = process.argv.indexOf("--report-file");
const reportFile = reportOption >= 0 ? resolve(process.argv[reportOption + 1]) : null;
const officialPaths = JSON.parse(await readFile("worker/public/analytics/paths-official.json", "utf8"));
const officialById = new Map(officialPaths.runs.map(run => [run.id, run]));
// Independent current-formula oracle. Do not call the browser score helper to
// establish the expected values it renders, and never read archived score fields.
function expectedScore(run) {
  const { time_s: time, damage_j: damage, fuel_kg: fuel, range_m: range } = run.final;
  const finiteNonnegative = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
  if (![time, damage, fuel, range].every(finiteNonnegative)) return null;
  let progress;
  if (run.outcome === "success" && damage <= 209200 && range <= 50) progress = time === 0 ? 0 : 12000 / time;
  else if (run.outcome === "failed_damage" && damage > 209200) {
    const initial = run.samples.find(row => row[0] === 0)?.[4];
    if (!finiteNonnegative(initial) || initial === 0) return null;
    progress = -100 * range / initial;
  } else return null;
  const components = { progress_points: progress, collision_penalty_points: 200 * damage / 209200,
    fuel_penalty_points: 100 * fuel / 75, offset_points: 50 };
  return { ...components, total: components.progress_points - components.collision_penalty_points - components.fuel_penalty_points + components.offset_points };
}
function meanScore(parts) {
  if (!parts.length) return null;
  const result = Object.fromEntries(["progress_points", "collision_penalty_points", "fuel_penalty_points", "offset_points"]
    .map(key => [key, parts.reduce((sum, part) => sum + part[key] / parts.length, 0)]));
  return { ...result, total: result.progress_points - result.collision_penalty_points - result.fuel_penalty_points + result.offset_points };
}
function expectedSummary(data, algorithm) {
  const seeds = data.comparison.seeds.flatMap(seed => {
    const runs = [1, 2].map(repetition => data.runs.find(run => run.algorithm === algorithm && run.seed === seed && run.repetition === repetition));
    const parts = runs.map(run => run && expectedScore(run));
    return parts.every(Boolean) ? [{ seed, runs, components: meanScore(parts) }] : [];
  });
  return { seeds, components: meanScore(seeds.map(seed => seed.components)) };
}
const supplementaryAssets = new Map();
let supplementaryManifest = null;
try {
  supplementaryManifest = await readFile("worker/public/analytics/metrics-manifest.json");
} catch (error) { if (error.code !== "ENOENT") throw error; }
if (supplementaryManifest !== null) {
  const manifest = JSON.parse(supplementaryManifest);
  assert.equal(manifest.schema, "wsorb.comparison_metrics.artifacts");
  assert.equal(manifest.schema_version, 1);
  const seen = new Set();
  for (const entry of manifest.artifacts) {
    assert(["official", "team"].includes(entry.id) && !seen.has(entry.id)); seen.add(entry.id);
    assert.equal(entry.path, `${entry.id}.json`);
    const bytes = await readFile(`worker/public/analytics/metrics-${entry.id}.json`);
    assert.equal(bytes.length, entry.bytes);
    assert.equal(createHash("sha256").update(bytes).digest("hex"), entry.sha256);
    supplementaryAssets.set(`/analytics/metrics-${entry.id}.json`, bytes);
  }
  supplementaryAssets.set("/analytics/metrics-manifest.json", supplementaryManifest);
}
const officialMetrics = supplementaryAssets.has("/analytics/metrics-official.json")
  ? JSON.parse(supplementaryAssets.get("/analytics/metrics-official.json")) : null;
await mkdir(output, { recursive: true });
const profile = await mkdtemp(resolve(output, "chrome-"));
const chromePath = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const server = spawn(process.execPath, ["worker/scripts/local.mjs"], {
  env: { ...process.env, PORT: "0", WSORB_LAB: "0", WSORB_HEADLESS: "0" }, stdio: ["ignore", "pipe", "pipe"],
});
let chrome, ws, reviewCDP;
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
  const cdpEndpoint = await matchOutput(chrome, /ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[a-z0-9-]+/, [chrome.stderr]);
  ws = new WebSocket(cdpEndpoint); await once(ws, "open");
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
  reviewCDP = cdp;
  await cdp("Page.enable"); await cdp("Runtime.enable"); await cdp("Network.enable");
  await cdp("Network.setBlockedURLs", { urls: ["*/sim/*"] });
  const errors = [];
  ws.addEventListener("message", event => { const message = JSON.parse(String(event.data));
    if (message.sessionId === sessionId && message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.text); });
  async function evaluate(expression) {
    const response = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description ?? response.exceptionDetails.text);
    return response.result.value;
  }
  const waitFor = (predicate, message) => evaluate(`new Promise((resolve,reject)=>{const end=Date.now()+15000;const check=()=>{if(${predicate})resolve(true);else if(Date.now()>end)reject(Error(${JSON.stringify(message)}));else setTimeout(check,50)};check()})`);
  const waitLoaded = () => waitFor(`(()=>{const el=document.querySelector('.comparison-workspace-body');return el&&!el.hidden&&el.querySelector('.comparison-row')})()`, "Saved comparison unavailable");
  async function choose(selector, value) {
    await evaluate(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n?.checkVisibility()||n.disabled||![...n.options].some(o=>o.value===${JSON.stringify(value)}))throw Error('Unavailable or undisclosed select: '+${JSON.stringify(selector)});n.value=${JSON.stringify(value)};n.dispatchEvent(new Event('change',{bubbles:true}))})()`);
  }
  async function enter(selector, value) {
    await evaluate(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n?.checkVisibility()||n.disabled)throw Error('Unavailable or undisclosed input: '+${JSON.stringify(selector)});n.value=${JSON.stringify(String(value))};n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}))})()`);
  }
  async function press(key, code = key) {
    const virtualKeys = { Enter: 13, Tab: 9, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Home: 36, End: 35, " ": 32 };
    const windowsVirtualKeyCode = virtualKeys[key];
    assert(windowsVirtualKeyCode, `No native key definition for ${key}`);
    const definition = { key, code: key === " " ? "Space" : code, windowsVirtualKeyCode };
    const text = key === "Enter" ? "\r" : key === " " ? " " : "";
    // key/code alone reach custom listeners but do not fully describe native
    // activation. Send the virtual key and character as a real keyboard does.
    await cdp("Input.dispatchKeyEvent", { type: text ? "keyDown" : "rawKeyDown", ...definition,
      ...(text ? { text, unmodifiedText: text } : {}) });
    await cdp("Input.dispatchKeyEvent", { type: "keyUp", ...definition });
  }
  async function pointer(selector, start, end = start) {
    const box = await evaluate(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n?.checkVisibility())throw Error('Missing or undisclosed pointer target');n.scrollIntoView({block:'center',inline:'center'});const b=n.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height}})()`);
    const point = position => ({ x: box.x + box.width * position[0], y: box.y + box.height * position[1] });
    const first = point(start), last = point(end);
    await cdp("Input.dispatchMouseEvent", { type: "mouseMoved", ...first });
    await cdp("Input.dispatchMouseEvent", { type: "mousePressed", ...first, button: "left", buttons: 1, clickCount: 1 });
    if (start !== end) {
      for (const fraction of [.25, .5, .75, 1]) await cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x: first.x + (last.x - first.x) * fraction, y: first.y + (last.y - first.y) * fraction, button: "left", buttons: 1 });
    }
    await cdp("Input.dispatchMouseEvent", { type: "mouseReleased", ...last, button: "left", buttons: 0, clickCount: 1 });
  }
  async function touch(selector, text = null) {
    const point = await evaluate(`(()=>{const matches=[...document.querySelectorAll(${JSON.stringify(selector)})];const n=${text === null ? "matches[0]" : `matches.find(n=>n.textContent.trim()===${JSON.stringify(text)})`};if(!n?.checkVisibility()||n.disabled)throw Error('Missing or undisclosed touch target');n.scrollIntoView({block:'center',inline:'center'});const b=n.getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2}})()`);
    await cdp("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ ...point, id: 1, radiusX: 1, radiusY: 1 }] });
    await cdp("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  }
  async function touchPan(selector) {
    const box = await evaluate(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});n.scrollIntoView({block:'center'});const b=n.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height,left:n.scrollLeft}})()`);
    const point = fraction => ({ x: box.x + box.width * fraction, y: box.y + Math.min(box.height / 2, 100), id: 1, radiusX: 1, radiusY: 1 });
    await cdp("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [point(.85)] });
    for (const fraction of [.7, .55, .4, .2]) await cdp("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [point(fraction)] });
    await cdp("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    return box.left;
  }
  async function labelled(host, pattern, controls = "input,select") {
    return evaluate(`(()=>{const root=document.querySelector(${JSON.stringify(host)});const n=[...root.querySelectorAll(${JSON.stringify(controls)})].find(n=>new RegExp(${JSON.stringify(pattern)},'i').test([...n.labels??[]].map(l=>l.textContent).join(' ')+' '+(n.getAttribute('aria-label')??'')));if(!n?.id)throw Error('Missing labelled control: '+${JSON.stringify(pattern)});return '#'+CSS.escape(n.id)})()`);
  }
  async function clickButton(host, text) {
    await evaluate(`(()=>{const n=[...document.querySelector(${JSON.stringify(host)}).querySelectorAll('button')].find(n=>n.textContent.trim()===${JSON.stringify(text)});if(!n?.checkVisibility()||n.disabled)throw Error('Unavailable or undisclosed button: '+${JSON.stringify(text)});n.scrollIntoView({block:'center',inline:'center'});n.focus()})()`);
    await press("Enter");
  }
  async function activate(selector) {
    const type = await evaluate(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n?.checkVisibility()||n.disabled)throw Error('Unavailable or undisclosed control: '+${JSON.stringify(selector)});n.scrollIntoView({block:'center',inline:'center'});n.focus();return n.type})()`);
    await press(type === "checkbox" ? " " : "Enter");
  }
  async function disclose(selector, open = true) {
    const current = await evaluate(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(n?.localName!=='details')throw Error('Missing disclosure: '+${JSON.stringify(selector)});return n.open})()`);
    if (current !== open) await activate(`${selector} > summary`);
    await waitFor(`document.querySelector(${JSON.stringify(selector)})?.open===${open}`, `Disclosure did not ${open ? "open" : "close"}: ${selector}`);
    if (open && selector === ".comparison-time-inspection") await waitFor(`document.querySelector('.comparison-time-plot')||!document.querySelector('.comparison-route-plot')`, "Time inspection did not attach its disclosed plots");
    if (open && selector === ".comparison-cursor-readout") await waitFor(`document.querySelector('.comparison-cursor-readout table')||!document.querySelector('.comparison-route-plot')`, "Exact cursor values did not populate");
  }
  async function retain(selector) {
    const result = await cdp("Runtime.evaluate", { expression: `document.querySelector(${JSON.stringify(selector)})` });
    assert(result.result.objectId, `Missing retained element: ${selector}`); return result.result.objectId;
  }
  async function connected(objectId) {
    const result = await cdp("Runtime.callFunctionOn", { objectId, functionDeclaration: "function(){return this.isConnected}", returnByValue: true });
    return result.result.value;
  }
  const selectedPoints = () => evaluate(`[...document.querySelectorAll('[data-flight-point][aria-pressed="true"]')].map(n=>n.dataset.runId)`);
  const inspectorValues = () => evaluate(`(()=>({from:document.querySelector('input[id$="-interval-from"]').value,to:document.querySelector('input[id$="-interval-to"]').value,cursor:document.querySelector('input[id$="-cursor-number"]').value,anchor:document.querySelector('.comparison-interval-controls > p').textContent}))()`);
  async function assertUsableFocus(message) {
    const focus = await evaluate(`(()=>{const n=document.activeElement,s=getComputedStyle(n),b=n.getBoundingClientRect();return {name:n.getAttribute('aria-label')||[...n.labels??[]].map(l=>l.textContent).join(' ')||n.textContent.trim(),connected:n.isConnected,visible:n.checkVisibility()&&b.width>0&&b.height>0&&b.bottom>0&&b.top<innerHeight,ring:s.outlineStyle!=='none'&&parseFloat(s.outlineWidth)>0||s.boxShadow!=='none',tag:n.localName}})()`);
    assert(focus.connected && focus.visible && focus.tag !== "body", `${message}: ${JSON.stringify(focus)}`);
    return focus;
  }
  const results = [];
  const matrixHost = ".comparison-matrix-host", tradeoffsHost = ".comparison-tradeoffs-host", inspectorHost = ".comparison-inspector-host";
  const filtersDisclosure = ".comparison-filters", timeDisclosure = ".comparison-time-inspection";
  const exploreDisclosure = ".comparison-tradeoffs-disclosure", columnsDisclosure = ".comparison-metric-picker";
  async function clearSelection() {
    const wasOpen = await evaluate(`document.querySelector('.comparison-metric-picker').open`);
    await disclose(columnsDisclosure);
    await clickButton(matrixHost, "Clear selection");
    if (!wasOpen) await disclose(columnsDisclosure, false);
  }
  const contrast = (foreground, background) => {
    const luminance = color => {
      const rgb = color.match(/[\d.]+/g).slice(0, 3).map(value => Number(value) / 255);
      return rgb.map(n => n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4).reduce((sum, n, i) => sum + n * [.2126, .7152, .0722][i], 0);
    };
    const a = luminance(foreground), b = luminance(background); return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
  };
  for (const [width, height, name] of [[1440, 1100, "desktop"], [720, 1100, "half-width"], [320, 900, "narrow"]]) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width === 320 });
    await cdp("Emulation.setTouchEmulationEnabled", { enabled: width === 320, maxTouchPoints: 1 });
    await cdp("Page.navigate", { url: new URL("compare.html", origin).href }); await waitLoaded();
    if (supplementaryAssets.has("/analytics/metrics-official.json")) await waitFor(`document.querySelector('a[href="/analytics/metrics-official.json"]')?.hidden===false`, "Included official supplement did not load");
    const geometry = await evaluate(`(()=>({width:innerWidth,pageWidth:document.documentElement.scrollWidth,rows:document.querySelectorAll('.comparison-row').length,widgets:document.querySelectorAll('.comparison-matrix-host,.comparison-tradeoffs-host,.comparison-inspector-host').length,duplicateIds:[...document.querySelectorAll('[id]')].map(n=>n.id).filter((v,i,a)=>a.indexOf(v)!==i),unlabelled:[...document.querySelectorAll('input,select,button')].filter(n=>!(n.labels?.length||n.textContent.trim()||n.getAttribute('aria-label'))).length}))()`);
    assert.equal(geometry.pageWidth, geometry.width, `Page overflows at ${width}px`);
    assert.equal(geometry.rows, 5); assert.equal(geometry.widgets, 3); assert.deepEqual(geometry.duplicateIds, []); assert.equal(geometry.unlabelled, 0);
    assert.match(await evaluate(`document.title`), /[Ff]light comparison/);
    assert.equal(await evaluate(`document.querySelector('h1').textContent`), "Compare flights");
    assert.equal(await evaluate(`[...document.querySelectorAll('h1,h2,h3')].filter(n=>n.textContent==='Compare flights').length`), 1, "The standalone shell must not duplicate its page heading");
    assert.equal(await evaluate(`document.querySelector('.comparison-evidence details').open`), false);
    assert.equal(await evaluate(`document.querySelector('.comparison-evidence details:last-child').open`), false);
    const compact = await evaluate(`(()=>{const root=document.querySelector('.comparison-workspace-body'),matrix=root.querySelector('.comparison-matrix-host'),inspector=root.querySelector('.comparison-inspector-host'),map=inspector.querySelector('.comparison-route-plot'),m=matrix.getBoundingClientRect(),i=inspector.getBoundingClientRect();return {order:[...root.children].map(n=>n.className),mode:matrix.querySelector('select[id$="-mode"]').value,closed:[...document.querySelectorAll('.comparison-filters,.comparison-metric-picker,.comparison-metric-coverage,.comparison-time-inspection,.comparison-tradeoffs-disclosure,.comparison-evidence details')].every(n=>!n.open),selected:[...matrix.querySelectorAll('[data-select-controller]:checked')].map(n=>n.dataset.selectController),maps:inspector.querySelectorAll('.comparison-route-plot').length,mapTop:map.getBoundingClientRect().top,inspectorWidth:i.width,matrixWidth:m.width,timelinesVisible:[...inspector.querySelectorAll('.comparison-time-plot')].some(n=>n.checkVisibility()),extraRowButtons:[...matrix.querySelectorAll('.comparison-row button')].filter(n=>!n.classList.contains('comparison-row-name')).length,visibleDeltas:[...matrix.querySelectorAll('.comparison-delta')].filter(n=>n.checkVisibility()).length,legendButtons:inspector.querySelectorAll('.comparison-path-legend button,input').length,endpointTables:inspector.querySelectorAll('.comparison-interval-stats table').length}})()`);
    assert.equal(compact.mode, "cohort"); assert(compact.closed, "Advanced comparison controls must start closed");
    assert.deepEqual(compact.selected, ["wso_official_profile"], "Default inspection must select only the baseline controller, not all policies");
    assert.equal(compact.maps, 2); assert.equal(compact.timelinesVisible, false); assert.equal(compact.endpointTables, 0);
    assert.equal(compact.extraRowButtons, 0); assert.equal(compact.visibleDeltas, 0); assert.equal(compact.legendButtons, 0);
    assert(compact.order[0].includes("comparison-matrix-host") && compact.order[1].includes("comparison-inspector-host") && compact.order[2].includes("comparison-tradeoffs-disclosure"));
    assert(Math.abs(compact.inspectorWidth - compact.matrixWidth) < 2, "Path inspection must use the full results width");
    assert(compact.mapTop < height, `Default maps must begin near the initial fold at ${width}px: ${compact.mapTop}`);
    const points = await evaluate(`(()=>{const matrix=document.querySelector('.comparison-matrix-host'),rows=[...matrix.querySelectorAll('.comparison-row')],first=rows[0],value=first.querySelector('[data-metric-id="score_points"] .comparison-metric-value'),coverage=first.querySelector('[data-metric-id="score_points"] .comparison-score-coverage'),b=value.getBoundingClientRect(),c=coverage.getBoundingClientRect(),scroller=value.closest('.comparison-table-scroll'),s=scroller.getBoundingClientRect(),note=matrix.querySelector('.comparison-score-note'),inside=box=>box.left>=Math.max(0,s.left)-1&&box.right<=Math.min(innerWidth,s.right)+1;return {columns:[...first.querySelectorAll('[data-metric-id]')].map(n=>n.dataset.metricId),sort:matrix.querySelector('th[aria-sort]')?.getAttribute('aria-sort'),sortLabel:matrix.querySelector('th[aria-sort] button')?.textContent,note:note?.textContent,noteVisible:note?.checkVisibility(),scrollLeft:scroller.scrollLeft,visible:value.checkVisibility()&&inside(b),coverageVisible:coverage.checkVisibility()&&inside(c),bounds:{value:{left:b.left,right:b.right},coverage:{left:c.left,right:c.right},scroller:{left:s.left,right:s.right}},rows:rows.map(row=>({id:row.dataset.rowId,value:row.querySelector('[data-metric-id="score_points"] .comparison-metric-value').textContent,raw:row.querySelector('[data-metric-id="score_points"] .comparison-metric-value').title,coverage:row.querySelector('[data-metric-id="score_points"] .comparison-score-coverage').textContent}))}})()`);
    assert.equal(points.columns[0], "score_points", "Points must precede time and fuel");
    assert.equal(points.sort, "descending"); assert.match(points.sortLabel, /Points/);
    assert(points.noteVisible && points.note.length < 180); assert.match(points.note, /Local estimate.*STP formula.*local measurements/i);
    assert.equal(points.scrollLeft, 0, "The default score check must not pre-scroll the table");
    assert(points.visible && points.coverageVisible, `Points and complete-pair coverage must be visible without horizontal scrolling at ${width}px: ${JSON.stringify(points.bounds)}`);
    const expectedPoints = points.rows.map(row => ({ id: row.id, ...expectedSummary(officialPaths, row.id) }));
    for (let index = 0; index < points.rows.length; index++) {
      const row = points.rows[index], expected = expectedPoints[index];
      assert.equal(row.value, formatMetric(expected.components?.total ?? null, "score_points"));
      assert(row.coverage.startsWith(`${expected.seeds.length}/3 seed pairs`), "Visible score coverage must disclose complete seed pairs");
      if (expected.components) assert(Math.abs(parseFloat(row.raw) - expected.components.total) < 1e-8, "Displayed points must use the current 75 kg formula on actual saved readings");
    }
    const actualScores = expectedPoints.map(row => row.components?.total ?? null), finiteScores = actualScores.filter(Number.isFinite);
    for (let index = 1; index < finiteScores.length; index++) assert(finiteScores[index - 1] + 1e-8 >= finiteScores[index], "Highest numeric points must lead the default matrix");
    assert(actualScores.slice(0, finiteScores.length).every(Number.isFinite), "An unavailable score must not precede numeric points");
    assert(actualScores.slice(finiteScores.length).every(value => value === null), "Unscored policies stay visible after numeric scores, not zero-filled");
    const pairs = await evaluate(`[...document.querySelectorAll('.comparison-row > th')].map(n=>{let p=n,b;while(p&&(!b||b==='rgba(0, 0, 0, 0)')){b=getComputedStyle(p).backgroundColor;p=p.parentElement}return {foreground:getComputedStyle(n).color,background:b}})`);
    const contrasts = pairs.map(pair => ({ ...pair, ratio: contrast(pair.foreground, pair.background) }));
    assert(pairs.length && contrasts.every(pair => pair.ratio >= 4.5), "Controller label contrast must meet WCAG AA");
    await evaluate(`document.querySelector('.comparison-workspace-toolbar select').focus()`); await press("Tab");
    const focus = await evaluate(`(()=>{const n=document.activeElement,s=getComputedStyle(n),b=n.getBoundingClientRect();return {label:[...n.labels??[]].map(l=>l.textContent).join(' '),visible:b.width>0&&b.height>0&&b.bottom>0&&b.top<innerHeight,ring:s.outlineStyle!=='none'&&parseFloat(s.outlineWidth)>0||s.boxShadow!=='none'}})()`);
    assert(focus.visible && focus.ring && await evaluate(`document.activeElement.matches('.comparison-filters > summary')`), "Keyboard focus must reach the closed Filters disclosure");
    const titleAndStatus = await evaluate(`({titles:[...document.querySelectorAll('.comparison-time-plot > title,.comparison-route-plot > title')].map(n=>n.textContent),statuses:[...document.querySelectorAll('.comparison-workspace-status [role="status"]')].map(n=>n.textContent)})`);
    assert(titleAndStatus.titles.every(title => title.length > 0 && title.length < 100));
    assert(titleAndStatus.statuses.every(status => status.length < 180));
    await evaluate(`scrollTo(0,0)`);
    const screenshot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `${name}.png`), Buffer.from(screenshot.data, "base64"));
    await evaluate(`document.querySelector('.comparison-inspector-host').scrollIntoView({block:'start'})`);
    const maps = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `${name}-maps.png`), Buffer.from(maps.data, "base64"));
    if (width === 320) {
      const row = ".comparison-row-name";
      await activate(row);
      assert.equal(await evaluate(`document.querySelectorAll('.comparison-row [data-select-controller]:checked').length`), 1);
      assert((await assertUsableFocus("Row inspection lost phone keyboard focus")).ring);
      const extraCheck = ".comparison-row:nth-child(2) [data-select-controller]";
      await touch(extraCheck);
      await waitFor(`document.querySelector(${JSON.stringify(extraCheck)}).checked`, "Phone checkbox touch did not add selection");
      assert.equal(await evaluate(`document.querySelectorAll('.comparison-row [data-select-controller]:checked').length`), 2);
      await evaluate(`const n=document.querySelector('.comparison-route-plot');n.scrollIntoView({block:'center'});n.focus()`); await press("ArrowRight");
      await waitFor(`document.querySelector('.comparison-time-inspection').open`, "Graph keyboard inspection must disclose its time controls");
      await waitFor(`document.querySelector('.comparison-time-plot')`, "Phone time plots did not attach after inspection opened");
      assert((await assertUsableFocus("Phone graph keyboard navigation lost focus")).ring);
      const cursorBeforeTouch = await evaluate(`document.querySelector('input[id$="-cursor-number"]').value`);
      await touch('.comparison-inspector button[aria-label="Next recorded sample"]');
      await waitFor(`document.querySelector('input[id$="-cursor-number"]').value!==${JSON.stringify(cursorBeforeTouch)}`, "Phone sample button did not move the cursor");
      const cursorAfterTouch = await evaluate(`document.querySelector('input[id$="-cursor-number"]').value`);
      assert.notEqual(cursorAfterTouch, cursorBeforeTouch);
      const phoneWindow = await inspectorValues();
      const timeScroll = ".comparison-inspector figure:has(.comparison-time-plot) .comparison-plot-scroll";
      await evaluate(`document.querySelector(${JSON.stringify(timeScroll)}).scrollLeft=0`);
      await touchPan(timeScroll);
      await waitFor(`document.querySelector(${JSON.stringify(timeScroll)}).scrollLeft>0`, "Phone timeline touch gesture did not pan its local scroller");
      assert.deepEqual(await inspectorValues(), phoneWindow, "Touch panning must not move the interval or cursor");
      await disclose(exploreDisclosure);
      await waitFor(`document.querySelector('[data-flight-point][tabindex="0"]')`, "Disclosed trade-offs did not load");
      await evaluate(`document.querySelector('.comparison-scatter [data-flight-point][tabindex="0"]').focus()`); await press("End");
      const revealed = await evaluate(`(()=>{const n=document.activeElement,b=n.getBoundingClientRect(),s=n.closest('svg').parentElement,r=s.getBoundingClientRect();return {point:n.hasAttribute('data-flight-point'),inside:b.left>=r.left&&b.right<=r.right}})()`);
      assert(revealed.point && revealed.inside, "Phone scatter keyboard navigation must reveal the focused point in its local scroller");
      assert((await assertUsableFocus("Phone scatter navigation lost focus")).ring);
      results.push({ phoneInput: "Exclusive row inspection, additive touch checkbox, disclosed graph controls and local timeline panning", cursorBeforeTouch, cursorAfterTouch });
    }
    results.push({ viewport: name, ...geometry, compactDefault: compact, pointsFirst: points, keyboardFocus: focus, measuredLabelContrast: contrasts });
  }
  await cdp("Emulation.setDeviceMetricsOverride", { width: 720, height: 1100, deviceScaleFactor: 1, mobile: false });
  await cdp("Emulation.setTouchEmulationEnabled", { enabled: false });
  await cdp("Page.navigate", { url: new URL("compare.html", origin).href }); await waitLoaded();
  await evaluate(`document.documentElement.style.fontSize='32px'`);
  const resized = await evaluate(`({width:innerWidth,pageWidth:document.documentElement.scrollWidth,fontSize:getComputedStyle(document.documentElement).fontSize,maps:document.querySelectorAll('.comparison-route-plot').length})`);
  assert.equal(resized.pageWidth, resized.width); assert.equal(resized.fontSize, "32px"); assert.equal(resized.maps, 2);
  await activate(".comparison-filters > summary");
  assert((await assertUsableFocus("200% text resize lost disclosure focus")).ring);
  const resizedShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  await writeFile(resolve(output, "text-200-percent.png"), Buffer.from(resizedShot.data, "base64"));
  results.push({ textResize: "200% root text at a 720 CSS-pixel viewport, not browser chrome zoom", ...resized });
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await cdp("Emulation.setTouchEmulationEnabled", { enabled: false });
  await cdp("Page.navigate", { url: new URL("compare.html", origin).href }); await waitLoaded();
  // Exercise the matrix's own controls, not an injected application state.
  const mode = await labelled(matrixHost, "^View", "select");
  await choose(mode, "trials");
  await disclose(filtersDisclosure);
  const difference = await labelled(filtersDisclosure, "Show differences from baseline", 'input[type="checkbox"]');
  await activate(difference);
  const baselineRow = await evaluate(`document.querySelector('.comparison-row.is-baseline').dataset.rowId`);
  const timeSort = `${matrixHost} thead button[aria-label^="Time"]`;
  await activate(timeSort);
  assert.equal(await evaluate(`document.querySelector('.comparison-matrix-host th[aria-sort]').getAttribute('aria-sort')`), "ascending");
  async function assertEfficiencySort(direction) {
    const rows = await evaluate(`[...document.querySelectorAll('.comparison-row')].map(n=>({id:n.dataset.rowId,value:n.querySelector('td[data-metric-id="time_s"] .comparison-metric-value').textContent,delta:n.querySelector('td[data-metric-id="time_s"] .comparison-delta').textContent}))`);
    const groups = rows.map(row => officialById.get(row.id).outcome === "success");
    const firstStopped = groups.indexOf(false);
    assert(firstStopped >= 0 && firstStopped > 0, "The actual pair must include arrivals and failures");
    assert(groups.slice(firstStopped).every(arrived => !arrived), "Efficiency sorting must keep failures after arrivals in either direction");
    for (const group of [rows.slice(0, firstStopped), rows.slice(firstStopped)]) {
      const values = group.map(row => officialById.get(row.id).final.time_s).filter(Number.isFinite);
      assert.deepEqual(values, values.toSorted((a, b) => direction * (a - b)));
    }
    assert(rows.slice(firstStopped).every(row => row.value !== "n/a" && row.delta === "Δ n/a · 0/0 paired"), "Failures keep readings but have no efficiency delta");
  }
  await assertEfficiencySort(1);
  await activate(timeSort);
  assert.equal(await evaluate(`document.querySelector('.comparison-matrix-host th[aria-sort]').getAttribute('aria-sort')`), "descending");
  await assertEfficiencySort(-1);
  await disclose(columnsDisclosure);
  await activate('.comparison-metric-picker input[value="burn_count"]');
  assert.match(await evaluate(`document.querySelector('.comparison-matrix-host thead').textContent`), /Burns/);
  assert.equal(await evaluate(`document.activeElement.value`), "burn_count");
  await activate('.comparison-metric-picker input[value="burn_count"]');
  await disclose(columnsDisclosure, false);
  await choose('select[id$="-baseline"]', "official_basic_beeline");
  assert.notEqual(await evaluate(`document.querySelector('.comparison-row.is-baseline').dataset.rowId`), baselineRow);
  await choose('select[id$="-baseline"]', "wso_official_profile");
  assert.equal(await evaluate(`document.querySelector('select[id$="-baseline"]').value`), "wso_official_profile");
  await choose(mode, "cohort");
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-row').length`), 5);
  assert.match(await evaluate(`document.querySelector('.comparison-matrix-host caption').textContent`), /Summary/);
  assert.match(await evaluate(`document.querySelector('.comparison-workspace-scope').textContent`), /All 3 seeds.*2 repetitions.*Path: seed/);
  assert.equal(await evaluate(`[...document.querySelectorAll('.comparison-row .comparison-arrival-count')].every(n=>n.textContent.includes('/6'))`), true);
  assert.equal(await evaluate(`document.querySelector('[data-select-all]').indeterminate`), true);
  await activate("[data-select-all]");
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-row [data-select-controller]:checked').length`), 5);
  await activate("[data-select-all]");
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-row [data-select-controller]:checked').length`), 0);
  await clearSelection();
  const summaryRows = await evaluate(`[...document.querySelectorAll('.comparison-row')].slice(0,2).map(n=>n.dataset.rowId)`);
  await activate(`.comparison-row[data-row-id="${summaryRows[0]}"] .comparison-row-name`);
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-row [data-select-controller]:checked').length`), 1);
  await activate(`.comparison-row[data-row-id="${summaryRows[1]}"] [data-select-controller]`);
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-row [data-select-controller]:checked').length`), 2, "Checkboxes add actual source IDs without replacing existing selections");
  await activate(`.comparison-row[data-row-id="${summaryRows[1]}"] .comparison-row-name`);
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-row [data-select-controller]:checked').length`), 1, "A controller name selects only its own actual runs");
  const cohortCSV = await evaluate(`(async()=>{const a=document.querySelector('a[download="selected-flight-samples.csv"]');const text=await(await fetch(a.href)).text();return [...new Set(text.trim().split(String.fromCharCode(10)).slice(1).map(line=>line.split(',')[0]))]})()`);
  assert.equal(cohortCSV.length, 6, "A summary selection must export its actual source runs");
  await disclose(columnsDisclosure);
  for (const id of ["min_radar_m", "policy_sample_count"]) await activate(`.comparison-metric-picker input[value="${id}"]`);
  await disclose(columnsDisclosure, false);
  await disclose(".comparison-metric-coverage");
  assert.match(await evaluate(`document.querySelector('.comparison-metric-coverage').textContent`), /efficiency uses arrivals only.*context uses attempts/i);
  const scoreBreakdown = await evaluate(`[...document.querySelectorAll('.comparison-score-breakdown tr[data-score-scope]')].map(row=>({id:row.dataset.rowId,run:row.dataset.runId,scope:row.dataset.scoreScope,components:Object.fromEntries([...row.querySelectorAll('[data-score-component]')].map(cell=>[cell.dataset.scoreComponent,cell.hasAttribute('data-score-value')?Number(cell.dataset.scoreValue):null])),text:row.textContent}))`);
  assert.equal(scoreBreakdown.filter(row => row.scope === "summary").length, 5);
  for (const row of scoreBreakdown.filter(row => row.scope === "summary" || row.scope === "trial")) {
    const expected = row.scope === "trial" ? expectedScore(officialById.get(row.run)) : expectedSummary(officialPaths, row.id).components;
    for (const [key, actual] of Object.entries(row.components)) {
      if (!expected) assert.equal(actual, null, "Unscored breakdown cells must stay unavailable");
      else assert(Math.abs(actual - expected[key]) < 1e-8, `Score component ${key} uses the wrong formula or population for ${row.run ?? row.id}`);
    }
    if (expected) assert(Math.abs(row.components.total - (row.components.progress_points - row.components.collision_penalty_points - row.components.fuel_penalty_points + row.components.offset_points)) < 1e-8);
  }
  assert(scoreBreakdown.some(row => row.scope === "trial" && row.components.total === null && /Not scored|No terminal formula|incomplete/i.test(row.text)), "Unavailable source outcomes need a visible explanation in the breakdown");
  assert.match(await evaluate(`document.querySelector('.comparison-score-method').textContent`), /75/);
  assert(await evaluate(`[...document.querySelectorAll('.comparison-score-method a')].some(link=>link.href.includes('c010564824c48488104e648781d5e987b402e47a'))`), "The breakdown must link the current pinned formula");
  const summaryCoverage = await evaluate(`[...document.querySelectorAll('.comparison-coverage-row')].filter(n=>['fuel_kg','min_radar_m','policy_sample_count'].includes(n.dataset.metricId)).map(n=>({algorithm:n.dataset.rowId,metric:n.dataset.metricId,runs:n.querySelector('.comparison-coverage-runs').textContent,pairs:n.querySelector('.comparison-coverage-pairs').textContent,timingCalls:n.querySelector('.comparison-coverage-timing-calls').textContent,readings:n.querySelector('.comparison-coverage-readings').textContent,differences:n.querySelector('.comparison-coverage-differences').textContent}))`);
  for (const coverage of summaryCoverage) {
    const runs = officialPaths.runs.filter(run => run.algorithm === coverage.algorithm);
    const eligible = coverage.metric === "fuel_kg" ? runs.filter(run => run.outcome === "success") : runs;
    const measured = run => coverage.metric === "fuel_kg" ? run.final.fuel_kg
      : coverage.metric === "policy_sample_count" ? officialMetrics?.runs.find(item => item.id === run.id)?.policy?.sample_count ?? null
        : run.samples.map(row => row[10]).filter(Number.isFinite).reduce((minimum, value) => minimum === null ? value : Math.min(minimum, value), null);
    const readings = eligible.filter(run => Number.isFinite(measured(run)));
    const pairs = eligible.flatMap(run => {
      const baseline = officialPaths.runs.find(item => item.algorithm === "wso_official_profile" && item.seed === run.seed && item.repetition === run.repetition);
      return baseline && (coverage.metric !== "fuel_kg" || baseline.outcome === "success") && Number.isFinite(measured(run)) && Number.isFinite(measured(baseline))
        ? [{ run, baseline, delta: measured(run) - measured(baseline) }] : [];
    });
    assert.equal(coverage.runs, `${readings.length}/${eligible.length} ${coverage.metric === "fuel_kg" ? "arrivals" : "attempts"}`);
    assert.equal(coverage.pairs, `${pairs.length}/${eligible.length} eligible runs paired`);
    for (const run of readings) assert(coverage.readings.includes(run.id) && coverage.readings.includes(`seed ${run.seed}`), "Coverage must identify its source runs");
    for (const pair of pairs) assert(coverage.differences.includes(pair.run.id) && coverage.differences.includes(`baseline run ${pair.baseline.id}`));
    const delta = await evaluate(`document.querySelector('.comparison-row[data-row-id="${coverage.algorithm}"] td[data-metric-id="${coverage.metric}"] .comparison-delta').textContent`);
    const values = pairs.map(pair => pair.delta).toSorted((a, b) => a - b), middle = Math.floor(values.length / 2);
    const median = values.length ? values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2 : null;
    const unit = getMetric(coverage.metric).unit;
    assert.equal(delta, `Δ ${formatMetric(median, coverage.metric, true)}${unit && Number.isFinite(median) ? ` ${unit}` : ""} · ${pairs.length}/${eligible.length} paired`, "Visible summary delta must use matched source differences and disclose its paired denominator");
    if (coverage.metric === "policy_sample_count") {
      const policies = eligible.flatMap(run => officialMetrics?.runs.find(item => item.id === run.id)?.policy ?? []);
      assert.equal(coverage.timingCalls, policies.length ? new Intl.NumberFormat().format(policies.reduce((sum, policy) => sum + policy.sample_count, 0)) : "n/a");
    }
  }
  await disclose(".comparison-metric-coverage", false);
  await choose(mode, "trials");
  const filteredPair = await evaluate(`({seed:document.querySelector('.comparison-workspace-toolbar select[id$="-seed"]').value,repetition:document.querySelector('.comparison-workspace-toolbar select[id$="-repetition"]').value})`);
  const fixedPairRuns = officialPaths.runs.filter(run => run.seed === Number(filteredPair.seed) && run.repetition === Number(filteredPair.repetition));
  async function restoreFilteredPair() {
    // Changing the outcome can follow a selected run to another eligible pair.
    // Explicitly inspect one pair before comparing its two outcome partitions.
    await choose('.comparison-workspace-toolbar select[id$="-seed"]', filteredPair.seed);
    await choose('.comparison-workspace-toolbar select[id$="-repetition"]', filteredPair.repetition);
  }
  const arrivedRow = await evaluate(`[...document.querySelectorAll('.comparison-row')].find(n=>n.querySelector('.comparison-arrival-value').textContent==='1/1')?.dataset.rowId`);
  assert(arrivedRow);
  await evaluate(`document.querySelector('.comparison-row[data-row-id="${arrivedRow}"] .comparison-row-name').focus()`);
  await choose('select[id$="-outcome"]', "failed");
  assert((await assertUsableFocus("Outcome filtering must recover focus when a row disappears")).ring);
  await restoreFilteredPair();
  const failedIds = await evaluate(`[...document.querySelectorAll('.comparison-row')].map(n=>n.dataset.rowId).toSorted()`);
  assert.deepEqual(failedIds, fixedPairRuns.filter(run => run.outcome !== "success").map(run => run.id).toSorted());
  assert(failedIds.length > 0 && failedIds.length < fixedPairRuns.length);
  await choose('select[id$="-outcome"]', "arrived");
  await restoreFilteredPair();
  const arrivedIds = await evaluate(`[...document.querySelectorAll('.comparison-row')].map(n=>n.dataset.rowId).toSorted()`);
  assert.deepEqual(arrivedIds, fixedPairRuns.filter(run => run.outcome === "success").map(run => run.id).toSorted());
  assert(arrivedIds.length > 0);
  assert.deepEqual([...arrivedIds, ...failedIds].toSorted(), fixedPairRuns.map(run => run.id).toSorted());
  await choose('select[id$="-outcome"]', "all");

  await disclose(exploreDisclosure);
  await waitFor(`document.querySelector('.comparison-scatter [data-flight-point]')`, "Trade-off points must load when disclosed");
  const xAxis = await labelled(tradeoffsHost, "^X axis", "select"), yAxis = await labelled(tradeoffsHost, "^Y axis", "select");
  await choose(xAxis, "lateral_m"); await choose(yAxis, "relative_speed_mps");
  assert.match(await evaluate(`document.querySelector('.comparison-scatter').textContent`), /Lateral offset/);
  assert.equal(await evaluate(`document.querySelector('input[id$="-frontier"]').disabled`), true);
  await choose(xAxis, "fuel_kg"); await choose(yAxis, "time_s");
  assert.equal(await evaluate(`document.querySelector('input[id$="-frontier"]').disabled`), false);
  await choose(mode, "cohort");
  const otherPoint = await evaluate(`(()=>{const n=[...document.querySelectorAll('[data-flight-point]')].find(n=>n.getAttribute('aria-label').includes('seed 101, repetition 2'));if(!n)throw Error('No real cross-seed point');return n.dataset.runId})()`);
  await pointer(`[data-flight-point][data-run-id="${otherPoint}"] > circle:first-of-type`, [.5, .5]);
  assert.equal(await evaluate(`document.querySelector('select[id$="-seed"]').value`), "101");
  assert.equal(await evaluate(`document.querySelector('select[id$="-repetition"]').value`), "2");
  assert.equal(await evaluate(`document.querySelectorAll('[data-flight-point][aria-pressed="true"]').length`), 1);
  assert.equal(await evaluate(`document.querySelector('[data-flight-point][aria-pressed="true"]').dataset.runId`), otherPoint);
  await choose('.comparison-workspace-toolbar select[id$="-seed"]', "100");
  await choose('.comparison-workspace-toolbar select[id$="-repetition"]', "1");
  assert.deepEqual(await selectedPoints(), [otherPoint], "Summary pair controls must not replace the cohort selection");
  assert.equal(await evaluate(`document.querySelector('input[id$="-cursor-number"]').disabled`), true);
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-route-plot').length`), 0, "An unselected pair must not show baseline-only inspection");
  await choose('.comparison-workspace-toolbar select[id$="-seed"]', "101");
  await choose('.comparison-workspace-toolbar select[id$="-repetition"]', "2");
  await evaluate(`document.querySelector('[data-flight-point][aria-pressed="true"]').focus()`);
  await press("ArrowRight"); await press("Enter");
  const keyboardPoint = await evaluate(`(()=>{const n=document.activeElement;return {id:n.dataset.runId,selected:n.getAttribute('aria-pressed'),ring:getComputedStyle(n).outlineStyle}})()`);
  assert.notEqual(keyboardPoint.id, otherPoint); assert.equal(keyboardPoint.selected, "true"); assert.notEqual(keyboardPoint.ring, "none");
  await activate('summary[id$="-range-summary"]');
  await clickButton(tradeoffsHost, "Use plotted bounds"); await clickButton(tradeoffsHost, "Select range");
  const plotted = await evaluate(`document.querySelectorAll('[data-flight-point]').length`);
  assert(plotted > 5);
  assert.equal(await evaluate(`document.querySelectorAll('[data-flight-point][aria-pressed="true"]').length`), plotted);
  await clearSelection();
  await pointer(".comparison-scatter", [.15, .125], [.955, .795]);
  assert.equal(await evaluate(`document.querySelectorAll('[data-flight-point][aria-pressed="true"]').length`), plotted);
  const matchedInspector = await evaluate(`({seed:Number(document.querySelector('select[id$="-seed"]').value),rep:Number(document.querySelector('select[id$="-repetition"]').value),ids:[...document.querySelectorAll('.comparison-path-legend [data-run-id]')].map(n=>n.dataset.runId)})`);
  assert(matchedInspector.ids.length && matchedInspector.ids.every(id => officialById.get(id).seed === matchedInspector.seed && officialById.get(id).repetition === matchedInspector.rep), "The inspector must not overlay other seed/repetition pairs");
  assert((await selectedPoints()).length > await evaluate(`document.querySelectorAll('.comparison-path-legend [data-run-id]').length`), "A multi-pair selection must not become one merged path");
  const retainedSelection = (await selectedPoints()).toSorted();
  await choose('.comparison-workspace-toolbar select[id$="-seed"]', "102");
  await choose('.comparison-workspace-toolbar select[id$="-repetition"]', "2");
  assert.deepEqual((await selectedPoints()).toSorted(), retainedSelection);
  const multiPair = await evaluate(`[...document.querySelectorAll('.comparison-path-legend [data-run-id]')].map(n=>n.dataset.runId)`);
  assert(multiPair.length > 0 && multiPair.every(id => officialById.get(id).seed === 102 && officialById.get(id).repetition === 2));
  await choose(mode, "trials");

  await disclose(timeDisclosure);
  await disclose(".comparison-interval-disclosure");
  await disclose(".comparison-cursor-readout");
  await disclose(".comparison-flight-picker");
  const cursor = await labelled(inspectorHost, "^Cursor,", 'input[type="number"]');
  const from = await labelled(inspectorHost, "^From,", 'input[type="number"]'), to = await labelled(inspectorHost, "^To,", 'input[type="number"]');
  const alignment = await labelled(inspectorHost, "^Align by", "select");
  const equal = await labelled(inspectorHost, "^Equal metres", 'input[type="checkbox"]');
  await choose('.comparison-workspace-toolbar select[id$="-seed"]', "100");
  await choose('.comparison-workspace-toolbar select[id$="-repetition"]', "1");
  await clearSelection();
  const selectedRun = officialPaths.runs.find(run => run.algorithm === "official_basic_beeline" && run.seed === 100 && run.repetition === 1);
  const legendSelector = `.comparison-run-toggle[data-run-id="${selectedRun.id}"]`;
  await evaluate(`document.querySelector(${JSON.stringify(legendSelector)}).focus()`); await press("Enter");
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(legendSelector)}).getAttribute('aria-pressed')`), "true");
  assert.equal(await evaluate(`document.querySelector('.comparison-row[data-row-id="${selectedRun.id}"] [data-select-controller]').checked`), true);
  assert((await assertUsableFocus("Inspector legend toggling lost keyboard focus")).ring);
  await enter(cursor, selectedRun.samples[1][0]);
  const coordinates = await evaluate(`(()=>{const t=document.querySelector('.comparison-cursor-readout table'),h=[...t.tHead.rows[0].cells].map(n=>n.textContent),row=[...t.tBodies[0].rows].find(n=>n.cells[0].textContent.includes(${JSON.stringify(selectedRun.label)}));return ['Along route, m','Lateral offset, m','Normal offset, m'].map(label=>row.cells[h.indexOf(label)].textContent)})()`);
  assert.deepEqual(coordinates, selectedRun.samples[1].slice(1, 4).map(value => value === null ? "n/a" : String(value)), "Exact cursor values must expose full-precision recorded route coordinates without enabling tracks");
  const retainedMatrix = await retain(".comparison-row"), retainedScatter = await retain("[data-flight-point]");
  const wholeFlightViews = () => evaluate(`({matrix:[...document.querySelectorAll('.comparison-row td[data-metric-id]')].map(n=>n.textContent),scatter:[...document.querySelectorAll('[data-flight-point]')].map(n=>[n.dataset.runId,n.getAttribute('transform')])})`);
  const wholeFlightBefore = await wholeFlightViews();
  await evaluate(`document.querySelector(${JSON.stringify(from)}).focus()`);
  await enter(from, 10); await enter(to, 20); await clickButton(inspectorHost, "Apply interval");
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(from)}).value`), "10");
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(to)}).value`), "20");
  assert.equal(await evaluate(`document.activeElement.textContent`), "Apply interval");
  assert.match(await evaluate(`document.querySelector('.comparison-interval-stats caption').textContent`), /Interval endpoint/);
  assert(await evaluate(`document.querySelectorAll('.comparison-time-plot .comparison-interval').length>0`));
  const retainedTrace = await retain(".comparison-time-plot path[data-run]");
  await enter(cursor, 12);
  assert.equal(await connected(retainedTrace), true, "Cursor movement must retain inspector traces");
  const beforeCursor = await evaluate(`document.querySelector(${JSON.stringify(cursor)}).value`);
  await evaluate(`document.querySelector('.comparison-time-plot').focus()`); await press("ArrowRight");
  const afterCursor = await evaluate(`document.querySelector(${JSON.stringify(cursor)}).value`);
  assert.notEqual(afterCursor, beforeCursor);
  assert.notEqual(await evaluate(`getComputedStyle(document.activeElement).outlineStyle`), "none");
  await pointer(".comparison-time-plot", [.25, .45], [.75, .45]);
  const brushedInterval = await evaluate(`({from:Number(document.querySelector(${JSON.stringify(from)}).value),to:Number(document.querySelector(${JSON.stringify(to)}).value)})`);
  assert(brushedInterval.from > 10 && brushedInterval.to < 20 && brushedInterval.to > brushedInterval.from);
  assert.deepEqual(await wholeFlightViews(), wholeFlightBefore, "Inspector intervals must not change whole-flight matrix values or scatter coordinates");
  // The global difference control already enabled the matrix. Turn it off and
  // back on to prove the timelines consume the same state, not a second switch.
  await activate(difference); assert.equal(await evaluate(`document.querySelector('.comparison-time-plot').dataset.difference`), "false");
  assert.equal(await evaluate(`[...document.querySelectorAll('.comparison-delta')].some(n=>n.checkVisibility())`), false);
  await activate(difference);
  assert.match(await evaluate(`document.querySelector('.comparison-time-plot title').textContent`), /^Δ /);
  await activate(equal);
  assert.equal(await evaluate(`document.querySelector('[data-map-scale]').dataset.mapScale`), "equal");
  await choose(alignment, "arrival");
  const anchoredBefore = await inspectorValues();
  await choose('.comparison-workspace-toolbar select[id$="-baseline"]', "official_basic_beeline");
  const anchoredAfter = await inspectorValues();
  assert.deepEqual(anchoredAfter, anchoredBefore, "A baseline change must not shift the selected run's event window or cursor");
  await choose('.comparison-workspace-toolbar select[id$="-baseline"]', "wso_official_profile");
  await choose(alignment, "damage"); await choose(alignment, "time");
  assert.equal(await connected(retainedMatrix), true, "Inspector changes must retain matrix rows");
  assert.equal(await connected(retainedScatter), true, "Inspector changes must not redraw the scatter plot");
  await clickButton(inspectorHost, "Clear interval");
  const sampleEndpointTime = selectedRun.samples[1][0];
  await enter(from, sampleEndpointTime); await enter(to, sampleEndpointTime); await clickButton(inspectorHost, "Apply interval");
  const sparse = await evaluate(`[...document.querySelectorAll('.comparison-interval-stats tbody tr')].map(n=>({name:n.cells[0].textContent,values:[...n.cells].slice(4,7).map(n=>n.textContent),coverage:n.cells[7].textContent}))`);
  assert(sparse.length > 0 && sparse.every(row => row.values.every(value => value === "n/a")), "A single endpoint must not claim zero cumulative deltas");
  assert(sparse.some(row => row.name.includes(selectedRun.label) && /0%.*one endpoint/.test(row.coverage)));
  await clickButton(inspectorHost, "Clear interval");
  // The two actual recordings end at different times. A missing paired delta
  // still gets an independent end-time annotation, never a made-up metric point.
  assert.notEqual(selectedRun.final.time_s, officialPaths.runs.find(run => run.algorithm === "wso_official_profile" && run.seed === 100 && run.repetition === 1).final.time_s);
  const marker = await evaluate(`(()=>{const plot=document.querySelector('.comparison-time-plot[data-track="range_m"][data-difference="true"]');const run=${JSON.stringify(selectedRun.id)};return {end:plot.querySelector('.comparison-end-time[data-run="'+run+'"][data-event-kind="terminal"]')?.dataset.timeS,points:[...plot.querySelectorAll('circle[data-run="'+run+'"] > title,rect[data-run="'+run+'"] > title,path[data-run="'+run+'"] > title')].map(n=>n.textContent)}})()`);
  assert.equal(Number(marker.end), selectedRun.final.time_s);
  assert.equal(marker.points.some(title => /at .* s/.test(title)), false, "Missing endpoint differences must not get metric-valued end markers");
  await evaluate(`document.querySelector('.comparison-time-plot').focus()`); await press("Home");
  await clearSelection();
  assert((await assertUsableFocus("Clearing selection must recover focus after removing inspector graphs")).ring);
  assert.equal(await evaluate(`document.querySelectorAll('[data-flight-point][aria-pressed="true"]').length`), 0);
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(cursor)}).disabled`), true);
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-route-plot').length`), 0);
  await activate(".comparison-row-name");
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-route-plot').length`), 2);
  await clearSelection();
  await evaluate(`document.querySelector('[data-flight-point][tabindex="0"]').focus()`); await press("Enter");
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(cursor)}).disabled`), false);
  results.push({ matrix: "Sort, columns, baseline and matched summary", scatter: "Axes, point, keyboard and numeric/pointer ranges", inspector: "Absolute interval, pointer brush, alignment, difference, equal scale and empty recovery", keyboardCursor: { before: beforeCursor, after: afterCursor } });

  await choose('select[id$="-world"]', "team"); await waitLoaded();
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-row').length`), 3);
  assert.match(await evaluate(`document.querySelector('.comparison-workspace-scope').textContent`), /Historical L2/);
  await choose('select[id$="-repetition"]', "2");
  assert.match(await evaluate(`document.querySelector('.comparison-workspace-scope').textContent`), /repetition 2/);
  assert.equal(await evaluate(`document.querySelector('select[id$="-baseline"]').value`), "basic_pd_radar");
  if (supplementaryAssets.has("/analytics/metrics-team.json")) await waitFor(`document.querySelector('a[href="/analytics/metrics-team.json"]')?.hidden===false`, "Included team supplement did not load");
  const selectedBefore = await evaluate(`[...document.querySelectorAll('.comparison-row [data-select-controller]:checked')].map(n=>n.dataset.selectController).toSorted()`);
  await choose('select[id$="-seed"]', "101");
  assert.deepEqual(await evaluate(`[...document.querySelectorAll('.comparison-row [data-select-controller]:checked')].map(n=>n.dataset.selectController).toSorted()`), selectedBefore);
  // Fail the supplementary route, then serve the exact built bytes on retry.
  // No substitute measurements are created and cache/network timing is irrelevant.
  let failSupplement = true;
  const intercepted = { failed: [], loaded: [] };
  const interceptSupplement = event => {
    const message = JSON.parse(String(event.data));
    if (message.sessionId !== sessionId || message.method !== "Fetch.requestPaused") return;
    const { requestId, request } = message.params, path = new URL(request.url).pathname;
    const raw = supplementaryAssets.get(path);
    const failed = failSupplement || !raw;
    void cdp("Fetch.fulfillRequest", { requestId, responseCode: failed ? 503 : 200,
      responseHeaders: [{ name: "Content-Type", value: "application/json" }, { name: "Cache-Control", value: "no-store" }],
      body: (failed ? Buffer.from('{"error":"review supplementary route unavailable"}') : raw).toString("base64"),
    }).then(() => { intercepted[failed ? "failed" : "loaded"].push(path); }).catch(error => errors.push(error.message));
  };
  ws.addEventListener("message", interceptSupplement);
  await cdp("Fetch.enable", { patterns: [{ urlPattern: "*/analytics/metrics-*.json", requestStage: "Request" }] });
  await cdp("Page.navigate", { url: new URL("compare.html", origin).href }); await waitLoaded();
  const retryVisible = `[...document.querySelectorAll('.comparison-workspace-status button')].some(n=>n.textContent==='Retry extra metrics'&&!n.hidden)`;
  await waitFor(retryVisible, "Optional metrics recovery unavailable");
  assert.equal(await evaluate(`document.querySelectorAll('.comparison-row').length`), 5);
  failSupplement = false;
  await evaluate(`[...document.querySelectorAll('.comparison-workspace-status button')].find(n=>n.textContent==='Retry extra metrics').focus()`);
  await press("Enter");
  if (supplementaryAssets.has("/analytics/metrics-official.json")) {
    await waitFor(`document.querySelector('a[href="/analytics/metrics-official.json"]')?.hidden===false&&!(${retryVisible})`, "Included supplement must recover successfully after retry");
    assert(intercepted.failed.includes("/analytics/metrics-official.json") && intercepted.loaded.includes("/analytics/metrics-official.json"));
    const recoveredFocus = await assertUsableFocus("Successful supplementary retry must recover focus to an enabled control");
    assert.equal(recoveredFocus.tag, "select"); assert(recoveredFocus.ring);
    results.push({ supplementaryMetrics: "Included supplements loaded; exact official bytes recovered after deterministic route failure",
      included: [...supplementaryAssets.keys()].filter(path => path !== "/analytics/metrics-manifest.json"), intercepted, recoveredFocus });
  } else {
    await waitFor(retryVisible, "Intentionally omitted supplement should remain unavailable");
    assert.equal(await evaluate(`document.querySelector('a[href="/analytics/metrics-official.json"]').hidden`), true);
    await assertUsableFocus("Failed optional retry lost its retry focus");
    results.push({ supplementaryMetrics: "Official supplement intentionally omitted by the built manifest; saved paths remain usable", intercepted });
  }
  await cdp("Fetch.disable"); ws.removeEventListener("message", interceptSupplement);
  // Also walk the existing dashboard's charts and empty connection views. Block
  // engine assets rather than supply a fake engine. The real page explicitly
  // supports recorded analytics before GPU readiness.
  await cdp("Network.enable"); await cdp("Network.setBlockedURLs", { urls: ["*/sim/*"] });
  for (const width of [1440, 720, 320]) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height: 1000, deviceScaleFactor: 1, mobile: false });
    await cdp("Page.navigate", { url: origin });
    await waitFor(`document.getElementById('open-controllers')?.checkVisibility()`, "Dashboard unavailable");
    await activate("#open-controllers");
    assert.equal(await evaluate(`document.getElementById('controller-panel').open`), true);
    assert.equal(await evaluate(`document.getElementById('lab-view-run').hidden`), false, "Test profiles must open the runner directly");
    assert.equal(await evaluate(`document.getElementById('lab-view-compare').hidden`), true);
    assert.equal(await evaluate(`document.getElementById('run-controller').disabled`), true);
    assert.equal(await evaluate(`document.getElementById('controller-owner').value`), "Bui");
    const owners = await evaluate(`[...document.getElementById('controller-owner').options].map(n=>n.value).toSorted()`);
    assert.deepEqual(owners, ["Bui", "Harry", "Tom", "WSO"]);
    const catalog = JSON.parse(await readFile("src/wsorb/controllers/catalog.json", "utf8"));
    for (const owner of owners) {
      await choose("#controller-owner", owner);
      const choices = await evaluate(`[...document.getElementById('controller-profile').options].map(n=>n.value).toSorted()`);
      assert.deepEqual(choices, catalog.filter(entry => entry.owner === owner).map(entry => entry.id).toSorted());
      for (const id of choices) {
        await choose("#controller-profile", id);
        assert.equal(await evaluate(`document.getElementById('controller-profile-source').textContent`), catalog.find(entry => entry.id === id).source);
        assert.equal(await evaluate(`document.getElementById('run-controller').disabled`), true, "Offline profile selection must never start a flight");
      }
    }
    await choose("#controller-owner", "Bui");
    await activate("#setup-controller");
    assert.equal(await evaluate(`document.getElementById('controller-setup').open`), true);
    assert.match(await evaluate(`document.getElementById('controller-setup').textContent`), /controllers serve.*--connection-file.*--room-file/s);
    await activate("#reconnect-controller");
    await waitFor(`document.getElementById('controller-results-status').dataset.dataState==='unavailable'`, "Offline connection recovery must explain unavailable data");
    assert.match(await evaluate(`document.getElementById('controller-service-status').textContent`), /[Cc]onnection|[Uu]navailable/);
    const dashboard = await evaluate(`(()=>{const p=document.getElementById('controller-panel');return {width:innerWidth,panelWidth:p.getBoundingClientRect().width,outerOverflow:p.scrollWidth>p.clientWidth,owners:[...document.getElementById('controller-owner').options].map(n=>n.value),runDisabled:document.getElementById('run-controller').disabled}})()`);
    assert.equal(dashboard.outerOverflow, false);
    await evaluate(`document.querySelector('.lab-scroll').scrollTop=0`);
    const dashboardShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `dashboard-${width}.png`), Buffer.from(dashboardShot.data, "base64"));
    await activate('.lab-navigation [data-lab-view="compare"]');
    await waitLoaded();
    await disclose("#historical-overview");
    await evaluate(`new Promise((resolve,reject)=>{let n=0;const check=()=>{if(document.querySelectorAll('.pilot-card').length>=3)resolve(true);else if(++n>200)reject(Error('historical charts unavailable'));else setTimeout(check,50)};check()})`);
    await evaluate(`document.getElementById('dashboard-overview').scrollIntoView({block:'start'})`);
    const chartNotes = await evaluate(`[...document.querySelectorAll('#dashboard-overview .metric-chart')].map(n=>{const notes=[...n.querySelectorAll('details')].find(d=>d.querySelector('summary')?.textContent==='Read chart notes'),plot=n.querySelector('svg[role="img"]');return {closed:notes&&!notes.open,name:plot?.getAttribute('aria-labelledby'),description:plot?.getAttribute('aria-describedby'),guidance:notes?.querySelector('.metric-chart__guidance')?.textContent}})`);
    assert.equal(chartNotes.length, 4);
    assert(chartNotes.every(note => note.closed && note.name.split(/\s+/).length === 1 && note.description));
    await evaluate(`(()=>{const notes=[...document.querySelectorAll('#dashboard-overview details')].find(d=>d.querySelector('summary')?.textContent==='Read chart notes');notes.querySelector('summary').focus()})()`); await press("Enter");
    assert.equal(await evaluate(`[...document.querySelectorAll('#dashboard-overview details')].find(d=>d.querySelector('summary')?.textContent==='Read chart notes').open`), true);
    assert((await assertUsableFocus("Historical chart notes must open by keyboard without losing focus")).ring);
    assert.match(chartNotes[0].guidance, /Compare local fuel use after arrival/);
    await press("Enter");
    const aggregateShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `historical-aggregate-${width}.png`), Buffer.from(aggregateShot.data, "base64"));
    await activate('[data-measurement-view="run"]');
    const singleRun = await evaluate(`(()=>{const choice=document.getElementById('overview-run'),status=document.getElementById('overview-selection-status'),notes=status.nextElementSibling;return {id:choice.value,enabled:!choice.disabled,status:status.textContent,notes:notes.textContent,notesClosed:notes.localName==='details'&&!notes.open}})()`);
    assert(singleRun.enabled && singleRun.id && singleRun.status.includes(singleRun.id));
    assert.match(singleRun.status, /^Seed \d+ · trial-/);
    assert.match(singleRun.status, /(?:\d+ samples|samples unavailable)$/);
    assert(singleRun.notesClosed);
    assert.match(singleRun.notes, /One recorded flight, with no averaging across runs/);
    for (const view of ["run", "data"]) {
      await evaluate(`document.querySelector('.lab-navigation [data-lab-view="${view}"]').focus()`); await press("Enter");
      assert.equal(await evaluate(`document.getElementById('lab-view-${view}').hidden`), false);
      assert((await assertUsableFocus("Dashboard view navigation lost keyboard focus")).ring);
    }
    await activate("#close-controllers");
    assert.equal(await evaluate(`document.activeElement.id`), "open-controllers");
    results.push({ dashboardWidth: width, ...dashboard, chartNotes, legacyViews: "Default Python profiles, catalog choices, offline setup/recovery, historical aggregate/single run and recorded data; no Run or Stop on an owner flight" });
  }

  // Isolated UI-contract fixture, never attached to the app runtime or a flight.
  // Bundle the real profile presenter in memory. The only engine-shaped method
  // accepts configure_flight metadata; no WASM, ownership or Python runs here.
  const fixtureBundle = await build({ entryPoints: ["worker/web/controller-profiles.ts"], bundle: true, write: false,
    format: "esm", platform: "browser", target: "es2022" });
  const fixtureHTML = (await readFile("worker/public/index.html", "utf8"))
    .replace('<script type="module" src="/app.js"></script>', '<script type="module" src="/__review/profile-fixture.js"></script>');
  assert(fixtureHTML.includes('/__review/profile-fixture.js') && !fixtureHTML.includes('src="/app.js"'), "The runner fixture must not initialize the real app runtime");
  const scenario = JSON.parse(await readFile("simulation/scenarios/phobos_escape.json", "utf8"));
  const fixtureBootstrap = `import {installControllerProfiles} from '/__review/profile-presenter.js';
    const scenario=${JSON.stringify(scenario)};
    const f=globalThis.__reviewProfileFixture={kind:'UI contract data only, no native or Python execution',events:[],writes:[],controls:[],failStop:false,failRead:false,active:null,results:[]};
    const expected={trial_revision:1,tuning_revision:0,manual_interventions:0,seed:100,running:false};
    const view=()=>({catalog:[],service:{online:true,busy:!!f.active},active:f.active,results:f.results});
    const bridge={status:'connected',async controllers(payload){
      if(!payload){if(f.failRead)throw Error('Deliberate isolated read failure');return view();}
      f.writes.push(structuredClone(payload));f.events.push(payload.action);
      if(payload.action==='submit'){
        if(f.active)throw Error('Fixture already has a job');
        f.active={id:'00000000-0000-4000-8000-000000000001',profile_id:payload.profile_id,status:'queued',stop_requested:false};
      }else if(payload.action==='stop'){
        if(f.failStop)throw Error('Deliberate isolated queue failure');
        if(payload.job_id!==f.active?.id)throw Error('Wrong fixture job');
        f.active={...f.active,status:'stopping',stop_requested:true};
      }else throw Error('Unexpected fixture queue write');
      return view();
    }};
    const api={observation:()=>JSON.stringify({...expected,time_s:0,flight_preset:'far_side_l2',geometry:{debris_count:500},ship_tuning:{mass_kg:scenario.experiment_settings.ship.mass_kg,thrust_scale:1},max_accel_mps2:.5,max_angular_accel_rad_s2:.05}),
      runtime_status:()=>JSON.stringify({running:false,keyboard_thrust:false}),control(raw){
        const payload=JSON.parse(raw);if(payload.op!=='configure_flight')throw Error('Fixture forbids actuation');
        f.events.push('configure_flight');f.controls.push(payload);expected.trial_revision++;
        return JSON.stringify({ok:true});
      }};
    installControllerProfiles({engine:()=>api,ready:()=>true,room:()=>null,local:()=>bridge});`;
  const fixtureAssets = new Map([
    ["/__review/profile-fixture.html", { type: "text/html", body: Buffer.from(fixtureHTML) }],
    ["/__review/profile-fixture.js", { type: "text/javascript", body: Buffer.from(fixtureBootstrap) }],
    ["/__review/profile-presenter.js", { type: "text/javascript", body: Buffer.from(fixtureBundle.outputFiles[0].contents) }],
  ]);
  const fixtureRequests = [];
  const interceptFixture = event => {
    const message = JSON.parse(String(event.data));
    if (message.sessionId !== sessionId || message.method !== "Fetch.requestPaused") return;
    const { requestId, request } = message.params, path = new URL(request.url).pathname;
    const asset = fixtureAssets.get(path);
    // No private API request is allowed even if a future fixture edit misroutes.
    const denied = !asset;
    fixtureRequests.push(path);
    void cdp("Fetch.fulfillRequest", { requestId, responseCode: denied ? 403 : 200,
      responseHeaders: [{ name: "Content-Type", value: asset?.type ?? "application/json" }, { name: "Cache-Control", value: "no-store" }],
      body: (asset?.body ?? Buffer.from('{"error":"fixture has no flight API"}')).toString("base64"),
    }).catch(error => errors.push(error.message));
  };
  ws.addEventListener("message", interceptFixture);
  await cdp("Fetch.enable", { patterns: [{ urlPattern: "*/__review/*", requestStage: "Request" },
    { urlPattern: "*/__local/*", requestStage: "Request" }, { urlPattern: "*/api/rooms/*", requestStage: "Request" }] });
  await cdp("Emulation.setDeviceMetricsOverride", { width: 720, height: 1100, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.navigate", { url: new URL("/__review/profile-fixture.html", origin).href });
  await waitFor(`document.getElementById('open-controllers')?.checkVisibility()`, "Isolated runner presenter unavailable");
  await activate("#open-controllers");
  await waitFor(`!document.getElementById('run-controller').disabled`, "Isolated ready queue did not enable Run");
  await activate("#run-controller");
  await waitFor(`document.getElementById('controller-fresh-confirm').open`, "Run must ask for replacement consent");
  assert.deepEqual(await evaluate(`({writes:__reviewProfileFixture.writes,controls:__reviewProfileFixture.controls})`), { writes: [], controls: [] });
  await activate('#controller-fresh-confirm button[value="cancel"]');
  await waitFor(`!document.getElementById('run-controller').disabled`, "Cancelled Run did not return to idle");
  assert.deepEqual(await evaluate(`__reviewProfileFixture.events`), [], "Cancelling consent must not reset or submit");
  await choose("#controller-owner", "Tom");
  await choose("#controller-profile", "tom_kalman_velocity_mvp");
  assert.deepEqual(await evaluate(`__reviewProfileFixture.events`), [], "Choosing a profile must not run it");
  await activate("#run-controller");
  await waitFor(`document.getElementById('controller-fresh-confirm').open`, "Second Run must ask for its own consent");
  await activate("#confirm-controller-run");
  await waitFor(`__reviewProfileFixture.active?.status==='queued'&&!document.getElementById('watch-controller-flight').disabled`, "Consented fixture submission did not become queued");
  const submission = await evaluate(`({events:__reviewProfileFixture.events,writes:__reviewProfileFixture.writes,controls:__reviewProfileFixture.controls})`);
  assert.deepEqual(submission.events, ["configure_flight", "submit"]);
  assert.equal(submission.writes.length, 1); assert.equal(submission.controls.length, 1);
  assert.deepEqual(submission.controls[0], { op: "configure_flight", preset: "far_side_l2", seed: scenario.default_seed, settings: scenario.experiment_settings });
  assert.deepEqual(submission.writes[0], { action: "submit", profile_id: "tom_kalman_velocity_mvp", seed: 100, duration_s: 1800,
    fresh: true, expected_context: { trial_revision: 2, tuning_revision: 0, manual_interventions: 0, seed: 100, running: false } });
  assert.match(await evaluate(`document.getElementById('controller-flight-status').textContent`), /Tom.*Queued/);
  await activate("#watch-controller-flight");
  assert.equal(await evaluate(`document.getElementById('controller-panel').open`), false);
  assert.equal(await evaluate(`document.activeElement.id`), "simulation-canvas");
  assert.equal(await evaluate(`document.getElementById('stop-controller-flight').disabled`), false);
  assert.equal(await evaluate(`document.getElementById('controller-flight').checkVisibility()`), true);
  await evaluate(`__reviewProfileFixture.failStop=true`);
  await activate("#stop-controller-flight");
  await waitFor(`document.getElementById('controller-flight-error').textContent.includes('Stop was not confirmed')&&!document.getElementById('stop-controller-flight').disabled`, "Failed Stop must remain explicit and recoverable");
  assert.equal(await evaluate(`__reviewProfileFixture.writes.filter(n=>n.action==='stop').length`), 1, "A failed Stop must not replay automatically");
  await evaluate(`__reviewProfileFixture.failStop=false`);
  await activate("#stop-controller-flight");
  await waitFor(`__reviewProfileFixture.active?.stop_requested&&document.getElementById('stop-controller-flight').disabled`, "Explicit fixture Stop was not reflected outside the modal");
  const fixtureResult = await evaluate(`({kind:__reviewProfileFixture.kind,events:__reviewProfileFixture.events,status:document.getElementById('controller-flight-status').textContent})`);
  assert.match(fixtureResult.status, /Stop requested/);
  assert(fixtureRequests.every(path => fixtureAssets.has(path)), "The isolated runner fixture must not call private flight endpoints");
  const fixtureShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  await writeFile(resolve(output, "isolated-runner-stop.png"), Buffer.from(fixtureShot.data, "base64"));
  const hypotheticalReceipt = {
    id: "00000000-0000-4000-8000-000000000001", profile_id: "tom_kalman_velocity_mvp", seed: 100, duration_s: 1800,
    state: "native_terminal", native_success: true, release_state: "acknowledged", world_metadata_unchanged: true,
    error_code: null, execution_kind: "asynchronous_fenced_leased_API", engine_sha256: "a".repeat(64), elapsed_s: 80,
    measurement: { status: "success", time_s: 80, range_m: 50, relative_speed_mps: 1, damage_j: 0, fuel_used_kg: 18.75,
      score: null, score_breakdown: null }, analytics: null,
  };
  await evaluate(`(()=>{const f=__reviewProfileFixture;f.active=null;f.results=[${JSON.stringify(hypotheticalReceipt)}];dispatchEvent(new Event('wsorb:controller-status'))})()`);
  await waitFor(`!document.getElementById('controller-flight-score').hidden&&document.getElementById('controller-flight-score').textContent==='175.00 pts · Local estimate'`, "The isolated 175 counter fixture did not show its current-formula estimate outside the modal");
  assert.equal(await evaluate(`__reviewProfileFixture.results[0].measurement.score`), null, "The estimate must not replace a native score receipt");
  await activate("#open-controllers");
  await waitFor(`document.getElementById('controller-latest-result-points').textContent==='175.00'`, "The latest-result card did not promote the hypothetical estimate");
  for (const width of [1440, 720, 320]) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height: 1100, deviceScaleFactor: 1, mobile: false });
    await evaluate(`document.querySelector('.lab-scroll').scrollTop=0`);
    const card = await evaluate(`(()=>{const panel=document.getElementById('controller-panel'),points=document.getElementById('controller-latest-result-points'),b=points.getBoundingClientRect();return {width:innerWidth,overflow:panel.scrollWidth>panel.clientWidth,points:points.textContent,visible:points.checkVisibility()&&b.top>=0&&b.bottom<=innerHeight&&b.left>=0&&b.right<=innerWidth,scope:document.getElementById('controller-latest-result-scope').textContent}})()`);
    assert.equal(card.overflow, false); assert.equal(card.points, "175.00"); assert(card.visible);
    assert.match(card.scope, /Local estimate.*STP formula.*local measurements/);
    const cardShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(resolve(output, `isolated-175-${width}.png`), Buffer.from(cardShot.data, "base64"));
    results.push({ isolatedHypotheticalPointsCard: card, notFlightEvidence: true });
  }
  await cdp("Emulation.setDeviceMetricsOverride", { width: 720, height: 1100, deviceScaleFactor: 1, mobile: false });
  await clickButton("#controller-latest-result", "View score details");
  assert.equal(await evaluate(`document.getElementById('lab-view-data').hidden`), false);
  assert.equal(await evaluate(`document.getElementById('controller-result-details').open`), false);
  await disclose("#controller-result-details");
  const fixtureTerms = await evaluate(`[...document.querySelector('#controller-score-breakdown-table .controller-score-points').parentElement.cells].slice(1,6).map(n=>Number(n.textContent))`);
  assert.deepEqual(fixtureTerms, [175, 150, 0, 25, 50]);
  assert.equal(fixtureTerms[0], fixtureTerms[1] - fixtureTerms[2] - fixtureTerms[3] + fixtureTerms[4]);
  assert.equal(await evaluate(`[...document.querySelectorAll('#controller-result-details details')].every(n=>!n.open)`), true, "Legacy totals must stay separately collapsed");
  async function publishFixtureReceipt(receipt, expectedPoints) {
    await evaluate(`__reviewProfileFixture.results=[${JSON.stringify(receipt)}]`);
    await activate("#refresh-controller-results");
    await waitFor(`document.querySelector('#controller-results-table .controller-score-points')?.textContent===${JSON.stringify(expectedPoints)}`, "Isolated receipt score did not update");
  }
  await publishFixtureReceipt({ ...hypotheticalReceipt, state: "duration_limit" }, "n/a");
  assert.equal(await evaluate(`document.getElementById('controller-flight-score').hidden`), true, "A late success observation after a time-budget result is not a terminal score");
  await publishFixtureReceipt(hypotheticalReceipt, "175.00");
  await publishFixtureReceipt({ ...hypotheticalReceipt, state: "error", error_code: "controller_failed" }, "n/a");
  const damageReceipt = { ...hypotheticalReceipt, native_success: false,
    measurement: { ...hypotheticalReceipt.measurement, status: "failed_damage", time_s: 120, range_m: 5000, damage_j: 313800, fuel_used_kg: 75 } };
  await publishFixtureReceipt(damageReceipt, "n/a");
  assert.match(await evaluate(`document.querySelector('#controller-score-breakdown-table .controller-score-points').parentElement.textContent`), /No recorded time-zero target range/);
  const definition = "native_actuation_rotation_episodes_v1";
  const analytics = { schema_version: 1, definition_id: definition,
    source_kind: "native_physics_step_counters_with_sampled_public_series",
    columns: [...NATIVE_COLUMNS], sample_count: 2,
    totals: { schema_version: 1, definition_id: definition,
      ...Object.fromEntries([...NATIVE_COUNTER_KEYS, ...NATIVE_NUMBER_KEYS].map(key => [key, key === "observed_sim_s" ? 120 : 0])),
      ...Object.fromEntries(NATIVE_FLAG_KEYS.map(key => [key, false])) },
    samples: [[0, 0, 0, 0, 0, 0, 0, 10000, 0], [120, 75, 0, 0, 0, 0, 1, 5000, 313800]],
  };
  await publishFixtureReceipt({ ...damageReceipt, analytics }, "-400.00");
  assert.equal(await evaluate(`document.getElementById('controller-flight-score').textContent`), "-400.00 pts · Local estimate");
  const laterStart = structuredClone(analytics); laterStart.samples[0][0] = .1;
  await publishFixtureReceipt({ ...damageReceipt, analytics: laterStart }, "n/a");
  await publishFixtureReceipt(hypotheticalReceipt, "175.00");
  await publishFixtureReceipt({ ...hypotheticalReceipt, id: "00000000-0000-4000-8000-000000000002" }, "175.00");
  await waitFor(`document.getElementById('controller-flight-score').hidden`, "An unrelated receipt must not supply the watched job's points");
  await evaluate(`__reviewProfileFixture.failRead=true`); await activate("#refresh-controller-results");
  await waitFor(`document.getElementById('controller-latest-result').dataset.dataState==='stale'`, "Retained point estimates must identify stale connection data");
  assert.match(await evaluate(`document.getElementById('controller-latest-result-note').textContent`), /Connection data is not current/);
  assert.equal(await evaluate(`document.getElementById('controller-flight-score').hidden`), true);
  assert.equal(await evaluate(`__reviewProfileFixture.writes.filter(n=>n.action==='submit').length`), 1, "Viewing or reducing receipts must not submit another job");
  assert(fixtureRequests.every(path => fixtureAssets.has(path)), "Score inspection must not call private flight endpoints");
  results.push({ profilePresenterFixture: fixtureResult, requests: fixtureRequests,
    hypotheticalScore: { total: 175, progress: 150, damage: 0, fuel: 25, offset: 50, terms: fixtureTerms },
    notFlightEvidence: "Only UI contract fixtures: consent, queue, Watch focus, Stop/error and current score reduction including missing r0 and stale/unrelated results; no actual engine, Python process, actuation or arrival" });
  await cdp("Fetch.disable"); ws.removeEventListener("message", interceptFixture);
  for (const width of [1440, 720, 320]) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height: 1500, deviceScaleFactor: 1, mobile: false });
    for (const name of ["official-comparison", "efficiency", "fuel-burns", "turns", "score-components"]) {
      await cdp("Page.navigate", { url: new URL(`analytics/${name}.svg`, origin).href });
      const labels = await evaluate(`(()=>{const root=document.documentElement;if(root.localName!=='svg')throw Error('SVG unavailable');const box=root.viewBox.baseVal;return {title:root.querySelector('title')?.textContent,clipped:[...root.querySelectorAll('text')].filter(n=>{const b=n.getBBox();return b.x < box.x-1||b.y < box.y-1||b.x+b.width > box.x+box.width+1||b.y+b.height > box.y+box.height+1}).map(n=>n.textContent)}})()`);
      assert(labels.title, `${name} needs a title`);
      assert.deepEqual(labels.clipped, [], `${name} has text outside its SVG viewport`);
      const shot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(resolve(output, `${name}-${width}.png`), Buffer.from(shot.data, "base64"));
      results.push({ svg: name, width, ...labels });
    }
    if (reportFile) {
      await cdp("Page.navigate", { url: pathToFileURL(reportFile).href });
      await evaluate(`new Promise((resolve,reject)=>{let n=0;const check=()=>{if(document.querySelector('#charts svg'))resolve(true);else if(++n>300)reject(Error(document.getElementById('cursor-status')?.textContent||'Python report unavailable'));else setTimeout(check,100)};check()})`);
      const report = await evaluate(`(()=>{const s=JSON.parse(document.getElementById('report-spec').textContent);return {trials:[...new Set(s.datasets.trace.map(r=>r.series_id))],repetitions:[...new Set(s.datasets.trace.map(r=>r.repetition))],svg:document.querySelectorAll('#charts svg').length}})()`);
      assert.equal(report.trials.length, 5); assert.deepEqual(report.repetitions, [1]); assert(report.svg > 0);
      await evaluate(`(()=>{const n=document.getElementById('cursor-number');n.value='33';n.dispatchEvent(new Event('input',{bubbles:true}));n.focus()})()`);
      assert.match(await evaluate(`document.getElementById('cursor-status').textContent`), /Cursor 33 s/);
      await press("ArrowUp");
      assert.notEqual(await evaluate(`document.getElementById('cursor-number').value`), "33");
      await assertUsableFocus("Python report cursor keyboard navigation lost focus");
      const reportShot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(resolve(output, `python-report-${width}.png`), Buffer.from(reportShot.data, "base64"));
      results.push({ pythonReport: reportFile, width, ...report, cursor: "Actual Vega rendered from saved trial data; native keyboard cursor" });
    }
  }
  assert.deepEqual(errors, []);
  const result = { result: "PASS", page: "Actual Chrome, built read-only comparison and offline dashboard, plus isolated runner UI fixture; no owner flight", viewports: results,
    checked: ["points and complete-pair coverage visible in the initial table viewport", "highest current-formula points first, missing scores last", "score breakdown reconciles from actual source readings and complete-pair populations", "table-first Summary with baseline-only source selection", "closed advanced controls and near-fold actual maps", "exclusive row-name inspection and additive native checkboxes", "arrival-first efficiency sorting", "metric-specific coverage and paired delta denominators", "global baseline difference shared by matrix and timelines", "whole-cohort and inspected-pair scope", "selected source CSV", "disclosed scatter axes and point/range selection", "disclosed inspector legend toggles and exact route coordinates", "single-endpoint unavailable deltas", "independent terminal annotations without missing metric values", "inspector interval and brush", "baseline-stable event windows", "cursor-only node retention", "empty selection and focus recovery", "outcome filters", "distinct scenarios", "seed/repetition mapping", "deterministic included-supplement retry", "phone keyboard and touch panning", "closed chart notes and concise accessible names", "default Test profiles catalog and offline recovery", "isolated presenter consent, queue metadata and outside Stop/error flow", "hypothetical 175-point card and exact component breakdown", "failure estimates require recorded time-zero range", "stale and unrelated receipts cannot supply watched-job points", "visible focus", "320px reflow and 200% text resize", "native control labels"],
    not_verified: ["Screen-reader speech", "browser chrome zoom", "GPU rendering or orange native trail", "actual Python controller flight", "licensed STP execution"], screenshots: output };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  console.log(`PASS: comparison workspace, dashboard and SVG review. Screenshots: ${output}`);
} catch (error) {
  let browser = null;
  if (reviewCDP) {
    try {
      const detail = await reviewCDP("Runtime.evaluate", { returnByValue: true,
        expression: `(()=>{const n=document.activeElement,b=n?.getBoundingClientRect();return {page:location.pathname,width:innerWidth,focus:n?{tag:n.localName,id:n.id,pressed:n.getAttribute('aria-pressed'),disabled:n.matches(':disabled'),box:{x:b.x,y:b.y,width:b.width,height:b.height}}:null,seed:document.querySelector('.comparison-workspace-toolbar select[id$="-seed"]')?.value,repetition:document.querySelector('.comparison-workspace-toolbar select[id$="-repetition"]')?.value,selected:[...document.querySelectorAll('[data-flight-point][aria-pressed="true"]')].map(n=>n.dataset.runId)}})()` });
      browser = detail.result.value;
      const shot = await reviewCDP("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(resolve(output, "failure.png"), Buffer.from(shot.data, "base64"));
    } catch { /* Keep the original review failure if Chrome has already closed. */ }
  }
  await writeFile(resolve(output, "result.json"), JSON.stringify({ result: "FAIL", error: String(error), browser, screenshots: output }, null, 2) + "\n");
  throw error;
} finally {
  ws?.close(); chrome?.kill("SIGTERM"); server.kill("SIGTERM");
}
