"""Minimal synchronous command-line interface for Stella."""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence

from stella.brain import Decision, LLMBrain
from stella.context import Context
from stella.llm import Message
from stella.memory import SQLiteMemory
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL, OllamaLLMClient
from stella.openai_client import OpenAILLMClient
from stella.stella import Stella, StellaResult
from stella.tools import (
    ApprovalRequest,
    DateTimeTool,
    EchoTool,
    FileSystemDeleteTool,
    FileSystemReadTool,
    FileSystemWriteTool,
    NetworkReadTool,
    SystemInfoTool,
    ToolApproval,
    ToolDispatcher,
)
from stella.trace import (
    ApprovalEvent,
    DecisionEvent,
    FinalResponseEvent,
    InputReceivedEvent,
    MemoryRetrievedEvent,
    MemoryWriteEvent,
    ToolResultEvent,
)


def run_cli(
    stella: Stella,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    debug: bool = False,
    debug_fn: Callable[[str], None] | None = None,
    trace: bool = False,
) -> None:
    """Run one interactive Stella session."""

    if isinstance(stella, Stella) and stella.approval_provider is None:
        stella.approval_provider = cli_approval_provider(
            input_fn=input_fn,
            output_fn=output_fn,
        )

    history: list[Message] = []
    while True:
        try:
            user_input = input_fn("You: ")
        except EOFError:
            output_fn("Goodbye!")
            return

        if user_input.strip().casefold() in {"exit", "quit"}:
            output_fn("Goodbye!")
            return

        result = stella.process(
            Context(
                user_input=user_input,
                conversation_history=list(history),
            )
        )
        if debug:
            (debug_fn or _print_debug)(format_decision(result.decision))
        if trace:
            timeline = format_trace(result)
            if timeline:
                output_fn("Stella did")
                for line in timeline:
                    output_fn(line)
                output_fn("")
        response = _display_response(result)
        if response is not None:
            output_fn(f"Stella: {response}")
        history.append(Message(role="user", content=user_input))
        if response is not None:
            history.append(Message(role="assistant", content=response))


def format_trace(result: StellaResult) -> list[str]:
    """Render one turn's metadata-only trace as compact timeline lines."""

    if result.interaction_trace is None:
        return []

    lines: list[str] = []
    for event in result.interaction_trace.events:
        if isinstance(event, InputReceivedEvent):
            lines.append(
                _trace_line(
                    "input",
                    f"{event.user_input_chars} chars, "
                    f"{event.conversation_messages} history messages",
                )
            )
        elif isinstance(event, MemoryRetrievedEvent):
            if event.count:
                lines.append(
                    _trace_line("memory", f"retrieved {event.count}")
                )
        elif isinstance(event, DecisionEvent):
            summary = event.kind.upper()
            if event.capability:
                summary += f" -> {event.capability}"
            if event.argument_keys:
                summary += f" ({', '.join(event.argument_keys)})"
            if event.memory_write_proposed:
                summary += " +memory proposal"
            lines.append(_trace_line("decision", summary))
        elif isinstance(event, ApprovalEvent):
            outcome = {
                True: "granted",
                False: "denied",
                None: "not requested",
            }[event.approved]
            suffix = f" for {event.capability}" if event.capability else ""
            lines.append(_trace_line("approval", outcome + suffix))
        elif isinstance(event, ToolResultEvent):
            status = "success" if event.success else "failed"
            lines.append(
                _trace_line(
                    "tool",
                    f"{event.capability or 'unknown'} {status}, "
                    f"{event.output_chars} chars output",
                )
            )
        elif isinstance(event, MemoryWriteEvent):
            if event.written:
                lines.append(
                    _trace_line(
                        "memory", f"written ({event.content_chars} chars)"
                    )
                )
            elif event.proposed:
                lines.append(_trace_line("memory", "proposed, not stored"))
        elif isinstance(event, FinalResponseEvent):
            if event.needs_more_information:
                lines.append(
                    _trace_line("final", "needs more information")
                )
            elif event.max_steps_reached:
                lines.append(_trace_line("final", "stopped at step limit"))
    return lines


def _trace_line(label: str, detail: str) -> str:
    return f"  {label:<9} {detail}"


