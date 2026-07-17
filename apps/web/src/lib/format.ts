// Display-only date formatting: day-first with slashes (DD/MM/YYYY), always.
// Never use these for machine-facing values (API payloads, `<input>` values,
// sort keys) — those must stay ISO.
export function formatDate(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  const day = pad(date.getDate());
  const month = pad(date.getMonth() + 1);
  const year = String(date.getFullYear()).padStart(4, "0");
  return `${day}/${month}/${year}`;
}

// DD/MM/YYYY date plus the locale's usual time-of-day rendering, joined the
// same way `Date.prototype.toLocaleString()` joins them.
export function formatDateTime(date: Date): string {
  return `${formatDate(date)}, ${date.toLocaleTimeString()}`;
}

export function formatSyncTimestamp(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) {
    return iso;
  }
  const elapsedMs = Date.now() - when.getTime();
  const minutes = Math.round(elapsedMs / 60_000);
  if (minutes < 1) {
    return "just now";
  }
  if (minutes < 60) {
    return `${String(minutes)}m ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${String(hours)}h ago`;
  }
  return formatDate(when);
}
