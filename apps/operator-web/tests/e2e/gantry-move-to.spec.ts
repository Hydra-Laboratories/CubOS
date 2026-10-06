import { expect, test } from "@playwright/test";
import { installApiMocks, requestsTo } from "./apiMocks";
import type { MockApiState } from "./apiMocks";
import type { Page } from "@playwright/test";

// Regression E2E for the Move To blank-field hazard: Number("") is 0, so a
// half-filled form used to command a silent move to 0 on the blank axes.

async function loadConnectedGantry(page: Page): Promise<MockApiState> {
  const state = await installApiMocks(page, { connected: true });
  await page.goto("/");
  await page.getByLabel("Gantry config", { exact: true }).selectOption("cub.yaml");
  // The Move To section renders once a config is selected and the polled
  // position reports connected.
  await expect(page.getByRole("button", { name: "Go" })).toBeVisible();
  return state;
}

test.describe("manual Move To safety", () => {
  test("blocks a move when an axis field is left blank", async ({ page }) => {
    const state = await loadConnectedGantry(page);

    await page.getByLabel("X (mm)").fill("100");
    await page.getByLabel("Y (mm)").fill("100");
    await page.getByRole("button", { name: "Go" }).click();

    await expect(page.getByText("Enter valid X, Y, and Z coordinates.")).toBeVisible();
    expect(requestsTo(state, "POST", "/gantry/move-to")).toHaveLength(0);
  });

  test("blocks a move when every field is blank", async ({ page }) => {
    const state = await loadConnectedGantry(page);

    await page.getByRole("button", { name: "Go" }).click();

    await expect(page.getByText("Enter valid X, Y, and Z coordinates.")).toBeVisible();
    expect(requestsTo(state, "POST", "/gantry/move-to")).toHaveLength(0);
  });

  test("sends a fully specified in-bounds move", async ({ page }) => {
    const state = await loadConnectedGantry(page);

    await page.getByLabel("X (mm)").fill("100");
    await page.getByLabel("Y (mm)").fill("50");
    await page.getByLabel("Z (mm)").fill("40");
    await page.getByRole("button", { name: "Go" }).click();

    await expect
      .poll(() => requestsTo(state, "POST", "/gantry/move-to"))
      .toEqual([
        expect.objectContaining({ body: { x: 100, y: 50, z: 40 } }),
      ]);
    await expect(page.getByText("Enter valid X, Y, and Z coordinates.")).toHaveCount(0);
  });

  test("rejects an out-of-volume move with an inline error", async ({ page }) => {
    const state = await loadConnectedGantry(page);

    await page.getByLabel("X (mm)").fill("301");
    await page.getByLabel("Y (mm)").fill("50");
    await page.getByLabel("Z (mm)").fill("40");
    await page.getByRole("button", { name: "Go" }).click();

    await expect(page.getByText(/Move target outside working volume/)).toBeVisible();
    expect(requestsTo(state, "POST", "/gantry/move-to")).toHaveLength(0);
  });
});
