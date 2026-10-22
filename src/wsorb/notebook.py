"""A git-backed shared notebook.

One file per entry keeps merges trivial: two people adding notes never touch the
same file. The generated index is a convenience, not the source of truth.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        return json.dumps(value)
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
    stamp = created.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-{_slugify(title)}"
    path = entries_dir / f"{base}.md"
    suffix = 2
    while path.exists():
        path = entries_dir / f"{base}-{suffix}.md"
        suffix += 1

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

    front = "\n".join(f"{key} = {_toml_value(value)}" for key, value in metadata.items())
    content = f"+++\n{front}\n+++\n\n# {title.strip()}\n\n{body.strip()}\n"
    path.write_text(content, encoding="utf-8")
    return path


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
    parts = text.split("+++", 2)
    if len(parts) < 3:
        raise ValueError(f"{entry_path} has no TOML front matter")
    metadata = tomllib.loads(parts[1])
    body = parts[2].strip()
    return NotebookEntry(path=entry_path, metadata=metadata, body=body)


def list_entries(notebook_dir: str | Path) -> list[NotebookEntry]:
    entries_dir = Path(notebook_dir) / "entries"
    if not entries_dir.exists():
        return []
    entries = [read_entry(path) for path in entries_dir.glob("*.md")]
    return sorted(entries, key=lambda entry: (entry.created, entry.path.name), reverse=True)


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


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
                    run_link = f"[{entry.run}](../{relative.as_posix()}/summary.md)"
                except ValueError:
                    run_link = entry.run
            tags = ", ".join(f"`{tag}`" for tag in entry.tags)
            lines.append(
                f"| {entry.created} | [{_escape_cell(entry.title)}](entries/{entry.path.name}) | "
                f"{_escape_cell(entry.author)} | `{entry.kind}` | {tags} | {run_link} |"
            )
    else:
        lines.append("_No entries yet._")
    return "\n".join(lines)


def write_index(notebook_dir: str | Path) -> Path:
    notebook = Path(notebook_dir)
    notebook.mkdir(parents=True, exist_ok=True)
    path = notebook / "NOTEBOOK.md"
    path.write_text(render_index(notebook), encoding="utf-8")
    return path
