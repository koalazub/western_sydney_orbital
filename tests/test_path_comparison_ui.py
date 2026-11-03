"""Pure browser-data and source contracts; Chrome review is a separate recipe."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "worker/web"

MODEL_FIXTURES = """
    import assert from 'node:assert/strict';
    import {readFileSync} from 'node:fs';
    import {createHash} from 'node:crypto';
    import * as model from './worker/web/comparison-model.ts';
    import {validatePathComparison} from './worker/web/path-comparison-data.ts';
    const raw=readFileSync('docs/analytics/path-comparison/official.json');
    const digest=createHash('sha256').update(raw).digest('hex');
    const saved=validatePathComparison(JSON.parse(raw));
    assert(saved);
    const state=(data,extra={})=>({data,supplement:null,mode:'cohort',
      seed:data.comparison.seeds[0],repetition:1,baseline:'wso_official_profile',
      selectedIds:[],anchorId:null,outcome:'all',xMetric:'fuel_kg',yMetric:'time_s',
      visibleMetrics:['time_s','fuel_kg'],cursor:0,interval:null,alignment:'time',
      difference:false,mapScale:'detail',...extra});
    const supplement=data=>({schema:'wsorb.comparison_metrics',schema_version:1,id:data.id,
      source:{path_sha256:digest,engine_sha256:data.comparison.engine_sha256,
        step_s:data.comparison.step_s},limitations:[...model.METRIC_LIMITATIONS],
      runs:data.runs.map(run=>({id:run.id,algorithm:run.algorithm,seed:run.seed,
        repetition:run.repetition,outcome:run.outcome,
        engine_sha256:data.comparison.engine_sha256,
        source_sha256:{...run.source_sha256},analytics:null,policy:null}))});
    // A small DTO fixture joined to actual path readings, not flight evidence.
    const analytics=run=>({schema_version:1,
      definition_id:'native_actuation_rotation_episodes_v1',
      source_kind:'native_physics_step_counters_with_sampled_public_series',
      totals:{schema_version:1,definition_id:'native_actuation_rotation_episodes_v1',
        ...Object.fromEntries([...model.NATIVE_COUNTER_KEYS,...model.NATIVE_NUMBER_KEYS]
          .map(key=>[key,key==='observed_sim_s'?run.final.time_s:0])),
        ...Object.fromEntries(model.NATIVE_FLAG_KEYS.map(key=>[key,false]))},
      columns:[...model.NATIVE_COLUMNS],sample_count:2,
      samples:[run.samples[0],run.samples.at(-1)].map(row=>
        [row[0],row[6],0,0,0,0,row[5],row[4],row[7]])});
"""

SCORE_FIXTURES = """
    // Small counter documents for reductions only, not replacement flight data.
    const scoreRun=(algorithm,seed,repetition,status,time,fuel,damage=0,range=50)=>{
      const run=structuredClone(saved.runs[0]);
      Object.assign(run,{id:`${algorithm}-${seed}-${repetition}`,seed,repetition,
        algorithm:algorithm==='candidate'?'basic_pd_radar':'official_basic_beeline',
        outcome:status,label:algorithm});
      Object.assign(run.final,{time_s:time,fuel_kg:fuel,damage_j:damage,range_m:range});
      run.samples[0][0]=0;run.samples[0][4]=10000;
      return run;
    };
    const scoreData={...structuredClone(saved),comparison:{...saved.comparison,
      seeds:[100,101,102],repetitions:2,route_length_m:987654},runs:[
      scoreRun('candidate',100,1,'success',80,18.75),
      scoreRun('candidate',100,2,'failed_damage',120,37.5,313800,5000),
      scoreRun('candidate',101,1,'success',120,75),
      scoreRun('candidate',101,2,'success',60,0),
      scoreRun('candidate',102,1,'success',40,0),
      scoreRun('candidate',102,2,'policy_error',1,0),
      scoreRun('reference',100,1,'success',120,75),
      scoreRun('reference',100,2,'success',120,75),
      scoreRun('reference',101,1,'success',80,0),
      scoreRun('reference',101,2,'success',80,0),
      scoreRun('reference',102,1,'success',120,75),
      scoreRun('reference',102,2,'success',120,75),
    ]};
    const scoreState=(extra={})=>state(scoreData,{baseline:'official_basic_beeline',...extra});
    const scoreRow=(extra={})=>model.rowsForState(scoreState(extra))
      .find(row=>row.label==='candidate');
    const assertReconciles=value=>assert(Math.abs(value.total-(value.progress_points
      -value.collision_penalty_points-value.fuel_penalty_points+value.offset_points))<1e-10);
