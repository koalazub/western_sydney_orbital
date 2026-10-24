"""Pure export schema/numeric contracts, not simulated flights or backend mocks."""

from __future__ import annotations

import csv
import hashlib
import io
import json

import numpy as np
import pytest

from wsorb.experiment_io import (
    HISTORY_COLUMNS,
    experiment_document,
    history_csv,
    json_bytes,
    json_safe,
    source_manifest,
)


def test_strict_json_preserves_missing_and_redacts_private_data_without_mutation():
    values = {
        "reading": np.float64(1.5),
        "missing": None,
        "bad": np.array([np.nan, np.inf, -np.inf]),
        "nested": {"Authorization": "private", "bearer_token": "private", "ok": 0},
        "connection_descriptor": {"url": "private", "token": "private"},
        "_client": object(),
    }
    assert json.loads(json_bytes(values)) == {
        "reading": 1.5,
        "missing": None,
        "bad": [None, None, None],
        "nested": {"ok": 0},
    }
    assert values["nested"]["Authorization"] == "private"
    assert "NaN" not in json_bytes(values).decode()


def test_export_rejects_clients_and_other_objects_instead_of_inspecting_them():
    with pytest.raises(TypeError, match="only public JSON-shaped data"):
        json_safe({"public": object()})
    with pytest.raises(TypeError, match="field names must be strings"):
        json_safe({1: 2})


def test_source_hashes_are_for_exact_supplied_bytes_and_explicit_labels():
    source = b"def policy():\n    return None\n"
    assert source_manifest({"saved notebook at export": source}) == [
        {"label": "saved notebook at export", "sha256": hashlib.sha256(source).hexdigest()}
    ]
    assert (
        source_manifest({"utf-8 source": "π\n"})[0]["sha256"]
        == hashlib.sha256("π\n".encode()).hexdigest()
    )


def test_csv_keeps_numeric_zero_and_negative_values_but_not_unavailable_or_private_values():
    data = history_csv(
        [
            {
                "telemetry_time_s": 2.0,
                "speed_mps": 0.0,
                "radial_climb_mps": -2.5,
                "damage_j_estimate": None,
                "frame": "=not-a-formula",
                "token": "private",
            }
        ]
    ).decode()
    rows = list(csv.DictReader(io.StringIO(data)))
    assert tuple(rows[0]) == HISTORY_COLUMNS
    assert rows[0]["speed_mps"] == "0.0"
    assert rows[0]["radial_climb_mps"] == "-2.5"
    assert rows[0]["damage_j_estimate"] == ""
    assert rows[0]["frame"] == "'=not-a-formula"
    assert "private" not in data
    assert len(list(csv.reader(io.StringIO(history_csv([]).decode())))) == 1


def test_experiment_document_separates_applied_draft_and_reported_world_settings():
    latest = {
        "observation": {
            "frame": "numeric-contract-frame",
            "radar_clock": "numeric-contract-clock",
            "experiment_settings": {"ship": {"mass_kg": 1200}},
        },
        "engine_build": {"sha256": "reported-engine-hash"},
        "connection": {"token": "private"},
        "client": object(),
    }
    document = experiment_document(
        applied_config={"controller": "coast", "gains": {"gain": 1}},
        draft_config={"controller": "planned", "gains": {"gain": 2}},
        selected_policy="planned",
        schedule_s=(0.05, 0.1),
        history=[{"telemetry_time_s": 2, "manual_intervention": True}],
        latest_snapshot=latest,
        sources={"saved numeric-contract source": "# test bytes\n"},
    )
    assert document["schema_version"] == 1
    assert document["applied_config"]["controller"] == "coast"
    assert document["draft_config"]["controller"] == "planned"
    assert document["applied_world_settings"] == {"ship": {"mass_kg": 1200}}
    assert document["engine_build"] == {"sha256": "reported-engine-hash"}
    assert document["frames_and_clocks"]["radar_clock"] == "numeric-contract-clock"
    assert document["claims"]["official_score"] is False
    assert document["claims"]["stp_physics_parity"] is False
    assert len(document["manual_interventions"]) == 1
    assert set(document["latest_public_snapshot"]) == {"observation", "engine_build"}
    assert "private" not in json_bytes(document).decode()


def test_empty_export_does_not_invent_a_run_clock_engine_or_measurements():
    document = experiment_document(
        applied_config=None,
        draft_config={},
        selected_policy="coast",
        schedule_s=(),
        history=[],
        latest_snapshot=None,
        sources={},
    )
    assert document["applied_config"] is None
    assert document["latest_public_snapshot"] is None
    assert document["engine_build"] is None
    assert document["latest_camera_status"] is None
    assert document["measured_history"] == []
    assert document["python_sources"] == []
