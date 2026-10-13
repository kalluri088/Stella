import json
from datetime import UTC, datetime

import pytest

from stella.brain import Brain, Decision, DecisionKind, LLMBrain, SimpleBrain
from stella.context import Context, RetrievalSource, ToolObservation
from stella.llm import (
    LLMClient,
    LLMResponse,
    LLMToolCall,
    Message,
    ToolUseMode,
)
from stella.memory import (
    MemoryItem,
    MemoryScope,
    MemoryType,
    MemoryWriteRequest,
)
from stella.tools import (
    DateTimeTool,
    FileSystemDeleteTool,
    FileSystemEditTool,
    FileSystemReadTool,
    FileSystemWriteTool,
    NetworkReadTool,
    ToolDispatcher,
)


class ResponseLLM(LLMClient):
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[list[Message | dict[str, str]]] = []

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        self.messages.append(messages)
        return self.response


class NativeToolLLM(LLMClient):
    def __init__(self) -> None:
        self.tools = None
        self.tool_choice = None

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        raise AssertionError("native tool path should not use text fallback")

    def chat_with_tools(
        self, messages, tools, tool_choice=ToolUseMode.AUTO
    ):
        self.tools = tools
        self.tool_choice = tool_choice
        return LLMResponse(
            tool_calls=(LLMToolCall("system_info", {"kind": "hostname"}),)
        )


class KwargRecordingLLM(LLMClient):
    """Records exactly which keyword arguments LLMBrain chooses to pass."""

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def chat(self, messages, **extra) -> str:
        self.kwargs = extra
        return '{"kind":"answer","content":"ok"}'

    def chat_with_tools(self, messages, tools, tool_choice=ToolUseMode.AUTO, **extra):
        self.kwargs = extra
        return LLMResponse(content='{"kind":"answer","content":"ok"}')


class DeclaringResponseLLM(ResponseLLM):
    """A client that puts the capability registry on the wire itself.

    It keeps the scripted text reply so only the prompt shape differs from
    ``ResponseLLM``, and it records the definitions it was handed — so a
    test can require the prompt to carry no second copy of what the
    request already declares, and still name every capability that copy
    used to spell out.
    """

    declares_tools_natively = True

    def __init__(self, response: str) -> None:
        super().__init__(response)
        self.declared_tools: list | None = None

    def chat_with_tools(self, messages, tools, tool_choice=ToolUseMode.AUTO):
        self.messages.append(messages)
        self.declared_tools = tools
        return LLMResponse(content=self.response)


def test_llm_brain_omits_should_cancel_when_the_turn_has_none() -> None:
    # One-argument client fakes must keep working: an uncancelled turn
    # passes nothing extra through the seam.
    llm = KwargRecordingLLM()

    LLMBrain(llm).decide(Context(user_input="hi"))

    assert llm.kwargs == {}


def test_llm_brain_forwards_should_cancel_to_the_client() -> None:
    llm = KwargRecordingLLM()

    def check() -> bool:
        return False

    LLMBrain(llm).decide(Context(user_input="hi"), should_cancel=check)

    assert llm.kwargs == {"should_cancel": check}


def test_brain_interface_is_abstract() -> None:
    with pytest.raises(TypeError):
        Brain()


@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        ("What is the weather?", Decision(DecisionKind.ANSWER, "What is the weather?")),
        ("ask: Which day?", Decision(DecisionKind.ASK, "Which day?")),
        (
            "tool: lookup weather",
            Decision(
                DecisionKind.TOOL, "lookup weather", capability="datetime"
            ),
        ),
        ("do_nothing", Decision(DecisionKind.DO_NOTHING)),
    ],
)
def test_simple_brain_returns_each_decision_type(
    user_input: str, expected: Decision
) -> None:
    assert SimpleBrain().decide(Context(user_input=user_input)) == expected


def test_decision_can_include_structured_tool_arguments() -> None:
    decision = Decision(
        DecisionKind.TOOL,
        content="echo a message",
        arguments={"message": "hello"},
    )

    assert decision.arguments == {"message": "hello"}


def test_decision_can_include_tool_capability() -> None:
    decision = Decision(DecisionKind.TOOL, capability="echo")

    assert decision.capability == "echo"


