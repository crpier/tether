import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

export interface ExplorerFinding {
  actual: string;
  evidence: string;
  expected: string;
  kind: string;
  repro: string[];
  suggestedLabels: string[];
  title: string;
}

export interface ExplorerPromptOptions {
  doNotTry: string;
  maxActions: number;
  notes: string;
  notesPath: string;
  productionUrl: string;
}

export interface IssueBodyOptions {
  productionUrl: string;
  runId: string;
}

export interface PiRunOptions {
  args?: string[];
  cwd: string;
  env?: NodeJS.ProcessEnv;
  name: string;
  onRender?: (chunk: string) => void;
  piCommand: string;
  prompt: string;
  sessionDir: string;
}

export interface AutoloopConfig {
  autoCreateIssue: boolean;
  browserExtensionPath: string;
  cwd: string;
  doNotTryPath: string;
  explorerMaxActions: number;
  ghCommand: string;
  issueLabels: string[];
  notesPath: string;
  piCommand: string;
  productionUrl: string;
  sessionDir: string;
}

export interface ExplorerRunResult {
  assistantText: string;
  finding: ExplorerFinding | undefined;
  issueUrl: string | undefined;
  runId: string;
}

const resultStart = "AUTORESEARCH_RESULT_START";
const resultEnd = "AUTORESEARCH_RESULT_END";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function readString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`finding.${key} must be a non-empty string`);
  }
  return value;
}

function readStringArray(
  record: Record<string, unknown>,
  key: string,
): string[] {
  const value = record[key];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`finding.${key} must be an array of strings`);
  }
  return value as string[];
}

function parseFindingPayload(value: unknown): ExplorerFinding | undefined {
  if (!isRecord(value)) return undefined;
  if (value.found === false) return undefined;
  if (value.found !== true) return undefined;

  return {
    actual: readString(value, "actual"),
    evidence: readString(value, "evidence"),
    expected: readString(value, "expected"),
    kind: readString(value, "kind"),
    repro: readStringArray(value, "repro"),
    suggestedLabels: readStringArray(value, "suggestedLabels"),
    title: readString(value, "title"),
  };
}

export function extractExplorerFinding(
  text: string,
): ExplorerFinding | undefined {
  const start = text.lastIndexOf(resultStart);
  const end = text.lastIndexOf(resultEnd);
  if (start === -1 || end === -1 || end <= start) return undefined;

  const jsonText = text.slice(start + resultStart.length, end).trim();
  return parseFindingPayload(JSON.parse(jsonText) as unknown);
}

export function formatFindingIssueBody(
  finding: ExplorerFinding,
  options: IssueBodyOptions,
): string {
  const repro = finding.repro
    .map((step, index) => `${String(index + 1)}. ${step}`)
    .join("\n");
  return `Found by the Pi autoloop explorer against ${options.productionUrl}.

Run: ${options.runId}

## Kind
${finding.kind}

## Evidence
${finding.evidence}

## Repro
${repro}

## Expected
${finding.expected}

## Actual
${finding.actual}
`;
}

export function buildExplorerPrompt(options: ExplorerPromptOptions): string {
  return `You are the Tether production exploratory tester in an autoresearch loop.

Target production app: ${options.productionUrl}

Use the browser tools for real exploratory testing:
- browser_open
- browser_snapshot
- browser_click
- browser_fill
- browser_console
- browser_network
- browser_screenshot
- browser_fill_secret

At most ${String(options.maxActions)} browser actions this run. Prefer broad, shallow exploration over repeating one flow.
If login is required and TETHER_AUTOLOOP_APP_PASSWORD is set, use browser_fill_secret so the password is not printed.
Do not perform destructive actions. Do not spam external services.

Persistent notes file: ${options.notesPath}
Read it, then update it with what you tried, what changed, and what future runs should avoid.

Existing notes:
${options.notes || "(none yet)"}

Do not try:
${options.doNotTry || "(none yet)"}

If you find a bug, confusing UX, accessibility issue, or obvious improvement, stop and report exactly one finding.
Finish with exactly one marked JSON object:

${resultStart}
{"found":true,"kind":"bug|ux|a11y|perf","title":"short title","evidence":"what you saw","repro":["step 1"],"expected":"expected behavior","actual":"actual behavior","suggestedLabels":["bug"]}
${resultEnd}

If no finding this run:
${resultStart}
{"found":false}
${resultEnd}
`;
}

