import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Match, Show, Switch, createEffect, createSignal } from "solid-js";

import type { Memory, Panel, TetherApi } from "../api";
import { formatDate as formatDateOnly } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { renderVegaLiteWidget } from "../components/widgets/vega-lite-widget";
import { Button } from "@/components/ui/button";

// One generic component serves every saved Synthetic panel forever — a panel
// is data (a saved faceted query plus a render choice), never a new component.
// Results are recomputed on every fetch (ADR 0006); rendering goes through the
// Widget vocabulary (ADR 0011): a Tether-styled table by default, or the
// stored Vega-Lite spec template with the result rows injected as its data.

function formatDate(value: string | null): string {
  if (value === null) {
    return "";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : formatDateOnly(parsed);
}

// Project a Memory onto the flat row shape both render kinds consume: the
// content, the tethered date, and the panel's chosen facet columns.
function resultRow(memory: Memory, columns: string[]): Record<string, string> {
  const row: Record<string, string> = {
    content: memory.content,
    tethered: formatDate(memory.tethered_at),
  };
  for (const column of columns) {
    row[column] = memory.facets[column] ?? "";
  }
  return row;
}

// Inject the result rows into the stored Vega-Lite spec template as its
// inline data source. The template's own `data` (if any) is replaced — the
// panel's query is the single source of rows, the template only styles them.
function specWithRows(
  specText: string,
  rows: Record<string, string>[],
): string {
  const spec = JSON.parse(specText) as { data?: unknown };
  spec.data = { values: rows };
  return JSON.stringify(spec);
}

function VegaLiteResults(props: {
  specText: string;
  rows: Record<string, string>[];
}) {
  const [failure, setFailure] = createSignal<string | undefined>();
  const [mount, setMount] = createSignal<HTMLDivElement>();

  createEffect(() => {
    const rows = props.rows;
    const target = mount();
    if (target === undefined) {
      return;
    }
    setFailure(undefined);
    void (async () => {
      try {
        await renderVegaLiteWidget(target, specWithRows(props.specText, rows));
      } catch {
        // A broken stored spec must never hide the data: fall back to the
        // table render below, with a visible note (mirrors the widget
        // vocabulary's graceful-fallback stance).
        setFailure("Chart spec failed to render — showing the table instead.");
      }
    })();
  });

  return (
    <div>
      <div ref={setMount} />
      <Show when={failure()}>
        {(message) => (
          <div>
            <p class="text-destructive text-xs" role="alert">
              {message()}
            </p>
            <ResultsTable columns={[]} rows={props.rows} />
          </div>
        )}
      </Show>
    </div>
  );
}

function ResultsTable(props: {
  columns: string[];
  rows: Record<string, string>[];
}) {
  const headers = () => ["content", ...props.columns, "tethered"];
  return (
    <div class="overflow-x-auto">
      <table class="w-full text-left text-sm">
        <thead>
          <tr>
            <For each={headers()}>
              {(header) => (
                <th class="text-muted-foreground py-1 pr-3 font-medium">
                  {header}
                </th>
              )}
            </For>
          </tr>
        </thead>
        <tbody>
          <For each={props.rows}>
            {(row) => (
              <tr class="border-t align-top">
                <For each={headers()}>
                  {(header) => <td class="py-1 pr-3">{row[header] ?? ""}</td>}
                </For>
              </tr>
            )}
          </For>
        </tbody>
      </table>
    </div>
  );
}

function SyntheticPanelCard(props: { api: TetherApi; panel: Panel }) {
  const queryClient = useQueryClient();
  const [error, setError] = createSignal<string | undefined>();
  const resultsQuery = createQuery(() => ({
    queryFn: () => props.api.getPanelResults(props.panel.id),
    queryKey: queryKeys.panelResults(props.panel.id),
  }));

  const rows = () =>
    (resultsQuery.data?.memories ?? []).map((memory) =>
      resultRow(memory, props.panel.columns),
    );

  const remove = () => {
    setError(undefined);
    void (async () => {
      try {
        await props.api.deletePanel(props.panel.id, props.panel.version);
        void queryClient.invalidateQueries({ queryKey: queryKeys.panels });
        void queryClient.refetchQueries({ queryKey: queryKeys.panels });
      } catch {
        setError("Could not delete the panel — refresh and try again.");
      }
    })();
  };

  return (
    <section aria-label={`Panel: ${props.panel.name}`} class={panelClass}>
      <div class="mb-2 flex items-center justify-between gap-2">
        <h2 class="text-sm font-semibold">{props.panel.name}</h2>
        <Button
          aria-label={`Delete panel ${props.panel.name}`}
          onClick={remove}
          size="sm"
          type="button"
          variant="ghost"
        >
          Delete
        </Button>
      </div>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive text-xs" role="alert">
            {message()}
          </p>
        )}
      </Show>
      <Switch>
        <Match when={resultsQuery.isPending}>
          <p class="text-muted-foreground text-sm">Loading…</p>
        </Match>
        <Match when={resultsQuery.isError}>
          <p class="text-destructive text-sm" role="alert">
            Could not run this panel's query.
          </p>
        </Match>
        <Match when={resultsQuery.data?.total === 0}>
          <p class="text-muted-foreground text-sm">
            No memories match this panel — its facets may have drifted (check
            the facet overview).
          </p>
        </Match>
        <Match when={resultsQuery.data}>
          {(results) => (
            <div class="flex flex-col gap-2">
              <Switch>
                <Match
                  when={
                    props.panel.render_kind === "vega-lite" &&
                    props.panel.vega_lite_spec !== null
                  }
                >
                  <VegaLiteResults
                    rows={rows()}
                    specText={props.panel.vega_lite_spec ?? ""}
                  />
                </Match>
                <Match when={true}>
                  <ResultsTable columns={props.panel.columns} rows={rows()} />
                </Match>
              </Switch>
              <Show when={results().total > results().memories.length}>
                <p class="text-muted-foreground text-xs">
                  Showing {results().memories.length} of {results().total}
                </p>
              </Show>
            </div>
          )}
        </Match>
      </Switch>
    </section>
  );
}

export function SyntheticPanels(props: { api: TetherApi }) {
  const panelsQuery = createQuery(() => ({
    queryFn: () => props.api.listPanels(),
    queryKey: queryKeys.panels,
  }));

  // No saved panels renders nothing at all: the panel column belongs to the
  // dedicated panels until the user actually saves a Synthetic one.
  return (
    <For each={panelsQuery.data ?? []}>
      {(panel) => <SyntheticPanelCard api={props.api} panel={panel} />}
    </For>
  );
}
