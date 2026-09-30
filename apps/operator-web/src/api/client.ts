const BASE = "/api/v1";

export type SettingsResponse = {
  config_dir: string;
};

export type UpdateStatus = {
  current_sha: string;
  latest_sha: string;
  commits_behind: number;
  update_available: boolean;
  checked_at: number;
  summary: string[];
  error: string | null;
};

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function errorMessageFromResponse(statusText: string, text: string): string {
  if (text) {
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === "string") {
        return parsed.detail;
      }
    } catch {
      // Fall back to the raw body below.
    }
    return text;
  }
  return statusText || "Request failed";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new ApiError(res.status, errorMessageFromResponse(res.statusText, text));
  }
  return res.json();
}

async function download(path: string): Promise<Blob> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    const text = await res.text();
    throw new ApiError(res.status, errorMessageFromResponse(res.statusText, text));
  }
  return res.blob();
}

// Deck
export const deckApi = {
  listConfigs: () => request<string[]>("/deck/configs"),
  get: (filename: string) =>
    request<import("../types").DeckResponse>(`/deck/${filename}`),
  put: (filename: string, body: import("../types").DeckConfig) =>
    request<import("../types").DeckResponse>(`/deck/${filename}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  delete: (filename: string) =>
    request<{ status: string; filename: string }>(`/deck/${filename}`, { method: "DELETE" }),
  previewWells: (config: import("../types").WellPlateConfig) =>
    request<Record<string, import("../types").WellPosition>>("/deck/preview-wells", {
      method: "POST",
      body: JSON.stringify(config),
    }),
};

// Gantry
export const gantryApi = {
  listConfigs: () => request<string[]>("/gantry/configs"),
  listInstrumentTypes: () =>
    request<import("../types").InstrumentTypeInfo[]>("/gantry/instrument-types"),
  listPipetteModels: () =>
    request<import("../types").PipetteModelInfo[]>("/gantry/pipette-models"),
  getInstrumentSchemas: () =>
    request<import("../types").InstrumentSchemas>("/gantry/instrument-schemas"),
  getInstrumentMethods: () =>
    request<import("../types").InstrumentMeasurementMethods>("/gantry/instrument-methods"),
  getInstrumentMethodParams: () =>
    request<import("../types").InstrumentMethodParams>("/gantry/instrument-method-params"),
  get: (filename: string) =>
    request<import("../types").GantryResponse>(`/gantry/${filename}`),
  put: (filename: string, body: import("../types").GantryConfig) =>
    request<import("../types").GantryResponse>(`/gantry/${filename}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  delete: (filename: string) =>
    request<{ status: string; filename: string }>(`/gantry/${filename}`, { method: "DELETE" }),
  getPosition: () =>
    request<import("../types").GantryPosition>("/gantry/position"),
  connect: (filename: string) =>
    request<import("../types").GantryPosition>("/gantry/connect", {
      method: "POST",
      body: JSON.stringify({ filename }),
    }),
  disconnect: () =>
    request<import("../types").GantryPosition>("/gantry/disconnect", {
      method: "POST",
    }),
  jog: (x = 0, y = 0, z = 0) =>
    request<{ status: string }>("/gantry/jog", {
      method: "POST",
      body: JSON.stringify({ x, y, z }),
    }),
  home: () =>
    request<import("../types").GantryPosition>("/gantry/home", {
      method: "POST",
    }),
  moveTo: (x: number, y: number, z: number) =>
    request<{ status: string }>("/gantry/move-to", {
      method: "POST",
      body: JSON.stringify({ x, y, z }),
    }),
  moveToBlocking: (x: number, y: number, z: number) =>
    request<import("../types").GantryPosition>("/gantry/move-to-blocking", {
      method: "POST",
      body: JSON.stringify({ x, y, z }),
    }),
  jogBlocking: (x = 0, y = 0, z = 0, timeout_s = 10) =>
    request<import("../types").GantryPosition>("/gantry/jog-blocking", {
      method: "POST",
      body: JSON.stringify({ x, y, z, timeout_s }),
    }),
  setWorkCoordinates: (body: { x?: number; y?: number; z?: number }) =>
    request<import("../types").GantryPosition>("/gantry/work-coordinates", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  prepareCalibrationOrigin: () =>
    request<import("../types").GantryPosition>("/gantry/calibration/prepare-origin", {
      method: "POST",
    }),
  homeAndCenterForCalibration: () =>
    request<{
      xy_bounds: { x: number; y: number; z: number };
      position: { x: number; y: number; z: number };
    }>("/gantry/calibration/home-and-center", {
      method: "POST",
    }),
  restoreCalibrationSoftLimits: () =>
    request<import("../types").GantryPosition>("/gantry/calibration/restore-soft-limits", {
      method: "POST",
    }),
  finalizeCalibrationOrigin: (body: {
    home_z: number;
    block_touch_z: number;
    block_height: number;
    factory_z_travel: number;
    tolerance_mm?: number;
  }) =>
    request<import("../types").FinalizeOriginResponse>("/gantry/calibration/finalize-origin", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  recoverCalibrationLimit: (body: {
    x: number;
    y: number;
    z: number;
    pull_off_mm?: number;
    feed_rate?: number;
  }) =>
    request<{
      status: string;
      attempts: number;
      pull_off: { x: number; y: number; z: number };
      messages: string[];
    }>("/gantry/calibration/recover-limit", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  configureSoftLimits: (body: {
    max_travel_x: number;
    max_travel_y: number;
    max_travel_z: number;
    status_report?: number;
    homing_pull_off?: number;
    tolerance_mm?: number;
  }) =>
    request<{ status: string }>("/gantry/soft-limits", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  unlock: () =>
    request<import("../types").GantryPosition>("/gantry/unlock", {
      method: "POST",
    }),
  resetUnlock: () =>
    request<import("../types").GantryPosition>("/gantry/reset-unlock", {
      method: "POST",
    }),
  resume: () =>
    request<import("../types").GantryPosition>("/gantry/resume", {
      method: "POST",
    }),
  feedHold: () =>
    request<import("../types").GantryPosition>("/gantry/feed-hold", {
      method: "POST",
    }),
  jogCancel: () =>
    request<import("../types").GantryPosition>("/gantry/jog-cancel", {
      method: "POST",
    }),
  readGrblSettings: () =>
    request<import("../types").GrblSettingsResponse>("/gantry/grbl-settings"),
  setGrblSetting: (body: { setting: string; value: string }) =>
    request<import("../types").GrblSettingsResponse>("/gantry/grbl-settings", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

// Protocol
export const protocolApi = {
  listCommands: () =>
    request<import("../types").CommandInfo[]>("/protocol/commands"),
  listConfigs: () => request<string[]>("/protocol/configs"),
  get: (filename: string) =>
    request<import("../types").ProtocolResponse>(`/protocol/${filename}`),
  put: (filename: string, body: import("../types").ProtocolConfig) =>
    request<{ status: string; filename: string }>(`/protocol/${filename}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  delete: (filename: string) =>
    request<{ status: string; filename: string }>(`/protocol/${filename}`, { method: "DELETE" }),
  validate: (body: import("../types").ProtocolConfig) =>
    request<import("../types").ProtocolValidationResponse>(
      "/protocol/validate",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
  validateSetup: (body: import("../types").ProtocolSetupValidationRequest) =>
    request<import("../types").ProtocolValidationResponse>(
      "/protocol/validate-setup",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
  run: (body: {
    gantry_file: string;
    deck_file: string;
    protocol_file: string;
  }, init?: Pick<RequestInit, "signal">) =>
    request<import("../types").ProtocolRunResponse>("/protocol/run", {
      method: "POST",
      body: JSON.stringify(body),
      signal: init?.signal,
    }),
  cancelRun: () =>
    request<{ status: string; warning?: string }>("/protocol/cancel", {
      method: "POST",
    }),
  runStatus: () =>
    request<import("../types").ProtocolRunStatus>("/protocol/run-status"),
};

// Versioned async runs resource (Feature 07: the only submission path that
// accepts fluid-state selection — see protocolApi.run above for the legacy
// synchronous, stateless path)
export const runsApi = {
  submit: (body: import("../types").RunSubmissionBody) =>
    request<import("../types").RunRecord>("/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  get: (runId: string) => request<import("../types").RunRecord>(`/runs/${runId}`),
  plan: (runId: string) =>
    request<import("../types").RunPlanResponse>(`/runs/${runId}/plan`),
  events: (runId: string, after = 0) =>
    request<import("../types").RunEventsResponse>(
      `/runs/${runId}/events?after=${after}`,
    ),
  cancel: (runId: string) =>
    request<import("../types").RunRecord>(`/runs/${runId}/cancel`, {
      method: "POST",
    }),
};

// Fluid/tip/cap state (Feature 07)
export const fluidStateApi = {
  create: (body: import("../types").CreateFluidStateRequest) =>
    request<import("../types").FluidStateSummary>("/fluid-states", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  list: () => request<import("../types").FluidStateSummary[]>("/fluid-states"),
  getActive: () => request<import("../types").ActiveFluidState | null>("/fluid-states/active"),
  selectActive: (fluid_state_id: number, expected_revision?: number) =>
    request<import("../types").ActiveFluidState>("/fluid-states/active", {
      method: "PUT",
      body: JSON.stringify({ fluid_state_id, expected_revision }),
    }),
  editContainer: (fluidStateId: number, body: import("../types").ManualContainerEditRequest) =>
    request<import("../types").ContainerView>(`/fluid-states/${fluidStateId}/containers/edit`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  applyManualEdits: (fluidStateId: number, body: import("../types").ManualEditBatchRequest) =>
    request<import("../types").FluidStateDetail>(`/fluid-states/${fluidStateId}/manual-edits`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listManualEdits: (fluidStateId: number) =>
    request<import("../types").ManualEditView[]>(`/fluid-states/${fluidStateId}/manual-edits`),
  get: (fluidStateId: number) =>
    request<import("../types").FluidStateDetail>(`/fluid-states/${fluidStateId}`),
  getContainers: (fluidStateId: number) =>
    request<import("../types").ContainerView[]>(`/fluid-states/${fluidStateId}/containers`),
  getTips: (fluidStateId: number) =>
    request<import("../types").TipStateResponse>(`/fluid-states/${fluidStateId}/tips`),
  getCaps: (fluidStateId: number) =>
    request<import("../types").CapStateResponse>(`/fluid-states/${fluidStateId}/caps`),
  getOperations: (fluidStateId: number, pendingOnly = true) =>
    request<import("../types").OperationsResponse>(
      `/fluid-states/${fluidStateId}/operations?pending_only=${pendingOnly}`,
    ),
  getReconciliation: (fluidStateId: number) =>
    request<import("../types").ReconciliationResponse>(
      `/fluid-states/${fluidStateId}/reconciliation`,
    ),
  resolveReconciliation: (
    fluidStateId: number,
    body: import("../types").ResolveReconciliationRequest,
  ) =>
    request<import("../types").ResolveReconciliationResponse>(
      `/fluid-states/${fluidStateId}/reconciliation/resolve`,
      { method: "POST", body: JSON.stringify(body) },
    ),
};

// Settings
export const settingsApi = {
  get: () => request<SettingsResponse>("/settings"),
  update: (config_dir: string) =>
    request<SettingsResponse>("/settings", {
      method: "PUT",
      body: JSON.stringify({ config_dir }),
    }),
  browse: () =>
    request<SettingsResponse>("/settings/browse", {
      method: "POST",
    }),
};

export const systemApi = {
  getUpdateStatus: (refresh = false) =>
    request<UpdateStatus>(`/system/update${refresh ? "?refresh=true" : ""}`),
  applyUpdate: () =>
    request<{ status: string; target_sha: string }>("/system/update/apply", {
      method: "POST",
      body: JSON.stringify({}),
    }),
  health: () => request<{ status: string }>("/health"),
};

// Result data
export const dataApi = {
  listCampaigns: () =>
    request<import("../types").CampaignSummary[]>("/data/campaigns"),
  downloadCampaignData: (campaignId: number) =>
    download(`/data/campaigns/${campaignId}/data.zip`),
};

// Manual instrument control (bring-up work, outside protocol runs)
export const instrumentsApi = {
  captureCameraFrame: (instrument: string, preview = false) =>
    request<{ instrument: string; image_path: string }>("/instruments/camera/capture", {
      method: "POST",
      body: JSON.stringify({ instrument, preview }),
    }),
  cameraLastImage: (instrument: string) =>
    // Cache-bust: repeated preview polls hit the same URL as the frame changes.
    download(`/instruments/camera/last-image?instrument=${encodeURIComponent(instrument)}&_=${Date.now()}`),
};
