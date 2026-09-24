"""Minimal synchronous command-line interface for Stella."""

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence

from stella.app import (
    StellaSession,
    StellaSettings,
    build_application,
    drain_persona_proposals,
)
from stella.brain import Decision
from stella.config import resolve_settings
from stella.llm import Message
from stella.persona import (
    ONBOARDING_QUESTIONS,
    ONBOARDING_SKIP_MARKER,
    PERSONA_DRAFT_SYSTEM,
    PERSONA_SKELETON,
    PRESET_TEMPLATES,
    PersonaPaths,
    PersonaReflection,
    ReflectionStore,
    TranscriptRecorder,
    ensure_persona_directory,
    persona_directory,
    write_persona_text,
)
from stella.stella import Stella, StellaResult
from stella.tools import (
    ActionPreview,
    ApprovalRequest,
    ToolApproval,
    action_summary,
)
from stella.trace import (
    ActionReceiptEvent,
    ApprovalEvent,
    DecisionEvent,
    FinalResponseEvent,
    InputReceivedEvent,
    InteractionTrace,
    MemoryActionEvent,
    MemoryIndexSyncEvent,
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
    persona_proposals: ReflectionStore | None = None,
    session: StellaSession | None = None,
) -> None:
    """Run one interactive Stella session.

    ``session`` should be the application's own session: it is the only
    one carrying the opt-in transcript recorder. Without it, a fresh
    (unrecorded) session is built — the historical behavior.
    """

    if isinstance(stella, Stella) and stella.approval_provider is None:
        stella.approval_provider = cli_approval_provider(
            input_fn=input_fn,
            output_fn=output_fn,
            status_fn=status_fn or _print_status,
        )
    # Queued `stella reflect` proposals become real approval prompts now
    # that an approval provider exists; anything left pending waits.
    drain_persona_proposals(stella, persona_proposals, notify=output_fn)
    status = status_fn or _print_status
    if session is None:
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
        elif isinstance(event, MemoryIndexSyncEvent):
            status = "refreshed" if event.ok else "REFRESH FAILED"
            lines.append(_trace_line("memory", f"semantic index {status}"))
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


def cli_approval_provider(
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    status_fn: Callable[[str], None] | None = None,
) -> Callable[..., ToolApproval]:
    """Create the CLI's explicit, action-specific approval callback."""

    def request_approval(
        request: ApprovalRequest,
        preview: ActionPreview | None = None,
    ) -> ToolApproval:
        output_fn(
            f"Stella would like to {action_summary(request)}. "
            "Type 'yes' to allow this; anything else will skip it."
        )
        # The preview is app-computed display, never authority: answering
        # still approves exactly the ApprovalRequest below.
        if preview is not None:
            for line in preview.detail_lines:
                output_fn(f"    {line}")
            if preview.truncated:
                output_fn("    [preview truncated]")
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


def _persona_paths() -> PersonaPaths:
    return PersonaPaths(persona_directory())


def open_persona_editor(output_fn: Callable[[str], None] = print) -> int:
    """Create persona.md from the commented skeleton if needed, then edit it.

    This is the human giving a direct order, not a model proposal, so it
    bypasses the approval boundary by design; the file it writes is still
    style data that the runtime places under the hard invariant.
    """

    paths = _persona_paths()
    ensure_persona_directory(paths)
    if not paths.persona.exists():
        write_persona_text(paths, PERSONA_SKELETON)
    editor = (
        os.environ.get("VISUAL", "").strip()
        or os.environ.get("EDITOR", "").strip()
        or "vi"
    )
    try:
        subprocess.run(
            [*shlex.split(editor), str(paths.persona)], check=False
        )
    except OSError:
        output_fn(
            f"Could not start {editor}. "
            f"Your persona is at {paths.persona}."
        )
        return 1
    return 0


