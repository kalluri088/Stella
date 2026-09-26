"""B1: argument-aware risk elevation must be strictly additive.

The proof obligation the deferral set ("a classifier would have to be
proven not to weaken the exact-match approval boundary"): for every
registered capability and any arguments, the effective risk is never
below the floor, a DANGEROUS floor never loses its approval, the
ApprovalRequest exact-match verification is untouched, and elevation
only ever ADDS an approval prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stella.memory import SQLiteMemory
from stella.os_tools import KeySendTool, ScreenReadTool, WindowFocusTool
from stella.reminders import SQLiteReminderStore
from stella.tools import (
    _RISK_ORDER,
    ApprovalRequest,
    DateTimeTool,
    EchoTool,
    FileSystemReadTool,
    MemoryListTool,
    MemoryWriteTool,
    NetworkReadTool,
    ReminderCreateTool,
    ReminderListTool,
    RiskLevel,
    SystemInfoTool,
    ToolApproval,
    ToolDispatcher,
    WorkspaceFindTool,
    WorkspaceListTool,
    WorkspaceSearchTool,
    _looks_sensitive_path,
)


def default_dispatcher(tmp_path: Path) -> ToolDispatcher:
    memory = SQLiteMemory(":memory:")
    reminders = SQLiteReminderStore(":memory:")
    from stella.tools import (
        MemoryForgetTool,
        MemoryUpdateTool,
        ReminderCancelTool,
    )

    return ToolDispatcher(
        [
            EchoTool(),
            SystemInfoTool(),
            DateTimeTool(),
            FileSystemReadTool(tmp_path),
            WorkspaceListTool(tmp_path),
            WorkspaceFindTool(tmp_path),
            WorkspaceSearchTool(tmp_path),
            MemoryListTool(memory),
            MemoryWriteTool(memory),
            MemoryUpdateTool(memory),
            MemoryForgetTool(memory),
            ReminderCreateTool(reminders),
            ReminderListTool(reminders),
            ReminderCancelTool(reminders),
            NetworkReadTool(),
        ]
    )


ARGUMENT_SAMPLES: tuple[dict[str, object], ...] = (
    {},
    {"path": "notes.txt"},
    {"path": ".env"},
    {"path": "secrets/api_token.pem"},
    {"query": "anything"},
    {"scope": "full_screen"},
    {"url": "https://example.invalid/"},
    {"content": "x" * 10},
    {"nonsense": object()},
)


class TestMonotonicity:
    """The core proof: elevation raises, never lowers."""

    def test_effective_risk_never_below_floor_for_any_arguments(
        self, tmp_path: Path
    ) -> None:
        dispatcher = default_dispatcher(tmp_path)
        for entry in dispatcher.describe():
            capability = str(entry["capability"])
            floor = dispatcher.risk_level(capability)
            assert floor is not None
            for arguments in ARGUMENT_SAMPLES:
                effective = dispatcher.effective_risk_level(capability, arguments)
                assert effective is not None
                assert _RISK_ORDER[effective] >= _RISK_ORDER[floor], (
                    f"{capability} with {arguments} demoted "
                    f"{floor.value} to {effective.value}"
                )

    def test_dangerous_floor_never_loses_its_approval(
        self, tmp_path: Path
    ) -> None:
        dispatcher = default_dispatcher(tmp_path)
        for entry in dispatcher.describe():
            capability = str(entry["capability"])
            if dispatcher.risk_level(capability) is RiskLevel.DANGEROUS:
                for arguments in ARGUMENT_SAMPLES:
                    assert dispatcher.requires_approval(capability, arguments)

    def test_no_arguments_means_the_floor_question(self, tmp_path: Path) -> None:
        dispatcher = default_dispatcher(tmp_path)
        for entry in dispatcher.describe():
            capability = str(entry["capability"])
            assert dispatcher.requires_approval(capability) is (
                dispatcher.risk_level(capability) is RiskLevel.DANGEROUS
            )

    def test_execute_refuses_unapproved_even_when_ask_caller_disagrees(
        self, tmp_path: Path
    ) -> None:
        # Defense in depth: even if a caller forgot to ask, execute()
        # re-derives the effective risk from validated arguments and
        # refuses on its own.
        dispatcher = default_dispatcher(tmp_path)
        result = dispatcher.execute(
            "filesystem_read", {"path": "credentials/aws.txt"}, None
        )
        assert not result.success
        assert result.output == "Approval required."


class TestApprovalTokenUntouched:
    def test_elevated_call_runs_only_with_exact_match(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "credentials").mkdir()
        (tmp_path / "credentials" / "aws.txt").write_text("AKIA123\n", encoding="utf-8")
        dispatcher = default_dispatcher(tmp_path)
        arguments = {"path": "credentials/aws.txt"}
        request = ApprovalRequest("filesystem_read", dict(arguments))

        mismatched = dispatcher.execute(
            "filesystem_read",
            arguments,
            ToolApproval(ApprovalRequest("filesystem_read", {"path": "other.txt"}), True),
        )
        assert not mismatched.success
        assert mismatched.output == "Invalid approval."

        denied = dispatcher.execute(
            "filesystem_read", arguments, ToolApproval(request, False)
        )
        assert not denied.success

        approved = dispatcher.execute(
            "filesystem_read", arguments, ToolApproval(request, True)
        )
        assert approved.success
        assert "AKIA123" in approved.output

    def test_elevation_happens_after_validation_not_before(
        self, tmp_path: Path
    ) -> None:
        dispatcher = default_dispatcher(tmp_path)
        # Workspace-escaping path: rejected by validation itself; no
        # approval machinery is ever consulted for it.
        result = dispatcher.execute("filesystem_read", {"path": "../.ssh/id_rsa"}, None)
        assert not result.success
        assert result.output == "Invalid tool arguments."


class TestSensitivePathRule:
    @pytest.mark.parametrize(
        ("path", "sensitive"),
        [
            ("notes.txt", False),
            ("src/main.py", False),
            (".env", True),
            (".env.local", True),
            ("config/.environment", True),
            ("secrets/db.txt", True),
            ("credentials/aws", True),
            ("tokens.json", True),
            ("keys/id_ed25519", False),  # "keys" alone is a directory name
            ("id_rsa", True),
            ("certs/server.pem", True),
            ("certs/server.crt", False),
            ("passwd", True),
            ("docs\\Passwords.md", True),
        ],
    )
    def test_matcher(self, path: str, sensitive: bool) -> None:
        assert _looks_sensitive_path(path) is sensitive

    def test_plain_read_stays_unapproved(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
        dispatcher = default_dispatcher(tmp_path)
        result = dispatcher.execute("filesystem_read", {"path": "notes.txt"}, None)
        assert result.success

    def test_workspace_search_is_immune_without_path_argument(
        self, tmp_path: Path
    ) -> None:
        # The find/list/search tools inherit the read floor but answer to
        # their own argument shapes; elevation must not fire on them.
        dispatcher = default_dispatcher(tmp_path)
        assert dispatcher.requires_approval(
            "workspace_search", {"query": "password"}
        ) is False


class TestScreenReadRule:
    def _tool(self) -> ScreenReadTool:
        class DeafHyprland:
            def active_window(self):  # pragma: no cover - never reached
                raise AssertionError("must not be consulted for risk")

        return ScreenReadTool(DeafHyprland())  # type: ignore[arg-type]

    def test_full_screen_escalates_active_window_does_not(self) -> None:
        tool = self._tool()
        dispatcher = ToolDispatcher([tool])
        assert tool.risk_level is RiskLevel.SENSITIVE
        assert dispatcher.requires_approval("screen_read", {"scope": "full_screen"})
        assert not dispatcher.requires_approval("screen_read", {"scope": "active_window"})
        assert not dispatcher.requires_approval("screen_read")

    def test_window_and_key_tools_keep_their_floors(self) -> None:
        class DeafHyprland:
            def active_window(self):  # pragma: no cover
                raise AssertionError

        dispatcher = ToolDispatcher(
            [WindowFocusTool(DeafHyprland()), KeySendTool(DeafHyprland())]  # type: ignore[arg-type]
        )
        for arguments in ARGUMENT_SAMPLES:
            assert not dispatcher.requires_approval("window_focus", arguments)
            # key_send was DANGEROUS before B1 and stays so for any arguments
            assert dispatcher.requires_approval("key_send", arguments)


class TestAuditHonesty:
    def test_elevated_refusal_is_recorded_as_dangerous(
        self, tmp_path: Path
    ) -> None:
        dispatcher = default_dispatcher(tmp_path)
        dispatcher.execute("filesystem_read", {"path": "prod/.env"}, None)
        record = dispatcher.audit_records[0]
        assert record.risk_level is RiskLevel.DANGEROUS
        assert record.approval_required is True
        assert record.approval_granted is False

    def test_nonelevated_call_records_the_floor(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
        dispatcher = default_dispatcher(tmp_path)
        dispatcher.execute("filesystem_read", {"path": "notes.txt"}, None)
        record = dispatcher.audit_records[0]
        assert record.risk_level is RiskLevel.SENSITIVE
        assert record.approval_required is False
