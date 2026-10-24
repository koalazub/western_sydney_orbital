// Real fetch, Web Crypto and WebAssembly only. Compilation may overlap transfer;
// execution still waits for the complete byte-count and SHA-256 checks.
export type EngineManifest = { bytes: number; sha256: string };

export type EngineLoadTimings = {
  streaming_compile: boolean;
  decoded_bytes: number;
  headers_ms: number;
  download_ms: number;
  buffer_assembly_ms: number;
  integrity_check_ms: number;
  integrity_ready_ms: number;
  module_ready_ms: number;
  verified_module_ms: number;
};

export function engineManifest(value: unknown): EngineManifest {
  if (!value || typeof value !== "object"
    || !("bytes" in value) || !Number.isSafeInteger(value.bytes)
    || Number(value.bytes) <= 0 || Number(value.bytes) > 64 * 1048576
    || !("sha256" in value) || typeof value.sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(value.sha256)) {
    throw new Error("Invalid engine manifest. Rebuild and reload the simulation.");
  }
  return { bytes: Number(value.bytes), sha256: value.sha256 };
}

async function readBytes(
  response: Response,
  expected: number,
  controller: AbortController,
  onProgress?: (loaded: number, total: number) => void,
) {
  if (!response.body) throw new Error("Engine response has no body.");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let loaded = 0;
  let lastReport = 0;
  onProgress?.(0, expected);
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    loaded += value.byteLength;
    if (loaded > expected) {
      controller.abort();
      throw new Error("Engine download exceeds its declared length.");
    }
    chunks.push(value);
    if (performance.now() - lastReport >= 80) {
      onProgress?.(loaded, expected);
      lastReport = performance.now();
    }
  }
  if (loaded !== expected) throw new Error("Incomplete engine download. Reload to get the current build.");
  const downloaded = performance.now();
  const bytes = new Uint8Array(loaded);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  onProgress?.(loaded, expected);
  return { bytes, downloaded, assembly_ms: performance.now() - downloaded };
}

export async function loadVerifiedEngine(
  url: string,
  metadata: EngineManifest,
  onProgress?: (loaded: number, total: number) => void,
): Promise<{ module: WebAssembly.Module; timings: EngineLoadTimings }> {
  const manifest = engineManifest(metadata);
  const started = performance.now();
  const controller = new AbortController();
  const response = await fetch(url, { signal: controller.signal });
  if (!response.ok) throw new Error(`Engine download returned HTTP ${response.status}`);
  const headersMs = performance.now() - started;
  const streaming = typeof WebAssembly.compileStreaming === "function";
  const compileResponse = streaming ? response.clone() : null;
  const body = readBytes(response, manifest.bytes, controller, onProgress);
  const compilation = (compileResponse
    ? WebAssembly.compileStreaming(compileResponse)
    : body.then(({ bytes }) => WebAssembly.compile(bytes)))
    .then(module => ({ module, ready_ms: performance.now() - started }));
  const verification = body.then(async ({ bytes, downloaded, assembly_ms }) => {
    const checking = performance.now();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
    if (sha256 !== manifest.sha256) throw new Error("Engine integrity check failed. Reload to get the current build.");
    return {
      download_ms: downloaded - started,
      buffer_assembly_ms: assembly_ms,
      integrity_check_ms: performance.now() - checking,
      integrity_ready_ms: performance.now() - started,
    };
  });
  const [verified, compiled] = await Promise.allSettled([verification, compilation]);
  if (verified.status === "rejected") { controller.abort(); throw verified.reason; }
  if (compiled.status === "rejected") { controller.abort(); throw compiled.reason; }
  return {
    module: compiled.value.module,
    timings: {
      streaming_compile: streaming,
      decoded_bytes: manifest.bytes,
      headers_ms: headersMs,
      ...verified.value,
      module_ready_ms: compiled.value.ready_ms,
      verified_module_ms: performance.now() - started,
    },
  };
}
