import { createContext, useContext } from "solid-js";
import type { Accessor, JSX, Setter } from "solid-js";

import type { WebHost } from "./host";
import type { ChatBus, ChatFrame, ConnectionStatus } from "./chat-bus";

// The WebSocket bus lives above the router (one /ws connection app-wide) so
// invalidations and notifications flow regardless of which page is mounted.
// Every frame is exposed here — the chat page reads them via `chatFrame` for
// its own "chat" deltas and for a "messages" invalidate's local refresh-token
// bump — while `invalidate`/`notify` frames are also handled once, centrally,
// in app.tsx (query invalidation/refetch).
export interface AppRuntimeContext {
  bus: Accessor<ChatBus | undefined>;
  connection: Accessor<ConnectionStatus>;
  // The most recently received `chat`-type frame. A new object reference on
  // every dispatch (even if two frames happen to carry equal fields), so a
  // consuming effect fires for each one in turn.
  chatFrame: Accessor<ChatFrame | undefined>;
}

export interface InboxDraftContext {
  inboxCaptureDraft: Accessor<string>;
  setInboxCaptureDraft: Setter<string>;
}

export interface AppContextValue extends AppRuntimeContext, InboxDraftContext {
  host: WebHost;
}

const AppContext = createContext<AppContextValue>();

export function AppContextProvider(props: {
  children: JSX.Element;
  value: AppContextValue;
}) {
  return (
    <AppContext.Provider value={props.value}>
      {props.children}
    </AppContext.Provider>
  );
}

function contextValue(): AppContextValue {
  const value = useContext(AppContext);
  if (value === undefined) {
    throw new Error("useAppContext must be used within AppContextProvider");
  }
  return value;
}

export function useAppContext(): AppRuntimeContext {
  return contextValue();
}

export function useInboxCaptureDraft(): InboxDraftContext {
  const value = contextValue();
  return {
    inboxCaptureDraft: value.inboxCaptureDraft,
    setInboxCaptureDraft: value.setInboxCaptureDraft,
  };
}

export function useHost<K extends keyof WebHost>(domain: K): WebHost[K] {
  return contextValue().host[domain];
}
