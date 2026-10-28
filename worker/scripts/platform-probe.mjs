// Probe the shipped CPU/WASM engine in a disposable instance, never a live flight.
// No DOM, GPU, renderer, engine callback or replacement clock is installed.
import { createHash } from "node:crypto";
import { lstat, readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { isMainThread, parentPort, Worker, workerData } from "node:worker_threads";

const SCHEMA = "wsorb.platform_probe.v1";
const MEASUREMENT = "CPU/WASM only in this Node process; not browser/GPU certification or licensed STP validation.";
const TIMEOUT_MS = 30_000;
const TICK_MS = 20;

const host = () => ({ platform: process.platform, arch: process.arch, node: process.version });
const require = (condition, message) => { if (!condition) throw new Error(message); };

function finiteJSON(value) {
  if (typeof value === "number") require(Number.isFinite(value), "Probe report contains a non-finite number");
  else if (Array.isArray(value)) value.forEach(finiteJSON);
  else if (value !== null && typeof value === "object") Object.values(value).forEach(finiteJSON);
  else require(value === null || ["string", "boolean"].includes(typeof value), "Probe report contains a non-JSON value");
  return value;
}

function failure(error, stage) {
  const hint = stage === "assets" && error?.code === "ENOENT"
    ? " Provide the complete prebuilt worker/public/sim assets; this probe never builds them." : "";
  return {
    schema: SCHEMA, ok: false, measurement: MEASUREMENT, host: host(), browser_gpu_verified: false,
    error: {
      stage,
      message: (String(error?.message ?? error) + hint).slice(0, 2048),
      code: typeof error?.code === "string" ? error.code.slice(0, 80) : null,
    },
  };
}

function inside(root, candidate, paths = path) {
  const relative = paths.relative(root, candidate);
  // Windows relative() returns an absolute path for a different drive/share.
  return relative !== "" && relative !== ".." && !relative.startsWith(`..${paths.sep}`)
    && !paths.isAbsolute(relative);
}

function windowsPathMath() {
  const root = "C:\\Games\\WSO #100% ü\\worker\\public";
  const glue = path.win32.resolve(root, "sim", "wsorb_bevy.js");
  return {
    measurement: "Pure Windows path/URL calculations, not Windows execution or filesystem access.",
    drive_url: pathToFileURL(glue, { windows: true }).href,
    unc_url: pathToFileURL("\\\\game-host\\share\\WSO #100% ü\\worker\\public\\sim\\wsorb_bevy.js", { windows: true }).href,
    contained_file: inside(root, glue, path.win32),
    sibling_directory: inside(root, `${root}-other\\sim\\wsorb_bevy.js`, path.win32),
    parent_directory: inside(root, path.win32.resolve(root, "..", "private.json"), path.win32),
    other_drive: inside(root, "D:\\private.json", path.win32),
    other_share: inside("\\\\game-host\\public", "\\\\game-host\\private\\token", path.win32),
    drive_relative_is_absolute: path.win32.isAbsolute("C:sim\\wsorb_bevy.js"),
  };
}

async function probe() {
  let stage = "runtime";
  try {
    const [major, minor, patch] = process.versions.node.split(".").map(Number);
    require(major === 26 && (minor > 12 || (minor === 12 && patch >= 1)),
      "Use the project's Node.js ^26.12.1 runtime to run this probe.");
    require(typeof globalThis.document === "undefined" && typeof globalThis.window === "undefined",
      "Run this CPU/WASM probe without a DOM.");
    // A real Node build may expose WebGPU. Availability alone is not rendering;
    // this probe never requests an adapter and checks actual native state below.
    const gpuAPIAvailable = typeof globalThis.navigator?.gpu !== "undefined";

    stage = "assets";
    // Resolve from this script, not the caller's cwd or a private owner descriptor.
    const publicRoot = fileURLToPath(new URL("../public/", import.meta.url));
    for (const directory of [publicRoot, path.resolve(publicRoot, "sim")]) {
      const info = await lstat(directory);
      require(info.isDirectory() && !info.isSymbolicLink(), "Probe asset directories must not be symlinks.");
    }
    const rootRealPath = await realpath(publicRoot);
    async function asset(name, size) {
      const filename = path.resolve(publicRoot, "sim", name);
      const info = await lstat(filename);
      require(info.isFile() && !info.isSymbolicLink(), "Probe assets must be regular files, not symlinks.");
      if (size !== undefined) require(info.size === size, "WASM size does not match the build manifest.");
      const actual = await realpath(filename);
      require(inside(rootRealPath, actual), "Probe asset is outside public assets.");
      return { filename: actual, size: info.size };
    }
    const manifestFile = await asset("manifest.json");
    require(manifestFile.size <= 4096, "Invalid engine manifest size.");
    const manifest = JSON.parse(await readFile(manifestFile.filename, "utf8"));
    require(manifest !== null && typeof manifest === "object" && !Array.isArray(manifest)
      && Number.isSafeInteger(manifest.bytes) && manifest.bytes > 0 && manifest.bytes <= 64 * 1048576
      && typeof manifest.sha256 === "string" && /^[0-9a-f]{64}$/.test(manifest.sha256), "Invalid engine manifest.");
    const wasm = await readFile((await asset("wsorb_bevy_bg.wasm", manifest.bytes)).filename);
    const sha256 = createHash("sha256").update(wasm).digest("hex");
    require(wasm.length === manifest.bytes, "WASM size does not match the build manifest.");
    require(sha256 === manifest.sha256, "WASM SHA-256 does not match the build manifest.");

    stage = "webassembly";
    const moduleUrl = pathToFileURL((await asset("wsorb_bevy.js")).filename);
    moduleUrl.searchParams.set("v", manifest.sha256);
    const api = await import(moduleUrl.href);
    for (const name of ["default", "headless_start", "headless_tick", "headless_status", "control",
      "observation", "runtime_status", "renderer_status", "ship_camera_status", "ship_camera_capture"]) {
      require(typeof api[name] === "function", `Built engine lacks ${name}; provide a current complete build.`);
    }
    await api.default({ module_or_path: wasm });
    const read = name => finiteJSON(JSON.parse(api[name]()));
    const nativeCall = (name, ...args) => {
      const result = api[name](...args);
      if (typeof result === "string") {
        const value = finiteJSON(JSON.parse(result));
        require(value?.ok !== false, `Native ${name} failed: ${value?.error ?? "unknown error"}`);
      }
    };
    const control = command => {
      const ack = finiteJSON(JSON.parse(api.control(JSON.stringify(command))));
      require(ack?.ok === true, `Native command rejected: ${ack?.error ?? "invalid acknowledgement"}`);
      return ack;
    };
    const snapshot = () => {
      const observation = read("observation");
      require(observation.geometry?.debris_count === 500 && observation.experiment_settings?.debris?.count === 500,
        "Probe requires the actual stock 500-body world; it will not reconfigure a different world.");
      require([observation.radar, observation.radar_backup].every(rows => Array.isArray(rows)
        && rows.length > 0 && rows.length <= 500), "Native stock-world radar observations are missing or invalid.");
      require(observation.shuttle != null && observation.target != null
        && typeof observation.time_s === "number" && typeof observation.navigation_sample_time_s === "number"
        && observation.navigation_sample_time_s <= observation.time_s, "Native public navigation observations are missing or invalid.");
      return {
        observation: {
          time_s: observation.time_s, seed: observation.seed, flight_preset: observation.flight_preset,
          trial_revision: observation.trial_revision, tuning_revision: observation.tuning_revision,
          manual_interventions: observation.manual_interventions, geometry: observation.geometry,
          radar_rows: observation.radar.length, radar_backup_rows: observation.radar_backup.length,
          navigation_sample_time_s: observation.navigation_sample_time_s,
          shuttle: observation.shuttle, target: observation.target, mission: observation.mission,
        },
        runtime: read("runtime_status"), physics: read("headless_status"),
      };
    };
    const idle = physics => require(physics.started === true && physics.owned === true
      && physics.phase === "idle" && physics.error === null, "Native headless scheduler is not idle and healthy.");
    const context = sample => ({
      trial_revision: sample.observation.trial_revision, tuning_revision: sample.observation.tuning_revision,
      manual_interventions: sample.observation.manual_interventions, seed: sample.observation.seed,
      running: sample.runtime.running,
    });

    stage = "headless_start";
    const initial = snapshot();
    require(initial.physics.phase === "unstarted" && initial.physics.started === false
      && initial.physics.owned === false && initial.runtime.running === false, "Probe did not receive a fresh, paused engine instance.");
    require(initial.observation.seed === 100 && initial.observation.flight_preset === "far_side_l2"
      && initial.observation.time_s === 0 && initial.observation.manual_interventions === 0,
      "Probe requires the stock seed-100 far-side L2 world at time zero; no world reset was attempted.");
    const rendererBefore = read("renderer_status");
    const camerasBefore = read("ship_camera_status");
    nativeCall("headless_start");
    // Publish the initial Runtime/Reports schedule without advancing either clock.
    nativeCall("headless_tick", 0);
    const started = snapshot();
    idle(started.physics);
    require(started.observation.time_s === initial.observation.time_s && started.physics.wall_elapsed_s === 0
      && started.runtime.running === false, "Headless startup unexpectedly advanced the paused world.");

    stage = "checked_play";
    const playAck = control({ op: "play", rate: 1, expected_context: context(started) });
    const playQueued = { ack: playAck, ...snapshot() };
    require(playQueued.runtime.running === false && playQueued.observation.time_s === started.observation.time_s,
      "Play acknowledgement must only queue playback before the next native tick.");

    let previous = performance.now();
    const clockStarted = previous;
    const deltas = [];
    async function tick() {
      await delay(TICK_MS);
      const now = performance.now();
      const delta = (now - previous) / 1000;
      require(now - clockStarted < 5000, "Native playback did not complete within the five-second tick budget.");
      require(Number.isFinite(delta) && delta > 0 && delta <= 1,
        "Host timer stalled outside the native 0..1 second tick limit; rerun without suspending the process.");
      previous = now;
      // Feed the measured duration once. No step command, catch-up or fake clock.
      nativeCall("headless_tick", delta);
      deltas.push(delta);
      const physics = read("headless_status");
      idle(physics);
      require(Math.abs(physics.wall_elapsed_s - (now - clockStarted) / 1000) < 0.000001,
        "Native elapsed wall clock differs from the actual performance.now tick durations.");
    }

    stage = "playback";
    let running;
    do {
      await tick();
      running = snapshot();
    } while (previous - clockStarted < 150 || running.observation.time_s <= started.observation.time_s);
    require(running.runtime.running === true && running.physics.running === true
      && running.physics.time_s === running.observation.time_s, "Native Play did not apply and advance physics.");

    stage = "pause";
    const pauseAck = control({ op: "pause" });
    const pauseQueued = { ack: pauseAck, ...snapshot() };
    require(pauseQueued.runtime.running === true, "Pause acknowledgement did not preserve queued-action semantics.");
    await tick();
    await tick();
    const paused = snapshot();
    require(paused.runtime.running === false && paused.physics.running === false
      && paused.observation.time_s === running.observation.time_s
      && paused.physics.wall_elapsed_s > running.physics.wall_elapsed_s, "Native pause did not freeze physics while real wall time advanced.");

    stage = "uninitialized_graphics";
    const renderer = read("renderer_status");
    const cameras = read("ship_camera_status");
    require(renderer.started === false && renderer.ready === false && renderer.adapter === null
      && renderer.rendered_frames === 0 && JSON.stringify(renderer) === JSON.stringify(rendererBefore),
      "CPU/WASM probe unexpectedly initialized or changed the renderer.");
    require(camerasBefore.configured === false && camerasBefore.views?.length === 0
      && cameras.configured === false && cameras.views?.length === 0, "CPU/WASM probe unexpectedly produced camera samples.");
    const captures = [];
    for (const camera of ["top", "bottom"]) {
      let rejection;
      try { await api.ship_camera_capture(camera); }
      catch (error) { rejection = error; }
      require(rejection?.code === "sensor_no_sample" && rejection?.camera === camera
        && typeof rejection?.message === "string", "Uninitialized native camera did not reject capture with sensor_no_sample.");
      captures.push({ camera: rejection.camera, code: rejection.code, message: rejection.message });
    }

    return finiteJSON({
      schema: SCHEMA, ok: true, measurement: MEASUREMENT, host: host(), browser_gpu_verified: false,
      node_gpu_api_available: gpuAPIAvailable,
      engine_build: { bytes: wasm.length, sha256, wasm_matches_manifest: true },
      observation_scope: "Selected actual public observation fields; no debug world or substituted sensor responses.",
      initial, started, play_queued: playQueued, running, pause_queued: pauseQueued, paused,
      clock: { source: "performance.now", tick_deltas_s: deltas, measured_elapsed_s: (previous - clockStarted) / 1000 },
      renderer, cameras, capture_rejections: captures, windows_path_math: windowsPathMath(),
      limits: ["No browser, WebGPU device, rendered frame or RGB readback was tested.",
        "No private lab, owner HTTP connection, Windows ACL protection or licensed STP runtime was tested.",
        "Platform and architecture identify this Node process only; path calculations do not test Windows execution."],
    });
  } catch (error) { return failure(error, stage); }
}

async function boundedProbe() {
  // A separate V8 instance owns this engine. The watchdog can stop a stuck WASM
  // call without installing callbacks or touching any other process/flight.
  const worker = new Worker(new URL(import.meta.url), {
    workerData: { task: SCHEMA }, stdout: true, stderr: true,
  });
  worker.stdout.pipe(process.stderr);
  worker.stderr.pipe(process.stderr);
  let timer;
  try {
    return await new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error("CPU/WASM probe exceeded its 30-second deadline.")), TIMEOUT_MS);
      worker.once("message", resolve);
      worker.once("error", reject);
      worker.once("exit", code => reject(new Error(`Probe worker exited without a result, code ${code}.`)));
    });
  } finally {
    clearTimeout(timer);
    await worker.terminate();
  }
}

if (!isMainThread && workerData?.task === SCHEMA) {
  parentPort.postMessage(await probe());
} else if (isMainThread && (import.meta.main ?? (process.argv[1]
  && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url))) {
  const args = process.argv.slice(2);
  if (args.length === 1 && ["--help", "-h"].includes(args[0])) {
    console.log("Usage: node worker/scripts/platform-probe.mjs\nRuns the same prebuilt engine in its own CPU/WASM instance with actual elapsed-time ticks.\nNo build, live flight, private lab, browser or GPU is accessed. Requires Node.js ^26.12.1.\nThis is not browser/GPU certification. There is no platform override.");
  } else {
    let result;
    try {
      require(args.length === 0, "Only --help is accepted. Platform/architecture overrides are not supported.");
      result = finiteJSON(await boundedProbe());
    } catch (error) { result = failure(error, "probe_process"); }
    console.log(JSON.stringify(result));
    if (!result.ok) process.exitCode = 1;
  }
}
