import { cleanup, fireEvent, render, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { TodoList } from "./todo-list";

function inputElement(element: HTMLElement): HTMLInputElement {
  if (!(element instanceof HTMLInputElement)) {
    throw new Error("expected input element");
  }
  return element;
}

afterEach(cleanup);

describe("<TodoList />", () => {
  test("it will render an text input and a button", () => {
    render(() => <TodoList />);

    expect(screen.getByPlaceholderText("new todo here")).toBeInTheDocument();
    expect(screen.getByText("Add Todo")).toBeInTheDocument();
  });

  test("it will add a new todo", () => {
    render(() => <TodoList />);
    const input = inputElement(screen.getByPlaceholderText("new todo here"));

    input.value = "test new todo";
    fireEvent.click(screen.getByText("Add Todo"));

    expect(input.value).toBe("");
    expect(screen.getByText(/test new todo/)).toBeInTheDocument();
  });

  test("it will mark a todo as completed", async () => {
    render(() => <TodoList />);
    const input = inputElement(screen.getByPlaceholderText("new todo here"));
    input.value = "mark new todo as completed";
    fireEvent.click(screen.getByText("Add Todo"));

    const completed = inputElement(await screen.findByRole("checkbox"));
    expect(completed.checked).toBe(false);
    fireEvent.click(completed);

    expect(completed.checked).toBe(true);
    expect(screen.getByText("mark new todo as completed")).toHaveStyle({
      "text-decoration": "line-through",
    });
  });
});
