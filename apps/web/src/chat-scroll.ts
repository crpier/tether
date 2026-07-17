// Pure scroll-position math for the chat transcript viewport. Kept separate
// from the DOM wiring in chat-view.tsx so the "am I pinned to the bottom"
// decision — the crux of the anti-jitter rule — is unit-testable without a
// browser.
//
// The rule the UI implements around these helpers: pinned state only ever
// changes in response to a *user* scroll event (never a programmatic
// follow-scroll); on any content change, a pinned viewport snaps to the
// bottom instantly, and a non-pinned viewport is left completely alone.

// How close to the bottom (in px) still counts as "pinned". Generous enough
// to absorb rounding from fractional scroll heights, small enough that a
// deliberate scroll-up registers immediately.
export const PINNED_THRESHOLD_PX = 40;

export function isPinned(
  scrollTop: number,
  scrollHeight: number,
  clientHeight: number,
  threshold: number = PINNED_THRESHOLD_PX,
): boolean {
  return scrollHeight - scrollTop - clientHeight <= threshold;
}

// Scroll-restore math for prepending older messages at the top of the
// transcript. `overflow-anchor` is off (the pinned-follow rule owns scroll
// position), so a manual restore is required: record scrollHeight/scrollTop
// right before the prepend, then shift scrollTop by however much the content
// grew so the rows the user was looking at stay under them.
export function restoredScrollTop(
  scrollTopBeforePrepend: number,
  scrollHeightBeforePrepend: number,
  scrollHeightAfterPrepend: number,
): number {
  return (
    scrollTopBeforePrepend +
    (scrollHeightAfterPrepend - scrollHeightBeforePrepend)
  );
}
