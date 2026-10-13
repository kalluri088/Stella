"""`stella doctor` — the read-only preflight a one-command install depends on.

These tests hold the two promises that make the command trustworthy: it
changes nothing it inspects, and it never prints a credential. Everything
else is presentation.
"""

import json

from stella import cli, config, doctor, provider_keys
from stella.app import StellaSettings
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
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    monkeypatch.delenv("STELLA_TRANSCRIPTION_COMMAND", raising=False)
    monkeypatch.delenv("STELLA_OUTLINE", raising=False)
    # Doctor's network probes are Ollama's model list, the saved provider's
    # GET /models, and — only when the owner has asked for it — Outline's
    # /healthz. Nothing here may depend on whether the machine running the
    # tests happens to have any of them up, so point them at ports that refuse
    # connections instantly.
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("OUTLINE_URL", "http://127.0.0.1:1")
    # The provider probe needs this one most: without it, a test that carries
    # a stored OpenAI key would resolve the SDK default and send a real HTTPS
    # request to api.openai.com from the suite.
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
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
    # The endpoint line is the one that sends the key over the wire, so it
    # belongs to this promise rather than being a clean-output test of its own
    # that could pass with the probe absent.
    assert any(check.name == "endpoint" for check in report.checks)
    assert sentinel not in rendered
    assert sentinel not in doctor.as_json(report)


def _endpoint_check(report):
    return next(check for check in report.checks if check.name == "endpoint")


def test_an_endpoint_that_does_not_answer_is_named_in_both_forms(
    tmp_path, monkeypatch
) -> None:
    # The failure #240 was filed for: a saved preset pointing at a router
    # that is not running. Every turn came back "Connection error." while the
    # report was green, because nothing in it had asked the address. The line
    # has to name the URL a turn would have been sent to — that is the entire
    # value over "a request failed" — and the fix has to say what to start.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("STELLA_MODEL", "some-model")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "openai")
    dead = "http://127.0.0.1:9/v1"
    monkeypatch.setenv("OPENAI_BASE_URL", dead)
    provider_keys.save_api_key("openai", "sk-endpoint-sentinel")
    monkeypatch.setattr(
        doctor,
        "check_api_key",
        lambda *_args, **_kwargs: config.KeyCheck(
            "unreachable", "the endpoint is not reachable"
        ),
    )
    report = collect()
    check = _endpoint_check(report)
    assert check.state == MISSING
    assert dead in check.detail
    assert "some-model" in check.detail
    assert dead in check.fix
    assert "endpoint" in json.loads(doctor.as_json(report))["missing"]
    rendered = render(report)
    assert dead in rendered
    # Naming the address is allowed; naming the token that was offered to it
    # is not, even on the line that failed.
    assert "sk-endpoint-sentinel" not in rendered


def test_the_probe_asks_the_address_a_turn_would_actually_be_sent_to(
    tmp_path, monkeypatch
) -> None:
    # A green line about the wrong server proves nothing about the next
    # request. The probe resolves the endpoint exactly the way
    # build_application hands it to the OpenAI client: the override first,
    # then the preset's own URL, then the SDK's default for a None — because
    # a None there is not "no endpoint".
    _isolate(monkeypatch, tmp_path)
    asked: list[str] = []

    def fake(endpoint, _key, *_args, **_kwargs):
        asked.append(endpoint)
        return config.KeyCheck("verified", "the endpoint accepted the key")

    monkeypatch.setattr(doctor, "check_api_key", fake)
    monkeypatch.setenv("STELLA_MODEL", "some-model")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("STELLA_PRESET", "openrouter")

    # 1. an explicit base URL wins over whatever the preset names
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    provider_keys.save_api_key("openrouter", "sk-route-sentinel")
    assert _endpoint_check(collect()).state == READY
    assert asked == ["http://127.0.0.1:9/v1"]

    # 2. without it the preset names the server, and the line says that
    # address rather than the preset's name
    asked.clear()
    monkeypatch.delenv("OPENAI_BASE_URL")
    check = _endpoint_check(collect())
    assert check.state == READY
    assert asked == ["https://openrouter.ai/api/v1"]
    assert "https://openrouter.ai/api/v1" in check.detail

    # 3. and a preset with no address of its own is the SDK default, which is
    # the server a turn would really reach — not "unknown, nothing to ask"
    asked.clear()
    monkeypatch.setenv("STELLA_PRESET", "custom")
    provider_keys.save_api_key("custom", "sk-custom-sentinel")
    check = _endpoint_check(collect())
    assert check.state == READY
    assert asked == [config.DEFAULT_OPENAI_BASE_URL]
    assert config.DEFAULT_OPENAI_BASE_URL in check.detail


