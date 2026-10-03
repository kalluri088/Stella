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
    def __init__(
        self,
        body: bytes,
        status: int = 200,
        *,
        lines: list[bytes] | None = None,
        readline_stalls: int = 0,
    ) -> None:
        self._body = body
        self.status = status
        # ``lines`` is the NDJSON body a streamed reply serves, one read-
        # line per call; ``readline_stalls`` makes the first N reads raise
        # a socket timeout so a mid-stream cancel can be exercised.
        self._lines = list(lines) if lines is not None else None
        self._readline_stalls = readline_stalls
        self.readline_calls = 0

    def read(self) -> bytes:
        return self._body

    def readline(self) -> bytes:
        self.readline_calls += 1
        if self._readline_stalls > 0:
            self._readline_stalls -= 1
            raise TimeoutError("no line had arrived yet")
        if self._lines is None:
            return b""
        return self._lines.pop(0) if self._lines else b""


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
        lines: list[bytes] | None = None,
        readline_stalls: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.body = body
        self.status = status
        # Number of header-read timeouts to simulate before the reply.
        self._wait_timeouts = wait_timeouts
        self._lines = lines
        self._readline_stalls = readline_stalls
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
        return FakeHTTPResponse(
            self.body,
            status=self.status,
            lines=self._lines,
            readline_stalls=self._readline_stalls,
        )

    def close(self) -> None:
        self.closed += 1


class ConnectionRecorder:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self._patchers: list = []

    def _start(self, factory) -> None:
        self._patchers = [
            patch("stella.ollama_client.http.client.HTTPConnection", factory),
            patch(
                "stella.ollama_client.http.client.HTTPSConnection", factory
            ),
        ]
        for patcher in self._patchers:
            patcher.start()

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

        self._start(factory)

    def install_lines(
        self,
        lines: list,
        *,
        status: int = 200,
        wait_timeouts: int = 0,
        readline_stalls: int = 0,
    ) -> None:
        """Replay an NDJSON stream from raw dicts/bytes, line by line.

        A dict becomes one JSON line; a ``bytes`` value is served verbatim
        so a malformed line can be fed on purpose.
        """

        encoded = [
            line if isinstance(line, bytes)
            else json.dumps(line).encode("utf-8") + b"\n"
            for line in lines
        ]

        def factory(host, port=None, timeout=None, **_kwargs):
            connection = FakeConnection(
                host,
                port,
                timeout=timeout,
                status=status,
                wait_timeouts=wait_timeouts,
                lines=encoded,
                readline_stalls=readline_stalls,
            )
            self.connections.append(connection)
            return connection

        self._start(factory)

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


# ------------------------------------------------------- native streaming


def content_stream(pieces: list[str]) -> list:
    """NDJSON lines carrying ``pieces`` as answer content, then done."""

    return [
        {"message": {"role": "assistant", "content": piece}}
        for piece in pieces
    ] + [{"done": True}]


def test_native_stream_forwards_content_and_returns_the_whole_answer(
    connections,
) -> None:
    connections.install_lines(content_stream(["Hel", "lo", " there."]))
    client = make_client(native=True)
    heard: list[str] = []

    result = client.stream_chat(
        [Message(role="user", content="hi")], heard.append
    )

    assert result == "Hello there."
    assert heard == ["Hel", "lo", " there."]
    assert connections.connections[-1].closed == 1


def test_native_stream_sends_the_stream_flag_and_the_answer_budget(
    connections,
) -> None:
    connections.install_lines(content_stream(["x"]))
    client = make_client(
        native=True,
        num_ctx=4096,
        answer_max_output_tokens=2048,
        decision_max_output_tokens=8192,
    )

    client.stream_chat([Message(role="user", content="hi")], lambda _d: None)

    payload = sent_payload(connections.connections[-1])
    assert payload["stream"] is True
    # A streamed reply is an answer: it carries the answer budget, never
    # the decision budget, and no tools (decisions never stream).
    assert payload["options"] == {"num_ctx": 4096, "num_predict": 2048}
    assert "tools" not in payload


def test_stream_and_buffered_requests_differ_only_by_the_flag() -> None:
    client = make_client(
        native=True, num_ctx=4096, think=False
    )
    buffered = client._native_request(
        [Message(role="user", content="hi")],
        None,
        budget=2048,
        stream=False,
    )
    streamed = client._native_request(
        [Message(role="user", content="hi")],
        None,
        budget=2048,
        stream=True,
    )

    assert buffered["stream"] is False
    assert streamed["stream"] is True
    assert {k: v for k, v in buffered.items() if k != "stream"} == {
        k: v for k, v in streamed.items() if k != "stream"
    }


def test_native_stream_ignores_thinking_and_tool_call_deltas(
    connections,
) -> None:
    connections.install_lines(
        [
            {"message": {"role": "assistant", "thinking": "hidden chain"}},
            {"message": {"role": "assistant", "content": "shown."}},
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "echo"}}],
                }
            },
            {"done": True},
        ]
    )
    client = make_client(native=True)
    heard: list[str] = []

    result = client.stream_chat(
        [Message(role="user", content="hi")], heard.append
    )

    assert result == "shown."
    assert heard == ["shown."]
    assert all("hidden" not in piece for piece in heard)


def test_native_stream_malformed_line_raises(connections) -> None:
    connections.install_lines([b"{this is not json}\n"])
    client = make_client(native=True)

    with pytest.raises(OSError, match="malformed"):
        client.stream_chat([Message(role="user", content="hi")], lambda _d: None)

    assert connections.connections[-1].closed == 1


def test_native_stream_truncation_without_done_raises(connections) -> None:
    # A content line then end-of-stream with no done marker: the answer
    # was cut short, and a partial reply is never handed back as whole.
    connections.install_lines(
        [{"message": {"role": "assistant", "content": "half an "}}]
    )
    client = make_client(native=True)
    heard: list[str] = []

    with pytest.raises(OSError, match="before completion"):
        client.stream_chat([Message(role="user", content="hi")], heard.append)

    # The bytes that did arrive were still forwarded, but no answer is
    # reported as complete.
    assert heard == ["half an "]


def test_native_stream_error_payload_raises(connections) -> None:
    connections.install_lines([{"error": "model not found"}])
    client = make_client(native=True)

    with pytest.raises(OSError, match="model not found"):
        client.stream_chat([Message(role="user", content="hi")], lambda _d: None)


def test_native_stream_cancel_midflight_aborts_the_read(connections) -> None:
    # Two reads stall before any further line arrives; the first cancel
    # check says keep waiting, the second abandons the stream.
    connections.install_lines(content_stream(["x"]), readline_stalls=2)
    client = make_client(native=True)
    cancels = iter([False, True])

    with pytest.raises(ProviderRequestCancelled):
        client.stream_chat(
            [Message(role="user", content="hi")],
            lambda _d: None,
            should_cancel=lambda: next(cancels, True),
        )

    connection = connections.connections[-1]
    assert connection.closed == 1
    # A fast poll timeout is installed for the wait and the long request
    # timeout restored afterwards, even when the stream is abandoned.
    assert connection.sock.timeouts[0] < NATIVE_REQUEST_TIMEOUT_SECONDS
    assert connection.sock.timeouts[-1] == NATIVE_REQUEST_TIMEOUT_SECONDS


def test_compat_client_declines_to_stream() -> None:
    with patch("stella.openai_client.OpenAI"):
        client = OllamaLLMClient(model="qwen3:4b")

        heard: list[str] = []
        assert (
            client.stream_chat(
                [Message(role="user", content="hi")], heard.append
            )
            is None
        )
        assert heard == []
