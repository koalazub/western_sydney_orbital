import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const worker = fileURLToPath(new URL("..", import.meta.url));
// These stages own disjoint outputs. License metadata can still wait on Cargo's
// package-download cache on a cold build; it does not compile another target.
const jobs = [
  { stage: "types", command: process.execPath, args: ["scripts/cloudflare.mjs", "wrangler", "types"] },
  { stage: "web", command: process.execPath, args: ["scripts/build-web.mjs"] },
  { stage: "licenses", command: process.execPath, args: ["scripts/build-licenses.mjs"] },
  { stage: "sim", command: "bash", args: ["scripts/build-sim.sh"] },
];
const running = new Set();
const started = process.hrtime.bigint();
let stopping = false;
let interrupted;
let killTimer;

function signalJob(child, signal) {
  if (!child.pid) return;
  try {
    // Each stage and its subprocesses share a private POSIX process group on
    // all four supported Nix platforms, including Cargo and Wrangler children.
    process.kill(-child.pid, signal);
  } catch (error) {
    if (error.code !== "ESRCH") {
      console.error(`Could not signal build process group ${child.pid}: ${error.message}`);
      child.kill(signal);
    }
  }
}

function stop(signal = "SIGTERM") {
  if (stopping) return;
  stopping = true;
  for (const child of running) signalJob(child, signal);
  killTimer = setTimeout(() => {
    for (const child of running) signalJob(child, "SIGKILL");
  }, 5000).unref();
}

function onSignal(signal) {
  interrupted = signal;
  stop(signal);
}
process.on("SIGINT", onSignal);
process.on("SIGTERM", onSignal);

function run(job) {
  const start = process.hrtime.bigint();
  return new Promise(resolve => {
    const child = spawn(job.command, job.args, { cwd: worker, stdio: "inherit", detached: true });
    running.add(child);
    let error;
    child.once("error", cause => {
      error = cause.message;
      stop();
    });
    child.once("close", (code, signal) => {
      const ok = code === 0 && !error;
      if (!ok) stop();
      // A failed wrapper may have exited before its descendants. Kill any
      // remaining group members before considering this job drained.
      if (stopping) signalJob(child, "SIGKILL");
      running.delete(child);
      resolve({ code, signal, ...(error ? { error } : {}) });
    });
  }).catch(error => {
    // Also drain siblings if spawn itself throws before a ChildProcess exists.
    stop();
    return { code: null, signal: null, error: error.message };
  }).then(result => {
    console.log(JSON.stringify({
      event: "build_stage", stage: job.stage,
      duration_ms: Number(process.hrtime.bigint() - start) / 1e6, ...result,
    }));
    return result.code === 0 && !result.error;
  });
}

const results = await Promise.allSettled(jobs.map(run));
clearTimeout(killTimer);
process.off("SIGINT", onSignal);
process.off("SIGTERM", onSignal);
const ok = !stopping && results.every(result => result.status === "fulfilled" && result.value);
console.log(JSON.stringify({
  event: "build_total", duration_ms: Number(process.hrtime.bigint() - started) / 1e6,
  status: ok ? "ok" : "failed", ...(interrupted ? { signal: interrupted } : {}),
}));
process.exitCode = interrupted === "SIGINT" ? 130 : interrupted === "SIGTERM" ? 143 : ok ? 0 : 1;
