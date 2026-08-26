import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type Todo = components["schemas"]["TodoRead"];
export type TodoStatus = components["schemas"]["TodoStatus"];
export type TodoReadiness = components["schemas"]["TodoReadinessRead"];

export interface TodosHost {
  listTodos(): Promise<TodoReadiness>;
  setTodoStatus(
    todoId: string,
    status: TodoStatus,
    version: number,
  ): Promise<Todo>;
}

export function createTodosHost(context: RestContext): TodosHost {
  return {
    async listTodos() {
      const { data, response } = await context.client.GET("/api/todos");
      return requireData(data, response);
    },
    async setTodoStatus(todoId, status, version) {
      const { data, response } = await context.client.POST(
        "/api/todos/{todo_id}/status",
        {
          body: { status, version },
          params: { path: { todo_id: todoId } },
        },
      );
      return requireData(data, response);
    },
  };
}
