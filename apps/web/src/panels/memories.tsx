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

import { ApiError } from "../api";
import type { Memory, MemoryState, TetherApi } from "../api";
import { formatDate as formatDateOnly } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
  TextFieldTextArea,
} from "@/components/ui/text-field";

// The two faces of the Review gate: the loose queue awaiting human review, and
// the tethered corpus the human has already vetted.
type MemoriesView = "review" | "corpus";

// Long enough to coalesce a typing burst into one request, short enough that
// results still feel immediate against the local single-tenant host.
const SEARCH_DEBOUNCE_MS = 150;

// Row labels feed the accessibility tree (and test selectors), so cap them:
// enough of a free-form memory to identify the row, without turning the
// label into a paragraph.
const LABEL_MAX_CHARS = 80;

function memoryLabel(content: string): string {
  return content.length <= LABEL_MAX_CHARS
    ? `Memory: ${content}`
    : `Memory: ${content.slice(0, LABEL_MAX_CHARS)}…`;
}

interface Editing {
  // The content the edit was formulated against; a 409 whose fresh row still
  // matches this basis is a mere version bump (e.g. a tether) and safe to
  // retry, while a changed basis is a genuine concurrent edit.
  basisContent: string;
  id: string;
  // Which state list the row lived in when the edit began, so conflict
  // recovery starts its refetch there (falling back to the other list if a
  // concurrent tether moved the row).
  state: MemoryState;
  version: number;
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : formatDateOnly(parsed);
}

