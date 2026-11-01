"""Offline JSON proposals for a new local world, including pasted NotebookLM output.

NotebookLM has no outbound execution role here. Parsing and approval produce
data only, with no clients, file access or commands. The existing explicit
new-world replacement workflow remains the only way to apply a draft.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from .local_api import FLIGHT_PRESETS, validate_experiment_settings

MAX_PROPOSAL_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 16
_SCHEMA = "wsorb.local_world_proposal"
_DOCUMENT_FIELDS = frozenset({"schema", "schema_version", "preset", "seed", "settings"})
_DEBRIS_FIELDS = frozenset(
    {
        "count",
        "radius_min_m",
        "radius_max_m",
        "speed_mps",
        "speed_spread_mps",
        "shell_inner_m",
        "shell_outer_m",
        "stream_axis",
        "stream_sign",
    }
)
_EJECTA_FIELDS = frozenset(
    {"source_direction", "kick_velocity_mps", "tangential_speed_mps", "patch_half_angle_deg"}
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Duplicate JSON key {name!r}; keep each field only once")
        result[name] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Proposal JSON cannot contain {value}; use finite, in-range numbers")


def _json_integer(value: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(
            "Proposal integer is too large; use numbers within the local settings bounds"
        ) from error


def _check_json_depth(text: str) -> None:
    """Bound all container nesting before decoding, without counting quoted brackets.

    This scan only checks depth. The JSON decoder still validates syntax.
    """
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise ValueError(
                    "Proposal JSON is too deeply nested; use at most 16 container levels "
                    "and only the documented objects and vectors"
                )
        elif character in "]}":
            depth = max(0, depth - 1)


def _load_document(source: bytes | str) -> Any:
    if not isinstance(source, (bytes, str)):
        raise ValueError("Proposal input must be UTF-8 JSON bytes or a string")
    if len(source) > MAX_PROPOSAL_BYTES:
        raise ValueError("Proposal exceeds 65536 UTF-8 bytes; export only the proposal JSON")
    try:
        data = source.encode("utf-8") if isinstance(source, str) else source
        if len(data) > MAX_PROPOSAL_BYTES:
            raise ValueError("Proposal exceeds 65536 UTF-8 bytes; export only the proposal JSON")
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("Proposal must be valid UTF-8; export the JSON as UTF-8") from error
    _check_json_depth(text)
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_int=_json_integer,
        )
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid proposal JSON at line {error.lineno}, column {error.colno}: {error.msg}. "
            "Paste one JSON object without Markdown fences or surrounding text"
        ) from error
    except RecursionError as error:
        raise ValueError(
            "Proposal JSON is too deeply nested; use only the documented objects and vectors"
        ) from error


def _require_fields(
    value: Any, path: str, required: frozenset[str], optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a JSON object with fields {', '.join(sorted(required))}")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    corrections = []
    if missing:
        corrections.append(f"add required fields {', '.join(sorted(missing))}")
    if unknown:
        corrections.append(f"remove unknown fields {', '.join(sorted(unknown))}")
    if corrections:
        raise ValueError(f"{path}: {'; '.join(corrections)}")
    return value


def _settings_shape(settings: Any) -> dict[str, Any]:
    settings = _require_fields(settings, "settings", frozenset({"ship", "debris"}))
    _require_fields(settings["ship"], "settings.ship", frozenset({"mass_kg", "inertia_scale"}))
    debris = _require_fields(
        settings["debris"],
        "settings.debris",
        _DEBRIS_FIELDS,
        frozenset({"distribution", "trajectory_adjustment"}),
    )
    # New scenario settings select transit_stream explicitly. Older documents
    # without distribution still normalize to legacy directed_stream, not that default.
    if "distribution" in debris:
        path = "settings.debris.distribution"
        distribution = _require_fields(
            debris["distribution"], path, frozenset({"kind"}), frozenset({"settings"})
        )
        kind = distribution["kind"]
        if isinstance(kind, str) and kind in (
            "directed_stream",
            "transit_stream",
            "official_cloud",
        ):
            _require_fields(distribution, path, frozenset({"kind"}))
        elif kind == "impact_ejecta":
            _require_fields(distribution, path, frozenset({"kind", "settings"}))
            _require_fields(distribution["settings"], f"{path}.settings", _EJECTA_FIELDS)
        else:
            raise ValueError(
                f"{path}.kind must be directed_stream, transit_stream, official_cloud "
                "or impact_ejecta"
            )
    return settings


def _strict_numbers(value: Any, path: str, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"{path} is too deeply nested; use the documented objects and vectors")
    if isinstance(value, bool):
        raise ValueError(f"{path} cannot be a boolean; use the documented number or string")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite; use an in-range number")
    if isinstance(value, dict):
        for name, child in value.items():
            _strict_numbers(child, f"{path}.{name}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _strict_numbers(child, f"{path}[{index}]", depth + 1)


def _normalize_zero(value: Any) -> Any:
    if isinstance(value, float):
        return 0.0 if value == 0 else value
    if isinstance(value, dict):
        return {name: _normalize_zero(child) for name, child in value.items()}
    if isinstance(value, list):
        return [_normalize_zero(child) for child in value]
    return value


def _validate_document(document: Any) -> dict[str, Any]:
    document = _require_fields(document, "proposal", _DOCUMENT_FIELDS)
    if document["schema"] != _SCHEMA:
        raise ValueError(f"schema must be {_SCHEMA!r}")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("schema_version must be the integer 1")
    preset = document["preset"]
    if not isinstance(preset, str) or preset not in FLIGHT_PRESETS:
        raise ValueError(f"preset must be one of {', '.join(FLIGHT_PRESETS)}")
    seed = document["seed"]
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF):
        raise ValueError("seed must be null or an integer from 0 to 4294967295")
    settings = _settings_shape(document["settings"])
    _strict_numbers(settings, "settings")
    try:
        settings = validate_experiment_settings(settings)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError(f"settings: {error}") from error
    return {
        "schema": _SCHEMA,
        "schema_version": 1,
        "preset": preset,
        "seed": seed,
        "settings": _normalize_zero(settings),
    }


@dataclass(frozen=True, slots=True, init=False)
class WorldProposal:
    """Validated, immutable review data. Documents returned to callers are fresh copies.

    Canonical JSON uses native numeric types, legacy distribution for omissions,
    omitted off trajectory adjustment, positive floating-point zero, sorted keys,
    compact separators and UTF-8 without a trailing newline. The digest is its
    lowercase SHA-256 hex.
    A digest identifies content, not its author or proof of human review.
    """

    _json_bytes: bytes = field(repr=False)

    def __init__(self, source: bytes | str) -> None:
        document = _validate_document(_load_document(source))
        text = json.dumps(document, allow_nan=False, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "_json_bytes", text.encode("utf-8"))

    @property
    def digest(self) -> str:
        """SHA-256 of the exact normalized JSON shown for review."""
        return hashlib.sha256(self._json_bytes).hexdigest()

    def to_document(self) -> dict[str, Any]:
        """Return a detached, mutable copy of the complete versioned proposal."""
        return json.loads(self._json_bytes)

    def to_json_bytes(self) -> bytes:
        """Return immutable canonical JSON bytes, without performing any I/O."""
        return self._json_bytes


def parse_world_proposal(source: bytes | str) -> WorldProposal:
    """Validate up to 64 KiB and 16 JSON container levels. Raise actionable ValueError."""
    return WorldProposal(source)


def approve_world_proposal(proposal: WorldProposal, digest: str) -> dict[str, Any]:
    """Return only preset/seed/settings draft data for the exact reviewed digest.

    Keep the digest from the review step, not one recomputed from later input.
    Revalidate a captured copy before checking identity and extracting its draft.
    This never tunes a ship or debris flow, replaces a world, starts or resets
    flight, or writes Git.
    """
    if type(proposal) is not WorldProposal:
        raise ValueError(
            "Approval requires a parsed WorldProposal; parse and review its JSON first"
        )
    reviewed = parse_world_proposal(proposal.to_json_bytes())
    if type(digest) is not str or digest != reviewed.digest:
        raise ValueError(
            "Approval digest does not match this proposal. Review the current normalized JSON "
            "and supply its exact SHA-256 digest"
        )
    document = reviewed.to_document()
    return {name: document[name] for name in ("preset", "seed", "settings")}
