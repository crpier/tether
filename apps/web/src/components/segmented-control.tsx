import { For } from "solid-js";
import type { JSX } from "solid-js";

import { Button } from "@/components/ui/button";

export function segmentedTabId(controlId: string, value: string): string {
  return `${controlId}-${value}-tab`;
}

export function segmentedPanelId(controlId: string, value: string): string {
  return `${controlId}-${value}-panel`;
}

// The repeated view-toggle idiom (Browse sections, Bucket
// active/history/triage, ...): a tabs pattern where
// exactly one option is selected at a time.
export function SegmentedControl<Value extends string>(props: {
  "aria-label": string;
  id: string;
  onChange: (value: Value) => void;
  options: { label: string; value: Value }[];
  value: Value;
}) {
  const buttons: HTMLButtonElement[] = [];

  const moveSelection = (index: number) => {
    props.onChange(props.options[index].value);
    buttons[index]?.focus();
  };

  const onKeyDown =
    (index: number): JSX.EventHandler<HTMLButtonElement, KeyboardEvent> =>
    (event) => {
      const last = props.options.length - 1;
      switch (event.key) {
        case "ArrowLeft":
          event.preventDefault();
          moveSelection(index === 0 ? last : index - 1);
          break;
        case "ArrowRight":
          event.preventDefault();
          moveSelection(index === last ? 0 : index + 1);
          break;
        case "End":
          event.preventDefault();
          moveSelection(last);
          break;
        case "Home":
          event.preventDefault();
          moveSelection(0);
          break;
      }
    };

  return (
    <div
      aria-label={props["aria-label"]}
      class="flex flex-wrap gap-1"
      role="tablist"
    >
      <For each={props.options}>
        {(option, index) => (
          <Button
            aria-controls={segmentedPanelId(props.id, option.value)}
            aria-selected={props.value === option.value}
            class="px-3"
            id={segmentedTabId(props.id, option.value)}
            onClick={() => {
              props.onChange(option.value);
            }}
            onKeyDown={onKeyDown(index())}
            ref={(element) => {
              buttons[index()] = element;
            }}
            role="tab"
            size="sm"
            tabIndex={props.value === option.value ? 0 : -1}
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
