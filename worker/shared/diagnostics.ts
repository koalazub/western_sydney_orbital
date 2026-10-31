// Callers opt in through authenticated requests or the server startup flag.
// Keep formatting here so diagnostics stay separate from flight code.
type Component = "local_relay" | "room_relay";
type Event = "call_received" | "call_dispatched" | "call_finished" | "call_rejected" | "call_timeout"
  | "page_claimed" | "page_conflict" | "page_expired" | "host_claimed" | "host_conflict" | "host_released"
  | "asset_finished";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const events = new Set<string>(["call_received", "call_dispatched", "call_finished", "call_rejected", "call_timeout",
  "page_claimed", "page_conflict", "page_expired", "host_claimed", "host_conflict", "host_released", "asset_finished"]);
const assets = new Set(["index", "app", "styles", "engine_manifest", "engine_glue", "engine_wasm", "local_bootstrap", "local_client"]);
const methods = new Set(["snapshot", "command", "capture", "preview", "raycast", "pan_tilt"]);
const operations = new Set(["pause", "play", "step", "reset", "set_command", "set_leased_command", "claim_control",
  "renew_control", "release_control", "set_sensor_fault", "clear_sensor_faults", "stop", "view", "debug_upright",
  "configure_experiment", "configure_flight", "place_lander", "move_launchpad", "tune_ship", "tune_debris_flow"]);
const codes = new Set(["local_busy", "page_mismatch", "stale_context", "command_expired", "sensor_unavailable", "sensor_no_sample"]);
const reasons = new Set(["owner_conflict", "no_owner", "in_flight", "invalid_request", "client_disconnected",
  "page_lease_expired", "call_deadline_expired", "native_rejection", "transport_failure", "host_released"]);
const limits = new Map<Component, { second: number; count: number; omitted: number }>();

export function validTraceId(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

export function diagnosticRecord(component: Component, event: Event, fields: Record<string, unknown>) {
  if (!["local_relay", "room_relay"].includes(component) || !events.has(event)) return undefined;
  const record: Record<string, unknown> = { schema: "wsorb.diagnostics.v1", component, event, wall_time_ms: Date.now() };
  for (const name of ["trace_id", "job_id", "page_id", "owner_page_id", "requester_page_id"]) {
    if (validTraceId(fields[name])) record[name] = fields[name];
  }
  if (typeof fields.method === "string" && methods.has(fields.method)) record.method = fields.method;
  if (typeof fields.asset === "string" && assets.has(fields.asset)) record.asset = fields.asset;
  if (fields.http_method === "GET" || fields.http_method === "HEAD") record.http_method = fields.http_method;
  if (typeof fields.op === "string" && operations.has(fields.op)) record.op = fields.op;
  if (fields.channel === "control" || fields.channel === "capture") record.channel = fields.channel;
  if (typeof fields.code === "string" && codes.has(fields.code)) record.code = fields.code;
  if (typeof fields.reason === "string" && reasons.has(fields.reason)) record.reason = fields.reason;
  for (const name of ["ok", "delivered"]) if (typeof fields[name] === "boolean") record[name] = fields[name];
  for (const name of ["http_status", "elapsed_ms", "deadline_ms", "source_time_s", "lease_sim_s", "epoch", "term", "bytes"]) {
    const value = fields[name];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) record[name] = value;
  }
  return record;
}

export function emitDiagnostic(enabled: boolean, component: Component, event: Event, fields: Record<string, unknown>): void {
  if (!enabled) return;
  try {
    const record = diagnosticRecord(component, event, fields);
    if (!record) return;
    const second = Math.floor(Date.now() / 1000);
    let limit = limits.get(component);
    if (!limit) { limit = { second, count: 0, omitted: 0 }; limits.set(component, limit); }
    if (limit.second !== second) { limit.second = second; limit.count = 0; }
    if (limit.count >= 10) { limit.omitted += 1; return; }
    limit.count += 1;
    if (limit.omitted) { record.omitted_events = limit.omitted; limit.omitted = 0; }
    // No raw body, URL, headers, native error strings, pixels or debris rows.
    console.debug(JSON.stringify(record));
  } catch { /* Logging must never alter delivery, ownership or command outcomes. */ }
}
