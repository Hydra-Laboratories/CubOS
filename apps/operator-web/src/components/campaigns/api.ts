import type { CampaignRecord, CampaignSpec } from "./types";

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
};
