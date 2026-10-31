// Optional, bounded browser diagnostics. No commands, captures or private state.
export type DiagnosticEngine = {
  observation?: () => string;
  runtime_status?: () => string;
  renderer_status?: () => string;
  camera_status?: () => string;
  set_presentation_diagnostics?: (enabled: boolean) => void;
  presentation_diagnostics?: () => string;
};
type Fields = Record<string, unknown>;
type Input = Fields | (() => Fields);
type Selector = (value: unknown) => unknown;
type Event = Readonly<Fields>;
export type Diagnostics = {
  readonly enabled: boolean;
  readonly page_id: string | undefined;
  readonly dropped: number;
  readonly recent: readonly Event[];
  enable: (enabled: boolean) => void;
  record: (event: string, input?: Input) => void;
  bindEngine: (api: DiagnosticEngine, ready: () => boolean) => void;
  setNativeCollection: (api: DiagnosticEngine, requested: boolean) => void;
  observeNative: (api: DiagnosticEngine, input?: Input) => void;
  snapshot: (api: DiagnosticEngine, page?: string) => Fields;
  onEnabledChange: (listener: (enabled: boolean) => void) => () => void;
};
declare global {
  var __wsorbDiagnostics: Diagnostics | undefined;
}

const LIMIT = 200;
const PER_SECOND = 5;
const COLLECTION_LEASE_MS = 2000;
const REPORT_LIMIT = 1024 * 1024;
const events = new Set([
  "engine.ready", "engine.sample", "engine.failed", "local.attach", "local.ready", "local.status",
  "local.lease_conflict", "local.received", "local.invoked", "local.ack", "local.native_rejection",
  "local.result_sent", "local.result_ack", "local.indeterminate", "local.no_retry", "local.request_failed",
  "room.connect", "room.status", "room.welcome", "room.claim", "room.owner", "room.conflict",
  "room.duplicate", "room.received", "room.invoked", "room.ack", "room.native_rejection",
  "room.result_sent", "room.indeterminate", "room.no_retry",
]);
const number: Selector = value => typeof value === "number" && Number.isFinite(value) ? value : undefined;
const boolean: Selector = value => typeof value === "boolean" ? value : undefined;
const uuid: Selector = value => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value) ? value : undefined;
const oneOf = (...values: string[]): Selector => value => typeof value === "string" && values.includes(value) ? value : undefined;
const mode = oneOf("orbit", "first_person", "third_person", "free", "overview", "ship_top", "ship_bottom");
const stage = oneOf("initializing", "preparing_view", "drawing_world", "presenting", "waiting_for_gpu", "ready", "failed");
const entity: Selector = value => typeof value === "string" && /^(Shuttle|Target|LandingPad|Phobos|Mars|L1|L2|Debris[0-9]{1,10})$/.test(value) ? value : undefined;
const method = oneOf("snapshot", "command", "raycast", "pan_tilt", "capture", "preview");
const op = oneOf("pause", "play", "step", "reset", "set_command", "set_leased_command", "claim_control", "renew_control", "release_control", "set_sensor_fault", "clear_sensor_faults", "stop", "view", "debug_upright", "configure_experiment", "configure_flight", "place_lander", "move_launchpad", "tune_ship", "tune_debris_flow");
const code = oneOf("stale_context", "command_expired", "sensor_unavailable", "sensor_no_sample");

