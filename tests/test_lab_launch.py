"""Private-file checks and real Marimo/local-server handoffs, without substitute services."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import selectors
import shutil
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import pytest

from wsorb.cli import _build_parser
from wsorb.lab import DEFAULT_TOKEN_FILE, NOTEBOOK_PATH, lab_command, lab_port, prepare_token_file
from wsorb.local_api import LocalSimulation

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "worker/scripts/notebook-launch.mjs"
NOTEBOOK_ROUTES = ("/notebook.html", "/__local/notebook")
ACTIVATED_NAVIGATION = {
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
}


@dataclass(repr=False)
class _NotebookServer:
    """Keep the private token-file path out of pytest's fixture argument display."""

    client: LocalSimulation
    port: int
    token_file: Path


def _lab_environment(**overrides: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"WSORB_LAB_TOKEN_FILE", "WSORB_LAB_PORT"}
    }
    return {**environment, **overrides}


def _write_token(path: Path, content: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secrets.token_urlsafe(32).encode("ascii") if content is None else content)
    path.chmod(0o600)
    return path


def test_token_is_created_privately_once_and_reused(tmp_path):
    path = prepare_token_file(root=tmp_path, path=DEFAULT_TOKEN_FILE)
    content, info = path.read_bytes(), path.stat()
    assert path == tmp_path.resolve() / DEFAULT_TOKEN_FILE
    assert re.fullmatch(rb"[A-Za-z0-9_-]{43}\n", content)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.getuid()
    assert info.st_nlink == 1
    assert prepare_token_file(root=tmp_path, path=DEFAULT_TOKEN_FILE) == path
    assert path.read_bytes() == content
    assert path.stat().st_ino == info.st_ino
    other = prepare_token_file(root=tmp_path, path="other.token")
    assert other.read_bytes() != content


