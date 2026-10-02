"""Outline tools: fake-transport tests, no network and no Outline server."""

from stella.app import StellaSettings
from stella.outline_tools import (
    MAX_OUTPUT_LINES,
    OutlineClient,
    OutlineCreateTool,
    OutlineError,
    OutlineSearchTool,
    OutlineUpdateTool,
    build_outline_tools,
    claim_due_reminders,
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


def server_routes(extra=(), **named):
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
        ("POST", "/api/v1/tasks/7/complete", {"item": {"id": 7, "title": "standup", "status": "done"}}),
        ("PATCH", "/api/v1/tasks/7", {"item": {"id": 7, "title": "standup", "due_at": 2}}),
        ("PATCH", "/api/v1/timers/2", {"id": 2, "label": "pasta", "state": "paused"}),
        ("PATCH", "/api/v1/projects/5", {"id": 5, "title": "Garden", "status": "archived"}),
    ]
    routes.extend([*extra, *named.items()])
    return routes


def client(*extra, **named):
    return OutlineClient(
        base_url="http://127.0.0.1:8741",
        token="t",
        transport=FakeServer(server_routes(extra, **named)),
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


def test_complete_reports_next_recurrence():
    tool = OutlineUpdateTool(
        client(("POST", "/api/v1/tasks/8/complete", {
            "item": {"id": 8, "title": "water plants", "status": "done"},
            "next_task": {"id": 9, "title": "water plants", "status": "open"},
        }))
    )
    result = tool.execute({"kind": "task", "id": 8, "action": "complete"})
    assert result.success
    assert "next occurrence #9" in result.output


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


# --------------------------------------------------------------------------
# v1.1 parity: edit, event reschedule, links, recurrence, tags, graph, export note
# --------------------------------------------------------------------------


def test_create_task_recurrence_and_tags_reach_the_api():
    srv = FakeServer(server_routes())
    tool = OutlineCreateTool(OutlineClient("http://127.0.0.1:8741", "t", srv))
    result = tool.execute(
        {
            "kind": "task",
            "title": "water plants",
            "recurrence": "every:120",
            "tags": ["Garden", "chore", "garden"],
        }
    )
    assert result.success
    post = next(c for c in srv.calls if c[0] == "POST" and c[1] == "/api/v1/tasks")
    assert post[2]["recurrence"] == "every:120"
    assert post[2]["tags"] == ["garden", "chore"]


def test_create_rejects_bad_recurrence_and_tags():
    tool = OutlineCreateTool(client())
    base = {"kind": "task", "title": "x"}
    assert not tool.validate_arguments({**base, "recurrence": "daily; rm -rf /"})
    assert not tool.validate_arguments({**base, "recurrence": "every:59"})
    assert not tool.validate_arguments({**base, "recurrence": "every:525601"})
    assert not tool.validate_arguments({**base, "recurrence": "yearly"})
    assert not tool.validate_arguments({**base, "tags": ["<script>"]})
    assert not tool.validate_arguments({**base, "tags": ["ok", "a" * 41]})
    assert not tool.validate_arguments({**base, "tags": ["x"] * 21})
    assert tool.validate_arguments({**base, "recurrence": "weekdays"})
    assert tool.validate_arguments({**base, "recurrence": "every:525600"})
    assert tool.validate_arguments({**base, "tags": ["ok_1"]})


def test_create_event_custom_duration():
    srv = FakeServer(server_routes())
    result = OutlineCreateTool(
        OutlineClient("http://127.0.0.1:8741", "t", srv)
    ).execute(
        {
            "kind": "event",
            "title": "workshop",
            "due_at": "2026-10-01T09:00",
            "duration_ms": 7_200_000,
        }
    )
    assert result.success
    post = next(c for c in srv.calls if c[0] == "POST" and c[1] == "/api/v1/events")
    assert post[2]["ends_at"] - post[2]["starts_at"] == 7_200_000


def test_update_task_edit_maps_fields():
    srv = FakeServer(server_routes())
    tool = OutlineUpdateTool(OutlineClient("http://127.0.0.1:8741", "t", srv))
    result = tool.execute(
        {
            "kind": "task",
            "id": 7,
            "action": "edit",
            "edits": {
                "title": "standup v2",
                "body": "new room",
                "priority": 3,
                "recurrence": "every:120",
                "tags": ["Work", "dailyx"],
            },
        }
    )
    assert result.success and "edited" in result.output
    patch = next(c for c in srv.calls if c[0] == "PATCH")
    assert patch[2] == {
        "title": "standup v2",
        "notes": "new room",
        "priority": 3,
        "recurrence": "every:120",
        "tags": ["work", "dailyx"],
    }


def test_update_edit_clear_values_and_validation():
    tool = OutlineUpdateTool(client())
    assert tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "edit",
         "edits": {"recurrence": None, "tags": []}}
    )
    assert not tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "edit", "edits": {}}
    )
    assert not tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "edit", "edits": {"status": "done"}}
    )
    assert not tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "edit", "edits": {"priority": 5}}
    )
    assert not tool.validate_arguments(
        {"kind": "note", "id": 2, "action": "edit", "edits": {"title": "x"}}
    )
    assert not tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "edit", "edits": {"title": "x"},
         "due_at": "2026-10-01T09:00"}
    )
    assert tool.validate_arguments(
        {"kind": "project", "id": 5, "action": "edit", "edits": {"body": "notes"}}
    )


