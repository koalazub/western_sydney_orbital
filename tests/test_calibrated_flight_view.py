"""Checks that the committed playback view embeds the committed telemetry."""

from __future__ import annotations

import json
from pathlib import Path

from wsorb.calibrated_flight_view import render_html

ROOT = Path(__file__).resolve().parents[1]
PUBLICATION = ROOT / "docs/analytics/official-comparison"
DATA = PUBLICATION / "calibrated-flight.json"
HTML = PUBLICATION / "calibrated-flight.html"


def test_playback_json_and_html_are_the_same_dataset() -> None:
    dataset = json.loads(DATA.read_text())
    assert dataset["schema"] == "wsorb.calibrated_flight_view"
    assert dataset["seed"] == 104
    assert [profile["id"] for profile in dataset["profiles"]] == [
        "ali_mppi",
        "tom_polyfit_mvp",
        "bui_alpha_beta_mvp",
        "basic_pd_radar",
    ]
    assert HTML.read_text() == render_html(dataset)
    assert all(len(profile["telemetry"]["samples"]) > 100 for profile in dataset["profiles"])
    assert all(profile["telemetry"]["columns"][0] == "time_s" for profile in dataset["profiles"])


def test_playback_does_not_publish_radar_or_private_payloads() -> None:
    forbidden = {"radar", "radar_rows", "token", "authorization", "connection_file", "room_id"}

    def walk(value):
        if isinstance(value, dict):
            assert not forbidden & set(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(DATA.read_text()))
