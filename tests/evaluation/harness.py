"""Small deterministic harness for evaluating Stella control flow."""

from collections.abc import Callable
from dataclasses import dataclass

from stella.proactivity import ProactivityResult
from stella.stella import StellaResult
from stella.trace import ApprovalEvent, DecisionEvent


@dataclass(frozen=True)
class EvaluationObservation:
    """Observable behavior extracted from one Stella scenario."""

    decision_kinds: tuple[str, ...]
    capabilities: tuple[str, ...]
    tool_successes: tuple[bool, ...]
    approval_decisions: tuple[bool | None, ...]
    memory_retrieved: int
    memory_written: bool
    trace_events: tuple[str, ...]
    policy_modes: tuple[str, ...] = ()
    proactivity_kind: str | None = None

    @classmethod
    def from_stella(
        cls,
        result: StellaResult,
        policy_modes: tuple[str, ...] = (),
    ) -> "EvaluationObservation":
        if result.interaction_trace is None:
            raise AssertionError("Stella result did not contain an interaction trace")

        trace = result.interaction_trace
        decisions = tuple(
            event
            for event in trace.events
            if isinstance(event, DecisionEvent)
        )
        approvals = tuple(
            event
            for event in trace.events
            if isinstance(event, ApprovalEvent)
        )
        return cls(
            decision_kinds=tuple(event.kind for event in decisions),
            capabilities=tuple(
                event.capability
                for event in decisions
                if event.kind == "tool" and event.capability is not None
            ),
            tool_successes=tuple(
                step.tool_result.success
                for step in result.step_trace
                if step.tool_result is not None
            ),
            approval_decisions=tuple(event.approved for event in approvals),
            memory_retrieved=len(result.retrieved_memories),
            memory_written=(
                result.memory_write is not None and result.memory_write.written
            ),
            trace_events=tuple(type(event).__name__ for event in trace.events),
            policy_modes=policy_modes,
        )

    @classmethod
    def from_proactivity(
        cls, result: ProactivityResult
    ) -> "EvaluationObservation":
        return cls(
            decision_kinds=(),
            capabilities=(),
            tool_successes=(),
            approval_decisions=(),
            memory_retrieved=0,
            memory_written=False,
            trace_events=(),
            policy_modes=(),
            proactivity_kind=result.kind.value,
        )


@dataclass(frozen=True)
class EvaluationExpectation:
    """Exact deterministic expectations for one scenario."""

    decision_kinds: tuple[str, ...] | None = None
    capabilities: tuple[str, ...] | None = None
    tool_successes: tuple[bool, ...] | None = None
    approval_decisions: tuple[bool | None, ...] | None = None
    memory_retrieved: int | None = None
    memory_written: bool | None = None
    trace_events: tuple[str, ...] | None = None
    policy_modes: tuple[str, ...] | None = None
    proactivity_kind: str | None = None

    def assert_matches(
        self,
        scenario_name: str,
        observation: EvaluationObservation,
    ) -> None:
        for field_name in (
            "decision_kinds",
            "capabilities",
            "tool_successes",
            "approval_decisions",
            "memory_retrieved",
            "memory_written",
            "trace_events",
            "policy_modes",
            "proactivity_kind",
        ):
            expected = getattr(self, field_name)
            if expected is not None:
                actual = getattr(observation, field_name)
                assert actual == expected, (
                    f"{scenario_name}: {field_name} expected "
                    f"{expected!r}, got {actual!r}"
                )


@dataclass(frozen=True)
class EvaluationCase:
    """One named deterministic scenario."""

    name: str
    run: Callable[[], EvaluationObservation]
    expected: EvaluationExpectation


def evaluate_case(case: EvaluationCase) -> EvaluationObservation:
    """Run one case and assert its observable behavior."""

    observation = case.run()
    case.expected.assert_matches(case.name, observation)
    return observation
