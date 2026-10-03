"""Tests for the opt-in native /api/chat mode of OllamaLLMClient."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stella.llm import (
    LLMToolDefinition,
    Message,
    ProviderRequestCancelled,
)
from stella.ollama_client import (
    NATIVE_REQUEST_TIMEOUT_SECONDS,
    OllamaLLMClient,
    native_chat_url,
)


class FakeSocket:
    """Records the timeout switches the cancel-aware read loop makes."""

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)


class FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body


class FakeConnection:
    """One captured http.client connection replaying a canned /api/chat."""

    def __init__(
        self,
        host: str,
        port: int | None,
        timeout: float | None = None,
        *,
        body: bytes = b"{}",
        status: int = 200,
        wait_timeouts: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.body = body
        self.status = status
        # Number of header-read timeouts to simulate before the reply.
        self._wait_timeouts = wait_timeouts
        self.getresponse_calls = 0
        self.sent: list[SimpleNamespace] = []
        self.closed = 0
        self.sock = FakeSocket()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.sent.append(
            SimpleNamespace(
                method=method, path=path, body=body, headers=headers
            )
        )

    def getresponse(self) -> FakeHTTPResponse:
        self.getresponse_calls += 1
        if self.getresponse_calls <= self._wait_timeouts:
            raise TimeoutError("timed out waiting for the server")
        return FakeHTTPResponse(self.body, status=self.status)

    def close(self) -> None:
        self.closed += 1


class ConnectionRecorder:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []

    def install(
        self,
        message: dict,
        *,
        status: int = 200,
        wait_timeouts: int = 0,
    ) -> None:
        body = json.dumps({"message": message}).encode("utf-8")

        def factory(host, port=None, timeout=None, **_kwargs):
            connection = FakeConnection(
                host,
                port,
                timeout=timeout,
                body=body,
                status=status,
                wait_timeouts=wait_timeouts,
            )
            self.connections.append(connection)
            return connection

        self._patchers = [
            patch("stella.ollama_client.http.client.HTTPConnection", factory),
            patch(
                "stella.ollama_client.http.client.HTTPSConnection", factory
            ),
        ]
        for patcher in self._patchers:
            patcher.start()

    def stop(self) -> None:
        for patcher in self._patchers:
            patcher.stop()


@pytest.fixture
def connections():
    """Fake http.client transports for Ollama's native /api/chat."""

    recorder = ConnectionRecorder()
    try:
        yield recorder
    finally:
        recorder.stop()


def sent_payload(connection: FakeConnection) -> dict:
    return json.loads(connection.sent[-1].body.decode("utf-8"))


def make_client(**kwargs) -> OllamaLLMClient:
    with patch("stella.openai_client.OpenAI"):
        return OllamaLLMClient(model="qwen3:4b", **kwargs)


def test_native_chat_url_strips_compatibility_suffix() -> None:
    assert (
        native_chat_url("http://127.0.0.1:11434/v1")
        == "http://127.0.0.1:11434/api/chat"
    )
    assert (
        native_chat_url("http://host:11434/v1/")
        == "http://host:11434/api/chat"
    )


def test_native_chat_sends_num_ctx_option(connections) -> None:
    connections.install({"role": "assistant", "content": "4", "tool_calls": []})
    client = make_client(native=True, num_ctx=4096)

    result = client.chat([Message(role="user", content="What is 2+2?")])

    assert result == "4"
    connection = connections.connections[-1]
    assert (connection.host, connection.port) == ("127.0.0.1", 11434)
    assert connection.sent[-1].method == "POST"
    assert connection.sent[-1].path == "/api/chat"
    assert connection.timeout == NATIVE_REQUEST_TIMEOUT_SECONDS
    payload = sent_payload(connection)
    assert payload["options"] == {"num_ctx": 4096}
    assert payload["stream"] is False
    assert payload["messages"] == [
        {"role": "user", "content": "What is 2+2?"}
    ]


def test_native_chat_with_tools_sends_num_ctx_and_parses_call(
    connections,
) -> None:
    connections.install(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "system_info",
                        "arguments": {"kind": "hostname"},
                    }
                }
            ],
        }
    )
    client = make_client(native=True, num_ctx=4096)

    result = client.chat_with_tools(
        [Message(role="user", content="hostname?")],
        [
            LLMToolDefinition(
                name="system_info",
                description="Reads host information.",
                arguments={"kind": "hostname|platform|cpu"},
            )
        ],
    )

    payload = sent_payload(connections.connections[-1])
    assert payload["options"] == {"num_ctx": 4096}
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "system_info"
    assert result.content is None
    assert result.tool_calls[0].name == "system_info"
    assert result.tool_calls[0].arguments == {"kind": "hostname"}


def test_native_tool_arguments_may_arrive_as_json_string(
    connections,
) -> None:
    connections.install(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "echo",
                        "arguments": '{"text":"hi"}',
                    }
                }
            ],
        }
    )
    client = make_client(native=True, num_ctx=4096)

    result = client.chat_with_tools(
        [], [LLMToolDefinition(name="echo", description="", arguments={})]
    )

    assert result.tool_calls[0].arguments == {"text": "hi"}