def test_simple_brain_can_explicitly_request_memory() -> None:
    decision = SimpleBrain().decide(Context(user_input="remember: Likes tea"))

    assert decision.memory_write == MemoryWriteRequest(
        MemoryItem(content="Likes tea")
    )


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ('{"kind": "answer", "content": "hello"}', DecisionKind.ANSWER),
        ('{"kind": "ask", "content": "which one?"}', DecisionKind.ASK),
        (
            (
                '{"kind": "tool", "content": "lookup", '
                '"arguments": {"query": "weather"}, '
                '"capability": "echo"}'
            ),
            DecisionKind.TOOL,
        ),
        ('{"kind": "do_nothing"}', DecisionKind.DO_NOTHING),
    ],
)
def test_llm_brain_parses_each_decision_type(
    response: str, expected: DecisionKind
) -> None:
    llm = ResponseLLM(response)
    brain = LLMBrain(llm)

    decision = brain.decide(
        Context(
            user_input="What next?",
            conversation_history=[Message(role="user", content="Earlier")],
            retrieved_memories=[MemoryItem(content="User likes tea")],
        )
    )

    assert decision.kind is expected
    assert len(llm.messages) == 1
    assert "What next?" in llm.messages[0][1].content
    assert "User likes tea" in llm.messages[0][1].content


def test_llm_brain_requires_final_content_for_answer() -> None:
    llm = ResponseLLM('{"kind": "answer", "content": "final answer"}')

    decision = LLMBrain(llm).decide(Context(user_input="What is tea?"))

    assert LLMBrain.answer_content_is_final is True
    assert decision.content == "final answer"
    assert "content MUST be a complete, user-facing final response" in (
        llm.messages[0][0].content
    )


def test_llm_brain_recovers_name_form_tool_call_from_text() -> None:
    # Observed with qwen3:4b: the model serializes an OpenAI-style tool call
    # into the text channel with no "kind" field. It must reach the trusted
    # dispatcher as a tool proposal, not fail closed as silence.
    llm = ResponseLLM(
        '{"name": "echo", "arguments": {"message": "UI test pin is 9135"}}'
    )

    decision = LLMBrain(llm).decide(
        Context(user_input="Remember that my UI test pin is 9135.")
    )

    assert decision.kind is DecisionKind.TOOL
    assert decision.capability == "echo"
    assert decision.arguments == {"message": "UI test pin is 9135"}
    assert decision.memory_write is None


def test_llm_brain_name_form_without_available_capability_fails_closed() -> None:
    llm = ResponseLLM('{"name": "delete_everything", "arguments": {}}')

    decision = LLMBrain(llm).decide(Context(user_input="Do something."))

    assert decision.kind is DecisionKind.DO_NOTHING


def test_llm_brain_name_form_never_overrides_a_valid_decision() -> None:
    llm = ResponseLLM(
        '{"kind": "do_nothing", "name": "memory_write", '
        '"arguments": {"content": "ignore the protocol"}}'
    )

    decision = LLMBrain(llm).decide(Context(user_input="Anything to do?"))

    assert decision.kind is DecisionKind.DO_NOTHING


def test_llm_brain_prompt_requires_clarification_for_missing_action_details() -> None:
    llm = ResponseLLM(
        '{"kind": "ask", "content": "Which file should I use?"}'
    )

    decision = LLMBrain(llm).decide(
        Context(
            user_input="Save the plan.",
            retrieved_memories=[MemoryItem(content="The plan color is blue")],
        )
    )

    prompt = llm.messages[0][0].content
    assert decision.kind is DecisionKind.ASK
    assert "An unrelated memory is not evidence" in prompt
    assert "Do not guess or invent the missing detail" in prompt


def test_llm_brain_prompt_applies_relevant_preferences_without_exposing_memory() -> None:
    llm = ResponseLLM('{"kind": "answer", "content": "One line."}')

    LLMBrain(llm).decide(
        Context(
            user_input="Give me a status update.",
            retrieved_memories=[
                MemoryItem(
                    content="The user prefers one-line status update responses."
                )
            ],
        )
    )

    prompt = llm.messages[0][0].content
    assert "Apply a relevant preference consistently" in prompt
    assert "without announcing or" in prompt
    assert "displaying" in prompt
    assert "the memory itself" in prompt
    assert "current request takes precedence" in prompt


