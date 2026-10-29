"""Read-only source contracts, not a DOM substitute or visual/browser proof."""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "worker/web/analytics-overview.ts").read_text()


def section(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


def test_public_interface_is_structural_and_has_no_data_or_control_capabilities():
    for declaration in (
        "export type OverviewRun =",
        "export type OverviewProfile =",
        "export function installAnalyticsOverview(container: HTMLElement)",
        "update(runs: OverviewRun[], profiles?: OverviewProfile[]): void",
        "profileId: string",
        "samples: Array<Array<number | null>>",
        "totals: Record<string, number | string | boolean>",
    ):
        assert declaration in SOURCE
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "localStorage",
        "sessionStorage",
        "console.",
        "api.control",
        "RoomEngine",
        "setTimeout(",
        "setInterval(",
        "requestAnimationFrame(",
        "combineScoreMetrics",
        "Math.random(",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "new Function(",
    ):
        assert forbidden not in SOURCE
    imports = re.findall(r'^import .*? from "([^"]+)";', SOURCE, re.MULTILINE)
    assert set(imports) == {"./chart-inspector.ts", "./analytics-insights.ts"}


def test_cards_and_four_chart_panels_are_the_default_visible_content():
    install = section("export function installAnalyticsOverview(", "\nNOT_PRESENT")
    assert 'html("div", "overview-cards")' in install
    assert 'html("div", "overview-chart-grid")' in install
    assert re.findall(r'makeChart\(prefix, "([a-z]+)",', install) == [
        "fuel",
        "time",
        "thrust",
        "turn",
    ]
    assert "grid.append(...charts.map(chart => chart.panel))" in install
    assert "cards.hidden = grid.hidden = groups.length === 0" in install
    assert "renderFuel(fuel, groups)" in install
    assert 'makeChart(prefix, "time", "Flight duration")' in install
    assert "renderDuration(time, groups)" in install
    assert "renderActivity(thrust, groups, false)" in install
    assert "renderActivity(turns, groups, true)" in install
    chart = section("function makeChart(", "function newPlot(")
    assert 'html("article", "metric-chart")' in chart
    assert 'html("details", "metric-chart__data")' in chart
    assert 'html("summary", "", "View data")' in chart
    assert "data.open" not in SOURCE
    assert "panel.hidden" not in SOURCE


def test_style_contract_exposes_compact_cards_and_panel_sections():
    for class_name in (
        "overview-cards",
        "pilot-card",
        "pilot-card__name",
        "pilot-card__profile",
        "pilot-card__value",
        "pilot-card__scope",
        "pilot-card__stats",
        "overview-chart-grid",
        "metric-chart",
        "metric-chart__header",
        "metric-chart__subtitle",
        "metric-chart__plot",
        "metric-chart__legend",
        "metric-chart__readouts",
        "metric-chart__note",
        "metric-chart__data",
        "overview-data-scroll",
        "overview-empty",
    ):
        assert f'"{class_name}"' in SOURCE
    for token in ("--team-bui", "--team-tom", "--team-harry", "--team-wso"):
        assert f"var({token}," in SOURCE
    assert 'setProperty("--pilot-color", group.team.color)' in SOURCE
    assert 'fontVariantNumeric = "tabular-nums"' in SOURCE


