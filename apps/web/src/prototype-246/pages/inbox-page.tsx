// PROTOTYPE #246 — throwaway, do not ship
//
// Inbox: everything awaiting adjudication — memory review queue, bucket
// triage, recall due prompts, fired reminders/notifications (dismissible).
// Same ?variant= switch as Proposals, applied to this heterogeneous item set.

import { For, Match, Show, Switch, createMemo, createSignal } from "solid-js";

import {
  mockBucketTriageItems,
  mockFiredReminders,
  mockMemoryReviewItems,
  mockRecallPrompts,
} from "../mock-data";
import { variant } from "../store";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { panelClass } from "@/lib/panel";
import { cx } from "@/lib/cva";

type ItemKind = "memory-review" | "bucket-triage" | "recall" | "fired";

interface InboxRow {
  id: string;
  kind: ItemKind;
  title: string;
  detail: string;
  meta: string;
}

function buildRows(): InboxRow[] {
  return [
    ...mockMemoryReviewItems.map((m): InboxRow => ({
      detail: m.text,
      id: m.id,
      kind: "memory-review",
      meta: `${m.provenance} · ${String(Math.round(m.confidence * 100))}% confidence`,
      title: "Review memory",
    })),
    ...mockBucketTriageItems.map((b): InboxRow => ({
      detail: b.raw,
      id: b.id,
      kind: "bucket-triage",
      meta: `captured ${new Date(b.capturedAt).toLocaleString()} · suggested: ${b.suggestedType}`,
      title: "Triage capture",
    })),
    ...mockRecallPrompts.map((r): InboxRow => ({
      detail: `${r.question} (memory: "${r.memoryText}")`,
      id: r.id,
      kind: "recall",
      meta: `due ${new Date(r.dueAt).toLocaleString()}`,
      title: "Recall prompt",
    })),
    ...mockFiredReminders.map((f): InboxRow => ({
      detail: `${f.title} — ${f.detail}`,
      id: f.id,
      kind: "fired",
      meta: `fired ${new Date(f.firedAt).toLocaleString()}`,
      title: "Reminder fired",
    })),
  ];
}

const ALL_ROWS = buildRows();

function kindLabel(kind: ItemKind): string {
  switch (kind) {
    case "memory-review":
      return "Memory review";
    case "bucket-triage":
      return "Bucket triage";
    case "recall":
      return "Recall due";
    case "fired":
      return "Fired reminder";
  }
}

function kindActions(kind: ItemKind): [string, string] {
  switch (kind) {
    case "memory-review":
      return ["Keep", "Discard"];
    case "bucket-triage":
      return ["Accept suggestion", "Recategorize"];
    case "recall":
      return ["Still true", "No longer true"];
    case "fired":
      return ["Dismiss", "Snooze"];
  }
}

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

// ---- Variant A: master-detail ------------------------------------

