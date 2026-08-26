import type {
  MemoriesHost,
  MemoryTopic,
  MemoryWorkspaceDiagnostic,
} from "../../host/memories";

export class FakeMemoriesHost implements MemoriesHost {
  topics: MemoryTopic[];
  workspaceDiagnostics: MemoryWorkspaceDiagnostic[] = [];
  listMemoryTopicsCalls: string[] = [];
  listWorkspaceDiagnosticsCalls = 0;

  constructor(topics: MemoryTopic[] = []) {
    this.topics = topics;
  }

  listMemoryTopics(q = ""): Promise<MemoryTopic[]> {
    this.listMemoryTopicsCalls.push(q);
    const term = q.trim().toLowerCase();
    return Promise.resolve(
      term
        ? this.topics.filter((topic) =>
            `${topic.title} ${topic.body}`.toLowerCase().includes(term),
          )
        : [...this.topics],
    );
  }

  listWorkspaceDiagnostics(): Promise<MemoryWorkspaceDiagnostic[]> {
    this.listWorkspaceDiagnosticsCalls += 1;
    return Promise.resolve([...this.workspaceDiagnostics]);
  }
}
