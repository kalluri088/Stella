"""The `stella audit` surface over the durable action trail."""

import json

from stella.audit import classify, format_line, run_audit
from stella.history import SQLiteActionHistory


def _entry(capability, *, approved=None, required=False, success=True, receipt=None):
    return {
        "timestamp": "2026-09-28T19:55:50.154017+00:00",
        "capability": capability,
        "risk_level": "dangerous" if required else "safe",
        "approval_required": required,
        "approval_granted": approved,
        "execution_success": success,
        "arguments": {"path": "notes/x.txt"} if capability == "filesystem_write" else {},
        "action_receipt": receipt,
    }


def _seed(tmp_path, entries):
    history = SQLiteActionHistory(tmp_path / "h.db", max_records=256)
    for entry in entries:
        history.append(entry)
    history.close()


def test_run_audit_prints_newest_last(tmp_path, capsys):
    _seed(tmp_path, [_entry("datetime"), _entry("memory_write", required=True, approved=True)])
    assert run_audit(tmp_path / "h.db", last=20) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert "datetime (safe) -> success" in lines[0]
    assert "memory_write (dangerous) -> approved" in lines[1]


def test_denied_beats_failure_in_classification():
    denied = _entry("network_read", required=True, approved=False, success=False)
    assert classify(denied) == "denied"
    assert classify(_entry("filesystem_write", required=True, approved=True)) == "approved"
    assert classify(_entry("reminder_list", success=False)) == "failure"


def test_filters_compose_and_trim(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            _entry("filesystem_write", required=True, approved=True),
            _entry("filesystem_write", required=True, approved=False, success=False),
            _entry("datetime"),
        ],
    )
    assert run_audit(tmp_path / "h.db", last=20, capability="filesystem") == 0
    out = capsys.readouterr().out
    assert out.count("filesystem_write") == 2
    assert "datetime" not in out
    assert run_audit(tmp_path / "h.db", last=20, outcome="denied") == 0
    out = capsys.readouterr().out
    assert "-> denied" in out and out.strip().count("\n") == 0
    assert run_audit(tmp_path / "h.db", last=1) == 0
    assert capsys.readouterr().out.strip().count("\n") == 0


def test_receipt_and_arguments_render(tmp_path, capsys):
    entry = _entry(
        "filesystem_write",
        required=True,
        approved=True,
        receipt={"action": "create", "status": "verified", "size_bytes": 5},
    )
    assert "[create:verified]" in format_line(entry)
    assert 'path="notes/x.txt"' in format_line(entry)


def test_json_mode_and_validation(tmp_path, capsys):
    _seed(tmp_path, [_entry("datetime")])
    assert run_audit(tmp_path / "h.db", last=5, as_json=True) == 0
    assert json.loads(capsys.readouterr().out)[0]["capability"] == "datetime"
    assert run_audit(tmp_path / "h.db", last=0) == 2
    assert "positive" in capsys.readouterr().out
    assert run_audit(tmp_path / "missing.db") == 0
    assert "No matching audit records" in capsys.readouterr().out


def test_cli_dispatch_audits(tmp_path, monkeypatch, capsys):
    from stella import cli

    _seed(tmp_path, [_entry("reminder_create", required=True, approved=False, success=False)])
    monkeypatch.setattr(cli, "default_history_db", lambda: str(tmp_path / "h.db"))
    try:
        cli.main(["audit", "--outcome", "denied"])
    except SystemExit as exit_code:
        assert exit_code.code == 0
    assert "reminder_create" in capsys.readouterr().out
