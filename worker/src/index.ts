import { DurableObject } from "cloudflare:workers";
import * as Y from "yjs";
import { emitDiagnostic } from "../shared/diagnostics.ts";
import { CONTROLLER_BODY_LIMIT, ControllerJobsError, controllerJobsAction, controllerJobsView, createControllerJobsState } from "../shared/controller-jobs.ts";
import type { ControllerJobsState } from "../shared/controller-jobs.ts";

const MAX_BODY = 64 * 1024;
const MAX_UPDATE = 256 * 1024;
const MAX_SNAPSHOT = 512 * 1024;
const MAX_CONNECTIONS = 32;
const PAGE_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const NATIVE_REJECTION_CODES = new Set(["stale_context", "command_expired", "sensor_unavailable", "sensor_no_sample"]);
const ROOM_TTL = 7 * 24 * 60 * 60 * 1000;
const headers = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };
const json = (data: unknown, status = 200) => Response.json(data, { status, headers });
async function readBoundedJson(request: Request, limit: number): Promise<unknown> {
  if (!request.body) throw new Error("Invalid JSON");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) { await reader.cancel(); throw new RangeError("Request too large"); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder().decode(bytes));
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
function bytesToBase64Url(bytes: Uint8Array): string {
  return bytesToBase64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function base64ToBytes(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length > MAX_UPDATE * 2) throw new Error("Invalid update");
  const binary = atob(value);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}
async function digest(value: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}
async function sameSecret(a: string, b: string): Promise<boolean> {
  const [left, right] = await Promise.all([digest(a), digest(b)]);
  return crypto.subtle.timingSafeEqual(left, right);
}
function validNotebookUrl(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048) return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && ["notebooklm.google.com", "docs.google.com"].includes(url.hostname);
  } catch { return false; }
}

