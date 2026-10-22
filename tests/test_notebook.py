from __future__ import annotations

from datetime import UTC, datetime

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
