"""Tests for the bounded action-history stores behind the dispatcher trail."""

from __future__ import annotations

from stella.history import InMemoryActionHistory, SQLiteActionHistory


def entry(index: int) -> dict[str, object]:
    return {"capability": f"tool_{index}", "arguments": {}, "timestamp": str(index)}


def test_in_memory_history_is_bounded_and_newest_first() -> None:
    history = InMemoryActionHistory(max_records=3)

    for index in range(5):
        history.append(entry(index))

    assert [record["capability"] for record in history.recent(10)] == [
        "tool_2",
        "tool_3",
        "tool_4",
    ]
    assert [record["capability"] for record in history.recent(2)] == [
        "tool_3",
        "tool_4",
    ]
    assert history.recent(0) == []


def test_sqlite_history_round_trips_entries_in_order(tmp_path) -> None:
    history = SQLiteActionHistory(tmp_path / "history.db", max_records=10)
    records = [
        {
            "capability": "filesystem_write",
            "arguments": {"path": "notes.txt", "content": "<42 characters>"},
            "risk_level": "dangerous",
            "approval_required": True,
            "approval_granted": True,
            "execution_success": True,
            "timestamp": "2026-09-24T00:00:00+00:00",
            "action_receipt": {
                "action": "create",
                "status": "verified",
                "size_bytes": 42,
            },
        }
    ]

    for record in records:
        history.append(record)

    assert history.recent(10) == records
    history.close()


def test_sqlite_history_enforces_retention_on_append(tmp_path) -> None:
    path = tmp_path / "history.db"
    history = SQLiteActionHistory(path, max_records=4)

    for index in range(10):
        history.append(entry(index))

    assert [record["capability"] for record in history.recent(100)] == [
        "tool_6",
        "tool_7",
        "tool_8",
        "tool_9",
    ]
    history.close()


def test_sqlite_history_survives_reopen(tmp_path) -> None:
    path = tmp_path / "history.db"
    first = SQLiteActionHistory(path, max_records=8)
    first.append(entry(1))
    first.append(entry(2))
    first.close()

    reopened = SQLiteActionHistory(path, max_records=8)

    assert [record["capability"] for record in reopened.recent(8)] == [
        "tool_1",
        "tool_2",
    ]
    assert reopened.recent(1) == [entry(2)]
    reopened.close()


def test_sqlite_history_recent_limit_and_empty(tmp_path) -> None:
    history = SQLiteActionHistory(tmp_path / "history.db", max_records=8)

    assert history.recent(5) == []
    history.append(entry(0))
    assert history.recent(0) == []

    history.close()
