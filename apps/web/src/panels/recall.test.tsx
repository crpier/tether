import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeApi, duePrompt, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Recall panel", () => {
  test("lists outstanding recall prompts with their choices", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [duePrompt({ question: "What does async IO multiplex?" })],
    });
    renderApp(api);

    const row = await screen.findByLabelText(
      "Recall prompt: What does async IO multiplex?",
    );
    expect(
      within(row).getByRole("button", { name: "One thread" }),
    ).toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Many threads" }),
    ).toBeInTheDocument();
  });

  test("answering a recall prompt submits the chosen option", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [
        duePrompt({
          choices: ["One thread", "Many threads"],
          promptId: "018f0000-0000-7000-8000-0000000000c9",
        }),
      ],
    });
    api.correctIndices["018f0000-0000-7000-8000-0000000000c9"] = 0;
    renderApp(api);

    const row = await screen.findByLabelText(
      "Recall prompt: What does async IO multiplex?",
    );
    fireEvent.click(within(row).getByRole("button", { name: "One thread" }));

    await waitFor(() => {
      expect(api.answerCalls).toHaveLength(1);
    });
    expect(api.answerCalls[0].promptId).toBe(
      "018f0000-0000-7000-8000-0000000000c9",
    );
    expect(api.answerCalls[0].selectedIndex).toBe(0);
    expect(
      await screen.findByText("Correct — see you next round."),
    ).toBeInTheDocument();
  });

  test("shows an empty state when no recall prompts are due", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    await screen.findByRole("heading", { name: "Tether chat" });
    expect(
      await screen.findByText("No recall prompts due"),
    ).toBeInTheDocument();
  });
});
