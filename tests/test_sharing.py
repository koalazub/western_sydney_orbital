from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wsorb.sharing import SharingHandler, _index, _safe_file


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
