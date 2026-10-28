// Actual compiled local physics for script experiments and integration tests.
// No renderer, DOM, SDK or sensor/physics substitute is installed.
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";
// Parse the real bridge without invoking it or installing a substitute DOM.
import "../web/local-api.js";

const api = await import("../public/sim/wsorb_bevy.js");
await api.default({ module_or_path: readFileSync(new URL("../public/sim/wsorb_bevy_bg.wasm", import.meta.url)) });

for await (const line of createInterface({ input: process.stdin })) {
  try {
    const request = JSON.parse(line);
    let value;
    if (request.method === "observation") value = JSON.parse(api.observation());
    else if (request.method === "driver_metadata") value = { node_version: process.version };
    else if (request.method === "step") {
      function control(command) {
        const result = JSON.parse(api.control(JSON.stringify(command)));
        if (!result.ok) throw new Error(result.error);
      }
      // This isolated engine starts/configures paused and never plays. Do not
      // enqueue native Pause actions: no renderer runs to drain that queue.
      // Reject a running engine before taking command ownership.
      if (JSON.parse(api.runtime_status()).running) throw new Error("Pause the mission before stepping manually");
      try {
        // Isolated synchronous manual stepping owns no live controller lease.
        // SetCommand cancels any old lease; Step preserves this legacy force.
        control({ op: "set_command", accel: request.accel, angular_accel: request.angular_accel });
        control({ op: "step", seconds: request.seconds });
        value = JSON.parse(api.observation());
      } finally {
        control({ op: "stop" });
      }
    }
    else if (request.method === "debug_snapshot") value = JSON.parse(api.command('{"op":"observe"}')).state;
    else if (request.method === "navigation") value = JSON.parse(api.navigation_status());
    else if (request.method === "cameras") value = JSON.parse(api.ship_camera_status());
    else if (request.method === "pan_tilt") value = JSON.parse(api.pan_tilt_camera(request.camera, request.yaw, request.pitch));
    else if (request.method === "bindings") value = JSON.parse(api.key_bindings_status());
    else if (request.method === "set_bindings") value = JSON.parse(api.set_key_bindings(JSON.stringify(request.settings)));
    else if (request.method === "raycast") value = JSON.parse(api.lookahead_raycast(JSON.stringify(request.query)));
    else if (request.method === "runtime") value = JSON.parse(api.runtime_status());
    else if (request.method === "telemetry") value = JSON.parse(api.telemetry_status());
    else if (request.method === "field_diagnostics") value = JSON.parse(api.field_diagnostics());
    else if (request.method === "control_result") value = JSON.parse(api.control(JSON.stringify(request.command)));
    else if (request.method === "capture_result") {
      try {
        const frame = await api.ship_camera_capture(request.camera);
        const { rgb, ...metadata } = frame;
        value = { ok: true, byte_length: rgb.length, metadata };
      } catch (error) {
        value = { ok: false, code: error.code ?? null, message: error.message ?? String(error) };
      }
    }
    else if (request.method === "renderer_status") value = JSON.parse(api.renderer_status());
    else if (request.method === "renderer_start_result") {
      try {
        api.start_simulation(request.selector ?? "#simulation-canvas");
        value = { ok: true };
      } catch (error) {
        value = { ok: false, code: error.code ?? null, message: error.message ?? String(error) };
      }
    }
    else if (request.method === "preview") {
      value = JSON.parse(api.set_navigation_preview(JSON.stringify(request.preview)));
    } else if (request.method === "command") {
      const result = JSON.parse(api.control(JSON.stringify(request.command)));
      if (!result.ok) throw new Error(result.error);
      value = { ok: true };
    } else throw new Error("Unsupported engine driver request");
    console.log(JSON.stringify({ value }));
  } catch (error) {
    console.log(JSON.stringify({ error: error.message }));
  }
}
