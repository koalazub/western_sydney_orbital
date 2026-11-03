"""Check the committed measured publication, without rerunning or emulating flights."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from wsorb.flight_analytics import validate_run_analytics
from wsorb.scoreboard import render_comparison_section

ROOT = Path(__file__).resolve().parents[1]
PUBLICATION = ROOT / "docs/analytics/team-comparison"


def bundle():
    return json.loads((PUBLICATION / "dataset.json").read_text())


def test_publication_digests_and_svg_graphs_match_the_committed_measurements():
    manifest = json.loads((PUBLICATION / "manifest.json").read_text())
    assert manifest["schema"] == "wsorb.teammate_analytics.artifacts"
    for artifact in manifest["artifacts"]:
        path = PUBLICATION / artifact["path"]
        assert path.parent == PUBLICATION and not path.is_symlink()
        data = path.read_bytes()
        assert len(data) == artifact["bytes"]
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
        assert len(data) <= 2 * 1024 * 1024
        if path.suffix == ".svg":
            svg = ElementTree.fromstring(data)
            names = [node.tag.rsplit("}", 1)[-1] for node in svg.iter()]
            assert names[0] == "svg"
            assert "title" in names and "desc" in names
            assert not {"script", "foreignObject", "image"} & set(names)


def test_every_published_pair_has_its_actual_native_metrics_and_matching_cohort():
    data = bundle()
    config = json.loads((ROOT / "examples/teammate_comparison.json").read_text())
    comparison = data["comparison"]
    assert comparison["seeds"] == config["seeds"]
    assert comparison["repetitions"] == config["repetitions"] == 2
    assert comparison["debris_count"] == config["debris_counts"][0] == 500
    assert comparison["execution_kind"] == "owned_native_manual_step"
    assert len(data["trials"]) == len(config["algorithms"]) * len(config["seeds"]) * 2
    seen = set()
    for trial in data["trials"]:
        key = trial["profile_id"], trial["seed"], trial["repetition"]
        assert key not in seen
        seen.add(key)
        assert trial["engine_sha256"] == comparison["engine_build"]["sha256"]
        assert validate_run_analytics(trial["analytics"]) == trial["analytics"]
        assert trial["analytics"]["totals"]["observed_sim_s"] == trial["measurement"]["time_s"]
        assert trial["score_breakdown"]["total"] == trial["score"]
    assert {row["owner"] for row in data["profiles"]} == {"Tom", "Harry", "Bui"}


def test_guide_generated_table_and_graph_links_are_not_stale():
    guide = (ROOT / "docs/flight-lab.md").read_text()
    generated = guide.split("<!-- BEGIN GENERATED TEAMMATE ANALYTICS -->\n", 1)[1].split(
        "<!-- END GENERATED TEAMMATE ANALYTICS -->", 1
    )[0]
    assert generated == render_comparison_section(bundle())
    readme = (ROOT / "README.md").read_text()
    assert "GENERATED TEAMMATE ANALYTICS" not in readme
    assert "(docs/flight-lab.md#recorded-teammate-flights)" in readme


def test_readme_guides_and_notebook_links_resolve_after_documentation_moves():
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    documents.extend(sorted((ROOT / "notebook").rglob("*.md")))
    for document in documents:
        for link in re.findall(r"\[[^\]\n]*\]\(([^)\s]+)\)", document.read_text()):
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            target = (document.parent / unquote(url.path)).resolve() if url.path else document
            assert target.exists(), f"{document.relative_to(ROOT)} links to missing {link}"
            if url.fragment and target.suffix == ".md":
                headings = re.findall(r"^#{1,6}\s+(.+)$", target.read_text(), re.MULTILINE)
                anchors = {
                    re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
                    for heading in headings
                }
                assert unquote(url.fragment) in anchors, (
                    f"{document.relative_to(ROOT)} links to missing heading {link}"
                )


def test_public_dataset_has_no_private_or_hidden_sensor_payload_fields():
    forbidden = {
        "token",
        "authorization",
        "room_id",
        "browser_url",
        "radar",
        "radar_backup",
        "cameras",
        "rgb",
        "raw_payload",
        "package_sources",
        "module_source",
        "exception",
        "error_message",
    }

    def walk(value):
        if isinstance(value, dict):
            assert not forbidden & set(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(bundle())