def test_cards_keep_full_cohort_medians_separate_from_selected_run_values():
    grouping = section("function groupsOf(", "function stat(")
    assert "grouped.get(run.profileId)" in grouping
    assert "profiles?.find(entry => entry.id === id)" in grouping
    assert "selected.find(run => run.repetition === 1) ?? selected[0]" in grouping
    assert "if (profiles) groups.sort(" in grouping
    assert "profiles.findIndex(profile => profile.id === left.id)" in grouping
    assert "profiles.findIndex(profile => profile.id === right.id)" in grouping
    assert grouping.count("sort(") == 1
    cards = section("function pilotCard(", "function makeChart(")
    for actual_field in (
        "profile.median_fuel_used_kg : group.first.measurement.fuel_used_kg",
        "profile.median_time_s : group.first.measurement.time_s",
        "profile.median_damage_j : group.first.measurement.damage_j",
        "profile.successes",
        "profile.attempts",
    ):
        assert actual_field in cards
    assert 'profile ? "Median fuel · local est." : "Run fuel · local est."' in cards
    assert 'profile ? "Median damage" : "Run damage"' in cards
    assert 'html("p", "pilot-card__value", finite(time) ? `${decimal(time)} s` : "n/a")' in cards
    assert "Full cohort · median flight duration" in cards
    assert "Selected run · ${runScope(group.first)}" in cards
    assert "value.title" in cards and "exact(time)" in cards
    assert "`${decimal(fuel, 2)} kg`" in cards
    assert "`${decimal(damage)} J`" in cards
    assert "reduce(" not in SOURCE
    assert "best" not in cards.lower()


def test_svg_construction_has_unique_names_and_no_pointer_only_interactions():
    plot = section("function newPlot(", "function replacePlot(")
    assert 'svg("svg", {' in plot
    assert 'role: "img"' in plot
    assert '"aria-labelledby": `${chart.key}-title ${chart.key}-description`' in plot
    assert 'svg("title", { id: `${chart.key}-title` }, chart.title)' in plot
    assert 'svg("desc", { id: `${chart.key}-description` }, description)' in plot
    assert "`analytics-overview-${++instanceCount}`" in SOURCE
    assert 'document.createElementNS("http://www.w3.org/2000/svg", tag)' in SOURCE
    assert "node.textContent = content" in SOURCE
    assert "addEventListener(" not in SOURCE
    assert "tabIndex = 0" in SOURCE
    assert "tabIndex = 1" not in SOURCE
    assert 'role="button"' not in SOURCE


def test_fuel_uses_actual_columns_timestamps_endpoints_and_null_gaps():
    fuel = section("function renderFuel(", "function renderDuration(")
    for actual_data in (
        'analytics.columns.indexOf("time_s")',
        'analytics.columns.indexOf("fuel_used_kg")',
        "analytics.samples.map(row =>",
        "const time = row[timeColumn], value = row[fuelColumn]",
        "finite(time) && finite(value) ? { time, value } : null",
        "for (const point of trace.points)",
        "if (point === null)",
        "connected = false",
        '`${connected ? "L" : "M"}${x(point.time)} ${y(point.value)} `',
        "index === 0 || index === available.length - 1",
        "exact(point.time)",
        "exact(point.value)",
        "secondary: run !== group.first",
        "trace.group.team.repeatDash",
    ):
        assert actual_data in fuel
    assert "Repeats stay separate; gaps stay open" in fuel
    assert 'svg("title"' in fuel
    assert "interpolate" not in fuel
    assert "Bezier" not in fuel
    assert "average" not in fuel.lower()
    assert "Math.max(...points.map(point => point.value))" in fuel
    assert "No observation is added at zero" in fuel


def test_flight_duration_has_measured_values_zero_baseline_scope_and_outcomes():
    duration = section("function renderDuration(", "function renderActivity(")
    for measured_duration in (
        "group.profile.median_time_s : group.first.measurement.time_s",
        "Full cohort · median flight duration",
        "Selected run · ${runScope(group.first)}",
        "group.profile.successes",
        "group.profile.attempts",
        "group.first.outcome",
        "Observed duration · seconds",
        "Shorter duration does not establish a better outcome",
        "Durations may include failures and timeouts, not just arrivals",
        "const x = (value: number) => 112 + value / maximum * 374",
        '"overview-zero-line"',
        "x1: x(0), x2: x(0)",
        "x: x(0)",
        "width: x(row.value) - x(0)",
        '"overview-duration-marker"',
        "`${decimal(row.value)} s`",
        "exact(row.value)",
        '"Observed flight duration, s"',
        "Zero is measured; n/a means unavailable",
    ):
        assert measured_duration in duration
    assert 'html("span", "overview-readout__value", row.outcome)' in duration
    assert "sort(" not in duration
    assert "total_turn_angle_rad" not in duration


