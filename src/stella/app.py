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
import random
import shlex
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from stella.audio import TranscriptionProvider
from stella.audio_output import (
    SpeechOutput,
    SpeechProvider,
    sentence_chunks,
)
from stella.barge_in import BargeInListener, SileroVad, capture_command
from stella.brain import LLMBrain
from stella.context import (
    MAX_INPUT_CONTENT_CHARS,
    Context,
    InputEnvelope,
    InputModality,
    InputPart,
    InputProvenance,
)
from stella.history import SQLiteActionHistory
from stella.llama_server import (
    DEFAULT_LLAMA_SERVER_BINARY,
    DEFAULT_LLAMA_SERVER_PORT,
    LlamaBrainServer,
    LlamaServerLLMClient,
)
from stella.llm import (
    CancelCheck,
    Message,
    ProviderRequestCancelled,
    run_cancellable,
)
from stella.memory import Memory, MemoryItem, SQLiteMemory
from stella.minilm_embedding import (
    MiniLMEmbeddingProvider,
    minilm_extra_available,
)
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL, OllamaLLMClient
from stella.ollama_embedding import OllamaEmbeddingProvider
from stella.openai_client import OpenAILLMClient
from stella.os_tools import build_desktop_tools
from stella.outline_tools import build_outline_tools
from stella.persona import (
    PersonaLoader,
    ReflectionStore,
    TranscriptRecorder,
)
from stella.reminders import ReminderStore, SQLiteReminderStore
from stella.semantic_memory import (
    EmbeddingProvider,
    LocalHashEmbeddingProvider,
    SemanticRetriever,
    SQLiteSemanticIndex,
    reconcile_semantic_index,
)
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
    ResidentSpeechProvider,
    SubprocessPlayer,
    SubprocessRecorder,
    VoiceError,
)
from stella.web_tools import build_web_tools

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
        on_activity: Callable[[str], None] | None = None,
        spoken: bool = False,
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
                # A spoken conversation turn declares its audio modality
                # here — the only place that knows the answer will be
                # heard. The transcript text is the content; the discarded
                # recording never enters the conversation.
                input_envelope=(
                    InputEnvelope(
                        (
                            InputPart(
                                modality=InputModality.AUDIO,
                                provenance=InputProvenance.USER,
                                content=user_input,
                            ),
                        )
                    )
                    if spoken
                    else None
                ),
            )
            options: dict[str, object] = {}
            if should_cancel is not None:
                options["should_cancel"] = should_cancel
            if on_activity is not None:
                options["on_activity"] = on_activity
            # Keywords appear only when set: applications (and embedding
            # test stubs) that understand neither still get process(context).
            result = self.stella.process(context, **options)
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
BARGE_MODES = {"auto", "off", "on"}
ENV_ON = {"1", "true", "on", "yes"}
ENV_OFF = {"0", "false", "off", "no"}


def _env_toggle(name: str) -> bool | None:
    """A general on/off environment toggle, or None when it says nothing."""

    raw = os.environ.get(name, "").strip().casefold()
    if raw in ENV_ON:
        return True
    if raw in ENV_OFF:
        return False
    return None


def transcripts_env_override() -> bool | None:
    """The STELLA_TRANSCRIPTS override, or None when it says nothing.

    Recording conversation text is opt-in: the environment variable only
    ever overrides the saved choice, it never turns recording on by
    default.
    """

    return _env_toggle("STELLA_TRANSCRIPTS")


def semantic_env_override() -> bool | None:
    """The STELLA_SEMANTIC_MEMORY override, or None when it says nothing.

    The semantic index duplicates every stored memory's content into a
    second local database, so building it is strictly opt-in; this only
    overrides the saved choice, never enables by default.
    """

    return _env_toggle("STELLA_SEMANTIC_MEMORY")


def os_tools_env_override() -> bool | None:
    """The STELLA_OS_TOOLS override, or None when it says nothing.

    Desktop tools see and touch the whole screen, so they are strictly
    opt-in; registration is additionally gated on a real Hyprland
    session (stella.os_tools), never on this flag alone.
    """

    return _env_toggle("STELLA_OS_TOOLS")


def outline_tools_env_override() -> bool | None:
    """The STELLA_OUTLINE override, or None when it says nothing.

    These tools read and modify the user's Outline app over its local
    API; they are strictly opt-in, and registration is additionally
    gated on a reachable Outline server (stella.outline_tools).
    """

    return _env_toggle("STELLA_OUTLINE")


def web_tools_env_override() -> bool | None:
    """The STELLA_WEB override, or None when it says nothing.

    These tools send data off the machine (queries to a search backend,
    fetches to arbitrary sites); they are strictly opt-in. Unlike the
    Outline tools there is no reachability probe — the keyless path
    degrades to a structured "web is off" instead (report 22).
    """

    return _env_toggle("STELLA_WEB")


