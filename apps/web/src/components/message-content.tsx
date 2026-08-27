import DOMPurify from "dompurify";
import { Marked, type MarkedToken, type Token, type Tokens } from "marked";
import {
  For,
  Show,
  createContext,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  useContext,
} from "solid-js";

import type {
  ArtifactPointer,
  ArtifactWidgetContext,
} from "./widgets/artifact-widget";
import { renderArtifactWidget } from "./widgets/artifact-widget";
import { renderMermaidWidget } from "./widgets/mermaid-widget";
import { renderVegaLiteWidget } from "./widgets/vega-lite-widget";
import { highlightCode, resolveHighlightLanguage } from "./code-highlighter";
import {
  EvidenceLink,
  evidenceTextParts,
  isEvidenceUri,
} from "./evidence-link";

// One Marked instance: GitHub-flavoured markdown with single newlines treated as
// line breaks (chat text rarely uses the double-newline paragraph convention).
const marked = new Marked({ gfm: true, breaks: true });

const proseClass =
  "w-full min-w-0 max-w-full text-sm break-words leading-relaxed";
const paragraphClass = "my-1 first:mt-0 last:mb-0";
const listClass = "my-1 pl-5";
const listItemClass = "my-0.5";
const linkClass = "underline underline-offset-2";
const inlineCodeClass = "rounded bg-background/60 px-1 py-0.5 text-[0.85em]";
const preClass = "my-2 overflow-x-auto rounded-md bg-background/70 p-3";
const blockCodeClass = "bg-transparent p-0";

function plainCodeClass(language: string | null): string {
  return language === null
    ? blockCodeClass
    : `${blockCodeClass} language-${language}`;
}
const blockquoteClass = "border-l-2 border-current/30 pl-3 opacity-80";
const tableClass = "w-full min-w-max border-collapse";
const tableCellClass = "border border-current/20 px-2 py-1";
const tableHeaderClass = `${tableCellClass} text-left`;
const headingClass = "mt-2 mb-1 font-semibold";

const OpenEvidenceContext = createContext<(uri: string) => void>();

// Force every rendered raw-HTML link to open in a new tab with tab-nabbing
// protection. Component-built links set these attributes directly; this hook is
// for DOMPurify-sanitized raw HTML passthrough and fallback rendering.
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

function sanitizeHtml(html: string): string {
  ensureNewTabHook();
  // ADD_ATTR opens target/rel so the new-tab hook's attributes survive; the
  // sanitizer still strips scripts/handlers and unsafe URL attributes.
  return DOMPurify.sanitize(html, { ADD_ATTR: ["target", "rel"] });
}

function sanitizeElementAttribute(
  tagName: "a" | "img",
  attribute: "href" | "src",
  value: string,
): string | undefined {
  const element = document.createElement(tagName);
  element.setAttribute(attribute, value);

  const template = document.createElement("template");
  template.innerHTML = sanitizeHtml(element.outerHTML);
  return (
    template.content.querySelector(tagName)?.getAttribute(attribute) ??
    undefined
  );
}

// Widget vocabulary v1 (ADR 0011) plus the `artifact` fence added in #188: a
// closed, literal switch over exactly these fence languages. GFM tables need no
// dispatch. Everything else falls through untouched to the plain code block —
// there is no generalized "looks like a widget" heuristic.
type WidgetLanguage = "artifact" | "mermaid" | "vega-lite";

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

function languageFromInfoString(infoString: string | undefined): string | null {
  const language = infoString?.trim().split(/\s+/, 1)[0];
  return language === undefined || language.length === 0 ? null : language;
}

// Highlight-language resolution for plain fences: widget-fence languages are
// excluded so they can never be highlighted, even while streaming (when they
// render as ordinary code blocks).
function highlightLanguageOf(infoString: string | undefined): string | null {
  if (widgetLanguageOf(infoString) !== null) {
    return null;
  }
  return resolveHighlightLanguage(infoString);
}

function widgetLanguageOf(
  infoString: string | undefined,
): WidgetLanguage | null {
  const language = languageFromInfoString(infoString);
  switch (language) {
    case "artifact":
    case "mermaid":
    case "vega-lite":
      return language;
    default:
      return null;
  }
}

