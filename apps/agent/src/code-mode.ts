import {
  CodeMode,
  Tool as ConfinedTool,
  toolError,
} from "@opencode-ai/codemode";
import type {
  AgentToolResult,
  ExtensionContext,
  ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { Effect } from "effect";
import { Type, type Static, type TSchema } from "typebox";

import type { TetherToolDetails } from "./runtime.js";

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_TOOL_CALLS = 20;
const DEFAULT_MAX_OUTPUT_BYTES = 48 * 1_024;

const parameters = Type.Object({
  code: Type.String({
    description:
      "TypeScript/JavaScript script body executed in the confined interpreter.",
    minLength: 1,
  }),
});

export interface CodeModeCallDetails {
  name: string;
  status: "completed" | "error" | "running";
}

export interface CodeModeDetails {
  toolCalls: CodeModeCallDetails[];
}

export interface CodeModeOptions {
  maxOutputBytes?: number;
  maxToolCalls?: number;
  timeoutMs?: number;
}

export interface CodeModeSource {
  description: string;
  input: ConfinedTool.JsonSchema;
  invoke: (
    toolCallId: string,
    input: unknown,
    signal: AbortSignal | undefined,
    context: ExtensionContext,
  ) => Promise<unknown>;
  name: string;
}

export function sourceForTool<TParams extends TSchema>(
  tool: ToolDefinition<TParams, TetherToolDetails>,
): CodeModeSource {
  return {
    description: tool.description,
    input: tool.parameters,
    name: tool.name,
    async invoke(toolCallId, input, signal, context) {
      const result = await tool.execute(
        toolCallId,
        input as Static<TParams>,
        signal,
        undefined,
        context,
      );
      return result.details.result;
    },
  };
}

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function codeModeTool(
  source: CodeModeSource,
  outerToolCallId: string,
  context: ExtensionContext,
  nextChildIndex: () => number,
): ConfinedTool.Tool {
  return ConfinedTool.make({
    description: source.description,
    input: source.input,
    output: {},
    execute: (input) =>
      Effect.tryPromise({
        try: async (signal) => {
          const childIndex = nextChildIndex();
          return source.invoke(
            `${outerToolCallId}/${String(childIndex)}`,
            input,
            signal,
            context,
          );
        },
        catch: (error) => toolError(safeMessage(error), error),
      }),
  });
}

function programOutput(
  value: CodeMode.DataValue,
  logs: readonly string[] | undefined,
): string {
  const rendered = typeof value === "string" ? value : JSON.stringify(value);
  if (logs === undefined || logs.length === 0) return rendered;
  return `${rendered}\n\nLogs:\n${logs.join("\n")}`;
}

function updateResult(
  calls: readonly CodeModeCallDetails[],
): AgentToolResult<CodeModeDetails> {
  const running = calls.filter((call) => call.status === "running").length;
  return {
    content: [
      {
        type: "text",
        text:
          running === 0
            ? "Finishing confined tool program."
            : `Running ${String(running)} nested tool ${running === 1 ? "call" : "calls"}.`,
      },
    ],
    details: { toolCalls: calls.map((call) => ({ ...call })) },
  };
}

export function createCodeModeTool(
  sources: readonly CodeModeSource[],
  options: CodeModeOptions = {},
): ToolDefinition<typeof parameters, CodeModeDetails> {
  return {
    name: "execute_tools",
    label: "ExecuteTools",
    description:
      'Run a fresh confined TypeScript/JavaScript program that orchestrates Tether tools and returns JSON. Inside the program call tools.<name>(params), use await or Promise.all, and return only the needed result. Call search({ query: "..." }) to discover exact signatures. Imports, packages, filesystem, process, environment, and network access are unavailable.',
    promptSnippet:
      "Execute a confined TypeScript/JavaScript program over Tether tools.",
    promptGuidelines: [
      "Use execute_tools when several tool calls need sequencing, parallelism, filtering, or aggregation.",
      "Inside execute_tools, call only exact tools.* signatures or use search({ query }) to discover them; return only data needed for the answer.",
      "Use direct tools for simple one-call work.",
    ],
    parameters,
    async execute(toolCallId, params, signal, onUpdate, context) {
      const calls: CodeModeCallDetails[] = [];
      let childIndex = 0;
      const tools = Object.fromEntries(
        sources.map((source) => [
          source.name,
          codeModeTool(source, toolCallId, context, () => {
            childIndex += 1;
            return childIndex;
          }),
        ]),
      );
      const publish = (): void => onUpdate?.(updateResult(calls));
      const runtime = CodeMode.make({
        tools,
        limits: {
          maxOutputBytes: options.maxOutputBytes ?? DEFAULT_MAX_OUTPUT_BYTES,
          maxToolCalls: options.maxToolCalls ?? DEFAULT_MAX_TOOL_CALLS,
          timeoutMs: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
        },
        onToolCallStart: ({ index, name }) =>
          Effect.sync(() => {
            calls[index] = { name, status: "running" };
            publish();
          }),
        onToolCallEnd: ({ index, outcome }) =>
          Effect.sync(() => {
            calls[index] = {
              ...calls[index],
              status: outcome === "success" ? "completed" : "error",
            };
            publish();
          }),
      });
      const result = await Effect.runPromise(runtime.execute(params.code), {
        signal,
      });
      if (!result.ok) {
        const suggestions = result.error.suggestions ?? [];
        throw new Error(
          [result.error.message, ...suggestions].filter(Boolean).join("\n"),
        );
      }
      return {
        content: [
          { type: "text", text: programOutput(result.value, result.logs) },
        ],
        details: { toolCalls: calls.map((call) => ({ ...call })) },
      };
    },
  };
}
