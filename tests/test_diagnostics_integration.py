"""Opt-in logs through the repository's real pytest HTTP/WASM fixtures."""

from __future__ import annotations

import io
import json
import secrets
import time
import uuid
from urllib.error import HTTPError

import pytest

from test_local_api import _http, _running_local_server
from test_room_integration import actual_room as actual_room
from test_room_integration import room_owner as room_owner
from test_room_integration import room_worker as room_worker
from wsorb import diagnostics as d
from wsorb.checkpoint import CheckpointError, CheckpointOptions, wait_for_owner
from wsorb.room_api import RoomSimulation


def _relay_events(output):
    return [
        value
        for line in "".join(output).splitlines()
        if line.startswith("{")
        and (value := json.loads(line)).get("schema") == "wsorb.diagnostics.v1"
    ]


def test_local_relay_default_off_and_unauthenticated_header_cannot_enable_logs(tmp_path):
    output = []
    with (
        d.diagnostics_context(False),
        _running_local_server(tmp_path, headless=True, diagnostic_output=output) as client,
    ):
        assert "diagnostics" not in client.status()
        snapshot = client.snapshot()
        assert "diagnostics" not in snapshot
        assert snapshot["renderer"]["ready"] is False
        with pytest.raises(HTTPError) as denied:
            _http(client, "/__local/status", headers={"X-WSORB-Diagnostics": "1"})
        assert denied.value.code == 401
        denied.value.close()
    assert _relay_events(output) == []


def test_local_relay_correlates_real_reads_and_stops_when_feature_is_disabled(tmp_path):
    output = []
    sink = io.StringIO()
    with _running_local_server(tmp_path, headless=True, diagnostic_output=output) as client:
        with d.diagnostics_context(False):
            before = client.snapshot()
        with d.diagnostics_context(sink=sink):
            status = client.status()
            sampled = client.snapshot()
        with d.diagnostics_context(False):
            assert "diagnostics" not in client.status()
            after = client.snapshot()
        assert status["diagnostics"]["enabled"] is True
        assert status["diagnostics"]["page_id"] is None
        assert sampled["diagnostics"]["diagnostics_helper_available"] is True
        assert sampled["diagnostics"]["camera_available"] is False
        assert sampled["observation"]["time_s"] == before["observation"]["time_s"] == 0
        assert after["observation"]["control_authority"]["status"] == "idle"
        assert after["renderer"]["started"] is after["renderer"]["ready"] is False
        assert after["observation"]["geometry"]["debris_count"] == 500
        secret = client._token
    events = _relay_events(output)
    assert [event["event"] for event in events] == ["call_received", "call_finished"]
    python_events = [json.loads(line) for line in sink.getvalue().splitlines()]
    requests = {
        event["request_id"]
        for event in python_events
        if event.get("method") == "snapshot" and event["event"] == "request_start"
    }
    assert {event["trace_id"] for event in events} == requests
    assert all(event["method"] == "snapshot" for event in events)
    assert secret not in "".join(output) + sink.getvalue()


def test_local_relay_does_not_log_raw_invalid_fields_or_headers(tmp_path):
    output = []
    canary = secrets.token_urlsafe(32)
    with _running_local_server(tmp_path, headless=True, diagnostic_output=output) as client:
        headers = {
            "Authorization": f"Bearer {client._token}",
            "Content-Type": "application/json",
            "X-WSORB-Diagnostics": "1",
            "X-WSORB-Request-ID": canary,
        }
        with pytest.raises(HTTPError) as denied:
            _http(
                client,
                "/__local/call",
                headers=headers,
                payload={"method": "snapshot", "private_payload": canary},
            )
        assert denied.value.code == 400
        denied.value.close()
        with d.diagnostics_context(False):
            assert client.snapshot()["observation"]["control_authority"]["status"] == "idle"
        token = client._token
    text = "".join(output)
    assert canary not in text and token not in text and "private_payload" not in text
    events = _relay_events(output)
    assert len(events) == 1 and events[0]["event"] == "call_rejected"
    assert str(uuid.UUID(events[0]["trace_id"])) == events[0]["trace_id"]


