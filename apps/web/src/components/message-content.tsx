import DOMPurify from "dompurify";
import { Marked } from "marked";
import { createMemo } from "solid-js";

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
export function MessageContent(props: { text: string }) {
  const html = createMemo(() => renderMarkdown(props.text));
  // innerHTML is fed only DOMPurify-sanitized markup (see renderMarkdown).
  return <div class={proseClass} innerHTML={html()} />;
}
