import { createQuery } from "@tanstack/solid-query";
import { For, Show, createSignal } from "solid-js";

import type { LedgerEntry, LedgersHost } from "../host/ledgers";
import { EvidenceLink } from "../components/evidence-link";
import { formatDateTime } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";

function displayValue(value: LedgerEntry["values"][string]): string {
  return typeof value === "boolean" ? (value ? "Yes" : "No") : String(value);
}

export function LedgersPanel(props: {
  api: LedgersHost;
  onOpenEvidence: (uri: string) => void;
}) {
  const [includeSuperseded, setIncludeSuperseded] = createSignal(false);
  const [selectedLedgerId, setSelectedLedgerId] = createSignal<string>();
  const proposalsQuery = createQuery(() => ({
    queryFn: () => props.api.listLedgerProposals(),
    queryKey: queryKeys.ledgerProposals,
  }));
  const ledgersQuery = createQuery(() => ({
    queryFn: () => props.api.listLedgers(),
    queryKey: queryKeys.ledgers,
  }));
  const detailQuery = createQuery(() => ({
    enabled: selectedLedgerId() !== undefined,
    queryFn: () => props.api.fetchLedger(selectedLedgerId()!),
    queryKey: ["ledgers", selectedLedgerId(), "detail"],
  }));
  const entriesQuery = createQuery(() => ({
    enabled: selectedLedgerId() !== undefined,
    queryFn: () =>
      props.api.listLedgerEntries(selectedLedgerId()!, includeSuperseded()),
    queryKey: queryKeys.ledgerEntries(
      selectedLedgerId() ?? "",
      includeSuperseded(),
    ),
  }));

  return (
    <section aria-label="Ledgers" class={panelClass}>
      <div class="mb-4">
        <h2 class="text-sm font-semibold">Ledgers</h2>
        <p class="text-muted-foreground mt-1 text-sm">
          User-approved structured histories outside Memory.
        </p>
      </div>

      <Show when={(proposalsQuery.data?.length ?? 0) > 0}>
        <section
          aria-labelledby="ledger-proposals-title"
          class="mb-5 space-y-2"
        >
          <h3 class="text-sm font-semibold" id="ledger-proposals-title">
            Pending proposals
          </h3>
          <For each={proposalsQuery.data ?? []}>
            {(proposal) => (
              <article
                aria-label={`Ledger proposal: ${proposal.name}`}
                class="bg-muted rounded-md border p-3"
              >
                <div class="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <h4 class="text-sm font-medium">{proposal.name}</h4>
                    <p class="text-muted-foreground mt-1 text-sm">
                      {proposal.purpose}
                    </p>
                  </div>
                  <a
                    class="text-primary text-sm font-medium underline-offset-4 hover:underline"
                    href={`/chat?prompt=${encodeURIComponent(`Approve Ledger proposal ${proposal.id}.`)}`}
                  >
                    Approve in Chat
                  </a>
                </div>
                <p class="text-muted-foreground mt-2 text-xs">
                  Revision {proposal.proposed_revision} ·{" "}
                  {proposal.ledger_status}
                </p>
                <ul class="mt-3 space-y-2">
                  <For each={proposal.fields}>
                    {(field) => (
                      <li class="rounded border px-2 py-1.5 text-sm">
                        <div class="flex items-baseline justify-between gap-3">
                          <span>{field.label}</span>
                          <span class="text-muted-foreground text-xs">
                            {field.type}
                            {field.required ? " · required" : " · optional"}
                            {field.deprecated ? " · deprecated" : ""}
                          </span>
                        </div>
                        <code class="text-muted-foreground text-xs">
                          {field.field_id}
                        </code>
                        <p class="text-muted-foreground text-xs">
                          {field.description}
                        </p>
                        <Show when={field.unit}>
                          {(unit) => (
                            <p class="text-muted-foreground text-xs">
                              Unit: {unit()}
                            </p>
                          )}
                        </Show>
                        <Show when={field.enum_values?.length}>
                          <p class="text-muted-foreground text-xs">
                            Values: {field.enum_values?.join(", ")}
                          </p>
                        </Show>
                      </li>
                    )}
                  </For>
                </ul>
                <div class="text-muted-foreground mt-2 flex items-center gap-1 text-xs">
                  <span>
                    Proposed {formatDateTime(new Date(proposal.created_at))}
                  </span>
                  <EvidenceLink
                    onOpen={props.onOpenEvidence}
                    uri={`tether://message/${proposal.proposed_by_message_id}`}
                  />
                </div>
              </article>
            )}
          </For>
        </section>
      </Show>

      <Show
        fallback={
          <div class="space-y-2">
            <p class="text-sm font-medium">No approved Ledgers</p>
            <a
              class="text-primary text-sm font-medium underline-offset-4 hover:underline"
              href={`/chat?prompt=${encodeURIComponent("Propose a Ledger for: ")}`}
            >
              Propose in Chat
            </a>
          </div>
        }
        when={(ledgersQuery.data?.length ?? 0) > 0}
      >
        <section aria-labelledby="approved-ledgers-title" class="space-y-2">
          <h3 class="text-sm font-semibold" id="approved-ledgers-title">
            Approved Ledgers
          </h3>
          <For each={ledgersQuery.data ?? []}>
            {(ledger) => (
              <article
                aria-label={`Ledger: ${ledger.name}`}
                class="rounded-md border p-3"
              >
                <div class="flex items-baseline justify-between gap-3">
                  <button
                    aria-label={`Open ${ledger.name}`}
                    class="text-left text-sm font-medium hover:underline"
                    onClick={() => {
                      setIncludeSuperseded(false);
                      setSelectedLedgerId(ledger.id);
                    }}
                    type="button"
                  >
                    {ledger.name}
                  </button>
                  <span class="text-muted-foreground text-xs">
                    revision {ledger.revision} · {ledger.status}
                  </span>
                </div>
                <p class="text-muted-foreground mt-1 text-sm">
                  {ledger.purpose}
                </p>
              </article>
            )}
          </For>
        </section>
      </Show>

      <Show when={detailQuery.data}>
        {(detail) => (
          <section
            aria-label={`Ledger details: ${detail().current.name}`}
            class="mt-5"
          >
            <div class="flex flex-wrap items-center justify-between gap-2 border-t pt-4">
              <div>
                <h3 class="text-sm font-semibold">{detail().current.name}</h3>
                <p class="text-muted-foreground text-xs">
                  {detail().revisions.length} schema revision
                  {detail().revisions.length === 1 ? "" : "s"}
                </p>
              </div>
              <div class="flex items-center gap-3">
                <button
                  class="text-primary text-sm font-medium hover:underline"
                  onClick={() => {
                    setIncludeSuperseded((current) => !current);
                  }}
                  type="button"
                >
                  {includeSuperseded() ? "Hide history" : "Show history"}
                </button>
                <a
                  class="text-primary text-sm font-medium hover:underline"
                  download={`${detail().current.name}.json`}
                  href={`/api/ledgers/${detail().current.id}/export`}
                >
                  Export
                </a>
              </div>
            </div>
            <details class="mt-3 rounded-md border p-3">
              <summary class="cursor-pointer text-sm font-medium">
                Schema revisions
              </summary>
              <ol class="mt-2 space-y-2">
                <For each={detail().revisions}>
                  {(revision) => (
                    <li class="text-sm">
                      <span class="font-medium">
                        Revision {revision.revision}
                      </span>
                      <span class="text-muted-foreground">
                        {" "}
                        · {revision.status}
                      </span>
                      <EvidenceLink
                        class="ml-1 text-xs underline underline-offset-2"
                        onOpen={props.onOpenEvidence}
                        uri={`tether://message/${revision.approved_by_message_id}`}
                      />
                      <ul class="text-muted-foreground mt-1 space-y-1 pl-4 text-xs">
                        <For each={revision.fields}>
                          {(field) => (
                            <li>
                              <span>
                                {field.label} ({field.field_id}): {field.type}
                                {field.required ? " · required" : " · optional"}
                                {field.deprecated ? " · deprecated" : ""}
                              </span>
                              <p>{field.description}</p>
                              <Show when={field.unit}>
                                {(unit) => <p>Unit: {unit()}</p>}
                              </Show>
                              <Show when={field.enum_values?.length}>
                                <p>Values: {field.enum_values?.join(", ")}</p>
                              </Show>
                            </li>
                          )}
                        </For>
                      </ul>
                    </li>
                  )}
                </For>
              </ol>
            </details>
            <div class="mt-3 space-y-2">
              <Show
                fallback={
                  <p class="text-muted-foreground text-sm">No entries</p>
                }
                when={(entriesQuery.data?.length ?? 0) > 0}
              >
                <For each={entriesQuery.data ?? []}>
                  {(entry) => (
                    <article
                      aria-label={`Ledger entry: ${entry.id}`}
                      class="rounded-md border p-3"
                    >
                      <div class="flex items-center justify-between gap-2">
                        <p class="text-muted-foreground text-xs">
                          {formatDateTime(
                            new Date(entry.occurred_at ?? entry.recorded_at),
                          )}{" "}
                          · revision {entry.revision}
                        </p>
                        <Show when={!entry.is_current}>
                          <span class="text-muted-foreground text-xs">
                            Superseded
                          </span>
                        </Show>
                      </div>
                      <dl class="mt-2 space-y-1 text-sm">
                        <For each={Object.entries(entry.values)}>
                          {([fieldId, value]) => (
                            <div class="flex justify-between gap-3">
                              <dt class="text-muted-foreground">
                                {detail()
                                  .revisions.find(
                                    (revision) =>
                                      revision.revision === entry.revision,
                                  )
                                  ?.fields.find(
                                    (field) => field.field_id === fieldId,
                                  )?.label ?? fieldId}
                              </dt>
                              <dd>{displayValue(value)}</dd>
                            </div>
                          )}
                        </For>
                      </dl>
                      <div class="mt-2 flex gap-2">
                        <For each={entry.evidence}>
                          {(uri) => (
                            <EvidenceLink
                              onOpen={props.onOpenEvidence}
                              uri={uri}
                            />
                          )}
                        </For>
                      </div>
                    </article>
                  )}
                </For>
              </Show>
            </div>
          </section>
        )}
      </Show>
    </section>
  );
}
