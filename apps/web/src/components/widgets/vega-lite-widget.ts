import { readCssVar } from "./theme-vars";

// `vega-embed` (and its `vega`/`vega-lite` peers) is heavy and only needed
// once a settled message actually contains a `vega-lite` fence — see
// mermaid-widget.ts for why the dynamic `import()` lives inside the render
// function rather than at module top-level.
async function loadEmbed() {
  const mod = await import("vega-embed");
  return mod.default;
}

// Renders `specText` (a JSON Vega-Lite spec) into `mount` via vega-embed.
// Throws on a spec that fails to parse as JSON or that vega-embed rejects as
// invalid; the caller (the fence-dispatch pass in message-content.tsx) is
// responsible for catching that and falling back to a plain code block.
export async function renderVegaLiteWidget(
  mount: HTMLElement,
  specText: string,
): Promise<void> {
  // JSON.parse throws SyntaxError on malformed input; that's exactly the
  // "invalid spec" case the caller catches, so no extra validation is added
  // here — the parse itself is the check.
  const spec: unknown = JSON.parse(specText);
  const embed = await loadEmbed();
  const background = readCssVar("--card");
  const textColor = readCssVar("--foreground");
  const axisColor = readCssVar("--border");
  // Actions menu (export/view-source) has no use in Tether's chat and is
  // extra surface for no benefit, so it's disabled outright (ADR 0011).
  await embed(mount, spec as object, {
    actions: false,
    renderer: "svg",
    config: {
      background,
      title: { color: textColor },
      axis: {
        labelColor: textColor,
        titleColor: textColor,
        domainColor: axisColor,
        tickColor: axisColor,
        gridColor: axisColor,
      },
      legend: { labelColor: textColor, titleColor: textColor },
    },
  });
}
