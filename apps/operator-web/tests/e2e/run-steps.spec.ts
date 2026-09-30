import { expect, test } from "@playwright/test";
import { installApiMocks, type RunScenario } from "./apiMocks";

/**
 * End-to-end coverage of the step-execution view across three representative
 * workflows. Each scenario freezes one exact moment of a run — the view is a
 * pure function of (plan, events), so there is nothing to race against.
 */

/** 1 — a liquid-handling run mid-transfer, with compound-command substeps. */
const LIQUID_HANDLING: RunScenario = {
  plan: [
    { index: 0, command: "home", summary: "all axes" },
    { index: 1, command: "pick_up_tip", summary: "from tips.A1" },
    { index: 2, command: "transfer", summary: "stock.A1 → plate.B3   500 µL" },
    { index: 3, command: "mix", summary: "plate.B3   200 µL   3x" },
    { index: 4, command: "serial_transfer", summary: "stock.A1 → plate ROW   10–100 µL" },
    { index: 5, command: "drop_tip", summary: "at trash" },
  ],
  events: [
    { index: 0, command: "home", outcome: "started" },
    { index: 0, command: "home", outcome: "completed", duration_s: 4.2 },
    { index: 1, command: "pick_up_tip", outcome: "started" },
    { index: 1, command: "pick_up_tip", outcome: "completed", duration_s: 6.8 },
    { index: 2, command: "transfer", outcome: "started" },
    { index: 2, command: "transfer", outcome: "started", substep: "stroke0" },
    { index: 2, command: "transfer", outcome: "completed", substep: "stroke0", duration_s: 11.4 },
    { index: 2, command: "transfer", outcome: "started", substep: "stroke1" },
  ],
  runState: "running",
};

/** 2 — a resumed run, where the fluid journal reports earlier work applied. */
const RESUMED_RUN: RunScenario = {
  plan: [
    { index: 0, command: "home", summary: "all axes" },
    { index: 1, command: "pick_up_tip", summary: "from tips.A4" },
    { index: 2, command: "transfer", summary: "stock.A1 → plate.A1   250 µL" },
    { index: 3, command: "transfer", summary: "stock.A1 → plate.A2   250 µL" },
    { index: 4, command: "transfer", summary: "stock.A1 → plate.A3   250 µL" },
    { index: 5, command: "drop_tip", summary: "at trash" },
  ],
  events: [
    { index: 0, command: "home", outcome: "started" },
    { index: 0, command: "home", outcome: "completed", duration_s: 4.1 },
    // A skipped command returns normally, so the engine emits `completed`
    // straight after each `skipped`. Feeding only the skip would let this
    // scenario pass against a reducer that drops it.
    {
      index: 1,
      command: "pick_up_tip",
      outcome: "skipped",
      reason: "already applied on a previous run",
    },
    { index: 1, command: "pick_up_tip", outcome: "completed", duration_s: 0.01 },
    {
      index: 2,
      command: "transfer",
      outcome: "skipped",
      reason: "already applied on a previous run",
    },
    { index: 2, command: "transfer", outcome: "completed", duration_s: 0.01 },
    {
      index: 3,
      command: "transfer",
      outcome: "skipped",
      reason: "already applied on a previous run",
    },
    { index: 3, command: "transfer", outcome: "completed", duration_s: 0.01 },
    { index: 4, command: "transfer", outcome: "started" },
  ],
  runState: "running",
};

/** 3 — a failed run: the failing step is named, later steps never ran. */
const FAILED_RUN: RunScenario = {
  plan: [
    { index: 0, command: "home", summary: "all axes" },
    { index: 1, command: "pick_up_tip", summary: "from tips.A1" },
    { index: 2, command: "transfer", summary: "stock.A1 → plate.B3   500 µL" },
    { index: 3, command: "drop_tip", summary: "at trash" },
  ],
  events: [
    { index: 0, command: "home", outcome: "started" },
    { index: 0, command: "home", outcome: "completed", duration_s: 4.0 },
    { index: 1, command: "pick_up_tip", outcome: "started" },
    { index: 1, command: "pick_up_tip", outcome: "completed", duration_s: 6.6 },
    { index: 2, command: "transfer", outcome: "started" },
    {
      index: 2,
      command: "transfer",
      outcome: "failed",
      duration_s: 121.3,
      error: "PipetteTimeoutError: Timed out (120.0s) waiting for response to command 12",
    },
  ],
  runState: "failed",
  runError: "PipetteTimeoutError: Timed out (120.0s) waiting for response to command 12",
};

async function startRun(page: import("@playwright/test").Page) {
  await page.goto("/");
  await page.getByLabel("Import gantry config").selectOption("cub.yaml");
  await page.getByRole("button", { name: "Deck", exact: true }).click();
  await page.getByLabel("Import deck config").selectOption("asmi_deck.yaml");
  await page.getByRole("button", { name: "Protocol", exact: true }).click();
  await page.getByLabel("Import protocol config").selectOption("indentation.yaml");
  await page.getByRole("button", { name: "Run Protocol" }).click();
  // Submitting switches the workspace into the Run view.
  await expect(page.getByRole("region", { name: "Run progress" })).toBeVisible();
  // The persistent right column keeps the live deck view alongside it.
  await expect(page.getByText("Deck Visualization")).toBeVisible();
}

test.describe("step-execution view", () => {
  test("shows a liquid-handling run in progress with substeps", async ({ page }) => {
    await installApiMocks(page, { connected: true, run: LIQUID_HANDLING });
    await startRun(page);

    await expect(page.getByLabel("step 0 home done")).toBeVisible();
    await expect(page.getByLabel("step 2 transfer running")).toBeVisible();
    await expect(page.getByLabel("step 5 drop_tip pending")).toBeVisible();
    // Compound-command legs nest under their parent step.
    await expect(page.getByLabel("substep stroke0 done")).toBeVisible();
    await expect(page.getByLabel("substep stroke1 running")).toBeVisible();
    await expect(page.getByText("2 / 6 steps")).toBeVisible();
  });

  test("distinguishes steps already applied on a previous run", async ({ page }) => {
    await installApiMocks(page, { connected: true, run: RESUMED_RUN });
    await startRun(page);

    await expect(page.getByLabel("step 1 pick_up_tip skipped")).toBeVisible();
    await expect(page.getByLabel("step 4 transfer running")).toBeVisible();
    // "Skipped" must read as work that happened, not work that was dropped.
    await expect(
      page.getByText("already applied on a previous run").first(),
    ).toBeVisible();
    await expect(page.getByText("3 skipped")).toBeVisible();
    // Steps after the active one were never reached — still pending.
    await expect(page.getByLabel("step 5 drop_tip pending")).toBeVisible();
  });

  test("names the failing step and leaves unreached steps pending", async ({ page }) => {
    await installApiMocks(page, { connected: true, run: FAILED_RUN });
    await startRun(page);

    await expect(page.getByText("Failed")).toBeVisible();
    await expect(page.getByLabel("step 2 transfer failed")).toBeVisible();
    await expect(page.getByText(/PipetteTimeoutError/).first()).toBeVisible();
    await expect(page.getByLabel("step 3 drop_tip pending")).toBeVisible();
    // A finished run offers no cancel.
    await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  });
});
