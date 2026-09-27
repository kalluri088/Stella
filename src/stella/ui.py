"""Local Tk graphical interface for Stella.

The window is a thin view over the shared application layer: every
conversation turn, memory edit, reminder change, and approval answer is
posted through ``StellaBridge``, whose single worker thread owns the
trusted Stella core. This module never touches files, databases, tools,
or the LLM directly, and it never manufactures authorization: approvals
are produced by the existing dispatcher, and the UI only ever answers the
exact request the dispatcher raised (closing a dialog denies it).

The one deliberate exception is ``SetupDialog``, shown before any Stella
application exists: it runs bounded provider probes and saves Stella's
non-secret configuration file. It never touches tools, memory,
reminders, or approvals, and a successful setup grants nothing beyond
"Stella can talk to this model".
"""

from __future__ import annotations

import os
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import ttk

from stella import config
from stella.app import (
    OutcomeStatus,
    StellaBridge,
    StellaSettings,
    TurnOutcome,
    UiEvent,
    build_application,
    default_data_dir,
    outcome_status,
)
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL
from stella.tools import ActionPreview, ApprovalRequest, action_summary

# Display labels for the semantic embedding choice; the menu is the only
# writer of the variable, and the reverse map carries the selection back.
_SEMANTIC_PROVIDER_LABELS = {
    "local-hash": "Local word-shape (no model)",
    "ollama": "Ollama embedding model",
    "minilm": "MiniLM (stella[embed] extra)",
}
_SEMANTIC_PROVIDER_BY_LABEL = {
    label: value for value, label in _SEMANTIC_PROVIDER_LABELS.items()
}

# Two complete palettes — dark and light — shared by the window, the
# dialogs and the setup wizard. Everything here is presentation only:
# the bridge contract, transcript wording and approval exactness are
# untouched by styling. Widgets read colors from the module-level
# ``THEME`` at build time; switching themes re-runs the ttk style setup
# and recolors the handful of plain Tk widgets that styles cannot reach.
@dataclass(frozen=True)
class Theme:
    window: str
    surface: str
    surface_alt: str
    field: str
    text: str
    text_dim: str
    accent: str
    accent_strong: str
    accent_hover: str
    on_accent: str
    error: str
    ok: str
    reminder: str
    user_bubble: str
    stella_bubble: str


_DARK_THEME = Theme(
    window="#101319",
    surface="#171b23",
    surface_alt="#212630",
    field="#272e3b",
    text="#e9ecf3",
    text_dim="#98a2b6",
    accent="#8fb2ff",
    accent_strong="#4f6ef7",
    accent_hover="#6f8bff",
    on_accent="#0f1219",
    error="#ff9a8d",
    ok="#8fe3b0",
    reminder="#d3b5ff",
    user_bubble="#232e47",
    stella_bubble="#1d2925",
)

_LIGHT_THEME = Theme(
    window="#f2f3f7",
    surface="#ffffff",
    surface_alt="#e7eaf1",
    field="#eceef4",
    text="#1c2029",
    text_dim="#5d6678",
    accent="#2f56c4",
    accent_strong="#2f56c4",
    accent_hover="#4469d6",
    on_accent="#ffffff",
    error="#b3362b",
    ok="#1e7f4f",
    reminder="#6b3fa0",
    user_bubble="#e6ecfa",
    stella_bubble="#e6f3ea",
)

THEMES: dict[str, Theme] = {"dark": _DARK_THEME, "light": _LIGHT_THEME}
DEFAULT_THEME = "dark"

THEME = _DARK_THEME
_theme_name = DEFAULT_THEME


def current_theme_name() -> str:
    return _theme_name


def apply_theme(name: str) -> Theme:
    """Point every widget builder at the named palette."""

    global THEME, _theme_name
    THEME = THEMES.get(name, _DARK_THEME)
    _theme_name = name if name in THEMES else DEFAULT_THEME
    return THEME


def _theme_choice_path() -> Path:
    return default_data_dir() / "ui-theme"


def load_theme_choice() -> str:
    """The last theme the user picked; unreadable state falls back to dark."""

    try:
        raw = _theme_choice_path().read_text(encoding="utf-8").strip().casefold()
    except OSError:
        return DEFAULT_THEME
    return raw if raw in THEMES else DEFAULT_THEME


def save_theme_choice(name: str) -> None:
    if name not in THEMES:
        return
    try:
        path = _theme_choice_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name + "\n", encoding="utf-8")
    except OSError:  # pragma: no cover - styling state is never critical
        pass

_FONT_CANDIDATES = (
    "Inter",
    "Cantarell",
    "Noto Sans",
    "Ubuntu",
    "DejaVu Sans",
    "Segoe UI",
)


def _font_family(root: tk.Misc) -> str | None:
    """Pick the nicest installed UI font, or None to keep Tk's default."""
    families = {name.casefold() for name in tkfont.families(root)}
    for candidate in _FONT_CANDIDATES:
        if candidate.casefold() in families:
            return candidate
    return None


