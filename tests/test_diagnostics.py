"""Pure logging contracts and genuine unavailable transports, without substitute engines."""

from __future__ import annotations

import ast
import contextvars
import inspect
import io
import json
import math
import os
import secrets
import socket
import stat
import uuid
from contextlib import contextmanager
from urllib.error import URLError
from urllib.request import Request

import pytest

from wsorb import diagnostics as d
from wsorb.local_api import LocalApiError, LocalBusyError, LocalSimulation
from wsorb.room_api import RoomApiError, RoomSimulation, _rpc_envelope


@contextmanager
def _environment(value):
    previous = os.environ.get("WSORB_DIAGNOSTICS")
    try:
        if value is None:
            os.environ.pop("WSORB_DIAGNOSTICS", None)
        else:
            os.environ["WSORB_DIAGNOSTICS"] = value
        yield
    finally:
        if previous is None:
            os.environ.pop("WSORB_DIAGNOSTICS", None)
        else:
            os.environ["WSORB_DIAGNOSTICS"] = previous


@contextmanager
def _unavailable_origin(scheme="http"):
    # A bound, non-listening port cannot be acquired by another server.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        yield f"{scheme}://127.0.0.1:{reserved.getsockname()[1]}"


def _lines(sink):
    return [json.loads(line) for line in sink.getvalue().splitlines()]


class _Unvisited(dict):
    """An off-path sentinel, not an HTTP, engine or graphics replacement."""

    def get(self, *args):
        raise AssertionError("Diagnostics visited an off-path payload")

    def __iter__(self):
        raise AssertionError("Diagnostics copied an off-path payload")


def test_default_off_returns_before_payload_inspection_or_serialization(capsys):
    with _environment(None):
        context = contextvars.Context()
        assert context.run(d.enabled) is False
        assert context.run(d.emit, "sample", snapshot=_Unvisited()) is False
        assert context.run(d.start_request, "local", "http", _Unvisited()) is None
        context.run(d.finish_request, None, response=_Unvisited())
    assert capsys.readouterr() == ("", "")


def test_feature_calls_and_nested_contexts_switch_off_on_off_without_output_leaks():
    sink = io.StringIO()
    with d.diagnostics_context(False):
        assert not d.enabled()
        assert not d.emit("sample", snapshot=_Unvisited())
        logger = d.enable(sink=sink)
        assert d.enabled()
        assert d.emit("status", status_available=False)
        with d.diagnostics_context(False):
            assert not d.emit("sample", snapshot=_Unvisited())
        assert d.enabled()
        d.disable()
        assert not d.enabled()
        assert not d.emit("sample", snapshot=_Unvisited())
        assert logger.events == 1
    assert len(_lines(sink)) == 1


@pytest.mark.parametrize(
    "flag,expected", [(None, False), ("0", False), ("true", False), ("1", True)]
)
def test_environment_enables_only_literal_one_and_explicit_disable_wins(flag, expected, capsys):
    with _environment(flag):
        context = contextvars.Context()
        assert context.run(d.enabled) is expected
        assert context.run(d.emit, "status", status_available=False) is expected
        context.run(d.disable)
        assert context.run(d.emit, "sample", snapshot=_Unvisited()) is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert bool(captured.err) is expected


def test_arbitrary_strings_and_unapproved_fields_are_rejected_not_redacted():
    canary = secrets.token_urlsafe(32)
    sink = io.StringIO()
    with d.diagnostics_context(sink=sink):
        assert not d.emit(canary)
        for field in (
            "error",
            "Authorization",
            "url",
            "token",
            "key",
            "argv",
            "request",
            "radar",
            "rgb",
        ):
            assert not d.emit("error", **{field: canary})
        for field in ("method", "op", "request_id", "code", "error_type", "transport", "stage"):
            assert not d.emit("error", **{field: canary})
        assert not d.emit("sample", snapshot={"camera": {"mode": canary}})
        assert not d.emit("sample", snapshot={"page_id": canary})
    assert sink.getvalue() == ""


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 2**2048, True, "1"])
def test_numbers_and_fixed_vectors_are_finite_bounded_and_never_coerced(value):
    sink = io.StringIO()
    with d.diagnostics_context(sink=sink):
        assert not d.emit("sample", elapsed_s=value)
        assert not d.emit("sample", snapshot={"lander": {"position_world_m": [0, 0, value]}})
    assert sink.getvalue() == ""


