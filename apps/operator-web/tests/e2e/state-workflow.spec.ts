import { expect, test } from "@playwright/test";
import { installApiMocks, requestsTo } from "./apiMocks";

async function loadProtocolSetup(page: import("@playwright/test").Page) {
  await page.goto("/");
  await page.getByLabel("Gantry config").selectOption("cub.yaml");
  await page.getByRole("button", { name: "Deck", exact: true }).click();
  await page.getByLabel("Deck config").selectOption("asmi_deck.yaml");
  await page.getByRole("button", { name: "Protocol", exact: true }).click();
  await page.getByLabel("Protocol config").selectOption("indentation.yaml");
  await expect(page.getByText("Step 1:")).toBeVisible();
}

test.describe("state workflow", () => {
  test("inspects state and selects the native new or resume workflow", async ({ page }, testInfo) => {
    const api = await installApiMocks(page, {
      fluidStates: { multiple: true, pendingStateId: 2 },
    });
    await loadProtocolSetup(page);

    await page.getByRole("radio", { name: "New fluid state" }).check();
    await page.getByLabel("Label for the new fluid state").fill("stale label");
    await page.getByRole("button", { name: "Add container" }).click();

    await page.getByRole("button", { name: "State", exact: true }).click();
    await expect(page.getByText("plate_1.A1")).toBeVisible();
    await expect(page.getByText("water: 50.000")).toBeVisible();
    await expect(page.getByText("This state cannot be resumed because it has pending or uncertain operations. Start a new state instead.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Resume state" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Resolve" })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("state-workflow.png"), fullPage: true });

    await page.getByRole("button", { name: "Start new state" }).click();
    await expect(page.getByRole("radio", { name: "New fluid state" })).toBeChecked();
    await expect(page.getByLabel("Label for the new fluid state")).toHaveValue("");
    await expect(page.getByText("No starting volumes — every container starts empty.")).toBeVisible();

    await page.getByRole("button", { name: "State", exact: true }).click();
    await page.getByLabel("Fluid state").selectOption("1");
    await expect(page.getByRole("button", { name: "Resume state" })).toBeEnabled();
    await page.getByRole("button", { name: "Resume state" }).click();

    await expect(page.getByRole("radio", { name: "Resume existing state" })).toBeChecked();
    await expect(page.getByLabel("Fluid state to resume")).toHaveValue("1");
    expect(requestsTo(api, "PATCH", "/fluid-states/1/containers/plate_1")).toHaveLength(0);
    expect(api.requests.filter((request) => request.method === "PATCH")).toHaveLength(0);
    expect(requestsTo(api, "POST", "/runs")).toHaveLength(0);
  });
});
