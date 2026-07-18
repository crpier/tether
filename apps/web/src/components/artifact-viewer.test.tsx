import { cleanup, render, screen, waitFor } from "@solidjs/testing-library";
import { createSignal } from "solid-js";
import { afterEach, describe, expect, test, vi } from "vitest";

import { FakeApi, artifact } from "../testing/harness";
import { ArtifactOverlay, injectArtifactCsp } from "./artifact-viewer";

function noop(): void {
  // Deliberately empty: several tests exercise the overlay without caring
  // about the close callback.
}

afterEach(cleanup);

describe("injectArtifactCsp", () => {
  test("inserts the CSP meta tag right after an existing <head>", () => {
    const html = "<html><head><title>t</title></head><body>hi</body></html>";
    const injected = injectArtifactCsp(html);
    expect(injected).toContain(
      '<head><meta http-equiv="Content-Security-Policy"',
    );
    expect(injected).toContain(
      "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:",
    );
  });

  test("synthesizes a <head> inside an existing <html> with none", () => {
    const html = "<html><body>hi</body></html>";
    const injected = injectArtifactCsp(html);
    expect(injected).toBe(
      "<html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:\"></head><body>hi</body></html>",
    );
  });

  test("prepends a synthesized <head> for a bare fragment", () => {
    const html = "<p>hi</p>";
    const injected = injectArtifactCsp(html);
    expect(
      injected.startsWith('<head><meta http-equiv="Content-Security-Policy"'),
    ).toBe(true);
    expect(injected.endsWith("</head><p>hi</p>")).toBe(true);
  });
});

