import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { formatDate } from "../lib/format";
import { FakeApi, renderApp, todo } from "../testing/harness";

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("Todos panel", () => {
  test("lists ready todos under the Ready heading", async () => {
    const api = new FakeApi({
      authenticated: true,
      todos: [todo({ action: "call the dentist" })],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Todo: call the dentist");
    expect(row).toHaveTextContent("call the dentist");
    expect(row).toHaveTextContent("ready");
  });

  test("waiting todos show their condition and deadline", async () => {
    const api = new FakeApi({
      authenticated: true,
      todos: [
        todo({
          action: "bring the book",
          condition: "next time I visit Ana",
          deadline: "2099-01-01T09:00:00Z",
          waiting: true,
        }),
      ],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Todo: bring the book");
    expect(row).toHaveTextContent("waiting");
    expect(row).toHaveTextContent("next time I visit Ana");
    expect(row).toHaveTextContent(formatDate(new Date("2099-01-01T09:00:00Z")));
  });

  test("completing a todo calls the API with its version and status", async () => {
    const api = new FakeApi({
      authenticated: true,
      todos: [todo({ action: "water plants", id: "todo-1", version: 3 })],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Todo: water plants");
    fireEvent.click(within(row).getByRole("button", { name: "Complete" }));

    await waitFor(() => {
      expect(api.setTodoStatusCalls).toEqual([
        { status: "completed", todoId: "todo-1", version: 3 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Todo: water plants"),
      ).not.toBeInTheDocument();
    });
  });

  test("abandoning a todo transitions it to abandoned", async () => {
    const api = new FakeApi({
      authenticated: true,
      todos: [todo({ action: "old task", id: "todo-2", version: 1 })],
    });
    renderApp(api);

    const row = await screen.findByLabelText("Todo: old task");
    fireEvent.click(within(row).getByRole("button", { name: "Abandon" }));

    await waitFor(() => {
      expect(api.setTodoStatusCalls).toEqual([
        { status: "abandoned", todoId: "todo-2", version: 1 },
      ]);
    });
  });

  test("a status transition recovers from a stale-version 409 by refetching", async () => {
    const api = new FakeApi({
      authenticated: true,
      todos: [todo({ action: "water plants", id: "todo-1", version: 1 })],
    });
    api.serverTodoVersions = { "todo-1": 2 };
    renderApp(api);

    const row = await screen.findByLabelText("Todo: water plants");
    fireEvent.click(within(row).getByRole("button", { name: "Complete" }));

    await waitFor(() => {
      expect(api.setTodoStatusCalls).toEqual([
        { status: "completed", todoId: "todo-1", version: 1 },
        { status: "completed", todoId: "todo-1", version: 2 },
      ]);
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("an empty list reads as nothing to do", async () => {
    const api = new FakeApi({ authenticated: true });
    renderApp(api);

    await screen.findByRole("heading", { name: "Todos" });
    expect(screen.getByText("Nothing to do right now")).toBeInTheDocument();
  });

  test("a todos invalidate frame refetches the list", async () => {
    const api = new FakeApi({ authenticated: true });
    const bus = renderApp(api);

    await screen.findByRole("heading", { name: "Todos" });
    await waitFor(() => {
      expect(api.listTodosCalls).toBeGreaterThan(0);
    });
    const before = api.listTodosCalls;
    api.storedTodos = [todo({ action: "captured by the agent" })];
    bus.emit({ keys: ["todos"], type: "invalidate" });

    await waitFor(() => {
      expect(api.listTodosCalls).toBeGreaterThan(before);
    });
    expect(
      await screen.findByLabelText("Todo: captured by the agent"),
    ).toBeInTheDocument();
  });
});