function markdownTokens(tokens: Token[] | undefined): MarkedToken[] {
  return (tokens ?? []) as MarkedToken[];
}

function lexMarkdown(text: string): MarkedToken[] {
  return marked.lexer(text) as MarkedToken[];
}

function renderTokenFallback(token: MarkedToken): string {
  const html = marked.parser([token]);
  return sanitizeHtml(typeof html === "string" ? html : "");
}

function RawHtml(props: { html: string }) {
  return <span innerHTML={sanitizeHtml(props.html)} />;
}

function FallbackBlock(props: { token: MarkedToken }) {
  return <div innerHTML={renderTokenFallback(props.token)} />;
}

function InlineTokens(props: { tokens: MarkedToken[] }) {
  return (
    <For each={props.tokens}>{(token) => <InlineToken token={token} />}</For>
  );
}

function EvidenceText(props: { text: string }) {
  const openEvidence = useContext(OpenEvidenceContext);
  if (openEvidence === undefined) {
    return props.text;
  }
  return (
    <For each={evidenceTextParts(props.text)}>
      {(part) =>
        part.evidence ? (
          <EvidenceLink onOpen={openEvidence} uri={part.text}>
            {part.text}
          </EvidenceLink>
        ) : (
          part.text
        )
      }
    </For>
  );
}

function InlineToken(props: { token: MarkedToken }) {
  const token = props.token;

  switch (token.type) {
    case "br":
      return <br />;
    case "codespan": {
      const openEvidence = useContext(OpenEvidenceContext);
      return isEvidenceUri(token.text) && openEvidence !== undefined ? (
        <EvidenceLink
          class={`${inlineCodeClass} font-mono`}
          onOpen={openEvidence}
          uri={token.text}
        >
          {token.text}
        </EvidenceLink>
      ) : (
        <code class={inlineCodeClass}>{token.text}</code>
      );
    }
    case "del":
      return (
        <del>
          <InlineTokens tokens={markdownTokens(token.tokens)} />
        </del>
      );
    case "em":
      return (
        <em>
          <InlineTokens tokens={markdownTokens(token.tokens)} />
        </em>
      );
    case "escape":
      return token.text;
    case "html":
      return <RawHtml html={token.text} />;
    case "image": {
      const src = sanitizeElementAttribute("img", "src", token.href);
      if (src === undefined) {
        return token.text;
      }
      return (
        <img alt={token.text} src={src} title={token.title ?? undefined} />
      );
    }
    case "link": {
      const openEvidence = useContext(OpenEvidenceContext);
      if (isEvidenceUri(token.href) && openEvidence !== undefined) {
        return (
          <EvidenceLink onOpen={openEvidence} uri={token.href}>
            <InlineTokens tokens={markdownTokens(token.tokens)} />
          </EvidenceLink>
        );
      }
      const href = sanitizeElementAttribute("a", "href", token.href);
      return (
        <a
          class={linkClass}
          href={href}
          rel="noopener noreferrer"
          target="_blank"
          title={token.title ?? undefined}
        >
          <InlineTokens tokens={markdownTokens(token.tokens)} />
        </a>
      );
    }
    case "strong":
      return (
        <strong>
          <InlineTokens tokens={markdownTokens(token.tokens)} />
        </strong>
      );
    case "text":
      return token.tokens === undefined ? (
        <EvidenceText text={token.text} />
      ) : (
        <InlineTokens tokens={markdownTokens(token.tokens)} />
      );
    default:
      return token.raw;
  }
}

