import type { OvernightQueueRecord } from "./types";

const base = "/api/v1/campaigns/overnight";

async function read<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error((await response.text()) || `${response.status} request failed`);
  return response.json() as Promise<T>;
}

export const overnightApi = {
  list: () => fetch(base).then((response) => read<OvernightQueueRecord[]>(response)),
  get: (queueId: string) => fetch(`${base}/${encodeURIComponent(queueId)}`).then((response) => read<OvernightQueueRecord>(response)),
  start: (queueId: string) => fetch(`${base}/${encodeURIComponent(queueId)}/start`, { method: "POST" }).then((response) => read<OvernightQueueRecord>(response)),
  cancel: (queueId: string) => fetch(`${base}/${encodeURIComponent(queueId)}/cancel`, { method: "POST" }).then((response) => read<OvernightQueueRecord>(response)),
};