def test_update_event_reschedule_preserves_duration():
    routes = server_routes([
        (
            "GET",
            "/api/v1/events/21",
            {"id": 21, "title": "dentist", "starts_at": 1_700_000_000_000,
             "ends_at": 1_700_007_200_000},
        ),
        (
            "PATCH",
            "/api/v1/events/21",
            {"id": 21, "title": "dentist", "starts_at": 2, "ends_at": 3},
        ),
    ])
    srv = FakeServer(routes)
    tool = OutlineUpdateTool(OutlineClient("http://127.0.0.1:8741", "t", srv))
    result = tool.execute(
        {"kind": "event", "id": 21, "action": "reschedule",
         "due_at": "2026-10-05T14:00+00:00"}
    )
    assert result.success
    patch = next(c for c in srv.calls if c[0] == "PATCH")
    start = patch[2]["starts_at"]
    assert start == 1_791_208_800_000  # 2026-10-05T14:00Z
    assert patch[2]["ends_at"] == start + 7_200_000


def test_update_link_attach_and_detach():
    routes = server_routes([
        ("GET", "/api/v1/people", {"items": [{"id": 46, "name": "Ada"}]}),
        ("POST", "/api/v1/links", {"id": 3, "person_id": 46, "owner_kind": "task",
                                   "owner_id": 7, "role": "with"}),
        ("GET", "/api/v1/links", {"items": [{"id": 3, "person_id": 46,
                                             "role": "with", "name": "Ada"}]}),
        ("DELETE", "/api/v1/links", {"deleted": True, "id": 3}),
    ])
    srv = FakeServer(routes)
    tool = OutlineUpdateTool(OutlineClient("http://127.0.0.1:8741", "t", srv))
    attached = tool.execute(
        {"kind": "link", "id": 7, "action": "attach", "person": "ada", "to": "task"}
    )
    assert attached.success and "Linked" in attached.output
    post = next(c for c in srv.calls if c[0] == "POST")
    assert post[2] == {"person_id": 46, "owner_kind": "task", "owner_id": 7}
    detached = tool.execute(
        {"kind": "link", "id": 7, "action": "detach", "person": "Ada", "to": "task"}
    )
    assert detached.success and "Unlinked" in detached.output
    assert any(c[0] == "DELETE" and "/api/v1/links/3" in c[1] for c in srv.calls)
    assert not tool.validate_arguments(
        {"kind": "link", "id": 7, "action": "attach", "person": "Ada"}
    )
    assert not tool.validate_arguments(
        {"kind": "link", "id": 7, "action": "attach", "person": "Ada",
         "to": "water"}
    )


def test_link_attach_unknown_person_fails_cleanly():
    tool = OutlineUpdateTool(client())  # no GET /people route -> empty items
    result = tool.execute(
        {"kind": "link", "id": 7, "action": "attach", "person": "Nobody",
         "to": "task"}
    )
    assert not result.success and "create one first" in result.output


def test_search_tag_lists_open_tasks():
    routes = server_routes([
        ("GET", "/api/v1/tasks", {
            "items": [{"id": 12, "title": "drink water", "due_at": None,
                       "status": "open", "tags": ["health"]}],
            "next_before_id": None,
        }),
    ])
    srv = FakeServer(routes)
    result = OutlineSearchTool(
        OutlineClient("http://127.0.0.1:8741", "t", srv)
    ).execute({"tag": "health"})
    assert result.success and "[task#12] drink water #health" in result.output
    assert any("tag=health" in path for _, path, _ in srv.calls)
    tool = OutlineSearchTool(client())
    assert not tool.validate_arguments({"tag": "no spaces!"})
    assert tool.validate_arguments({"tag": "health"})