function CodeBlock(props: {
  // Raw fence-info language (first token of the info string) — kept on the
  // <code> element so the `pre code.language-<lang>` selector contract holds
  // regardless of highlighting.
  language: string | null;
  // Resolved Shiki grammar name, or null when this fence must never be
  // highlighted (unknown language, or a widget-fence language).
  highlightLanguage: string | null;
  streaming: boolean;
  text: string;
}) {
  const [highlighted, setHighlighted] = createSignal<string | null>(null);

  // Settle-gated per-block highlighting (B2): no per-token work while
  // streaming; unknown languages resolve to null and keep the plain block.
  createEffect(() => {
    const text = props.text;
    const highlightLanguage = props.highlightLanguage;
    const rawLanguage = props.language;
    const streaming = props.streaming;
    if (streaming || highlightLanguage === null) {
      setHighlighted(null);
      return;
    }

    let disposed = false;
    void highlightCode(text, highlightLanguage).then((html) => {
      if (!disposed && html !== null) {
        // Re-tag the raw fence-info language on Shiki's <code> so the
        // `pre code.language-<lang>` selector contract survives the swap.
        const template = document.createElement("template");
        template.innerHTML = html;
        const code = template.content.querySelector("code");
        if (rawLanguage !== null) {
          code?.classList.add(`language-${rawLanguage}`);
        }
        setHighlighted(template.innerHTML);
      }
    });
    onCleanup(() => {
      disposed = true;
    });
  });

  return (
    <Show
      fallback={
        <pre class={preClass}>
          <code class={plainCodeClass(props.language)}>{props.text}</code>
        </pre>
      }
      when={highlighted()}
    >
      <div class={preClass} innerHTML={highlighted() ?? undefined} />
    </Show>
  );
}

function WidgetBlock(props: {
  context: ArtifactWidgetContext;
  language: WidgetLanguage;
  spec: string;
}) {
  let mountEl: HTMLDivElement | undefined;
  const [failed, setFailed] = createSignal(false);

  createEffect(() => {
    const language = props.language;
    const spec = props.spec;
    const context = props.context;
    const mount = mountEl;
    if (mount === undefined) {
      return;
    }

    let disposed = false;
    setFailed(false);
    mount.replaceChildren();
    mount.className = "";
    mount.removeAttribute("data-artifact-id");

    void Promise.resolve()
      .then(() => widgetRenderers[language](mount, spec, context))
      .catch(() => {
        if (!disposed) {
          setFailed(true);
        }
      });

    onCleanup(() => {
      disposed = true;
    });
  });

  return (
    <Show
      fallback={
        <>
          <CodeBlock
            highlightLanguage={null}
            language={props.language}
            streaming={false}
            text={props.spec}
          />
          <p class="mt-1 text-xs opacity-70" data-widget-error={props.language}>
            Widget failed to render — showing raw source.
          </p>
        </>
      }
      when={!failed()}
    >
      <div
        data-widget={props.language}
        ref={(element) => {
          mountEl = element;
        }}
      />
    </Show>
  );
}

function Heading(props: { depth: number; tokens: MarkedToken[] }) {
  const className = `${headingClass} ${props.depth === 1 ? "text-base" : "text-sm"}`;

  switch (props.depth) {
    case 1:
      return (
        <h1 class={className}>
          <InlineTokens tokens={props.tokens} />
        </h1>
      );
    case 2:
      return (
        <h2 class={className}>
          <InlineTokens tokens={props.tokens} />
        </h2>
      );
    case 3:
      return (
        <h3 class={className}>
          <InlineTokens tokens={props.tokens} />
        </h3>
      );
    case 4:
      return (
        <h4 class={className}>
          <InlineTokens tokens={props.tokens} />
        </h4>
      );
    case 5:
      return (
        <h5 class={className}>
          <InlineTokens tokens={props.tokens} />
        </h5>
      );
    default:
      return (
        <h6 class={className}>
          <InlineTokens tokens={props.tokens} />
        </h6>
      );
  }
}

function TableCell(props: { cell: Tokens.TableCell }) {
  return (
    <Show
      fallback={
        <td
          class={tableCellClass}
          style={
            props.cell.align === null
              ? undefined
              : { "text-align": props.cell.align }
          }
        >
          <InlineTokens tokens={markdownTokens(props.cell.tokens)} />
        </td>
      }
      when={props.cell.header}
    >
      <th
        class={tableHeaderClass}
        style={
          props.cell.align === null
            ? undefined
            : { "text-align": props.cell.align }
        }
      >
        <InlineTokens tokens={markdownTokens(props.cell.tokens)} />
      </th>
    </Show>
  );
}

