import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type {
  DeckConfig,
  DeckResponse,
  FluidStateSummary,
  GantryConfig,
  GantryPosition,
  GantryResponse,
  ProtocolConfig,
  ProtocolResponse,
  ProtocolRunStatus,
  WellPlateConfig,
  WellPosition,
} from "./types";

type ApiState = {
  decks: Record<string, DeckResponse>;
  gantries: Record<string, GantryResponse>;
  protocols: Record<string, ProtocolResponse>;
};

let lastSubmittedBody: Record<string, unknown> | null = null;

type FetchMockOptions = {
  protocolRun?: (init?: RequestInit) => Promise<Response>;
  protocolCancel?: () => Response | Promise<Response>;
  runStatus?: () => ProtocolRunStatus;
  validateSetup?: () => Response | Promise<Response>;
  calibrationWarning?: string | null;
  fluidStates?: FluidStateSummary[];
  runsSubmit?: (body: Record<string, unknown> | null) => Response | Promise<Response>;
  runsGet?: (runId: string) => Response | Promise<Response>;
  runsCancel?: (runId: string) => Response | Promise<Response>;
  runPlan?: (runId: string) => Response | Promise<Response>;
  runEvents?: (runId: string) => Response | Promise<Response>;
  browse?: () => Response | Promise<Response>;
};

