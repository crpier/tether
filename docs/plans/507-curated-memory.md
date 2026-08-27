# Curated Memory implementation plan

Tracks [#507](https://github.com/crpier/tether/issues/507). It establishes the Memory substrate required by [#454](https://github.com/crpier/tether/issues/454); Health/Telemetry summary schemas and general cross-source consolidation remain in #454.

Implementation status: the destructive legacy-domain cutover, Dreaming-only file authority, Conversation assimilation, Health assimilation, direct Topic retrieval/context, and read-only Topic UI are implemented. Suppression/erasure and maintenance remain later slices. Section 3's planned pi filesystem tools and Bubblewrap shell were never shipped and are superseded by ADR 0033; current Dreaming returns curated text for validated host writes.

## Outcome

Tether automatically turns settled host-owned evidence into a small, current, agent-organized Markdown workspace. Those files are canonical Memory, and Dreaming is their sole writer. SQLite owns evidence, suppressions, orchestration, full-file history, and recorded file state used to authorize or repair mutations; pi sessions are execution/audit records only. Foreground chat receives a fresh bounded projection of relevant topic files on every model call.

There is no legacy migration, loose/tethered compatibility, Review inbox, direct Memory editing, runtime shadow mode, first-class Claim table, or run-level atomic publish. Production is reset to an empty Memory workspace when this cutover deploys.

## File contract

Canonical root: `/data/kb/memory` in production and `<kb_root>/memory` elsewhere.

Only regular `.md` files and directories are durable Memory content. Dotfiles, editor artifacts, and non-Markdown files are ignored by reconciliation and retrieval. Symlinks are forbidden. The agent chooses nested directories and meaningful filenames.

A file normally represents one Topic and gathers related Claims. One standalone Claim may occupy a file; the prompt may permit other useful arrangements. Validators enforce format, not topic design.

```markdown
---
title: Travel preferences
evidence:
  - tether://message/019...
context: always
review_after: 2027-03-01
---

Prefer aisle seats on long flights. [source](tether://message/019...)
```

Validation:

- leading YAML frontmatter is required and parsed with a safe loader;
- the document must be UTF-8 and frontmatter must be a mapping;
- duplicate keys and custom YAML tags are rejected;
- `title` is required and must be a non-empty string;
- unknown fields are preserved;
- recognized optional fields are best-effort hints: malformed `evidence`, `context`, or `review_after` values are ignored and surfaced diagnostically rather than blocking a mutation.

Canonical evidence locators use source-specific host-resolvable URIs, initially `tether://message/<message-id>`. Later adapters add source kinds without changing Markdown semantics. File-level `evidence` aids inspection and erasure; inline Markdown links provide optional Claim precision. The host does not parse Claims into rows.

## Host state

Finalize typed snekql models and migrations test-first. The expected responsibilities are:

- **File mutation** — idempotency key `(run_id, tool_call_id)`, Dreaming actor, operation and arguments, timestamps, and acknowledgement state.
- **File version** — mutation, path, complete content or tombstone, content hash, and timestamp. Store complete contents without deltas/deduplication initially.
- **Dream run** — kind (`assimilation`, `maintenance`, `manual`), immutable evidence bounds, model/prompt version, trigger, status, pending-retry state, timing/cost, report, and failure.
- **Conversation cursor** — highest successfully assimilated host Message sequence per Tether Conversation.
- **Maintenance progress** — path plus current content hash/version and last successful maintenance time.
- **Suppression** — exact evidence URIs, selected text as semantic exclusion exemplar, optional user clarification, and active/revoked state.
- **Context inclusion** — foreground run, selected path and exact content hash/version, selection reason, and token contribution.
- **Purge request** — content-free target identities, live-purge state, remote-backup state, attempts, and terminal status.

Store the latest recorded state for each path alongside complete history. Reconciliation accepts exact pending Dreaming mutations, repairs other edits and deletions from the latest recorded state, and removes unknown valid topic files.

## Delivery slices

Every code slice follows red-green-refactor with snektest for Python and the existing TypeScript test runners. Keep each slice gate-clean before proceeding.

### 1. Replace the legacy Memory domain

Write failing store/service tests, then:

- add the new orchestration/history/suppression models;
- remove `Memory`, loose/tethered state, facets, Review services/routes/tools, Memory Candidate behavior, and old projection writes;
- remove or adapt APIs, panels, Todo/Project links, Search callers, and ingestion paths that assume `memory_id`;
- keep Recall as a learning workflow independent of Memory trust, removing its tethering dependency;
- make direct captures and imports canonical evidence that later feeds Dreaming rather than Memory rows;
- make the deployment cutover explicitly destructive for legacy Memory state instead of implementing backfill or dual reads.

Acceptance:

- a fresh database has no legacy Memory/Review lifecycle;
- all surviving source and vertical models have explicit ownership;
- no code path can create a loose/tethered Memory.

### 2. Canonical workspace and reconciliation

Write filesystem/service tests for valid files, malformed frontmatter, unauthorized additions/edits/deletions, moves, symlinks, editor artifacts, and crash gaps. Then implement:

- canonical root creation and safe path resolution;
- frontmatter parser and recognized metadata reader;
- startup reconciliation plus lightweight filesystem-change reconciliation;
- complete file versions and tombstones;
- exact recognition of recorded but unacknowledged Dreaming mutations;
- repair of unauthorized edits and deletions from recorded state;
- removal of unknown valid topics and diagnostic exclusion of malformed files;
- mutation-grouped before/after diff generation.

Reconcile the complete workspace before each dream run so move/delete preimages are recorded. Ordinary history is retained indefinitely. Whole-run rollback and storage compaction are deferred.

Acceptance:

- rebuilding derived index state from recorded files loses no current Memory;
- retrying a recorded mutation notification returns the original acknowledgement;
- external file changes cannot alter current Memory;
- malformed files remain excluded from model context.

### 3. Dreaming pi harness and mutation tools

Start with agent-package tests around tool overrides and host tests around acknowledgement. Implement a dedicated ephemeral dreaming runtime that disables context-file loading and exposes only:

- confined native `read`;
- wrapped native `write` and `edit`;
- native-shaped `move({source, destination})` and `delete({path})`;
- native-interface `bash` executed through Bubblewrap.

Mutation protocol:

1. serialize mutating tool calls with one workspace lock;
2. confine paths, reject symlinks, compute prospective content, and validate;
3. execute the native mutation exactly once;
4. notify host with run/tool identity, operation, paths, and resulting hashes;
5. host rereads surviving files and transactionally records versions/tombstones plus projection invalidation;
6. retry notification—but never mutation—after uncertain transport;
7. return the native-shaped result only after ACK.

Bubblewrap exposes the Memory root read-only, required binaries/libraries read-only, and writable ephemeral `/tmp`; it hides home, databases, host sockets, secrets, and unrelated paths, disables networking, sanitizes environment, and enforces tight timeout/output/process limits. Bash needs no mutation ACK.

Acceptance:

- no tool can mutate or read outside its intended boundary;
- shell redirection/interpreters cannot alter Memory;
- mutation success followed by lost ACK converges through idempotent notification;
- process death between mutation and recording converges on startup;
- failed runs retain acknowledged file changes.

### 4. Conversation assimilation orchestration

Build fake-clock/fake-dream-runner tests first. Then add:

- a configurable twenty-minute settling period based on the last **user Message in a Tether Conversation**, independent of pi session rotation;
- a requirement that its corresponding foreground turn has completed;
- immutable inclusive Message bounds and bounded ordered slicing;
- one cursor per Tether Conversation, advanced only by success or valid no-op;
- one global dream run, foreground priority, and one collapsed pending follow-up;
- retry of the same evidence bounds against current files after failure;
- `queue_memory_assimilation` for explicit remember requests and corrections, launching after the foreground turn and bypassing only the idle wait;
- operational enable/pause and a real `Dream now` scheduler action; no shadow cursor/mode.

The dynamic prompt includes run kind/ID, bounds, complete bounded evidence slice, canonical URI/timestamp/source/role/authority per item, relevant suppressions/corrections, user timezone, and work budget. Assistant/reasoning/tool rows may be included as explicitly context-only trajectory; only user Messages and appropriately scoped verified sources support user Claims. The static prompt defines authority, file conventions, desired organization, and the rule that existing Memory prose is patchable state rather than evidence.

Acceptance scenarios include messages every fifteen minutes across rotating five-minute pi sessions, a newly arriving Message during a run, overlapping triggers, valid no-op, partial mutation then failure, and explicit immediate assimilation.

### 5. Maintenance

Test deterministic selection before model behavior. Run one bounded batch daily while foreground is idle and after pending assimilation in `Dream now`.

Selection order:

1. suppression/deletion/correction targets;
2. files whose recognized `review_after` is due;
3. current file versions never maintained;
4. least-recently-maintained current versions.

Supply current time/timezone, bounded starting paths, resolvable evidence cited by those files, and relevant policy records. The selected paths are entry points, not mutation boundaries; pi may inspect/reorganize the whole workspace. Successful no-op marks only the selected current versions maintained; changed content becomes eligible again.

Acceptance covers temporary plans, contradiction, topic merge/split, missing evidence, invalid optional metadata, and no guilt/backlog behavior after inactivity.

### 6. File-derived retrieval and foreground context

Write reconciler, ranking, budget, and pi-context-hook tests first. Replace Memory-row hydration with current-file hydration:

- derive lexical/vector candidates from valid Markdown and rebuild all index state from files;
- treat LanceDB only as a candidate generator and revalidate path/hash against current files;
- select recognized `context: always` topics within a strict cap, then prompt-relevant diverse topics;
- inject complete selected files through pi's non-destructive `context` hook before each model call;
- record path, exact version/hash, selection reason, and token use in the foreground trace;
- retain a host memory-search fallback without foreground mutation access.

Never persist injected Memory in pi JSONL or the stable system-prompt prefix. Current files must affect the next action even while index reconciliation is pending; use a small-corpus direct fallback rather than stale content.

### 7. Suppression and erasure

Drive this slice with end-to-end timeline tests.

**Don't remember this**:

- retain canonical source Messages;
- persist exact source URIs plus selected text/clarification as a Suppression;
- delete whole affected Memory files located by frontmatter evidence references;
- enqueue targeted maintenance;
- exclude suppressed evidence from future dream support and foreground context;
- require explicit suppression removal before restoring affected history.

**Delete this everywhere**:

- immediately purge selected source records, affected current files, matching historical file contents, derived indexes, traces that retain content, and pi session copies;
- retain only content-free identifiers/status needed to finish idempotently;
- mark the operation pending until the privileged backup worker completes;
- have a host-side deployment worker take the backup lock, forget and prune every Restic snapshot, create a clean snapshot, then acknowledge completion;
- keep live content deleted and retry remote work after failures;
- verify/document that the dedicated B2 bucket has no object lock or retention policy.

Do not claim completion in UI while old remote snapshots remain recoverable.

### 8. Memory and Dream UI

Implement component tests first and exercise every changed flow in headed Playwright while iterating.

Replace Review/Inbox Memory UX with:

- agent-created directory tree and rendered topic view;
- recognized metadata and resolvable evidence links;
- file history and readable grouped diffs;
- Dream run list/detail with queued/running/no-op/changed/partial-failure/success states;
- prominent diagnostics for invalid external files;
- Don't remember this and Delete this everywhere with accurate pending-erasure state;
- polished global Dream now action that queues behind foreground work, collapses repeated requests, and progressively exposes bounds/model/tools/timing/logs.

Do not build an editor. Neovim and Obsidian may inspect canonical files read-only; corrections go through conversation as Evidence.

### 9. Operations and cutover

- update backup staging to include the recursive canonical `memory/` tree and retained pi audit sessions while excluding LanceDB/editor artifacts;
- implement one shared backup/purge lock and the privileged purge worker;
- update restore reconciliation and purge-safety runbooks;
- reset the empty production Memory/database state during the cutover; do not write a data migration;
- update `architecture.md`, API docs, prompts, and remove superseded terminology/code after the new path is complete;
- keep #454's Health proving slice blocked from inferred Memory writes until this substrate lands.

## Evaluation fixtures

Use temporary canonical workspaces and normalized evidence timelines, never a runtime shadow mode. Avoid golden Markdown. Assert structural/safety outcomes and inspect usefulness through diffs:

- durable preference carried forward;
- explicit correction supersedes old assertion;
- verified source scoped correctly;
- repeated implicit pattern represented cautiously;
- assistant hallucination never promoted;
- transient plan reviewed/retired after time passes;
- related Claims consolidate while a lone Claim may remain alone;
- duplicate/retry/no-op converges acceptably;
- suppression prevents recreation;
- delete-everywhere leaves no recoverable test copy;
- context stays relevant and within budget.

Offline model comparisons may run two isolated temporary workspaces over identical evidence and blind-grade their diffs. Evaluator judgments can inform model/prompt selection but never authorize erasure or override deterministic invariants.

## Completion gate

Run the repository's full validation gate from `AGENTS.md`, including host pyright/ruff/snektest, agent and web type/lint/format/tests, codegen drift, and live Playwright smoke. For web work, also verify the affected flows interactively in headed Playwright with a clean console. Dogfood real `Dream now` runs and inspect file/history/context traces before enabling the production scheduler.
