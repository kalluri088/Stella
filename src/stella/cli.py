"""Minimal synchronous command-line interface for Stella."""

import argparse
import datetime as dt
import json
import sys
from collections.abc import Callable, Sequence

from stella.app import StellaSession, StellaSettings, build_application
from stella.brain import Decision
from stella.config import resolve_settings
from stella.stella import Stella, StellaResult
from stella.tools import ApprovalRequest, ToolApproval
from stella.trace import (
    ActionReceiptEvent,
    ApprovalEvent,
    DecisionEvent,
    FinalResponseEvent,
    InputReceivedEvent,
    InteractionTrace,
    MemoryActionEvent,
    MemoryRetrievedEvent,
    MemoryWriteEvent,
    ReminderLifecycleEvent,
    ToolResultEvent,
)

# input() only provides line editing (arrow keys, word kill) and
# up/down history when GNU readline is loaded into the process.
try:  # pragma: no cover - platform builds without readline skip this
    import readline  # noqa: F401
except ImportError:
    pass


def run_cli(
    stella: Stella,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    debug: bool = False,
    debug_fn: Callable[[str], None] | None = None,
    trace: bool = False,
    status_fn: Callable[[str], None] | None = None,
) -> None:
    """Run one interactive Stella session."""

    if isinstance(stella, Stella) and stella.approval_provider is None:
        stella.approval_provider = cli_approval_provider(
            input_fn=input_fn,
            output_fn=output_fn,
            status_fn=status_fn or _print_status,
        )
    status = status_fn or _print_status
    session = StellaSession(stella)
    while True:
        try:
            user_input = input_fn("You: ")
        except (EOFError, KeyboardInterrupt):
            output_fn("Goodbye!")
            return

        if user_input.strip().casefold() in {"exit", "quit"}:
            output_fn("Goodbye!")
            return

        if not user_input.strip():
            # Empty or whitespace-only input is harmless: no processing,
            # no history entry, no thinking indicator.
            continue

        # Real interactions are the only scheduling trigger: due reminders
        # are delivered through the existing bounded proactivity decision.
        _deliver_due_reminders(stella, output_fn, trace=trace)
        status("Stella is thinking...")
        outcome = session.run_turn(user_input)
        if outcome.interrupted:
            output_fn("Stella stopped that request. Nothing was changed.")
            continue
        if outcome.error_message is not None:
            output_fn(outcome.error_message)
            continue
        result = outcome.result
        if debug:
            (debug_fn or _print_debug)(format_decision(result.decision))
        if trace:
            timeline = format_trace(result)
            if timeline:
                output_fn("Stella did")
                for line in timeline:
                    output_fn(line)
                output_fn("")
        response = outcome.response
        if response is not None:
            output_fn(f"Stella: {response}")
        else:
            # A deliberate no-op should not look like a silent failure.
            status("Stella has nothing to add.")


def _deliver_due_reminders(
    stella: Stella,
    output_fn: Callable[[str], None],
    trace: bool = False,
) -> None:
    """Run the trusted per-interaction due-reminder check and show results."""

    if not isinstance(stella, Stella):
        # Minimal test or embedding stubs may not carry the reminder flow.
        return
    reminder_trace = InteractionTrace(interaction_id="reminder-check")
    deliveries = stella.check_due_reminders(
        dt.datetime.now(dt.UTC), trace=reminder_trace
    )
    for delivery in deliveries:
        if delivery.delivered and delivery.message is not None:
            output_fn(f"Stella: {delivery.message}")
    if trace:
        for event in reminder_trace.events:
            line = _reminder_lifecycle_line(event)
            if line is not None:
                output_fn(line)


