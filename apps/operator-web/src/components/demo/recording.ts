import type { DemoCampaignChoice, DemoEvent } from "./types";

const RECORDER_MIME_TYPES = [
  "video/webm;codecs=vp9",
  "video/webm;codecs=vp8",
  "video/webm",
  "video/mp4",
];

export interface DemoSyncEvent {
  sequence: number;
  kind: string;
  label: string;
  trial_id?: string | null;
  server_time: string | number;
  timeline_elapsed_ms: number;
  observed_wall_time: string;
  observed_performance_ms: number;
  video_elapsed_ms: number;
  polling_uncertainty_ms: number;
}

export function campaignToAssociate(
  campaigns: DemoCampaignChoice[],
  baseline: Map<string, string | undefined>,
): DemoCampaignChoice | null {
  return [...campaigns]
    .filter((campaign) => {
      const previousState = baseline.get(String(campaign.campaign_id));
      return !baseline.has(String(campaign.campaign_id))
        || (previousState !== "running" && campaign.state === "running");
    })
    .sort((left, right) => String(right.created_at ?? "").localeCompare(String(left.created_at ?? "")))[0] ?? null;
}

export function supportedRecorderMime(MediaRecorderClass: typeof MediaRecorder = MediaRecorder): string {
  return RECORDER_MIME_TYPES.find((mime) => MediaRecorderClass.isTypeSupported(mime)) ?? "";
}

export function stopMediaStream(stream: MediaStream | null): void {
  stream?.getTracks().forEach((track) => track.stop());
}

export function isPhotoPauseEvent(event: DemoEvent): boolean {
  return event.kind === "photo_pause"
    && event.data?.capture_still === true
    && event.data?.wait_completed === true;
}

export function syncEvent(
  event: DemoEvent,
  recordingStartedPerformanceMs: number,
  observedPerformanceMs: number,
  pollingUncertaintyMs: number,
  observedWallTime = new Date().toISOString(),
): DemoSyncEvent {
  return {
    sequence: event.sequence,
    kind: event.kind,
    label: event.label,
    trial_id: event.trial_id,
    server_time: event.server_time,
    timeline_elapsed_ms: event.elapsed_ms,
    observed_wall_time: observedWallTime,
    observed_performance_ms: observedPerformanceMs,
    video_elapsed_ms: Math.max(0, observedPerformanceMs - recordingStartedPerformanceMs),
    polling_uncertainty_ms: Math.max(0, pollingUncertaintyMs),
  };
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function captureVideoStill(video: HTMLVideoElement, filename: string): boolean {
  if (!video.videoWidth || !video.videoHeight) return false;
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d")?.drawImage(video, 0, 0);
  canvas.toBlob((blob) => { if (blob) downloadBlob(blob, filename); }, "image/jpeg", 0.94);
  return true;
}

export async function persistRecordingChunk(recordingId: string, index: number, chunk: Blob): Promise<void> {
  if (!("indexedDB" in globalThis)) return;
  await new Promise<void>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("chunks", { keyPath: ["recordingId", "index"] });
    request.onerror = () => resolve();
    request.onsuccess = () => {
      const transaction = request.result.transaction("chunks", "readwrite");
      transaction.objectStore("chunks").put({ recordingId, index, chunk });
      transaction.oncomplete = () => { request.result.close(); resolve(); };
      transaction.onerror = () => { request.result.close(); resolve(); };
    };
  });
}

export async function loadRecordingChunks(recordingId: string): Promise<Blob[]> {
  if (!("indexedDB" in globalThis)) return [];
  return new Promise<Blob[]>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("chunks", { keyPath: ["recordingId", "index"] });
    request.onerror = () => resolve([]);
    request.onsuccess = () => {
      const transaction = request.result.transaction("chunks", "readonly");
      const getAll = transaction.objectStore("chunks").getAll(IDBKeyRange.bound([recordingId, 0], [recordingId, Number.MAX_SAFE_INTEGER]));
      getAll.onsuccess = () => resolve((getAll.result as { index: number; chunk: Blob }[])
        .sort((left, right) => left.index - right.index).map((entry) => entry.chunk));
      getAll.onerror = () => resolve([]);
      transaction.oncomplete = () => request.result.close();
    };
  });
}
