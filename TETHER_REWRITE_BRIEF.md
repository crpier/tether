# Tether Rewrite Brief

Working name: **Tether**

Tether is a local-first personal continuity layer. Its job is to connect scattered personal context — memories, saved media, recipes, pantry state, purchase decisions, reminders, imported conversations, and assistant chat history — so an assistant can help the user capture, revisit, decide, and act.

This document maps the current `active-workbench` experiment into a coherent starting point for a rewrite.

## Product Thesis

General assistants lose continuity. Notes and task apps preserve information but rarely help reconnect it to the right moment. Tether should sit between those worlds:

- capture information with low friction
- preserve why it mattered
- connect related artifacts across domains
- resurface open loops at the right time
- guide action when the user is ready to do something

The core promise is not “store everything.” It is:

> Tether keeps the thread between what you saved, why you saved it, and what you should do with it next.

## Core Pillars

### 1. Capture

The system should make it easy to save useful context before it is fully structured.

Current examples:
- memory entries
- bucket/backlog items
- recipes
- pantry items
- purchases under consideration
- scheduled prompts/reminders
- liked/watch-later YouTube content
- imported external conversations
- voice messages in chat

Rewrite principle: capture should accept messy input, then enrich or structure it later.

### 2. Connect

This is the theme to emphasize for Tether. The product should connect pieces of information across time, domains, and workflows.

Current connection points:
- bucket items can store immutable `intent_context` explaining why/where they were saved
- memory entries carry source refs and can be semantically searched
- conversation imports create artifacts linked back to source conversations/messages
- recipes connect to pantry state and guided cooking sessions
- YouTube transcripts connect liked videos to search and spaced recall reviews
- scheduled prompts connect stored context to future assistant action
- chat threads connect current interaction to daily session history

Rewrite principle: every durable artifact should have provenance, relationships, and enough context to be useful later.

### 3. Review

Stored information should not become a passive graveyard. The system should surface stale, ambiguous, due, or actionable items.

Current examples:
- bucket review/triage
- bucket recommendations
- purchase review and buy/wait decisions
- scheduled prompt tasks
- YouTube spaced-recall reviews
- pantry suggestions biased toward expiring food

Rewrite principle: review is a first-class loop, not a report bolted onto storage.

### 4. Guided Action

Tether should help the user execute real workflows, not just retrieve facts.

Current examples:
- guided cooking sessions with generated step plans and timers
- recipe shopping-list generation
- purchase decision support
- bucket next-action recommendations
- scheduled assistant/coding prompts that execute later

Rewrite principle: the assistant should move from context to action with explicit, inspectable steps.

## Current Feature Map

### Assistant Chat

Current behavior:
- Browser chat UI at `/chat` backed by OpenCode assistant sessions.
- Maintains daily chat sessions named `Daily chat YYYY-MM-DD`.
- Supports model selection from OpenCode providers/models.
- Sends messages asynchronously to OpenCode.
- Polls live thread state.
- Displays assistant text, reasoning parts, tool calls, tool status, timing/token/cost metrics, and voice clip attachments.
- Supports browser web-push notifications for assistant replies.
- Supports voice-message recording, upload, transcription, and linking the transcript/audio to the chat turn.

Key current implementation choices:
- Chat storage lives mostly in OpenCode session/message storage, not the app database.
- The backend provides a UI-facing adapter over OpenCode's HTTP API.
- Voice clips and notification records are stored locally in SQLite/files.

Rewrite notes:
- Decide whether chat history should remain an adapter over a third-party/local assistant runtime or become first-class Tether data.
- Preserve the idea of daily continuity, but avoid coupling core product identity to OpenCode.
- Tool-call display and auditability are important because the product acts on personal state.

### Tool API / Assistant Tooling

Current behavior:
- Exposes a catalog at `GET /tools`.
- Exposes one HTTP endpoint per tool under `/tools/{domain.action}`.
- All tools use a consistent envelope:
  - `ok`
  - `request_id`
  - `result`
  - `provenance`
  - `audit_event_id`
  - `undo_token`
  - `error`
- Requests include:
  - `tool`
  - `request_id`
  - optional `idempotency_key`
  - `payload`
  - `context` with timezone/session id
