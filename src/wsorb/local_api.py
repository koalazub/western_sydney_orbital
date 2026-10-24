"""Python access to the real local browser simulation, not the STP SDK.

Start the built game with ``WSORB_LAB=1 just --no-deps local`` and open its
printed URL in a WebGPU browser. The loopback server relays requests to that
browser's existing WASM instance. No browser means no simulation results.

Use one command writer. Raw commands can leave thrust installed until ``stop``;
``run_step`` instead pauses, advances a bounded interval and stops in ``finally``.
No request is retried, since a lost response does not prove a command failed.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import numpy as np

DEFAULT_CONNECTION = Path(".devenv/wsorb-local.json")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_STEP_SECONDS = 1.0
COMMANDS = frozenset(
    {
        "pause",
        "play",
        "step",
        "reset",
        "set_command",
        "stop",
        "view",
        "debug_upright",
        "configure_experiment",
    }
)


class LocalApiError(RuntimeError):
    """The local transport or actual browser engine rejected a request."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the local bearer token to a redirect target.
        return None


def _vector(value: Sequence[float], name: str) -> list[float]:
    array = np.asarray(value)
    if array.shape != (3,) or array.dtype.kind not in "iuf" or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a finite real vector of shape (3,)")
    return array.astype(float).tolist()


def _seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds must be a finite number between 0 and 1")
    if not math.isfinite(value) or not 0 <= value <= MAX_STEP_SECONDS:
        raise ValueError("seconds must be a finite number between 0 and 1")
    return float(value)


def _connection(value: Any) -> tuple[str, str]:
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise LocalApiError("Unsupported local connection file version")
    url, token = value.get("url"), value.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        raise LocalApiError("Invalid local connection file")
    try:
        parsed = urlsplit(url)
        valid_url = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid_url = False
    if not valid_url or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise LocalApiError("Connection must use an authenticated 127.0.0.1 HTTP endpoint")
    return url.rstrip("/"), token