def test_local_score_fields_are_compatibility_only_and_never_presented_or_rescaled():
    assert "score: number | null" in SOURCE
    assert "mean_score: number | null" in SOURCE
    rendering = SOURCE.split("const TEAMS:", 1)[1]
    assert re.search(r"\.(?:score|mean_score|scored_pairs)\b", rendering) is None
    for removed_presentation in (
        "renderScores",
        "pilot-card__score",
        "overview-score-bar",
        "overview-score-marker",
        "Score comparison",
        "Native score",
        "native points",
        "signDisplay",
        "pairScope",
        "maxFuel",
        "wikiFuel",
        "fuel_penalty_points",
        "nominal_time",
    ):
        assert removed_presentation not in SOURCE


def test_native_activity_durations_counts_and_physical_rotation_stay_distinct():
    activity = section("function renderActivity(", "export function installAnalyticsOverview(")
    for field in (
        "burn_duration_s",
        "angular_burn_duration_s",
        "turn_duration_s",
        "burn_count",
        "angular_burn_count",
        "turn_count",
        "total_turn_angle_rad",
    ):
        assert f'"{field}"' in activity
    assert "total(group.first, metric.key)" in activity
    assert "total(group.first, metric.count)" in activity
    assert "native physics-step totals" in activity
    assert "Turning can continue without angular thrust" in activity
    assert "Durations overlap; bars are not stacked" in activity
    assert "dashed outline" in activity
    assert "if (value === 0)" in activity
    assert "samples" not in activity
    assert "turn_duration_s" not in section("function total(", "function groupsOf(")
    assert "run.analytics?.totals[key]" in SOURCE


def test_numeric_alternatives_are_native_disclosures_with_at_most_four_columns():
    table = section("function dataTable(", "function selectionCaption(")
    assert 'html("table", "overview-data-table")' in table
    assert 'html("caption", "", caption)' in table
    assert 'cell.scope = "col"' in table
    assert '.scope = "row"' in table
    assert "chart.table.replaceChildren(table)" in table
    for headings in (
        '["Run", "Time, s", "Fuel, kg"]',
        '["Profile", "Scope", "Duration, s", "Arrivals / outcome"]',
        '["Run", "Native metric", "Value", "Episodes"]',
    ):
        assert headings in SOURCE
    assert 'table.setAttribute("role", "region")' in SOURCE
    assert 'table.setAttribute("aria-label", `${title} data`)' in SOURCE
    assert "data.append(summary, table)" in SOURCE


def test_empty_missing_and_zero_values_are_not_fabricated_results():
    assert 'finite(value) ? String(value) : "n/a"' in SOURCE
    assert "numeric(run.analytics?.totals[key])" in SOURCE
    assert "?? 0" not in SOURCE
    assert "|| 0" not in SOURCE
    assert "No measured runs in this selection" in SOURCE
    assert "Choose another seed or teammate, or run a test" in SOURCE
    empty = SOURCE.split("if (!groups.length) {", 1)[1].split("// Selection changes", 1)[0]
    assert 'lastWidthKey = ""' in empty
    assert "chart.disposeInspection?.()" in empty
    assert "return;" in empty
    assert "Fuel samples unavailable for this selection" in SOURCE
    assert "Flight durations unavailable for this selection" in SOURCE
    assert "Native activity totals unavailable for this selection" in SOURCE
    assert "Zero is a measured zero; n/a means unavailable" in SOURCE


def test_svg_stays_responsive_without_animation_or_fixed_dom_dimensions():
    assert 'viewBox: "0 0 640 260"' in SOURCE
    assert "viewBox: `0 0 640 ${height}`" in SOURCE
    assert "new ResizeObserver(() => renderCharts()).observe(container)" in SOURCE
    chart = section("function makeChart(", "function newPlot(")
    assert "width:" not in chart and "height:" not in chart
    assert "animation" not in SOURCE.lower()
    assert '"vector-effect": "non-scaling-stroke"' in SOURCE
    for stretching in ("scaleY", "textLength", "lengthAdjust", 'transform: "scale'):
        assert stretching not in SOURCE


