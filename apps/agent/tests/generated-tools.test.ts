import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { describe, expect, test } from "vitest";

import tetherToolsExtension from "../src/generated/index.js";

interface RegisteredToolSummary {
  name: string;
  parameters: unknown;
}

describe("generated tool extension", () => {
  test("registers the six Memory tools", () => {
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
      "tether",
      "edit",
      "reject",
    ]);
  });
});
