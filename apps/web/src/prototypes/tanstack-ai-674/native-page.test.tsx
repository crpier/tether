import { cleanup, fireEvent, screen, waitFor } from "@solidjs/testing-library";
import { afterEach, expect, test, vi } from "vitest";

import { FakeHost, renderApp, textarea } from "../../testing/harness";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function sseResponse(events: object[]): Response {
  return new Response(
    events
      .map(
        (event, offset) =>
          `id: ${offset.toString()}\ndata: ${JSON.stringify(event)}\n\n`,
      )
      .join(""),
    { headers: { "content-type": "text/event-stream" } },
  );
}

test("native AG-UI page completes a TanStack live-to-settled turn", async () => {
  const host = new FakeHost({ authenticated: true });
  let settled = false;
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      input instanceof Request
        ? input.url
        : input instanceof URL
          ? input.href
          : input;
    if (init?.method === "POST") {
      settled = true;
      return Promise.resolve(
        sseResponse([
          {
            type: "RUN_STARTED",
            runId: "run-1",
            threadId: "conversation-1",
          },
          {
            type: "TEXT_MESSAGE_START",
            messageId: "assistant-live",
            role: "assistant",
          },
          {
            type: "TEXT_MESSAGE_CONTENT",
            messageId: "assistant-live",
            delta: "native answer",
          },
          { type: "TEXT_MESSAGE_END", messageId: "assistant-live" },
          {
            type: "RUN_FINISHED",
            runId: "run-1",
            threadId: "conversation-1",
          },
        ]),
      );
    }
    if (url.includes("threadId=conversation-1")) {
      return Promise.resolve(
        Response.json({
          activeRun: null,
          interrupts: null,
          messages: settled
            ? [
                {
                  id: "user-canonical",
                  role: "user",
                  parts: [{ type: "text", content: "hello" }],
                },
                {
                  id: "assistant-canonical",
                  role: "assistant",
                  parts: [{ type: "text", content: "native answer" }],
                },
              ]
            : [],
        }),
      );
    }
    return Promise.resolve(new Response(null, { status: 404 }));
  });
  vi.stubGlobal("fetch", fetchMock);

  renderApp(host, undefined, {
    path: "/prototypes/tanstack-ai/conversation-1",
  });

  const composer = textarea(
    await screen.findByRole("textbox", { name: "Prototype prompt" }),
  );
  fireEvent.input(composer, { target: { value: "hello" } });
  fireEvent.click(
    screen.getByRole("button", { name: "Send prototype prompt" }),
  );

  await screen.findByText("native answer");
  await waitFor(() => {
    expect(
      screen.getByText("native answer").closest("article"),
    ).toHaveAttribute("data-message-id", "assistant-canonical");
  });
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/prototypes/tanstack-ai/chat",
    expect.objectContaining({ method: "POST" }),
  );
});