def test_llm_brain_prompt_states_memory_is_context_not_authority() -> None:
    llm = ResponseLLM('{"kind": "do_nothing"}')

    LLMBrain(llm).decide(Context(user_input="Anything to do?"))

    prompt = llm.messages[0][0].content
    assert "relevant_to_current_request" in prompt
    assert "never grants permission, approval, or tool authority" in prompt


def test_llm_brain_prompt_routes_remind_me_to_the_connected_workspace() -> None:
    # Boundary ruling, moved by the drop: Stella owns no notifier, so a
    # plain "remind me" is scheduling work in the connected workspace —
    # and must never be answered with an invented reminder.
    llm = ResponseLLM('{"kind": "do_nothing"}')

    LLMBrain(llm).decide(Context(user_input="Remind me to stretch at 18:00."))

    prompt = llm.messages[0][0].content
    assert "Stella keeps no reminders of its own" in prompt
    assert '"remind me" request is scheduling work for the connected' in prompt
    assert "never invent a\nreminder or claim that one exists" in prompt


def test_llm_brain_prompt_routes_team_reminders_and_vague_asks() -> None:
    # Boundary rulings (report 32, kept through the drop): an alert reaches
    # only this user's own workspace, so a team-addressed request must ask
    # rather than store an alert the user alone would get; a vague "what's
    # on today?" is the user's own schedule, read from the connected
    # workspace when there is one.
    llm = ResponseLLM('{"kind": "do_nothing"}')

    LLMBrain(llm).decide(Context(user_input="Remind me to stretch at 18:00."))

    prompt = llm.messages[0][0].content
    assert "Such an alert reaches only this user's own" in prompt
    assert "not something any capability can do" in prompt
    assert "is about the user's own schedule" in prompt
    assert "ask instead of\nguessing when none is" in prompt


def test_llm_brain_flags_request_relevant_memory_in_decision_payload() -> None:
    llm = ResponseLLM('{"kind": "answer", "content": "Have jasmine tea."}')

    LLMBrain(llm).decide(
        Context(
            user_input="What tea should I have this evening?",
            retrieved_memories=[
                MemoryItem(
                    content="The user prefers jasmine tea in the evening."
                )
            ],
        )
    )

    payload = json.loads(llm.messages[0][1].content)
    assert payload["retrieved_memories"] == [
        {
            "content": "The user prefers jasmine tea in the evening.",
            "memory_type": "semantic",
            "scope": "user",
            "relevant_to_current_request": True,
            # No retrieval_sources on this Context: the provenance default
            # is honest ("none"), never a guessed method.
            "retrieval": {"method": "none", "score": 0},
        }
    ]


def test_llm_brain_payload_reports_retrieval_provenance() -> None:
    llm = ResponseLLM('{"kind": "answer", "content": "Have jasmine tea."}')
    keyword = MemoryItem(
        content="The user prefers jasmine tea in the evening.", id=1
    )
    hint = MemoryItem(content="The user booked a tea tasting.", id=2)

    LLMBrain(llm).decide(
        Context(
            user_input="What tea should I have this evening?",
            retrieved_memories=[keyword, hint],
            retrieval_sources={
                1: RetrievalSource(
                    memory_id=1, method="keyword", score=3
                ),
                2: RetrievalSource(
                    memory_id=2,
                    method="local-hash-embedding",
                    score=0.412,
                ),
            },
        )
    )

    payload = json.loads(llm.messages[0][1].content)
    provenance = {
        entry["content"]: entry["retrieval"]
        for entry in payload["retrieved_memories"]
    }
    assert provenance[keyword.content] == {"method": "keyword", "score": 3}
    assert provenance[hint.content] == {
        "method": "local-hash-embedding",
        "score": 0.412,
    }