def test_search_person_detail_with_links():
    routes = server_routes([
        ("GET", "/api/v1/people", {"items": [{"id": 46, "name": "Ada"}]}),
        ("GET", "/api/v1/people/46", {
            "id": 46, "name": "Ada", "phone": "555-0100", "email": "",
            "notes": "likes graphs",
        }),
        ("GET", "/api/v1/people/46/entities", {"items": [
            {"id": 5, "title": "write paper", "kind": "task", "when_ms": None},
        ]}),
    ])
    result = OutlineSearchTool(
        OutlineClient("http://127.0.0.1:8741", "t", FakeServer(routes))
    ).execute({"kind": "person", "query": "Ada"})
    assert result.success
    assert "[person#46] Ada" in result.output
    assert "phone: 555-0100" in result.output
    assert "linked [task#5] write paper" in result.output


def test_search_graph_renders_connections_as_text():
    routes = server_routes([
        ("GET", "/api/v1/people", {"items": [{"id": 46, "name": "Ada"}]}),
        ("GET", "/api/v1/graph", {
            "nodes": [
                {"id": "person:46", "kind": "person", "label": "Ada", "weight": 1},
                {"id": "task:5", "kind": "task", "label": "write paper",
                 "weight": 1},
            ],
            "edges": [{"source": "person:46", "target": "task:5",
                       "role": "with"}],
        }),
    ])
    result = OutlineSearchTool(
        OutlineClient("http://127.0.0.1:8741", "t", FakeServer(routes))
    ).execute({"kind": "graph", "query": "Ada"})
    assert result.success
    assert 'person "Ada" —with→ task "write paper"' in result.output
    assert "busiest" not in result.output  # no marker when the cap never bit
    tool = OutlineSearchTool(client())
    assert not tool.validate_arguments({"kind": "graph"})  # query required


def test_search_graph_admits_a_capped_network():
    routes = server_routes([
        ("GET", "/api/v1/people", {"items": [{"id": 46, "name": "Ada"}]}),
        ("GET", "/api/v1/graph", {
            "nodes": [
                {"id": "person:46", "kind": "person", "label": "Ada", "weight": 1},
                {"id": "task:5", "kind": "task", "label": "write paper",
                 "weight": 1},
            ],
            "edges": [{"source": "person:46", "target": "task:5",
                       "role": "with"}],
            "truncated": True,
        }),
    ])
    result = OutlineSearchTool(
        OutlineClient("http://127.0.0.1:8741", "t", FakeServer(routes))
    ).execute({"kind": "graph", "query": "Ada"})
    assert "busiest connections only" in result.output


def test_new_action_summaries():
    assert outline_tool_summaries(
        "outline_update",
        {"kind": "task", "id": 7, "action": "edit",
         "edits": {"title": "secret value", "tags": ["hidden"]}},
    ) == "edit the Outline task with id 7 (tags, title)"
    assert outline_tool_summaries(
        "outline_update",
        {"kind": "link", "id": 7, "action": "attach", "person": "Ada",
         "to": "task"},
    ) == 'link the person "Ada" to the Outline task with id 7'
    assert outline_tool_summaries(
        "outline_create",
        {"kind": "task", "title": "t", "recurrence": "weekly", "tags": ["a"]},
    ) == 'add a new task to Outline: "t" repeating "weekly" tagged a'


# --------------------------------------------------------------------------
# v1.2 parity: reminders
# --------------------------------------------------------------------------


def test_create_task_remind_reaches_the_api():
    srv = FakeServer(server_routes())
    result = OutlineCreateTool(
        OutlineClient("http://127.0.0.1:8741", "t", srv)
    ).execute(
        {"kind": "task", "title": "station dropoff", "remind": "2026-10-05T09:30+00:00"}
    )
    assert result.success
    post = next(c for c in srv.calls if c[0] == "POST" and c[1] == "/api/v1/tasks")
    assert post[2]["remind_at"] == 1_791_192_600_000  # 2026-10-05T09:30Z


