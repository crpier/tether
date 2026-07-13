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
});
