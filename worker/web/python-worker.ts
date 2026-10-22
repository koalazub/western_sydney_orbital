const PYODIDE_VERSION = "0.29.3";
const COMMAND_TIMEOUT_MS = 12_000;
let pyodide: any;
const pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();
let requestId = 0;

function simCall(command: Record<string, unknown>) {
  const id = ++requestId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("Simulation host did not respond. Check that a host is online.")); }, COMMAND_TIMEOUT_MS);
    pending.set(id, { resolve: (value) => { clearTimeout(timer); resolve(value); }, reject: (error) => { clearTimeout(timer); reject(error); } });
    postMessage({ type: "command", id, command });
  });
}

const bridge = {
  reset: (seed = 42) => simCall({ op: "reset", seed }),
  observe: () => simCall({ op: "observe" }),
  move_to: (entity: string, x: number, y: number) => simCall({ op: "move_to", entity, x, y }),
  stop: (entity: string) => simCall({ op: "stop", entity }),
  step: (seconds: number) => simCall({ op: "step", seconds }),
  report: () => simCall({ op: "report" }),
  set_attribute: (entity: string, attribute: string, value: unknown) => simCall({ op: "set_attribute", entity, attribute, value }),
};

onmessage = async ({ data }) => {
  if (data.type === "result") {
    const call = pending.get(data.id);
    if (call) { pending.delete(data.id); data.ok ? call.resolve(data.state) : call.reject(new Error(data.error || "Simulation command failed")); }
    return;
  }
  if (data.type === "init") {
    try {
      importScripts(`https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/pyodide.js`);
      // The pinned official CDN bundle exposes loadPyodide as a global.
      pyodide = await (globalThis as any).loadPyodide({ indexURL: `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/` });
      pyodide.registerJsModule("_wsorb_bridge", bridge);
      postMessage({ type: "ready", version: PYODIDE_VERSION });
    } catch (error) { postMessage({ type: "error", error: error instanceof Error ? error.message : String(error) }); }
    return;
  }
  if (data.type === "run" && pyodide) {
    try {
      postMessage({ type: "status", status: "Running strategy…" });
      pyodide.setStdout({ batched: (text: string) => postMessage({ type: "stdout", text }) });
      pyodide.setStderr({ batched: (text: string) => postMessage({ type: "stderr", text }) });
      await pyodide.runPythonAsync(`import _wsorb_bridge\nclass _Sim:\n    async def reset(self, seed=42): return (await _wsorb_bridge.reset(seed)).to_py()\n    async def observe(self): return (await _wsorb_bridge.observe()).to_py()\n    async def move_to(self, entity, x, y): return (await _wsorb_bridge.move_to(entity, x, y)).to_py()\n    async def stop(self, entity): return (await _wsorb_bridge.stop(entity)).to_py()\n    async def step(self, seconds): return (await _wsorb_bridge.step(seconds)).to_py()\n    async def report(self): return (await _wsorb_bridge.report()).to_py()\n    async def set_attribute(self, entity, attribute, value): return (await _wsorb_bridge.set_attribute(entity, attribute, value)).to_py()\nsim = _Sim()\n`);
      await pyodide.runPythonAsync(data.code);
      postMessage({ type: "done" });
    } catch (error) { postMessage({ type: "error", error: error instanceof Error ? error.message : String(error) }); }
  }
};

addEventListener("close", () => { for (const call of pending.values()) call.reject(new Error("Python worker stopped")); pending.clear(); });
