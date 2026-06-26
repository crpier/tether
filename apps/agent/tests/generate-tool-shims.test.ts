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
});
