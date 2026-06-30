# Chat UX findings — learning from t3code, applied to Tether

Phase 1: investigation only. No code changed. This is a notes/plan artifact.

Sources: `~/Projects/t3code` (React/Effect agent GUI), Tether's current chat
(`apps/web/src/app.tsx` `ChatView`, `apps/web/src/chat-bus.ts`,
`apps/host/tether/chat_ws.py`, `pi_runtime.py`, `chat_engine.py`), plus a live
probe of `pi --mode rpc` (see "pi RPC protocol facts").

---

## 0. TL;DR

Tether's chat works but is a thin transcript: plain-text bubbles, no markdown,
no autoscroll, a single accumulating stream string, and "refetch everything on
every event." The biggest *correctness* risks aren't visual — they're in the
host stream lifecycle (disconnect mid-turn poisons the next turn; a 60s stall
tells the browser nothing; reasoning deltas can leak into the answer on
non-Codex providers). The biggest *experience* gaps are markdown rendering,
scroll/anchoring, and a real per-turn working/tool model.

t3code is worth copying mostly at the **model layer** (normalize provider events
into a stable timeline of typed rows) and the **scroll layer**, not the
1:1 React components.

---

## 1. What t3code does well (transferable patterns)

### 1.1 A normalization seam: raw events → typed timeline rows
`session-logic.ts` turns provider runtime events into a small typed vocabulary
before the UI sees them:
- `WorkLogEntry` with a `tone: "thinking" | "tool" | "info" | "error"`,
  lifecycle `status`, `command`, `changedFiles`, `detail`.
- `TimelineEntry = message | work | proposed-plan`.
- `MessagesTimelineRow` (the render model) adds `work-toggle`, `turn-fold`,
  `working` rows.

The UI never parses provider payloads. This is the single most important idea to
steal: **put a pure function between pi and the bubbles.** It's independently
unit-testable (t3code has `MessagesTimeline.logic.test.ts`, 1173 lines) and lets
you change rendering without touching transport.

### 1.2 Turn folding — "Worked for 1m 12s"
Settled turns collapse all their thinking/tool chatter behind a single
`turn-fold` row; only the terminal assistant message stays visible
(`deriveTurnFolds`). Anti-flicker rules worth noting:
- A turn is "unsettled" while running OR while the *previous* turn is still the
  active one (right after send, before the server creates the new turn) — folding
  is keyed on turn lifecycle, not transient "working" state, so it doesn't flash.
- A streaming turn never folds.
- An interrupted turn stays expanded ("You stopped after 8s") until the next turn.

### 1.3 Scroll: three explicit modes, not "scrollToBottom on every chunk"
`timelineScrollAnchoring.ts` + LegendList:
- `following-end` | `anchoring-new-turn` | `free-scrolling`.
- On a new user turn it anchors the *new user message to the top* of the
  viewport (ChatGPT-style) rather than pinning the bottom, so the reply grows
  into open space. `anchoredEndSpace` reserves end padding to make that possible.
- `maintainVisibleContentPosition` + `maintainScrollAtEnd` keep position stable
  when content above resizes (e.g. a code block finishes highlighting).
- Manual scroll-up switches to `free-scrolling` and stops auto-follow; an
  explicit affordance returns you to the end. `[overflow-anchor:none]` disables
  the browser's native anchoring so theirs wins.
- Expanding a collapsed group preserves the clicked row's screen position by
  measuring `getBoundingClientRect().bottom` before/after a `flushSync` and
  correcting scroll by the delta. Nice trick for jump-free expand.

### 1.4 Streaming markdown that doesn't thrash
`ChatMarkdown.tsx`: `react-markdown` + `remark-gfm` + `remark-breaks`, and
crucially `rehype-raw` **followed by `rehype-sanitize`** (raw HTML allowed but
sanitized — required if you render model output as HTML).
- Shiki highlight results are cached in an LRU keyed by `hash(code+lang+theme)`,
  but **the cache is bypassed while streaming** (`isStreaming` ⇒ don't read or
  write cache) because partial code is wasteful/incorrect to cache. Final code
  gets cached once the stream ends.
- Highlight failure falls back to `lang:"text"` instead of throwing.

### 1.5 Don't re-render the list every second
- The "Working for 12s" timer mutates its own text node via `setInterval` +
  `textRef.current.textContent` — **no React commit per tick** (comment in
  `MessagesTimeline.tsx:1076`). Same for elapsed labels.
- `computeStableMessagesTimelineRows` keeps referential identity for unchanged
  rows (by-id map + shallow per-variant comparison) so virtualization/memo don't
  churn while one row streams.
- Virtualized via `@legendapp/list`; stable `keyExtractor` (row id) and
  `getItemType` (`message:user`, `work`, …) for recycling.

