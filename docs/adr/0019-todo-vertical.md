# The Todo vertical is one bespoke single-action construct with computed waiting and a standing digest

One-off actionable items — "bring the book next time I visit Ana", "dig out the grey shirt before the gala", "research the pension transfer" — had no home in Tether. The interim mechanism, an `action: pending` facet the Gmail gate wrote on actionable emails, had no lifecycle, no waiting semantics, no proactive surfacing, and covered only email-sourced items; the user's Google Tasks "Inbox" tab (one-off tasks and things to research) had no tether equivalent. #236 asked for the todo/waiting-on construct, inheriting the boundary decisions #234 / ADR 0017 made (three disjoint constructs; no seed/graduation from Bucket) and resolving the honest-acknowledgement question ADR 0017 §d deferred to it.

The answer is a bespoke **Todo** vertical following ADR 0016's stance: its own `todo` table, its own `todo_memories` link table, base-set statuses as convention, an optional two-form waiting condition, **computed** readiness, chat-authoring tools, a read-and-transition panel, and the first instance of the standing system-prompt digest ADR 0017 introduced. The Gmail gate creates Todo rows instead of `action: pending` facets; the legacy facets are migrated once.

## a. Boundary: a Todo is exactly one action, disjoint from Bucket items and Projects

A Bucket item means "consume it"; a Project means "multiple coordinated steps"; a Todo means "one action, no steps". The three stay disjoint (ADR 0017 §a): a Todo is not a degenerate Project and not an actionable Bucket item. Graduation from Todo to Project is behavioral only (§f), never a schema seed. Keeping the boundary sharp keeps each construct's contract clean — Bucket's permanent-retention dedup, Project's step DAG, Todo's computed waiting — with no shared framework leaking between them.

## b. Storage: a bespoke table, base-set status as convention

`todo` carries `id`, `action` (the single action, in the user's terms), `status` (`active` / `completed` / `abandoned`, the ADR 0016 base set as a plain string column, not a schema-enforced enum), a nullable free-text `condition`, a nullable `trigger_id`, a `version` for optimistic concurrency, and timestamps. Memory links live in a bespoke `todo_memories` table (real columns, no generic edge table, per ADR 0016). The `trigger_id` is a plain nullable spine→vertical reference, not a DB-enforced foreign key — the same shape as `Notification.trigger_id`, and one trigger per Todo (a Todo is a single action with at most one deadline).

## c. Waiting is computed, never stored

A Todo is *waiting* while it has an unmet text condition **or** an unfired linked trigger, and *ready* otherwise; a Todo with neither is ready now. Nothing stores "waiting" — so a Todo can never get wedged in a stale waiting state (the failure mode a stored status invites). A free-text condition has no machine-checkable "met" signal, so it keeps a Todo waiting until the Todo is settled — surfaced for relevance-gated mention rather than mechanical resolution. A linked trigger is satisfied once the notification history carries a row for its id, exactly the fire-computed-from-`Notification` precedent ADR 0017 §d cites, with **zero new write paths**. This is the honest-acknowledgement answer ADR 0017 deferred to #236: firing a deadline trigger readies the Todo (the mechanical half), while the text condition carries the human-judgement half that no trigger can assert.

## d. Digest: the first standing system-prompt digest

Readiness feeds a pure composition seam — a `TodoReadiness` in, a system-prompt block out (`render_todo_digest`) — appended to the conversation persona at spawn (`compose_conversation_prompt`). Ready Todos are listed (capped, newest first) under an instruction to surface them proactively; waiting Todos are listed with their condition and/or deadline under an instruction to raise one **only when the conversation makes it relevant** (the user mentions the person, place, or event it waits on), never as an unprompted list. This is ADR 0017 §f's digest idiom, now shipped for the first time. The persona stays a constant prefix so provider prompt caches stay warm; the digest, which changes only as todos are added or settled, is appended after it. An empty digest yields the bare persona, so a user with no todos carries no extra prompt weight. The seam is pure and tested at the block boundary (todos in → text out), not by inspecting live prompts.

## e. Gate integration: the gate writes Todos, un-gated

`_capture_memory` stops writing the `action: pending` facet. When a verdict is actionable, the gate creates a Todo (its action the email subject) linked back to the captured Memory and, when a deadline once-trigger was created, to that trigger. The Todo is written directly (un-gated), at parity with the gate's shipped Memory/trigger behavior (ADR 0014's gate-follows-authorship: the gate already writes tethered Memories and triggers un-gated). Proposal-gating of gate-created Todos is an earned-autonomy question, deferred. The ordering invariant is preserved: the Memory and its idempotency row are recorded before the trigger or Todo is attempted, so a failure past that point never re-captures a second Memory on retry — the trigger/Todo simply do not get a second attempt, and the Memory is never duplicated.

