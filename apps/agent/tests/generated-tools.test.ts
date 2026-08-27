import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { describe, expect, test } from "vitest";

import { createCodeModeTool } from "../src/code-mode.js";
import tetherToolsExtension, {
  tetherToolSources,
} from "../src/generated/index.js";

interface RegisteredToolSummary {
  name: string;
  parameters: unknown;
}

describe("generated tool extension", () => {
  test("registers all generated Tether tools", () => {
    const registeredTools: RegisteredToolSummary[] = [];
    const pi = {
      registerTool(tool: RegisteredToolSummary): void {
        registeredTools.push({ name: tool.name, parameters: tool.parameters });
      },
    } as unknown as ExtensionAPI;

    tetherToolsExtension(pi);

    expect(registeredTools.map((tool) => tool.name)).toEqual([
      "search",
      "queue_memory_assimilation",
      "add_movie",
      "add_place",
      "add_book",
      "add_travel",
      "add_purchase",
      "complete_bucket_item",
      "delete_bucket_item",
      "search_bucket_items",
      "set_purchase_decision",
      "set_bucket_item_intent",
      "create_todo",
      "set_todo_status",
      "link_todo_trigger",
      "list_todos",
      "triage_report",
      "browse_youtube",
      "search_youtube",
      "summarize_youtube_activity",
      "fetch_youtube_transcript",
      "ignore_youtube_video",
      "retry_youtube_video",
      "web_search",
      "archive_gmail_message",
      "search_gmail",
      "read_gmail_message",
      "list_gmail_labels",
      "trash_gmail_message",
      "update_gmail_labels",
      "create_trigger",
      "list_triggers",
      "delete_trigger",
      "start_recall",
      "list_due_recall_prompts",
      "answer_recall_prompt",
      "propose_essay_grade",
      "read_conversation_history",
      "create_artifact",
      "update_artifact",
      "list_artifact_events",
      "create_panel",
      "list_panels",
      "update_panel",
      "delete_panel",
      "label_ebook",
      "match_ebook_filename",
      "list_unlabeled_ebooks",
      "analyze_health_connect",
      "health_connect_inventory",
      "query_health_connect",
      "summarize_health_connect",
      "record_product_observation",
      "list_product_observations",
      "execute_tools",
    ]);
    expect(tetherToolSources).toHaveLength(54);
  });

  test("makes every generated schema discoverable by confined code", async () => {
    const tool = createCodeModeTool(tetherToolSources);

    const execution = await tool.execute(
      "catalog-call",
      { code: 'return search({ query: "create_todo" })' },
      undefined,
      undefined,
      {} as ExtensionContext,
    );
    const content = execution.content[0];

    expect(content.type).toBe("text");
    if (content.type !== "text") return;
    const discovery = JSON.parse(content.text) as {
      items: { path: string; signature: string }[];
    };
    const createTodo = discovery.items.find(
      (item) => item.path === "tools.create_todo",
    );
    expect(createTodo?.path).toBe("tools.create_todo");
    expect(createTodo?.signature).toContain("tools.create_todo");
  });
});
