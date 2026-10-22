"""A git-backed shared notebook.

One file per entry reduces merge conflicts. The generated index is a convenience,
not the source of truth.
"""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

VALID_KINDS = {"note", "hypothesis", "run", "campaign", "decision", "retro", "reference"}


@dataclass(frozen=True, slots=True)
class NotebookEntry:
    path: Path
    metadata: dict[str, Any]
    body: str

    @property
    def title(self) -> str:
        return str(self.metadata.get("title", self.path.stem))

    @property
    def author(self) -> str:
        return str(self.metadata.get("author", "unknown"))

    @property
    def kind(self) -> str:
        return str(self.metadata.get("kind", "note"))

    @property
    def created(self) -> str:
        return str(self.metadata.get("created", ""))

    @property
    def tags(self) -> list[str]:
        raw = self.metadata.get("tags", [])
        if isinstance(raw, str):
            return [raw]
        return [str(item) for item in raw]

    @property
    def run(self) -> str | None:
        value = self.metadata.get("run")
        return str(value) if value else None


def _slugify(title: str, max_length: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug or "entry")[:max_length].rstrip("-")


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float, bool)):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported notebook metadata value: {type(value)!r}")


def _write_entry(
    entries_dir: Path,
    *,
    title: str,
    author: str,
    kind: str,
    tags: list[str],
    body: str,
    run: str | None,
    created: datetime,
) -> Path:
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown notebook kind {kind!r}; use one of {sorted(VALID_KINDS)}")
    if not title.strip():
        raise ValueError("title cannot be empty")
    if not author.strip():
        raise ValueError("author cannot be empty")

    entries_dir.mkdir(parents=True, exist_ok=True)
    stamp = created.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    base = f"{stamp}-{_slugify(title)}"
    path = entries_dir / f"{base}.md"
    suffix = 2

    metadata: dict[str, Any] = {
        "id": path.stem,
        "title": title.strip(),
        "created": created.astimezone(UTC).isoformat(timespec="seconds"),
        "author": author.strip(),
        "kind": kind,
        "tags": [tag.strip() for tag in tags if tag.strip()],
    }
    if run:
        metadata["run"] = run

    while True:
        metadata["id"] = path.stem
        front = "\n".join(f"{key} = {_toml_value(value)}" for key, value in metadata.items())
        content = f"+++\n{front}\n+++\n\n# {title.strip()}\n\n{body.strip()}\n"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            return path
        except FileExistsError:
            path = entries_dir / f"{base}-{suffix}.md"
            suffix += 1


def add_entry(
    notebook_dir: str | Path,
    *,
    title: str,
    author: str,
    kind: str = "note",
    tags: list[str] | None = None,
    body: str = "",
    run: str | None = None,
    created: datetime | None = None,
) -> Path:
    notebook = Path(notebook_dir)
    return _write_entry(
        notebook / "entries",
        title=title,
        author=author,
        kind=kind,
        tags=tags or [],
        body=body,
        run=run,
        created=created or datetime.now(UTC),
    )


def read_entry(path: str | Path) -> NotebookEntry:
    entry_path = Path(path)
    text = entry_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "+++":
        raise ValueError(f"{entry_path} has no TOML front matter")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "+++")
    except StopIteration as exc:
        raise ValueError(f"{entry_path} has unclosed TOML front matter") from exc
    try:
        metadata = tomllib.loads("".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{entry_path}: invalid notebook metadata: {exc}") from exc
    tags = metadata.get("tags", [])
    if not isinstance(tags, (str, list)):
        raise ValueError(f"{entry_path}: tags must be text or a list")
    body = "".join(lines[end + 1 :]).strip()
    return NotebookEntry(path=entry_path, metadata=metadata, body=body)


def list_entries(notebook_dir: str | Path) -> list[NotebookEntry]:
    entries_dir = Path(notebook_dir) / "entries"
    if not entries_dir.exists():
        return []
    entries = [read_entry(path) for path in entries_dir.glob("*.md")]
    return sorted(entries, key=lambda entry: (entry.created, entry.path.name), reverse=True)


def _escape_cell(value: str) -> str:
    return re.sub(r"([\\|\[\]`])", r"\\\1", value.replace("\n", " ").replace("\r", " "))


def render_index(notebook_dir: str | Path) -> str:
    notebook = Path(notebook_dir)
    entries = list_entries(notebook)
    lines = [
        "# Shared mission notebook",
        "",
        "> Generated from notebook entries.",
        "",
    ]
    if entries:
        lines.extend(
            [
                "| Date (UTC) | Entry | Author | Kind | Tags | Run |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for entry in entries:
            run_link = ""
            if entry.run:
                target = (notebook.parent / entry.run).resolve()
                try:
                    relative = target.relative_to(notebook.parent.resolve())
                    url = quote(f"../{relative.as_posix()}/summary.md", safe="/")
                    run_link = f"[{_escape_cell(entry.run)}]({url})"
                except ValueError:
                    run_link = _escape_cell(entry.run)
            tags = ", ".join(_escape_cell(tag) for tag in entry.tags)
            lines.append(
                f"| {_escape_cell(entry.created)} | "
                f"[{_escape_cell(entry.title)}](entries/{quote(entry.path.name, safe='')}) | "
                f"{_escape_cell(entry.author)} | {_escape_cell(entry.kind)} | {tags} | {run_link} |"
            )
    else:
        lines.append("_No entries yet._")
    return "\n".join(lines) + "\n"


def write_index(notebook_dir: str | Path) -> Path:
    notebook = Path(notebook_dir)
    notebook.mkdir(parents=True, exist_ok=True)
    path = notebook / "NOTEBOOK.md"
    content = render_index(notebook)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=notebook, prefix=".index-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
