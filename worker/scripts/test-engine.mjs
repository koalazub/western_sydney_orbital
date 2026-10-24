// Actual compiled physics for the existing Python integration tests.
// No renderer, DOM, SDK or sensor/physics substitute is installed.
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

const api = await import("../public/sim/wsorb_bevy.js");
await api.default({ module_or_path: readFileSync(new URL("../public/sim/wsorb_bevy_bg.wasm", import.meta.url)) });

for await (const line of createInterface({ input: process.stdin })) {
  try {
    const request = JSON.parse(line);
    let value;
    if (request.method === "observation") value = JSON.parse(api.observation());
    else if (request.method === "navigation") value = JSON.parse(api.navigation_status());
    else if (request.method === "preview") {
      value = JSON.parse(api.set_navigation_preview(JSON.stringify(request.preview)));
    } else if (request.method === "command") {
      const result = JSON.parse(api.command(JSON.stringify(request.command)));
      if (!result.ok) throw new Error(result.error);
      value = { ok: true };
    } else throw new Error("Unsupported test driver request");
    console.log(JSON.stringify({ value }));
  } catch (error) {
    console.log(JSON.stringify({ error: error.message }));
  }
}
