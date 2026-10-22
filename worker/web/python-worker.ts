const PYODIDE_VERSION = "0.29.3";
const PYODIDE_BASE = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const COMMAND_TIMEOUT_MS = 30_000;
const MAX_FILE_BYTES = 50 * 1024 * 1024;
const nativeImportScripts = globalThis.importScripts.bind(globalThis);
let pyodide: any;
let initializing = false;
let executing = false;
const pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();
let requestId = 0;

function progress(stage: "download" | "initialize" | "ready", file: string, loaded?: number, total?: number, message?: string) {
  postMessage({ type: "progress", stage, file, ...(loaded === undefined ? {} : { loaded }), ...(total === undefined ? {} : { total }), ...(message ? { message } : {}) });
}

async function readBounded(response: Response, file: string, report: boolean): Promise<ArrayBuffer> {
  if (!response.ok) throw new Error(`Could not download ${file} (HTTP ${response.status}).`);
  const headerLength = Number(response.headers.get("content-length"));
  const total = Number.isFinite(headerLength) && headerLength > 0 ? headerLength : undefined;
  const reader = response.body?.getReader();
  if (!reader) {
    const data = await response.arrayBuffer();
    if (data.byteLength > MAX_FILE_BYTES) throw new Error(`${file} exceeds the 50 MB download limit.`);
    if (report) progress("download", file, data.byteLength, total, total === undefined ? "Download complete; server did not provide a size." : undefined);
    return data;
  }
  const chunks: Uint8Array[] = [];
  let loaded = 0;
  if (report) progress("download", file, 0, total);
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    loaded += value.byteLength;
    if (loaded > MAX_FILE_BYTES) {
      await reader.cancel();
      throw new Error(`${file} exceeds the 50 MB download limit.`);
    }
    chunks.push(value);
    if (report) progress("download", file, loaded, total);
  }
  const bytes = new Uint8Array(loaded);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  if (report && total === undefined) progress("download", file, loaded, undefined, "Download complete; server did not provide a size.");
  return bytes.buffer;
}

async function fetchBlobScript(url: string): Promise<string> {
  const file = new URL(url).pathname.split("/").pop() || "pyodide.js";
  const bytes = await readBounded(await fetch(url), file, true);
  return URL.createObjectURL(new Blob([bytes], { type: "text/javascript" }));
}

const originalFetch = globalThis.fetch.bind(globalThis);
globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const request = input instanceof Request ? input.url : String(input);
  if (!request.startsWith(PYODIDE_BASE)) return originalFetch(input, init);
  const file = new URL(request).pathname.split("/").pop() || "runtime file";
  const response = await originalFetch(input, init);
  const { status, statusText, headers } = response;
  const bytes = await readBounded(response, file, true);
  return new Response(bytes, { status, statusText, headers });
};

function simCall(command: Record<string, unknown>) {
  const id = ++requestId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("Simulation host did not respond. Check that a host is online.")); }, COMMAND_TIMEOUT_MS);
    pending.set(id, { resolve: (value) => { clearTimeout(timer); resolve(value); }, reject: (error) => { clearTimeout(timer); reject(error); } });
    postMessage({ type: "command", id, command });
  });
}

function jsArray(value: unknown): unknown[] {
  if (value && typeof value === "object" && "toJs" in value) {
    const proxy = value as { toJs: (options?: unknown) => unknown };
    // Function arguments are borrowed proxies. Pyodide releases them when the
    // call returns; destroying one here corrupts its automatic proxy cleanup.
    return Array.from(proxy.toJs({ create_proxies: false }) as Iterable<unknown>);
  }
  return Array.from(value as Iterable<unknown>);
}

const bridge = {
  reset: (seed = 100) => simCall({ op: "reset", seed }),
  observe: () => simCall({ op: "observe" }),
  step: (seconds: number) => simCall({ op: "step", seconds }),
  set_command: (accel: unknown, angular_accel: unknown = [0, 0, 0]) => simCall({ op: "set_command", accel: jsArray(accel), angular_accel: jsArray(angular_accel) }),
  stop: () => simCall({ op: "stop", entity: "Shuttle" }),
};

