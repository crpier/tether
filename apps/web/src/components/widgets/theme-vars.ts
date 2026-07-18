// Widgets are rendered by libraries (Mermaid, vega-embed) that don't read the
// app's Tailwind theme directly. Reads Tether's existing CSS custom
// properties (the same ones the chat bubble and prose styling use) straight
// off the document rather than hardcoding a light-mode palette — that's what
// keeps a widget tracking the current light/dark theme instead of freezing on
// whichever theme was active when the library's own default kicked in.
//
// Tether's palette (app.css) is authored in `oklch(...)`. vega-embed's config
// (background/text colors, passed straight to SVG `fill`/CSS) accepts that
// natively — this raw value is fine for it. Mermaid is a different story
// (see mermaid-widget.ts): its color library can't parse `oklch()`, so it
// deliberately does *not* use this helper and instead switches between
// Mermaid's own built-in light/dark themes via `isDarkTheme` below.
export function readCssVar(name: string): string {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}

// Tether's dark mode is toggled by adding `data-kb-theme="dark"` somewhere in
// the document (see app.css's `@custom-variant dark`); this is a
// document-wide check (not scoped to a mount point still detached from the
// DOM at render time) so it works regardless of where in the tree that
// attribute ends up living.
export function isDarkTheme(): boolean {
  return document.querySelector('[data-kb-theme="dark"]') !== null;
}
