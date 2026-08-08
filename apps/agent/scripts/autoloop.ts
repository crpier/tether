import { chromium } from "playwright";

import {
  abortableDelay,
  autoloopConfigFromEnv,
  ensurePlaywrightBrowser,
  runExplorerOnce,
  runPiJson,
  type AutoloopConfig,
} from "../src/autoloop.js";

function boolEnv(name: string): boolean {
  return process.env[name] === "1" || process.env[name] === "true";
}

async function runFixer(
  issueUrl: string,
  config: Pick<AutoloopConfig, "cwd" | "piCommand" | "sessionDir">,
): Promise<void> {
  const autoMerge = boolEnv("TETHER_AUTOLOOP_AUTO_MERGE");
  const autoDeploy = boolEnv("TETHER_AUTOLOOP_AUTO_DEPLOY");
  const prompt = `Pick up GitHub issue ${issueUrl} for Tether.

Follow the repository AGENTS.md workflow exactly: stash/fetch/branch from origin/main, TDD, validation gate, then open a PR against main.
Reference the issue in commits/PR.
${autoMerge ? "If all checks pass, merge the PR." : "Do not merge the PR."}
${autoDeploy ? "After merging, deploy with the documented just deploy flow." : "Do not deploy."}
Stop after the requested PR/merge/deploy boundary.`;

  await runPiJson({
    cwd: config.cwd,
    name: `fixer-${new Date().toISOString()}`,
    onRender: (chunk) => process.stdout.write(chunk),
    piCommand: config.piCommand,
    prompt,
    sessionDir: config.sessionDir,
  });
}

async function main(): Promise<void> {
  await ensurePlaywrightBrowser(chromium.executablePath());
  const config = autoloopConfigFromEnv();
  const loopDelayMs = Number.parseInt(
    process.env.TETHER_AUTOLOOP_DELAY_MS ?? "5000",
    10,
  );
  const runFixerEnabled = boolEnv("TETHER_AUTOLOOP_FIXER");
  const stopController = new AbortController();

  process.on("SIGINT", () => {
    stopController.abort();
    process.stdout.write("\n[autoloop] stopping\n");
  });
  process.on("SIGTERM", () => {
    stopController.abort();
  });

  while (!stopController.signal.aborted) {
    process.stdout.write(
      `\n[autoloop] explorer cycle ${new Date().toISOString()}\n`,
    );
    const result = await runExplorerOnce(config).catch((error: unknown) => {
      if (stopController.signal.aborted) return undefined;
      throw error;
    });
    if (result === undefined) break;
    if (result.finding === undefined) {
      process.stdout.write("\n[autoloop] no finding\n");
      await abortableDelay(loopDelayMs, stopController.signal);
      continue;
    }

    process.stdout.write(`\n[autoloop] finding: ${result.finding.title}\n`);
    if (result.issueUrl !== undefined) {
      process.stdout.write(`[autoloop] issue: ${result.issueUrl}\n`);
    }

    if (runFixerEnabled && result.issueUrl !== undefined) {
      await runFixer(result.issueUrl, config);
    }

    await abortableDelay(loopDelayMs, stopController.signal);
  }
}

await main();
