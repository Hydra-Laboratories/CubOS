import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import CampaignRefillRecovery, { refillOperationKey } from "./CampaignRefillRecovery";
import type { CampaignRecord } from "./types";

const campaign = (): CampaignRecord => ({
  campaign_id: "campaign-48",
  spec: {
    name: "Color match", gantry_file: "g.yaml", deck_file: "d.yaml", protocol_file: "p.yaml",
    parameters: [], sequences: [], objective: { mode: "result", path: "1.delta_e_00", direction: "minimize" },
    optimizer: { method: "ei", kernel: "matern52", initial_trials: 6, initial_points: [], exploration: 0.1, seed: 1 },
    stop: { max_trials: 50, target_value: 5, patience: 50, min_improvement: 0, max_seconds: null },
    mock_mode: false, fluid_state_id: 6,
  },
  state: "awaiting_refill", created_at: "", updated_at: "", active_run_id: null, trials: [], best_objective: 9,
  stop_reason: null, error: "Pending batch needs stock refill.", pause_requested: false, stop_requested: false,
  pause_reason: "inventory_refill", pending_batch: { batch_index: 7, run_id: "pending-run-7" },
  refill_requirements: [
    { target: "10_vial_holder.A1", available_ul: 20, required_ul: 450, capacity_ul: 5000 },
    { target: "10_vial_holder.A2", available_ul: 15, required_ul: 350, capacity_ul: 5000 },
  ],
});

afterEach(() => vi.restoreAllMocks());

it("uses a different idempotency key when the same stock needs a later batch refill", () => {
  const requirement = campaign().refill_requirements![0];
  const first = campaign();
  const later = { ...campaign(), pending_batch: { batch_index: 8, run_id: "pending-run-8" } };
  expect(refillOperationKey(first, requirement)).not.toBe(refillOperationKey(later, requirement));
});

it("records final replacement volumes for every shortage before resuming the exact pending batch", async () => {
  const updated = { ...campaign(), state: "running" as const, refill_requirements: [] };
  const onUpdated = vi.fn();
  const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    requests.push({ path, body });
    if (path.endsWith("/resume")) return new Response(JSON.stringify(updated), { status: 200 });
    return new Response(JSON.stringify({ fluid_state_id: 6, ...body, status: "applied", composition: body.composition ?? { unknown: body.volume_ul } }), { status: 200 });
  });

  render(<CampaignRefillRecovery campaign={campaign()} onUpdated={onUpdated} />);
  expect(screen.getByText(/final total volume now present/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Resume pending batch" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Refill operator"), { target: { value: "Alex" } });
  fireEvent.change(screen.getByLabelText("Refill reason"), { target: { value: "Fresh diluted dyes loaded" } });

  fireEvent.change(screen.getByLabelText("10_vial_holder.A1 confirmed final volume"), { target: { value: "1000" } });
  fireEvent.change(screen.getByLabelText("10_vial_holder.A1 composition"), { target: { value: '{"red":1000}' } });
  fireEvent.click(screen.getByRole("button", { name: "Confirm 10_vial_holder.A1 refill" }));
  await screen.findByRole("button", { name: "Refill confirmed" });
  expect(screen.getByRole("button", { name: "Resume pending batch" })).toBeDisabled();

  fireEvent.change(screen.getByLabelText("10_vial_holder.A2 confirmed final volume"), { target: { value: "900" } });
  fireEvent.change(screen.getByLabelText("10_vial_holder.A2 composition"), { target: { value: '{"yellow":900}' } });
  fireEvent.click(screen.getByRole("button", { name: "Confirm 10_vial_holder.A2 refill" }));
  await waitFor(() => expect(screen.getAllByRole("button", { name: "Refill confirmed" })).toHaveLength(2));

  const resume = screen.getByRole("button", { name: "Resume pending batch" });
  expect(resume).toBeEnabled();
  fireEvent.click(resume);
  await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(updated));
  expect(requests.map((request) => request.path)).toEqual([
    "/api/v1/fluid-states/6/reconcile-stock",
    "/api/v1/fluid-states/6/reconcile-stock",
    "/api/v1/campaigns/campaign-48/resume",
  ]);
  expect(requests[0].body).toMatchObject({
    target: "10_vial_holder.A1", volume_ul: 1000, composition: { red: 1000 },
    operation_key: "campaign-campaign-48-pending-run-7-refill-10_vial_holder.A1", operator: "Alex", reason: "Fresh diluted dyes loaded",
  });
});

it("keeps entered refill evidence after an API error and never resumes", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("capacity changed", { status: 409 }));
  render(<CampaignRefillRecovery campaign={campaign()} onUpdated={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Refill operator"), { target: { value: "Alex" } });
  fireEvent.change(screen.getByLabelText("10_vial_holder.A1 confirmed final volume"), { target: { value: "1000" } });
  fireEvent.click(screen.getByRole("button", { name: "Confirm 10_vial_holder.A1 refill" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("capacity changed");
  expect(screen.getByLabelText("10_vial_holder.A1 confirmed final volume")).toHaveValue(1000);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Resume pending batch" })).toBeDisabled();
});
