"""Synchronous Python access to the actual local WASM physics, without a browser.

This uses the same compiled engine as the game, not the licensed STP runtime.
Only public observations are exposed to policies. Rendering and camera images
are outside this adapter. Build the engine with ``just build`` before use.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from .experiment import Decision
from .local_api import FLIGHT_PRESETS, validate_experiment_settings


class EngineError(RuntimeError):
    """The actual engine rejected a request or its process became unavailable."""


class HeadlessSimulation:
    """One isolated WASM instance, owned exclusively by its Python context.

    Each step holds a command over an explicit simulation interval. There is
    no wall-clock pacing or background worker. A timed-out request closes the
    process; it is never retried against an uncertain world state.

    This isolated driver uses the engine's legacy SetCommand path, not live
    controller consent or expiring leases. It owns the whole process, advances
    only on explicit steps, and clears the command after each returned sample.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        node: str = "node",
        timeout_s: float = 30.0,
    ) -> None:
        if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        self.root = Path(root or Path(__file__).resolve().parents[2]).resolve()
        self.node = node
        self.timeout_s = float(timeout_s)
        self._process: subprocess.Popen | None = None
        self._stderr = None
        self._buffer = b""
        self._metadata: dict[str, Any] = {}
        self._scenario = json.loads(
            (self.root / "simulation/scenarios/phobos_escape.json").read_text()
        )

    @property
    def scenario(self) -> dict[str, Any]:
        """Current checked-in scenario source; never an implicit world reset."""
        return deepcopy(self._scenario)

    @property
    def metadata(self) -> dict[str, Any]:
        return deepcopy(self._metadata)

    def __enter__(self) -> HeadlessSimulation:
        if self._process is not None:
            raise EngineError("This engine context is already open")
        paths = {
            "wasm_sha256": "worker/public/sim/wsorb_bevy_bg.wasm",
            "module_sha256": "worker/public/sim/wsorb_bevy.js",
            "scenario_source_sha256": "simulation/scenarios/phobos_escape.json",
            "driver_sha256": "worker/scripts/engine-driver.mjs",
        }
        try:
            hashes = {
                name: hashlib.sha256((self.root / path).read_bytes()).hexdigest()
                for name, path in paths.items()
            }
        except OSError as error:
            raise EngineError("Engine assets unavailable; run just build first") from error
        self._stderr = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(
                [self.node, str(self.root / "worker/scripts/engine-driver.mjs")],
                cwd=self.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                bufsize=0,
            )
            self._buffer = b""
            runtime = self._query("driver_metadata")
            self._metadata = {
                **hashes,
                **runtime,
                "backend": "local_wasm_without_renderer",
                "control_mode": "isolated_legacy_command_step_stop",
                "scenario_source": self.scenario,
                "scenario_source_note": (
                    "Hash identifies the source on disk; the WASM hash identifies the executed "
                    "binary. Rebuild after scenario changes."
                ),
            }
            self.observation()  # Fail at connection, rather than in the first trial.
            return self
        except OSError as error:
            self.close()
            raise EngineError(
                "Cannot start Node; use the project development environment"
            ) from error
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        self._buffer = b""

    def _query(self, method: str, **payload: Any) -> Any:
        process = self._process
        if process is None:
            raise EngineError("Open the engine with a with statement first")
        request = json.dumps({"method": method, **payload}, allow_nan=False).encode() + b"\n"
        try:
            process.stdin.write(request)
            deadline = time.monotonic() + self.timeout_s
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while b"\n" not in self._buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise EngineError(f"Engine request {method!r} timed out")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise EngineError("Engine process stopped before returning a response")
                    self._buffer += chunk
                    if len(self._buffer) > 16 * 1024 * 1024:
                        raise EngineError("Engine response exceeded the 16 MiB limit")
            line, self._buffer = self._buffer.split(b"\n", 1)
            result = json.loads(line)
            if not isinstance(result, dict) or not ({"value", "error"} & result.keys()):
                raise EngineError("Malformed engine response")
        except (OSError, ValueError, EngineError) as error:
            self.close()
            if isinstance(error, EngineError):
                raise
            raise EngineError("Engine transport failed") from error
        if "error" in result:
            raise EngineError(str(result["error"]))
        return result["value"]

    def observation(self) -> dict[str, Any]:
        return self._query("observation")

    def configure_flight(
        self, settings: Mapping[str, Any], preset: str, seed: int
    ) -> dict[str, Any]:
        parsed = validate_experiment_settings(settings)
        if preset not in FLIGHT_PRESETS:
            raise ValueError("Unknown flight preset")
        if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
            raise ValueError("seed must be an integer between 0 and 4294967295")
        self._query(
            "command",
            command={"op": "configure_flight", "settings": parsed, "preset": preset, "seed": seed},
        )
        observation = self.observation()
        if (
            observation.get("time_s") != 0
            or observation.get("seed") != seed
            or observation.get("flight_preset") != preset
            or validate_experiment_settings(observation["experiment_settings"]) != parsed
        ):
            raise EngineError("Engine did not confirm the requested fresh trial")
        return observation

    def step(
        self,
        accel: Sequence[float],
        angular_accel: Sequence[float],
        seconds: float,
    ) -> dict[str, Any]:
        command = Decision(accel, angular_accel)
        maximum = self._scenario["integration"]["maximum_command_step_s"]
        if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 < seconds <= maximum:
            raise ValueError(f"seconds must be finite, positive and at most {maximum}")
        return self._query(
            "step",
            accel=command.accel.tolist(),
            angular_accel=command.angular_accel.tolist(),
            seconds=float(seconds),
        )
