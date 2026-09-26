"""B4: every written provider obligation replays on every dialect.

These tests are the offline half of the conformance suite: a loopback
simulated provider replays known real-world wire shapes at the actual
client classes and the real ``LLMBrain`` decision path. No network
beyond 127.0.0.1, no GPU, no sleeps.
"""

import pytest

from stella.brain import LLMBrain
from stella.conformance import (
    ALL_DIALECTS,
    CONFORMANCE_CASES,
    DECISION_NUM_CTX,
    SimulatedProvider,
    WireReply,
    decision_prompt_tokens,
    run_case,
    run_conformance,
)
from stella.context import Context, ToolObservation
from stella.llm import FakeLLMClient, Message
from stella.memory import MemoryItem


@pytest.mark.parametrize("dialect", sorted(ALL_DIALECTS))
@pytest.mark.parametrize(
    "case", CONFORMANCE_CASES, ids=[case.name for case in CONFORMANCE_CASES]
)
def test_conformance_obligation_holds_on_dialect(
    case, dialect: str
) -> None:
    if dialect not in case.dialects:
        pytest.skip(f"{case.name} does not apply to {dialect}")
    result = run_case(case, dialect)
    assert result.passed, f"{case.name} [{dialect}]: {result.detail}"


def test_full_conformance_matrix_all_passes() -> None:
    results = run_conformance()
    assert len(results) == sum(
        len(case.dialects) for case in CONFORMANCE_CASES
    )
    failed = [
        f"{r.case} [{r.dialect}]: {r.detail}" for r in results if not r.passed
    ]
    assert not failed, "\n".join(failed)


def test_simulated_provider_records_the_request() -> None:
    with SimulatedProvider() as provider:
        provider.queue(WireReply(content='{"kind":"do_nothing"}'))
        from stella.ollama_client import OllamaLLMClient

        client = OllamaLLMClient(
            model="m", base_url=provider.base_url, native=True
        )
        client.chat([{"role": "user", "content": "hi"}])

        assert provider.requests[-1]["path"] == "/api/chat"
        assert provider.requests[-1]["payload"]["messages"] == [
            {"role": "user", "content": "hi"}
        ]


def test_decision_prompt_still_fits_the_wired_context_budget() -> None:
    # The written form of the report-15 obligation: the decision prompt
    # must fit the context budget the client is configured with. This is
    # the prompt-bloat tripwire — it only needs to catch an order-of-
    # magnitude overrun, which is why a ~4 chars/token estimate is
    # enough here while exact counts stay the provider's job.
    brain = LLMBrain(FakeLLMClient())  # the full default tool set
    context = Context(
        user_input="remind me in two minutes to check the kettle",
        conversation_history=[
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi there"),
        ]
        * 6,
        retrieved_memories=[
            MemoryItem(content=f"remembered fact number {index}")
            for index in range(5)
        ],
        tool_observations=[
            ToolObservation(
                capability="datetime",
                arguments={"kind": "datetime"},
                success=True,
                output="2026-01-01T12:00+00:00",
            )
        ]
        * 4,
    )
    tokens = decision_prompt_tokens(brain, context)
    assert tokens > 1000  # the tripwire measures a real-size prompt
    assert tokens < DECISION_NUM_CTX
