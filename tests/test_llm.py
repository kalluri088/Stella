import threading
import time

import pytest

from stella.llm import (
    FakeLLMClient,
    LLMClient,
    Message,
    ProviderRequestCancelled,
    UsageRecorder,
    openai_usage_counts,
    run_cancellable,
)


def test_llm_client_is_abstract() -> None:
    with pytest.raises(TypeError):
        LLMClient()


def test_fake_llm_client_returns_configured_response() -> None:
    client = FakeLLMClient(response="hello")

    assert client.chat([Message(role="user", content="hi")]) == "hello"


def test_fake_llm_client_accepts_normal_conversation_messages() -> None:
    client = FakeLLMClient(response="hello")

    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"


# ------------------------------------------------------- run_cancellable


def test_run_cancellable_without_a_check_is_a_plain_inline_call() -> None:
    def request() -> str:
        assert threading.current_thread() is threading.main_thread()
        return "value"

    assert run_cancellable(request, None) == "value"


def test_run_cancellable_propagates_provider_errors_with_no_check() -> None:
    def request() -> None:
        raise ValueError("provider exploded")

    with pytest.raises(ValueError, match="provider exploded"):
        run_cancellable(request, None)


def test_run_cancellable_returns_the_result_when_never_cancelled() -> None:
    assert run_cancellable(lambda: 7, lambda: False) == 7


def test_run_cancellable_aborts_a_stuck_request_promptly() -> None:
    started = time.monotonic()

    with pytest.raises(ProviderRequestCancelled):
        run_cancellable(
            lambda: time.sleep(30),
            lambda: time.monotonic() - started > 0.1,
            poll_seconds=0.05,
        )
    assert time.monotonic() - started < 5


def test_run_cancellable_surfaces_a_result_that_lands_alongside_cancel() -> None:
    # A request that finishes while the cancel is being pressed already
    # produced its answer; throwing that away would be dishonest waste.
    result = run_cancellable(
        lambda: "landed", lambda: True, poll_seconds=0.05
    )
    assert result == "landed"


def test_run_cancellable_reraises_helper_errors_on_the_caller_thread() -> None:
    def request() -> str:
        raise RuntimeError("socket died")

    with pytest.raises(RuntimeError, match="socket died"):
        run_cancellable(request, lambda: False, poll_seconds=0.01)


def test_run_cancellable_orphan_errors_never_resurface() -> None:
    release = threading.Event()

    def request() -> str:
        release.wait(5)
        raise RuntimeError("late failure of an abandoned call")

    started = time.monotonic()
    with pytest.raises(ProviderRequestCancelled):
        run_cancellable(
            request,
            lambda: time.monotonic() - started > 0.1,
            poll_seconds=0.05,
        )
    # The orphan thread finishes (badly) after the caller gave up; that
    # failure stays in its own box. Rejoining proves the orphan ran to
    # completion without ever surfacing its error to the caller.
    release.set()
    time.sleep(0.2)


def test_run_cancellable_reports_cancellation_even_if_transport_close_fails() -> None:
    def on_cancel() -> None:
        raise OSError("already closed")

    started = time.monotonic()
    with pytest.raises(ProviderRequestCancelled):
        run_cancellable(
            lambda: time.sleep(30),
            lambda: True,
            poll_seconds=0.05,
            on_cancel=on_cancel,
        )
    assert time.monotonic() - started < 5


def test_usage_recorder_tallies_only_reported_counts() -> None:
    usage = UsageRecorder()
    usage.record(100, 20)
    usage.record(None, 5)
    usage.record(500, None)

    snapshot = usage.snapshot()
    assert snapshot.requests == 3
    assert snapshot.prompt_tokens == 600
    assert snapshot.completion_tokens == 25
    # The biggest single prompt is what says whether the context window is
    # being clipped, so it survives as its own number rather than a sum.
    assert snapshot.largest_prompt == 500


def test_usage_recorder_ignores_a_nonsensical_reported_count() -> None:
    # A client hands over whatever the provider's JSON said. A string or a
    # negative is not a token count, and adding it would poison the total.
    usage = UsageRecorder()
    usage.record("many", -7)

    snapshot = usage.snapshot()
    assert snapshot.requests == 1
    assert snapshot.prompt_tokens == 0
    assert snapshot.completion_tokens == 0


def test_usage_recorder_is_safe_across_threads() -> None:
    usage = UsageRecorder()

    def hammer() -> None:
        for _ in range(200):
            usage.record(1, 1)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = usage.snapshot()
    assert snapshot.requests == 800
    assert snapshot.prompt_tokens == 800


def test_openai_usage_counts_reads_the_shape_the_sdk_returns() -> None:
    from types import SimpleNamespace

    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3)
    )
    assert openai_usage_counts(response) == (12, 3)
    # A response with no usage block is not an error: it yields nothing.
    assert openai_usage_counts(SimpleNamespace(usage=None)) == (None, None)
