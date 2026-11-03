import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { access, copyFile, mkdir, mkdtemp, readFile, readdir, realpath, rename, rm, rmdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scripts = dirname(fileURLToPath(import.meta.url));
const repository = resolve(scripts, "../..");
const published = join(repository, "worker/public/sim");
const receiptPath = join(repository, ".devenv/wsorb-wasm-cache.json");
const lockPath = `${receiptPath}.lock`;
const names = ["wsorb_bevy.js", "wsorb_bevy.d.ts", "wsorb_bevy_bg.wasm", "wsorb_bevy_bg.wasm.d.ts", "manifest.json"];
const bindgenFlags = ["--target", "web", "--out-name", "wsorb_bevy"];
const optFlags = ["-Oz", "--all-features", "--strip-debug"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
let activeChild;
let interrupted;

function checkInterrupted() {
  if (interrupted) throw new Error(`Simulation build interrupted by ${interrupted}`);
}

function onSignal(signal) {
  interrupted = signal;
  activeChild?.kill(signal);
}
process.on("SIGINT", onSignal);
process.on("SIGTERM", onSignal);

async function timed(stage, action) {
  const start = process.hrtime.bigint();
  let status = "failed";
  try {
    const result = await action();
    status = "ok";
    return result;
  } finally {
    console.log(JSON.stringify({
      event: "sim_stage", stage, status,
      duration_ms: Number(process.hrtime.bigint() - start) / 1e6,
    }));
  }
}

function run(command, args, capture = false) {
  checkInterrupted();
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: repository, stdio: ["ignore", capture ? "pipe" : "inherit", "inherit"],
    });
    activeChild = child;
    let output = "";
    let error;
    child.stdout?.setEncoding("utf8").on("data", bytes => { output += bytes; });
    child.once("error", cause => { error = cause; });
    child.once("close", (code, signal) => {
      activeChild = undefined;
      if (error || code !== 0 || interrupted) {
        reject(error ?? new Error(`${command} failed: ${signal ?? interrupted ?? code}`));
      } else resolve(output);
    });
  });
}

async function tool(name) {
  for (const directory of (process.env.PATH ?? "").split(delimiter)) {
    const path = resolve(repository, directory || ".", name);
    try {
      await access(path, constants.X_OK);
      const resolved = await realpath(path);
      const version = (await run(resolved, ["--version"], true)).trim();
      if (!version) throw new Error(`${name} returned an empty version`);
      return { path, resolved, version };
    } catch (error) {
      if (!["ENOENT", "ENOTDIR", "EACCES"].includes(error.code)) throw error;
    }
  }
  throw new Error(`${name} is missing from the project environment`);
}

async function artifacts(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  if (entries.length !== names.length || entries.some(entry => !entry.isFile() || !names.includes(entry.name))) {
    throw new Error(`Incomplete or unexpected simulation artifact set in ${directory}`);
  }
  const contents = await Promise.all(names.map(name => readFile(join(directory, name))));
  if (contents.some(bytes => bytes.length === 0)) throw new Error(`Empty simulation artifact in ${directory}`);
  const files = Object.fromEntries(names.map((name, index) => [name, hash(contents[index])]));
  const manifest = JSON.parse(contents[names.indexOf("manifest.json")].toString("utf8"));
  const wasm = contents[names.indexOf("wsorb_bevy_bg.wasm")];
  if (Object.keys(manifest).sort().join(",") !== "bytes,sha256"
      || manifest.bytes !== wasm.length || manifest.sha256 !== files["wsorb_bevy_bg.wasm"]
      || wasm.subarray(0, 8).toString("hex") !== "0061736d01000000") {
    throw new Error(`Simulation manifest does not match its WASM in ${directory}`);
  }
  return files;
}

function sameFiles(left, right) {
  return left && typeof left === "object" && Object.keys(left).length === names.length
    && names.every(name => left[name] === right[name]);
}

