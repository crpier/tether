# TanStack AI chat-runtime spike

Issue: [#674](https://github.com/crpier/tether/issues/674)

## Recommendation

Reject adoption for Tether's chat runtime at the versions tested.

TanStack AI now handles server hydration, stream replay, run rejoin, queueing, interrupts, and persistent WebSocket or SSE connections. The prior review's capability gap has narrowed substantially. The remaining problem is ownership. TanStack's state model assumes that the client owns unsent queue entries and its own run and live Message identities. Tether durably accepts every prompt into a host FIFO, assigns canonical turn and Message identities, and keeps spoken settlement and attachments tied to those host records.

The browser translation path retained Tether's runtime and added another reducer. The host AG-UI path was cleaner, but it required a protocol translator, a replay log, run-identity mapping, canonical transcript reconciliation, and Tether-specific queue and stop behavior. That relocates the difficult invariants instead of deleting them.

## Versions tested

Pinned exactly on 2026-08-28:

| Package | Version |
| --- | ---: |
| `@tanstack/ai` | `0.52.0` |
| `@tanstack/ai-client` | `0.29.2` |
| `@tanstack/ai-solid` | `0.19.4` |
| React parity reference, not installed | `@tanstack/ai-react@0.22.4` |

`@tanstack/ai@0.52.0` depends on `@ag-ui/core@0.1.1-canary.beta.0`. All packages remain pre-1.0 and were publishing several releases per week during this spike.

## What was built

The throwaway branch contains two paths:

1. `apps/web/src/prototypes/tanstack-ai-674/tanstack-live-chat-turn.ts`
   translates existing Tether WebSocket frames into AG-UI chunks and satisfies
   the existing `createLiveChatTurn` caller contract.
2. `apps/host/tether/tanstack_ai_prototype.py` exposes server hydration,
   resumable SSE, and AG-UI events while retaining `ConversationTurns`, SQLite,
   and Pi. `apps/web/src/prototypes/tanstack-ai-674/native-page.tsx` consumes it
   with `@tanstack/ai-solid`.

Neither path is intended for `main` or deployment.

A real local Tether Conversation completed through the native path:

- Conversation: `01a04a1e-81f5-722c-b10f-2700df0fab16`
- live user and assistant output streamed through TanStack;
- final rows reconciled to canonical Message ids;
- browser console and HTTP checks were clean.

A second Conversation, `01a04a1f-97d3-7128-84e3-5ec8a1e97aed`, refreshed while its turn was pending or running. The new client hydrated the Conversation, joined `run-1787949979714-cwt2ya`, replayed the event log, settled, and reconciled canonical ids without duplicate user rows.

## Ownership diagrams

### Current Tether path

```text
ChatPage
  -> createLiveChatTurn
     -> host REST history                canonical Messages and turns
     -> ChatBus /ws                      live frames, invalidation, notification
        -> ConversationTurns FIFO        canonical queue and cancellation
           -> Pi runtime
           -> SQLite Messages and turns  transcript authority
```

One host model owns transcript, turn identity, queueing, cancellation, and settlement. The browser has provisional presentation state only.

### Browser translation prototype

```text
ChatPage contract
  -> TanStack compatibility wrapper
     -> TetherFrameConnection            Tether frames -> AG-UI chunks
     -> TanStack ChatClient               second transcript/run/queue state
     -> manual Tether queue bridge        durable pending turns
     -> manual canonical reconciliation
     -> manual spoken settlement
     -> existing ChatBus /ws
        -> unchanged host runtime
```

The adapter retained `ChatBus`, durable queue handling, history loading, canonical id replacement, spoken settlement, attachment forwarding, and Tether row projection. TanStack reduced stream-part assembly but introduced its own transcript, run, queue, and subscription lifecycle.

### Host AG-UI prototype

```text
Prototype page
  -> TanStack useChat
     -> resumable SSE
        -> AG-UI host translator
           -> process-local delivery log
           -> TanStack run id <-> Tether request/turn mapping
           -> ConversationTurns FIFO
              -> Pi runtime
              -> SQLite Messages and turns
  -> post-finish hydration                canonical id reconciliation

Existing ChatBus /ws remains for invalidation and notification events.
```

This path has the better seam. It still needs a durable replay log and durable TanStack-to-Tether identity mapping to survive host restart without duplicate submission.

## Behavior matrix

| Behavior | Current Tether | Browser translator | Host AG-UI |
| --- | --- | --- | --- |
| Settled history hydration | Pass | Pass through manual projection | Pass through `hydrate()` |
| Live text | Pass | Pass | Pass with real Conversation |
| Live reasoning and tools | Pass | Pass in compatibility test | Translator implemented, not exercised by local faux provider |
| Refresh during running turn | Host state recovers; missed deltas are not replayed | Same current behavior | Pass in same host process with replay |
| Rejoin after host restart | Canonical turn recovers | Canonical turn recovers | Fail unless replay log and run mapping become durable |
| Stable final Message ids | Pass | Pass after history replacement | Pass after custom user-id event and post-finish hydration |
| Same assistant id from first delta through settlement | No, current live ids are provisional | No | No; AG-UI live id changes to canonical id at settlement |
| Duplicate optimistic rows | Pass | Pass | Pass after custom reconciliation |
| First prompt durability | Pass | Pass | Pass |
| Follow-up prompt durability | Pass, every prompt reaches host immediately | Pass only through a manual host-queue bridge | Fail; TanStack keeps follow-ups unsent and reload loses them |
| Pending/running hydration | Pass | Pass through manual Tether state | One active run only; no representation for the rest of Tether's FIFO |
| Stop | Cancels canonical turn | Pass through retained Tether abort logic | Fail; `chat.stop()` aborts the HTTP stream but host execution succeeds |
| Error settlement | Pass | Mapped to `RUN_ERROR` | Mapped to `RUN_ERROR` |
| Attachments | Pass | Existing attachment ids forwarded manually | Endpoint accepts ids, but TanStack multimodal input does not replace Tether staging and binding |
| Spoken turn settlement | Pass | Pass only through retained `AgentEndFrame` logic | Not supplied by Solid state; would require custom chunk handling |
| Tool-only spoken suppression | Pass | Retained manually | Requires custom terminal metadata handling |
| Global invalidations and notifications | Pass on `/ws` | Unchanged | `/ws` remains beside SSE |

Two failures were reproduced against the real local host:

- Clicking Stop on Conversation `01a04a20-be97-76ee-80fe-aa0f40baf037`
  aborted the TanStack stream, but the host turn succeeded and persisted its
  assistant Message.
- Sending two prompts on Conversation
  `01a04a21-038a-706c-9737-05ad8364f869` put only the first in Tether's durable
  FIFO. The second lived in TanStack's client queue and vanished on refresh.

## Measurements

### Source

Current chat runtime:

| File | Lines |
| --- | ---: |
| `apps/web/src/live-chat-turn.ts` | 1,022 |
| `apps/web/src/live-chat-turn-state.ts` | 461 |
| `apps/web/src/chat-bus.ts` | 181 |
| Total | 1,664 |

Prototype production code added while deleting none of the current runtime:

| Area | Lines |
| --- | ---: |
| Browser frame adapter and compatibility wrapper | 813 |
| Host AG-UI translation, hydration, identity map, replay log | 531 |
| Native Solid page | 126 |
| Route wiring | 14 inserted, 1 replaced |
| Total inserted | 1,484 |

The prototype added 543 test lines. The browser approach alone added 813 adapter lines and retained all 1,664 current lines. The host approach added about 670 production lines before implementing durable restart replay, host cancellation, attachments, spoken turns, pagination, or the full Kitn projection.

A hypothetical host-AG-UI adoption could remove much of the 1,483 lines in `live-chat-turn.ts` and `live-chat-turn-state.ts`. The experiment showed that durable FIFO handling, canonical reconciliation, spoken settlement, attachment binding, and pagination would return in a wrapper around TanStack. Gross deletion therefore overstates behavioral deletion.

### Bundle

Baseline production build:

- all JavaScript: 6,971,707 bytes raw, 1,748,315 bytes gzip;
- main chunk: 507,032 bytes raw, reported as 156.19 kB gzip by Vite.

Eagerly loading TanStack on the prototype route increased JavaScript by
173,119 bytes raw and 45,523 bytes gzip. The main chunk grew from 507,032 to
680,155 bytes raw.

Lazy loading produced:

- initial main chunk: 507,329 bytes raw, reported as 156.24 kB gzip;
- TanStack prototype chunk: 173,083 bytes raw, 45,599 bytes gzip;
- all JavaScript: 7,145,083 bytes raw, 1,793,960 bytes gzip.

Lazy loading keeps TanStack out of ordinary Tether routes. A real replacement cannot defer the package on Chat, so the eager figure is the relevant cost before subtracting deleted Tether code.

### Duplicated concepts

| Concept | Browser translator | Host AG-UI |
| --- | ---: | ---: |
| Transcript representations | 2 | 2 projections, 1 durable transcript |
| Run identities | 3: TanStack run, Tether request, Tether turn | Same, plus a mapping registry |
| Queue models | 2: TanStack unsent queue and Tether durable FIFO | Same |
| Chat reconnect models | 2 | 1 TanStack chat reconnect plus the retained global `/ws` reconnect |
| Persistence mechanisms | Tether DB plus TanStack client state | Tether DB plus required delivery replay log |

## Solid parity

`@tanstack/ai-solid@0.19.4` covers `useChat`, generation hooks, audio recording,
server hydration, queues, interrupts, SSE, and WebSocket adapters. It does not
export equivalents for React's:

- `useRealtimeChat`;
- `useMcpAppBridge`;
- `MCPAppResource`.

The headless client exports `RealtimeClient` and `createMcpAppBridge`, so Tether
could build Solid wrappers. That is more maintained code, not deletion. The
published Solid README is also titled `@tanstack/ai-react` and uses React
examples, which weakens confidence in Solid-specific documentation and parity.

## Upgrade and ownership risks

- Core, client, Solid, and React use independent version numbers and move at
  different rates.
- Core currently pins a canary AG-UI dependency.
- The prototype needed behavior not expressed by `ChatHydrationResult`: all
  durable pending turns, Tether request identity, canonical Message replacement,
  and host cancellation.
- TanStack run ids use strings such as `run-1787949979714-cwt2ya`; Tether's
  idempotent request ids are UUIDs. The prototype required another mapping.
- Same-process replay worked. Restart-safe replay would add a durable event log
  and durable identity mapping beside canonical Messages and turns.
- Solid-specific realtime voice and MCP Apps would require local wrappers.

## Deletion test

TanStack deletes generic AG-UI stream reduction and supplies a good reconnect
engine. It does not delete Tether's hard chat behavior:

- host-accepted FIFO queueing;
- idempotent submission and cancellation;
- canonical row identity and ordering;
- settled-history reconciliation;
- attachments;
- spoken settlement;
- tool-only suppression;
- global invalidation and notification transport.

The browser adapter plainly fails the deletion test. The host AG-UI design is
better, but it moves those invariants into host translation, replay, and client
reconciliation. Adopting it would exchange understood Tether code for a larger
protocol and ownership footprint.

## Conditions for another review

Review again if TanStack can represent a server-owned queue of already accepted
runs, adopt server Message and run identities without custom events, and direct
Stop to a server-owned turn. Solid should also reach realtime voice and MCP Apps
parity. A separate architectural decision would be required if Tether ever
chooses AG-UI plus a durable event log as its canonical chat delivery protocol.
