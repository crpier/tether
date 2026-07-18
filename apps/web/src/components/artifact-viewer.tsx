import {
  Match,
  Show,
  Switch,
  createEffect,
  createSignal,
  onCleanup,
} from "solid-js";

import type { TetherApi } from "../api";
import type { ArtifactPointer } from "./widgets/artifact-widget";

// The CSP injected into every artifact's srcdoc document (#188, ADR 0011).
// Deliberately no `connect-src` and no external `img-src`/`font-src`: an
// artifact is a closed, self-contained document — it may run its own inline
// script/style and render data-URI images, but it cannot fetch anything.
// This is a client-side `<meta>` tag, not a host response header, because the
// HTML arrives as fetched JSON (`ArtifactRead.html`), not a served document
// response.
const ARTIFACT_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:";

// Prepends the CSP `<meta>` tag into `html`'s `<head>`, tolerating documents
// that omit `<head>` or even `<html>` (an agent-authored artifact may be a
// bare fragment) by synthesizing the wrapper it's missing.
export function injectArtifactCsp(html: string): string {
  const metaTag = `<meta http-equiv="Content-Security-Policy" content="${ARTIFACT_CSP}">`;
  const headMatch = /<head[^>]*>/i.exec(html);
  if (headMatch !== null) {
    const insertAt = headMatch.index + headMatch[0].length;
    return html.slice(0, insertAt) + metaTag + html.slice(insertAt);
  }
  const htmlMatch = /<html[^>]*>/i.exec(html);
  if (htmlMatch !== null) {
    const insertAt = htmlMatch.index + htmlMatch[0].length;
    return `${html.slice(0, insertAt)}<head>${metaTag}</head>${html.slice(insertAt)}`;
  }
  return `<head>${metaTag}</head>${html}`;
}

type ViewerState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; html: string; version: number };

const loadingState: ViewerState = { status: "loading" };

// Full-screen overlay (no router — a signal-driven overlay owned by the
// caller, consistent with the rest of the SPA) that fetches an artifact's
// latest HTML fresh on every open and mounts it in a sandboxed iframe.
//
// Talk-back: the artifact's only channel out is `window.parent.postMessage`.
// This listens on `window` (an iframe's `postMessage` target is always the
// parent window, never a specific element) and validates `event.source`
// against the *currently mounted* iframe's `contentWindow` before relaying —
// anything else (another frame, a stale/torn-down iframe) is ignored. The
// relayed payload is opaque JSON; only a bare, non-array object is forwarded,
// matching the fence-JSON discipline used elsewhere in the widget vocabulary.
export function ArtifactOverlay(props: {
  api: TetherApi;
  artifact: ArtifactPointer | null;
  onClose: () => void;
}) {
  const [state, setState] = createSignal<ViewerState>(loadingState);

  // Refetches whenever the pointed-at artifact id changes (including
  // null -> pointer on open). A response landing after the overlay has
  // already moved on (closed, or reopened on a different artifact) is
  // discarded rather than clobbering newer state.
  createEffect(() => {
    const pointer = props.artifact;
    if (pointer === null) {
      setState(loadingState);
      return;
    }
    const artifactId = pointer.id;
    setState(loadingState);
    void (async () => {
      try {
        const fetched = await props.api.getArtifact(artifactId);
        if (props.artifact?.id !== artifactId) {
          return;
        }
        setState({
          html: fetched.html,
          status: "ready",
          version: fetched.version,
        });
      } catch (caught) {
        if (props.artifact?.id !== artifactId) {
          return;
        }
        setState({
          message:
            caught instanceof Error
              ? caught.message
              : "Could not load artifact",
          status: "error",
        });
      }
    })();
  });

  // Wires the postMessage relay to whichever iframe element is currently
  // mounted. Called from the iframe's `ref`, so a fresh element (a re-open,
  // or a fetch settling into a new render) always gets its own listener, and
  // the previous one is torn down via `onCleanup` rather than accumulating.
  const attachRelay = (element: HTMLIFrameElement, artifactId: string) => {
    const listener = (event: MessageEvent) => {
      if (event.source !== element.contentWindow) {
        return;
      }
      const data: unknown = event.data;
      if (typeof data !== "object" || data === null || Array.isArray(data)) {
        return;
      }
      void props.api.postArtifactEvent(
        artifactId,
        data as Record<string, unknown>,
      );
    };
    window.addEventListener("message", listener);
    onCleanup(() => {
      window.removeEventListener("message", listener);
    });
  };

  return (
    <Show when={props.artifact}>
      {(pointer) => (
        <div
          aria-label="Artifact viewer"
          aria-modal="true"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
        >
          <div class="flex max-h-full w-full max-w-3xl flex-col overflow-hidden rounded-lg border bg-card shadow-lg">
            <header class="flex items-center justify-between gap-3 border-b px-4 py-2">
              <div>
                <h2 class="text-sm font-semibold">{pointer().title}</h2>
                <Show
                  when={
                    state().status === "ready"
                      ? (state() as Extract<ViewerState, { status: "ready" }>)
                      : undefined
                  }
                >
                  {(ready) => (
                    <p class="text-muted-foreground text-xs">
                      Version {ready().version}
                    </p>
                  )}
                </Show>
              </div>
              <button
                aria-label="Close artifact"
                class="shrink-0 rounded-md border px-2 py-1 text-xs font-medium hover:bg-muted"
                onClick={props.onClose}
                type="button"
              >
                Close
              </button>
            </header>
            <div class="flex-1 overflow-auto p-2">
              <Switch>
                <Match when={state().status === "loading"}>
                  <p class="text-muted-foreground p-3 text-sm">
                    Loading artifact…
                  </p>
                </Match>
                <Match
                  when={
                    state().status === "error"
                      ? (state() as Extract<ViewerState, { status: "error" }>)
                      : undefined
                  }
                >
                  {(errored) => (
                    <p class="text-destructive p-3 text-sm" role="alert">
                      {errored().message}
                    </p>
                  )}
                </Match>
                <Match
                  when={
                    state().status === "ready"
                      ? (state() as Extract<ViewerState, { status: "ready" }>)
                      : undefined
                  }
                >
                  {(ready) => (
                    <iframe
                      class="h-[60vh] w-full rounded border bg-white"
                      ref={(element) => {
                        attachRelay(element, pointer().id);
                      }}
                      sandbox="allow-scripts"
                      srcdoc={injectArtifactCsp(ready().html)}
                      title={pointer().title}
                    />
                  )}
                </Match>
              </Switch>
            </div>
          </div>
        </div>
      )}
    </Show>
  );
}
