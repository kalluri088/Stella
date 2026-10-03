"""Local Tk graphical interface for Stella.

The window is a thin view over the shared application layer: every
conversation turn, memory edit, and approval answer is
posted through ``StellaBridge``, whose single worker thread owns the
trusted Stella core. This module never touches files, databases, tools,
or the LLM directly, and it never manufactures authorization: approvals
are produced by the existing dispatcher, and the UI only ever answers the
exact request the dispatcher raised (closing a dialog denies it).

The one deliberate exception is ``SetupDialog``, shown before any Stella
application exists: it runs bounded provider probes, saves Stella's
non-secret configuration file, and stores a verified API key in the
private key file owned by ``stella.provider_keys`` (never in
config.json, never in the environment). It never touches tools, memory
or approvals, and a successful setup grants nothing beyond
"Stella can talk to this model".

Typed slash commands are intercepted in the view through
``stella.commands`` (a pure classifier and renderer that reads only the
user's own template files); a command never reaches the bridge, and a
template's expansion re-enters as an ordinary turn. Voice transcripts
take a different path and are never command-parsed.
"""

from __future__ import annotations

import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import ttk

from stella import config, provider_keys
from stella.app import (
    StellaBridge,
    StellaSettings,
    TurnOutcome,
    UiEvent,
    build_application,
    default_data_dir,
    outcome_status,
)
from stella.commands import (
    CommandCall,
    expand_template,
    help_lines,
    load_template_body,
    parse_command_line,
    parse_limit,
    status_lines,
    suggest_commands,
    version_line,
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
    rail: str
    surface: str
    card: str
    surface_alt: str
    border: str
    field: str
    text: str
    text_dim: str
    accent: str
    accent_strong: str
    accent_hover: str
    on_accent: str
    error: str
    ok: str
    warning: str
    # The user's quote band. Stella's reply carries no band and no
    # label at all: the transcript separates roles as a terminal does —
    # a full-width "> " blockquote for the user, plain left-aligned
    # text for the model.
    user_quote: str
    # The ">" marker's color is an explicit theme field, not an alias
    # of the brand accent: accent == ok in every palette, and a marker
    # derived from brand fields would read as just another teal widget.
    user_head: str


_DARK_THEME = Theme(
    window="#141210",
    rail="#141210",
    surface="#141210",
    card="#1f1c1a",
    surface_alt="#33302d",
    border="#33302d",
    field="#141210",
    text="#e7e5e4",
    text_dim="#a8a29e",
    accent="#2dd4bf",
    accent_strong="#2dd4bf",
    accent_hover="#5eead4",
    on_accent="#141210",
    error="#ef4444",
    ok="#2dd4bf",
    warning="#d97706",
    user_quote="#292524",
    user_head="#e7e5e4",
)

_LIGHT_THEME = Theme(
    window="#ffffff",
    rail="#fafaf9",
    surface="#fafaf9",
    card="#ffffff",
    surface_alt="#e7e5e4",
    border="#e7e5e4",
    field="#f5f5f4",
    text="#1c1917",
    text_dim="#78716c",
    accent="#0d9488",
    accent_strong="#0d9488",
    accent_hover="#0f766e",
    on_accent="#ffffff",
    error="#dc2626",
    ok="#0d9488",
    warning="#d97706",
    # The single quote band sits on a pure-white transcript, so it must be
    # visibly apart from the background; the near-invisible off-white
    # (#f5f5f4) of the old two-band design washed out against #ffffff.
    user_quote="#e7e5e4",
    user_head="#1c1917",
)

THEMES: dict[str, Theme] = {"dark": _DARK_THEME, "light": _LIGHT_THEME}
DEFAULT_THEME = "dark"

# Frame characters for the working-status spinner (100 ms tick).
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

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
        borderwidth=0,
        padding=6,
    )
    style.configure("Toplevel", background=THEME.surface)
    style.configure("TFrame", background=THEME.surface)
    style.configure("Rail.TFrame", background=THEME.rail)
    style.configure("Card.TFrame", background=THEME.card)
    style.configure("TLabel", background=THEME.surface, foreground=THEME.text)
    style.configure("Card.TLabel", background=THEME.card)
    style.configure(
        "CardDim.TLabel", background=THEME.card, foreground=THEME.text_dim
    )
    style.configure("Dim.TLabel", foreground=THEME.text_dim)
    # Approval-dialog advisory line (mismatch warning): same surface as
    # TLabel, but impossible to mistake for the action heading.
    style.configure("Warning.TLabel", foreground=THEME.warning)
    # Rail labels sit on the sidebar color, not the content surface.
    style.configure(
        "RailDim.TLabel", background=THEME.rail, foreground=THEME.text_dim
    )
    style.configure(
        "RailBrand.TLabel",
        background=THEME.rail,
        foreground=THEME.accent,
        font=(ui_family, ui_size + 4, "bold"),
    )
    style.configure(
        "Status.TLabel",
        foreground=THEME.accent,
        font=(ui_family, ui_size, "italic"),
    )
    style.configure(
        "Brand.TLabel",
        foreground=THEME.accent,
        font=(ui_family, ui_size + 4, "bold"),
    )
    style.configure(
        "Section.TLabel",
        font=(ui_family, ui_size + 2, "bold"),
    )
    style.configure(
        "SectionCard.TLabel",
        background=THEME.card,
        font=(ui_family, ui_size + 2, "bold"),
    )
    style.configure(
        "Heading.TLabel", font=(ui_family, ui_size + 1, "bold")
    )
    # Flat buttons: quiet fill, a line-color border, accent on hover.
    style.configure(
        "TButton",
        background=THEME.field,
        foreground=THEME.text,
        bordercolor=THEME.surface_alt,
        lightcolor=THEME.surface_alt,
        darkcolor=THEME.surface_alt,
        focusthickness=0,
        relief="flat",
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
        relief="flat",
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", THEME.surface_alt), ("active", THEME.accent_hover)],
        foreground=[("disabled", THEME.text_dim)],
    )
    # Navigation rail: flat full-width entries, the active one lifted
    # onto the panel surface with the accent color.
    style.configure(
        "Nav.TButton",
        background=THEME.rail,
        foreground=THEME.text_dim,
        bordercolor=THEME.rail,
        lightcolor=THEME.rail,
        darkcolor=THEME.rail,
        focusthickness=0,
        relief="flat",
        anchor="w",
        padding=(16, 9),
    )
    style.map(
        "Nav.TButton",
        background=[("active", THEME.card)],
        foreground=[("active", THEME.text)],
    )
    style.configure(
        "NavActive.TButton",
        background=THEME.card,
        foreground=THEME.accent,
        bordercolor=THEME.card,
        lightcolor=THEME.card,
        darkcolor=THEME.card,
        font=(ui_family, ui_size, "bold"),
        anchor="w",
        padding=(16, 9),
    )
    style.configure(
        "TMenubutton",
        background=THEME.field,
        foreground=THEME.text,
        bordercolor=THEME.field,
        lightcolor=THEME.field,
        darkcolor=THEME.field,
        focusthickness=0,
        relief="flat",
        padding=(10, 5),
    )
    style.map("TMenubutton", background=[("active", THEME.accent_strong)])
    style.configure(
        "TEntry",
        insertcolor=THEME.text,
        bordercolor=THEME.border,
        lightcolor=THEME.border,
        darkcolor=THEME.border,
        fieldbackground=THEME.field,
        foreground=THEME.text,
        padding=7,
    )
    style.configure("TSeparator", background=THEME.border)
    style.configure(
        "TCheckbutton", background=THEME.surface, foreground=THEME.text, padding=4
    )
    style.map(
        "TCheckbutton",
        background=[("active", THEME.surface)],
        foreground=[("disabled", THEME.text_dim)],
        indicatorcolor=[("selected", THEME.accent)],
    )
    style.configure("Card.TCheckbutton", background=THEME.card)
    style.map(
        "Card.TCheckbutton",
        background=[("active", THEME.card)],
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
        bordercolor=THEME.border,
        lightcolor=THEME.border,
        darkcolor=THEME.border,
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
        troughcolor=THEME.card,
        bordercolor=THEME.card,
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
        highlightthickness=1,
        highlightbackground=THEME.border,
        highlightcolor=THEME.border,
        borderwidth=0,
        relief="flat",
        activestyle="none",
        selectborderwidth=0,
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
        self._pulse = 0
        self._turn_activity = ""
        self._dialogs: list[tk.Toplevel] = []
        root.title("Stella")
        root.geometry("1180x680")
        root.minsize(920, 560)
        root.configure(background=THEME.window)
        _configure_styles(root)

        # Layout: a full-height navigation rail on the left, one section
        # of content on the right. The rail replaces the old notebook so
        # every panel gets the full window instead of a narrow side tab.
        outer = ttk.Frame(root)
        outer.pack(fill="both", expand=True)

        rail = ttk.Frame(outer, style="Rail.TFrame", width=180)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        brand = ttk.Frame(rail, style="Rail.TFrame")
        brand.pack(fill="x", pady=(18, 14), padx=4)
        ttk.Label(
            brand, text="✦  Stella", style="RailBrand.TLabel"
        ).pack(side="left", padx=(16, 0))
        ttk.Label(
            brand, text="local-first", style="RailDim.TLabel"
        ).pack(side="left", padx=(8, 0), pady=(5, 0))
        self._nav_buttons: dict[str, ttk.Button] = {}
        self._sections: dict[str, ttk.Frame] = {}
        self._section = "chat"
        for key, label in (
            ("chat", "✎  Chat"),
            ("memories", "▤  Memories"),
            ("history", "≡  History"),
            ("settings", "⚙  Settings"),
        ):
            button = ttk.Button(
                rail, text=label, style="Nav.TButton",
                command=lambda name=key: self._show_section(name),
            )
            button.pack(fill="x", padx=10, pady=1)
            self._nav_buttons[key] = button
        self._theme_button = ttk.Button(
            rail, text=self._theme_button_text(),
            command=self._toggle_theme, style="Nav.TButton",
        )
        self._theme_button.pack(side="bottom", fill="x", padx=10, pady=(0, 14))
        ttk.Label(
            rail,
            text="your conversation stays\non this machine",
            style="RailDim.TLabel",
            justify="left",
        ).pack(side="bottom", padx=16, pady=(0, 10), anchor="w")

        content = ttk.Frame(outer)
        # No horizontal padding here: the chat page's quote band must
        # reach the window edge. The other pages get their margin in
        # _show_section, where the padding is page-specific.
        content.pack(side="left", fill="both", expand=True, padx=0, pady=14)

        chat = ttk.Frame(content)
        self._sections["chat"] = chat
        transcript_card = ttk.Frame(chat, style="Card.TFrame")
        transcript_card.pack(fill="both", expand=True)
        self._chat = tk.Text(
            transcript_card,
            wrap="word",
            state="disabled",
            background=THEME.window,
            foreground=THEME.text,
            insertbackground=THEME.text,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=THEME.border,
            relief="flat",
            # No horizontal internal padding: the user's quote band is
            # painted across the whole display line, and padx would stop
            # it short of the window edge. Text indentation lives in the
            # tags' lmargin instead.
            padx=0,
            pady=14,
            font="TkTextFont",
            spacing1=4,
            spacing2=2,
        )
        scrollbar = ttk.Scrollbar(
            transcript_card, command=self._chat.yview
        )
        self._chat.configure(yscrollcommand=scrollbar.set)
        # The scrollbar floats over the transcript instead of packing
        # beside it: the quote band paints the whole display line, and
        # a packed scrollbar would reserve a dead column at the right
        # edge. Text stays clear of it through the tags' rmargin —
        # backgrounds ignore margins and still run edge to edge.
        scrollbar.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        self._chat.pack(fill="both", expand=True)
        self._configure_chat_tags()
        self._status = ttk.Label(chat, text="", anchor="w",
                                 style="Status.TLabel")
        self._status.pack(fill="x", padx=16, pady=(10, 4))
        composer = ttk.Frame(chat, style="Card.TFrame")
        composer.pack(fill="x")
        input_row = ttk.Frame(composer, style="Card.TFrame")
        input_row.pack(fill="x", padx=8, pady=(8, 2))
        self._input = tk.Text(
            input_row,
            height=3,
            wrap="word",
            background=THEME.field,
            foreground=THEME.text,
            insertbackground=THEME.text,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=THEME.border,
            highlightcolor=THEME.accent_strong,
            padx=12,
            pady=10,
            font="TkTextFont",
        )
        self._input.pack(side="left", fill="both", expand=True)
        button_col = ttk.Frame(input_row, style="Card.TFrame")
        button_col.pack(side="left", fill="y", padx=(8, 0))
        send_button = ttk.Button(
            button_col, text="Send", command=self._send,
            style="Accent.TButton",
        )
        send_button.pack(fill="x")
        self._cancel_button = ttk.Button(
            button_col, text="Cancel", command=self._cancel_turn,
            state="disabled",
        )
        self._cancel_button.pack(fill="x", pady=(6, 0))
        self._input.bind("<Control-Return>", lambda _event: self._send())
        # Terminal-style recall: Up/Down walk the messages this window
        # sent, but only when the cursor sits on the first (Up) or last
        # (Down) line, so multi-line editing keeps normal cursor keys.
        self._sent_history: list[str] = []
        self._history_pos: int | None = None
        self._history_draft = ""
        self._input.bind("<Up>", lambda _event: self._recall(-1))
        self._input.bind("<Down>", lambda _event: self._recall(1))
        hint_row = ttk.Frame(composer, style="Card.TFrame")
        hint_row.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(
            hint_row,
            text=(
                "Ctrl+Enter sends · Enter adds a new line "
                "· ↑/↓ recall what you sent"
            ),
            style="CardDim.TLabel",
        ).pack(side="left")
        self._build_voice_row(composer, bridge)
        self._show_section("chat")

        self._build_memory_section(content)
        self._build_history_section(content)
        self._build_settings_section(content, settings)

        self._line(
            "Ask Stella anything. The menu on the left manages memories, "
            "recent actions, and the minimal local settings."
        )
        bridge.post_memories()
        # Action history is durable, so the History section shows what
        # Stella did in earlier sessions, not only in this window.
        bridge.post_history()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._tick)
        # Queued persona reflection proposals need the approval-drain
        # loop above to already be running, so they are requested one
        # beat later, never during construction.
        root.after(250, self._bridge.post_persona_drain)

    # ------------------------------------------------------------ sections

    def _show_section(self, name: str) -> None:
        previous = self._sections.get(self._section)
        if previous is not None:
            previous.pack_forget()
        self._section = name
        # The chat page runs edge to edge so the user's quote band
        # reaches the window border; every other page keeps the margin.
        self._sections[name].pack(
            fill="both", expand=True, padx=0 if name == "chat" else 14
        )
        for key, button in self._nav_buttons.items():
            button.configure(
                style="NavActive.TButton" if key == name else "Nav.TButton"
            )

    def _section_header(self, parent: ttk.Frame, title: str, note: str) -> None:
        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text=title, style="Section.TLabel").pack(
            side="left"
        )
        ttk.Label(header, text=note, style="Dim.TLabel").pack(
            side="left", padx=(12, 0), pady=(4, 0)
        )

    def _section_card(self, parent: ttk.Frame) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame")
        card.pack(fill="both", expand=True)
        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill="both", expand=True, padx=14, pady=14)
        return inner

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
        self._root.configure(background=theme.window)
        self._chat.configure(
            background=theme.window,
            foreground=theme.text,
            insertbackground=theme.text,
            highlightbackground=theme.border,
        )
        self._configure_chat_tags()
        self._input.configure(
            background=theme.field,
            foreground=theme.text,
            insertbackground=theme.text,
            highlightbackground=theme.border,
            highlightcolor=theme.accent_strong,
        )
        for box in (
            self._memory_list,
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
                    highlightbackground=theme.border,
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
        # "gap" is configured last so its background wins over the quote
        # band's on the blank spacer line.
        # Terminal-style roles: the user's message is a blockquote — a
        # "> " marker, a hanging indent past it, and a band that spans
        # the whole window (Tk paints a tagged line's background across
        # the full display line, and the Text carries no horizontal
        # padding). Stella answers in plain left-aligned text with no
        # label; marker and band are the whole separation.
        # rmargin keeps every line's text clear of the overlaid
        # scrollbar; the painted band ignores margins and still runs
        # edge to edge.
        chat.tag_configure(
            "quote",
            background=THEME.user_quote,
            lmargin1=16,
            lmargin2=34,
            rmargin=18,
            spacing1=10,
            spacing3=0,
        )
        chat.tag_configure(
            "stella",
            lmargin1=16,
            lmargin2=16,
            rmargin=18,
            spacing1=10,
            spacing3=0,
        )
        chat.tag_configure("body-stella", foreground=THEME.text)
        # The ">" marker uses its own explicit theme field, not a brand
        # derivation: accent == ok in both palettes, and a marker painted
        # from brand fields would collide with everything else teal.
        chat.tag_configure(
            "head-user", foreground=THEME.user_head, font=self._head_font
        )
        chat.tag_configure("body-user", foreground=THEME.text)
        chat.tag_configure(
            "note",
            foreground=THEME.text_dim,
            font=self._italic_font,
            lmargin1=14,
            lmargin2=14,
            rmargin=18,
            spacing1=6,
            spacing3=0,
        )
        chat.tag_configure(
            "error",
            foreground=THEME.error,
            lmargin1=14,
            lmargin2=14,
            rmargin=18,
            spacing1=8,
            spacing3=0,
        )
        chat.tag_configure(
            "alert",
            foreground=THEME.warning,
            lmargin1=14,
            lmargin2=14,
            rmargin=18,
            spacing1=8,
            spacing3=0,
        )
        chat.tag_configure("gap", background=THEME.window)

    def _line(self, text: str, role: str = "note") -> None:
        chat = self._chat
        chat.configure(state="normal")
        if role in ("user", "stella"):
            first, *rest = text.split("\n")
            split = first.find(": ")
            body = first[split + 2 :] if 0 < split <= 20 else first
            if role == "user":
                # The "> " marker replaces the "You:" label: a quote line
                # announces itself. Every hard line of the message gets
                # its own marker, like a blockquote. Each line's newline
                # carries the line tag too: Tk only paints a tagged
                # line's background across the full display width when
                # the newline itself has the tag — an untagged newline
                # leaves the band as wide as the text (a "Hi" quote
                # would otherwise be a 40-pixel stripe).
                for line in (body, *rest):
                    chat.insert("end", "> ", ("quote", "head-user"))
                    chat.insert("end", line, ("quote", "body-user"))
                    chat.insert("end", "\n", ("quote",))
            else:
                # Stella's reply carries no label and no band: it starts
                # flush at the quote marker's left margin, plain, like a
                # terminal assistant's own output. Its newlines are
                # tagged with the (background-free) "stella" line tag for
                # the same reason — the tag must own the whole line.
                for line in (body, *rest):
                    chat.insert("end", line, ("stella", "body-stella"))
                    chat.insert("end", "\n", ("stella",))
            # The blank spacer line is its own "\n" carrying only "gap",
            # so the window background repaints just that line; the
            # quote band stops at the message's last line.
            chat.insert("end", "\n", ("gap",))
        elif role in ("error", "alert"):
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
        if not self._sent_history or self._sent_history[-1] != user_input:
            self._sent_history.append(user_input)
        self._history_pos = None
        self._history_draft = ""
        self._line(f"You: {user_input}", role="user")
        call = parse_command_line(user_input)
        if call is not None:
            # A typed command is handled here, on the view side of the
            # bridge: it never reaches the model and never posts a turn
            # unless a template expands into one below.
            self._run_command(call)
            return
        self._start_turn(user_input)

    def _start_turn(self, text: str) -> None:
        self._busy = True
        self._begin_turn_timer()
        self._status.configure(text="Stella is working · 0 s")
        self._bridge.post_turn(text)

    def _run_command(self, call: CommandCall) -> None:
        if call.is_control:
            if call.name == "exit":
                self._on_close()
                return
            if call.name in {"trace", "debug"}:
                self._line(
                    f"/{call.name} belongs to the terminal: start Stella "
                    f"with --{call.name} to see those renderings.",
                    role="note",
                )
                return
            if call.name == "clear":
                self._bridge.post_clear_history()
                return
            if call.name == "history":
                limit = parse_limit(call.argument)
                if limit is None:
                    self._line(
                        "Usage: /history [number of records]", role="note"
                    )
                else:
                    self._bridge.post_action_trail(limit)
                return
            if call.name == "help":
                lines = help_lines()
            elif call.name == "status":
                settings, session, stella = self._bridge.status_snapshot()
                lines = status_lines(
                    settings=settings, session=session, stella=stella
                )
            else:
                lines = [version_line()]
            self._line("\n".join(lines), role="note")
            return
        body, error = load_template_body(call.name)
        if error is not None:
            self._line(error, role="note")
            suggestions = suggest_commands(call.name)
            if suggestions:
                listed = ", ".join(f"/{name}" for name in suggestions)
                self._line(f"Did you mean {listed}?", role="note")
            return
        # The expansion is ordinary input: it goes through the bridge
        # exactly like a typed sentence, approvals included.
        self._start_turn(expand_template(body, call.argument))

    def _recall(self, direction: int) -> str:
        # Up (-1) / Down (+1) through what this window sent. Returning
        # an empty string lets the Text's normal cursor movement happen
        # instead: recall only owns the arrow when the cursor is
        # already on the very first (Up) or very last (Down) line, so
        # editing a multi-line draft is never hijacked.
        if not self._sent_history:
            return ""
        line, _ = self._input.index("insert").split(".")
        last_line = int(self._input.index("end-1c").split(".")[0])
        if direction < 0 and int(line) != 1:
            return ""
        if direction > 0 and int(line) != last_line:
            return ""
        if direction > 0 and self._history_pos is None:
            return ""
        if self._history_pos is None:
            self._history_draft = self._input.get("1.0", "end").rstrip("\n")
            self._history_pos = 0
        else:
            # pos counts back from the newest entry: Up walks older
            # (pos grows), Down walks newer (pos shrinks).
            self._history_pos -= direction
            if self._history_pos < 0:
                # Back past the newest entry: restore the draft the
                # recall started from, and stop browsing.
                self._history_pos = None
        if self._history_pos is None:
            text = self._history_draft
            self._history_draft = ""
        else:
            self._history_pos = min(
                self._history_pos, len(self._sent_history) - 1
            )
            text = self._sent_history[-1 - self._history_pos]
        self._input.delete("1.0", "end")
        if text:
            self._input.insert("1.0", text)
        self._input.mark_set("insert", "end-1c")
        return "break"

    def _begin_turn_timer(self) -> None:
        self._turn_started = time.monotonic()
        self._cancelling = False
        self._pulse = 0
        self._turn_activity = ""

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
        row = ttk.Frame(chat, style="Card.TFrame")
        row.pack(fill="x", padx=8, pady=(2, 0))
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
            style="Card.TCheckbutton",
            state="normal" if speech_ok else "disabled",
        )
        self._speak_toggle.pack(side="left", padx=4)
        # Mute is a session switch beside "Speak replies", not a capability
        # opt-in: it is never saved, and its whole job is to be reachable in
        # one click the moment the microphone feels wrong.
        self._mute_var = tk.BooleanVar(value=False)
        self._mute_toggle = ttk.Checkbutton(
            row,
            text="Mute mic",
            variable=self._mute_var,
            command=self._toggle_mute,
            style="Card.TCheckbutton",
            state="normal" if mic_ok else "disabled",
        )
        self._mute_toggle.pack(side="left", padx=4)
        # The dot is the only sign of a microphone Stella is holding open
        # with no button pressed for it — the wake ear, an interruption
        # listener, a capture in flight. It is redrawn on the poll tick
        # only when the answer changes.
        self._mic_dot = ttk.Label(row, text="●", style="CardDim.TLabel")
        self._mic_dot.pack(side="left", padx=(6, 0))
        self._mic_dot_color: str | None = None
        self._render_mic_hot()
        if not mic_ok:
            self._mic_button.configure(state="disabled")
        ttk.Label(
            chat,
            style="CardDim.TLabel",
            text=(
                "Voice input is not available here (no capture command or "
                "transcription provider)."
                if not mic_ok
                else "Listening and speaking are explicit; recordings are "
                "removed right after transcription. A red dot means the "
                "microphone is open right now."
            ),
        ).pack(anchor="w", padx=10, pady=(2, 6))

    def _render_mic_hot(self) -> None:
        # A ttk label repaints on every configure, so the dot is touched
        # only when its color actually changes — which covers both the
        # microphone opening and the theme being switched under it.
        color = (
            THEME.error if self._bridge.mic_hot() else THEME.text_dim
        )
        if color == self._mic_dot_color:
            return
        self._mic_dot_color = color
        self._mic_dot.configure(foreground=color)

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

    def _toggle_mute(self) -> None:
        self._bridge.set_mic_muted(self._mute_var.get())

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
        # model reads as slow rather than broken — and when it finished,
        # so an old answer is never mistaken for a current one.
        # Display only; the stored conversation never carries this text.
        if outcome.duration_seconds is None:
            return ""
        seconds = outcome.duration_seconds
        text = f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s"
        return f" (took {text} · {time.strftime('%H:%M')})"

    # ------------------------------------------------------ event pump

    def _tick(self) -> None:
        for event in self._bridge.poll():
            self._handle_event(event)
        self._drain_approvals()
        self._render_working_status()
        self._render_mic_hot()
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
        # A quiet braille spinner says "alive" between the whole-second
        # updates; with no activity named yet the asserted prefix stays
        # exactly "Stella is working · ". A calling:<capability> event
        # upgrades "working" to "calling memory list" mid-turn, so the
        # long tool+synthesis stretch never reads as a dead pane.
        self._pulse = (self._pulse + 1) % len(_SPINNER)
        self._status.configure(
            text=(
                f"Stella is {self._turn_activity or 'working'}"
                f" · {seconds} s  "
                f"{_SPINNER[self._pulse]}"
            )
        )

    def _handle_event(self, event: UiEvent) -> None:
        kind, payload = event.kind, event.payload
        if kind == "turn":
            self._handle_turn(payload)
        elif kind == "activity":
            # Core-known capability name, never model text.
            phase = str(payload).removeprefix("calling:").replace("_", " ")
            self._turn_activity = f"calling {phase}" if phase else ""
        elif kind == "reminder_delivered":
            # One Outline reminder this process claimed. Stella keeps no
            # reminder store, so every line here names its source.
            self._line(str(payload), role="alert")
        elif kind == "memories":
            self._show_memories(payload)
        elif kind == "memory_result":
            self._memory_status.configure(text=str(payload))
        elif kind == "history":
            self._show_history(payload)
        elif kind == "settings":
            self._line(f"(settings) {payload}")
        elif kind == "notice":
            self._line(f"(persona) {payload}")
        elif kind == "note":
            self._line(str(payload), role="note")
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
        if preview is not None and preview.warning:
            # Trusted display code's advisory: the user's own turn
            # mentioned neither this action nor its target. Review
            # material only — the answer still binds to the exact
            # ApprovalRequest, never to this line.
            ttk.Label(
                dialog,
                text=preview.warning,
                wraplength=420,
                justify="left",
                style="Warning.TLabel",
            ).pack(padx=16, pady=(0, 6), anchor="w")
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
                highlightbackground=THEME.border,
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

    def _build_memory_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        self._sections["memories"] = frame
        self._section_header(
            frame, "Memories", "what Stella has been told to remember"
        )
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 8))
        self._memory_search = ttk.Entry(row)
        self._memory_search.pack(side="left", fill="x", expand=True)
        ttk.Button(
            row, text="Search", command=self._refresh_memories
        ).pack(side="left", padx=(8, 0))
        card = ttk.Frame(frame, style="Card.TFrame")
        card.pack(fill="both", expand=True)
        self._memory_list = tk.Listbox(
            card, exportselection=False, height=16
        )
        _style_listbox(self._memory_list)
        self._memory_list.pack(fill="both", expand=True, padx=12, pady=12)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(
            actions, text="Refresh", command=self._refresh_all_memories
        ).pack(side="left")
        ttk.Button(
            actions, text="Forget selected", command=self._forget_memory
        ).pack(side="left", padx=6)
        self._memory_status = ttk.Label(
            frame, text="", wraplength=460, style="Dim.TLabel"
        )
        self._memory_status.pack(pady=(6, 0), anchor="w")

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

    # ---------------------------------------------------------- history

    def _build_history_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        self._sections["history"] = frame
        self._section_header(
            frame, "History", "what Stella recently did, newest first"
        )
        card = ttk.Frame(frame, style="Card.TFrame")
        card.pack(fill="both", expand=True)
        self._history_list = tk.Listbox(
            card, exportselection=False, height=16
        )
        _style_listbox(self._history_list)
        self._history_list.pack(fill="both", expand=True, padx=12, pady=12)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Refresh", command=self._refresh_history).pack(
            side="left"
        )
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text="Kept between launches; metadata only, never file contents.",
            wraplength=460,
        ).pack(pady=(6, 0), anchor="w")

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

    def _build_settings_section(
        self, parent: ttk.Frame, settings: StellaSettings
    ) -> None:
        frame = ttk.Frame(parent)
        self._sections["settings"] = frame
        self._section_header(
            frame, "Settings", "the minimal local configuration"
        )
        self._panel_settings = settings
        self._settings_fields: dict[str, ttk.Entry] = {}
        provider_row = ttk.Frame(frame)
        provider_row.pack(fill="x", pady=4)
        ttk.Label(provider_row, text="Provider:").pack(side="left")
        self._provider = ttk.Combobox(
            provider_row,
            values=_PRESET_LABELS,
            state="readonly",
            width=24,
        )
        self._provider.set(_preset_label_for_settings(settings))
        self._provider.pack(side="left", padx=6)
        self._provider.bind(
            "<<ComboboxSelected>>", lambda _event: self._panel_preset_changed()
        )
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
            ("Workspace", settings.workspace, False),
        ):
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=6, pady=2)
            ttk.Label(row, text=f"{label}:", width=16).pack(side="left")
            entry = ttk.Entry(row, width=34, show="*" if masked else "")
            entry.insert("0", value)
            entry.pack(side="left", fill="x", expand=True)
            self._settings_fields[label] = entry
        self._panel_key_hint = ttk.Label(
            frame, style="Dim.TLabel", wraplength=340
        )
        self._panel_key_hint.pack(padx=6, anchor="w")
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
        self._outline_tools_var = tk.BooleanVar(
            value=settings.outline_tools_enabled
        )
        ttk.Checkbutton(
            frame,
            text="Outline app tools (search, create, update)",
            variable=self._outline_tools_var,
        ).pack(padx=6, pady=(4, 0), anchor="w")
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text=(
                "Adds outline_search, outline_create and outline_update "
                "for the Outline task app over its local API. The tools "
                "appear only while an Outline server is reachable; "
                "reading never asks for approval, writing always does."
            ),
            wraplength=340,
        ).pack(padx=6, anchor="w")
        self._web_tools_var = tk.BooleanVar(value=settings.web_tools_enabled)
        ttk.Checkbutton(
            frame,
            text="Web capability (search, page fetch)",
            variable=self._web_tools_var,
        ).pack(padx=6, pady=(4, 0), anchor="w")
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text=(
                "Adds web_search and web_fetch. Every use asks you first "
                "and names where the data goes: a TinyFish key (free tier, "
                "a commercial decision) or the keyless ddgs/https path. "
                "Page text comes back marked as untrusted. Needs the "
                "stella[web] extra for the keyless fallback."
            ),
            wraplength=340,
        ).pack(padx=6, anchor="w")
        self._wake_var = tk.BooleanVar(value=settings.wake_word_enabled)
        ttk.Checkbutton(
            frame,
            text="Wake word (always-open microphone)",
            variable=self._wake_var,
        ).pack(padx=6, pady=(4, 0), anchor="w")
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text=(
                "Say \"hey Jarvis\" and Stella starts listening, so no "
                "button press is needed. The microphone stays open for a "
                "local detector only: it classifies frames and records "
                "nothing until the phrase fires, and nothing at all is "
                "kept when it does not. One confirmed phrase is exactly a "
                "Listen press — it grants no authority, and it never "
                "answers an approval dialog for you. Needs the "
                "stella[wake] extra and the models under ~/models/"
                "openwakeword; without them the box simply does nothing."
            ),
            wraplength=340,
        ).pack(padx=6, anchor="w")
        ttk.Label(
            frame,
            style="Dim.TLabel",
            text=(
                "The API key is verified once, then kept in a private "
                "file inside Stella's data directory and used in every "
                "later session; an exported OPENAI_API_KEY overrides the "
                "stored key for the OpenAI slot on that launch."
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
        self._panel_preset_changed()

    def _current_state_text(self) -> str:
        model = self._panel_settings.model or "(not set)"
        return f"Provider: {self._panel_settings.provider}  Model: {model}"

    def _preset_id(self) -> str:
        return _PRESET_BY_LABEL[self._provider.get()]

    def _panel_preset_changed(self) -> None:
        preset = provider_keys.PRESETS[self._preset_id()]
        url_field = self._settings_fields["OpenAI base URL"]
        # A disabled Tk Entry silently rejects delete/insert, so the
        # field must be re-enabled before any value rewrite.
        url_field.configure(state="normal")
        if preset.id != "custom":
            # The endpoint belongs to the preset, not to the user: show
            # it, but only "Other OpenAI-compatible" accepts an edit.
            url_field.delete("0", "end")
            url_field.insert(
                "0", preset.base_url or "(OpenAI default endpoint)"
            )
            url_field.configure(state="disabled")
        self._update_panel_key_hint()

    def _update_panel_key_hint(self) -> None:
        preset = provider_keys.PRESETS[self._preset_id()]
        stored = (
            provider_keys.stored_api_key(preset.id) if preset.key_required else None
        )
        if stored:
            self._panel_key_hint.configure(
                text=(
                    f"Stored key for {preset.label} "
                    f"{provider_keys.redacted_hint(stored)} — entering a "
                    "new key on Apply replaces it."
                )
            )
        else:
            self._panel_key_hint.configure(text="")

    def _draft_settings(self) -> StellaSettings:
        fields = self._settings_fields
        preset_id = self._preset_id()
        provider = _provider_for_preset(preset_id)
        # Replace over the panel's settings so voice fields that the panel
        # does not show (modes, models, commands) survive an Apply click.
        return replace(
            self._panel_settings,
            provider=provider,
            preset=preset_id if provider == "openai" else None,
            model=fields["Model"].get().strip() or None,
            openai_base_url=(
                fields["OpenAI base URL"].get().strip() or None
                if preset_id == "custom"
                else None
            ),
            ollama_base_url=fields["Ollama base URL"].get().strip()
            or DEFAULT_OLLAMA_BASE_URL,
            memory_db=fields["Memory DB"].get().strip(),
            workspace=fields["Workspace"].get().strip(),
            transcripts_enabled=self._transcripts_var.get(),
            semantic_memory_enabled=self._semantic_var.get(),
            os_tools_enabled=self._os_tools_var.get(),
            outline_tools_enabled=self._outline_tools_var.get(),
            web_tools_enabled=self._web_tools_var.get(),
            # The saved choice and the mode this session runs are one
            # decision, so Apply moves both: the checkbox is what the
            # owner wants, and a rebuild arms or stops the ear from it.
            wake_word_enabled=self._wake_var.get(),
            wake_word="on" if self._wake_var.get() else "off",
            semantic_provider=_SEMANTIC_PROVIDER_BY_LABEL[
                self._semantic_provider_var.get()
            ],
        )

    def _entered_key(self) -> str:
        return self._settings_fields["API key"].get()

    def _store_entered_key(self) -> str | None:
        """Persist a typed key through the mismatch gate; None on success.

        A returned string is the refusal the user must see: Apply is not
        allowed to store a key that does not belong to the chosen
        provider. Local presets need no key, so a leftover field value is
        kept on screen and stored nowhere.
        """

        key = self._entered_key()
        if not key:
            return None
        preset = provider_keys.PRESETS[self._preset_id()]
        if not preset.key_required:
            return None
        hint = provider_keys.mismatch_hint(preset.id, key)
        if hint:
            return hint
        try:
            provider_keys.save_api_key(preset.id, key)
        except (ValueError, OSError) as error:
            return f"That key cannot be stored: {error}"
        self._settings_fields["API key"].delete("0", "end")
        self._update_panel_key_hint()
        return None

    def _apply_settings(self) -> None:
        refusal = self._store_entered_key()
        if refusal:
            self._settings_status.configure(text=refusal)
            return
        preset = provider_keys.PRESETS[self._preset_id()]
        if (
            preset.key_required
            and provider_keys.effective_api_key(preset.id) is None
        ):
            # Catch the keyless switch here instead of posting a rebuild
            # the worker can only fail mid-flight: nothing is changed,
            # and the field to fix is named.
            self._settings_status.configure(
                text=(
                    f"{preset.label} has no stored key yet — type one "
                    "into the API key field, then Apply again."
                )
            )
            return
        applied = self._draft_settings()
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
            preset=draft.preset,
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


# The provider picker is the preset table, local modes first. Labels are
# what the combobox shows; ids are what the store and the build path use.
_PRESET_ORDER = (
    "ollama",
    "llama",
    "openai",
    "anthropic",
    "xai",
    "groq",
    "openrouter",
    "google",
    "freellmapi",
    "custom",
)
_PRESET_LABELS = [provider_keys.PRESETS[pid].label for pid in _PRESET_ORDER]
_PRESET_BY_LABEL = {provider_keys.PRESETS[pid].label: pid for pid in _PRESET_ORDER}


def _provider_for_preset(preset_id: str) -> str:
    """The runtime provider behind one picker entry.

    Local presets name their own provider; every hosted preset rides the
    existing ``openai`` provider with its endpoint and key slot.
    """

    if preset_id == "ollama":
        return "ollama"
    if preset_id == "llama":
        return "llama"
    return "openai"


def _preset_label_for_settings(settings: StellaSettings) -> str:
    """The picker entry a live configuration came from (or belongs to)."""

    if settings.provider == "ollama":
        return provider_keys.PRESETS["ollama"].label
    if settings.provider == "llama":
        return provider_keys.PRESETS["llama"].label
    preset = provider_keys.PRESETS.get(settings.preset or "")
    if preset is not None and preset.key_required:
        return preset.label
    return provider_keys.PRESETS["openai"].label


class SetupDialog:
    """First-run provider, model and key configuration, before any Stella
    application exists.

    The picker is the preset table from ``stella.provider_keys``: local
    Ollama and llama.cpp need no key, every hosted preset carries its
    endpoint, suggested models and key shape, and "Other
    OpenAI-compatible" takes a base URL. A typed key is verified against
    the chosen provider — a Claude-shaped key in the OpenAI slot is
    refused with a hint before any request — and only a passing test
    stores it, in Stella's private key file, for every later session.
    Nothing secret is ever written to config.json.
    """

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
        picker_row = ttk.Frame(dialog)
        picker_row.pack(fill="x", padx=16)
        ttk.Label(picker_row, text="Provider:").pack(side="left")
        self._preset = tk.StringVar(value=_PRESET_LABELS[0])
        self._preset_box = ttk.Combobox(
            picker_row,
            values=_PRESET_LABELS,
            state="readonly",
            width=26,
            textvariable=self._preset,
        )
        self._preset_box.pack(side="left", padx=6)
        self._preset_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._preset_changed()
        )
        fields = ttk.Frame(dialog)
        fields.pack(fill="x", padx=16, pady=6)
        self._fields: dict[str, tk.Widget] = {}
        self._rows: dict[str, ttk.Frame] = {}
        for label, masked in (
            ("Model", False),
            ("Ollama base URL", False),
            ("API base URL", False),
            ("API key", True),
        ):
            row = ttk.Frame(fields)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"{label}:", width=15).pack(side="left")
            widget: tk.Widget = (
                ttk.Combobox(row, width=38)
                if label == "Model"
                else ttk.Entry(row, width=38, show="*" if masked else "")
            )
            widget.pack(side="left", fill="x", expand=True)
            self._fields[label] = widget
            self._rows[label] = row
        self._fields["Ollama base URL"].insert("0", DEFAULT_OLLAMA_BASE_URL)  # type: ignore[union-attr]
        self._key_hint = ttk.Label(
            fields, style="Dim.TLabel", wraplength=420
        )
        self._key_hint.pack(anchor="w")
        for label in ("Model", "Ollama base URL", "API base URL", "API key"):
            # Editing the configuration invalidates a previous test, so
            # the user can never start from a stale success. The key
            # entry belongs to this list now that a passing test saves
            # the key: retyping must re-disable Start.
            widget = self._fields[label]
            widget.bind("<KeyRelease>", lambda _event: self._mark_untested())
            if isinstance(widget, ttk.Combobox):
                widget.bind(
                    "<<ComboboxSelected>>", lambda _event: self._mark_untested()
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
            text="Pick a provider, then test the connection.",
            wraplength=420,
            justify="left",
            style="Dim.TLabel",
        )
        self.status.pack(padx=16, pady=(4, 16), anchor="w")
        self._model_list.bind("<<ListboxSelect>>", self._model_selected)
        self._preset_changed()
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        # No transient(): the parent root is withdrawn, and a transient
        # window of an unmapped parent is never mapped under XWayland
        # compositors, leaving first-run setup invisible and blocking
        # wait_window forever.
        dialog.grab_set()

    # ----------------------------------------------------------- widgets

    def _preset_id(self) -> str:
        return _PRESET_BY_LABEL[self._preset.get()]

    def _preset_changed(self) -> None:
        preset = provider_keys.PRESETS[self._preset_id()]
        provider = _provider_for_preset(preset.id)
        self._fields["API key"].delete("0", "end")  # type: ignore[union-attr]
        self._show(self._rows["Ollama base URL"], provider == "ollama")
        self._show(self._rows["API base URL"], preset.id == "custom")
        self._show(self._rows["API key"], preset.key_required)
        self._show(self._model_list, provider == "ollama")
        self._refresh_button.configure(
            state="normal" if provider == "ollama" else "disabled"
        )
        model = self._fields["Model"]
        assert isinstance(model, ttk.Combobox)
        model.configure(values=list(preset.models))
        if preset.models and model.get() not in preset.models:
            # Suggest the preset's first model, but never overwrite a
            # custom name the user already typed for a keyless preset.
            model.set(preset.models[0])
        self._update_key_hint()
        self._mark_untested()

    def _update_key_hint(self) -> None:
        preset = provider_keys.PRESETS[self._preset_id()]
        stored = (
            provider_keys.stored_api_key(preset.id) if preset.key_required else None
        )
        if stored:
            self._key_hint.configure(
                text=(
                    f"Stored key for {preset.label} "
                    f"{provider_keys.redacted_hint(stored)} — leave the "
                    "field empty to keep it."
                )
            )
        else:
            self._key_hint.configure(text="")

    def _show(self, widget: tk.Misc, visible: bool) -> None:
        # Re-packing always anchors before the key-hint label, so toggling
        # between presets cannot shuffle rows below the model list.
        if visible:
            widget.pack(fill="x", pady=2, before=self._key_hint)
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
            model = self._fields["Model"]
            assert isinstance(model, ttk.Combobox)
            model.set(name)

    def _draft(self) -> tuple[str, StellaSettings]:
        preset_id = self._preset_id()
        provider = _provider_for_preset(preset_id)
        openai_base_url = (
            self._fields["API base URL"].get().strip() or None  # type: ignore[union-attr]
            if preset_id == "custom"
            else None
        )
        settings = StellaSettings.from_saved(
            provider=provider,
            model=self._fields["Model"].get().strip(),  # type: ignore[union-attr]
            ollama_base_url=self._fields["Ollama base URL"].get().strip()  # type: ignore[union-attr]
            or DEFAULT_OLLAMA_BASE_URL,
            openai_base_url=openai_base_url,
            preset=preset_id if provider == "openai" else None,
        )
        return preset_id, settings

    # ------------------------------------------------------------ probes

    def refresh_models(self) -> None:
        scan = config.scan_ollama_models(
            self._fields["Ollama base URL"].get().strip()  # type: ignore[union-attr]
            or DEFAULT_OLLAMA_BASE_URL
        )
        self._model_list.delete(0, "end")
        for name in scan.models:
            self._model_list.insert("end", name)
        self.status.configure(text=scan.message)
        self._mark_untested()

    def _draft_signature(self) -> tuple[object, ...]:
        _preset_id, draft = self._draft()
        return (
            draft.provider,
            draft.preset,
            draft.model,
            draft.ollama_base_url,
            draft.openai_base_url,
        )

    def test_connection(self) -> None:
        preset_id, draft = self._draft()
        preset = provider_keys.PRESETS[preset_id]
        api_key = self._fields["API key"].get() if preset.key_required else ""  # type: ignore[union-attr]
        result = config.test_connection(
            provider=draft.provider,
            model=draft.model or "",
            ollama_base_url=draft.ollama_base_url,
            openai_base_url=draft.openai_base_url,
            api_key=api_key or None,
            preset=preset_id if preset.key_required else None,
            llama_binary=draft.llama_binary,
        )
        saved_note = ""
        if result.ok and api_key:
            try:
                provider_keys.save_api_key(preset_id, api_key)
            except (ValueError, OSError) as error:
                result = config.ConnectionTest(
                    ok=False,
                    message=f"Connected, but the key was not stored: {error}",
                )
            else:
                # The verified key now lives in the private store; the
                # widget forgets it immediately, so nothing secret lingers
                # on screen or in this process's environment.
                self._fields["API key"].delete("0", "end")  # type: ignore[union-attr]
                self._update_key_hint()
                saved_note = (
                    " Key saved for future sessions (never shown again)."
                )
        self._tested_draft = self._draft_signature() if result.ok else None
        self._finish_button.configure(
            state="normal" if result.ok else "disabled"
        )
        self.status.configure(
            text=(
                result.message if result.ok else f"Not connected: {result.message}"
            )
            + saved_note
        )

    def finish(self) -> None:
        if (
            self._tested_draft is None
            or self._draft_signature() != self._tested_draft
        ):
            self._mark_untested()
            return
        _preset_id, draft = self._draft()
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
