from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wsorb.sharing import SharingHandler, _index, _safe_file


@pytest.mark.skipif(
    sys.platform == "win32", reason="Bash wrapper runs on Linux/macOS, not native Windows"
)
def test_wrapper_uses_tailnet_serve_mount_and_scoped_cleanup(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "tailscale.log"
    port_socket = socket.socket()
    port_socket.bind(("127.0.0.1", 0))
    port = port_socket.getsockname()[1]
    port_socket.close()
    serve_pid_file = tmp_path / "serve.pid"
    fake_ts = bin_dir / "tailscale"
    fake_ts.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$TS_LOG"\n'
        'case "$*" in\n'
        "  'status --json') printf '%s\\n' \"$TS_STATUS\" ;;\n"
        "  'serve status --json') echo \"$SERVE_CONFIG\" ;;\n"
        "  'funnel status --json') echo \"$FUNNEL_CONFIG\" ;;\n"
        "  'serve --https=443 --set-path=/wsorb http://127.0.0.1:'*) "
        'echo "$$" > "$SERVE_PID_FILE"; exec sleep 30 ;;\n'
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_ts.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        TS_LOG=str(log),
        SERVE_PID_FILE=str(serve_pid_file),
        TS_STATUS='{"BackendState":"Running","Self":{"DNSName":"host.example.ts.net."}}',
        SERVE_CONFIG=(
            '{"TCP":{"443":{"HTTPS":true}},"Web":{"host:443":'
            '{"Handlers":{"/other":{"Proxy":"http://127.0.0.1:1234"}}}},"AllowFunnel":{}}'
        ),
        FUNNEL_CONFIG="{}",
        WSORB_PORT=str(port),
    )
    proc = subprocess.Popen(
        ["bash", "scripts/serve-tailnet"],
        cwd=Path(__file__).parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if log.exists() and "serve --https=443" in log.read_text(encoding="utf-8"):
                break
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                raise AssertionError(stderr or stdout)
            time.sleep(0.05)
        else:
            raise AssertionError("wrapper did not configure Serve")
        time.sleep(0.1)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    commands = log.read_text(encoding="utf-8").splitlines()
    assert "Tailnet-only notebook URL: https://host.example.ts.net/wsorb/" in stdout
    assert "status --json" in commands
    assert "serve status --json" in commands
    assert any(
        "--set-path=/wsorb" in command and f"http://127.0.0.1:{port}" in command
        for command in commands
    )
    assert not any("--bg" in command or command.endswith(" off") for command in commands)
    assert not any(command == "serve reset" or command == "funnel reset" for command in commands)
    assert not any(
        "funnel " in command and not command.endswith("status --json") for command in commands
    )
    serve_pid = int(serve_pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(serve_pid, 0)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2)
    assert commands.count("funnel status --json") == 1
    assert not any(command == "funnel" or "reset" in command for command in commands)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Bash wrapper runs on Linux/macOS, not native Windows"
)
@pytest.mark.parametrize(
    ("serve_config", "funnel_config", "state", "serve_result", "expected"),
    [
        ('{"mount":"/wsorb"}', "{}", "Running", "0", "already mentions"),
        ("{}", "{}", "NeedsLogin", "0", "not running"),
        ("{}", '{"AllowFunnel":{"host:443":true}}', "Running", "0", "Funnel is inactive"),
        ("{}", "not JSON", "Running", "0", "Funnel is inactive"),
        ("{}", "{}", "Running", "1", "Notebook sharing stopped"),
    ],
)
def test_wrapper_fails_closed(
    tmp_path: Path,
    serve_config: str,
    funnel_config: str,
    state: str,
    serve_result: str,
    expected: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ts = bin_dir / "tailscale"
    fake_ts.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  'status --json') printf '%s\\n' \"$TS_STATUS\" ;;\n"
        "  'serve status --json') echo \"$SERVE_CONFIG\" ;;\n"
        "  'funnel status --json') echo \"$FUNNEL_CONFIG\" ;;\n"
        "  'serve --https=443 --set-path=/wsorb http://127.0.0.1:'*) "
        'exit "$SERVE_RESULT" ;;\n'
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_ts.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SERVE_CONFIG=serve_config,
        FUNNEL_CONFIG=funnel_config,
        TS_STATUS=f'{{"BackendState":"{state}","Self":{{"DNSName":"host.example.ts.net."}}}}',
        SERVE_RESULT=serve_result,
    )
    result = subprocess.run(
        ["bash", "scripts/serve-tailnet"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert expected in result.stderr


@pytest.mark.skipif(
    sys.platform == "win32", reason="Bash wrapper runs on Linux/macOS, not native Windows"
)
def test_wrapper_fails_when_backend_port_is_occupied(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ts = bin_dir / "tailscale"
    log = tmp_path / "tailscale.log"
    fake_ts.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$TS_LOG"\n'
        'case "$*" in\n'
        "  'status --json') printf '%s\\n' \"$TS_STATUS\" ;;\n"
        "  'serve status --json'|'funnel status --json') echo '{}' ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_ts.chmod(0o755)
    try:
        result = subprocess.run(
            ["bash", "scripts/serve-tailnet"],
            cwd=Path(__file__).parents[1],
            env=dict(
                os.environ,
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                TS_LOG=str(log),
                TS_STATUS='{"BackendState":"Running","Self":{"DNSName":"host.example.ts.net."}}',
                WSORB_PORT=str(port),
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        listener.close()
    assert "Address already in use" in result.stderr
    assert "exited before becoming ready" in result.stderr
    assert not any(line.startswith("serve --https=") for line in log.read_text().splitlines())


def test_allowlisted_files_and_generated_index(tmp_path: Path) -> None:
    (tmp_path / "notebook/entries").mkdir(parents=True)
    (tmp_path / "notebook/NOTEBOOK.md").write_text("Notebook", encoding="utf-8")
    (tmp_path / "notebook/entries/a.md").write_text("Entry", encoding="utf-8")
    (tmp_path / "private.md").write_text("private", encoding="utf-8")
    run = "20261022T022803Z-35a51684"
    (tmp_path / f"runs/{run}").mkdir(parents=True)
    (tmp_path / f"runs/{run}/summary.md").write_text("Report", encoding="utf-8")
    (tmp_path / f"runs/{run}/campaign.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"runs/{run}/results.csv").write_text("a\n", encoding="utf-8")
    (tmp_path / "runs/scratch").mkdir()
    (tmp_path / "runs/scratch/private.md").write_text("secret", encoding="utf-8")
    (tmp_path / "runs/.private").mkdir()
    (tmp_path / "runs/.private/secret").write_text("secret", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    SharingHandler.root = tmp_path
    server = ThreadingHTTPServer(("127.0.0.1", 0), SharingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/healthz") as response:
            assert response.read() == b"ok\n"
        with urlopen(base + "/") as response:
            index = response.read().decode()
        assert 'href="notebook/entries/a.md"' in index
        assert 'href="notebook/NOTEBOOK.md"' in index
        assert f'href="runs/{run}/summary.md"' in index
        assert "/runs/scratch" not in index
        with urlopen(base + "/notebook/entries/a.md") as response:
            assert response.read() == b"Entry"
        with urlopen(base + f"/runs/{run}/summary.md") as response:
            assert response.read() == b"Report"
        # Tailscale Serve --set-path=/wsorb strips the mount prefix before proxying.
        # Relative links in the browser keep that prefix in the public URL.
        public_url = "https://host.example.ts.net/wsorb/"
        assert public_url + "notebook/entries/a.md" == (
            "https://host.example.ts.net/wsorb/notebook/entries/a.md"
        )
        for path in (
            "/.env",
            "/.git/config",
            f"/runs/{run}/private.txt",
            "/runs/scratch/private.md",
            "/runs/.private/secret",
            "/runs/scratch/summary.md",
            f"/runs/{run}/../.env",
            "/notebook/../.env",
            "/notebook/entries/folder%5C..%5C..%5Cprivate.md",
        ):
            try:
                with urlopen(base + path):
                    pass
            except HTTPError as error:
                assert error.code == 404
                error.close()
            else:
                raise AssertionError(f"unexpectedly served {path}")
        request = Request(base + "/", method="POST")
        try:
            with urlopen(request):
                pass
        except HTTPError as error:
            assert error.code == 405
            error.close()
        else:
            raise AssertionError("POST must be disabled")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_symlink_and_traversal_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "outside.md"
    secret.write_text("secret", encoding="utf-8")
    (root / "link.md").symlink_to(secret)
    assert _safe_file(root, ["link.md"]) is None
    assert _safe_file(root, ["..", "outside.md"]) is None
    assert _safe_file(root, [".env"]) is None
    assert _safe_file(root, ["entries", r"folder\..\..\..\private.md"]) is None
    assert _safe_file(root, ["C:private.md"]) is None

    notebook_target = tmp_path / "notebook-target"
    (notebook_target / "entries").mkdir(parents=True)
    (notebook_target / "entries/leak.md").write_text("private", encoding="utf-8")
    (root / "notebook").symlink_to(notebook_target, target_is_directory=True)
    assert b"leak.md" not in _index(root)
