import { describe, expect, test } from "vitest";

import { formatToolResult } from "./tool-result";

describe("formatToolResult", () => {
  test("returns empty string for null/undefined", () => {
    expect(formatToolResult(null)).toBe("");
    expect(formatToolResult(undefined)).toBe("");
  });

  test("returns empty string for an empty object", () => {
    expect(formatToolResult({})).toBe("");
  });

  test("pretty-prints a plain object untouched when short", () => {
    expect(formatToolResult({ ok: true })).toBe(
      JSON.stringify({ ok: true }, null, 2),
    );
  });

  test("deep-parses a nested serialized-JSON string field", () => {
    const value = {
      transcript: JSON.stringify({ lines: ["a", "b"] }),
    };
    const result = formatToolResult(value);
    expect(result).toBe(
      JSON.stringify({ transcript: { lines: ["a", "b"] } }, null, 2),
    );
  });

  test("recursively deep-parses a serialized JSON string nested inside a parsed field", () => {
    // The outer field parses straight to an object; one of that object's own
    // fields is itself a serialized-JSON string, which should also unwrap.
    const value = {
      outer: JSON.stringify({ inner: JSON.stringify({ a: 1 }) }),
    };
    const result = formatToolResult(value);
    expect(result).toBe(
      JSON.stringify({ outer: { inner: { a: 1 } } }, null, 2),
    );
  });

  test("does not re-parse a string that decodes to another string (avoids mangling plain text)", () => {
    // value.outer, when JSON-parsed once, yields the plain string '{"a":1}'
    // rather than an object/array — parsing stops there (left as a string)
    // rather than chasing it through a second parse.
    const doublyEncoded = JSON.stringify(JSON.stringify({ a: 1 }));
    const value = { outer: doublyEncoded };
    const result = formatToolResult(value);
    const parsed = JSON.parse(result) as { outer: string };
    expect(parsed.outer).toBe(doublyEncoded);
  });

  test("does not parse JSON scalar strings (numbers, booleans, null)", () => {
    const value = { a: "123", b: "true", c: "null", d: "  " };
    const result = formatToolResult(value);
    expect(result).toBe(JSON.stringify(value, null, 2));
  });

  test("trims a string value longer than 30 chars to 30 chars + ellipsis", () => {
    const long = "a".repeat(31);
    const value = { text: long };
    const result = formatToolResult(value);
    const parsed = JSON.parse(result) as { text: string };
    expect(parsed.text).toBe(`${"a".repeat(30)}…`);
  });

  test("leaves a string of exactly 30 chars untouched", () => {
    const exact = "a".repeat(30);
    const value = { text: exact };
    const result = formatToolResult(value);
    const parsed = JSON.parse(result) as { text: string };
    expect(parsed.text).toBe(exact);
  });

  test("trims a top-level plain (non-JSON) string result", () => {
    const long = "b".repeat(45);
    expect(formatToolResult(long)).toBe(`${"b".repeat(30)}…`);
  });

  test("leaves a short top-level plain string untouched", () => {
    expect(formatToolResult("short string")).toBe("short string");
  });

  test("deep-parses a top-level string that is itself serialized JSON", () => {
    const value = JSON.stringify({ nested: JSON.stringify({ deep: true }) });
    const result = formatToolResult(value);
    expect(result).toBe(JSON.stringify({ nested: { deep: true } }, null, 2));
  });

  test("recurses into arrays, trimming and deep-parsing elements", () => {
    const value = [
      "c".repeat(40),
      JSON.stringify({ x: 1 }),
      { y: "d".repeat(40) },
    ];
    const result = formatToolResult(value);
    expect(result).toBe(
      JSON.stringify(
        [`${"c".repeat(30)}…`, { x: 1 }, { y: `${"d".repeat(30)}…` }],
        null,
        2,
      ),
    );
  });

  test("leaves numbers, booleans, and null values untouched", () => {
    const value = { n: 42, t: true, f: false, nil: null };
    expect(formatToolResult(value)).toBe(JSON.stringify(value, null, 2));
  });
});
