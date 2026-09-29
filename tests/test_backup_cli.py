"""The `stella backup` / `stella restore` surface over the state databases."""

import json
import sqlite3

from stella.app import (
    default_history_db,
    default_memory_db,
    default_reminders_db,
    default_semantic_db,
    default_transcripts_db,
)
from stella.backup import DATABASES, run_backup, run_restore


def _make_db(path, value):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS item (text TEXT)")
    connection.execute("INSERT INTO item VALUES (?)", (value,))
    connection.commit()
    connection.close()


def _seed_state(state_dir):
    _make_db(state_dir / "stella_memory.db", "live")
    _make_db(state_dir / "stella_action_history.db", "trail")
    (state_dir / "config.json").write_text('{"model": "qwen3:4b"}')


def _rows(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [row[0] for row in connection.execute("SELECT text FROM item")]
    finally:
        connection.close()


def test_database_names_match_app_helpers():
    # The runtime resolves these paths through stella.app; a rename there
    # must fail this test rather than silently back up nothing.
    basenames = {
        name.rpartition("/")[2]
        for name in (
            default_memory_db(),
            default_reminders_db(),
            default_history_db(),
            default_transcripts_db(),
            default_semantic_db(),
        )
    }
    assert set(DATABASES) == basenames


def test_backup_copies_databases_and_manifest(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    _seed_state(state)
    dest = tmp_path / "backup"
    assert run_backup(state, dest) == 0
    assert _rows(dest / "stella_memory.db") == ["live"]
    assert (dest / "config.json").read_text() == '{"model": "qwen3:4b"}'
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["format"] == 1
    assert manifest["databases"] == [
        "stella_memory.db",
        "stella_action_history.db",
    ]
    assert manifest["config"] is True
    # Databases absent from the state dir are not fabricated into it.
    assert not (dest / "stella_reminders.db").exists()
    assert "Workspace files" in capsys.readouterr().out


def test_backup_refuses_empty_or_missing_state(tmp_path, capsys):
    assert run_backup(tmp_path / "nowhere", tmp_path / "dest") == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_backup(empty, tmp_path / "dest2") == 1
    assert not (tmp_path / "dest2").exists()
    out = capsys.readouterr().out
    assert "nothing to back up" in out


def test_backup_source_stays_readable_while_open(tmp_path):
    # The online-backup promise: Stella does not have to stop.
    state = tmp_path / "state"
    state.mkdir()
    _make_db(state / "stella_memory.db", "before")
    writer = sqlite3.connect(state / "stella_memory.db")
    try:
        assert run_backup(state, tmp_path / "backup") == 0
        assert _rows(tmp_path / "backup" / "stella_memory.db") == ["before"]
    finally:
        writer.close()


def test_restore_round_trip_recovers_mutated_state(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    _seed_state(state)
    dest = tmp_path / "backup"
    assert run_backup(state, dest) == 0
    _make_db(state / "stella_memory.db", "changed after backup")
    assert run_restore(dest, state, yes=True) == 0
    assert _rows(state / "stella_memory.db") == ["live"]
    out = capsys.readouterr().out
    assert "pre-restore" in out
    safety = next(p for p in state.iterdir() if p.name.startswith("pre-restore-"))
    assert _rows(safety / "stella_memory.db") == ["live", "changed after backup"]


def test_restore_asks_before_touching_anything(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    _seed_state(state)
    dest = tmp_path / "backup"
    assert run_backup(state, dest) == 0
    _make_db(state / "stella_memory.db", "keep me")
    answers = iter([""])  # any non-yes answer declines
    assert run_restore(dest, state, input_fn=lambda _: next(answers)) == 1
    assert _rows(state / "stella_memory.db") == ["live", "keep me"]
    assert "Nothing was restored" in capsys.readouterr().out


def test_restore_rejects_non_backup_and_broken_manifests(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    _seed_state(state)
    assert run_restore(tmp_path / "nowhere", state, yes=True) == 2
    bogus = tmp_path / "bogus"
    bogus.mkdir()
    (bogus / "manifest.json").write_text('{"format": 99, "databases": []}')
    assert run_restore(bogus, state, yes=True) == 2
    missing = tmp_path / "missing-member"
    missing.mkdir()
    (missing / "manifest.json").write_text(
        '{"format": 1, "databases": ["stella_memory.db"], "config": false}'
    )
    assert run_restore(missing, state, yes=True) == 2
    assert _rows(state / "stella_memory.db") == ["live"]
    out = capsys.readouterr().out
    assert "Unsupported backup format" in out
    assert "missing" in out


def test_restore_creates_missing_databases_without_safety_dir(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    _seed_state(state)
    dest = tmp_path / "backup"
    assert run_backup(state, dest) == 0
    wiped = tmp_path / "fresh"
    wiped.mkdir()
    assert run_restore(dest, wiped, yes=True) == 0
    assert _rows(wiped / "stella_memory.db") == ["live"]
    assert (wiped / "config.json").exists()
    out = capsys.readouterr().out
    assert "pre-restore" not in out
    assert "Restored 2 database(s)" in out


def test_cli_dispatch_backup_and_restore(tmp_path, monkeypatch, capsys):
    from stella import cli

    state = tmp_path / "state"
    state.mkdir()
    _seed_state(state)
    monkeypatch.setattr(cli, "default_data_dir", lambda: state)
    dest = tmp_path / "via-cli"
    try:
        cli.main(["backup", str(dest)])
    except SystemExit as exit_code:
        assert exit_code.code == 0
    assert (dest / "manifest.json").is_file()
    _make_db(state / "stella_memory.db", "mutated")
    try:
        cli.main(["restore", str(dest), "--yes"])
    except SystemExit as exit_code:
        assert exit_code.code == 0
    assert _rows(state / "stella_memory.db") == ["live"]