## f. Graduation is behavioral, no schema

When a Todo turns out to be multi-step, the agent offers to turn it into a Project (ADR 0017), carrying its condition/context into the new Project and setting the Todo `abandoned` with a back-reference Memory. This is prompt-guided behavior only — there is no `graduated_to` column and no seed FK. Graduation assistance without graduation schema keeps the Todo↔Project boundary (§a) from leaking a coupling that the observed workflow never needs mechanized.

## g. Migration: one-time, idempotent

A boot backfill lifts every Memory carrying `facets.action == "pending"` (the legacy convention) into a Todo — its action the Memory's first line — links the Todo back to the source Memory, and strips the `action` key. Stripping the key is what makes a rerun a no-op; the per-Memory link is also de-duped, so a partial run never double-creates. Running it every boot is therefore safe and cheap for a single-tenant corpus.

## h. Surface area

Tables: `todo` and the `todo_memories` link table.

In-chat agent tools, ungated per ADR 0014 (human-initiated): `create_todo` (with optional free-text condition), `set_todo_status`, `link_todo_trigger`, `link_todo_memory`, `list_todos` (the ready/waiting split). Chat is the sole authoring surface for structure.

Panel: `apps/web/src/panels/todos.tsx`, following ADR 0015's panel pattern — ready/waiting badges and click-through status transitions (complete / abandon) only, no authoring. REST routes (`GET /api/todos` for the split, `POST /api/todos/{id}/status`) and host module layout (`apps/host/tether/todo*.py`) follow the existing Bucket house pattern.

## Considered options

- **A stored `waiting` status** — rejected: it is the exact stale-state failure computed waiting exists to prevent; readiness is cheap to derive from the condition column plus notification history, with no new write path.
- **Reusing the `action: pending` facet convention** — rejected: a facet has no lifecycle, no waiting semantics, no proactive surfacing, and only covers email; the whole point of the vertical is to give the one-off actionable a real home. The facets are migrated once and the convention retired.
- **A `graduated_to` column / seed FK into Project** — rejected: graduation is assisted in chat but schema-free (§f); the observed workflow never needs the coupling mechanized, and a column would blur the disjoint-constructs boundary.
- **Proposal-gating gate-created Todos** — deferred, not rejected: the gate writes Todos un-gated at parity with its shipped Memory/trigger writes (ADR 0014); earned-autonomy gating is a later question that owns that class of problem.
- **A generic vertical↔vertical edge table for Todo↔trigger / Todo↔Memory** — rejected per ADR 0016: neither is a genuine cross-vertical edge; the trigger link is a plain spine→vertical reference (like `Notification.trigger_id`) and Memory links live in a bespoke `todo_memories` table.
- **Multiple triggers per Todo** — rejected as premature: a Todo is a single action with at most one deadline; a single nullable `trigger_id` column matches the construct, and a link table is additive later if a real need appears.

## Consequences

- Waiting is a read-time computation over the condition column plus `Notification` history; that query must be written once, correctly, and reused rather than duplicated per call site (the readiness seam owns it).
- The standing digest ships the ADR 0017 §f idiom for the first time; it adds a small, bounded per-turn token cost only when the user has todos, and its proactivity beyond "surface ready, gate waiting on relevance" stays in the map's fog, deliberately not designed now.
- Todo→Project graduation and the earned-autonomy gating of gate-created Todos both stay open; the vertical must not grow ad hoc versions of either in the meantime.
- The Gmail gate now depends on the Todo service; the `action: pending` facet convention is retired after the one-time migration, leaving a single convention (the Todo row) for actionable email.
