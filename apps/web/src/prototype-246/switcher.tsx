// PROTOTYPE #246 — throwaway, do not ship
//
// Floating bottom-center variant switcher pill. Deliberately high-contrast /
// shadow-heavy so it reads as tooling, not part of the page being evaluated.

import { onCleanup, onMount } from "solid-js";

import { VARIANTS, cycleVariant, variant } from "./store";

const VARIANT_NAMES: Record<string, string> = {
  A: "master-detail",
  B: "stacked cards",
  C: "dense table",
};

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    target.isContentEditable ||
    target.getAttribute("contenteditable") === "true"
  );
}

export function PrototypeSwitcher() {
  const onKeyDown = (event: KeyboardEvent) => {
    if (isTypingTarget(event.target)) {
      return;
    }
    if (event.key === "ArrowLeft") {
      cycleVariant(-1);
    } else if (event.key === "ArrowRight") {
      cycleVariant(1);
    }
  };

  onMount(() => window.addEventListener("keydown", onKeyDown));
  onCleanup(() => window.removeEventListener("keydown", onKeyDown));

  return (
    <div class="fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 items-center gap-1 rounded-full border-2 border-yellow-400 bg-black px-2 py-1.5 text-white shadow-[0_0_0_4px_rgba(250,204,21,0.25),0_8px_24px_rgba(0,0,0,0.5)]">
      <button
        class="flex size-7 items-center justify-center rounded-full bg-yellow-400 text-black hover:bg-yellow-300"
        onClick={() => cycleVariant(-1)}
        type="button"
      >
        ←
      </button>
      <span class="min-w-40 px-2 text-center font-mono text-xs font-bold tracking-wide">
        PROTOTYPE {variant()} — {VARIANT_NAMES[variant()]}
      </span>
      <button
        class="flex size-7 items-center justify-center rounded-full bg-yellow-400 text-black hover:bg-yellow-300"
        onClick={() => cycleVariant(1)}
        type="button"
      >
        →
      </button>
      <span class="ml-1 pr-1 font-mono text-[10px] text-yellow-300">
        {VARIANTS.join(" · ")}
      </span>
    </div>
  );
}
