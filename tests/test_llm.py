import pytest

from stella.llm import FakeLLMClient, LLMClient, Message


def test_llm_client_is_abstract() -> None:
    with pytest.raises(TypeError):
        LLMClient()


def test_fake_llm_client_returns_configured_response() -> None:
    client = FakeLLMClient(response="hello")

    assert client.chat([Message(role="user", content="hi")]) == "hello"


def test_fake_llm_client_accepts_normal_conversation_messages() -> None:
    client = FakeLLMClient(response="hello")

    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"