export function MemoriesPanel(props: {
  api: TetherApi;
  // Restricts which sub-view is shown and hides the toggle entirely when set
  // (#250): the Inbox page only ever wants the review queue, and the Browse
  // page only ever wants the corpus, so callers pin the view rather than
  // relying on the user finding the right tab.
  initialView?: MemoriesView;
}) {
  const queryClient = useQueryClient();
  const [view, setView] = createSignal<MemoriesView>(
    props.initialView ?? "review",
  );
  const fixedView = props.initialView !== undefined;
  const [captureContent, setCaptureContent] = createSignal("");
  const [search, setSearch] = createSignal("");
  const [debouncedSearch, setDebouncedSearch] = createSignal("");
  const [error, setError] = createSignal<string | undefined>();
  const [editing, setEditing] = createSignal<Editing | undefined>();
  const [draft, setDraft] = createSignal("");

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

  const searchTerm = createMemo(() => debouncedSearch().trim());

  const looseQuery = createQuery(() => ({
    queryFn: () => props.api.listMemories("loose"),
    queryKey: queryKeys.memoriesState("loose"),
  }));
  const tetheredQuery = createQuery(() => ({
    enabled: view() === "corpus",
    queryFn: () => props.api.listMemories("tethered"),
    queryKey: queryKeys.memoriesState("tethered"),
  }));
  const searchQuery = createQuery(() => {
    const term = searchTerm();
    return {
      enabled: view() === "corpus" && term.length > 0,
      // Guard the blank term even though the query is disabled then: the WS
      // invalidate frame (`invalidateNamedKey`) refetches every cached
      // "memories" query, including the empty-term cache entry this disabled
      // query registers, and the host 400s a blank search.
      queryFn: () =>
        term.length === 0
          ? Promise.resolve<Memory[]>([])
          : props.api.searchMemories(term),
      queryKey: queryKeys.memoriesSearch(term),
    };
  });

  const corpusItems = createMemo(() =>
    searchTerm().length > 0
      ? (searchQuery.data ?? [])
      : (tetheredQuery.data ?? []),
  );

  const refresh = () => {
    // Mark every memories query stale but only refetch what is on screen now.
    // A broad refetch would fan out to the disabled corpus queries and to one
    // cache entry per previously typed search term; those refetch when their
    // view or term is next looked at.
    void queryClient.invalidateQueries({
      queryKey: queryKeys.memories,
      refetchType: "none",
    });
    void queryClient.refetchQueries({
      queryKey: queryKeys.memoriesState("loose"),
    });
    if (view() === "corpus") {
      void queryClient.refetchQueries({
        queryKey: queryKeys.memoriesState("tethered"),
      });
      const term = searchTerm();
      if (term.length > 0) {
        void queryClient.refetchQueries({
          queryKey: queryKeys.memoriesSearch(term),
        });
      }
    }
  };

  const capture = () => {
    setError(undefined);
    const content = captureContent().trim();
    if (content.length === 0) {
      setError("Write something to capture");
      return;
    }
    void (async () => {
      try {
        await props.api.captureMemory(content);
        setCaptureContent("");
        refresh();
      } catch (caught) {
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not capture the memory",
        );
      }
    })();
  };

  const act = (item: Memory, action: "tether" | "reject") => {
    void (async () => {
      setError(undefined);
      try {
        await call(action, item.id, item.version);
        refresh();
      } catch (caught) {
        // Same stale-version race as the bucket panel: the agent (or another
        // tab) touched the memory after we loaded the row, so the server's
        // version moved on. Refetch and retry once with the fresh version
        // instead of dead-ending on a bare 409.
        if (caught instanceof ApiError && caught.status === 409) {
          setError(await retryWithFreshVersion(item, action));
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : `Could not ${action} the memory`,
        );
      }
    })();
  };

  const call = (
    action: "tether" | "reject",
    memoryId: string,
    version: number,
  ) =>
    action === "tether"
      ? props.api.tetherMemory(memoryId, version)
      : props.api.rejectMemory(memoryId, version);

  // Refetch the row (following it across state lists), then retry once with
  // the current version. Returns undefined when the memory is now settled
  // (acted on here, gone server-side, or already tethered as-is), or the
  // retry's own error message to show.
  const retryWithFreshVersion = async (
    item: Memory,
    action: "tether" | "reject",
  ): Promise<string | undefined> => {
    const fresh = await refetchOne(item.state, item.id);
    if (fresh === undefined) {
      refresh();
      return undefined;
    }
    if (action === "tether") {
      // Tethering is the Review gate's vouch, so never retry over content the
      // user has not seen — surface the conflict and let them re-confirm the
      // refreshed row. Reject stays blind: rejecting changed content is safe.
      if (fresh.content !== item.content) {
        refresh();
        return "The memory changed before it could be tethered — review it and tether again";
      }
      if (fresh.state === "tethered") {
        // Concurrently tethered with exactly the content the user vouched
        // for: the intent is already done, and the host would 409 a re-tether.
        refresh();
        return undefined;
      }
    }
    try {
      await call(action, item.id, fresh.version);
      refresh();
      return undefined;
    } catch (retryCaught) {
      return retryCaught instanceof Error
        ? retryCaught.message
        : `Could not ${action} the memory`;
    }
  };

  // Find a memory's fresh row after a 409. A host-side tether MOVES a row
  // loose -> tethered while bumping its version, so a miss in the list the
  // row came from does not mean it settled — fall back to the other list
  // before concluding the memory is gone.
  const refetchOne = async (
    state: MemoryState,
    memoryId: string,
  ): Promise<Memory | undefined> => {
    const own = (await fetchStateList(state)).find(
      (candidate) => candidate.id === memoryId,
    );
    if (own !== undefined) {
      return own;
    }
    const other: MemoryState = state === "loose" ? "tethered" : "loose";
    return (await fetchStateList(other)).find(
      (candidate) => candidate.id === memoryId,
    );
  };

  // fetchQuery rather than refetchQueries: it fetches even when no observer
  // is mounted (the tethered list is disabled while the review view is up)
  // and still lands in the cache the on-screen queries read.
  const fetchStateList = (state: MemoryState) =>
    queryClient.fetchQuery({
      queryFn: () => props.api.listMemories(state),
      queryKey: queryKeys.memoriesState(state),
    });

  const startEdit = (item: Memory) => {
    setError(undefined);
    setEditing({
      basisContent: item.content,
      id: item.id,
      state: item.state,
      version: item.version,
    });
    setDraft(item.content);
  };

  const cancelEdit = () => {
    setEditing(undefined);
    setDraft("");
  };

  const saveEdit = () => {
    const current = editing();
    if (current === undefined) {
      return;
    }
    setError(undefined);
    const content = draft().trim();
    if (content.length === 0) {
      setError("A memory needs content — reject it instead of blanking it");
      return;
    }
    void (async () => {
      try {
        await props.api.editMemory(current.id, content, current.version);
        cancelEdit();
        refresh();
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409) {
          await recoverEditConflict(current, content);
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not save the memory",
        );
      }
    })();
  };

  // A 409 on save: refetch and compare the fresh content with the basis the
  // edit was formulated against. A mere version bump (e.g. the memory was
  // tethered meanwhile) is retried transparently; a genuine concurrent content
  // change re-arms the editor (keeping the draft) so a second Save, after
  // reviewing the refreshed row, deliberately wins.
  const recoverEditConflict = async (current: Editing, content: string) => {
    const fresh = await refetchOne(current.state, current.id);
    if (fresh === undefined) {
      cancelEdit();
      refresh();
      return;
    }
    if (fresh.content === current.basisContent) {
      try {
        await props.api.editMemory(current.id, content, fresh.version);
        cancelEdit();
        refresh();
      } catch (retryCaught) {
        setError(
          retryCaught instanceof Error
            ? retryCaught.message
            : "Could not save the memory",
        );
      }
      return;
    }
    setEditing({
      ...current,
      basisContent: fresh.content,
      version: fresh.version,
    });
    setError(
      "The memory changed while you were editing — save again to overwrite it",
    );
  };

  const onCaptureSubmit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (
    event,
  ) => {
    event.preventDefault();
    capture();
  };

  const editorRow = () => (
    <div class="space-y-2">
      <TextField onChange={setDraft} value={draft()}>
        <TextFieldLabel>Memory content</TextFieldLabel>
        <TextFieldTextArea name="content" />
      </TextField>
      <div class="flex justify-end gap-2">
        <Button onClick={saveEdit} size="sm" type="button">
          Save
        </Button>
        <Button onClick={cancelEdit} size="sm" type="button" variant="ghost">
          Cancel
        </Button>
      </div>
    </div>
  );

  // Rows keep their actions right-aligned so a future bulk-select checkbox
  // column can slot in on the left without reshuffling the layout.
  const memoryRow = (item: Memory) => (
    <li
      aria-label={memoryLabel(item.content)}
      class="bg-muted rounded-md border px-3 py-2 text-sm"
    >
      <Show fallback={editorRow()} when={editing()?.id !== item.id}>
        <p>{item.content}</p>
        <div class="mt-1 flex flex-wrap items-center gap-1">
          <span class="text-muted-foreground text-xs">
            {item.state === "tethered"
              ? `tethered ${formatDate(item.tethered_at ?? item.updated_at)}`
              : `captured ${formatDate(item.created_at)}`}
          </span>
          <Show when={item.state === "loose"}>
            <Button
              class="ml-auto"
              onClick={() => {
                act(item, "tether");
              }}
              size="sm"
              type="button"
              variant="ghost"
            >
              Tether
            </Button>
          </Show>
          <Button
            class={item.state === "loose" ? "" : "ml-auto"}
            onClick={() => {
              startEdit(item);
            }}
            size="sm"
            type="button"
            variant="ghost"
          >
            Edit
          </Button>
          <Button
            onClick={() => {
              act(item, "reject");
            }}
            size="sm"
            type="button"
            variant="ghost"
          >
            Reject
          </Button>
        </div>
      </Show>
    </li>
  );

  return (
    <section aria-label="Memories" class={panelClass}>
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold">Memories</h2>
        <Show when={!fixedView}>
          <div class="flex gap-1" role="group" aria-label="Memories view">
            <For each={["review", "corpus"] as const}>
              {(candidate) => (
                <Button
                  aria-pressed={view() === candidate}
                  onClick={() => {
                    setView(candidate);
                  }}
                  size="sm"
                  type="button"
                  variant={view() === candidate ? "secondary" : "ghost"}
                >
                  {candidate === "review" ? "Review" : "Corpus"}
                </Button>
              )}
            </For>
          </div>
        </Show>
      </div>
      <Switch>
        <Match when={view() === "review"}>
          <form class="space-y-3" onSubmit={onCaptureSubmit}>
            <TextField onChange={setCaptureContent} value={captureContent()}>
              <TextFieldLabel>Capture</TextFieldLabel>
              <TextFieldInput name="capture" />
            </TextField>
            <Button type="submit">Capture memory</Button>
          </form>
          <Show
            fallback={
              <p class="text-muted-foreground mt-3 text-sm">
                Review queue is clear
              </p>
            }
            when={(looseQuery.data ?? []).length > 0}
          >
            <ul class="mt-3 space-y-2">
              <For each={looseQuery.data ?? []}>{memoryRow}</For>
            </ul>
          </Show>
        </Match>
        <Match when={view() === "corpus"}>
          <TextField onChange={onSearchInput} value={search()}>
            <TextFieldLabel>Search memories</TextFieldLabel>
            <TextFieldInput name="search" type="search" />
          </TextField>
          <Show
            fallback={
              <p class="text-muted-foreground mt-3 text-sm">
                {searchTerm().length > 0
                  ? "No matches"
                  : "No tethered memories yet"}
              </p>
            }
            when={corpusItems().length > 0}
          >
            <ul class="mt-3 space-y-2">
              <For each={corpusItems()}>{memoryRow}</For>
            </ul>
          </Show>
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
