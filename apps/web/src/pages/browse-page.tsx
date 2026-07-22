import { Match, Switch, createSignal } from "solid-js";

import { useAppContext } from "../app-context";
import { SegmentedControl } from "../components/segmented-control";
import { BucketPanel } from "../panels/bucket";
import { MemoriesPanel } from "../panels/memories";
import { SyntheticPanels } from "../panels/synthetic";
import { TodosPanel } from "../panels/todos";
import { TriggersPanel } from "../panels/triggers";

type BrowseView = "memories" | "bucket" | "todos" | "reminders" | "panels";

// Look-things-up state (#250): memory corpus search, todos, triggers, and the
// user's synthetic panels. This is deliberately not master-detail — nothing
// here is awaiting adjudication, so a page-level segmented control between
// the existing panel components is enough room.
export function BrowsePage() {
  const { api } = useAppContext();
  const [view, setView] = createSignal<BrowseView>("memories");

  return (
    <main
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
            {/* Review lives on the Inbox page; Browse only opens on Corpus. */}
            <MemoriesPanel api={api} initialView="corpus" />
          </Match>
          <Match when={view() === "bucket"}>
            <BucketPanel api={api} hiddenViews={["triage"]} />
          </Match>
          <Match when={view() === "todos"}>
            <TodosPanel api={api} />
          </Match>
          <Match when={view() === "reminders"}>
            <TriggersPanel api={api} />
          </Match>
          <Match when={view() === "panels"}>
            <SyntheticPanels api={api} />
          </Match>
        </Switch>
      </div>
    </main>
  );
}
