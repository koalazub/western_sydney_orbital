"""Read-only, allowlisted HTTP view of notebook and campaign artefacts."""

from __future__ import annotations

import argparse
import html
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

CAMPAIGN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
REPORT_FILES = {"summary.md", "campaign.json", "results.csv"}


def _safe_file(root: Path, parts: list[str]) -> Path | None:
    if not parts or any(
        part in {"", ".", ".."}
        or part.startswith(".")
        or "\\" in part
        or re.match(r"^[A-Za-z]:", part)
        for part in parts
    ):
        return None
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return current if current.is_file() else None


def _listed(root: Path, base: str, allowed: set[str] | None, suffix: str) -> list[Path]:
    folder = root
    for part in base.split("/"):
        folder = folder / part
        if folder.is_symlink() or not folder.is_dir():
            return []
    if not folder.is_dir():
        return []
    return sorted(
        (
            p
            for p in folder.iterdir()
            if (allowed is None or p.name in allowed)
            and p.suffix == suffix
            and not p.is_symlink()
            and p.is_file()
        ),
        key=lambda p: p.name,
        reverse=True,
    )


def _index(root: Path) -> bytes:
    links = ['<li><a href="notebook/NOTEBOOK.md">Notebook index</a></li>']
    for entry in _listed(root, "notebook/entries", None, ".md"):
        links.append(
            f'<li><a href="notebook/entries/{html.escape(quote(entry.name, safe=""))}">'
            f"{html.escape(entry.stem)}</a></li>"
        )
    runs = root / "runs"
    if runs.is_dir() and not runs.is_symlink():
        for group in sorted(runs.iterdir()):
            if group.is_symlink() or not group.is_dir() or not CAMPAIGN_ID.fullmatch(group.name):
                continue
            available = [
                name
                for name in sorted(REPORT_FILES)
                if _safe_file(root, ["runs", group.name, name])
            ]
            if not available:
                continue
            prefix = f"runs/{quote(group.name, safe='')}/"
            report_links = " · ".join(
                f'<a href="{prefix}{quote(name, safe="")}">{html.escape(name)}</a>'
                for name in available
            )
            links.append(f"<li>{html.escape(group.name)}: {report_links}</li>")
    body = "<!doctype html><meta charset=utf-8><title>WSORB shared notebook</title>"
    body += "<h1>WSORB shared notebook and reports</h1><ul>" + "".join(links) + "</ul>"
    return body.encode()


class SharingHandler(BaseHTTPRequestHandler):
    server_version = "WSORBShare/1"
    root: Path

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self.send_error(404)
            return
        decoded = unquote(parsed.path)
        if decoded == "/healthz":
            data, content_type = b"ok\n", "text/plain; charset=utf-8"
        elif decoded == "/":
            data, content_type = _index(self.root), "text/html; charset=utf-8"
        else:
            match = re.fullmatch(r"/(notebook|runs)/(.+)", decoded)
            if not match:
                self.send_error(404)
                return
            area, relative = match.groups()
            parts = relative.split("/")
            if area == "notebook":
                if parts == ["NOTEBOOK.md"]:
                    path = _safe_file(self.root, ["notebook", "NOTEBOOK.md"])
                elif len(parts) == 2 and parts[0] == "entries" and parts[1].endswith(".md"):
                    path = _safe_file(self.root, ["notebook", *parts])
                else:
                    path = None
            elif len(parts) == 2 and CAMPAIGN_ID.fullmatch(parts[0]) and parts[1] in REPORT_FILES:
                path = _safe_file(self.root, ["runs", *parts])
            else:
                path = None
            if path is None:
                self.send_error(404)
                return
            data = path.read_bytes()
            content_type = {
                ".md": "text/markdown; charset=utf-8",
                ".json": "application/json",
                ".csv": "text/csv; charset=utf-8",
            }.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self) -> None:
        self.send_error(405)

    def do_POST(self) -> None:
        self.send_error(405)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"sharing: {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("WSORB_PORT", "8765")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    SharingHandler.root = Path.cwd().resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), SharingHandler)
    server.daemon_threads = True
    print(f"WSORB read-only backend ready at http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
