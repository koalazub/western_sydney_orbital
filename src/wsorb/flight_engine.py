"""Compatibility access to an owned native headless WASM trial.

This uses the existing local API and isolated server, not the licensed STP
runtime. Policies receive public observations only. Build with ``just build``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from copy import deepcopy
from pathlib import Path
from typing import Any

from .checkpoint import ROOT, isolated_simulation
from .experiment import Decision
from .local_api import (
    FLIGHT_PRESETS,
    MAX_STEP_SECONDS,
    LocalSimulation,
    validate_experiment_settings,
)

EXECUTION_KIND = "owned_native_manual_step"


class EngineError(RuntimeError):
    """The owned native engine rejected a request or became unavailable."""


class HeadlessSimulation:
    """Small public-observation facade over ``isolated_simulation``.

    The native headless scheduler runs without a renderer. ``step`` delegates
    to the existing paused ``run_step`` API, not asynchronous controller leases.
    A failed request closes the owned context and is never retried.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        node: str = "node",
        timeout_s: float = 30.0,
    ) -> None:
        self.root = ROOT if root is None else Path(root).resolve()
        if self.root != ROOT:
            raise ValueError("root overrides are unsupported; the isolated server uses repo ROOT")
        if node != "node":
            raise ValueError("node overrides are unsupported; use the project development PATH")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not 0 < timeout_s <= 30
            or not math.isfinite(timeout_s)
        ):
            raise ValueError("timeout_s must be finite, positive and at most 30")
        self.timeout_s = float(timeout_s)
        self._context: AbstractContextManager[LocalSimulation] | None = None
        self._client: LocalSimulation | None = None
        self._metadata: dict[str, Any] = {}
        self._scenario = json.loads((ROOT / "simulation/scenarios/phobos_escape.json").read_text())

    @property
    def scenario(self) -> dict[str, Any]:
        """Copy of the scenario source, not a claim about an implicit world reset."""
        return deepcopy(self._scenario)

    @property
    def metadata(self) -> dict[str, Any]:
        return deepcopy(self._metadata)

    def __enter__(self) -> HeadlessSimulation:
        if self._context is not None:
            raise EngineError("This engine context is already open")
        paths = {
            "wasm_sha256": "worker/public/sim/wsorb_bevy_bg.wasm",
            "module_sha256": "worker/public/sim/wsorb_bevy.js",
            "manifest_sha256": "worker/public/sim/manifest.json",
            "scenario_source_sha256": "simulation/scenarios/phobos_escape.json",
            "driver_sha256": "worker/scripts/headless.mjs",
            "launcher_sha256": "worker/scripts/local.mjs",
            "bridge_sha256": "worker/web/local-api.js",
            "local_api_adapter_sha256": "src/wsorb/local_api.py",
        }
        try:
            sources = {name: (ROOT / path).read_bytes() for name, path in paths.items()}
            hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
            manifest = json.loads(sources["manifest_sha256"])
            self._scenario = json.loads(sources["scenario_source_sha256"])
            if manifest != {
                "sha256": hashes["wasm_sha256"],
                "bytes": len(sources["wasm_sha256"]),
            }:
                raise EngineError("Engine bytes do not match the manifest; run just build first")
        except (OSError, ValueError) as error:
            raise EngineError("Engine assets unavailable; run just build first") from error
        try:
            context = isolated_simulation()
            self._client = context.__enter__()
            self._context = context
            self._client.timeout_s = self.timeout_s
            snapshot = self._request(lambda client: client.snapshot())
            if snapshot.get("engine_build") != manifest:
                raise EngineError("Native engine build receipt does not match the actual manifest")
            self._metadata = {
                **hashes,
                "engine_build": manifest,
                "backend": "native_headless",
                "driver_source": paths["driver_sha256"],
                "control_mode": "owned_native_API_manual_step",
                "execution_kind": EXECUTION_KIND,
                "execution_note": "Paused run_step commands, not asynchronous leased control.",
                "maximum_step_s": MAX_STEP_SECONDS,
                "scenario_source": self.scenario,
                "scenario_source_note": (
                    "Source on disk is copied and hashed; the manifest and WASM hash identify "
                    "the executed binary. Rebuild after scenario changes."
                ),
            }
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, EngineError) or not isinstance(error, Exception):
                raise
            raise EngineError(str(error)) from error

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        context, self._context = self._context, None
        self._client = None
        if context is not None:
            context.__exit__(None, None, None)

    def _request(self, call: Callable[[LocalSimulation], dict[str, Any]]) -> dict[str, Any]:
        if self._client is None:
            raise EngineError("Open the engine with a with statement first")
        try:
            snapshot = call(self._client)
            if not isinstance(snapshot.get("observation"), dict):
                raise EngineError("Native public observation is unavailable")
            return snapshot
        except BaseException as error:
            self.close()
            if not isinstance(error, Exception):
                raise
            raise EngineError(
                "Native engine request failed; owned context closed without retry"
            ) from error

    def observation(self) -> dict[str, Any]:
        return self._request(lambda client: client.snapshot())["observation"]

    def configure_flight(
        self, settings: Mapping[str, Any], preset: str, seed: int
    ) -> dict[str, Any]:
        parsed = validate_experiment_settings(settings)
        if not isinstance(preset, str) or preset not in FLIGHT_PRESETS:
            raise ValueError("Unknown flight preset")
        if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
            raise ValueError("seed must be an integer between 0 and 4294967295")
        return self._request(lambda client: client.configure_flight(parsed, preset, seed))[
            "observation"
        ]

    def step(
        self,
        accel: Sequence[float],
        angular_accel: Sequence[float],
        seconds: float,
    ) -> dict[str, Any]:
        command = Decision(accel, angular_accel)
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 0 < seconds <= MAX_STEP_SECONDS
            or not math.isfinite(seconds)
        ):
            raise ValueError(f"seconds must be finite, positive and at most {MAX_STEP_SECONDS}")
        return self._request(
            lambda client: client.run_step(command.accel, command.angular_accel, seconds=seconds)
        )["observation"]
