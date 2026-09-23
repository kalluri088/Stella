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
import tkinter as tk
from dataclasses import replace
from tkinter import ttk

from stella import config
from stella.app import (
    OutcomeStatus,
    StellaBridge,
    StellaSettings,
    TurnOutcome,
    UiEvent,
    build_application,
    outcome_status,
)
from stella.cli import _action_summary
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL
from stella.tools import ApprovalRequest


class StellaWindow:
    """One Tk window driven entirely by posted bridge commands."""

    def __init__(
        self, root: tk.Tk, bridge: StellaBridge, settings: StellaSettings
    ) -> None:
        self._bridge = bridge
        self._root = root
        self._busy = False
        self._listening = False
        self._speaking = False
        self._dialogs: list[tk.Toplevel] = []
        self._reminder_rows: tuple[tuple[str, str], ...] = ()
        root.title("Stella")
        root.geometry("980x620")

        main = ttk.Frame(root)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        chat = ttk.Frame(main)
        chat.pack(side="left", fill="both", expand=True)
        transcript_frame = ttk.Frame(chat)
        transcript_frame.pack(fill="both", expand=True)
        self._chat = tk.Text(
            transcript_frame, wrap="word", state="disabled", height=18
        )
        scrollbar = ttk.Scrollbar(
            transcript_frame, command=self._chat.yview
        )
        self._chat.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._chat.pack(side="left", fill="both", expand=True)
        self._status = ttk.Label(chat, text="", anchor="w")
        self._status.pack(fill="x")
        input_frame = ttk.Frame(chat)
        input_frame.pack(fill="x")
        self._input = tk.Text(input_frame, height=3, wrap="word")
        self._input.pack(side="left", fill="both", expand=True)
        send_button = ttk.Button(
            input_frame, text="Send", command=self._send
        )
        send_button.pack(side="left", fill="y", padx=(4, 0))
        self._input.bind("<Control-Return>", lambda _event: self._send())
        ttk.Label(
            chat,
            text="Ctrl+Enter sends; Enter adds a new line.",
        ).pack(anchor="w")
        self._build_voice_row(chat, bridge)

        notebook = ttk.Notebook(main)
        notebook.pack(side="left", fill="y", padx=(8, 0))
        self._build_memory_tab(notebook)
        self._build_reminder_tab(notebook)
        self._build_settings_tab(notebook, settings)

        self._line(
            "Ask Stella anything. The tabs manage memories, reminders, "
            "and the minimal local settings."
        )
        bridge.post_memories()
        bridge.post_reminders()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._tick)

    # ----------------------------------------------------------- chat

    def _line(self, text: str) -> None:
        self._chat.configure(state="normal")
        self._chat.insert("end", text + "\n\n", "see")
        self._chat.configure(state="disabled")
        self._chat.see("end")

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
        self._line(f"You: {user_input}")
        self._busy = True
        self._status.configure(text="Stella is thinking...")
        self._bridge.post_turn(user_input)

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
            text=(
                "Voice input is not available here (no capture command or "
                "transcription provider)."
                if not mic_ok
                else "Listening and speaking are explicit; recordings are "
                "removed right after transcription."
            ),
        ).pack(anchor="w")

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
            self._mic_button.configure(text="Stop")
            self._mic_cancel.configure(state="normal")
            self._status.configure(text="Listening...")
        elif state == "transcribing":
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
            self._line(f"✗ Stella sent an unexpected voice state: {state}")

    def _handle_voice_error(self, message: str) -> None:
        # A voice failure never corrupts conversation state: the transcript
        # simply was not sent, and any playback state is reset.
        self._listening = False
        self._speaking = False
        self._mic_button.configure(text="Listen")
        self._mic_cancel.configure(state="disabled")
        self._stop_speaking.configure(state="disabled")
        self._line(f"✗ {message}")
        if not self._busy:
            self._status.configure(text="")

    def _handle_turn(self, outcome: TurnOutcome) -> None:
        self._busy = False
        self._status.configure(text="")
        if outcome.interrupted:
            self._line("Stella stopped that request. Nothing was changed.")
            return
        if outcome.error_message is not None:
            self._line(f"✗ {outcome.error_message}")
            return
        if outcome.response is not None:
            self._line(f"Stella: {outcome.response}")
        else:
            self._line("Stella has nothing to add.")
        if outcome.result is not None and outcome.result.tool_result is not None:
            status = outcome_status(outcome.result.tool_result)
            self._line(f"Action outcome: {status.symbol} {status.kind}")

    # ------------------------------------------------------ event pump

    def _tick(self) -> None:
        for event in self._bridge.poll():
            self._handle_event(event)
        self._drain_approvals()
        self._root.after(100, self._tick)

    def _handle_event(self, event: UiEvent) -> None:
        kind, payload = event.kind, event.payload
        if kind == "turn":
            self._handle_turn(payload)
        elif kind == "reminder_delivered":
            self._line(f"Reminder: {payload}")
        elif kind == "memories":
            self._show_memories(payload)
        elif kind == "memory_result":
            self._memory_status.configure(text=str(payload))
        elif kind == "reminders":
            self._show_reminders(payload)
        elif kind == "reminder_result":
            status: OutcomeStatus = payload
            self._reminder_status.configure(
                text=f"{status.symbol} {status.detail}"
            )
        elif kind == "settings":
            self._line(f"(settings) {payload}")
        elif kind == "voice_state":
            self._handle_voice_state(str(payload))
        elif kind == "voice_transcript":
            self._line(f"You (voice): {payload}")
            self._busy = True
            self._status.configure(text="Stella is thinking...")
        elif kind == "voice_error":
            self._handle_voice_error(str(payload))
        elif kind == "error":
            self._line(f"✗ {payload}")
        else:  # pragma: no cover - unknown kinds must not appear
            self._line(f"✗ Stella sent an unexpected update: {kind}")

    # ------------------------------------------------------- approvals

    def _drain_approvals(self) -> None:
        while True:
            pending = self._bridge.next_approval_request()
            if pending is None:
                return
            token, request = pending
            self._show_approval(token, request)

    def _show_approval(
        self, token: int, request: ApprovalRequest
    ) -> None:
        dialog = tk.Toplevel(self._root)
        dialog.title("Stella needs approval")
        dialog.resizable(False, False)

        def answer(approved: bool) -> None:
            self._bridge.resolve_approval(token, approved)
            if dialog in self._dialogs:
                self._dialogs.remove(dialog)
            dialog.grab_release()
            dialog.destroy()
            if self._status.cget("text") == "":
                self._status.configure(text="Stella is thinking...")

        ttk.Label(dialog, text="Stella wants to:").pack(padx=10, pady=(10, 0))
        ttk.Label(
            dialog,
            text=_action_summary(request),
            wraplength=420,
            justify="left",
        ).pack(padx=10, pady=6)
        buttons = ttk.Frame(dialog)
        buttons.pack(pady=10)
        cancel_button = ttk.Button(
            buttons, text="Cancel", command=lambda: answer(False)
        )
        allow_button = ttk.Button(
            buttons, text="Allow", command=lambda: answer(True)
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
        self._memory_list.pack(padx=6)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", padx=6, pady=4)
        ttk.Button(
            actions, text="Refresh", command=self._refresh_all_memories
        ).pack(side="left")
        ttk.Button(
            actions, text="Forget selected", command=self._forget_memory
        ).pack(side="left", padx=6)
        self._memory_status = ttk.Label(frame, text="", wraplength=340)
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
            provider_row, values=["openai", "ollama"], state="readonly", width=12
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
        ttk.Label(
            frame,
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
        self._dialog = dialog
        ttk.Label(
            dialog,
            text=(
                "Welcome to Stella.\n\n"
                "How would you like Stella to run?"
            ),
            justify="left",
        ).pack(padx=12, pady=(12, 4), anchor="w")
        self._mode = tk.StringVar(value="ollama")
        choices = ttk.Frame(dialog)
        choices.pack(fill="x", padx=12)
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
        fields.pack(fill="x", padx=12, pady=6)
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
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=2)
        self._refresh_button = ttk.Button(
            buttons, text="Refresh models", command=self.refresh_models
        )
        self._refresh_button.pack(side="left")
        self._test_button = ttk.Button(
            buttons, text="Test connection", command=self.test_connection
        )
        self._test_button.pack(side="left", padx=6)
        self._finish_button = ttk.Button(
            buttons, text="Start Stella", command=self.finish, state="disabled"
        )
        self._finish_button.pack(side="left")
        self.status = ttk.Label(
            dialog,
            text="Pick a model, then test the connection.",
            wraplength=420,
            justify="left",
        )
        self.status.pack(padx=12, pady=(2, 12), anchor="w")
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