function createState(): ApiState {
  return {
    decks: {
      "deck.yaml": {
        filename: "deck.yaml",
        labware: [
          {
            key: "plate_1",
            config: {
              type: "well_plate",
              name: "Deck Plate",
              model_name: "plate-model",
              rows: 8,
              columns: 12,
              length: 127.76,
              width: 85.47,
              height: 14.22,
              calibration: {
                a1: { x: 10, y: 20, z: 30 },
                a2: { x: 20, y: 20, z: 30 },
              },
              x_offset: 9,
              y_offset: 9,
              capacity_ul: 200,
              working_volume_ul: 150,
            },
            wells: null,
          },
        ],
      },
    },
    gantries: {
      "cubos.yaml": {
        filename: "cubos.yaml",
        config: {
          serial_port: "",
          gantry_type: "cub_xl",
          cnc: {
      factory_z_travel_mm: 80,
            calibration_block_height_mm: 35,
            y_axis_motion: "head",
            safe_z: 80,
          },
          working_volume: { x_min: 0, x_max: 300, y_min: 0, y_max: 200, z_min: 0, z_max: 80 },
          grbl_settings: {},
          instruments: {
            pipette_1: {
              type: "pipette",
              vendor: "opentrons",
              offset_x: 1,
              offset_y: 2,
              depth: 0,
              port: "/dev/ttyUSB0",
            },
          },
        },
      },
    },
    protocols: {
      "move.yaml": {
        filename: "move.yaml",
        positions: {
          park: [10, 20, 30],
        },
        steps: [
          {
            command: "move",
            args: { instrument: "pipette_1", position: "plate_1.A1", travel_z: 3 },
          },
        ],
      },
    },
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function toDeckResponse(filename: string, body: DeckConfig): DeckResponse {
  return {
    filename,
    labware: Object.entries(body.labware).map(([key, config]) => ({
      key,
      config,
      wells: config.type === "well_plate" ? previewWells(config) : null,
    })),
  };
}

function previewWells(config: WellPlateConfig): Record<string, WellPosition> {
  const a1 = config.calibration.a1 ?? { x: 0, y: 0, z: 0 };
  const wells: Record<string, WellPosition> = {};
  for (let row = 0; row < config.rows; row += 1) {
    const rowName = String.fromCharCode("A".charCodeAt(0) + row);
    for (let column = 0; column < config.columns; column += 1) {
      wells[`${rowName}${column + 1}`] = {
        x: a1.x + column * config.x_offset,
        y: a1.y - row * config.y_offset,
        z: a1.z,
      };
    }
  }
  return wells;
}

function toGantryResponse(filename: string, body: GantryConfig): GantryResponse {
  return {
    filename,
    config: body,
  };
}

function toProtocolResponse(filename: string, body: ProtocolConfig): ProtocolResponse {
  return {
    filename,
    positions: body.positions,
    steps: body.protocol,
  };
}

function installFetchMock(state: ApiState, options: FetchMockOptions = {}) {
  let gantryConnected = false;
  let lastSubmittedRunId: string | null = null;
  let lastSubmittedFluidStateId: number | null = null;
  const gantryPosition = (x = 0, y = 0, z = 0): GantryPosition => ({
    x,
    y,
    z,
    work_x: x,
    work_y: y,
    work_z: z,
    status: "Idle",
    connected: gantryConnected,
    calibration_active: false,
    calibration_warning: options.calibrationWarning ?? null,
  });

  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
      "http://localhost",
    );
    const path = url.pathname;
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;

    if (path === "/api/v1/settings" && method === "GET") {
      return jsonResponse({ config_dir: "/mock/CubOS/configs" });
    }
    if (path === "/api/v1/settings/browse" && method === "POST") {
      if (options.browse) return options.browse();
      return jsonResponse({ config_dir: "/mock/CubOS/selected-configs" });
    }
    if (path === "/api/v1/settings" && method === "PUT") {
      return jsonResponse({ config_dir: body?.config_dir ?? "/mock/CubOS/configs" });
    }
    if (path === "/api/v1/deck/configs") {
      return jsonResponse(Object.keys(state.decks));
    }
    if (path === "/api/v1/gantry/configs") {
      return jsonResponse(Object.keys(state.gantries));
    }
    if (path === "/api/v1/protocol/configs") {
      return jsonResponse(Object.keys(state.protocols));
    }
    if (path === "/api/v1/gantry/instrument-types") {
      return jsonResponse([{ type: "pipette", vendors: ["opentrons"], is_mock: false }]);
    }
    if (path === "/api/v1/gantry/instrument-schemas") {
      return jsonResponse({
        pipette: {
          opentrons: [
            { name: "port", type: "str", required: true, default: "/dev/ttyUSB0", choices: null },
          ],
        },
      });
    }
    if (path === "/api/v1/gantry/instrument-methods") {
      return jsonResponse({
        asmi: ["indentation", "measure"],
        filmetrics: ["measure"],
        pipette: [],
        uv_curing: ["measure"],
        uvvis_ccs: ["measure"],
      });
    }
    if (path === "/api/v1/protocol/commands") {
      return jsonResponse([
        {
          name: "move",
          args: [
            { name: "instrument", type: "str", required: true, default: null },
            { name: "position", type: "Any", required: true, default: null },
            { name: "travel_z", type: "float | None", required: false, default: null },
          ],
          description: "Move gantry",
        },
        {
          name: "scan",
          args: [
            { name: "plate", type: "str", required: true, default: null },
            { name: "instrument", type: "str", required: true, default: null },
            { name: "method", type: "str", required: true, default: null },
            { name: "measurement_height", type: "float", required: true, default: null },
            { name: "interwell_scan_height", type: "float", required: true, default: null },
            { name: "indentation_limit_height", type: "float | None", required: false, default: null },
            { name: "method_kwargs", type: "Dict[str, Any] | None", required: false, default: null },
          ],
          description: "Scan plate",
        },
      ]);
    }
    if (path === "/api/v1/protocol/run" && method === "POST") {
      if (options.protocolRun) return options.protocolRun(init);
      return jsonResponse({ status: "complete", steps_executed: 1, campaign_id: 123 });
    }
    if (path === "/api/v1/protocol/cancel" && method === "POST") {
      if (options.protocolCancel) return options.protocolCancel();
      return jsonResponse({ status: "cancel_requested" });
    }
    if (path === "/api/v1/protocol/run-status") {
      return jsonResponse(options.runStatus ? options.runStatus() : { active: false, protocol_file: null });
    }
    if (path === "/api/v1/protocol/validate-setup" && method === "POST") {
      if (options.validateSetup) return options.validateSetup();
      return jsonResponse({
        valid: true,
        errors: [],
        output: "RESULT: PASS",
      });
    }
    if (path === "/api/v1/gantry/position") {
      return jsonResponse(gantryPosition());
    }
    if (path === "/api/v1/gantry/connect" && method === "POST") {
      gantryConnected = true;
      return jsonResponse(gantryPosition());
    }
    if (path === "/api/v1/gantry/disconnect" && method === "POST") {
      gantryConnected = false;
      return jsonResponse(gantryPosition());
    }
    if (path === "/api/v1/gantry/calibration/prepare-origin" && method === "POST") {
      gantryConnected = true;
      return jsonResponse(gantryPosition(0, 0, 80));
    }
    if (path === "/api/v1/gantry/calibration/home-and-center" && method === "POST") {
      gantryConnected = true;
      return jsonResponse({
        xy_bounds: { x: 300, y: 200, z: 80 },
        position: { x: 150, y: 100, z: 80 },
      });
    }
    if (path === "/api/v1/gantry/calibration/restore-soft-limits" && method === "POST") {
      return jsonResponse(gantryPosition());
    }
    if (path === "/api/v1/gantry/work-coordinates" && method === "POST") {
      return jsonResponse(gantryPosition(body?.x ?? 0, body?.y ?? 0, body?.z ?? 0));
    }
    if (path === "/api/v1/gantry/calibration/finalize-origin" && method === "POST") {
      return jsonResponse({
        measured_volume: { x: 300, y: 200, z: 80 },
        z_calibration: {
          block_height: body?.block_height ?? 35,
          total_z_range: body?.factory_z_travel ?? 80,
          home_z: body?.home_z ?? 80,
          block_touch_z: body?.block_touch_z ?? 0,
          home_to_block_travel: 45,
          remaining_below_block: 35,
          can_reach_deck_bottom: true,
          z_min: 0,
          z_max: 80,
          max_travel_z: 80,
        },
        max_travel: { x: 310, y: 210, z: 90 },
        homing_pull_off_mm: 10,
        position: { x: 300, y: 200, z: 80 },
      });
    }
    if (path === "/api/v1/gantry/jog-blocking" && method === "POST") {
      return jsonResponse(gantryPosition(0, 0, body?.z ?? 0));
    }
    if (path === "/api/v1/gantry/home" && method === "POST") {
      return jsonResponse(gantryPosition(300, 200, 80));
    }
    if (path === "/api/v1/gantry/soft-limits" && method === "POST") {
      return jsonResponse({ status: "ok" });
    }
    if (path === "/api/v1/deck/preview-wells" && method === "POST") {
      return jsonResponse(previewWells(body as WellPlateConfig));
    }
    if (path === "/api/v1/fluid-states" && method === "GET") {
      return jsonResponse(options.fluidStates ?? []);
    }
    if (path === "/api/v1/runs" && method === "POST") {
      if (options.runsSubmit) return options.runsSubmit(body);
      lastSubmittedBody = body;
      lastSubmittedRunId = (body?.run_id as string) ?? null;
      lastSubmittedFluidStateId = (body?.state as { fluid_state_id?: number } | undefined)?.fluid_state_id
        ?? null;
      return jsonResponse({
        run_id: lastSubmittedRunId,
        state: "queued",
        created_at: 0,
        started_at: null,
        finished_at: null,
        mock_mode: false,
        metadata: {},
        digests: {},
        result: null,
        error: null,
        artifacts: [],
        fluid_state_id: lastSubmittedFluidStateId,
      });
    }
    if (path.endsWith("/cancel") && path.startsWith("/api/v1/runs/") && method === "POST") {
      const runId = path.slice("/api/v1/runs/".length, -"/cancel".length);
      if (options.runsCancel) return options.runsCancel(runId);
      return jsonResponse({
        run_id: runId,
        state: "cancel_requested",
        created_at: 0,
        started_at: 0,
        finished_at: null,
        mock_mode: false,
        metadata: {},
        digests: {},
        result: null,
        error: null,
        artifacts: [],
        fluid_state_id: null,
      });
    }
    if (path.endsWith("/plan") && path.startsWith("/api/v1/runs/") && method === "GET") {
      const runId = path.slice("/api/v1/runs/".length, -"/plan".length);
      if (options.runPlan) return options.runPlan(runId);
      return jsonResponse({ run_id: runId, steps: [] });
    }
    if (path.includes("/events") && path.startsWith("/api/v1/runs/") && method === "GET") {
      const runId = path.slice("/api/v1/runs/".length).split("/")[0];
      if (options.runEvents) return options.runEvents(runId);
      return jsonResponse({ run_id: runId, events: [] });
    }
    if (path.startsWith("/api/v1/runs/") && method === "GET") {
      const runId = path.slice("/api/v1/runs/".length);
      if (options.runsGet) return options.runsGet(runId);
      return jsonResponse({
        run_id: runId,
        state: "succeeded",
        created_at: 0,
        started_at: 0,
        finished_at: 1,
        mock_mode: false,
        metadata: {},
        digests: {},
        result: { status: "ok", steps_executed: 1, campaign_id: 456 },
        error: null,
        artifacts: [],
        fluid_state_id: lastSubmittedRunId === runId ? lastSubmittedFluidStateId : null,
      });
    }

    const [, api, version, kind, filename] = path.split("/");
    if (api !== "api" || version !== "v1" || !filename) {
      return new Response("Not found", { status: 404 });
    }

    const collection = kind === "deck" ? state.decks : kind === "gantry" ? state.gantries : kind === "protocol" ? state.protocols : null;
    if (collection && method === "DELETE") {
      if (!(filename in collection)) return new Response(JSON.stringify({ detail: `Config not found: ${filename}` }), { status: 404 });
      delete collection[filename];
      return jsonResponse({ status: "deleted", filename });
    }
    if (collection && method === "GET" && !(filename in collection)) {
      return new Response(JSON.stringify({ detail: `Config not found: ${filename}` }), { status: 404 });
    }
    if (kind === "deck") {
      if (method === "GET") return jsonResponse(state.decks[filename]);
      if (method === "PUT") {
        state.decks[filename] = toDeckResponse(filename, body as DeckConfig);
        return jsonResponse(state.decks[filename]);
      }
    }
    if (kind === "gantry") {
      if (method === "GET") return jsonResponse(state.gantries[filename]);
      if (method === "PUT") {
        state.gantries[filename] = toGantryResponse(filename, body as GantryConfig);
        return jsonResponse(state.gantries[filename]);
      }
    }
    if (kind === "protocol") {
      if (method === "GET") return jsonResponse(state.protocols[filename]);
      if (method === "PUT") {
        state.protocols[filename] = toProtocolResponse(filename, body as ProtocolConfig);
        return jsonResponse({ status: "ok", filename });
      }
    }

    return new Response("Not found", { status: 404 });
  });

  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderApp() {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchOnWindowFocus: false,
      },
    },
  });

  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