export class Room extends DurableObject<Env> {
  private readonly commandWaiters = new Map<string, (result: unknown) => void>();
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      ctx.storage.sql.exec(`CREATE TABLE IF NOT EXISTS room (key_hash TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, document BLOB NOT NULL, state TEXT, notebook_url TEXT, host_id TEXT)`);
      ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, requester TEXT NOT NULL, created_at INTEGER NOT NULL)");
      ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS controller_jobs (id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL)");
    });
  }

  private row() {
    return this.ctx.storage.sql.exec<{ key_hash: string; created_at: number; updated_at: number; document: ArrayBuffer; state: string | null; notebook_url: string | null; host_id: string | null }>("SELECT * FROM room LIMIT 1").toArray()[0];
  }
  async initialize(key: string): Promise<void> {
    const hash = bytesToBase64(await digest(key));
    const now = Date.now();
    this.ctx.storage.sql.exec("INSERT INTO room (key_hash, created_at, updated_at, document) VALUES (?, ?, ?, ?)", hash, now, now, Y.encodeStateAsUpdate(new Y.Doc()));
    await this.ctx.storage.setAlarm(now + ROOM_TTL);
  }
  private async authorized(key: string): Promise<boolean> {
    const row = this.row();
    if (!row) return false;
    const hash = bytesToBase64(await digest(key));
    return sameSecret(hash, row.key_hash);
  }
  async state(key: string): Promise<Response> {
    if (!(await this.authorized(key))) return json({ error: "Unauthorized" }, 401);
    const row = this.row()!;
    return json({ document: bytesToBase64(new Uint8Array(row.document)), state: row.state ? JSON.parse(row.state) : null, notebookUrl: row.notebook_url });
  }
  async controllers(key: string, action?: unknown): Promise<Response> {
    if (!(await this.authorized(key))) return json({ error: "Unauthorized" }, 401);
    // Authorization may await, but this load/transition/save section may not.
    // SQLite persists jobs separately from the room document and saved state.
    const saved = this.ctx.storage.sql.exec<{ state: string }>("SELECT state FROM controller_jobs WHERE id = 1").toArray()[0];
    const state: ControllerJobsState = saved ? JSON.parse(saved.state) : createControllerJobsState();
    if (action === undefined) return json(controllerJobsView(state, Date.now()));
    const owner = this.row()!.host_id;
    try {
      const transition = controllerJobsAction(state, action, {
        now: Date.now(), jobId: crypto.randomUUID(), ownerPageId: owner,
        ownerAvailable: owner !== null && this.connectedOwner(owner),
      });
      this.ctx.storage.sql.exec("INSERT INTO controller_jobs (id, state) VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET state = excluded.state", JSON.stringify(transition.state));
      return json(transition.response);
    } catch (error) {
      if (error instanceof ControllerJobsError) return json({ error: error.message }, error.status);
      throw error;
    }
  }
  private connectedOwner(id: string): boolean {
    return this.ctx.getWebSockets().some((socket) => socket.readyState === WebSocket.OPEN && (socket.deserializeAttachment() as { clientId?: string } | null)?.clientId === id);
  }
  async command(key: string, id: string, command: Record<string, unknown>, diagnostics = false, targetPage?: string): Promise<Response> {
    if (!(await this.authorized(key))) return json({ error: "Unauthorized" }, 401);
    if (targetPage !== undefined && !PAGE_UUID.test(targetPage)) return json({ error: "X-WSORB-Target-Page must be a canonical UUID" }, 400);
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(id) || !command || typeof command !== "object" || Array.isArray(command)) return json({ error: "Invalid command" }, 400);
    const serialized = JSON.stringify(command);
    if (new TextEncoder().encode(serialized).byteLength > MAX_BODY) return json({ error: "Command too large" }, 413);
    const detail = command.command as Record<string, unknown> | undefined;
    const fence = detail?.controller_fence as Record<string, unknown> | undefined;
    const started = diagnostics ? Date.now() : undefined;
    emitDiagnostic(diagnostics, "room_relay", "call_received", {
      trace_id: id, method: command.method, op: detail?.op,
      source_time_s: detail?.source_time_s, lease_sim_s: detail?.lease_sim_s,
      epoch: fence?.epoch, term: fence?.term,
    });
    const row = this.row()!;
    if (targetPage !== undefined && targetPage !== row.host_id) return json({ error: "Target room owner changed", code: "page_mismatch" }, 409);
    if (!row.host_id || !this.connectedOwner(row.host_id)) {
      emitDiagnostic(diagnostics, "room_relay", "call_rejected", { trace_id: id, reason: "no_owner", http_status: 503 });
      return json({ error: "No connected room owner" }, 503);
    }
    this.expirePending();
    if (this.commandWaiters.size >= 64 || this.ctx.storage.sql.exec<{ count: number }>("SELECT COUNT(*) AS count FROM pending").one().count >= 64) return json({ error: "Too many pending commands" }, 429);
    if (this.ctx.storage.sql.exec("SELECT id FROM pending WHERE id = ?", id).toArray().length) return json({ error: "Command ID already pending" }, 409);
    const requester = `http:${crypto.randomUUID()}`;
    this.ctx.storage.sql.exec("INSERT INTO pending (id, requester, created_at) VALUES (?, ?, ?)", id, requester, Date.now());
    const result = new Promise<unknown>((resolve) => {
      let settled = false;
      const finish = (value: unknown) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.commandWaiters.delete(id);
        resolve(value);
      };
      const timer = setTimeout(() => {
        this.ctx.storage.sql.exec("DELETE FROM pending WHERE id = ?", id);
        finish({ error: "Room owner timed out", id });
      }, 30_000);
      this.commandWaiters.set(id, finish);
      emitDiagnostic(diagnostics, "room_relay", "call_dispatched", { trace_id: id, page_id: row.host_id, method: command.method, op: detail?.op });
      this.sendTo(row.host_id!, { type: "execute", id, command, requester, ...(diagnostics ? { diagnostics: true } : {}) });
      this.ctx.storage.setAlarm(Date.now() + 30_000).catch(() => finish({ error: "Unable to schedule command timeout", id }));
    });
    const value = await result;
    // A completed owner RPC can contain a native rejection. Its code and ID
    // are usable only inside the authenticated result envelope, not an HTTP
    // gateway error. Transport timeouts without a result remain HTTP 504.
    const reply = value as { type?: string; error?: string };
    const status = reply.type === "result" ? 200 : reply.error ? 504 : 200;
    const resultValue = value as { ok?: unknown; code?: unknown };
    emitDiagnostic(diagnostics, "room_relay", status === 504 ? "call_timeout" : "call_finished", {
      trace_id: id, page_id: row.host_id, method: command.method, op: detail?.op,
      ok: resultValue.ok === true, http_status: status, code: resultValue.code,
      elapsed_ms: started === undefined ? undefined : Date.now() - started,
    });
    return Response.json(value, { status, headers: { ...headers, ...(status === 200 ? { "X-WSORB-Page": row.host_id } : {}) } });
  }
  async fetch(request: Request): Promise<Response> {
    const key = request.headers.get("X-Room-Key") ?? "";
    if (!(await this.authorized(key))) return json({ error: "Unauthorized" }, 401);
    const protocols = (request.headers.get("Sec-WebSocket-Protocol") ?? "").split(",").map((item) => item.trim());
    const auth = protocols.find((item) => item.startsWith("auth."))?.slice(5) ?? "";
    if (request.headers.get("Upgrade") !== "websocket" || !protocols.includes("wsorb") || !(await sameSecret(auth, key))) return json({ error: "WebSocket upgrade required" }, 426);
    const sockets = this.ctx.getWebSockets();
    if (sockets.length >= MAX_CONNECTIONS) return json({ error: "Room is full" }, 429);
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    const row = this.row()!;
    const clientId = crypto.randomUUID();
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({ clientId });
    server.send(JSON.stringify({ type: "welcome", clientId, hostId: row.host_id, document: bytesToBase64(new Uint8Array(row.document)), state: row.state ? JSON.parse(row.state) : null, notebookUrl: row.notebook_url ?? "" }));
    return new Response(null, { status: 101, webSocket: client, headers: { "Sec-WebSocket-Protocol": "wsorb" } });
  }
  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    const text = typeof message === "string" ? message : new TextDecoder().decode(message);
    const messageSize = typeof message === "string" ? new TextEncoder().encode(message).byteLength : message.byteLength;
    // State snapshots are JSON-wrapped and updates are base64-encoded, so bound
    // the whole frame above those encodings before parsing it.
    if (messageSize > MAX_SNAPSHOT * 2) { ws.close(1009, "Message too large"); return; }
    let msg: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Invalid message");
      msg = parsed as Record<string, unknown>;
    } catch { ws.close(1003, "Invalid message"); return; }
    if (msg.type !== "document" && msg.type !== "state" && msg.type !== "result" && messageSize > MAX_BODY) { ws.close(1009, "Message too large"); return; }
    const attachment = ws.deserializeAttachment() as { clientId?: string; diagnostics?: boolean } | null;
    const clientId = attachment?.clientId;
    if (!clientId) return;
    const diagnostics = typeof msg.diagnostics === "boolean" ? msg.diagnostics : attachment?.diagnostics === true;
    if (typeof msg.diagnostics === "boolean") ws.serializeAttachment({ clientId, diagnostics });
    const row = this.row();
    if (!row) return;
    if (msg.type === "document" && typeof msg.update === "string") {
      this.expirePending();
      let update: Uint8Array;
      let combined: Uint8Array;
      try {
        update = base64ToBytes(msg.update);
        combined = Y.mergeUpdates([new Uint8Array(row.document), update]);
      } catch {
        ws.send(JSON.stringify({ type: "error", error: "Invalid document update" }));
        return;
      }
      if (update.byteLength > MAX_UPDATE) { ws.close(1009, "Update too large"); return; }
      if (combined.byteLength > MAX_SNAPSHOT) { ws.send(JSON.stringify({ type: "error", error: "Document limit reached" })); return; }
      this.ctx.storage.sql.exec("UPDATE room SET document = ?, updated_at = ?", combined, Date.now());
      this.broadcast(JSON.stringify(msg));
    } else if (msg.type === "release-host" && clientId === row.host_id) {
      this.releaseHost(clientId, "Simulation host released control", diagnostics);
    } else if (msg.type === "claim-host") {
      const activeHost = row.host_id && this.ctx.getWebSockets().some((socket) =>
        socket.readyState === WebSocket.OPEN &&
        (socket.deserializeAttachment() as { clientId?: string } | null)?.clientId === row.host_id
      );
      const hostId = activeHost ? row.host_id : clientId;
      emitDiagnostic(diagnostics, "room_relay", hostId === clientId ? "host_claimed" : "host_conflict", { page_id: clientId, owner_page_id: hostId });
      if (hostId === clientId) this.ctx.storage.sql.exec("UPDATE room SET host_id = ?, updated_at = ?", hostId, Date.now());
      this.broadcast(JSON.stringify({ type: "host", hostId }));
    } else if (msg.type === "state" && clientId === row.host_id && msg.state !== undefined) {
      const state = JSON.stringify(msg.state);
      if (new TextEncoder().encode(state).byteLength > MAX_SNAPSHOT) return;
      this.ctx.storage.sql.exec("UPDATE room SET state = ?, updated_at = ?", state, Date.now());
      this.broadcast(JSON.stringify({ type: "state", state: msg.state }));
    } else if (msg.type === "notebook" && validNotebookUrl(msg.url)) {
      this.ctx.storage.sql.exec("UPDATE room SET notebook_url = ?, updated_at = ?", msg.url, Date.now());
      this.broadcast(JSON.stringify({ type: "notebook", url: msg.url }));
    } else if (msg.type === "command" && typeof msg.id === "string" && /^[A-Za-z0-9_-]{1,100}$/.test(msg.id) && msg.command && typeof msg.command === "object" && !Array.isArray(msg.command) && row.host_id) {
      this.expirePending();
      const pendingCount = this.ctx.storage.sql.exec<{ count: number }>("SELECT COUNT(*) AS count FROM pending").one().count;
      if (pendingCount >= 64) { ws.send(JSON.stringify({ type: "error", error: "Too many pending commands" })); return; }
      if (this.ctx.storage.sql.exec("SELECT id FROM pending WHERE id = ?", msg.id).toArray().length) { ws.send(JSON.stringify({ type: "error", error: "Command ID already pending" })); return; }
      this.ctx.storage.sql.exec("INSERT INTO pending (id, requester, created_at) VALUES (?, ?, ?)", msg.id, clientId, Date.now());
      this.ctx.storage.setAlarm(Date.now() + 30_000).catch(() => undefined);
      emitDiagnostic(diagnostics, "room_relay", "call_dispatched", { trace_id: msg.id, page_id: row.host_id });
      this.sendTo(row.host_id, { type: "execute", id: msg.id, command: msg.command, requester: clientId, ...(diagnostics ? { diagnostics: true } : {}) });
    } else if (msg.type === "result" && typeof msg.id === "string" && clientId === row.host_id) {
      this.expirePending();
      const pending = this.ctx.storage.sql.exec<{ requester: string; created_at: number }>("SELECT requester, created_at FROM pending WHERE id = ?", msg.id).toArray()[0];
      if (!pending) return;
      this.ctx.storage.sql.exec("DELETE FROM pending WHERE id = ?", msg.id);
      // A live public API reply is not the room's saved state. Forward its value
      // only to its requester; never persist or broadcast each sensor receipt.
      const oversized = msg.value !== undefined && new TextEncoder().encode(JSON.stringify(msg.value)).byteLength > MAX_SNAPSHOT;
      const result = {
        type: "result", id: msg.id, ok: msg.ok === true && !oversized,
        value: oversized ? undefined : msg.value,
        state: msg.state,
        error: oversized ? "Room result exceeds the response limit" : typeof msg.error === "string" ? msg.error.slice(0, 500) : undefined,
        code: typeof msg.code === "string" && NATIVE_REJECTION_CODES.has(msg.code) ? msg.code : undefined,
      };
      const waiter = this.commandWaiters.get(msg.id);
      if (waiter) waiter(result);
      else this.sendTo(pending.requester, result);
      if (msg.state !== undefined) {
        const state = JSON.stringify(msg.state);
        if (new TextEncoder().encode(state).byteLength <= MAX_SNAPSHOT) {
          this.ctx.storage.sql.exec("UPDATE room SET state = ?, updated_at = ?", state, Date.now());
          this.broadcast(JSON.stringify({ type: "state", state: msg.state }));
        }
      }
    }
  }
  private broadcast(data: string): void {
    for (const socket of this.ctx.getWebSockets()) try { socket.send(data); } catch { /* closed socket */ }
  }
  private sendTo(clientId: string, data: unknown): void {
    for (const socket of this.ctx.getWebSockets()) if ((socket.deserializeAttachment() as { clientId?: string } | null)?.clientId === clientId) try { socket.send(JSON.stringify(data)); } catch { /* closed socket */ }
  }
  private expirePending(): void {
    const expired = this.ctx.storage.sql.exec<{ id: string; requester: string }>("SELECT id, requester FROM pending WHERE created_at <= ?", Date.now() - 30_000).toArray();
    for (const pending of expired) {
      this.ctx.storage.sql.exec("DELETE FROM pending WHERE id = ?", pending.id);
      const value = { type: "result", id: pending.id, ok: false, error: "Room owner timed out" };
      const waiter = this.commandWaiters.get(pending.id);
      if (waiter) waiter(value);
      else if (!pending.requester.startsWith("http:")) this.sendTo(pending.requester, value);
    }
  }
  private releaseHost(clientId: string, reason: string, diagnostics = false): void {
    const row = this.row();
    if (row?.host_id === clientId) {
      emitDiagnostic(diagnostics, "room_relay", "host_released", { page_id: clientId, reason: "host_released" });
      this.ctx.storage.sql.exec("UPDATE room SET host_id = NULL WHERE host_id = ?", clientId);
      this.broadcast(JSON.stringify({ type: "host", hostId: null }));
      const pending = this.ctx.storage.sql.exec<{ id: string; requester: string }>("SELECT id, requester FROM pending").toArray();
      for (const item of pending) {
        this.ctx.storage.sql.exec("DELETE FROM pending WHERE id = ?", item.id);
        const failure = { type: "result", id: item.id, ok: false, error: reason };
        const waiter = this.commandWaiters.get(item.id);
        if (waiter) waiter(failure);
        else if (!item.requester.startsWith("http:")) this.sendTo(item.requester, failure);
      }
    }
  }
  async webSocketClose(ws: WebSocket): Promise<void> {
    const attachment = ws.deserializeAttachment() as { clientId?: string; diagnostics?: boolean } | null;
    if (attachment?.clientId) this.releaseHost(attachment.clientId, "Room owner disconnected", attachment.diagnostics === true);
  }
  async alarm(): Promise<void> {
    const row = this.row();
    this.expirePending();
    const hasSavedWork = row && (new Uint8Array(row.document).byteLength > 2 || row.state !== null || !!row.notebook_url);
    if (row && !hasSavedWork && Date.now() - row.updated_at >= ROOM_TTL && this.ctx.getWebSockets().length === 0) {
      this.ctx.storage.sql.exec("DELETE FROM room");
      await this.ctx.storage.deleteAlarm();
    } else if (hasSavedWork) {
      await this.ctx.storage.deleteAlarm();
    } else await this.ctx.storage.setAlarm(Date.now() + ROOM_TTL);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if ((url.pathname === "/health" || url.pathname === "/api/health") && request.method === "GET") {
      return json({ service: "western-sydney-orbital", status: "ready", simulation: "bevy-wasm-browser" });
    }
    // External Python uses bearer authentication without a browser Origin.
    // Browser callers, including WebSockets, must stay on this exact origin.
    const origin = request.headers.get("Origin");
    if (url.pathname.startsWith("/api/") && origin !== null && origin !== url.origin) return json({ error: "Same-origin request required" }, 403);
    const roomMatch = /^\/api\/rooms\/([a-f0-9-]+)\/(state|ws)$/.exec(url.pathname);
    if (url.pathname === "/api/rooms" && request.method === "POST") {
      if (!request.headers.get("Origin") || request.headers.get("Origin") !== url.origin) return json({ error: "Same-origin request required" }, 403);
      if (!(await env.ROOM_CREATE_LIMIT.limit({ key: "room-creation" })).success) return json({ error: "Please wait before creating another room" }, 429);
      if (Number(request.headers.get("Content-Length") ?? 0) > MAX_BODY) return json({ error: "Request too large" }, 413);
      const id = crypto.randomUUID();
      const keyBytes = crypto.getRandomValues(new Uint8Array(32));
      const key = bytesToBase64Url(keyBytes);
      await env.ROOMS.getByName(id).initialize(key);
      return json({ id, key }, 201);
    }
    if (roomMatch) {
      const [, id, action] = roomMatch;
      const suppliedKey = action === "ws"
        ? (request.headers.get("Sec-WebSocket-Protocol") ?? "").split(",").map(part => part.trim()).find(part => part.startsWith("auth."))?.slice(5) ?? ""
        : request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "") ?? "";
      if (!/^[A-Za-z0-9_-]{43}$/.test(suppliedKey)) return json({ error: "Unauthorized" }, 401);
      const stub = env.ROOMS.getByName(id);
      if (action === "state" && request.method === "GET") {
        const key = request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "") ?? "";
        return stub.state(key);
      }
      if (action === "ws" && request.method === "GET") {
        const protocol = request.headers.get("Sec-WebSocket-Protocol") ?? "";
        const key = protocol.split(",").map((part) => part.trim()).find((part) => part.startsWith("auth."))?.slice(5) ?? "";
        const forwarded = new Request(request, { headers: new Headers(request.headers) });
        forwarded.headers.set("X-Room-Key", key);
        return stub.fetch(forwarded);
      }
      return json({ error: "Method not allowed" }, 405);
    }
    const commandMatch = /^\/api\/rooms\/([a-f0-9-]+)\/commands$/.exec(url.pathname);
    const controllersMatch = /^\/api\/rooms\/([a-f0-9-]+)\/controllers$/.exec(url.pathname);
    if (controllersMatch) {
      const key = request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "") ?? "";
      if (!/^[A-Za-z0-9_-]{43}$/.test(key)) return json({ error: "Unauthorized" }, 401);
      const stub = env.ROOMS.getByName(controllersMatch[1]);
      if (request.method === "GET") return stub.controllers(key);
      if (request.method !== "POST") return json({ error: "Method not allowed" }, 405);
      if (Number(request.headers.get("Content-Length") ?? 0) > CONTROLLER_BODY_LIMIT) return json({ error: "Request too large" }, 413);
      let body: unknown;
      try { body = await readBoundedJson(request, CONTROLLER_BODY_LIMIT); }
      catch (error) { return json({ error: error instanceof RangeError ? "Request too large" : "Invalid JSON" }, error instanceof RangeError ? 413 : 400); }
      return stub.controllers(key, body);
    }
    if (commandMatch && request.method === "POST") {
      const key = request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "") ?? "";
      if (!/^[A-Za-z0-9_-]{43}$/.test(key)) return json({ error: "Unauthorized" }, 401);
      const targetPage = request.headers.get("X-WSORB-Target-Page") ?? undefined;
      if (targetPage !== undefined && !PAGE_UUID.test(targetPage)) return json({ error: "X-WSORB-Target-Page must be a canonical UUID" }, 400);
      const contentLength = Number(request.headers.get("Content-Length") ?? 0);
      if (contentLength > MAX_BODY) return json({ error: "Request too large" }, 413);
      let body: unknown;
      try { body = await readBoundedJson(request, MAX_BODY); }
      catch (error) { return json({ error: error instanceof RangeError ? "Request too large" : "Invalid JSON" }, error instanceof RangeError ? 413 : 400); }
      if (!body || typeof body !== "object" || Array.isArray(body)) return json({ error: "Invalid command request" }, 400);
      const payload = body as { id?: unknown; command?: unknown };
      if (!payload.command || typeof payload.command !== "object" || Array.isArray(payload.command)) return json({ error: "command must be an object" }, 400);
      const id = typeof payload.id === "string" ? payload.id : crypto.randomUUID();
      return env.ROOMS.getByName(commandMatch[1]).command(key, id, payload.command as Record<string, unknown>, request.headers.get("X-WSORB-Diagnostics") === "1", targetPage);
    }
    if (url.pathname.startsWith("/api/")) return json({ error: "Not found" }, 404);
    const response = await env.ASSETS.fetch(request);
    const resultHeaders = new Headers(response.headers);
    resultHeaders.set("X-Content-Type-Options", "nosniff");
    resultHeaders.set("Referrer-Policy", "no-referrer");
    resultHeaders.set("Link", "</licenses.txt>; rel=license");
    resultHeaders.set("X-Frame-Options", "DENY");
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers: resultHeaders });
  },
} satisfies ExportedHandler<Env>;
