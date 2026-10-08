import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import CampaignPanel from "./CampaignPanel";
import type { CampaignRecord } from "./types";

const record = (overrides: Partial<CampaignRecord> = {}): CampaignRecord => ({
  campaign_id: "c-1", spec: { name: "Sweep", gantry_file: "g.yaml", deck_file: "d.yaml", protocol_file: "p.yaml", parameters: [], sequences: [], objective: { mode: "result", path: "result.value", direction: "maximize" }, optimizer: { method: "ei", initial_trials: 3, exploration: .1, seed: 42 }, stop: { max_trials: 4, target_value: null, patience: 2, min_improvement: 0, max_seconds: null }, mock_mode: true, fluid_state_id: null }, state: "running", created_at: "", updated_at: "", active_run_id: "run-7", trials: [], best_objective: null, stop_reason: null, error: null, pause_requested: false, stop_requested: false, ...overrides,
});

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe("CampaignPanel", () => {
  it("validates a spec and binds a numeric protocol argument", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (String(input).endsWith("/validate")) {
        const body = JSON.parse(String(init?.body));
        expect(body.spec.name).toBe("pH sweep");
        expect(body.spec.parameters[0].bindings).toEqual([{ step_index: 0, argument: "volume" }]);
        return new Response(JSON.stringify({ valid: true, errors: [] }), { status: 200 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    render(<CampaignPanel gantryFile="g.yaml" deckFile="d.yaml" protocolFile="p.yaml" protocolSteps={[{ command: "aspirate", args: { volume: 5, well: "A1" } }]} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Campaign name"), { target: { value: "pH sweep" } });
    fireEvent.click(screen.getByRole("button", { name: "Add parameter" }));
    expect(screen.getByRole("checkbox", { name: "Step 1: aspirate.volume" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/campaigns/validate", expect.objectContaining({ method: "POST" })));
  });

  it("posts lifecycle controls and opens the active run", async () => {
    const selected = vi.fn();
    const current = record();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/campaigns")) return new Response(JSON.stringify([current]), { status: 200 });
      if (path.endsWith("/pause")) return new Response(JSON.stringify(record({ state: "paused" })), { status: 200 });
      return new Response(JSON.stringify(current), { status: 200 });
    });
    render(<CampaignPanel gantryFile="g" deckFile="d" protocolFile="p" onRunSelected={selected} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("running"));
    fireEvent.click(screen.getByRole("button", { name: "View current trial" }));
    expect(selected).toHaveBeenCalledWith("run-7");
    fireEvent.click(screen.getByRole("button", { name: "Pause after trial" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("paused"));
    expect(selected).toHaveBeenCalledTimes(1);
  });
});


it("submits a finite manual observation for the waiting trial", async () => {
  const waiting = record({ state: "awaiting_observation", active_run_id: null,
    trials: [{ index: 0, parameters: {x: 1}, run_id: "run-7", state: "succeeded", objective: null }] });
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (String(input).endsWith("/observation")) {
      expect(JSON.parse(String(init?.body))).toEqual({ value: 0 });
      return new Response(JSON.stringify({...waiting, state: "running"}),{status:200});
    }
    return new Response(JSON.stringify([waiting]),{status:200});
  });
  render(<CampaignPanel gantryFile="g" deckFile="d" protocolFile="p" />);
  const field = await screen.findByLabelText("Manual observation");
  fireEvent.change(field,{target:{value:"0"}});
  fireEvent.click(screen.getByRole("button",{name:"Submit observation"}));
  await waitFor(()=>expect(fetchMock).toHaveBeenCalledWith("/api/v1/campaigns/c-1/observation",expect.objectContaining({method:"POST"})));
});

it("sends optimizer controls and shared parameter bindings", async () => {
  let submitted: unknown;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (String(input).endsWith("/validate")) { submitted=JSON.parse(String(init?.body)); return new Response(JSON.stringify({valid:true,errors:[]}),{status:200}); }
    return new Response("[]",{status:200});
  });
  render(<CampaignPanel gantryFile="g" deckFile="d" protocolFile="p" protocolSteps={[{command:"transfer",args:{volume_ul:100}},{command:"transfer",args:{volume_ul:100}}]} />);
  fireEvent.change(screen.getByLabelText("Campaign name"),{target:{value:"Mixture"}});
  fireEvent.change(screen.getByLabelText("Optimizer method"),{target:{value:"lcb"}});
  fireEvent.change(screen.getByLabelText("Initial trials"),{target:{value:"2"}});
  fireEvent.change(screen.getByLabelText("Optimizer seed"),{target:{value:"17"}});
  fireEvent.click(screen.getByRole("button",{name:"Add parameter"}));
  fireEvent.click(screen.getByRole("checkbox",{name:"Step 2: transfer.volume_ul"}));
  fireEvent.click(screen.getByRole("button",{name:"Validate"}));
  await waitFor(()=>expect(submitted).toMatchObject({spec:{optimizer:{method:"lcb",initial_trials:2,seed:17},parameters:[{bindings:[{step_index:0,argument:"volume_ul"},{step_index:1,argument:"volume_ul"}]}]}}));
});
