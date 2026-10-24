"""Pure, privacy-limited experiment exports. No files, clients or downloads here."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .experiment import HISTORY_LIMIT, TELEMETRY_FIELDS

HISTORY_COLUMNS = (
    "segment",
    "run_index",
    "sample_source",
    "telemetry_time_s",
    "telemetry_epoch",
    "telemetry_seed",
    "telemetry_epoch_started_at_s",
    "telemetry_status",
    "telemetry_clock",
    "observation_time_s",
    "observation_seed",
    "observation_clock",
    "frame",
    "radar_clock",
    "rotation_convention",
    "manual_interventions",
    "manual_intervention",
    "manual_intervention_source",
    *TELEMETRY_FIELDS,
    "ship_mass_kg",
    "ship_inertia_xx_kg_m2",
    "ship_inertia_yy_kg_m2",
    "ship_inertia_zz_kg_m2",
    "reference_accel_limit_mps2",
    "reference_angular_accel_limit_rad_s2",
    *(f"command_accel_body_{axis}_mps2" for axis in "xyz"),
    *(f"command_angular_accel_body_{axis}_rad_s2" for axis in "xyz"),
)
_PUBLIC_SNAPSHOT_KEYS = (
    "observation",
    "telemetry",
    "navigation",
    "runtime",
    "engine_build",
    "cameras",
)
_PRIVATE_KEYS = (
    "token",
    "authorization",
    "password",
    "secret",
    "credential",
    "connection",
    "client",
    "headers",
    "apikey",
)


def json_safe(value: Any) -> Any:
    """Copy JSON-shaped data, redact credential keys and keep missing values null.

    Nonfinite numeric readings become null, not zero. Unknown objects are
    rejected without inspecting private attributes or calling their repr.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return json_safe(float(value))
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, Mapping):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("Export field names must be strings")
            normalized = "".join(char for char in key.lower() if char.isalnum())
            if key.startswith("_") or any(part in normalized for part in _PRIVATE_KEYS):
                continue
            result[key] = json_safe(child)
        return result
    if isinstance(value, (list, tuple)):
        return [json_safe(child) for child in value]
    raise TypeError("Export accepts only public JSON-shaped data and numeric arrays")


def json_bytes(value: Any) -> bytes:
    """Encode strict UTF-8 JSON without NaN/Infinity or object introspection."""
    return (json.dumps(json_safe(value), allow_nan=False, indent=2) + "\n").encode("utf-8")


def source_manifest(sources: Mapping[str, str | bytes]) -> list[dict[str, Any]]:
    """Hash supplied actual source bytes. Labels must say what was captured."""
    manifest = []
    for label, source in sorted(sources.items()):
        data = source.encode("utf-8") if isinstance(source, str) else source
        manifest.append({"label": label, "sha256": hashlib.sha256(data).hexdigest()})
    return manifest


def _history_rows(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        json_safe({key: row.get(key) for key in HISTORY_COLUMNS})
        for row in history[-HISTORY_LIMIT:]
    ]


def history_csv(history: Sequence[Mapping[str, Any]]) -> bytes:
    """Export source-qualified rows. Empty cells mean unavailable, never zero."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=HISTORY_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _history_rows(history):
        # Text labels should remain text when opened in spreadsheet programs.
        writer.writerow(
            {
                key: "'" + value
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r"))
                else value
                for key, value in row.items()
            }
        )
    return output.getvalue().encode("utf-8")


def experiment_document(
    *,
    applied_config: Mapping[str, Any] | None,
    draft_config: Mapping[str, Any],
    selected_policy: str,
    schedule_s: Sequence[float],
    history: Sequence[Mapping[str, Any]],
    latest_snapshot: Mapping[str, Any] | None,
    sources: Mapping[str, str | bytes],
    camera_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a versioned local-practice record from explicitly supplied public data.

    Applied controller settings are separate from draft edits and the world's
    actual reported reset settings. The caller supplies saved/current source
    labels honestly; hashes do not prove that saved files ran in this session.
    """
    latest = (
        {key: latest_snapshot[key] for key in _PUBLIC_SNAPSHOT_KEYS if key in latest_snapshot}
        if latest_snapshot is not None
        else None
    )
    observation = latest.get("observation", {}) if latest else {}
    observation = observation if isinstance(observation, Mapping) else {}
    rows = _history_rows(history)
    return json_safe(
        {
            "schema": "wsorb.local_experiment",
            "schema_version": 1,
            "claims": {
                "backend": "local Bevy/Rapier practice",
                "official_score": False,
                "stp_physics_parity": False,
                "history": ("At most 2000 received telemetry samples, not every physics substep"),
                "applied_config_scope": (
                    "Latest accepted policy action; history may span earlier runs"
                ),
                "missing_values": "null in JSON; empty cells in CSV",
                "source_hashes": "Supplied source bytes, not proof of licensed SDK execution",
            },
            "applied_config": applied_config,
            "draft_config": draft_config,
            "selected_policy": selected_policy,
            "schedule_s": schedule_s,
            "applied_world_settings": observation.get("experiment_settings"),
            "frames_and_clocks": {
                key: observation.get(key) for key in ("frame", "rotation_convention", "radar_clock")
            },
            "engine_build": latest.get("engine_build") if latest else None,
            "python_sources": source_manifest(sources),
            "latest_camera_status": camera_status,
            "manual_interventions": [row for row in rows if row["manual_intervention"]],
            "measured_history": rows,
            "latest_public_snapshot": latest,
        }
    )
