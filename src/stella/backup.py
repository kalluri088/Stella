"""Export and import of Stella's persistent state (``stella backup/restore``).

Report 34's crash audit left one promise unkept: the state databases
were only safe to copy with the process stopped. SQLite's online backup
API removes that requirement for ``backup`` — each database is copied as
a consistent snapshot even while Stella holds it open — while
``restore`` still replaces the files wholesale and therefore documents
that Stella must not be running.

The scope is deliberately the same set of files the runtime resolves
under ``default_data_dir()``: the state databases plus ``config.json``.
Workspace files are ordinary files the user can copy, and the persona
already has its own snapshot/``revert`` history; neither belongs here.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

#: State databases captured by a backup, matched by file name under the
#: state directory. ``tests/test_backup_cli.py`` pins these against the
#: ``stella.app`` path helpers so the list cannot drift from the runtime.
DATABASES = (
    "stella_memory.db",
    "stella_reminders.db",
    "stella_action_history.db",
    "stella_transcript.db",
    "stella_semantic_index.db",
)

CONFIG_NAME = "config.json"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1


def _snapshot_database(source: Path, dest: Path) -> None:
    """Copy one SQLite database as a consistent standalone file."""

    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            origin.backup(target)
        finally:
            target.close()
    finally:
        origin.close()


def _integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    return row is not None and row[0] == "ok"


def run_backup(
    state_dir: str | Path,
    dest_dir: str | Path,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Write a backup of the state databases + config into ``dest_dir``."""

    source = Path(state_dir)
    if not source.is_dir():
        output_fn(f"There is no Stella state directory at {source} yet; nothing to back up.")
        return 1
    dest = Path(dest_dir)
    if dest.exists() and not dest.is_dir():
        output_fn(f"{dest} exists and is not a directory; nothing was written.")
        return 2
    found = [name for name in DATABASES if (source / name).is_file()]
    if not found:
        output_fn(f"No Stella state databases were found under {source}; nothing to back up.")
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    for name in found:
        _snapshot_database(source / name, dest / name)
    has_config = (source / CONFIG_NAME).is_file()
    if has_config:
        shutil.copyfile(source / CONFIG_NAME, dest / CONFIG_NAME)
    manifest = {
        "format": FORMAT_VERSION,
        "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source": str(source),
        "databases": found,
        "config": has_config,
    }
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    extras = " and config.json" if has_config else ""
    output_fn(f"Backed up {len(found)} database(s){extras} to {dest}.")
    output_fn(
        "Workspace files and the persona directory are not included; the "
        "persona keeps its own history for 'stella persona revert'."
    )
    return 0


def _load_manifest(backup_dir: Path, output_fn: Callable[[str], None]) -> dict | None:
    path = backup_dir / MANIFEST_NAME
    if not path.is_file():
        output_fn(f"{backup_dir} has no {MANIFEST_NAME}; it was not written by 'stella backup'.")
        return None
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        output_fn(f"{path} is not readable JSON; nothing was restored.")
        return None
    if not isinstance(manifest, dict):
        output_fn(f"{path} does not contain a manifest object; nothing was restored.")
        return None
    if manifest.get("format") != FORMAT_VERSION:
        output_fn(f"Unsupported backup format {manifest.get('format')!r}; nothing was restored.")
        return None
    names = manifest.get("databases")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        output_fn("The backup manifest lists no databases; nothing was restored.")
        return None
    missing = [n for n in names if not (backup_dir / n).is_file()]
    if missing:
        output_fn(f"The backup is missing {', '.join(missing)}; nothing was restored.")
        return None
    return manifest


def run_restore(
    backup_dir: str | Path,
    state_dir: str | Path,
    yes: bool = False,
    output_fn: Callable[[str], None] = print,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Replace the state databases + config from a backup directory.

    Existing live databases are snapshotted into a ``pre-restore-*``
    directory first, so a wrong restore is itself undoable. Stella must
    be closed: the restore swaps files on disk and a running process
    would keep its now-detached handles.
    """

    source = Path(backup_dir)
    target = Path(state_dir)
    manifest = _load_manifest(source, output_fn)
    if manifest is None:
        return 2
    names = manifest["databases"]
    overwrite = [name for name in names if (target / name).is_file()]
    restoring_config = bool(manifest.get("config")) and (source / CONFIG_NAME).is_file()
    if overwrite or restoring_config:
        output_fn("This will replace, in " + str(target) + ":")
        for name in names:
            verb = "replace" if name in overwrite else "create"
            output_fn(f"    {verb} {name}")
        if restoring_config:
            output_fn(f"    replace {CONFIG_NAME}")
    if not yes:
        try:
            answer = input_fn("Restore this backup over the current state? [y/N]: ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.casefold() not in {"y", "yes"}:
            output_fn("Nothing was restored.")
            return 1
    target.mkdir(parents=True, exist_ok=True)
    safety_dir = None
    if overwrite:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        safety_dir = target / f"pre-restore-{stamp}"
        safety_dir.mkdir()
        for name in overwrite:
            _snapshot_database(target / name, safety_dir / name)
    for name in names:
        _snapshot_database(source / name, target / name)
    if restoring_config:
        shutil.copyfile(source / CONFIG_NAME, target / CONFIG_NAME)
    broken = [name for name in names if not _integrity_ok(target / name)]
    if broken:
        output_fn(
            f"Integrity check failed for {', '.join(broken)} after restore; the "
            f"pre-restore copies (if any) are intact and the backup was not trusted."
        )
        return 1
    output_fn(f"Restored {len(names)} database(s) from {source}.")
    if safety_dir is not None:
        output_fn(f"The previous state is kept in {safety_dir}; delete it once you are happy.")
    output_fn("Start Stella again to use the restored state.")
    return 0