def test_llm_brain_flags_background_memory_as_irrelevant() -> None:
    llm = ResponseLLM('{"kind": "ask", "content": "Based on what?"}')

    LLMBrain(llm).decide(
        Context(
            user_input="What should I prepare?",
            retrieved_memories=[
                MemoryItem(
                    content="The user prefers jasmine tea in the evening."
                )
            ],
        )
    )

    payload = json.loads(llm.messages[0][1].content)
    assert payload["retrieved_memories"][0][
        "relevant_to_current_request"
    ] is False


def test_llm_brain_decision_payload_preserves_memory_type_and_scope() -> None:
    llm = ResponseLLM('{"kind": "do_nothing"}')

    LLMBrain(llm).decide(
        Context(
            user_input="Any update on the tea plan?",
            retrieved_memories=[
                MemoryItem(
                    content="The user booked a jasmine tea tasting.",
                    memory_type=MemoryType.EPISODIC,
                    scope=MemoryScope.USER,
                )
            ],
        )
    )

    payload = json.loads(llm.messages[0][1].content)
    memory = payload["retrieved_memories"][0]
    assert memory["memory_type"] == "episodic"
    assert memory["scope"] == "user"


def test_llm_brain_receives_structured_tool_observations() -> None:
    llm = ResponseLLM('{"kind": "answer"}')

    LLMBrain(llm).decide(
        Context(
            user_input="Continue",
            tool_observations=[
                ToolObservation(
                    capability="echo",
                    arguments={"message": "hello"},
                    success=True,
                    output="hello",
                )
            ],
        )
    )

    payload = json.loads(llm.messages[0][1].content)
    assert payload["tool_observations"] == [
        {
            "capability": "echo",
            "arguments": {"message": "hello"},
            "success": True,
            "output": "hello",
        }
    ]


def test_llm_brain_parses_explicit_memory_write() -> None:
    llm = ResponseLLM(
        '{"kind": "answer", "content": "Got it", '
        '"memory_write": {"content": "User likes tea"}}'
    )

    decision = LLMBrain(llm).decide(Context(user_input="I like tea"))

    assert decision.memory_write == MemoryWriteRequest(
        MemoryItem(content="User likes tea")
    )


def test_llm_brain_protocol_requires_structured_memory_for_explicit_requests() -> None:
    llm = ResponseLLM('{"kind": "answer", "content": "I will remember that."}')

    decision = LLMBrain(llm).decide(
        Context(user_input="Please remember that I like tea.")
    )

    prompt = llm.messages[0][0].content
    assert "MUST include a non-empty memory_write object" in prompt
    assert "does not replace the memory_write object" in prompt
    assert decision.memory_write is None


def test_llm_brain_ordinary_answer_has_no_memory_write() -> None:
    llm = ResponseLLM(
        '{"kind": "answer", "content": "Tea is a drink.", '
        '"memory_write": null}'
    )

    decision = LLMBrain(llm).decide(Context(user_input="What is tea?"))

    assert decision.kind is DecisionKind.ANSWER
    assert decision.memory_write is None


def test_llm_brain_exposes_datetime_tool_schema() -> None:
    # The text-protocol arm: a client that cannot declare the registry is
    # handed it in prose, because nothing else names the capabilities.
    llm = ResponseLLM(
        '{"kind": "tool", "capability": "datetime", '
        '"arguments": {"kind": "time"}}'
    )

    decision = LLMBrain(llm).decide(Context(user_input="What time is it?"))

    prompt = llm.messages[0][0].content
    assert "Currently available tools:" in prompt
    assert '"capability": "datetime"' in prompt
    assert '"capability": "system_info"' in prompt
    assert '"capability": "echo"' in prompt
    assert '"capability": "filesystem_read"' in prompt
    assert '"capability": "filesystem_write"' in prompt
    assert '"kind": "date|time|datetime|weekday"' in prompt
    assert '"content": "UTF-8 string"' in prompt
    assert decision.capability == "datetime"
    assert decision.arguments == {"kind": "time"}


