import { createQuery, useQueryClient } from "@tanstack/solid-query";
import {
  For,
  Match,
  Show,
  Switch,
  createMemo,
  createSignal,
  onCleanup,
} from "solid-js";
import type { JSX } from "solid-js";

import {
  SegmentedControl,
  segmentedPanelId,
  segmentedTabId,
} from "../components/segmented-control";
import { ApiError } from "../api";
import type {
  BucketItem,
  BucketItemType,
  BucketTriageReport,
  DedupAdvisory,
  TetherApi,
} from "../api";
import { formatDate as formatDateOnly } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
} from "@/components/ui/text-field";

const selectClass =
  "border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-9 rounded-md border px-3 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-[3px]";
const fieldLabelClass = "text-muted-foreground text-xs font-medium";

type BucketView = "active" | "history" | "triage";

interface PayloadField {
  key: string;
  label: string;
  numeric?: boolean;
}

// One entry per item type: the required field that names the intention and the
// optional field that pins it down (the same field Triage's under-specified
// heuristic checks host-side).
const ITEM_TYPE_FIELDS: Record<
  BucketItemType,
  { label: string; optional: PayloadField; primary: PayloadField }
> = {
  book: {
    label: "Book",
    optional: { key: "author", label: "Author" },
    primary: { key: "title", label: "Title" },
  },
  movie: {
    label: "Movie",
    optional: { key: "year", label: "Year", numeric: true },
    primary: { key: "title", label: "Title" },
  },
  place: {
    label: "Place",
    optional: { key: "location", label: "Location" },
    primary: { key: "name", label: "Name" },
  },
  purchase: {
    label: "Purchase",
    optional: { key: "price", label: "Price" },
    primary: { key: "name", label: "Name" },
  },
  travel: {
    label: "Travel",
    optional: { key: "season", label: "Season" },
    primary: { key: "destination", label: "Destination" },
  },
};

const ITEM_TYPES = Object.keys(ITEM_TYPE_FIELDS) as BucketItemType[];

// Long enough to coalesce a typing burst into one request, short enough that
// results still feel immediate against the local single-tenant host.
const SEARCH_DEBOUNCE_MS = 150;

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : formatDateOnly(parsed);
}

// The raw timestamp of the terminal transition a history row is ordered by.
function terminalStamp(item: BucketItem): string | undefined {
  return item.deleted_at ?? item.completed_at ?? undefined;
}

function terminalDate(item: BucketItem): string | undefined {
  const stamp = terminalStamp(item);
  return stamp === undefined ? undefined : formatDate(stamp);
}

function terminalTime(item: BucketItem): number {
  const stamp = terminalStamp(item);
  const parsed = stamp === undefined ? Number.NaN : new Date(stamp).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}

function duplicateLine(duplicate: BucketItem): string {
  const stamp = terminalDate(duplicate);
  const when = stamp === undefined ? "" : ` ${stamp}`;
  return `${duplicate.title} · ${duplicate.state}${when}`;
}

function metadataLine(item: BucketItem): string | undefined {
  const field = ITEM_TYPE_FIELDS[item.item_type].optional;
  const value = item.data[field.key];
  if (typeof value !== "string" && typeof value !== "number") {
    return undefined;
  }
  return `${field.label}: ${value.toString()}`;
}

const ALL_BUCKET_VIEWS: BucketView[] = ["active", "history", "triage"];

