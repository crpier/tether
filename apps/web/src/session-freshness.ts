// Pure freshness check mirroring the host's session-rotation rule
// (`ConversationService.resolve_session` in apps/host/tether/conversations.py):
// a prompt landing after `session_gap_seconds` of idle time rotates onto a
// fresh pi session instead of reusing the warm one. The frontend never
// hardcodes the gap — it reads `session_gap_seconds` off the conversation the
// host already sends, so a host-side tuning change take effect without a web
// deploy.
//
// Unlike the host's own rotation decision (which treats "no prior activity"
// as warm — there's nothing stale to abandon), a `latestActivity` of `null`
// here is surfaced as "will start fresh": there's no warm context to lose,
// and the very first message *does* start the only session there is.

export function willStartFreshSession(
  latestActivity: string | null,
  sessionGapSeconds: number,
  now: number,
): boolean {
  if (latestActivity === null) {
    return true;
  }
  const last = Date.parse(latestActivity);
  if (Number.isNaN(last)) {
    return true;
  }
  return now - last > sessionGapSeconds * 1000;
}