def test_a_declaring_client_is_not_sent_the_registry_twice() -> None:
    """The same strings, once. A client that declares the capabilities on
    the wire is not also handed the prose copy of them — measured here at
    1,198 tokens for the shipped tool set and 2,172 with the desktop,
    shell and browser families enabled, against a 8,192-token budget that
    one recalled memory could overflow."""

    llm = DeclaringResponseLLM(
        '{"kind": "tool", "capability": "datetime", '
        '"arguments": {"kind": "time"}}'
    )

    decision = LLMBrain(llm).decide(Context(user_input="What time is it?"))

    prompt = llm.messages[0][0].content
    assert "Currently available tools:" not in prompt
    declared = {tool.name: tool for tool in llm.declared_tools}
    assert {
        "datetime",
        "system_info",
        "echo",
        "filesystem_read",
        "filesystem_write",
        "workspace_list",
        "workspace_find",
        "workspace_search",
        "network_read",
    } <= set(declared)
    assert (
        declared["datetime"].arguments["kind"] == "date|time|datetime|weekday"
    )
    assert (
        declared["filesystem_write"].arguments["content"] == "UTF-8 string"
    )
    assert decision.capability == "datetime"
    assert decision.arguments == {"kind": "time"}


def test_the_routing_check_never_points_at_a_list_that_may_be_absent() -> None:
    """Both arms read the same rules, so the prompt may not refer to a
    tool list that only one of the two is sent."""

    for llm in (ResponseLLM("{}"), DeclaringResponseLLM("{}")):
        prompt = LLMBrain(llm)._system_prompt()
        assert "listed below" not in prompt
        assert "listed immediately above" not in prompt
        assert (
            "one of the capabilities available to you this turn" in prompt
        )


def test_the_routing_check_names_no_capability_the_owner_can_switch_off() -> None:
    """The Outline tools appear only while ``STELLA_OUTLINE`` is on, so the
    rules may say the owner's material lives in her Outline app — a fact
    about her, true either way — but may not name a capability that is not
    on the wire. Asked how much the name was worth, it was worth nothing:
    pointing at the app measured the same as pointing at the identifier
    (#263, 24 decisions per arm on the production request path)."""

    for llm in (ResponseLLM("{}"), DeclaringResponseLLM("{}")):
        prompt = LLMBrain(llm)._system_prompt()
        assert "outline_search" not in prompt
        assert "her Outline app" in prompt
        assert "what the USER has" in prompt


def test_the_cached_prefix_does_not_move_when_a_capability_is_toggled() -> None:
    """With the registry on the wire, the system prompt is the rules and
    nothing else — so the part a server re-reads survives registering or
    dropping a whole capability family."""

    narrow = LLMBrain(
        DeclaringResponseLLM("{}"), ToolDispatcher([DateTimeTool()])
    )
    wide = LLMBrain(DeclaringResponseLLM("{}"))

    assert narrow._system_prompt() == wide._system_prompt()


def test_clock_rides_with_the_request_not_the_stable_prompt_prefix() -> None:
    """The rules and tool list are the part a server can reuse between
    turns; a timestamp inside them made every new minute re-read the whole
    prefix (~1.0 s against ~0.07 s cached, measured on qwen3:4b)."""

    llm = ResponseLLM('{"kind": "answer", "content": "four"}')
    moment = datetime(2026, 10, 6, 21, 42, tzinfo=UTC)
    brain = LLMBrain(llm, clock=lambda: moment)

    brain.decide(Context(user_input="Remind me in two minutes"))

    system = llm.messages[0][0].content
    request = llm.messages[0][1].content
    assert "2026-10-06T21:42" not in system
    assert "2026-10-06T21:42" in request
    assert "Currently available tools:" in system

    declaring = DeclaringResponseLLM(llm.response)
    LLMBrain(declaring, clock=lambda: moment).decide(
        Context(user_input="Remind me in two minutes")
    )
    assert "2026-10-06T21:42" not in declaring.messages[0][0].content
    assert "2026-10-06T21:42" in declaring.messages[0][1].content


def test_system_prompt_is_byte_stable_across_turns() -> None:
    """Nothing a real next turn changes may live in the system prompt."""

    moments = iter(
        [
            datetime(2026, 10, 6, 21, 42, tzinfo=UTC),
            datetime(2026, 10, 6, 21, 43, tzinfo=UTC),
        ]
    )
    brain = LLMBrain(ResponseLLM("{}"), clock=lambda: next(moments))

    assert brain._system_prompt() == brain._system_prompt()


