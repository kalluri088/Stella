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
16. Outline parity is API-first: the Outline HTTP API is the single capability
    surface, and Stella's `outline_tools.py` and the Outline web UI are both
    thin clients of it. A new Outline capability is not done until Stella can
    drive it too — except the documented non-parities recorded in that
    module's docstring (bulk export, and the graph *layout*; graph
    *connections* are full parity via link tools and `kind=graph` search).
    Keep exactly three tools — grow capabilities inside `kind`/`action`
    values, never add verbs. Recurrence/tag token validators are mirrored
    from the Outline server; change both sides together.

## How to work here

When implementing a change: inspect the repository before editing; identify
the smallest relevant files; understand existing tests and interfaces; make
the smallest coherent change; preserve backwards compatibility where
practical; add or modify focused tests; update docs for meaningful changes.
Avoid giant all-context prompts — one concrete task with constraints and a
validation list per change.

**Laptop discipline (owner rule, stated repeatedly).** This machine is the
owner's daily driver, not a test rig.

* The tree is the development copy; the owner's day-to-day Stella is the
  installed release. Run the tree through `bin/stella-dev` (or
  `bin/stella-dev ui`), which redirects `XDG_DATA_HOME`, `XDG_CONFIG_HOME`
  and `STELLA_VOICE_SOCKET` into `.dev/` — without it, a tree run shares the
  release's databases, persona files and voice socket. Only point the tree at
  real state deliberately, when the owner asks for that.
* Stella GUI instances and Stella test windows open on **Hyprland
  workspace 6 only** — never in the owner's active workspace.
* Run pytest in batches of **one or two files**, each under `timeout`,
  bracketed by `free -m`. Never run the whole suite or parallel jobs in one
  command, even when it "looks cheap".
* Before launching anything that loads models (voice, OCR, embeddings),
  check `free -m` first and stop instead of adding load when it is tight.

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
