import { describe, expect, it, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import RunPanel from "./RunPanel";
import type { PlanStep, RunEvent, RunRecord, StepOutcome } from "../../types";

const PLAN: PlanStep[] = [
  { index: 0, command: "home", summary: "all axes", args: {} },
  { index: 1, command: "pick_up_tip", summary: "from tips.A1", args: {} },
  { index: 2, command: "transfer", summary: "stock.A1 → plate.B3   500 µL", args: {} },
];

let sequence = 0;

function stepEvent(
  index: number,
  command: string,
  outcome: StepOutcome,
  extra: Record<string, unknown> = {},
): RunEvent {
  sequence += 1;
  return {
    sequence,
    timestamp: sequence,
    state: "running",
    message: `step ${index} ${command} ${outcome}`,
    kind: "step",
    data: {
      index,
      command,
      substep: null,
      outcome,
      duration_s: null,
      error: null,
      reason: null,
      ...extra,
    },
  };
}

function record(state: RunRecord["state"], error: string | null = null): RunRecord {
  return {
    run_id: "run-1",
    state,
    created_at: 0,
    started_at: 0,
    finished_at: state === "running" ? null : 1,
    mock_mode: false,
    metadata: {},
    digests: {},
    result: null,
    error,
    artifacts: [],
    fluid_state_id: null,
  };
}

interface Scenario {
  plan?: PlanStep[];
  events?: RunEvent[];
  record?: RunRecord;
  planStatus?: number;
}

function installFetch(scenario: Scenario) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/plan")) {
      if (scenario.planStatus && scenario.planStatus >= 400) {
        return new Response("could not compile", { status: scenario.planStatus });
      }
      return new Response(
        JSON.stringify({ run_id: "run-1", steps: scenario.plan ?? PLAN }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (path.includes("/events")) {
      return new Response(
        JSON.stringify({ run_id: "run-1", events: scenario.events ?? [] }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(JSON.stringify(scenario.record ?? record("running")), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPanel(props: Partial<React.ComponentProps<typeof RunPanel>> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RunPanel runId="run-1" {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  sequence = 0;
  vi.unstubAllGlobals();
});

describe("RunPanel", () => {
  it("renders every planned step before anything has run", async () => {
    installFetch({});
    renderPanel();
    expect(await screen.findByText("home")).toBeInTheDocument();
    expect(screen.getByText("pick_up_tip")).toBeInTheDocument();
    // testing-library normalizes the summary's column whitespace.
    expect(screen.getByText(/stock\.A1 → plate\.B3\s+500 µL/)).toBeInTheDocument();
  });

  it("shows progress counts and the run state", async () => {
    installFetch({
      events: [
        stepEvent(0, "home", "completed"),
        stepEvent(1, "pick_up_tip", "started"),
      ],
    });
    renderPanel();
    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(await screen.findByText(/1 \/ 3 steps/)).toBeInTheDocument();
  });

  it("labels each step with its status for assistive tech", async () => {
    installFetch({
      events: [
        stepEvent(0, "home", "completed"),
        stepEvent(1, "pick_up_tip", "started"),
      ],
    });
    renderPanel();
    expect(await screen.findByLabelText("step 0 home done")).toBeInTheDocument();
    expect(screen.getByLabelText("step 1 pick_up_tip running")).toBeInTheDocument();
    expect(screen.getByLabelText("step 2 transfer pending")).toBeInTheDocument();
  });

  it("spells out why a step was skipped", async () => {
    installFetch({
      events: [
        stepEvent(1, "pick_up_tip", "skipped", {
          reason: "already applied on a previous run",
        }),
      ],
    });
    renderPanel();
    expect(
      await screen.findByText("already applied on a previous run"),
    ).toBeInTheDocument();
    expect(await screen.findByText(/1 skipped/)).toBeInTheDocument();
  });

  it("surfaces a failing step's error and the run error", async () => {
    installFetch({
      record: record("failed", "Gantry lost connection"),
      events: [
        stepEvent(1, "pick_up_tip", "failed", {
          error: "PipetteTimeoutError: no response",
        }),
      ],
    });
    renderPanel();
    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(
      await screen.findByText("PipetteTimeoutError: no response"),
    ).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Gantry lost connection",
    );
  });

  it("nests substeps beneath their step", async () => {
    installFetch({
      events: [
        stepEvent(2, "transfer", "started"),
        stepEvent(2, "transfer", "started", { substep: "leg0" }),
        stepEvent(2, "transfer", "completed", { substep: "leg0", duration_s: 0.4 }),
      ],
    });
    renderPanel();
    expect(await screen.findByLabelText("substep leg0 done")).toBeInTheDocument();
  });

  it("reports a plan that could not be compiled", async () => {
    installFetch({ planStatus: 422 });
    renderPanel();
    // The plan query retries once before surfacing the error.
    expect(
      await screen.findByText(/Could not load the step plan/, {}, { timeout: 4000 }),
    ).toBeInTheDocument();
  });

  it("offers cancel while running and hides it once terminal", async () => {
    const onCancel = vi.fn();
    installFetch({});
    const { unmount } = renderPanel({ onCancel });
    const cancel = await screen.findByRole("button", { name: "Cancel run" });
    await userEvent.click(cancel);
    expect(onCancel).toHaveBeenCalledOnce();
    unmount();

    installFetch({ record: record("succeeded") });
    renderPanel({ onCancel });
    await screen.findByText("Succeeded");
    expect(screen.queryByRole("button", { name: "Cancel run" })).toBeNull();
  });

  it("renders a large plan without choking", async () => {
    const plan: PlanStep[] = Array.from({ length: 500 }, (_, index) => ({
      index,
      command: "transfer",
      summary: `stock.A1 → plate.${index}   50 µL`,
      args: {},
    }));
    installFetch({
      plan,
      events: [
        stepEvent(249, "transfer", "completed", { duration_s: 0.2 }),
        stepEvent(250, "transfer", "started"),
      ],
    });
    renderPanel();
    expect(
      await screen.findByText(/1 \/ 500 steps/, {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("step 250 transfer running"),
    ).toBeInTheDocument();
  });

  /**
   * R-1: the whole view is a function of `(plan, events)` fetched by run id.
   * A component that has watched a run live and one freshly mounted onto the
   * same finished run must render identically — that is what makes a
   * mid-run page refresh safe.
   */
  it("renders identically when mounted fresh onto an in-progress run", async () => {
    const events = [
      stepEvent(0, "home", "completed", { duration_s: 1.2 }),
      stepEvent(1, "pick_up_tip", "started"),
    ];

    installFetch({ events: [events[0]] });
    const live = renderPanel();
    await screen.findByLabelText("step 0 home done");
    // Second batch arrives while mounted.
    installFetch({ events });
    await waitFor(
      () => expect(screen.getByLabelText("step 1 pick_up_tip running")).toBeInTheDocument(),
      { timeout: 3000 },
    );
    const watched = live.container.querySelector("[data-testid='step-list']")!.innerHTML;
    live.unmount();

    installFetch({ events });
    const remounted = renderPanel();
    await screen.findByLabelText("step 1 pick_up_tip running");
    const rebuilt = remounted.container.querySelector("[data-testid='step-list']")!.innerHTML;

    expect(rebuilt).toBe(watched);
  });
});
