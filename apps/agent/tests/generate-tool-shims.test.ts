import { describe, expect, test } from "vitest";

import { renderTypeBoxProperty } from "../scripts/generate-tool-shims.js";

describe("renderTypeBoxProperty", () => {
  test("uses StringEnum for generated string enums", () => {
    const rendered = renderTypeBoxProperty(
      "state",
      { enum: ["loose", "tethered"], type: "string" },
      true,
    );

    expect(rendered).toBe('state: StringEnum(["loose", "tethered"] as const)');
  });

  test("keeps Pydantic defaults optional in TypeBox", () => {
    const rendered = renderTypeBoxProperty(
      "limit",
      { default: 50, exclusiveMinimum: 0, type: "integer" },
      false,
    );

    expect(rendered).toBe(
      "limit: Type.Optional(Type.Integer({ default: 50, exclusiveMinimum: 0 }))",
    );
  });

  test("preserves inclusive numeric bounds", () => {
    const rendered = renderTypeBoxProperty(
      "limit",
      { default: 20, maximum: 100, minimum: 1, type: "integer" },
      false,
    );

    expect(rendered).toBe(
      "limit: Type.Optional(Type.Integer({ default: 20, maximum: 100, minimum: 1 }))",
    );
  });

  test("unwraps a nullable optional to its inner type", () => {
    const rendered = renderTypeBoxProperty(
      "year",
      { anyOf: [{ type: "integer" }, { type: "null" }], default: null },
      false,
    );

    expect(rendered).toBe("year: Type.Optional(Type.Integer())");
  });

  test("unwraps a nullable optional boolean to Type.Boolean", () => {
    const rendered = renderTypeBoxProperty(
      "confirmed_correct",
      { anyOf: [{ type: "boolean" }, { type: "null" }], default: null },
      false,
    );

    expect(rendered).toBe("confirmed_correct: Type.Optional(Type.Boolean())");
  });

  test("unwraps a nullable optional enum to a StringEnum", () => {
    const rendered = renderTypeBoxProperty(
      "source",
      {
        anyOf: [
          { enum: ["liked", "watch_later"], type: "string" },
          { type: "null" },
        ],
        default: null,
      },
      false,
    );

    expect(rendered).toBe(
      'source: Type.Optional(StringEnum(["liked", "watch_later"] as const))',
    );
  });

  test("renders a string-map object as Type.Record", () => {
    const rendered = renderTypeBoxProperty(
      "facets",
      { additionalProperties: { type: "string" }, type: "object" },
      true,
    );

    expect(rendered).toBe("facets: Type.Record(Type.String(), Type.String())");
  });

  test("preserves array bounds", () => {
    const rendered = renderTypeBoxProperty(
      "entries",
      {
        items: { type: "string" },
        maxItems: 25,
        minItems: 1,
        type: "array",
      },
      true,
    );

    expect(rendered).toBe(
      "entries: Type.Array(Type.String(), { maxItems: 25, minItems: 1 })",
    );
  });

  test("renders a bounded scalar-map object as Type.Record", () => {
    const rendered = renderTypeBoxProperty(
      "values",
      {
        additionalProperties: {
          anyOf: [{ type: "boolean" }, { type: "integer" }, { type: "string" }],
        },
        maxProperties: 32,
        minProperties: 1,
        type: "object",
      },
      true,
    );

    expect(rendered).toBe(
      "values: Type.Record(Type.String(), Type.Union([Type.Boolean(), Type.Integer(), Type.String()]), { maxProperties: 32, minProperties: 1 })",
    );
  });

  test("unwraps a nullable optional string-map object to Type.Record", () => {
    const rendered = renderTypeBoxProperty(
      "facets",
      {
        anyOf: [
          { additionalProperties: { type: "string" }, type: "object" },
          { type: "null" },
        ],
        default: null,
      },
      false,
    );

    expect(rendered).toBe(
      "facets: Type.Optional(Type.Record(Type.String(), Type.String()))",
    );
  });
});