def test_create_event_remind_reaches_the_api():
    srv = FakeServer(server_routes())
    result = OutlineCreateTool(
        OutlineClient("http://127.0.0.1:8741", "t", srv)
    ).execute(
        {"kind": "event", "title": "sync", "due_at": "2026-10-01T09:00+00:00",
         "remind": "2026-10-01T08:45+00:00"}
    )
    assert result.success
    post = next(c for c in srv.calls if c[0] == "POST" and c[1] == "/api/v1/events")
    assert post[2]["remind_at"] == 1_790_844_300_000  # 2026-10-01T06:45Z


def test_create_rejects_bad_remind_and_other_kinds():
    tool = OutlineCreateTool(client())
    assert tool.validate_arguments(
        {"kind": "task", "title": "x", "remind": "2026-10-05T09:30+00:00"}
    )
    assert not tool.validate_arguments({"kind": "task", "title": "x", "remind": "soon"})
    assert not tool.validate_arguments({"kind": "task", "title": "x", "remind": 123})
    # remind is only a task/event affordance
    assert not tool.validate_arguments(
        {"kind": "note", "title": "x", "remind": "2026-10-05T09:30+00:00"}
    )


def test_update_edit_remind_maps_and_clears():
    tool = OutlineUpdateTool(client())
    assert tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "edit",
         "edits": {"remind": "2026-10-05T09:30+00:00"}}
    )
    assert tool.validate_arguments(
        {"kind": "event", "id": 2, "action": "edit", "edits": {"remind": None}}
    )
    assert not tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "edit", "edits": {"remind": "9am"}}
    )
    srv = FakeServer(server_routes())
    result = OutlineUpdateTool(
        OutlineClient("http://127.0.0.1:8741", "t", srv)
    ).execute(
        {"kind": "task", "id": 7, "action": "edit",
         "edits": {"remind": "2026-10-05T09:30+00:00"}}
    )
    assert result.success
    patch = next(c for c in srv.calls if c[0] == "PATCH")
    assert patch[2] == {"remind_at": 1_791_192_600_000}


def test_remind_shown_in_task_lines():
    routes = server_routes([
        ("GET", "/api/v1/tasks", {"items": [
            {"id": 9, "title": "ferry tickets", "due_at": 1_791_192_600_000,
             "remind_at": 1_791_181_000_000},
        ], "next_before_id": None}),
    ])
    result = OutlineSearchTool(
        OutlineClient("http://127.0.0.1:8741", "t", FakeServer(routes))
    ).execute({"kind": "task", "when": "overdue"})
    assert result.success and "remind" in result.output


def test_remind_action_summary():
    assert outline_tool_summaries(
        "outline_update",
        {"kind": "task", "id": 7, "action": "edit",
         "edits": {"remind": "2026-10-05T09:30+00:00"}},
    ) == "edit the Outline task with id 7 (remind)"


# --------------------------------------------------------------------------
# reminders boundary (report 30): remind must never read as Stella's own
# --------------------------------------------------------------------------


def test_remind_field_is_framed_as_outline_app_only():
    client = OutlineClient("http://127.0.0.1:8741", "t", FakeServer([]))
    create = OutlineCreateTool(client)
    update = OutlineUpdateTool(client)
    assert "alert inside the Outline app itself" in create.description
    assert "not Stella's own reminder" in create.description
    schema_field = create.argument_schema["remind"]
    assert "not Stella's own reminders" in schema_field
    assert "alert inside the Outline app (not" in update.description


def test_remind_me_prompt_without_outline_still_validates_the_same():
    # The boundary is wording, not contract: the remind validator keeps
    # its exact shape so nothing about an outline_create call changes.
    client = OutlineClient("http://127.0.0.1:8741", "t", FakeServer([]))
    tool = OutlineCreateTool(client)
    assert tool.validate_arguments(
        {"kind": "task", "title": "t", "remind": "2026-09-28T18:00+05:30"}
    )
    assert not tool.validate_arguments(
        {"kind": "task", "title": "t", "remind": "not-a-time"}
    )


# --------------------------------------------------------------------------
# reminder pump (report 54): Stella delivers due reminders with no browser
# --------------------------------------------------------------------------


def claim_client(routes):
    server = FakeServer(routes)
    return (
        OutlineClient("http://127.0.0.1:8741", "t", server),
        server,
    )