def test_limits_apply_before_inspecting_dropped_events():
    sink = io.StringIO()
    with d.diagnostics_context(sink=sink, max_events=2) as logger:
        assert d.emit("status", status_available=False)
        assert d.emit("status", status_available=False)
        assert not d.emit("sample", snapshot=_Unvisited())
        assert logger.events == 2 and logger.dropped == 1
    assert len(_lines(sink)) == 2
    sink = io.StringIO()
    with d.diagnostics_context(sink=sink, max_events_per_second=1) as logger:
        assert d.emit("status", status_available=False)
        assert not d.emit("sample", snapshot=_Unvisited())
        assert logger.dropped == 1
    assert len(_lines(sink)) == 1
    for options in (
        {"max_events": d.MAX_EVENTS + 1},
        {"max_events_per_second": 21},
        {"max_events": True},
        {"max_events": 0},
    ):
        with pytest.raises(ValueError):
            d.Diagnostics(**options)


def test_only_selected_public_metadata_is_projected_without_walking_sensor_payloads():
    # Projection input, not a fabricated engine invocation or motion measurement.
    canary = secrets.token_urlsafe(32)
    metadata = {
        "observation": {
            "time_s": 12.5,
            "navigation_sample_time_s": 12.4,
            "mission": {
                "target_distance_m": 500,
                "relative_speed_mps": 8,
                "status": "running",
                "score_label": canary,
            },
            "control_authority": {"status": "active", "epoch": 3, "term": 4, "run_id": canary},
            "applied_physics": {
                "source_time_s": 12.4,
                "position_world_m": [1, 2, 3],
                "hidden": canary,
            },
            "radar": _Unvisited(),
            "hidden_truth": _Unvisited(),
        },
        "runtime": {"running": True},
        "renderer": {"rendered_frames": 42, "error": canary, "adapter": _Unvisited()},
        "cameras": _Unvisited(),
        "rgb": _Unvisited(),
        "token": canary,
    }
    summary = d.public_snapshot(metadata)
    assert summary["lander"] == {"source_time_s": 12.4, "position_world_m": [1, 2, 3]}
    assert summary["owner"] == {"status": "active", "epoch": 3, "term": 4}
    assert summary["renderer"] == {"rendered_frames": 42}
    assert summary["range_m"] == 500
    assert summary["page_id_available"] is False
    assert summary["camera_available"] is False
    assert summary["presentation_available"] is False
    assert canary not in json.dumps(summary, allow_nan=False)


def test_optional_visual_metadata_never_claims_an_unavailable_getter_exists():
    page_id = str(uuid.uuid4())
    value = {
        "diagnostics": {
            "page_id": page_id,
            "camera": {"available": False},
            "presentation": {"available": False},
        }
    }
    summary = d.public_snapshot(value)
    assert summary["page_id"] == page_id
    assert summary["page_id_available"] is True
    assert summary["camera_available"] is summary["presentation_available"] is False


def test_native_presentation_shape_preserves_clocks_transforms_and_camera_without_error_text():
    # Projection-schema validation only, not evidence of a rendered or native measurement.
    canary = secrets.token_urlsafe(32)
    metadata = {
        "diagnostics": {
            "camera_status_available": True,
            "presentation_diagnostics_available": True,
            "native_collection_available": True,
            "camera": {
                "mode": "follow",
                "focus": "Shuttle",
                "position_m": [1, 2, 3],
                "rotation": [0, 0, 0, 1],
                "active": True,
                "error": canary,
            },
            "presentation": {
                "schema": "wsorb.presentation_diagnostics.v1",
                "enabled": True,
                "available": True,
                "state": "observed",
                "measurement": "bevy_main_world_transforms_not_gpu_pixels",
                "sample": {
                    "collection": {
                        "stage": "post_update_after_transform_propagation",
                        "bevy_frame": 42,
                        "real_elapsed_s": 3.0,
                    },
                    "scene_snapshot_clock": "not_exposed_by_scene_api",
                    "scene_snapshot_source_time_s": None,
                    "published_snapshot": {
                        "source_time_s": 12.5,
                        "lander": {"position_world_m": [1, 2, 3]},
                    },
                    "native_lander": {"state": "unavailable", "error": canary},
                    "lander_visual": {
                        "state": "observed",
                        "transforms": {
                            "has_parent": False,
                            "global_transform": {
                                "translation_world_m": [1, 2, 3],
                                "linear_world_from_local_columns": [
                                    [1, 0, 0],
                                    [0, 1, 0],
                                    [0, 0, 1],
                                ],
                            },
                        },
                    },
                    "position_differences": {"lander_global_minus_native_m": None},
                },
            },
        }
    }
    summary = d.public_snapshot(metadata)
    sample = summary["presentation"]["sample"]
    assert sample["published_snapshot"]["source_time_s"] == 12.5
    assert sample["native_lander"] == {"state": "unavailable"}
    assert sample["collection"]["bevy_frame"] == 42
    assert "scene_snapshot_source_time_s" not in sample
    assert summary["camera"]["position_m"] == [1, 2, 3]
    # A supported getter is not proof that a main renderer/camera was started.
    assert summary["camera_available"] is False
    assert summary["presentation_available"] is True
    sink = io.StringIO()
    with d.diagnostics_context(sink=sink):
        assert d.emit("sample", snapshot=summary)
    assert canary not in sink.getvalue()
    assert len(sink.getvalue().encode()) < d.MAX_LINE_BYTES


