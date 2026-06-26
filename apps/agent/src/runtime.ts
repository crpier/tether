import type { AgentToolResult } from "@earendil-works/pi-coding-agent";

export interface GeneratedToolMetadata {
  endpoint: string;
  name: string;
}

export interface TetherToolConfig {
  baseUrl: string;
  secret: string;
  sessionId: string;
}

export interface TetherToolDetails {
  provenance: unknown;
  quota: unknown;
  result: unknown;
}

interface ToolEnvelope {
  error?: { code?: unknown; message?: unknown } | null;
  provenance?: unknown;
  quota?: unknown;
  result?: unknown;
  success?: unknown;
}

function readRequiredEnv(name: string): string {
  const value = process.env[name];
  if (value === undefined || value.length === 0) {
    throw new Error(`missing ${name}`);
  }
  return value;
}

export function readTetherToolConfig(): TetherToolConfig {
  return {
    baseUrl: readRequiredEnv("TETHER_TOOL_BASE_URL"),
    secret: readRequiredEnv("TETHER_TOOL_SECRET"),
    sessionId: readRequiredEnv("TETHER_TOOL_SESSION_ID"),
  };
}

function toolUrl(config: TetherToolConfig, endpoint: string): URL {
  const baseUrl = config.baseUrl.endsWith("/")
    ? config.baseUrl.slice(0, -1)
    : config.baseUrl;
  return new URL(endpoint, `${baseUrl}/`);
}

function envelopeErrorMessage(envelope: ToolEnvelope): string {
  const code =
    typeof envelope.error?.code === "string"
      ? envelope.error.code
      : "tool_error";
  const message =
    typeof envelope.error?.message === "string"
      ? envelope.error.message
      : "tool failed";
  return `${code}: ${message}`;
}

export async function executeTetherTool(
  tool: GeneratedToolMetadata,
  params: object,
  signal: AbortSignal | undefined,
  config: TetherToolConfig = readTetherToolConfig(),
): Promise<AgentToolResult<TetherToolDetails>> {
  const response = await fetch(toolUrl(config, tool.endpoint), {
    body: JSON.stringify({ ...params, session_id: config.sessionId }),
    headers: {
      "content-type": "application/json",
      "x-tether-tool-secret": config.secret,
    },
    method: "POST",
    signal,
  });

  if (!response.ok) {
    throw new Error(
      `tool endpoint returned HTTP ${String(response.status)}: ${await response.text()}`,
    );
  }

  const envelope = (await response.json()) as ToolEnvelope;
  if (envelope.success !== true) {
    throw new Error(envelopeErrorMessage(envelope));
  }

  return {
    content: [{ type: "text", text: `${tool.name} succeeded` }],
    details: {
      provenance: envelope.provenance ?? null,
      quota: envelope.quota ?? null,
      result: envelope.result ?? null,
    },
  };
}