function VariantA(props: {
  resolved: Partial<Record<string, string>>;
  resolve: (id: string, action: string) => void;
}) {
  const [selectedId, setSelectedId] = createSignal<string | null>(
    ALL_ROWS[0]?.id ?? null,
  );
  const selected = createMemo(() =>
    ALL_ROWS.find((r) => r.id === selectedId()),
  );

  return (
    <div class="flex h-full min-h-0 gap-4">
      <div class="w-80 shrink-0 overflow-y-auto rounded-xl border">
        <For each={ALL_ROWS}>
          {(row) => {
            const done = () => props.resolved[row.id];
            return (
              <button
                class={cx(
                  "flex w-full flex-col gap-1 border-b px-3 py-2.5 text-left text-sm last:border-0",
                  selectedId() === row.id ? "bg-accent" : "hover:bg-accent/50",
                  done() && "opacity-50",
                )}
                onClick={() => setSelectedId(row.id)}
                type="button"
              >
                <div class="flex items-center justify-between gap-2">
                  <Badge variant="outline">{kindLabel(row.kind)}</Badge>
                  {done() && (
                    <span class="text-xs text-muted-foreground">{done()}</span>
                  )}
                </div>
                <span class="truncate text-xs text-muted-foreground">
                  {row.detail}
                </span>
              </button>
            );
          }}
        </For>
      </div>

      <div class="min-w-0 flex-1 overflow-y-auto">
        <Show
          fallback={
            <p class="text-sm text-muted-foreground">Nothing selected.</p>
          }
          when={selected()}
        >
          {(row) => {
            const [primary, secondary] = kindActions(row().kind);
            const done = () => props.resolved[row().id];
            return (
              <div class={cx(panelClass, "flex flex-col gap-3")}>
                <Badge variant="outline">{kindLabel(row().kind)}</Badge>
                <p class="text-sm">{row().detail}</p>
                <p class="text-xs text-muted-foreground">{row().meta}</p>
                <div class="flex items-center gap-2 border-t pt-3">
                  <Show
                    fallback={
                      <span class="text-sm text-muted-foreground">
                        {done()}
                      </span>
                    }
                    when={!done()}
                  >
                    <Button
                      onClick={() => props.resolve(row().id, primary)}
                      size="sm"
                    >
                      {primary}
                    </Button>
                    <Button
                      onClick={() => props.resolve(row().id, secondary)}
                      size="sm"
                      variant="outline"
                    >
                      {secondary}
                    </Button>
                  </Show>
                  <DiscussButton />
                </div>
              </div>
            );
          }}
        </Show>
      </div>
    </div>
  );
}

// ---- Variant B: stacked cards, grouped by section ------------------

function VariantB(props: {
  resolved: Partial<Record<string, string>>;
  resolve: (id: string, action: string) => void;
}) {
  const groups: { kind: ItemKind; heading: string }[] = [
    { heading: "Memory review", kind: "memory-review" },
    { heading: "Bucket triage", kind: "bucket-triage" },
    { heading: "Recall due", kind: "recall" },
    { heading: "Fired reminders", kind: "fired" },
  ];

  return (
    <div class="flex flex-col gap-6">
      <For each={groups}>
        {(group) => {
          const rows = ALL_ROWS.filter((r) => r.kind === group.kind);
          return (
            <div class="flex flex-col gap-2">
              <h3 class="text-sm font-semibold text-muted-foreground">
                {group.heading} ({rows.length})
              </h3>
              <For each={rows}>
                {(row) => {
                  const [primary, secondary] = kindActions(row.kind);
                  const done = () => props.resolved[row.id];
                  return (
                    <div
                      class={cx(
                        panelClass,
                        "flex flex-col gap-2",
                        done() && "opacity-60",
                      )}
                    >
                      <p class="text-sm">{row.detail}</p>
                      <p class="text-xs text-muted-foreground">{row.meta}</p>
                      <div class="flex items-center gap-2 border-t pt-2">
                        <Show
                          fallback={
                            <span class="text-sm text-muted-foreground">
                              {done()}
                            </span>
                          }
                          when={!done()}
                        >
                          <Button
                            onClick={() => props.resolve(row.id, primary)}
                            size="sm"
                          >
                            {primary}
                          </Button>
                          <Button
                            onClick={() => props.resolve(row.id, secondary)}
                            size="sm"
                            variant="outline"
                          >
                            {secondary}
                          </Button>
                        </Show>
                        <DiscussButton />
                      </div>
                    </div>
                  );
                }}
              </For>
            </div>
          );
        }}
      </For>
    </div>
  );
}

// ---- Variant C: dense table -----------------------------------------