function BlockTokens(props: {
  context: ArtifactWidgetContext;
  streaming: boolean;
  tokens: MarkedToken[];
}) {
  return (
    <For each={props.tokens}>
      {(token) => (
        <BlockToken
          context={props.context}
          streaming={props.streaming}
          token={token}
        />
      )}
    </For>
  );
}

function BlockToken(props: {
  context: ArtifactWidgetContext;
  streaming: boolean;
  token: MarkedToken;
}) {
  const token = props.token;

  switch (token.type) {
    case "blockquote":
      return (
        <blockquote class={blockquoteClass}>
          <BlockTokens
            context={props.context}
            streaming={props.streaming}
            tokens={markdownTokens(token.tokens)}
          />
        </blockquote>
      );
    case "code": {
      const widgetLanguage = widgetLanguageOf(token.lang);
      if (widgetLanguage !== null && !props.streaming) {
        return (
          <WidgetBlock
            context={props.context}
            language={widgetLanguage}
            spec={token.text}
          />
        );
      }
      return (
        <CodeBlock
          highlightLanguage={highlightLanguageOf(token.lang)}
          language={languageFromInfoString(token.lang)}
          streaming={props.streaming}
          text={token.text}
        />
      );
    }
    case "def":
    case "space":
      return null;
    case "heading":
      return (
        <Heading depth={token.depth} tokens={markdownTokens(token.tokens)} />
      );
    case "hr":
      return <hr />;
    case "html":
      return <div innerHTML={sanitizeHtml(token.text)} />;
    case "list": {
      const className = token.ordered
        ? `${listClass} list-decimal`
        : `${listClass} list-disc`;
      const start =
        token.ordered && typeof token.start === "number"
          ? token.start
          : undefined;
      const items = (
        <For each={token.items}>
          {(item) => (
            <li class={listItemClass}>
              <Show when={item.task}>
                <input checked={item.checked} disabled type="checkbox" />{" "}
              </Show>
              <BlockTokens
                context={props.context}
                streaming={props.streaming}
                tokens={markdownTokens(item.tokens)}
              />
            </li>
          )}
        </For>
      );
      return token.ordered ? (
        <ol class={className} start={start}>
          {items}
        </ol>
      ) : (
        <ul class={className}>{items}</ul>
      );
    }
    case "paragraph":
      return (
        <p class={paragraphClass}>
          <InlineTokens tokens={markdownTokens(token.tokens)} />
        </p>
      );
    case "table":
      return (
        <div class="my-2 max-w-full overflow-x-auto">
          <table class={tableClass}>
            <thead>
              <tr>
                <For each={token.header}>
                  {(cell) => <TableCell cell={cell} />}
                </For>
              </tr>
            </thead>
            <tbody>
              <For each={token.rows}>
                {(row) => (
                  <tr>
                    <For each={row}>{(cell) => <TableCell cell={cell} />}</For>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </div>
      );
    case "text":
      return token.tokens === undefined ? (
        token.text
      ) : (
        <InlineTokens tokens={markdownTokens(token.tokens)} />
      );
    default:
      return <FallbackBlock token={token} />;
  }
}

// Render assistant text as markdown components. Used for settled and streaming
// messages alike; partial markdown during a stream degrades gracefully. Widget
// fences are components, but remain settle-gated so a streaming half-diagram is
// just a plain code block until the message is final.
export function MessageContent(props: {
  text: string;
  streaming?: boolean;
  onOpenArtifact?: (artifact: ArtifactPointer) => void;
  onOpenEvidence?: (uri: string) => void;
}) {
  const tokens = createMemo(() => lexMarkdown(props.text));
  const context = createMemo<ArtifactWidgetContext>(() => ({
    onOpenArtifact: (pointer) => {
      props.onOpenArtifact?.(pointer);
    },
  }));

  return (
    <OpenEvidenceContext.Provider value={props.onOpenEvidence}>
      <div class={proseClass}>
        <BlockTokens
          context={context()}
          streaming={props.streaming ?? false}
          tokens={tokens()}
        />
      </div>
    </OpenEvidenceContext.Provider>
  );
}
