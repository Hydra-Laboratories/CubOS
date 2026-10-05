import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import OvernightQueuePanel from "./OvernightQueuePanel";
import type { OvernightQueueRecord } from "./types";

const prepared: OvernightQueueRecord = {
  queue_id: "night-1", name: "Five colors", state: "prepared",
  created_at: "2026-10-05T20:00:00Z", updated_at: "2026-10-05T20:00:00Z",
  current_job_index: null, stop_reason: null, error: null, cancel_requested: false,
  resource_summary: { campaign_count: 5, sample_count: 40, tip_count: 85, total_volume_ul: 6000,
    candidate_wells: Array.from({ length: 40 }, (_, index) => `A${index + 1}`), fluid_state_id: 12,
    gantry_file: "gantry.yaml", deck_file: "deck.yaml" },
  jobs: Array.from({ length: 5 }, (_, index) => ({ job_id: `job-${index}`, index, name: `Target ${index + 1}`,
    state: "pending" as const, target_rgb: [100 + index, 20, 150] as [number, number, number], optimizer_seed: index,
    color_setup: {}, trials_completed: 0 })),
  events: [],
};

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><OvernightQueuePanel /></QueryClientProvider>);
}

afterEach(() => vi.restoreAllMocks());

describe("OvernightQueuePanel", () => {
  it("starts a prepared queue once and exposes cancel only after it is running", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/overnight")) return new Response(JSON.stringify([prepared]), { status: 200 });
      if (path.endsWith("/start")) return new Response(JSON.stringify({ ...prepared, state: "running", current_job_index: 0 }), { status: 202 });
      if (path.endsWith("/cancel")) return new Response(JSON.stringify({ ...prepared, state: "running", current_job_index: 0, cancel_requested: true }), { status: 200 });
      return new Response(JSON.stringify(prepared), { status: 200 });
    });
    renderPanel();
    const start = await screen.findByRole("button", { name: "Start overnight queue" });
    expect(screen.queryByRole("button", { name: "Cancel queue" })).not.toBeInTheDocument();
    fireEvent.click(start);
    fireEvent.click(start);
    const cancel = await screen.findByRole("button", { name: "Cancel queue" });
    expect(screen.queryByRole("button", { name: "Start overnight queue" })).not.toBeInTheDocument();
    fireEvent.click(cancel);
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel requested" })).toBeDisabled());
    expect(fetchMock.mock.calls.filter(([input, init]) => String(input).endsWith("/start") && init?.method === "POST")).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([input, init]) => String(input).endsWith("/cancel") && init?.method === "POST")).toHaveLength(1);
  });

  it("distinguishes target reached from budget exhausted in terminal results", async () => {
    const completed = { ...prepared, state: "completed" as const, jobs: prepared.jobs.map((job, index) => ({ ...job,
      state: "completed" as const, trials_completed: index === 0 ? 3 : 8, best_objective: index + 0.25,
      stop_reason: index === 0 ? "target_reached" : "trial_budget_exhausted" })) };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => String(input).endsWith("/overnight")
      ? new Response(JSON.stringify([completed]), { status: 200 })
      : new Response(JSON.stringify(completed), { status: 200 }));
    renderPanel();
    expect(await screen.findByText("Target reached")).toBeInTheDocument();
    expect((await screen.findAllByText("Budget exhausted"))).toHaveLength(4);
    expect(screen.queryByRole("button", { name: /Start|Cancel/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Mac webcam recording needs this browser/)).toBeInTheDocument();
  });

  it("shows a blocked refill queue as inspection-required without restart controls", async () => {
    const blocked = { ...prepared, state: "blocked" as const, stop_reason: "inventory_refill" };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => String(input).endsWith("/overnight")
      ? new Response(JSON.stringify([blocked]), { status: 200 })
      : new Response(JSON.stringify(blocked), { status: 200 }));
    renderPanel();
    expect(await screen.findByText("Queue stopped for inspection")).toBeInTheDocument();
    expect(screen.getByText("inventory refill")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Start overnight|Cancel queue/ })).not.toBeInTheDocument();
  });
});