- Write tools are audited and can use idempotency records.
- OpenCode tools are generated/wrapped as `active_workbench_*` TypeScript tools.

Current ready tools:
- YouTube: `youtube.likes.list_recent`, `youtube.likes.search_recent_content`, `youtube.watch_later.list`, `youtube.watch_later.search_content`, `youtube.watch_later.recommend`, `youtube.transcript.get`
- Bucket: `bucket.item.add`, `bucket.item.update`, `bucket.item.complete`, `bucket.item.search`, `bucket.review.run`, `bucket.item.recover_context`, `bucket.item.recommend`
- Purchase: `purchase.item.add`, `purchase.item.update`, `purchase.item.search`, `purchase.item.review`, `purchase.item.decide`
- Recipes/pantry: `recipe.item.add`, `recipe.item.import_url`, `recipe.item.search`, `recipe.item.get`, `pantry.item.add`, `pantry.item.update`, `pantry.item.search`, `recipe.plan.suggest`, `recipe.plan.shopping_list`
- Scheduling: `schedule.task.create`, `schedule.task.list`, `schedule.task.update`, `schedule.task.delete`
- Runtime: `opencode.runtime.inspect`
- Memory: `memory.create`, `memory.list`, `memory.search`, `memory.delete`, `memory.undo`

Rewrite notes:
- Keep a stable tool-envelope concept. It is one of the cleanest architectural ideas in the current project.
- Consider making tools capability-oriented rather than endpoint-oriented internally.
- Keep audit/provenance/idempotency as platform primitives, not per-feature afterthoughts.

### Memory

Current behavior:
- Stores durable memory entries with content JSON, tags, source refs, creation time, deletion time, and canonical markdown files.
- Returns undo tokens for created memories.
- Supports list, lexical search, semantic search, delete, and undo.
- Uses markdown files under a vault-like directory plus SQLite as a ledger.
- Uses LanceDB + FastEmbed for chunked semantic search when available.
- Provides internal memory recall endpoint for assistant context injection.

Important concepts:
- Memory should be inspectable and reversible.
- Memory entries should be short, factual, and source-linked.
- Recall context is selected and formatted before injection into assistant prompts.

Rewrite notes:
- Treat memory as a central Tether primitive.
- Separate durable facts from transient chat summaries.
- Model provenance and confidence explicitly.
- Consider a unified “artifact graph” where memories can connect to conversations, bucket items, recipes, videos, and reminders.

### Bucket / Personal Backlog

Current behavior:
- Stores structured bucket items in SQLite.
- Requires explicit domain on add, e.g. `research`, `movie`, `tv`, `book`, `music`, `game`, `place`, `travel`.
- Supports add/merge, update, complete, search, review, recover context, and recommend.
- Active/completed status model.
- Dedupes by normalized title/canonical id/domain/year.
- Stores metadata JSON for notes, year, duration, rating, popularity, genres, providers, external URL, tags, annotation status, and provider details.
- Supports immutable `intent_context` with `why` and `where_from`; once set, it cannot be rewritten.
- Search includes title, notes, and intent context.
- Review surfaces duplicate groups, under-specified items, ready items, blocked items, oversized items, archive candidates, and next actions.
- Recommendations exclude unannotated items.

Metadata/enrichment:
- Movie/TV resolution via TMDb.
- Book resolution via BookWyrm.
- Music album resolution via MusicBrainz.
- Research URL title resolution.
- Provider quota/soft-limit tracking.
- Add may return clarification candidates instead of writing when match confidence is uncertain.

Rewrite notes:
- Bucket is really “open-loop inventory.” Rename/domain-model accordingly if useful.
- Preserve immutable save-context; it is highly aligned with Tether.
- Avoid making metadata enrichment block low-friction capture. Capture first, resolve later.
- Distinguish item identity, user intent, external metadata, and review state.

### YouTube Likes, Watch Later, Transcripts

Current behavior:
- OAuth-backed YouTube mode only.
- Lists recent liked videos from local cache populated by background sync.
- Searches liked videos by title, description, and cached transcript.
- Accepts watch-later snapshots pushed into the backend.
- Lists/searches/recommends watch-later videos from cache.
- Retrieves transcripts by video id or URL.
- Transcript retrieval can use YouTube captions API or Supadata depending on feature flags.
- Caches likes, watch-later metadata, transcripts, ignored video ids, quota state, and retry state.
- Background jobs sync likes, watch-later metadata, and transcripts.
- Persistent ignored-video list purges and prevents reintroduction.
- Handles provider rate limits/backoffs and tracks daily YouTube quota.