def format_startup(stella: Stella) -> list[str]:
    """Describe the active configuration; never includes secrets."""

    llm = getattr(getattr(stella, "brain", None), "llm", None)
    base_url = str(getattr(getattr(llm, "client", None), "base_url", "") or "")
    database = getattr(stella.memory, "database_path", None)
    workspace = None
    for tool in getattr(stella.tools, "_tools", {}).values():
        if hasattr(tool, "workspace"):
            workspace = str(tool.workspace)
            break
    return [
        f"provider:  {type(llm).__name__ if llm is not None else 'unknown'}",
        f"model:     {getattr(llm, 'model', None) or 'unknown'}",
        f"endpoint:  {base_url or 'default'}",
        f"memory db: {database if database is not None else 'in-memory'}",
        f"workspace: {workspace or 'not configured'}",
    ]


def cli_approval_provider(
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> Callable[[ApprovalRequest], ToolApproval]:
    """Create the CLI's explicit, action-specific approval callback."""

    def request_approval(request: ApprovalRequest) -> ToolApproval:
        arguments = json.dumps(request.arguments, sort_keys=True)
        output_fn(
            "Approval required for action: "
            f"capability={request.capability!r}, arguments={arguments}"
        )
        try:
            answer = input_fn("Approve this action? [yes/approve/no]: ")
        except EOFError:
            answer = ""
        approved = answer.strip().casefold() in {"yes", "approve"}
        return ToolApproval(request=request, approved=approved)

    return request_approval


def format_decision(decision: Decision) -> str:
    """Return a JSON inspection string for a structured decision."""

    payload = {
        "kind": decision.kind.value,
        "content": decision.content,
        "arguments": decision.arguments,
        "capability": decision.capability,
        "memory_write": (
            {"content": decision.memory_write.item.content}
            if decision.memory_write is not None
            else None
        ),
    }
    return f"Decision: {json.dumps(payload, sort_keys=True)}"


def _print_debug(message: str) -> None:
    print(message, file=sys.stderr)


def _display_response(result: StellaResult) -> str | None:
    if result.response is not None:
        return result.response
    if result.tool_result is not None:
        return result.tool_result.output
    if result.needs_more_information:
        return "I need more information."
    return None


def create_stella_from_environment() -> Stella:
    """Build the CLI's LLM-backed Stella instance from environment variables."""

    model = os.environ.get("STELLA_MODEL")
    if not model:
        raise SystemExit("STELLA_MODEL is required")

    provider = os.environ.get("STELLA_LLM_PROVIDER", "openai").casefold()
    if provider == "ollama":
        llm = OllamaLLMClient(
            model=model,
            base_url=os.environ.get(
                "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
            ),
        )
    elif provider == "openai":
        llm = OpenAILLMClient(
            model=model,
            base_url=os.environ.get("OPENAI_BASE_URL"),
        )
    else:
        raise SystemExit("STELLA_LLM_PROVIDER must be 'openai' or 'ollama'")
    memory = SQLiteMemory(os.environ.get("STELLA_MEMORY_DB", "stella_memory.db"))
    tools = ToolDispatcher(
        [
            DateTimeTool(),
            SystemInfoTool(),
            EchoTool(),
            FileSystemReadTool(
                os.environ.get("STELLA_WORKSPACE", "./stella_workspace")
            ),
            FileSystemWriteTool(
                os.environ.get("STELLA_WORKSPACE", "./stella_workspace")
            ),
            FileSystemDeleteTool(
                os.environ.get("STELLA_WORKSPACE", "./stella_workspace")
            ),
            NetworkReadTool(),
        ]
    )
    return Stella(
        brain=LLMBrain(llm, tools),
        llm=llm,
        tool=tools,
        memory=memory,
        max_tool_steps=2,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Start an interactive Stella session."""

    parser = argparse.ArgumentParser(description="Start an interactive Stella session")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print each structured Brain decision to stderr",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="render a compact 'Stella did' timeline after each turn",
    )
    args = parser.parse_args(argv)
    stella = create_stella_from_environment()
    for line in format_startup(stella):
        print(line)
    try:
        run_cli(stella, debug=args.debug, trace=args.trace)
    finally:
        if isinstance(stella.memory, SQLiteMemory):
            stella.memory.close()