def _configure_styles(root: tk.Misc) -> None:
    """Apply the Stella theme to every Tk and ttk widget in this app.

    ttk styles and named fonts are process-wide, so calling this from
    any window configures all of them; it is deliberately idempotent.
    """
    family = _font_family(root)
    if family is not None:
        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
        ):
            try:
                tkfont.nametofont(name).configure(family=family, size=10)
            except tk.TclError:  # pragma: no cover - exotic Tk builds
                pass
    default = tkfont.nametofont("TkDefaultFont")
    ui_family = default.actual("family")
    ui_size = default.actual("size")

    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure(
        ".",
        background=THEME.surface,
        foreground=THEME.text,
        fieldbackground=THEME.field,
        padding=6,
    )
    style.configure("Toplevel", background=THEME.surface)
    style.configure("TFrame", background=THEME.surface)
    style.configure("Card.TFrame", background=THEME.surface_alt)
    style.configure("TLabel", background=THEME.surface, foreground=THEME.text)
    style.configure("Dim.TLabel", foreground=THEME.text_dim)
    style.configure(
        "Status.TLabel",
        foreground=THEME.accent,
        font=(ui_family, ui_size, "italic"),
    )
    style.configure(
        "Brand.TLabel",
        foreground=THEME.accent,
        font=(ui_family, ui_size + 5, "bold"),
    )
    style.configure(
        "Heading.TLabel", font=(ui_family, ui_size + 1, "bold")
    )
    style.configure(
        "TButton",
        background=THEME.field,
        foreground=THEME.text,
        bordercolor=THEME.surface_alt,
        lightcolor=THEME.surface_alt,
        darkcolor=THEME.surface_alt,
        focusthickness=0,
        padding=(14, 7),
    )
    style.map(
        "TButton",
        background=[("disabled", THEME.surface_alt), ("active", THEME.accent_strong)],
        foreground=[("disabled", THEME.text_dim), ("active", THEME.on_accent)],
    )
    style.configure(
        "Accent.TButton",
        background=THEME.accent_strong,
        foreground=THEME.on_accent,
        bordercolor=THEME.accent_strong,
        lightcolor=THEME.accent_strong,
        darkcolor=THEME.accent_strong,
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", THEME.surface_alt), ("active", THEME.accent_hover)],
        foreground=[("disabled", THEME.text_dim)],
    )
    style.configure(
        "TMenubutton",
        background=THEME.field,
        foreground=THEME.text,
        bordercolor=THEME.surface_alt,
        focusthickness=0,
        padding=(10, 5),
    )
    style.map("TMenubutton", background=[("active", THEME.accent_strong)])
    style.configure(
        "TNotebook", background=THEME.surface, borderwidth=0, tabmargins=(2, 0, 2, 0)
    )
    style.configure(
        "TNotebook.Tab",
        background=THEME.surface_alt,
        foreground=THEME.text_dim,
        padding=(18, 9),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", THEME.field)],
        foreground=[("selected", THEME.text)],
    )
    style.configure(
        "TEntry",
        insertcolor=THEME.text,
        bordercolor=THEME.surface_alt,
        lightcolor=THEME.surface_alt,
        darkcolor=THEME.surface_alt,
        fieldbackground=THEME.field,
        foreground=THEME.text,
        padding=7,
    )
    style.configure("TLabel.TSeparator", background=THEME.surface)
    style.configure(
        "TCheckbutton", background=THEME.surface, foreground=THEME.text, padding=4
    )
    style.map(
        "TCheckbutton",
        background=[("active", THEME.surface)],
        foreground=[("disabled", THEME.text_dim)],
        indicatorcolor=[("selected", THEME.accent)],
    )
    style.configure("TRadiobutton", background=THEME.surface, foreground=THEME.text)
    style.map(
        "TRadiobutton",
        background=[("active", THEME.surface)],
        indicatorcolor=[("selected", THEME.accent)],
    )
    style.configure(
        "TCombobox",
        fieldbackground=THEME.field,
        background=THEME.field,
        foreground=THEME.text,
        arrowcolor=THEME.text_dim,
        bordercolor=THEME.surface_alt,
        lightcolor=THEME.surface_alt,
        darkcolor=THEME.surface_alt,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", THEME.field)],
        selectbackground=[("readonly", THEME.field)],
        selectforeground=[("readonly", THEME.text)],
    )
    style.configure(
        "TScrollbar",
        background=THEME.surface_alt,
        troughcolor=THEME.surface,
        bordercolor=THEME.surface,
        arrowcolor=THEME.text_dim,
        relief="flat",
    )
    style.map("TScrollbar", background=[("active", THEME.accent_strong)])
    root.option_add("*Menu*background", THEME.field)
    root.option_add("*Menu*foreground", THEME.text)
    root.option_add("*Menu*activeBackground", THEME.accent_strong)
    root.option_add("*Menu*activeForeground", THEME.on_accent)


def _style_listbox(box: tk.Listbox) -> None:
    """Theme a plain tk.Listbox to match the ttk widgets around it."""
    box.configure(
        background=THEME.field,
        foreground=THEME.text,
        selectbackground=THEME.accent_strong,
        selectforeground=THEME.on_accent,
        highlightthickness=0,
        borderwidth=0,
        relief="flat",
        activestyle="none",
        font="TkDefaultFont",
    )