def test_server_environment_flag_can_enable_relay_logging_without_client_flag(tmp_path):
    output = []
    with (
        d.diagnostics_context(False),
        _running_local_server(
            tmp_path, headless=True, diagnostic_output=output, diagnostics=True
        ) as client,
    ):
        assert client.status()["diagnostics"]["enabled"] is True
        snapshot = client.snapshot()
        assert snapshot["runtime"]["running"] is False
    assert [event["event"] for event in _relay_events(output)] == ["call_received", "call_finished"]


def test_room_snapshot_diagnostics_are_opt_in_and_do_not_start_a_flight(room_owner):
    _, _, _, path, _ = room_owner
    client = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
    with d.diagnostics_context(False):
        before = client.snapshot()
    sink = io.StringIO()
    with d.diagnostics_context(sink=sink):
        sampled = client.snapshot()
    with d.diagnostics_context(False):
        after = client.snapshot()
    assert "diagnostics" not in before and "diagnostics" not in after
    assert isinstance(sampled.get("diagnostics"), dict)
    assert sampled["observation"]["time_s"] == after["observation"]["time_s"] == 0
    assert after["observation"]["control_authority"]["status"] == "idle"
    assert after["runtime"]["running"] is False
    assert after["renderer"]["started"] is after["renderer"]["ready"] is False
    assert after["cameras"]["configured"] is False
    assert d.public_snapshot(sampled)["camera_available"] is False
    assert client._token not in sink.getvalue()
    records = [json.loads(line) for line in sink.getvalue().splitlines()]
    request_ids = {record["request_id"] for record in records if "request_id" in record}
    assert len(request_ids) == 1
    assert all(record.get("method") == "snapshot" for record in records if "method" in record)


def test_owner_wait_reads_real_headless_readiness_without_starting_or_claiming(tmp_path):
    with _running_local_server(tmp_path, headless=True) as client:
        options = CheckpointOptions(connection_file="explicit.json", run=True, wait_for_owner_s=3)
        timeout = client.timeout_s
        wait_for_owner(client, options, deadline=time.monotonic() + 3)
        snapshot = client.snapshot()
        assert client.timeout_s == timeout
        assert snapshot["runtime"]["running"] is False
        assert snapshot["observation"]["time_s"] == 0
        assert snapshot["observation"]["control_authority"]["status"] == "idle"
        assert snapshot["renderer"]["ready"] is False


def test_owner_wait_timeout_does_not_dispatch_a_command_to_an_absent_browser(tmp_path):
    with _running_local_server(tmp_path) as client:
        options = CheckpointOptions(
            connection_file="explicit.json", run=True, wait_for_owner_s=0.05
        )
        with pytest.raises(CheckpointError) as expired:
            wait_for_owner(client, options, deadline=time.monotonic() + 1)
        assert expired.value.code == "owner_wait_timeout"
        assert client.status()["connected"] is client.status()["busy"] is False


def test_owner_wait_does_not_replace_an_actual_native_controller(tmp_path):
    with _running_local_server(tmp_path, headless=True) as client:
        initial = client.snapshot()
        grant = client.claim_control("existing_run", "existing_controller", initial, lease_sim_s=1)
        authority = grant["observation"]["control_authority"]
        try:
            options = CheckpointOptions(
                connection_file="explicit.json", run=True, wait_for_owner_s=3
            )
            with pytest.raises(CheckpointError) as occupied:
                wait_for_owner(client, options, deadline=time.monotonic() + 3)
            assert occupied.value.code == "owner_in_use"
            actual = client.snapshot()
            assert actual["observation"]["control_authority"] == authority
            assert actual["runtime"]["running"] is False
        finally:
            client.release_control("existing_run", authority["epoch"], client.snapshot())
