"""Checks that the committed visual is derived from the committed metrics."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from wsorb.official_comparison_chart import (
    load_summary,
    render_svg,
    update_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "docs/analytics/official-comparison/summary.csv"
MANIFEST = ROOT / "docs/analytics/official-comparison/manifest.json"


def test_summary_and_svg_use_the_same_published_values() -> None:
    rows = load_summary(SUMMARY)
    assert [row.algorithm for row in rows] == [
        "official_basic_beeline",
        "ali_official_profile",
        "baseline",
        "body-axis",
        "basic_pd_radar",
    ]
    assert rows[1].median_success_time_s == pytest.approx(153.20000000000059)
    assert rows[1].median_final_relative_speed_mps == pytest.approx(1.2046708023861086)
    assert rows[1].median_fuel_used_kg_estimate == pytest.approx(108.95033726595258)
    assert rows[1].outcomes == {"success": 6}
    assert rows[0].outcomes == {"failed_damage": 2, "success": 4}
    svg = render_svg(
        rows,
        source_sha256=hashlib.sha256(SUMMARY.read_bytes()).hexdigest(),
    )
    root = ET.fromstring(svg)
    assert root.attrib["data-source-sha256"] == hashlib.sha256(SUMMARY.read_bytes()).hexdigest()
    text = "".join(root.itertext())
    for expected in ("153.2 s", "1.20 m/s", "109.0 kg", "4/6 success", "6/6 success"):
        assert expected in text
    assert "Local practice evidence" in text


def test_committed_svg_and_manifest_match_the_current_renderer() -> None:
    publication = SUMMARY.parent
    rendered = render_svg(
        load_summary(SUMMARY), source_sha256=hashlib.sha256(SUMMARY.read_bytes()).hexdigest()
    )
    assert (publication / "performance.svg").read_text() == rendered
    manifest = json.loads(MANIFEST.read_text())
    for entry in manifest["artifacts"]:
        data = (publication / entry["path"]).read_bytes()
        assert entry["bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert "successful runs only" in rendered
    assert "including failures" in rendered
    assert 'class="outcome-count"' in rendered


def test_missing_algorithm_is_rejected(tmp_path) -> None:
    source = SUMMARY.read_text().splitlines()
    header, body = source[0], source[1:]
    body = [line for line in body if not line.startswith("baseline,")]
    broken = tmp_path / "summary.csv"
    broken.write_text("\n".join([header, *body]) + "\n")
    with pytest.raises(ValueError, match="missing algorithms"):
        load_summary(broken)


def test_manifest_update_records_the_visual(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(MANIFEST.read_text())
    svg = tmp_path / "performance.svg"
    svg.write_text("<svg/>")
    update_manifest(manifest, svg)
    parsed = json.loads(manifest.read_text())
    entry = next(item for item in parsed["artifacts"] if item["path"] == "performance.svg")
    assert entry["bytes"] == len("<svg/>")
    assert entry["sha256"] == hashlib.sha256(b"<svg/>").hexdigest()