def test_presentation_projection_retains_the_actual_scene_cache_clock_and_own_poses():
    # Projection input only. The Rust suite verifies actual scene-clock sampling.
    value = {
        "diagnostics": {
            "presentation": {
                "schema": "wsorb.presentation_diagnostics.v1",
                "enabled": True,
                "available": True,
                "state": "observed",
                "sample": {
                    "scene_snapshot_clock": "scene_cache_snapshot",
                    "scene_snapshot_source_time_s": 12.5,
                    "scene_snapshot": {
                        "source_time_s": 12.5,
                        "source_seed": 100,
                        "tuning_revision": 0,
                        "lander": {"position_world_m": [1, 2, 3]},
                        "landing_pad": {"position_world_m": [0, 0, 0]},
                        "unrelated_payload": "not-loggable",
                    },
                },
            }
        }
    }
    sample = d.public_snapshot(value)["presentation"]["sample"]
    assert sample["scene_snapshot_clock"] == "scene_cache_snapshot"
    assert sample["scene_snapshot_source_time_s"] == 12.5
    assert sample["scene_snapshot"]["source_time_s"] == 12.5
    assert sample["scene_snapshot"]["lander"]["position_world_m"] == [1, 2, 3]
    assert "unrelated_payload" not in sample["scene_snapshot"]


def test_trace_metadata_preserves_original_source_lease_and_fence_without_command_vectors():
    sink = io.StringIO()
    source = {
        "method": "command",
        "command": {
            "op": "set_leased_command",
            "source_time_s": 12.5,
            "lease_sim_s": 0.75,
            "controller_fence": {"epoch": 3, "term": 4, "run_id": secrets.token_urlsafe(32)},
            "accel": _Unvisited(),
            "angular_accel": _Unvisited(),
        },
    }
    with d.diagnostics_context(sink=sink):
        trace = d.start_request("local", "http", source)
        d.finish_request(trace, response={"ok": True, "value": {"ok": True}})
    start, end = _lines(sink)
    assert start["source_time_s"] == end["source_time_s"] == 12.5
    assert start["lease_sim_s"] == end["lease_sim_s"] == 0.75
    assert start["fence_epoch"] == end["fence_epoch"] == 3
    assert start["fence_term"] == end["fence_term"] == 4
    assert end["result_kind"] == "ack" and end["outcome"] == "acknowledged"
    assert "snapshot" not in end
    assert source["command"]["source_time_s"] == 12.5


def test_room_http_and_validated_rpc_are_distinct_and_share_existing_envelope_uuid():
    sink = io.StringIO()
    envelope = _rpc_envelope({"method": "snapshot"}, 1)
    data = json.dumps(envelope, allow_nan=False).encode()
    with d.diagnostics_context(sink=sink):
        rpc = d.start_request("room", "rpc", {"method": "snapshot"}, request_id=envelope["id"])
        http = d.start_request("room", "http", parent=rpc)
        # Header/request construction only. Neither document is an engine receipt.
        request = Request("https://example.invalid", data=data, headers=d.request_headers(http))
        assert request.data == data
        assert request.get_header("X-wsorb-diagnostics") == "1"
        assert request.get_header("X-wsorb-request-id") == envelope["id"]
        d.finish_request(http, http_status=200)
    assert set(envelope) == {"id", "command"}
    assert set(envelope["command"]) == {"version", "deadline_ms", "method"}
    events = _lines(sink)
    assert {event["request_id"] for event in events} == {envelope["id"]}
    assert events[-1]["result_kind"] == "envelope"
    assert events[-1]["outcome"] == "received"
    assert "snapshot" not in events[-1]
    assert d.request_headers(None) == {}


