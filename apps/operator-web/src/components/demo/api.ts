import type { CampaignPresentation, DemoCampaignChoice, DemoMarker } from "./types";

const campaignBase = "/api/v1/campaigns";

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error((await response.text()) || `${response.status} request failed`);
  return response.json() as Promise<T>;
}

export const demoApi = {
  listCampaigns: () => fetch(campaignBase).then((response) => readJson<DemoCampaignChoice[]>(response)),
  presentation: (campaignId: string | number) =>
    fetch(`${campaignBase}/${encodeURIComponent(campaignId)}/presentation`)
      .then((response) => readJson<CampaignPresentation>(response)),
  assetUrl: (campaignId: string | number, assetId: string) =>
    `${campaignBase}/${encodeURIComponent(campaignId)}/presentation/assets/${encodeURIComponent(assetId)}`,
  exportUrl: (campaignId: string | number) =>
    `${campaignBase}/${encodeURIComponent(campaignId)}/presentation/export.zip`,
  addMarker: (campaignId: string | number, body: {
    client_id: string;
    label: string;
    client_time?: number;
    footage_offset_ms?: number;
    timeline_elapsed_ms?: number;
    video_time_ms?: number;
    recording_name?: string;
    recording_size?: number;
    recording_last_modified?: number;
    recording_sha256?: string;
  }) =>
    fetch(`${campaignBase}/${encodeURIComponent(campaignId)}/presentation/markers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((response) => readJson<DemoMarker>(response)),
};
