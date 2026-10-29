// Presentation only. Coordinates and measurement values come from the caller's
// recorded points. Only their on-screen positions are calculated here.
export type ChartInspectionPoint = {
  x: number;
  y: number;
  title: string;
  series?: string;
  values: Array<{ label: string; value: number | string | null; unit?: string }>;
};

type InspectionMode = "pointer" | "keyboard";
type PlotGeometry = { matrix: DOMMatrix };
type Bounds = { left: number; top: number; right: number; bottom: number };
type TabStop = { original: string | null; users: number; changed: boolean };

let instanceCount = 0;
const tabStops = new WeakMap<HTMLElement, TabStop>();
const activeInspections = new WeakMap<Element, () => void>();
const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });

function ownTabStop(container: HTMLElement): () => void {
  let state = tabStops.get(container);
  if (!state || container.getAttribute("tabindex") !== "0") {
    const original = container.getAttribute("tabindex");
    state = { original, users: 0, changed: original !== "0" };
    tabStops.set(container, state);
    if (state.changed) container.setAttribute("tabindex", "0");
  }
  const owned = state;
  owned.users++;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    owned.users--;
    const restore = () => {
      if (owned.users || tabStops.get(container) !== owned) return;
      tabStops.delete(container);
      if (!owned.changed || container.getAttribute("tabindex") !== "0") return;
      if (owned.original === null) container.removeAttribute("tabindex");
      else container.setAttribute("tabindex", owned.original);
    };
    // A synchronous SVG replacement can acquire this same tab stop before the
    // attribute is restored. The persistent container never needs to blur.
    if (owned.changed && container.ownerDocument.activeElement === container) queueMicrotask(restore);
    else restore();
  };
}

function formatValue(value: number | string | null, unit?: string): string {
  if (value === null || (typeof value === "number" && !Number.isFinite(value))) return "n/a";
  const text = typeof value === "number" ? numberFormat.format(value) : value;
  return unit ? `${text} ${unit}` : text;
}

