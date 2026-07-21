# The Project vertical is one generic multi-step tracker, steps ordered by a pure blocked_by DAG

Personal projects (fix the bathtub faucet, improve media backups) need multi-step tracking: ordered/dependent steps, lifecycle, links from steps to Scheduled triggers and to Memories, and some form of proactive surfacing in chat. #234 asked for one generic Project vertical serving arbitrary project types, not a vertical per project kind, designed within ADR 0016's bespoke-per-vertical stance (real FK link tables, lifecycle base-set convention, no shared framework).

The answer is a single generic `project` / `project_step` vertical, with dependency order as a pure `blocked_by` DAG (no stored position column), manual lifecycle transitions, fire-unblocks trigger links, project-level memory links, and interim proactive surfacing via a standing chat-turn digest rather than any scheduled or graph-triggered mechanism.

## a. Boundary: projects, todos, and bucket items are three disjoint constructs

A Bucket item means "consume it" (a consumption intention). A todo/waiting-on item (#236, undesigned) means one action. A Project means multiple coordinated steps. Projects are born as projects in chat — there is no seed FK from a bucket item into a project, and todo→project graduation is deferred to #236. Real-world evidence (the user's own workflow) shows projects never enter through the bucket; keeping the boundary sharp also keeps bucket items' permanent-retention dedup contract untouched by project concerns.

## b. Steps are ordered by a pure blocked_by DAG, not a position column

Dependency lives in one link table, `project_step_blocked_by` (real FKs, no `kind` column, following ADR 0016's per-relationship link-table shape), and nowhere else — there is no stored position/order column on `project_step`. Ready and blocked are computed at read time from the DAG, never stored; the stored `project_step.status` stays within the lifecycle base set. Display order is derived: ready steps first, then creation order. The agent wires dependency chains in chat as steps are described.

Personal-project order is dependency in disguise — "step 3 waits on step 1" is what a numbered list is standing in for. A position column can't express a step blocked by two others, or two steps that can proceed in parallel. Keeping both a position column and a dependency table would be two ordering systems that can silently disagree; pure `blocked_by` is chosen instead.

## c. Lifecycle: manual transitions only, terminal blockers unblock dependents

`project` and `project_step` each carry their own typed `status: active | completed | abandoned` enum, per ADR 0016's base-set convention. Transitions are human clicks or explicit chat instructions only — the agent may note that all of a project's steps are terminal, but never auto-completes the project, matching ADR 0015's "state transitions are human clicks, not computed" precedent; finishing the last known step often reveals another one the human hadn't mentioned yet.

Abandoning a project cascades to nothing — steps keep whatever status they had. Queries scope to active projects by default. A step blocked by another step is unblocked once that blocker reaches *either* terminal state, `completed` or `abandoned` — abandoning a path doesn't strand whatever was waiting behind it.

## d. Trigger links fire-unblocks, computed from Notification history

`project_step_trigger_blocker(step_id, trigger_id)` is a plain spine→vertical FK pair, the same shape as `Notification.trigger_id` — explicitly *not* the vertical↔vertical generic edge table that ADR 0016 declined to build, since a project step depending on a scheduled trigger is not a cross-vertical edge in that sense. A blocker is satisfied once the linked ScheduledTrigger has fired successfully at least once after the link was created — computable from existing Notification history, with zero new write paths.

Firing is not the same as doing: triggers have a dispatch lifecycle only, no human acknowledgement that the triggered task was actually carried out. The agent therefore phrases surfacing conditionally — "if Tuesday's cleaning happened, re-caulking is ready" — rather than asserting the step is ready outright. Building honest acknowledgement machinery (did the human actually do the recurring task) is deferred to #236's waiting-on design.

## e. Memory links are project-level only, and don't move or promote anything

`project_memory_link(project_id, memory_id)` records "canonical reference for this project" — e.g. a faucet model number that several steps need. Linking a Memory to a project neither moves it out of the Commons nor promotes its trust class; it stays exactly where ADR 0010's provenance model puts it. Step-level linking is deferred until a real need surfaces; the link table shape makes it additive later without a migration of existing links.

## f. Interim proactive surfacing: a standing digest in the system prompt, relevance-gated

Each chat turn's system prompt carries a compact digest: active projects, ready-step counts, the next ready step's title, and staleness expressed as plain fact (e.g. "last touched N days ago") rather than accrued guilt. For a single user with a handful of active projects this is tens of tokens. Whether the digest surfaces as a mention in a given turn is relevance-gated by prompt wording, not machinery — there's no rate limit or assertiveness knob, deliberately left in the map's proactivity fog rather than designed now. There is no time-scheduled nagging: this follows ADR 0015's event-driven-never-scheduled stance and the no-guilt principle. Detail beyond the digest is available on demand via tools, not pushed.

## g. Surface area

Tables: `project` and `project_step` (title plus free-text notes; project detail lives in chat and linked Memories, not in extra columns), plus the three link tables above (`project_step_blocked_by`, `project_step_trigger_blocker`, `project_memory_link`).

In-chat agent tools, ungated per ADR 0014's gate-follows-authorship principle (these are human-initiated, same as Teaching's tool belt): `create_project` (steps and dependency wiring in one call), `add_step`, `set_step_status`, `set_project_status`, `link_step_trigger`, `link_project_memory`, `list_projects`, `get_project`.

Panel: `apps/web/src/panels/project.tsx`, following ADR 0015's panel pattern — computed ready/blocked badges, click-through status transitions, no dependency or link editing in the panel. Chat is the sole authoring surface for structure; the panel is read-plus-status-click only.

REST routes and host module layout (`apps/host/tether/project*.py`) follow the existing Bucket house pattern.

## Considered options

- **Position column with a fallback to explicit dependencies** — rejected: a hybrid of stored order and a dependency table is two ordering systems that can disagree; pure `blocked_by` with derived display order removes the possibility entirely.
- **Bucket items as the entry point for projects** — rejected: the user's actual workflow shows projects starting as projects, not graduating from consumption intentions; a seed FK from bucket items would couple two constructs that stay cleanly disjoint, for no observed use case.
- **A generic vertical↔vertical edge table for trigger links** — rejected per ADR 0016: nothing here is a genuine cross-vertical edge in the sense that decision reserved; `project_step_trigger_blocker` is a plain spine→vertical FK, the same shape already used by `Notification.trigger_id`.
- **A human acknowledgement step for trigger-satisfied blockers** — deferred, not rejected: fire-unblocks with conditional phrasing ships now; honest "did you actually do it" tracking waits for #236's waiting-on design, which owns that class of problem.
- **Auto-completing a project when all its steps are terminal** — rejected: matches ADR 0015's precedent that state transitions are human acts; finishing a known step often surfaces another one, so computed completion would frequently be wrong.
- **Step-level Memory links from the start** — rejected as premature: project-level links cover the observed cases (a faucet model number serving multiple steps); step-level linking is additive later if a real need appears.

## Consequences

- `project_step` carries no position/order column; every ready/blocked and display-order computation happens at read time from `project_step_blocked_by`, so those queries must be written once, correctly, and reused rather than duplicated per call site.
- Todo→project graduation and honest trigger-acknowledgement both stay open until #236 designs the todo/waiting-on construct; the Project vertical must not grow ad hoc versions of either in the meantime.
- The system-prompt digest adds a small, fixed per-turn token cost for every chat turn, not just ones where a project is relevant; this is accepted as the interim design, with the deferred "proactivity beyond cleanup" work as the eventual reconsideration point.
- `create_project`, `add_step`, `set_step_status`, `set_project_status`, `link_step_trigger`, `link_project_memory`, `list_projects`, and `get_project` all need registering in the tool belt and codegen shims before any project can be created from chat.
- The panel is read-plus-status-click only; any future request for in-panel dependency or link editing is a scope change against this ADR, not a bug.
