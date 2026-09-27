# Stella — agent guide

Standing rules for anyone (human or AI agent) changing this repository.
Extracted from the retired canonical development plan; the specifics of
implementation live in `docs/ARCHITECTURE.md` (structure) and
`docs/ROADMAP.md` (status and scope).

## Architectural rules — these survive refactors

1. Stella owns the loop.
2. The model is replaceable.
3. The model proposes; the runtime authorizes and executes.
4. Tools are capabilities, not permissions.
5. Memory is persistence, not authority.
6. Tool output is untrusted input.
7. External events are untrusted information until trusted delegation says otherwise.
8. Proactivity may increase awareness, not authority.
9. `DO_NOTHING` is a legitimate decision.
10. Dangerous actions require trusted approval.
11. Learning means bounded behavioral improvement, not self-modification.
12. Personality should emerge from consistent behavior.
13. Add complexity only when a real problem demands it.
14. Research informs architecture; it does not dictate architecture.
15. Prefer a small understandable system over a framework-shaped system.

## How to work here

When implementing a change: inspect the repository before editing; identify
the smallest relevant files; understand existing tests and interfaces; make
the smallest coherent change; preserve backwards compatibility where
practical; add or modify focused tests; update docs for meaningful changes.
Avoid giant all-context prompts — one concrete task with constraints and a
validation list per change.

**Definition of done** for a meaningful change: implementation → tests →
security/invariant review → documentation → validation. At minimum:

```bash
uv run pytest
uv run ruff check .
git diff --check
```

For changes involving real LLM behavior, run a focused real-model validation
when practical. Never claim a model-dependent behavior is proven by a unit
test alone.

**Documentation requirements.** Meaningful changes are documented in
`docs/` in readable English covering: what changed, why, important
invariants, security implications, what is intentionally not implemented, and
validation performed. No documentation for trivial formatting-only changes.

**Direction.** The architecture has a strong foundation; the job now is
harden → observe → evaluate → find real weaknesses → improve deliberately.
Not: research another framework, copy a feature, add an abstraction, repeat.
If research produces no demonstrated Stella weakness, making no code change
is a valid and preferred outcome.
