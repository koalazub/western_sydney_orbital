// Real Room WebSocket peer for the API tests. This uses the shipped WASM's
// native schedules, not a browser/GPU substitute or a mock RPC responder.
import { readFile, realpath } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createHeadless } from "../worker/scripts/headless.mjs";
import { connectRoom } from "../worker/web/room-control.ts";

const connection = JSON.parse(await readFile(process.argv[2], "utf8"));
const origin = new URL(connection.url);
if (origin.protocol !== "http:" || origin.hostname !== "127.0.0.1" || !origin.port
    || origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash) {
  throw new Error("Room integration tests require their own loopback Worker, never production");
}
const publicRoot = resolve(import.meta.dirname, "../worker/public");
const engineBuild = JSON.parse(await readFile(resolve(publicRoot, "sim/manifest.json"), "utf8"));
const native = await createHeadless({ publicRoot, rootRealPath: await realpath(publicRoot), engineBuild });
const moduleUrl = pathToFileURL(resolve(publicRoot, "sim/wsorb_bevy.js"));
moduleUrl.searchParams.set("v", engineBuild.sha256);
// createHeadless initialized this exact module. Never initialize it twice.
const api = await import(moduleUrl.href);
const report = state => console.log(JSON.stringify({ state, backend: "native_headless", browser_gpu_verified: false }));
const owner = connectRoom(
  { origin: connection.url, id: connection.room_id, key: connection.token },
  api,
  engineBuild,
  state => {
    if (state === "connected") report("owner_connected");
    if (["error", "conflict", "indeterminate", "disconnected"].includes(state)) {
      native.close();
      report(state === "conflict" ? "host_conflict" : state);
    }
  },
  // This peer truthfully gates on native physics, not invented browser/GPU
  // readiness. Its snapshot still reports the renderer and cameras unstarted.
  () => native.status().physics_ready === true && process.env.WSORB_ROOM_READ_ONLY !== "1",
  connection.url,
  () => native.status().physics_ready === true,
);
process.once("SIGTERM", () => { native.close(); owner.disconnect(); setTimeout(() => process.exit(), 200).unref(); });
