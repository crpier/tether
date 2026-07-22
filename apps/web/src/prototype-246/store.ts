// PROTOTYPE #246 — throwaway, do not ship
//
// In-memory nav state (current page) + URL-synced variant state, shared by
// the shell and every page.

import { createSignal } from "solid-js";

import type { ProtoPage, ProtoVariant } from "./types";

export const [currentPage, setCurrentPage] = createSignal<ProtoPage>("chat");

const VARIANTS: ProtoVariant[] = ["A", "B", "C"];

function readVariantFromUrl(): ProtoVariant {
  const params = new URLSearchParams(location.search);
  const raw = params.get("variant");
  return raw === "A" || raw === "B" || raw === "C" ? raw : "A";
}

export const [variant, setVariantSignal] =
  createSignal<ProtoVariant>(readVariantFromUrl());

export function setVariant(next: ProtoVariant): void {
  setVariantSignal(next);
  const params = new URLSearchParams(location.search);
  params.set("variant", next);
  history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
}

export function cycleVariant(direction: 1 | -1): void {
  const index = VARIANTS.indexOf(variant());
  const next =
    VARIANTS[(index + direction + VARIANTS.length) % VARIANTS.length];
  setVariant(next);
}

export { VARIANTS };
