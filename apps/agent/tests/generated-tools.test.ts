import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { describe, expect, test } from "vitest";

import tetherToolsExtension from "../src/generated/index.js";

interface RegisteredToolSummary {
  name: string;
  parameters: unknown;
}

describe("generated tool extension", () => {
  test("registers the Memory, Bucket item, and YouTube tools", () => {
    const registeredTools: RegisteredToolSummary[] = [];
    const pi = {
      registerTool(tool: RegisteredToolSummary): void {
        registeredTools.push({ name: tool.name, parameters: tool.parameters });
      },
    } as unknown as ExtensionAPI;

    tetherToolsExtension(pi);

    expect(registeredTools.map((tool) => tool.name)).toEqual([
      "capture",
      "browse",
      "search",
      "review_digest",
      "tether",
      "edit",
      "reject",
      "add_movie",
      "add_place",
      "complete_bucket_item",
      "delete_bucket_item",
      "search_bucket_items",
      "browse_youtube",
      "search_youtube",
      "fetch_youtube_transcript",
      "ignore_youtube_video",
      "retry_youtube_video",
      "create_trigger",
      "list_triggers",
      "delete_trigger",
    ]);
  });
});