def test_known_busy_is_a_skip_and_known_rejection_text_is_never_loggable():
    assert d._local_skip({"busy": True, "connected": True}) == "local_busy"
    assert d._local_skip({"connected": False}) == "owner_unavailable"
    assert d._local_skip({"backend": "native_headless", "physics_ready": True}) is None
    assert (
        d._local_skip({"backend": "native_headless", "physics_ready": False})
        == "physics_unavailable"
    )
    sink = io.StringIO()
    canary = secrets.token_urlsafe(32)
    with d.diagnostics_context(sink=sink):
        trace = d.start_request("local", "http", method="snapshot")
        d.finish_request(trace, error=LocalBusyError(canary), http_status=409)
    end = _lines(sink)[-1]
    assert end["outcome"] == "rejected_unapplied" and end["code"] == "local_busy"
    assert end["error_type"] == "LocalBusyError"
    assert canary not in sink.getvalue()


def test_actual_unavailable_local_call_remains_unknown_once_with_logging_on_off_and_broken_sink():
    token = secrets.token_urlsafe(32)
    with _unavailable_origin() as origin:
        client = LocalSimulation(origin, token, timeout_s=0.1)
        sink = io.StringIO()
        with d.diagnostics_context(sink=sink), pytest.raises(LocalApiError) as rejected:
            client.snapshot()
        assert isinstance(rejected.value.__cause__, URLError)
        events = _lines(sink)
        assert [event["event"] for event in events] == ["request_start", "request_end"]
        assert events[-1]["outcome"] == "unknown"
        assert events[-1]["result_kind"] == "unavailable"
        assert "snapshot" not in events[-1]
        assert token not in sink.getvalue() and origin not in sink.getvalue()
        with d.diagnostics_context(False), pytest.raises(LocalApiError) as off:
            client.snapshot()
        assert type(off.value) is type(rejected.value)
        broken = io.StringIO()
        broken.close()
        with d.diagnostics_context(sink=broken) as logger, pytest.raises(LocalApiError) as failed:
            client.snapshot()
        assert isinstance(failed.value.__cause__, URLError)
        assert logger.sink_failures == 2


def test_actual_unavailable_room_call_uses_one_uuid_without_saved_state_or_retries():
    token = secrets.token_urlsafe(32)
    with _unavailable_origin() as origin:
        client = RoomSimulation(
            origin, str(uuid.uuid4()), token, allow_loopback=True, timeout_s=0.1
        )
        sink = io.StringIO()
        with d.diagnostics_context(sink=sink), pytest.raises(RoomApiError):
            client.snapshot()
        events = _lines(sink)
        assert [(event["event"], event["stage"]) for event in events] == [
            ("request_start", "rpc"),
            ("request_start", "http"),
            ("request_end", "http"),
            ("request_end", "rpc"),
        ]
        assert len({event["request_id"] for event in events}) == 1
        assert all(event["outcome"] == "unknown" for event in events[-2:])
        assert token not in sink.getvalue() and origin not in sink.getvalue()
        broken = io.StringIO()
        broken.close()
        with d.diagnostics_context(sink=broken) as logger, pytest.raises(RoomApiError):
            client.snapshot()
        assert logger.sink_failures == 4


@pytest.mark.parametrize(
    "extra", [[], ["--run"], ["--isolated"], ["--allow-loopback"], ["--connection-file", "other"]]
)
def test_collector_requires_exactly_one_explicit_descriptor_and_has_no_actuation_flags(extra):
    if not extra:
        with pytest.raises(d.DiagnosticsError):
            d.parse_args([])
    else:
        with pytest.raises(d.DiagnosticsError):
            d.parse_args(["--room-file", "private.json", *extra])
    options = d.parse_args(["--connection-file", "private.json"])
    assert options.duration_s == 5 and options.cadence_hz == 1
    assert options.output is None and not hasattr(options, "run")


