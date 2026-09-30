import { expect, test } from "@playwright/test";
import { installApiMocks } from "./apiMocks";
import { writeFile } from "node:fs/promises";

test("creates a custom grid through calibration without moving hardware", async ({ page }, testInfo) => {
  const state = await installApiMocks(page, { connected: true });
  let saved: { labware: Record<string, { name: string; rows?: number; columns?: number; calibration?: unknown }> } | null = null;
  let x = 100;
  await page.route("**/api/v1/gantry/position", (route) => route.fulfill({ json: {
    x, y: 50, z: 20, work_x: x, work_y: 50, work_z: 20, status: "Idle", connected: true, calibration_active: false,
  } }));
  await page.route("**/api/v1/deck/cub_deck.yaml", async (route) => {
    if (route.request().method() === "PUT") saved = route.request().postDataJSON();
    if (!saved) return route.fallback();
    await route.fulfill({ json: { filename: "cub_deck.yaml", labware: Object.entries(saved.labware).map(([key, config]) => ({ key, config, wells: null })) } });
  });
  await page.goto("/");
  await page.getByLabel("Gantry config", { exact: true }).selectOption("cub.yaml");
  await page.getByRole("button", { name: "Deck", exact: true }).click();
  await page.getByLabel("Deck config", { exact: true }).selectOption("asmi_deck.yaml");
  await expect(page.locator("#plate_1-name")).toHaveValue("Plate 1");
  await page.locator("#plate_1-name").fill("Existing edited plate");
  await expect(page.getByRole("button", { name: "+ Well Plate", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Vial", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "New Labware", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Labware name", { exact: true }).fill("Custom 20 well plate");
  await dialog.getByLabel("Rows", { exact: true }).fill("4");
  await dialog.getByLabel("Columns", { exact: true }).fill("5");
  await dialog.getByLabel("Well spacing X (mm)", { exact: true }).fill("12.5");
  await dialog.getByLabel("Well spacing Y (mm)", { exact: true }).fill("14");
  await page.screenshot({ path: testInfo.outputPath("new-labware-form.png"), fullPage: true });
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await dialog.getByRole("button", { name: "Record A1", exact: true }).click();
  x = 112.5;
  await dialog.getByRole("button", { name: "Record A2", exact: true }).click();
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await dialog.getByRole("button", { name: "Save labware calibration", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#custom_20_well_plate-rows")).toHaveValue("4");
  await expect(page.locator("#custom_20_well_plate-cols")).toHaveValue("5");
  expect(saved!.labware.plate_1.name).toBe("Existing edited plate");
  await writeFile(testInfo.outputPath("saved-deck.json"), JSON.stringify(saved, null, 2));
  await page.reload();
  await page.getByRole("button", { name: "Deck", exact: true }).click();
  await expect(page.locator("#custom_20_well_plate-rows")).toHaveValue("4");
  await page.screenshot({ path: testInfo.outputPath("new-labware-saved.png"), fullPage: true });
  expect(state.requests.filter((request) => request.method === "POST" && request.path.startsWith("/gantry/"))).toHaveLength(0);
});