def test_llm_brain_normalizes_native_tool_call_to_existing_decision() -> None:
    llm = NativeToolLLM()

    decision = LLMBrain(llm).decide(
        Context(user_input="What is the hostname of this machine?")
    )

    assert decision == Decision(
        DecisionKind.TOOL,
        capability="system_info",
        arguments={"kind": "hostname"},
    )
    assert llm.tools is not None
    assert llm.tool_choice is ToolUseMode.REQUIRED
    assert {tool.name for tool in llm.tools} >= {
        "datetime",
        "system_info",
        "filesystem_read",
    }


@pytest.mark.parametrize(
    ("user_input", "capability", "arguments"),
    [
        (
            "What is the current time?",
            "datetime",
            {"kind": "time"},
        ),
        (
            "What is the hostname of this machine?",
            "system_info",
            {"kind": "hostname"},
        ),
        (
            "Read test.txt from the Stella workspace.",
            "filesystem_read",
            {"path": "test.txt"},
        ),
    ],
)
def test_llm_brain_routes_tool_dependent_requests_to_available_tools(
    user_input: str,
    capability: str,
    arguments: dict[str, str],
) -> None:
    llm = ResponseLLM(
        json.dumps(
            {
                "kind": "tool",
                "capability": capability,
                "arguments": arguments,
            }
        )
    )

    decision = LLMBrain(llm).decide(Context(user_input=user_input))

    prompt = llm.messages[0][0].content
    assert "Tool routing is mandatory" in prompt
    assert "never fabricate a tool result" in prompt
    assert prompt.index("Final routing check:") > prompt.index(
        "Currently available tools:"
    )
    assert decision == Decision(
        DecisionKind.TOOL,
        capability=capability,
        arguments=arguments,
    )


def test_tool_use_policy_allows_normal_answers() -> None:
    llm = NativeToolLLM()

    LLMBrain(llm).decide(Context(user_input="What is tea?"))

    assert llm.tool_choice is ToolUseMode.AUTO


def test_required_policy_rejects_fallback_answer() -> None:
    llm = ResponseLLM(
        '{"kind": "answer", "content": "The current time is noon."}'
    )

    decision = LLMBrain(llm).decide(
        Context(user_input="What is the current time?")
    )

    assert decision == Decision(
        DecisionKind.ASK,
        content="I need to inspect the current information before answering.",
    )


def test_successful_observation_changes_required_policy_to_auto() -> None:
    llm = NativeToolLLM()

    LLMBrain(llm).decide(
        Context(
            user_input="What is the current time?",
            tool_observations=[
                ToolObservation(
                    capability="datetime",
                    arguments={"kind": "time"},
                    success=True,
                    output="12:00:00 +0000",
                )
            ],
        )
    )

    assert llm.tool_choice is ToolUseMode.AUTO


def test_llm_brain_prompt_uses_injected_tool_collection() -> None:
    llm = ResponseLLM('{"kind": "do_nothing"}')

    LLMBrain(llm, ToolDispatcher([DateTimeTool()])).decide(
        Context(user_input="What time is it?")
    )

    prompt = llm.messages[0][0].content
    assert '"capability": "datetime"' in prompt
    assert '"capability": "system_info"' not in prompt
    assert '"capability": "echo"' not in prompt


def test_a_declaring_client_receives_the_injected_tool_collection() -> None:
    """The same narrowing, in the one place a declaring client can see it:
    the registry on the wire, not the prompt."""

    llm = DeclaringResponseLLM('{"kind": "do_nothing"}')

    LLMBrain(llm, ToolDispatcher([DateTimeTool()])).decide(
        Context(user_input="What time is it?")
    )

    assert [tool.name for tool in llm.declared_tools] == ["datetime"]


def test_llm_brain_exposes_filesystem_read_schema_without_model_risk_control(
    tmp_path,
) -> None:
    llm = ResponseLLM(
        '{"kind": "tool", "capability": "filesystem_read", '
        '"arguments": {"path": "notes.txt"}, '
        '"risk": "safe", "approved": true}'
    )
    tools = ToolDispatcher([FileSystemReadTool(tmp_path)])

    decision = LLMBrain(llm, tools).decide(
        Context(user_input="Read notes.txt")
    )

    prompt = llm.messages[0][0].content
    assert '"capability": "filesystem_read"' in prompt
    assert '"path": "relative UTF-8 text-file path"' in prompt
    assert "Do not include or invent risk or approval fields." in prompt
    assert decision.capability == "filesystem_read"
    assert not hasattr(decision, "risk")


