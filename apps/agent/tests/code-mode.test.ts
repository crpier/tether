import type {
  AgentToolResult,
  ExtensionContext,
  ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { describe, expect, test, vi } from "vitest";

import {
  createCodeModeTool,
  type CodeModeDetails,
  type CodeModeSource,
  sourceForTool,
} from "../src/code-mode.js";
import type { TetherToolDetails } from "../src/runtime.js";

const context = {} as ExtensionContext;

function sourceTool(
  name: string,
  execute: (
    input: unknown,
    signal: AbortSignal | undefined,
  ) => Promise<unknown>,
): CodeModeSource {
  return {
    description: `${name} test tool`,
    input: {
      properties: { input: {} },
      type: "object",
    },
    name,
    invoke: async (_toolCallId, params, signal) =>
      execute((params as { input?: unknown }).input, signal),
  };
}

async function run(
  tools: CodeModeSource[],
  code: string,
  options?: {
    maxOutputBytes?: number;
    maxToolCalls?: number;
    timeoutMs?: number;
  },
  signal?: AbortSignal,
): Promise<AgentToolResult<CodeModeDetails>> {
  return createCodeModeTool(tools, options).execute(
    "outer-call",
    { code },
    signal,
    undefined,
    context,
  );
}

describe("execute_tools", () => {
  test("adapts generated tools without bypassing their execution boundary", async () => {
    const execute = vi.fn(() =>
      Promise.resolve({
        content: [{ type: "text" as const, text: "ignored presentation" }],
        details: {
          provenance: { kind: "manual" },
          quota: null,
          result: { authorized: true },
        },
      }),
    );
    const generated: ToolDefinition<
      ReturnType<typeof Type.Object>,
      TetherToolDetails
    > = {
      name: "authorized_action",
      label: "AuthorizedAction",
      description: "Run an authorized host action.",
      parameters: Type.Object({ input: Type.String() }),
      execute,
    };

    const execution = await run(
      [sourceForTool(generated)],
      'return await tools.authorized_action({ input: "value" })',
    );

    expect(execution.content).toEqual([
      { type: "text", text: '{"authorized":true}' },
    ]);
    expect(execute).toHaveBeenCalledWith(
      "outer-call/1",
      { input: "value" },
      expect.any(AbortSignal),
      undefined,
      context,
    );
  });

  test("sequences, parallelizes, and processes structured tool results", async () => {
    const list = vi.fn(() =>
      Promise.resolve([
        { id: "1", state: "open" },
        { id: "2", state: "done" },
        { id: "3", state: "open" },
      ]),
    );
    const label = vi.fn((input: unknown) =>
      Promise.resolve({
        ...(input as object),
        labeled: true,
      }),
    );

    const execution = await run(
      [sourceTool("list_todos", list), sourceTool("label_todo", label)],
      `
        const todos = await tools.list_todos({});
        const open = todos.filter((todo) => todo.state === "open");
        const labeled = await Promise.all(
          open.map((todo) => tools.label_todo({ input: { id: todo.id } })),
        );
        return { count: labeled.length, labeled };
      `,
    );

    expect(execution.content).toEqual([
      {
        type: "text",
        text: JSON.stringify({
          count: 2,
          labeled: [
            { id: "1", labeled: true },
            { id: "3", labeled: true },
          ],
        }),
      },
    ]);
    expect(list).toHaveBeenCalledOnce();
    expect(label).toHaveBeenCalledTimes(2);
    expect(execution.details.toolCalls).toEqual([
      { name: "list_todos", status: "completed" },
      { name: "label_todo", status: "completed" },
      { name: "label_todo", status: "completed" },
    ]);
  });

  test("discovers exact callable signatures without ambient host authority", async () => {
    const execution = await run(
      [sourceTool("list_todos", () => Promise.resolve([]))],
      `
        return {
          ambient: {
            fetch: typeof fetch,
            process: typeof process,
            require: typeof require,
          },
          discovered: search({ query: "todo" }),
        };
      `,
    );

    const content = execution.content[0];
    expect(content.type).toBe("text");
    expect(JSON.parse(content.type === "text" ? content.text : "null")).toEqual(
      {
        ambient: {
          fetch: "undefined",
          process: "undefined",
          require: "undefined",
        },
        discovered: {
          items: [
            {
              description: "list_todos test tool",
              path: "tools.list_todos",
              signature:
                "tools.list_todos(input: {\n  input?: unknown,\n}): Promise<unknown>",
            },
          ],
          next: null,
          remaining: 0,
        },
      },
    );
  });

  test("bounds busy loops and admitted tool calls", async () => {
    await expect(run([], "while (true) {}", { timeoutMs: 10 })).rejects.toThrow(
      "Execution timed out after 10ms",
    );

    await expect(
      run(
        [sourceTool("echo", (input) => Promise.resolve(input))],
        `
          await tools.echo({ input: 1 });
          await tools.echo({ input: 2 });
          return "unreachable";
        `,
        { maxToolCalls: 1 },
      ),
    ).rejects.toThrow("Execution exceeded its tool-call limit of 1");

    const oversized = await run([], 'return "x".repeat(1_000)', {
      maxOutputBytes: 64,
    });
    const oversizedContent = oversized.content[0];
    expect(oversizedContent.type).toBe("text");
    if (oversizedContent.type === "text") {
      expect(oversizedContent.text).toContain("result truncated");
    }
  });

  test("propagates cancellation into a nested Tether tool", async () => {
    let nestedSignal: AbortSignal | undefined;
    const waiting = sourceTool(
      "wait",
      (_input, signal) =>
        new Promise((_resolve, reject) => {
          nestedSignal = signal;
          signal?.addEventListener(
            "abort",
            () => reject(new Error("nested aborted")),
            { once: true },
          );
        }),
    );
    const controller = new AbortController();
    const execution = run(
      [waiting],
      "return await tools.wait({})",
      { timeoutMs: 1_000 },
      controller.signal,
    );

    await vi.waitFor(() => expect(nestedSignal).toBeDefined());
    controller.abort();

    await expect(execution).rejects.toThrow();
    expect(nestedSignal?.aborted).toBe(true);
  });
});
