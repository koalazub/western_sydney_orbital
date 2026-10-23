// Opt-in CLI diagnostics. Never log headers, credentials or request/response bodies.
import { channel } from "node:diagnostics_channel";

if (process.env.WSO_CF_HTTP2 === "1") {
  const { Agent, setGlobalDispatcher } = await import("undici");
  setGlobalDispatcher(new Agent({ allowH2: true }));
}

if (process.env.WSO_CF_NETWORK_DIAGNOSTICS === "1") {
  const pending = new Map();
  const emit = (event, state, extra = {}) => {
    console.error(`[cf-network] ${JSON.stringify({ event, method: state.method,
      path: state.path, elapsed_ms: Math.round(performance.now() - state.started),
      body_bytes: state.bytes, ...extra })}`);
  };
  channel("undici:request:create").subscribe(({ request }) => {
    if (new URL(request.origin).hostname !== "api.cloudflare.com") return;
    const state = { method: request.method, path: request.path.split("?")[0],
      started: performance.now(), bytes: 0, socket: null, initialWritten: 0 };
    pending.set(request, state);
    emit("created", state);
  });
  channel("undici:client:sendHeaders").subscribe(({ request, socket }) => {
    const state = pending.get(request);
    if (!state) return;
    state.socket = socket;
    state.initialWritten = socket.bytesWritten;
    emit("connected", state, { protocol: socket.alpnProtocol || "http/1.1",
      authorized: socket.authorized, address: socket.remoteAddress });
  });
  channel("undici:request:bodyChunkSent").subscribe(({ request, chunk }) => {
    const state = pending.get(request);
    if (state) state.bytes += chunk.byteLength;
  });
  channel("undici:request:bodySent").subscribe(({ request }) => {
    const state = pending.get(request);
    if (state) emit("body_sent", state);
  });
  channel("undici:request:headers").subscribe(({ request, response }) => {
    const state = pending.get(request);
    if (state) emit("response", state, { status: response.statusCode });
  });
  channel("undici:request:trailers").subscribe(({ request }) => {
    const state = pending.get(request);
    if (state) emit("complete", state);
    pending.delete(request);
  });
  channel("undici:request:error").subscribe(({ request, error }) => {
    const state = pending.get(request);
    if (state) emit("error", state, { name: error.name, code: error.code,
      cause_code: error.cause?.code });
    pending.delete(request);
  });
  setInterval(() => {
    for (const state of pending.values()) {
      emit("progress", state, { socket_bytes_written: state.socket
        ? state.socket.bytesWritten - state.initialWritten : 0 });
    }
  }, 15_000).unref();
}
