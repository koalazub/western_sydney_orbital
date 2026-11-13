"""Static HTML/source contracts only, not browser, GPU or interactive proof."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def test_game_tools_reserve_space_outside_the_native_canvas(page):
    """Source layout contract, with actual sizing checked separately in Chrome."""
    scene = page.ids["simulation-scene"]
    canvas = page.ids["simulation-canvas"]
    opener = page.ids["open-controllers"]
    footer = next(element for element in page.elements if element["tag"] == "footer")
    assert canvas["parents"][-1] is scene
    assert scene not in opener["parents"]
    assert footer in opener["parents"]
    for identity in ("local-python-connection", "python-room"):
        assert footer in page.ids[identity]["parents"]
        assert scene not in page.ids[identity]["parents"]
    for identity in ("quick-launch", "quick-profile", "quick-run", "quick-status"):
        assert footer in page.ids[identity]["parents"]
    assert "quick-stop" not in page.ids
    assert "grid-template-rows: minmax(0, 1fr) auto" in CSS
    tools = CSS.split(".simulation-tools {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in tools and "overflow-y: auto" in tools
    for selector in (".controller-toggle", ".quick-launch", ".python-room"):
        rule = CSS.split(selector + " {", 1)[1].split("}", 1)[0]
        assert "position: absolute" not in rule
        assert "inset-" not in rule and "z-index" not in rule
    runtime = (WEB / "app-runtime.ts").read_text()
    assert 'viewportResize.observe(byId("simulation-scene"))' in runtime


def test_table_headings_wrap_at_words_without_squeezing_numeric_columns():
    headings = CSS.split('.controller-results th[scope="col"] {', 1)[1].split("}", 1)[0]
    assert "min-width: 9rem" in headings and "max-width: 12rem" in headings
    assert "white-space: normal" in headings
    assert "word-break: normal" in headings and "overflow-wrap: normal" in headings
    assert "vertical-align: bottom" in headings
    tables = CSS.split(".controller-results table {", 1)[1].split("}", 1)[0]
    assert "width: max-content" in tables and "tabular-nums" in tables
    cells = CSS.split(".controller-results :is(th, td) {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in cells
    wrapper = CSS.split(".controller-results {", 1)[1].split("}", 1)[0]
    assert "overflow-x: auto" in wrapper


def test_single_run_is_an_explicit_read_only_selection_beside_aggregate():
    assert 'data-measurement-view="aggregate" aria-pressed="true">Aggregate' in SHELL
    assert 'data-measurement-view="run" aria-pressed="false">Single run' in SHELL
    assert 'id="overview-run" aria-describedby="overview-selection-status"' in SHELL
    assert 'id="overview-selection-status"' in SHELL
    assert "matching.filter(run => run.id === runChoice.value)" in SOURCE
    assert "aggregate ? selectedProfiles : undefined" in SOURCE
    assert 'runChoice.addEventListener("change", published.render)' in SOURCE
    assert "One recorded flight, with no averaging across runs." in SOURCE
    assert "Missing native analytics stay unavailable." in SOURCE
    assert "not summed fuel" in SOURCE
    assert "${run.id} · ${run.label}" in SOURCE
    assert "summary.textContent = run ? `Seed ${run.seed} · ${run.id} · ${run.outcome}" in SOURCE
    assert 'new Option("All recorded seeds", "all")' in SOURCE
    assert 'seedChoice.disabled = measurementView === "aggregate"' in SOURCE
    assert 'new Option(`Seed ${run.seed} · rep ${run.repetition ?? "n/a"}' in SOURCE
    legend = CSS.split(".lab-measurement-view legend {", 1)[1].split("}", 1)[0]
    assert "float:" not in legend


def test_fuel_explanation_names_the_actual_single_run_and_counter_mismatch(page):
    notes = page.ids["fuel-model-notes"]
    assert notes["tag"] == "details"
    assert '"fuel-model-notes"' in SHELL
    text = content(notes)
    assert "trial-00004" in text and "84.996 kg" in text
    assert "one flight, not fuel summed across the cohort" in text
    assert "uses timestep, vehicle mass and per-jet impulse" in text
    assert "resultant translational force" in text
    assert "Direct angular torque adds no extra local fuel" in text
    assert "recorded local fuel directly" in text
    assert "local estimates, not official STP scores" in text


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
    assert (
        'owner.addEventListener("change", () => { selectOwner(); '
        "quickProfile.value = profile.value; void changedProfile(); })" in CONTROLLERS
    )
    assert (
        'profile.addEventListener("change", () => { quickProfile.value = profile.value; '
        "void changedProfile(); })" in CONTROLLERS
    )
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
        "../../src/wsorb/controller_worlds.json",
        "../../simulation/scenarios/flight_analytics.json",
        "../shared/controller-jobs.ts",
        "../../src/wsorb/scoring_reference.json",
        "./analytics-overview.ts",
        "./chart-inspector.ts",
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
    assert "ownerStyle[owner]" in graph
    assert "index % 7" not in graph
    assert 'selectedSeeds.join(", ")' in graph
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
    assert "Compare flights shows current-formula local point estimates" in content(
        page.ids["published-scoreboard-note"]
    )
    assert "preserves measurements and legacy diagnostics" in content(
        page.ids["published-scoreboard-note"]
    )
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
        "/analytics/official-comparison.svg",
        "/analytics/paths-official.json",
        "/analytics/paths-official-trials.csv",
    }
    for link in links:
        assert link["attrs"]["referrerpolicy"] == "no-referrer"
        if "download" in link["attrs"]:
            if link["attrs"]["href"] in {
                "/analytics/paths-official.json",
                "/analytics/paths-official-trials.csv",
            }:
                assert any(
                    parent["tag"] == "nav"
                    and parent["attrs"].get("aria-label") == "Official-profile comparison evidence"
                    for parent in link["parents"]
                )
                assert any(
                    parent["tag"] == "section"
                    and parent["attrs"].get("aria-labelledby") == "official-comparison-heading"
                    for parent in link["parents"]
                )
            else:
                assert page.ids["published-analytics-content"] in link["parents"]
        else:
            assert link["attrs"]["href"] == "/analytics/official-comparison.svg"
            assert link["attrs"]["target"] == "_blank"
            assert set(link["attrs"]["rel"].split()) == {"noopener", "noreferrer"}
        assert "?" not in link["attrs"]["href"] and "#" not in link["attrs"]["href"]


def test_result_hook_preserves_existing_consent_poll_pause_and_focus_behavior(page):
    assert "disabled" in page.ids["run-controller"]["attrs"]
    assert "disabled" in page.ids["quick-run"]["attrs"]
    assert "run.disabled = true;" in CONTROLLERS
    assert (
        "run.disabled = busy || !current?.service.online || current.service.busy "
        "|| !!active || !options.ready();"
    ) in CONTROLLERS
    assert "const analytics = installFlightAnalytics();" in CONTROLLERS
    assert CONTROLLERS.count("analytics.updateRoomResults(view.results);") == 1
    unavailable = CONTROLLERS.split("function unavailableResults()", 1)[1].split(
        "const request =", 1
    )[0]
    assert 'dataState = savedCount === undefined ? "unavailable" : "stale";' in unavailable
    assert "renderDataStatus();" in unavailable
    assert "view = undefined;" in unavailable
    assert "renderRunnerControls();" in unavailable
    for destructive_update in (
        "analytics.updateRoomResults(",
        "renderResults(",
        "table.replaceChildren(",
    ):
        assert destructive_update not in unavailable
    assert re.search(r"\b(?:savedCount|savedSource)\s*=(?!=)", unavailable) is None
    data_status = CONTROLLERS.split("function renderDataStatus()", 1)[1].split(
        "function unavailableResults()", 1
    )[0]
    for retained_status in (
        "Connection data stale. Showing the last successful receipts and traces.",
        "Previous connection data retained, not current.",
        "Check the Python connection, then use Refresh results.",
        "Refresh only reads saved data.",
        '"controller-results-status", "controller-recent-data-status"',
        "target.dataset.dataState = displayedState;",
    ):
        assert retained_status in data_status
    refresh = CONTROLLERS.split("async function refresh()", 1)[1].split(
        "async function requestStop()", 1
    )[0]
    assert "const value = await request()();" in refresh
    assert refresh.count("if (version !== refreshVersion) return;") == 2
    assert refresh.index("if (version !== refreshVersion) return;") < refresh.index(
        "render(value, source);"
    )
    assert "unavailableResults();" in refresh
    accepted = CONTROLLERS.split(
        "function render(value: Record<string, unknown>, source: ResultsSource)", 1
    )[1].split('byId("controller-score-label")', 1)[0]
    for recovered_data in (
        "if (!sameSource(source, resultsSource()))",
        'typeof value.service.online !== "boolean"',
        'typeof value.service.busy !== "boolean"',
        "renderResults(view.results);",
        "analytics.updateRoomResults(view.results);",
        "savedSource = source;",
        "savedCount = view.results.length;",
        'dataState = "loaded";',
        "renderDataStatus();",
    ):
        assert recovered_data in accepted
    for preserved in (
        "before.time_s > 0 && !await confirmFreshTrial(label(chosen.profileId), chosen)",
        "const expected = await freshTrial(api, chosen, signal);",
        "const changedProfile = async () => { description(); await requestStop(); };",
        "if (disposed || document.hidden || !autoUpdate",
        "|| !panel.open && !confirmedView()?.active && !resultsSource()) return;",
        'byId("pause-controller-updates").addEventListener("click", toggleUpdates);',
        "panel.querySelector<HTMLButtonElement>('.lab-navigation "
        '[data-lab-view="run"]\')?.focus();',
        "target.focus({ preventScroll: true });",
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


def test_dashboard_opens_profiles_and_keeps_recorded_data_separate():
    assert 'showView("run")' in SHELL
    assert 'dashboard.showView("run")' in CONTROLLERS
    assert 'id="lab-view-run" class="lab-view" aria-label="Test Python profiles"' in SHELL
    assert 'id="lab-view-compare" class="lab-view" aria-label="Compare flights" hidden' in SHELL
    assert 'id="dashboard-overview"' in SHELL
    assert 'id="lab-view-data"' in SHELL
    assert 'byId("lab-data-content").append(byId("flight-analytics"))' in SHELL
    assert (
        "table" not in SHELL.split('id="lab-view-compare"', 1)[1].split('id="lab-view-run"', 1)[0]
    )
    assert "Points use the current STP formula" in SHELL
    assert "Local estimates appear in Compare and connection results" in SHELL
    assert "View scoring formula" in SHELL
    assert 'if (!button.closest(".lab-navigation"))' in SHELL


def test_profile_controls_use_catalog_owners_and_actual_python_queue_after_consent(page):
    assert content(page.ids["open-controllers"]) == "More options"
    assert content(page.ids["quick-run"]) == "Run flight"
    assert content(page.ids["run-controller"]) == "Run flight"
    assert content(page.ids["watch-controller-flight"]) == "Watch flight"
    assert "new Set(profiles.map(entry => entry.owner))" in CONTROLLERS
    assert (
        'chooseQuickProfile(profiles.some(entry => entry.id === "ali_mppi") '
        '? "ali_mppi" : profiles[0].id);' in CONTROLLERS
    )
    assert 'quickProfile.addEventListener("change", () => {' in CONTROLLERS
    assert "profiles.filter(entry => entry.owner === owner.value)" in CONTROLLERS
    assert "Number(b.target_seeking) - Number(a.target_seeking)" in CONTROLLERS
    for identity in (
        "controller-profile-source",
        "controller-profile-id",
        "controller-profile-contributor",
        "controller-profile-kind",
        "controller-run-reason",
    ):
        assert identity in page.ids
    submit = CONTROLLERS.split("async function startRun() {", 1)[1].split("function schedule()", 1)[
        0
    ]
    ordered = [
        "const chosen = selection();",
        "before.time_s > 0 && !await confirmFreshTrial(label(chosen.profileId), chosen)",
        "const send = request();",
        "render(await send(), source);",
        "const current = confirmedView();",
        "if (!current?.service.online || current.service.busy || "
        "current.active || !options.ready())",
        "const expected = await freshTrial(api, chosen, signal);",
        'op: "view", mode: "orbit", focus: "Shuttle"',
        'action: "submit", profile_id: chosen.profileId, world_id: chosen.worldId',
        'panel.close("watch");',
    ]
    assert [submit.index(part) for part in ordered] == sorted(
        submit.index(part) for part in ordered
    )
    assert "owner.disabled = profile.disabled = world.disabled = seed.disabled" in submit
    assert "if (busy || run.disabled) return;" in submit
    assert "fresh: true, expected_context: expected" in submit
    assert "seed: chosen.seed" in submit and "duration_s: chosen.duration" in submit
    launcher = (WEB / "flight-launch.ts").read_text()
    assert 'op: "configure_flight", preset: world.preset' in launcher
    assert "seed: selection.seed, settings: world.settings" in launcher
    assert "sameData(nativeSettings(actual.experiment_settings), world.settings)" in launcher
    assert "controller-world" in page.ids and "controller-seed" in page.ids
    assert "Set up Python runner" in content(page.ids["setup-controller"])
    assert page.ids["controller-setup"]["tag"] == "details"
    assert "open" not in page.ids["controller-setup"]["attrs"]
    setup = content(page.ids["controller-setup"])
    assert "Python runs on a teammate's machine" in setup
    assert "Cloudflare Free" in setup and "does not start or host Python" in setup
    for command in (
        "devenv shell",
        "WSORB_LAB=1 just local",
        "uv run --locked wsorb controllers serve --connection-file .devenv/wsorb-local.json --run",
        "uv run --locked wsorb room create --connection-file .devenv/wsorb-room.json",
        "uv run --locked wsorb controllers serve --room-file .devenv/wsorb-room.json --run",
    ):
        assert command in setup
    assert "A comparison link only selects flight settings" in setup
    assert "ready game and an online, idle Python service" in setup
    assert (
        'if (!api) throw new Error("The real engine is unavailable. No run was started.")' in submit
    )
    for forbidden in ("eval(", "new Function(", "python-worker", "create_controller("):
        assert forbidden not in CONTROLLERS
    runtime = (WEB / "app-runtime.ts").read_text()
    for removed in ("prepareHosted", "automaticHosted", "startHostedRunner", "createFlightRoom"):
        assert removed not in CONTROLLERS and removed not in runtime
    assert not (WEB / "hosted-runner.ts").exists()
    assert 'permission.returnValue !== "allow"' in runtime
    assert 'permission.returnValue = "";' in runtime
    assert "connectPythonRoom(connection)" in runtime
    assert "room: () => roomConnected ? roomConnection : null" in runtime
    assert "Python runs on your machine, not this site" in content(page.ids["controller-scope"])
    assert "This site does not host Python" in SHELL


def test_watched_job_keeps_stop_outside_modal_without_claiming_native_running(page):
    flight = page.ids["controller-flight"]
    assert "hidden" in flight["attrs"]
    assert flight not in page.ids["simulation-canvas"]["parents"]
    assert page.ids["controller-panel"] not in flight["parents"]
    assert page.ids["controller-flight-status"]["attrs"]["role"] == "status"
    assert page.ids["controller-flight-error"]["attrs"]["role"] == "alert"
    outside_stop = page.ids["stop-controller-flight"]
    assert outside_stop["tag"] == "button" and "disabled" in outside_stop["attrs"]
    assert outside_stop["attrs"]["aria-describedby"] == "controller-flight-status"
    assert "actual positions, not a predicted route" in content(flight)
    assert "pause-controller-flight-updates" in page.ids
    assert 'outsideStop.addEventListener("click", () => { void requestStop(); });' in CONTROLLERS
    assert (
        'watch.addEventListener("click", () => { if (!watch.disabled) panel.close("watch"); });'
        in CONTROLLERS
    )
    assert (
        'panel.returnValue === "watch" ? byId("simulation-canvas") : openingControl' in CONTROLLERS
    )
    assert (
        "stop.disabled = outsideStop.disabled = starting ? starting.signal.aborted : "
        "busy || !active || active.stop_requested;" in CONTROLLERS
    )
    assert "const current = confirmedView();" in CONTROLLERS
    assert "sameSource(viewSource, resultsSource()) && sourceAvailable(viewSource)" in CONTROLLERS
    stop = CONTROLLERS.split("async function requestStop()", 1)[1].split("const changedProfile", 1)[
        0
    ]
    assert "if (!current?.active || busy) return;" in stop
    assert 'action: "stop", job_id: current.active.id' in stop
    assert (
        "Stop was not confirmed. Inspect the live flight and refresh before another action." in stop
    )
    assert "api.control(" not in stop
    assert "starting.abort();" in stop
    assert 'confirmation.close("cancel")' in stop
    submit = CONTROLLERS.split("async function startRun() {", 1)[1].split("function schedule()", 1)[
        0
    ]
    assert 'stop.textContent = outsideStop.textContent = starting ? "Cancel startup"' in CONTROLLERS
    assert "const signal = starting.signal;" in submit
    assert "if (signal.aborted && record(submitted.active)" in submit
    assert 'action: "stop", job_id: submitted.active.id' in submit
    assert submit.count('action: "submit"') == 1
    assert "controller-flight-measurements" in page.ids
    presenter = CONTROLLERS.split("function renderRunnerControls()", 1)[1].split(
        "function renderDataStatus()", 1
    )[0]
    assert "Last confirmed profile. Status unavailable" in presenter
    assert "last confirmed, updates paused" in presenter
    assert "Controller running" not in presenter


def test_current_formula_and_separate_legacy_receipts_are_visible_in_data_view(page):
    detail = content(page.ids["controller-score-detail"])
    assert "Success = 100" in detail and "Damage failure = 100" in detail
    assert "120/T" in detail and "2D/209200" in detail
    assert "max(F/75, 0)" in detail
    assert "recorded initial target range" in detail
    assert "At T = 0, the time term is 0" in detail
    assert "negative and are not capped at 100" in detail
    assert "recorded local measurements, not official STP counters" in detail
    sources = page.ids["controller-score-sources"]
    assert any(
        element["tag"] == "a"
        and sources in element["parents"]
        and "c010564824c48488104e648781d5e987b402e47a/Systems/Scoring.py"
        in element["attrs"].get("href", "")
        for element in page.elements
    )
    source_text = content(sources)
    assert "actual time-zero target range from validated native analytics" in source_text
    assert "Running, time-budget, stopped, interrupted and error results stay n/a" in source_text
    table = page.ids["controller-results-table"]
    headings = [
        content(element)
        for element in page.elements
        if element["tag"] == "th" and table in element["parents"]
    ]
    assert headings[:2] == ["Teammate / profile", "Points · pts"]
    assert "Legacy SI total, not STP" not in content(table)
    assert "Legacy SI total, not STP" in content(page.ids["controller-legacy-results-table"])
    details = page.ids["controller-result-details"]
    assert details["tag"] == "details" and "open" not in details["attrs"]
    assert details in page.ids["controller-legacy-results-table"]["parents"]
    assert details in page.ids["controller-score-breakdown-table"]["parents"]
    assert "Original numeric precision" in content(page.ids["controller-score-breakdown-table"])
    assert 'value="score"' not in (WEB / "index.html").read_text()
    assert "rankingScore" not in CONTROLLERS and "scoreOrder" not in CONTROLLERS
    assert "Raw score" in content(page.ids["controller-score-example"])
    assert "0.25" in content(page.ids["controller-score-example"])
    assert "Historical documentation example, not a flight" in content(
        page.ids["score-example-heading"]
    )
    assert "STP raw score: n/a" not in SHELL and "Final points: n/a" not in SHELL
    assert 'link.target = "_blank"' in SOURCE
    assert 'link.rel = "noopener noreferrer"' in SOURCE
    assert "(opens in a new tab)" in SOURCE


def test_real_profile_score_reduction_requires_terminal_context_and_recorded_failure_range():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the actual profile score reduction")
    subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            r"""
            import assert from 'node:assert/strict';
            import {readFileSync} from 'node:fs';
            import {stripTypeScriptTypes} from 'node:module';
            import {runInNewContext} from 'node:vm';
            import {estimateScore} from './worker/web/score-estimate.ts';
            import * as jobs from './worker/shared/controller-jobs.ts';
            const source=readFileSync('worker/web/controller-profiles.ts','utf8');
            // Execute the real pure receipt reduction, without installing a DOM,
            // engine, transport or a substitute controller implementation.
            const pure=source.slice(source.indexOf('const record ='),
              source.indexOf('const numericScore ='));
            assert(pure.includes('function currentScore('));
            const score=runInNewContext(stripTypeScriptTypes(pure)+'\ncurrentScore',
              {estimateScore,...jobs});
            const receipt={state:'native_terminal',native_success:true,error_code:null,
              world_metadata_unchanged:true,analytics:null,
              measurement:{status:'success',time_s:80,damage_j:0,fuel_used_kg:18.75,
                range_m:50,score:-999,score_breakdown:null}};
            const before=JSON.stringify(receipt);
            assert.equal(score(receipt).total,175,'Current local estimate ignores the legacy -999');
            assert.equal(JSON.stringify(receipt),before,'The receipt stays unchanged');
            for(const state of ['stopped','duration_limit','wall_budget_limit',
              'interrupted','error'])
              assert.equal(score({...receipt,state}),null,
                'A late success observation is not terminal consent');
            for(const field of ['world_metadata_unchanged','native_success'])
              for(const value of [false,null,undefined])
                assert.equal(score({...receipt,[field]:value}),null);
            for(const error_code of ['controller_failed','interrupted',undefined])
              assert.equal(score({...receipt,error_code}),null);
            for(const key of ['time_s','damage_j','fuel_used_kg'])
              assert.equal(score({...receipt,measurement:{...receipt.measurement,[key]:null}}),null);
            const failure={...receipt,native_success:false,measurement:{...receipt.measurement,
              status:'failed_damage',time_s:120,damage_j:313800,fuel_used_kg:75,range_m:5000}};
            assert.equal(score(failure),null,
              'Neither the current world nor a legacy score supplies r0');
            failure.analytics={schema_version:1,definition_id:jobs.CONTROLLER_ANALYTICS_DEFINITION,
              source_kind:'native_physics_step_counters_with_sampled_public_series',
              columns:[...jobs.CONTROLLER_ANALYTICS_COLUMNS],sample_count:2,
              totals:{schema_version:1,definition_id:jobs.CONTROLLER_ANALYTICS_DEFINITION,observed_sim_s:120},
              samples:[[0,0,0,0,0,0,0,10000,0],[120,75,0,0,0,0,1,5000,313800]]};
            assert.equal(score(failure).total,-400,'Failure uses its recorded time-zero range');
            for(const mutate of [
              x=>x.analytics.samples[0][0]=.1,
              x=>x.analytics.samples[0][7]=null,
              x=>x.analytics.samples[0][7]=0,
              x=>x.analytics.samples[1][0]=0,
              x=>x.analytics.columns.reverse(),
              x=>x.analytics.definition_id='unknown',
              x=>x.analytics.source_kind='unverified',
              x=>x.analytics.schema_version=2,
              x=>x.analytics.sample_count=1,
              x=>x.analytics.totals.observed_sim_s=0,
              x=>x.analytics.totals.definition_id='unknown',
              x=>x.analytics.samples=Array.from({length:49},(_,i)=>[i,0,0,0,0,0,0,10000,0]),
              x=>x.world_metadata_unchanged=false,
              x=>x.native_success=true,
              x=>x.error_code='controller_failed',
            ]){
              const invalid=structuredClone(failure);mutate(invalid);
              assert.equal(score(invalid),null,
                'Invalid or unrelated analytics cannot establish r0');
            }
            """,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


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


def test_custom_metric_charts_share_actual_sample_inspection_and_reading_guidance():
    graph = section("function renderGraph(", "function installPlot(")
    assert "customInspections.get(prefix)?.()" in graph
    assert "installChartInspector(container, svg, inspection)" in graph
    assert 'label: "Time", value: row[0]' in graph
    assert "label: metric.label, value: row[metric.column]" in graph
    assert '"data-chart-inspection-series": run.id' in graph
    assert "metric.guidance" in graph
    assert "Compare engine-on time with fuel and damage after arrival." in SOURCE
    assert 'svg.setAttribute("aria-labelledby", `${prefix}-plot-title`)' in graph
    assert 'svg.setAttribute("aria-describedby", `${prefix}-plot-description`)' in graph
    assert 'selectionSummary.textContent = "Read selection notes"' in SOURCE
    assert "selectionNotes.open" not in SOURCE
    assert (
        'run.analytics ? `${run.analytics.sample_count} samples` : "samples unavailable"' in SOURCE
    )
    assert '.metric-chart[tabindex="0"]:focus-visible' in CSS