Important current user interpretation:
- The assistant treats “watched,” “saw,” or “recent video” as liked-video queries because liked videos are the current available signal.

Rewrite notes:
- Model external content as connected artifacts: video metadata, transcript, user signal, cache status, review status.
- Keep quota/backoff logic durable; provider APIs are expensive and brittle.
- Separate ingestion from retrieval from learning/review.

### YouTube Learning Reviews

Current behavior:
- Turns educational liked videos with transcripts into spaced-recall items.
- Generates a recall pack with summary, concepts, and questions.
- Schedules three reviews per video at fixed intervals: 2, 5, and 10 days.
- Supports multiple-choice and short-answer prompts.
- UI at `/reviews` lists due reviews, submits answers, snoozes, and skips.
- Stores learning items, review attempts, scores, latency, snooze/skip state.
- Background transcript processing can generate reviews for eligible videos.

Rewrite notes:
- This is a strong “saved material becomes useful again” loop.
- Keep it modular: content ingestion -> eligibility/classification -> recall generation -> schedule -> review UI -> outcomes.
- Consider making review a generic primitive that can support videos, articles, books, recipes, or personal notes later.

### Purchase Planning

Current behavior:
- Dedicated workflow for things the user is considering buying.
- Stores title, category, status, priority, reason, decision factors, target/current price, currency, store, product URL, watch source, price history summary, last price check, last review, bought/dismissed timestamps.
- Supports add, update, search, review, and decide.
- Review identifies stale watches, missing price context, and buy-now opportunities.
- Decide returns buy/wait/unclear style guidance for a tracked item.

Rewrite notes:
- Purchase candidates are a specialized open-loop domain.
- Keep separate from generic bucket items because price/decision state matters.
- Could become an example of a domain module built on common capture/review/decision primitives.

### Recipes, Pantry, and Cooking

Current behavior:
- Stores recipes as human-editable markdown files plus indexed metadata in SQLite.
- Recipe markdown expects title, optional metadata lines, ingredients, and instructions.
- Imports recipes from URLs when structured recipe data is available; weak HTML fallback returns preview rather than auto-saving.
- Searches recipes by title, tags, summary, and ingredients.
- Tracks pantry/fridge items with quantity text, unit, location, expiration, notes.
- Suggests recipes based on pantry coverage, missing ingredients, total time, and soon-expiring items.
- Generates shopping lists by comparing recipe ingredients against pantry.
- Guided cooking UI at `/guided-cooking` starts a cooking session from a saved recipe.
- Cooking sessions generate a more granular plan than raw recipe instructions.
- Cooking steps include estimated seconds, auto-advance hints, timer durations, attention levels, and parallel hints.
- Timers can be started/cancelled; sessions can move next/previous/goto/complete.
- Recipe revisions can be proposed/applied, including revisions linked to cooking sessions.
- Cooking profile entries store durable cooking preferences/notes.

Rewrite notes:
- Cooking is the strongest rich vertical because it uses all pillars: capture, connect, review, guide.
- Preserve markdown as user-owned recipe source, but consider a clearer parser/schema boundary.
- Treat guided cooking sessions as operational state, not just recipe display.
- Connect cooking profile, recipe revisions, pantry history, and meal suggestions explicitly.

### Scheduled Prompt Tasks / Reminders

Current behavior:
- Stores scheduled prompts that run against local OpenCode services.
- Supports `once`, `daily`, and `weekly` schedules.
- Supports `assistant` and `coding` service targets.
- Assistant tasks default to the assistant checkout/directory.
- Coding tasks require explicit directory.
- Scheduler creates OpenCode sessions and sends stored prompts automatically.
- Tools support create/list/update/delete.
- No pause/disable state in current v1.

Important current prompt behavior:
- Reminder wording is rewritten into executable future-assistant instructions, not stored as “remind me to...” text.

