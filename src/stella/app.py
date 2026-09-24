"""Shared application layer used by both the CLI and the graphical UI.

The UI is an interface to Stella, never a second Stella: every turn,
memory edit, and reminder change here flows through the same trusted core
(``Stella``, ``ToolDispatcher``, ``Memory``, ``ReminderStore``) that the CLI
already uses. This module adds no new authorization path; it only reuses
existing trusted operations, serialises access onto one worker thread, and
maps existing ``ToolResult``/``ActionReceipt`` semantics onto honest
user-facing statuses.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import queue
import shlex
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from stella.audio import TranscriptionProvider
from stella.audio_output import SpeechProvider
from stella.brain import LLMBrain
from stella.context import (
    MAX_INPUT_CONTENT_CHARS,
    Context,
    InputModality,
    InputPart,
    InputProvenance,
)
from stella.history import SQLiteActionHistory
from stella.llm import Message
from stella.memory import Memory, MemoryItem, SQLiteMemory
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL, OllamaLLMClient
from stella.openai_client import OpenAILLMClient
from stella.persona import (
    PersonaLoader,
    ReflectionStore,
    TranscriptRecorder,
)
from stella.reminders import ReminderStore, SQLiteReminderStore
from stella.stella import ReminderDelivery, Stella, StellaResult
from stella.tools import (
    MAX_AUDIT_RECORDS,
    ActionPreview,
    ApprovalRequest,
    AuditRecord,
    DateTimeTool,
    FileSystemDeleteTool,
    FileSystemEditTool,
    FileSystemReadTool,
    FileSystemWriteTool,
    MemoryForgetTool,
    MemoryListTool,
    MemoryUpdateTool,
    MemoryWriteTool,
    NetworkReadTool,
    PersonaEditTool,
    ReminderCancelTool,
    ReminderCreateTool,
    ReminderListTool,
    SystemInfoTool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
    WorkspaceFindTool,
    WorkspaceListTool,
    WorkspaceSearchTool,
)
from stella.voice import (
    CommandSpeechProvider,
    CommandTranscriptionProvider,
    OpenAISpeechProvider,
    OpenAITranscriptionProvider,
    Player,
    Recorder,
    SubprocessPlayer,
    SubprocessRecorder,
    VoiceError,
)

__all__ = [
    "ApprovalBroker",
    "MemoryPanel",
    "OutcomeStatus",
    "ReminderPanel",
    "StellaApplication",
    "StellaBridge",
    "StellaSession",
    "StellaSettings",
    "TurnOutcome",
    "UiEvent",
    "VoicePanel",
    "build_application",
    "display_response",
    "outcome_status",
]


def display_response(result: StellaResult) -> str | None:
    """Return the user-facing response text for one turn, if any."""

    if result.response is not None:
        return result.response
    if result.tool_result is not None:
        return result.tool_result.output
    if result.needs_more_information:
        return "I need more information."
    return None


@dataclass(frozen=True)
class OutcomeStatus:
    """One honest user-facing status derived from trusted result data.

    ``kind`` is exactly one of: ``verified``, ``unverified``,
    ``inconclusive``, ``failed``, ``missing``, ``invalid``, ``denied``,
    ``succeeded`` (a success that needs no state verification), or ``none``.
    Only ``verified`` and ``succeeded`` carry the success symbol; an
    unverified mutation never renders as a verified success.
    """

    kind: str
    symbol: str
    detail: str


def outcome_status(result: ToolResult | None) -> OutcomeStatus:
    """Map a ``ToolResult`` (and its receipt) onto an ``OutcomeStatus``."""

    if result is None:
        return OutcomeStatus("none", "", "Nothing was executed.")
    if not result.success:
        if result.output in {"Approval required.", "Approval denied."}:
            return OutcomeStatus(
                "denied", "✗", "The action was denied. Nothing was changed."
            )
        return OutcomeStatus(
            "failed", "✗", result.output or "The action failed."
        )
    receipt = result.action_receipt
    if receipt is None:
        return OutcomeStatus("succeeded", "✓", result.output)
    if receipt.status == "verified":
        return OutcomeStatus("verified", "✓", result.output)
    if receipt.status == "unverified":
        return OutcomeStatus("unverified", "✗", result.output)
    if receipt.status == "inconclusive":
        return OutcomeStatus("inconclusive", "?", result.output)
    # Remaining documented receipt statuses are honest failures.
    return OutcomeStatus(receipt.status, "✗", result.output)


def _history_outcome(record: AuditRecord) -> str:
    """One honest outcome word for a History row.

    Denied and "approval required" both arrive as an ungranted approval
    (the trail keeps the same fields for both), so they share a word
    rather than claim a distinction the record cannot make.
    """

    if record.approval_granted is False:
        return "not approved"
    if record.execution_success:
        receipt = record.action_receipt
        if receipt is not None and receipt.status == "verified":
            return "done · verified"
        return "done"
    receipt = record.action_receipt
    return f"failed · {receipt.status}" if receipt is not None else "failed"


@dataclass(frozen=True)
class TurnOutcome:
    """The bounded result of one conversation turn."""

    result: StellaResult | None = None
    response: str | None = None
    error_message: str | None = None
    interrupted: bool = False
    cancelled: bool = False
    # Wall-clock seconds the turn consumed; None only when the turn
    # never started measuring. Display material (A6): slow answers
    # should read as "local model", not "broken".
    duration_seconds: float | None = None


class StellaSession:
    """One continuous conversation shared by the CLI and the UI.

    It only orchestrates calls that the CLI already made inline: the
    trusted due-reminder check (per interaction, plus the UI bridge's
    idle tick) and ``Stella.process``.
    """

    def __init__(
        self,
        stella: Stella,
        error_footer: str = "try again or type 'exit' to quit.",
        transcripts: TranscriptRecorder | None = None,
    ) -> None:
        self.stella = stella
        self.history: list[Message] = []
        self._error_footer = error_footer
        # Opt-in observer for `stella reflect`: it records, it never
        # influences a turn. None (the default) means no recording.
        self.transcripts = transcripts

    def check_due_reminders(
        self, now: dt.datetime | None = None
    ) -> tuple[ReminderDelivery, ...]:
        if not isinstance(self.stella, Stella):
            # Minimal test or embedding stubs may not carry the reminder flow.
            return ()
        if now is None:
            now = dt.datetime.now(dt.UTC)
        return self.stella.check_due_reminders(now)

    def run_turn(
        self,
        user_input: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TurnOutcome:
        started = time.monotonic()

        def timed(**fields: object) -> TurnOutcome:
            return TurnOutcome(
                duration_seconds=time.monotonic() - started, **fields
            )

        try:
            context = Context(
                user_input=user_input,
                conversation_history=list(self.history),
            )
            result = (
                self.stella.process(context)
                if should_cancel is None
                # Only applications that understand cancellation receive
                # it; the keyword never reaches embedding test stubs.
                else self.stella.process(context, should_cancel=should_cancel)
            )
        except KeyboardInterrupt:
            self._record_turn(user_input, cancelled=True)
            return timed(interrupted=True)
        except Exception as error:  # noqa: BLE001 - keep the session alive
            detail = " ".join(str(error).split()) or type(error).__name__
            return timed(
                error_message=(
                    f"Stella could not finish that request "
                    f"({detail[:160]}). Nothing was changed; "
                    f"{self._error_footer}"
                )
            )
        if getattr(result, "cancelled", False):
            # A cancelled turn is discarded whole: nothing is appended to
            # the conversation history, so the next turn never "remembers"
            # an answer that was never given.
            self._record_turn(user_input, cancelled=True)
            return timed(result=result, cancelled=True)
        response = display_response(result)
        self.history.append(Message(role="user", content=user_input))
        if response is not None:
            self.history.append(Message(role="assistant", content=response))
        self._record_turn(user_input, response=response)
        return timed(result=result, response=response)

    def _record_turn(
        self,
        user_input: str,
        response: str | None = None,
        cancelled: bool = False,
    ) -> None:
        if self.transcripts is None:
            return
        self.transcripts.record_turn(
            user_input, response=response, cancelled=cancelled
        )


VOICE_MODES = {"auto", "openai", "off"}
TRANSCRIPT_ENV_ON = {"1", "true", "on", "yes"}
TRANSCRIPT_ENV_OFF = {"0", "false", "off", "no"}


def transcripts_env_override() -> bool | None:
    """The STELLA_TRANSCRIPTS override, or None when it says nothing.

    Recording conversation text is opt-in: the environment variable only
    ever overrides the saved choice, it never turns recording on by
    default.
    """

    raw = os.environ.get("STELLA_TRANSCRIPTS", "").strip().casefold()
    if raw in TRANSCRIPT_ENV_ON:
        return True
    if raw in TRANSCRIPT_ENV_OFF:
        return False
    return None


def default_data_dir() -> Path:
    """Stella's persistent-state directory following the XDG base spec.

    Desktop launchers start applications from an arbitrary working
    directory, so state must not be relative to the current directory or
    restarting would appear to lose memory and reminders.
    """

    return (
        Path(
            os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~/.local/share")
        )
        / "stella"
    )


def default_memory_db() -> str:
    return str(default_data_dir() / "stella_memory.db")


def default_reminders_db() -> str:
    return str(default_data_dir() / "stella_reminders.db")


def default_history_db() -> str:
    return str(default_data_dir() / "stella_action_history.db")


def default_transcripts_db() -> str:
    return str(default_data_dir() / "stella_transcript.db")


def default_workspace() -> str:
    return str(default_data_dir() / "workspace")


@dataclass(frozen=True)
class StellaSettings:
    """The minimal local configuration a Stella application needs."""

    provider: str = "openai"
    model: str | None = None
    openai_base_url: str | None = None
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    memory_db: str = field(default_factory=default_memory_db)
    reminders_db: str = field(default_factory=default_reminders_db)
    history_db: str = field(default_factory=default_history_db)
    transcripts_db: str = field(default_factory=default_transcripts_db)
    transcripts_enabled: bool = False
    workspace: str = field(default_factory=default_workspace)
    voice_transcription: str = "auto"
    voice_speech: str = "auto"
    transcription_model: str = "whisper-1"
    transcription_command: str | None = None
    speech_model: str = "tts-1"
    speech_voice: str = "alloy"
    speech_command: str | None = None

    @staticmethod
    def _environment_fields() -> dict:
        """Settings fields that always come from the environment.

        Shared by the environment path and the saved-configuration path so
        advanced overrides (databases, workspace, voice) keep working no
        matter how Stella was configured.
        """

        transcription_mode = os.environ.get(
            "STELLA_VOICE_TRANSCRIPTION", "auto"
        ).casefold()
        speech_mode = os.environ.get("STELLA_VOICE_SPEECH", "auto").casefold()
        if transcription_mode not in VOICE_MODES:
            raise SystemExit(
                "STELLA_VOICE_TRANSCRIPTION must be 'auto', 'openai' or 'off'"
            )
        if speech_mode not in VOICE_MODES:
            raise SystemExit(
                "STELLA_VOICE_SPEECH must be 'auto', 'openai' or 'off'"
            )
        return {
            "memory_db": os.environ.get(
                "STELLA_MEMORY_DB", default_memory_db()
            ),
            "reminders_db": os.environ.get(
                "STELLA_REMINDERS_DB", default_reminders_db()
            ),
            "history_db": os.environ.get(
                "STELLA_HISTORY_DB", default_history_db()
            ),
            "transcripts_db": os.environ.get(
                "STELLA_TRANSCRIPT_DB", default_transcripts_db()
            ),
            "workspace": os.environ.get(
                "STELLA_WORKSPACE", default_workspace()
            ),
            "voice_transcription": transcription_mode,
            "voice_speech": speech_mode,
            "transcription_model": os.environ.get(
                "STELLA_TRANSCRIPTION_MODEL", "whisper-1"
            ),
            "transcription_command": os.environ.get(
                "STELLA_TRANSCRIPTION_COMMAND"
            ),
            "speech_model": os.environ.get("STELLA_SPEECH_MODEL", "tts-1"),
            "speech_voice": os.environ.get("STELLA_SPEECH_VOICE", "alloy"),
            "speech_command": os.environ.get("STELLA_SPEECH_COMMAND"),
        }

    @classmethod
    def from_saved(
        cls,
        *,
        provider: str,
        model: str,
        ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL,
        openai_base_url: str | None = None,
        transcripts_enabled: bool = False,
    ) -> StellaSettings:
        """Settings from the saved first-run configuration."""

        override = transcripts_env_override()
        return cls(
            provider=provider,
            model=model,
            ollama_base_url=ollama_base_url,
            openai_base_url=openai_base_url,
            transcripts_enabled=(
                transcripts_enabled if override is None else override
            ),
            **cls._environment_fields(),
        )

    @classmethod
    def from_environment(cls) -> StellaSettings:
        """Read the same environment variables the CLI has always used."""

        model = os.environ.get("STELLA_MODEL")
        if not model:
            raise SystemExit("STELLA_MODEL is required")
        provider = os.environ.get("STELLA_LLM_PROVIDER")
        if provider is None:
            # First-run default: use the cloud only when it is configured,
            # otherwise fall back to a local Ollama model.
            provider = "openai" if os.environ.get("OPENAI_API_KEY") else "ollama"
        provider = provider.casefold()
        if provider not in {"openai", "ollama"}:
            raise SystemExit(
                "STELLA_LLM_PROVIDER must be 'openai' or 'ollama'"
            )
        return cls(
            provider=provider,
            model=model,
            openai_base_url=os.environ.get("OPENAI_BASE_URL"),
            ollama_base_url=os.environ.get(
                "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
            ),
            transcripts_enabled=transcripts_env_override() is True,
            **cls._environment_fields(),
        )


@dataclass
class StellaApplication:
    """A built Stella core plus the session and settings that own it."""

    session: StellaSession
    settings: StellaSettings
    voice: VoicePanel | None = None
    proposals: ReflectionStore | None = None

    def close(self) -> None:
        if self.voice is not None:
            self.voice.dispose()
        memory = self.session.stella.memory
        if isinstance(memory, SQLiteMemory):
            memory.close()
        reminders = self.session.stella.reminders
        if isinstance(reminders, SQLiteReminderStore):
            reminders.close()
        history = self.session.stella.tools.history
        if isinstance(history, SQLiteActionHistory):
            history.close()
        if isinstance(self.session.transcripts, TranscriptRecorder):
            self.session.transcripts.close()
        if self.proposals is not None:
            self.proposals.close()


def build_application(settings: StellaSettings) -> StellaApplication:
    """Construct the trusted Stella core exactly like the CLI does."""

    if not settings.model:
        raise SystemExit("STELLA_MODEL is required")
    if settings.provider == "ollama":
        # The compatibility endpoint ignores per-request options on Ollama
        # 0.33.x; native /api/chat is the only way to apply num_ctx=4096,
        # which keeps the model fully on GPU (measured ~3x faster turns).
        llm = OllamaLLMClient(
            model=settings.model,
            base_url=settings.ollama_base_url,
            native=True,
            num_ctx=4096,
        )
    elif settings.provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit(
                "No OpenAI API key is configured. Enter it in Settings, "
                "or export OPENAI_API_KEY before launching."
            )
        llm = OpenAILLMClient(
            model=settings.model,
            base_url=settings.openai_base_url,
        )
    else:
        raise SystemExit("STELLA_LLM_PROVIDER must be 'openai' or 'ollama'")
    # Desktop launchers may start us from an arbitrary directory, and the
    # default state lives under XDG paths that do not exist on first run,
    # so ensure every configured location exists before opening it.
    Path(settings.memory_db).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.reminders_db).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.history_db).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.transcripts_db).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.workspace).mkdir(parents=True, exist_ok=True)
    memory = SQLiteMemory(settings.memory_db)
    reminders = SQLiteReminderStore(settings.reminders_db)
    history = SQLiteActionHistory(settings.history_db, MAX_AUDIT_RECORDS)
    # Recording conversation text is strictly opt-in; the proposal queue
    # beside it is always available so a queued edit survives turning
    # recording back off.
    transcripts = TranscriptRecorder(settings.transcripts_db)
    proposals = ReflectionStore(settings.transcripts_db)
    workspace = settings.workspace
    tools = ToolDispatcher(
        [
            DateTimeTool(),
            SystemInfoTool(),
            # No EchoTool here on purpose: a registered echo capability lets
            # a confused model "succeed" by echoing the user's own text,
            # which reads as a fake assistant response ("iawd" dogfood).
            FileSystemReadTool(workspace),
            FileSystemWriteTool(workspace),
            FileSystemEditTool(workspace),
            FileSystemDeleteTool(workspace),
            WorkspaceListTool(workspace),
            WorkspaceFindTool(workspace),
            WorkspaceSearchTool(workspace),
            NetworkReadTool(),
            MemoryListTool(memory),
            MemoryWriteTool(memory),
            MemoryUpdateTool(memory),
            MemoryForgetTool(memory),
            ReminderCreateTool(reminders),
            ReminderListTool(reminders),
            ReminderCancelTool(reminders),
            # Style data with its own write path: exactly the two persona
            # files, DANGEROUS, verified like every other mutation.
            PersonaEditTool(),
        ],
        history=history,
    )
    stella = Stella(
        brain=LLMBrain(llm, tools, persona=PersonaLoader()),
        llm=llm,
        tool=tools,
        memory=memory,
        max_tool_steps=2,
        reminders=reminders,
    )
    # The shared application backs the desktop UI too, so its session must
    # not quote CLI-only instructions ("type 'exit'") in UI error messages.
    # The interactive CLI loop builds its own StellaSession with the hint.
    return StellaApplication(
        StellaSession(
            stella,
            error_footer="try again.",
            transcripts=(transcripts if settings.transcripts_enabled else None),
        ),
        settings,
        build_voice(settings),
        proposals=proposals,
    )


def drain_persona_proposals(
    stella: Stella,
    store: ReflectionStore | None,
    notify: Callable[[str], None] | None = None,
) -> int:
    """Surface queued reflection proposals as real approvals, one by one.

    A proposal is only a stored persona_edit argument set: answering it
    travels the exact same dispatcher path as a mid-conversation tool
    call (exact-match approval, validation, verified receipt, audit
    trail), and a denial or a missing approval provider changes nothing.
    """

    if store is None:
        return 0
    dispatcher = getattr(stella, "tools", None)
    provider = getattr(stella, "approval_provider", None)
    if provider is None or dispatcher is None:
        # No approver is present (batch or stub context): proposals stay
        # queued for the next interactive session.
        return 0
    handled = 0
    for proposal in store.pending():
        arguments = dict(proposal.arguments)
        request = ApprovalRequest("persona_edit", arguments)
        preview = dispatcher.preview("persona_edit", arguments)
        approval = provider(request, preview)
        result = dispatcher.execute(
            "persona_edit", arguments, approval=approval
        )
        store.resolve(
            proposal.id,
            approved=bool(approval.approved),
            success=bool(result.success),
        )
        if notify is not None:
            reason = arguments.get("summary") or "a persona style proposal"
            notify(f"Persona proposal ({reason}): {result.output}")
        handled += 1
    return handled


class VoicePanel:
    """Trusted application-layer orchestration of the voice periphery.

    It owns the recorder, transcriber, speech provider, and player, and
    turns them into one-shot commands the bridge can post. A transcript
    leaves this panel only as ordinary text, which the caller then feeds
    through the exact same ``StellaSession.run_turn`` path as typed input:
    voice gains no reasoning path, no approval, and no authority here.
    Recordings are removed as soon as transcription is done.
    """

    def __init__(
        self,
        recorder: Recorder | None,
        player: Player | None,
        transcriber: TranscriptionProvider | None,
        speech_provider: SpeechProvider | None,
        input_notice: str | None = None,
        output_notice: str | None = None,
    ) -> None:
        self._recorder = recorder
        self._player = player
        self._transcriber = transcriber
        self._speech = speech_provider
        self._input_notice = input_notice
        self._output_notice = output_notice
        self.speech_enabled = False

    @property
    def input_available(self) -> bool:
        return (
            self._recorder is not None
            and self._transcriber is not None
            and self._recorder.available()
        )

    @property
    def output_available(self) -> bool:
        return (
            self._player is not None
            and self._speech is not None
            and self._player.available()
        )

    def start_listening(self) -> None:
        if self._recorder is None or self._transcriber is None:
            raise VoiceError(
                self._input_notice
                or "Voice input is not available in this configuration."
            )
        self._recorder.start()

    def stop_and_transcribe(self) -> str:
        """Finish one recording and return its transcript, or raise."""

        if self._recorder is None or self._transcriber is None:
            raise VoiceError(
                self._input_notice
                or "Voice input is not available in this configuration."
            )
        try:
            path = self._recorder.stop()
            # The audio part carries only the bounded temporary reference;
            # raw audio never enters the conversation or history.
            part = InputPart(
                modality=InputModality.AUDIO,
                provenance=InputProvenance.USER,
                reference=path,
            )
            transcript = self._transcriber.transcribe(part)
        except VoiceError:
            raise
        except Exception as error:  # friendly text, never a trace
            detail = " ".join(str(error).split()) or type(error).__name__
            raise VoiceError(
                f"Transcription failed ({detail[:120]}). Nothing was sent "
                "to Stella."
            ) from error
        finally:
            self._recorder.dispose()
        if not isinstance(transcript, str) or not transcript.strip():
            raise VoiceError(
                "No speech was recognized. Nothing was sent to Stella."
            )
        return transcript.strip()[:MAX_INPUT_CONTENT_CHARS]

    def abandon_listening(self) -> None:
        if self._recorder is not None:
            self._recorder.cancel()

    def synthesize(self, result: StellaResult) -> str:
        """Render one existing final response and return its artifact."""

        if self._speech is None:
            raise VoiceError(
                self._output_notice
                or "Voice output is not available right now."
            )
        artifact = Stella.speak(result, self._speech)
        return artifact.reference

    def play(self, path: str) -> None:
        if self._player is None:
            raise VoiceError("Voice output is not available right now.")
        self._player.play(path)

    def cancel_playback(self) -> None:
        """Stop audio output only; the Stella decision is untouched."""

        if self._player is not None:
            self._player.stop()

    def dispose_artifact(self, path: str) -> None:
        """Remove one played synthesis so no audio outlives its purpose."""

        try:
            os.remove(path)
        except OSError as error:
            del error

    def dispose(self) -> None:
        if self._recorder is not None:
            # cancel() also releases the capture process and its files.
            self._recorder.cancel()
        provider_dispose = getattr(self._speech, "dispose", None)
        if provider_dispose is not None:
            provider_dispose()


def build_voice(settings: StellaSettings) -> VoicePanel:
    """Assemble the local-first voice periphery; never raises at startup.

    A broken optional voice setting disables only that voice capability
    (the reason surfaces when voice is used) instead of preventing Stella
    from starting: text chat, memory, actions and reminders must survive
    a misconfigured transcription or speech command.
    """

    recorder = SubprocessRecorder()
    player = SubprocessPlayer()
    input_notice: str | None = None
    output_notice: str | None = None
    try:
        transcriber = _build_transcriber(settings)
    except Exception as error:  # noqa: BLE001 - voice is optional
        transcriber = None
        detail = " ".join(str(error).split()) or type(error).__name__
        input_notice = (
            f"Voice input is unavailable ({detail[:120]}). "
            "Fix the transcription command and restart Stella."
        )
    try:
        speech = _build_speech_provider(settings)
    except Exception as error:  # noqa: BLE001 - voice is optional
        speech = None
        detail = " ".join(str(error).split()) or type(error).__name__
        output_notice = (
            f"Voice output is unavailable ({detail[:120]}). "
            "Fix the speech command and restart Stella."
        )
    return VoicePanel(
        recorder,
        player,
        transcriber,
        speech,
        input_notice=input_notice,
        output_notice=output_notice,
    )


def _openai_speech_client() -> object:
    from openai import OpenAI

    # The SDK resolves OPENAI_API_KEY/OPENAI_BASE_URL from the environment,
    # the same credentials the ordinary LLM client already uses.
    return OpenAI()


def _build_transcriber(
    settings: StellaSettings,
) -> TranscriptionProvider | None:
    if settings.voice_transcription == "off":
        return None
    if settings.transcription_command:
        return CommandTranscriptionProvider(
            shlex.split(settings.transcription_command)
        )
    if settings.voice_transcription in {"auto", "openai"} and os.environ.get(
        "OPENAI_API_KEY"
    ):
        return OpenAITranscriptionProvider(
            _openai_speech_client(), model=settings.transcription_model
        )
    return None


def _build_speech_provider(
    settings: StellaSettings,
) -> SpeechProvider | None:
    if settings.voice_speech == "off":
        return None
    if settings.speech_command:
        return CommandSpeechProvider(shlex.split(settings.speech_command))
    if settings.voice_speech == "auto":
        for binary in ("espeak-ng", "espeak"):
            if shutil.which(binary):
                return CommandSpeechProvider(
                    [binary, "-w", "{output}", "{text}"]
                )
    if settings.voice_speech in {"auto", "openai"} and os.environ.get(
        "OPENAI_API_KEY"
    ):
        return OpenAISpeechProvider(
            _openai_speech_client(),
            model=settings.speech_model,
            voice=settings.speech_voice,
        )
    return None


class MemoryPanel:
    """Trusted application-layer view over the existing memory backend.

    Rows keep their internal ids only inside this object; the UI sees
    content strings and positional selections, never database ids.
    """

    def __init__(self, memory: Memory) -> None:
        self._memory = memory
        self._rows: list[MemoryItem] = []

    def refresh(self, query: str | None = None) -> tuple[str, ...]:
        search = query.strip() if query and query.strip() else None
        self._rows = list(self._memory.retrieve(search))
        return tuple(item.content for item in self._rows)

    def forget(self, index: int) -> str:
        if not isinstance(index, int) or not 0 <= index < len(self._rows):
            return "No memory is selected. Pick one from the list first."
        item = self._rows[index]
        if self._memory.delete(item.id):
            self._rows.pop(index)
            return "That memory was forgotten."
        return (
            "That memory could not be forgotten; it may have changed. "
            "Refresh the list and try again."
        )


class ReminderPanel:
    """Trusted application-layer view over the existing reminder store.

    Every mutation runs through the same ``Reminder*Tool`` implementations
    (and therefore the same validation and honest output wording) that the
    approved tool path uses. A reminder here remains a notification event:
    nothing on this panel can execute other tools.
    """

    def __init__(self, store: ReminderStore | None) -> None:
        self._store = store

    @property
    def available(self) -> bool:
        return self._store is not None

    def pending_rows(self) -> tuple[tuple[str, str], ...]:
        """Pending reminders as (content, due-time) display pairs."""

        if self._store is None:
            return ()
        return tuple(
            (reminder.content, reminder.due_at.isoformat())
            for reminder in self._store.pending()
        )

    def create(self, content: str, due_at_iso: str) -> ToolResult:
        if self._store is None:
            return self._unavailable()
        return ReminderCreateTool(self._store).execute(
            {"content": content, "due_at": due_at_iso}
        )

    def cancel(self, query: str) -> ToolResult:
        if self._store is None:
            return self._unavailable()
        return ReminderCancelTool(self._store).execute({"query": query})

    def list(self) -> ToolResult:
        if self._store is None:
            return self._unavailable()
        return ReminderListTool(self._store).execute({})

    @staticmethod
    def _unavailable() -> ToolResult:
        return ToolResult(
            success=False,
            output="Reminders are not available in this configuration.",
        )


class ApprovalBroker:
    """Bridge between the dispatcher's approval calls and a UI approver.

    The dispatcher invokes ``request`` with its own exact
    ``ApprovalRequest``; the broker blocks until the UI answers *that*
    request and then produces the ``ToolApproval`` itself, always attached
    to the dispatcher's original request object. A UI answer can therefore
    never approve a different capability or a different argument set, and
    unmade requests default to denial.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens = itertools.count()
        self._waiting: dict[int, _PendingApproval] = {}
        self._outstanding = queue.Queue()

    def request(
        self,
        request: ApprovalRequest,
        preview: ActionPreview | None = None,
    ) -> ToolApproval:
        token = next(self._tokens)
        pending = _PendingApproval(request, preview)
        with self._lock:
            self._waiting[token] = pending
        # The preview travels beside the request, never inside it: the
        # ToolApproval below is still produced solely from the original
        # dispatcher request, so a preview can never change what the
        # answer authorizes.
        self._outstanding.put((token, request, preview))
        pending.answered.wait()
        with self._lock:
            self._waiting.pop(token, None)
        return ToolApproval(request=request, approved=pending.approved)

    def next_request(
        self, timeout: float | None = None
    ) -> tuple[int, ApprovalRequest, ActionPreview | None] | None:
        """Pop one outstanding request; None timeout means non-blocking."""

        try:
            if timeout is None:
                return self._outstanding.get_nowait()
            return self._outstanding.get(timeout=timeout)
        except queue.Empty:
            return None

    def resolve(self, token: int, approved: bool) -> bool:
        """Answer one still-outstanding request; unknown tokens do nothing.

        The pending entry is removed under the same lock that marks it
        answered, so each token can be answered exactly once and a later
        ``resolve`` or ``deny_outstanding`` can never overwrite (or race with)
        the answer the dispatcher will observe.
        """

        with self._lock:
            pending = self._waiting.pop(token, None)
            if pending is None:
                return False
            pending.approved = approved
            pending.answered.set()
            return True

    def deny_outstanding(self) -> None:
        """Deny every unanswered request (used when the application exits)."""

        with self._lock:
            for pending in self._waiting.values():
                pending.approved = False
                pending.answered.set()
            self._waiting.clear()


