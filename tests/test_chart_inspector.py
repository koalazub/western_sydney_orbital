"""Read-only source contracts, not DOM execution or visual/browser evidence."""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "worker/web/chart-inspector.ts").read_text()


def section(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


def test_inspection_has_one_visible_owner_and_dismisses_on_scroll():
    show = section("function show(", "function onPointerMove(")
    assert "activeInspections.get(inspectionScope)" in show
    assert "if (previous && previous !== hide) previous()" in show
    assert "activeInspections.set(inspectionScope, hide)" in show
    hide = section("function hide(", "function readGeometry(")
    assert "activeInspections.delete(inspectionScope)" in hide
    changed = section("function onGeometryChange(", 'plot.addEventListener("pointermove"')
    assert 'if (event.type === "scroll") { hide(); return; }' in changed


def test_public_interface_accepts_only_caller_supplied_points_and_returns_disposer():
    interface = section("export type ChartInspectionPoint =", "type InspectionMode")
    for declaration in (
        "x: number;",
        "y: number;",
        "title: string;",
        "series?: string;",
        "values: Array<{ label: string; value: number | string | null; unit?: string }>",
    ):
        assert declaration in interface
    assert re.search(
        r"export function installChartInspector\(\s*container: HTMLElement,\s*"
        r"plot: SVGSVGElement,\s*points: ChartInspectionPoint\[\],\s*"
        r"description\?: string,?\s*\): \(\) => void",
        SOURCE,
    )
    assert len(re.findall(r"\bexport\b", SOURCE)) == 2


def test_helper_has_no_data_loading_control_execution_or_html_parsing_capabilities():
    for forbidden in (
        "import ",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "localStorage",
        "sessionStorage",
        "console.",
        "location.",
        "api.control",
        "RoomEngine",
        "canvas",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "new Function(",
        "setTimeout(",
        "setInterval(",
        "requestAnimationFrame(",
        "ResizeObserver",
        "MutationObserver",
        "Math.random(",
    ):
        assert forbidden not in SOURCE


def test_static_snapshots_filter_only_nonfinite_coordinates_and_preserve_point_order():
    snapshot = section("// Snapshot without", "const doc =")
    assert "points.map((point, originalIndex) => ({" in snapshot
    assert "...point, originalIndex, values: point.values.map(value => ({ ...value }))" in snapshot
    assert ".filter(point => Number.isFinite(point.x) && Number.isFinite(point.y))" in snapshot
    assert snapshot.index("originalIndex") < snapshot.index(".filter(")
    assert "if (!samples.length) return () => {};" in snapshot
    for fabricated_value in ("sort(", "reduce(", "parseFloat(", "parseInt(", "toFixed("):
        assert fabricated_value not in SOURCE
    assert re.search(r"\b(?:points|samples)\.(?:push|splice|reverse)\(", SOURCE) is None
    assert "?? 0" not in snapshot and "|| 0" not in snapshot


def test_numeric_values_keep_six_decimals_strings_stay_literal_and_missing_is_na():
    assert "new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 })" in SOURCE
    formatting = section("function formatValue(", "export function installChartInspector(")
    assert 'value === null || (typeof value === "number" && !Number.isFinite(value))' in formatting
    assert 'return "n/a"' in formatting
    assert 'typeof value === "number" ? numberFormat.format(value) : value' in formatting
    assert "`${text} ${unit}`" in formatting
    show = section("function show(", "function onPointerMove(")
    assert "title.textContent = point.title" in show
    assert "label.textContent = value.label" in show
    assert "detail.textContent = formatValue(value.value, value.unit)" in show
    assert 'doc.createElement("dt")' in show and 'doc.createElement("dd")' in show
    assert "if (changed)" in show


def test_screen_distance_uses_ctm_transformed_existing_points_and_both_pointer_axes():
    geometry = section("function readGeometry(", "function tooltipBounds(")
    assert "plot.getScreenCTM()" in geometry
    assert "new DOMPoint(point.x, point.y).matrixTransform(matrix)" in geometry
    assert "Number.isFinite(screen.x) && Number.isFinite(screen.y) ? screen : null" in geometry
    pointer = section("function onPointerMove(", "function onPointerDown(")
    assert "Number.isFinite(event.clientX)" in pointer
    assert "Number.isFinite(event.clientY)" in pointer
    assert "const point = samples[index]" in pointer
    assert "screenPoint(point, geometry.matrix)" in pointer
    assert "Math.hypot(screen.x - event.clientX, screen.y - event.clientY)" in pointer
    assert "distance < nearestDistance" in pointer
    assert 'show(nearestIndex, "pointer", geometry)' in pointer
    for invented_hit in ("offsetX", "offsetY", "getPointAtLength", "getTotalLength", "inverse("):
        assert invented_hit not in SOURCE


def test_direct_annotations_use_original_finite_point_indices_only_inside_the_plot():
    pointer = section("function onPointerMove(", "function onPointerDown(")
    assert "if (!(target instanceof Element) || !plot.contains(target)) return" in pointer
    assert 'target.closest("[data-chart-inspection-index]")' in pointer
    assert "directTarget && plot.contains(directTarget)" in pointer
    assert 'directTarget.getAttribute("data-chart-inspection-index")' in pointer
    assert r"/^\d+$/.test(rawIndex)" in pointer
    assert "Number.isSafeInteger(originalIndex)" in pointer
    assert "samples.findIndex(point => point.originalIndex === originalIndex)" in pointer
    assert 'if (directIndex >= 0 && show(directIndex, "pointer", geometry)) return' in pointer
    assert pointer.index("show(directIndex") < pointer.index("let nearestDistance")
    assert "samples[originalIndex]" not in pointer


def test_series_annotation_limits_nearest_search_to_matching_actual_run_points():
    pointer = section("function onPointerMove(", "function onPointerDown(")
    assert 'target.closest("[data-chart-inspection-series]")' in pointer
    assert "seriesTarget && plot.contains(seriesTarget)" in pointer
    assert 'seriesTarget.getAttribute("data-chart-inspection-series") : null' in pointer
    assert "if (series !== null && point.series !== series) continue" in pointer
    assert pointer.index("point.series !== series") < pointer.index("screenPoint(point")
    assert "if (nearestIndex < 0) hide()" in pointer
    assert pointer.index("show(directIndex") < pointer.index("const seriesTarget")


def test_hidden_detached_null_or_singular_svg_geometry_is_unavailable_not_zero():
    geometry = section("function readGeometry(", "function screenPoint(")
    assert '!plot.isConnected || typeof DOMPoint === "undefined"' in geometry
    assert "plot.getBoundingClientRect()" in geometry
    assert "rect.width > 0 && rect.height > 0" in geometry
    assert "if (!matrix ||" in geometry
    assert (
        "[matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f].every(Number.isFinite)"
        in geometry
    )
    assert "!Number.isFinite(determinant) || determinant === 0" in geometry
    assert "catch {\n      return null;" in geometry
    assert "|| 1" not in geometry and "?? 0" not in geometry
    show = section("function show(", "function onPointerMove(")
    assert "if (!point || !screen || !bounds) { hide(); return false; }" in show


def test_visible_html_tooltip_and_hint_have_collision_checked_unique_ids():
    for class_name in (
        "chart-inspector-tooltip",
        "chart-inspector-tooltip__title",
        "chart-inspector-tooltip__values",
        "chart-inspector-crosshair",
        "chart-inspector-marker",
        "chart-inspector-instructions",
        "chart-inspector-plot",
    ):
        assert f'"{class_name}"' in SOURCE
    assert "`chart-inspector-${++instanceCount}`" in SOURCE
    assert "doc.getElementById(`${prefix}-instructions`)" in SOURCE
    assert "doc.getElementById(`${prefix}-tooltip`)" in SOURCE
    assert 'tooltip.setAttribute("role", "tooltip")' in SOURCE
    assert "Hover to inspect · Arrow keys when focused" in SOURCE
    assert "context.textContent = `. ${description}`" in SOURCE
    assert "container.append(instructions, tooltip)" in SOURCE
    assert "tooltip.hidden = false" in section("function show(", "function onPointerMove(")


def test_marker_and_crosshair_use_actual_sample_coordinates_and_are_decorative():
    assert 'overlay.setAttribute("aria-hidden", "true")' in SOURCE
    assert 'crosshair.setAttribute("aria-hidden", "true")' in SOURCE
    assert 'marker.setAttribute("aria-hidden", "true")' in SOURCE
    assert 'overlay.setAttribute("pointer-events", "none")' in SOURCE
    show = section("function show(", "function onPointerMove(")
    assert 'crosshair.setAttribute("transform", `translate(${point.x} ${point.y})`)' in show
    assert 'marker.setAttribute("cx", String(point.x))' in show
    assert 'marker.setAttribute("cy", String(point.y))' in show
    assert 'overlay.setAttribute("visibility", "visible")' in show
    assert "event.client" not in show
    assert SOURCE.count('"vector-effect", "non-scaling-stroke"') == 2


def test_fixed_tooltip_clamps_to_open_dialog_and_visual_viewport_with_safe_width():
    assert 'container.closest<HTMLDialogElement>("dialog[open]")' in SOURCE
    assert 'tooltip.style.position = "fixed"' in SOURCE
    assert 'tooltip.style.pointerEvents = "none"' in SOURCE
    assert 'tooltip.style.boxSizing = "border-box"' in SOURCE
    assert 'tooltip.style.minWidth = "0"' in SOURCE
    assert 'tooltip.style.overflowWrap = "anywhere"' in SOURCE
    bounds = section("function tooltipBounds(", "function show(")
    for actual_geometry in (
        "visualViewport?.offsetLeft",
        "visualViewport?.offsetTop",
        "visualViewport?.width ?? doc.documentElement.clientWidth",
        "visualViewport?.height ?? doc.documentElement.clientHeight",
        "dialog && !dialog.open",
        'container.closest<HTMLDialogElement>("dialog[open]")',
        "openDialog.getBoundingClientRect()",
        "Math.max(bounds.left, rect.left)",
        "Math.min(bounds.right, rect.right)",
        "Math.max(bounds.top, rect.top)",
        "Math.min(bounds.bottom, rect.bottom)",
        "Object.values(bounds).every(Number.isFinite)",
    ):
        assert actual_geometry in bounds
    show = section("function show(", "function onPointerMove(")
    assert "Math.min(280, bounds.right - bounds.left - 16)" in show
    assert "bounds.bottom - bounds.top - 16" in show
    assert "tooltip.getBoundingClientRect()" in show
    assert "screen.y + 12 : screen.y - 12 - rect.height" in show
    assert (
        "Math.max(bounds.left + 8, Math.min(preferredLeft, bounds.right - 8 - rect.width))" in show
    )
    assert (
        "Math.max(bounds.top + 8, Math.min(preferredTop, bounds.bottom - 8 - rect.height))" in show
    )


def test_keyboard_order_is_one_container_tab_stop_with_only_supported_unmodified_keys():
    tab_stop = section("function ownTabStop(", "function formatValue(")
    assert 'container.setAttribute("tabindex", "0")' in tab_stop
    assert SOURCE.count('setAttribute("tabindex", "0")') == 1
    assert 'setAttribute("tabindex", "1")' not in SOURCE
    keys = section("function onKeyDown(", "function onDialogKeyDown(")
    assert "event.target !== container || event.defaultPrevented || event.isComposing" in keys
    for modifier in ("altKey", "ctrlKey", "metaKey", "shiftKey"):
        assert f"event.{modifier}" in keys
    for command in (
        'case "ArrowLeft": next = Math.max(0, current - 1)',
        'case "ArrowRight": next = Math.min(samples.length - 1, current + 1)',
        'case "Home": next = 0',
        'case "End": next = samples.length - 1',
        "default: return",
        'if (!show(next, "keyboard")) return',
    ):
        assert command in keys
    assert keys.count("event.preventDefault()") == 1
    assert keys.count("event.stopPropagation()") == 1


def test_escape_only_consumes_visible_tooltip_and_blur_or_pointer_leave_hides():
    keys = section("function onKeyDown(", "function onDialogKeyDown(")
    assert 'if (event.key === "Escape") {\n      if (tooltip.hidden) return;\n      hide();' in keys
    assert keys.index("if (tooltip.hidden) return") < keys.index("event.preventDefault()")
    leave = section("function onPointerLeave(", "function onFocus(")
    assert 'if (mode !== "keyboard") hide()' in leave
    assert 'container.addEventListener("blur", hide)' in SOURCE
    assert 'dialog?.addEventListener("close", hide)' in SOURCE
    focus = section("function onFocus(", "function onKeyDown(")
    assert 'show(activeIndex < 0 ? 0 : activeIndex, "keyboard")' in focus
    hide = section("function hide(", "function readGeometry(")
    assert "tooltip.hidden = true" in hide
    assert 'overlay.setAttribute("visibility", "hidden")' in hide


def test_hover_escape_is_dialog_scoped_and_does_not_take_keys_from_data_controls():
    keys = section("function onDialogKeyDown(", "function onGeometryChange(")
    assert 'event.key !== "Escape" || tooltip.hidden || event.defaultPrevented' in keys
    assert "event.target !== container && event.composedPath().includes(container)" in keys
    for modifier in ("altKey", "ctrlKey", "metaKey", "shiftKey", "isComposing"):
        assert f"event.{modifier}" in keys
    assert "hide()" in keys
    assert keys.count("event.preventDefault()") == 1
    assert keys.count("event.stopPropagation()") == 1
    assert 'dialog?.addEventListener("keydown", onDialogKeyDown, true)' in SOURCE
    assert 'dialog?.removeEventListener("keydown", onDialogKeyDown, true)' in SOURCE
    assert 'doc.addEventListener("keydown"' not in SOURCE
    assert 'view?.addEventListener("keydown"' not in SOURCE


def test_keyboard_announcements_are_polite_but_pointer_updates_and_hidden_state_are_off():
    assert 'tooltip.setAttribute("aria-atomic", "true")' in SOURCE
    show = section("function show(", "function onPointerMove(")
    assert 'tooltip.setAttribute("aria-live", nextMode === "keyboard" ? "polite" : "off")' in show
    assert show.index('setAttribute("aria-live"') < show.index("title.textContent = point.title")
    hide = section("function hide(", "function readGeometry(")
    assert 'tooltip.setAttribute("aria-live", "off")' in hide
    assert '"assertive"' not in SOURCE


def test_pointer_focus_does_not_capture_or_cancel_touch_scrolling():
    pointer_down = section("function onPointerDown(", "function onPointerLeave(")
    assert 'event.pointerType === "touch" || event.button !== 0' in pointer_down
    assert "container.focus({ preventScroll: true })" in pointer_down
    assert pointer_down.index("onPointerMove(event)") < pointer_down.index("container.focus(")
    assert 'plot.addEventListener("pointermove", onPointerMove, { passive: true })' in SOURCE
    assert 'plot.addEventListener("pointerdown", onPointerDown, { passive: true })' in SOURCE
    assert "setPointerCapture" not in SOURCE
    assert "touchAction" not in SOURCE
    assert "preventDefault" not in pointer_down


def test_caller_role_name_svg_titles_and_children_are_never_replaced():
    for protected_attribute in ("role", "aria-label", "aria-labelledby"):
        assert f'container.setAttribute("{protected_attribute}"' not in SOURCE
        assert f'plot.setAttribute("{protected_attribute}"' not in SOURCE
    for destructive_operation in (
        "container.replaceChildren",
        "container.remove()",
        "container.blur()",
        "plot.replaceChildren",
        "plot.remove()",
        "plot.replaceWith",
        "querySelector(",
    ):
        assert destructive_operation not in SOURCE
    assert 'createElementNS(namespace, "title")' not in SOURCE
    assert 'createElementNS(namespace, "desc")' not in SOURCE


def test_disposal_is_idempotent_and_removes_every_listener_and_only_generated_nodes():
    assert "if (disposed) return;\n    disposed = true;" in SOURCE
    added = re.findall(r'(\w+)\??\.addEventListener\("(\w+)", (\w+)', SOURCE)
    removed = re.findall(r'(\w+)\??\.removeEventListener\("(\w+)", (\w+)', SOURCE)
    assert sorted(added) == sorted(removed)
    assert 'doc.removeEventListener("scroll", onGeometryChange, true)' in SOURCE
    assert re.findall(r"\b(\w+)\.remove\(\)", SOURCE) == [
        "overlay",
        "instructions",
        "tooltip",
    ]
    assert "releaseTabStop()" in SOURCE


def test_plot_cursor_class_is_removed_only_if_this_helper_added_it():
    assert 'const addedPlotClass = !plot.classList.contains("chart-inspector-plot")' in SOURCE
    assert 'if (addedPlotClass) plot.classList.add("chart-inspector-plot")' in SOURCE
    assert 'if (addedPlotClass) plot.classList.remove("chart-inspector-plot")' in SOURCE
    assert "plot.className =" not in SOURCE


def test_disposal_preserves_concurrent_attribute_changes_and_svg_swap_focus():
    assert 'const originalDescription = container.getAttribute("aria-describedby")' in SOURCE
    assert 'const installedDescription = [...originalIds, ...ownedIds].join(" ")' in SOURCE
    assert "if (currentDescription === installedDescription)" in SOURCE
    assert 'container.setAttribute("aria-describedby", originalDescription)' in SOURCE
    assert "filter(id => id && !ownedIds.includes(id))" in SOURCE
    assert "owned.users || tabStops.get(container) !== owned" in SOURCE
    assert '!owned.changed || container.getAttribute("tabindex") !== "0"' in SOURCE
    assert "container.ownerDocument.activeElement === container) queueMicrotask(restore)" in SOURCE
    assert 'container.setAttribute("tabindex", owned.original)' in SOURCE
    assert "if (doc.activeElement === container) onFocus()" in SOURCE


def test_scroll_and_resize_use_current_dom_geometry_without_polling_or_data_updates():
    changed = section("function onGeometryChange(", 'plot.addEventListener("pointermove"')
    assert "if (!tooltip.hidden && mode !== null) show(activeIndex, mode)" in changed
    assert (
        'doc.addEventListener("scroll", onGeometryChange, { capture: true, passive: true })'
        in SOURCE
    )
    assert 'view?.addEventListener("resize", onGeometryChange)' in SOURCE
    assert 'visualViewport?.addEventListener("resize", onGeometryChange)' in SOURCE
    assert 'visualViewport?.addEventListener("scroll", onGeometryChange)' in SOURCE
    assert "animation" not in SOURCE.lower()