Rewrite notes:
- Scheduling should be a core Tether primitive: “bring this context back later and do something with it.”
- Decouple schedule storage from OpenCode as the only executor.
- Model task intent, schedule, target capability, run history, and user-visible outcome separately.

### Conversation Import

Current behavior:
- Imports external conversation exports such as ChatGPT/T3Chat.
- Stores import runs, conversations, and messages.
- Extracts high-confidence memories and bucket items automatically.
- Lower-confidence candidates are returned for review instead of written blindly.
- Maintains an artifact ledger to dedupe repeat imports.
- Keeps provenance back to stored source conversation/message.

Rewrite notes:
- This is directly aligned with Tether’s continuity/connectivity theme.
- Make import a pipeline: parse -> normalize -> candidate extraction -> confidence -> review/write -> provenance links.
- Avoid one-off import logic that cannot generalize to more sources.

### Notifications and Web Push

Current behavior:
- Browser push subscription management.
- VAPID configuration via environment variables.
- Chat notification polling loop checks for completed assistant messages and sends notifications.
- Stores subscriptions and notification message state.

Rewrite notes:
- Notifications should be part of the review/resurface system, not only chat.
- Support notification policies to avoid noise.

### Runtime Inspection / Ops

Current behavior:
- Tool can inspect configured OpenCode runtime artifacts and paths without exposing secrets.
- Logs and telemetry are written locally.
- Scheduler uses a file lock to avoid overlapping scheduler processes.
- Production deployment is documented for a NixOS Hetzner VM.

Rewrite notes:
- Keep operational introspection, but separate it from user-facing product capabilities.
- Local-first state needs clear backup/export/migration support in the rewrite.

## Current High-Level Architecture

### Runtime Shape

Current stack:
- Backend: Python 3.12, FastAPI, Pydantic, SQLite.
- Frontend: React + Vite + TypeScript.
- Assistant runtime: OpenCode HTTP API + OpenCode custom TypeScript tools.
- Search/embeddings: FastEmbed + LanceDB for memory chunks.
- External APIs: YouTube Data API/OAuth, YouTube captions/transcripts, Supadata, TMDb, BookWyrm, MusicBrainz, OpenAI transcription, Web Push.
- Local state root: `.active-workbench/` by default.

Current process model:
1. FastAPI app starts.
2. Dependency graph is built via cached factories.
3. SQLite database schema initializes on startup.
4. Optional background scheduler starts in a daemon thread.
5. Frontend static app is served when built.
6. OpenCode tools call backend tool endpoints.
7. Browser UI calls `/ui/*`, `/guided-cooking/*`, and `/tools/*` endpoints.

### Main Backend Layers

Current layers:

1. **API routes**
   - HTTP request/response models.
   - UI endpoints.
   - tool endpoints.
   - imports.
   - static frontend routes.

2. **Tool dispatcher**
   - Central command router for tool calls.
   - Validates and normalizes payloads.
   - Calls repositories/services.
   - Produces consistent `ToolResponse` envelopes.
   - Handles audit events, write idempotency, provenance, undo tokens.

3. **Services**
   - Cross-repository workflows and external API integrations.
   - Examples: YouTube service, learning service, bucket metadata service, scheduler, OpenCode prompt service, recipe import, cooking plan generation, memory indexing, web push.

4. **Repositories**
   - SQLite persistence per domain.
   - Mostly synchronous repository methods.
   - Dataclasses represent persisted domain records.

5. **Local files**
   - recipe markdown files
   - memory markdown files
   - voice clip audio
   - OAuth/token files
   - logs/telemetry
   - LanceDB memory index

### Request Flow: Assistant Tool Call

Typical tool call path:

1. OpenCode invokes generated TypeScript tool.
2. TypeScript wrapper POSTs to FastAPI `/tools/{tool_name}`.
3. Route wraps payload into `ToolRequest`.
4. `ToolDispatcher.dispatch()` validates request and idempotency.
5. Dispatcher calls domain repository/service.
6. Write calls create audit event and may produce undo token/provenance.
7. Backend returns normalized `ToolResponse`.
8. Assistant summarizes the result to the user.

### Request Flow: Browser Chat

Typical chat path:

