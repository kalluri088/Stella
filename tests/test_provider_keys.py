"""Tests for the provider preset table and the private API-key store."""

import json
import os
import stat

import pytest

from stella import provider_keys


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Never read or write the developer's real key store in tests."""

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


class TestStore:
    def test_roundtrip_keeps_only_the_store_on_disk(self):
        provider_keys.save_api_key("anthropic", "sk-ant-test-value")
        assert provider_keys.stored_api_key("anthropic") == "sk-ant-test-value"
        body = json.loads(
            provider_keys.api_keys_path().read_text(encoding="utf-8")
        )
        assert body == {"version": 1, "keys": {"anthropic": "sk-ant-test-value"}}

    def test_store_file_is_private(self):
        provider_keys.save_api_key("openai", "sk-proj-test")
        mode = stat.S_IMODE(os.stat(provider_keys.api_keys_path()).st_mode)
        assert mode == 0o600

    def test_saving_one_provider_preserves_another(self):
        provider_keys.save_api_key("openai", "sk-proj-one")
        provider_keys.save_api_key("xai", "xai-two")
        assert provider_keys.stored_api_key("openai") == "sk-proj-one"
        assert provider_keys.stored_api_key("xai") == "xai-two"
        assert provider_keys.stored_presets() == ("openai", "xai")

    def test_delete_removes_only_its_preset(self):
        provider_keys.save_api_key("openai", "sk-proj-one")
        provider_keys.save_api_key("groq", "gsk_test")
        provider_keys.delete_api_key("openai")
        assert provider_keys.stored_api_key("openai") is None
        assert provider_keys.stored_api_key("groq") == "gsk_test"

    def test_missing_or_malformed_store_reads_as_empty(self, tmp_path):
        assert provider_keys.stored_api_key("openai") is None
        provider_keys.api_keys_path().parent.mkdir(parents=True, exist_ok=True)
        provider_keys.api_keys_path().write_text("not json at all")
        assert provider_keys.stored_api_key("openai") is None
        assert provider_keys.stored_presets() == ()
        provider_keys.api_keys_path().write_text('{"version": 1, "keys": 7}')
        assert provider_keys.stored_api_key("openai") is None

    @pytest.mark.parametrize(
        "preset_id, key",
        [
            ("openai", ""),
            ("openai", "   "),
            ("openai", "sk-has internal space"),
            ("openai", "sk\nnewline"),
            ("openai", "x" * 4097),
            ("nonsense", "sk-proj-ok"),
            ("ollama", "sk-proj-ok"),
        ],
    )
    def test_rejects_keys_that_cannot_be_tokens_and_unknown_presets(
        self, preset_id, key
    ):
        with pytest.raises(ValueError):
            provider_keys.save_api_key(preset_id, key)

    def test_surrounding_whitespace_is_stripped_not_rejected(self):
        provider_keys.save_api_key("openai", "  sk-proj-pasted\n")
        assert provider_keys.stored_api_key("openai") == "sk-proj-pasted"

    def test_saving_over_an_unreadable_store_is_refused_not_wiped(self):
        # A corrupt or transiently unreadable store must never be merged
        # onto an empty view of itself — that would silently drop every
        # other provider's key.
        provider_keys.api_keys_path().parent.mkdir(parents=True, exist_ok=True)
        for body in ("not json at all", '{"version": 1, "keys": 7}'):
            provider_keys.api_keys_path().write_text(body)
            with pytest.raises(ValueError):
                provider_keys.save_api_key("openai", "sk-proj-new")
            assert provider_keys.api_keys_path().read_text() == body

    def test_a_refused_save_leaves_no_temp_files_behind(self):
        provider_keys.api_keys_path().parent.mkdir(parents=True, exist_ok=True)
        provider_keys.api_keys_path().write_text("corrupt")
        with pytest.raises(ValueError):
            provider_keys.save_api_key("openai", "sk-proj-new")
        provider_keys.api_keys_path().unlink()
        provider_keys.save_api_key("openai", "sk-proj-new")
        leftovers = [
            entry.name
            for entry in provider_keys.api_keys_path().parent.iterdir()
            if entry.name != "api_keys.json"
        ]
        assert leftovers == []

    def test_deleting_from_an_unreadable_store_does_not_clobber_it(self):
        provider_keys.api_keys_path().parent.mkdir(parents=True, exist_ok=True)
        provider_keys.api_keys_path().write_text("corrupt")
        provider_keys.delete_api_key("openai")
        assert provider_keys.api_keys_path().read_text() == "corrupt"


