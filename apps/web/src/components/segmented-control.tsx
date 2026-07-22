import { For } from "solid-js";

import { Button } from "@/components/ui/button";

// The repeated view-toggle idiom (Proposals queue/history/grants, Memories
// review/corpus, Bucket active/history/triage, ...): a small button group
// where exactly one option is pressed at a time.
export function SegmentedControl<Value extends string>(props: {
  "aria-label": string;
  onChange: (value: Value) => void;
  options: { label: string; value: Value }[];
  value: Value;
}) {
  return (
    <div aria-label={props["aria-label"]} class="flex gap-1" role="group">
      <For each={props.options}>
        {(option) => (
          <Button
            aria-pressed={props.value === option.value}
            onClick={() => {
              props.onChange(option.value);
            }}
            size="sm"
            type="button"
            variant={props.value === option.value ? "secondary" : "ghost"}
          >
            {option.label}
          </Button>
        )}
      </For>
    </div>
  );
}
