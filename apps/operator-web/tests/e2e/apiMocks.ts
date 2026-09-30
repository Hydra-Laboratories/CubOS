import type { Page } from "@playwright/test";

// Hermetic /api/v1 mock for E2E runs: every request the operator app makes
// is answered here in the Playwright (Node) process, so no CubOS backend or
// hardware is involved. Tests read `state.requests` to assert on outbound
// traffic (e.g. "no move command was sent").

export interface RecordedRequest {
  method: string;
  path: string;
  body: unknown;
}

export interface MockApiState {
  connected: boolean;
  requests: RecordedRequest[];
}

/**
 * Frozen run state for the step-execution view.
 *
 * The run view is a pure function of (plan, events) fetched by run id, so a
 * scenario just supplies both plus the run record. That renders one exact
 * moment of a run deterministically — no timing races in E2E.
 */
export interface RunScenario {
  plan: { index: number; command: string; summary: string; args?: Record<string, unknown> }[];
  events: {
    index: number;
    command: string;
    outcome: "started" | "completed" | "failed" | "skipped";
    substep?: string | null;
    duration_s?: number | null;
    error?: string | null;
    reason?: string | null;
  }[];
  runState?: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  runError?: string | null;
}

function runRecord(runId: string, scenario: RunScenario | undefined) {
  const state = scenario?.runState ?? "running";
  return {
    run_id: runId,
    state,
    created_at: 0,
    started_at: 0,
    finished_at: state === "running" || state === "queued" ? null : 1,
    mock_mode: false,
    metadata: {},
    digests: {},
    result: state === "succeeded" ? { status: "ok", steps_executed: 1, campaign_id: 42 } : null,
    error: scenario?.runError ?? null,
    artifacts: [],
    fluid_state_id: null,
  };
}

function runEvents(runId: string, scenario: RunScenario | undefined) {
  const events = (scenario?.events ?? []).map((event, position) => ({
    sequence: position + 1,
    timestamp: position + 1,
    state: "running",
    message: `step ${event.index} ${event.command} ${event.outcome}`,
    kind: "step",
    data: {
      index: event.index,
      command: event.command,
      substep: event.substep ?? null,
      outcome: event.outcome,
      duration_s: event.duration_s ?? null,
      error: event.error ?? null,
      reason: event.reason ?? null,
    },
  }));
  return { run_id: runId, events };
}

const GANTRY_CONFIG = {
  serial_port: "/dev/ttyUSB0",
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
    pipette_1: { type: "mock_pipette", vendor: "mock", offset_x: 0, offset_y: 0, depth: 0 },
  },
};

const DECK_LABWARE = [
  {
    key: "plate_1",
    config: {
      type: "well_plate",
      name: "Plate 1",
      model_name: "panda_96_wellplate",
      rows: 2,
      columns: 2,
      length: 127.76,
      width: 85.47,
      height: 14.22,
      calibration: {
        a1: { x: 100, y: 100, z: 20 },
        a2: { x: 109, y: 100, z: 20 },
      },
      x_offset: 9,
      y_offset: 9,
      capacity_ul: 200,
      working_volume_ul: 150,
    },
    wells: {
      A1: { x: 100, y: 100, z: 20 },
      A2: { x: 109, y: 100, z: 20 },
      B1: { x: 100, y: 91, z: 20 },
      B2: { x: 109, y: 91, z: 20 },
    },
  },
];

const FLUID_STATE_SUMMARY = {
  id: 1,
  label: "demo",
  deck_path: "/decks/demo.yaml",
  deck_fingerprint: "a".repeat(64),
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:05:00Z",
  container_count: 1,
  operation_count: 0,
};

const FLUID_STATE_DETAIL = {
  ...FLUID_STATE_SUMMARY,
  containers: [
    {
      labware_key: "plate_1",
      location_id: "A1",
      labware_type: "well_plate",
      capacity_ul: 200,
      working_volume_ul: 150,
      current_volume_ul: 50,
      composition: { water: 50 },
      version: 1,
      updated_at: "2026-07-01T00:05:00Z",
      role: null,
      solution: null,
      allowed_solutions: null,
    },
  ],
  pending_operation_count: 0,
  reconciliation_required_count: 0,
};

function position(state: MockApiState) {
  return {
    x: 10,
    y: 20,
    z: 30,
    work_x: 10,
    work_y: 20,
    work_z: 30,
    status: "Idle",
    connected: state.connected,
    calibration_active: false,
  };
}

