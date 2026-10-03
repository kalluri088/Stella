"""Settings-wiring drift guard.

Every opt-in capability is hand-wired across six touchpoints: an
``*_env_override()`` in app.py, a ``Settings`` field, its use in
``Settings.from_saved`` and ``Settings.from_environment``, a
``config._CONFIG_FIELDS`` entry (persist + load), and a Settings checkbox
in ui.py that feeds the save dict. One missed line is a silently
unsaveable or unregisterable tool, so the pairing is enforced mechanically:
a new capability that skips a touchpoint fails here until it is wired (or
deliberately added below with a reason).
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from pathlib import Path

import pytest

from stella import app as app_module
from stella import config as config_module
from stella.app import StellaSettings

UI_SOURCE = (Path(inspect.getsourcefile(app_module)).parent / "ui.py").read_text("utf-8")

# env override -> the Settings field it governs. The names do not map
# mechanically (semantic_env_override -> semantic_memory_enabled), so the
# pairing is explicit; test_override_functions_are_exactly_the_table stops
# a new override from existing outside the table unnoticed.
OVERRIDE_FIELDS = {
    "transcripts_env_override": "transcripts_enabled",
    "semantic_env_override": "semantic_memory_enabled",
    "semantic_provider_env_override": "semantic_provider",
    "os_tools_env_override": "os_tools_enabled",
    "outline_tools_env_override": "outline_tools_enabled",
    "web_tools_env_override": "web_tools_enabled",
    "shell_tools_env_override": "shell_tools_enabled",
    "wake_env_override": "wake_word_enabled",
}

# Settings checkbox that writes each opt-in field into the save dict.
UI_VARS = {
    "transcripts_enabled": "self._transcripts_var",
    "semantic_memory_enabled": "self._semantic_var",
    "os_tools_enabled": "self._os_tools_var",
    "outline_tools_enabled": "self._outline_tools_var",
    "web_tools_enabled": "self._web_tools_var",
    "shell_tools_enabled": "self._shell_tools_var",
    "wake_word_enabled": "self._wake_var",
}


def _settings_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(StellaSettings)}


def test_override_functions_are_exactly_the_table() -> None:
    found = set(
        re.findall(r"^def (\w*_env_override)\(", inspect.getsource(app_module), re.MULTILINE)
    )
    assert found == set(OVERRIDE_FIELDS), (
        "a *_env_override appeared or vanished; update OVERRIDE_FIELDS only "
        "together with the full six-touchpoint wiring"
    )


def test_every_override_governs_a_settings_field() -> None:
    fields = _settings_fields()
    for field in OVERRIDE_FIELDS.values():
        assert field in fields, f"override governs unknown field {field}"


@pytest.mark.parametrize("override,field", sorted(OVERRIDE_FIELDS.items()))
def test_from_saved_consults_the_override_and_wires_the_field(override, field) -> None:
    body = inspect.getsource(StellaSettings.from_saved)
    assert f"{override}()" in body, f"{field}: {override} is not consulted in from_saved"
    assert re.search(rf"\b{field}=", body), f"{field} is not passed through in from_saved"


@pytest.mark.parametrize("field", sorted(set(OVERRIDE_FIELDS.values())))
def test_from_environment_wires_the_field(field: str) -> None:
    body = inspect.getsource(StellaSettings.from_environment)
    assert re.search(rf"\b{field}=", body), f"{field} is not set in from_environment"


@pytest.mark.parametrize("field", sorted(set(OVERRIDE_FIELDS.values())))
def test_field_is_persisted_in_config_fields(field: str) -> None:
    assert field in config_module._CONFIG_FIELDS, f"{field} never reaches config.json"


def test_config_fields_are_all_real_settings_fields() -> None:
    fields = _settings_fields()
    unknown = set(config_module._CONFIG_FIELDS) - fields
    assert not unknown, f"_CONFIG_FIELDS names non-fields: {sorted(unknown)}"


@pytest.mark.parametrize("field", sorted(set(OVERRIDE_FIELDS.values())))
def test_resolve_settings_reads_the_field_back(field: str) -> None:
    body = inspect.getsource(config_module.resolve_settings)
    assert re.search(rf'raw\.get\("{field}"[,)]', body), (
        f"{field} is not read back from config.json"
    )


@pytest.mark.parametrize("field,var", sorted(UI_VARS.items()))
def test_ui_checkbox_writes_the_field_into_the_save_dict(field: str, var: str) -> None:
    assert f"{var} = tk.BooleanVar" in UI_SOURCE, f"{field}: no checkbox var {var}"
    assert f"{field}={var}.get()" in UI_SOURCE, (
        f"{field}: {var} never reaches the saved settings"
    )


def test_ui_opt_in_vars_are_exactly_the_table() -> None:
    found = set(re.findall(r"(self\._\w+_var) = tk\.BooleanVar", UI_SOURCE))
    # the speak and mute toggles are live session controls, not capability
    # opt-ins: they change what this running session does and are never saved
    expected = set(UI_VARS.values()) | {"self._speak_var", "self._mute_var"}
    assert found == expected, (
        "a BooleanVar capability checkbox appeared or vanished; add/remove it "
        "in UI_VARS together with the full six-touchpoint wiring"
    )


# ---------------------------------------------------------------- plain fields
#
# Saved-configuration fields that are not capability opt-ins (no
# *_env_override, no checkbox): they still must be persisted and read
# back, so they get their own explicit table. A new field must appear
# here or in OVERRIDE_FIELDS — test_config_fields_are_exactly_the_tables
# closes the set in both directions. "core" only records which fields
# existed before the drift guard; every plain field gets the same wiring
# check.

PLAIN_CONFIG_FIELDS = (
    "provider",
    "model",
    "preset",
    "ollama_base_url",
    "openai_base_url",
)


def test_config_fields_are_exactly_the_tables() -> None:
    assert set(config_module._CONFIG_FIELDS) == set(PLAIN_CONFIG_FIELDS) | set(
        OVERRIDE_FIELDS.values()
    ), (
        "a saved-configuration field appeared or vanished; every one is "
        "either a capability opt-in (OVERRIDE_FIELDS) or a plain field "
        "(PLAIN_CONFIG_FIELDS)"
    )


@pytest.mark.parametrize("field", sorted(PLAIN_CONFIG_FIELDS))
def test_plain_config_field_is_wired_end_to_end(field: str) -> None:
    assert field in _settings_fields(), f"{field} is not a StellaSettings field"
    assert re.search(rf"\b{field}=", inspect.getsource(StellaSettings.from_saved)), (
        f"{field} is not passed through in from_saved"
    )
    assert re.search(
        rf"\b{field}=", inspect.getsource(StellaSettings.from_environment)
    ), f"{field} is not set in from_environment"
    # provider/model are guaranteed by load_configuration and indexed
    # directly; the optional fields are read with raw.get.
    resolve_body = inspect.getsource(config_module.resolve_settings)
    assert (
        f'raw.get("{field}")' in resolve_body or f'raw["{field}"]' in resolve_body
    ), f"{field} is not read back from config.json"