DUE_ITEMS = {
    "items": [
        {"kind": "task", "id": 7, "title": "file taxes", "remind_at": 100},
        {"kind": "event", "id": 11, "title": " design review ", "remind_at": 200},
        {"kind": "note", "id": 9, "title": "wrong kind", "remind_at": 1},
        {"kind": "task", "id": True, "title": "bool id", "remind_at": 1},
        {"kind": "task", "id": 8, "title": "  ", "remind_at": 1},
        {"kind": "task", "id": 9, "title": "bad time", "remind_at": "100"},
        "not-a-mapping",
    ]
}


def test_claim_validates_rows_and_acknowledges_only_the_good_ones():
    client, server = claim_client([
        ("GET", "/api/v1/reminders/due", DUE_ITEMS),
        ("POST", "/api/v1/reminders/fire", {"fired": 2}),
    ])
    due = claim_due_reminders(client)
    assert [(d.kind, d.id, d.title) for d in due] == [
        ("task", 7, "file taxes"),
        ("event", 11, "design review"),
    ]
    fires = [c for c in server.calls if c[0] == "POST"]
    assert len(fires) == 1
    assert fires[0][2] == {"items": [{"kind": "task", "id": 7}, {"kind": "event", "id": 11}]}


def test_claim_skips_the_ack_when_nothing_is_due():
    client, server = claim_client([
        ("GET", "/api/v1/reminders/due", {"items": []}),
    ])
    assert claim_due_reminders(client) == ()
    assert not [c for c in server.calls if c[0] == "POST"]


def test_claim_propagates_a_failed_ack_so_items_stay_due():
    client, _ = claim_client([
        ("GET", "/api/v1/reminders/due", DUE_ITEMS),
        ("POST", "/api/v1/reminders/fire", OutlineError("rejected")),
    ])
    try:
        claim_due_reminders(client)
    except OutlineError:
        pass
    else:
        raise AssertionError("a failed fire must raise, not silently drop")


def test_pump_polls_at_most_once_a_minute_and_backs_off(monkeypatch):
    import stella.outline_tools as module

    probes = []
    claims = []
    monkeypatch.setattr(module, "_healthz", lambda c: probes.append(1) or True)
    monkeypatch.setattr(
        module, "claim_due_reminders", lambda c: claims.append(1) or ()
    )
    pump = module.OutlineReminderPump(OutlineClient("http://x", "t", FakeServer([])))
    assert pump.claim() == ()
    assert pump.claim() == ()  # same minute: no second HTTP cycle
    assert len(probes) == 1 and len(claims) == 1
    # a failing cycle doubles the quiet period
    monkeypatch.setattr(module, "_healthz", lambda c: False)
    assert pump.claim() == ()
    assert len(claims) == 1


def test_pump_backs_off_when_the_claim_fails(monkeypatch):
    import stella.outline_tools as module

    monkeypatch.setattr(module, "_healthz", lambda c: True)
    calls = []

    def boom(client):
        calls.append(1)
        raise OutlineError("server went away mid-poll")

    monkeypatch.setattr(module, "claim_due_reminders", boom)
    pump = module.OutlineReminderPump(OutlineClient("http://x", "t", FakeServer([])))
    assert pump.claim() == ()
    assert pump.claim() == ()
    assert len(calls) == 1


def test_pump_is_armed_only_by_a_real_transport_build(tmp_path, monkeypatch):
    import stella.outline_tools as module

    monkeypatch.setattr(module, "_ACTIVE_REMINDER_PUMP", None)
    data_dir = tmp_path / "outline"
    data_dir.mkdir()
    (data_dir / "outline.token").write_text("secret-token\n")
    env = {"OUTLINE_DATA_DIR": str(data_dir)}
    # fake transport (tests): tools exist, pump stays unarmed
    assert len(build_outline_tools(env, transport=FakeServer(server_routes()))) == 3
    assert module.active_reminder_pump() is None
    # real transport and a reachable server: the sweep is armed
    monkeypatch.setattr(module, "_healthz", lambda c: True)
    assert len(build_outline_tools({**env, "OUTLINE_URL": "http://127.0.0.1:9"})) == 3
    assert module.active_reminder_pump() is not None


# --------------------------------------------------------------------------
# search rendering parity: everything /api/v1/search resolves must survive
# --------------------------------------------------------------------------


