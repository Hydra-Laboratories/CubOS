import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import CampaignPanel from "./CampaignPanel";
import type { CampaignRecord } from "./types";
import type { DeckResponse, GantryResponse } from "../../types";

vi.mock("../../hooks/useFluidState", () => ({
  useCreateFluidState: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

const record = (overrides: Partial<CampaignRecord> = {}): CampaignRecord => ({
  campaign_id: "c-1", spec: { name: "Sweep", gantry_file: "g.yaml", deck_file: "d.yaml", protocol_file: "p.yaml", parameters: [], sequences: [], objective: { mode: "result", path: "result.value", direction: "maximize" }, optimizer: { method: "ei", kernel: "matern52", initial_trials: 3, initial_points: [], exploration: .1, seed: 42 }, stop: { max_trials: 4, target_value: null, patience: 2, min_improvement: 0, max_seconds: null }, mock_mode: true, fluid_state_id: null }, state: "running", created_at: "", updated_at: "", active_run_id: "run-7", trials: [], best_objective: null, stop_reason: null, error: null, pause_requested: false, stop_requested: false, ...overrides,
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

  it("opens the running campaign camera from its loaded setup instead of the editable draft field", async () => {
    const current = record({ state: "running", spec: { ...record().spec, mock_mode: false, protocol_file: "p.yaml" } });
    const monitorStatus = {
      instrument: "snapshot_camera", state: "running", connected: true, lease_id: "lease-1", lease_expires_at: 15, subscriber_count: 1,
      camera_id: 0, requested_resolution: { width: 800, height: 600 }, actual_resolution: { width: 800, height: 600 },
      requested_pixel_format: null, actual_pixel_format: "BGR8", frame_id: null, received_at: null, frame_age_seconds: null,
      image_url: null, control_fingerprint: "x", run_id: "run-7", campaign_id: "c-1", trial_number: 1,
      step_index: null, step_command: null, step_substep: null, expected_well: null, well_identity_verification: "not_verified_by_cv",
      roi: null, quality: null, processing_profile: null, latest_analysis: null, warnings: [], error: null,
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.endsWith("/campaigns")) return new Response(JSON.stringify([current]), { status: 200 });
      if (path.includes("/camera/monitor")) {
        if (init?.method === "POST") {
          expect(JSON.parse(String(init.body))).toMatchObject({ instrument: "snapshot_camera" });
        }
        return new Response(JSON.stringify(monitorStatus), { status: 200 });
      }
      return new Response("[]", { status: 200 });
    });
    const gantry: GantryResponse = {
      filename: "g.yaml",
      config: { serial_port: "", gantry_type: "cub_xl", cnc: { factory_z_travel_mm: 80 }, working_volume: { x_min: 0, x_max: 300, y_min: 0, y_max: 200, z_min: 0, z_max: 80 }, instruments: { snapshot_camera: { type: "camera", vendor: "opencv", offset_x: 0, offset_y: 0, depth: -100 } } },
    };
    render(<CampaignPanel gantryFile="g.yaml" deckFile="d.yaml" protocolFile="p.yaml" gantry={gantry} protocolSteps={[{ command: "measure_color", args: { instrument: "snapshot_camera" } }]} />);
    await screen.findByLabelText("Live campaign camera");
    fireEvent.change(screen.getByLabelText("Color camera"), { target: { value: "editable_draft_camera" } });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/instruments/camera/monitor/start",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ instrument: "snapshot_camera" }) }),
    ));
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/start"))).toHaveLength(1);
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

it("offers safe recovery for an interrupted campaign whose last native run succeeded", async () => {
  const interrupted = record({
    state: "interrupted",
    trials: [{ index: 0, parameters: { x: 1 }, run_id: "run-7", state: "succeeded", objective: null }],
  });
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    if (path.endsWith("/resume")) return new Response(JSON.stringify({ ...interrupted, state: "running" }), { status: 200 });
    return new Response(JSON.stringify([interrupted]), { status: 200 });
  });
  render(<CampaignPanel gantryFile="g" deckFile="d" protocolFile="p" />);
  const resume = await screen.findByRole("button", { name: "Resume campaign" });
  fireEvent.click(resume);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/campaigns/c-1/resume", expect.objectContaining({ method: "POST" })));
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

it("builds the color-matching campaign preset", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]", { status: 200 }));
  const protocolSteps = [
    { command: "pick_up_tip", args: { position: "tips.A1" } },
    { command: "transfer", args: { volume_ul: 100, destination: "plate.A2" } },
    { command: "pick_up_tip", args: { position: "tips.A2" } },
    { command: "transfer", args: { volume_ul: 100, destination: "plate.A2" } },
    { command: "pick_up_tip", args: { position: "tips.A3" } },
    { command: "transfer", args: { volume_ul: 100, destination: "plate.A2" } },
    { command: "mix", args: { position: "plate.A2", volume_ul: 225, cycles: 3 } },
    { command: "move", args: { instrument: "camera", position: "plate.A2" } },
    { command: "measure_color", args: { instrument: "camera", position: "plate.A2", reference_lab: [50, 10, 20], reference_processing_profile_id: "profile-1" } },
  ];
  render(<CampaignPanel gantryFile="g" deckFile="d" protocolFile="p" protocolSteps={protocolSteps} />);
  fireEvent.click(screen.getByRole("button", { name: "Use loaded protocol" }));
  expect(screen.getByLabelText("Campaign name")).toHaveValue("CIEDE2000 color matching");
  expect(screen.getByLabelText("GP kernel")).toHaveValue("matern52");
  expect(screen.getByLabelText("Objective result path")).toHaveValue("8.delta_e_00");
  expect(screen.getByLabelText("Design 1 red_ul")).toHaveValue(200);
  expect(screen.getByLabelText("Design 6 blue_ul")).toHaveValue(125);
  expect(screen.getByLabelText("Sum constraint total")).toHaveValue(300);
  expect((screen.getByLabelText("Sequence 1 values") as HTMLTextAreaElement).value).toContain("plate.B12");
  expect(screen.getByText("Target Lab 50.0000, 10.0000, 20.0000")).toBeInTheDocument();
  expect(screen.getByText("Accepted target will be saved in p with its processing profile.")).toBeInTheDocument();
});

it("shows target, current, and best color results", async () => {
  const colorRecord = record({
    best_objective: 2.4,
    spec: {
      ...record().spec,
      parameters: [
        { name: "red_ul", minimum: 50, maximum: 200, step: 5, bindings: [{ step_index: 0, argument: "volume_ul" }] },
        { name: "yellow_ul", minimum: 50, maximum: 200, step: 5, bindings: [{ step_index: 1, argument: "volume_ul" }] },
        { name: "blue_ul", minimum: 50, maximum: 200, step: 5, bindings: [{ step_index: 2, argument: "volume_ul" }] },
      ],
      sum_constraint: { parameters: ["red_ul", "yellow_ul", "blue_ul"], total: 300 },
    },
    trials: [{
      index: 0,
      parameters: { red_ul: 125, yellow_ul: 50, blue_ul: 125 },
      run_id: "color-1",
      state: "succeeded",
      objective: 2.4,
      measurement: { rgb: [128, 90, 40], lab: [44, 15, 30], reference_lab: [45, 12, 28], delta_e_00: 2.4 },
    }],
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([colorRecord]), { status: 200 }));
  render(<CampaignPanel gantryFile="g" deckFile="d" protocolFile="p" />);
  const readout = await screen.findByLabelText("Color matching results");
  expect(readout).toHaveTextContent("Target");
  expect(readout).toHaveTextContent("Current");
  expect(readout).toHaveTextContent("Best formulation");
  expect(readout).toHaveTextContent("red_ul 125 µL");
});

it("previews labware-relative image height as carriage Z without inventing a default", () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]", { status: 200 }));
  const deck: DeckResponse = {
    filename: "d.yaml",
    labware: [{
      key: "plate",
      config: { type: "well_plate", name: "Plate", model_name: "plate", rows: 8, columns: 12, calibration: { a1: { x: 1, y: 2, z: 38.5 }, a2: { x: 2, y: 2, z: 38.5 } }, x_offset: 1, y_offset: 1 },
      wells: { A1: { x: 1, y: 2, z: 38.5 } },
    }],
  };
  const gantry: GantryResponse = {
    filename: "g.yaml",
    config: {
      serial_port: "",
      gantry_type: "cub_xl",
      cnc: { factory_z_travel_mm: 80 },
      working_volume: { x_min: 0, x_max: 300, y_min: 0, y_max: 200, z_min: 0, z_max: 80 },
      instruments: { camera: { type: "camera", vendor: "opencv", offset_x: 0, offset_y: 0, depth: -114.964 } },
    },
  };
  render(<CampaignPanel gantryFile="g.yaml" deckFile="d.yaml" protocolFile="p.yaml" deck={deck} gantry={gantry} />);
  expect(screen.getByLabelText("Color capture image height")).toHaveValue(null);
  fireEvent.change(screen.getByLabelText("Color capture image height"), { target: { value: "122.964" } });
  expect(screen.getByText(/carriage Z 46.500 mm/)).toBeInTheDocument();
  expect(screen.getByText(/Review physical camera clearance before running/)).toBeInTheDocument();
});