function VariantC(props: {
  resolved: Partial<Record<string, string>>;
  resolve: (id: string, action: string) => void;
}) {
  const [expanded, setExpanded] = createSignal<string | null>(null);
  const [selected, setSelected] = createSignal<Set<string>>(new Set());

  const toggleSelected = (id: string) => {
    const next = new Set(selected());
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };

  const bulkResolve = (action: string) => {
    for (const id of selected()) {
      props.resolve(id, action);
    }
    setSelected(new Set<string>());
  };

  return (
    <div class="flex flex-col gap-2">
      <Show when={selected().size > 0}>
        <div class="flex items-center gap-2 rounded-lg border bg-accent px-3 py-2 text-sm">
          <span class="font-medium">{selected().size} selected</span>
          <Button onClick={() => bulkResolve("Resolved")} size="sm">
            Bulk resolve
          </Button>
        </div>
      </Show>

      <div class="overflow-x-auto rounded-xl border">
        <table class="w-full min-w-[720px] text-left text-sm">
          <thead class="bg-muted/50 text-xs uppercase text-muted-foreground">
            <tr>
              <th class="w-8 px-2 py-2"></th>
              <th class="px-2 py-2">Type</th>
              <th class="px-2 py-2">Detail</th>
              <th class="px-2 py-2">Meta</th>
              <th class="px-2 py-2">Status</th>
              <th class="px-2 py-2"></th>
            </tr>
          </thead>
          <tbody>
            <For each={ALL_ROWS}>
              {(row) => {
                const [primary, secondary] = kindActions(row.kind);
                const done = () => props.resolved[row.id];
                const isExpanded = () => expanded() === row.id;
                return (
                  <>
                    <tr class={cx("border-t", done() && "opacity-50")}>
                      <td class="px-2 py-1.5">
                        <input
                          checked={selected().has(row.id)}
                          disabled={!!done()}
                          onChange={() => toggleSelected(row.id)}
                          type="checkbox"
                        />
                      </td>
                      <td class="px-2 py-1.5">
                        <Badge variant="outline">{kindLabel(row.kind)}</Badge>
                      </td>
                      <td class="max-w-xs px-2 py-1.5">
                        <button
                          class="truncate text-left hover:underline"
                          onClick={() =>
                            setExpanded(isExpanded() ? null : row.id)
                          }
                          type="button"
                        >
                          {isExpanded() ? "▾" : "▸"} {row.detail}
                        </button>
                      </td>
                      <td class="px-2 py-1.5 text-xs text-muted-foreground">
                        {row.meta}
                      </td>
                      <td class="px-2 py-1.5">
                        <Show
                          fallback={
                            <div class="flex gap-1">
                              <Button
                                onClick={() => props.resolve(row.id, primary)}
                                size="sm"
                                variant="outline"
                              >
                                {primary}
                              </Button>
                              <Button
                                onClick={() => props.resolve(row.id, secondary)}
                                size="sm"
                                variant="outline"
                              >
                                {secondary}
                              </Button>
                            </div>
                          }
                          when={done()}
                        >
                          <span class="text-muted-foreground">{done()}</span>
                        </Show>
                      </td>
                      <td class="px-2 py-1.5">
                        <DiscussButton />
                      </td>
                    </tr>
                    <Show when={isExpanded()}>
                      <tr class="border-t bg-muted/30">
                        <td class="px-2 py-2 text-xs" colspan={6}>
                          {row.detail}
                        </td>
                      </tr>
                    </Show>
                  </>
                );
              }}
            </For>
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function InboxPage() {
  const [resolved, setResolved] = createSignal<Partial<Record<string, string>>>(
    {},
  );
  const resolve = (id: string, action: string) =>
    setResolved({ ...resolved(), [id]: action });

  return (
    <div class="flex flex-1 flex-col gap-4 p-4">
      <div class="flex items-center justify-between border-b pb-2">
        <h1 class="text-lg font-semibold">Inbox</h1>
        <span class="text-sm text-muted-foreground">
          {ALL_ROWS.length} awaiting adjudication
        </span>
      </div>

      <Switch>
        <Match when={variant() === "A"}>
          <VariantA resolve={resolve} resolved={resolved()} />
        </Match>
        <Match when={variant() === "B"}>
          <VariantB resolve={resolve} resolved={resolved()} />
        </Match>
        <Match when={variant() === "C"}>
          <VariantC resolve={resolve} resolved={resolved()} />
        </Match>
      </Switch>
    </div>
  );
}