class StellaWindow:
    """One Tk window driven entirely by posted bridge commands."""

    def __init__(
        self, root: tk.Tk, bridge: StellaBridge, settings: StellaSettings
    ) -> None:
        self._bridge = bridge
        self._root = root
        self._busy = False
        self._listening = False
        self._transcribing = False
        self._speaking = False
        self._turn_started: float | None = None
        self._cancelling = False
        self._shown_seconds = -1
        self._dialogs: list[tk.Toplevel] = []
        self._reminder_rows: tuple[tuple[str, str], ...] = ()
        root.title("Stella")
        root.geometry("1180x680")
        root.minsize(920, 560)
        root.configure(background=THEME.surface)
        _configure_styles(root)

        main = ttk.Frame(root)
        main.pack(fill="both", expand=True, padx=16, pady=14)

        chat = ttk.Frame(main)
        chat.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(chat)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Stella", style="Brand.TLabel").pack(
            side="left"
        )
        ttk.Label(
            header,
            text="local-first · your conversation stays on this machine",
            style="Dim.TLabel",
        ).pack(side="left", padx=(12, 0), pady=(7, 0))
        self._theme_button = ttk.Button(
            header, text=self._theme_button_text(),
            command=self._toggle_theme, width=11,
        )
        self._theme_button.pack(side="right")
        transcript_frame = ttk.Frame(chat)
        transcript_frame.pack(fill="both", expand=True)
        self._chat = tk.Text(
            transcript_frame,
            wrap="word",
            state="disabled",
            background=THEME.window,
            foreground=THEME.text,
            insertbackground=THEME.text,
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            padx=10,
            pady=12,
            font="TkTextFont",
            spacing1=4,
            spacing2=2,
        )
        scrollbar = ttk.Scrollbar(
            transcript_frame, command=self._chat.yview
        )
        self._chat.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._chat.pack(side="left", fill="both", expand=True)
        self._configure_chat_tags()
        self._status = ttk.Label(chat, text="", anchor="w",
                                 style="Status.TLabel")
        self._status.pack(fill="x", pady=(8, 2))
        input_frame = ttk.Frame(chat)
        input_frame.pack(fill="x")
        self._input = tk.Text(
            input_frame,
            height=3,
            wrap="word",
            background=THEME.field,
            foreground=THEME.text,
            insertbackground=THEME.text,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=THEME.surface_alt,
            highlightcolor=THEME.accent_strong,
            padx=10,
            pady=8,
            font="TkTextFont",
        )
        self._input.pack(side="left", fill="both", expand=True)
        send_button = ttk.Button(
            input_frame, text="Send", command=self._send,
            style="Accent.TButton",
        )
        send_button.pack(side="left", fill="y", padx=(6, 0))
        self._cancel_button = ttk.Button(
            input_frame, text="Cancel", command=self._cancel_turn,
            state="disabled",
        )
        self._cancel_button.pack(side="left", fill="y", padx=(6, 0))
        self._input.bind("<Control-Return>", lambda _event: self._send())
        ttk.Label(
            chat,
            text="Ctrl+Enter sends; Enter adds a new line.",
            style="Dim.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        self._build_voice_row(chat, bridge)

        notebook = ttk.Notebook(main)
        notebook.pack(side="left", fill="y", padx=(14, 0))
        self._build_memory_tab(notebook)
        self._build_reminder_tab(notebook)
        self._build_history_tab(notebook)
        self._build_settings_tab(notebook, settings)

        self._line(
            "Ask Stella anything. The tabs manage memories, reminders, "
            "recent actions, and the minimal local settings."
        )
        bridge.post_memories()
        bridge.post_reminders()
        # Action history is durable, so the History tab shows what Stella
        # did in earlier sessions, not only in this window.
        bridge.post_history()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._tick)
        # Queued persona reflection proposals need the approval-drain
        # loop above to already be running, so they are requested one
        # beat later, never during construction.
        root.after(250, self._bridge.post_persona_drain)

    # ------------------------------------------------------ theme switch

    def _theme_button_text(self) -> str:
        return "Light mode" if current_theme_name() == "dark" else "Dark mode"

    def _toggle_theme(self) -> None:
        name = "light" if current_theme_name() == "dark" else "dark"
        apply_theme(name)
        save_theme_choice(name)
        self._restyle()
        self._theme_button.configure(text=self._theme_button_text())

    def _restyle(self) -> None:
        """Repaint everything ttk styles do not already cover.

        Re-running ``_configure_styles`` restyles all ttk widgets live;
        plain Tk widgets (the transcript, the composer, the lists, any
        open approval dialog) carry their colors directly and must be
        recolored here.
        """
        _configure_styles(self._root)
        theme = THEME
        self._root.configure(background=theme.surface)
        self._chat.configure(
            background=theme.window,
            foreground=theme.text,
            insertbackground=theme.text,
        )
        self._configure_chat_tags()
        self._input.configure(
            background=theme.field,
            foreground=theme.text,
            insertbackground=theme.text,
            highlightbackground=theme.surface_alt,
            highlightcolor=theme.accent_strong,
        )
        for box in (
            self._memory_list,
            self._reminder_list,
            self._history_list,
        ):
            _style_listbox(box)
        for dialog in self._dialogs:
            dialog.configure(background=theme.surface)
            box = getattr(dialog, "preview_box", None)
            if box is not None:
                box.configure(
                    background=theme.window,
                    foreground=theme.text_dim,
                    highlightbackground=theme.surface_alt,
                )
                box.tag_configure("added", foreground=theme.ok)
                box.tag_configure("removed", foreground=theme.error)

    # ----------------------------------------------------------- chat

    def _configure_chat_tags(self) -> None:
        # Speech-bubble rendering for the transcript: presentation only,
        # the stored text (and every asserted prefix) is exactly what
        # the state machine inserted.
        base = tkfont.nametofont("TkTextFont")
        self._head_font = tkfont.Font(
            family=base.actual("family"),
            size=base.actual("size"),
            weight="bold",
        )
        self._italic_font = tkfont.Font(
            family=base.actual("family"),
            size=base.actual("size"),
            slant="italic",
        )
        chat = self._chat
        # "gap" is configured last so its background wins over a bubble's
        # on the blank spacer line.
        chat.tag_configure(
            "bubble-stella",
            background=THEME.stella_bubble,
            lmargin1=14,
            lmargin2=14,
            rmargin=150,
            spacing1=8,
            spacing3=0,
        )
        chat.tag_configure(
            "bubble-user",
            background=THEME.user_bubble,
            lmargin1=150,
            lmargin2=150,
            rmargin=14,
            justify="right",
            spacing1=8,
            spacing3=0,
        )
        chat.tag_configure("head-stella", foreground=THEME.ok, font=self._head_font)
        chat.tag_configure("body-stella", foreground=THEME.text)
        chat.tag_configure("head-user", foreground=THEME.accent, font=self._head_font)
        chat.tag_configure("body-user", foreground=THEME.text)
        chat.tag_configure(
            "note",
            foreground=THEME.text_dim,
            font=self._italic_font,
            lmargin1=14,
            lmargin2=14,
            rmargin=14,
            spacing1=6,
            spacing3=0,
        )
        chat.tag_configure(
            "error",
            foreground=THEME.error,
            lmargin1=14,
            lmargin2=14,
            rmargin=14,
            spacing1=8,
            spacing3=0,
        )
        chat.tag_configure(
            "reminder",
            foreground=THEME.reminder,
            lmargin1=14,
            lmargin2=14,
            rmargin=14,
            spacing1=8,
            spacing3=0,
        )
        chat.tag_configure("gap", background=THEME.window)

    def _line(self, text: str, role: str = "note") -> None:
        chat = self._chat
        chat.configure(state="normal")
        if role in ("user", "stella"):
            bubble = "bubble-user" if role == "user" else "bubble-stella"
            split = text.find(": ")
            if 0 < split <= 20:
                chat.insert(
                    "end",
                    text[: split + 2],
                    (bubble, f"head-{role}"),
                )
                chat.insert(
                    "end",
                    text[split + 2 :],
                    (bubble, f"body-{role}"),
                )
            else:
                chat.insert("end", text, (bubble, f"body-{role}"))
            chat.insert("end", "\n", (bubble,))
            chat.insert("end", "\n", ("gap",))
        elif role == "reminder" or role == "error":
            chat.insert("end", text + "\n", (role,))
            chat.insert("end", "\n", ("gap",))
        else:
            chat.insert("end", text + "\n", ("note",))
            chat.insert("end", "\n", ("gap",))
        chat.configure(state="disabled")
        chat.see("end")

    def _send(self) -> None:
        if self._listening:
            self._status.configure(
                text="Finish or cancel the recording first."
            )
            return
        user_input = self._input.get("1.0", "end").strip()
        if not user_input or self._busy:
            return
        self._input.delete("1.0", "end")
        self._line(f"You: {user_input}", role="user")
        self._busy = True
        self._begin_turn_timer()
        self._status.configure(text="Stella is working · 0 s")
        self._bridge.post_turn(user_input)

    def _begin_turn_timer(self) -> None:
        self._turn_started = time.monotonic()
        self._cancelling = False
        self._shown_seconds = -1

    def _cancel_turn(self) -> None:
        if not self._busy or self._cancelling:
            return
        # Ask the bridge to stop at the next safe point. An approval that
        # is still open is denied by the same call (fail-closed), so
        # cancelling can never leave a pending prompt waiting forever.
        self._cancelling = True
        self._cancel_button.configure(state="disabled")
        self._status.configure(text="Stella is stopping...")
        self._bridge.cancel_current_turn()
        # Any approval dialog still on screen was just denied at the
        # broker; dismiss it so a later click can never appear to reopen
        # a question that has already been answered fail-closed.
        for dialog in list(self._dialogs):
            dialog.cancel_button.invoke()

    # ------------------------------------------------------------ voice

    def _build_voice_row(self, chat: ttk.Frame, bridge: StellaBridge) -> None:
        row = ttk.Frame(chat)
        row.pack(fill="x", pady=(2, 0))
        mic_ok, speech_ok = bridge.voice_capabilities()
        self._mic_button = ttk.Button(
            row, text="Listen", command=self._toggle_listen, width=10
        )
        self._mic_button.pack(side="left")
        self._mic_cancel = ttk.Button(
            row, text="Cancel", command=self._cancel_listen, width=8,
            state="disabled",
        )
        self._mic_cancel.pack(side="left", padx=4)
        self._stop_speaking = ttk.Button(
            row,
            text="Stop speaking",
            command=bridge.stop_playback,
            state="disabled",
        )
        self._stop_speaking.pack(side="left", padx=4)
        self._speak_var = tk.BooleanVar(value=False)
        self._speak_toggle = ttk.Checkbutton(
            row,
            text="Speak replies",
            variable=self._speak_var,
            command=self._toggle_speech,
            state="normal" if speech_ok else "disabled",
        )
        self._speak_toggle.pack(side="left", padx=4)
        if not mic_ok:
            self._mic_button.configure(state="disabled")
        ttk.Label(
            chat,
            style="Dim.TLabel",
            text=(
                "Voice input is not available here (no capture command or "
                "transcription provider)."
                if not mic_ok
                else "Listening and speaking are explicit; recordings are "
                "removed right after transcription."
            ),
        ).pack(anchor="w", pady=(2, 0))

    def _toggle_listen(self) -> None:
        if self._busy:
            return
        if self._listening:
            # Optimistically disarm; the transcription events confirm the
            # outcome. The window never claims to listen while it does not.
            self._listening = False
            self._mic_button.configure(text="Listen")
            self._mic_cancel.configure(state="disabled")
            self._status.configure(text="Transcribing...")
            self._bridge.post_listen_stop()
        else:
            self._bridge.post_listen_start()

    def _cancel_listen(self) -> None:
        if self._transcribing:
            # The pre-turn window (A8): the same button now stops work in
            # flight — the flag is what the transcription poll checks.
            # One shot: re-arming waits for the next explicit Listen.
            self._mic_cancel.configure(state="disabled")
            self._bridge.cancel_current_turn()
            return
        if not self._listening:
            return
        self._listening = False
        self._mic_button.configure(text="Listen")
        self._mic_cancel.configure(state="disabled")
        self._bridge.post_listen_cancel()

    def _toggle_speech(self) -> None:
        self._bridge.set_speech_enabled(self._speak_var.get())

    def _handle_voice_state(self, state: str) -> None:
        if state == "listening":
            self._listening = True
            self._transcribing = False
            self._mic_button.configure(text="Stop")
            self._mic_cancel.configure(state="normal")
            self._status.configure(text="Listening...")
        elif state == "transcribing":
            # A8: the pre-turn window is cancellable too — the mic Cancel
            # button carries on, now stopping the transcription itself.
            self._transcribing = True
            self._mic_cancel.configure(state="normal")
            self._status.configure(text="Transcribing...")
        elif state == "speaking":
            self._speaking = True
            self._stop_speaking.configure(state="normal")
            self._status.configure(text="Speaking...")
        elif state == "idle":
            self._speaking = False
            self._stop_speaking.configure(state="disabled")
            if not self._busy:
                self._status.configure(text="")
        else:  # pragma: no cover - unknown states must not appear
            self._line(
                f"✗ Stella sent an unexpected voice state: {state}",
                role="error",
            )

    def _handle_voice_error(self, message: str) -> None:
        # A voice failure never corrupts conversation state: the transcript
        # simply was not sent, and any playback state is reset.
        self._listening = False
        self._transcribing = False
        self._speaking = False
        self._mic_button.configure(text="Listen")
        self._mic_cancel.configure(state="disabled")
        self._stop_speaking.configure(state="disabled")
        self._line(f"✗ {message}", role="error")
        if not self._busy:
            self._status.configure(text="")

    def _handle_turn(self, outcome: TurnOutcome) -> None:
        self._busy = False
        self._turn_started = None
        self._cancelling = False
        self._status.configure(text="")
        tail = self._duration_tail(outcome)
        if outcome.interrupted:
            self._line("Stella stopped that request. Nothing was changed.")
            return
        if outcome.cancelled:
            self._line(
                "Stella stopped that request at your cancel"
                f"{tail}. It was discarded and is not part of the "
                "conversation."
            )
            return
        if outcome.error_message is not None:
            self._line(f"✗ {outcome.error_message}", role="error")
            return
        if outcome.response is not None:
            self._line(f"Stella: {outcome.response}{tail}", role="stella")
        else:
            self._line(f"Stella has nothing to add.{tail}", role="stella")
        if outcome.result is not None and outcome.result.tool_result is not None:
            status = outcome_status(outcome.result.tool_result)
            self._line(f"Action outcome: {status.symbol} {status.kind}")

    @staticmethod
    def _duration_tail(outcome: TurnOutcome) -> str:
        # A6: every finished turn says how long it took, so a slow local
        # model reads as slow rather than broken. Display only; the
        # stored conversation never carries this text.
        if outcome.duration_seconds is None:
            return ""
        seconds = outcome.duration_seconds
        text = f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s"
        return f" (took {text})"

    # ------------------------------------------------------ event pump

    def _tick(self) -> None:
        for event in self._bridge.poll():
            self._handle_event(event)
        self._drain_approvals()
        self._render_working_status()
        self._root.after(100, self._tick)

    def _render_working_status(self) -> None:
        # Elapsed time is display-only; the cancel it advertises is
        # cooperative and lands between steps, never inside a tool or an
        # in-flight provider request.
        self._cancel_button.configure(
            state="normal" if self._busy and not self._cancelling else "disabled"
        )
        if not self._busy or self._dialogs or self._turn_started is None:
            return
        if self._cancelling:
            self._status.configure(text="Stella is stopping...")
            return
        seconds = int(time.monotonic() - self._turn_started)
        if seconds != self._shown_seconds:
            self._shown_seconds = seconds
            self._status.configure(text=f"Stella is working · {seconds} s")

    def _handle_event(self, event: UiEvent) -> None:
        kind, payload = event.kind, event.payload
        if kind == "turn":
            self._handle_turn(payload)
        elif kind == "reminder_delivered":
            self._line(f"Reminder: {payload}", role="reminder")
        elif kind == "memories":
            self._show_memories(payload)
        elif kind == "memory_result":
            self._memory_status.configure(text=str(payload))
        elif kind == "reminders":
            self._show_reminders(payload)
        elif kind == "history":
            self._show_history(payload)
        elif kind == "reminder_result":
            status: OutcomeStatus = payload
            self._reminder_status.configure(
                text=f"{status.symbol} {status.detail}"
            )
        elif kind == "settings":
            self._line(f"(settings) {payload}")
        elif kind == "notice":
            self._line(f"(persona) {payload}")
        elif kind == "voice_state":
            self._handle_voice_state(str(payload))
        elif kind == "voice_transcript":
            self._line(f"You (voice): {payload}", role="user")
            self._transcribing = False
            self._mic_cancel.configure(state="disabled")
            self._busy = True
            self._begin_turn_timer()
            self._status.configure(text="Stella is working · 0 s")
        elif kind == "voice_error":
            self._handle_voice_error(str(payload))
        elif kind == "error":
            self._line(f"✗ {payload}", role="error")
        else:  # pragma: no cover - unknown kinds must not appear
            self._line(
                f"✗ Stella sent an unexpected update: {kind}", role="error"
            )

    # ------------------------------------------------------- approvals

    def _drain_approvals(self) -> None:
        while True:
            pending = self._bridge.next_approval_request()
            if pending is None:
                return
            token, request, preview = pending
            self._show_approval(token, request, preview)

    def _show_approval(
        self,
        token: int,
        request: ApprovalRequest,
        preview: ActionPreview | None = None,
    ) -> None:
        dialog = tk.Toplevel(self._root)
        dialog.title("Stella needs approval")
        dialog.resizable(False, False)
        dialog.configure(background=THEME.surface)

        def answer(approved: bool) -> None:
            self._bridge.resolve_approval(token, approved)
            if dialog in self._dialogs:
                self._dialogs.remove(dialog)
            dialog.grab_release()
            dialog.destroy()
            if self._status.cget("text") == "":
                self._status.configure(text="Stella is working...")

        ttk.Label(
            dialog, text="Approval needed", style="Heading.TLabel"
        ).pack(padx=16, pady=(16, 2), anchor="w")
        ttk.Label(dialog, text="Stella wants to:", style="Dim.TLabel").pack(
            padx=16, anchor="w"
        )
        ttk.Label(
            dialog,
            text=action_summary(request),
            wraplength=420,
            justify="left",
            style="Heading.TLabel",
        ).pack(padx=16, pady=(4, 8), anchor="w")
        if preview is not None and preview.detail_lines:
            # App-computed display of what the exact validated arguments
            # mean (current file content, diff, target). Review material
            # only: the Allow/Cancel answer still binds solely to the
            # dispatcher's ApprovalRequest, never to this text.
            box = tk.Text(
                dialog,
                height=min(14, len(preview.detail_lines) + 1),
                width=72,
                font="TkFixedFont",
                state="disabled",
                wrap="none",
                background=THEME.window,
                foreground=THEME.text_dim,
                relief="flat",
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=THEME.surface_alt,
                padx=8,
                pady=6,
            )
            box.tag_configure("added", foreground=THEME.ok)
            box.tag_configure("removed", foreground=THEME.error)
            box.pack(padx=10, pady=(0, 4))
            dialog.preview_box = box
            lines = list(preview.detail_lines)
            if preview.truncated:
                lines.append("[preview truncated]")
            box.configure(state="normal")
            for line in lines:
                tag = ""
                if line.startswith("+"):
                    tag = "added"
                elif line.startswith("-"):
                    tag = "removed"
                box.insert("end", line + "\n", (tag,) if tag else ())
            box.configure(state="disabled")
        buttons = ttk.Frame(dialog)
        buttons.pack(pady=14)
        cancel_button = ttk.Button(
            buttons, text="Cancel", command=lambda: answer(False)
        )
        allow_button = ttk.Button(
            buttons,
            text="Allow",
            command=lambda: answer(True),
            style="Accent.TButton",
        )
        cancel_button.pack(side="left", padx=6)
        allow_button.pack(side="left", padx=6)
        dialog.bind("<Return>", lambda _event: answer(True))
        dialog.bind("<Escape>", lambda _event: answer(False))
        dialog.protocol("WM_DELETE_WINDOW", lambda: answer(False))
        dialog.transient(self._root)
        dialog.grab_set()
        dialog.focus_set()
        # Widget tests drive the buttons directly; the dialog itself is
        # tracked so closing the window can never strand a blocked turn.
        dialog.allow_button = allow_button
        dialog.cancel_button = cancel_button
        self._dialogs.append(dialog)
        self._status.configure(text="Stella is waiting for your approval...")

    # --------------------------------------------------------- memories

    def _build_memory_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Memories")
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=6, pady=6)
        self._memory_search = ttk.Entry(row, width=28)
        self._memory_search.pack(side="left", fill="x", expand=True)
        ttk.Button(
            row, text="Search", command=self._refresh_memories
        ).pack(side="left", padx=4)
        self._memory_list = tk.Listbox(
            frame, exportselection=False, width=48, height=18
        )
        _style_listbox(self._memory_list)
        self._memory_list.pack(padx=6)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", padx=6, pady=4)
        ttk.Button(
            actions, text="Refresh", command=self._refresh_all_memories
        ).pack(side="left")
        ttk.Button(
            actions, text="Forget selected", command=self._forget_memory
        ).pack(side="left", padx=6)
        self._memory_status = ttk.Label(
            frame, text="", wraplength=340, style="Dim.TLabel"
        )
        self._memory_status.pack(padx=6, pady=4, anchor="w")

    def _refresh_memories(self) -> None:
        self._bridge.post_memories(self._memory_search.get())

    def _refresh_all_memories(self) -> None:
        self._memory_search.delete("0", "end")
        self._bridge.post_memories()

    def _show_memories(self, rows: tuple[str, ...]) -> None:
        self._memory_list.delete(0, "end")
        if not rows:
            self._memory_list.insert("end", "(no memories stored)")
            return
        for content in rows:
            self._memory_list.insert("end", content)

    def _forget_memory(self) -> None:
        selected = self._memory_list.curselection()
        if not selected:
            self._memory_status.configure(
                text="No memory is selected. Pick one from the list first."
            )
            return
        self._bridge.post_forget(selected[0])

    # -------------------------------------------------------- reminders

    def _build_reminder_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Reminders")
        self._reminder_list = tk.Listbox(
            frame, exportselection=False, width=48, height=14
        )
        _style_listbox(self._reminder_list)
        self._reminder_list.pack(padx=6, pady=(6, 2))
        add_row = ttk.Frame(frame)
        add_row.pack(fill="x", padx=6, pady=2)
        ttk.Label(add_row, text="What:").pack(side="left")
        self._reminder_content = ttk.Entry(add_row, width=30)
        self._reminder_content.pack(side="left", fill="x", expand=True)
        due_row = ttk.Frame(frame)
        due_row.pack(fill="x", padx=6, pady=2)
        ttk.Label(due_row, text="Due (ISO):").pack(side="left")
        self._reminder_due = ttk.Entry(due_row, width=30)
        self._reminder_due.pack(side="left", fill="x", expand=True)
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text="Example due time: 2026-01-01T09:00:00+00:00",
        ).pack(padx=6, anchor="w")
        actions = ttk.Frame(frame)
        actions.pack(fill="x", padx=6, pady=4)
        ttk.Button(
            actions, text="Add reminder", command=self._add_reminder
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Cancel selected",
            command=self._cancel_reminder,
        ).pack(side="left", padx=6)
        ttk.Button(
            actions, text="Refresh", command=self._refresh_reminders
        ).pack(side="left")
        self._reminder_status = ttk.Label(frame, text="", wraplength=340)
        self._reminder_status.pack(padx=6, pady=4, anchor="w")

    def _refresh_reminders(self) -> None:
        self._bridge.post_reminders()

    def _show_reminders(self, rows: tuple[tuple[str, str], ...]) -> None:
        self._reminder_rows = rows
        self._reminder_list.delete(0, "end")
        if not rows:
            self._reminder_list.insert("end", "(no pending reminders)")
            return
        for content, due in rows:
            self._reminder_list.insert("end", f"{content} — due {due}")

    def _add_reminder(self) -> None:
        content = self._reminder_content.get().strip()
        due = self._reminder_due.get().strip()
        if not content or not due:
            self._reminder_status.configure(
                text="A reminder needs both a message and a due time."
            )
            return
        self._bridge.post_reminder_add(content, due)
        self._reminder_content.delete(0, "end")
        self._reminder_due.delete(0, "end")

    def _cancel_reminder(self) -> None:
        selected = self._reminder_list.curselection()
        if not selected or selected[0] >= len(self._reminder_rows):
            self._reminder_status.configure(
                text="No reminder is selected. Pick one from the list first."
            )
            return
        content, _due = self._reminder_rows[selected[0]]
        self._bridge.post_reminder_cancel(content)

    # ---------------------------------------------------------- history

    def _build_history_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="History")
        self._history_list = tk.Listbox(
            frame, exportselection=False, width=64, height=18
        )
        _style_listbox(self._history_list)
        self._history_list.pack(padx=6, pady=(6, 2))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", padx=6, pady=4)
        ttk.Button(actions, text="Refresh", command=self._refresh_history).pack(
            side="left"
        )
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text="What Stella recently did, newest first. Kept between "
            "launches; metadata only, never file contents.",
            wraplength=460,
        ).pack(padx=6, anchor="w")

    def _refresh_history(self) -> None:
        self._bridge.post_history()

    def _show_history(self, rows: tuple[str, ...]) -> None:
        self._history_list.delete(0, "end")
        if not rows:
            self._history_list.insert("end", "(no actions recorded yet)")
            return
        for row in rows:
            self._history_list.insert("end", row)

    # --------------------------------------------------------- settings

    def _build_settings_tab(
        self, notebook: ttk.Notebook, settings: StellaSettings
    ) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Settings")
        self._panel_settings = settings
        self._settings_fields: dict[str, ttk.Entry] = {}
        provider_row = ttk.Frame(frame)
        provider_row.pack(fill="x", padx=6, pady=4)
        ttk.Label(provider_row, text="Provider:").pack(side="left")
        self._provider = ttk.Combobox(
            provider_row,
            values=["openai", "ollama", "llama"],
            state="readonly",
            width=12,
        )
        self._provider.set(settings.provider)
        self._provider.pack(side="left", padx=6)
        self._connection_status = ttk.Label(
            provider_row, text=self._current_state_text(), wraplength=280
        )
        self._connection_status.pack(side="left", padx=6)
        for label, value, masked in (
            ("Model", settings.model or "", False),
            ("API key", "", True),
            ("OpenAI base URL", settings.openai_base_url or "", False),
            ("Ollama base URL", settings.ollama_base_url, False),
            ("Memory DB", settings.memory_db, False),
            ("Reminders DB", settings.reminders_db, False),
            ("Workspace", settings.workspace, False),
        ):
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=6, pady=2)
            ttk.Label(row, text=f"{label}:", width=16).pack(side="left")
            entry = ttk.Entry(row, width=34, show="*" if masked else "")
            entry.insert("0", value)
            entry.pack(side="left", fill="x", expand=True)
            self._settings_fields[label] = entry
        self._transcripts_var = tk.BooleanVar(
            value=settings.transcripts_enabled
        )
        ttk.Checkbutton(
            frame,
            text="Record transcripts for persona reflection",
            variable=self._transcripts_var,
        ).pack(padx=6, pady=(4, 0), anchor="w")
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text=(
                "Conversation text is stored in a bounded local file; "
                "'stella reflect' turns it into style proposals that "
                "only take effect if you approve them."
            ),
            wraplength=340,
        ).pack(padx=6, anchor="w")
        self._semantic_var = tk.BooleanVar(
            value=settings.semantic_memory_enabled
        )
        ttk.Checkbutton(
            frame,
            text="Semantic memory recall (embedding index)",
            variable=self._semantic_var,
        ).pack(padx=6, pady=(4, 0), anchor="w")
        provider_row = ttk.Frame(frame)
        provider_row.pack(padx=6, pady=(4, 0), anchor="w")
        ttk.Label(provider_row, text="Embedding:").pack(side="left")
        initial_label = _SEMANTIC_PROVIDER_LABELS[
            settings.semantic_provider
        ]
        self._semantic_provider_var = tk.StringVar(value=initial_label)
        ttk.OptionMenu(
            provider_row,
            self._semantic_provider_var,
            initial_label,
            *_SEMANTIC_PROVIDER_LABELS.values(),
        ).pack(side="left")
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text=(
                "Stored approved memories are additionally indexed by the "
                "chosen embedding so related phrasings can still be found; "
                "matches are always labeled as weaker hints, never as "
                "understanding. Ollama uses a local embedding model "
                "(STELLA_EMBED_MODEL); MiniLM needs the stella[embed] extra "
                "and runs on CPU."
            ),
            wraplength=340,
        ).pack(padx=6, anchor="w")
        self._os_tools_var = tk.BooleanVar(
            value=settings.os_tools_enabled
        )
        ttk.Checkbutton(
            frame,
            text="Desktop awareness (screen read, focus, typing)",
            variable=self._os_tools_var,
        ).pack(padx=6, pady=(4, 0), anchor="w")
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text=(
                "Adds screen_read, window_focus and key_send on a Hyprland "
                "desktop. Every use asks you first, with the exact window "
                "named; screen text is read by local OCR only and typing "
                "only ever goes to a window Stella just focused."
            ),
            wraplength=340,
        ).pack(padx=6, anchor="w")
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text=(
                "The API key applies to this session only and is never "
                "saved or shown again; to keep it permanently, export "
                "OPENAI_API_KEY."
            ),
            wraplength=340,
        ).pack(padx=6, anchor="w")
        actions = ttk.Frame(frame)
        actions.pack(fill="x", padx=6, pady=6)
        ttk.Button(actions, text="Apply", command=self._apply_settings).pack(
            side="left"
        )
        ttk.Button(
            actions, text="Test connection", command=self._test_connection
        ).pack(side="left", padx=6)
        ttk.Button(
            actions, text="List models", command=self._list_models
        ).pack(side="left")
        self._settings_status = ttk.Label(frame, text="", wraplength=340)
        self._settings_status.pack(padx=6, pady=4, anchor="w")

    def _current_state_text(self) -> str:
        model = self._panel_settings.model or "(not set)"
        return f"Provider: {self._panel_settings.provider}  Model: {model}"

    def _draft_settings(self) -> StellaSettings:
        fields = self._settings_fields
        # Replace over the panel's settings so voice fields that the panel
        # does not show (modes, models, commands) survive an Apply click.
        return replace(
            self._panel_settings,
            provider=self._provider.get(),
            model=fields["Model"].get().strip() or None,
            openai_base_url=fields["OpenAI base URL"].get().strip() or None,
            ollama_base_url=fields["Ollama base URL"].get().strip()
            or DEFAULT_OLLAMA_BASE_URL,
            memory_db=fields["Memory DB"].get().strip(),
            reminders_db=fields["Reminders DB"].get().strip(),
            workspace=fields["Workspace"].get().strip(),
            transcripts_enabled=self._transcripts_var.get(),
            semantic_memory_enabled=self._semantic_var.get(),
            os_tools_enabled=self._os_tools_var.get(),
            semantic_provider=_SEMANTIC_PROVIDER_BY_LABEL[
                self._semantic_provider_var.get()
            ],
        )

    def _entered_key(self) -> str:
        return self._settings_fields["API key"].get()

    def _apply_key_to_environment(self) -> None:
        # Session-scoped on purpose: Stella has no secure credential
        # store, so the key is never persisted. The build path reads it
        # from the environment exactly like the CLI always has.
        key = self._entered_key()
        if key:
            os.environ["OPENAI_API_KEY"] = key
            self._settings_fields["API key"].delete("0", "end")

    def _apply_settings(self) -> None:
        applied = self._draft_settings()
        self._apply_key_to_environment()
        self._settings_status.configure(text="Restarting Stella with these settings...")
        self._bridge.post_apply_settings(applied)

    def _test_connection(self) -> None:
        draft = self._draft_settings()
        if not draft.model:
            self._settings_status.configure(text="Enter a model first.")
            return
        result = config.test_connection(
            provider=draft.provider,
            model=draft.model,
            ollama_base_url=draft.ollama_base_url,
            openai_base_url=draft.openai_base_url,
            api_key=self._entered_key() or None,
            llama_binary=draft.llama_binary,
        )
        self._settings_status.configure(
            text=result.message if result.message else "Not connected."
        )
        self._connection_status.configure(
            text=(
                f"{self._current_state_text()}  "
                f"Status: {'Connected' if result.ok else 'Not connected'}"
            )
        )

    def _list_models(self) -> None:
        draft = self._draft_settings()
        if draft.provider != "ollama":
            self._settings_status.configure(
                text="Model listing is only available for a local Ollama server."
            )
            return
        scan = config.scan_ollama_models(draft.ollama_base_url)
        self._settings_status.configure(text=scan.message)
        if scan.models:
            self._settings_fields["Model"].delete("0", "end")
            self._settings_fields["Model"].insert(
                "0", scan.models[0]
            )

    # ---------------------------------------------------------- shutdown

    def _on_close(self) -> None:
        self._bridge.stop()
        self._root.destroy()


