import {
  DEFAULT_MAX_BYTES,
  DEFAULT_MAX_LINES,
  truncateHead,
  type ExtensionAPI,
} from "@earendil-works/pi-coding-agent";
import { readFile, realpath } from "node:fs/promises";
import {
  dirname,
  extname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";

export function createRestrictedSkillReadExtension(
  skillRoots: readonly string[],
): (pi: ExtensionAPI) => void {
  return (pi: ExtensionAPI): void => {
    pi.registerTool({
      name: "read",
      label: "Read bundled skill reference",
      description:
        "Read an instruction or Markdown reference file from a bundled Tether skill.",
      parameters: Type.Object({
        path: Type.String(),
        offset: Type.Optional(Type.Integer({ minimum: 1 })),
        limit: Type.Optional(Type.Integer({ minimum: 1 })),
      }),
      async execute(_toolCallId, params) {
        const canonicalPath = await realpath(
          resolve(params.path.replace(/^@/, "")),
        );
        const canonicalRoots = await Promise.all(
          skillRoots.map((root) => realpath(root)),
        );
        const allowed = canonicalRoots.some((root) => {
          const pathFromRoot = relative(root, canonicalPath);
          return (
            pathFromRoot !== ".." &&
            !pathFromRoot.startsWith(`..${sep}`) &&
            !isAbsolute(pathFromRoot)
          );
        });
        if (!allowed) {
          throw new Error("Read path is outside bundled skills");
        }
        if (extname(canonicalPath) !== ".md") {
          throw new Error("Only Markdown skill files are readable");
        }
        let content: string;
        try {
          content = new TextDecoder("utf-8", { fatal: true }).decode(
            await readFile(canonicalPath),
          );
        } catch {
          throw new Error("Bundled skill files must contain valid UTF-8 text");
        }
        const startLine = (params.offset ?? 1) - 1;
        const selectedText =
          params.offset === undefined && params.limit === undefined
            ? content
            : content
                .split("\n")
                .slice(startLine, startLine + (params.limit ?? Infinity))
                .join("\n");
        const truncation = truncateHead(selectedText, {
          maxBytes: DEFAULT_MAX_BYTES,
          maxLines: DEFAULT_MAX_LINES,
        });
        const text = truncation.truncated
          ? `${truncation.content}\n\n[Output truncated: ${String(truncation.outputLines)} of ${String(truncation.totalLines)} lines]`
          : truncation.content;
        return {
          content: [{ type: "text", text }],
          details: {},
        };
      },
    });
  };
}

const bundledSkillsRoot = join(
  dirname(fileURLToPath(import.meta.url)),
  "../skills",
);

export default createRestrictedSkillReadExtension([
  join(bundledSkillsRoot, "grilling"),
  join(bundledSkillsRoot, "writing-great-skills"),
]);