// Own data properties only. Neither disabled logging nor allowlisting invokes
// caller getters, toJSON(), iterators or arbitrary string conversion.
function field(value: unknown, key: string): unknown {
  if (value === null || typeof value !== "object") return undefined;
  return Object.getOwnPropertyDescriptor(value, key)?.value;
}
function select(value: unknown, schema: Record<string, Selector>): Fields {
  const output: Fields = {};
  for (const [key, selector] of Object.entries(schema)) {
    const result = selector(field(value, key));
    if (result !== undefined) output[key] = result;
  }
  return Object.freeze(output);
}
const vector = (length: number): Selector => value => {
  if (!Array.isArray(value) || value.length !== length) return undefined;
  const result = Array.from({ length }, (_, index) => number(field(value, String(index))));
  return result.every(value => value !== undefined) ? Object.freeze(result) : undefined;
};
const object = (schema: Record<string, Selector>): Selector => value => {
  if (value === null) return null;
  if (typeof value !== "object" || Array.isArray(value)) return undefined;
  return select(value, schema);
};
const nullable = (selector: Selector): Selector => value => value === null ? null : selector(value);
const frame = oneOf("Phobos-centered Mars–Phobos circular rotating frame; +X toward Mars, +Y orbit normal, +Z right-handed; SI units");
const pose = {
  source_time_s: number, frame, position_world_m: vector(3), rotation_body_to_world_xyzw: vector(4),
  velocity_world_mps: vector(3), angular_velocity_world_rad_s: vector(3),
};
const camera = object({
  mode, focus: entity, position_m: vector(3), rotation: vector(4), distance_m: number,
  active: boolean, locked: boolean, speed_mps: number,
});
const renderer = object({
  started: boolean, ready: boolean, stage, rendered_frames: number, submitted_primary_frames: number,
  completed_primary_frame: boolean, primary_world_draws: number, primary_planetary_draw_mask: number,
  primary_viewport: nullable(vector(2)), primary_target_size: nullable(vector(2)), compiled_pipelines: number,
  pending_pipelines: number, surface_maps_ready: boolean, surface_maps_gpu_ready: boolean,
});
const observation = object({
  time_s: number, trial_revision: number, tuning_revision: number, manual_interventions: number,
  shuttle: object({ position: vector(3), velocity: vector(3), rotation: vector(4), angular_velocity: vector(3) }),
  applied_physics: object(pose),
});
const bodyPose = object({ position_world_m: vector(3), rotation_body_to_world_xyzw: vector(4), velocity_world_mps: vector(3) });
const columns: Selector = value => {
  if (!Array.isArray(value) || value.length !== 3) return undefined;
  const result = [0, 1, 2].map(index => vector(3)(field(value, String(index))));
  return result.every(value => value !== undefined) ? Object.freeze(result) : undefined;
};
const visual = object({
  state: oneOf("missing", "observed", "ambiguous"), matching_entities: number,
  transforms: object({
    has_parent: boolean,
    local_transform: object({ translation_parent_m: vector(3), rotation_parent_from_local_xyzw: vector(4), scale: vector(3) }),
    global_transform: object({ translation_world_m: vector(3), linear_world_from_local_columns: columns }),
  }),
});
const presentation = object({
  schema: oneOf("wsorb.presentation_diagnostics.v1"), enabled: boolean, available: boolean,
  state: oneOf("disabled", "waiting_for_scene", "observed"),
  measurement: oneOf("bevy_main_world_transforms_not_gpu_pixels"),
  sample: object({
    collection: object({ stage: oneOf("post_update_after_transform_propagation"), bevy_frame: nullable(number), real_elapsed_s: nullable(number) }),
    simulation_time_basis: oneOf("simulation_elapsed_seconds"),
    scene_snapshot_clock: oneOf("not_exposed_by_scene_api", "scene_cache_snapshot", "missing_scene_cache"),
    scene_snapshot_source_time_s: nullable(number),
    scene_snapshot: object({ source_time_s: number, source_seed: number, tuning_revision: number,
      frame, lander: bodyPose, landing_pad: bodyPose }),
    published_snapshot: object({ source_time_s: number, source_seed: number, trial_revision: number, tuning_revision: number,
      frame, lander: bodyPose, landing_pad: bodyPose }),
    native_lander: object({ state: oneOf("observed", "unavailable"), source_time_s: number, tuning_revision: number, frame, pose: bodyPose }),
    lander_visual: visual, landing_pad_visual: visual,
    position_differences: object({
      lander_global_minus_published_m: nullable(vector(3)), landing_pad_global_minus_published_m: nullable(vector(3)),
      lander_global_minus_native_m: nullable(vector(3)), lander_native_minus_published_m: nullable(vector(3)),
    }),
  }),
});
const metadata = {
  page_id: uuid, local_page_id: uuid, id: uuid, request_id: uuid, trace_id: uuid, client_id: uuid, host_id: uuid,
  method, op, code, diagnostics: boolean, ok: boolean, owner: boolean, duplicate: boolean,
  command_invoked: boolean, engine_failed: boolean, no_retry: boolean, http_status: number,
  source_time_s: number, lease_sim_s: number, deadline_ms: number,
  hidden: boolean, focused: boolean, visibility: oneOf("visible", "hidden", "prerender"), startup_stage: stage,
  status: oneOf("waiting_for_engine", "connecting", "claiming", "connected", "disconnected", "conflict", "error", "indeterminate", "ready"),
  observation, camera, renderer, presentation, runtime: object({ running: boolean, rate: number }),
  camera_available: boolean, camera_status_available: boolean, presentation_diagnostics_available: boolean, native_collection_available: boolean,
  diagnostics_helper_available: boolean,
};