export function BucketPanel(props: {
  api: TetherApi;
  // Hides sub-views from the toggle and its initial selection (#250): the
  // Inbox page only ever wants Triage (the review-queue obligation), and
  // Browse only wants Active/History (look-things-up, not adjudication).
  hiddenViews?: BucketView[];
}) {
  const queryClient = useQueryClient();
  const availableViews = createMemo(() =>
    ALL_BUCKET_VIEWS.filter(
      (candidate) => !(props.hiddenViews ?? []).includes(candidate),
    ),
  );
  const [view, setView] = createSignal<BucketView>(
    availableViews()[0] ?? "active",
  );
  const [itemType, setItemType] = createSignal<BucketItemType>("movie");
  const [formOpen, setFormOpen] = createSignal(false);
  const [primaryValue, setPrimaryValue] = createSignal("");
  const [optionalValue, setOptionalValue] = createSignal("");
  const [intentContext, setIntentContext] = createSignal("");
  const [search, setSearch] = createSignal("");
  const [debouncedSearch, setDebouncedSearch] = createSignal("");
  const [error, setError] = createSignal<string | undefined>();
  const [advisory, setAdvisory] = createSignal<DedupAdvisory | undefined>();

  // Debounce keystrokes so each typing pause issues one search request (and
  // registers one cache entry) instead of one per keystroke.
  let searchDebounce: ReturnType<typeof setTimeout> | undefined;
  onCleanup(() => {
    clearTimeout(searchDebounce);
  });
  const onSearchInput = (value: string) => {
    setSearch(value);
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      setDebouncedSearch(value);
    }, SEARCH_DEBOUNCE_MS);
  };

  const fields = createMemo(() => ITEM_TYPE_FIELDS[itemType()]);
  const searchTerm = createMemo(() => debouncedSearch().trim());

  const activeQuery = createQuery(() => ({
    queryFn: () => props.api.listBucketItems("active"),
    queryKey: queryKeys.bucketItemsView("active"),
  }));
  const searchQuery = createQuery(() => {
    const term = searchTerm();
    return {
      enabled: term.length > 0,
      // Guard the blank term even though the query is disabled then: the WS
      // invalidate frame (`invalidateNamedKey`) refetches every cached
      // "bucket-items" query, including the empty-term cache entry this
      // disabled query registers, and the host 400s a blank search.
      queryFn: () =>
        term.length === 0
          ? Promise.resolve<BucketItem[]>([])
          : props.api.searchBucketItems(term),
      queryKey: queryKeys.bucketSearch(term),
    };
  });
  const completedQuery = createQuery(() => ({
    enabled: view() === "history",
    queryFn: () => props.api.listBucketItems("completed"),
    queryKey: queryKeys.bucketItemsView("completed"),
  }));
  const deletedQuery = createQuery(() => ({
    enabled: view() === "history",
    queryFn: () => props.api.listBucketItems("deleted"),
    queryKey: queryKeys.bucketItemsView("deleted"),
  }));
  const triageQuery = createQuery(() => ({
    enabled: view() === "triage",
    queryFn: () => props.api.getBucketTriage(),
    queryKey: queryKeys.bucketItemsView("triage"),
  }));

  const listedItems = createMemo(() =>
    searchTerm().length > 0
      ? (searchQuery.data ?? [])
      : (activeQuery.data ?? []),
  );

  const refresh = () => {
    // Mark every bucket query stale but only refetch what is on screen now
    // (the active list, plus the current search results while a term is
    // typed). A broad refetch would fan out to the disabled history/triage
    // queries and to one cache entry per previously typed search term; those
    // refetch when their view or term is next looked at.
    void queryClient.invalidateQueries({
      queryKey: queryKeys.bucketItems,
      refetchType: "none",
    });
    void queryClient.refetchQueries({
      queryKey: queryKeys.bucketItemsView("active"),
    });
    const term = searchTerm();
    if (term.length > 0) {
      void queryClient.refetchQueries({
        queryKey: queryKeys.bucketSearch(term),
      });
    }
  };

  const submit = () => {
    setError(undefined);
    setAdvisory(undefined);
    const config = fields();
    const primary = primaryValue().trim();
    if (primary.length === 0) {
      setError(`Add a ${config.primary.label.toLowerCase()}`);
      return;
    }
    if (intentContext().trim().length === 0) {
      setError("Add a reason so future-you knows why it's here");
      return;
    }
    const data: Record<string, string | number> = {
      [config.primary.key]: primary,
    };
    const optional = optionalValue().trim();
    if (optional.length > 0) {
      if (config.optional.numeric) {
        const numericValue = Number(optional);
        if (!Number.isInteger(numericValue)) {
          setError(`${config.optional.label} must be a whole number`);
          return;
        }
        data[config.optional.key] = numericValue;
      } else {
        data[config.optional.key] = optional;
      }
    }
    void (async () => {
      try {
        const added = await props.api.addBucketItem({
          data,
          intent_context: intentContext().trim(),
          item_type: itemType(),
        });
        setPrimaryValue("");
        setOptionalValue("");
        setIntentContext("");
        setFormOpen(false);
        if (added.dedup.severity !== "none") {
          setAdvisory(added.dedup);
        }
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not add the item",
        );
      }
    })();
  };

  const act = (item: BucketItem, action: "complete" | "delete") => {
    void (async () => {
      setError(undefined);
      try {
        await call(action, item.id, item.version);
        refresh();
      } catch (caught) {
        // Same stale-version race as the reminders panel: the agent (or another
        // tab) touched the item after we loaded the row, so the server's
        // version moved on. Refetch and retry once with the fresh version
        // instead of dead-ending on a bare 409.
        if (caught instanceof ApiError && caught.status === 409) {
          setError(await retryWithFreshVersion(item.id, action));
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : `Could not ${action} the item`,
        );
      }
    })();
  };

  const call = (
    action: "complete" | "delete",
    bucketItemId: string,
    version: number,
  ) =>
    action === "complete"
      ? props.api.completeBucketItem(bucketItemId, version)
      : props.api.deleteBucketItem(bucketItemId, version);

  // Refetch the active list, then retry once with the current version. Returns
  // undefined when the item is now settled (terminated here, or already gone
  // server-side), or the retry's own error message to show.
  //
  // This mirrors the editable-reminders pattern, but the symmetry is partial:
  // bucket items have no editable fields, so the only version bumps are the
  // terminal transitions themselves. Against the real host a 409 therefore
  // means the item already left the active list and the refetch settles it;
  // the retry-with-fresh-version arm only fires against fakes (or a future
  // host that bumps versions without terminating).
  const retryWithFreshVersion = async (
    bucketItemId: string,
    action: "complete" | "delete",
  ): Promise<string | undefined> => {
    await queryClient.refetchQueries({
      queryKey: queryKeys.bucketItemsView("active"),
    });
    const fresh = (
      queryClient.getQueryData<BucketItem[]>(
        queryKeys.bucketItemsView("active"),
      ) ?? []
    ).find((candidate) => candidate.id === bucketItemId);
    if (fresh === undefined) {
      refresh();
      return undefined;
    }
    try {
      await call(action, bucketItemId, fresh.version);
      refresh();
      return undefined;
    } catch (retryCaught) {
      return retryCaught instanceof Error
        ? retryCaught.message
        : `Could not ${action} the item`;
    }
  };

  const onSubmit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (event) => {
    event.preventDefault();
    submit();
  };

  return (
    <section aria-label="Bucket" class={panelClass}>
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold">Bucket</h2>
        <Show when={availableViews().length > 1}>
          <SegmentedControl
            aria-label="Bucket view"
            id="bucket-view"
            onChange={setView}
            options={availableViews().map((candidate) => ({
              label:
                candidate === "active"
                  ? "Active"
                  : candidate === "history"
                    ? "History"
                    : "Triage",
              value: candidate,
            }))}
            value={view()}
          />
        </Show>
      </div>
      <Switch>
        <Match when={view() === "active"}>
          <div
            aria-labelledby={segmentedTabId("bucket-view", "active")}
            id={segmentedPanelId("bucket-view", "active")}
            role="tabpanel"
          >
            <Show
              fallback={
                <Button
                  onClick={() => {
                    setFormOpen(true);
                  }}
                  type="button"
                >
                  Add item
                </Button>
              }
              when={formOpen()}
            >
              <form class="space-y-3" onSubmit={onSubmit}>
                <label class="grid gap-1">
                  <span class={fieldLabelClass}>Type</span>
                  <select
                    class={selectClass}
                    name="item_type"
                    onChange={(event) => {
                      setItemType(event.currentTarget.value as BucketItemType);
                      setPrimaryValue("");
                      setOptionalValue("");
                    }}
                    value={itemType()}
                  >
                    <For each={ITEM_TYPES}>
                      {(candidate) => (
                        <option value={candidate}>
                          {ITEM_TYPE_FIELDS[candidate].label}
                        </option>
                      )}
                    </For>
                  </select>
                </label>
                <TextField onChange={setPrimaryValue} value={primaryValue()}>
                  <TextFieldLabel>{fields().primary.label}</TextFieldLabel>
                  <TextFieldInput name="primary" />
                </TextField>
                <TextField onChange={setOptionalValue} value={optionalValue()}>
                  <TextFieldLabel>{fields().optional.label}</TextFieldLabel>
                  <TextFieldInput name="optional" />
                </TextField>
                <TextField onChange={setIntentContext} value={intentContext()}>
                  <TextFieldLabel>Reason</TextFieldLabel>
                  <TextFieldInput name="intent_context" />
                </TextField>
                <div class="flex gap-2">
                  <Button type="submit">Add item</Button>
                  <Button
                    onClick={() => {
                      setFormOpen(false);
                      setError(undefined);
                    }}
                    type="button"
                    variant="ghost"
                  >
                    Cancel
                  </Button>
                </div>
              </form>
            </Show>
            <Show when={advisory()}>
              {(dedup) => (
                <div
                  aria-label="Duplicate advisory"
                  class="bg-muted mt-3 rounded-md border px-3 py-2 text-sm"
                  role="status"
                >
                  <div class="flex items-start gap-2">
                    <p class="flex-1">
                      {dedup().severity === "warn"
                        ? "Added, but it duplicates an active item:"
                        : "Added — you've had this before:"}
                    </p>
                    <button
                      aria-label="Dismiss advisory"
                      class="shrink-0 opacity-70 hover:opacity-100"
                      onClick={() => {
                        setAdvisory(undefined);
                      }}
                      type="button"
                    >
                      ✕
                    </button>
                  </div>
                  <ul class="text-muted-foreground mt-1 space-y-0.5 text-xs">
                    <For each={dedup().duplicates}>
                      {(duplicate) => <li>{duplicateLine(duplicate)}</li>}
                    </For>
                  </ul>
                </div>
              )}
            </Show>
            <div class="mt-3">
              <TextField onChange={onSearchInput} value={search()}>
                <TextFieldLabel>Search</TextFieldLabel>
                <TextFieldInput name="search" type="search" />
              </TextField>
            </div>
            <Show
              fallback={
                <Show
                  fallback={
                    <div class="mt-3 space-y-1">
                      <p class="text-sm font-medium">
                        Nothing in the bucket yet
                      </p>
                      <p class="text-muted-foreground text-sm">
                        Bucket items are things you intend to consume or visit,
                        such as books, movies, and places.
                      </p>
                    </div>
                  }
                  when={searchTerm().length > 0}
                >
                  <p class="text-muted-foreground mt-3 text-sm">No matches</p>
                </Show>
              }
              when={listedItems().length > 0}
            >
              <ul class="mt-3 space-y-2">
                <For each={listedItems()}>
                  {(item) => (
                    <li
                      aria-label={`Bucket item: ${item.title}`}
                      class="bg-muted rounded-md border px-3 py-2 text-sm"
                    >
                      <div class="flex flex-wrap items-center gap-1">
                        <span class="font-medium">{item.title}</span>
                        <Badge variant="secondary">{item.item_type}</Badge>
                        <span class="text-muted-foreground text-xs">
                          {` · added ${formatDate(item.created_at)}`}
                        </span>
                        <Button
                          aria-label={`Complete Bucket item: ${item.title}`}
                          class="ml-auto"
                          onClick={() => {
                            act(item, "complete");
                          }}
                          size="sm"
                          type="button"
                          variant="ghost"
                        >
                          Complete
                        </Button>
                        <Button
                          aria-label={`Delete Bucket item: ${item.title}`}
                          onClick={() => {
                            act(item, "delete");
                          }}
                          size="sm"
                          type="button"
                          variant="ghost"
                        >
                          Delete
                        </Button>
                      </div>
                      <Show when={metadataLine(item)}>
                        {(metadata) => (
                          <p class="text-muted-foreground mt-0.5 text-xs">
                            {metadata()}
                          </p>
                        )}
                      </Show>
                      <p class="text-muted-foreground mt-0.5 text-xs italic">
                        {item.intent_context || "no reason noted"}
                      </p>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </div>
        </Match>
        <Match when={view() === "history"}>
          <div
            aria-labelledby={segmentedTabId("bucket-view", "history")}
            id={segmentedPanelId("bucket-view", "history")}
            role="tabpanel"
          >
            <BucketHistory
              completed={completedQuery.data ?? []}
              deleted={deletedQuery.data ?? []}
            />
          </div>
        </Match>
        <Match when={view() === "triage"}>
          <div
            aria-labelledby={segmentedTabId("bucket-view", "triage")}
            id={segmentedPanelId("bucket-view", "triage")}
            role="tabpanel"
          >
            <Show
              fallback={<p class="text-muted-foreground text-sm">Loading…</p>}
              when={triageQuery.data}
            >
              {(report) => <BucketTriage report={report()} />}
            </Show>
          </div>
        </Match>
      </Switch>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive mt-2 text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
    </section>
  );
}

function HistoryRow(props: { item: BucketItem }) {
  return (
    <li
      aria-label={`Bucket item: ${props.item.title}`}
      class="bg-muted rounded-md border px-3 py-2 text-sm"
    >
      <div class="flex flex-wrap items-center gap-1">
        <span class="font-medium">{props.item.title}</span>
        <Badge variant="secondary">{props.item.item_type}</Badge>
        <span class="text-muted-foreground text-xs">
          {` · ${props.item.state} ${terminalDate(props.item) ?? ""}`}
        </span>
      </div>
      <Show when={metadataLine(props.item)}>
        {(metadata) => (
          <p class="text-muted-foreground mt-0.5 text-xs">{metadata()}</p>
        )}
      </Show>
      <p class="text-muted-foreground mt-0.5 text-xs italic">
        {props.item.intent_context || "no reason noted"}
      </p>
    </li>
  );
}

function BucketHistory(props: {
  completed: BucketItem[];
  deleted: BucketItem[];
}) {
  // One interleaved timeline, newest terminal transition first.
  const items = createMemo(() =>
    [...props.completed, ...props.deleted].sort(
      (first, second) => terminalTime(second) - terminalTime(first),
    ),
  );
  return (
    <Show
      fallback={<p class="text-muted-foreground text-sm">No history yet</p>}
      when={items().length > 0}
    >
      <ul class="space-y-2">
        <For each={items()}>{(item) => <HistoryRow item={item} />}</For>
      </ul>
    </Show>
  );
}

function BucketTriage(props: { report: BucketTriageReport }) {
  const titles = createMemo(() => {
    const byId = new Map(
      props.report.active.map((item) => [item.id, item.title]),
    );
    return (bucketItemId: string) => byId.get(bucketItemId) ?? bucketItemId;
  });
  const findingCount = createMemo(
    () =>
      props.report.under_specified.length +
      props.report.duplicates.length +
      props.report.stale.length,
  );

  return (
    <Show
      fallback={
        <p class="text-muted-foreground text-sm">
          Nothing to triage — the backlog looks healthy.
        </p>
      }
      when={findingCount() > 0}
    >
      <div class="space-y-3 text-sm">
        <Show when={props.report.under_specified.length > 0}>
          <div>
            <h3 class={fieldLabelClass}>Under-specified</h3>
            <ul class="mt-1 space-y-1">
              <For each={props.report.under_specified}>
                {(flagged) => (
                  <li>
                    <span class="font-medium">
                      {titles()(flagged.bucket_item_id)}
                    </span>
                    <span class="text-muted-foreground">
                      {" — "}
                      {flagged.reason}
                    </span>
                  </li>
                )}
              </For>
            </ul>
          </div>
        </Show>
        <Show when={props.report.duplicates.length > 0}>
          <div>
            <h3 class={fieldLabelClass}>Duplicates</h3>
            <ul class="mt-1 space-y-1">
              <For each={props.report.duplicates}>
                {(cluster) => (
                  <li>
                    <span class="font-medium">
                      {titles()(cluster.bucket_item_ids[0])}
                    </span>
                    <span class="text-muted-foreground">
                      {` — ${cluster.bucket_item_ids.length.toString()} items share one identity`}
                    </span>
                  </li>
                )}
              </For>
            </ul>
          </div>
        </Show>
        <Show when={props.report.stale.length > 0}>
          <div>
            <h3 class={fieldLabelClass}>Stale</h3>
            <ul class="mt-1 space-y-1">
              <For each={props.report.stale}>
                {(staleItem) => (
                  <li>
                    <span class="font-medium">
                      {titles()(staleItem.bucket_item_id)}
                    </span>
                    <span class="text-muted-foreground">
                      {` — saved ${staleItem.intent_context.age_days.toString()} days ago · "${staleItem.intent_context.intent_context}" (${Math.round(staleItem.intent_context.decay * 100).toString()}% faded)`}
                    </span>
                  </li>
                )}
              </For>
            </ul>
          </div>
        </Show>
      </div>
    </Show>
  );
}