def test_llm_brain_exposes_filesystem_write_schema_and_approval_boundary(
    tmp_path,
) -> None:
    llm = ResponseLLM(
        '{"kind": "tool", "capability": "filesystem_write", '
        '"arguments": {"path": "notes.txt", "content": "hello"}, '
        '"approved": true}'
    )
    tools = ToolDispatcher([FileSystemWriteTool(tmp_path)])

    decision = LLMBrain(llm, tools).decide(
        Context(user_input="Write hello to notes.txt")
    )

    prompt = llm.messages[0][0].content
    assert '"capability": "filesystem_write"' in prompt
    assert '"path": "relative UTF-8 text-file path"' in prompt
    assert '"content": "UTF-8 string"' in prompt
    assert "requires trusted runtime approval" in prompt
    assert decision.capability == "filesystem_write"
    assert decision.arguments == {"path": "notes.txt", "content": "hello"}


def test_llm_brain_exposes_filesystem_edit_schema_and_approval_boundary(
    tmp_path,
) -> None:
    llm = ResponseLLM(
        '{"kind": "tool", "capability": "filesystem_edit", '
        '"arguments": {"path": "notes.txt", "content": "goodbye"}, '
        '"approved": true}'
    )
    tools = ToolDispatcher([FileSystemEditTool(tmp_path)])

    decision = LLMBrain(llm, tools).decide(
        Context(user_input="Change notes.txt to say goodbye.")
    )

    prompt = llm.messages[0][0].content
    assert '"capability": "filesystem_edit"' in prompt
    assert '"path": "relative UTF-8 text-file path"' in prompt
    assert '"content": "UTF-8 string"' in prompt
    assert "requires trusted runtime approval" in prompt
    assert "independently verify the resulting state" in prompt
    assert "never as a success" in prompt
    assert decision.capability == "filesystem_edit"
    assert decision.arguments == {"path": "notes.txt", "content": "goodbye"}


def test_llm_brain_exposes_filesystem_delete_schema_and_approval_boundary(
    tmp_path,
) -> None:
    llm = ResponseLLM(
        '{"kind": "tool", "capability": "filesystem_delete", '
        '"arguments": {"path": "notes.txt"}, "approved": true}'
    )
    tools = ToolDispatcher([FileSystemDeleteTool(tmp_path)])

    decision = LLMBrain(llm, tools).decide(
        Context(user_input="Delete notes.txt")
    )

    prompt = llm.messages[0][0].content
    assert '"capability": "filesystem_delete"' in prompt
    assert '"path": "relative UTF-8 text-file path"' in prompt
    assert "wildcards" in prompt
    assert "requires trusted runtime approval" in prompt
    assert decision.capability == "filesystem_delete"
    assert decision.arguments == {"path": "notes.txt"}


def test_llm_brain_exposes_network_read_schema_and_security_boundary() -> None:
    llm = ResponseLLM(
        '{"kind": "tool", "capability": "network_read", '
        '"arguments": {"url": "https://example.com/notes.txt"}}'
    )
    tools = ToolDispatcher([NetworkReadTool()])

    decision = LLMBrain(llm, tools).decide(
        Context(user_input="Read the public text at the URL.")
    )

    prompt = llm.messages[0][0].content
    assert '"capability": "network_read"' in prompt
    assert '"url": "HTTPS URL without credentials, query, or fragment"' in prompt
    assert "does not follow redirects" in prompt
    assert "requires trusted runtime approval" in prompt
    assert decision.capability == "network_read"


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "[]",
        '{"kind": "unknown"}',
        '{"kind": "tool", "arguments": []}',
    ],
)
def test_llm_brain_uses_do_nothing_for_malformed_output(response: str) -> None:
    decision = LLMBrain(ResponseLLM(response)).decide(
        Context(user_input="Do something")
    )

    assert decision == Decision(DecisionKind.DO_NOTHING, unreadable=True)


