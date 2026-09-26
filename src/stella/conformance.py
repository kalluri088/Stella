"""Stage B4: one provider conformance contract, replayed on every dialect.

A provider swap replaces two things at once: the HTTP dialect the client
speaks and the model whose replies the brain must parse. This module
states the obligations Stella's decision path depends on, independently
of any particular provider, and proves them offline by driving the real
client classes (``OpenAILLMClient``, ``OllamaLLMClient`` compat and
native) against a loopback ``http.server`` that replays the exact wire
shapes real providers are known to produce — clean JSON, fence-wrapped
JSON, stringified or malformed tool arguments, empty replies, prose
instead of decisions, and HTTP 500s.

Every check is deterministic and CPU-only: no network beyond 127.0.0.1,
no GPU, no fixed sleeps. The same ``CONFORMANCE_CASES`` table is what a
live endpoint probe (kept outside the repo in ``~/tools``) re-asks
against a real model, so "provider X conforms" is a measured claim with
one shared definition — the report-15 lesson: silent prompt truncation
was a provider behavior nobody had written down as a question.
"""

import http.server
import json
import threading
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self

from stella.brain import Decision, DecisionKind, LLMBrain
from stella.context import Context
from stella.llama_server import LlamaServerLLMClient
from stella.llm import LLMClient
from stella.memory import MemoryItem, MemoryWriteRequest
from stella.ollama_client import OllamaLLMClient
from stella.openai_client import OpenAILLMClient
from stella.tools import DateTimeTool, EchoTool, SystemInfoTool, ToolDispatcher

MODEL = "conformance-model"
# The decision-prompt context budget the shipping config uses; the
# prompt-fit tripwire below measures against exactly this number.
DECISION_NUM_CTX = 8192
FIXED_CLOCK = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class WireReply:
    """One provider answer in transport-neutral form.

    ``tool_calls`` entries carry arguments either as a dict (the native
    Ollama shape) or as a raw string (what chat-completions and the
    Responses API always put on the wire, including the malformed
    variants small models produce).
    """

    content: str | None = None
    tool_calls: tuple[tuple[str, object], ...] = ()
    status: int = 200


@dataclass(frozen=True)
class ConformanceExpectation:
    """What the full stack must do with one ``WireReply``."""

    decision: Decision | None = None
    raises: bool = False
    dispatcher_rejects: bool = False
    request_tool_names: frozenset[str] | None = None
    request_num_ctx: int | None = None


@dataclass(frozen=True)
class ConformanceCase:
    """One named obligation plus the provider behavior that tests it."""

    name: str
    obligation: str
    user_input: str
    reply: WireReply
    expectation: ConformanceExpectation
    dialects: frozenset[str] = field(default_factory=lambda: ALL_DIALECTS)


@dataclass(frozen=True)
class ConformanceResult:
    case: str
    dialect: str
    passed: bool
    detail: str


def conformance_dispatcher() -> ToolDispatcher:
    """The fixed trusted environment every case is decided against."""

    return ToolDispatcher([DateTimeTool(), EchoTool(), SystemInfoTool()])


def conformance_brain(llm: LLMClient) -> LLMBrain:
    return LLMBrain(llm, tools=conformance_dispatcher(), clock=lambda: FIXED_CLOCK)


# --- the provider simulator -------------------------------------------------


class _SimulatedServer(http.server.ThreadingHTTPServer):
    """A loopback endpoint with a scripted reply queue and request log."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SimulatedHandler)
        self.condition = threading.Condition()
        self.script: list[WireReply] = []
        self.requests: list[dict] = []

    def next_reply(self) -> WireReply:
        with self.condition:
            if self.script:
                return self.script.pop(0)
        raise LookupError("the simulated provider has no scripted reply")


class _SimulatedHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        path = urllib.parse.urlsplit(self.path).path
        server: _SimulatedServer = self.server  # type: ignore[assignment]
        server.requests.append(
            {"path": path, "payload": json.loads(raw.decode("utf-8"))}
        )
        reply = server.next_reply()
        if reply.status >= 400:
            body = json.dumps(
                {
                    "error": {
                        "message": "simulated provider failure",
                        "type": "server_error",
                    }
                }
            ).encode("utf-8")
        else:
            body = json.dumps(_envelope(path, reply)).encode("utf-8")
        self.send_response(reply.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Keep the simulated provider silent; tests own the reporting."""


def _wire_arguments(arguments: object, *, native: bool) -> object:
    if isinstance(arguments, str):
        return arguments
    if native:
        return arguments
    return json.dumps(arguments)


