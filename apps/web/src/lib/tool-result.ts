// Tool results are often huge, and are frequently JSON whose own fields hold
// serialized JSON strings (a transcript blob, a nested API payload, …). The
// transcript only needs to convey shape + gist, not completeness, so before
// display we deep-parse any string field that itself parses as JSON and trim
// long strings down to a preview.

const MAX_STRING_LENGTH = 30;

function trimString(value: string): string {
  return value.length > MAX_STRING_LENGTH
    ? `${value.slice(0, MAX_STRING_LENGTH)}…`
    : value;
}

// Parse `value` as JSON, but only accept the result if it's an object or
// array — a string like "123" or "true" parses fine as a JSON scalar, and
// replacing it would mangle ordinary text/numbers-as-strings that just
// happen to look numeric or boolean.
function tryParseJsonStructure(value: string): object | unknown[] | undefined {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return undefined;
  }
  return typeof parsed === "object" && parsed !== null ? parsed : undefined;
}

// Recursively replace any string that parses as a JSON object/array with the
// parsed structure (recursing into it too, in case it's serialized more than
// once), then trim any remaining string longer than MAX_STRING_LENGTH.
function deepParseAndTrim(value: unknown): unknown {
  if (typeof value === "string") {
    const parsed = tryParseJsonStructure(value);
    return parsed === undefined ? trimString(value) : deepParseAndTrim(parsed);
  }
  if (Array.isArray(value)) {
    return value.map(deepParseAndTrim);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, entry]) => [
        key,
        deepParseAndTrim(entry),
      ]),
    );
  }
  return value;
}

// Render a tool result for the transcript's disclosure: deep-parse any
// serialized-JSON strings, trim long strings to a preview, then pretty-print.
// Mirrors the plain-string/empty-object handling of the sibling args
// formatter so both disclosures look consistent, but only this one applies
// the deep-parse + trim treatment (arguments are left untouched).
export function formatToolResult(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  const transformed = deepParseAndTrim(value);
  if (typeof transformed === "string") {
    return transformed;
  }
  if (
    transformed !== null &&
    typeof transformed === "object" &&
    !Array.isArray(transformed) &&
    Object.keys(transformed).length === 0
  ) {
    return "";
  }
  try {
    return JSON.stringify(transformed, null, 2);
  } catch {
    return "[unserializable]";
  }
}
