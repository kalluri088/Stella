import json

import pytest

from stella.brain import Brain, Decision, DecisionKind, LLMBrain, SimpleBrain
from stella.context import Context, ToolObservation
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
        }
    ]


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

    assert decision == Decision(DecisionKind.DO_NOTHING)
