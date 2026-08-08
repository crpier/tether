import type {
  Todo,
  TodoReadiness,
  TodosHost,
  TodoStatus,
} from "../../host/todos";
import { ApiError } from "../../host/error";

export class FakeTodosHost implements TodosHost {
  storedTodos: Todo[];
  listTodosCalls = 0;
  setTodoStatusCalls: {
    status: TodoStatus;
    todoId: string;
    version: number;
  }[] = [];
  serverTodoVersions: Record<string, number> = {};
  setTodoStatusRejections: ApiError[] = [];

  constructor(todos: Todo[] = []) {
    this.storedTodos = todos;
  }

  listTodos(): Promise<TodoReadiness> {
    this.listTodosCalls += 1;
    const active = this.storedTodos.filter((item) => item.status === "active");
    return Promise.resolve({
      ready: active.filter((item) => !item.waiting),
      waiting: active.filter((item) => item.waiting),
    });
  }

  setTodoStatus(
    todoId: string,
    status: TodoStatus,
    version: number,
  ): Promise<Todo> {
    this.setTodoStatusCalls.push({ status, todoId, version });
    const forced = this.setTodoStatusRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverTodoVersions[todoId];
    if (
      Object.hasOwn(this.serverTodoVersions, todoId) &&
      serverVersion !== version
    ) {
      this.storedTodos = this.storedTodos.map((item) =>
        item.id === todoId ? { ...item, version: serverVersion } : item,
      );
      return Promise.reject(new ApiError(409));
    }
    const existing = this.storedTodos.find((item) => item.id === todoId);
    if (existing === undefined) {
      return Promise.reject(new ApiError(404));
    }
    const updated = { ...existing, status, version: version + 1 };
    this.storedTodos = this.storedTodos.map((item) =>
      item.id === todoId ? updated : item,
    );
    this.serverTodoVersions[todoId] = updated.version;
    return Promise.resolve(updated);
  }
}
