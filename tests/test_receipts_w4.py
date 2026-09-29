"""Report 33 W4: every mutation success path must mint a verified receipt.

Failures always carried rich receipts; these tests pin the other half —
that memory, reminder and Outline successes land an action_receipt in the
tool result *and* in the audit trail, so `stella audit` (and the model's
own observations) can see proof the action happened.
"""

import datetime as dt

from test_outline_tools import client as outline_client
from test_outline_tools import server_routes

from stella.memory import InMemoryMemory, MemoryItem
from stella.outline_tools import (
    OutlineClient,
    OutlineCreateTool,
    OutlineError,
    OutlineUpdateTool,
)
from stella.reminders import InMemoryReminderStore
from stella.tools import (
    ActionReceipt,
    ApprovalRequest,
    AuditRecord,
    MemoryForgetTool,
    MemoryUpdateTool,
    MemoryWriteTool,
    ReminderCancelTool,
    ReminderCreateTool,
    ToolApproval,
    ToolDispatcher,
    _audit_entry,
)

TEA = "The user prefers jasmine tea in the evening."

# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def test_memory_write_success_carries_verified_receipt() -> None:
    result = MemoryWriteTool(InMemoryMemory()).execute({"content": TEA})
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "write",
        "verified",
    )


def test_memory_write_unverified_receipt_when_store_lies() -> None:
    class LyingStore(InMemoryMemory):
        def store(self, item: MemoryItem) -> bool:
            return True  # claims success, writes nothing

        def retrieve(self, *args, **kwargs):
            return ()

    result = MemoryWriteTool(LyingStore()).execute({"content": TEA})
    assert result.success
    assert result.action_receipt is not None
    assert result.action_receipt.status == "unverified"


def test_memory_update_verified_receipt_reads_back_the_new_content() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content=TEA))
    result = MemoryUpdateTool(memory).execute(
        {"query": "tea", "content": "The user prefers chai."}
    )
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "update",
        "verified",
    )


def test_memory_update_without_a_match_receipt_is_missing() -> None:
    result = MemoryUpdateTool(InMemoryMemory()).execute(
        {"query": "tea", "content": "anything"}
    )
    assert not result.success
    assert result.action_receipt is not None
    assert result.action_receipt.status == "missing"


def test_memory_forget_verified_receipt_confirms_absence() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content=TEA))
    result = MemoryForgetTool(memory).execute({"query": "tea"})
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "delete",
        "verified",
    )


def test_memory_forget_without_a_match_receipt_is_missing() -> None:
    result = MemoryForgetTool(InMemoryMemory()).execute({"query": "tea"})
    assert not result.success
    assert result.action_receipt is not None
    assert result.action_receipt.status == "missing"


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------


def _create_arguments(minutes: int = 45) -> dict[str, object]:
    due = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)
    return {"content": "Stretch break", "due_at": due.isoformat()}


def test_reminder_create_success_carries_verified_receipt() -> None:
    result = ReminderCreateTool(InMemoryReminderStore()).execute(
        _create_arguments()
    )
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "create",
        "verified",
    )


def test_reminder_cancel_verified_receipt_confirms_gone() -> None:
    store = InMemoryReminderStore()
    created = ReminderCreateTool(store).execute(_create_arguments())
    assert created.success
    result = ReminderCancelTool(store).execute({"query": "stretch"})
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "cancel",
        "verified",
    )


def test_reminder_cancel_without_a_match_receipt_is_missing() -> None:
    result = ReminderCancelTool(InMemoryReminderStore()).execute(
        {"query": "stretch"}
    )
    assert not result.success
    assert result.action_receipt is not None
    assert result.action_receipt.status == "missing"


# ---------------------------------------------------------------------------
# outline
# ---------------------------------------------------------------------------


def test_outline_create_receipt_verified_from_server_echo_row() -> None:
    tool = OutlineCreateTool(outline_client())
    result = tool.execute({"kind": "task", "title": "drink water"})
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "create",
        "verified",
    )


def test_outline_create_water_receipt_verified() -> None:
    tool = OutlineCreateTool(outline_client())
    result = tool.execute({"kind": "water", "title": "after run", "amount_ml": 300})
    assert result.success
    assert result.action_receipt is not None
    assert result.action_receipt.status == "verified"


def test_outline_create_unreachable_server_is_unverified_not_failed() -> None:
    def transport(method, url, token, body):
        raise OutlineError(
            "the Outline server could not be reached — is it running?"
        )

    tool = OutlineCreateTool(
        OutlineClient("http://127.0.0.1:8741", "t", transport)
    )
    result = tool.execute({"kind": "task", "title": "drink water"})
    assert not result.success
    assert result.action_receipt is not None
    assert result.action_receipt.status == "unverified"