export function renderPiJsonEvent(event: unknown): string | undefined {
  if (!isRecord(event)) return undefined;

  if (
    event.type === "message_update" &&
    isRecord(event.assistantMessageEvent)
  ) {
    const update = event.assistantMessageEvent;
    if (update.type === "text_delta" && typeof update.delta === "string") {
      return update.delta;
    }
  }

  if (event.type === "tool_execution_start") {
    const toolName =
      typeof event.toolName === "string" ? event.toolName : "tool";
    const args = JSON.stringify(event.args ?? {});
    return `\n[tool] ${toolName} ${args}\n`;
  }

  if (event.type === "tool_execution_end") {
    const toolName =
      typeof event.toolName === "string" ? event.toolName : "tool";
    const status = event.isError === true ? "error" : "ok";
    return `\n[tool:${status}] ${toolName}\n`;
  }

  return undefined;
}

function splitJsonLines(buffer: string): { lines: string[]; rest: string } {
  const parts = buffer.split("\n");
  const rest = parts.pop() ?? "";
  return { lines: parts, rest };
}

export async function runPiJson(options: PiRunOptions): Promise<string> {
  await mkdir(options.sessionDir, { recursive: true });
  const child = spawn(
    options.piCommand,
    [
      "--mode",
      "json",
      "--session-dir",
      options.sessionDir,
      "--approve",
      "--name",
      options.name,
      ...(options.args ?? []),
      options.prompt,
    ],
    {
      cwd: options.cwd,
      env: { ...process.env, ...options.env },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let assistantText = "";
  let stdoutBuffer = "";
  let stderrBuffer = "";

  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    stdoutBuffer += chunk;
    const { lines, rest } = splitJsonLines(stdoutBuffer);
    stdoutBuffer = rest;
    for (const line of lines) {
      if (line.trim().length === 0) continue;
      const event = JSON.parse(line) as unknown;
      const rendered = renderPiJsonEvent(event);
      if (rendered !== undefined) options.onRender?.(rendered);
      if (
        isRecord(event) &&
        event.type === "message_update" &&
        isRecord(event.assistantMessageEvent)
      ) {
        const update = event.assistantMessageEvent;
        if (update.type === "text_delta" && typeof update.delta === "string") {
          assistantText += update.delta;
        }
      }
    }
  });

  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    stderrBuffer += chunk;
    options.onRender?.(chunk);
  });

  const exitCode = await new Promise<number | null>(
    (resolveExit, rejectExit) => {
      child.on("error", rejectExit);
      child.on("close", resolveExit);
    },
  );

  if (stdoutBuffer.trim().length > 0) {
    const event = JSON.parse(stdoutBuffer) as unknown;
    const rendered = renderPiJsonEvent(event);
    if (rendered !== undefined) options.onRender?.(rendered);
    if (
      isRecord(event) &&
      event.type === "message_update" &&
      isRecord(event.assistantMessageEvent)
    ) {
      const update = event.assistantMessageEvent;
      if (update.type === "text_delta" && typeof update.delta === "string") {
        assistantText += update.delta;
      }
    }
  }

  if (exitCode !== 0) {
    throw new Error(
      `pi exited with ${String(exitCode)}${stderrBuffer ? `: ${stderrBuffer}` : ""}`,
    );
  }

  return assistantText;
}

async function readTextOrDefault(
  path: string,
  fallback: string,
): Promise<string> {
  try {
    return await readFile(path, "utf8");
  } catch (error: unknown) {
    if (isRecord(error) && error.code === "ENOENT") return fallback;
    throw error;
  }
}

export async function ensureAutoloopFiles(
  config: AutoloopConfig,
): Promise<void> {
  await mkdir(dirname(config.notesPath), { recursive: true });
  await mkdir(dirname(config.doNotTryPath), { recursive: true });
  await mkdir(config.sessionDir, { recursive: true });
  await writeFile(
    config.notesPath,
    await readTextOrDefault(config.notesPath, "# Explorer notes\n"),
    "utf8",
  );
  await writeFile(
    config.doNotTryPath,
    await readTextOrDefault(config.doNotTryPath, "# Do not try\n"),
    "utf8",
  );
}

