import type { CameraMonitorStatus } from "./cameraMonitorTypes";
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