"""


def node_assertions(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the real TypeScript parser")
    subprocess.run(
        [node, "--input-type=module", "-e", source], check=True, cwd=ROOT, capture_output=True
    )


def test_real_browser_parser_accepts_both_committed_worlds_and_rejects_mixed_data():
    node_assertions("""
        import assert from 'node:assert/strict';
        import {readFileSync} from 'node:fs';
        import {validatePathComparison,pairedRuns,timeline,sampleAtTime,
          segments,traceStyle,selectedCSV}
          from './worker/web/path-comparison-data.ts';
        for(const id of ['official','team']) {
          const data=JSON.parse(readFileSync(`docs/analytics/path-comparison/${id}.json`));
          assert(validatePathComparison(data));
          const runs=pairedRuns(data,100,1);
          assert.equal(runs.length,id==='official'?5:3);
          assert(runs.every(run=>run.seed===100&&run.repetition===1));
          const numbers=new Set(runs.map(run=>traceStyle(run.algorithm).number));
          assert.equal(numbers.size,runs.length);
          assert(timeline(runs).length>1);
          assert(selectedCSV(runs).includes('repetition'));
          const copy=structuredClone(data);copy.runs.push(copy.runs[0]);
          assert.equal(validatePathComparison(copy),null);
          const world=structuredClone(data);world.comparison.distribution='transit_stream';
          assert.equal(validatePathComparison(world),null);
          const time=structuredClone(data);time.runs[0].samples[1][0]=-1;
          assert.equal(validatePathComparison(time),null);
          const final=structuredClone(data);final.runs[0].final.fuel_kg=999999;
          assert.equal(validatePathComparison(final),null);
          const privacy=structuredClone(data);privacy.runs[0].token='private';
          assert.equal(validatePathComparison(privacy),null);
          const missing=structuredClone(data);missing.runs.pop();
          assert.equal(validatePathComparison(missing),null);
        }
        const run={samples:[[0,1],[1,null],[2,3]]};
        assert.equal(sampleAtTime(run,.5),null);
        assert.equal(sampleAtTime(run,3),null);
        assert.deepEqual(segments(run.samples,0,1),[[[0,1]],[[2,3]]]);
        const repeated=[{samples:[[0],[.5]]},{samples:[[0],[.50000000001],[1]]}];
        assert.deepEqual(timeline(repeated),[0,.5,1]);
    """)


def test_current_score_algebra_uses_75_kg_and_keeps_components_in_points():
    node_assertions("""
        import assert from 'node:assert/strict';
        import {estimateScore} from './worker/web/score-estimate.ts';
        // Hypothetical algebra fixtures, not a teammate or licensed flight.
        const success={status:'success',time_s:80,damage_j:0,fuel_used_kg:18.75,
          range_m:50,initial_range_m:10000};
        const before=JSON.stringify(success),result=estimateScore(success);
        assert.deepEqual(result,{total:175,progress_points:150,
          collision_penalty_points:0,fuel_penalty_points:25,offset_points:50,
          progress_basis:'time_ratio'});
        assert.equal(JSON.stringify(success),before,'Scoring is a read-only reduction');
        assert.equal(estimateScore({...success,time_s:120,fuel_used_kg:75}).total,50);
        assert.equal(estimateScore({...success,time_s:0,fuel_used_kg:0}).total,50,
          'The source gives no time bonus at exactly zero');
        assert.equal(estimateScore({...success,time_s:120,damage_j:209200,
          fuel_used_kg:75}).total,-150,'Damage equality still permits success');
        assert.equal(estimateScore({...success,time_s:120,fuel_used_kg:300}).total,-250,
          'Scores and fuel penalties are not clamped to a positive range');
        const failure={...success,status:'failed_damage',time_s:120,
          damage_j:313800,fuel_used_kg:37.5,range_m:5000};
        assert.deepEqual(estimateScore(failure),{total:-350,progress_points:-50,
          collision_penalty_points:300,fuel_penalty_points:50,offset_points:50,
          progress_basis:'remaining_distance_ratio'});
        assert.equal(estimateScore({...failure,initial_range_m:20000}).total,-325,
          'Failure uses its supplied initial range, not a fixed mission distance');
        assert.equal(estimateScore({...failure,fuel_used_kg:75}).total,-400);
        assert.equal(estimateScore({...failure,range_m:50}).total,-300.5,
          'A damage failure near the target must not acquire a success time bonus');
        for(const input of [success,failure]){
          const value=estimateScore(input);
          assert.equal(value.total,value.progress_points-value.collision_penalty_points
            -value.fuel_penalty_points+value.offset_points);
          value.total=999;
          assert.notEqual(estimateScore(input).total,999,'Returned values are detached');
        }
    """)


def test_score_estimates_reject_nonterminal_missing_invalid_and_contradictory_inputs():
    node_assertions("""
        import assert from 'node:assert/strict';
        import {estimateScore} from './worker/web/score-estimate.ts';
        const valid={status:'success',time_s:80,damage_j:0,fuel_used_kg:18.75,
          range_m:50,initial_range_m:10000};
        for(const status of ['running','duration_limit','wall_budget_limit','timeout',
          'interrupted','stopped','error','policy_error','native_terminal','failed',
          '',null,undefined,true])assert.equal(estimateScore({...valid,status}),null);
        for(const key of ['time_s','damage_j','fuel_used_kg']){
          const missing={...valid};delete missing[key];
          assert.equal(estimateScore(missing),null,`Missing ${key} is not zero`);
          for(const value of [null,true,false,'0',NaN,Infinity,-Infinity,-.001])
            assert.equal(estimateScore({...valid,[key]:value}),null,`${key}: ${value}`);
        }
        for(const key of ['range_m','initial_range_m'])
          for(const value of [true,'50',NaN,Infinity,-1])
            assert.equal(estimateScore({...valid,[key]:value}),null);
        assert.equal(estimateScore({...valid,range_m:50.001}),null);
        assert.equal(estimateScore({...valid,damage_j:209200.001}),null);
        assert.equal(estimateScore({...valid,time_s:Number.MIN_VALUE}),null,
          'Overflow is unavailable, not Infinity');
        const failure={...valid,status:'failed_damage',damage_j:313800,range_m:5000};
        for(const key of ['range_m','initial_range_m']){
          const missing={...failure};delete missing[key];
          assert.equal(estimateScore(missing),null);
          assert.equal(estimateScore({...failure,[key]:null}),null);
        }
        assert.equal(estimateScore({...failure,initial_range_m:0}),null);
        assert.equal(estimateScore({...failure,damage_j:209200}),null);
        assert.equal(estimateScore({...failure,initial_range_m:Number.MIN_VALUE}),null);
        assert.equal(estimateScore(null),null);
    """)


def test_score_summary_averages_complete_same_seed_pairs_not_counters_or_arrivals():
    node_assertions(
        MODEL_FIXTURES
        + SCORE_FIXTURES
        + """
        const before=JSON.stringify(scoreData),row=scoreRow();
        assert.equal(row.attempts,6);assert.equal(row.arrivals,4);
        assert.equal(row.score.aggregation,'seed_pair_mean');
        assert.equal(row.score.eligibleRuns,6);assert.equal(row.score.eligibleSeeds,3);
        assert.equal(row.score.readings.length,5,'An error is unavailable, not zero');
        assert.deepEqual(row.score.seedMeans.map(pair=>[pair.seed,pair.components.total]),
          [[100,-87.5],[101,150]],'The mixed success/failure pair is included');
        assert.deepEqual(row.score.contributingRunIds,
          ['candidate-100-1','candidate-100-2','candidate-101-1','candidate-101-2']);
        assert.equal(row.values.score_points,31.25,
          'Mean of complete pair means, not 95 from all five available run scores');
        assert.equal(row.values.time_s,70,'Other efficiency summaries remain arrival medians');
        assert.equal(row.values.fuel_kg,9.375);
        assert.notEqual(row.values.score_points,100*(120/70-9.375/75+.5),
          'Formula evaluation must precede aggregation, never use median counters');
        assert.deepEqual(row.score.components,{total:31.25,progress_points:100,
          collision_penalty_points:75,fuel_penalty_points:43.75,offset_points:50});
        for(const [metric,field] of Object.entries(model.SCORE_METRIC_FIELDS)){
          assert.equal(row.values[metric],row.score.components[field]);
          assert.equal(row.counts[metric],5);
          assert.deepEqual(row.coverage[metric].readings.map(reading=>reading.runId),
            row.score.readings.map(reading=>reading.runId));
        }
        assertReconciles(row.score.components);
        assert.equal(model.isEfficiencyMetric('score_points'),false,
          'The arrival-only efficiency filter must not classify the score population');
        assert.equal(JSON.stringify(scoreData),before,'No archived counter is rewritten');
        const trial=scoreRow({mode:'trials',seed:100,repetition:2});
        assert.equal(trial.values.score_points,-350);
        assert.equal(trial.deltas.score_points,-400,
          'A terminal damage failure has a real matched score delta');
        assert.equal(trial.deltas.fuel_kg,null,
          'Existing efficiency deltas still require two arrivals');
        assert.equal(trial.score.aggregation,'trial');assertReconciles(trial.score.components);
    """
    )


def test_score_delta_uses_only_complete_matched_candidate_and_baseline_pairs():
    node_assertions(
        MODEL_FIXTURES
        + SCORE_FIXTURES
        + """
        const row=scoreRow(),reference=model.rowsForState(scoreState())
          .find(item=>item.label==='reference');
        assert(Math.abs(reference.values.score_points-100)<1e-10);
        assert.equal(row.deltas.score_points,-93.75);
        assert.notEqual(row.deltas.score_points,row.values.score_points-reference.values.score_points,
          'A reference-only seed must not enter the paired difference population');
        assert.deepEqual(row.score.matchedSeedMeans.map(pair=>[pair.seed,pair.components.total]),
          [[100,-137.5],[101,-50]]);
        assert.equal(row.score.pairs.length,4);
        assert.deepEqual(row.score.matchedSeedMeans[0].baselineIds,
          ['reference-100-1','reference-100-2']);
        for(const [metric,field] of Object.entries(model.SCORE_METRIC_FIELDS)){
          assert.equal(row.deltas[metric],row.score.deltas[field]);
          assert.equal(row.coverage[metric].pairs.length,4);
          assert.equal(row.coverage[metric].pairedEligible,6);
        }
        assertReconciles(row.score.deltas);
        scoreData.runs=scoreData.runs.filter(run=>run.id!=='reference-100-2');
        const missing=scoreRow();
        assert.equal(missing.values.score_points,31.25,'Reference gaps do not alter own values');
        assert.equal(missing.deltas.score_points,-50);
        assert.deepEqual(missing.score.matchedSeedMeans.map(pair=>pair.seed),[101]);
        assert.equal(missing.score.pairs.length,2,'One counterpart is not a complete paired seed');
        assert.equal(missing.coverage.score_points.pairedEligible,6);
    """
    )


def test_score_pair_gaps_do_not_borrow_a_different_repetition_seed_or_initial_range():
    node_assertions(
        MODEL_FIXTURES
        + SCORE_FIXTURES
        + """
        const failure=scoreData.runs.find(run=>run.id==='candidate-100-2');
        assert.equal(model.scoreForRun(failure).total,-350,
          'The actual time-zero range is 10000, not the geometry route length 987654');
        failure.samples[0][4]=20000;
        assert.equal(model.scoreForRun(failure).total,-325);
        failure.samples[0][4]=null;
        assert.equal(model.scoreForRun(failure),null,'A later range cannot replace missing r0');
        assert.equal(scoreRow().values.score_points,150,'The incomplete seed is excluded');
        failure.samples[0][4]=10000;failure.samples[0][0]=.1;
        assert.equal(model.scoreForRun(failure),null,'A nonzero-time reading is not r0');
        const success=scoreData.runs.find(run=>run.id==='candidate-100-1');
        success.samples[0][4]=null;
        assert.equal(model.scoreForRun(success).total,175,'A success does not require r0');
        scoreData.runs=scoreData.runs.filter(run=>run.label!=='candidate'
          ||run.id==='candidate-100-1'||run.id==='candidate-101-2');
        const split=scoreRow();
        assert.equal(split.score.readings.length,2);
        assert.deepEqual(split.score.seedMeans,[]);
        assert.equal(split.values.score_points,null,
          'Different seeds do not form a consecutive two-run pair');
        assert.equal(split.score.components,null);assert.equal(split.score.deltas,null);
        assert.deepEqual(split.score.contributingRunIds,[]);
        const filtered=scoreRow({outcome:'arrived'});
        assert.equal(filtered.values.score_points,null,
          'Filtering cannot invent the missing repeats');
    """
    )


def test_complete_zero_point_pair_is_measured_but_missing_terminal_counters_are_not_zero():
    node_assertions(
        MODEL_FIXTURES
        + SCORE_FIXTURES
        + """
        scoreData.runs=scoreData.runs.filter(run=>run.label==='candidate'&&run.seed===100);
        for(const [index,run] of scoreData.runs.entries()){
          run.outcome='success';Object.assign(run.final,{time_s:index?120:60,fuel_kg:0,damage_j:0,range_m:50});
        }
        assert.equal(scoreRow().values.score_points,200,'Mean of 250 and 150 points');
        assert.notEqual(scoreRow().values.score_points,12000/90+50,
          'Scoring the mean completion time gives the wrong result');
        for(const run of scoreData.runs){
          run.outcome='success';Object.assign(run.final,{time_s:240,fuel_kg:75,damage_j:0,range_m:50});
        }
        const zero=scoreRow();
        assert.equal(zero.values.score_points,0);
        assert.equal(zero.score.seedMeans.length,1);
        assert.equal(zero.score.contributingRunIds.length,2);
        assert.equal(zero.score.deltas,null,'An absent baseline is not a zero-point baseline');
        scoreData.runs[1].final.fuel_kg=null;
        const missing=scoreRow();
        assert.equal(missing.score.readings.length,1);
        assert.equal(missing.counts.score_points,1);
        assert.equal(missing.values.score_points,null,'One measured zero is not a complete pair');
        assert.equal(missing.score.components,null);
        assert.equal(missing.attempts,2);
    """
    )


def test_comparison_is_read_only_bounded_and_has_recovery_explainers():
    source = (WEB / "path-comparison.ts").read_text()
    renderers = [
        (WEB / name).read_text()
        for name in (
            "path-comparison.ts",
            "path-comparison-data.ts",
            "comparison-model.ts",
            "comparison-matrix.ts",
            "comparison-tradeoffs.ts",
            "comparison-inspector.ts",
        )
    ]
    for required in (
        'method: "GET"',
        'mode: "same-origin"',
        'credentials: "omit"',
        'redirect: "error"',
        "AbortSignal.timeout(15000)",
        "bytes > maxBytes",
        "await reader.cancel()",
        "validatePathComparison(",
        "boundedJSON(files[id], MAX_PATH_BYTES)",
        "boundedJSON(metricFiles[data.id], MAX_METRIC_BYTES)",
        "validateMetricPublication(",
        "Retry flight measurements",
        "Retry extra metrics",
        "token !== request",
        "metricToken !== metricRequest",
        "installMetricMatrix(matrixHost, actions)",
        "installTradeoffExplorer(tradeoffsHost, actions)",
        "installIntervalInspector(inspectorHost, actions)",
        '"Data & method"',
        '"Export data"',
        "selectedCSV(runs)",
    ):
        assert required in source
    assert "MAX_METRIC_BYTES = 2 * 1024 * 1024" in renderers[2]
    for forbidden in (
        "api.control",
        "RoomEngine",
        "hostedRequest",
        "requestStop",
        'from "./engine',
        'from "./room',
        'from "./controller',
        "innerHTML",
        "localStorage",
        'method: "POST"',
        "setInterval(",
        "requestAnimationFrame(",
    ):
        assert all(forbidden not in renderer for renderer in renderers)
    assert all("fetch(" not in renderer for renderer in renderers[1:])
    css = (WEB / "styles.css").read_text()
    assert ".comparison-workspace" in css
    assert any(
        f'{scope} :is(button, select, input, summary, a, svg, [tabindex="0"]):focus-visible' in css
        for scope in (".path-comparison", ".comparison-workspace")
    )
    assert "--team-ali:" in css
    assert "prefers-reduced-motion" in css or "animation:" not in source
    shell = (WEB / "dashboard-shell.ts").read_text()
    assert shell.index('id="dashboard-path-comparison"') < shell.index('id="historical-overview"')
    assert 'href="/compare.html"' in (WEB / "index.html").read_text()
    page = (WEB / "compare.html").read_text()
    assert 'lang="en"' in page and "<noscript>" in page
    assert "simulation-canvas" not in page and "app.js" not in page


def test_publication_hashes_are_checked_during_browser_build():
    script = (ROOT / "worker/scripts/build-web.mjs").read_text()
    assert "pathParser.validatePathComparison" in script
    assert "entry.bytes !== bytes.length" in script
    assert "metricParser.validateMetricPublication" in script
    assert "paths.data, paths.sha256)" in script
    assert "data.source.path_sha256 !== paths.sha256" in script
    manifest = json.loads((ROOT / "docs/analytics/path-comparison/manifest.json").read_text())
    assert {entry["path"] for entry in manifest["artifacts"]} == {
        "official.json",
        "team.json",
        "official-trials.csv",
    }


def test_summary_starts_with_actual_baseline_ids_and_table_before_path_and_exploration():
    source = (WEB / "path-comparison.ts").read_text()
    initial = source.split("function initialState(", 1)[1].split(
        "export function installPathComparison(", 1
    )[0]
    assert 'mode: "cohort"' in initial
    assert 'outcome: "all"' in initial
    assert "difference: false" in initial
    assert "data.runs.filter(run => run.algorithm === baseline)" in initial
    assert "selectedIds: selected.map(run => run.id)" in initial
    assert "selected.find(run => run.seed === seed && run.repetition === repetition)" in initial
    assert "anchorId: anchor?.id ?? null" in initial
    assert "body.append(matrixHost, inspectorHost, tradeoffsDisclosure, evidence)" in source
    assert 'html("details", "comparison-filters")' in source
    assert 'html("summary", "", "Filters")' in source
    for key in ("outcome", "seed", "repetition", "baseline"):
        assert (
            f'"{key}"'
            in source.split("const outcome = select(filterFields", 1)[1].split(
                "toolbar.append(filters)", 1
            )[0]
        )
    assert (
        "differenceLabel.append(difference, "
        'document.createTextNode("Show differences from baseline"))' in source
    )
    assert "tradeoffsDisclosure.open =" not in source
    assert "filters.open =" not in source
    assert "tradeoffs ??= installTradeoffExplorer(tradeoffsHost, actions)" in source
    assert "if (!state || disposed || !tradeoffsDisclosure.open) return;" in source
    assert "if (tradeoffsDisclosure.open) updateTradeoffs();" in source
    assert "tradeoffs?.dispose()" in source


def test_matrix_native_checkboxes_add_sources_and_names_inspect_exclusively():
    matrix = (WEB / "comparison-matrix.ts").read_text()
    row = matrix.split("function rowView(", 1)[1].split("function updateSelection()", 1)[0]
    assert 'select.type = "checkbox"' in row
    assert "select.dataset.selectController = row.algorithm" in row
    assert "selectLabel.htmlFor = select.id" in row
    assert 'html("button", "comparison-row-name")' in row
    assert "if (select.checked) selected.add(run.id); else selected.delete(run.id);" in row
    assert "actions.selectRuns([...selected])" in row
    assert "selectedIds: created.row.runs.map(run => run.id)" in row
    assert "anchorId: pair.id" in row
    assert "view.select.indeterminate = count > 0 && !all" in matrix
    assert (
        "selectAll.indeterminate = visibleSelected > 0 && visibleSelected < visibleIds.size"
        in matrix
    )
    assert "Set baseline" not in matrix
    assert 'html("summary", "", "Columns")' in matrix
    assert 'html("summary", "", "Metric details")' in matrix
    assert '"About metrics"' not in matrix and '"Counts and availability"' not in matrix
    assert "cell.delta.hidden = !state.difference" in matrix
    assert "rendered.difference !== state.difference" in matrix
    assert "if (coverage.open) renderCoverage()" in matrix
    assert '"comparison-coverage-readings"' in matrix
    assert '"comparison-coverage-differences"' in matrix
    assert "supplied.pairedEligible" in matrix
    assert "if (isEfficiencyMetric(key) && (a.arrivals > 0) !== (b.arrivals > 0))" in matrix


def test_path_defaults_to_static_legend_and_maps_with_time_and_raw_values_disclosed():
    inspector = (WEB / "comparison-inspector.ts").read_text()
    assert 'heading = html("h3", "", "Flight path")' in inspector
    assert 'html("ol", "comparison-path-legend")' in inspector
    legend = inspector.split("function drawPathLegend()", 1)[1].split("function drawLegend()", 1)[0]
    assert "runs.map(run =>" in legend
    assert "item.dataset.runId = run.id" in legend
    assert "button(" not in legend and "addEventListener" not in legend
    assert (
        "container.replaceChildren(header, pathLegend, maps, status, empty, inspection)"
        in inspector
    )
    assert 'html("details", "comparison-time-inspection")' in inspector
    assert 'html("summary", "", "Inspect time")' in inspector
    assert 'html("details", "comparison-interval-disclosure")' in inspector
    assert 'html("summary", "", "Measure interval")' in inspector
    assert 'html("summary", "", "Exact cursor values")' in inspector
    assert "readoutDetails.open = true" not in inspector
    assert "Difference from baseline" not in inspector
    assert (
        "const timeCharts = aligned.length && inspection.open "
        "? activeTracks().map(drawTimeline) : []" in inspector
    )
    assert "if (!state || !inspection.open || !readoutDetails.open) return;" in inspector
    assert "intervalSummary.hidden = !selected || !runs.length" in inspector
    assert "if (!selected) { intervalSummary.replaceChildren(); return; }" in inspector
    assert "Whole-recording endpoint deltas" not in inspector
    assert "inspection.open = true" in inspector
    assert "intervalDisclosure.open = true" in inspector
    assert 'inspection.addEventListener("toggle"' in inspector
    assert "inputTime(from), end = inputTime(to)" in inspector
    assert "saved?.text === input.value ? saved.actual : input.valueAsNumber" in inspector
    assert "exact ? String(value) : number(value)" in inspector


def test_summary_uses_metric_populations_and_actual_source_pair_differences():
    node_assertions(
        MODEL_FIXTURES
        + """
        const data=structuredClone(saved);
        const candidates=data.runs.filter(r=>r.algorithm==='official_basic_beeline').slice(0,5);
        const baseline=candidates.map(run=>data.runs.find(r=>r.algorithm==='wso_official_profile'
          &&r.seed===run.seed&&r.repetition===run.repetition));
        assert(baseline.every(Boolean));
        const own=[10,20,100,null,.1],reference=[1,2,50,10,.01];
        candidates.forEach((run,i)=>{
          run.outcome=i===4?'failed_damage':'success';run.final.fuel_kg=own[i];
          for(const row of run.samples)row[10]=i===3?null:10+i;
          baseline[i].outcome='success';baseline[i].final.fuel_kg=reference[i];
        });
        // A source run without its paired baseline must not acquire a delta.
        data.runs=[...candidates,...baseline.filter((_,i)=>i!==1)];
        const view=state(data);
        const row=model.rowsForState(view).find(r=>r.algorithm===candidates[0].algorithm);
        assert.equal(row.attempts,5);assert.equal(row.arrivals,4);
        assert.equal(row.values.fuel_kg,20);
        assert.deepEqual(row.spread.fuel_kg,[10,20,100]);
        assert.equal(row.counts.fuel_kg,3);
        assert.equal(row.coverage.fuel_kg.population,'arrivals');
        assert.equal(row.coverage.fuel_kg.eligible,4);
        assert.equal(row.coverage.fuel_kg.pairedEligible,4);
        assert.deepEqual(row.coverage.fuel_kg.readings,candidates.slice(0,3).map((run,i)=>
          ({runId:run.id,seed:run.seed,repetition:run.repetition,value:own[i]})));
        assert.deepEqual(row.coverage.fuel_kg.pairs,[0,2].map(i=>({runId:candidates[i].id,
          baselineId:baseline[i].id,seed:candidates[i].seed,repetition:candidates[i].repetition,
          delta:own[i]-reference[i]})));
        assert.equal(row.deltas.fuel_kg,29.5,'Median of 9 and 50, not a difference of medians');
        assert.equal(row.coverage.min_radar_m.population,'attempts');
        assert.equal(row.coverage.min_radar_m.eligible,5);
        assert.equal(row.counts.min_radar_m,4);
        assert.equal(row.coverage.min_radar_m.readings.at(-1).runId,candidates[4].id);
        assert.equal(row.coverage.source_samples.pairs.length,4);
        const failed=model.rowsForState({...view,mode:'trials',seed:candidates[4].seed,
          repetition:candidates[4].repetition}).find(r=>r.id===candidates[4].id);
        assert.equal(failed.values.fuel_kg,.1,'Failure measurements remain visible');
        assert.equal(failed.deltas.fuel_kg,null,'Short failure is not an efficiency improvement');
        assert.equal(failed.coverage.fuel_kg.population,'attempts');
        assert.equal(failed.coverage.fuel_kg.pairedEligible,0);
    """
    )


def test_policy_receipts_distinguish_missing_calls_and_measured_zero():
    node_assertions(
        MODEL_FIXTURES
        + """
        const data=structuredClone(saved),extra=supplement(data);
        const runs=data.runs.filter(r=>r.algorithm==='official_basic_beeline').slice(0,3);
        data.runs=runs;extra.runs=extra.runs.filter(r=>runs.some(run=>run.id===r.id));
        const byId=id=>extra.runs.find(r=>r.id===id);
        byId(runs[0].id).policy=null;
        byId(runs[1].id).policy={sample_count:0,median_policy_wall_s:null,
          max_policy_wall_s:null,policy_calls_over_step_budget:null};
        byId(runs[2].id).policy={sample_count:2,median_policy_wall_s:.02,
          max_policy_wall_s:.03,policy_calls_over_step_budget:0};
        assert.equal(model.metricValue(runs[0],'policy_sample_count',extra),null);
        assert.equal(model.metricValue(runs[1],'policy_sample_count',extra),0);
        assert.equal(model.metricValue(runs[1],'median_policy_wall_s',extra),null);
        assert.equal(model.metricValue(runs[2],'policy_sample_count',extra),2);
        const row=model.rowsForState(state(data,{supplement:extra}))[0];
        assert.equal(row.coverage.policy_sample_count.population,'attempts');
        assert.equal(row.coverage.policy_sample_count.eligible,3);
        assert.deepEqual(row.coverage.policy_sample_count.readings.map(r=>r.value),[0,2]);
        assert.equal(row.coverage.median_policy_wall_s.readings.length,1);
        assert.equal(row.coverage.median_policy_wall_s.timingCalls,2);
        assert.equal(model.getMetric('policy_sample_count').unit,'calls');
    """
    )


def test_inspection_requires_selection_and_baseline_does_not_move_its_anchor():
    node_assertions(
        MODEL_FIXTURES
        + """
        const data=structuredClone(saved);
        const run=data.runs.find(r=>r.algorithm==='official_basic_beeline');
        const base=state(data,{mode:'trials',seed:run.seed,repetition:run.repetition,
          alignment:'arrival',cursor:12,interval:[10,20]});
        assert.deepEqual(model.inspectionRuns(base),[]);
        assert.equal(model.inspectionAnchor(base),undefined);
        const selected={...base,selectedIds:[run.id],anchorId:run.id};
        assert.equal(model.inspectionAnchor(selected).id,run.id);
        const changed={...selected,baseline:data.runs.find(r=>r.algorithm!==run.algorithm
          &&r.algorithm!==base.baseline).algorithm};
        assert.equal(model.inspectionAnchor(changed).id,run.id);
        assert.deepEqual(changed.interval,[10,20]);
        assert(model.inspectionRuns(changed).every(r=>r.seed===run.seed&&r.repetition===run.repetition));
        const other=data.runs.find(r=>r.seed!==run.seed);
        assert.equal(model.inspectionAnchor({...base,selectedIds:[other.id],anchorId:other.id}),undefined);
        assert.deepEqual(model.inspectionRuns({...base,selectedIds:[other.id],anchorId:other.id}),[]);
    """
    )


def test_interval_needs_two_readings_and_does_not_borrow_native_endpoint_positions():
    node_assertions(
        MODEL_FIXTURES
        + """
        const run=structuredClone(saved.runs[0]);
        const [a,b]=run.samples.slice(0,2).map(row=>row.slice());
        run.samples=[a,b];run.final.time_s=b[0];
        a[6]=b[6]=5;a[7]=b[7]=0;
        const one=model.intervalStats(run,[a[0],a[0]]);
        assert.equal(one.start_s,a[0]);assert.equal(one.end_s,a[0]);
        assert.equal(one.coverage,0);
        for(const key of ['fuel_kg','damage_j','progress_m'])assert.equal(one[key],null);
        const two=model.intervalStats(run,[a[0],b[0]]);
        assert.equal(two.fuel_kg,0);assert.equal(two.damage_j,0);
        assert.equal(two.progress_m,b[1]-a[1]);assert.equal(two.coverage,1);
        const extra=supplement({...saved,runs:[run]}),native=analytics(run);
        const middle=(a[0]+b[0])/2;
        native.samples.splice(1,0,[middle,5,0,0,0,0,null,null,0]);native.sample_count=3;
        extra.runs[0].analytics=native;
        const endpoint=model.intervalStats(run,[middle,b[0]],extra);
        assert.equal(endpoint.start_s,middle);assert.equal(endpoint.end_s,b[0]);
        assert.equal(endpoint.fuel_kg,0);assert.equal(endpoint.damage_j,0);
        assert.equal(endpoint.progress_m,null,
          'A nearby path position is not a native endpoint position');
        const missing=model.intervalStats(run,[middle,middle],extra);
        assert.equal(missing.fuel_kg,null);assert.equal(missing.progress_m,null);
        assert.equal(model.intervalStats(run,[b[0]+1,b[0]+2],extra).start_s,null);
    """
    )


def test_frontier_excludes_failures_missing_values_and_context_axes():
    node_assertions(
        MODEL_FIXTURES
        + """
        const runs=structuredClone(saved.runs.slice(0,5));
        const values=[[1,8],[3,4],[4,9],[0,0],[null,1]];
        runs.forEach((run,i)=>{run.outcome=i===3?'failed_damage':'success';
          run.final.fuel_kg=values[i][0];run.final.time_s=values[i][1];});
        assert.deepEqual(new Set(model.paretoRunIds(runs,'fuel_kg','time_s')),
          new Set([runs[0].id,runs[1].id]));
        assert.deepEqual(model.paretoRunIds(runs,'lateral_m','time_s'),[]);
        assert.deepEqual(model.paretoRunIds(runs,'not_a_metric','time_s'),[]);
        const extra=supplement({...saved,runs});
        extra.runs[0].source_sha256.trace='0'.repeat(64);
        assert.deepEqual(model.paretoRunIds(runs,'fuel_kg','time_s',extra),[]);
    """
    )


def test_supplement_validator_requires_exact_bytes_source_cohort_and_native_consistency():
    node_assertions(
        MODEL_FIXTURES
        + """
        const data=structuredClone(saved),extra=supplement(data);
        extra.runs[0].analytics=analytics(data.runs[0]);
        extra.runs[1].policy={sample_count:0,median_policy_wall_s:null,
          max_policy_wall_s:null,policy_calls_over_step_budget:null};
        assert(model.validateMetricPublication(extra,data,digest));
        assert.equal(model.validateMetricPublication(extra,data),null,'Digest is required');
        const reencoded=createHash('sha256').update(JSON.stringify(data,null,2)).digest('hex');
        assert.notEqual(reencoded,digest);
        assert.equal(model.validateMetricPublication(extra,data,reencoded),null);
        for(const mutate of [
          x=>x.source.path_sha256='0'.repeat(64),
          x=>x.id='team',
          x=>x.source.engine_sha256='0'.repeat(64),
          x=>x.source.step_s*=2,
          x=>x.runs[0].source_sha256.trace='0'.repeat(64),
          x=>x.runs[0].source_sha256.run='0'.repeat(64),
          x=>x.runs[0].seed++,
          x=>x.runs[0].repetition++,
          x=>x.runs[0].outcome='policy_error',
          x=>x.runs.pop(),
          x=>x.runs[1]=structuredClone(x.runs[0]),
          x=>x.runs[0].analytics.samples.at(-1)[1]+=1,
          x=>x.runs[0].analytics.samples[1][2]=1,
          x=>x.runs[0].analytics.totals.burn_count=1,
          x=>x.runs[0].analytics.totals.observed_sim_s+=1,
          x=>x.runs[1].policy.median_policy_wall_s=0,
        ]) {
          const invalid=structuredClone(extra);mutate(invalid);
          assert.equal(model.validateMetricPublication(invalid,data,digest),null);
        }
        const missing=structuredClone(extra);missing.runs[1].policy=null;
        assert(model.validateMetricPublication(missing,data,digest));
    """
    )