async function main() {
  await mkdir(dirname(receiptPath), { recursive: true });
  try { await mkdir(lockPath); }
  catch (error) {
    if (error.code === "EEXIST") {
      throw new Error(`Simulation build lock exists: ${lockPath}. Do not run overlapping builds; investigate a stale lock only after its build has stopped.`);
    }
    throw error;
  }
  let work;
  let publishStage;
  let temporaryReceipt;
  try {
    // Cargo always owns Rust freshness, including compiler, features and flags.
    await timed("cargo", () => run("cargo", [
      "build", "--manifest-path", "simulation/Cargo.toml", "--locked", "--release", "--target", "wasm32-unknown-unknown",
    ]));
    let input;
    let raw;
    let key;
    const hit = await timed("cache_check", async () => {
      const target = resolve(repository, process.env.CARGO_TARGET_DIR || "simulation/target");
      const path = join(target, "wasm32-unknown-unknown/release/wsorb_bevy.wasm");
      raw = await readFile(path);
      input = {
        wasm: { path, sha256: hash(raw) },
        tools: {
          bindgen: await tool("wasm-bindgen"), opt: await tool("wasm-opt"),
          node: { path: await realpath(process.execPath), version: process.version },
        },
        // Only invocation-local paths are normalized; all transformation flags
        // and this implementation are part of the cache identity.
        commands: {
          bindgen: [...bindgenFlags, "--out-dir", "<stage>", "<cargo-wasm>"],
          opt: [...optFlags, "<stage>/wsorb_bevy_bg.wasm", "-o", "<stage>/wsorb_bevy_bg.optimized.wasm"],
        },
        scripts: { "wasm-artifacts.mjs": hash(await readFile(join(scripts, "wasm-artifacts.mjs"))) },
      };
      key = hash(JSON.stringify(input));
      try {
        const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
        if (receipt.schema === 1 && receipt.key === key && sameFiles(receipt.files, await artifacts(published))) {
          console.log(JSON.stringify({ event: "wasm_cache", status: "hit" }));
          return true;
        }
      } catch (error) {
        if (error.code !== "ENOENT") console.warn(`WASM cache validation failed; regenerating: ${error.message}`);
      }
      console.log(JSON.stringify({ event: "wasm_cache", status: "miss" }));
      return false;
    });
    checkInterrupted();
    if (hit) return;
    // os.tmpdir honors the environment's approved TMPDIR. The exact mkdtemp
    // directory below is the only private tool-output tree removed on exit.
    work = await mkdtemp(join(tmpdir(), "wsorb-build-"));
    const stage = join(work, "artifacts");
    await mkdir(stage);
    const frozenInput = join(work, "input.wasm");
    await writeFile(frozenInput, raw);
    await timed("bindgen", () => run(input.tools.bindgen.resolved, [...bindgenFlags, "--out-dir", stage, frozenInput]));
    const wasmPath = join(stage, "wsorb_bevy_bg.wasm");
    await timed("wasm_opt", () => run(input.tools.opt.resolved, [...optFlags, wasmPath, "-o", join(stage, "wsorb_bevy_bg.optimized.wasm")]));
    await rename(join(stage, "wsorb_bevy_bg.optimized.wasm"), wasmPath);
    const wasm = await readFile(wasmPath);
    await writeFile(join(stage, "manifest.json"), JSON.stringify({ bytes: wasm.length, sha256: hash(wasm) }) + "\n");
    const expected = await artifacts(stage);
    await timed("publish", async () => {
      checkInterrupted();
      await mkdir(published, { recursive: true });
      // Prepare on the destination filesystem, even when TMPDIR is elsewhere.
      publishStage = await mkdtemp(join(published, ".wsorb-publish-"));
      for (const name of names) {
        checkInterrupted();
        await copyFile(join(stage, name), join(publishStage, name));
      }
      if (!sameFiles(expected, await artifacts(publishStage))) throw new Error("Simulation publication copy differs from staged artifacts");
      // Each replacement is atomic, NOT the whole set. Fixed-path readers and
      // deployment/tests must wait until publication has finished. Manifest last.
      for (const name of names) {
        checkInterrupted();
        await rename(join(publishStage, name), join(published, name));
      }
      await rm(publishStage, { recursive: true, force: true });
      publishStage = undefined;
      if (!sameFiles(expected, await artifacts(published))) throw new Error("Published simulation artifacts failed verification");
      checkInterrupted();
      temporaryReceipt = `${receiptPath}.${randomUUID()}.tmp`;
      await writeFile(temporaryReceipt, JSON.stringify({ schema: 1, key, input, files: expected }) + "\n", { flag: "wx" });
      checkInterrupted();
      await rename(temporaryReceipt, receiptPath);
      temporaryReceipt = undefined;
    });
  } finally {
    if (temporaryReceipt) await rm(temporaryReceipt, { force: true });
    if (publishStage) await rm(publishStage, { recursive: true, force: true });
    if (work) await rm(work, { recursive: true, force: true });
    await rmdir(lockPath);
  }
}

try { await main(); }
catch (error) {
  console.error(error);
  process.exitCode = interrupted === "SIGINT" ? 130 : interrupted === "SIGTERM" ? 143 : 1;
} finally {
  process.off("SIGINT", onSignal);
  process.off("SIGTERM", onSignal);
  if (interrupted) process.exitCode = interrupted === "SIGINT" ? 130 : 143;
}
