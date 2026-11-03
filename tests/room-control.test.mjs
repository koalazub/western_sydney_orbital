// Real built browser WASM and its existing native headless adapter only.
// These tests do not start a browser, renderer, GPU or substitute room server.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { readFile, realpath } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createHeadless } from "../worker/scripts/headless.mjs";
import { connectRoom, executeRoomCall } from "../worker/web/room-control.ts";
import { freshTrial } from "../worker/web/flight-launch.ts";
import { flightWorlds } from "../worker/web/flight-selection.ts";

test("room calls against the verified native WASM, with renderer unstarted", async t => {
  const publicRoot = fileURLToPath(new URL("../worker/public/", import.meta.url));
  const build = JSON.parse(await readFile(new URL("../worker/public/sim/manifest.json", import.meta.url), "utf8"));
  const headless = await createHeadless({ publicRoot, rootRealPath: await realpath(publicRoot), engineBuild: build });
  const moduleUrl = new URL("../worker/public/sim/wsorb_bevy.js", import.meta.url);
  moduleUrl.searchParams.set("v", build.sha256);
  // Import the exact instance initialized and verified by createHeadless.
  const api = await import(moduleUrl.href);
  const envelope = (method, fields = {}) => ({ version: 1, method, deadline_ms: Date.now() + 2500, ...fields });
  const invoke = (method, fields = {}) => executeRoomCall(api, build, envelope(method, fields));
  const snapshot = () => {
    const result = invoke("snapshot");
    assert.equal(result.ok, true, result.error);
    return result.value;
  };
  const context = value => ({
    trial_revision: value.observation.trial_revision,
    tuning_revision: value.observation.tuning_revision,
    manual_interventions: value.observation.manual_interventions,
    seed: value.observation.seed,
    running: value.runtime.running,
  });
  const claim = (value, run_id = "room-test") => ({
    op: "claim_control", run_id, controller_id: "native-primary",
    expected_epoch: value.observation.control_authority.epoch,
    expected_term: value.observation.control_authority.term,
    source_time_s: value.observation.time_s, lease_sim_s: 1, expected_context: context(value),
  });
  const release = (value, run_id = "room-test") => ({
    op: "release_control", run_id, expected_epoch: value.observation.control_authority.epoch,
    expected_context: context(value),
  });
  const leased = value => {
    const authority = value.observation.control_authority;
    return {
      op: "set_leased_command", accel: [0, 0, 0.5], angular_accel: [0, 0, 0.05],
      source_time_s: value.observation.time_s, lease_sim_s: 0.1,
      expected_context: context(value), expected_sensor_revision: value.observation.sensor_health.revision,
      controller_fence: {
        epoch: authority.epoch, term: authority.term,
        run_id: authority.run_id ?? "room-test", controller_id: authority.controller_id ?? "native-primary",
      },
    };
  };
  const renewed = value => {
    const request = leased(value);
    return {
      op: "renew_control", controller_fence: request.controller_fence,
      source_time_s: request.source_time_s, lease_sim_s: 0.75,
      expected_context: request.expected_context, expected_sensor_revision: request.expected_sensor_revision,
    };
  };
  const preInvocation = call => {
    const result = executeRoomCall(api, build, call);
    assert.equal(result.ok, false);
    assert.match(result.error, /no command was invoked\./);
    assert.equal(Object.hasOwn(result, "code"), false, "A local check must not fabricate a native rejection code");
    assert.equal(Object.hasOwn(result, "value"), false);
    return result;
  };
  const nativeRejection = (command, code) => {
    const result = invoke("command", { command });
    assert.equal(result.ok, false);
    assert.equal(result.code, code);
    assert.match(result.error, /^Native engine rejected/);
    assert.doesNotMatch(result.error, /no command was invoked/);
    assert.equal(Object.hasOwn(result, "value"), false);
    return result;
  };

  try {
    await t.test("snapshot contains only actual public reports and the verified local build", () => {
      const result = snapshot();
      assert.deepEqual(Object.keys(result).sort(), ["cameras", "engine_build", "navigation", "observation", "renderer", "runtime", "telemetry"]);
      for (const [name, method] of Object.entries({
        observation: "observation", telemetry: "telemetry_status", navigation: "navigation_status",
        runtime: "runtime_status", renderer: "renderer_status", cameras: "ship_camera_status",
      })) assert.deepEqual(result[name], JSON.parse(api[method]()));
      assert.deepEqual(result.engine_build, build);
      assert.notEqual(result.engine_build, build);
      assert.equal(result.renderer.started, false);
      assert.equal(result.renderer.ready, false);
      assert.equal(result.cameras.configured, false);
      assert.deepEqual(result.observation.control_capabilities.ownership_renewal, { schema_version: 1, max_lease_sim_s: 0.75 });
      assert.deepEqual(result.cameras.views, []);
      for (const field of ["state", "assets", "rng_state", "sensor_state"]) {
        assert.equal(Object.hasOwn(result.observation, field), false, `${field} is not a public observation`);
      }
      assert.equal(headless.status().physics_ready, true);
    });

    await t.test("malformed, expired and excessive future calls fail before command invocation", () => {
      const before = snapshot();
      const valid = envelope("command", { command: claim(before) });
      for (const invalid of [
        null, [], {}, { ...valid, version: 2 }, { ...valid, version: true },
        { ...valid, deadline_ms: Date.now() - 1 }, { ...valid, deadline_ms: Date.now() },
        { ...valid, deadline_ms: Date.now() + 60_000 }, { ...valid, deadline_ms: 1.5 },
        { ...valid, deadline_ms: Infinity }, { ...valid, deadline_ms: "soon" },
        { ...valid, deadline_ms: Number.MAX_SAFE_INTEGER + 1 },
        { ...valid, extra: "untrusted" }, { ...valid, method: "capture" },
        { ...valid, method: "command", command: null },
        { ...envelope("snapshot"), command: { op: "reset" } },
      ]) preInvocation(invalid);
      for (const missing of ["version", "method", "deadline_ms", "command"]) {
        const invalid = structuredClone(valid);
        delete invalid[missing];
        preInvocation(invalid);
      }
      for (const method of ["capture", "pan_tilt", "preview", "raycast", "observe", "eval"]) {
        preInvocation(envelope(method));
      }
      assert.deepEqual(snapshot(), before);
    });

    await t.test("only exact checked command shapes, fences and bounded vectors are admitted", () => {
      const before = snapshot();
      const requests = [claim(before), release(before), leased(before), renewed(before), { op: "play", rate: 1, expected_context: context(before) }];
      for (const request of requests) {
        for (const missing of Object.keys(request)) {
          const invalid = structuredClone(request);
          delete invalid[missing];
          preInvocation(envelope("command", { command: invalid }));
        }
        preInvocation(envelope("command", { command: { ...request, extra: true } }));
        for (const missing of Object.keys(request.expected_context)) {
          const invalid = structuredClone(request);
          delete invalid.expected_context[missing];
          preInvocation(envelope("command", { command: invalid }));
        }
        for (const expected_context of [null, [], { ...context(before), extra: true }, { ...context(before), running: 1 },
          { ...context(before), seed: true }, { ...context(before), trial_revision: -1 },
          { ...context(before), tuning_revision: 2 ** 32 }, { ...context(before), manual_interventions: 0.5 }]) {
          preInvocation(envelope("command", { command: { ...request, expected_context } }));
        }
      }
      for (const op of ["observe", "reset", "pause", "step", "set_command", "stop", "tune_ship", "tune_debris_flow",
        "configure_flight", "configure_experiment", "place_lander", "move_launchpad", "set_sensor_fault", "clear_sensor_faults", "restore", "view", "debug_upright", "capture"]) {
        preInvocation(envelope("command", { command: { op } }));
      }
      for (const rate of [0, 5, 10, "1", true, null]) {
        preInvocation(envelope("command", { command: { op: "play", rate, expected_context: context(before) } }));
      }
      const bounded = leased(before);
      for (const [field, value] of [["lease_sim_s", 0.750001], ["lease_sim_s", 0.049],
        ["expected_sensor_revision", true], ["controller_fence", null], ["accel", [0, 0, 0]], ["angular_accel", [0, 0, 0]]]) {
        preInvocation(envelope("command", { command: { ...renewed(before), [field]: value } }));
      }
      for (const [field, value] of [
        ["accel", [0.4, 0.4, 0]], ["accel", [0, 0, 0.50001]], ["accel", [NaN, 0, 0]],
        ["accel", [Infinity, 0, 0]], ["accel", [false, 0, 0]], ["accel", ["0", 0, 0]], ["accel", [0, 0]],
        ["angular_accel", [0.04, 0.04, 0]], ["angular_accel", [0, 0, 0.05001]],
        ["source_time_s", -1], ["source_time_s", NaN], ["source_time_s", Infinity], ["source_time_s", true],
        ["lease_sim_s", 0.049], ["lease_sim_s", 1.001], ["lease_sim_s", true], ["lease_sim_s", Infinity],
        ["expected_sensor_revision", -1], ["expected_sensor_revision", 2 ** 32], ["expected_sensor_revision", 0.1],
        ["controller_fence", null], ["controller_fence", { ...bounded.controller_fence, extra: 0 }],
        ["controller_fence", { ...bounded.controller_fence, term: true }],
        ["controller_fence", { ...bounded.controller_fence, run_id: "../not-an-id" }],
      ]) preInvocation(envelope("command", { command: { ...bounded, [field]: value } }));
      for (const missing of Object.keys(bounded.controller_fence)) {
        const invalid = structuredClone(bounded);
        delete invalid.controller_fence[missing];
        preInvocation(envelope("command", { command: invalid }));
      }
      for (const [field, value] of [["run_id", "../bad"], ["controller_id", "a".repeat(65)], ["expected_epoch", -1], ["expected_term", 2 ** 32]]) {
        preInvocation(envelope("command", { command: { ...claim(before), [field]: value } }));
      }
      const disguised = Object.assign(Object.create({ toJSON: () => ({ op: "reset" }) }), claim(before));
      preInvocation(envelope("command", { command: disguised }));
      const disguisedVector = [0, 0, 0];
      Object.setPrototypeOf(disguisedVector, Object.assign(Object.create(Array.prototype), { toJSON: () => [10, 0, 0] }));
      preInvocation(envelope("command", { command: { ...bounded, accel: disguisedVector } }));
      const secret = randomBytes(32).toString("base64url");
      const redacted = preInvocation(envelope("command", { command: { op: secret, payload: secret.repeat(2000) } }));
      assert.equal(JSON.stringify(redacted).includes(secret), false);
      assert.deepEqual(snapshot(), before);
    });

    await t.test("malformed room descriptors open no socket and never expose credentials", () => {
      const before = snapshot();
      const key = randomBytes(32).toString("base64url");
      for (const connection of [
        { origin: "http://example.com", id: "abc", key },
        { origin: "https://example.com/other", id: "abc", key },
        { origin: `https://example.com/?key=${key}`, id: "abc", key },
        { origin: `https://user:${key}@example.com`, id: "abc", key },
        { origin: "https://example.com", id: "../escape", key },
        { origin: "https://example.com", id: "abc", key: `${key}/bad` },
      ]) {
        const reports = [];
        const owner = connectRoom(connection, api, build, (...value) => reports.push(value), () => JSON.parse(api.renderer_status()).ready);
        assert.equal(typeof owner.disconnect, "function");
        owner.disconnect();
        assert.equal(reports.length, 1);
        assert.equal(reports[0][0], "error");
        assert.equal(JSON.stringify(reports).includes(key), false);
      }
      assert.deepEqual(snapshot(), before);
    });

    await t.test("native stale_context stays a real rejection, distinct from local validation", () => {
      const before = snapshot();
      const request = claim(before);
      request.expected_context.trial_revision += 1;
      nativeRejection(request, "stale_context");
      assert.deepEqual(snapshot(), before);
      // This future source timestamp passes transport shape checks. Only native
      // simulation time can reject it, and its untrusted error text is redacted.
      const rejected = invoke("command", { command: { ...claim(before), source_time_s: before.observation.time_s + 100 } });
      assert.deepEqual(rejected, { ok: false, error: "Native engine rejected the room command." });
      assert.deepEqual(snapshot(), before);
    });

    let originalLease;
    await t.test("claims, fenced leases and releases use actual authority and ACK-only results", () => {
      const before = snapshot();
      const request = claim(before);
      const original = structuredClone(request);
      assert.deepEqual(invoke("command", { command: request }), { ok: true, value: { ok: true } });
      assert.deepEqual(request, original, "The executor must not relabel the source observation");
      const claimed = snapshot();
      assert.equal(claimed.observation.control_authority.status, "active");
      assert.equal(claimed.observation.control_authority.term, before.observation.control_authority.term + 1);
      nativeRejection(claim(claimed), "stale_context");
      const wrongSensor = leased(claimed);
      wrongSensor.expected_sensor_revision += 1;
      nativeRejection(wrongSensor, "stale_context");
      const wrongFence = leased(claimed);
      wrongFence.controller_fence.term += 1;
      nativeRejection(wrongFence, "stale_context");
      assert.deepEqual(snapshot(), claimed);
      const revoke = release(claimed);
      assert.deepEqual(invoke("command", { command: revoke }), { ok: true, value: { ok: true } });
      const revoked = snapshot();
      assert.equal(revoked.observation.control_authority.status, "revoked");
      nativeRejection(leased(claimed), "stale_context");
      const freshClaim = { ...claim(revoked), run_id: "room-second-run" };
      assert.deepEqual(invoke("command", { command: freshClaim }), { ok: true, value: { ok: true } });
      const admitted = snapshot();
      originalLease = leased(admitted);
      const unchanged = structuredClone(originalLease);
      assert.deepEqual(invoke("command", { command: originalLease }), { ok: true, value: { ok: true } });
      assert.deepEqual(originalLease, unchanged);
      const applied = snapshot();
      assert.deepEqual(applied.observation.shuttle.commanded_accel_mps2, originalLease.accel);
      assert.deepEqual(applied.observation.shuttle.commanded_angular_accel_rad_s2, originalLease.angular_accel);
      assert.equal(applied.observation.command_watchdog.source_time_s, originalLease.source_time_s);
      assert.equal(applied.observation.command_watchdog.expires_at_sim_time_s, originalLease.source_time_s + originalLease.lease_sim_s);
    });

    await t.test("Play is a checked rate-1 ACK and native expiry wins despite a fresh wall deadline", async () => {
      const before = snapshot();
      assert.equal(before.runtime.running, false);
      assert.deepEqual(invoke("command", { command: { op: "play", rate: 1, expected_context: context(before) } }), { ok: true, value: { ok: true } });
      assert.equal(snapshot().runtime.running, false, "A queued native ACK is not an applied playback report");
      // Real elapsed wall time drives the existing scheduler, not a synthetic
      // renderer or a replacement step command. This is not a full-flight run.
      await delay(350);
      assert.equal(headless.status().physics_ready, true);
      const expired = snapshot();
      assert.equal(expired.runtime.running, true);
      assert.ok(expired.observation.time_s > originalLease.source_time_s + originalLease.lease_sim_s);
      assert.equal(expired.observation.command_watchdog.status, "expired");
      assert.deepEqual(expired.observation.shuttle.commanded_accel_mps2, [0, 0, 0]);
      assert.deepEqual(expired.observation.shuttle.commanded_angular_accel_rad_s2, [0, 0, 0]);
      nativeRejection({ ...originalLease, expected_context: context(expired) }, "command_expired");
      assert.deepEqual(snapshot(), expired);
      assert.equal(expired.renderer.started, false);
      assert.equal(expired.renderer.ready, false);
      assert.equal(expired.cameras.configured, false);
    });

    await t.test("ownership renewal forwards its actual native receipt without installing or extending actuation", () => {
      // Reset only this test-owned native WASM after the existing claim/Play
      // regression has completed. Reset must establish the real new trial;
      // changing an old request's epoch or playback context would not isolate it.
      const previous = snapshot();
      assert.deepEqual(JSON.parse(api.control(JSON.stringify({ op: "reset" }))), { ok: true });
      assert.equal(headless.status().physics_ready, true);
      const before = snapshot();
      assert.equal(before.runtime.running, false);
      assert.equal(before.observation.time_s, 0);
      assert.equal(before.observation.seed, previous.observation.seed);
      assert.equal(before.observation.trial_revision, previous.observation.trial_revision + 1);
      assert.deepEqual(before.observation.experiment_settings, previous.observation.experiment_settings);
      const priorAuthority = previous.observation.control_authority;
      const resetAuthority = before.observation.control_authority;
      if (["active", "expired"].includes(priorAuthority.status)) {
        assert.equal(resetAuthority.status, "revoked");
        assert.equal(resetAuthority.epoch, priorAuthority.epoch + 1);
        assert.equal(resetAuthority.term, priorAuthority.term);
        assert.equal(resetAuthority.run_id, priorAuthority.run_id);
        assert.equal(resetAuthority.controller_id, null);
        assert.equal(resetAuthority.expires_at_sim_time_s, null);
      } else {
        assert.deepEqual(resetAuthority, priorAuthority);
      }
      assert.equal(before.observation.command_watchdog.status, "idle");
      // This is a new explicit cohort for the reset trial. All context and
      // expected-counter fields still come directly from its native observation.
      const run = "ownership-renewal-test";
      assert.notEqual(run, before.observation.control_authority.run_id);
      assert.equal(invoke("command", { command: { ...claim(before, run), lease_sim_s: 0.1 } }).ok, true);
      const granted = snapshot();
      assert.equal(invoke("command", { command: { ...leased(granted), lease_sim_s: 0.05 } }).ok, true);
      const source = snapshot();
      const request = renewed(source);
      const response = invoke("command", { command: request });
      assert.equal(response.ok, true, response.error);
      const after = snapshot();
      assert.deepEqual(response, { ok: true, value: { ok: true, ownership_renewal: {
        source_time_s: source.observation.time_s, lease_sim_s: 0.75,
        sensor_revision: source.observation.sensor_health.revision,
        control_authority: after.observation.control_authority,
      } } });
      assert.equal(after.observation.control_authority.run_id, run);
      assert.equal(after.observation.control_authority.expires_at_sim_time_s, request.source_time_s + 0.75);
      assert.equal(after.observation.control_authority.term, source.observation.control_authority.term);
      assert.deepEqual(after.observation.command_watchdog, source.observation.command_watchdog);
      assert.deepEqual(after.observation.shuttle, source.observation.shuttle);
      assert.deepEqual(after.observation.applied_physics, source.observation.applied_physics);
      assert.equal(after.observation.time_s, source.observation.time_s);
      assert.deepEqual(after.runtime, source.runtime);
      nativeRejection(request, "stale_context");
      assert.deepEqual(snapshot(), after);
      assert.deepEqual(invoke("command", { command: release(after, run) }), { ok: true, value: { ok: true } });
    });
    await t.test("launcher prepares the chosen real world and seed without playing or claiming", async () => {
      for (const world of flightWorlds) {
        const seed = world.id === "team" ? 0 : world.id === "official" ? 101 : 0xffffffff;
        const selected = { profileId: "basic_pd_radar", worldId: world.id, seed, duration: 300 };
        let consent;
        try {
          consent = await freshTrial(api, selected, new AbortController().signal);
        } catch (error) {
          assert.fail(`Launcher world ${world.id}, seed ${selected.seed}: ${error.message}`);
        }
        const actual = snapshot();
        assert.equal(actual.observation.flight_preset, world.preset);
        const expectedNativeSettings = structuredClone(world.settings);
        if (expectedNativeSettings.debris.distribution.kind === "directed_stream") {
          delete expectedNativeSettings.debris.distribution;
        }
        assert.deepEqual(actual.observation.experiment_settings, expectedNativeSettings, `Native serialized setup for ${world.id}`);
        assert.equal(actual.observation.seed, seed);
        assert.equal(actual.observation.time_s, 0);
        assert.equal(actual.runtime.running, false);
        assert.notEqual(actual.observation.control_authority.status, "active");
        assert.deepEqual(consent, context(actual));
      }
      const before = snapshot();
      const selection = { profileId: "basic_pd_radar", worldId: "official", seed: 101, duration: 300 };
      await assert.rejects(freshTrial(api, selection, AbortSignal.abort()), { name: "AbortError" });
      assert.deepEqual(snapshot(), before, "Cancelled startup cannot reset a flight");
      await assert.rejects(freshTrial(api, { ...selection, worldId: "unknown" }, new AbortController().signal), /supported flight world/);
      assert.deepEqual(snapshot(), before, "Unknown world cannot fall back to L2");
      const active = invoke("command", { command: claim(before, "launcher-blocked") });
      assert.equal(active.ok, true, active.error);
      const owned = snapshot();
      await assert.rejects(freshTrial(api, selection, new AbortController().signal), /Stop the active controller/);
      assert.deepEqual(snapshot(), owned, "Fresh setup cannot replace an active controller");
      assert.equal(invoke("command", { command: release(owned, "launcher-blocked") }).ok, true);
    });
  } finally {
    // Stop only this test's scheduler. Never issue native pause/reset/release on
    // connection loss, and never touch an existing browser or local lab process.
    headless.close();
  }
});
