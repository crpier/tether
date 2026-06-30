export type ChatFrame =
  | {
      type: "chat";
      conversation_id?: string;
      event?: string;
      delta?: unknown;
      detail?: string;
      message_id?: string;
      seq?: number;
      tool_name?: string | null;
      tool_id?: string | null;
      tool_args?: unknown;
      tool_result?: unknown;
      content_index?: number | null;
    }
  | { type: "invalidate"; keys: string[] }
  | {
      type: "notify";
      trigger_id: string;
      title?: string | null;
      body: string;
    };

export type ConnectionStatus = "connecting" | "open" | "closed";

export interface ChatBusHandlers {
  onDisconnect(): void;
  onFrame(frame: ChatFrame): void;
  onStatus?(status: ConnectionStatus): void;
}

const INITIAL_RETRY_MS = 500;
const MAX_RETRY_MS = 16_000;

export interface ChatBus {
  abort(conversationId: string): void;
  close(): void;
  sendPrompt(conversationId: string, content: string): void;
}

export type CreateChatBus = (handlers: ChatBusHandlers) => ChatBus;

function parseFrame(data: string): ChatFrame | null {
  const parsed: unknown = JSON.parse(data);
  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }
  const candidate = parsed as Partial<ChatFrame>;
  if (
    candidate.type === "chat" ||
    candidate.type === "invalidate" ||
    candidate.type === "notify"
  ) {
    return candidate as ChatFrame;
  }
  return null;
}

export const createBrowserChatBus: CreateChatBus = (handlers) => {
  let closed = false;
  let socket: WebSocket | undefined;
  let retryDelay = INITIAL_RETRY_MS;
  let retryTimer: number | undefined;
  const queuedFrames: string[] = [];

  const sendSerialized = (serialized: string) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(serialized);
      return;
    }
    queuedFrames.push(serialized);
  };

  const connect = () => {
    handlers.onStatus?.("connecting");
    socket = new WebSocket("/ws");
    socket.addEventListener("open", () => {
      // A clean open resets the backoff, so the next disconnect retries fast.
      retryDelay = INITIAL_RETRY_MS;
      handlers.onStatus?.("open");
      while (queuedFrames.length > 0 && socket?.readyState === WebSocket.OPEN) {
        const serialized = queuedFrames.shift();
        if (serialized !== undefined) {
          socket.send(serialized);
        }
      }
    });
    socket.addEventListener("message", (event) => {
      if (typeof event.data !== "string") {
        return;
      }
      const frame = parseFrame(event.data);
      if (frame !== null) {
        handlers.onFrame(frame);
      }
    });
    socket.addEventListener("close", () => {
      if (closed) {
        return;
      }
      handlers.onStatus?.("closed");
      handlers.onDisconnect();
      // Exponential backoff capped at MAX_RETRY_MS so a server that stays down
      // does not get hammered once a second forever.
      const delay = retryDelay;
      retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS);
      retryTimer = window.setTimeout(connect, delay);
    });
  };

  connect();

  return {
    abort(conversationId) {
      sendSerialized(
        JSON.stringify({ conversation_id: conversationId, type: "abort" }),
      );
    },
    close() {
      closed = true;
      if (retryTimer !== undefined) {
        window.clearTimeout(retryTimer);
      }
      socket?.close();
    },
    sendPrompt(conversationId, content) {
      sendSerialized(
        JSON.stringify({
          content,
          conversation_id: conversationId,
          type: "prompt",
        }),
      );
    },
  };
};