it("reviews a rejected target on the saved frame before building the color campaign", async () => {
  let setupBody: Record<string, unknown> | undefined;
  const prepared = {
    ...record().spec,
    name: "CIEDE2000 color matching",
    protocol_file: "ade_color_matching_1234.yaml",
    objective: { mode: "result" as const, path: "11.delta_e_00", direction: "minimize" as const },
  };
  const selectedRun = vi.fn();
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path.endsWith("/color-target")) {
      expect(JSON.parse(String(init?.body))).toMatchObject({
        target_well: "plate.C4",
        gantry_file: "g.yaml",
        deck_file: "d.yaml",
      });
      return new Response(JSON.stringify({
        run_id: "target-1",
        state: "succeeded",
        result: { results: [null, {
          measurement_status: "rejected",
          roi: { center_x_px: 455, center_y_px: 317, center_residual_px: 58 },
          quality: { flags: ["expected_center_unverified"], valid_fraction: 0.8, glare_fraction: 0.02 },
        }] },
        error: null,
      }), { status: 200 });
    }
    if (path.endsWith("/color-target/target-1/reanalyze")) {
      expect(JSON.parse(String(init?.body))).toEqual({
        expected_center: [0.56875, 317 / 600],
        expected_center_source: "operator_selected",
      });
      return new Response(JSON.stringify({
        measurement_status: "accepted",
        comparison_status: "not_requested",
        lab: [42, 12, 18],
        processing_profile: { id: "profile-v1", calibration_status: "uncalibrated" },
        analysis_revision: 1,
        quality: { accepted: true, flags: [], valid_fraction: 0.91, glare_fraction: 0.01 },
        roi: { center_x_px: 455, center_y_px: 317, center_residual_px: 2 },
      }), { status: 200 });
    }
    if (path.endsWith("/color-setup")) {
      setupBody = JSON.parse(String(init?.body));
      return new Response(JSON.stringify(prepared), { status: 200 });
    }
    return new Response("[]", { status: 200 });
  });
  render(<CampaignPanel gantryFile="g.yaml" deckFile="d.yaml" protocolFile="old.yaml" onRunSelected={selectedRun} />);
  expect(screen.getByText("g.yaml")).toBeInTheDocument();
  expect(screen.getByText("d.yaml")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Target well"), { target: { value: "plate.C4" } });
  fireEvent.click(screen.getByRole("button", { name: "Capture plate.C4 target for review" }));
  const savedImage = await screen.findByAltText("Saved target frame for plate.C4");
  expect(screen.getByText("target-1")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Open run" }));
  expect(selectedRun).toHaveBeenCalledWith("target-1");
  vi.spyOn(savedImage, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600, x: 0, y: 0, toJSON: () => ({}) });
  expect(screen.queryByRole("button", { name: "Build campaign from accepted target" })).not.toBeInTheDocument();
  expect(screen.getByText("expected_center_unverified")).toBeInTheDocument();
  fireEvent.click(savedImage, { clientX: 455, clientY: 317 });
  fireEvent.click(screen.getByRole("button", { name: "Analyze saved frame at selected center" }));
  await screen.findByRole("button", { name: "Build campaign from accepted target" });
  fireEvent.click(screen.getByRole("button", { name: "Build campaign from accepted target" }));
  await waitFor(() => expect(screen.getByLabelText("Campaign name")).toHaveValue("CIEDE2000 color matching"));
  expect(selectedRun).toHaveBeenCalledTimes(1);
  expect(setupBody).toMatchObject({
    target_well: "plate.C4",
    target_lab: [42, 12, 18],
    target_run_id: "target-1",
    target_analysis_revision: 1,
    reference_processing_profile_id: "profile-v1",
    expected_center: [0.56875, 317 / 600],
    expected_center_source: "operator_selected",
    red_source: "stocks.A1",
    yellow_source: "stocks.A2",
    blue_source: "stocks.A3",
  });
  expect(screen.getByText(/Campaign protocol ade_color_matching_1234.yaml is ready/)).toBeInTheDocument();
});

