// PROTOTYPE #246 — throwaway, do not ship
//
// Chat stays pure: transcript + composer full width, model selector / new
// chat in header, nothing else around it. Not variant-gated — settled shape.

import { For, Show, createSignal } from "solid-js";

import { mockChatTranscript } from "../mock-data";
import { Button } from "@/components/ui/button";
import { TextField, TextFieldTextArea } from "@/components/ui/text-field";
import { cx } from "@/lib/cva";

export function ChatPage() {
  const [draft, setDraft] = createSignal("");

  return (
    <div class="flex h-full flex-col">
      <header class="flex items-center justify-between border-b px-4 py-3">
        <select class="rounded-md border bg-background px-2 py-1 text-sm">
          <option>claude-sonnet-5</option>
          <option>gpt-5</option>
        </select>
        <Button size="sm" variant="outline">
          New chat
        </Button>
      </header>

      <div class="flex-1 overflow-y-auto px-4 py-6">
        <div class="mx-auto flex max-w-3xl flex-col gap-5">
          <For each={mockChatTranscript}>
            {(message) => (
              <div
                class={cx(
                  "flex flex-col gap-2",
                  message.role === "user" ? "items-end" : "items-start",
                )}
              >
                <div
                  class={cx(
                    "max-w-[85%] rounded-2xl px-4 py-2 text-sm",
                    message.role === "user"
                      ? "bg-primary text-primary-foreground"
                      : "bg-muted text-foreground",
                  )}
                >
                  <Show when={message.text}>{message.text}</Show>
                  <Show when={message.tableWidget}>
                    {(table) => (
                      <div class="mt-1 overflow-x-auto rounded-md border bg-background/60">
                        <table class="w-full text-left text-xs">
                          <thead>
                            <tr class="border-b">
                              <For each={table().columns}>
                                {(col) => (
                                  <th class="px-2 py-1 font-semibold">{col}</th>
                                )}
                              </For>
                            </tr>
                          </thead>
                          <tbody>
                            <For each={table().rows}>
                              {(row) => (
                                <tr class="border-b last:border-0">
                                  <For each={row}>
                                    {(cell) => (
                                      <td class="px-2 py-1">{cell}</td>
                                    )}
                                  </For>
                                </tr>
                              )}
                            </For>
                          </tbody>
                        </table>
                      </div>
                    )}
                  </Show>
                </div>
              </div>
            )}
          </For>
        </div>
      </div>

      <div class="border-t px-4 py-3">
        <div class="mx-auto flex max-w-3xl items-end gap-2">
          <TextField class="flex-1">
            <TextFieldTextArea
              onInput={(e) => setDraft(e.currentTarget.value)}
              placeholder="Message Tether…"
              value={draft()}
            />
          </TextField>
          <Button disabled={draft().length === 0}>Send</Button>
        </div>
      </div>
    </div>
  );
}
