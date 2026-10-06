import { expect, test } from "@playwright/test";
import { installApiMocks } from "./apiMocks";

// Regression E2E for fluid-state seed validation: a component listed twice
// in one container row must block Run inline (the payload is keyed by
// component name, so the duplicate would silently overwrite and the server
// would reject the submit this validator claimed was fine).
test.describe("protocol fluid-state seed rows", () => {
  test("flags duplicate composition components and blocks Run until fixed", async ({ page }) => {
    await installApiMocks(page, { connected: true });
    await page.goto("/");

    // Load gantry and deck so the Protocol tab unlocks, then a protocol.
    await page.getByLabel("Gantry config", { exact: true }).selectOption("cub.yaml");
    await page.getByRole("button", { name: "Deck", exact: true }).click();
    await page.getByLabel("Deck config", { exact: true }).selectOption("asmi_deck.yaml");
    await page.getByRole("button", { name: "Protocol", exact: true }).click();
    await page.getByLabel("Protocol config", { exact: true }).selectOption("indentation.yaml");
    await expect(page.getByText("Step 1:")).toBeVisible();

    await page.getByRole("radio", { name: "New fluid state" }).check();
    await page.getByRole("button", { name: "Add container" }).click();
    await page.getByLabel("Seed container 1").fill("plate_1.A1");
    await page.getByLabel("Seed volume 1").fill("100");

    await page.getByLabel("Add component to container 1").click();
    await page.getByLabel("Seed 1 component 1 name").fill("water");
    await page.getByLabel("Seed 1 component 1 volume").fill("50");
    await page.getByLabel("Add component to container 1").click();
    await page.getByLabel("Seed 1 component 2 name").fill("water");
    await page.getByLabel("Seed 1 component 2 volume").fill("50");

    await expect(page.getByRole("alert")).toContainText('lists component "water" more than once');
    await expect(page.getByRole("button", { name: "Run Protocol" })).toBeDisabled();

    // Renaming the duplicate clears the error and unblocks Run.
    await page.getByLabel("Seed 1 component 2 name").fill("dmso");
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Run Protocol" })).toBeEnabled();
  });
});