export async function installApiMocks(
  page: Page,
  options: {
    connected?: boolean;
    fluidStates?: boolean;
    run?: RunScenario;
  } = {},
): Promise<MockApiState> {
  const state: MockApiState = {
    connected: options.connected ?? false,
    requests: [],
  };
  const withFluidStates = options.fluidStates ?? false;

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    let body: unknown = null;
    try {
      body = request.postDataJSON();
    } catch {
      body = null;
    }
    state.requests.push({ method, path, body });

    const json = (payload: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });

    if (path === "/settings") return json({ config_dir: "/data/cubos-configs" });
    if (path === "/deck/configs") return json(["asmi_deck.yaml"]);
    if (path === "/gantry/configs") return json(["cub.yaml"]);
    if (path === "/protocol/configs") return json(["indentation.yaml"]);
    if (path === "/protocol/commands") {
      return json([
        {
          name: "move",
          description: "Move to a position",
          args: [{ name: "position", type: "str", required: true, default: null }],
        },
      ]);
    }
    if (path === "/protocol/run-status") return json({ active: false, protocol_file: null });
    if (path === "/gantry/instrument-types") {
      return json([{ type: "mock_pipette", vendors: ["mock"], is_mock: true }]);
    }
    if (path === "/gantry/instrument-schemas") return json({});
    if (path === "/gantry/instrument-methods") return json({});
    if (path === "/gantry/position") return json(position(state));
    if (path === "/gantry/connect" && method === "POST") {
      state.connected = true;
      return json(position(state));
    }
    if (path === "/gantry/disconnect" && method === "POST") {
      state.connected = false;
      return json(position(state));
    }
    if (path === "/gantry/move-to" && method === "POST") return json({ status: "ok" });
    if (path === "/gantry/cub.yaml") return json({ filename: "cub.yaml", config: GANTRY_CONFIG });
    if (path === "/deck/asmi_deck.yaml") {
      return json({ filename: "asmi_deck.yaml", labware: DECK_LABWARE });
    }
    if (path === "/deck/cub_deck.yaml") {
      // Serves both the import-flow PUT and subsequent reloads.
      return json({ filename: "cub_deck.yaml", labware: DECK_LABWARE });
    }
    if (path === "/deck/preview-wells" && method === "POST") {
      return json(DECK_LABWARE[0].wells);
    }
    if (path === "/protocol/indentation.yaml") {
      return json({
        filename: "indentation.yaml",
        positions: null,
        steps: [{ command: "move", args: { position: "plate_1.A1" } }],
      });
    }
    // ── Versioned runs resource (drives the step-execution view) ──────
    if (path === "/runs" && method === "POST") {
      const runId = (body as { run_id?: string } | null)?.run_id ?? "run-1";
      return json({ ...runRecord(runId, options.run), state: "queued" }, 202);
    }
    if (path.startsWith("/runs/") && path.endsWith("/plan")) {
      const runId = path.slice("/runs/".length, -"/plan".length);
      return json({
        run_id: runId,
        steps: (options.run?.plan ?? []).map((step) => ({ args: {}, ...step })),
      });
    }
    if (path.startsWith("/runs/") && path.includes("/events")) {
      const runId = path.slice("/runs/".length).split("/")[0];
      return json(runEvents(runId, options.run));
    }
    if (path.startsWith("/runs/") && path.endsWith("/cancel") && method === "POST") {
      const runId = path.slice("/runs/".length, -"/cancel".length);
      return json(runRecord(runId, options.run));
    }
    if (path.startsWith("/runs/") && method === "GET") {
      return json(runRecord(path.slice("/runs/".length), options.run));
    }

    if (path === "/data/campaigns") return json([]);
    if (path === "/fluid-states") return json(withFluidStates ? [FLUID_STATE_SUMMARY] : []);
    if (path === "/fluid-states/1") return json(FLUID_STATE_DETAIL);
    if (path === "/fluid-states/1/containers") return json(FLUID_STATE_DETAIL.containers);
    if (path === "/fluid-states/1/tips") {
      return json({
        fluid_state_id: 1,
        containers: [],
        pipette: {
          pipette_key: "pipette_1",
          rack_key: null,
          slot_id: null,
          tip_extension_mm: null,
          contents_known_empty: true,
          attachment_uncertain: false,
          updated_at: "2026-07-01T00:05:00Z",
        },
      });
    }
    if (path === "/fluid-states/1/caps") return json({ fluid_state_id: 1, containers: [] });
    if (path === "/fluid-states/1/reconciliation") return json({ fluid_state_id: 1, items: [] });

    return json({ detail: `Unmocked endpoint: ${method} ${path}` }, 404);
  });

  return state;
}

export function requestsTo(state: MockApiState, method: string, path: string): RecordedRequest[] {
  return state.requests.filter((entry) => entry.method === method && entry.path === path);
}
