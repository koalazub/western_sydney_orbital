"""Pure browser-data and source contracts; Chrome review is a separate recipe."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "worker/web"


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


def test_comparison_is_read_only_bounded_and_has_recovery_explainers():
    source = (WEB / "path-comparison.ts").read_text()
    for required in (
        'credentials: "omit"',
        'redirect: "error"',
        "bytes > MAX_PATH_BYTES",
        "await reader.cancel()",
        "validatePathComparison(",
        "No measurements are interpolated",
        "Previous sample",
        "Next sample",
        "ArrowRight",
        "Retry flight measurements",
        "preceding interval",
        "vertical scale is expanded",
        "not hull clearance",
        "not wired into the published Ali flights",
        'html("fieldset", "path-profiles")',
        'check.type = "checkbox"',
        "traceStyle(run.algorithm)",
        "No averaging",
        "row[16]",
    ):
        assert required in source
    for forbidden in (
        "api.control",
        "RoomEngine",
        "hostedRequest",
        "requestStop",
        "innerHTML",
        "localStorage",
        'method: "POST"',
        "setInterval(",
        "requestAnimationFrame(",
    ):
        assert forbidden not in source
    css = (WEB / "styles.css").read_text()
    assert ".path-plot" in css and ".path-scroll" in css
    assert (
        ".path-comparison :is(button, select, input, summary, a, svg, "
        '[tabindex="0"]):focus-visible' in css
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
    manifest = json.loads((ROOT / "docs/analytics/path-comparison/manifest.json").read_text())
    assert {entry["path"] for entry in manifest["artifacts"]} == {
        "official.json",
        "team.json",
        "official-trials.csv",
    }
