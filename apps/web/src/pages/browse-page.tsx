import { Match, Switch, createSignal } from "solid-js";

import { useHost } from "../app-context";
import {
  SegmentedControl,
  segmentedPanelId,
  segmentedTabId,
} from "../components/segmented-control";
import { BucketPanel, type BucketView } from "../panels/bucket";
import { MemoriesPanel, type MemoryEditing } from "../panels/memories";
import { SyntheticPanels } from "../panels/synthetic";
import { TodosPanel } from "../panels/todos";
import { TriggersPanel } from "../panels/triggers";

export type BrowseView =
  "memories" | "bucket" | "todos" | "reminders" | "panels";

// Look-things-up state (#250): memory corpus search, todos, triggers, and the
// user's synthetic panels. This is deliberately not master-detail — nothing
// here is awaiting adjudication, so a page-level segmented control between
// the existing panel components is enough room.
export function BrowsePage(
  props: { initialBucketView?: BucketView; initialView?: BrowseView } = {},
) {
  const bucket = useHost("bucket");
  const memories = useHost("memories");
  const panels = useHost("panels");
  const todos = useHost("todos");
  const triggers = useHost("triggers");
  const [view, setView] = createSignal<BrowseView>(
    props.initialView ?? "memories",
  );
  const [memoryEditing, setMemoryEditing] = createSignal<
    MemoryEditing | undefined
  >();
  const [memoryDraft, setMemoryDraft] = createSignal("");

  return (
    <section
      aria-labelledby="browse-title"
      class="flex min-h-full flex-1 flex-col"
    >
      <header class="bg-card flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3 sm:px-5">
        <h1
          id="browse-title"
          class="mr-auto text-lg font-semibold tracking-tight"
        >
          Browse
        </h1>
        <SegmentedControl
          aria-label="Browse view"
          id="browse-view"
          onChange={setView}
          options={[
            { label: "Memories", value: "memories" },
            { label: "Bucket", value: "bucket" },
            { label: "Todos", value: "todos" },
            { label: "Reminders", value: "reminders" },
            { label: "Panels", value: "panels" },
          ]}
          value={view()}
        />
      </header>
      <div class="mx-auto w-full max-w-3xl flex-1 space-y-4 p-4 sm:p-5">
        <Switch>
          <Match when={view() === "memories"}>
            <div
              aria-labelledby={segmentedTabId("browse-view", "memories")}
              id={segmentedPanelId("browse-view", "memories")}
              role="tabpanel"
            >
              {/* Review lives on the Inbox page; Browse only opens on Corpus.
                  Keep the edit draft in Browse so inactive controls can unmount
                  without losing in-progress edits. */}
              <MemoriesPanel
                api={memories}
                draft={memoryDraft}
                editing={memoryEditing}
                initialView="corpus"
                setDraft={setMemoryDraft}
                setEditing={setMemoryEditing}
              />
            </div>
          </Match>
          <Match when={view() === "bucket"}>
            <div
              aria-labelledby={segmentedTabId("browse-view", "bucket")}
              id={segmentedPanelId("browse-view", "bucket")}
              role="tabpanel"
            >
              <BucketPanel
                api={bucket}
                hiddenViews={["triage"]}
                initialView={props.initialBucketView}
              />
            </div>
          </Match>
          <Match when={view() === "todos"}>
            <div
              aria-labelledby={segmentedTabId("browse-view", "todos")}
              id={segmentedPanelId("browse-view", "todos")}
              role="tabpanel"
            >
              <TodosPanel api={todos} />
            </div>
          </Match>
          <Match when={view() === "reminders"}>
            <div
              aria-labelledby={segmentedTabId("browse-view", "reminders")}
              id={segmentedPanelId("browse-view", "reminders")}
              role="tabpanel"
            >
              <TriggersPanel api={triggers} />
            </div>
          </Match>
          <Match when={view() === "panels"}>
            <div
              aria-labelledby={segmentedTabId("browse-view", "panels")}
              id={segmentedPanelId("browse-view", "panels")}
              role="tabpanel"
            >
              <SyntheticPanels api={panels} />
            </div>
          </Match>
        </Switch>
      </div>
    </section>
  );
}
