"""Static HTML/source contracts only, not browser, GPU or interactive proof."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "worker/web"
SOURCE = (WEB / "flight-analytics.ts").read_text()
CONTROLLERS = (WEB / "controller-profiles.ts").read_text()
CSS = (WEB / "styles.css").read_text()
SHELL = (WEB / "dashboard-shell.ts").read_text()
COLUMNS = (
    "time_s",
    "fuel_used_kg",
    "burn_duration_s",
    "angular_burn_duration_s",
    "turn_duration_s",
    "total_turn_angle_rad",
    "relative_speed_mps",
    "range_m",
    "damage_j",
)


class Elements(HTMLParser):
    """Inspect actual source elements without substituting a DOM or renderer."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.ids = {}
        self.duplicates = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element = {
            "tag": tag,
            "attrs": attributes,
            "parents": tuple(self.stack),
            "text": [],
        }
        self.elements.append(element)
        identity = attributes.get("id")
        if identity:
            if identity in self.ids:
                self.duplicates.append(identity)
            self.ids[identity] = element
        if tag not in {"meta", "link", "input", "br", "hr", "img", "source", "wbr"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        for element in self.stack:
            element["text"].append(data)


@pytest.fixture(scope="module")
def page():
    parsed = Elements()
    parsed.feed((WEB / "index.html").read_text())
    return parsed


def content(element):
    return " ".join(" ".join(element["text"]).split())


def section(start, end):
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


def test_analytics_is_a_disclosure_inside_the_existing_native_dialog(page):
    assert page.duplicates == []
    panel = page.ids["controller-panel"]
    details = page.ids["flight-analytics"]
    assert panel["tag"] == "dialog"
    assert details["tag"] == "details" and "open" not in details["attrs"]
    assert panel in details["parents"]
    assert "hidden" in page.ids["published-analytics-content"]["attrs"]
    assert "role" not in details["attrs"]
    assert "read-only" in SOURCE.lower()
    assert "never select a flight controller" in content(page.ids["analytics-read-only"])
    assert "Published matched-trial scoreboard" in content(page.ids["published-analytics-heading"])
    assert "Current connection job analytics" in content(page.ids["room-analytics-heading"])
    assert not [
        element
        for element in page.elements
        if details in element["parents"] and element["tag"] in {"canvas", "img", "iframe"}
    ]


def test_every_analytics_control_has_a_native_label_and_no_profile_stop_binding(page):
    details = page.ids["flight-analytics"]
    controls = [
        element
        for element in page.elements
        if details in element["parents"] and element["tag"] == "select"
    ]
    assert len(controls) == 7
    labels = {
        element["attrs"].get("for"): element
        for element in page.elements
        if element["tag"] == "label"
    }
    for control in controls:
        identity = control["attrs"]["id"]
        assert control["attrs"]["name"] == identity
        assert identity in labels and content(labels[identity])
        assert identity not in {"controller-owner", "controller-profile"}
        assert "onchange" not in control["attrs"]
    assert 'control.addEventListener("change", render)' in SOURCE
    assert 'cohort.addEventListener("change", renderRoom)' in SOURCE
    assert 'owner.addEventListener("change", () => { selectOwner(); void changedProfile(); })' in (
        CONTROLLERS
    )
    assert 'profile.addEventListener("change", () => { void changedProfile(); })' in CONTROLLERS
    for forbidden in (
        "requestStop(",
        "freshTrial(",
        "hostedRequest(",
        "options.engine",
        "options.room",
        "api.control(",
        'method: "POST"',
        "setInterval(",
        "setTimeout(",
        "requestAnimationFrame(",
    ):
        assert forbidden not in SOURCE


def test_publication_is_lazy_fixed_origin_credential_free_and_stream_bounded():
    load = section("async function loadPublication()", "function roomRuns(")
    assert 'fetch("/analytics/team-comparison.json", {' in load
    for required in (
        'method: "GET"',
        'mode: "same-origin"',
        'credentials: "omit"',
        'redirect: "error"',
        'referrerPolicy: "no-referrer"',
        'response.headers.get("content-length")',
        'response.headers.get("content-type")',
        "response.body?.getReader()",
        "size > MAX_PUBLIC_BYTES",
        "await reader.cancel()",
        "validatePublication(JSON.parse(",
    ):
        assert required in load
    for forbidden in ("Authorization", "headers:", "body:", "connection", "URLSearchParams"):
        assert forbidden not in load
    assert "const MAX_PUBLIC_BYTES = 2 * 1024 * 1024;" in SOURCE
    install = section("export function installFlightAnalytics()", "\nNOT_PRESENT")
    assert (
        'if (!byId<HTMLDialogElement>("controller-panel").open || loaded || loading) return;'
        in install
    )
    assert "openPublished() { void load(); }" in install
    assert 'details.addEventListener("toggle"' in install
    assert "Published analytics unavailable." in install
    assert "catch {" in install
    assert "cause.message" not in SOURCE and "error.message" not in SOURCE
    assert SOURCE.count("await loadPublication()") == 1


def test_publication_validates_the_matched_cohort_instead_of_importing_jobs():
    validation = section("function validatePublication(", "async function loadPublication()")
    assert '"wsorb.teammate_analytics"' in validation
    assert 'shape(value, ["schema", "schema_version", "comparison", "profiles", "trials"])' in (
        validation
    )
    for required in (
        "comparison.seeds.length > 64",
        "value.trials.length > 256",
        "new Set(comparison.seeds).size",
        "comparison.definitions",
        "comparison.source_hashes",
        "trial.engine_sha256 !== comparison.engine_build.sha256",
        "pairs.has(pair)",
        "runs.length !== summaries.length * comparison.seeds.length",
        "summary.scored_runs",
        "summary.scored_pairs",
        "summary.successes",
        "comparison.repetitions !== 2",
        "trial.repetition",
        '!["success", "failed_damage"].includes(trial.outcome)',
        "MAX_PUBLIC_SAMPLES",
    ):
        assert required in validation
    assert "Object.keys(value).every" in SOURCE
    assert "Number.isFinite(value)" in SOURCE and "Number.isSafeInteger(value)" in SOURCE
    room = section("function roomRuns(", "function cell(")
    assert "results.slice(-32).reverse()" in room
    assert "profiles.find(" in room
    assert "MAX_ROOM_SAMPLES" in room
    assert "result.world_metadata_unchanged === true" in room
    assert "result.execution_kind === CONTROLLER_EXECUTION_KIND" in room
    assert "unverified:" in room
    assert "fetch(" not in room and "publication" not in room.lower()
    imports = re.findall(r'^import .*? from "([^"]+)";', SOURCE, re.MULTILINE)
    assert set(imports) == {
        "../../src/wsorb/controllers/catalog.json",
        "../../simulation/scenarios/flight_analytics.json",
        "../shared/controller-jobs.ts",
        "../../src/wsorb/scoring_reference.json",
        "./analytics-overview.ts",
    }


def test_native_totals_and_sample_columns_are_explicit_not_poll_inferred():
    columns = re.search(r"const COLUMNS = \[(.*?)\] as const;", SOURCE, re.DOTALL)
    assert columns
    assert tuple(re.findall(r'"([a-z_]+)"', columns[1])) == COLUMNS
    native_definitions = json.loads(
        (ROOT / "simulation/scenarios/flight_analytics.json").read_text()
    )
    assert native_definitions == {
        "schema_version": 1,
        "definition_id": "native_actuation_rotation_episodes_v1",
        "burn_force_threshold_n": 1e-6,
        "angular_torque_threshold_nm": 1e-6,
        "turn_speed_threshold_rad_s": 1e-3,
    }
    validation = section("function validateAnalytics(", "function validateTerms(")
    for required in (
        "MAX_SCHEMA_SAMPLES",
        "value.sample_count !== value.samples.length",
        "value.columns.some((name, i) => name !== COLUMNS[i])",
        'typeof totals[key] !== "boolean"',
        "COUNTERS.some(key => !integer(totals[key]))",
        "NATIVE_NUMBERS.some(key => !nonnegative(totals[key]))",
        "totals.observed_sim_s",
        "time <= rows[rows.length - 1][0]",
        "rows[0][0] !== 0",
        "rows.push(raw.slice())",
        "totals: { ...totals }",
    ):
        assert required in validation
    totals = section("function renderTotals(", "const svgNode")
    assert "exact(run.analytics?.totals[key])" in totals
    assert "samples" not in totals
    for limit in (
        "const MAX_SCHEMA_SAMPLES = 256;",
        "const MAX_PUBLIC_SAMPLES = 192;",
        "const MAX_ROOM_SAMPLES = 48;",
    ):
        assert limit in SOURCE


def test_optional_publisher_coverage_stats_have_bounded_structured_validation():
    stats = section("function validateProfileStats(", "function validatePublication(")
    for required in (
        'shape(pair, ["seed", "run_ids", "mean_score", "outcomes"])',
        "entry.paired_scores.length !== seeds.length",
        "pair.run_ids.length !== 2",
        "pair.outcomes.length !== 2",
        "seen.has(pair.seed)",
        "PUBLIC_OUTCOMES.includes(key)",
        "shape(entry.metric_runs, MEASUREMENT_FIELDS)",
        '"unscored_runs", "physical_runs", "analytics_runs"',
    ):
        assert required in stats
    assert "trial.engine_sha256 === null" in SOURCE
    assert "trial.analytics === null && trial.score === null" in SOURCE
    assert "comparison.definitions === null" in SOURCE
    assert "rankable: false" in SOURCE
    assert "Object.entries(SCORE_COMPATIBILITY)" in SOURCE
    assert (
        'value.schema_version === 2 && !Object.hasOwn(comparison, "score_compatibility")' in SOURCE
    )
    assert "ordered.sort(" not in SOURCE
    assert "Unranked measured profiles." in SOURCE


@pytest.mark.parametrize("prefix", ["published-analytics", "room-analytics"])
def test_each_svg_has_names_legend_full_numeric_alternative_and_csv(page, prefix):
    svg = page.ids[f"{prefix}-plot"]
    assert svg["tag"] == "svg" and svg["attrs"]["role"] == "img"
    assert svg["attrs"]["viewbox"] == "0 0 760 330"
    assert svg["attrs"]["aria-labelledby"].split() == [
        f"{prefix}-plot-title",
        f"{prefix}-plot-description",
    ]
    for identity in svg["attrs"]["aria-labelledby"].split():
        assert content(page.ids[identity])
    assert page.ids[f"{prefix}-legend"]["tag"] == "ol"
    for suffix in ("outcomes", "totals", "samples"):
        table = page.ids[f"{prefix}-{suffix}"]
        assert table["tag"] == "table"
        assert any(parent["attrs"].get("tabindex") == "0" for parent in table["parents"])
        assert any(
            element["tag"] == "caption" and table in element["parents"] for element in page.elements
        )
    samples = page.ids[f"{prefix}-samples"]
    headings = [
        element
        for element in page.elements
        if element["tag"] == "th" and samples in element["parents"]
    ]
    assert len(headings) == len(COLUMNS) + 2
    assert all(element["attrs"].get("scope") == "col" for element in headings)
    download = page.ids[f"{prefix}-csv"]
    assert download["tag"] == "a"
    assert "download" in download["attrs"] and "hidden" in download["attrs"]
    assert "href" not in download["attrs"]
    assert "native counters, not the graph" in content(page.ids["analytics-sampling-note"])


def test_chart_uses_actual_samples_and_keeps_null_gaps_and_full_precision():
    graph = section("function renderGraph(", "function installPlot(")
    assert "extent(points.map(row => row[0]!))" in graph
    assert "extent(points.map(row => row[metric.column]!))" in graph
    assert "if (row[metric.column] === null) { connected = false; continue; }" in graph
    assert "for (const row of run.analytics!.samples)" in graph
    assert 'svgNode("circle"' in graph
    assert "String(index + 1)" in graph
    assert 'svgNode("title"' in graph and 'svgNode("desc"' in graph
    assert "No recorded samples for this selection." in graph
    plot = section("function installPlot(", "function renderPublication(")
    assert "sample.map(exact)" in plot
    assert 'value === null ? "" : String(value)' in plot
    assert "download.hidden = sampleCount === 0" in plot
    assert "URL.revokeObjectURL(csvUrl)" in plot
    assert "seeds.includes(100)" in plot
    assert 'new Option("Compare all profiles", "all")' in plot
    for forbidden in ("interpolate(", "Math.random(", "new Array(", "Array.from({ length:"):
        assert forbidden not in graph


def test_measured_comparison_does_not_display_or_rank_incompatible_pair_points(page):
    scoreboard = page.ids["published-analytics-scoreboard"]
    assert "Medians over available physical outcomes" in content(scoreboard)
    assert "No point ranking" in content(scoreboard)
    assert "STP points are unavailable" in content(page.ids["published-scoreboard-note"])
    publication = section("function renderPublication(", "export function installFlightAnalytics()")
    assert "sort(" not in publication
    assert "rounded(entry.mean_score)" not in publication
    assert "rounded(entry.median_score)" not in publication
    assert "repetition ${trial.repetition}" in SOURCE
    assert "repetition: null" in SOURCE
    assert "Each published repetition remains its own line." in content(
        page.ids["analytics-sampling-note"]
    )


def test_download_links_use_only_the_fixed_public_asset_contract(page):
    links = [
        element
        for element in page.elements
        if element["tag"] == "a" and element["attrs"].get("href", "").startswith("/analytics/")
    ]
    assert {element["attrs"]["href"] for element in links} == {
        "/analytics/team-comparison.json",
        "/analytics/trials.csv",
        "/analytics/summary.csv",
        "/analytics/score-components.svg",
        "/analytics/fuel-burns.svg",
        "/analytics/turns.svg",
        "/analytics/efficiency.svg",
    }
    for link in links:
        assert "download" in link["attrs"]
        assert link["attrs"]["referrerpolicy"] == "no-referrer"
        assert page.ids["published-analytics-content"] in link["parents"]
        assert "?" not in link["attrs"]["href"] and "#" not in link["attrs"]["href"]


def test_result_hook_preserves_existing_consent_poll_pause_and_focus_behavior(page):
    assert "disabled" in page.ids["run-controller"]["attrs"]
    assert "run.disabled = true;" in CONTROLLERS
    assert "run.disabled = busy || !view.service.online || !!active || !options.ready();" in (
        CONTROLLERS
    )
    assert "const analytics = installFlightAnalytics();" in CONTROLLERS
    assert CONTROLLERS.count("analytics.updateRoomResults(view.results);") == 1
    assert "analytics.updateRoomResults([]);" in CONTROLLERS
    for preserved in (
        "if (!await confirmFreshTrial(label(profile.value))) return;",
        "const expected = await freshTrial(api);",
        "const changedProfile = async () => { description(); await requestStop(); };",
        "if (!panel.open || !autoUpdate) return;",
        'byId<HTMLButtonElement>("pause-controller-updates")',
        "panel.querySelector<HTMLButtonElement>('.lab-navigation "
        '[data-lab-view="compare"]\')?.focus();',
        "open.focus({ preventScroll: true });",
        'event.code !== "KeyC" || !event.shiftKey',
    ):
        assert preserved in CONTROLLERS
    assert "if (nextSignature === signature) return;" in SOURCE
    assert "if (details.open) updateCohorts();" in SOURCE


def test_static_accessibility_reflow_and_no_inline_script_contracts(page):
    for element in page.elements:
        for reference in ("aria-labelledby", "aria-describedby"):
            for identity in element["attrs"].get(reference, "").split():
                assert identity in page.ids
        assert not any(name.startswith("on") for name in element["attrs"])
        assert int(element["attrs"].get("tabindex", "0")) <= 0
    assert page.ids["published-analytics-load-status"]["attrs"]["role"] == "status"
    assert page.ids["published-analytics-status"]["attrs"]["role"] == "status"
    assert page.ids["room-analytics-status"]["attrs"]["role"] == "status"
    scripts = [element for element in page.elements if element["tag"] == "script"]
    assert len(scripts) == 1 and scripts[0]["attrs"]["src"] == "/app.js"
    assert not content(scripts[0])
    assert "overflow-x: auto" in CSS
    assert ".analytics-chart-scroll" in CSS and ".analytics-manifest dl" in CSS
    assert "grid-template-columns: minmax(0, 1fr)" in CSS
    assert "font-variant-numeric: tabular-nums" in CSS
    assert "summary, a, pre, .controller-results, .analytics-chart-scroll):focus-visible" in CSS
    assert "@media (forced-colors: active)" in CSS
    assert "animation:" not in CSS and "transition:" not in CSS


def _contrast(foreground, background):
    def luminance(color):
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_declared_chart_and_legend_colors_have_measured_contrast():
    colors = re.findall(r"--analytics-series-\d: (#[a-fA-F0-9]{6});", CSS)
    colors += re.findall(r"--team-(?:bui|tom|harry): (#[a-fA-F0-9]{6});", CSS)
    assert len(colors) == 7
    for color in colors:
        # Small endpoint labels and legend text need 4.5:1, lines need 3:1.
        assert _contrast(color, "#070b10") >= 4.5
        assert _contrast(color, "#171c24") >= 4.5
    assert _contrast("#b7c6d4", "#070b10") >= 4.5
    assert _contrast("#526374", "#070b10") >= 3
    assert _contrast("#94d9f5", "#101923") >= 3


def test_dashboard_defaults_to_visible_graphs_and_moves_raw_tables_out_of_compare():
    assert 'showView("compare")' in SHELL
    assert 'id="dashboard-overview"' in SHELL
    assert 'id="lab-view-data"' in SHELL
    assert 'byId("lab-data-content").append(byId("flight-analytics"))' in SHELL
    assert (
        "table" not in SHELL.split('id="lab-view-compare"', 1)[1].split('id="lab-view-run"', 1)[0]
    )
    assert "STP points unavailable" in SHELL
    assert "View scoring formula" in SHELL
    assert 'if (!button.closest(".lab-navigation"))' in SHELL


def test_documented_formula_sources_and_legacy_limits_are_visible_in_data_view(page):
    assert "Raw Score = Time Score" in content(page.ids["controller-score-detail"])
    assert "Final points = 100" in content(page.ids["controller-score-detail"])
    assert "120/T" in content(page.ids["controller-score-detail"])
    assert "2D/209200" in content(page.ids["controller-score-detail"])
    assert "F/1.15" in content(page.ids["controller-score-detail"])
    assert "not collision count" in content(page.ids["controller-score-detail"])
    assert "Stats/FuelUsed" in content(page.ids["controller-score-detail"])
    assert "150" in content(page.ids["controller-score-sources"])
    assert "75" in content(page.ids["controller-score-sources"])
    assert "Legacy SI total, not STP" in content(page.ids["controller-results-table"])
    assert 'value="score"' not in (WEB / "index.html").read_text()
    assert "rankingScore" not in CONTROLLERS and "scoreOrder" not in CONTROLLERS
    assert "Raw score" in content(page.ids["controller-score-example"])
    assert "0.25" in content(page.ids["controller-score-example"])
    assert "Documentation example, not a recorded flight" in content(
        page.ids["score-example-heading"]
    )
    assert "STP raw score: n/a" in SHELL and "Final points: n/a" in SHELL
    assert 'link.target = "_blank"' in SOURCE
    assert 'link.rel = "noopener noreferrer"' in SOURCE
    assert "(opens in a new tab)" in SOURCE


def test_failed_publication_retry_does_not_destroy_the_overview_renderer():
    install = section("export function installFlightAnalytics()", "\nNOT_PRESENT")
    assert 'querySelector<HTMLElement>(".overview-empty")' in install
    assert 'byId("dashboard-overview").replaceChildren' not in install


def test_connection_headline_requires_native_evidence_and_updates_only_on_state_events(page):
    runtime = (WEB / "app-runtime.ts").read_text()
    monitor = runtime.split("function monitorLocalPythonConnection()", 1)[1].split(
        "function requestInitialFollowView()", 1
    )[0]
    assert "describeControllerStatus(connection, observation, runtime)" in monitor
    assert "JSON.parse(engine.observation())" in monitor
    assert "JSON.parse(engine.runtime_status())" in monitor
    assert "if (receipt.eventKey === previous) return;" in monitor
    assert "events.children.length > 5" in monitor
    assert "This does not mean a controller has started" not in monitor
    assert "connection.error}`" not in monitor
    for forbidden in (".control(", "fetch(", "controllers(", "console."):
        assert forbidden not in monitor
    assert page.ids["local-python-status"]["attrs"]["role"] == "status"
    assert "role" not in page.ids["local-python-evidence"]["attrs"]
    assert page.ids["local-controller-events"]["tag"] == "ol"
    assert "pointer-events: auto" in CSS.split(".connection-details {", 1)[1].split("}", 1)[0]


def test_fresh_confirmation_does_not_block_the_authenticated_page_lease(page):
    confirmation = page.ids["controller-fresh-confirm"]
    assert confirmation["tag"] == "dialog"
    assert confirmation["attrs"]["aria-describedby"] == "controller-fresh-chosen"
    assert any(
        element["tag"] == "form" and element["attrs"].get("method") == "dialog"
        for element in page.elements
        if confirmation in element["parents"]
    )
    assert "window.confirm(" not in CONTROLLERS
    assert "dialog.showModal()" in CONTROLLERS
    assert 'dialog.returnValue === "run"' in CONTROLLERS
