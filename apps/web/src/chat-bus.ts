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
    }
  | { type: "invalidate"; keys: string[] }
  | {
      type: "notify";
      trigger_id: string;
      title?: string | null;
      body: string;
    };

export interface ChatBusHandlers {
  onDisconnect(): void;
  onFrame(frame: ChatFrame): void;
}

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
  const queuedFrames: string[] = [];

  const sendSerialized = (serialized: string) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(serialized);
      return;
    }
    queuedFrames.push(serialized);
  };

  const connect = () => {
    socket = new WebSocket("/ws");
    socket.addEventListener("open", () => {
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
      handlers.onDisconnect();
      window.setTimeout(connect, 1_000);
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