def apply_persona_preset(
    name: str,
    force: bool = False,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Write a starter persona from a named preset template."""

    paths = _persona_paths()
    if paths.persona.exists() and not force:
        output_fn(
            f"A persona already exists at {paths.persona}. "
            "Pass --force to replace it with the preset."
        )
        return 1
    write_persona_text(paths, PRESET_TEMPLATES[name])
    output_fn(
        f"Wrote the {name} preset to {paths.persona}. "
        "Edit it any time with 'stella persona'."
    )
    return 0


def _mark_persona_onboarding_skipped(paths: PersonaPaths) -> None:
    ensure_persona_directory(paths)
    try:
        (paths.directory / ONBOARDING_SKIP_MARKER).touch()
    except OSError:
        pass


def run_persona_onboarding(
    stella: Stella,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """Draft a first persona from three questions; the user keeps veto power.

    The user supplies taste, not prose: the answers go to the configured
    LLM, the draft is only displayed, and nothing is written until the
    user says so. Declining (or any failure) records a marker so Stella
    never nags twice; delete it to be asked again.
    """

    paths = _persona_paths()
    if paths.persona.exists() or (
        paths.directory / ONBOARDING_SKIP_MARKER
    ).exists():
        return
    output_fn(
        "Stella has no persona yet. Three quick questions and Stella "
        "will draft one; leave the first answer empty to skip forever."
    )
    answers: list[tuple[str, str]] = []
    try:
        for index, question in enumerate(ONBOARDING_QUESTIONS):
            answer = input_fn(f"{question}\n> ").strip()
            if not answer and index == 0:
                _mark_persona_onboarding_skipped(paths)
                output_fn("No persona saved; Stella keeps the default voice.")
                return
            answers.append((question, answer or "Your call."))
    except (EOFError, KeyboardInterrupt):
        _mark_persona_onboarding_skipped(paths)
        output_fn("No persona saved; Stella keeps the default voice.")
        return
    prompt = "\n".join(f"Q: {question}\nA: {answer}" for question, answer in answers)
    try:
        draft = stella.llm.chat(
            [
                Message(role="system", content=PERSONA_DRAFT_SYSTEM),
                Message(role="user", content=prompt),
            ]
        )
    except Exception as error:  # noqa: BLE001 - drafting is optional polish
        detail = " ".join(str(error).split()) or type(error).__name__
        _mark_persona_onboarding_skipped(paths)
        output_fn(
            f"Stella could not draft a persona ({detail[:120]}). "
            "Run 'stella persona' to write one by hand."
        )
        return
    output_fn("Here is Stella's draft persona:\n")
    output_fn(draft.strip())
    output_fn("")
    try:
        choice = (
            input_fn(
                "Save this persona? Type 'yes' to save, 'edit' to save "
                "then open your editor, or anything else to skip: "
            )
            .strip()
            .casefold()
        )
    except (EOFError, KeyboardInterrupt):
        choice = ""
    if choice == "yes":
        write_persona_text(paths, draft)
        output_fn(f"Persona saved to {paths.persona}.")
    elif choice == "edit":
        write_persona_text(paths, draft)
        output_fn(f"Persona saved to {paths.persona}; opening your editor.")
        open_persona_editor(output_fn)
    else:
        _mark_persona_onboarding_skipped(paths)
        output_fn("No persona saved; Stella keeps the default voice.")


def run_persona_reflection(output_fn: Callable[[str], None] = print) -> int:
    """One offline reflection pass over recorded transcripts (cron-safe).

    Reads only what the opt-in transcript database holds, proposes at
    most two addons edits, and queues them as approval material: this
    command never writes a persona file.
    """

    settings = resolve_settings()
    if settings is None:
        output_fn(
            "Stella is not configured yet, so there are no transcripts "
            "to reflect on. Run 'stella-ui' once first."
        )
        return 0
    if not settings.transcripts_enabled:
        output_fn(
            "Transcript recording is off, so there is no observed "
            "behavior to learn from. Turn it on in Settings (or "
            "with STELLA_TRANSCRIPTS=1) and use Stella for a while."
        )
        return 0
    application = build_application(settings)
    try:
        transcripts = application.session.transcripts
        if not isinstance(transcripts, TranscriptRecorder):  # pragma: no cover
            output_fn("The transcript store is unavailable; nothing ran.")
            return 1
        reflection = PersonaReflection(
            PersonaPaths(persona_directory()),
            transcripts,
            application.proposals,
            application.session.stella.llm,
        )
        outcome = reflection.run()
        for line in outcome.signals:
            output_fn(f"signal: {line}")
        if outcome.reason is not None:
            output_fn(f"{outcome.reason.capitalize()}.")
        if outcome.queued:
            output_fn(
                f"Queued {outcome.queued} persona style proposal(s); you "
                "will be asked to approve them in your next Stella "
                "session. Nothing was written."
            )
        if outcome.rejected:
            output_fn(
                f"Rejected {outcome.rejected} candidate edit(s) that "
                "failed the style, cap, or authority checks."
            )
        if not outcome.queued and outcome.reason is None and not outcome.rejected:
            output_fn("The model proposed no style edits this run.")
        return 0
    finally:
        application.close()


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch the stella command: chat by default, persona subcommands."""

    parser = argparse.ArgumentParser(
        description="Stella: a local AI assistant. With no arguments, "
        "starts an interactive session."
    )
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
    commands = parser.add_subparsers(dest="command")
    chat_parser = commands.add_parser(
        "chat", help="start an interactive session (the default)"
    )
    chat_parser.add_argument(
        "--debug",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    chat_parser.add_argument(
        "--trace",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    persona_parser = commands.add_parser(
        "persona",
        help="create or edit Stella's persona files (opens $EDITOR)",
    )
    persona_commands = persona_parser.add_subparsers(dest="persona_command")
    preset_parser = persona_commands.add_parser(
        "preset", help="start persona.md from a preset template"
    )
    preset_parser.add_argument(
        "name", choices=tuple(sorted(PRESET_TEMPLATES)), help="preset to write"
    )
    preset_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing persona.md",
    )
    commands.add_parser(
        "reflect",
        help=(
            "review recorded transcripts and queue persona style "
            "proposals for the next session (never writes anything)"
        ),
    )
    args = parser.parse_args(argv)
    if args.command == "persona":
        if args.persona_command == "preset":
            raise SystemExit(apply_persona_preset(args.name, force=args.force))
        raise SystemExit(open_persona_editor())
    if args.command == "reflect":
        raise SystemExit(run_persona_reflection())
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
        run_persona_onboarding(application.session.stella)
        run_cli(
            application.session.stella,
            debug=getattr(args, "debug", False),
            trace=getattr(args, "trace", False),
            persona_proposals=application.proposals,
            session=application.session,
        )
    finally:
        application.close()
