import type { components } from "../generated";
import { ApiError } from "./error";
import { requireData, type RestContext } from "./transport";

export type Memory = components["schemas"]["MemoryRead"];
export type MemoryState = components["schemas"]["MemoryState"];

export interface MemoryWorkspaceDiagnostic {
  code: string;
  message: string;
  path: string;
}

export interface MemoriesHost {
  listMemories(state: MemoryState): Promise<Memory[]>;
  listWorkspaceDiagnostics(): Promise<MemoryWorkspaceDiagnostic[]>;
  searchMemories(q: string): Promise<Memory[]>;
  captureMemory(content: string): Promise<Memory>;
  editMemory(
    memoryId: string,
    content: string,
    version: number,
  ): Promise<Memory>;
  tetherMemory(memoryId: string, version: number): Promise<Memory>;
  rejectMemory(memoryId: string, version: number): Promise<Memory>;
}

export function createMemoriesHost(context: RestContext): MemoriesHost {
  return {
    async listMemories(state) {
      const { data, response } = await context.client.GET("/api/memories", {
        params: { query: { state } },
      });
      return requireData(data, response);
    },
    async listWorkspaceDiagnostics() {
      const response = await context.fetch(
        "/api/memories/workspace-diagnostics",
      );
      if (!response.ok) {
        throw new ApiError(response.status);
      }
      return (await response.json()) as MemoryWorkspaceDiagnostic[];
    },
    async searchMemories(q) {
      const { data, response } = await context.client.GET(
        "/api/memories/search",
        { params: { query: { q } } },
      );
      return requireData(data, response);
    },
    async captureMemory(content) {
      const { data, response } = await context.client.POST("/api/memories", {
        body: { content },
      });
      return requireData(data, response);
    },
    async editMemory(memoryId, content, version) {
      const { data, response } = await context.client.PATCH(
        "/api/memories/{memory_id}",
        {
          body: { content, version },
          params: { path: { memory_id: memoryId } },
        },
      );
      return requireData(data, response);
    },
    async tetherMemory(memoryId, version) {
      const { data, response } = await context.client.POST(
        "/api/memories/{memory_id}/tether",
        {
          body: { version },
          params: { path: { memory_id: memoryId } },
        },
      );
      return requireData(data, response);
    },
    async rejectMemory(memoryId, version) {
      const { data, response } = await context.client.DELETE(
        "/api/memories/{memory_id}",
        {
          params: { path: { memory_id: memoryId }, query: { version } },
        },
      );
      return requireData(data, response);
    },
  };
}
