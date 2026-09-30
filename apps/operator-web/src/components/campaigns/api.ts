import type { CameraMonitorStatus, CampaignPresetResponse, CampaignPresetSummary, CampaignRecord, CampaignSpec, ColorCampaignSetup, ColorSetupDraft, ColorTargetRun } from "./types";

const base = "/api/v1/campaigns";
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, { headers: { "Content-Type": "application/json" }, ...init });
  if (!response.ok) throw new Error((await response.text()) || `${response.status} request failed`);
  return response.json() as Promise<T>;
}
const post = <T>(path: string, body: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body) });
export const campaignApi = {
  list: () => request<CampaignRecord[]>(""),
  validate: (spec: CampaignSpec) => post<{ valid: boolean; errors: string[]; preview?: { parameters: Record<string, number>; protocol_yaml: string } }>("/validate", { spec }),
  create: (spec: CampaignSpec) => post<CampaignRecord>("", { spec }),
  get: (id: string | number) => request<CampaignRecord>(`/${id}`),
  pause: (id: string | number) => post<CampaignRecord>(`/${id}/pause`, {}),
  resume: (id: string | number) => post<CampaignRecord>(`/${id}/resume`, {}),
  stop: (id: string | number) => post<CampaignRecord>(`/${id}/stop`, {}),
  cancel: (id: string | number) => post<CampaignRecord>(`/${id}/cancel`, {}),
  observation: (id: string | number, value: number) => post<CampaignRecord>(`/${id}/observation`, { value }),
  readColorTarget: (body: Omit<ColorCampaignSetup, "target_lab" | "red_source" | "yellow_source" | "blue_source" | "candidate_wells" | "fluid_state_id" | "source_protocol_file" | "batch_size">) =>
    post<ColorTargetRun>("/color-target", body),
  getColorTarget: (runId: string) => request<ColorTargetRun>(`/color-target/${runId}`),
  colorTargetImageUrl: (runId: string) => `${base}/color-target/${encodeURIComponent(runId)}/image`,
  colorTargetAnalysisImageUrl: (runId: string, revision = 0) => `${base}/color-target/${encodeURIComponent(runId)}/analysis-image?revision=${revision}`,
  reanalyzeColorTarget: (runId: string, expectedCenter: [number, number]) =>
    post<Record<string, unknown>>(`/color-target/${encodeURIComponent(runId)}/reanalyze`, {
      expected_center: expectedCenter,
      expected_center_source: "operator_selected",
    }),
  previewRgbTarget: (rgb: [number, number, number]) =>
    post<{ rgb: [number, number, number]; lab: [number, number, number] }>("/color-target/rgb-preview", {
      rgb,
    }),
  prepareColor: (body: ColorCampaignSetup) => post<CampaignSpec>("/color-setup", body),
  attachFluidState: (id: string | number, fluidStateId: number, reconciliationNote: string) =>
    post<CampaignRecord>(`/${id}/fluid-state`, { fluid_state_id: fluidStateId, reconciliation_note: reconciliationNote }),
  listPresets: () => request<CampaignPresetSummary[]>("/presets"),
  getPreset: (filename: string) => request<CampaignPresetResponse>(`/presets/${encodeURIComponent(filename)}`),
  savePreset: (filename: string, name: string, spec: CampaignSpec, colorSetup: ColorSetupDraft) =>
    request<CampaignPresetResponse>(`/presets/${encodeURIComponent(filename)}`, {
      method: "PUT",
      body: JSON.stringify({ name, spec, color_setup: colorSetup }),
    }),
};

export interface StockReconciliationResponse {
  fluid_state_id: number;
  operation_key: string;
  target: string;
  volume_ul: number;
  composition: Record<string, number>;
  operator: string;
  reason: string;
  status: string;
}

export async function reconcileCampaignStock(
  fluidStateId: number,
  body: {
    target: string;
    volume_ul: number;
    composition?: Record<string, number>;
    operation_key: string;
    operator: string;
    reason: string;
  },
): Promise<StockReconciliationResponse> {
  const response = await fetch(`/api/v1/fluid-states/${fluidStateId}/reconcile-stock`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error((await response.text()) || `${response.status} request failed`);
  return response.json() as Promise<StockReconciliationResponse>;
}

const cameraMonitorBase = "/api/v1/instruments/camera/monitor";
export const cameraMonitorApi = {
  start: (instrument: string) => fetch(`${cameraMonitorBase}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instrument }),
  }).then(async (response) => {
    if (!response.ok) throw new Error((await response.text()) || `${response.status} request failed`);
    return response.json() as Promise<CameraMonitorStatus>;
  }),
  heartbeat: (instrument: string, leaseId: string) => fetch(`${cameraMonitorBase}/heartbeat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instrument, lease_id: leaseId }),
  }).then(async (response) => {
    if (!response.ok) throw new Error((await response.text()) || `${response.status} request failed`);
    return response.json() as Promise<CameraMonitorStatus>;
  }),
  stop: (instrument: string, leaseId: string) => fetch(`${cameraMonitorBase}/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instrument, lease_id: leaseId }),
  }).then(async (response) => {
    if (!response.ok) throw new Error((await response.text()) || `${response.status} request failed`);
    return response.json() as Promise<CameraMonitorStatus>;
  }),
  get: (instrument: string) => fetch(`${cameraMonitorBase}?instrument=${encodeURIComponent(instrument)}`)
    .then(async (response) => {
      if (!response.ok) throw new Error((await response.text()) || `${response.status} request failed`);
      return response.json() as Promise<CameraMonitorStatus>;
    }),
  frameUrl: (instrument: string, frameId: number) =>
    `${cameraMonitorBase}/frame?instrument=${encodeURIComponent(instrument)}&frame_id=${frameId}`,
};
