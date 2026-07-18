import DOMPurify from "dompurify";
import { Marked } from "marked";
import { createEffect, createMemo } from "solid-js";

import type {
  ArtifactPointer,
  ArtifactWidgetContext,
} from "./widgets/artifact-widget";
import { renderArtifactWidget } from "./widgets/artifact-widget";
import { renderMermaidWidget } from "./widgets/mermaid-widget";
import { renderVegaLiteWidget } from "./widgets/vega-lite-widget";

// One Marked instance: GitHub-flavoured markdown with single newlines treated as
// line breaks (chat text rarely uses the double-newline paragraph convention).
const marked = new Marked({ gfm: true, breaks: true });

// Tailwind has no typography plugin here, so style the rendered markdown with
// child selectors. Kept terse; covers the structures pi actually emits.
const proseClass = [
  "text-sm break-words leading-relaxed",
  "[&_p]:my-1 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0",
  "[&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5",
  "[&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5",
  "[&_li]:my-0.5",
  "[&_a]:underline [&_a]:underline-offset-2",
  "[&_code]:bg-background/60 [&_code]:rounded [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.85em]",
  "[&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-background/70 [&_pre]:p-3",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_blockquote]:border-l-2 [&_blockquote]:border-current/30 [&_blockquote]:pl-3 [&_blockquote]:opacity-80",
  "[&_table]:my-2 [&_table]:w-full [&_table]:border-collapse",
  "[&_th]:border [&_th]:border-current/20 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left",
  "[&_td]:border [&_td]:border-current/20 [&_td]:px-2 [&_td]:py-1",
  "[&_h1]:mt-2 [&_h1]:mb-1 [&_h1]:text-base [&_h1]:font-semibold",
  "[&_h2]:mt-2 [&_h2]:mb-1 [&_h2]:text-sm [&_h2]:font-semibold",
  "[&_h3]:mt-2 [&_h3]:mb-1 [&_h3]:text-sm [&_h3]:font-semibold",
].join(" ");

// Force every rendered link to open in a new tab with tab-nabbing protection.
// A hook (not a marked renderer override) so it applies uniformly to autolinks,
// reference links, and inline links alike, and runs after sanitization sees the
// anchor. Registered lazily behind a guard rather than at import: DOMPurify
// hooks are process-global, so we avoid an import-time side-effect and only add
// the hook the first time we actually sanitize.
let newTabHookRegistered = false;
function ensureNewTabHook(): void {
  if (newTabHookRegistered) {
    return;
  }
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
  newTabHookRegistered = true;
}

// Widget vocabulary v1 (ADR 0011) plus the `artifact` fence added in #188: a
// closed, literal switch over exactly these fence languages. GFM tables need
// no dispatch — `marked`'s native table support plus the prose styling above
// already renders them as a first-class element. Everything else (typos,
// unsupported tags) falls through untouched to the plain code block `marked`
// already produces — there is no generalized "looks like a widget"
// heuristic, deliberately, so growing the vocabulary stays a reviewed code
// change (a new case here, a new renderer module) rather than something the
// agent can trigger by guessing a fence tag.
type WidgetLanguage = "artifact" | "mermaid" | "vega-lite";

// `artifact`'s renderer additionally takes a context carrying the
// open-overlay callback (see artifact-widget.ts); `mermaid`/`vega-lite`
// ignore the extra argument, which JS callbacks are free to do.
const widgetRenderers: Record<
  WidgetLanguage,
  (
    mount: HTMLElement,
    spec: string,
    context: ArtifactWidgetContext,
  ) => Promise<void>
> = {
  artifact: renderArtifactWidget,
  mermaid: renderMermaidWidget,
  "vega-lite": renderVegaLiteWidget,
};

function widgetLanguageOf(code: HTMLElement): WidgetLanguage | null {
  // `marked` tags a fenced code block's language on the <code> element as
  // `language-<lang>`, trimmed of any trailing info-string content — so a
  // typo'd or unsupported tag never matches the literal strings below.
  for (const lang of code.classList) {
    if (lang === "language-artifact") {
      return "artifact";
    }
    if (lang === "language-mermaid") {
      return "mermaid";
    }
    if (lang === "language-vega-lite") {
      return "vega-lite";
    }
  }
  return null;
}