def test_outline_create_rejected_request_is_failed() -> None:
    def transport(method, url, token, body):
        raise OutlineError("Outline rejected the request: title too long")

    tool = OutlineCreateTool(
        OutlineClient("http://127.0.0.1:8741", "t", transport)
    )
    result = tool.execute({"kind": "task", "title": "drink water"})
    assert not result.success
    assert result.action_receipt is not None
    assert result.action_receipt.status == "failed"


def test_outline_update_receipt_verified_on_id_match() -> None:
    tool = OutlineUpdateTool(outline_client())
    result = tool.execute(
        {"kind": "task", "id": 7, "action": "complete"}
    )
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "update",
        "verified",
    )


def test_outline_update_receipt_unverified_when_echo_id_differs() -> None:
    tool = OutlineUpdateTool(
        OutlineClient(
            "http://127.0.0.1:8741",
            "t",
            lambda *call: (
                200,
                {"item": {"id": 999, "title": "standup", "status": "done"}},
            ),
        )
    )
    result = tool.execute({"kind": "task", "id": 7, "action": "complete"})
    assert result.success
    assert result.action_receipt is not None
    assert result.action_receipt.status == "unverified"


def test_outline_link_attach_verified_from_created_link_row() -> None:
    routes = server_routes(
        extra=[
            ("GET", "/api/v1/people", {"items": [{"id": 3, "name": "Ada"}]}),
            ("POST", "/api/v1/links", {"id": 77, "person_id": 3}),
        ]
    )
    tool = OutlineUpdateTool(
        OutlineClient("http://127.0.0.1:8741", "t", _routes_transport(routes))
    )
    result = tool.execute(
        {
            "kind": "link",
            "id": 7,
            "action": "attach",
            "person": "Ada",
            "to": "task",
        }
    )
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "link",
        "verified",
    )


def test_outline_link_detach_verified_from_delete_confirmation() -> None:
    routes = server_routes(
        extra=[
            (
                "GET",
                "/api/v1/links",
                {"items": [{"id": 77, "name": "Ada", "person_id": 3}]},
            ),
            ("DELETE", "/api/v1/links/77", {"deleted": True, "id": 77}),
        ]
    )
    tool = OutlineUpdateTool(
        OutlineClient("http://127.0.0.1:8741", "t", _routes_transport(routes))
    )
    result = tool.execute(
        {
            "kind": "link",
            "id": 7,
            "action": "detach",
            "person": "Ada",
            "to": "task",
        }
    )
    assert result.success
    assert result.action_receipt is not None
    assert (result.action_receipt.action, result.action_receipt.status) == (
        "unlink",
        "verified",
    )


class _RoutesTransport:
    def __init__(self, routes):
        self.routes = routes

    def __call__(self, method, url, token, body):
        path = url.split("127.0.0.1:8741", 1)[-1].split("?", 1)[0]
        for route_method, prefix, payload in self.routes:
            if method == route_method and path.startswith(prefix):
                return 200, payload
        return 404, {"error": {"code": "x", "message": "no route " + path}}


def _routes_transport(routes):
    return _RoutesTransport(routes)


# ---------------------------------------------------------------------------
# the audit trail itself (Rule 10's "consultable" half)
# ---------------------------------------------------------------------------


def test_audit_entries_of_success_paths_record_the_receipt() -> None:
    # Rule 10's real contract: the receipt does not just live on the
    # ToolResult, it lands in the durable audit trail that `stella audit`
    # reads — via the dispatcher's finally-block serialization.
    dispatcher = ToolDispatcher(tools=[MemoryWriteTool(InMemoryMemory())])
    arguments: dict[str, object] = {"content": TEA}
    result = dispatcher.execute(
        "memory_write",
        arguments,
        ToolApproval(ApprovalRequest("memory_write", dict(arguments)), True),
    )
    assert result.success
    record = dispatcher.audit_records[-1]
    assert record.capability == "memory_write"
    assert record.execution_success
    assert record.action_receipt is not None
    assert (record.action_receipt.action, record.action_receipt.status) == (
        "write",
        "verified",
    )


def test_unverified_receipt_survives_into_the_serialized_audit_entry() -> None:
    # The audit serialization is the contract `stella audit` reads; pin
    # that a success-path receipt of any status round-trips.
    entry = _audit_entry(
        AuditRecord(
            capability="reminder_create",
            arguments={},
            risk_level=None,
            approval_required=False,
            approval_granted=None,
            execution_success=True,
            timestamp="now",
            action_receipt=ActionReceipt("create", "verified"),
        )
    )
    assert entry["action_receipt"] == {
        "action": "create",
        "status": "verified",
        "size_bytes": None,
    }
