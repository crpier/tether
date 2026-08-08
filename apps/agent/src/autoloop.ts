import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { access, mkdir, readFile, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

export interface ExplorerFinding {
  actual: string;
  evidence: string;
  expected: string;
  fingerprint: string;
  kind: string;
  repro: string[];
  suggestedLabels: string[];
  title: string;
}

export interface ExplorerReport {
  doNotTry: string[];
  finding: ExplorerFinding | undefined;
  notes: string[];
}

export interface ExplorerPromptOptions {
  doNotTry: string;
  maxActions: number;
  notes: string;
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

export async function abortableDelay(
  milliseconds: number,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) return;
  await new Promise<void>((resolveDelay) => {
    const done = (): void => {
      clearTimeout(timeout);
      signal.removeEventListener("abort", done);
      resolveDelay();
    };
    const timeout = setTimeout(done, milliseconds);
    signal.addEventListener("abort", done, { once: true });
  });
}

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
    fingerprint: readString(value, "fingerprint"),
    kind: readString(value, "kind"),
    repro: readStringArray(value, "repro"),
    suggestedLabels: readStringArray(value, "suggestedLabels"),
    title: readString(value, "title"),
  };
}

function extractMarkedPayload(text: string): unknown {
  const start = text.lastIndexOf(resultStart);
  const end = text.lastIndexOf(resultEnd);
  if (start === -1 || end === -1 || end <= start) return undefined;

  const jsonText = text.slice(start + resultStart.length, end).trim();
  return JSON.parse(jsonText) as unknown;
}

export function extractExplorerReport(text: string): ExplorerReport {
  const payload = extractMarkedPayload(text);
  if (!isRecord(payload)) {
    throw new Error("explorer must return a marked JSON object");
  }
  const finding = isRecord(payload.finding)
    ? parseFindingPayload({ ...payload.finding, found: true })
    : undefined;
  return {
    doNotTry: readStringArray(payload, "doNotTry"),
    finding,
    notes: readStringArray(payload, "notes"),
  };
}

