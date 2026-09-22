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


def run_cli(
    stella: Stella,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    debug: bool = False,
    debug_fn: Callable[[str], None] | None = None,
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
        response = _display_response(result)
        if response is not None:
            output_fn(f"Stella: {response}")
        history.append(Message(role="user", content=user_input))
        if response is not None:
            history.append(Message(role="assistant", content=response))


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
    args = parser.parse_args(argv)
    stella = create_stella_from_environment()
    try:
        run_cli(stella, debug=args.debug)
    finally:
        if isinstance(stella.memory, SQLiteMemory):
            stella.memory.close()