def _envelope(path: str, reply: WireReply) -> dict:
    """Serialize one neutral reply in the dialect the endpoint was asked."""

    native = path.endswith("/api/chat")
    responses = path.endswith("/responses")
    if native:
        return {
            "model": MODEL,
            "created_at": FIXED_CLOCK.isoformat(),
            "message": {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {
                        "function": {
                            "name": name,
                            "arguments": _wire_arguments(
                                arguments, native=True
                            ),
                        }
                    }
                    for name, arguments in reply.tool_calls
                ],
            },
            "done": True,
            "prompt_eval_count": 1,
            "eval_count": 1,
        }
    if responses:
        output: list[dict] = [
            {
                "type": "function_call",
                "id": f"fc_{index}",
                "call_id": f"call_{index}",
                "name": name,
                "arguments": _wire_arguments(arguments, native=False),
                "status": "completed",
            }
            for index, (name, arguments) in enumerate(reply.tool_calls)
        ]
        if reply.content is not None:
            output.append(
                {
                    "type": "message",
                    "id": "msg_0",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": reply.content,
                            "annotations": [],
                        }
                    ],
                }
            )
        return {
            "id": "conformance-response-1",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": MODEL,
            "output": output,
        }
    message: dict[str, object] = {
        "role": "assistant",
        "content": reply.content,
    }
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": _wire_arguments(arguments, native=False),
                },
            }
            for index, (name, arguments) in enumerate(reply.tool_calls)
        ]
    return {
        "id": "conformance-completion-1",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": (
                    "tool_calls" if reply.tool_calls else "stop"
                ),
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


class SimulatedProvider:
    """A scripted stand-in endpoint speaking every dialect Stella uses.

    Use as a context manager; queue one ``WireReply`` per request the
    case will make, then point a client factory at ``base_url``.
    """

    def __init__(self) -> None:
        self._server: _SimulatedServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self._server = _SimulatedServer()
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    def queue(self, reply: WireReply) -> None:
        assert self._server is not None
        self._server.script.append(reply)

    @property
    def requests(self) -> list[dict]:
        assert self._server is not None
        return list(self._server.requests)


# --- client dialects ---------------------------------------------------------


def _openai_compat_client(provider: SimulatedProvider) -> LLMClient:
    return OpenAILLMClient(
        model=MODEL, base_url=provider.base_url, api_key="conformance"
    )


def _ollama_compat_client(provider: SimulatedProvider) -> LLMClient:
    return OllamaLLMClient(model=MODEL, base_url=provider.base_url)


def _ollama_native_client(provider: SimulatedProvider) -> LLMClient:
    return OllamaLLMClient(
        model=MODEL,
        base_url=provider.base_url,
        native=True,
        num_ctx=DECISION_NUM_CTX,
    )


def _llama_server_compat_client(provider: SimulatedProvider) -> LLMClient:
    return LlamaServerLLMClient(model=MODEL, base_url=provider.base_url)


ALL_DIALECTS: frozenset[str] = frozenset(
    {
        "openai-compat",
        "ollama-compat",
        "ollama-native",
        "llama-server-compat",
    }
)

DIALECT_CLIENTS: dict[str, Callable[[SimulatedProvider], LLMClient]] = {
    "openai-compat": _openai_compat_client,
    "ollama-compat": _ollama_compat_client,
    "ollama-native": _ollama_native_client,
    "llama-server-compat": _llama_server_compat_client,
}


# --- the obligations themselves ----------------------------------------

ANSWER_JSON = '{"kind":"answer","content":"Hello there."}'


def _memory_write_request(content: str) -> MemoryWriteRequest:
    return MemoryWriteRequest(MemoryItem(content))


def _answer(text: str = "Hello there.") -> Decision:
    return Decision(DecisionKind.ANSWER, content=text)


def _tool(
    capability: str,
    arguments: dict[str, object],
    tool_final: bool = False,
) -> Decision:
    return Decision(
        DecisionKind.TOOL,
        capability=capability,
        arguments=arguments,
        tool_final=tool_final,
    )


