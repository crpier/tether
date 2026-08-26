# Engineering principles

These conventions apply to Tether's retained domain host. Open WebUI owns the
assistant interface and runtime; its internal behavior is outside this guide.

## Operations are strict about existence and convergent about state

Every state mutation asks two separate questions: does the target exist, and
what state is it in?

- Existence is strict. A missing live entity is a caller error. Raise the
  domain-specific not-found error instead of silently succeeding.
- State is convergent. For a live entity, drive it to the requested end state.
  Repeating the same request is a no-op rather than an error.

This gives retries and duplicate tool calls safe idempotency without hiding bad
identifiers. Completing an already completed Todo is harmless. Completing a
Todo that does not exist is not.

Do not apply convergence when a write replaces distinct prior state that the
caller used to make its decision. Such writes need a precondition, usually an
expected version. Reject the write if the row changed after the caller read it.
This matters even in a single-user system because two Open WebUI conversations
or an ingestion worker can act on the same record.

Use this test: if losing the prior value would cost real work to notice and
recover, require a precondition instead of using last-write-wins.

## Bound every tool

Open WebUI receives a small allowlist of typed operations, not general access to
Tether internals.

- Validate every request with the operation's Pydantic model.
- Keep list and search results bounded.
- Return declared domain failures in the tool envelope.
- Do not add a generic database query, filesystem, shell, or code-execution
  tool.
- Log operation, duration, and outcome without request bodies, prompts, health
  values, or credentials.

The model may choose the wrong tool or malformed arguments. Validation and
domain invariants must prevent that mistake from corrupting state.

## Keep credentials separate

`TETHER_OPEN_WEBUI_TOKEN` authenticates Open WebUI tool traffic inside the
Compose network. `TETHER_API_TOKEN` authenticates Android Health Connect at the
public host origin. Generate them independently and never substitute one for
the other.

Provider API keys belong to Open WebUI configuration or supported environment
settings. They do not belong in Tether source, tool results, or browser-visible
host responses.

## Prefer deterministic integrations

Tether keeps typed domain state and deterministic ingestion. It does not run a
second agent loop, model-backed scheduler, transcript mirror, or assistant
memory writer behind Open WebUI. If a feature belongs to generic chat, model
execution, voice, files, memory, or web search, use stock Open WebUI rather than
recreating it in the host.

## No streak mechanics or guilt accrual

Tether does not track consecutive-day streaks or turn absence into debt. A user
returning after a year should not see a broken-streak warning or a backlog score
whose purpose is to punish time away.

Retained ingestion and triage may use elapsed time for correctness or ordering.
They must not frame absence as failure. Tether is a single-user tool with no
engagement metric to optimize.