describe("ArtifactOverlay", () => {
  test("renders nothing when no artifact is open", () => {
    const api = new FakeApi({ authenticated: true });
    const { container } = render(() => (
      <ArtifactOverlay api={api} artifact={null} onClose={noop} />
    ));
    expect(
      container.querySelector("[aria-label='Artifact viewer']"),
    ).toBeNull();
  });

  test("fetches and mounts the artifact's HTML in a sandboxed iframe", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedArtifacts = [
      artifact({
        html: "<p>Hello artifact</p>",
        id: "018f0000-0000-7000-8000-0000000005aa",
        title: "Quiz",
        version: 3,
      }),
    ];

    const { container } = render(() => (
      <ArtifactOverlay
        api={api}
        artifact={{ id: "018f0000-0000-7000-8000-0000000005aa", title: "Quiz" }}
        onClose={noop}
      />
    ));

    const iframe = await waitFor(() => {
      const found = container.querySelector<HTMLIFrameElement>("iframe");
      expect(found).not.toBeNull();
      return found;
    });
    expect(iframe?.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe?.hasAttribute("allow-same-origin")).toBe(false);
    expect(iframe?.srcdoc).toContain("Hello artifact");
    expect(iframe?.srcdoc).toContain("Content-Security-Policy");
    expect(api.getArtifactCalls).toEqual([
      "018f0000-0000-7000-8000-0000000005aa",
    ]);
    expect(
      await waitFor(() => container.querySelector("[role='dialog']")),
    ).not.toBeNull();
    expect(container.textContent).toContain("Version 3");
  });

  test("relays a postMessage from the mounted iframe to the events API", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedArtifacts = [
      artifact({
        html: "<p>hi</p>",
        id: "018f0000-0000-7000-8000-0000000005bb",
      }),
    ];

    const { container } = render(() => (
      <ArtifactOverlay
        api={api}
        artifact={{ id: "018f0000-0000-7000-8000-0000000005bb", title: "Quiz" }}
        onClose={noop}
      />
    ));

    const iframe = await waitFor(() => {
      const found = container.querySelector<HTMLIFrameElement>("iframe");
      expect(found).not.toBeNull();
      return found;
    });

    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "answer", value: 3 },
        source: iframe?.contentWindow,
      }),
    );

    await waitFor(() => {
      expect(api.postArtifactEventCalls).toHaveLength(1);
    });
    expect(api.postArtifactEventCalls[0]).toEqual({
      artifactId: "018f0000-0000-7000-8000-0000000005bb",
      payload: { type: "answer", value: 3 },
    });
  });

  test("ignores a postMessage whose source is not the mounted iframe", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedArtifacts = [
      artifact({
        html: "<p>hi</p>",
        id: "018f0000-0000-7000-8000-0000000005cc",
      }),
    ];

    const { container } = render(() => (
      <ArtifactOverlay
        api={api}
        artifact={{ id: "018f0000-0000-7000-8000-0000000005cc", title: "Quiz" }}
        onClose={noop}
      />
    ));

    await waitFor(() => {
      expect(container.querySelector("iframe")).not.toBeNull();
    });

    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "spoofed" },
        source: window,
      }),
    );

    // Give any (wrongly-wired) relay a turn to run before asserting it didn't.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.postArtifactEventCalls).toHaveLength(0);
  });

  test("ignores a non-object postMessage payload", async () => {
    const api = new FakeApi({ authenticated: true });
    api.storedArtifacts = [
      artifact({
        html: "<p>hi</p>",
        id: "018f0000-0000-7000-8000-0000000005dd",
      }),
    ];

    const { container } = render(() => (
      <ArtifactOverlay
        api={api}
        artifact={{ id: "018f0000-0000-7000-8000-0000000005dd", title: "Quiz" }}
        onClose={noop}
      />
    ));

    const iframe = await waitFor(() => {
      const found = container.querySelector<HTMLIFrameElement>("iframe");
      expect(found).not.toBeNull();
      return found;
    });

    window.dispatchEvent(
      new MessageEvent("message", {
        data: "just a string",
        source: iframe?.contentWindow,
      }),
    );

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.postArtifactEventCalls).toHaveLength(0);
  });

  test("closing tears down the overlay; reopening re-fetches", async () => {
    const api = new FakeApi({ authenticated: true });
    const pointer = {
      id: "018f0000-0000-7000-8000-0000000005ee",
      title: "Quiz",
    };
    api.storedArtifacts = [artifact({ html: "<p>hi</p>", id: pointer.id })];
    const onClose = vi.fn();

    function Wrapper() {
      const [current, setCurrent] = createSignal<typeof pointer | null>(
        pointer,
      );
      return (
        <>
          <ArtifactOverlay
            api={api}
            artifact={current()}
            onClose={() => {
              onClose();
              setCurrent(null);
            }}
          />
          <button
            onClick={() => {
              setCurrent(pointer);
            }}
            type="button"
          >
            reopen
          </button>
        </>
      );
    }

    const { container } = render(() => <Wrapper />);

    await waitFor(() => {
      expect(container.querySelector("iframe")).not.toBeNull();
    });

    screen
      .getByLabelText("Close artifact")
      .dispatchEvent(new MouseEvent("click", { bubbles: true }));

    await waitFor(() => {
      expect(container.querySelector("iframe")).toBeNull();
    });
    expect(
      container.querySelector("[aria-label='Artifact viewer']"),
    ).toBeNull();

    screen
      .getByText("reopen")
      .dispatchEvent(new MouseEvent("click", { bubbles: true }));

    await waitFor(() => {
      expect(container.querySelector("iframe")).not.toBeNull();
    });
    expect(api.getArtifactCalls).toEqual([pointer.id, pointer.id]);
  });

  test("shows an error when the fetch fails", async () => {
    const api = new FakeApi({ authenticated: true });

    const { container } = render(() => (
      <ArtifactOverlay
        api={api}
        artifact={{ id: "018f0000-0000-7000-8000-000000009999", title: "Quiz" }}
        onClose={noop}
      />
    ));

    await waitFor(() => {
      expect(container.querySelector("[role='alert']")).not.toBeNull();
    });
    expect(container.querySelector("iframe")).toBeNull();
  });
});