export async function createGitHubIssue(
  finding: ExplorerFinding,
  body: string,
  config: Pick<AutoloopConfig, "ghCommand" | "issueLabels" | "cwd">,
): Promise<string> {
  const tmpPath = join(tmpdir(), `tether-autoloop-${randomUUID()}.md`);
  await writeFile(tmpPath, body, "utf8");

  const labels = Array.from(new Set(config.issueLabels));
  const args = [
    "issue",
    "create",
    "--title",
    finding.title,
    "--body-file",
    tmpPath,
  ];
  if (labels.length > 0) args.push("--label", labels.join(","));

  const child = spawn(config.ghCommand, args, {
    cwd: config.cwd,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    stdout += chunk;
  });
  child.stderr.on("data", (chunk: string) => {
    stderr += chunk;
  });
  const exitCode = await new Promise<number | null>(
    (resolveExit, rejectExit) => {
      child.on("error", rejectExit);
      child.on("close", resolveExit);
    },
  );
  if (exitCode !== 0) {
    throw new Error(
      `gh issue create exited with ${String(exitCode)}${stderr ? `: ${stderr}` : ""}`,
    );
  }
  return stdout.trim();
}

export async function runExplorerOnce(
  config: AutoloopConfig,
  onRender: (chunk: string) => void = (chunk) => {
    process.stdout.write(chunk);
  },
): Promise<ExplorerRunResult> {
  await ensureAutoloopFiles(config);
  const runId = `explorer-${new Date().toISOString()}`;
  const notes = await readTextOrDefault(config.notesPath, "");
  const doNotTry = await readTextOrDefault(config.doNotTryPath, "");
  const assistantText = await runPiJson({
    args: ["--extension", config.browserExtensionPath],
    cwd: config.cwd,
    name: runId,
    onRender,
    piCommand: config.piCommand,
    prompt: buildExplorerPrompt({
      doNotTry,
      maxActions: config.explorerMaxActions,
      notes,
      notesPath: config.notesPath,
      productionUrl: config.productionUrl,
    }),
    sessionDir: config.sessionDir,
  });
  const finding = extractExplorerFinding(assistantText);
  const issueUrl =
    finding !== undefined && config.autoCreateIssue
      ? await createGitHubIssue(
          finding,
          formatFindingIssueBody(finding, {
            productionUrl: config.productionUrl,
            runId,
          }),
          config,
        )
      : undefined;
  return { assistantText, finding, issueUrl, runId };
}

export function autoloopConfigFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): AutoloopConfig {
  const cwd = resolve(env.TETHER_AUTOLOOP_CWD ?? process.cwd());
  const root = resolve(cwd, env.TETHER_AUTOLOOP_DIR ?? ".tether/autoloop");
  return {
    autoCreateIssue: env.TETHER_AUTOLOOP_CREATE_ISSUE !== "0",
    browserExtensionPath: resolve(
      cwd,
      env.TETHER_AUTOLOOP_BROWSER_EXTENSION ??
        "apps/agent/src/autoloop-browser-extension.ts",
    ),
    cwd,
    doNotTryPath: resolve(root, "do-not-try.md"),
    explorerMaxActions: Number.parseInt(
      env.TETHER_AUTOLOOP_MAX_ACTIONS ?? "12",
      10,
    ),
    ghCommand: env.TETHER_AUTOLOOP_GH ?? "gh",
    issueLabels: (env.TETHER_AUTOLOOP_ISSUE_LABELS ?? "bug")
      .split(",")
      .map((label) => label.trim())
      .filter((label) => label.length > 0),
    notesPath: resolve(root, "explorer-notes.md"),
    piCommand: env.TETHER_AUTOLOOP_PI ?? "pi",
    productionUrl: env.TETHER_AUTOLOOP_PRODUCTION_URL ?? "https://tether",
    sessionDir: resolve(root, "pi-sessions"),
  };
}