SEMANTIC_PROVIDERS = frozenset({"local-hash", "ollama", "minilm"})


def semantic_provider_env_override() -> str | None:
    """The STELLA_SEMANTIC_PROVIDER override, or None when it says nothing.

    The embedding provider is an explicit choice; an invalid name fails
    loudly rather than silently falling back to another model's vectors.
    """

    raw = os.environ.get("STELLA_SEMANTIC_PROVIDER", "").strip().casefold()
    if not raw:
        return None
    if raw not in SEMANTIC_PROVIDERS:
        raise SystemExit(
            "STELLA_SEMANTIC_PROVIDER must be "
            + ", ".join(sorted(SEMANTIC_PROVIDERS))
        )
    return raw


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


def default_semantic_db() -> str:
    return str(default_data_dir() / "stella_semantic_index.db")


def default_workspace() -> str:
    return str(default_data_dir() / "workspace")


def default_vad_model() -> str:
    """Where the small Silero VAD ONNX file lives on this machine.

    A ~2 MB model file, not a pip package: installing ``silero-vad``
    would drag CUDA torch in with it (research report 08 measured that
    5.4 GB trap). Stella runs the file itself through onnxruntime.
    """

    return str(Path.home() / "models" / "silero" / "silero_vad.onnx")


@dataclass(frozen=True)
class StellaSettings:
    """The minimal local configuration a Stella application needs."""

    provider: str = "openai"
    model: str | None = None
    openai_base_url: str | None = None
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    llama_binary: str = DEFAULT_LLAMA_SERVER_BINARY
    llama_port: int = DEFAULT_LLAMA_SERVER_PORT
    memory_db: str = field(default_factory=default_memory_db)
    reminders_db: str = field(default_factory=default_reminders_db)
    history_db: str = field(default_factory=default_history_db)
    transcripts_db: str = field(default_factory=default_transcripts_db)
    transcripts_enabled: bool = False
    semantic_db: str = field(default_factory=default_semantic_db)
    semantic_memory_enabled: bool = False
    os_tools_enabled: bool = False
    outline_tools_enabled: bool = False
    web_tools_enabled: bool = False
    semantic_provider: str = "local-hash"
    semantic_embed_model: str = "nomic-embed-text"
    workspace: str = field(default_factory=default_workspace)
    voice_transcription: str = "auto"
    voice_speech: str = "auto"
    transcription_model: str = "whisper-1"
    transcription_command: str | None = None
    speech_model: str = "tts-1"
    speech_voice: str = "alloy"
    speech_command: str | None = None
    speech_resident: bool = False
    voice_barge_in: str = "auto"
    vad_model: str = field(default_factory=default_vad_model)
    barge_source: str | None = None
    barge_threshold: float = 0.5

    def __post_init__(self) -> None:
        if self.semantic_provider not in SEMANTIC_PROVIDERS:
            raise SystemExit(
                "semantic_provider must be "
                + ", ".join(sorted(SEMANTIC_PROVIDERS))
            )

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
        barge_mode = os.environ.get("STELLA_VOICE_BARGE_IN", "auto").casefold()
        if barge_mode not in BARGE_MODES:
            raise SystemExit(
                "STELLA_VOICE_BARGE_IN must be 'auto', 'on' or 'off'"
            )
        raw_threshold = os.environ.get("STELLA_BARGE_THRESHOLD", "0.5")
        try:
            barge_threshold = float(raw_threshold)
        except ValueError:
            barge_threshold = -1.0
        if not 0.0 < barge_threshold < 1.0:
            raise SystemExit(
                "STELLA_BARGE_THRESHOLD must be a speech probability "
                "strictly between 0 and 1"
            )
        raw_port = os.environ.get(
            "STELLA_LLAMA_SERVER_PORT", str(DEFAULT_LLAMA_SERVER_PORT)
        )
        try:
            llama_port = int(raw_port)
        except ValueError:
            llama_port = -1
        if not 0 < llama_port < 65536:
            raise SystemExit(
                "STELLA_LLAMA_SERVER_PORT must be a TCP port between "
                "1 and 65535"
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
            "semantic_db": os.environ.get(
                "STELLA_SEMANTIC_DB", default_semantic_db()
            ),
            "semantic_embed_model": os.environ.get(
                "STELLA_EMBED_MODEL", "nomic-embed-text"
            ),
            "workspace": os.environ.get(
                "STELLA_WORKSPACE", default_workspace()
            ),
            "llama_binary": os.environ.get(
                "STELLA_LLAMA_SERVER_BINARY", DEFAULT_LLAMA_SERVER_BINARY
            ),
            "llama_port": llama_port,
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
            "speech_resident": _env_toggle("STELLA_SPEECH_RESIDENT") is True,
            "voice_barge_in": barge_mode,
            "vad_model": os.environ.get(
                "STELLA_VAD_MODEL", default_vad_model()
            ),
            "barge_source": os.environ.get("STELLA_BARGE_SOURCE") or None,
            "barge_threshold": barge_threshold,
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
        semantic_memory_enabled: bool = False,
        semantic_provider: str = "local-hash",
        os_tools_enabled: bool = False,
        outline_tools_enabled: bool = False,
        web_tools_enabled: bool = False,
    ) -> StellaSettings:
        """Settings from the saved first-run configuration."""

        transcript_override = transcripts_env_override()
        semantic_override = semantic_env_override()
        provider_override = semantic_provider_env_override()
        os_override = os_tools_env_override()
        outline_override = outline_tools_env_override()
        web_override = web_tools_env_override()
        return cls(
            provider=provider,
            model=model,
            ollama_base_url=ollama_base_url,
            openai_base_url=openai_base_url,
            transcripts_enabled=(
                transcripts_enabled
                if transcript_override is None
                else transcript_override
            ),
            semantic_memory_enabled=(
                semantic_memory_enabled
                if semantic_override is None
                else semantic_override
            ),
            os_tools_enabled=(
                os_tools_enabled if os_override is None else os_override
            ),
            outline_tools_enabled=(
                outline_tools_enabled
                if outline_override is None
                else outline_override
            ),
            web_tools_enabled=(
                web_tools_enabled if web_override is None else web_override
            ),
            semantic_provider=(
                semantic_provider if provider_override is None
                else provider_override
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
        if provider not in {"openai", "ollama", "llama"}:
            raise SystemExit(
                "STELLA_LLM_PROVIDER must be 'openai', 'ollama' or 'llama'"
            )
        return cls(
            provider=provider,
            model=model,
            openai_base_url=os.environ.get("OPENAI_BASE_URL"),
            ollama_base_url=os.environ.get(
                "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
            ),
            transcripts_enabled=transcripts_env_override() is True,
            semantic_memory_enabled=semantic_env_override() is True,
            os_tools_enabled=os_tools_env_override() is True,
            outline_tools_enabled=outline_tools_env_override() is True,
            web_tools_enabled=web_tools_env_override() is True,
            semantic_provider=(
                semantic_provider_env_override() or "local-hash"
            ),
            **cls._environment_fields(),
        )


@dataclass
class StellaApplication:
    """A built Stella core plus the session and settings that own it."""

    session: StellaSession
    settings: StellaSettings
    voice: VoicePanel | None = None
    proposals: ReflectionStore | None = None
    brain_server: LlamaBrainServer | None = None
    barge_in: BargeInListener | None = None
    barge_notice: str | None = None

    def close(self) -> None:
        # The brain process is Stella's child: closing the application
        # stops it, so no llama-server is ever left running unsupervised.
        if self.brain_server is not None:
            self.brain_server.stop()
        if self.barge_in is not None:
            # The ear is Stella's child too: its capture process ends
            # with the application, never outliving the window.
            self.barge_in.stop()
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
        retriever = self.session.stella.semantic_retriever
        if (
            retriever is not None
            and isinstance(retriever.index, SQLiteSemanticIndex)
        ):
            retriever.index.close()


def _build_embedding_provider(
    settings: StellaSettings,
) -> EmbeddingProvider:
    """Instantiate the explicitly chosen embedding provider.

    Every provider is constructed lazily-cheap here; a selected-but-
    unusable backend fails loudly at this point rather than silently
    embedding with a different model.
    """

    if settings.semantic_provider == "ollama":
        return OllamaEmbeddingProvider(
            model=settings.semantic_embed_model,
            base_url=settings.ollama_base_url,
        )
    if settings.semantic_provider == "minilm":
        if not minilm_extra_available():
            raise SystemExit(
                "semantic provider 'minilm' needs the optional extra: "
                "install stella[embed] (CPU-only torch suffices) or "
                "choose another STELLA_SEMANTIC_PROVIDER"
            )
        return MiniLMEmbeddingProvider()
    return LocalHashEmbeddingProvider()


def build_application(settings: StellaSettings) -> StellaApplication:
    """Construct the trusted Stella core exactly like the CLI does."""
    if not settings.model:
        raise SystemExit("STELLA_MODEL is required")
    # For the llama provider the model is a GGUF path and the brain is a
    # child process Stella owns; it is constructed here but started only
    # once everything else exists, so a failure anywhere below can never
    # leave a server running.
    brain_server: LlamaBrainServer | None = None
    if settings.provider == "ollama":
        # The compatibility endpoint ignores per-request options on Ollama
        # 0.33.x; native /api/chat is the only way to apply num_ctx. It must
        # exceed the whole decision prompt: at 4096 the server evaluated
        # exactly 2050 of the ~5300 prompt tokens for every turn and still
        # reported finish "stop" — silently dropping the head of the
        # instruction prompt (measured via Ollama's own prompt_eval_count,
        # ~/tools/measure_prompt_ctx.py, research report 15).
        llm = OllamaLLMClient(
            model=settings.model,
            base_url=settings.ollama_base_url,
            native=True,
            num_ctx=8192,
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
    elif settings.provider == "llama":
        brain_server = LlamaBrainServer(
            model_path=settings.model,
            binary=settings.llama_binary,
            port=settings.llama_port,
        )
        # llama-server always serves exactly the one loaded model, so the
        # request-time model name is a formality; the GGUF path identifies
        # it honestly.
        llm = LlamaServerLLMClient(
            model=settings.model,
            base_url=brain_server.base_url,
        )
    else:
        raise SystemExit(
            "STELLA_LLM_PROVIDER must be 'openai', 'ollama' or 'llama'"
        )
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
    # The semantic index duplicates every stored memory into a second
    # local database, so it exists only while the opt-in flag is on;
    # off means no file is even created.
    semantic_retriever = None
    if settings.semantic_memory_enabled:
        Path(settings.semantic_db).parent.mkdir(parents=True, exist_ok=True)
        semantic_index = SQLiteSemanticIndex(settings.semantic_db, memory.scope)
        semantic_retriever = SemanticRetriever(
            _build_embedding_provider(settings), semantic_index
        )
        # One rebuild at startup heals anything changed while Stella was
        # away. A failure here is not fatal and not silent: every later
        # memory mutation re-syncs and reports honestly.
        reconcile_semantic_index(memory, semantic_retriever)
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
    # Desktop capabilities are doubly gated: an explicit opt-in flag and
    # a real Hyprland session with the measured binaries (reports
    # 03/11/13). Off or unavailable means the model never sees them.
    if settings.os_tools_enabled:
        for tool in build_desktop_tools(os.environ):
            tools.register(tool)
    # Outline capabilities are doubly gated too: an explicit opt-in flag
    # and a reachable Outline server with a readable token. A server
    # that is not running means the model never sees these tools.
    if settings.outline_tools_enabled:
        for tool in build_outline_tools(os.environ):
            tools.register(tool)
    # The web capability is a single gate: the flag. There is nothing to
    # probe — without a TinyFish key the search falls to the keyless ddgs
    # path, and if that is unavailable the tools answer "web is off"
    # rather than pretending (report 22).
    if settings.web_tools_enabled:
        for tool in build_web_tools(os.environ):
            tools.register(tool)
    stella = Stella(
        brain=LLMBrain(llm, tools, persona=PersonaLoader()),
        llm=llm,
        tool=tools,
        memory=memory,
        max_tool_steps=2,
        reminders=reminders,
        semantic_retriever=semantic_retriever,
    )
    # The shared application backs the desktop UI too, so its session must
    # not quote CLI-only instructions ("type 'exit'") in UI error messages.
    # The interactive CLI loop builds its own StellaSession with the hint.
    voice = build_voice(settings)
    # Barge-in is explicitly opt-in and its absence must never affect
    # anything else: an enabled-but-broken ear becomes one honest
    # message at startup (via the bridge), not a failed launch.
    barge_in: BargeInListener | None = None
    barge_notice: str | None = None
    try:
        barge_in = build_barge_in(settings)
    except VoiceError as error:
        barge_notice = str(error)
    # Last possible moment to spawn the brain: nothing after this can
    # fail and strand the process (stop() also runs inside a failed
    # start(), and close() owns it afterwards).
    if brain_server is not None:
        brain_server.start()
    return StellaApplication(
        StellaSession(
            stella,
            error_footer="try again.",
            transcripts=(transcripts if settings.transcripts_enabled else None),
        ),
        settings,
        voice,
        proposals=proposals,
        brain_server=brain_server,
        barge_in=barge_in,
        barge_notice=barge_notice,
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


# D3: application-authored narration for spoken conversation turns. These
# fixed phrases tell a listening user that Stella is busy rather than
# stuck; the model never authors them and they change no decision. No
# entry exists for "answering" — the reply itself is that phase's speech.
NARRATION_PHRASES: dict[str, tuple[str, ...]] = {
    "thinking": (
        "Let me think about that.",
        "One moment, I'm thinking.",
    ),
    "working": (
        "Working on that now.",
        "One second, I'm taking care of it.",
    ),
}


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

    def stop_and_transcribe(
        self, should_cancel: CancelCheck | None = None
    ) -> str:
        """Finish one recording and return its transcript, or raise.

        ``should_cancel`` (A8) makes the whole pre-turn window — finishing
        the capture and running transcription — abandonable: on a cancel
        the capture process and a cancellable transcription command are
        killed, and :class:`ProviderRequestCancelled` is raised. A
        cancelled transcript is never replaced with invented text.
        """

        recorder, transcriber = self._recorder, self._transcriber
        if recorder is None or transcriber is None:
            raise VoiceError(
                self._input_notice
                or "Voice input is not available in this configuration."
            )

        def work() -> str:
            try:
                path = recorder.stop()
                # The audio part carries only the bounded temporary
                # reference; raw audio never enters the conversation.
                part = InputPart(
                    modality=InputModality.AUDIO,
                    provenance=InputProvenance.USER,
                    reference=path,
                )
                transcript = transcriber.transcribe(part)
            except VoiceError:
                raise
            except Exception as error:  # friendly text, never a trace
                detail = " ".join(str(error).split()) or type(error).__name__
                raise VoiceError(
                    f"Transcription failed ({detail[:120]}). Nothing was "
                    "sent to Stella."
                ) from error
            finally:
                recorder.dispose()
            if not isinstance(transcript, str) or not transcript.strip():
                raise VoiceError(
                    "No speech was recognized. Nothing was sent to Stella."
                )
            return transcript.strip()[:MAX_INPUT_CONTENT_CHARS]

        def abort() -> None:
            # Best-effort transport stops only: killing the capture
            # process and (for command providers) the running transcription
            # command. Cloud providers expose no handle and are abandoned.
            recorder.cancel()
            cancel_command = getattr(transcriber, "cancel", None)
            if cancel_command is not None:
                cancel_command()

        return run_cancellable(work, should_cancel, on_cancel=abort)

    def abandon_listening(self) -> None:
        if self._recorder is not None:
            self._recorder.cancel()

    def synthesize(
        self, result: StellaResult, should_cancel: CancelCheck | None = None
    ) -> str:
        """Render one existing final response and return its artifact.

        ``should_cancel`` (A8) abandons a running synthesis: a command
        provider's process is killed and :class:`ProviderRequestCancelled`
        is raised, so no artifact is ever handed to playback.
        """

        speech = self._speech
        if speech is None:
            raise VoiceError(
                self._output_notice
                or "Voice output is not available right now."
            )

        def work() -> str:
            artifact = Stella.speak(result, speech)
            return artifact.reference

        def abort() -> None:
            # Cloud providers expose no handle; their partial artifact
            # stays in the provider temp dir and is swept at shutdown.
            cancel_command = getattr(speech, "cancel", None)
            if cancel_command is not None:
                cancel_command()

        return run_cancellable(work, should_cancel, on_cancel=abort)

    def synthesize_phrase(
        self, text: str, should_cancel: CancelCheck | None = None
    ) -> str:
        """Render one application-authored phrase, as reply speech does.

        Narration is fixed text the application itself wrote (D3), never
        model output: it goes through the same bounded provider call with
        the same abandonable cancel path as a real reply.
        """

        speech = self._speech
        if speech is None:
            raise VoiceError(
                self._output_notice
                or "Voice output is not available right now."
            )

        def work() -> str:
            artifact = speech.speak(SpeechOutput(text))
            return artifact.reference

        def abort() -> None:
            cancel_command = getattr(speech, "cancel", None)
            if cancel_command is not None:
                cancel_command()

        return run_cancellable(work, should_cancel, on_cancel=abort)

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


def build_barge_in(settings: StellaSettings) -> BargeInListener | None:
    """Assemble the barge-in ear, or None when the feature is off.

    The default mode is ``auto``: the ear arms only when the user has
    named a capture source with ``STELLA_BARGE_SOURCE`` — the documented
    way to point it at an echo-cancelled microphone. Without that
    declaration the raw mic hears the speakers directly and live
    measurement (research report 29) showed uncancelled playback frames
    self-firing the interrupt, so an un-armed ear is the safe default;
    ``on`` arms on the system default source anyway, ``off`` never.

    Unlike :func:`build_voice` this raises :class:`VoiceError` when
    barge-in was explicitly asked for but cannot work (missing extra,
    missing model): the reason must surface once as a message instead
    of the feature silently doing nothing forever.
    """

    if settings.voice_barge_in == "off":
        return None
    if settings.voice_barge_in == "auto" and settings.barge_source is None:
        return None
    vad = SileroVad(settings.vad_model)
    return BargeInListener(
        features=vad.features,
        command=capture_command(settings.barge_source),
        threshold=settings.barge_threshold,
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
        command = shlex.split(settings.speech_command)
        if settings.speech_resident:
            # D2: one resident worker keeps the model loaded across
            # sentences; the per-sentence start-up floor of a plain
            # command provider is what users hear as inter-sentence
            # silence. Without a command there is nothing to keep
            # resident, so this path never applies to auto/espeak.
            return ResidentSpeechProvider(command)
        return CommandSpeechProvider(command)
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


# Closes a chunked reply's play queue; no generated artifact path can
# contain a NUL, so this sentinel string can never collide with one.
_END_OF_SPEECH = "\x00stella-end-of-speech"


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
        self._barge: BargeInListener | None = None
        self._playback: threading.Thread | None = None
        self._speech_interrupt: threading.Event | None = None
        self._speech_consumer: threading.Thread | None = None
        self._commands: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._turn_cancel = threading.Event()
        # D3 narration: one non-blocking slot plus a per-turn retire event.
        self._narration_lock = threading.Lock()
        self._narration_dead: threading.Event | None = None
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
        if (
            self._barge is not None
            and self._barge is not application.barge_in
        ):
            # Rebuilding the application replaces the ear too: the old
            # capture process must not outlive the settings that grew it.
            self._barge.stop()
        self._barge = application.barge_in
        if self._barge is not None:
            self._barge.on_speech = self._on_barge_in
        if application.barge_notice is not None:
            # An enabled-but-unusable ear is reported once, honestly,
            # and changes nothing else about how Stella behaves.
            self._emit("voice_error", application.barge_notice)

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
            # Clear at submission: this turn owns the flag from here on,
            # and a cancel that arrived for an earlier turn can never be
            # blamed on (or erased by) this one mid-flight.
            self._turn_cancel.clear()
            self._speak_after(self._handle_turn(user_input))

        self._post(handle)

    def cancel_current_turn(self) -> None:
        """Ask the running turn to stop at its next safe point.

        Callable from any thread (the UI button presses it while the
        worker is busy): it only sets a flag, denies any outstanding
        approval — the canonical safe answer the broker already uses when
        the application exits — and stops any spoken audio, because a
        user who cancels should hear silence within a tick, not the tail
        of a discarded turn. It never approves, executes, or interrupts
        an action that is already in flight.
        """

        self._turn_cancel.set()
        self.approvals.deny_outstanding()
        self._flush_narration()
        self._interrupt_speech()
        if self._voice is not None:
            self._voice.cancel_playback()

    def _interrupt_speech(self) -> None:
        """Retire the current chunked reply's audio, never its decision.

        Callable from any thread: an :class:`threading.Event` is
        thread-safe, and it only stops sound that has not been heard yet.
        """

        if self._speech_interrupt is not None:
            self._speech_interrupt.set()

    def _on_barge_in(self) -> None:
        """One confirmed interruption is exactly one Cancel-button press.

        Called from the barge-in thread; ``cancel_current_turn`` is
        documented as any-thread callable. The detector contributes no
        decision, no text, and no approval — the user's voice is just
        another way to press the button that already exists.
        """

        self.cancel_current_turn()

    def _begin_barge_in(self) -> None:
        """Arm the ear for one speaking episode; problems retire the ear."""

        listener = self._barge
        if listener is None:
            return
        if listener.failed:
            # The listener thread faulted once (a broken VAD model can
            # do that): retire silently now, not loudly on every reply.
            self._barge = None
            return
        try:
            listener.start()
        except VoiceError as error:
            self._barge = None
            self._emit("voice_error", str(error))

    def _end_barge_in(self) -> None:
        if self._barge is not None:
            self._barge.stop()

    def _should_cancel(self) -> bool:
        return self._turn_cancel.is_set()

    def _handle_turn(
        self, user_input: str, spoken: bool = False
    ) -> TurnOutcome:
        session = self._require_session()
        self._check_due_reminders()
        dead = threading.Event()
        self._narration_dead = dead

        def observe(kind: str) -> None:
            self._narrate(kind, dead)

        # A spoken turn gains two things and only two things: the core
        # sees the audio modality (briefer, speakable answers) and the
        # activity observer (filler the application itself authored).
        outcome = session.run_turn(
            user_input,
            should_cancel=self._should_cancel,
            on_activity=observe if spoken else None,
            spoken=spoken,
        )
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

    def _voice_conversation(self) -> bool:
        """True when a voice turn is a spoken conversation.

        Spoken-ness is decided at the trusted application edge: the
        audio-modality envelope — with its brevity note and its work
        narration — applies only when the answer will actually be heard.
        A voice-typed turn with speech output off stays an ordinary
        text turn.
        """

        panel = self._voice
        return (
            panel is not None
            and panel.speech_enabled
            and panel.output_available
        )

    def _narrate(self, kind: str, dead: threading.Event | None) -> None:
        """Offer one fixed narration phrase without ever blocking the turn.

        Runs on the worker thread inside a live turn, so nothing audible
        happens here: it takes the single narration slot with a
        non-blocking lock — a busy slot drops the phrase, narration never
        stacks — and hands synthesis and playback to a daemon thread.
        Only :data:`NARRATION_PHRASES` text is ever spoken, and only for
        kinds that have phrases.
        """

        if dead is None or dead.is_set() or self._should_cancel():
            return
        phrases = NARRATION_PHRASES.get(kind)
        panel = self._voice
        if not phrases or panel is None:
            return
        if not self._narration_lock.acquire(blocking=False):
            return
        if dead.is_set() or self._should_cancel():
            self._narration_lock.release()
            return
        threading.Thread(
            target=self._speak_narration,
            args=(panel, random.choice(phrases), dead),
            name="stella-narration",
            daemon=True,
        ).start()

    def _speak_narration(
        self, panel: VoicePanel, phrase: str, dead: threading.Event
    ) -> None:
        """Synthesize and play one filler phrase off the worker thread.

        Narration is decoration: every failure — synthesis, playback, or
        a player stopped underneath it — stays silent, because the real
        reply the user asked for is still coming and reports itself.
        """

        try:
            try:
                path = panel.synthesize_phrase(phrase, self._should_cancel)
            except Exception:  # noqa: BLE001 - decoration fails silently
                return
            if dead.is_set() or self._should_cancel():
                panel.dispose_artifact(path)
                return
            try:
                panel.play(path)
            except Exception:  # noqa: BLE001, S110 - decoration is silent
                pass
            finally:
                panel.dispose_artifact(path)
        finally:
            self._narration_lock.release()

    def _flush_narration(self) -> None:
        """Retire this turn's narration: nothing of it may follow onto
        the speakers once the reply speaks, the user cancels, or playback
        is stopped."""

        if self._narration_dead is not None:
            self._narration_dead.set()

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
                # Push-to-talk takes the microphone back: the barge-in
                # ear never competes with an explicit Listen press.
                self._end_barge_in()
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
            self._turn_cancel.clear()
            self._emit("voice_state", "transcribing")
            try:
                transcript = panel.stop_and_transcribe(self._should_cancel)
            except ProviderRequestCancelled:
                # The user stopped the voice input itself: nothing was
                # sent, nothing was invented, and no turn starts.
                self._emit(
                    "voice_error",
                    "Voice input was cancelled at your request. Nothing "
                    "was sent to Stella.",
                )
                return
            except VoiceError as error:
                # A failed transcript is never replaced with invented text.
                self._emit("voice_error", str(error))
                return
            self._emit("voice_transcript", transcript)
            # From here the transcript follows the exact typed-input path,
            # with one honest addition: when this really is a spoken
            # conversation (the answer will be heard), the turn is marked
            # spoken for brevity and narration.
            self._speak_after(
                self._handle_turn(
                    transcript, spoken=self._voice_conversation()
                )
            )

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
        For a chunked reply this silences the whole reply — the sound
        playing now and the sentences only queued — because "stop
        speaking" was never a request to pause until the next sentence.
        """

        self._flush_narration()
        self._interrupt_speech()
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
        if self._should_cancel():
            # A cancelled turn never grows a voice: the user asked to
            # stop, so nothing is synthesized and nothing is played.
            return
        # The answer is now the narration: work filler must not follow
        # the reply onto the speakers (phrases already audible finish).
        self._flush_narration()
        chunks = sentence_chunks(outcome.response)
        if len(chunks) > 1:
            # A9: the first sentence should be speaking while the rest
            # is still being synthesized; whole-file speech made the
            # user wait for every character before hearing any.
            self._speak_chunks(panel, outcome.result, chunks)
            return
        try:
            path = panel.synthesize(outcome.result, self._should_cancel)
        except ProviderRequestCancelled:
            # Cancelled mid-synthesis: silence is the requested outcome,
            # not a failure to report.
            return
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
        if self._should_cancel():
            # Discard the tail: an artifact finished a moment before the
            # cancel was noticed must never reach a speaker.
            panel.dispose_artifact(path)
            return
        # A new reply may interrupt still-playing audio; that cancels only
        # playback, never any Stella decision. The join keeps the old
        # thread's "idle" event ordered before the new "speaking" state.
        panel.cancel_playback()
        if self._playback is not None:
            self._playback.join(timeout=2)
        self._emit("voice_state", "speaking")
        self._begin_barge_in()

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
                self._end_barge_in()
                panel.dispose_artifact(path)
                self._emit("voice_state", "idle")

        self._playback = threading.Thread(
            target=play, name="stella-playback", daemon=True
        )
        self._playback.start()

    def _speak_chunks(
        self, panel: VoicePanel, result: StellaResult, chunks: list[str]
    ) -> None:
        """Speak one multi-sentence reply chunk by chunk.

        The worker thread stays the producer: it synthesizes one
        sentence at a time — chunk k+1 renders while chunk k plays,
        because local speech is faster than real time — while a
        consumer thread plays and disposes each artifact in order. An
        interrupt event ("Stop speaking", a cancel, shutdown, or a
        newer reply) drains the queue unsaid; the decision and its text
        are never touched.
        """

        # Retire any previous chunked consumer before this reply can
        # contend for the one-at-a-time player. There is never a
        # previous producer to retire: it is this very thread.
        self._interrupt_speech()
        interrupt = threading.Event()
        self._speech_interrupt = interrupt

        def stopped() -> bool:
            return self._should_cancel() or interrupt.is_set()

        outbox: queue.Queue[str] = queue.Queue(maxsize=3)

        def consume() -> None:
            while True:
                item = outbox.get()
                if item == _END_OF_SPEECH:
                    break
                if interrupt.is_set():
                    panel.dispose_artifact(item)
                    continue
                try:
                    panel.play(item)
                except VoiceError as error:
                    # One honest report, then the rest goes unsaid: a
                    # failing player will not recover mid-reply.
                    self._emit("voice_error", str(error))
                    interrupt.set()
                except BaseException as error:  # noqa: BLE001 - report, never crash
                    detail = " ".join(str(error).split()) or type(error).__name__
                    self._emit(
                        "voice_error",
                        f"Stella could not play the response ({detail[:120]}).",
                    )
                    interrupt.set()
                finally:
                    panel.dispose_artifact(item)
            self._end_barge_in()
            self._emit("voice_state", "idle")

        try:
            first = panel.synthesize(
                replace(result, response=chunks[0]), stopped
            )
        except ProviderRequestCancelled:
            # Cancelled mid-synthesis: silence is the requested outcome.
            return
        except VoiceError as error:
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
        if stopped():
            # Discard the tail: nothing of a cancelled reply reaches
            # a speaker.
            panel.dispose_artifact(first)
            return
        # A new reply may interrupt still-playing audio; that cancels only
        # playback, never any Stella decision. The joins keep the old
        # threads' "idle" events ordered before the new "speaking" state.
        panel.cancel_playback()
        if self._playback is not None:
            self._playback.join(timeout=2)
        if self._speech_consumer is not None:
            self._speech_consumer.join(timeout=2)
        self._emit("voice_state", "speaking")
        self._begin_barge_in()
        outbox.put(first)
        self._speech_consumer = threading.Thread(
            target=consume, name="stella-speech-playback", daemon=True
        )
        self._speech_consumer.start()
        try:
            for chunk in chunks[1:]:
                if stopped():
                    break
                try:
                    path = panel.synthesize(
                        replace(result, response=chunk), stopped
                    )
                except ProviderRequestCancelled:
                    break
                except VoiceError as error:
                    # Sentences already heard stay heard; the text
                    # response remains fully available either way.
                    self._emit("voice_error", str(error))
                    break
                except Exception as error:  # noqa: BLE001 - friendly text
                    detail = " ".join(str(error).split()) or type(error).__name__
                    self._emit(
                        "voice_error",
                        f"Stella could not prepare speech ({detail[:120]}). "
                        "The text response is still available.",
                    )
                    break
                if stopped():
                    panel.dispose_artifact(path)
                    break
                outbox.put(path)
        finally:
            # The consumer exits only on this sentinel, so every
            # synthesized artifact is either played and disposed or
            # drained and disposed.
            outbox.put(_END_OF_SPEECH)

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
            old = self._application
            # One llama brain binds one port: when the replacement wants
            # the same port, the retiring brain must release it first.
            # The old session keeps running otherwise; if the build then
            # fails, its turns honestly report the stopped brain until
            # the settings are fixed and applied again.
            if (
                old is not None
                and old.brain_server is not None
                and settings.provider == "llama"
                and old.settings.llama_port == settings.llama_port
            ):
                old.brain_server.stop()
            application = build_application(settings)
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
        self._interrupt_speech()
        if self._voice is not None:
            self._voice.cancel_playback()
        self._end_barge_in()
        self._post(None)
        self._thread.join(timeout=5)
        if self._playback is not None:
            self._playback.join(timeout=2)
        if self._speech_consumer is not None:
            self._speech_consumer.join(timeout=2)
        if self._application is not None:
            try:
                self._application.close()
            except Exception as error:  # noqa: BLE001 - never raise in shutdown
                del error
