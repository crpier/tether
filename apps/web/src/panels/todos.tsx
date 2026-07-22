import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Show, createMemo, createSignal } from "solid-js";

import { ApiError } from "../api";
import type { TetherApi, Todo, TodoStatus } from "../api";
import { formatDate as formatDateOnly } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

// Todos are chat-authored; the panel only reads the ready/waiting split and
// transitions status (complete/abandon), mirroring the Project panel design.

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : formatDateOnly(parsed);
}

function waitingDetail(todo: Todo): string | undefined {
  const parts: string[] = [];
  if (todo.condition) {
    parts.push(todo.condition);
  }
  if (todo.deadline) {
    parts.push(`by ${formatDate(todo.deadline)}`);
  }
  return parts.length > 0 ? parts.join(" · ") : undefined;
}

export function TodosPanel(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const [error, setError] = createSignal<string | undefined>();

  const todosQuery = createQuery(() => ({
    queryFn: () => props.api.listTodos(),
    queryKey: queryKeys.todos,
  }));

  const ready = createMemo(() => todosQuery.data?.ready ?? []);
  const waiting = createMemo(() => todosQuery.data?.waiting ?? []);
  const isEmpty = createMemo(
    () => ready().length === 0 && waiting().length === 0,
  );

  const refresh = () => {
    void queryClient.refetchQueries({ queryKey: queryKeys.todos });
  };

  const act = (todo: Todo, status: TodoStatus) => {
    void (async () => {
      setError(undefined);
      try {
        await props.api.setTodoStatus(todo.id, status, todo.version);
        refresh();
      } catch (caught) {
        // Same stale-version race as the other panels: the agent (or another
        // tab) settled the todo after we loaded the row, so its version moved
        // on. Refetch and retry once with the fresh version.
        if (caught instanceof ApiError && caught.status === 409) {
          setError(await retryWithFreshVersion(todo.id, status));
          return;
        }
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not update the todo",
        );
      }
    })();
  };

  const retryWithFreshVersion = async (
    todoId: string,
    status: TodoStatus,
  ): Promise<string | undefined> => {
    await queryClient.refetchQueries({ queryKey: queryKeys.todos });
    const readiness = queryClient.getQueryData<{
      ready: Todo[];
      waiting: Todo[];
    }>(queryKeys.todos);
    const fresh = [
      ...(readiness?.ready ?? []),
      ...(readiness?.waiting ?? []),
    ].find((candidate) => candidate.id === todoId);
    if (fresh === undefined) {
      refresh();
      return undefined;
    }
    try {
      await props.api.setTodoStatus(todoId, status, fresh.version);
      refresh();
      return undefined;
    } catch (retryCaught) {
      return retryCaught instanceof Error
        ? retryCaught.message
        : "Could not update the todo";
    }
  };

  return (
    <section aria-label="Todos" class={panelClass}>
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold">Todos</h2>
      </div>
      <Show
        fallback={
          <p class="text-muted-foreground text-sm">Nothing to do right now</p>
        }
        when={!isEmpty()}
      >
        <Show when={ready().length > 0}>
          <div>
            <h3 class="text-muted-foreground text-xs font-medium">Ready</h3>
            <ul class="mt-1 space-y-2">
              <For each={ready()}>
                {(todo) => <TodoRow todo={todo} onAct={act} variant="ready" />}
              </For>
            </ul>
          </div>
        </Show>
        <Show when={waiting().length > 0}>
          <div class="mt-3">
            <h3 class="text-muted-foreground text-xs font-medium">Waiting</h3>
            <ul class="mt-1 space-y-2">
              <For each={waiting()}>
                {(todo) => (
                  <TodoRow todo={todo} onAct={act} variant="waiting" />
                )}
              </For>
            </ul>
          </div>
        </Show>
      </Show>
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

function TodoRow(props: {
  todo: Todo;
  variant: "ready" | "waiting";
  onAct: (todo: Todo, status: TodoStatus) => void;
}) {
  const detail = createMemo(() => waitingDetail(props.todo));
  return (
    <li
      aria-label={`Todo: ${props.todo.action}`}
      class="bg-muted rounded-md border px-3 py-2 text-sm"
    >
      <div class="flex flex-wrap items-center gap-1">
        <span class="font-medium">{props.todo.action}</span>
        <Badge variant={props.variant === "ready" ? "secondary" : "outline"}>
          {props.variant}
        </Badge>
        <Button
          class="ml-auto"
          onClick={() => {
            props.onAct(props.todo, "completed");
          }}
          size="sm"
          type="button"
          variant="ghost"
        >
          Complete
        </Button>
        <Button
          onClick={() => {
            props.onAct(props.todo, "abandoned");
          }}
          size="sm"
          type="button"
          variant="ghost"
        >
          Abandon
        </Button>
      </div>
      <Show when={detail()}>
        {(text) => (
          <p class="text-muted-foreground mt-0.5 text-xs italic">{text()}</p>
        )}
      </Show>
    </li>
  );
}