def test_relative_environment_override_is_rooted_at_the_repository(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    script = """
import sys
from pathlib import Path
from wsorb.lab import prepare_token_file

print(prepare_token_file(root=Path(sys.argv[1])))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=elsewhere,
        env=_lab_environment(
            PYTHONPATH=str(ROOT / "src"),
            WSORB_LAB_TOKEN_FILE="private/editor.token",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    path = tmp_path.resolve() / "private/editor.token"
    assert result.stdout.strip() == str(path)
    assert path.is_file()
    assert not (elsewhere / "private").exists()


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666, 0o400, 0o700, 0o1600])
def test_existing_token_permissions_are_not_silently_repaired(tmp_path, mode):
    path = _write_token(tmp_path / DEFAULT_TOKEN_FILE)
    original = path.read_bytes()
    path.chmod(mode)
    with pytest.raises(ValueError, match="private regular file"):
        prepare_token_file(root=tmp_path, path=DEFAULT_TOKEN_FILE)
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "hardlink"])
def test_existing_token_must_be_a_single_regular_file(tmp_path, kind):
    path = tmp_path / DEFAULT_TOKEN_FILE
    path.parent.mkdir()
    if kind == "symlink":
        path.symlink_to(_write_token(tmp_path / "secret.token"))
    elif kind == "directory":
        path.mkdir(mode=0o600)
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)
    else:
        os.link(_write_token(tmp_path / "secret.token"), path)
    with pytest.raises(ValueError, match="private regular file"):
        prepare_token_file(root=tmp_path, path=DEFAULT_TOKEN_FILE)


@pytest.mark.parametrize(
    "content",
    [b"", b"a" * 42, b"a" * 4096, b"\x80" * 43, b"a" * 43 + b"\n\n"],
)
def test_token_reads_reject_oversize_and_non_ascii_content(tmp_path, content):
    path = _write_token(tmp_path / DEFAULT_TOKEN_FILE, content)
    with pytest.raises(ValueError, match="token"):
        prepare_token_file(root=tmp_path, path=DEFAULT_TOKEN_FILE)
    assert path.read_bytes() == content


@pytest.mark.parametrize("directory", ["worker/public", "worker/web", "dist"])
def test_token_cannot_be_created_in_public_assets(tmp_path, directory):
    public = tmp_path / directory
    with pytest.raises(ValueError, match="outside public assets"):
        prepare_token_file(root=tmp_path, path=public / "editor.token")
    assert not public.exists()


def test_token_directory_cannot_be_a_symlink_or_group_writable(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        prepare_token_file(root=tmp_path, path="linked/editor.token")
    directory.chmod(0o770)
    with pytest.raises(ValueError, match="group or world writable"):
        prepare_token_file(root=tmp_path, path="private/editor.token")
    assert not (directory / "editor.token").exists()


@pytest.mark.parametrize(
    "value",
    [0, -1, 65536, True, 1.5, "", "01", "1.0", " 2718", "２７１８", "http://localhost:2718"],
)
def test_lab_port_rejects_non_ports(value):
    with pytest.raises(ValueError, match="1 to 65535"):
        lab_port(value)


@pytest.mark.parametrize("value", [1, "2718", 65535])
def test_lab_port_accepts_actual_ports(value):
    assert lab_port(value) == int(value)


def test_command_uses_the_canonical_notebook_and_only_a_password_file(tmp_path):
    token_file = tmp_path / "marimo.token"
    assert lab_command(token_file, port=41234) == [
        "marimo",
        "--quiet",
        "edit",
        str(ROOT / "notebooks/flight_lab.py"),
        "--host",
        "127.0.0.1",
        "--port",
        "41234",
        "--headless",
        "--no-sandbox",
        "--skip-update-check",
        "--token",
        "--token-password-file",
        str(token_file),
    ]
    assert NOTEBOOK_PATH.is_absolute()
    assert not token_file.exists()


def test_parser_and_help_do_not_start_an_editor_or_create_a_token(tmp_path):
    token_file = tmp_path / "uncreated.token"
    parser = _build_parser()
    assert vars(parser.parse_args(["lab"])) == {"command": "lab", "port": None}
    assert vars(parser.parse_args(["lab", "--port", "41234"])) == {"command": "lab", "port": 41234}
    result = subprocess.run(
        [sys.executable, "-m", "wsorb", "lab", "--help"],
        cwd=tmp_path,
        env=_lab_environment(
            PYTHONPATH=str(ROOT / "src"),
            WSORB_LAB_TOKEN_FILE=str(token_file),
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "WSORB_LAB_PORT" in result.stdout
    assert result.stderr == ""
    with pytest.raises(SystemExit) as invalid_port:
        parser.parse_args(["lab", "--port", "0"])
    assert invalid_port.value.code == 2
    assert not token_file.exists()
    notebook = parser.parse_args(["notebook", "list"])
    assert notebook.notebook_command == "list"
    assert notebook.notebook_dir == Path("notebook")


def test_invalid_environment_port_fails_before_launch(tmp_path):
    token_file = tmp_path / "uncreated.token"
    result = subprocess.run(
        [sys.executable, "-m", "wsorb", "lab"],
        cwd=tmp_path,
        env=_lab_environment(
            PYTHONPATH=str(ROOT / "src"),
            WSORB_LAB_TOKEN_FILE=str(token_file),
            WSORB_LAB_PORT="0",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert "1 to 65535" in result.stderr
    assert not token_file.exists()


def _node_resolve(root: Path, **overrides):
    node = shutil.which("node")
    if node is None:
        pytest.skip("The notebook handoff needs the project's Node runtime")
    script = """
const { resolveNotebook } = await import(process.argv[1]);
try {
  const result = await resolveNotebook({ root: process.argv[2] });
  process.stdout.write(JSON.stringify({ result }));
} catch (error) {
  process.stdout.write(JSON.stringify({ error: error.message }));
  process.exitCode = 2;
}
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, HELPER.as_uri(), str(root)],
        env=_lab_environment(**overrides),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode in (0, 2), result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("port", ["0", "65536", "01", "2718/", "https://example.com"])
def test_node_resolver_rejects_invalid_port_without_disclosing_a_file_path(tmp_path, port):
    result = _node_resolve(tmp_path, WSORB_LAB_PORT=port)
    assert result == {"error": "WSORB_LAB_PORT must be an integer from 1 to 65535"}
    assert str(tmp_path) not in json.dumps(result)


def test_node_resolver_returns_null_for_missing_files_and_a_closed_real_port(tmp_path):
    assert _node_resolve(tmp_path) == {"result": None}
    prepare_token_file(root=tmp_path, path=DEFAULT_TOKEN_FILE)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        assert _node_resolve(tmp_path, WSORB_LAB_PORT=str(port)) == {"result": None}


@pytest.mark.parametrize("kind", ["mode", "symlink", "hardlink", "oversize", "non-ascii"])
def test_node_resolver_rejects_unsafe_files_without_disclosing_secrets(tmp_path, kind):
    path = prepare_token_file(root=tmp_path, path=DEFAULT_TOKEN_FILE)
    token = path.read_text().strip()
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "moved.token"
        path.rename(target)
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(path, tmp_path / "linked.token")
    else:
        path.write_bytes(b"a" * 4096 if kind == "oversize" else b"\x80" * 43)
    result = _node_resolve(tmp_path)
    assert "error" in result
    assert token not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)