async function importConfig(user: ReturnType<typeof userEvent.setup>, label: string, filename: string) {
  await user.selectOptions(screen.getByRole("combobox", { name: label }), filename);
}

async function waitForSettingsLoad() {
  await screen.findByDisplayValue("/mock/CubOS/configs");
}

async function loadRequiredProtocolDependencies(user: ReturnType<typeof userEvent.setup>) {
  await importConfig(user, "Gantry config", "cubos.yaml");
  await user.click(screen.getByRole("button", { name: "Deck" }));
  await importConfig(user, "Deck config", "deck.yaml");
}

async function connectGantry(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Connect" }));
  await screen.findByRole("button", { name: "Disconnect" });
}

async function returnToWorkflowTab(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Workflow" }));
}

describe("CubOS editor interactions", () => {
  beforeEach(() => {
    localStorage.clear();
    installFetchMock(createState());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a browse button for the config directory", async () => {
    renderApp();
    await waitForSettingsLoad();

    expect(screen.getByDisplayValue("/mock/CubOS/configs")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Browse" })).toBeInTheDocument();
  });

  it("updates the config directory from browse selection", async () => {
    const user = userEvent.setup();
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Browse" }));

    await waitFor(() => expect(screen.getByDisplayValue("/mock/CubOS/selected-configs")).toBeInTheDocument());
  });

  it("falls back to in-app path entry when the native picker cannot open", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      browse: () => new Response(
        JSON.stringify({ detail: "Directory picker failed: no display" }),
        { status: 400, headers: { "Content-Type": "application/json" } },
      ),
    });
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Browse" }));

    const dialog = await screen.findByRole("dialog", { name: "Select config directory" });
    expect(dialog).toBeInTheDocument();
    const pathInput = screen.getByLabelText("Config directory path");
    // Prefilled with the current directory for editing in place.
    expect(pathInput).toHaveValue("/mock/CubOS/configs");

    await user.clear(pathInput);
    await user.type(pathInput, "/mock/CubOS/typed-configs");
    await user.click(screen.getByRole("button", { name: "Use Directory" }));

    await waitFor(() => expect(screen.getByDisplayValue("/mock/CubOS/typed-configs")).toBeInTheDocument());
    expect(screen.queryByRole("dialog", { name: "Select config directory" })).not.toBeInTheDocument();
  });

  it("keeps a cancelled native picker silent", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      browse: () => new Response(
        JSON.stringify({ detail: "No directory selected" }),
        { status: 400, headers: { "Content-Type": "application/json" } },
      ),
    });
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Browse" }));

    expect(screen.queryByRole("dialog", { name: "Select config directory" })).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("/mock/CubOS/configs")).toBeInTheDocument();
  });

  it("clears loaded config selections when the config directory changes", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    expect(await screen.findByLabelText("Travel Z")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Browse" }));

    await waitFor(() => expect(screen.getByDisplayValue("/mock/CubOS/selected-configs")).toBeInTheDocument());
    expect(await screen.findByText("Please load Gantry, Deck configs first.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Travel Z")).not.toBeInTheDocument();
  });

  it("guards dirty config-directory changes", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Deck" }));
    await importConfig(user, "Deck config", "deck.yaml");
    const nameField = await screen.findByDisplayValue("Deck Plate");
    await user.clear(nameField);
    await user.type(nameField, "Edited Plate");

    await user.click(screen.getByRole("button", { name: "Browse" }));

    expect(await screen.findByRole("alertdialog")).toHaveTextContent(
      "Discard unsaved config changes and switch config directory?",
    );
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByDisplayValue("/mock/CubOS/configs")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Edited Plate")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/settings",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("loads and saves a gantry config across tab switches", async () => {
    const user = userEvent.setup();
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    const serialPort = await screen.findByLabelText("Serial port");
    expect(serialPort).toHaveValue("");

    await user.type(serialPort, "/dev/ttyUSB9");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(screen.getByRole("button", { name: "Deck" }));
    await user.click(screen.getByRole("button", { name: "Gantry" }));

    await waitFor(() => expect(screen.getByLabelText("Serial port")).toHaveValue("/dev/ttyUSB9"));
  });

  it("loads and saves a deck config across tab switches", async () => {
    const user = userEvent.setup();
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Deck" }));
    await importConfig(user, "Deck config", "deck.yaml");
    const nameField = await screen.findByDisplayValue("Deck Plate");
    expect(nameField).toHaveValue("Deck Plate");

    await user.clear(nameField);
    await user.type(nameField, "Renamed Plate");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(screen.getByRole("button", { name: "Gantry" }));
    await user.click(screen.getByRole("button", { name: "Deck" }));

    await waitFor(() => expect(screen.getByDisplayValue("Renamed Plate")).toBeInTheDocument());
  });

  it("imports a deck config into cub_deck.yaml", async () => {
    const user = userEvent.setup();
    const state = createState();
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Deck" }));
    await importConfig(user, "Deck config", "deck.yaml");

    expect(await screen.findByDisplayValue("Deck Plate")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByPlaceholderText("cub_deck.yaml")).toBeInTheDocument());
    expect(
      state.decks["cub_deck.yaml"]?.labware.map(({ key, config }) => ({ key, config })),
    ).toEqual(
      state.decks["deck.yaml"]?.labware.map(({ key, config }) => ({ key, config })),
    );
  });

  it("uses live preview wells for loaded deck edits before saving", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Deck" }));
    await importConfig(user, "Deck config", "deck.yaml");
    expect(await screen.findByText("A1: (10, 20, 30)")).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Calibration A1 X"));
    await user.type(screen.getByLabelText("Calibration A1 X"), "111");

    expect(await screen.findByText("A1: (111, 20, 30)")).toBeInTheDocument();
  });

  it("loads and saves gantry instruments across tab switches", async () => {
    const user = userEvent.setup();
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    const portField = await screen.findByLabelText("Port *");
    expect(portField).toHaveValue("/dev/ttyUSB0");

    await user.clear(portField);
    await user.type(portField, "/dev/ttyUSB4");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(screen.getByRole("button", { name: "Deck" }));
    await user.click(screen.getByRole("button", { name: "Gantry" }));

    await waitFor(() => expect(screen.getByLabelText("Port *")).toHaveValue("/dev/ttyUSB4"));
  });

  it("saves a new gantry filename before selecting it for fetches", async () => {
    const user = userEvent.setup();
    const state = createState();
    const fetchMock = installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    expect(await screen.findByLabelText("Serial port")).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("cubos.yaml"), "qa_new_gantry");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(state.gantries["qa_new_gantry.yaml"]).toBeDefined());
    const newFileCalls = fetchMock.mock.calls.filter(([input]) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      return url.pathname === "/api/v1/gantry/qa_new_gantry.yaml";
    });
    expect(newFileCalls.length).toBeGreaterThan(0);
    expect(newFileCalls[0][1]?.method).toBe("PUT");
  });

  it("opens the gantry calibration wizard from the control panel", async () => {
    const user = userEvent.setup();
    const state = createState();
    const fetchMock = installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    await user.click(await screen.findByRole("button", { name: "Calibrate" }));

    expect(screen.getByRole("dialog", { name: "Gantry calibration" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /XY origin/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("button", { name: "Home gantry" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Home gantry" }));

    const blockHeight = await screen.findByLabelText("Reference height (mm)");
    expect(blockHeight).toHaveValue("35");
    expect(blockHeight).toBeEnabled();
    await user.clear(blockHeight);
    await user.type(blockHeight, "36.25");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    const setOrigin = await screen.findByRole("button", { name: "Set origin and continue" });
    expect(setOrigin).toBeInTheDocument();
    await waitFor(() => expect(setOrigin).toBeEnabled());
    expect(screen.queryByRole("button", { name: /Set XY origin/ })).not.toBeInTheDocument();
    expect(screen.queryByText("XY origin")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Set Z reference/ })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibration/prepare-origin",
      expect.objectContaining({ method: "POST" }),
    );

    await user.click(setOrigin);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/work-coordinates",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ x: 0, y: 0, z: 36.25 }),
      }),
    ));
    expect(await screen.findByText("Origin set.")).toBeInTheDocument();
    expect(screen.queryByText(/Program GRBL soft-limit travel spans/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Done" })).not.toBeInTheDocument();

    await user.click(within(screen.getByRole("dialog", { name: "Gantry calibration" })).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibration/finalize-origin",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          home_z: 80,
          block_touch_z: 0,
          block_height: 36.25,
          factory_z_travel: 80,
        }),
      }),
    ));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/cubos.yaml",
      expect.objectContaining({ method: "PUT" }),
    ));
    expect(state.gantries["cubos.yaml"]?.config.working_volume.z_max).toBe(80);
    expect(state.gantries["cubos.yaml"]?.config.grbl_settings?.max_travel_z).toBe(90);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Gantry calibration" })).not.toBeInTheDocument());
  });

  it("shows an error when block height is empty and Continue is clicked", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    await user.click(await screen.findByRole("button", { name: "Calibrate" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Home gantry" }));

    const blockHeight = await screen.findByLabelText("Reference height (mm)");
    await user.clear(blockHeight);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText("Enter a calibration reference height before continuing.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set origin and continue" })).not.toBeInTheDocument();
  });

  it("shows an error when block height is zero or negative and Continue is clicked", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    await user.click(await screen.findByRole("button", { name: "Calibrate" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Home gantry" }));

    const blockHeight = await screen.findByLabelText("Reference height (mm)");
    await user.clear(blockHeight);
    await user.type(blockHeight, "0");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText("Calibration reference height must be greater than 0.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set origin and continue" })).not.toBeInTheDocument();
  });

  it("reconnects after saving calibrated output to a different gantry filename", async () => {
    const user = userEvent.setup();
    const state = createState();
    const fetchMock = installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    await user.click(await screen.findByRole("button", { name: "Calibrate" }));

    const outputYaml = screen.getByLabelText("Output YAML");
    await user.clear(outputYaml);
    await user.type(outputYaml, "calibrated.yaml");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Home gantry" }));
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    const setOrigin = await screen.findByRole("button", { name: "Set origin and continue" });
    await waitFor(() => expect(setOrigin).toBeEnabled());
    await user.click(setOrigin);
    expect(await screen.findByText("Origin set.")).toBeInTheDocument();
    await user.click(within(screen.getByRole("dialog", { name: "Gantry calibration" })).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibrated.yaml",
      expect.objectContaining({ method: "PUT" }),
    ));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/disconnect",
      expect.objectContaining({ method: "POST" }),
    ));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/connect",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ filename: "calibrated.yaml" }),
      }),
    ));
  });

  it("loads and saves a protocol config across tab switches", async () => {
    const user = userEvent.setup();
    const state = createState();
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    const travelZField = await screen.findByLabelText("Travel Z");
    expect(travelZField).toHaveValue("3");
    const parkYField = await screen.findByLabelText("park coordinates Y");
    expect(parkYField).toHaveValue("20");

    await user.clear(travelZField);
    await user.type(travelZField, "42");
    const positionField = screen.getByLabelText(/^Position \*$/);
    await user.clear(positionField);
    await user.type(positionField, "park");
    await user.clear(parkYField);
    await user.type(parkYField, "40");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(state.protocols["move.yaml"]?.positions).toEqual({ park: [10, 40, 30] }));
    expect(state.protocols["move.yaml"]?.steps[0].args.position).toBe("park");
    await user.click(screen.getByRole("button", { name: "Gantry" }));
    await user.click(screen.getByRole("button", { name: "Protocol" }));

    await waitFor(() => expect(screen.getByDisplayValue("42")).toBeInTheDocument());
    expect(screen.getByLabelText("park coordinates Y")).toHaveValue("40");
    expect(screen.getByLabelText(/^Position \*$/)).toHaveValue("park");
  });

  it("adds protocol named positions independently from protocol steps", async () => {
    const user = userEvent.setup();
    const state = createState();
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(await screen.findByRole("button", { name: "Add Position" }));
    const nameField = await screen.findByLabelText("Position 2 name");

    await user.clear(nameField);
    await user.type(nameField, "staging_position");
    await user.clear(screen.getByLabelText("staging_position coordinates X"));
    await user.type(screen.getByLabelText("staging_position coordinates X"), "11");
    await user.clear(screen.getByLabelText("staging_position coordinates Y"));
    await user.type(screen.getByLabelText("staging_position coordinates Y"), "22");
    await user.clear(screen.getByLabelText("staging_position coordinates Z"));
    await user.type(screen.getByLabelText("staging_position coordinates Z"), "33");

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(state.protocols["move.yaml"]?.positions).toEqual({
        park: [10, 20, 30],
        staging_position: [11, 22, 33],
      });
    });
  });

  it("validates the selected setup through the full CubOS setup endpoint", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.mocked(fetch);

    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(await screen.findByRole("button", { name: "Validate" }));

    await waitFor(() => expect(screen.getByText("Protocol is valid.")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/protocol/validate-setup",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          gantry_file: "cubos.yaml",
          deck_file: "cub_deck.yaml",
          protocol_file: "move.yaml",
        }),
      }),
    );
  });

  it("clears stale validation and blocks Validate while protocol edits are unsaved", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.mocked(fetch);

    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(await screen.findByRole("button", { name: "Validate" }));
    expect(await screen.findByText("Protocol is valid.")).toBeInTheDocument();

    const travelZField = await screen.findByLabelText("Travel Z");
    await user.clear(travelZField);
    await user.type(travelZField, "12");

    expect(screen.queryByText("Protocol is valid.")).not.toBeInTheDocument();

    const callsBeforeDirtyValidate = fetchMock.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Validate" }));

    expect(await screen.findByText("Save your changes first — Validate checks the saved files.")).toBeInTheDocument();
    const validateCallsAfterDirtyValidate = fetchMock.mock.calls
      .slice(callsBeforeDirtyValidate)
      .filter(([input]) => {
        const url = new URL(
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
          "http://localhost",
        );
        return url.pathname === "/api/v1/protocol/validate-setup";
      });
    expect(validateCallsAfterDirtyValidate).toHaveLength(0);
  });

  it("surfaces Validate setup request failures", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      validateSetup: () => new Response(JSON.stringify({ detail: "validator unavailable" }), {
        status: 500,
        statusText: "Internal Server Error",
        headers: { "Content-Type": "application/json" },
      }),
    });

    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(await screen.findByRole("button", { name: "Validate" }));

    expect(await screen.findByText("validator unavailable")).toBeInTheDocument();
  });

  it("guards Validate when no protocol file is selected", async () => {
    const user = userEvent.setup();

    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await user.click(await screen.findByRole("button", { name: "Validate" }));

    expect(await screen.findByText("Select gantry, deck, and protocol files before setup validation.")).toBeInTheDocument();
  });

  it("surfaces saved-setup validation errors from CubOS", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      validateSetup: () => jsonResponse({
        valid: false,
        errors: ["step 2: unknown position"],
      }),
    });

    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(await screen.findByRole("button", { name: "Validate" }));

    expect(await screen.findByText("step 2: unknown position")).toBeInTheDocument();
  });

  it("keeps Run Protocol disabled while the gantry is disconnected", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.mocked(fetch);

    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    const runButton = await screen.findByRole("button", { name: "Run Protocol" });

    expect(runButton).toBeDisabled();
    await user.click(runButton);
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("allows Run Protocol despite a GRBL settings mismatch (calibration is advisory, not blocking)", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock(createState(), {
      calibrationWarning: "Finish gantry calibration before running protocols.",
    });
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");

    // A GRBL-settings mismatch must not block running or surface a
    // "calibration needed" banner: commissioning machines legitimately
    // differ from the YAML, and the operator owns that decision.
    expect(screen.queryByText("CALIBRATION NEEDED")).not.toBeInTheDocument();
    const runButton = await screen.findByRole("button", { name: "Run Protocol" });
    await waitFor(() => expect(runButton).toBeEnabled());
    await user.click(runButton);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/runs",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("blocks Run Protocol after editing a loaded protocol until the change is saved", async () => {
    const user = userEvent.setup();
    const state = createState();
    const fetchMock = installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");

    // Gantry is connected and nothing edited yet: Run is allowed.
    const runButton = await screen.findByRole("button", { name: "Run Protocol" });
    await waitFor(() => expect(runButton).toBeEnabled());

    // Editing a field marks the protocol dirty: Run must be blocked and
    // the user warned, and clicking it must not hit the run endpoint.
    const travelZField = await screen.findByLabelText("Travel Z");
    await user.clear(travelZField);
    await user.type(travelZField, "99");

    expect(await screen.findByText(/Unsaved changes/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Run Protocol" })).toBeDisabled());
    await user.click(screen.getByRole("button", { name: "Run Protocol" }));
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({ method: "POST" }),
    );

    // Saving clears the dirty state and re-enables running.
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(state.protocols["move.yaml"]?.steps[0].args.travel_z).toBe(99));
    await waitFor(() => expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument());

    const runAfterSave = screen.getByRole("button", { name: "Run Protocol" });
    await waitFor(() => expect(runAfterSave).toBeEnabled());
    await user.click(runAfterSave);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({ method: "POST" }),
    ));
    // run_id is generated per submission, so assert the config selection
    // rather than an exact body string.
    expect(lastSubmittedBody).toMatchObject({
      gantry_file: "cubos.yaml",
      deck_file: "cub_deck.yaml",
      protocol_file: "move.yaml",
    });
    await returnToWorkflowTab(user);
    expect(await screen.findByText(/campaign #456 created/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Last Campaign")).toHaveValue("#456");
  });

  it("surfaces protocol run failures and re-enables Run Protocol", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      runsGet: (runId: string) => jsonResponse({
        run_id: runId,
        state: "failed",
        created_at: 0,
        started_at: 0,
        finished_at: 1,
        mock_mode: false,
        metadata: {},
        digests: {},
        result: null,
        error: "Gantry lost connection",
        artifacts: [],
        fluid_state_id: null,
      }),
    });

    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(await screen.findByRole("button", { name: "Run Protocol" }));

    // The Run view reports the failure where the operator already is.
    expect(await screen.findByRole("alert")).toHaveTextContent("Gantry lost connection");
    await returnToWorkflowTab(user);
    expect(await screen.findAllByText("Gantry lost connection")).not.toHaveLength(0);
    await waitFor(() => expect(screen.getByRole("button", { name: "Run Protocol" })).toBeEnabled());
    expect(screen.queryByRole("button", { name: "Running..." })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancelling..." })).not.toBeInTheDocument();
  });

  it("keeps the run pending after cancel until the run reaches a terminal state", async () => {
    const user = userEvent.setup();
    // The run stays "running" until the test flips it, mirroring a gantry
    // that has acknowledged the cancel but not yet stopped.
    let runState = "running";
    const fetchMock = installFetchMock(createState(), {
      runsGet: (runId: string) => jsonResponse({
        run_id: runId,
        state: runState,
        created_at: 0,
        started_at: 0,
        finished_at: runState === "running" ? null : 1,
        mock_mode: false,
        metadata: {},
        digests: {},
        result: runState === "succeeded"
          ? { status: "ok", steps_executed: 1, campaign_id: 456 }
          : null,
        error: null,
        artifacts: [],
        fluid_state_id: null,
      }),
    });
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(await screen.findByRole("button", { name: "Run Protocol" }));

    await returnToWorkflowTab(user);
    expect(await screen.findByRole("button", { name: "Running..." })).toBeDisabled();
    const cancelButton = await screen.findByRole("button", { name: "Cancel Run" });
    expect(cancelButton).toBeEnabled();

    await user.click(cancelButton);

    // An addressable run cancels through its own resource, not the legacy
    // whole-session endpoint.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/v1\/runs\/.+\/cancel$/),
      expect.objectContaining({ method: "POST" }),
    ));
    expect(await screen.findAllByText(/cancellation requested/i)).not.toHaveLength(0);
    expect(screen.getAllByRole("button", { name: "Cancelling..." }).every((button) => button.hasAttribute("disabled"))).toBe(true);
    expect(screen.getByRole("button", { name: "Cancelling — waiting for protocol to stop" })).toBeDisabled();

    runState = "succeeded";

    expect(await screen.findByText(/campaign #456 created/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancelling — waiting for protocol to stop" })).not.toBeInTheDocument();
  });

  it("enters a Run view on submit, and keeps it reachable afterwards", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      runPlan: (runId: string) => jsonResponse({
        run_id: runId,
        steps: [
          { index: 0, command: "home", summary: "all axes", args: {} },
          { index: 1, command: "move", summary: "pipette → plate_1.A1", args: {} },
        ],
      }),
    });
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");

    // No run yet: an empty Run view would be a dead tab.
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();

    await user.click(await screen.findByRole("button", { name: "Run Protocol" }));

    // Submitting switches into the run mode and shows the compiled steps
    // alongside the live gantry readout.
    const runRegion = await screen.findByRole("region", { name: "Run progress" });
    expect(runRegion).toBeInTheDocument();
    expect(await screen.findByText("all axes")).toBeInTheDocument();
    expect(screen.getByText("pipette → plate_1.A1")).toBeInTheDocument();

    // Navigating away and back returns to the same run.
    await user.click(screen.getByRole("button", { name: "State" }));
    expect(screen.queryByRole("region", { name: "Run progress" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("region", { name: "Run progress" })).toBeInTheDocument();
  });

  it("reports the run outcome inside the Run view", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      runPlan: (runId: string) => jsonResponse({
        run_id: runId,
        steps: [{ index: 0, command: "home", summary: "all axes", args: {} }],
      }),
    });
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(await screen.findByRole("button", { name: "Run Protocol" }));

    // The operator stays in the Run view when it finishes, so the outcome
    // has to be reported there and not only in the workflow footer.
    expect(
      await screen.findByText(/campaign #456 created/i),
    ).toBeInTheDocument();
  });

  it("shows a protocol-running sidebar banner outside the Protocol tab", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock(createState(), {
      runStatus: () => ({ active: true, protocol_file: "move.yaml" }),
    });
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Deck" }));

    expect(await screen.findByText("● Protocol running…")).toBeInTheDocument();
    expect(screen.getByText("Protocol running — manual control locked")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/protocol/cancel",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("blocks Run Protocol when an unsaved edit lives in another tab (Deck)", async () => {
    const user = userEvent.setup();
    const state = createState();
    const fetchMock = installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    // Edit the deck but do NOT save it.
    await user.click(screen.getByRole("button", { name: "Deck" }));
    const deckNameField = await screen.findByDisplayValue("Deck Plate");
    await user.clear(deckNameField);
    await user.type(deckNameField, "Edited Plate");

    // The protocol itself is untouched, but Run executes the saved deck
    // file, so the unsaved deck edit must still block running.
    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(/Unsaved changes/i);
    expect(banner).toHaveTextContent("Deck");
    await waitFor(() => expect(screen.getByRole("button", { name: "Run Protocol" })).toBeDisabled());
    await user.click(screen.getByRole("button", { name: "Run Protocol" }));
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("prompts to save deck edits in the Deck tab", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Deck" }));
    await importConfig(user, "Deck config", "deck.yaml");
    const nameField = await screen.findByDisplayValue("Deck Plate");

    // No prompt before editing.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await user.clear(nameField);
    await user.type(nameField, "Renamed Plate");

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(/Unsaved changes/i);
    expect(banner).toHaveTextContent(/save this deck/i);
  });

  it("prompts to save gantry edits and keeps GRBL under an Advanced settings expander", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    await screen.findByLabelText("Serial port");

    // GRBL fields are hidden until Advanced settings is expanded.
    expect(screen.queryByLabelText("Steps/mm X")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Advanced settings/ }));
    expect(await screen.findByLabelText("Steps/mm X")).toBeInTheDocument();

    // Editing prompts to save in this (Gantry) tab.
    await user.type(screen.getByLabelText("Serial port"), "/dev/ttyUSB9");
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(/Unsaved changes/i);
    expect(banner).toHaveTextContent(/save this gantry/i);
  });

  it("adds protocol steps with deck, instrument, method, and ASMI force-limit choices", async () => {
    const user = userEvent.setup();
    const state = createState();
    state.gantries["cubos.yaml"].config.instruments = {
      asmi: {
        type: "asmi",
        vendor: "vernier",
        offset_x: 1,
        offset_y: 2,
        depth: 0,
      },
    };
    installFetchMock(state);

    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    expect(screen.getByText("Load a protocol or add steps.")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Add step" }), "scan");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByLabelText(/Plate/)).toHaveValue("plate_1");
    expect(screen.getByLabelText(/Instrument/)).toHaveValue("asmi");
    expect(screen.getByLabelText(/^Measurement \*$/)).toHaveValue("indentation");
    expect(screen.getByLabelText("Force limit (N)")).toHaveValue("10");
  });

  it("guards discarding unsaved deck edits when switching the imported file", async () => {
    const user = userEvent.setup();
    const state = createState();
    state.decks["deck2.yaml"] = {
      filename: "deck2.yaml",
      labware: [
        {
          key: "plate_2",
          config: { ...state.decks["deck.yaml"].labware[0].config, name: "Second Deck Plate" },
          wells: null,
        },
      ],
    };
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();

    await user.click(screen.getByRole("button", { name: "Deck" }));
    await importConfig(user, "Deck config", "deck.yaml");
    const nameField = await screen.findByDisplayValue("Deck Plate");
    await user.clear(nameField);
    await user.type(nameField, "Edited Plate");

    // Cancelling the confirm keeps the edits and the current file.
    await importConfig(user, "Deck config", "deck2.yaml");
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("cub_deck.yaml");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByDisplayValue("Edited Plate")).toBeInTheDocument();

    // Confirming discards the edit and switches to the newly imported file.
    await importConfig(user, "Deck config", "deck2.yaml");
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    expect(await screen.findByDisplayValue("Second Deck Plate")).toBeInTheDocument();
  });

  it("restores the active tab and loaded configs after a reload", async () => {
    const user = userEvent.setup();
    const state = createState();
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await screen.findByLabelText("Travel Z");
    cleanup();

    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    expect(await screen.findByLabelText("Travel Z")).toHaveValue("3");
    expect(screen.getByRole("combobox", { name: "Protocol config" })).toHaveValue("move.yaml");
    await user.click(screen.getByRole("button", { name: "Gantry" }));
    expect(await screen.findByRole("combobox", { name: "Gantry config" })).toHaveValue("cubos.yaml");
    await user.click(screen.getByRole("button", { name: "Deck" }));
    expect(await screen.findByRole("combobox", { name: "Deck config" })).toHaveValue("deck.yaml");
  });

  it("drops a restored selection whose file no longer exists", async () => {
    localStorage.setItem(
      "cubos-workspace:/mock/CubOS/configs",
      JSON.stringify({ activeTab: "Gantry", gantryFile: "gone.yaml", deckFile: null, protocolFile: null, deckImportedFrom: null }),
    );
    renderApp();
    await waitForSettingsLoad();
    expect(await screen.findByText("gone.yaml was not found in the config directory.")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Gantry config" })).toHaveValue("");
    expect(screen.queryByText(/Gantry load failed/)).not.toBeInTheDocument();
  });

  it("deletes a config after confirmation and clears the selection", async () => {
    const user = userEvent.setup();
    const state = createState();
    state.protocols["scratch.yaml"] = { ...state.protocols["move.yaml"], filename: "scratch.yaml" };
    const fetchMock = installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "scratch.yaml");
    await screen.findByLabelText("Travel Z");

    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Delete scratch.yaml from the config directory?");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(fetchMock).not.toHaveBeenCalledWith("/api/v1/protocol/scratch.yaml", expect.objectContaining({ method: "DELETE" }));

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Deleted scratch.yaml.")).toBeInTheDocument();
    expect(state.protocols["scratch.yaml"]).toBeUndefined();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Protocol config" })).toHaveValue(""));
    expect(screen.queryByRole("option", { name: "scratch.yaml" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Travel Z")).not.toBeInTheDocument();
  });

  it("keeps protocol edits when deck changes are discarded", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    const travelZField = await screen.findByLabelText("Travel Z");
    await user.clear(travelZField);
    await user.type(travelZField, "42");

    await user.click(screen.getByRole("button", { name: "Deck" }));
    const deckNameField = await screen.findByDisplayValue("Deck Plate");
    await user.clear(deckNameField);
    await user.type(deckNameField, "Edited Plate");
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.getByDisplayValue("Deck Plate")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    expect(await screen.findByLabelText("Travel Z")).toHaveValue("42");
  });

  it("starts a new protocol from the picker and confirms before dropping edits", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    const travelZField = await screen.findByLabelText("Travel Z");
    await user.clear(travelZField);
    await user.type(travelZField, "42");

    await user.click(screen.getByRole("button", { name: "New" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("start a new protocol");
    await user.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByLabelText("Travel Z")).not.toBeInTheDocument());
    expect(screen.getByRole("combobox", { name: "Protocol config" })).toHaveValue("");
    expect(screen.getByText("Load a protocol or add steps.")).toBeInTheDocument();
  });

  it("acknowledges a save-as under the new filename", async () => {
    const user = userEvent.setup();
    const state = createState();
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await importConfig(user, "Gantry config", "cubos.yaml");
    await screen.findByLabelText("Serial port");

    await user.type(screen.getByRole("textbox", { name: "Save as filename" }), "copy_of_cubos");
    expect(screen.getByText(/Save writes/)).toHaveTextContent("copy_of_cubos.yaml as a new file.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(state.gantries["copy_of_cubos.yaml"]).toBeDefined());
    expect(await screen.findByRole("status", { name: "" })).toHaveTextContent("Saved copy_of_cubos.yaml");
    expect(screen.getByRole("combobox", { name: "Gantry config" })).toHaveValue("copy_of_cubos.yaml");
  });

  it("asks before save-as overwrites an existing config", async () => {
    const user = userEvent.setup();
    const state = createState();
    state.gantries["other.yaml"] = { ...state.gantries["cubos.yaml"], filename: "other.yaml" };
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await importConfig(user, "Gantry config", "cubos.yaml");
    await screen.findByLabelText("Serial port");

    await user.type(screen.getByRole("textbox", { name: "Save as filename" }), "other");
    expect(screen.getByText(/Save writes/)).toHaveTextContent("already exists and will be overwritten");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("other.yaml already exists. Overwrite it?");
    await user.click(screen.getByRole("button", { name: "Overwrite" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Gantry config" })).toHaveValue("other.yaml"));
  });

  it("guards discarding unsaved gantry edits when switching the imported file", async () => {
    const user = userEvent.setup();
    const state = createState();
    state.gantries["cubos2.yaml"] = {
      filename: "cubos2.yaml",
      config: { ...state.gantries["cubos.yaml"].config, serial_port: "/dev/ttyUSB-second" },
    };
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();

    await importConfig(user, "Gantry config", "cubos.yaml");
    const serialPort = await screen.findByLabelText("Serial port");
    await user.type(serialPort, "-edited");

    // Cancelling the confirm keeps the edits and the current file. (The
    // field now shows a per-field amber "*" since it differs from the
    // saved baseline, so match loosely.)
    await importConfig(user, "Gantry config", "cubos2.yaml");
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Discard unsaved gantry changes?");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText(/^Serial port/)).toHaveValue("-edited");

    // Confirming discards the edit and switches to the newly imported file.
    await importConfig(user, "Gantry config", "cubos2.yaml");
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.getByLabelText("Serial port")).toHaveValue("/dev/ttyUSB-second"));
  });

  it("guards discarding unsaved protocol edits when switching the imported file", async () => {
    const user = userEvent.setup();
    const state = createState();
    state.protocols["move2.yaml"] = {
      filename: "move2.yaml",
      positions: { staging: [5, 5, 5] },
      steps: [{ command: "move", args: { instrument: "pipette_1", position: "plate_1.A1", travel_z: 9 } }],
    };
    installFetchMock(state);
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    const travelZField = await screen.findByLabelText("Travel Z");
    await user.clear(travelZField);
    await user.type(travelZField, "77");

    // Cancelling the confirm keeps the edits and the current file.
    await importConfig(user, "Protocol config", "move2.yaml");
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Discard unsaved protocol changes?");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText("Travel Z")).toHaveValue("77");

    // Confirming discards the edit and switches to the newly imported file.
    await importConfig(user, "Protocol config", "move2.yaml");
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.getByLabelText("Travel Z")).toHaveValue("9"));
  });

  it("guards page unload while any editor has unsaved edits", async () => {
    const user = userEvent.setup();
    installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();

    const cleanEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(cleanEvent);
    expect(cleanEvent.defaultPrevented).toBe(false);

    await user.click(screen.getByRole("button", { name: "Deck" }));
    await importConfig(user, "Deck config", "deck.yaml");
    const nameField = await screen.findByDisplayValue("Deck Plate");
    await user.type(nameField, "!");

    const dirtyEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirtyEvent);
    expect(dirtyEvent.defaultPrevented).toBe(true);

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());

    const afterSaveEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(afterSaveEvent);
    expect(afterSaveEvent.defaultPrevented).toBe(false);
  });

  // ── Feature 07: create-new/resume-existing fluid-state run submission ──

  it("keeps Run Protocol disabled while Resume is chosen without a state selected", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      fluidStates: [{
        id: 5,
        label: "seed",
        deck_path: "/deck.yaml",
        deck_fingerprint: "abc",
        created_at: "now",
        updated_at: "now",
        container_count: 1,
        operation_count: 0,
      }],
    });
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    const runButton = await screen.findByRole("button", { name: "Run Protocol" });
    await waitFor(() => expect(runButton).toBeEnabled());

    await user.click(screen.getByRole("radio", { name: "Resume existing state" }));
    expect(screen.getByRole("button", { name: "Run Protocol" })).toBeDisabled();

    await user.selectOptions(screen.getByLabelText("Fluid state to resume"), "5");
    await waitFor(() => expect(screen.getByRole("button", { name: "Run Protocol" })).toBeEnabled());
  });

  it("submits a stateful run through the versioned runs resource when resuming a state", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock(createState(), {
      fluidStates: [{
        id: 5,
        label: "seed",
        deck_path: "/deck.yaml",
        deck_fingerprint: "abc",
        created_at: "now",
        updated_at: "now",
        container_count: 1,
        operation_count: 0,
      }],
    });
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(screen.getByRole("radio", { name: "Resume existing state" }));
    await user.selectOptions(screen.getByLabelText("Fluid state to resume"), "5");

    const runButton = await screen.findByRole("button", { name: "Run Protocol" });
    await waitFor(() => expect(runButton).toBeEnabled());
    await user.click(runButton);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({ method: "POST" }),
    ));
    const [, submitInit] = fetchMock.mock.calls.find(([input]) => input === "/api/v1/runs")!;
    expect(JSON.parse(String(submitInit?.body))).toMatchObject({
      gantry_file: "cubos.yaml",
      deck_file: "cub_deck.yaml",
      protocol_file: "move.yaml",
      state: { fluid_state_id: 5 },
    });

    await returnToWorkflowTab(user);
    expect(await screen.findByText(/campaign #456 created/i)).toBeInTheDocument();
    // The legacy synchronous endpoint is no longer used by the UI at all.
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/protocol/run",
      expect.anything(),
    );
  });

  it("surfaces a deck-fingerprint mismatch error clearly when resuming", async () => {
    const user = userEvent.setup();
    installFetchMock(createState(), {
      fluidStates: [{
        id: 5,
        label: "seed",
        deck_path: "/deck.yaml",
        deck_fingerprint: "abc",
        created_at: "now",
        updated_at: "now",
        container_count: 1,
        operation_count: 0,
      }],
      runsSubmit: async () => new Response(
        JSON.stringify({
          detail: "Fluid state 5 belongs to deck fingerprint abc123, but the "
            + "supplied deck resolves to def456.",
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    });
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(screen.getByRole("radio", { name: "Resume existing state" }));
    await user.selectOptions(screen.getByLabelText("Fluid state to resume"), "5");
    await user.click(await screen.findByRole("button", { name: "Run Protocol" }));

    await user.click(screen.getByRole("button", { name: "Workflow" }));
    expect(await screen.findByText(/deck fingerprint/i)).toBeInTheDocument();
  });

  it("seeds per-container starting volumes into a new-state run submission", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(screen.getByRole("radio", { name: "New fluid state" }));

    await user.click(screen.getByRole("button", { name: "Add container" }));
    await user.type(screen.getByLabelText("Seed container 1"), "plate_1.A1");
    await user.type(screen.getByLabelText("Seed volume 1"), "150");
    // A composition breakdown that sums to the volume.
    await user.click(screen.getByRole("button", { name: "Add component to container 1" }));
    await user.type(screen.getByLabelText("Seed 1 component 1 name"), "water");
    await user.type(screen.getByLabelText("Seed 1 component 1 volume"), "150");

    const runButton = await screen.findByRole("button", { name: "Run Protocol" });
    await waitFor(() => expect(runButton).toBeEnabled());
    await user.click(runButton);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({ method: "POST" }),
    ));
    const [, submitInit] = fetchMock.mock.calls.find(([input]) => input === "/api/v1/runs")!;
    expect(JSON.parse(String(submitInit?.body))).toMatchObject({
      deck_file: "cub_deck.yaml",
      protocol_file: "move.yaml",
      state: {
        initial_state: {
          fluids: { "plate_1.A1": { volume_ul: 150, composition: { water: 150 } } },
        },
      },
    });
  });

  it("keeps the new-state submission empty when no seed rows are added", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(screen.getByRole("radio", { name: "New fluid state" }));
    await user.type(screen.getByLabelText("Label for the new fluid state"), "empty seed");

    const runButton = await screen.findByRole("button", { name: "Run Protocol" });
    await waitFor(() => expect(runButton).toBeEnabled());
    await user.click(runButton);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({ method: "POST" }),
    ));
    const [, submitInit] = fetchMock.mock.calls.find(([input]) => input === "/api/v1/runs")!;
    expect(JSON.parse(String(submitInit?.body))).toMatchObject({
      state: { initial_state: { label: "empty seed", fluids: {} } },
    });
  });

  it("blocks a new-state run when a seed composition does not sum to its volume", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock(createState());
    renderApp();
    await waitForSettingsLoad();
    await loadRequiredProtocolDependencies(user);
    await connectGantry(user);

    await user.click(screen.getByRole("button", { name: "Protocol" }));
    await importConfig(user, "Protocol config", "move.yaml");
    await user.click(screen.getByRole("radio", { name: "New fluid state" }));

    await user.click(screen.getByRole("button", { name: "Add container" }));
    await user.type(screen.getByLabelText("Seed container 1"), "plate_1.A1");
    await user.type(screen.getByLabelText("Seed volume 1"), "150");
    await user.click(screen.getByRole("button", { name: "Add component to container 1" }));
    await user.type(screen.getByLabelText("Seed 1 component 1 name"), "water");
    await user.type(screen.getByLabelText("Seed 1 component 1 volume"), "40");

    // Run is blocked client-side and never reaches the runs resource.
    expect(screen.getByRole("button", { name: "Run Protocol" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/composition sums to 40/i);
    expect(fetchMock).not.toHaveBeenCalledWith("/api/v1/runs", expect.anything());
  });
});