it("lets an accepted camera target be changed or retaken without keeping stale build evidence", async () => {
  localStorage.setItem("cubos.active-learning.target-review", JSON.stringify({
    runId: "accepted-target-1",
    measurement: {
      measurement_status: "accepted",
      lab: [42, 12, 18],
      processing_profile: { id: "profile-v1", calibration_status: "uncalibrated" },
    },
    selectedCenter: { x: 0.5, y: 0.5 },
    targetWell: "plate.C7",
    cameraInstrument: "camera",
    roiFraction: 0.5,
    captureImageHeight: "",
    selectionNeedsAnalysis: false,
  }));
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]", { status: 200 }));

  render(<CampaignPanel gantryFile="g.yaml" deckFile="d.yaml" protocolFile="p.yaml" />);
  expect(screen.getByRole("button", { name: "Change target well" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retake plate.C7 target photo" })).toBeInTheDocument();
  expect(screen.queryByLabelText("Target well")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Change target well" }));
  const targetWell = screen.getByLabelText("Target well");
  expect(targetWell).toHaveValue("plate.C7");
  expect(screen.getByRole("button", { name: "Build campaign from accepted target" })).toBeInTheDocument();
  fireEvent.change(targetWell, { target: { value: "plate.C6" } });
  expect(screen.queryByRole("button", { name: "Build campaign from accepted target" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Capture plate.C6 target for review" })).toBeEnabled();
});

it("keeps a failed target preflight visible beside the capture action", async () => {
  const selectedRun = vi.fn();
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    if (String(input).endsWith("/color-target")) {
      return new Response(JSON.stringify({
        run_id: "target-failed-1",
        state: "failed",
        result: null,
        error: "Routing preflight failed: camera instrument has no collision envelopes.",
      }), { status: 200 });
    }
    return new Response("[]", { status: 200 });
  });
  render(<CampaignPanel gantryFile="picus1000_arducam_usb_seed.yaml" deckFile="cub_deck.yaml" protocolFile="old.yaml" onRunSelected={selectedRun} />);
  fireEvent.click(screen.getByRole("button", { name: "Capture plate.A1 target for review" }));
  expect(await screen.findByText("target-failed-1")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Routing preflight failed");
  expect(screen.getByText("picus1000_arducam_usb_seed.yaml")).toBeInTheDocument();
  expect(screen.getByText("cub_deck.yaml")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Open run" }));
  expect(selectedRun).toHaveBeenCalledWith("target-failed-1");
});

it("reveals the water diluent fields only once enabled, with a stocks.A4 default", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]", { status: 200 }));
  render(<CampaignPanel gantryFile="g.yaml" deckFile="d.yaml" protocolFile="p.yaml" />);
  await screen.findByLabelText("Water (diluent)");

  expect(screen.getByLabelText("Water (diluent)")).not.toBeChecked();
  expect(screen.queryByLabelText("Water source")).not.toBeInTheDocument();

  fireEvent.click(screen.getByLabelText("Water (diluent)"));
  expect(screen.getByLabelText("Water source")).toHaveValue("stocks.A4");
  expect(screen.getByLabelText("Dye minimum microliters")).toHaveValue(50);
  expect(screen.getByLabelText("Dye maximum microliters")).toHaveValue(200);

  fireEvent.change(screen.getByLabelText("Water source"), { target: { value: "stocks.B1" } });
  expect(screen.getByLabelText("Water source")).toHaveValue("stocks.B1");

  fireEvent.click(screen.getByLabelText("Water (diluent)"));
  expect(screen.queryByLabelText("Water source")).not.toBeInTheDocument();
});
