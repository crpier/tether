import { Navigate, Route, Router } from "@solidjs/router";
import {
  QueryClientProvider,
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import { Show, createSignal, onCleanup, onMount } from "solid-js";

import { createRestApi } from "./api";
import type { TetherApi } from "./api";
import type { AppContextValue } from "./app-context";
import { AppContextProvider } from "./app-context";
import { createBrowserChatBus } from "./chat-bus";
import type {
  ChatBus,
  ChatFrame,
  ConnectionStatus,
  CreateChatBus,
} from "./chat-bus";
import {
  makeQueryClient,
  queryKeys,
  invalidateNamedKey,
} from "./lib/query-keys";
import { LoginScreen } from "./login";
import { BrowsePage } from "./pages/browse-page";
import { ChatPage } from "./pages/chat-page";
import { InboxPage } from "./pages/inbox-page";
import { ProposalsPage } from "./pages/proposals-page";
import { SettingsPage } from "./pages/settings-page";
import { Shell } from "./shell";

export interface AppDependencies {
  api?: TetherApi;
  createChatBus?: CreateChatBus;
}

// The WebSocket bus and frame handling live above the router, beside the
// session gate (#250): one /ws connection app-wide, so `invalidate` and
// `notify` frames flow regardless of which page is mounted. Only created once
// a session is confirmed authenticated.
function ConnectedApp(props: Required<AppDependencies>) {
  const queryClient = useQueryClient();
  const [connection, setConnection] =
    createSignal<ConnectionStatus>("connecting");
  const [chatFrame, setChatFrame] = createSignal<ChatFrame | undefined>();
  const [bus, setBus] = createSignal<ChatBus | undefined>();

  onMount(() => {
    const created = props.createChatBus({
      onDisconnect() {
        // Reconnection/backoff is handled inside the bus itself; nothing to
        // do here beyond the status transition onStatus already reports.
      },
      onFrame(frame) {
        // Every frame is also handed to the chat page (via `chatFrame`) so it
        // can react to the ones it cares about — its own `chat`-type deltas,
        // and an `invalidate` naming "messages", which needs a local
        // refresh-token bump alongside the query refetch below (a settled
        // history page can otherwise land on an already-active query that a
        // bare `refetchQueries` does not reliably re-run).
        setChatFrame(frame);
        if (frame.type === "chat") {
          return;
        }
        if (frame.type === "invalidate") {
          for (const key of frame.keys) {
            invalidateNamedKey(queryClient, key);
          }
          return;
        }
        // "notify" frames (a fired trigger) invalidate the notifications
        // query directly — the Inbox's fired-reminders section reads it.
        void queryClient.invalidateQueries({
          queryKey: queryKeys.notifications,
        });
        void queryClient.refetchQueries({ queryKey: queryKeys.notifications });
      },
      onStatus(status) {
        setConnection(status);
      },
    });
    setBus(created);
    onCleanup(() => {
      created.close();
    });
  });

  const value: AppContextValue = {
    api: props.api,
    bus,
    chatFrame,
    connection,
  };

  return (
    <AppContextProvider value={value}>
      <Router root={Shell}>
        <Route component={ChatPage} path="/" />
        <Route component={ProposalsPage} path="/proposals" />
        <Route component={InboxPage} path="/inbox" />
        <Route component={BrowsePage} path="/browse" />
        <Route component={SettingsPage} path="/settings" />
        <Route component={() => <Navigate href="/" />} path="*404" />
      </Router>
    </AppContextProvider>
  );
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
          <ConnectedApp {...props} />
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