@dataclass
class _PendingApproval:
    request: ApprovalRequest
    preview: ActionPreview | None = None
    answered: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


@dataclass(frozen=True)
class UiEvent:
    """One asynchronous application-layer event destined for the UI."""

    kind: str
    payload: object = None


class ReminderScheduler:
    """Wakes on an interval and asks the bridge for one due-reminder sweep.

    It owns no Stella state: the ticker thread only calls ``on_tick``,
    which posts onto the bridge's single command queue, so all reminder
    evaluation still happens on the one worker thread that owns Stella.
    """

    def __init__(
        self,
        on_tick: Callable[[], None],
        interval_seconds: float = 5.0,
    ) -> None:
        self._on_tick = on_tick
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="stella-reminder-tick", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._on_tick()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


class StellaBridge:
    """Serialise all UI requests onto one worker thread that owns Stella.

    SQLite connections are thread-bound, and Stella's own state is not
    thread-safe, so exactly one worker thread touches the application
    layer. The UI thread only posts commands and drains events; it never
    calls into ``Stella``, the dispatcher, memory, or the reminder store
    itself. Every command is wrapped so an unexpected failure becomes one
    friendly ``("error", ...)`` event instead of a stack trace.
    """

    def __init__(
        self,
        factory: Callable[[], StellaApplication],
        *,
        reminder_tick_seconds: float | None = 5.0,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.approvals = ApprovalBroker()
        self._reminder_tick_seconds = reminder_tick_seconds
        self._now = now if now is not None else (lambda: dt.datetime.now(dt.UTC))
        self._scheduler: ReminderScheduler | None = None
        self._application: StellaApplication | None = None
        self._memory: MemoryPanel | None = None
        self._reminders: ReminderPanel | None = None
        self._history_stamp: str | None = None
        self._voice: VoicePanel | None = None
        self._playback: threading.Thread | None = None
        self._commands: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._turn_cancel = threading.Event()
        self._events: queue.Queue[UiEvent] = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="stella-app", daemon=True
        )
        self._thread.start()
        # The application must be *built* on the worker thread because the
        # SQLite connections it owns are bound to their creating thread.
        self._post(lambda: self._create(factory))
        self._ready.wait()

    def _create(self, factory: Callable[[], StellaApplication]) -> None:
        try:
            application = factory()
        except BaseException as error:  # noqa: BLE001 - report, never crash
            # SystemExit included: a missing model must not kill the thread.
            detail = " ".join(str(error).split()) or type(error).__name__
            self._emit(
                "error",
                f"Stella could not start ({detail[:160]}). "
                "Nothing was changed.",
            )
            self._ready.set()
            return
        self._application = application
        self._rebind(application)
        if self._reminder_tick_seconds is not None:
            # Only a successfully started Stella gets a ticker, and it must
            # be running before _ready releases the caller: stop() from the
            # UI could otherwise catch a half-built scheduler.
            self._scheduler = ReminderScheduler(
                self.post_reminder_check, self._reminder_tick_seconds
            )
            self._scheduler.start()
        self._ready.set()

    def _rebind(self, application: StellaApplication) -> None:
        stella = application.session.stella
        if isinstance(stella, Stella):
            stella.approval_provider = self.approvals.request
        self._memory = MemoryPanel(stella.memory)
        self._reminders = ReminderPanel(
            getattr(stella, "reminders", None)
        )
        self._voice = application.voice

    def _serve(self) -> None:
        while True:
            command = self._commands.get()
            if command is None:
                return
            try:
                command()
            except BaseException as error:  # noqa: BLE001 - report, never crash
                detail = " ".join(str(error).split()) or type(error).__name__
                self._events.put(
                    UiEvent(
                        "error",
                        f"Stella could not complete that request "
                        f"({detail[:160]}). Nothing was changed.",
                    )
                )

    def _post(self, command: Callable[[], None]) -> None:
        self._commands.put(command)

    def _emit(self, kind: str, payload: object = None) -> None:
        self._events.put(UiEvent(kind, payload))

    def poll(self) -> list[UiEvent]:
        """Drain every queued event without blocking the UI thread."""

        events: list[UiEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def next_approval_request(
        self, timeout: float | None = None
    ) -> tuple[int, ApprovalRequest, ActionPreview | None] | None:
        return self.approvals.next_request(timeout)

    def resolve_approval(self, token: int, approved: bool) -> bool:
        return self.approvals.resolve(token, approved)

    def _require_session(self) -> StellaSession:
        if self._application is None:
            raise RuntimeError("Stella is not running in this session.")
        return self._application.session

    def _require_memory(self) -> MemoryPanel:
        if self._memory is None:
            raise RuntimeError("Stella is not running in this session.")
        return self._memory

    def _require_reminders(self) -> ReminderPanel:
        if self._reminders is None:
            raise RuntimeError("Stella is not running in this session.")
        return self._reminders

    def post_turn(self, user_input: str) -> None:
        def handle() -> None:
            self._speak_after(self._handle_turn(user_input))

        self._post(handle)

    def cancel_current_turn(self) -> None:
        """Ask the running turn to stop at its next safe point.

        Callable from any thread (the UI button presses it while the
        worker is busy): it only sets a flag and denies any outstanding
        approval — the canonical safe answer the broker already uses when
        the application exits. It never approves, executes, or interrupts
        an action that is already in flight.
        """

        self._turn_cancel.set()
        self.approvals.deny_outstanding()

    def _should_cancel(self) -> bool:
        return self._turn_cancel.is_set()

    def _handle_turn(self, user_input: str) -> TurnOutcome:
        session = self._require_session()
        self._turn_cancel.clear()
        self._check_due_reminders()
        outcome = session.run_turn(user_input, should_cancel=self._should_cancel)
        self._emit("turn", outcome)
        # The History panel tracks what the turn actually did without
        # waiting for the user to press Refresh; turns that touched no
        # capability change nothing and emit nothing.
        self._emit_history_if_new()
        return outcome

    def post_reminder_check(self) -> None:
        """One due-reminder sweep, run on the worker thread.

        This is the ticker's entire entry point: it posts, it never
        evaluates reminders on the ticker thread itself.
        """

        self._post(self._check_due_reminders)

    def _check_due_reminders(self) -> None:
        session = self._require_session()
        delivered = False
        for delivery in session.check_due_reminders(self._now()):
            if delivery.delivered and delivery.message is not None:
                self._emit("reminder_delivered", delivery.message)
                delivered = True
        if delivered and self._reminders is not None:
            # A handled row disappears from the Reminders panel without
            # waiting for the next user interaction.
            self._emit("reminders", self._reminders.pending_rows())

    # -------------------------------------------------------------- voice

    def voice_capabilities(self) -> tuple[bool, bool]:
        """(microphone usable, speech output usable) for the UI display.

        This only reads availability metadata (which local commands
        exist); it opens no device and touches no Stella state.
        """

        if self._voice is None:
            return (False, False)
        return (self._voice.input_available, self._voice.output_available)

    def set_speech_enabled(self, enabled: bool) -> None:
        """Toggle speaking final responses aloud (a plain display pref)."""

        if self._voice is not None:
            self._voice.speech_enabled = bool(enabled)

    def post_listen_start(self) -> None:
        def handle() -> None:
            panel = self._voice
            if panel is None:
                self._emit(
                    "voice_error",
                    "Voice input is not available in this configuration.",
                )
                return
            try:
                panel.start_listening()
            except VoiceError as error:
                self._emit("voice_error", str(error))
            else:
                # The UI shows "Listening..." only after this event: the
                # window never claims to listen when nothing is recording.
                self._emit("voice_state", "listening")

        self._post(handle)

    def post_listen_stop(self) -> None:
        def handle() -> None:
            panel = self._voice
            if panel is None:
                self._emit(
                    "voice_error",
                    "Voice input is not available in this configuration.",
                )
                return
            self._emit("voice_state", "transcribing")
            try:
                transcript = panel.stop_and_transcribe()
            except VoiceError as error:
                # A failed transcript is never replaced with invented text.
                self._emit("voice_error", str(error))
                return
            self._emit("voice_transcript", transcript)
            # From here the transcript follows the exact typed-input path.
            self._speak_after(self._handle_turn(transcript))

        self._post(handle)

    def post_listen_cancel(self) -> None:
        def handle() -> None:
            if self._voice is not None:
                self._voice.abandon_listening()
            self._emit("voice_state", "idle")

        self._post(handle)

    def stop_playback(self) -> None:
        """Cancel audio output from the UI thread, deliberately.

        Playback is a peripheral sound process, not Stella state: the
        worker may be busy elsewhere, and stopping speech must never
        cancel or alter the decision that already produced the response.
        """

        if self._voice is not None:
            self._voice.cancel_playback()

    def _speak_after(self, outcome: TurnOutcome) -> None:
        panel = self._voice
        if (
            panel is None
            or not panel.speech_enabled
            or outcome.result is None
            or outcome.response is None
            or not panel.output_available
        ):
            return
        try:
            path = panel.synthesize(outcome.result)
        except VoiceError as error:
            # The text response stays visible and untouched.
            self._emit("voice_error", str(error))
            return
        except Exception as error:  # noqa: BLE001 - friendly text, never a trace
            detail = " ".join(str(error).split()) or type(error).__name__
            self._emit(
                "voice_error",
                f"Stella could not prepare speech ({detail[:120]}). "
                "The text response is still available.",
            )
            return
        # A new reply may interrupt still-playing audio; that cancels only
        # playback, never any Stella decision. The join keeps the old
        # thread's "idle" event ordered before the new "speaking" state.
        panel.cancel_playback()
        if self._playback is not None:
            self._playback.join(timeout=2)
        self._emit("voice_state", "speaking")

        def play() -> None:
            try:
                panel.play(path)
            except VoiceError as error:
                self._emit("voice_error", str(error))
            except BaseException as error:  # noqa: BLE001 - report, never crash
                detail = " ".join(str(error).split()) or type(error).__name__
                self._emit(
                    "voice_error",
                    f"Stella could not play the response ({detail[:120]}).",
                )
            finally:
                panel.dispose_artifact(path)
                self._emit("voice_state", "idle")

        self._playback = threading.Thread(
            target=play, name="stella-playback", daemon=True
        )
        self._playback.start()

    def post_memories(self, query: str | None = None) -> None:
        def handle() -> None:
            self._emit("memories", self._require_memory().refresh(query))

        self._post(handle)

    def post_forget(self, index: int) -> None:
        def handle() -> None:
            memory = self._require_memory()
            message = memory.forget(index)
            self._emit("memory_result", message)
            self._emit("memories", memory.refresh())

        self._post(handle)

    def post_reminders(self) -> None:
        def handle() -> None:
            self._emit("reminders", self._require_reminders().pending_rows())

        self._post(handle)

    def post_reminder_add(self, content: str, due_at_iso: str) -> None:
        def handle() -> None:
            panel = self._require_reminders()
            result = panel.create(content, due_at_iso)
            self._emit("reminder_result", outcome_status(result))
            self._emit("reminders", panel.pending_rows())

        self._post(handle)

    def post_reminder_cancel(self, query: str) -> None:
        def handle() -> None:
            panel = self._require_reminders()
            result = panel.cancel(query)
            self._emit("reminder_result", outcome_status(result))
            self._emit("reminders", panel.pending_rows())

        self._post(handle)

    def post_history(self) -> None:
        """Send the newest action-history rows to the UI."""

        def handle() -> None:
            self._emit("history", self._history_rows())

        self._post(handle)

    def _emit_history_if_new(self) -> None:
        records = self._require_session().stella.tools.audit_records
        if not records:
            return
        stamp = records[-1].timestamp
        if stamp != self._history_stamp:
            self._history_stamp = stamp
            self._emit("history", self._history_rows())

    def _history_rows(self, limit: int = 50) -> tuple[str, ...]:
        """Display rows for the History panel: newest first, metadata only.

        Arguments never appear here; only the capability, an outcome word
        and the dispatch time. File contents and tool output were already
        kept out of the durable trail by the dispatcher's redaction.
        """

        records = self._require_session().stella.tools.audit_records[-limit:]
        return tuple(
            f"{record.timestamp[:16]}  "
            f"{record.capability or 'unknown'}  "
            f"{_history_outcome(record)}"
            for record in reversed(records)
        )

    def post_persona_drain(self) -> None:
        """Ask the worker to surface queued persona proposals now.

        Scheduled once after the window exists (never during startup):
        approval dialogs need the UI poll loop to be running, and the
        blocking approval happens on the worker thread like any turn.
        """

        def handle() -> None:
            application = self._application
            if application is None:
                return
            drain_persona_proposals(
                application.session.stella,
                application.proposals,
                notify=lambda message: self._emit("notice", message),
            )

        self._post(handle)

    def post_apply_settings(self, settings: StellaSettings) -> None:
        def handle() -> None:
            # Build first so a bad configuration cannot destroy the
            # working session; only then retire the old application and
            # persist the new choice for the next launch.
            application = build_application(settings)
            old = self._application
            self._application = application
            self._rebind(application)
            if old is not None:
                old.close()
            # Imported here: stella.config imports this module, so a
            # module-level import both ways would be circular.
            from stella.config import save_configuration

            save_configuration(settings)
            self._emit(
                "settings",
                (
                    f"Stella now uses {settings.provider}/"
                    f"{settings.model} with workspace {settings.workspace}. "
                    "These settings are saved for the next launch."
                ),
            )

        self._post(handle)

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.stop()
            self._scheduler = None
        self.approvals.deny_outstanding()
        if self._voice is not None:
            self._voice.cancel_playback()
        self._post(None)
        self._thread.join(timeout=5)
        if self._playback is not None:
            self._playback.join(timeout=2)
        if self._application is not None:
            try:
                self._application.close()
            except Exception as error:  # noqa: BLE001 - never raise in shutdown
                del error
