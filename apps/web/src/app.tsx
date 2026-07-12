import { QueryClientProvider, createQuery } from "@tanstack/solid-query";
import { Show } from "solid-js";

import { createRestApi } from "./api";
import type { TetherApi } from "./api";
import { createBrowserChatBus } from "./chat-bus";
import type { CreateChatBus } from "./chat-bus";
import { ChatView } from "./chat-view";
import { makeQueryClient, queryKeys } from "./lib/query-keys";
import { LoginScreen } from "./login";

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
