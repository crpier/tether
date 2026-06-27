import { For } from "solid-js";
import { createStore } from "solid-js/store";

interface Todo {
  completed: boolean;
  id: number;
  text: string;
}

export const TodoList = () => {
  let input: HTMLInputElement | undefined;
  const [todos, setTodos] = createStore<Todo[]>([]);
  const addTodo = (text: string) => {
    setTodos(todos.length, { completed: false, id: todos.length, text });
  };
  const toggleTodo = (id: number) => {
    setTodos(id, "completed", (completed) => !completed);
  };

  return (
    <>
      <div>
        <input
          placeholder="new todo here"
          ref={(element) => {
            input = element;
          }}
        />
        <button
          onClick={() => {
            if (!input?.value.trim()) return;
            addTodo(input.value);
            input.value = "";
          }}
        >
          Add Todo
        </button>
      </div>
      <div>
        <For each={todos}>
          {(todo) => {
            const { id, text } = todo;
            return (
              <div>
                <input
                  type="checkbox"
                  checked={todo.completed}
                  onchange={[toggleTodo, id]}
                />
                <span
                  style={{
                    "text-decoration": todo.completed ? "line-through" : "none",
                  }}
                >
                  {text}
                </span>
              </div>
            );
          }}
        </For>
      </div>
    </>
  );
};