def test_native_malformed_tool_arguments_fail_closed(
    connections,
) -> None:
    connections.install(
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "echo", "arguments": "not-json"}}
            ],
        }
    )
    client = make_client(native=True)

    result = client.chat_with_tools(
        [], [LLMToolDefinition(name="echo", description="", arguments={})]
    )

    assert result.tool_calls[0].arguments == {}


def test_native_omits_options_without_num_ctx(connections) -> None:
    connections.install({"role": "assistant", "content": "ok"})
    client = make_client(native=True)

    client.chat([Message(role="user", content="hi")])

    payload = sent_payload(connections.connections[-1])
    assert "options" not in payload


def test_native_error_status_raises(connections) -> None:
    connections.install({"role": "assistant", "content": "x"}, status=500)
    client = make_client(native=True)

    with pytest.raises(OSError, match="HTTP 500"):
        client.chat([Message(role="user", content="hi")])

    # The failed exchange still closes its connection.
    assert connections.connections[-1].closed == 1


def test_native_cancel_while_waiting_aborts_the_wait(connections) -> None:
    connections.install(
        {"role": "assistant", "content": "never delivered"},
        wait_timeouts=1,
    )
    client = make_client(native=True)
    cancels = iter([True])

    with pytest.raises(ProviderRequestCancelled):
        client.chat(
            [Message(role="user", content="hi")],
            should_cancel=lambda: next(cancels, True),
        )

    connection = connections.connections[-1]
    # One simulated header timeout, then the cancel check fired: the
    # wait was abandoned without ever reading the eventual reply.
    assert connection.getresponse_calls == 1
    assert connection.closed == 1
    # Poll timeout was installed for the header wait and the long request
    # timeout restored afterwards — cancellation never leaves a socket on
    # a fast-timeout setting.
    assert connection.sock.timeouts[0] < NATIVE_REQUEST_TIMEOUT_SECONDS
    assert connection.sock.timeouts[-1] == NATIVE_REQUEST_TIMEOUT_SECONDS


def test_native_cancel_check_false_keeps_waiting_for_the_reply(
    connections,
) -> None:
    connections.install(
        {"role": "assistant", "content": "late but real"},
        wait_timeouts=2,
    )
    client = make_client(native=True)

    result = client.chat(
        [Message(role="user", content="hi")],
        should_cancel=lambda: False,
    )

    assert result == "late but real"
    connection = connections.connections[-1]
    assert connection.getresponse_calls == 3
    assert connection.closed == 1


def test_compat_mode_remains_default_and_untouched() -> None:
    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.chat.completions.create.return_value = (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="hello", tool_calls=None
                        )
                    )
                ]
            )
        )
        client = OllamaLLMClient(model="qwen3:4b")

        assert client.native is False
        result = client.chat([Message(role="user", content="hi")])

    openai.return_value.chat.completions.create.assert_called_once_with(
        model="qwen3:4b",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result == "hello"


def test_native_decision_call_caps_decode_and_turns_thinking_off(
    connections,
) -> None:
    connections.install({"role": "assistant", "content": "", "tool_calls": []})
    client = make_client(
        native=True,
        num_ctx=8192,
        decision_max_output_tokens=8192,
        answer_max_output_tokens=2048,
        think=False,
    )

    client.chat_with_tools(
        [Message(role="user", content="hi")],
        [LLMToolDefinition(name="echo", description="", arguments={})],
    )

    payload = sent_payload(connections.connections[-1])
    assert payload["options"] == {"num_ctx": 8192, "num_predict": 8192}
    assert payload["think"] is False


def test_native_chat_uses_the_answer_budget(connections) -> None:
    connections.install({"role": "assistant", "content": "a joke"})
    client = make_client(
        native=True,
        decision_max_output_tokens=8192,
        answer_max_output_tokens=2048,
    )

    client.chat([Message(role="user", content="tell a joke")])

    payload = sent_payload(connections.connections[-1])
    assert payload["options"] == {"num_predict": 2048}
    # Answer prose never hides a tool decision: no thinking switch is
    # needed per call kind — the client-level knob applies to both.
    assert "think" not in payload


def test_native_think_true_is_sent_explicitly(connections) -> None:
    connections.install({"role": "assistant", "content": "x"})
    client = make_client(native=True, think=True)

    client.chat([Message(role="user", content="hi")])

    assert sent_payload(connections.connections[-1])["think"] is True


def test_native_knobs_absent_by_default_send_nothing(connections) -> None:
    connections.install({"role": "assistant", "content": "x"})
    client = make_client(native=True)

    client.chat([Message(role="user", content="hi")])

    payload = sent_payload(connections.connections[-1])
    assert "think" not in payload
    assert "options" not in payload


def test_compat_decision_call_sends_max_tokens() -> None:
    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.chat.completions.create.return_value = (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="hi", tool_calls=None
                        )
                    )
                ]
            )
        )
        client = OllamaLLMClient(
            model="qwen3:4b", decision_max_output_tokens=8192
        )

        client.chat_with_tools(
            [Message(role="user", content="hi")],
            [LLMToolDefinition(name="echo", description="", arguments={})],
        )

    request = openai.return_value.chat.completions.create.call_args.kwargs
    assert request["max_tokens"] == 8192