async function initialize() {
  if (pyodide || initializing) return;
  initializing = true;
  let scriptUrl: string | undefined;
  let asmUrl: string | undefined;
  const originalImportScripts = globalThis.importScripts;
  try {
    scriptUrl = await fetchBlobScript(`${PYODIDE_BASE}pyodide.js`);
    nativeImportScripts(scriptUrl);
    URL.revokeObjectURL(scriptUrl);
    scriptUrl = undefined;
    asmUrl = await fetchBlobScript(`${PYODIDE_BASE}pyodide.asm.js`);
    globalThis.importScripts = ((...urls: string[]) => nativeImportScripts(...urls.map(url => url === `${PYODIDE_BASE}pyodide.asm.js` ? asmUrl! : url))) as typeof importScripts;
    progress("initialize", "Pyodide runtime", undefined, undefined, "Starting Python. Additional runtime downloads will report their sizes.");
    pyodide = await (globalThis as any).loadPyodide({ indexURL: PYODIDE_BASE });
    globalThis.importScripts = originalImportScripts;
    URL.revokeObjectURL(asmUrl);
    asmUrl = undefined;
    pyodide.registerJsModule("_wsorb_bridge", bridge);
    await pyodide.runPythonAsync(`
import _wsorb_bridge

def _as_python(value):
    convert = getattr(value, "to_py", None)
    if convert is None:
        return value
    try:
        return convert()
    finally:
        destroy = getattr(value, "destroy", None)
        if destroy is not None:
            destroy()

class _Sim:
    async def reset(self, seed=100):
        return _as_python(await _wsorb_bridge.reset(seed))
    async def observe(self):
        return _as_python(await _wsorb_bridge.observe())
    async def step(self, seconds):
        return _as_python(await _wsorb_bridge.step(seconds))
    async def set_command(self, accel, angular_accel=(0, 0, 0)):
        return _as_python(await _wsorb_bridge.set_command(accel, angular_accel))
    async def stop(self):
        return _as_python(await _wsorb_bridge.stop())
    async def radar(self, n=20):
        observation = await self.observe()
        return list(observation.get("radar", []))[:n]

sim = _Sim()
async def SetCommand(accel, angular_accel=(0, 0, 0)):
    return await sim.set_command(accel, angular_accel)
async def GetNearestDebrisLocations(n=20):
    return await sim.radar(n)
`);
    progress("ready", "", undefined, undefined, "Python ready.");
    postMessage({ type: "ready", version: PYODIDE_VERSION });
  } catch (error) {
    globalThis.importScripts = originalImportScripts;
    postMessage({ type: "error", phase: "init", error: error instanceof Error ? error.message : String(error) });
  } finally {
    if (scriptUrl) URL.revokeObjectURL(scriptUrl);
    if (asmUrl) URL.revokeObjectURL(asmUrl);
    initializing = false;
  }
}

onmessage = async ({ data }) => {
  if (data.type === "result") {
    const call = pending.get(data.id);
    if (call) { pending.delete(data.id); data.ok ? call.resolve(data.state) : call.reject(new Error(data.error || "Simulation command failed")); }
    return;
  }
  if (data.type === "init") { await initialize(); return; }
  if (data.type === "run" && pyodide) {
    if (executing) { postMessage({ type: "error", phase: "execute", error: "A strategy is already running." }); return; }
    executing = true;
    try {
      postMessage({ type: "status", status: "Running strategy…" });
      pyodide.setStdout({ batched: (text: string) => postMessage({ type: "stdout", text }) });
      pyodide.setStderr({ batched: (text: string) => postMessage({ type: "stderr", text }) });
      await pyodide.runPythonAsync(`for _name in list(globals()):
    if not _name.startswith("__") and _name not in {"_wsorb_bridge", "_as_python", "_Sim", "sim", "SetCommand", "GetNearestDebrisLocations"}:
        del globals()[_name]`);
      await pyodide.runPythonAsync(data.code);
      postMessage({ type: "done" });
    } catch (error) {
      postMessage({ type: "error", phase: "execute", error: error instanceof Error ? error.message : String(error) });
    } finally { executing = false; }
  }
};

addEventListener("close", () => { for (const call of pending.values()) call.reject(new Error("Python worker stopped")); pending.clear(); });
addEventListener("unhandledrejection", event => {
  postMessage({ type: "error", phase: "fatal", error: event.reason instanceof Error ? event.reason.message : String(event.reason) });
});