class SetupDialog:
    """First-run model configuration, before any Stella application exists.

    Offers the three shapes the existing provider layer already supports:
    a local Ollama server, the OpenAI API, or any OpenAI-compatible
    endpoint (the same ``openai`` provider with a base URL). The chosen
    configuration is only saved after a successful connection test, and
    an entered API key stays in this process's environment: Stella has no
    secure credential store, so keys are never persisted.
    """

    MODES = ("ollama", "openai", "compatible")

    def __init__(self, parent: tk.Misc) -> None:
        self.result: StellaSettings | None = None
        self._parent = parent
        self._tested_draft: tuple[object, ...] | None = None
        dialog = tk.Toplevel(parent)
        dialog.title("Welcome to Stella")
        dialog.resizable(False, False)
        dialog.configure(background=THEME.surface)
        _configure_styles(parent)
        self._dialog = dialog
        ttk.Label(
            dialog,
            text="Welcome to Stella",
            style="Brand.TLabel",
        ).pack(padx=16, pady=(16, 2), anchor="w")
        ttk.Label(
            dialog,
            text="How would you like Stella to run?",
            style="Dim.TLabel",
            justify="left",
        ).pack(padx=16, pady=(0, 6), anchor="w")
        self._mode = tk.StringVar(value="ollama")
        choices = ttk.Frame(dialog)
        choices.pack(fill="x", padx=16)
        for mode, label in (
            ("ollama", "Local model (Ollama)"),
            ("openai", "OpenAI API"),
            ("compatible", "Other OpenAI-compatible API"),
        ):
            ttk.Radiobutton(
                choices,
                text=label,
                value=mode,
                variable=self._mode,
                command=self._mode_changed,
            ).pack(anchor="w")
        fields = ttk.Frame(dialog)
        fields.pack(fill="x", padx=16, pady=6)
        self._fields: dict[str, ttk.Entry] = {}
        for label in ("Model", "Ollama base URL", "API base URL", "API key"):
            row = ttk.Frame(fields)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"{label}:", width=15).pack(side="left")
            entry = ttk.Entry(
                row, width=38, show="*" if label == "API key" else ""
            )
            entry.pack(side="left", fill="x", expand=True)
            self._fields[label] = entry
        self._fields["Ollama base URL"].insert("0", DEFAULT_OLLAMA_BASE_URL)
        for label in ("Model", "Ollama base URL", "API base URL"):
            # Editing the configuration invalidates a previous test, so
            # the user can never start from a stale success.
            self._fields[label].bind(
                "<KeyRelease>", lambda _event: self._mark_untested()
            )
        self._model_list = tk.Listbox(fields, height=5, width=55,
                                      exportselection=False)
        _style_listbox(self._model_list)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=16, pady=4)
        self._refresh_button = ttk.Button(
            buttons, text="Refresh models", command=self.refresh_models
        )
        self._refresh_button.pack(side="left")
        self._test_button = ttk.Button(
            buttons, text="Test connection", command=self.test_connection
        )
        self._test_button.pack(side="left", padx=6)
        self._finish_button = ttk.Button(
            buttons, text="Start Stella", command=self.finish, state="disabled",
            style="Accent.TButton",
        )
        self._finish_button.pack(side="left")
        self.status = ttk.Label(
            dialog,
            text="Pick a model, then test the connection.",
            wraplength=420,
            justify="left",
            style="Dim.TLabel",
        )
        self.status.pack(padx=16, pady=(4, 16), anchor="w")
        self._model_list.bind("<<ListboxSelect>>", self._model_selected)
        self._mode_changed()
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        # No transient(): the parent root is withdrawn, and a transient
        # window of an unmapped parent is never mapped under XWayland
        # compositors, leaving first-run setup invisible and blocking
        # wait_window forever.
        dialog.grab_set()

    # ----------------------------------------------------------- widgets

    def _mode_changed(self) -> None:
        mode = self._mode.get()
        self._show(self._fields["Ollama base URL"], mode == "ollama")
        self._show(self._fields["API base URL"], mode == "compatible")
        self._show(self._fields["API key"], mode in {"openai", "compatible"})
        self._show(self._model_list, mode == "ollama")
        self._refresh_button.configure(state="normal" if mode == "ollama" else "disabled")
        self._mark_untested()

    @staticmethod
    def _show(widget: tk.Misc, visible: bool) -> None:
        if visible:
            widget.pack(fill="x" if isinstance(widget, tk.Listbox) else None,
                        pady=2)
        else:
            widget.pack_forget()

    def _mark_untested(self) -> None:
        # Any change to provider, endpoint, model, or key invalidates a
        # previous success: only a tested configuration may start Stella.
        self._tested_draft = None
        self._finish_button.configure(state="disabled")

    def _model_selected(self, _event: object) -> None:
        selected = self._model_list.curselection()
        if selected:
            name = self._model_list.get(selected[0])
            self._fields["Model"].delete("0", "end")
            self._fields["Model"].insert("0", name)

    def _draft(self) -> tuple[str, StellaSettings]:
        mode = self._mode.get()
        provider = "ollama" if mode == "ollama" else "openai"
        openai_base_url = (
            self._fields["API base URL"].get().strip() or None
            if mode == "compatible"
            else None
        )
        settings = StellaSettings.from_saved(
            provider=provider,
            model=self._fields["Model"].get().strip(),
            ollama_base_url=self._fields["Ollama base URL"].get().strip()
            or DEFAULT_OLLAMA_BASE_URL,
            openai_base_url=openai_base_url,
        )
        return mode, settings

    # ------------------------------------------------------------ probes

    def refresh_models(self) -> None:
        scan = config.scan_ollama_models(
            self._fields["Ollama base URL"].get().strip()
            or DEFAULT_OLLAMA_BASE_URL
        )
        self._model_list.delete(0, "end")
        for name in scan.models:
            self._model_list.insert("end", name)
        self.status.configure(text=scan.message)
        self._mark_untested()

    def _draft_signature(self) -> tuple[object, ...]:
        _mode, draft = self._draft()
        return (
            draft.provider,
            draft.model,
            draft.ollama_base_url,
            draft.openai_base_url,
        )

    def test_connection(self) -> None:
        mode, draft = self._draft()
        api_key = self._fields["API key"].get() if mode != "ollama" else ""
        if mode != "ollama" and not api_key:
            existing = os.environ.get("OPENAI_API_KEY")
            if existing:
                api_key = existing
                self.status.configure(
                    text="Using the OPENAI_API_KEY already set in the environment."
                )
        result = config.test_connection(
            provider=draft.provider,
            model=draft.model or "",
            ollama_base_url=draft.ollama_base_url,
            openai_base_url=draft.openai_base_url,
            api_key=api_key or None,
        )
        if result.ok and api_key:
            # Session-scoped on purpose; never written to the config file.
            os.environ["OPENAI_API_KEY"] = api_key
            self._fields["API key"].delete("0", "end")
        self._tested_draft = self._draft_signature() if result.ok else None
        self._finish_button.configure(
            state="normal" if result.ok else "disabled"
        )
        self.status.configure(
            text=result.message if result.ok else f"Not connected: {result.message}"
        )

    def finish(self) -> None:
        if (
            self._tested_draft is None
            or self._draft_signature() != self._tested_draft
        ):
            self._mark_untested()
            return
        _mode, draft = self._draft()
        config.save_configuration(draft)
        self.result = draft
        self._dialog.destroy()

    def run(self) -> StellaSettings | None:
        self._parent.wait_window(self._dialog)
        return self.result


def main() -> None:
    """Launch the Stella desktop window."""

    apply_theme(load_theme_choice())
    settings = config.resolve_settings()
    if settings is None:
        root = tk.Tk()
        root.withdraw()
        settings = SetupDialog(root).run()
        if settings is None:
            root.destroy()
            return
        root.destroy()
    bridge = StellaBridge(lambda: build_application(settings))
    root = tk.Tk()
    StellaWindow(root, bridge, settings)
    try:
        root.mainloop()
    finally:
        bridge.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
