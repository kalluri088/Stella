"""Outline tools: fake-transport tests, no network and no Outline server."""

from stella.app import StellaSettings
from stella.outline_tools import (
    OutlineClient,
    OutlineCreateTool,
    OutlineError,
    OutlineSearchTool,
    OutlineUpdateTool,
    build_outline_tools,
    outline_tool_summaries,
)
from stella.tools import ApprovalRequest, RiskLevel, action_summary


class FakeServer:
    """A scripted Outline API: routes are (method, path-prefix) -> payload."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, token, body):
        path = url.split("127.0.0.1:8741", 1)[-1]
        self.calls.append((method, path, body))
        best = None
        for route_method, prefix, payload in self.routes:
            if (
                method == route_method
                and path.startswith(prefix)
                and (best is None or len(prefix) >= len(best[0]))
            ):
                best = (prefix, payload)
        if best is not None:
            if isinstance(best[1], Exception):
                raise best[1]
            return 200, best[1]
        return 404, {"error": {"code": "not_found", "message": "no route " + path}}


def server_routes(**extra):
    routes = [
        ("GET", "/healthz", {"ok": True, "version": "1.0"}),
        ("GET", "/api/v1/today", {
            "day_start_ms": 0,
            "overdue_tasks": [{"id": 3, "title": "file taxes", "due_at": 1}],
            "today_tasks": [{"id": 7, "title": "standup", "due_at": 2}],
            "events": [{"id": 11, "title": "design review", "starts_at": 3}],
            "water_total_ml": 750,
            "active_timers": [{"id": 2, "label": "pasta", "state": "running"}],
            "upcoming_events": [],
            "open_task_count": 2,
        }),
        ("GET", "/api/v1/search", {"items": [
            {"kind": "task", "id": 12, "title": "drink water", "snippet": ""},
        ]}),
        ("GET", "/api/v1/tasks", {"items": [
            {"id": 12, "title": "drink water", "due_at": None, "status": "open"},
        ], "next_before_id": None}),
        ("GET", "/api/v1/events", {"items": [
            {"id": 21, "title": "dentist", "starts_at": 9_999_999_999_000},
        ]}),
        ("GET", "/api/v1/projects", {"items": [
            {"id": 5, "title": "Garden", "status": "active"},
        ], "next_before_id": None}),
        ("POST", "/api/v1/tasks", {"id": 40, "title": "drink water"}),
        ("POST", "/api/v1/water", {"id": 41, "amount_ml": 300}),
        ("POST", "/api/v1/timers", {"id": 42, "label": "pasta"}),
        ("POST", "/api/v1/events", {"id": 43, "title": "call mom"}),
        ("POST", "/api/v1/notes", {"id": 44, "body": "soil was dry"}),
        ("POST", "/api/v1/projects", {"id": 45, "title": "Inbox"}),
        ("POST", "/api/v1/people", {"id": 46, "name": "Ada"}),
        ("POST", "/api/v1/tasks/7/complete", {"id": 7, "title": "standup", "status": "done"}),
        ("PATCH", "/api/v1/tasks/7", {"id": 7, "title": "standup", "due_at": 2}),
        ("PATCH", "/api/v1/timers/2", {"id": 2, "label": "pasta", "state": "paused"}),
        ("PATCH", "/api/v1/projects/5", {"id": 5, "title": "Garden", "status": "archived"}),
    ]
    routes.extend(extra.items())
    return routes


def client(**extra):
    return OutlineClient(
        base_url="http://127.0.0.1:8741",
        token="t",
        transport=FakeServer(server_routes(**extra)),
    )


# --------------------------------------------------------------------------
# registration gating
# --------------------------------------------------------------------------


def test_missing_token_registers_nothing(tmp_path):
    env = {"OUTLINE_DATA_DIR": str(tmp_path / "absent")}
    assert build_outline_tools(env) == []


def test_token_file_and_health_probe_gate_registration(tmp_path):
    data_dir = tmp_path / "outline"
    data_dir.mkdir()
    (data_dir / "outline.token").write_text("secret-token\n")
    env = {"OUTLINE_DATA_DIR": str(data_dir)}
    # No injected transport here: nothing listens on 8741 in CI, so the
    # quarter-second probe fails and the tools stay unregistered.
    tools = build_outline_tools({**env, "OUTLINE_URL": "http://127.0.0.1:1"})
    assert tools == []


def test_registered_when_server_answers():
    tools = build_outline_tools(
        {"OUTLINE_TOKEN": "t"}, transport=FakeServer(server_routes())
    )
    assert [tool.name for tool in tools] == [
        "outline_search",
        "outline_create",
        "outline_update",
    ]
    assert tools[0].risk_level is RiskLevel.SAFE
    assert tools[1].risk_level is RiskLevel.SENSITIVE
    assert tools[2].risk_level is RiskLevel.SENSITIVE


def test_environment_token_wins_and_bad_url_rejected():
    assert build_outline_tools({"OUTLINE_URL": "ftp://x", "OUTLINE_TOKEN": "t"}) == []
    tools = build_outline_tools(
        {"OUTLINE_TOKEN": "t"}, transport=FakeServer(server_routes())
    )
    assert len(tools) == 3


# --------------------------------------------------------------------------
# outline_search
# --------------------------------------------------------------------------


def test_search_today_renders_bounded_framed_data():
    result = OutlineSearchTool(client()).execute({"when": "today"})
    assert result.success
    assert result.output.startswith("Outline results (stored data only")
    assert "[task#7] standup" in result.output
    assert "OVERDUE file taxes" in result.output
    assert "water today: 750 ml" in result.output


def test_search_query_hits_fts_endpoint():
    srv = FakeServer(server_routes())
    result = OutlineSearchTool(
        OutlineClient("http://127.0.0.1:8741", "t", srv)
    ).execute({"query": "drink water", "kind": "task"})
    assert result.success and "[task#12] drink water" in result.output
    assert any(path.startswith("/api/v1/search") for _, path, _ in srv.calls)


def test_search_rejects_bad_arguments():
    tool = OutlineSearchTool(client())
    assert not tool.validate_arguments({})
    assert not tool.validate_arguments({"when": "yesterday"})
    assert not tool.validate_arguments({"query": "x", "extra": "y"})
    assert tool.validate_arguments({"when": "overdue"})


def test_search_reports_unreachable_server_as_failure():
    def unreachable(*args):
        raise OutlineError(
            "the Outline server could not be reached — is it running?"
        )

    tool = OutlineSearchTool(
        OutlineClient("http://127.0.0.1:8741", "t", unreachable)
    )
    result = tool.execute({"when": "today"})
    assert not result.success
    assert "could not be reached" in result.output


def test_search_reports_rejected_request_as_failure():
    routes = server_routes()
    routes.append(
        ("GET", "/api/v1/today", OutlineError("Outline rejected the request: nope"))
    )
    result = OutlineSearchTool(
        OutlineClient("http://127.0.0.1:8741", "t", FakeServer(routes))
    ).execute({"when": "today"})
    assert not result.success and "rejected" in result.output


def test_search_filters_when_and_query_combined():
    result = OutlineSearchTool(client()).execute({"when": "today", "query": "standup"})
    assert "[task#7] standup" in result.output
    assert "file taxes" not in result.output


# --------------------------------------------------------------------------
# outline_create
# --------------------------------------------------------------------------


def test_create_task_with_project_resolved_by_title():
    srv = FakeServer(server_routes())
    tool = OutlineCreateTool(OutlineClient("http://127.0.0.1:8741", "t", srv))
    result = tool.execute(
        {"kind": "task", "title": "drink water", "project": "garden"}
    )
    assert result.success and "Created task #40" in result.output
    post = [call for call in srv.calls if call[0] == "POST" and call[1] == "/api/v1/tasks"]
    assert post and post[0][2]["project_id"] == 5


def test_create_task_unknown_project_fails_cleanly():
    result = OutlineCreateTool(client()).execute(
        {"kind": "task", "title": "x", "project": "Nope"}
    )
    assert not result.success and "no Outline project" in result.output


def test_create_event_requires_explicit_start():
    tool = OutlineCreateTool(client())
    assert not tool.validate_arguments({"kind": "event", "title": "call mom"})
    result = tool.execute({"kind": "task", "title": "x", "due_at": "nonsense"})
    assert not result.success


def test_create_event_from_iso_start():
    srv = FakeServer(server_routes())
    result = OutlineCreateTool(
        OutlineClient("http://127.0.0.1:8741", "t", srv)
    ).execute({"kind": "event", "title": "call mom", "due_at": "2026-09-27T17:00"})
    assert result.success
    event_post = [
        c for c in srv.calls if c[0] == "POST" and c[1] == "/api/v1/events"
    ]
    assert event_post and event_post[0][2]["ends_at"] - event_post[0][2]["starts_at"] == 3_600_000


def test_create_water_and_timer_and_note_defaults():
    tool = OutlineCreateTool(client())
    assert not tool.validate_arguments({"kind": "water", "title": "h"})
    assert tool.execute(
        {"kind": "water", "title": "h", "amount_ml": 300}
    ).output.startswith("Logged 300 ml")
    assert not tool.validate_arguments({"kind": "timer", "title": "pasta"})
    assert tool.execute(
        {"kind": "timer", "title": "pasta", "duration_ms": 600_000}
    ).success
    note = OutlineCreateTool(client())
    result = note.execute({"kind": "note", "title": "soil was dry"})
    assert result.success  # attaches to (or creates) the Inbox project


def test_create_rejects_injection_shaped_arguments():
    tool = OutlineCreateTool(client())
    assert not tool.validate_arguments(
        {"kind": "water", "title": "x", "amount_ml": "300"}
    )
    assert not tool.validate_arguments(
        {"kind": "water", "title": "x", "amount_ml": True}
    )
    assert not tool.validate_arguments(
        {"kind": "task", "title": "x", "due_at": "9e17"}
    )
    assert not tool.validate_arguments(
        {"kind": "task", "title": "x", "amount_ml": 300}
    )


# --------------------------------------------------------------------------
# outline_update
# --------------------------------------------------------------------------


def test_update_task_complete_and_timer_pause():
    tool = OutlineUpdateTool(client())
    assert tool.execute({"kind": "task", "id": 7, "action": "complete"}).success
    assert tool.execute({"kind": "timer", "id": 2, "action": "pause"}).success
    assert tool.execute({"kind": "project", "id": 5, "action": "archive"}).success


def test_update_validation_matrix():
    tool = OutlineUpdateTool(client())
    assert not tool.validate_arguments({"kind": "task", "id": 0, "action": "complete"})
    assert not tool.validate_arguments({"kind": "task", "id": 7, "action": "delete"})
    assert not tool.validate_arguments({"kind": "timer", "id": 2, "action": "complete"})
    assert not tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "reschedule"}  # due_at required
    )
    assert tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "reschedule", "due_at": "2026-10-01T09:00"}
    )


# --------------------------------------------------------------------------
# approval summaries (action_summary chain)
# --------------------------------------------------------------------------


def test_summaries_are_plain_language_and_bounded():
    assert outline_tool_summaries(
        "outline_search", {"query": "taxes", "when": "overdue"}
    ) == 'read your Outline data matching "taxes" in the overdue window (read-only)'
    create = outline_tool_summaries(
        "outline_create",
        {"kind": "task", "title": "drink water", "due_at": "2026-09-27T17:00"},
    )
    assert create == (
        'add a new task to Outline: "drink water" due "2026-09-27T17:00"'
    )
    assert outline_tool_summaries(
        "outline_update", {"kind": "timer", "id": 2, "action": "pause"}
    ) == "pause the Outline timer with id 2"
    assert outline_tool_summaries("other_tool", {}) is None


def test_action_summary_routes_through_outline_chain():
    summary = action_summary(
        ApprovalRequest("outline_create", {"kind": "water", "title": "h", "amount_ml": 250})
    )
    assert summary == "add a new water to Outline: \"h\" (250 ml)"


# --------------------------------------------------------------------------
# settings wiring
# --------------------------------------------------------------------------


def test_stella_environment_default_leaves_outline_tools_off(monkeypatch):
    monkeypatch.delenv("STELLA_OUTLINE", raising=False)
    settings = StellaSettings(provider="ollama", model="x")
    assert settings.outline_tools_enabled is False


def test_saved_settings_flag_respects_env_override(monkeypatch):
    monkeypatch.setenv("STELLA_OUTLINE", "1")
    settings = StellaSettings.from_saved(
        provider="ollama", model="x", outline_tools_enabled=False
    )
    assert settings.outline_tools_enabled is True
    monkeypatch.setenv("STELLA_OUTLINE", "0")
    settings = StellaSettings.from_saved(
        provider="ollama", model="x", outline_tools_enabled=True
    )
    assert settings.outline_tools_enabled is False