export function installChartInspector(
  container: HTMLElement,
  plot: SVGSVGElement,
  points: ChartInspectionPoint[],
  description?: string,
): () => void {
  // Snapshot without sorting, filling gaps, or changing supplied values.
  const samples = points.map((point, originalIndex) => ({
    ...point, originalIndex, values: point.values.map(value => ({ ...value })),
  })).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
  if (!samples.length) return () => {};

  const doc = container.ownerDocument;
  const view = doc.defaultView;
  const dialog = container.closest<HTMLDialogElement>("dialog");
  const inspectionScope = dialog ?? doc.documentElement;
  const visualViewport = view?.visualViewport;
  const addedPlotClass = !plot.classList.contains("chart-inspector-plot");
  if (addedPlotClass) plot.classList.add("chart-inspector-plot");
  let prefix: string;
  do { prefix = `chart-inspector-${++instanceCount}`; }
  while (doc.getElementById(`${prefix}-instructions`) || doc.getElementById(`${prefix}-tooltip`));

  const instructions = doc.createElement("p");
  instructions.className = "chart-inspector-instructions";
  instructions.id = `${prefix}-instructions`;
  instructions.textContent = "Hover to inspect · Arrow keys when focused";
  if (description) {
    const context = doc.createElement("span");
    context.className = "screen-reader-only";
    context.textContent = `. ${description}`;
    instructions.append(context);
  }

  const tooltip = doc.createElement("div");
  tooltip.className = "chart-inspector-tooltip";
  tooltip.id = `${prefix}-tooltip`;
  tooltip.setAttribute("role", "tooltip");
  tooltip.setAttribute("aria-live", "off");
  tooltip.setAttribute("aria-atomic", "true");
  tooltip.hidden = true;
  // Geometry and event behavior only. Colors, type, borders and focus styles
  // belong to the caller's stylesheet.
  tooltip.style.position = "fixed";
  tooltip.style.pointerEvents = "none";
  tooltip.style.boxSizing = "border-box";
  tooltip.style.width = "max-content";
  tooltip.style.minWidth = "0";
  tooltip.style.overflowWrap = "anywhere";
  tooltip.style.whiteSpace = "normal";
  tooltip.style.overflow = "hidden";
  tooltip.style.left = "0px";
  tooltip.style.top = "0px";
  const title = doc.createElement("div");
  title.className = "chart-inspector-tooltip__title";
  const values = doc.createElement("dl");
  values.className = "chart-inspector-tooltip__values";
  tooltip.append(title, values);

  const namespace = "http://www.w3.org/2000/svg";
  const overlay = doc.createElementNS(namespace, "g");
  overlay.setAttribute("aria-hidden", "true");
  overlay.setAttribute("pointer-events", "none");
  overlay.setAttribute("visibility", "hidden");
  const crosshair = doc.createElementNS(namespace, "path");
  crosshair.setAttribute("class", "chart-inspector-crosshair");
  crosshair.setAttribute("aria-hidden", "true");
  crosshair.setAttribute("fill", "none");
  crosshair.setAttribute("d", "M-8 0H8M0 -8V8");
  crosshair.setAttribute("vector-effect", "non-scaling-stroke");
  const marker = doc.createElementNS(namespace, "circle");
  marker.setAttribute("class", "chart-inspector-marker");
  marker.setAttribute("aria-hidden", "true");
  marker.setAttribute("r", "4");
  marker.setAttribute("vector-effect", "non-scaling-stroke");
  overlay.append(crosshair, marker);
  plot.append(overlay);
  container.append(instructions, tooltip);

  const releaseTabStop = ownTabStop(container);
  const originalDescription = container.getAttribute("aria-describedby");
  const originalIds = originalDescription?.trim().split(/\s+/).filter(Boolean) ?? [];
  const ownedIds = [instructions.id, tooltip.id];
  const installedDescription = [...originalIds, ...ownedIds].join(" ");
  container.setAttribute("aria-describedby", installedDescription);
  let disposed = false;
  let activeIndex = -1;
  let mode: InspectionMode | null = null;

  function hide() {
    tooltip.hidden = true;
    tooltip.setAttribute("aria-live", "off");
    overlay.setAttribute("visibility", "hidden");
    mode = null;
    if (activeInspections.get(inspectionScope) === hide) activeInspections.delete(inspectionScope);
  }

  function readGeometry(): PlotGeometry | null {
    if (disposed || !plot.isConnected || typeof DOMPoint === "undefined") return null;
    try {
      const rect = plot.getBoundingClientRect();
      if (!(rect.width > 0 && rect.height > 0)) return null;
      let left = Math.max(0, rect.left), top = Math.max(0, rect.top);
      let right = Math.min(view?.innerWidth ?? doc.documentElement.clientWidth, rect.right);
      let bottom = Math.min(view?.innerHeight ?? doc.documentElement.clientHeight, rect.bottom);
      for (let ancestor = plot.parentElement; ancestor && view; ancestor = ancestor.parentElement) {
        const style = view.getComputedStyle(ancestor);
        const box = ancestor.getBoundingClientRect();
        if (/^(auto|scroll|hidden|clip)$/.test(style.overflowX)) {
          left = Math.max(left, box.left); right = Math.min(right, box.right);
        }
        if (/^(auto|scroll|hidden|clip)$/.test(style.overflowY)) {
          top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom);
        }
      }
      if (right <= left || bottom <= top) return null;
      const matrix = plot.getScreenCTM();
      if (!matrix || ![matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f].every(Number.isFinite)) return null;
      const determinant = matrix.a * matrix.d - matrix.b * matrix.c;
      if (!Number.isFinite(determinant) || determinant === 0) return null;
      return { matrix };
    } catch {
      return null;
    }
  }

  function screenPoint(point: ChartInspectionPoint, matrix: DOMMatrix): DOMPoint | null {
    try {
      const screen = new DOMPoint(point.x, point.y).matrixTransform(matrix);
      return Number.isFinite(screen.x) && Number.isFinite(screen.y) ? screen : null;
    } catch {
      return null;
    }
  }

  function tooltipBounds(): Bounds | null {
    if (!view || (dialog && !dialog.open)) return null;
    const left = visualViewport?.offsetLeft ?? 0;
    const top = visualViewport?.offsetTop ?? 0;
    const width = visualViewport?.width ?? doc.documentElement.clientWidth;
    const height = visualViewport?.height ?? doc.documentElement.clientHeight;
    const bounds = { left, top, right: left + width, bottom: top + height };
    // A renderer may install while its dialog is closed. Resolve the current
    // open boundary when inspecting, not from the initial visibility state.
    const openDialog = container.closest<HTMLDialogElement>("dialog[open]");
    if (openDialog) {
      const rect = openDialog.getBoundingClientRect();
      bounds.left = Math.max(bounds.left, rect.left);
      bounds.top = Math.max(bounds.top, rect.top);
      bounds.right = Math.min(bounds.right, rect.right);
      bounds.bottom = Math.min(bounds.bottom, rect.bottom);
    }
    if (!Object.values(bounds).every(Number.isFinite)
      || bounds.right - bounds.left <= 16 || bounds.bottom - bounds.top <= 16) return null;
    return bounds;
  }

  function show(index: number, nextMode: InspectionMode, geometry = readGeometry()): boolean {
    const point = samples[index];
    const screen = geometry && point ? screenPoint(point, geometry.matrix) : null;
    const bounds = screen ? tooltipBounds() : null;
    if (!point || !screen || !bounds) { hide(); return false; }
    const previous = activeInspections.get(inspectionScope);
    if (previous && previous !== hide) previous();
    activeInspections.set(inspectionScope, hide);
    const changed = index !== activeIndex || nextMode !== mode || tooltip.hidden;
    tooltip.setAttribute("aria-live", nextMode === "keyboard" ? "polite" : "off");
    tooltip.hidden = false;
    if (changed) {
      title.textContent = point.title;
      values.replaceChildren();
      for (const value of point.values) {
        const label = doc.createElement("dt");
        const detail = doc.createElement("dd");
        label.textContent = value.label;
        detail.textContent = formatValue(value.value, value.unit);
        values.append(label, detail);
      }
    }
    tooltip.style.maxWidth = `${Math.min(280, bounds.right - bounds.left - 16)}px`;
    tooltip.style.maxHeight = `${bounds.bottom - bounds.top - 16}px`;
    const rect = tooltip.getBoundingClientRect();
    if (!(rect.width > 0 && rect.height > 0)
      || !Number.isFinite(rect.width) || !Number.isFinite(rect.height)) { hide(); return false; }
    const preferredLeft = screen.x + 12 + rect.width <= bounds.right - 8
      ? screen.x + 12 : screen.x - 12 - rect.width;
    const preferredTop = screen.y + 12 + rect.height <= bounds.bottom - 8
      ? screen.y + 12 : screen.y - 12 - rect.height;
    tooltip.style.left = `${Math.max(bounds.left + 8, Math.min(preferredLeft, bounds.right - 8 - rect.width))}px`;
    tooltip.style.top = `${Math.max(bounds.top + 8, Math.min(preferredTop, bounds.bottom - 8 - rect.height))}px`;
    crosshair.setAttribute("transform", `translate(${point.x} ${point.y})`);
    marker.setAttribute("cx", String(point.x));
    marker.setAttribute("cy", String(point.y));
    overlay.setAttribute("visibility", "visible");
    activeIndex = index;
    mode = nextMode;
    return true;
  }

  function onPointerMove(event: PointerEvent) {
    if (!Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) return;
    const target = event.target;
    if (!(target instanceof Element) || !plot.contains(target)) return;
    const geometry = readGeometry();
    if (!geometry) { hide(); return; }

    // Bars can be long enough that another bar's endpoint is closer. An
    // annotation identifies an actual supplied point, not a cursor value.
    const directTarget = target.closest("[data-chart-inspection-index]");
    if (directTarget && plot.contains(directTarget)) {
      const rawIndex = directTarget.getAttribute("data-chart-inspection-index");
      if (rawIndex !== null && /^\d+$/.test(rawIndex)) {
        const originalIndex = Number(rawIndex);
        if (Number.isSafeInteger(originalIndex)) {
          const directIndex = samples.findIndex(point => point.originalIndex === originalIndex);
          if (directIndex >= 0 && show(directIndex, "pointer", geometry)) return;
        }
      }
    }

    const seriesTarget = target.closest("[data-chart-inspection-series]");
    const series = seriesTarget && plot.contains(seriesTarget)
      ? seriesTarget.getAttribute("data-chart-inspection-series") : null;
    let nearestIndex = -1;
    let nearestDistance = Infinity;
    for (let index = 0; index < samples.length; index++) {
      const point = samples[index];
      if (series !== null && point.series !== series) continue;
      const screen = screenPoint(point, geometry.matrix);
      if (!screen) continue;
      const distance = Math.hypot(screen.x - event.clientX, screen.y - event.clientY);
      if (Number.isFinite(distance) && distance < nearestDistance) {
        nearestIndex = index;
        nearestDistance = distance;
      }
    }
    if (nearestIndex < 0) hide();
    else show(nearestIndex, "pointer", geometry);
  }

  function onPointerDown(event: PointerEvent) {
    if (event.pointerType === "touch" || event.button !== 0) return;
    onPointerMove(event);
    container.focus({ preventScroll: true });
  }

  function onPointerLeave() {
    if (mode !== "keyboard") hide();
  }

  function onFocus() {
    if (mode !== "pointer") show(activeIndex < 0 ? 0 : activeIndex, "keyboard");
  }

  function onKeyDown(event: KeyboardEvent) {
    // View data summaries, tables and other descendant controls keep their keys.
    if (event.target !== container || event.defaultPrevented || event.isComposing
      || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.key === "Escape") {
      if (tooltip.hidden) return;
      hide();
    } else {
      const current = activeIndex < 0 ? 0 : activeIndex;
      let next: number;
      switch (event.key) {
        case "ArrowLeft": next = Math.max(0, current - 1); break;
        case "ArrowRight": next = Math.min(samples.length - 1, current + 1); break;
        case "Home": next = 0; break;
        case "End": next = samples.length - 1; break;
        default: return;
      }
      if (!show(next, "keyboard")) return;
    }
    event.preventDefault();
    event.stopPropagation();
  }

  function onDialogKeyDown(event: KeyboardEvent) {
    // Hover does not move focus. Dismiss within this dialog even when another
    // dialog control still has focus, without installing global key handlers.
    if (event.key !== "Escape" || tooltip.hidden || event.defaultPrevented || event.isComposing
      || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.target !== container && event.composedPath().includes(container)) return;
    hide();
    event.preventDefault();
    event.stopPropagation();
  }

  function onGeometryChange(event: Event) {
    if (event.type === "scroll") { hide(); return; }
    if (!tooltip.hidden && mode !== null) show(activeIndex, mode);
  }

  plot.addEventListener("pointermove", onPointerMove, { passive: true });
  plot.addEventListener("pointerdown", onPointerDown, { passive: true });
  plot.addEventListener("pointerleave", onPointerLeave);
  plot.addEventListener("pointercancel", onPointerLeave);
  container.addEventListener("focus", onFocus);
  container.addEventListener("blur", hide);
  container.addEventListener("keydown", onKeyDown);
  doc.addEventListener("scroll", onGeometryChange, { capture: true, passive: true });
  view?.addEventListener("resize", onGeometryChange);
  visualViewport?.addEventListener("resize", onGeometryChange);
  visualViewport?.addEventListener("scroll", onGeometryChange);
  dialog?.addEventListener("keydown", onDialogKeyDown, true);
  dialog?.addEventListener("close", hide);
  if (doc.activeElement === container) onFocus();

  return () => {
    if (disposed) return;
    disposed = true;
    plot.removeEventListener("pointermove", onPointerMove);
    plot.removeEventListener("pointerdown", onPointerDown);
    plot.removeEventListener("pointerleave", onPointerLeave);
    plot.removeEventListener("pointercancel", onPointerLeave);
    container.removeEventListener("focus", onFocus);
    container.removeEventListener("blur", hide);
    container.removeEventListener("keydown", onKeyDown);
    doc.removeEventListener("scroll", onGeometryChange, true);
    view?.removeEventListener("resize", onGeometryChange);
    visualViewport?.removeEventListener("resize", onGeometryChange);
    visualViewport?.removeEventListener("scroll", onGeometryChange);
    dialog?.removeEventListener("keydown", onDialogKeyDown, true);
    dialog?.removeEventListener("close", hide);
    hide();
    overlay.remove();
    instructions.remove();
    tooltip.remove();
    if (addedPlotClass) plot.classList.remove("chart-inspector-plot");
    const currentDescription = container.getAttribute("aria-describedby");
    if (currentDescription === installedDescription) {
      if (originalDescription === null) container.removeAttribute("aria-describedby");
      else container.setAttribute("aria-describedby", originalDescription);
    } else if (currentDescription !== null) {
      const remaining = currentDescription.split(/\s+/).filter(id => id && !ownedIds.includes(id));
      if (remaining.length) container.setAttribute("aria-describedby", remaining.join(" "));
      else container.removeAttribute("aria-describedby");
    }
    releaseTabStop();
  };
}
