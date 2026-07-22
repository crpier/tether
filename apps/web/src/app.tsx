import { QueryClientProvider, createQuery } from "@tanstack/solid-query";
import { Show } from "solid-js";

import { createRestApi } from "./api";
import type { TetherApi } from "./api";
import { createBrowserChatBus } from "./chat-bus";
import type { CreateChatBus } from "./chat-bus";
import { ChatView } from "./chat-view";
import { makeQueryClient, queryKeys } from "./lib/query-keys";
import { LoginScreen } from "./login";
import { PrototypeShell } from "./prototype-246/shell";

export interface AppDependencies {
  api?: TetherApi;
  createChatBus?: CreateChatBus;
}

function AppBody(props: Required<AppDependencies>) {
  const sessionQuery = createQuery(() => ({
    queryFn: () => props.api.getSession(),
    queryKey: queryKeys.session,
  }));

  return (
    <Show
      fallback={<p>Loading…</p>}
      when={!sessionQuery.isLoading && sessionQuery.data}
    >
      {(session) => (
        <Show
          fallback={<LoginScreen api={props.api} />}
          when={session().authenticated}
        >
          <ChatView api={props.api} createChatBus={props.createChatBus} />
        </Show>
      )}
    </Show>
  );
}

export function App(props: AppDependencies = {}) {
  // PROTOTYPE #246 — throwaway, do not ship. Dev-only escape hatch to render
  // the #246 UI-shell prototype instead of the real app; skips auth entirely
  // since the prototype is all mock data. See src/prototype-246/.
  if (
    import.meta.env.DEV &&
    new URLSearchParams(location.search).get("prototype") === "shell"
  ) {
    return <PrototypeShell />;
  }

  const dependencies: Required<AppDependencies> = {
    api: props.api ?? createRestApi(),
    createChatBus: props.createChatBus ?? createBrowserChatBus,
  };
  const queryClient = makeQueryClient();

  return (
    <QueryClientProvider client={queryClient}>
      <AppBody {...dependencies} />
    </QueryClientProvider>
  );
}