def test_a_preset_with_no_key_is_named_but_not_probed(
    tmp_path, monkeypatch
) -> None:
    # Nothing to authenticate the request with means no answer is knowable,
    # and a blind probe is a wrong answer waiting to nag. The line still says
    # which address the next turn would go to, because that is the fact the
    # owner needs in order to add the key in the right place.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("STELLA_MODEL", "some-model")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("STELLA_PRESET", "freellmapi")

    def never_probed(*_args, **_kwargs):
        raise AssertionError("a keyless preset must not reach the network")

    monkeypatch.setattr(doctor, "check_api_key", never_probed)
    check = _endpoint_check(collect())
    assert check.state == INFO
    assert "http://127.0.0.1:1/v1" in check.detail
    # and the advice must not send a FreeLLMAPI owner to a variable that is
    # read for the OpenAI and custom slots only
    assert "counts for the OpenAI and custom slots only" in check.fix


def test_a_router_that_guards_its_model_list_is_not_called_a_bad_key(
    tmp_path, monkeypatch
) -> None:
    # The same mistake the startup key sweep exists to prevent, replayed in
    # the report: a chat-dialect router answers 401 to GET /models while
    # accepting the identical key on chat. doctor has to send the probe with
    # the dialect the launch would use, or a working setup is told its key was
    # refused.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("STELLA_MODEL", "some-model")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("STELLA_PRESET", "freellmapi")
    provider_keys.save_api_key("freellmapi", "sk-router-sentinel")
    seen: dict[str, bool] = {}

    def fake(_endpoint, _key, *_args, chat_dialect=False, **_kwargs):
        seen["chat_dialect"] = chat_dialect
        return config.KeyCheck(
            "inconclusive", "this endpoint guards its model list"
        )

    monkeypatch.setattr(doctor, "check_api_key", fake)
    check = _endpoint_check(collect())
    assert seen["chat_dialect"] is True
    assert check.state == INFO
    assert "refused" not in check.detail
    # A responses-dialect preset gets judged on its model list, so a 401 there
    # really is the key.
    monkeypatch.setenv("STELLA_PRESET", "openai")
    provider_keys.save_api_key("openai", "sk-plain-sentinel")
    seen.clear()
    _endpoint_check(collect())
    assert seen["chat_dialect"] is False


def test_a_llama_box_is_no_longer_asked_for_a_key(
    tmp_path, monkeypatch
) -> None:
    # Stella owns and starts the server for this provider, so the preset says
    # a key does not apply (provider_keys.PRESETS, key_required=False). It used
    # to fall through to the OpenAI branch and be told "api key: MISSING — add
    # it in stella-ui" on a correctly configured machine, which is the kind of
    # advice that sends an owner editing a setting that cannot help.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("STELLA_MODEL", str(tmp_path / "absent.gguf"))
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "llama")
    names = [check.name for check in collect().checks]
    assert "api key" not in names
    assert "endpoint" not in names
    assert "model file" in names and "llama-server" in names


def test_llama_checks_answer_on_the_file_and_the_binary(tmp_path) -> None:
    # Driven directly: llama_binary is neither a saved field nor read by the
    # pure-environment path, so a collect() run can only ever reach the missing
    # side of the binary from the environment. Both states of both lines
    # belong to the diagnosis, so both are named here.
    gguf = tmp_path / "brain.gguf"
    gguf.write_text("not a real model\n")

    ready = doctor._llama_checks(
        StellaSettings(
            provider="llama", model=str(gguf), llama_binary="sh"
        )
    )
    assert [check.state for check in ready] == [READY, READY]

    missing = doctor._llama_checks(
        StellaSettings(
            provider="llama",
            model=str(tmp_path / "absent.gguf"),
            llama_binary="no-such-server",
        )
    )
    assert [check.state for check in missing] == [MISSING, MISSING]
    assert ".gguf" in missing[0].fix
    assert "llama.cpp" in missing[1].fix


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


def _web_check(report):
    return next(check for check in report.checks if check.name == "web search")


def test_the_web_line_lights_for_either_place_its_key_lives(
    tmp_path, monkeypatch
) -> None:
    # The web tools read TINYFISH_API_KEY first and then the secret typed into
    # Settings; doctor read only the store, so an environment-only key was
    # reported as "web tools say 'web is off'" on a machine whose tools were
    # calling api.tinyfish.ai (#266). Both sources are one answer here.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)

    provider_keys.save_secret(provider_keys.TINYFISH_SECRET, "tf-sentinel-value")
    report = collect()
    assert _web_check(report).state == READY
    assert _web_check(report).detail == "TinyFish key"
    assert "tf-sentinel-value" not in render(report)

    provider_keys.delete_secret(provider_keys.TINYFISH_SECRET)
    monkeypatch.setenv("TINYFISH_API_KEY", "sk-tf-sentinel")
    report = collect()
    assert _web_check(report).state == READY
    assert "sk-tf-sentinel" not in render(report)


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