### 1.6 Error/status surfaces are first-class, layered, non-fatal
- `ThreadErrorBanner` — dismissible inline error, `line-clamp-3` + full text in
  tooltip (long provider errors don't blow up the layout).
- `ProviderStatusBanner` — distinguishes `warning` vs `error` vs `unauthenticated`
  ("Sign in via the CLI"), separate from per-message errors.
- Empty assistant response renders the literal `(empty response)` placeholder
  (`MessagesTimeline.tsx:979`) instead of a blank bubble.

### 1.7 Connection runtime separates "connected" from "synced"
`docs/architecture/connection-runtime.md`:
- One retry owner; transient failures retry forever with exp backoff capped at
  16s; offline waits don't consume attempts; explicit retry/credential change/
  app-activation interrupt the backoff.
- "Connected" only after the socket opens **and** an initial config RPC succeeds
  (proves the server is actually responsive — not just that a socket object
  exists).
- A failed data subscription does **not** tear down a healthy transport; it shows
  "connected, with a sync error."
- Cached projections are never allowed to overwrite newer live data during a fast
  reconnect.

---

## 2. pi RPC protocol facts (verified live, GPT-5.5 / openai-codex)

Probe: `pi --mode rpc --no-tools --thinking medium`, one prompt. Event order:

```
response (get_state)        # capabilities: model, contextWindow, maxTokens, thinkingLevel, isStreaming, isCompacting, steeringMode...
agent_start
turn_start
message_start  (role:user)        # pi echoes the user msg as its own message events
message_end    (role:user)
message_update assistantMessageEvent.type = thinking_start  (contentIndex 0)
message_update                      ... thinking_end          (codex: NO thinking_delta — reasoning is encrypted/empty)
message_update                      ... text_start            (contentIndex 1)
message_update                      ... text_delta  { delta:"391", contentIndex:1 }   # the actual answer chunk
message_update                      ... text_end
turn_end       (assistant message, incl. encrypted thinkingSignature)
agent_end      { messages:[...full final transcript...] }
```

Key facts:
- **Sub-type lives in `assistantMessageEvent.type`**, not the outer event.
  Tether forwards the inner type as the wire `event` (so the browser sees
  `text_delta`, `thinking_start`, …), and only `text_delta` carries `delta`.
- **`contentIndex` separates thinking (0) from text (1).** Tether ignores it.
- **Codex reasoning is opaque** (`thinking:""`, `encrypted_content`). You cannot
  show reasoning *text* for Codex — only "thinking…" presence. Other providers
  *will* stream `thinking_delta` with real text.
- **`agent_end.messages` is the authoritative final transcript** (and `turn_end`
  carries the settled assistant message). Tether ignores both and reconstructs
  from its own `message_end` handling.
- The runtime is a **single long-lived process per conversation** (idle TTL
  30 min, `chat_engine.py`) with **one shared unbounded event queue**
  (`PiRpcClient.events`); the stdout reader fills it autonomously regardless of
  whether anyone is consuming.

---

## 3. Tether current chat — gaps and concrete edge cases

### 3.1 Correctness / lifecycle bugs (highest priority — host side)

1. **Disconnect mid-turn poisons the next turn.** On WS disconnect,
   `websocket_bus` cancels the in-flight `_run_prompt` task, but pi keeps
   generating and keeps enqueuing events into the shared `runtime.client.events`
   queue. Nobody drains them. On reconnect + next prompt, `_stream_runtime` reads
   the **leftover events from the aborted turn first** (old deltas, an old
   `agent_end`) — so the new prompt's stream is corrupted / ends immediately.
   The queue is never reset between prompts. (`chat_ws.py` + `pi_runtime.py`.)
   - Related: the in-flight assistant message is never persisted on disconnect
     (settle runs in the cancelled task), so pi's session history and Tether's DB
     diverge — pi "remembers" a turn the transcript doesn't show.

2. **60s stall is invisible to the browser.** `_AGENT_EVENT_TIMEOUT_SECONDS = 60`
   ⇒ `next_event` raises `TimeoutError`, which `_run_prompt` **re-raises without
   sending any frame**. The browser never gets `error`/`agent_end`; `generating`
   stays `true` forever (Stop stays enabled, Send disabled). Compare the
   `PiRuntimeError` path, which *does* send an error frame.

3. **Reasoning can leak into the answer on non-Codex providers.**
   `_forward_message_update` / `_delta_text` append *any* `delta` (including a
   future `thinking_delta`) to both the forwarded stream and the
   `streamed_text` persistence fallback, with no `contentIndex`/sub-type check.
   Masked today only because Codex thinking is encrypted (empty delta). Switch to
   a plaintext-reasoning model (the allowlist also has `opencode-go`) and
   reasoning tokens land in the assistant bubble.

4. **Abort leaves partial text unpersisted but shown.** `abort` acks immediately
   and flips `generating=false`, but the streaming task keeps running. If pi
   ends an aborted turn without a `message_end`, the partial answer is never
   persisted; it shows live, then **vanishes** when `rehydrate` clears
   `streamText`. Also: after `abort_ack` the user can hit Send while the old task
   is still draining → host rejects with "generation already running."

### 3.2 Streaming/state-management gaps (web side)

5. **"Refetch everything on every event."** `message_end`, `tool_end`, and
   `agent_end` all call `rehydrate()` (invalidate + refetch conversations AND
   messages, plus a manual refresh counter). Per turn that's several full message
   refetches. Single-tenant so it's cheap, but it causes flicker windows where
   `streamText` is cleared by the `createEffect` before/after the refetch settles.

6. **Tool messages render in the wrong place and can double.** Live tools are
   appended to a separate `liveToolMessages` array shown *after* all stored
   messages, so a tool that ran mid-turn appears at the bottom, not inline. On
   `tool_end` the code both appends an optimistic tool row **and** `rehydrate()`s
   (which refetches the now-persisted tool row) → transient duplicate until the
   effect clears the optimistic list.

7. **Single accumulating `streamText` can't model reality.** One string can't
   represent multiple assistant messages per turn, tool calls interleaved with
   text, or thinking-vs-text. pi already produces multi-content turns.

8. **Many no-op frames forwarded.** `thinking_start/end`, `text_start/end` are
   all sent to the browser and fall through to the `default` case that extracts
   an empty delta. Harmless but noise; no "thinking…" affordance is derived from
   them either.

### 3.3 Experience gaps (web side)

9. **No markdown.** Assistant text is `whitespace-pre-wrap` plain text
   (`app.tsx` `MessageRows`). Code, lists, tables, links all render raw. This is
   the most visible quality gap vs t3code.
10. **No autoscroll / anchoring.** The transcript `<section>` has
    `overflow-y-auto` and nothing scrolls it; during a stream the user watches
    text grow off-screen and must scroll manually. No "jump to latest."
11. **No working indicator with elapsed time, no per-turn duration, no
    "stopped" state, no empty-response placeholder.**
12. **Error UX is a bare red `<p>`** that isn't dismissible and has no
    distinction between provider/auth/transport/turn errors.
13. **No virtualization** — fine now (one short conversation), but the whole
    transcript re-renders on each `streamText` tick.
14. **Reconnect is bare** (`chat-bus.ts`): fixed 1s retry, no backoff, no
    "reconnecting" UI, no `connected-but-syncing` distinction, no resume of an
    in-flight turn (ties back to 3.1.1).

---

## 4. Recommended direction (for phase 2 — not yet implemented)

Ordered by value/effort. Fix the host lifecycle first; it's where data actually
breaks.

**Host (correctness):**
- A. Drain/reset the pi event queue at the start of each prompt, OR tag events
  with a turn/prompt id and ignore stale ones; and on disconnect, either issue
  `abort` to pi or keep draining so the next turn starts clean. (Fixes 3.1.1.)
- B. On `TimeoutError`, send an `error`/`agent_end` frame before raising. Decide
  whether 60s of silence should even be terminal for long tool runs. (3.1.2.)
- C. Gate streamed text by `contentIndex`/sub-type so thinking never merges into
  the answer; consider forwarding a typed `reasoning` channel instead of dropping
  it. (3.1.3.)
- D. Consider trusting `turn_end` / `agent_end.messages` as the settle source of
  truth instead of hand-reassembling deltas.

**Web (model + experience):**
- E. Introduce a normalization function (pi frame → typed timeline rows) à la
  `session-logic.ts`; render from that, not from three ad-hoc signals
  (`optimisticMessages` / `liveToolMessages` / `streamText`). (3.2.5–7.)
- F. Render assistant messages as sanitized markdown with streaming-aware code
  highlighting. (3.3.9.)
- G. Add scroll anchoring (follow-end + anchor-new-turn + free-scroll + jump
  button) with stable scroll on content resize. (3.3.10.)
- H. Working indicator with self-ticking elapsed timer; per-turn "Worked for X";
  inline tool rows with tone/success/failure; `(empty response)` placeholder.
- I. Dismissible, layered error/connection banners; backoff + "reconnecting" UI
  in the chat bus.

---

## 5. Probe script

`scratchpad/pi_probe.py` (session-local) drives `pi --mode rpc` and dumps event
types + `assistantMessageEvent` sub-types. Re-run with a plaintext-reasoning
provider to confirm `thinking_delta` shape before implementing C/reasoning UI.