def test_node_resolver_rejects_public_asset_paths_even_when_missing(tmp_path):
    result = _node_resolve(tmp_path, WSORB_LAB_TOKEN_FILE="worker/public/editor.token")
    assert result == {"error": "Marimo token file must be outside public assets"}


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@pytest.fixture(scope="module")
def real_marimo(tmp_path_factory):
    if importlib.util.find_spec("marimo") is None:
        pytest.skip("The real editor check needs the optional lab extra")
    executable_path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    if shutil.which("marimo", path=executable_path) is None:
        pytest.skip("The real editor check needs the installed Marimo CLI")
    directory = tmp_path_factory.mktemp("real-marimo").resolve()
    token_file = directory / "editor.token"
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    environment = _lab_environment(
        PATH=executable_path,
        PYTHONPATH=str(ROOT / "src"),
        WSORB_LAB_TOKEN_FILE=str(token_file),
        WSORB_LAB_PORT=str(port),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "wsorb", "lab", "--port", str(port)],
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("The real Marimo CLI exited before loopback readiness")
            try:
                with opener.open(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except HTTPError as error:
                with error:
                    if (
                        error.code == 303
                        and error.headers.get("Location") == "/auth/login?next=%2F"
                    ):
                        break
            except (OSError, URLError):
                pass
            time.sleep(0.05)
        else:
            pytest.fail("The real Marimo CLI did not become ready on its configured loopback port")
        yield port, token_file
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        if token_file.is_file():
            token = token_file.read_text().strip()
            assert token not in stdout + stderr, "Marimo printed the private token"


def test_real_marimo_accepts_the_private_file_token_and_strips_it_from_the_url(real_marimo):
    port, token_file = real_marimo
    token = token_file.read_text().strip()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    with pytest.raises(HTTPError) as denied:
        opener.open(f"http://127.0.0.1:{port}/api/version", timeout=3)
    assert denied.value.code in (401, 403)
    denied.value.close()
    with pytest.raises(HTTPError) as authenticated:
        opener.open(f"http://localhost:{port}/?access_token={token}", timeout=3)
    with authenticated.value as response:
        assert response.code == 303
        assert response.headers["Location"] == "/"
        cookie = response.headers["Set-Cookie"].split(";", 1)[0]
    request = Request(f"http://localhost:{port}/api/version", headers={"Cookie": cookie})
    with opener.open(request, timeout=3) as response:
        assert response.read(64).decode().strip() == "0.25.2"


@pytest.mark.parametrize("relative", [False, True])
def test_node_resolver_reaches_the_actual_editor_using_the_same_private_file(real_marimo, relative):
    port, token_file = real_marimo
    token = token_file.read_text().strip()
    result = _node_resolve(
        token_file.parent if relative else ROOT,
        WSORB_LAB_PORT=str(port),
        WSORB_LAB_TOKEN_FILE=token_file.name if relative else str(token_file),
    )
    assert result == {"result": {"url": f"http://localhost:{port}/?access_token={token}"}}


@pytest.fixture(scope="module")
def notebook_server(tmp_path_factory, real_marimo):
    __tracebackhide__ = True
    node = shutil.which("node")
    if node is None or not (ROOT / "worker/public/index.html").is_file():
        pytest.skip("The real local transport check needs Node and assets from just build")
    port, token_file = real_marimo
    connection = tmp_path_factory.mktemp("notebook-server").resolve() / "connection.json"
    process = subprocess.Popen(
        [node, "worker/scripts/local.mjs"],
        cwd=ROOT,
        env=_lab_environment(
            PORT="0",
            WSORB_LAB="1",
            WSORB_LOCAL_CONNECTION=str(connection),
            WSORB_LAB_TOKEN_FILE=str(token_file),
            WSORB_LAB_PORT=str(port),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "Local notebook server did not print a URL"
            line = process.stdout.readline()
        assert re.search(r"http://127\.0\.0\.1:\d+/", line), "Local notebook server startup failed"
        client = LocalSimulation.from_file(connection)
        assert stat.S_IMODE(connection.stat().st_mode) == 0o600
        server = _NotebookServer(client, port, token_file)
        yield server
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    assert not connection.exists(), "Shutdown should remove its own connection descriptor"
    _assert_no_notebook_secrets(line + stdout + stderr, server)


def _notebook_http(server, route, *, headers=None):
    __tracebackhide__ = True
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    try:
        return opener.open(Request(f"{server.client.url}{route}", headers=headers or {}), timeout=5)
    except HTTPError as error:
        return error


def _assert_no_notebook_secrets(content, server):
    __tracebackhide__ = True
    private = (
        server.token_file.read_text().strip(),
        server.client._token,
        str(server.token_file),
    )
    if "access_token=" in content or any(secret in content for secret in private):
        pytest.fail("Local notebook output disclosed private credentials or their file path")


def _assert_notebook_security_headers(response):
    expected = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
    for name, value in expected.items():
        if response.headers.get(name) != value:
            pytest.fail(f"Notebook response must set {name} to {value}")


def _assert_notebook_redirect(response, server):
    __tracebackhide__ = True
    assert response.status == 302
    _assert_notebook_security_headers(response)
    token = server.token_file.read_text().strip()
    expected = f"http://localhost:{server.port}/?access_token={token}"
    if response.headers.get("Location") != expected:
        pytest.fail("Notebook redirect did not exactly match the real editor's private URL")
    if response.read():
        pytest.fail("Notebook redirects must not include a response body")


def _assert_notebook_fallback(response, server):
    __tracebackhide__ = True
    body = response.read().decode()
    _assert_no_notebook_secrets(body + str(response.headers), server)
    assert response.status == 200
    _assert_notebook_security_headers(response)
    assert response.headers.get_content_type() == "text/html"
    assert "Location" not in response.headers
    assert "devenv up lab simulation" in body


@pytest.mark.parametrize("route", NOTEBOOK_ROUTES)
@pytest.mark.parametrize("authorization", ["same-origin", "bearer"])
def test_real_notebook_route_redirects_same_origin_or_bearer_requests(
    notebook_server, route, authorization
):
    client = notebook_server.client
    headers = (
        {**ACTIVATED_NAVIGATION, "Sec-Fetch-Site": "same-origin"}
        if authorization == "same-origin"
        else {"Authorization": f"Bearer {client._token}"}
    )
    with _notebook_http(notebook_server, route, headers=headers) as response:
        _assert_notebook_redirect(response, notebook_server)


@pytest.mark.parametrize("route", NOTEBOOK_ROUTES)
def test_real_notebook_route_accepts_noopener_noreferrer_activated_navigation(
    notebook_server, route
):
    # window.open with noopener,noreferrer has no Origin/Referer or bearer token.
    with _notebook_http(notebook_server, route, headers=ACTIVATED_NAVIGATION) as response:
        _assert_notebook_redirect(response, notebook_server)


@pytest.mark.parametrize("route", NOTEBOOK_ROUTES)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Sec-Fetch-Site": "none"}, {"Authorization": "Bearer invalid"}],
    ids=["raw-get", "site-only", "invalid-bearer"],
)
def test_real_notebook_route_falls_back_without_revealing_a_ready_editors_token(
    notebook_server, route, headers
):
    with _notebook_http(notebook_server, route, headers=headers) as response:
        _assert_notebook_fallback(response, notebook_server)


@pytest.mark.parametrize("route", NOTEBOOK_ROUTES)
@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Sec-Fetch-Site", None),
        ("Sec-Fetch-Mode", None),
        ("Sec-Fetch-Dest", None),
        ("Sec-Fetch-User", None),
        ("Sec-Fetch-Mode", "cors"),
        ("Sec-Fetch-Dest", "iframe"),
        ("Sec-Fetch-User", "?0"),
    ],
    ids=[
        "missing-site",
        "missing-mode",
        "missing-destination",
        "missing-user",
        "non-navigation",
        "subframe",
        "not-activated",
    ],
)
def test_real_notebook_route_requires_complete_activated_navigation_metadata(
    notebook_server, route, header, value
):
    headers = {**ACTIVATED_NAVIGATION}
    if value is None:
        del headers[header]
    else:
        headers[header] = value
    with _notebook_http(notebook_server, route, headers=headers) as response:
        _assert_notebook_fallback(response, notebook_server)


@pytest.mark.parametrize("route", NOTEBOOK_ROUTES)
@pytest.mark.parametrize(
    "extra",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://example.com"},
        {"Origin": "null"},
        {"Host": "untrusted.example"},
    ],
    ids=["cross-site", "same-site", "foreign-origin", "opaque-origin", "rebound-host"],
)
def test_real_notebook_route_rejects_foreign_requests_even_with_a_valid_bearer(
    notebook_server, route, extra
):
    client = notebook_server.client
    headers = {
        **ACTIVATED_NAVIGATION,
        "Authorization": f"Bearer {client._token}",
        **extra,
    }
    with _notebook_http(notebook_server, route, headers=headers) as response:
        _assert_no_notebook_secrets(
            response.read().decode() + str(response.headers), notebook_server
        )
        assert response.status == 403
        assert "Location" not in response.headers
