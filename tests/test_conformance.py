"""B4: every written provider obligation replays on every dialect.

These tests are the offline half of the conformance suite: a loopback
simulated provider replays known real-world wire shapes at the actual
client classes and the real ``LLMBrain`` decision path. No network
beyond 127.0.0.1, no GPU, no sleeps.
"""
import json

import pytest

from stella.brain import LLMBrain
from stella.conformance import (
    ALL_DIALECTS,
    CONFORMANCE_CASES,
    DECISION_NUM_CTX,
    MODEL,
    SimulatedProvider,
    WireReply,
    decision_prompt_tokens,
    estimate_tokens,
    run_case,
    run_conformance,
)
from stella.context import Context, ToolObservation
from stella.llm import FakeLLMClient, Message
from stella.memory import MemoryItem
from stella.ollama_client import OllamaLLMClient


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


def _busiest_context() -> Context:
    """A turn carrying everything a real one can arrive with."""

    return Context(
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


def test_decision_prompt_still_fits_the_wired_context_budget() -> None:
    # The written form of the report-15 obligation: the decision prompt
    # must fit the context budget the client is configured with. This is
    # the prompt-bloat tripwire — it only needs to catch an order-of-
    # magnitude overrun, which is why a ~4 chars/token estimate is
    # enough here while exact counts stay the provider's job.
    # FakeLLMClient is the pessimistic arm: it declares nothing, so the
    # prompt also carries the prose copy of the capability registry.
    brain = LLMBrain(FakeLLMClient())  # the full default tool set
    tokens = decision_prompt_tokens(brain, _busiest_context())
    assert tokens > 1000  # the tripwire measures a real-size prompt
    assert tokens < DECISION_NUM_CTX


def test_a_declaring_clients_prompt_is_smaller_by_the_copy_it_declares() -> None:
    """The tripwire must not stay blind to the shipped path.

    Ollama's native `/api/chat` is the client the local brain uses, and it
    is the one that declares the registry on the wire — which is where a
    prompt measured at 8,151 tokens against an 8,192 budget overflowed.
    The same turn is measured here both ways, over the Brain's own default
    tool set; the shipped application registers more capabilities and so
    saves more."""

    client = OllamaLLMClient(
        model=MODEL,
        base_url="http://127.0.0.1:1/v1",
        native=True,
        num_ctx=DECISION_NUM_CTX,
    )
    brain = LLMBrain(client)
    context = _busiest_context()

    declared = decision_prompt_tokens(brain, context)
    client.declares_tools_natively = False
    prose_copy = decision_prompt_tokens(brain, context)

    registry = (
        "Currently available tools:\n"
        f"{json.dumps(brain.tools.describe(), sort_keys=True)}\n\n"
    )
    assert declared < prose_copy
    # ~4 characters per token rounds each side, so the saving is the
    # block's own estimated size to within one token — no more, no less.
    assert prose_copy - declared >= estimate_tokens(registry) - 1
    assert declared < DECISION_NUM_CTX