function read(api: DiagnosticEngine, name: keyof DiagnosticEngine, ready: () => boolean = () => true): unknown {
  try {
    if (!ready()) return undefined;
    const getter = api[name];
    if (typeof getter !== "function") return undefined;
    const json = (getter as () => string)();
    if (typeof json !== "string" || json.length > REPORT_LIMIT) return undefined;
    return JSON.parse(json);
  } catch { return undefined; }
}
const nativeEnabled = new WeakMap<DiagnosticEngine, boolean>();
function collect(api: DiagnosticEngine, enabled: boolean, force = false) {
  try {
    if (typeof api.set_presentation_diagnostics !== "function" || !force && nativeEnabled.get(api) === enabled) return;
    api.set_presentation_diagnostics(enabled);
    nativeEnabled.set(api, enabled);
  } catch { /* Diagnostics must not affect commands or readiness. */ }
}
function newPageId(): string | undefined {
  try { return crypto.randomUUID(); } catch { return undefined; }
}
const peerPageId = newPageId();
export function diagnosticPageId(): string | undefined {
  try { return globalThis.__wsorbDiagnostics?.page_id ?? peerPageId; }
  catch { return peerPageId; }
}

// Explicit RPC opt-in, independent of whether console logging is enabled.
// Missing old exports stay missing. Never synthesize a native renderer report.
export function snapshotDiagnostics(api: DiagnosticEngine, page?: string, ready: () => boolean = () => true): Fields {
  try {
    const actualCamera = read(api, "camera_status", ready);
    const actualPresentation = read(api, "presentation_diagnostics", ready);
    const actualRenderer = read(api, "renderer_status", ready);
    return select({
      page_id: page, camera: actualCamera, presentation: actualPresentation,
      camera_available: actualCamera !== undefined && field(actualRenderer, "started") === true,
      camera_status_available: actualCamera !== undefined,
      presentation_diagnostics_available: actualPresentation !== undefined,
      native_collection_available: typeof api.set_presentation_diagnostics === "function",
      diagnostics_helper_available: true,
    }, metadata);
  } catch { return Object.freeze({}); }
}

export function setNativeDiagnostics(api: DiagnosticEngine, requested: boolean) {
  try {
    if (globalThis.__wsorbDiagnostics) globalThis.__wsorbDiagnostics.setNativeCollection(api, requested === true);
    else collect(api, requested === true);
  } catch { /* An optional diagnostic hook cannot change RPC classification. */ }
}

export function recordDiagnostic(event: string, input?: Input) {
  try { globalThis.__wsorbDiagnostics?.record(event, input); }
  catch { /* A replaced or broken log sink cannot interrupt flight control. */ }
}

export function diagnosticsEnabled(): boolean {
  try { return globalThis.__wsorbDiagnostics?.enabled === true; } catch { return false; }
}