def _reminder_lifecycle_line(event: object) -> str | None:
    if not isinstance(event, ReminderLifecycleEvent):
        return None
    detail = event.action
    if event.reminder_id is not None:
        detail += f" #{event.reminder_id}"
    if event.outcome:
        detail += f" ({event.outcome})"
    return _trace_line("reminder", detail)


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
        elif isinstance(event, MemoryActionEvent):
            lines.append(
                _trace_line("memory", f"{event.action} {event.count}")
            )
        elif isinstance(event, ActionReceiptEvent):
            detail = f"{event.action} {event.status}"
            if event.size_bytes is not None:
                detail += f" ({event.size_bytes} bytes)"
            lines.append(_trace_line("action", detail))
        elif isinstance(event, ReminderLifecycleEvent):
            line = _reminder_lifecycle_line(event)
            if line is not None:
                lines.append(line)
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
    reminders_database = getattr(
        getattr(stella, "reminders", None), "database_path", None
    )
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
        (
            f"reminders db: {reminders_database}"
            if reminders_database is not None
            else "reminders: disabled"
        ),
        f"workspace: {workspace or 'not configured'}",
    ]


def _action_summary(request: ApprovalRequest) -> str:
    """Describe one approval request in plain user-facing language."""

    arguments = request.arguments

    def quoted(key: str) -> str | None:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return json.dumps(value)
        return None

    capability = request.capability
    if capability == "filesystem_write":
        path = quoted("path")
        if path is not None and quoted("content") is not None:
            return f"create a new text file {path} in your Stella workspace"
    elif capability == "filesystem_edit":
        path = quoted("path")
        if path is not None and quoted("content") is not None:
            return f"replace the contents of {path} in your Stella workspace"
    elif capability == "filesystem_delete":
        path = quoted("path")
        if path is not None:
            return (
                f"delete the file {path} from your Stella workspace "
                "(this cannot be undone)"
            )
    elif capability == "network_read":
        url = quoted("url")
        if url is not None:
            return f"fetch text from this public web address: {url}"
    elif capability == "memory_write":
        content = quoted("content")
        if content is not None:
            return f"remember this as a permanent fact: {content}"
    elif capability == "memory_update":
        query = quoted("query")
        content = quoted("content")
        if query is not None and content is not None:
            return f"change the memory matching {query} to {content}"
    elif capability == "memory_forget":
        query = quoted("query")
        if query is not None:
            return (
                f"delete stored memories matching {query} "
                "(this cannot be undone)"
            )
    elif capability == "memory_list":
        return "show everything it has remembered about you"
    elif capability == "reminder_create":
        content = quoted("content")
        due_at = quoted("due_at")
        if content is not None and due_at is not None:
            return (
                f"create a reminder for {due_at} that says {content} "
                "(it will only notify you later, never act)"
            )
    elif capability == "reminder_cancel":
        query = quoted("query")
        if query is not None:
            return (
                f"cancel the pending reminder matching {query} "
                "(this cannot be undone)"
            )
    return (
        f"use the '{capability}' tool with arguments "
        f"{json.dumps(arguments, sort_keys=True)}"
    )


def cli_approval_provider(
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    status_fn: Callable[[str], None] | None = None,
) -> Callable[[ApprovalRequest], ToolApproval]:
    """Create the CLI's explicit, action-specific approval callback."""

    def request_approval(request: ApprovalRequest) -> ToolApproval:
        output_fn(
            f"Stella would like to {_action_summary(request)}. "
            "Type 'yes' to allow this; anything else will skip it."
        )
        try:
            answer = input_fn("Approve? [yes/no]: ")
        except EOFError:
            answer = ""
        approved = answer.strip().casefold() in {"yes", "approve"}
        if approved and status_fn is not None:
            # Approval returns control to another multi-second model
            # phase; keep the thinking indicator consistent.
            status_fn("Stella is thinking...")
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


def _print_status(message: str) -> None:
    print(f"({message})", file=sys.stderr)


def create_stella_from_environment() -> Stella:
    """Build the CLI's LLM-backed Stella instance from environment variables."""

    return build_application(StellaSettings.from_environment()).session.stella


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
    settings = resolve_settings()
    if settings is None:
        print(
            "Stella is not configured yet. Run 'stella-ui' once to pick a "
            "model through the setup window, or set STELLA_MODEL (and "
            "optionally STELLA_LLM_PROVIDER) as before.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    application = build_application(settings)
    for line in format_startup(application.session.stella):
        print(line)
    print("Ask Stella anything. Type 'exit' to quit.\n")
    try:
        run_cli(application.session.stella, debug=args.debug, trace=args.trace)
    finally:
        application.close()
