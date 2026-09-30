/** True when two JSON-ish values should be treated as equal for dirty
 * comparison. Handles the YAML edge cases this codebase hits: null vs
 * undefined (YAML writes `null` for omitted optional values while a
 * freshly-added instrument has `undefined`); numeric strings vs numbers
 * (round-trip through form inputs). Falls back to strict equality. */
export function isFieldEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a == null && b == null) return true;  // null ~ undefined
  if (typeof a === "number" && typeof b === "string") return String(a) === b;
  if (typeof a === "string" && typeof b === "number") return a === String(b);
  return false;
}

/** Trim a user-typed config name and give it a `.yaml` extension unless it
 * already ends in `.yaml` or `.yml`. Returns "" for blank input. */
export function normalizeYamlFilename(raw: string): string {
  const name = raw.trim();
  if (!name) return "";
  return /\.ya?ml$/i.test(name) ? name : `${name}.yaml`;
}
