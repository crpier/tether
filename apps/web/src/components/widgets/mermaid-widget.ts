import type { Mermaid } from "mermaid";

import { isDarkTheme } from "./theme-vars";

// `mermaid` is heavy relative to the rest of the chat bundle and only needed
// once a settled message actually contains a `mermaid` fence. This module is
// imported statically by message-content.tsx (it's tiny), but the library
// itself is pulled in only inside `loadMermaid`, so the dynamic `import()`
// becomes its own bundle chunk that a widget-free conversation never fetches.
//
// Theming: Tether's palette (app.css) is authored in `oklch(...)`, which
// Mermaid's color library (khroma) cannot parse — handing it a raw
// `oklch()` custom property throws at render time. Rather than doing color
// math to convert, this points Mermaid at its own built-in `dark`/`default`
// theme, matched to Tether's current theme (the ADR 0011 spec's explicitly
// sanctioned fallback: "the closest built-in Mermaid theme that matches").
let mermaidPromise: Promise<Mermaid> | null = null;

function loadMermaid(): Promise<Mermaid> {
  mermaidPromise ??= import("mermaid").then((mod) => mod.default);
  return mermaidPromise;
}

let renderCounter = 0;

// Renders `spec` (raw Mermaid diagram text) into `mount`. Mermaid produces its
// own sanitized SVG string from the spec directly — this is not passed
// through DOMPurify, a deliberate exception documented in ADR 0011 (the
// closed fence vocabulary is the control, not sanitization). Throws on an
// invalid spec; the caller (the fence-dispatch pass in message-content.tsx)
// is responsible for catching that and falling back to a plain code block.
export async function renderMermaidWidget(
  mount: HTMLElement,
  spec: string,
): Promise<void> {
  const mermaid = await loadMermaid();
  // Re-initialize on every render (cheap — no diagram work happens here) so
  // a light/dark toggle between messages picks up the current theme, rather
  // than freezing on whichever theme was active the first time a mermaid
  // fence rendered.
  //
  // Strict security level refuses script-bearing / clickable node content
  // embedded in a diagram spec — the trust boundary here is ADR 0011's
  // constrained vocabulary, not sanitization, but strict mode is a free
  // extra guard against a spec that tries to smuggle a click handler.
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: isDarkTheme() ? "dark" : "default",
    themeVariables: { fontSize: "14px" },
  });
  renderCounter += 1;
  const id = `mermaid-widget-${renderCounter.toString()}`;
  const { svg } = await mermaid.render(id, spec);
  mount.innerHTML = svg;
}
