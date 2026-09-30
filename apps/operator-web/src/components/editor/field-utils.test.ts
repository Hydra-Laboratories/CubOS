import { describe, expect, it } from "vitest";
import { isFieldEqual, normalizeYamlFilename } from "./field-utils";

describe("isFieldEqual", () => {
  it("treats strictly equal values as equal", () => {
    expect(isFieldEqual(5, 5)).toBe(true);
    expect(isFieldEqual("a", "a")).toBe(true);
  });

  it("treats null and undefined as equal (YAML omits optionals as null)", () => {
    expect(isFieldEqual(null, undefined)).toBe(true);
    expect(isFieldEqual(undefined, null)).toBe(true);
  });

  it("treats a number and its string form as equal (form round-trips)", () => {
    expect(isFieldEqual(5, "5")).toBe(true);
    expect(isFieldEqual("5", 5)).toBe(true);
  });

  it("treats genuinely different values as not equal", () => {
    expect(isFieldEqual(5, 6)).toBe(false);
    expect(isFieldEqual("a", "b")).toBe(false);
    expect(isFieldEqual(5, "6")).toBe(false);
  });
});

describe("normalizeYamlFilename", () => {
  it("appends .yaml and keeps existing yaml/yml extensions", () => {
    expect(normalizeYamlFilename("  my_deck ")).toBe("my_deck.yaml");
    expect(normalizeYamlFilename("deck.yaml")).toBe("deck.yaml");
    expect(normalizeYamlFilename("deck.yml")).toBe("deck.yml");
    expect(normalizeYamlFilename("   ")).toBe("");
  });
});
