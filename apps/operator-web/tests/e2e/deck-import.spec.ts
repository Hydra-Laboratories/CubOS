import { expect, test } from "@playwright/test";
import { installApiMocks, requestsTo } from "./apiMocks";

test.describe("deck import flow", () => {
  test("copies the imported deck into the working file and labels the tab with the source name", async ({ page }) => {
    const state = await installApiMocks(page);
    await page.goto("/");

    await page.getByRole("button", { name: "Deck", exact: true }).click();
    await page.getByLabel("Deck config", { exact: true }).selectOption("asmi_deck.yaml");

    // The import writes a working copy instead of touching the source file.
    await expect
      .poll(() => requestsTo(state, "PUT", "/deck/cub_deck.yaml").length)
      .toBeGreaterThan(0);
    expect(requestsTo(state, "PUT", "/deck/asmi_deck.yaml")).toHaveLength(0);

    // The Deck tab shows the user-facing source filename, not the
    // working-copy name.
    await expect(page.getByRole("button", { name: "Deck", exact: true })).toContainText("asmi_deck.yaml");

    // The imported labware is editable in the deck editor.
    await expect(page.locator("#plate_1-name")).toHaveValue("Plate 1");
  });
});
