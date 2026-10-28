"""Pure HTTP-header/request policy checks. No engine, network or graphics replacement."""

from __future__ import annotations

import io
import json
import secrets
import uuid
from email.message import Message
from urllib.request import Request

import pytest

from wsorb import diagnostics
from wsorb.local_api import (
    LocalApiError,
    LocalPageMismatchError,
    LocalSimulation,
    _PageBinding,
    _response_page_id,
)


def _headers(*pages: str) -> Message:
    headers = Message()
    for page in pages:
        headers["X-WSORB-Page"] = page
    return headers


def test_receipt_parser_accepts_one_uuid_and_not_a_target_header_or_missing_receipt():
    page = str(uuid.uuid4())
    assert _response_page_id(_headers(page)) == page
    assert _response_page_id(_headers(page.upper())) == page
    headers = Message()
    headers["X-WSORB-Target-Page"] = page
    assert _response_page_id(headers) is None
    assert _response_page_id(_headers()) is None


@pytest.mark.parametrize(
    "receipt",
    [
        "",
        "not-a-page",
        "00000000-0000-0000-0000-000000000000",
        "12345678-1234-9abc-8def-123456789abc",
        "12345678-1234-4abc-0def-123456789abc",
        "urn:uuid:12345678-1234-4abc-8def-123456789abc",
        "{12345678-1234-4abc-8def-123456789abc}",
        " 12345678-1234-4abc-8def-123456789abc",
        "12345678-1234-4abc-8def-123456789abc\n",
    ],
)
def test_invalid_receipts_are_fatal_without_echoing_header_content(receipt):
    with pytest.raises(LocalApiError, match="invalid page receipt") as rejected:
        _response_page_id(_headers(receipt))
    assert type(rejected.value) is LocalApiError
    assert getattr(rejected.value, "code", None) is None
    if receipt:
        assert receipt not in str(rejected.value)


def test_duplicate_or_joined_receipts_are_not_a_verified_page():
    page = str(uuid.uuid4())
    for headers in (_headers(page, page), _headers(f"{page}, {page}")):
        with pytest.raises(LocalApiError, match="invalid page receipt"):
            _response_page_id(headers)


def test_only_a_successful_snapshot_receipt_can_be_pinned_by_request_policy():
    # Header-policy input, not a fabricated snapshot or authenticated HTTP response.
    page = str(uuid.uuid4())
    binding = _PageBinding()
    assert binding.last_observed_page_id is binding.bound_page_id is None
    binding.observe(_headers(page))  # Other methods/errors do not learn an identity.
    assert binding.bind() is None
    assert binding.request_headers() == {}
    with pytest.raises(LocalApiError, match="No verified snapshot"):
        binding.bind(require=True)
    binding.observe(_headers(page), successful_snapshot=True)
    assert binding.last_observed_page_id == page
    assert binding.bound_page_id is None
    assert binding.bind(require=True) == page
    request = Request("http://127.0.0.1:1/__local/call", headers=binding.request_headers())
    assert request.get_header("X-wsorb-target-page") == page
    assert request.get_header("X-wsorb-page") is None
    assert binding.bind() == binding.bind(require=True) == page


def test_missing_legacy_or_native_receipts_never_pretend_to_pin_a_page():
    binding = _PageBinding()
    binding.observe(_headers(), successful_snapshot=True)
    assert binding.bind(require=False) is None
    assert binding.last_observed_page_id is binding.bound_page_id is None
    assert binding.request_headers() == {}
    with pytest.raises(LocalApiError, match="No verified snapshot"):
        binding.bind(require=True)
    binding.observe(_headers(str(uuid.uuid4())), successful_snapshot=True)
    binding.observe(_headers(), successful_snapshot=True)
    assert binding.last_observed_page_id is None and binding.bind() is None


@pytest.mark.parametrize("require", [None, 0, 1, "yes", []])
def test_binding_requires_a_boolean_feature_choice(require):
    binding = _PageBinding()
    with pytest.raises(ValueError, match="boolean"):
        binding.bind(require=require)
    assert binding.bound_page_id is None