CONFORMANCE_CASES: tuple[ConformanceCase, ...] = (
    ConformanceCase(
        name="answer-plain-json",
        obligation="a well-formed decision JSON on the text channel becomes that decision",
        user_input="Say hello.",
        reply=WireReply(content=ANSWER_JSON),
        expectation=ConformanceExpectation(decision=_answer()),
    ),
    ConformanceCase(
        name="answer-fenced-json",
        obligation="JSON wrapped in code fences (a very common provider habit) is still parsed",
        user_input="Say hello.",
        reply=WireReply(content=f"```json\n{ANSWER_JSON}\n```"),
        expectation=ConformanceExpectation(decision=_answer()),
    ),
    ConformanceCase(
        name="answer-prose-wrapped-json",
        obligation="JSON embedded in surrounding prose is recovered, fences or no fences",
        user_input="Say hello.",
        reply=WireReply(content=f"Sure, here it is:\n{ANSWER_JSON}\nDone."),
        expectation=ConformanceExpectation(decision=_answer()),
    ),
    ConformanceCase(
        name="bare-prose-fails-closed",
        obligation="a reply with no decision JSON at all must never be spoken; it is do_nothing",
        user_input="Say hello.",
        reply=WireReply(content="Hello there! How are you?"),
        expectation=ConformanceExpectation(
            decision=Decision(DecisionKind.DO_NOTHING)
        ),
    ),
    ConformanceCase(
        name="empty-reply-fails-closed",
        obligation="an empty assistant turn (no content, no calls) is do_nothing, not a crash",
        user_input="Say hello.",
        reply=WireReply(content=None),
        expectation=ConformanceExpectation(
            decision=Decision(DecisionKind.DO_NOTHING)
        ),
    ),
    ConformanceCase(
        name="text-protocol-tool-call",
        obligation='kind=tool with a capability and arguments on the text channel becomes a tool decision',
        user_input="What time is it?",
        reply=WireReply(
            content='{"kind":"tool","capability":"datetime","arguments":{"kind":"time"}}'
        ),
        expectation=ConformanceExpectation(
            decision=_tool("datetime", {"kind": "time"})
        ),
    ),
    ConformanceCase(
        name="native-tool-call",
        obligation="a native tool call is normalized into the same tool decision",
        user_input="What time is it?",
        reply=WireReply(
            content=None,
            tool_calls=(("datetime", {"kind": "time"}),),
        ),
        expectation=ConformanceExpectation(
            decision=_tool("datetime", {"kind": "time"})
        ),
    ),
    ConformanceCase(
        name="native-tool-call-arguments-as-string",
        obligation="tool arguments that arrive as a JSON string are parsed, not dropped",
        user_input="What time is it?",
        reply=WireReply(
            content=None,
            tool_calls=(("datetime", '{"kind": "time"}'),),
        ),
        expectation=ConformanceExpectation(
            decision=_tool("datetime", {"kind": "time"})
        ),
    ),
    ConformanceCase(
        name="native-tool-call-malformed-arguments",
        obligation="unparseable tool arguments fail closed to no arguments for the trusted validator",
        user_input="What time is it?",
        reply=WireReply(
            content=None,
            tool_calls=(("datetime", "not-json"),),
        ),
        expectation=ConformanceExpectation(
            decision=_tool("datetime", {})
        ),
    ),
    ConformanceCase(
        name="native-tool-call-final-marker-stripped",
        obligation='the reserved "tool_final" argument is honored and stripped before the dispatcher sees arguments',
        user_input="What time is it?",
        reply=WireReply(
            content=None,
            tool_calls=(("datetime", {"kind": "time", "tool_final": True}),),
        ),
        expectation=ConformanceExpectation(
            decision=_tool("datetime", {"kind": "time"}, tool_final=True)
        ),
    ),
    ConformanceCase(
        name="invented-capability-fails-closed",
        obligation="a capability the model invented is refused by the trusted dispatcher, never executed",
        user_input="What time is it?",
        reply=WireReply(
            content=None,
            tool_calls=(("delete_the_database", {}),),
        ),
        expectation=ConformanceExpectation(
            decision=_tool("delete_the_database", {}),
            dispatcher_rejects=True,
        ),
    ),
    ConformanceCase(
        name="required-tool-guard-overrides-answer",
        obligation=(
            "when a tool is REQUIRED and the provider answers anyway "
            "(compat endpoints do not enforce required), the answer guard "
            "turns it into a clarification, never a claimed inspection"
        ),
        user_input="What is the current time?",
        reply=WireReply(
            content='{"kind":"answer","content":"It is 3pm."}'
        ),
        expectation=ConformanceExpectation(
            decision=Decision(
                DecisionKind.ASK,
                content=(
                    "I need to inspect the current information before "
                    "answering."
                ),
            )
        ),
    ),
    ConformanceCase(
        name="memory-write-proposal-passes-through",
        obligation="a memory_write proposal object survives normalization into the decision (proposal only, never execution)",
        user_input="Remember that I like tea.",
        reply=WireReply(
            content='{"kind":"answer","content":"OK.","memory_write":{"content":"I like tea."}}'
        ),
        expectation=ConformanceExpectation(
            decision=Decision(
                DecisionKind.ANSWER,
                content="OK.",
                memory_write=_memory_write_request("I like tea."),
            )
        ),
    ),
    ConformanceCase(
        name="transport-error-surfaces-as-error",
        obligation=(
            "an HTTP 500 must raise; a provider failure surfaces as an "
            "error, never as a fake empty or confident reply"
        ),
        user_input="Say hello.",
        reply=WireReply(status=500),
        expectation=ConformanceExpectation(raises=True),
    ),
    ConformanceCase(
        name="native-request-carries-num-ctx",
        obligation=(
            "the native dialect sends the decision context budget as "
            "options.num_ctx — the request-side half of the report-15 "
            "truncation obligation"
        ),
        user_input="Say hello.",
        reply=WireReply(content=ANSWER_JSON),
        expectation=ConformanceExpectation(
            decision=_answer(),
            request_num_ctx=DECISION_NUM_CTX,
        ),
        dialects=frozenset({"ollama-native"}),
    ),
    ConformanceCase(
        name="request-declares-tools",
        obligation="every tool-channel request declares the currently available capabilities to the provider",
        user_input="Say hello.",
        reply=WireReply(content=ANSWER_JSON),
        expectation=ConformanceExpectation(
            decision=_answer(),
            request_tool_names=frozenset({"datetime", "echo", "system_info"}),
        ),
    ),
)

