// Shiki syntax highlighting for `<CodeBlock>` (#537, phase B2). Fine-grained
// bundle: `shiki/core` plus the JS regex engine (no WASM), with a curated
// language list loaded on demand via dynamic `import()` — an unknown or
// missing language falls back to plain rendering (the caller renders its own
// `<pre><code>` when this returns null). Widget-fence languages (`mermaid`,
// `vega-lite`, `artifact`) are deliberately absent from the curated list, so
// they can never be highlighted.
//
// Dual light/dark themes (github-light/github-dark) are emitted with
// `defaultColor: false`: tokens carry the light color inline and the dark
// color in a `--shiki-dark` custom property, and app.css switches between
// them under `[data-kb-theme="dark"]` — same mechanism the rest of the app
// uses (see widgets/theme-vars.ts). The `pre` background is overridden to
// transparent in app.css so the block keeps the app's own `bg-background/70`.
import {
  createHighlighterCore,
  type HighlighterCore,
  type LanguageInput,
} from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";

type LanguageLoader = () => Promise<{ default: LanguageInput }>;

// Curated language list: the languages pi and the agent plausibly emit in
// chat. Growing this list is a reviewed code change, mirroring the widget
// vocabulary discipline in message-content.tsx.
const languageLoaders = new Map<string, LanguageLoader>([
  ["bash", () => import("@shikijs/langs/bash")],
  ["css", () => import("@shikijs/langs/css")],
  ["go", () => import("@shikijs/langs/go")],
  ["html", () => import("@shikijs/langs/html")],
  ["javascript", () => import("@shikijs/langs/javascript")],
  ["json", () => import("@shikijs/langs/json")],
  ["jsx", () => import("@shikijs/langs/jsx")],
  ["markdown", () => import("@shikijs/langs/markdown")],
  ["python", () => import("@shikijs/langs/python")],
  ["rust", () => import("@shikijs/langs/rust")],
  ["sql", () => import("@shikijs/langs/sql")],
  ["tsx", () => import("@shikijs/langs/tsx")],
  ["typescript", () => import("@shikijs/langs/typescript")],
  ["yaml", () => import("@shikijs/langs/yaml")],
]);

// Common fence-info aliases mapped onto curated grammar names.
const languageAliases: Record<string, string> = {
  js: "javascript",
  py: "python",
  sh: "bash",
  shell: "bash",
  ts: "typescript",
  yml: "yaml",
  zsh: "bash",
};

// One highlighter for the app's lifetime: themes load eagerly (small), the
// JS engine needs no async setup beyond the core promise.
let highlighterPromise: Promise<HighlighterCore> | null = null;

function getHighlighter(): Promise<HighlighterCore> {
  highlighterPromise ??= createHighlighterCore({
    engine: createJavaScriptRegexEngine(),
    langs: [],
    themes: [
      import("@shikijs/themes/github-light"),
      import("@shikijs/themes/github-dark"),
    ],
  });
  return highlighterPromise;
}

export function resolveHighlightLanguage(
  infoString: string | undefined,
): string | null {
  const language = infoString?.trim().split(/\s+/, 1)[0]?.toLowerCase();
  if (language === undefined || language.length === 0) {
    return null;
  }
  return languageAliases[language] ?? language;
}

// Returns Shiki HTML (`<pre class="shiki">…`) with dual light/dark themes, or
// null when the language is not in the curated list (caller renders plain).
export async function highlightCode(
  code: string,
  language: string,
): Promise<string | null> {
  const loader = languageLoaders.get(language);
  if (loader === undefined) {
    return null;
  }

  const highlighter = await getHighlighter();
  await highlighter.loadLanguage((await loader()).default);
  return highlighter.codeToHtml(code, {
    defaultColor: false,
    lang: language,
    themes: { dark: "github-dark", light: "github-light" },
  });
}