def test_vertical_geometry_uses_actual_width_and_reserves_readable_axis_footer():
    geometry = section("function plotGeometry(", "function emptyPlot(")
    for real_width_geometry in (
        "chart.plot.getBoundingClientRect().width",
        "const width = measuredWidth > 0 ? measuredWidth : 640",
        "const labelUnits = Math.max(15, 12 * 640 / width)",
        'setProperty("--overview-svg-label", `${labelUnits}px`)',
        "const top = Math.max(30, 1.8 * labelUnits)",
        "const bottom = top + bodyHeight(labelUnits)",
    ):
        assert real_width_geometry in geometry
    # These are declared spacing ratios, not simulated text measurements.
    ratios = {
        name: float(re.search(rf"{name}: bottom \+ ([\d.]+) \* labelUnits", geometry)[1])
        for name in ("height", "tickBaseline", "titleBaseline")
    }
    assert (ratios["titleBaseline"] - ratios["tickBaseline"]) * 12 >= 18
    assert ratios["height"] - ratios["titleBaseline"] >= 0.3
    for start, end in (
        ("function renderFuel(", "function renderDuration("),
        ("function renderDuration(", "function renderActivity("),
        ("function renderActivity(", "export function installAnalyticsOverview("),
    ):
        chart = section(start, end)
        assert "geometry.tickBaseline" in chart
        assert "geometry.titleBaseline" in chart
        assert "bottom + 21" not in chart and "bottom + 22" not in chart
        assert "height - 3" not in chart


def test_fuel_and_duration_bodies_grow_without_changing_measurement_scales():
    fuel = section("function renderFuel(", "function renderDuration(")
    assert "plotGeometry(chart, units => Math.max(182, 9 * units))" in fuel
    assert "68 + (value - minimumTime) / timeSpan * 550" in fuel
    assert "bottom - value / fuelSpan * (bottom - top)" in fuel
    assert "y(fuel) + 0.32 * labelUnits" in fuel
    assert "top - 0.8 * labelUnits" in fuel
    assert ", geometry.height)" in fuel
    duration = section("function renderDuration(", "function renderActivity(")
    assert "rows.length * Math.max(54, 2.6 * units)" in duration
    assert "const rowStep = Math.max(54, 2.6 * labelUnits)" in duration
    assert "top + (index + 0.5) * rowStep" in duration
    assert duration.count("y + 0.32 * labelUnits") == 2
    assert "112 + value / maximum * 374" in duration
    assert "width: x(row.value) - x(0)" in duration


def test_activity_value_pairs_and_groups_have_label_relative_vertical_clearance():
    activity = section("function renderActivity(", "export function installAnalyticsOverview(")
    assert "groups.length * (2 * Math.max(24, 1.7 * units) + 1.2 * units)" in activity
    assert "const pairGap = Math.max(24, 1.7 * labelUnits)" in activity
    assert "const groupHeight = 2 * pairGap + 1.2 * labelUnits" in activity
    assert "top + (index + 0.5) * groupHeight" in activity
    assert "rowY + (metricIndex - 0.5) * pairGap" in activity
    assert "const y = centerY - barHeight / 2" in activity
    assert "centerY + 0.32 * labelUnits" in activity
    assert "rowY + 0.32 * labelUnits, group.owner" in activity
    assert "height: barHeight" in activity
    assert "width: x(value) - x(0)" in activity
    pair_ratio = float(re.search(r"pairGap = Math.max\(24, ([\d.]+) \* labelUnits\)", activity)[1])
    assert pair_ratio * 12 >= 20
    assert "metricIndex * 24" not in activity


