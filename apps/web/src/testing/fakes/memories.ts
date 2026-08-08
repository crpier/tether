import type { MemoriesHost, Memory, MemoryState } from "../../host/memories";
import { ApiError } from "../../host/error";
import { memory } from "../fixtures";

export class FakeMemoriesHost implements MemoriesHost {
  storedMemories: Memory[];
  captureMemoryCalls: string[] = [];
  editMemoryCalls: { content: string; memoryId: string; version: number }[] =
    [];
  tetherMemoryCalls: { memoryId: string; version: number }[] = [];
  rejectMemoryCalls: { memoryId: string; version: number }[] = [];
  searchMemoriesCalls: string[] = [];
  listMemoriesCalls = 0;
  serverMemoryVersions: Record<string, number> = {};
  serverMemoryEdits: Record<string, Partial<Memory>> = {};
  captureMemoryRejections: ApiError[] = [];
  editMemoryRejections: ApiError[] = [];
  tetherMemoryRejections: ApiError[] = [];
  rejectMemoryRejections: ApiError[] = [];

  constructor(memories: Memory[] = []) {
    this.storedMemories = memories;
  }

  listMemories(state: MemoryState): Promise<Memory[]> {
    this.listMemoriesCalls += 1;
    return Promise.resolve(
      this.storedMemories.filter((candidate) => candidate.state === state),
    );
  }

  searchMemories(q: string): Promise<Memory[]> {
    this.searchMemoriesCalls.push(q);
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    return Promise.resolve(
      this.storedMemories.filter(
        (candidate) =>
          candidate.state === "tethered" &&
          terms.every((term) => candidate.content.toLowerCase().includes(term)),
      ),
    );
  }

  captureMemory(content: string): Promise<Memory> {
    this.captureMemoryCalls.push(content);
    const forced = this.captureMemoryRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const captured = memory({
      content,
      id: `018f0000-0000-7000-8000-0000000002${this.captureMemoryCalls.length
        .toString()
        .padStart(2, "0")}`,
    });
    this.storedMemories = [captured, ...this.storedMemories];
    return Promise.resolve(captured);
  }

  editMemory(
    memoryId: string,
    content: string,
    version: number,
  ): Promise<Memory> {
    this.editMemoryCalls.push({ content, memoryId, version });
    return this.mutateMemory(
      memoryId,
      version,
      this.editMemoryRejections,
      (current) => ({ ...current, content, version: version + 1 }),
    );
  }

  tetherMemory(memoryId: string, version: number): Promise<Memory> {
    this.tetherMemoryCalls.push({ memoryId, version });
    return this.mutateMemory(
      memoryId,
      version,
      this.tetherMemoryRejections,
      (current) => ({
        ...current,
        state: "tethered",
        tethered_at: "2026-01-02T00:00:00Z",
        version: version + 1,
      }),
      { conflictWhen: (current) => current.state === "tethered" },
    );
  }

  rejectMemory(memoryId: string, version: number): Promise<Memory> {
    this.rejectMemoryCalls.push({ memoryId, version });
    return this.mutateMemory(
      memoryId,
      version,
      this.rejectMemoryRejections,
      (current) => ({ ...current, version: version + 1 }),
      { remove: true },
    );
  }

  private mutateMemory(
    memoryId: string,
    version: number,
    rejections: ApiError[],
    apply: (current: Memory) => Memory,
    options: {
      conflictWhen?: (current: Memory) => boolean;
      remove?: boolean;
    } = {},
  ): Promise<Memory> {
    const forced = rejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverMemoryVersions[memoryId];
    if (
      Object.hasOwn(this.serverMemoryVersions, memoryId) &&
      serverVersion !== version
    ) {
      this.storedMemories = this.storedMemories.map((existing) =>
        existing.id === memoryId
          ? {
              ...existing,
              ...this.serverMemoryEdits[memoryId],
              version: serverVersion,
            }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    const current = this.storedMemories.find(
      (existing) => existing.id === memoryId,
    );
    if (current === undefined) {
      return Promise.reject(new ApiError(404));
    }
    if (options.conflictWhen?.(current) === true) {
      return Promise.reject(new ApiError(409));
    }
    const mutated = apply(current);
    this.serverMemoryVersions[memoryId] = mutated.version;
    this.storedMemories =
      options.remove === true
        ? this.storedMemories.filter((existing) => existing.id !== memoryId)
        : this.storedMemories.map((existing) =>
            existing.id === memoryId ? mutated : existing,
          );
    return Promise.resolve(mutated);
  }
}