1. Browser sends message to `/ui/chat/thread/messages`.
2. Backend ensures today’s OpenCode session exists.
3. Backend sends message to OpenCode, optionally with selected model.
4. Browser polls `/ui/chat/thread/live`.
5. Backend reads OpenCode messages and maps them into UI message shape.
6. Scheduler/notification loop can detect completed messages and send web push.

### Background Scheduler Responsibilities

Current scheduler loop does several jobs:
- run due scheduled prompt tasks
- run bucket annotation poll
- sync YouTube likes
- refresh watch-later metadata
- prefetch/fetch transcripts
- send chat notifications

Important implementation detail:
- A file lock prevents multiple scheduler processes from running concurrently against shared state.

Rewrite notes:
- Split scheduler responsibilities into explicit jobs/queues/workers if the rewrite grows.
- Keep durable throttles/backoffs in the database so restarts and multiple processes do not multiply external API calls.

## Current Data Model Inventory

SQLite tables currently include:

- `audit_events`
- `idempotency_records`
- `memory_entries`
- `memory_undo_tokens`
- `jobs`
- `scheduled_tasks`
- `youtube_quota_daily`
- `youtube_quota_by_tool_daily`
- `youtube_cache_state`
- `youtube_ignored_video_ids`
- `youtube_likes_cache`
- `youtube_transcript_cache`
- `youtube_watch_later_cache`
- `youtube_watch_later_push_history`
- `youtube_transcript_sync_state`
- `youtube_learning_items`
- `youtube_learning_reviews`
- `youtube_learning_attempts`
- `bucket_items`
- `bucket_tmdb_quota_daily`
- `bucket_bookwyrm_quota_daily`
- `bucket_musicbrainz_quota_daily`
- `purchase_items`
- `recipes`
- `recipe_revisions`
- `pantry_items`
- `cooking_sessions`
- `cooking_session_steps`
- `cooking_session_timers`
- `cooking_profile_entries`
- `conversation_import_runs`
- `imported_conversations`
- `imported_conversation_messages`
- `conversation_import_artifacts`
- `chat_voice_clips`
- `web_push_subscriptions`
- `chat_notification_messages`

Rewrite data-model opportunity:
- Introduce shared primitives around `Artifact`, `SourceRef`, `Relationship`, `ReviewState`, `Schedule`, and `ActionRun`.
- Then model domain-specific records as extensions rather than isolated silos.

A possible core graph:

```text
Artifact
  id
  type: memory | video | transcript | bucket_item | recipe | pantry_item | purchase | conversation | message | schedule | review | cooking_session
  title
  body/summary
  created_at / updated_at / archived_at
  provenance[]
  metadata

Relationship
  from_artifact_id
  to_artifact_id
  type: derived_from | mentions | explains | schedules | reviews | uses | replaces | duplicates | recommends | completed_by
  confidence
  created_at

ReviewState
  artifact_id
  status
  due_at
  priority
  reason
  last_reviewed_at
  outcome

ActionRun
  id
  action_type
  target_artifact_id
  status
  started_at / completed_at
  input
  output
  error
```

This would make “connects various pieces of information” part of the system shape rather than just UI copy.

## External Integrations

Current integrations and purpose:

- **OpenCode**: assistant runtime, chat sessions, scheduled prompt execution, custom tools.
- **YouTube OAuth/Data API**: liked-video metadata and video details.
- **YouTube captions / youtube-transcript-api**: native transcript source.
- **Supadata**: alternative transcript provider and fallback.
- **TMDb**: movie/TV metadata and disambiguation.
- **BookWyrm**: book metadata and disambiguation.
- **MusicBrainz**: album metadata and disambiguation.
- **OpenAI transcription**: voice-message transcription.
- **Web Push/VAPID**: browser notifications.
- **FastEmbed/LanceDB**: local semantic memory search.

Rewrite notes:
- Put every integration behind a port/interface with durable rate-limit state.
- External enrichers should return candidates and confidence rather than directly mutating core records.
- Provider-specific payloads should not leak into user-facing workflows except as hidden confirmation identifiers.

## Product Loops Worth Preserving

### Capture -> Connect -> Review -> Act

Example: user saves a research topic.
1. Capture as bucket/research item with why/where context.
2. Connect to source conversation, URL, memory, or related videos.
3. Review surfaces it as under-specified/stale/actionable.
4. Assistant helps pick next action or complete/archive it.