class LocalSimulation:
    """A synchronous, authenticated client for one local browser engine."""

    topCamera = "top"
    bottomCamera = "bottom"

    def __init__(self, url: str, token: str, *, timeout_s: float = 8.0):
        self.url, self._token = _connection({"version": 1, "url": url, "token": token})
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
            raise ValueError("timeout_s must be between 0 and 30")
        self.timeout_s = timeout_s
        # Ignore proxy environment variables for this loopback-only connection.
        self._http = build_opener(ProxyHandler({}), _NoRedirects())

    @classmethod
    def from_file(
        cls, path: str | Path | None = None, *, timeout_s: float = 8.0
    ) -> LocalSimulation:
        """Read a private connection file. This does not send any command."""
        path = Path(path or os.environ.get("WSORB_LOCAL_CONNECTION", DEFAULT_CONNECTION))
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 8192:
                raise LocalApiError(
                    "Local connection file must be a private regular file, mode 0600"
                )
            url, token = _connection(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            raise LocalApiError(
                f"Cannot read {path}. Start the game with WSORB_LAB=1 and connect again."
            ) from error
        return cls(url, token, timeout_s=timeout_s)

    def _request(
        self,
        route: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.url}/__local/{route}", data=data, headers=headers)
        try:
            with self._http.open(
                request, timeout=self.timeout_s if timeout_s is None else timeout_s
            ) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            with error:
                detail = error.read(8192)
            try:
                message = json.loads(detail).get("error", f"HTTP {error.code}")
            except (ValueError, AttributeError):
                message = f"HTTP {error.code}"
            raise LocalApiError(str(message)) from error
        except (OSError, URLError) as error:
            raise LocalApiError(
                f"Local simulator unavailable at {self.url}. Command outcome may be unknown; "
                "do not automatically retry thrust. Check the game and reconnect."
            ) from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise LocalApiError("Local simulator response exceeds the 2 MiB limit")
        try:
            result = json.loads(body)
        except ValueError as error:
            raise LocalApiError("Local simulator returned invalid JSON") from error
        if not isinstance(result, dict):
            raise LocalApiError("Local simulator returned an invalid response")
        return result

    def status(self) -> dict[str, Any]:
        """Read connection readiness without issuing a browser or flight command."""
        return self._request("status")

    def _call(
        self, payload: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        result = self._request("call", payload, timeout_s=timeout_s)
        if result.get("ok") is not True:
            raise LocalApiError(str(result.get("error", "Local browser request failed")))
        value = result.get("value")
        if not isinstance(value, dict):
            raise LocalApiError("Local browser returned an invalid result")
        return value

    def snapshot(self) -> dict[str, Any]:
        """Read public observations and actual telemetry, navigation and runtime reports.

        This does not return the debug snapshot's hidden debris truth. Each report
        keeps its own timestamps; sensor/telemetry cadence can differ from physics.
        """
        return self._call({"method": "snapshot"})

    def GetNearestDebrisLocations(self, N: int) -> list[np.ndarray]:
        """Return public 3×3 radar rows using the official helper's name and shape.

        This is the local adapter, not an SDK call. It preserves measurement
        timestamps and row ordering, without adding IDs or debris velocities.
        """
        if type(N) is not int or N < 0:
            raise ValueError("N must be a non-negative integer")
        rows = self.snapshot()["observation"]["radar"]
        return [np.asarray(row, dtype=np.float64) for row in rows[:N]]

    def SetCommand(self, accelCommand: Sequence[float], angAccelCommand: Sequence[float]) -> None:
        """Use the official body-frame vector contract through the local adapter.

        The licensed STPClient implements this same helper with real SetParam
        calls. Neither policy code nor its commands need Marimo or Rust imports.
        """
        accel = _vector(accelCommand, "accelCommand")
        angular_accel = _vector(angAccelCommand, "angAccelCommand")
        self.command({"op": "set_command", "accel": accel, "angular_accel": angular_accel})

    def preview(self, report: Mapping[str, Any] | None) -> None:
        """Display a Python-computed advisory, or clear it with None. Never steer."""
        self._call({"method": "preview", "preview": None if report is None else dict(report)})

    def camera_status(self) -> dict[str, Any]:
        """Read actual requested aim, optics and sampled ship-camera metadata."""
        status = self.snapshot()["cameras"]
        if not isinstance(status, dict):
            raise LocalApiError("The real ship cameras have not initialized")
        return status

    def PanTiltCamera(self, camera: str, yaw_deg: float, pitch_deg: float) -> None:
        """Apply the official helper's degree convention and asymmetric clipping.

        Local handles are topCamera/bottomCamera strings; STPClient uses real
        SDK entity handles. FOV, resolution and exposure are not changed.
        """
        if camera not in (self.topCamera, self.bottomCamera):
            raise ValueError("camera must be topCamera or bottomCamera")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (yaw_deg, pitch_deg)
        ):
            raise ValueError("camera angles must be finite numbers")
        self._call(
            {
                "method": "pan_tilt",
                "camera": camera,
                "yaw_deg": float(yaw_deg),
                "pitch_deg": float(pitch_deg),
            }
        )

    def capture_image(self, camera: str) -> dict[str, Any]:
        """Read a real GPU frame with no enhancement, overlays or replacement pixels.

        Returns metadata plus immutable uint8 RGB of shape (512,512,3).
        The readback has its own bounded channel, separate from flight writes.
        This local convenience is not the SDK's CaptureImageBlocking function.
        """
        if camera not in (self.topCamera, self.bottomCamera):
            raise ValueError("camera must be topCamera or bottomCamera")
        result = self._call(
            {"method": "capture", "camera": camera}, timeout_s=max(25.0, self.timeout_s)
        )
        metadata, encoded = result.get("metadata"), result.get("rgb_base64")
        if not isinstance(metadata, dict) or not isinstance(encoded, str):
            raise LocalApiError("Camera returned an invalid capture")
        if (
            metadata.get("width"),
            metadata.get("height"),
            metadata.get("channels"),
            metadata.get("bits_per_channel"),
        ) != (512, 512, 3, 8):
            raise LocalApiError("Camera capture does not match 512×512 RGB8")
        try:
            pixels = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise LocalApiError("Camera returned invalid RGB data") from error
        if len(pixels) != 512 * 512 * 3:
            raise LocalApiError("Camera returned an incomplete RGB frame")
        rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(512, 512, 3)
        return {"metadata": metadata, "rgb": rgb}

    def command(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Forward a local API operation. Queued playback ACKs are not applied state.

        These operations are not official STP SDK calls. Prefer ``run_step`` for
        experiments; raw ``set_command`` persists until replaced or stopped.
        """
        op = request.get("op")
        if not isinstance(op, str) or op not in COMMANDS:
            raise ValueError("Unsupported local lab command; read data through snapshot()")
        return self._call({"method": "command", "command": dict(request)})

    def pause(self) -> None:
        """Wait for the actual runtime to pause before allowing a manual step."""
        self.command({"op": "pause"})
        deadline = time.monotonic() + self.timeout_s
        while self.snapshot()["runtime"]["running"]:
            if time.monotonic() >= deadline:
                raise LocalApiError("Pause has not applied. Keep the game visible and try Stop.")
            time.sleep(0.05)

    def stop(self) -> None:
        """Zero requested thrust and torque. This does not brake the lander."""
        self.command({"op": "stop"})

    def reset(self, seed: int | None = None) -> dict[str, Any]:
        if seed is not None and (type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF):
            raise ValueError("seed must be an integer between 0 and 4294967295")
        self.pause()
        self.command({"op": "reset", "seed": seed})
        return self.snapshot()

    def upright(self) -> dict[str, Any]:
        """Local test reset: pause, align to the pad, clear spin/thrust, record it.

        This is not an STP command or a physically flown maneuver. Position,
        linear velocity, time, fuel and damage are preserved by the engine.
        """
        self.pause()
        self.command({"op": "debug_upright"})
        return self.snapshot()

    def configure_experiment(
        self, settings: Mapping[str, Any], seed: int | None = None
    ) -> dict[str, Any]:
        """Pause and reset the real local world with explicit bounded what-if settings.

        Ship mass/inertia and asteroid fields are local experiments, not SDK
        hardware setters. Reference acceleration limits remain unchanged.
        """
        if seed is not None and (type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF):
            raise ValueError("seed must be an integer between 0 and 4294967295")
        payload = {"op": "configure_experiment", "settings": dict(settings), "seed": seed}
        json.dumps(payload, allow_nan=False)
        self.pause()
        self.command(payload)
        return self.snapshot()

    def step(self, seconds: float) -> dict[str, Any]:
        seconds = _seconds(seconds)
        self.pause()
        self.command({"op": "step", "seconds": seconds})
        return self.snapshot()

    def run_step(
        self,
        accel: Sequence[float],
        angular_accel: Sequence[float] = (0.0, 0.0, 0.0),
        *,
        seconds: float = 0.05,
    ) -> dict[str, Any]:
        """Apply body-frame m/s² and rad/s² for one paused, bounded physics step.

        The actual engine clamps reference vector norms to 0.5 and 0.05. This
        uses the local command actuator, not keyboard nozzles or Space boost.
        Always attempts Stop after taking command ownership. Delivery cannot be
        guaranteed if the browser/connection fails. Returned data is sampled
        before that cleanup, so it can describe the force used during the step.
        """
        accel = _vector(accel, "accel")
        angular_accel = _vector(angular_accel, "angular_accel")
        seconds = _seconds(seconds)
        self.pause()
        try:
            self.SetCommand(accel, angular_accel)
            self.command({"op": "step", "seconds": seconds})
            return self.snapshot()
        finally:
            self.stop()
