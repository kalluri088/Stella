"""Approval cards for DANGEROUS capabilities must say what they would do.

The pattern this week: a preview must name the affected target — file
previews diff the path, os previews name the window. This test pins the
invariant registry-wide for the always-live capabilities: every DANGEROUS
tool overrides ``preview``, and the memory/reminder writes (added to the
pattern last) produce bounded, honest, side-effect-free detail lines —
including the "would do nothing" cases.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from stella.memory import MemoryItem, SQLiteMemory
from stella.reminders import SQLiteReminderStore
from stella.tools import (
    MAX_PREVIEW_LINES,
    ActionPreview,
    ApprovalRequest,
    DateTimeTool,
    FileSystemDeleteTool,
    FileSystemEditTool,
    FileSystemReadTool,
    FileSystemWriteTool,
    MemoryForgetTool,
    MemoryListTool,
    MemoryUpdateTool,
    MemoryWriteTool,
    NetworkReadTool,
    PersonaEditTool,
    ReminderCancelTool,
    ReminderCreateTool,
    ReminderListTool,
    RiskLevel,
    SystemInfoTool,
    Tool,
    WorkspaceFindTool,
    WorkspaceListTool,
    WorkspaceSearchTool,
)


@pytest.fixture
def stores(tmp_path: Path) -> tuple[SQLiteMemory, SQLiteReminderStore, Path]:
    return (
        SQLiteMemory(tmp_path / "memory.db"),
        SQLiteReminderStore(tmp_path / "reminders.db"),
        tmp_path,
    )


def _base_tools(memory: SQLiteMemory, reminders: SQLiteReminderStore, workspace: Path) -> list[Tool]:
    """The always-registered registry, mirroring build_application."""
    return [
        DateTimeTool(),
        SystemInfoTool(),
        FileSystemReadTool(workspace),
        FileSystemWriteTool(workspace),
        FileSystemEditTool(workspace),
        FileSystemDeleteTool(workspace),
        WorkspaceListTool(workspace),
        WorkspaceFindTool(workspace),
        WorkspaceSearchTool(workspace),
        NetworkReadTool(),
        MemoryListTool(memory),
        MemoryWriteTool(memory),
        MemoryUpdateTool(memory),
        MemoryForgetTool(memory),
        ReminderCreateTool(reminders),
        ReminderListTool(reminders),
        ReminderCancelTool(reminders),
        PersonaEditTool(),
    ]


def test_every_dangerous_tool_overrides_preview(stores) -> None:
    memory, reminders, workspace = stores
    bare = [
        tool.name
        for tool in _base_tools(memory, reminders, workspace)
        if tool.risk_level is RiskLevel.DANGEROUS
        and type(tool).preview is Tool.preview
    ]
    assert not bare, f"DANGEROUS tools with no preview implementation: {bare}"


def _assert_bounded_card(preview: ActionPreview | None) -> tuple[str, ...]:
    assert preview is not None, "card would render bare"
    assert preview.detail_lines, "card would render no detail"
    assert len(preview.detail_lines) <= MAX_PREVIEW_LINES + 2
    for line in preview.detail_lines:
        assert isinstance(line, str) and line.strip()
    return preview.detail_lines


def test_memory_write_card_shows_the_fact(stores) -> None:
    memory, _, _ = stores
    tool = MemoryWriteTool(memory)
    lines = _assert_bounded_card(
        tool.preview(ApprovalRequest("memory_write", {"content": "likes trains"}))
    )
    assert any("store" in line.lower() for line in lines)
    assert any("likes trains" in line for line in lines)
    assert memory.retrieve() == []  # previewing must not write


def test_memory_update_card_shows_old_and_new(stores) -> None:
    memory, _, _ = stores
    memory.store(MemoryItem(content="lives in Pune"))
    tool = MemoryUpdateTool(memory)
    preview = tool.preview(
        ApprovalRequest(
            "memory_update", {"query": "lives", "content": "lives in Delhi"}
        )
    )
    lines = _assert_bounded_card(preview)
    assert any("Pune" in line for line in lines), "card must show what is replaced"
    assert any("Delhi" in line for line in lines)
    assert [item.content for item in memory.retrieve()] == ["lives in Pune"]


def test_memory_update_card_is_honest_about_no_match(stores) -> None:
    memory, _, _ = stores
    tool = MemoryUpdateTool(memory)
    lines = _assert_bounded_card(
        tool.preview(
            ApprovalRequest("memory_update", {"query": "absent", "content": "x y z"})
        )
    )
    assert any("do nothing" in line for line in lines)


def test_memory_forget_card_names_the_victims(stores) -> None:
    memory, _, _ = stores
    memory.store(MemoryItem(content="secret snack location"))
    tool = MemoryForgetTool(memory)
    lines = _assert_bounded_card(
        tool.preview(ApprovalRequest("memory_forget", {"query": "snack"}))
    )
    assert any("secret snack location" in line for line in lines)
    assert len(memory.retrieve()) == 1  # previewing must not delete


def test_reminder_create_card_shows_content_and_time(stores) -> None:
    _, reminders, _ = stores
    tool = ReminderCreateTool(reminders)
    # The due time must be computed ahead of now, never hard-coded: a
    # pinned "future" date silently turns this card into the past-due
    # refusal card once the clock passes it (it did, on its own due day).
    due_at = (
        dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    ).replace(microsecond=0)
    raw_due = due_at.isoformat()
    lines = _assert_bounded_card(
        tool.preview(
            ApprovalRequest(
                "reminder_create",
                {"content": "call the dentist", "due_at": raw_due},
            )
        )
    )
    assert any("call the dentist" in line for line in lines)
    assert any(raw_due in line for line in lines)
    assert reminders.pending() == ()


def test_reminder_create_card_flags_unparseable_time(stores) -> None:
    _, reminders, _ = stores
    tool = ReminderCreateTool(reminders)
    lines = _assert_bounded_card(
        tool.preview(
            ApprovalRequest(
                "reminder_create", {"content": "x y", "due_at": "not a time"}
            )
        )
    )
    assert any("cannot be understood" in line for line in lines)


def test_reminder_create_card_flags_a_time_that_would_be_rejected(
    stores,
) -> None:
    _, reminders, _ = stores
    tool = ReminderCreateTool(reminders)
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    lines = _assert_bounded_card(
        tool.preview(
            ApprovalRequest("reminder_create", {"content": "x y", "due_at": past})
        )
    )
    assert any("would fail" in line for line in lines), (
        "the card must not advertise a reminder creation that execute rejects"
    )


def test_reminder_cancel_card_distinguishes_one_and_many(stores) -> None:
    _, reminders, _ = stores
    now = dt.datetime.now(dt.UTC)
    first = reminders.create("standup notes", now + dt.timedelta(days=1), now)
    second = reminders.create("standup drink", now + dt.timedelta(days=2), now)
    assert first is not None and second is not None
    tool = ReminderCancelTool(reminders)

    many = _assert_bounded_card(
        tool.preview(ApprovalRequest("reminder_cancel", {"query": "standup"}))
    )
    assert any("nothing would be cancelled" in line for line in many)

    one = _assert_bounded_card(
        tool.preview(ApprovalRequest("reminder_cancel", {"query": "drink"}))
    )
    assert any("standup drink" in line for line in one)
    assert len(reminders.pending()) == 2  # previewing must not cancel