export function createDiagnostics(initiallyEnabled = false): Diagnostics {
  const pageId = newPageId();
  const recent: Event[] = [];
  const listeners = new Set<(enabled: boolean) => void>();
  const nativeTargets = new Set<DiagnosticEngine>();
  const collectionLeases = new Map<DiagnosticEngine, { expires: number; timer: ReturnType<typeof setTimeout> }>();
  let enabled = initiallyEnabled === true;
  let dropped = 0;
  let windowStart = 0;
  let count = 0;
  let bound: { api: DiagnosticEngine; ready: () => boolean } | undefined;
  const ready = () => { try { return !bound || bound.ready() === true; } catch { return false; } };
  const canRead = (api: DiagnosticEngine) => bound?.api !== api || ready();
  const helper: Diagnostics = Object.freeze({
    get enabled() { return enabled; },
    get page_id() { return pageId; },
    get dropped() { return dropped; },
    get recent() { return Object.freeze(recent.slice()); },
    enable(value: boolean) {
      enabled = value === true;
      if (!enabled) {
        for (const lease of collectionLeases.values()) clearTimeout(lease.timer);
        collectionLeases.clear();
      }
      for (const api of nativeTargets) {
        if (bound?.api !== api || ready()) collect(api, enabled, true);
      }
      for (const listener of listeners) {
        try { listener(enabled); } catch { /* Optional observers cannot interrupt the page. */ }
      }
    },
    record(event: string, input?: Input) {
      if (!enabled) return;
      try {
        if (!events.has(event)) return;
        const now = performance.now();
        if (now - windowStart >= 1000) { windowStart = now; count = 0; }
        if (count >= PER_SECOND) { dropped++; return; }
        count++;
        const data = select(typeof input === "function" ? input() : input, metadata);
        const entry = Object.freeze({ event, at_ms: now, document_id: pageId, dropped, ...data });
        if (recent.length === LIMIT) recent.shift();
        recent.push(entry);
        try { console.debug("[wsorb diagnostics]", entry); } catch { /* Retain the in-memory record. */ }
      } catch { dropped++; }
    },
    bindEngine(api: DiagnosticEngine, isReady: () => boolean) {
      if (bound) return;
      try {
        if (isReady() !== true) return;
        bound = { api, ready: isReady };
        nativeTargets.add(api);
        collect(api, enabled);
      } catch { /* Never initialize or restart an engine for diagnostics. */ }
    },
    setNativeCollection(api: DiagnosticEngine, requested: boolean) {
      if (bound?.api === api && !ready()) return;
      if (nativeTargets.size < 4) nativeTargets.add(api);
      if (!nativeTargets.has(api)) return;
      if (requested === true) {
        const previous = collectionLeases.get(api);
        if (previous) clearTimeout(previous.timer);
        const expires = performance.now() + COLLECTION_LEASE_MS;
        const timer = setTimeout(() => {
          if (collectionLeases.get(api)?.expires !== expires) return;
          collectionLeases.delete(api);
          if (canRead(api)) collect(api, enabled);
        }, COLLECTION_LEASE_MS);
        collectionLeases.set(api, { expires, timer });
      }
      // A one-shot snapshot cannot observe a PostUpdate sample in the same JS
      // call. Keep collection briefly active across unrelated ordinary RPCs,
      // then expire it. Explicit disable above clears it immediately.
      collect(api, enabled || (collectionLeases.get(api)?.expires ?? 0) > performance.now());
    },
    observeNative(api: DiagnosticEngine, input?: Input) {
      if (!enabled) return;
      if (!canRead(api)) return;
      helper.record("engine.sample", () => ({
        ...select(typeof input === "function" ? input() : input, metadata),
        observation: read(api, "observation", () => canRead(api)), runtime: read(api, "runtime_status", () => canRead(api)),
        renderer: read(api, "renderer_status", () => canRead(api)), ...snapshotDiagnostics(api, undefined, () => canRead(api)),
      }));
    },
    snapshot(api: DiagnosticEngine, page?: string) { return snapshotDiagnostics(api, page, () => canRead(api)); },
    onEnabledChange(listener: (enabled: boolean) => void) {
      if (listeners.size < 8) listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  });
  return helper;
}

export function installDiagnostics(): Diagnostics {
  if (globalThis.__wsorbDiagnostics) return globalThis.__wsorbDiagnostics;
  let enabled = false;
  try { enabled = new URLSearchParams(location.search).get("diagnostics") === "1"; } catch { /* Non-browser peer. */ }
  const helper = createDiagnostics(enabled);
  globalThis.__wsorbDiagnostics = helper;
  return helper;
}
