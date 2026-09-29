"""Read side of the durable action trail: the ``stella audit`` surface.

The dispatcher's ``SQLiteActionHistory`` (rule 10's approval trail) has
always been append-only from the running process; this module answers
the other half of the question — "what did Stella do, and did I allow
it?" — after the fact. It opens the database read-only, so it never
creates state and never competes with a running session, and it filters
in memory: retention already caps the whole trail at
``MAX_AUDIT_RECORDS`` rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

#: Outcome names accepted by ``stella audit --outcome``.
OUTCOMES = ("any", "success", "failure", "denied", "approved")

#: The dispatcher's retention cap (``stella.tools.MAX_AUDIT_RECORDS``).
#: Duplicated deliberately: importing it would drag the whole tool
#: stack into a read-only CLI path. Rows beyond it no longer exist.
RETAINED_WINDOW = 256


def load_entries(database_path: str | Path, limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` newest audit entries, oldest first.

    A missing database is not an error condition to hide behind: return
    an empty list and let the caller say "no trail yet" explicitly.
    """

    path = Path(database_path)
    if not path.exists():
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT entry FROM action_history ORDER BY id DESC LIMIT ?",
            (max(limit, 1),),
        ).fetchall()
    finally:
        connection.close()
    return [json.loads(row[0]) for row in reversed(rows)]


def classify(entry: dict[str, Any]) -> str:
    """One outcome label per entry: denied > approved/success/failure."""

    if entry.get("approval_required") and entry.get("approval_granted") is False:
        return "denied"
    if entry.get("execution_success"):
        return "approved" if entry.get("approval_granted") else "success"
    return "failure"


def filter_entries(
    entries: list[dict[str, Any]],
    capability: str | None = None,
    outcome: str = "any",
) -> list[dict[str, Any]]:
    selected = entries
    if capability:
        needle = capability.casefold()
        selected = [
            entry
            for entry in selected
            if (entry.get("capability") or "").casefold().find(needle) >= 0
        ]
    if outcome != "any":
        selected = [entry for entry in selected if classify(entry) == outcome]
    return selected


def format_line(entry: dict[str, Any]) -> str:
    """One human-readable line; fields absent from a record render as ``-``."""

    stamp = (entry.get("timestamp") or "").split("+")[0].replace("T", " ")
    capability = entry.get("capability") or "-"
    risk = entry.get("risk_level") or "-"
    outcome = classify(entry)
    receipt = entry.get("action_receipt")
    receipt_text = (
        f" [{receipt.get('action')}:{receipt.get('status')}]" if receipt else ""
    )
    arguments = entry.get("arguments") or {}
    if arguments:
        rendered = ", ".join(f"{key}={json.dumps(value)}" for key, value in sorted(arguments.items()))
    else:
        rendered = "no args"
    return f"{stamp or '-'}  {capability} ({risk}) -> {outcome}{receipt_text}  {rendered}"


def run_audit(
    database_path: str | Path,
    last: int = 20,
    capability: str | None = None,
    outcome: str = "any",
    as_json: bool = False,
) -> int:
    """Print the filtered trail; return a process exit code."""

    if last <= 0:
        print("'--last' must be a positive number of records", flush=False)
        return 2
    if outcome not in OUTCOMES:
        print(f"'--outcome' must be one of: {', '.join(OUTCOMES)}")
        return 2
    # The filter runs over the retained window, then trims to `last`, so
    # "the last 50 memory_* records" never silently means "the last 50
    # records that happened to be memory_* somewhere in the newest 256".
    entries = load_entries(database_path, max(last, RETAINED_WINDOW))
    selected = filter_entries(entries, capability=capability, outcome=outcome)[-last:]
    if as_json:
        print(json.dumps(selected, indent=2, sort_keys=True))
        return 0
    if not selected:
        print("No matching audit records (the trail is bounded to the newest 256).")
        return 0
    for entry in selected:
        print(format_line(entry))
    return 0
