"""Authenticated calls to a hosted room's connected, real WASM engine.

This transport never reads saved room state as an observation. Each snapshot
is a new owner RPC. Only snapshots, native ownership, fenced decision leases
and checked rate-1 Play are supported. No request is retried.

``wsorb room create --connection-file PATH`` reserves a new private descriptor
before creating a room. Its successful browser link is a bearer capability.
Keep both that link and the descriptor private. Creation does not start a
browser, reset a world or send a flight command.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from http.client import HTTPException
from pathlib import Path
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import __version__, diagnostics
from .local_api import (
    MAX_RESPONSE_BYTES,
    LocalApiError,
    LocalSimulation,
    _channels,
    _expected_context,
    _NoRedirects,
    _ownership_command,
    _resilience_command,
)

DEFAULT_ORIGIN = "https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev"
USER_AGENT = f"wsorb-controller/{__version__}"
MAX_DESCRIPTOR_BYTES = 8192
MAX_TIMEOUT_MS = 30_000
MAX_ADMISSION_MS = 10_000
_UNKNOWN_OUTCOME = "Command outcome may be unknown; do not automatically retry actuation."


class RoomApiError(LocalApiError):
    """A credential-free hosted transport or descriptor failure."""


def _private_platform() -> None:
    if os.name != "posix":
        raise RoomApiError(
            "Private room files require POSIX permissions. Use WSL2 on its Linux filesystem; "
            "mode bits are not verified NTFS ACL protection."
        )


def _origin(value: Any, *, allow_loopback: bool = False) -> str:
    message = "Room URL must be a credential-free HTTPS origin with no path, query or fragment."
    if type(allow_loopback) is not bool:
        raise ValueError("allow_loopback must be a boolean test opt-in")
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or not value.isascii()
        or re.search(r"[\x00-\x20\x7f\\?#]", value)
    ):
        raise RoomApiError(message)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
        if ":" in host:
            ipaddress.IPv6Address(host)
            authority = f"[{host}]"
        else:
            if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) is None:
                raise ValueError
            authority = host
        if parsed.netloc.lower() != authority + (f":{port}" if port is not None else ""):
            raise ValueError
        if parsed.scheme == "https":
            return f"https://{authority}" + (f":{port}" if port not in (None, 443) else "")
        if allow_loopback and parsed.scheme == "http" and host == "127.0.0.1" and port:
            return f"http://127.0.0.1:{port}"
    except ValueError:
        pass
    raise RoomApiError(message)


def _room_id(value: Any) -> str:
    try:
        if isinstance(value, str) and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise RoomApiError("Room ID must be a canonical UUID.")


def _descriptor(value: Any, *, allow_loopback: bool = False) -> tuple[str, str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "url", "room_id", "token"}
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise RoomApiError("Invalid version-1 room descriptor.")
    token = value["token"]
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise RoomApiError("Invalid room credential.")
    return _origin(value["url"], allow_loopback=allow_loopback), _room_id(value["room_id"]), token


def _timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_s must be finite and between 0 and 30")
    try:
        valid = math.isfinite(value) and 0 < value <= MAX_TIMEOUT_MS / 1000
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("timeout_s must be finite and between 0 and 30")
    return float(value)


def _private_regular(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_size <= MAX_DESCRIPTOR_BYTES
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _read_descriptor(path: Path) -> Any:
    _private_platform()
    message = "Room descriptor must be a private regular file, mode 0600, at most 8192 bytes."
    try:
        before = path.lstat()
        if not _private_regular(before):
            raise RoomApiError(message)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        with os.fdopen(os.open(path, flags), "rb") as source:
            opened = os.fstat(source.fileno())
            if not _private_regular(opened) or not _same_file(before, opened):
                raise RoomApiError(message)
            body = source.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(body) > MAX_DESCRIPTOR_BYTES:
            raise RoomApiError(message)
        return json.loads(body)
    except (OSError, ValueError):
        raise RoomApiError("Cannot read a valid private room descriptor.") from None


def _owns_private_file(path: Path, owned: os.stat_result) -> bool:
    try:
        current = path.lstat()
        return _private_regular(current) and _same_file(current, owned)
    except OSError:
        return False


def _remove_owned_file(path: Path, owned: os.stat_result) -> None:
    # Never unlink a replacement, symlink or a file that preceded this command.
    if _owns_private_file(path, owned):
        with suppress(OSError):
            path.unlink()


@contextmanager
def _reserve_descriptor(path: Path) -> Iterator[tuple[TextIO, os.stat_result]]:
    _private_platform()
    fd = None
    owned = None
    complete = False
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        owned = os.fstat(fd)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            fd = None
            yield target, owned
        complete = True
    except OSError:
        raise RoomApiError(
            "Cannot reserve or write a new private connection file. Existing paths are never "
            "overwritten. Check the parent directory and choose an unused path."
        ) from None
    finally:
        if fd is not None:
            os.close(fd)
        if not complete and owned is not None:
            _remove_owned_file(path, owned)


def _http_client():
    # Reuse handlers, but never proxy a bearer capability or follow a redirect.
    client = build_opener(ProxyHandler({}), _NoRedirects())
    # Cloudflare-managed workers.dev rejects the default Python-urllib signature.
    # Identify this application honestly, never pretend to be a web browser.
    client.addheaders = [("User-Agent", USER_AGENT)]
    return client


def _post_json(
    http,
    request: Request,
    *,
    timeout_s: float,
    expected_status: int,
    diagnostic_trace: diagnostics.RequestTrace | None = None,
) -> dict[str, Any]:
    trace = diagnostics.start_request("room", "http", method="create_room", parent=diagnostic_trace)
    if trace is not None:
        for name, value in diagnostics.request_headers(trace).items():
            request.add_header(name, value)
    http_status = None
    try:
        try:
            with http.open(request, timeout=timeout_s) as response:
                if trace is not None:
                    http_status = response.status
                if response.status != expected_status:
                    raise RoomApiError(f"Unexpected room HTTP status. {_UNKNOWN_OUTCOME}")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if trace is not None:
                http_status = error.code
            with error:
                failure_body = error.read(8193)
            try:
                failure = json.loads(failure_body) if len(failure_body) <= 8192 else None
            except ValueError:
                failure = None
            if error.code == 503 and failure == {"error": "No connected room owner"}:
                unavailable = RoomApiError(
                    "Hosted room has no connected owner. This request was not dispatched."
                )
                unavailable.code = "owner_unavailable"
                raise unavailable from None
            rejected = RoomApiError(f"Room HTTP request failed. {_UNKNOWN_OUTCOME}")
            rejected.http_status = error.code
            raise rejected from None
        except (OSError, URLError, HTTPException):
            unavailable = RoomApiError(f"Room transport unavailable. {_UNKNOWN_OUTCOME}")
            unavailable.code = "transport_unavailable"
            raise unavailable from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise RoomApiError(f"Room response exceeds the 2 MiB limit. {_UNKNOWN_OUTCOME}")
        try:
            result = json.loads(body)
        except ValueError:
            raise RoomApiError(f"Room returned invalid JSON. {_UNKNOWN_OUTCOME}") from None
        if not isinstance(result, dict):
            raise RoomApiError(f"Room returned an invalid response. {_UNKNOWN_OUTCOME}")
    except BaseException as error:
        diagnostics.finish_request(trace, error=error, http_status=http_status)
        raise
    diagnostics.finish_request(trace, response=result, http_status=http_status)
    return result


def _room_command(request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValueError("Hosted room commands must be typed mappings")
    op = request.get("op")
    if op in ("claim_control", "release_control"):
        return _ownership_command(request)
    if op == "set_leased_command":
        parsed = _resilience_command(request)
        if "controller_fence" not in parsed:
            raise ValueError("Hosted leased commands require the actual native controller_fence")
        return parsed
    if op == "play":
        if (
            set(request) != {"op", "rate", "expected_context"}
            or type(request["rate"]) is not int
            or request["rate"] != 1
        ):
            raise ValueError("Hosted Play requires rate 1 and the complete expected_context")
        return {
            "op": "play",
            "rate": 1,
            "expected_context": _expected_context(request["expected_context"]),
        }
    raise ValueError("Unsupported hosted room operation; use snapshots and fenced native control")


def _room_call(payload: Any) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        if payload.get("method") == "snapshot" and set(payload) == {"method"}:
            return {"method": "snapshot"}
        if payload.get("method") == "command" and set(payload) == {"method", "command"}:
            return {"method": "command", "command": _room_command(payload["command"])}
    raise ValueError("Unsupported hosted room RPC; only snapshot and checked command are supported")


def _rpc_envelope(payload: Mapping[str, Any], timeout_s: float) -> dict[str, Any]:
    timeout = _timeout(timeout_s)
    return {
        "id": str(uuid.uuid4()),
        "command": {
            "version": 1,
            "deadline_ms": int(time.time() * 1000)
            + max(1, min(MAX_ADMISSION_MS, math.ceil(timeout * 1000))),
            **_room_call(payload),
        },
    }


class RoomSimulation(LocalSimulation):
    """A separate hosted transport, not a relaxation of the loopback local client.

    Inherited native validators and ownership helpers retain their exact source
    clocks and fences. All other local operations are rejected before HTTP.
    ``allow_loopback=True`` is only for genuine local Wrangler tests. The CLI
    and checkpoint command never enable it.
    """

    def __init__(
        self,
        url: str,
        room_id: str,
        token: str,
        *,
        timeout_s: float = 8.0,
        allow_loopback: bool = False,
    ) -> None:
        self.url, self.room_id, self._token = _descriptor(
            {"version": 1, "url": url, "room_id": room_id, "token": token},
            allow_loopback=allow_loopback,
        )
        self.timeout_s = _timeout(timeout_s)
        self._channels = _channels(f"{self.url}/api/rooms/{self.room_id}")
        self._http = _http_client()

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        timeout_s: float = 8.0,
        allow_loopback: bool = False,
    ) -> RoomSimulation:
        """Read an explicit private room descriptor without sending any request."""
        url, room_id, token = _descriptor(
            _read_descriptor(Path(path)), allow_loopback=allow_loopback
        )
        return cls(url, room_id, token, timeout_s=timeout_s, allow_loopback=allow_loopback)

    def _result(self, result: Any, request_id: str) -> dict[str, Any]:
        message = f"Room result envelope or request ID does not match. {_UNKNOWN_OUTCOME}"
        if (
            not isinstance(result, dict)
            or result.get("type") != "result"
            or result.get("id") != request_id
            or type(result.get("ok")) is not bool
        ):
            raise RoomApiError(message)
        fields = {"type", "id", "ok"}
        if result["ok"]:
            if set(result) != fields | {"value"} or not isinstance(result["value"], dict):
                raise RoomApiError(message)
            return result["value"]
        if (
            set(result) not in (fields | {"error"}, fields | {"error", "code"})
            or not isinstance(result.get("error"), str)
            or not result["error"]
            or ("code" in result and not isinstance(result["code"], str))
        ):
            raise RoomApiError(message)
        if result.get("code") in ("stale_context", "command_expired"):
            # Do not echo remote text, which may contain credentials or argv.
            raise self._rejection(
                {
                    "code": result["code"],
                    "error": "Native hosted command rejected before application.",
                },
                "Native hosted command rejected before application.",
            )
        raise RoomApiError(f"Hosted room RPC failed. {_UNKNOWN_OUTCOME}")

    def _call(
        self, payload: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        call = _room_call(payload)
        timeout = _timeout(self.timeout_s if timeout_s is None else timeout_s)
        with self._channels.control:
            envelope = _rpc_envelope(call, timeout)
            trace = diagnostics.start_request("room", "rpc", call, request_id=envelope["id"])
            request = Request(
                f"{self.url}/api/rooms/{self.room_id}/commands",
                data=json.dumps(envelope, allow_nan=False).encode(),
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                result = _post_json(
                    self._http,
                    request,
                    timeout_s=timeout,
                    expected_status=200,
                    diagnostic_trace=trace,
                )
                value = self._result(result, envelope["id"])
            except BaseException as error:
                diagnostics.finish_request(trace, error=error)
                raise
            diagnostics.finish_request(trace, value=value)
            return value

    def command(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Send one allowed native operation. An ACK is not applied-state proof."""
        return self._call({"method": "command", "command": _room_command(request)})

    def _unsupported(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError(
            "Unsupported hosted room operation; use snapshots and fenced native control"
        )

    # Some local helpers take a snapshot before writing. Block the helpers
    # themselves, not just their eventual command, so they perform no HTTP.
    _request = _unsupported
    bind_to_observed_page = _unsupported
    # Hosted owner RPCs do not implement the local page-header protocol.
    last_observed_page_id = None
    bound_page_id = None
    status = _unsupported
    GetNearestDebrisLocations = _unsupported
    SetCommand = _unsupported
    set_command_checked = _unsupported
    set_sensor_fault = _unsupported
    clear_sensor_faults = _unsupported
    preview = _unsupported
    raycast = _unsupported
    tune_ship = _unsupported
    tune_debris_flow = _unsupported
    camera_status = _unsupported
    PanTiltCamera = _unsupported
    capture_image = _unsupported
    pause = _unsupported
    stop = _unsupported
    reset = _unsupported
    upright = _unsupported
    configure_experiment = _unsupported
    configure_flight = _unsupported
    place_lander = _unsupported
    move_launchpad = _unsupported
    step = _unsupported
    run_step = _unsupported


def create_room(
    connection_file: str | Path, *, url: str = DEFAULT_ORIGIN, timeout_s: float = 8.0
) -> dict[str, str]:
    """Create once, without actuation, after exclusively reserving the private file."""
    origin, timeout, path = _origin(url), _timeout(timeout_s), Path(connection_file)
    with _reserve_descriptor(path) as (target, owned):
        request = Request(
            f"{origin}/api/rooms",
            data=b"{}",
            headers={
                "Origin": origin,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        result = _post_json(_http_client(), request, timeout_s=timeout, expected_status=201)
        if set(result) != {"id", "key"}:
            raise RoomApiError(
                "Room creation returned an invalid credential response. No retry was made."
            )
        descriptor = {"version": 1, "url": origin, "room_id": result["id"], "token": result["key"]}
        _, room_id, token = _descriptor(descriptor)
        if not _owns_private_file(path, owned):
            raise RoomApiError("Reserved connection file changed. No flight command was sent.")
        json.dump(descriptor, target, allow_nan=False, separators=(",", ":"))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
        if not _owns_private_file(path, owned):
            raise RoomApiError("Reserved connection file changed. No flight command was sent.")
    return {"connection_file": str(path), "browser_url": f"{origin}/#room={room_id}&key={token}"}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RoomApiError("Invalid room arguments. Use --help.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _Parser(
        prog="wsorb room",
        description="Create a private hosted room; never start a flight.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="room_command", required=True)
    create = sub.add_parser(
        "create", help="create a room and save a new private descriptor", allow_abbrev=False
    )
    create.add_argument(
        "--connection-file",
        type=Path,
        required=True,
        help="new private file; never overwrite an existing path",
    )
    create.add_argument(
        "--url", default=DEFAULT_ORIGIN, help="HTTPS origin, default the production game"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Print a bearer-capability browser link only after explicit successful creation."""
    try:
        args = parse_args(argv)
        result = create_room(args.connection_file, url=args.url)
    except (Exception, KeyboardInterrupt) as error:
        message = (
            str(error)
            if isinstance(error, RoomApiError)
            else "Room creation failed; no request was retried."
        )
        print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False))
    print("Keep the browser URL private. Its fragment grants room control.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
