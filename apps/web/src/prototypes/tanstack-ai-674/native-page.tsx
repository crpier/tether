// THROWAWAY PROTOTYPE. This route is evidence for the TanStack AI deletion test.

import { useParams } from "@solidjs/router";
import type { MessagePart } from "@tanstack/ai-client";
import { fetchServerSentEvents, useChat } from "@tanstack/ai-solid";
import { For, Show, createSignal } from "solid-js";

function partText(part: MessagePart): string {
  if (part.type === "text" || part.type === "thinking") {
    return typeof part.content === "string" ? part.content : "";
  }
  if (part.type === "tool-call") {
    return `${part.name} ${JSON.stringify(part.output ?? part.input ?? null)}`;
  }
  return "";
}

export function NativeTanStackAiPrototypePage() {
  const params = useParams<{ conversationId: string }>();
  const [prompt, setPrompt] = createSignal("");
  const connection = fetchServerSentEvents("/api/prototypes/tanstack-ai/chat");
  const chat = useChat({
    connection,
    forwardedProps: { replyMode: "text" },
    persistence: true,
    threadId: params.conversationId,
    onFinish: () => {
      void connection.hydrate?.(params.conversationId).then((hydration) => {
        chat.setMessages(hydration.messages);
      });
    },
    onCustomEvent: (name, value) => {
      if (
        name !== "tether.user-message" ||
        typeof value !== "object" ||
        value === null ||
        !("messageId" in value) ||
        typeof value.messageId !== "string"
      ) {
        return;
      }
      const messageId = value.messageId;
      const current = chat.messages();
      const userIndex = current.findLastIndex(
        (message) => message.role === "user",
      );
      if (userIndex < 0) {
        return;
      }
      chat.setMessages(
        current.map((message, index) =>
          index === userIndex ? { ...message, id: messageId } : message,
        ),
      );
    },
  });

  const send = () => {
    const content = prompt();
    if (content.trim().length === 0) {
      return;
    }
    setPrompt("");
    void chat.sendMessage(content);
  };

  return (
    <section class="mx-auto flex min-h-full w-full max-w-3xl flex-col gap-4 p-6">
      <header>
        <p class="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Throwaway protocol experiment
        </p>
        <h1 class="text-xl font-semibold">TanStack AI native AG-UI path</h1>
        <p data-testid="prototype-status">
          {chat.status()} / {chat.connectionStatus()} / run{" "}
          {chat.runId() ?? "none"}
        </p>
      </header>

      <div
        aria-label="Prototype transcript"
        class="flex-1 space-y-3"
        role="log"
      >
        <For each={chat.messages()}>
          {(message) => (
            <article data-message-id={message.id} data-role={message.role}>
              <strong>{message.role}</strong>
              <For each={message.parts}>
                {(part) => <p>{partText(part)}</p>}
              </For>
            </article>
          )}
        </For>
        <For each={chat.queue()}>
          {(queued) => (
            <article data-queued-id={queued.id}>
              queued:{" "}
              {typeof queued.content === "string" ? queued.content : "media"}
            </article>
          )}
        </For>
      </div>

      <Show when={chat.error()}>
        {(error) => <p role="alert">{error().message}</p>}
      </Show>

      <div class="flex gap-2">
        <textarea
          aria-label="Prototype prompt"
          class="min-h-20 flex-1 rounded border p-2"
          onInput={(event) => setPrompt(event.currentTarget.value)}
          value={prompt()}
        />
        <button
          aria-label="Send prototype prompt"
          disabled={prompt().trim().length === 0}
          onClick={send}
          type="button"
        >
          Send
        </button>
        <button disabled={!chat.isLoading()} onClick={chat.stop} type="button">
          Stop
        </button>
      </div>
    </section>
  );
}
