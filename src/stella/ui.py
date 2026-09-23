"""Local Tk graphical interface for Stella.

The window is a thin view over the shared application layer: every
conversation turn, memory edit, reminder change, and approval answer is
posted through ``StellaBridge``, whose single worker thread owns the
trusted Stella core. This module never touches files, databases, tools,
or the LLM directly, and it never manufactures authorization: approvals
are produced by the existing dispatcher, and the UI only ever answers the
exact request the dispatcher raised (closing a dialog denies it).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

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
        self._settings_fields: dict[str, ttk.Entry] = {}
        provider_row = ttk.Frame(frame)
        provider_row.pack(fill="x", padx=6, pady=4)
        ttk.Label(provider_row, text="Provider:").pack(side="left")
        self._provider = ttk.Combobox(
            provider_row, values=["openai", "ollama"], state="readonly", width=12
        )
        self._provider.set(settings.provider)
        self._provider.pack(side="left", padx=6)
        for label, value in (
            ("Model", settings.model or ""),
            ("OpenAI base URL", settings.openai_base_url or ""),
            ("Ollama base URL", settings.ollama_base_url),
            ("Memory DB", settings.memory_db),
            ("Reminders DB", settings.reminders_db),
            ("Workspace", settings.workspace),
        ):
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=6, pady=2)
            ttk.Label(row, text=f"{label}:", width=16).pack(side="left")
            entry = ttk.Entry(row, width=34)
            entry.insert("0", value)
            entry.pack(side="left", fill="x", expand=True)
            self._settings_fields[label] = entry
        actions = ttk.Frame(frame)
        actions.pack(fill="x", padx=6, pady=6)
        ttk.Button(actions, text="Apply", command=self._apply_settings).pack(
            side="left"
        )
        self._settings_status = ttk.Label(frame, text="", wraplength=340)
        self._settings_status.pack(padx=6, pady=4, anchor="w")

    def _apply_settings(self) -> None:
        fields = self._settings_fields
        applied = StellaSettings(
            provider=self._provider.get(),
            model=fields["Model"].get().strip() or None,
            openai_base_url=fields["OpenAI base URL"].get().strip() or None,
            ollama_base_url=fields["Ollama base URL"].get().strip(),
            memory_db=fields["Memory DB"].get().strip(),
            reminders_db=fields["Reminders DB"].get().strip(),
            workspace=fields["Workspace"].get().strip(),
        )
        self._settings_status.configure(text="Restarting Stella with these settings...")
        self._bridge.post_apply_settings(applied)

    # ---------------------------------------------------------- shutdown

    def _on_close(self) -> None:
        self._bridge.stop()
        self._root.destroy()


def main() -> None:
    """Launch the Stella desktop window."""

    try:
        settings = StellaSettings.from_environment()
    except SystemExit as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Stella cannot start", str(error))
        root.destroy()
        return
    bridge = StellaBridge(lambda: build_application(settings))
    root = tk.Tk()
    StellaWindow(root, bridge, settings)
    try:
        root.mainloop()
    finally:
        bridge.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
