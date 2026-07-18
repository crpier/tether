import { cleanup, fireEvent, render, waitFor } from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { MessageContent } from "./message-content";

// Widget dispatch mocks at the "renderer library call" boundary (mirrors how
// a network call would be mocked): the fence-detection/fallback logic in
// message-content.tsx runs for real, only the heavy rendering library call is
// replaced.
vi.mock("./widgets/mermaid-widget", () => ({
  renderMermaidWidget: vi.fn((mount: HTMLElement) => {
    mount.innerHTML = "<svg data-testid='mermaid-svg'></svg>";
    return Promise.resolve();
  }),
}));
vi.mock("./widgets/vega-lite-widget", () => ({
  renderVegaLiteWidget: vi.fn((mount: HTMLElement) => {
    mount.innerHTML = "<div data-testid='vega-view'></div>";
    return Promise.resolve();
  }),
}));

import { renderMermaidWidget } from "./widgets/mermaid-widget";
import { renderVegaLiteWidget } from "./widgets/vega-lite-widget";

const mermaidFence = "```mermaid\ngraph TD;\nA-->B;\n```";
const vegaFence = '```vega-lite\n{"mark": "bar"}\n```';

afterEach(() => {
  cleanup();
  vi.mocked(renderMermaidWidget).mockClear();
  vi.mocked(renderVegaLiteWidget).mockClear();
});

describe("MessageContent", () => {
  test("rendered links open in a new tab with tab-nabbing protection", () => {
    const { container } = render(() => (
      <MessageContent text="See [example](https://example.com)." />
    ));

    const link = container.querySelector("a");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
  });

  test("autolinks also get the new-tab attributes", () => {
    const { container } = render(() => (
      <MessageContent text="https://example.org/path" />
    ));

    const link = container.querySelector("a");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
  });

  test("script payloads are still stripped", () => {
    const { container } = render(() => (
      <MessageContent text={"<img src=x onerror=alert(1)>"} />
    ));

    expect(container.querySelector("img")?.getAttribute("onerror")).toBeNull();
  });

  test("a settled message with a mermaid fence mounts the widget", async () => {
    const { container } = render(() => (
      <MessageContent text={mermaidFence} streaming={false} />
    ));

    await waitFor(() => {
      expect(
        container.querySelector("[data-testid='mermaid-svg']"),
      ).not.toBeNull();
    });
    expect(container.querySelector("pre code.language-mermaid")).toBeNull();
    expect(renderMermaidWidget).toHaveBeenCalledTimes(1);
  });

  test("a settled message with a vega-lite fence mounts the widget", async () => {
    const { container } = render(() => (
      <MessageContent text={vegaFence} streaming={false} />
    ));

    await waitFor(() => {
      expect(
        container.querySelector("[data-testid='vega-view']"),
      ).not.toBeNull();
    });
    expect(renderVegaLiteWidget).toHaveBeenCalledTimes(1);
  });

  test("a streaming message with a mermaid fence still renders a plain code block", async () => {
    const { container } = render(() => (
      <MessageContent text={mermaidFence} streaming={true} />
    ));

    // Give any (wrongly-scheduled) async dispatch a turn to run before asserting.
    await Promise.resolve();
    expect(container.querySelector("pre code.language-mermaid")).not.toBeNull();
    expect(container.querySelector("[data-testid='mermaid-svg']")).toBeNull();
    expect(renderMermaidWidget).not.toHaveBeenCalled();
  });

  test("an unrecognized fence language always renders as a plain code block", async () => {
    const text = "```plantuml\n@startuml\n@enduml\n```";

    const settled = render(() => (
      <MessageContent text={text} streaming={false} />
    ));
    await Promise.resolve();
    expect(
      settled.container.querySelector("pre code.language-plantuml"),
    ).not.toBeNull();
    settled.unmount();

    const streaming = render(() => (
      <MessageContent text={text} streaming={true} />
    ));
    await Promise.resolve();
    expect(
      streaming.container.querySelector("pre code.language-plantuml"),
    ).not.toBeNull();
  });

  test("a widget renderer that throws leaves the code block and adds a failure note", async () => {
    vi.mocked(renderMermaidWidget).mockRejectedValueOnce(
      new Error("bad mermaid spec"),
    );

    const { container } = render(() => (
      <MessageContent text={mermaidFence} streaming={false} />
    ));

    await waitFor(() => {
      expect(container.querySelector("[data-widget-error]")).not.toBeNull();
    });
    expect(container.querySelector("pre code.language-mermaid")).not.toBeNull();
  });

  test("a settled message with an artifact fence renders a card", async () => {
    const onOpenArtifact = vi.fn();
    const fence =
      '```artifact\n{"id": "018f0000-0000-7000-8000-000000000abc", "title": "Quiz"}\n```';

    const { container } = render(() => (
      <MessageContent
        onOpenArtifact={onOpenArtifact}
        streaming={false}
        text={fence}
      />
    ));

    const card = await waitFor(() => {
      const found = container.querySelector<HTMLElement>(
        "[data-widget='artifact']",
      );
      expect(found).not.toBeNull();
      return found;
    });
    expect(card?.getAttribute("data-artifact-id")).toBe(
      "018f0000-0000-7000-8000-000000000abc",
    );
    expect(card?.textContent).toContain("Quiz");
    expect(container.querySelector("pre code.language-artifact")).toBeNull();

    const openButton = card?.querySelector("button");
    expect(openButton).not.toBeNull();
    if (openButton) {
      fireEvent.click(openButton);
    }
    expect(onOpenArtifact).toHaveBeenCalledTimes(1);
    expect(onOpenArtifact).toHaveBeenCalledWith({
      id: "018f0000-0000-7000-8000-000000000abc",
      title: "Quiz",
    });
  });

  test("an artifact fence with malformed JSON falls back to a code block", async () => {
    const fence = '```artifact\n{"id": "abc"\n```';

    const { container } = render(() => (
      <MessageContent streaming={false} text={fence} />
    ));

    await waitFor(() => {
      expect(container.querySelector("[data-widget-error]")).not.toBeNull();
    });
    expect(
      container.querySelector("pre code.language-artifact"),
    ).not.toBeNull();
    expect(container.querySelector("[data-widget='artifact']")).toBeNull();
  });

  test("an artifact fence missing required fields falls back to a code block", async () => {
    const fence =
      '```artifact\n{"id": "018f0000-0000-7000-8000-000000000abc"}\n```';

    const { container } = render(() => (
      <MessageContent streaming={false} text={fence} />
    ));

    await waitFor(() => {
      expect(container.querySelector("[data-widget-error]")).not.toBeNull();
    });
    expect(
      container.querySelector("pre code.language-artifact"),
    ).not.toBeNull();
  });

  test("a streaming message with an artifact fence still renders a plain code block", async () => {
    const fence =
      '```artifact\n{"id": "018f0000-0000-7000-8000-000000000abc", "title": "Quiz"}\n```';

    const { container } = render(() => (
      <MessageContent streaming={true} text={fence} />
    ));

    await Promise.resolve();
    expect(
      container.querySelector("pre code.language-artifact"),
    ).not.toBeNull();
    expect(container.querySelector("[data-widget='artifact']")).toBeNull();
  });

  test("a GFM table renders as sanitized table markup", () => {
    const text = "| a | b |\n| - | - |\n| 1 | 2 |";

    const { container } = render(() => <MessageContent text={text} />);

    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelectorAll("th")).toHaveLength(2);
    expect(container.querySelectorAll("td")).toHaveLength(2);
  });
});
