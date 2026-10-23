from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from wsorb.notebook import add_entry, list_entries, read_entry, write_index


def test_frontmatter_round_trip_and_generated_index(tmp_path) -> None:
    created = datetime(2026, 1, 2, tzinfo=UTC)
    path = add_entry(
        tmp_path,
        title="Relay | test",
        author="Ada",
        kind="hypothesis",
        tags=["comms"],
        body="Test body.",
        created=created,
    )
    entry = read_entry(path)
    assert entry.title == "Relay | test"
    assert entry.tags == ["comms"]
    assert entry.body.endswith("Test body.")
    assert list_entries(tmp_path) == [entry]
    index = write_index(tmp_path).read_text()
    assert "Relay \\| test" in index


@pytest.mark.parametrize("title", ["Rescue 🚀", "C+++ notes", 'A "quoted" title', "澳大利亚"])
def test_titles_round_trip_as_toml(tmp_path, title) -> None:
    entry = add_entry(tmp_path, title=title, author="Zoë 🚀", body="A +++ marker in the body.")
    parsed = read_entry(entry)
    assert parsed.title == title
    assert parsed.author == "Zoë 🚀"
    assert parsed.body.endswith("A +++ marker in the body.")


def test_concurrent_entries_do_not_overwrite_each_other(tmp_path) -> None:
    created = datetime(2026, 1, 2, tzinfo=UTC)

    def write(number):
        return add_entry(
            tmp_path, title="Same title", author="Team", body=f"Note {number}", created=created
        )

    with ThreadPoolExecutor(max_workers=8) as workers:
        paths = list(workers.map(write, range(16)))
    assert len(set(paths)) == 16
    assert len({read_entry(path).body for path in paths}) == 16
    assert all(read_entry(path).metadata["id"] == path.stem for path in paths)


@pytest.mark.parametrize(
    "text", ["No metadata", 'text +++ title="bad" +++', '+++\ntitle="unclosed"\n']
)
def test_metadata_requires_standalone_delimiters(tmp_path, text) -> None:
    path = tmp_path / "invalid.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="front matter"):
        read_entry(path)
