import { createQuery } from "@tanstack/solid-query";
import { For, Show, createSignal } from "solid-js";

import { EvidenceLink } from "../components/evidence-link";
import { MessageContent } from "../components/message-content";
import type { MemoriesHost } from "../host/memories";
import { queryKeys } from "../lib/query-keys";
import { TextField, TextFieldInput } from "@/components/ui/text-field";

export function MemoriesPanel(props: {
  api: MemoriesHost;
  onOpenEvidence: (uri: string) => void;
}) {
  const [search, setSearch] = createSignal("");
  const topics = createQuery(() => ({
    queryFn: () => props.api.listMemoryTopics(search()),
    queryKey: queryKeys.memoryTopics(search()),
  }));
  const diagnostics = createQuery(() => ({
    queryFn: () => props.api.listWorkspaceDiagnostics(),
    queryKey: queryKeys.memoriesWorkspaceDiagnostics,
  }));

  return (
    <section aria-label="Memory Topics" class="space-y-4">
      <header class="space-y-1">
        <h2 class="text-lg font-semibold">Memory</h2>
        <p class="text-muted-foreground text-sm">
          Dreaming maintains these Evidence-backed Topics. Corrections happen in
          chat, not through direct editing.
        </p>
      </header>

      <TextField onChange={setSearch} value={search()}>
        <TextFieldInput
          aria-label="Search Memory"
          placeholder="Search Topics"
        />
      </TextField>

      <Show when={(diagnostics.data?.length ?? 0) > 0}>
        <aside class="border-destructive/40 bg-destructive/5 rounded-lg border p-3">
          <h3 class="font-medium">Memory workspace diagnostics</h3>
          <ul class="mt-2 space-y-1 text-sm">
            <For each={diagnostics.data ?? []}>
              {(diagnostic) => (
                <li>{`${diagnostic.path}: ${diagnostic.message}`}</li>
              )}
            </For>
          </ul>
        </aside>
      </Show>

      <Show
        fallback={
          <p class="text-muted-foreground text-sm">No Memory Topics yet</p>
        }
        when={(topics.data?.length ?? 0) > 0}
      >
        <ul class="space-y-3">
          <For each={topics.data ?? []}>
            {(topic) => (
              <li
                aria-label={`Memory Topic: ${topic.title}`}
                class="bg-card rounded-lg border p-4"
              >
                <div class="flex items-start justify-between gap-3">
                  <h3 class="font-medium">{topic.title}</h3>
                  <code class="text-muted-foreground text-xs">
                    {topic.path}
                  </code>
                </div>
                <div class="mt-2">
                  <MessageContent
                    onOpenEvidence={props.onOpenEvidence}
                    text={topic.body}
                  />
                </div>
                <Show when={topic.evidence.length > 0}>
                  <details class="mt-3 text-xs">
                    <summary class="text-muted-foreground cursor-pointer select-none font-medium">
                      {topic.evidence.length} Evidence{" "}
                      {topic.evidence.length === 1 ? "source" : "sources"}
                    </summary>
                    <ul class="mt-2 max-h-52 space-y-1 overflow-y-auto pl-2">
                      <For each={topic.evidence}>
                        {(evidence) => (
                          <li class="text-muted-foreground break-all font-mono">
                            <EvidenceLink
                              onOpen={props.onOpenEvidence}
                              uri={evidence}
                            >
                              {evidence}
                            </EvidenceLink>
                          </li>
                        )}
                      </For>
                    </ul>
                  </details>
                </Show>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </section>
  );
}
