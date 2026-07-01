import { cleanup, render } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { MessageContent } from "./message-content";

afterEach(cleanup);

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
});
