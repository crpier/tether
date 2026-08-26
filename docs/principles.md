# Engineering principles

Cross-cutting conventions for *how Tether's code behaves*, distinct from the ADRs (which record specific hard-to-reverse decisions) and CONTEXT.md (which fixes domain language). When a new operation is written, it should follow these unless there is a recorded reason not to.

## Operations are strict about existence, convergent about state

Every state-mutating operation faces two separate questions about the entity it targets: **does it exist**, and **what state is it in**. We answer them differently, and on purpose.

- **Existence is strict.** Targeting an id that does not refer to a live entity is a referential bug in the *caller*, not a state to absorb. Raise (e.g. `MemoryNotFoundError`). Never silently succeed on a missing target — best-effort about existence hides the bug instead of surfacing it.
- **State is convergent.** Given a live row, the operation drives toward its declared end-state regardless of the starting state, and re-asserting the end-state is a **no-op, not an error**. `tether` → tethered and `delete` → deleted are idempotent on a present row by construction.

This buys idempotency exactly where it is free and wanted: retried or duplicated requests, double-clicks, and at-least-once delivery from the scheduler all *converge* instead of erroring.

Two tempting extremes are both rejected:

- *Blanket strictness* — "re-tethering an already-tethered Memory is a conflict, raise" — turns benign retries into errors and forces every caller to pre-check state it shouldn't have to.
- *Blanket leniency* — "operating on a missing id quietly succeeds" — is the best-effort trap: it makes referential bugs invisible.

So: **lenient about redundant operations, strict about nonsensical ones.**

### Caveat: operations that overwrite distinct prior state

Convergence is safe *only* when "already in the end-state" carries no information the caller must reconcile. It breaks for an operation whose new value **depends on, and overwrites, a distinct prior value the caller was reasoning about**. There, "just converge on what I'm setting" silently discards a decision someone else made.

`edit_content` is the canonical case. An edit is formulated against the content the author last saw — frequently an agent proposing a change to *specific* text. Under last-write-wins, a concurrent edit (a second conversation the same human is holding, or a background agent) silently overwrites that basis; the displaced content is technically recoverable from history but, in practice, very hard to locate and reconstruct. Such an operation is therefore **not** convergent: the caller must prove it is editing the state it believes it is.

The mechanism is **optimistic concurrency control** — the caller supplies the version it read, and the write is rejected as a conflict if the row has moved on since. This is *not* a multi-user concern (Tether is single-user; see architecture.md "Security"): one human running two conversations can unwittingly target the same Memory. Nor does it contradict ADR 0001's "a human edit *is* the review" — it decides *which* review wins rather than letting a race decide silently.

Rule of thumb: if losing the prior value would cost the user real work to notice and recover, the operation is state-destructive and needs a precondition, not convergence.

## No streak mechanics, no guilt accrual

Nothing in Tether tracks consecutive-day streaks, and no state accrues a sense of falling behind during an absence — a user coming back after a week or a year sees the same thing they would after a day. This holds everywhere the temptation to add it recurs: Recall scheduling, Curriculum/Lesson progress, Triage, Bucket item staleness, proactive surfacing.

This is a deliberate rejection of the common growth-hacking pattern where absence is framed as a broken streak or a debt to repay. A single-user tool with no engagement metrics to optimize has no reason to manufacture pressure, and a personal operating system that guilts its one user for living their life works against the reason it exists. Where an operation needs to *notice* time has passed (e.g. Recall's adaptive scheduling, Triage's staleness detection), it may use elapsed time to inform its own logic, but never to display a counter, a "your streak is broken" message, or any framing that makes the absence itself the subject.