### Save -> Revisit -> Use

Example: user likes an educational YouTube video.
1. Ingest liked video and transcript.
2. Search transcript later by content.
3. Generate learning reviews if educational.
4. Review UI prompts recall so the video becomes retained knowledge.

### Choose -> Prepare -> Execute

Example: user wants dinner.
1. Search recipes and pantry.
2. Suggest best fits and missing ingredients.
3. Start guided cooking.
4. Record session/revision/preferences for future cooking.

### Consider -> Watch -> Decide

Example: user is thinking of buying something.
1. Capture purchase candidate.
2. Track price/context/decision factors.
3. Review stale candidates.
4. Decide buy/wait/unclear based on current context.

## Pain Points In Current Architecture

These are not failures; they are expected artifacts of the experiment.

- Product concepts are scattered across many repositories/services instead of organized around core primitives.
- The tool dispatcher is powerful but very large and centralizes too much domain logic.
- Chat identity and storage are coupled to OpenCode sessions.
- Scheduler owns unrelated responsibilities in one loop.
- Some domains are well-developed verticals while others are thin tool wrappers.
- Metadata enrichment, provider confirmation, and capture are sometimes entangled.
- The frontend has feature pages but not yet a unified Tether information model.
- There is no explicit relationship graph even though the product value depends on connecting things.
- Local-first backup/export/migration is not yet a first-class feature.

## Suggested Rewrite Architecture

### Core Modules

1. **Kernel**
   - IDs, timestamps, settings, logging, errors.
   - Database/unit-of-work.
   - Event/audit log.
   - Idempotency.

2. **Artifact Store**
   - Durable records for things Tether knows about.
   - Provenance/source refs.
   - Relationships between artifacts.
   - Text/indexing hooks.

3. **Memory and Recall**
   - Durable facts/preferences.
   - Semantic and lexical search.
   - Context-pack construction for assistant prompts.

4. **Review Engine**
   - Due items, stale items, open loops, recommendations.
   - Domain-specific review policies plugged into a shared engine.

5. **Action/Schedule Engine**
   - Reminders, recurring prompts, delayed actions, run history.
   - Executor adapters: assistant, coding, notification, future mobile.

6. **Assistant Interface**
   - Tool catalog.
   - Tool invocation envelope.
   - Chat/session abstraction.
   - Prompt/context injection.
   - Model/runtime adapters.

7. **Domain Modules**
   - Backlog/bucket
   - Media/YouTube
   - Learning reviews
   - Recipes/pantry/cooking
   - Purchases
   - Imports

8. **Access Layers**
   - Web UI.
   - OpenCode tools.
   - Future Android app.
   - Future Wear OS/notification surfaces.

### Architectural Direction

Prefer this dependency direction:

```text
Access layers
  -> Assistant/tool API
    -> Application services / use cases
      -> Core primitives + domain modules
        -> Repositories / integration ports
```

Avoid:
- domain logic in HTTP routes
- provider-specific details in core records
- UI pages depending on OpenCode concepts directly
- scheduler logic directly knowing every domain detail

## Rewrite MVP Candidate

A focused rewrite should probably not rebuild everything at once. Suggested first version:

1. Core artifact/provenance/relationship model.
2. Memory create/list/search/delete/undo.
3. Assistant tool envelope and audit/idempotency.
4. Backlog item capture with immutable intent context.
5. Review surface for backlog/open loops.
6. Basic chat adapter with context recall.
7. One rich connection loop: either YouTube transcript search or recipes/pantry suggestions.

Then add:
- scheduled prompts/reminders
- conversation import
- learning reviews
- guided cooking
- purchases
- notifications/mobile

## Naming/Language Notes For Tether

Good product language:
- tether
- thread
- continuity
- context
- provenance
- source
- artifact
- relationship
- open loop
- review
- resurface
- recall
- guided action

Avoid centering language around:
- workbench
- generic productivity
- generic notes
- generic AI assistant
- “second brain” unless intentionally referencing that market

Possible one-line positioning:

> Tether is a private continuity layer that connects your saved context to future action.

Possible system metaphor:

> Artifacts are the things Tether knows. Tethers are the links between them. Reviews are how forgotten context comes back into attention. Actions are how context becomes useful.