class TestPrecedence:
    def test_environment_wins_for_the_openai_slot(self, monkeypatch):
        provider_keys.save_api_key("openai", "sk-proj-stored")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-env")
        assert provider_keys.effective_api_key("openai") == "sk-proj-env"
        assert provider_keys.effective_api_key(None) == "sk-proj-env"
        assert provider_keys.effective_api_key("custom") == "sk-proj-env"

    def test_store_is_used_when_the_environment_is_silent(self):
        provider_keys.save_api_key("anthropic", "sk-ant-stored")
        assert provider_keys.effective_api_key("anthropic") == "sk-ant-stored"

    def test_openai_env_key_never_leaks_into_a_named_provider_preset(
        self, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-env")
        assert provider_keys.effective_api_key("anthropic") is None
        provider_keys.save_api_key("anthropic", "sk-ant-stored")
        assert provider_keys.effective_api_key("anthropic") == "sk-ant-stored"

    def test_legacy_missing_preset_resolves_the_openai_slot(self):
        provider_keys.save_api_key("openai", "sk-proj-legacy")
        assert provider_keys.effective_api_key(None) == "sk-proj-legacy"


class TestDetection:
    @pytest.mark.parametrize(
        "key, expected",
        [
            ("sk-or-v1-abc123", "openrouter"),
            ("sk-proj-abc123", "openai"),
            ("sk-ant-api03-abc", "anthropic"),
            ("gsk_abcdef", "groq"),
            ("xai-abcdef", "xai"),
            ("AIzaSyabcdef", "google"),
            ("sk-abc123", "openai"),
            ("  sk-ant-api03-padded  ", "anthropic"),
            ("totally-unknown-shape", None),
            ("", None),
        ],
    )
    def test_longest_prefix_wins(self, key, expected):
        assert provider_keys.detect_key_provider(key) == expected

    def test_mismatch_hint_names_the_other_provider_not_the_key(self):
        hint = provider_keys.mismatch_hint("openai", "sk-ant-api03-secretvalue")
        assert hint is not None
        assert "Claude (Anthropic)" in hint
        assert "platform.openai.com" in hint
        assert "secretvalue" not in hint
        assert "sk-ant" not in hint

    @pytest.mark.parametrize(
        "preset_id, key",
        [
            ("anthropic", "sk-ant-api03-match"),
            ("custom", "sk-ant-api03-whatever"),
            (None, "sk-ant-api03-whatever"),
            ("openai", "totally-unknown-shape"),
            ("ollama", "sk-ant-api03-x"),
        ],
    )
    def test_no_hint_when_nothing_is_wrong_or_nothing_is_known(
        self, preset_id, key
    ):
        assert provider_keys.mismatch_hint(preset_id, key) is None

    def test_mismatch_hint_is_silent_for_an_empty_key(self):
        assert provider_keys.mismatch_hint("openai", "") is None


class TestDisplayHelpers:
    def test_redacted_hint_shows_only_the_suffix(self):
        assert provider_keys.redacted_hint("sk-ant-api03-abcdef1f3a") == "…1f3a"
        assert provider_keys.redacted_hint("short") == "stored"

    def test_dialect_table(self):
        assert provider_keys.tool_dialect_for(None) == "responses"
        assert provider_keys.tool_dialect_for("openai") == "responses"
        for preset_id in ("anthropic", "xai", "groq", "openrouter", "google"):
            assert provider_keys.tool_dialect_for(preset_id) == "chat"
        # An unknown gateway is assumed to speak chat.completions: it is
        # the overwhelmingly common dialect.
        assert provider_keys.tool_dialect_for("custom") == "chat"
        assert provider_keys.tool_dialect_for("nonsense") == "responses"

    def test_base_urls(self):
        assert provider_keys.base_url_for("openai") is None
        assert (
            provider_keys.base_url_for("anthropic")
            == "https://api.anthropic.com/v1"
        )
        assert provider_keys.base_url_for("nonsense") is None

    def test_every_keyed_preset_can_be_detected_or_is_custom(self):
        for preset in provider_keys.PRESETS.values():
            if preset.key_required and preset.id != "custom":
                assert preset.detection_prefixes, preset.id