export function extractExplorerFinding(
  text: string,
): ExplorerFinding | undefined {
  return parseFindingPayload(extractMarkedPayload(text));
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
Autoloop fingerprint: ${finding.fingerprint}

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
Before reporting a possibly transient loading or empty-state problem, confirm it with a fresh browser_snapshot.
If login is required and TETHER_AUTOLOOP_APP_PASSWORD is set, use browser_fill_secret so the password is not printed.
Do not perform destructive actions. Do not spam external services.

You cannot access shell or file tools. The supervisor owns persistent state.
Return only concise new coverage notes; do not restate existing entries.

Existing notes:
${options.notes || "(none yet)"}

Do not try:
${options.doNotTry || "(none yet)"}

Never report an existing issue or a wording variant of anything under Do not try.
If you find a bug, confusing UX, accessibility issue, or obvious improvement, stop and report exactly one finding.
Use a stable kebab-case fingerprint based on affected surface and failure, independent of wording.
Finish with exactly one marked JSON object and no file edits:

${resultStart}
{"finding":{"kind":"bug|ux|a11y|perf","fingerprint":"stable-kebab-case-id","title":"short title","evidence":"what you saw","repro":["step 1"],"expected":"expected behavior","actual":"actual behavior","suggestedLabels":["bug"]},"notes":["concise new coverage note"],"doNotTry":["concise new thing future runs should avoid"]}
${resultEnd}

If no finding this run, omit finding:
${resultStart}
{"notes":["concise new coverage note"],"doNotTry":[]}
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
    if (event.isError === true && isRecord(event.result)) {
      const content = event.result.content;
      if (Array.isArray(content)) {
        const details = content
          .flatMap((item): string[] =>
            isRecord(item) &&
            item.type === "text" &&
            typeof item.text === "string"
              ? [item.text]
              : [],
          )
          .join("\n")
          .trim()
          .slice(0, 500);
        if (details.length > 0) {
          return `\n[tool:${status}] ${toolName}: ${details}\n`;
        }
      }
    }
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

export function mergeMarkdownEntries(
  existing: string,
  heading: string,
  entries: string[],
  limit = 100,
): string {
  const normalize = (entry: string): string =>
    entry
      .replace(/^\s*-\s*/, "")
      .replace(/\s+/g, " ")
      .trim();
  const existingEntries = existing
    .split("\n")
    .filter((line) => /^\s*-\s+/.test(line))
    .map(normalize);
  const unique = Array.from(
    new Set(
      [...existingEntries, ...entries.map(normalize)].filter(
        (entry) => entry.length > 0,
      ),
    ),
  ).slice(-limit);
  return `${heading}\n${unique.map((entry) => `- ${entry}`).join("\n")}${unique.length > 0 ? "\n" : ""}`;
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

export interface GitHubIssueSummary {
  body: string;
  url: string;
}

export function findOpenIssueByFingerprint(
  issues: GitHubIssueSummary[],
  fingerprint: string,
): string | undefined {
  const marker = `Autoloop fingerprint: ${fingerprint}`;
  return issues.find((issue) => issue.body.includes(marker))?.url;
}

async function listOpenGitHubIssues(
  config: Pick<AutoloopConfig, "ghCommand" | "cwd">,
): Promise<GitHubIssueSummary[]> {
  const child = spawn(
    config.ghCommand,
    [
      "issue",
      "list",
      "--state",
      "open",
      "--limit",
      "100",
      "--json",
      "body,url",
    ],
    {
      cwd: config.cwd,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
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
      `gh issue list exited with ${String(exitCode)}${stderr ? `: ${stderr}` : ""}`,
    );
  }
  const payload = JSON.parse(stdout) as unknown;
  if (!Array.isArray(payload))
    throw new Error("gh issue list returned invalid JSON");
  return payload.flatMap((value): GitHubIssueSummary[] => {
    if (
      !isRecord(value) ||
      typeof value.body !== "string" ||
      typeof value.url !== "string"
    ) {
      return [];
    }
    return [{ body: value.body, url: value.url }];
  });
}

export async function createGitHubIssue(
  finding: ExplorerFinding,
  body: string,
  config: Pick<AutoloopConfig, "ghCommand" | "issueLabels" | "cwd">,
): Promise<string> {
  const existingUrl = findOpenIssueByFingerprint(
    await listOpenGitHubIssues(config),
    finding.fingerprint,
  );
  if (existingUrl !== undefined) return existingUrl;

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
  await unlink(tmpPath).catch(() => undefined);
  if (exitCode !== 0) {
    throw new Error(
      `gh issue create exited with ${String(exitCode)}${stderr ? `: ${stderr}` : ""}`,
    );
  }
  return stdout.trim();
}

export async function ensurePlaywrightBrowser(
  executablePath: string,
): Promise<void> {
  try {
    await access(executablePath);
  } catch {
    throw new Error(
      "Playwright Chromium is missing; run: pnpm -C apps/agent exec playwright install chromium",
    );
  }
}

export function explorerPiArgs(browserExtensionPath: string): string[] {
  return [
    "--no-builtin-tools",
    "--no-extensions",
    "--no-skills",
    "--extension",
    browserExtensionPath,
  ];
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
    args: explorerPiArgs(config.browserExtensionPath),
    cwd: config.cwd,
    env: {
      TETHER_AUTOLOOP_MAX_ACTIONS: String(config.explorerMaxActions),
      TETHER_AUTOLOOP_PRODUCTION_URL: config.productionUrl,
    },
    name: runId,
    onRender,
    piCommand: config.piCommand,
    prompt: buildExplorerPrompt({
      doNotTry,
      maxActions: config.explorerMaxActions,
      notes,
      productionUrl: config.productionUrl,
    }),
    sessionDir: config.sessionDir,
  });
  const report = extractExplorerReport(assistantText);
  await writeFile(
    config.notesPath,
    mergeMarkdownEntries(notes, "# Explorer notes", report.notes),
    "utf8",
  );
  let updatedDoNotTry = mergeMarkdownEntries(
    doNotTry,
    "# Do not try",
    report.doNotTry,
  );
  const finding = report.finding;
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
  if (finding !== undefined) {
    const issueReference = issueUrl === undefined ? "" : ` (${issueUrl})`;
    updatedDoNotTry = mergeMarkdownEntries(updatedDoNotTry, "# Do not try", [
      `Known finding [${finding.fingerprint}]: ${finding.title}${issueReference}; do not report wording variants while unresolved.`,
    ]);
  }
  await writeFile(config.doNotTryPath, updatedDoNotTry, "utf8");
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
    productionUrl:
      env.TETHER_AUTOLOOP_PRODUCTION_URL ?? "https://tether.tail2da0b1.ts.net",
    sessionDir: resolve(root, "pi-sessions"),
  };
}