def test_resize_rerenders_cached_selection_only_for_changed_positive_width():
    install = section("export function installAnalyticsOverview(", "\nNOT_PRESENT")
    for cache_guard in (
        "let groups: Group[] = []",
        'let lastWidthKey = ""',
        "let rendering = false",
        "function renderCharts(force = false)",
        "if (rendering || !groups.length) return",
        "charts.map(chart => chart.plot.getBoundingClientRect().width)",
        "widths.every(width => width > 0)",
        'widths.map(width => width.toFixed(2)).join(":")',
        "if (!force && (!widthKey || widthKey === lastWidthKey)) return",
        "lastWidthKey = widthKey",
        "rendering = true",
        "finally {\n      rendering = false",
        "new ResizeObserver(() => renderCharts()).observe(container)",
    ):
        assert cache_guard in install
    assert install.index("lastWidthKey = widthKey") < install.index("renderFuel(fuel, groups)")
    update = install.split("update(runs, profiles) {", 1)[1]
    assert "groups = groupsOf(runs, profiles)" in update
    assert "const groups" not in update
    assert "renderCharts(true)" in update
    assert "sizeLabels" not in SOURCE


def test_empty_plots_keep_accessible_names_and_separate_their_message_lines():
    empty = section("function emptyPlot(", "function legendItem(")
    assert "plotGeometry(chart, units => Math.max(182, 6 * units))" in empty
    assert "newPlot(chart, message, geometry.height)" in empty
    assert 'const suffix = " for this selection"' in empty
    assert "lines.forEach((line, index)" in empty
    assert "1.7 * geometry.labelUnits" in empty
    assert "replacePlot(chart, plot)" in empty


def _contrast(foreground: str, background: str, opacity: float = 1) -> float:
    def channels(color: str) -> list[float]:
        return [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]

    backdrop = channels(background)
    foreground_channels = [
        source * opacity + target * (1 - opacity)
        for source, target in zip(channels(foreground), backdrop, strict=True)
    ]

    def luminance(values: list[float]) -> float:
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in values]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    light, dark = sorted((luminance(foreground_channels), luminance(backdrop)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_declared_fallback_text_and_trace_colors_have_measured_contrast():
    # Declared fallbacks only. Rendered CSS overrides remain a browser check.
    for background in ("#101319", "#171c24"):
        assert _contrast("#edf1f7", background) >= 4.5
        assert _contrast("#9ba7ba", background) >= 4.5
        for color in ("#4dd6c1", "#8aa7ff", "#ffb477", "#b8adff"):
            assert _contrast(color, background) >= 4.5
            assert _contrast(color, background, opacity=0.65) >= 3


def test_inspection_uses_supplied_samples_and_disposes_old_plot_bindings():
    replace = section("function replacePlot(", "function renderInsight(")
    assert "chart.disposeInspection?.()" in replace
    assert "installChartInspector(chart.panel, plot, points" in replace
    fuel = section("function renderFuel(", "function renderDuration(")
    assert "trace.points.filter((point): point is Point => point !== null)" in fuel
    assert 'label: "Time", value: point.time' in fuel
    assert 'label: "Fuel used", value: point.value' in fuel
    assert '"data-chart-inspection-series": trace.run.id' in fuel
    assert "series: trace.run.id" in fuel
    for start, end in (
        ("function renderDuration(", "function renderActivity("),
        ("function renderActivity(", "export function installAnalyticsOverview("),
    ):
        bars = section(start, end)
        assert '"data-chart-inspection-index": inspection.length' in bars
        assert '"pointer-events": "all"' in bars
        assert "replacePlot(chart, plot, inspection)" in bars


def test_each_chart_has_visible_direction_and_scoped_data_insight():
    insight = section("function renderInsight(", "function plotGeometry(")
    assert "deriveMetricInsight(metric, groups)" in insight
    assert "chart.guidance.textContent = insight.guidance" in insight
    assert "chart.insight.textContent = insight.observation" in insight
    assert "chart.insightScope.textContent = insight.scope" in insight
    install = section("export function installAnalyticsOverview(", "\nNOT_PRESENT")
    assert "for (const chart of charts) renderInsight(chart, groups)" in install
    assert 'panel.setAttribute("aria-describedby", `${guidance.id} ${insight.id}`)' in SOURCE
