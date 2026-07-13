import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { ApiError } from "../api";
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
    expect(api.answerCalls[0].selected_index).toBe(0);
    expect(
      await screen.findByText("Correct — see you next round."),
    ).toBeInTheDocument();
  });

  test("a short-answer prompt submits typed free text", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [
        duePrompt({
          kind: "short_answer",
          promptId: "018f0000-0000-7000-8000-0000000000c2",
          question: "Name the syscall behind the event loop.",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText(
      "Recall prompt: Name the syscall behind the event loop.",
    );
    fireEvent.input(within(row).getByLabelText("Your answer"), {
      target: { value: "epoll" },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Submit answer" }));

    await waitFor(() => {
      expect(api.answerCalls).toHaveLength(1);
    });
    expect(api.answerCalls[0].answer_text).toBe("epoll");
    expect(api.answerCalls[0].selected_index).toBeUndefined();
    expect(
      await screen.findByText("Correct — see you next round."),
    ).toBeInTheDocument();
  });

  test("an essay prompt proposes a grade and the human confirms it", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [
        duePrompt({
          kind: "essay",
          promptId: "018f0000-0000-7000-8000-0000000000c3",
          question: "Explain how an event loop schedules coroutines.",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText(
      "Recall prompt: Explain how an event loop schedules coroutines.",
    );
    fireEvent.input(within(row).getByLabelText("Your essay"), {
      target: { value: "Readiness polling plus cooperative yields." },
    });
    fireEvent.click(
      within(row).getByRole("button", { name: "Submit for grading" }),
    );

    await waitFor(() => {
      expect(api.proposeCalls).toHaveLength(1);
    });
    expect(api.proposeCalls[0].answerText).toBe(
      "Readiness polling plus cooperative yields.",
    );
    expect(
      within(row).getByText("Mentions readiness and cooperative yielding."),
    ).toBeInTheDocument();
    expect(
      within(row).getByText(/Model suggests: correct/),
    ).toBeInTheDocument();

    fireEvent.click(
      within(row).getByRole("button", { name: "Confirm correct" }),
    );

    await waitFor(() => {
      expect(api.answerCalls).toHaveLength(1);
    });
    expect(api.answerCalls[0].confirmed_correct).toBe(true);
    expect(api.answerCalls[0].answer_text).toBe(
      "Readiness polling plus cooperative yields.",
    );
  });

  test("the human can override the proposed essay grade", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [
        duePrompt({
          kind: "essay",
          promptId: "018f0000-0000-7000-8000-0000000000c4",
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText(
      "Recall prompt: What does async IO multiplex?",
    );
    fireEvent.input(within(row).getByLabelText("Your essay"), {
      target: { value: "A weak essay." },
    });
    fireEvent.click(
      within(row).getByRole("button", { name: "Submit for grading" }),
    );

    fireEvent.click(
      await within(row).findByRole("button", { name: "Mark incorrect" }),
    );

    await waitFor(() => {
      expect(api.answerCalls).toHaveLength(1);
    });
    expect(api.answerCalls[0].confirmed_correct).toBe(false);
  });

  test("a failed grade proposal still lets the human grade the essay", async () => {
    const api = new FakeApi({
      authenticated: true,
      duePrompts: [
        duePrompt({
          kind: "essay",
          promptId: "018f0000-0000-7000-8000-0000000000c5",
        }),
      ],
    });
    api.proposeRejections.push(new ApiError(500));
    renderApp(api);

    const row = await screen.findByLabelText(
      "Recall prompt: What does async IO multiplex?",
    );
    fireEvent.input(within(row).getByLabelText("Your essay"), {
      target: { value: "An essay the model never saw graded." },
    });
    fireEvent.click(
      within(row).getByRole("button", { name: "Submit for grading" }),
    );

    // The failure is surfaced, but the confirm/override step still renders so
    // the human can grade unaided (the proposal is advisory — ADR 0004).
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      within(row).getByText("No model proposal — grade your own essay."),
    ).toBeInTheDocument();
    fireEvent.click(
      within(row).getByRole("button", { name: "Confirm correct" }),
    );

    await waitFor(() => {
      expect(api.answerCalls).toHaveLength(1);
    });
    expect(api.answerCalls[0].confirmed_correct).toBe(true);
    expect(api.answerCalls[0].answer_text).toBe(
      "An essay the model never saw graded.",
    );
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