// Walks a freshly-sanitized message fragment for `mermaid`/`vega-lite`/
// `artifact` fences and promotes each to a rendered widget in place of its
// `<pre>` block. Only called once a message has settled (see the `streaming`
// gate in `MessageContent`) — never on a per-token basis. Each widget's render is
// isolated: a throw from one (bad spec, a rejected Vega-Lite schema) leaves
// that fence's code block visible and appends a short inline note, without
// touching sibling widgets or the rest of the message.
async function dispatchWidgets(
  container: HTMLElement,
  context: ArtifactWidgetContext,
): Promise<void> {
  const codeBlocks = Array.from(
    container.querySelectorAll<HTMLElement>("pre > code"),
  );
  await Promise.all(
    codeBlocks.map(async (code) => {
      const lang = widgetLanguageOf(code);
      if (lang === null) {
        return;
      }
      const pre = code.parentElement;
      if (pre === null) {
        return;
      }
      const spec = code.textContent;
      const mount = document.createElement("div");
      mount.setAttribute("data-widget", lang);
      try {
        await widgetRenderers[lang](mount, spec, context);
        // The container may have been unmounted (or re-rendered with fresh
        // markup) while the widget's async render was in flight; skip the
        // DOM mutation rather than clobbering a stale/detached tree.
        if (!container.isConnected || pre.parentElement === null) {
          return;
        }
        pre.replaceWith(mount);
      } catch {
        if (!container.isConnected || pre.parentElement === null) {
          return;
        }
        const note = document.createElement("p");
        note.setAttribute("data-widget-error", lang);
        note.className = "mt-1 text-xs opacity-70";
        note.textContent = "Widget failed to render — showing raw source.";
        pre.insertAdjacentElement("afterend", note);
      }
    }),
  );
}

function renderMarkdown(text: string): string {
  ensureNewTabHook();
  // marked can return a Promise only with async extensions; this config is
  // synchronous, so the string branch always holds.
  const parsed = marked.parse(text);
  const html = typeof parsed === "string" ? parsed : "";
  // Sanitize model output before it touches innerHTML. ADD_ATTR opens target/rel
  // so the new-tab hook's attributes survive; the sanitizer still strips
  // scripts/handlers.
  return DOMPurify.sanitize(html, { ADD_ATTR: ["target", "rel"] });
}

// Render assistant text as sanitized markdown. Used for settled and streaming
// messages alike; partial markdown during a stream degrades gracefully (an
// unclosed code fence just renders as text until the closing fence arrives).
//
// `streaming` gates widget dispatch: while a message is still streaming,
// `mermaid`/`vega-lite` fences render as ordinary code blocks like any other
// fence (re-parsing/re-rendering a diagram on every delta would be wasted
// work at best, a flickering half-diagram at worst). Once a message
// transitions to settled, this re-renders once with fence interception on,
// promoting any recognized fence to a live widget. Defaults to settled
// (`false`) so callers rendering already-final text (e.g. tests) don't need
// to pass it.
export function MessageContent(props: {
  text: string;
  streaming?: boolean;
  onOpenArtifact?: (artifact: ArtifactPointer) => void;
}) {
  let containerEl: HTMLDivElement | undefined;
  const html = createMemo(() => renderMarkdown(props.text));

  createEffect(() => {
    // Track both signals so a message re-renders its widgets on the
    // streaming -> settled transition, not just on text changes.
    const currentHtml = html();
    const streaming = props.streaming ?? false;
    void currentHtml;
    if (containerEl === undefined || streaming) {
      return;
    }
    void dispatchWidgets(containerEl, {
      onOpenArtifact: (pointer) => {
        props.onOpenArtifact?.(pointer);
      },
    });
  });

  // innerHTML is fed only DOMPurify-sanitized markup (see renderMarkdown).
  return (
    <div
      ref={(el) => {
        containerEl = el;
      }}
      class={proseClass}
      innerHTML={html()}
    />
  );
}