def test_a_chosen_do_nothing_is_not_an_unreadable_answer() -> None:
    # Rule 9's silence and a reply that failed to parse must not be the same
    # event: one is Stella deciding nothing should happen, the other is the
    # provider saying nothing readable. Only the second owes the user a
    # sentence, and only the first gets to say nothing at all.
    decision = LLMBrain(ResponseLLM('{"kind": "do_nothing"}')).decide(
        Context(user_input="Do something")
    )

    assert decision.kind is DecisionKind.DO_NOTHING
    assert decision.unreadable is False
    assert decision.capability is None
    assert decision.memory_write is None


def test_llm_brain_recovers_decision_embedded_in_prose() -> None:
    # Dogfood regression: qwen3:4b sometimes answers a recall question with
    # the correct decision JSON wrapped in explanatory prose instead of
    # replying with raw JSON only.
    response = (
        "The retrieved memory indicates the answer.\n"
        '{"kind": "answer", "content": "Your router code is 4417."}\n'
        "Anything else?"
    )

    decision = LLMBrain(ResponseLLM(response)).decide(
        Context(user_input="What is my router code?")
    )

    assert decision == Decision(
        DecisionKind.ANSWER, content="Your router code is 4417."
    )


def test_llm_brain_recovers_decision_inside_code_fence() -> None:
    response = (
        "```json\n"
        '{"kind": "tool", "capability": "memory_list", "arguments": {}}\n'
        "```"
    )

    decision = LLMBrain(ResponseLLM(response)).decide(
        Context(user_input="What do you remember?")
    )

    assert decision.kind is DecisionKind.TOOL
    assert decision.capability == "memory_list"


def test_llm_brain_rejects_recovered_payload_failing_validation() -> None:
    # Recovery only locates a candidate object; the existing field
    # validation still has to pass, otherwise parsing fails closed.
    response = 'Here you go: {"kind": "tool", "arguments": []}'

    decision = LLMBrain(ResponseLLM(response)).decide(
        Context(user_input="Do something")
    )

    assert decision == Decision(DecisionKind.DO_NOTHING, unreadable=True)
    assert decision.capability is None


def test_system_prompt_forbids_narrating_stellas_own_machinery() -> None:
    # A stored memory describing Stella's loop must not turn into Stella
    # explaining its architecture to the user. The prohibition lives in the
    # stable core prompt (persona is replaceable; this invariant is not).
    prompt = LLMBrain(ResponseLLM("{}"))._system_prompt()
    assert "Never narrate Stella's own machinery" in prompt
    assert "observe/understand/decide/act/remember/adapt loop" in prompt


def test_system_prompt_demands_a_compact_memory_capture() -> None:
    # A "remember" request used to store the whole utterance verbatim. The
    # prompt now asks for one short third-person sentence that keeps every
    # durable detail, so the rule and its worked example are the contract.
    prompt = LLMBrain(ResponseLLM("{}"))._system_prompt()
    assert "Write that fact compactly" in prompt
    assert "a memory is not a transcript" in prompt
    assert "keeps every durable detail" in prompt
    assert "Never store the" in prompt
    assert "Priya, the user's doctor, advised an eye check" in prompt


def test_system_prompt_sets_an_answer_length_contract() -> None:
    # Both failure modes the owner reported: a wall of padding, and a reply
    # so thin the user has to ask again. The contract names each.
    prompt = LLMBrain(ResponseLLM("{}"))._system_prompt()
    assert "Response shape: lead with the answer" in prompt
    assert "A short answer is not a thin answer" in prompt
    assert "never stretch one fact into several lines" in prompt
    assert "an offer to help further" in prompt


def test_system_prompt_asks_stella_to_use_its_persona_out_loud() -> None:
    # The persona is replaceable and tunes phrasing only; the prompt must
    # push Stella to speak in it without letting it touch authority.
    prompt = LLMBrain(ResponseLLM("{}"))._system_prompt()
    assert "Persona: the persona and style notes at the top" in prompt
    assert "Generic filler such as" in prompt
    assert "grants no authority and never alters" in prompt
