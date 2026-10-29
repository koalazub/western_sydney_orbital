"""Offline numeric/schema/serialization checks, not SDK, browser or engine tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from wsorb.local_api import FLIGHT_PRESETS, validate_experiment_settings
from wsorb.proposals import (
    MAX_PROPOSAL_BYTES,
    WorldProposal,
    approve_world_proposal,
    parse_world_proposal,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenario():
    """Read real versioned local settings, not an invented replacement scenario."""
    document = json.loads((ROOT / "simulation/scenarios/phobos_escape.json").read_text("utf-8"))
    assert document["experiment_description"].startswith("local_experiment_v")
    return document


@pytest.fixture
def document(scenario):
    return {
        "schema": "wsorb.local_world_proposal",
        "schema_version": 1,
        "preset": "far_side_l2",
        "seed": scenario["default_seed"],
        "settings": deepcopy(scenario["experiment_settings"]),
    }


@pytest.fixture
def impact_document(document, scenario):
    document["settings"]["debris"]["distribution"] = {
        "kind": "impact_ejecta",
        "settings": deepcopy(scenario["impact_ejecta_defaults"]),
    }
    return document


def test_checked_in_settings_have_canonical_json_and_a_content_digest(document):
    original = json.dumps(document, indent=2).encode("utf-8")
    proposal = parse_world_proposal(original)
    expected = {**document, "settings": validate_experiment_settings(document["settings"])}
    expected_bytes = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert isinstance(proposal, WorldProposal)
    assert proposal.to_document() == expected
    assert proposal.to_json_bytes() == expected_bytes
    assert proposal.digest == hashlib.sha256(expected_bytes).hexdigest()
    assert proposal.digest != hashlib.sha256(original).hexdigest()
    assert parse_world_proposal(expected_bytes).to_json_bytes() == expected_bytes
    assert parse_world_proposal(expected_bytes.decode("utf-8")).digest == proposal.digest
    assert proposal.to_document()["settings"]["debris"]["distribution"] == {
        "kind": "transit_stream"
    }
    assert document["settings"]["debris"]["distribution"] == {"kind": "transit_stream"}
    assert "trajectory_adjustment" not in proposal.to_document()["settings"]["debris"]
    assert json.loads(original) == document


def test_key_order_whitespace_numeric_spelling_and_explicit_off_share_a_digest(document):
    proposal = parse_world_proposal(json.dumps(document))
    alternate = {name: document[name] for name in reversed(document)}
    alternate["settings"]["ship"]["inertia_scale"] = 1
    alternate["settings"]["debris"]["speed_mps"] = 20
    alternate["settings"]["debris"]["trajectory_adjustment"] = "off"
    reordered = json.dumps(alternate, sort_keys=True, indent=4)
    assert parse_world_proposal(reordered).digest == proposal.digest


def test_legacy_omission_and_explicit_directed_stream_keep_the_old_canonical_shape(document):
    """Legacy proposal serialization only, not replay or flight proof."""
    del document["settings"]["debris"]["distribution"]
    legacy = parse_world_proposal(json.dumps(document))
    expected = deepcopy(document)
    expected["settings"]["debris"]["distribution"] = {"kind": "directed_stream"}
    assert legacy.to_json_bytes() == json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected["settings"]["debris"]["trajectory_adjustment"] = "off"
    assert parse_world_proposal(json.dumps(expected)).digest == legacy.digest
    assert "distribution" not in document["settings"]["debris"]
    assert "trajectory_adjustment" not in legacy.to_document()["settings"]["debris"]


@pytest.mark.parametrize("kind", ["directed_stream", "transit_stream", "impact_ejecta"])
def test_adjustment_is_explicit_in_review_and_off_keeps_the_omitted_digest(
    document, scenario, kind
):
    distribution = {"kind": kind}
    if kind == "impact_ejecta":
        distribution["settings"] = deepcopy(scenario["impact_ejecta_defaults"])
    debris = document["settings"]["debris"]
    debris["distribution"] = distribution
    omitted = parse_world_proposal(json.dumps(document))
    debris["trajectory_adjustment"] = "off"
    assert parse_world_proposal(json.dumps(document)).digest == omitted.digest
    debris["trajectory_adjustment"] = "toward_lander"
    adjusted = parse_world_proposal(json.dumps(document))
    assert adjusted.digest != omitted.digest
    assert adjusted.to_document()["settings"]["debris"]["trajectory_adjustment"] == "toward_lander"
    assert adjusted.to_document()["settings"]["debris"]["distribution"] == distribution
    draft = approve_world_proposal(adjusted, adjusted.digest)
    assert draft["settings"]["debris"]["trajectory_adjustment"] == "toward_lander"
    assert parse_world_proposal(adjusted.to_json_bytes()).digest == adjusted.digest
    with pytest.raises(ValueError, match="Review the current normalized JSON"):
        approve_world_proposal(adjusted, omitted.digest)


def test_transit_and_legacy_streams_cannot_share_an_approval_digest(document):
    transit = parse_world_proposal(json.dumps(document))
    document["settings"]["debris"]["distribution"] = {"kind": "directed_stream"}
    legacy = parse_world_proposal(json.dumps(document))
    assert legacy.digest != transit.digest
    with pytest.raises(ValueError, match="Review the current normalized JSON"):
        approve_world_proposal(transit, legacy.digest)


@pytest.mark.parametrize(
    "adjustment", [None, True, False, 0, 1, "", "OFF", "toward-Lander", [], {}, {"kind": "off"}]
)
def test_adjustment_rejects_boolean_numbers_wrong_kinds_and_unknown_names(document, adjustment):
    document["settings"]["debris"]["trajectory_adjustment"] = adjustment
    with pytest.raises(ValueError, match="trajectory_adjustment"):
        parse_world_proposal(json.dumps(document))


def test_signed_vector_and_scalar_zero_have_one_canonical_encoding(impact_document):
    impact_document["settings"]["debris"]["speed_spread_mps"] = -0.0
    ejecta = impact_document["settings"]["debris"]["distribution"]["settings"]
    ejecta["source_direction"] = [-1, -0.0, 0]
    ejecta["kick_velocity_mps"] = [0, -0.0, 0.0]
    proposal = parse_world_proposal(json.dumps(impact_document))
    normalized = proposal.to_document()
    assert b"-0.0" not in proposal.to_json_bytes()
    assert normalized["settings"] == validate_experiment_settings(impact_document["settings"])
    assert parse_world_proposal(json.dumps(normalized)).digest == proposal.digest


@pytest.mark.parametrize("preset", FLIGHT_PRESETS)
@pytest.mark.parametrize("seed", [None, 0, 0xFFFFFFFF])
def test_approval_returns_only_new_world_draft_data(document, preset, seed):
    document.update(preset=preset, seed=seed)
    proposal = parse_world_proposal(json.dumps(document))
    draft = approve_world_proposal(proposal, proposal.digest)
    assert draft == {
        "preset": preset,
        "seed": seed,
        "settings": validate_experiment_settings(document["settings"]),
    }
    assert set(draft) == {"preset", "seed", "settings"}


def test_review_data_is_frozen_and_all_returned_documents_are_detached(impact_document):
    proposal = parse_world_proposal(json.dumps(impact_document))
    original_bytes, original_digest = proposal.to_json_bytes(), proposal.digest
    detached = proposal.to_document()
    detached["seed"] = 0
    detached["settings"]["ship"]["mass_kg"] = 100
    detached["settings"]["debris"]["distribution"]["settings"]["source_direction"][0] = 42
    draft = approve_world_proposal(proposal, original_digest)
    draft["settings"]["debris"]["distribution"]["settings"]["kick_velocity_mps"][0] = 200
    draft["settings"]["ship"]["mass_kg"] = 200
    impact_document["settings"]["ship"]["mass_kg"] = 300
    assert proposal.to_json_bytes() == original_bytes
    assert proposal.digest == original_digest
    assert proposal.to_document()["settings"]["ship"]["mass_kg"] != 100
    assert approve_world_proposal(proposal, original_digest)["settings"]["ship"]["mass_kg"] != 200
    with pytest.raises(FrozenInstanceError):
        proposal._json_bytes = b"{}"


@pytest.mark.parametrize("changed", ["preset", "seed", "settings"])
def test_changed_proposal_cannot_use_a_previous_review_digest(document, changed):
    original = parse_world_proposal(json.dumps(document))
    if changed == "settings":
        document["settings"]["ship"]["mass_kg"] = 4000
    else:
        document[changed] = "mars_facing_l1" if changed == "preset" else 123
    updated = parse_world_proposal(json.dumps(document))
    assert updated.digest != original.digest
    with pytest.raises(ValueError, match="Review the current normalized JSON"):
        approve_world_proposal(updated, original.digest)


@pytest.mark.parametrize("digest", [None, True, b"0" * 64, "", "0" * 64, "f" * 65, "é" * 64])
def test_approval_requires_the_exact_string_digest(document, digest):
    proposal = parse_world_proposal(json.dumps(document))
    with pytest.raises(ValueError, match="exact SHA-256 digest"):
        approve_world_proposal(proposal, digest)
    for changed in (proposal.digest.upper(), " " + proposal.digest, proposal.digest + "\n"):
        with pytest.raises(ValueError, match="exact SHA-256 digest"):
            approve_world_proposal(proposal, changed)


def test_approval_rejects_unparsed_documents_and_direct_construction_also_validates(document):
    with pytest.raises(ValueError, match="parsed WorldProposal"):
        approve_world_proposal(document, "0" * 64)
    document["settings"]["ship"]["mass_kg"] = 99
    with pytest.raises(ValueError, match="mass_kg"):
        WorldProposal(json.dumps(document))


def test_approval_revalidates_actual_stored_bytes(document):
    proposal = parse_world_proposal(json.dumps(document))
    old_digest = proposal.digest
    document["seed"] = 123
    # Deliberately bypass frozen assignment to check the approval boundary itself.
    object.__setattr__(proposal, "_json_bytes", json.dumps(document).encode("utf-8"))
    with pytest.raises(ValueError, match="digest does not match"):
        approve_world_proposal(proposal, old_digest)
    document["settings"]["ship"]["mass_kg"] = 99
    object.__setattr__(proposal, "_json_bytes", json.dumps(document).encode("utf-8"))
    with pytest.raises(ValueError, match="mass_kg"):
        approve_world_proposal(proposal, proposal.digest)


@pytest.mark.parametrize("missing", ["schema", "schema_version", "preset", "seed", "settings"])
def test_every_top_level_field_is_required(document, missing):
    del document[missing]
    with pytest.raises(ValueError, match=f"add required fields {missing}"):
        parse_world_proposal(json.dumps(document))


@pytest.mark.parametrize(
    ("name", "value", "correction"),
    [
        ("schema", "wsorb.local_experiment", "schema must"),
        ("schema", None, "schema must"),
        ("schema_version", True, "schema_version must be the integer 1"),
        ("schema_version", 1.0, "schema_version must be the integer 1"),
        ("schema_version", 2, "schema_version must be the integer 1"),
        ("schema_version", "1", "schema_version must be the integer 1"),
        ("preset", "moon_facing_mars_l2", "preset must"),
        ("preset", [], "preset must"),
        ("seed", True, "seed must be null or an integer"),
        ("seed", -1, "seed must be null or an integer"),
        ("seed", 0x100000000, "seed must be null or an integer"),
        ("seed", 100.0, "seed must be null or an integer"),
        ("seed", "100", "seed must be null or an integer"),
    ],
)
def test_top_level_values_use_the_exact_versioned_contract(document, name, value, correction):
    document[name] = value
    with pytest.raises(ValueError, match=correction):
        parse_world_proposal(json.dumps(document))


@pytest.mark.parametrize("path", [(), ("settings",), ("settings", "ship"), ("settings", "debris")])
def test_unknown_fields_are_named_and_never_silently_dropped(document, path):
    target = document
    for name in path:
        target = target[name]
    target["outbound_command"] = "do not execute"
    with pytest.raises(ValueError, match="remove unknown fields outbound_command"):
        parse_world_proposal(json.dumps(document))


@pytest.mark.parametrize(
    ("family", "missing"),
    [
        ("ship", "mass_kg"),
        ("ship", "inertia_scale"),
        ("debris", "count"),
        ("debris", "stream_axis"),
    ],
)
def test_partial_settings_are_rejected_instead_of_merged_with_defaults(document, family, missing):
    del document["settings"][family][missing]
    with pytest.raises(ValueError, match=f"add required fields {missing}"):
        parse_world_proposal(json.dumps(document))


@pytest.mark.parametrize("path", [("settings",), ("settings", "ship"), ("settings", "debris")])
@pytest.mark.parametrize("value", [None, [], "settings", True])
def test_settings_containers_must_be_objects(document, path, value):
    target = document
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_world_proposal(json.dumps(document))


@pytest.mark.parametrize(
    ("family", "name", "value", "correction"),
    [
        ("ship", "mass_kg", True, "mass_kg cannot be a boolean"),
        ("ship", "inertia_scale", False, "inertia_scale cannot be a boolean"),
        ("ship", "mass_kg", 99, "mass_kg must"),
        ("ship", "mass_kg", 100001, "mass_kg must"),
        ("ship", "inertia_scale", 0.09, "inertia_scale must"),
        ("ship", "inertia_scale", 11, "inertia_scale must"),
        ("debris", "count", True, "count cannot be a boolean"),
        ("debris", "count", 1.0, "count must be a whole number"),
        ("debris", "count", 100001, "count must be a whole number"),
        ("debris", "radius_min_m", 0.049, "radius_min_m must"),
        ("debris", "radius_max_m", 0.1, "radius_max_m must"),
        ("debris", "speed_mps", 201, "speed_mps must"),
        ("debris", "speed_spread_mps", 11, "speed_spread_mps must"),
        ("debris", "shell_inner_m", -1, "shell_inner_m must"),
        ("debris", "shell_outer_m", 50001, "shell_outer_m must"),
        ("debris", "shell_outer_m", 69, "shell must be at least 10 m thick"),
        ("debris", "stream_axis", "north", "stream_axis x/y/z"),
        ("debris", "stream_sign", True, "stream_sign cannot be a boolean"),
        ("debris", "stream_sign", 0, "stream_sign -1/1"),
    ],
)
def test_settings_keep_native_bounds_and_reject_boolean_numbers(
    document, family, name, value, correction
):
    document["settings"][family][name] = value
    with pytest.raises(ValueError, match=correction):
        parse_world_proposal(json.dumps(document))


def test_impact_settings_use_the_checked_in_values_and_native_normalization(impact_document):
    proposal = parse_world_proposal(json.dumps(impact_document))
    assert proposal.to_document()["settings"] == validate_experiment_settings(
        impact_document["settings"]
    )
    assert (
        approve_world_proposal(proposal, proposal.digest)["settings"]
        == proposal.to_document()["settings"]
    )


@pytest.mark.parametrize(
    ("name", "value", "correction"),
    [
        ("source_direction", [1, False, 0], "source_direction\\[1\\] cannot be a boolean"),
        ("kick_velocity_mps", [True, 0, 0], "kick_velocity_mps\\[0\\] cannot be a boolean"),
        ("source_direction", [0, 0, 0], "source_direction must be nonzero"),
        ("source_direction", [1, 0], "source_direction must be a finite real vector"),
        ("kick_velocity_mps", [200, 1, 0], "kick_velocity_mps must have speed at most 200"),
        ("tangential_speed_mps", 201, "tangential_speed_mps must"),
        ("patch_half_angle_deg", 0, "patch_half_angle_deg must be greater than zero"),
        ("patch_half_angle_deg", 181, "patch_half_angle_deg must"),
    ],
)
def test_impact_numbers_are_strict_before_native_validation(
    impact_document, name, value, correction
):
    impact_document["settings"]["debris"]["distribution"]["settings"][name] = value
    with pytest.raises(ValueError, match=correction):
        parse_world_proposal(json.dumps(impact_document))


def test_distribution_fields_are_exact_at_each_depth(impact_document):
    distribution = impact_document["settings"]["debris"]["distribution"]
    for target in (distribution, distribution["settings"]):
        target["extra"] = 1
        with pytest.raises(ValueError, match="remove unknown fields extra"):
            parse_world_proposal(json.dumps(impact_document))
        del target["extra"]
    del distribution["settings"]["source_direction"]
    with pytest.raises(ValueError, match="add required fields source_direction"):
        parse_world_proposal(json.dumps(impact_document))


@pytest.mark.parametrize(
    "distribution",
    [
        None,
        True,
        "transit_stream",
        [],
        {},
        {"kind": None},
        {"kind": True},
        {"kind": 1},
        {"kind": []},
        {"kind": {}},
        {"kind": "arbitrary"},
        {"kind": "directed_stream", "settings": {}},
        {"kind": "transit_stream", "settings": {}},
        {"kind": "transit_stream", "trajectory_adjustment": "toward_lander"},
        {"kind": "impact_ejecta"},
    ],
)
def test_distribution_cannot_expand_the_native_schema(document, distribution):
    document["settings"]["debris"]["distribution"] = distribution
    with pytest.raises(ValueError, match="settings.debris.distribution"):
        parse_world_proposal(json.dumps(document))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonstandard_nonfinite_json_constants_are_rejected(document, value):
    document["settings"]["ship"]["mass_kg"] = value
    with pytest.raises(ValueError, match="use finite, in-range numbers"):
        parse_world_proposal(json.dumps(document))


def test_json_exponent_overflow_names_the_numeric_field(document):
    document["settings"]["ship"]["mass_kg"] = "overflow"
    source = json.dumps(document).replace('"overflow"', "1e9999")
    with pytest.raises(ValueError, match="settings.ship.mass_kg must be finite"):
        parse_world_proposal(source)


def test_excessive_integer_tokens_have_a_bounded_correction(document):
    document["seed"] = "huge_integer"
    source = json.dumps(document).replace('"huge_integer"', "1" * 5000)
    with pytest.raises(ValueError, match="integer is too large|seed must be null or an integer"):
        parse_world_proposal(source)


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_json_keys_are_rejected_even_with_escaped_spelling(document, nested):
    source = json.dumps(document)
    if nested:
        source = source.replace('"mass_kg":', '"mass_\\u006bg": 100, "mass_kg":', 1)
        key = "mass_kg"
    else:
        source = '{"seed": 0,' + source[1:]
        key = "seed"
    with pytest.raises(ValueError, match=f"Duplicate JSON key '{key}'"):
        parse_world_proposal(source)


@pytest.mark.parametrize("as_bytes", [False, True])
def test_input_limit_counts_utf8_bytes_and_accepts_the_exact_limit(document, as_bytes):
    source = json.dumps(document)
    padded = source + " " * (MAX_PROPOSAL_BYTES - len(source.encode("utf-8")))
    proposal = parse_world_proposal(padded.encode("utf-8") if as_bytes else padded)
    assert proposal.digest == parse_world_proposal(source).digest
    oversized = padded + " "
    with pytest.raises(ValueError, match="exceeds 65536 UTF-8 bytes"):
        parse_world_proposal(oversized.encode("utf-8") if as_bytes else oversized)


def test_multibyte_text_cannot_bypass_the_input_limit(document):
    document["schema"] = "é" * (MAX_PROPOSAL_BYTES // 2)
    source = json.dumps(document, ensure_ascii=False)
    assert len(source) < MAX_PROPOSAL_BYTES < len(source.encode("utf-8"))
    with pytest.raises(ValueError, match="exceeds 65536 UTF-8 bytes"):
        parse_world_proposal(source)


@pytest.mark.parametrize("source", [b"\xff", "\ud800", "{}".encode("utf-16")])
def test_input_requires_valid_utf8(source):
    with pytest.raises(ValueError, match="valid UTF-8"):
        parse_world_proposal(source)


@pytest.mark.parametrize("source", [None, {}, [], 1, bytearray(b"{}")])
def test_input_accepts_only_bytes_or_text(source):
    with pytest.raises(ValueError, match="bytes or a string"):
        parse_world_proposal(source)


@pytest.mark.parametrize("source", ["", "```json\n{}\n```", "{} {}", '{"seed":1,}', "\ufeff{}"])
def test_invalid_json_has_a_correction_instead_of_being_extracted_from_prose(source):
    with pytest.raises(ValueError, match="Paste one JSON object without Markdown fences"):
        parse_world_proposal(source)


@pytest.mark.parametrize("source", ["[]", "null", "true", "1", '"proposal"'])
def test_top_level_json_must_be_an_object(source):
    with pytest.raises(ValueError, match="proposal must be a JSON object"):
        parse_world_proposal(source)


@pytest.mark.parametrize("depth", [17, 5000])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_excessive_json_nesting_is_rejected_with_a_correction(depth, as_bytes):
    source = "[" * depth + "0" + "]" * depth
    with pytest.raises(ValueError, match="deeply nested"):
        parse_world_proposal(source.encode("utf-8") if as_bytes else source)


@pytest.mark.parametrize(
    ("opening", "closing", "correction"),
    [("[", "]", "proposal must be a JSON object"), ('{"value":', "}", "add required fields")],
)
def test_depth_boundary_is_checked_before_root_and_field_validation(opening, closing, correction):
    at_limit = opening * 16 + "0" + closing * 16
    with pytest.raises(ValueError, match=correction):
        parse_world_proposal(at_limit)
    over_limit = opening + at_limit + closing
    with pytest.raises(ValueError, match="at most 16 container levels"):
        parse_world_proposal(over_limit)


def test_unknown_fields_cannot_hide_excessive_mixed_container_depth(document):
    source = json.dumps(document)
    nested = '{"value":[' * 8 + "0" + "]}" * 8
    source = source[:-1] + ',"unknown":' + nested + "}"
    with pytest.raises(ValueError, match="deeply nested"):
        parse_world_proposal(source)


@pytest.mark.parametrize(
    "value",
    [
        "[" * 5000,
        "}" * 5000,
        '[{]}"\\' * 100,
        "\\" * 3 + '"' + "[" * 100,
        'quoted \\" [ bracket',
        "\\u0022" + "[" * 100,
    ],
)
def test_string_brackets_and_escaped_quotes_do_not_count_as_container_depth(value):
    source = json.dumps(value)
    with pytest.raises(ValueError, match="proposal must be a JSON object"):
        parse_world_proposal(source)
    # Brackets in field names must stay strings too, not hide or add depth.
    source = json.dumps({value: 0})
    with pytest.raises(ValueError, match="add required fields"):
        parse_world_proposal(source)


@pytest.mark.parametrize("prefix", ["\\", "\\\\", '"', '\\"'])
def test_escaped_string_end_does_not_hide_later_container_depth(prefix):
    source = '{"prefix":' + json.dumps(prefix) + ',"nested":' + "[" * 16 + "0" + "]" * 16 + "}"
    with pytest.raises(ValueError, match="deeply nested"):
        parse_world_proposal(source)
