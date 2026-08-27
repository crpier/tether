import {
  Navigate,
  Route,
  Router,
  useLocation,
  useSearchParams,
} from "@solidjs/router";
import {
  QueryClientProvider,
  createQuery,
  useQueryClient,
} from "@tanstack/solid-query";
import { Show, createSignal, onCleanup, onMount } from "solid-js";

import { createRestHost, type WebHost } from "./host";
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
import { EvidenceInspector } from "./components/evidence-inspector";
import { LoginScreen } from "./login";
import type { BucketView } from "./panels/bucket";
import { BrowsePage, type BrowseView } from "./pages/browse-page";
import { ChatPage } from "./pages/chat-page";
import { HealthPage } from "./pages/health-page";
import { NotFoundPage } from "./pages/not-found-page";
import { SettingsPage } from "./pages/settings-page";
import { Shell } from "./shell";

export interface AppDependencies {
  host?: WebHost;
  createChatBus?: CreateChatBus;
}

type SearchValue = string | string[] | undefined;

function singleSearchValue(value: SearchValue): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function parseBucketTab(value: SearchValue): BucketView | undefined {
  const raw = singleSearchValue(value);
  return raw === "active" || raw === "history" ? raw : undefined;
}

function parseBrowseTab(value: SearchValue): BrowseView | undefined {
  const raw = singleSearchValue(value);
  return raw === "memories" ||
    raw === "dreaming" ||
    raw === "bucket" ||
    raw === "todos" ||
    raw === "reminders" ||
    raw === "feedback" ||
    raw === "panels"
    ? raw
    : undefined;
}

function RootChatRedirect() {
  const location = useLocation();
  return <Navigate href={`/chat${location.search}`} />;
}

function BrowseIndexPage() {
  const [searchParams] = useSearchParams();
  return <BrowsePage initialView={parseBrowseTab(searchParams.tab)} />;
}

function directBrowsePage(view: BrowseView, bucketView?: BucketView) {
  return function DirectBrowsePage() {
    const [searchParams] = useSearchParams();
    const queryBucketView =
      view === "bucket" ? parseBucketTab(searchParams.tab) : undefined;
    return (
      <BrowsePage
        initialBucketView={bucketView ?? queryBucketView}
        initialView={view}
      />
    );
  };
}

const BrowseMemoriesPage = directBrowsePage("memories");
const BrowseDreamingPage = directBrowsePage("dreaming");
const BrowseBucketPage = directBrowsePage("bucket");
const BrowseBucketHistoryPage = directBrowsePage("bucket", "history");
const BrowseTodosPage = directBrowsePage("todos");
const BrowseRemindersPage = directBrowsePage("reminders");
const BrowseFeedbackPage = directBrowsePage("feedback");
const BrowsePanelsPage = directBrowsePage("panels");

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
  const [evidenceUri, setEvidenceUri] = createSignal<string | null>(null);

  onMount(() => {
    const created = props.createChatBus({
      onDisconnect() {
        setChatFrame({ type: "connection", status: "closed" });
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
        // "notify" frames keep the fired-notification cache current.
        void queryClient.invalidateQueries({
          queryKey: queryKeys.notifications,
        });
        void queryClient.refetchQueries({ queryKey: queryKeys.notifications });
      },
      onStatus(status) {
        setConnection(status);
        setChatFrame({ type: "connection", status });
        if (status === "open") {
          invalidateNamedKey(queryClient, "conversations");
          invalidateNamedKey(queryClient, "messages");
        }
      },
    });
    setBus(created);
    onCleanup(() => {
      created.close();
    });
  });

  const value: AppContextValue = {
    bus,
    host: props.host,
    chatFrame,
    connection,
    openEvidence: setEvidenceUri,
  };

  return (
    <AppContextProvider value={value}>
      <Router root={Shell}>
        <Route component={RootChatRedirect} path="/" />
        <Route component={ChatPage} path="/chat" />
        <Route component={ChatPage} path="/chat/:conversationId" />
        <Route component={HealthPage} path="/health" />
        <Route component={BrowseIndexPage} path="/browse" />
        <Route component={BrowseMemoriesPage} path="/browse/memories" />
        <Route component={BrowseDreamingPage} path="/browse/dreaming" />
        <Route component={BrowseBucketPage} path="/browse/bucket" />
        <Route
          component={BrowseBucketHistoryPage}
          path="/browse/bucket/history"
        />
        <Route component={BrowseTodosPage} path="/browse/todos" />
        <Route component={BrowseRemindersPage} path="/browse/reminders" />
        <Route component={BrowseFeedbackPage} path="/browse/feedback" />
        <Route component={BrowsePanelsPage} path="/browse/panels" />
        <Route component={SettingsPage} path="/settings" />
        <Route component={NotFoundPage} path="*404" />
      </Router>
      <EvidenceInspector
        api={props.host.evidence}
        onClose={() => {
          setEvidenceUri(null);
        }}
        uri={evidenceUri()}
      />
    </AppContextProvider>
  );
}

function AppBody(props: Required<AppDependencies>) {
  const sessionQuery = createQuery(() => ({
    queryFn: () => props.host.auth.getSession(),
    queryKey: queryKeys.session,
  }));

  return (
    <Show
      fallback={<p>Loading…</p>}
      when={!sessionQuery.isLoading && sessionQuery.data}
    >
      {(session) => (
        <Show
          fallback={<LoginScreen auth={props.host.auth} />}
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
    createChatBus: props.createChatBus ?? createBrowserChatBus,
    host: props.host ?? createRestHost(),
  };
  const queryClient = makeQueryClient();

  return (
    <QueryClientProvider client={queryClient}>
      <AppBody {...dependencies} />
    </QueryClientProvider>
  );
}