def test_search_lines_keep_date_host_and_flattened_snippet():
    search_payload = {"items": [
        {"kind": "event", "id": 21, "title": "dentist",
         "snippet": "", "starts_at": 1_760_000_000_000},
        {"kind": "note", "id": 44, "title": "",
         "snippet": "soil was dry\n  second line padded out with quite a lot of text so that the bound has to bite here here here here here here here here here here here here here here here here here",
         "starts_at": None, "host_kind": "project", "host_id": 5,
         "host_title": "Garden"},
        {"kind": "task", "id": 12, "title": "drink water", "snippet": "",
         "starts_at": None},
    ]}
    tool = OutlineSearchTool(client(("GET", "/api/v1/search", search_payload)))
    result = tool.execute({"query": "x"})
    assert result.success
    lines = result.output.splitlines()[1:]  # skip the untrusted-data header
    assert lines[1].startswith("[note#44] → in [project#5] Garden · soil was dry second line")
    assert "\n" not in lines[1]
    assert len(lines[1].split(" · ")[1]) <= 120
    assert lines[2] == "[task#12] drink water"
    assert lines[0].startswith("[event#21] dentist — ")


def test_today_view_includes_upcoming_events_and_open_count():
    today = {
        "overdue_tasks": [], "today_tasks": [], "events": [],
        "water_total_ml": 0,
        "upcoming_events": [{"id": 31, "title": "lunch with Sam",
                             "starts_at": 1_760_100_000_000}],
        "open_task_count": 2,
    }
    tool = OutlineSearchTool(client(("GET", "/api/v1/today", today)))
    result = tool.execute({"when": "today"})
    assert "open tasks: 2" in result.output
    assert "next up: lunch with Sam" in result.output


def test_today_view_survives_missing_optional_fields():
    routes = [
        ("GET", "/api/v1/today", {"overdue_tasks": [], "today_tasks": [],
                                  "events": [], "water_total_ml": 0}),
    ]
    tool = OutlineSearchTool(client(*routes))
    result = tool.execute({"when": "today"})
    assert "water today: 0 ml" in result.output
    assert "open tasks" not in result.output


def test_today_view_keeps_time_sensitive_sections_under_overdue_flood():
    today = {
        "overdue_tasks": [
            {"id": 100 + i, "title": f"stale {i}", "due_at": 1_759_000_000_000}
            for i in range(30)
        ],
        "today_tasks": [{"id": 7, "title": "standup", "due_at": 2}],
        "events": [{"id": 21, "title": "dentist", "starts_at": 1_760_000_000_000}],
        "water_total_ml": 500,
        "upcoming_events": [
            {"id": 31, "title": "lunch", "starts_at": 1_760_100_000_000},
            {"id": 32, "title": "review", "starts_at": 1_760_200_000_000},
            {"id": 33, "title": "trip", "starts_at": 1_760_300_000_000},
        ],
        "active_timers": [],
        "open_task_count": 40,
    }
    tool = OutlineSearchTool(client(("GET", "/api/v1/today", today)))
    result = tool.execute({"when": "today"})
    lines = result.output.splitlines()[1:]
    assert lines[0] == "water today: 500 ml · open tasks: 40"
    assert any("[task#7] standup" in line for line in lines)
    assert any("next up: lunch" in line for line in lines)
    assert any("next up: review" in line for line in lines)
    assert not any("next up: trip" in line for line in lines)
    assert sum(1 for line in lines if "OVERDUE" in line) == 3
    assert "(+1 more)" in result.output  # upcoming section overflow
    assert "(+27 more)" in result.output  # overdue section overflow
    assert len(lines) <= MAX_OUTPUT_LINES


def test_today_sections_are_unmarked_when_they_fit():
    result = OutlineSearchTool(client()).execute({"when": "today"})
    assert "(+" not in result.output


def test_today_next_up_does_not_repeat_a_scheduled_event():
    later_today = {"id": 6, "title": "midnight snack",
                   "starts_at": 1_760_000_000_000}
    tomorrow = {"id": 9, "title": "futuresite",
                "starts_at": 1_760_300_000_000}
    today = {
        "overdue_tasks": [], "today_tasks": [], "events": [later_today],
        "water_total_ml": 0, "active_timers": [],
        # /today deliberately lists a later-today event in both sets
        "upcoming_events": [later_today, tomorrow],
    }
    tool = OutlineSearchTool(client(("GET", "/api/v1/today", today)))
    result = tool.execute({"when": "today"})
    lines = result.output.splitlines()[1:]
    assert sum("midnight snack" in line for line in lines) == 1
    assert any("next up: futuresite" in line for line in lines)


