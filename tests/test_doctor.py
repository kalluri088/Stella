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
    monkeypatch.delenv("STELLA_OUTLINE", raising=False)
    # Doctor's network probes are Ollama's model list and — only when the
    # owner has asked for it — Outline's /healthz. Nothing here may depend
    # on whether the machine running the tests happens to have either up,
    # so point them at ports that refuse connections instantly.
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("OUTLINE_URL", "http://127.0.0.1:1")
    # Without this the Outline probe would read the *real* token from this
    # platform's user data directory, so a test run on the owner's laptop
    # would answer a different question than a test run in CI.
    monkeypatch.setenv("OUTLINE_DATA_DIR", str(tmp_path / "share" / "outline"))
    monkeypatch.delenv("OUTLINE_TOKEN", raising=False)
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


def test_a_platform_without_unix_sockets_is_reported_not_raised(
    tmp_path, monkeypatch
) -> None:
    # ``stella doctor`` is the command the installer tells every user to
    # run, and the doorbell it asks about lives in a socket family this
    # platform either has or has not. Absent, the answer is a line saying
    # so — never an AttributeError on the machine being diagnosed.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("stella.headless_voice.has_unix_sockets", lambda: False)
    check = next(item for item in collect().checks if item.name == "voice server")
    assert check.state == INFO
    assert "no control socket" in check.detail


def _outline_check(report):
    return next(check for check in report.checks if check.name == "outline")


def test_a_capability_that_is_off_is_not_probed(tmp_path, monkeypatch) -> None:
    # The owner who never ticked the box must not pay a loopback GET, and
    # must not be told anything is missing. Registration is silent by
    # design, so doctor is the only place this state gets said out loud.
    _isolate(monkeypatch, tmp_path)

    def never_probed(*args, **kwargs):
        raise AssertionError("a disabled Outline must not reach the network")

    monkeypatch.setattr(doctor, "probe_outline", never_probed)
    check = _outline_check(collect())
    assert check.state == INFO
    assert not check.fix


def test_a_ticked_box_with_no_server_says_so_in_terms_to_act_on(
    tmp_path, monkeypatch
) -> None:
    # The defect this pins: Outline is doubly gated, so a checked Settings
    # box with nothing listening on 8741 produces no tools and no message.
    # doctor has to distinguish "you never asked" from "you asked and it
    # is not there", because only the second one is a problem.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("STELLA_OUTLINE", "1")
    monkeypatch.setenv("OUTLINE_TOKEN", "outline-sentinel-value")
    report = collect()
    check = _outline_check(report)
    assert check.state == MISSING
    assert "no Outline server answered" in check.detail
    assert "start the Outline server" in check.fix


def test_a_reachable_outline_is_reported_ready(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("STELLA_OUTLINE", "1")
    monkeypatch.setenv("OUTLINE_TOKEN", "outline-sentinel-value")
    monkeypatch.setattr("stella.outline_tools._healthz", lambda client: True)
    check = _outline_check(collect())
    assert check.state == READY
    assert not check.fix


def test_a_missing_token_names_the_directory_that_was_read(
    tmp_path, monkeypatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("STELLA_OUTLINE", "1")
    check = _outline_check(collect())
    assert check.state == MISSING
    assert "no outline.token was found" in check.detail
    assert str(tmp_path / "share" / "outline") in check.detail
    assert "OUTLINE_DATA_DIR" in check.fix


def test_doctor_never_prints_the_outline_token(tmp_path, monkeypatch) -> None:
    # Every state the Outline gate can be in, both output forms, one
    # sentinel: the diagnosis names where the token lives and never what
    # it is, so a pasted report cannot leak a working workspace credential.
    sentinel = "outline-sentinel-doctor-must-not-print"
    for healthy in (True, False):
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("STELLA_OUTLINE", "1")
        monkeypatch.setenv("OUTLINE_TOKEN", sentinel)
        monkeypatch.setattr(
            "stella.outline_tools._healthz", lambda client, ok=healthy: ok
        )
        report = collect()
        assert sentinel not in render(report)
        assert sentinel not in doctor.as_json(report)


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
