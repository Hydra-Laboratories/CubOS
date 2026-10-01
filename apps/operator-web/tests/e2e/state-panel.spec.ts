import { expect, test } from "@playwright/test";
import { installApiMocks } from "./apiMocks";

test.describe("State view", () => {
  test("auto-selects the newest fluid state and shows its containers", async ({ page }) => {
    await installApiMocks(page, { fluidStates: true });
    await page.goto("/");

    await page.getByRole("button", { name: "State", exact: true }).click();

    await expect(page.getByLabel("Fluid state")).toHaveValue("1");
    await expect(page.getByText("plate_1.A1")).toBeVisible();
    await expect(page.getByText("water: 50.000")).toBeVisible();
  });

  // Regression: picking the placeholder used to snap straight back to the
  // auto-selected newest state.
  test("keeps an explicit 'no state' selection", async ({ page }) => {
    await installApiMocks(page, { fluidStates: true });
    await page.goto("/");

    await page.getByRole("button", { name: "State", exact: true }).click();
    await expect(page.getByLabel("Fluid state")).toHaveValue("1");

    await page.getByLabel("Fluid state").selectOption("");

    await expect(page.getByText(/No saved state selected/)).toBeVisible();
    await expect(page.getByLabel("Fluid state")).toHaveValue("");
  });
});