def test_list_views_admit_when_their_page_was_cut():
    routes = [
        ("GET", "/api/v1/tasks", {"items": [{"id": 1, "title": "a", "due_at": 1}],
                                  "next_before_id": 1}),
        ("GET", "/api/v1/search", {"items": [{"kind": "task", "id": 1, "title": "a",
                                              "snippet": ""}], "has_more": True}),
    ]
    tool = OutlineSearchTool(client(*routes))
    assert "(+ more" in tool.execute({"when": "overdue"}).output
    assert "(+ more" in tool.execute({"tag": "home"}).output
    assert "(+ more" in tool.execute({"query": "a", "kind": "task"}).output


def test_full_pages_carry_no_truncation_marker():
    routes = [
        ("GET", "/api/v1/tasks", {"items": [{"id": 1, "title": "a", "due_at": 1}],
                                  "next_before_id": None}),
        ("GET", "/api/v1/search", {"items": [{"kind": "task", "id": 1, "title": "a",
                                              "snippet": ""}], "has_more": False}),
    ]
    tool = OutlineSearchTool(client(*routes))
    for arguments in ({"when": "overdue"}, {"tag": "home"}, {"query": "a", "kind": "task"}):
        assert "(+" not in tool.execute(arguments).output


def test_task_lines_show_priority():
    routes = [
        ("GET", "/api/v1/tasks", {"items": [
            {"id": 1, "title": "urgent", "priority": 3},
            {"id": 2, "title": "plain", "priority": 0},
            {"id": 3, "title": "weird", "priority": True},
        ], "next_before_id": None}),
    ]
    output = OutlineSearchTool(client(*routes)).execute({"when": "overdue"}).output
    assert "[task#1] urgent !!!" in output
    assert "[task#2] plain" in output and "plain !" not in output
    assert "[task#3] weird" in output and "weird !" not in output


def test_create_task_priority_reaches_the_api_and_bounds_are_rejected():
    srv = FakeServer(server_routes())
    tool = OutlineCreateTool(OutlineClient("http://127.0.0.1:8741", "t", srv))
    assert tool.execute({"kind": "task", "title": "file taxes", "priority": 3}).success
    post = next(c for c in srv.calls if c[0] == "POST" and c[1] == "/api/v1/tasks")
    assert post[2]["priority"] == 3
    base = {"kind": "task", "title": "x"}
    assert tool.validate_arguments({**base, "priority": 0})
    assert not tool.validate_arguments({**base, "priority": 4})
    assert not tool.validate_arguments({**base, "priority": "high"})
    assert not tool.validate_arguments({**base, "priority": True})


def test_update_task_restore_round_trip():
    tool = OutlineUpdateTool(client(
        ("POST", "/api/v1/tasks/7/restore", {"restored": True, "id": 7}),
        ("GET", "/api/v1/tasks/7", {"id": 7, "title": "standup", "status": "open"}),
    ))
    result = tool.execute({"kind": "task", "id": 7, "action": "restore"})
    assert result.success and ": restore" in result.output
    # restore takes no extras
    assert not tool.validate_arguments(
        {"kind": "task", "id": 7, "action": "restore", "edits": {"title": "x"}}
    )


def test_update_event_restore_round_trip():
    tool = OutlineUpdateTool(client(
        ("POST", "/api/v1/events/9/restore", {"restored": True, "id": 9}),
        ("GET", "/api/v1/events/9", {"id": 9, "title": "standup", "starts_at": 5}),
    ))
    result = tool.execute({"kind": "event", "id": 9, "action": "restore"})
    assert result.success and ": restore" in result.output
    assert not tool.validate_arguments(
        {"kind": "event", "id": 9, "action": "restore", "edits": {"title": "x"}}
    )


def test_update_project_restore_round_trip():
    tool = OutlineUpdateTool(client(
        (
            "POST",
            "/api/v1/projects/4/restore",
            {"restored": True, "id": 4, "tasks_restored": 3},
        ),
        ("GET", "/api/v1/projects/4", {"id": 4, "title": "Attic", "status": "active"}),
    ))
    result = tool.execute({"kind": "project", "id": 4, "action": "restore"})
    assert result.success and ": restore" in result.output
    assert not tool.validate_arguments(
        {"kind": "project", "id": 4, "action": "restore", "edits": {"title": "x"}}
    )