# --- the harness ---------------------------------------------------------


def _request_tool_names(payload: dict) -> set[str]:
    names = set()
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function", tool)
        if isinstance(function, dict) and "name" in function:
            names.add(str(function["name"]))
    return names


def run_case(case: ConformanceCase, dialect: str) -> ConformanceResult:
    """Replay one obligation's provider behavior through one real client."""

    factory = DIALECT_CLIENTS[dialect]
    with SimulatedProvider() as provider:
        provider.queue(case.reply)
        brain = conformance_brain(factory(provider))
        context = Context(user_input=case.user_input)
        try:
            decision = brain.decide(context)
        except Exception as error:  # noqa: BLE001 - the contract is the type
            if case.expectation.raises:
                return ConformanceResult(
                    case.name,
                    dialect,
                    True,
                    f"raised {type(error).__name__} as required",
                )
            return ConformanceResult(
                case.name,
                dialect,
                False,
                f"unexpected {type(error).__name__}: {error}",
            )
        if case.expectation.raises:
            return ConformanceResult(
                case.name,
                dialect,
                False,
                f"expected a raised error, got decision {decision.kind.value}",
            )
        expected = case.expectation.decision
        if decision != expected:
            return ConformanceResult(
                case.name,
                dialect,
                False,
                f"decision {decision!r} != expected {expected!r}",
            )
        if case.expectation.dispatcher_rejects:
            result = conformance_dispatcher().execute(
                decision.capability, dict(decision.arguments or {})
            )
            if result.success:
                return ConformanceResult(
                    case.name, dialect, False, "dispatcher executed it"
                )
        request_checks = _check_requests(case, dialect, provider.requests)
        if request_checks is not None:
            return ConformanceResult(
                case.name, dialect, False, request_checks
            )
        return ConformanceResult(case.name, dialect, True, case.obligation)


def _check_requests(
    case: ConformanceCase, dialect: str, requests: list[dict]
) -> str | None:
    """Verify request-side obligations; None means every check held."""

    expectation = case.expectation
    if (
        expectation.request_tool_names is None
        and expectation.request_num_ctx is None
    ):
        return None
    if not requests:
        return "the client never reached the provider"
    payload = requests[-1]["payload"]
    if expectation.request_tool_names is not None:
        declared = _request_tool_names(payload)
        missing = expectation.request_tool_names - declared
        if missing:
            return f"request did not declare tools: {sorted(missing)}"
    if expectation.request_num_ctx is not None:
        options = payload.get("options") or {}
        if options.get("num_ctx") != expectation.request_num_ctx:
            return (
                f"request options.num_ctx is "
                f"{options.get('num_ctx')!r}, expected "
                f"{expectation.request_num_ctx!r}"
            )
    return None


def run_conformance(
    cases: Iterable[ConformanceCase] = CONFORMANCE_CASES,
    dialects: Iterable[str] | None = None,
) -> list[ConformanceResult]:
    """Run the whole contract across every applicable client dialect."""

    requested = set(dialects) if dialects is not None else ALL_DIALECTS
    return [
        run_case(case, dialect)
        for case in cases
        for dialect in sorted(requested & case.dialects)
    ]


# --- prompt fit (the written form of the report-15 question) -------------

# Deliberately crude: ~4 characters per BPE token for mixed English and
# JSON. The tripwire is about order of magnitude — catching a prompt that
# has silently outgrown the context budget — not exact token counts,
# which only the provider itself can report (prompt_eval_count).
APPROX_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return (len(text) + APPROX_CHARS_PER_TOKEN - 1) // (
        APPROX_CHARS_PER_TOKEN
    )


def decision_prompt(brain: LLMBrain, context: Context) -> str:
    """The exact text a decision turn sends: system prompt plus payload."""

    return f"{brain._system_prompt()}\n{brain._context_payload(context)}"


def decision_prompt_tokens(
    brain: LLMBrain, context: Context
) -> int:
    return estimate_tokens(decision_prompt(brain, context))
