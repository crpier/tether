// Renders an `artifact` fence (#188) as a card — title plus an Open control —
// rather than a live-mounted widget: the fence only ever carries a pointer
// (`{"id", "title"}"), never the artifact's HTML, so there is nothing to
// render inline. Opening the card is handled by the caller via
// `onOpenArtifact`; this module owns only the card's DOM and the fence-JSON
// parse/validate step.
//
// Throws on unparseable or malformed JSON (missing/non-string `id`/`title`);
// the caller (the fence-dispatch pass in message-content.tsx) is responsible
// for catching that and falling back to a plain code block — same failure
// discipline as the other widgets, no partial-artifact-card state.
export interface ArtifactPointer {
  id: string;
  title: string;
}

export interface ArtifactWidgetContext {
  onOpenArtifact?: (artifact: ArtifactPointer) => void;
}

function parseArtifactPointer(specText: string): ArtifactPointer {
  const parsed: unknown = JSON.parse(specText);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("artifact fence must be a JSON object");
  }
  const { id, title } = parsed as Record<string, unknown>;
  if (typeof id !== "string" || id.length === 0) {
    throw new Error("artifact fence missing string `id`");
  }
  if (typeof title !== "string" || title.length === 0) {
    throw new Error("artifact fence missing string `title`");
  }
  return { id, title };
}

export function renderArtifactWidget(
  mount: HTMLElement,
  specText: string,
  context: ArtifactWidgetContext = {},
): Promise<void> {
  const pointer = parseArtifactPointer(specText);

  mount.setAttribute("data-artifact-id", pointer.id);
  mount.className =
    "flex items-center justify-between gap-3 rounded-md border bg-background/60 px-3 py-2";

  const label = document.createElement("p");
  label.className = "text-sm font-medium";
  label.textContent = pointer.title;
  mount.append(label);

  const openButton = document.createElement("button");
  openButton.type = "button";
  openButton.textContent = "Open";
  openButton.className =
    "shrink-0 rounded-md border px-2 py-1 text-xs font-medium hover:bg-muted";
  openButton.addEventListener("click", () => {
    context.onOpenArtifact?.(pointer);
  });
  mount.append(openButton);

  return Promise.resolve();
}
