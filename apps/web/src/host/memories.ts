import type { components } from "../generated";
import { ApiError } from "./error";
import { requireData, type RestContext } from "./transport";

export type MemoryTopic = components["schemas"]["MemoryTopicRead"];

export interface MemoryWorkspaceDiagnostic {
  code: string;
  message: string;
  path: string;
}

export interface MemoriesHost {
  listMemoryTopics(q?: string): Promise<MemoryTopic[]>;
  listWorkspaceDiagnostics(): Promise<MemoryWorkspaceDiagnostic[]>;
}

export function createMemoriesHost(context: RestContext): MemoriesHost {
  return {
    async listMemoryTopics(q = "") {
      const { data, response } = await context.client.GET(
        "/api/memory-topics",
        { params: { query: { q } } },
      );
      return requireData(data, response);
    },
    async listWorkspaceDiagnostics() {
      const response = await context.fetch("/api/memory-topics/diagnostics");
      if (!response.ok) {
        throw new ApiError(response.status);
      }
      return (await response.json()) as MemoryWorkspaceDiagnostic[];
    },
  };
}
