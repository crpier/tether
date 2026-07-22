// PROTOTYPE #246 — throwaway, do not ship
//
// Browse: memory corpus + search, todos, triggers/reminders, synthetic
// panels. Not variant-gated — the open question is only about Proposals and
// Inbox internals; Browse gets one reasonable layout.

import { For, Show, createMemo, createSignal } from "solid-js";

import { mockCorpus, mockTodos, mockTriggers } from "../mock-data";
import { Badge } from "@/components/ui/badge";
import { TextField, TextFieldInput } from "@/components/ui/text-field";
import { panelClass } from "@/lib/panel";
import { cx } from "@/lib/cva";

function DiscussButton() {
  return (
    <button
      class="rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
      title="Discuss in chat (non-functional in prototype)"
      type="button"
    >
      Discuss →
    </button>
  );
}

function CorpusSection() {
  const [query, setQuery] = createSignal("");
  const filtered = createMemo(() => {
    const q = query().toLowerCase();
    return q.length === 0
      ? mockCorpus
      : mockCorpus.filter((m) => m.text.toLowerCase().includes(q));
  });

  return (
    <section class="flex flex-col gap-2">
      <div class="flex items-center justify-between">
        <h2 class="text-sm font-semibold">
          Memory corpus ({mockCorpus.length})
        </h2>
      </div>
      <TextField>
        <TextFieldInput
          onInput={(e) => setQuery(e.currentTarget.value)}
          placeholder="Search memories…"
          value={query()}
        />
      </TextField>
      <div class="flex flex-col gap-1.5">
        <For each={filtered()}>
          {(memory) => (
            <div class="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm">
              <span class="truncate">{memory.text}</span>
              <div class="flex shrink-0 items-center gap-2">
                <Badge
                  variant={memory.state === "active" ? "default" : "outline"}
                >
                  {memory.state}
                </Badge>
                <DiscussButton />
              </div>
            </div>
          )}
        </For>
        <Show when={filtered().length === 0}>
          <p class="text-sm text-muted-foreground">No matches.</p>
        </Show>
      </div>
    </section>
  );
}

function TodosSection() {
  const ready = createMemo(() => mockTodos.filter((t) => t.status === "ready"));
  const waiting = createMemo(() =>
    mockTodos.filter((t) => t.status === "waiting"),
  );

  return (
    <section class="flex flex-col gap-2">
      <h2 class="text-sm font-semibold">Todos ({mockTodos.length})</h2>
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div class="flex flex-col gap-1.5">
          <h3 class="text-xs font-semibold uppercase text-muted-foreground">
            Ready ({ready().length})
          </h3>
          <For each={ready()}>
            {(todo) => (
              <div class="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm">
                <span class="truncate">{todo.title}</span>
                <DiscussButton />
              </div>
            )}
          </For>
        </div>
        <div class="flex flex-col gap-1.5">
          <h3 class="text-xs font-semibold uppercase text-muted-foreground">
            Waiting ({waiting().length})
          </h3>
          <For each={waiting()}>
            {(todo) => (
              <div class="flex flex-col gap-0.5 rounded-lg border px-3 py-2 text-sm">
                <div class="flex items-center justify-between gap-2">
                  <span class="truncate">{todo.title}</span>
                  <DiscussButton />
                </div>
                <span class="text-xs text-muted-foreground">
                  waiting on: {todo.waitingOn}
                </span>
              </div>
            )}
          </For>
        </div>
      </div>
    </section>
  );
}

function TriggersSection() {
  return (
    <section class="flex flex-col gap-2">
      <h2 class="text-sm font-semibold">Triggers ({mockTriggers.length})</h2>
      <div class="flex flex-col gap-1.5">
        <For each={mockTriggers}>
          {(trigger) => (
            <div class="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm">
              <div>
                <span class="font-medium">{trigger.label}</span>
                <span class="ml-2 text-xs text-muted-foreground">
                  {trigger.recurrence}
                </span>
              </div>
              <span class="text-xs text-muted-foreground">
                next: {new Date(trigger.nextFireAt).toLocaleString()}
              </span>
            </div>
          )}
        </For>
      </div>
    </section>
  );
}

function SyntheticSection() {
  return (
    <section class="flex flex-col gap-2">
      <h2 class="text-sm font-semibold">Synthetic panels</h2>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div class={cx(panelClass, "text-sm")}>
          <p class="font-medium">Weekly Gmail purge summary</p>
          <p class="text-xs text-muted-foreground">
            Auto-generated panel — placeholder content in this prototype.
          </p>
        </div>
        <div class={cx(panelClass, "text-sm")}>
          <p class="font-medium">Reading list digest</p>
          <p class="text-xs text-muted-foreground">
            Auto-generated panel — placeholder content in this prototype.
          </p>
        </div>
      </div>
    </section>
  );
}

export function BrowsePage() {
  return (
    <div class="flex flex-1 flex-col gap-6 p-4">
      <h1 class="text-lg font-semibold">Browse</h1>
      <CorpusSection />
      <TodosSection />
      <TriggersSection />
      <SyntheticSection />
    </div>
  );
}
