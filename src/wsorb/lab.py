"""Launch the repository's editor with a private, reusable Marimo password file."""

from __future__ import annotations

import importlib.util
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK_PATH = (PROJECT_ROOT / "notebooks/flight_lab.py").resolve()
DEFAULT_PORT = 2718
DEFAULT_TOKEN_FILE = Path(".devenv/wsorb-marimo.token")
MAX_TOKEN_BYTES = 44
PUBLIC_DIRECTORIES = ("worker/public", "worker/web", "dist")


def lab_port(value: str | int) -> int:
    """Reject ephemeral ports and anything that cannot name one actual TCP port."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or re.fullmatch(r"[1-9][0-9]{0,4}", str(value)) is None
        or not 1 <= int(value) <= 65535
    ):
        raise ValueError("WSORB_LAB_PORT must be an integer from 1 to 65535")
    return int(value)


def _token_path(root: Path, configured: str | Path) -> Path:
    if not str(configured):
        raise ValueError("WSORB_LAB_TOKEN_FILE must name a private token file")
    path = Path(os.path.abspath(root / configured))
    for directory in PUBLIC_DIRECTORIES:
        public = root / directory
        if path.is_relative_to(public) or path.is_relative_to(public.resolve()):
            raise ValueError("Marimo token file must be outside public assets")
    for parent in path.parents:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Marimo token directories must not use symlinks")
    return path


def _check_token_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_size not in (43, MAX_TOKEN_BYTES)
    ):
        raise ValueError("Marimo token must be an owned private regular file, mode 0600")


def _read_token_file(path: Path) -> None:
    info = path.lstat()
    _check_token_file(info)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        _check_token_file(opened)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("Marimo token file changed while opening it")
        content = os.read(descriptor, MAX_TOKEN_BYTES + 1)
        _check_token_file(os.fstat(descriptor))
        if re.fullmatch(rb"[A-Za-z0-9_-]{43}\n?", content) is None:
            raise ValueError("Marimo token file must contain one 43-character ASCII token")
    finally:
        os.close(descriptor)


def prepare_token_file(*, root: Path = PROJECT_ROOT, path: str | Path | None = None) -> Path:
    """Create once with exclusive/no-follow open, or validate without replacing it.

    Relative overrides resolve from the repository, not the caller's directory.
    The Node notebook resolver applies the same path, mode and token checks.
    """
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "getuid"):
        raise ValueError("The local lab requires POSIX private-file protections")
    configured = (
        os.environ.get("WSORB_LAB_TOKEN_FILE", DEFAULT_TOKEN_FILE) if path is None else path
    )
    try:
        token_file = _token_path(root.resolve(), configured)
        token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if token_file.parent.stat().st_mode & 0o022:
            raise ValueError("Marimo token directory must not be group or world writable")
        try:
            descriptor = os.open(
                token_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as token:
                os.fchmod(token.fileno(), 0o600)
                token.write((secrets.token_urlsafe(32) + "\n").encode("ascii"))
                token.flush()
                os.fsync(token.fileno())
        _read_token_file(token_file)
    except OSError:
        raise ValueError("Cannot create or read the private Marimo token file") from None
    return token_file


def lab_command(token_file: Path, *, port: str | int = DEFAULT_PORT) -> list[str]:
    """Build public CLI arguments without launching a process or reading a secret."""
    return [
        "marimo",
        "--quiet",
        "edit",
        str(NOTEBOOK_PATH),
        "--host",
        "127.0.0.1",
        "--port",
        str(lab_port(port)),
        "--headless",
        "--no-sandbox",
        "--skip-update-check",
        "--token",
        "--token-password-file",
        str(token_file),
    ]


def launch_lab(*, port: str | int | None = None) -> NoReturn:
    """Replace this process with the real Marimo CLI, bound only to loopback."""
    port = lab_port(os.environ.get("WSORB_LAB_PORT", DEFAULT_PORT) if port is None else port)
    if importlib.util.find_spec("marimo") is None or shutil.which("marimo") is None:
        raise ValueError(
            "Marimo is optional. Start the lab with `uv run --locked --extra lab wsorb lab`"
        )
    if not NOTEBOOK_PATH.is_file():
        raise ValueError("The repository's notebooks/flight_lab.py is missing")
    command = lab_command(prepare_token_file(), port=port)
    os.chdir(PROJECT_ROOT)
    os.execvp(command[0], command)