def test_public_binding_does_no_io_accepts_no_guessed_uuid_and_exposes_read_only_ids():
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    assert client.bind_to_observed_page(require=False) is None
    assert client.last_observed_page_id is client.bound_page_id is None
    with pytest.raises(LocalApiError, match="No verified snapshot"):
        client.bind_to_observed_page(require=True)
    with pytest.raises(ValueError, match="boolean"):
        client.bind_to_observed_page(require=1)
    with pytest.raises(TypeError):
        client.bind_to_observed_page(str(uuid.uuid4()))
    with pytest.raises(TypeError):
        client.bind_to_observed_page(page_id=str(uuid.uuid4()))
    for name in ("last_observed_page_id", "bound_page_id"):
        with pytest.raises(AttributeError):
            setattr(client, name, str(uuid.uuid4()))


@pytest.mark.parametrize("successful_snapshot", [False, True])
def test_pinned_policy_rejects_missing_changed_and_invalid_postdispatch_receipts_as_unknown(
    successful_snapshot,
):
    page = str(uuid.uuid4())
    other = str(uuid.uuid4())
    binding = _PageBinding()
    binding.observe(_headers(page), successful_snapshot=True)
    binding.bind()
    binding.observe(_headers(page), successful_snapshot=successful_snapshot)
    for headers in (_headers(), _headers(other), _headers("private-invalid-header")):
        with pytest.raises(LocalApiError, match="outcome may be unknown") as rejected:
            binding.observe(headers, successful_snapshot=successful_snapshot)
        assert type(rejected.value) is LocalApiError
        assert getattr(rejected.value, "code", None) is None
        assert page not in str(rejected.value) and other not in str(rejected.value)
        assert "private-invalid-header" not in str(rejected.value)
        assert binding.last_observed_page_id == binding.bound_page_id == page
        assert binding.bind() == page  # No automatic or explicit rebinding path.
        assert binding.request_headers() == {"X-WSORB-Target-Page": page}


def test_only_actual_http409_page_mismatch_is_a_typed_undelivered_transport_rejection():
    token = secrets.token_urlsafe(32)
    origin = "http://127.0.0.1:1"
    client = LocalSimulation(origin, token)
    private_text = f"{origin} Bearer {token} X-WSORB-Page: private-header"
    failure = {"code": "page_mismatch", "error": private_text}
    mismatch = client._rejection(failure, "HTTP 409", http_status=409)
    assert type(mismatch) is LocalPageMismatchError
    assert mismatch.code == "page_mismatch" and "not dispatched" in str(mismatch)
    assert not any(value in str(mismatch) for value in (origin, token, "X-WSORB", "private-header"))
    for status in (None, 200, 400, 401, 403, 503):
        rejected = client._rejection(failure, "Rejection", http_status=status)
        assert type(rejected) is LocalApiError
        assert getattr(rejected, "code", None) is None
        assert "outcome may be unknown" in str(rejected)
        assert private_text not in str(rejected)
    rejected = client._rejection({"error": private_text}, "HTTP 409", http_status=409)
    assert type(rejected) is LocalApiError


def test_diagnostics_distinguish_verified_predispatch_mismatch_from_unknown_receipt_loss():
    # Error/header policy logging only. No document is passed off as engine evidence.
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    mismatch = client._rejection({"code": "page_mismatch"}, "HTTP 409", http_status=409)
    page = str(uuid.uuid4())
    binding = _PageBinding()
    binding.observe(_headers(page), successful_snapshot=True)
    binding.bind()
    with pytest.raises(LocalApiError) as missing:
        binding.observe(_headers())
    sink = io.StringIO()
    with diagnostics.diagnostics_context(sink=sink):
        for error, status in ((mismatch, 409), (missing.value, 200)):
            trace = diagnostics.start_request("local", "http", {"method": "command"})
            diagnostics.finish_request(trace, error=error, http_status=status)
        trace = diagnostics.start_request("local", "http", {"method": "command"})
        diagnostics.finish_request(
            trace, response={"ok": False, "code": "page_mismatch"}, http_status=200
        )
    ends = [json.loads(line) for line in sink.getvalue().splitlines()][1::2]
    assert ends[0]["error_type"] == "LocalPageMismatchError"
    assert ends[0]["code"] == "page_mismatch" and ends[0]["outcome"] == "rejected_unapplied"
    for end in ends[1:]:
        assert end["code"] == "unknown_outcome" and end["outcome"] == "unknown"
    assert page not in sink.getvalue() and client.url not in sink.getvalue()