@pytest.mark.parametrize(
    "name,value",
    [
        ("duration-s", "30.01"),
        ("duration-s", "0"),
        ("cadence-hz", "2.01"),
        ("cadence-hz", "0"),
        ("duration-s", "nan"),
        ("cadence-hz", "inf"),
    ],
)
def test_collector_bounds_duration_and_cadence_before_io(name, value):
    with pytest.raises(d.DiagnosticsError):
        d.parse_args(["--room-file", "private.json", f"--{name}", value])
    with pytest.raises(d.DiagnosticsError):
        d.DiagnosticsOptions(connection_file="private.json", duration_s=True)


def test_collector_only_calls_status_and_fresh_snapshot_on_its_clients():
    tree = ast.parse(inspect.getsource(d._collect))
    methods = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
    }
    assert methods == {"status", "snapshot"}


def test_collector_on_real_unavailable_transport_logs_one_error_and_final_counts(tmp_path, capsys):
    token = secrets.token_urlsafe(32)
    descriptor = tmp_path / "local.json"
    with _unavailable_origin() as origin:
        descriptor.write_text(json.dumps({"version": 1, "url": origin, "token": token}))
        descriptor.chmod(0o600)
        assert d.main(["--connection-file", str(descriptor), "--duration-s", "0.2"]) == 1
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert captured.err == ""
    assert events[-1]["event"] == "summary"
    assert events[-1]["sample_count"] == events[-1]["skip_count"] == 0
    assert events[-1]["error_count"] == 1
    assert events[-1]["actuation_permitted"] is False
    assert [event["event"] for event in events].count("request_start") == 1
    assert token not in captured.out and origin not in captured.out
    assert list(tmp_path.iterdir()) == [descriptor]


def test_collector_room_descriptor_does_not_enable_http_test_transport_or_fallback(
    tmp_path, capsys
):
    path = tmp_path / "room.json"
    token = secrets.token_urlsafe(32)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "url": "http://127.0.0.1:1",
                "token": token,
                "room_id": str(uuid.uuid4()),
            }
        )
    )
    path.chmod(0o600)
    assert d.main(["--room-file", str(path)]) == 1
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert not any(
        json.loads(line)["event"] == "request_start" for line in captured.out.splitlines()
    )


def test_selected_output_is_exclusive_private_and_never_follows_symlinks(tmp_path):
    path = tmp_path / "diagnostics.jsonl"
    with d._output_file(path) as sink:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        sink.write("owner data\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    broken = tmp_path / "broken.jsonl"
    broken.symlink_to(tmp_path / "absent.jsonl")
    for invalid in (path, link, broken, tmp_path):
        with pytest.raises(d.DiagnosticsError), d._output_file(invalid):
            pytest.fail("Existing paths must not be opened")
    assert path.read_text() == "owner data\n"
    assert link.is_symlink() and broken.is_symlink()


def test_only_explicit_output_is_written_and_contains_no_descriptor_or_error_text(tmp_path, capsys):
    output = tmp_path / "selected.jsonl"
    canary = secrets.token_urlsafe(32)
    descriptor = tmp_path / canary
    assert d.main(["--room-file", str(descriptor), "--output", str(output)]) == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert events[-1]["error_count"] == 1
    assert canary not in output.read_text()
    assert capsys.readouterr() == ("", "")
    assert list(tmp_path.iterdir()) == [output]


def test_parser_never_echoes_arbitrary_argv_or_creates_output(capsys, tmp_path):
    canary = secrets.token_urlsafe(32)
    output = tmp_path / "unused.jsonl"
    assert d.main(["--room-file", "room.json", "--output", str(output), "--unknown", canary]) == 1
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert json.loads(captured.out.splitlines()[0])["code"] == "invalid_arguments"
    assert not output.exists()


@pytest.mark.skipif(
    os.environ.get("WSORB_DIAGNOSTICS_INTEGRATION") != "1",
    reason="Explicit opt-in only, using the owned Node/WASM fixture without flight commands.",
)
def test_real_owned_native_reads_survive_a_broken_sink_and_never_actuate(tmp_path):
    from test_local_api import _running_local_server

    with _running_local_server(tmp_path, headless=True) as client:
        before = client.snapshot()
        broken = io.StringIO()
        broken.close()
        with d.diagnostics_context(sink=broken) as logger:
            status = client.status()
            after = client.snapshot()
        assert logger.sink_failures == 4
        assert status["physics_ready"] is True
        assert after["runtime"]["running"] is False
        assert after["observation"]["time_s"] == before["observation"]["time_s"] == 0
        assert (
            after["observation"]["control_authority"] == before["observation"]["control_authority"]
        )
