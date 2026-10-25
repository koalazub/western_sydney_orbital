export type StartupErrorCode =
  | "insecure_context"
  | "webgpu_unavailable"
  | "adapter_unavailable"
  | "adapter_request_error"
  | "canvas_missing"
  | "canvas_invalid"
  | "canvas_owned"
  | "canvas_context_unavailable"
  | "canvas_context_error";

export class GraphicsStartupError extends Error {
  readonly code: StartupErrorCode;

  constructor(code: StartupErrorCode, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "WSOStartupError";
    this.code = code;
  }
}

export type GPUPreflight = {
  backend: "WebGPU";
  vendor: string | null;
  architecture: string | null;
  device: string | null;
  description: string | null;
  fallback: boolean | null;
  canvas_context: "available";
  canvas_origin: "fresh-owned";
};

type AdapterInfo = {
  vendor?: string;
  architecture?: string;
  device?: string;
  description?: string;
  isFallbackAdapter?: boolean;
};
type BrowserGPUAdapter = { info?: AdapterInfo; isFallbackAdapter?: boolean };
type BrowserGPU = {
  requestAdapter(options: { powerPreference: "high-performance" }): Promise<BrowserGPUAdapter | null>;
};

const canvasContexts = new WeakMap<HTMLCanvasElement, object>();

function browserGPU(): BrowserGPU {
  if (globalThis.isSecureContext !== true) {
    throw new GraphicsStartupError("insecure_context", "This page is not running in a secure context. WebGPU requires a secure context.");
  }
  const gpu = (globalThis.navigator as (Navigator & { gpu?: BrowserGPU }) | undefined)?.gpu;
  if (!gpu || typeof gpu.requestAdapter !== "function") {
    throw new GraphicsStartupError("webgpu_unavailable", "This browser does not expose the WebGPU adapter API to this page.");
  }
  return gpu;
}

export function claimPageStartup(): boolean {
  // The global claim survives separate ESM instances, including query versions.
  // It records ownership only and cannot be cleared after a failed startup.
  const key = Symbol.for("wsorb.page-startup.v1");
  if (Object.prototype.hasOwnProperty.call(globalThis, key)) return false;
  Object.defineProperty(globalThis, key, { value: true });
  return true;
}

function validateCanvas(canvas: unknown): asserts canvas is HTMLCanvasElement {
  if (typeof document === "undefined" || typeof HTMLCanvasElement === "undefined"
    || !(canvas instanceof HTMLCanvasElement) || !canvas.isConnected
    || canvas.ownerDocument !== document || document.getElementById("simulation-canvas") !== canvas) {
    throw new GraphicsStartupError("canvas_invalid", "The simulation target is not the connected #simulation-canvas HTML canvas in this document.");
  }
}

function rejectNativeOwner(canvas: HTMLCanvasElement) {
  if (canvas.hasAttribute("data-wsorb-renderer-owner") || canvas.hasAttribute("data-raw-handle")) {
    throw new GraphicsStartupError("canvas_owned", "The simulation canvas has an existing renderer or native window ownership marker. It was not replaced.");
  }
}

function webGPUContext(canvas: HTMLCanvasElement): object {
  let context: object | null;
  try {
    context = canvas.getContext("webgpu");
  } catch (cause) {
    canvas.dataset.wsorbSurface = "webgpu-error";
    throw new GraphicsStartupError("canvas_context_error", "The browser threw an exception while requesting the simulation canvas's WebGPU context.", { cause });
  }
  if (context === null) {
    canvas.dataset.wsorbSurface = "webgpu-unavailable";
    throw new GraphicsStartupError("canvas_context_unavailable", 'The browser returned null for getContext("webgpu") on the fresh simulation canvas.');
  }
  return context;
}

export function createEngineCanvas(): HTMLCanvasElement {
  const template = typeof document === "undefined" ? null : document.getElementById("simulation-canvas");
  if (!template) {
    throw new GraphicsStartupError("canvas_missing", "The page has no #simulation-canvas element.");
  }
  validateCanvas(template);
  rejectNativeOwner(template);
  browserGPU();
  if (template.hasAttribute("data-wsorb-surface")) {
    throw new GraphicsStartupError("canvas_owned", "The simulation canvas already has a graphics startup claim. It was not replaced.");
  }

  // Cloning preserves the template's attributes, including its accessible name,
  // but not a prior drawing context or listeners attached to the old element.
  const canvas = template.cloneNode(false) as HTMLCanvasElement;
  template.replaceWith(canvas);
  validateCanvas(canvas);
  canvas.dataset.wsorbSurface = "claiming";
  // No await or other context-type probe may precede this synchronous claim.
  canvasContexts.set(canvas, webGPUContext(canvas));
  canvas.dataset.wsorbSurface = "webgpu-available";
  return canvas;
}

export async function graphicsPreflight(canvas: HTMLCanvasElement): Promise<GPUPreflight> {
  const gpu = browserGPU();
  validateCanvas(canvas);
  rejectNativeOwner(canvas);
  const claimedContext = canvasContexts.get(canvas);
  if (!claimedContext || canvas.dataset.wsorbSurface !== "webgpu-available") {
    throw new GraphicsStartupError("canvas_invalid", "WebGPU preflight requires the fresh canvas returned by createEngineCanvas().");
  }

  let adapter: BrowserGPUAdapter | null;
  try {
    adapter = await gpu.requestAdapter({ powerPreference: "high-performance" });
  } catch (cause) {
    throw new GraphicsStartupError("adapter_request_error", "The browser rejected the WebGPU adapter request.", { cause });
  }
  if (!adapter) {
    throw new GraphicsStartupError("adapter_unavailable", "The browser returned no WebGPU adapter for this page.");
  }
  validateCanvas(canvas);
  rejectNativeOwner(canvas);
  // Repeating the same context type must return the originally claimed handle.
  // Bevy still owns device creation, canvas configuration and render readiness.
  if (webGPUContext(canvas) !== claimedContext) {
    canvas.dataset.wsorbSurface = "webgpu-error";
    throw new GraphicsStartupError("canvas_context_error", "The simulation canvas returned a different WebGPU context than its startup context.");
  }
  // This adapter is the preflight result, not Bevy's selected rendering device.
  const info = adapter.info;
  return {
    backend: "WebGPU",
    vendor: info?.vendor ?? null,
    architecture: info?.architecture ?? null,
    device: info?.device ?? null,
    description: info?.description ?? null,
    fallback: info?.isFallbackAdapter ?? adapter.isFallbackAdapter ?? null,
    canvas_context: "available",
    canvas_origin: "fresh-owned",
  };
}
