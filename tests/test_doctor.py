"""`stella doctor` — the read-only preflight a one-command install depends on.

These tests hold the two promises that make the command trustworthy: it
changes nothing it inspects, and it never prints a credential. Everything
else is presentation.
"""

import json

from stella import cli, doctor, provider_keys
from stella.doctor import INFO, MISSING, READY, collect, render


def _isolate(monkeypatch, tmp_path):
    """Point every Stella-owned path at a directory that does not exist yet.

    This is both the fixture for "nothing is written" and the closest thing
    to a fresh machine: no config, no databases, no persona, no socket.
    """

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("WIN_PD_OVERRIDE_LOCAL_APPDATA", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("STELLA_VOICE_SOCKET", str(tmp_path / "run" / "sock"))
    monkeypatch.delenv("STELLA_MODEL", raising=False)
    monkeypatch.delenv("STELLA_SEMANTIC_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("STELLA_TRANSCRIPTION_COMMAND", raising=False)
    # Doctor's one network probe is Ollama's model list. Nothing here may
    # depend on whether the machine running the tests happens to have a
    # server up, so point it at a port that refuses connections instantly.
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:1")
    return tmp_path


def test_doctor_creates_no_state_of_its_own(tmp_path, monkeypatch) -> None:
    # The read-only promise, measured rather than asserted: a first-run
    # machine has no Stella directories at all, and asking doctor about it
    # must not leave one behind.
    root = _isolate(monkeypatch, tmp_path)
    report = collect()
    assert report.checks
    for name in ("share", "config", "run"):
        assert not (root / name).exists(), name


def test_an_unconfigured_machine_names_the_missing_piece(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    report = collect()
    provider = next(check for check in report.checks if check.name == "provider")
    assert provider.state == MISSING
    assert "stella-ui" in provider.fix
    # and the report says so in the block a human reads, not only in a field
    assert "to fix" in render(report)


def test_doctor_never_prints_a_credential(tmp_path, monkeypatch) -> None:
    # The security invariant: doctor asks whether a key exists and drops the
    # value in the same expression, so neither the text nor the JSON form can
    # carry a secret into a terminal screenshot, a log or a bug report.
    _isolate(monkeypatch, tmp_path)
    sentinel = "sk-doctor-should-never-print-this"
    provider_keys.save_api_key("openai", sentinel)
    monkeypatch.setenv("STELLA_MODEL", "some-model")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "openai")
    report = collect()
    rendered = render(report)
    assert any(check.name == "api key" for check in report.checks)
    assert sentinel not in rendered
    assert sentinel not in doctor.as_json(report)


def test_a_broken_setting_degrades_to_one_line(
    tmp_path, monkeypatch
) -> None:
    # Stella exits loudly on an invalid environment value; doctor is the
    # command you run when Stella will not start, so it reports the same
    # problem instead of dying the same way.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("STELLA_MODEL", "some-model")
    monkeypatch.setenv("STELLA_SEMANTIC_PROVIDER", "not-a-provider")
    report = collect()
    settings = next(check for check in report.checks if check.name == "settings")
    assert settings.state == MISSING
    assert "config.json" in settings.fix


def test_json_output_is_machine_readable(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    payload = json.loads(doctor.as_json(collect()))
    assert payload["checks"]
    assert set(payload["missing"]) <= {check["name"] for check in payload["checks"]}
    assert all(
        set(check) == {"group", "name", "state", "detail", "fix"}
        for check in payload["checks"]
    )
    assert all(check["state"] in (READY, MISSING, INFO) for check in payload["checks"])


def test_every_check_carries_a_group_and_a_state(tmp_path, monkeypatch) -> None:
    # Shape guard: the renderer indexes on group order and the exit code on
    # state names, so a new Check left without one of them is a bug here.
    _isolate(monkeypatch, tmp_path)
    for check in collect().checks:
        assert check.group in ("build", "state", "model", "voice", "desktop", "interface")
        assert check.state in (READY, MISSING, INFO)
        assert check.detail.strip()


def test_cli_wires_the_command_before_any_configuration(
    tmp_path, monkeypatch, capsys
) -> None:
    # Ordering is the feature: `chat` refuses to run unconfigured, and the
    # whole point of doctor is to answer questions on exactly that machine.
    _isolate(monkeypatch, tmp_path)
    try:
        cli.main(["doctor"])
    except SystemExit as exit_code:
        assert exit_code.code == 0
    else:
        raise AssertionError("doctor must exit")
    assert "read-only" in capsys.readouterr().out
