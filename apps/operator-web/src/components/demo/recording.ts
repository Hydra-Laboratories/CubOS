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
  observation_uncertainty_ms: number;
  video_time_basis: "poll_observation";
}

export interface RecordingManifest {
  recordingId: string;
  campaignId: string | null;
  startedWallTime: string;
  mimeType: string;
  chunkCount: number;
  status: "recording" | "ready" | "incomplete";
  sidecar?: Record<string, unknown>;
  stills?: { eventSequence: number; filename: string }[];
}

export function campaignToAssociate(
  campaigns: DemoCampaignChoice[],
  baseline: Map<string, string | undefined>,
): DemoCampaignChoice | null {
  return [...campaigns]
    .filter((campaign) => {
      return !baseline.has(String(campaign.campaign_id)) && campaign.state === "running";
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

export function verifiedPhotoWindowRemainingMs(event: DemoEvent, serverNowEstimateMs: number | null): number | null {
  const stableUntil = event.data?.stable_until_server_time;
  if (serverNowEstimateMs == null || typeof stableUntil !== "number" || !Number.isFinite(stableUntil)) return null;
  return stableUntil * 1000 - serverNowEstimateMs;
}

export function hasVerifiedPhotoCaptureWindow(event: DemoEvent, serverNowEstimateMs: number | null): boolean {
  const remaining = verifiedPhotoWindowRemainingMs(event, serverNowEstimateMs);
  return remaining != null && remaining > 250;
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
    observation_uncertainty_ms: Math.max(0, pollingUncertaintyMs),
    video_time_basis: "poll_observation",
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

export function captureVideoStill(video: HTMLVideoElement): Promise<Blob | null> {
  if (!video.videoWidth || !video.videoHeight) return Promise.resolve(null);
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d")?.drawImage(video, 0, 0);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.94));
}

export async function persistRecordingChunk(recordingId: string, index: number, chunk: Blob): Promise<boolean> {
  if (!("indexedDB" in globalThis)) return false;
  return new Promise<boolean>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 3);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("chunks")) request.result.createObjectStore("chunks", { keyPath: ["recordingId", "index"] });
      if (!request.result.objectStoreNames.contains("recordings")) request.result.createObjectStore("recordings", { keyPath: "recordingId" });
      if (!request.result.objectStoreNames.contains("stills")) request.result.createObjectStore("stills", { keyPath: ["recordingId", "eventSequence"] });
    };
    request.onerror = () => resolve(false);
    request.onsuccess = () => {
      const transaction = request.result.transaction("chunks", "readwrite");
      transaction.objectStore("chunks").put({ recordingId, index, chunk });
      transaction.oncomplete = () => { request.result.close(); resolve(true); };
      transaction.onerror = () => { request.result.close(); resolve(false); };
    };
  });
}

export async function deleteRecordingChunks(recordingId: string): Promise<void> {
  if (!("indexedDB" in globalThis)) return;
  await new Promise<void>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 3);
    request.onerror = () => resolve();
    request.onsuccess = () => {
      const transaction = request.result.transaction(["chunks", "recordings", "stills"], "readwrite");
      transaction.objectStore("chunks").delete(IDBKeyRange.bound([recordingId, 0], [recordingId, Number.MAX_SAFE_INTEGER]));
      transaction.objectStore("recordings").delete(recordingId);
      transaction.objectStore("stills").delete(IDBKeyRange.bound([recordingId, 0], [recordingId, Number.MAX_SAFE_INTEGER]));
      transaction.oncomplete = () => { request.result.close(); resolve(); };
      transaction.onerror = () => { request.result.close(); resolve(); };
    };
  });
}

export async function loadRecordingChunks(recordingId: string): Promise<{ index: number; chunk: Blob }[]> {
  if (!("indexedDB" in globalThis)) return [];
  return new Promise<{ index: number; chunk: Blob }[]>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 3);
    request.onerror = () => resolve([]);
    request.onsuccess = () => {
      const transaction = request.result.transaction("chunks", "readonly");
      const getAll = transaction.objectStore("chunks").getAll(IDBKeyRange.bound([recordingId, 0], [recordingId, Number.MAX_SAFE_INTEGER]));
      getAll.onsuccess = () => resolve((getAll.result as { index: number; chunk: Blob }[])
        .sort((left, right) => left.index - right.index));
      getAll.onerror = () => resolve([]);
      transaction.oncomplete = () => request.result.close();
    };
  });
}

export async function saveRecordingManifest(manifest: RecordingManifest): Promise<boolean> {
  if (!("indexedDB" in globalThis)) return false;
  return new Promise<boolean>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 3);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("chunks")) request.result.createObjectStore("chunks", { keyPath: ["recordingId", "index"] });
      if (!request.result.objectStoreNames.contains("recordings")) request.result.createObjectStore("recordings", { keyPath: "recordingId" });
      if (!request.result.objectStoreNames.contains("stills")) request.result.createObjectStore("stills", { keyPath: ["recordingId", "eventSequence"] });
    };
    request.onerror = () => resolve(false);
    request.onsuccess = () => {
      const transaction = request.result.transaction("recordings", "readwrite");
      transaction.objectStore("recordings").put(manifest);
      transaction.oncomplete = () => { request.result.close(); resolve(true); };
      transaction.onerror = () => { request.result.close(); resolve(false); };
    };
  });
}

export async function listRecordingManifests(): Promise<RecordingManifest[]> {
  if (!("indexedDB" in globalThis)) return [];
  return new Promise<RecordingManifest[]>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 3);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("chunks")) request.result.createObjectStore("chunks", { keyPath: ["recordingId", "index"] });
      if (!request.result.objectStoreNames.contains("recordings")) request.result.createObjectStore("recordings", { keyPath: "recordingId" });
      if (!request.result.objectStoreNames.contains("stills")) request.result.createObjectStore("stills", { keyPath: ["recordingId", "eventSequence"] });
    };
    request.onerror = () => resolve([]);
    request.onsuccess = () => {
      const transaction = request.result.transaction("recordings", "readonly");
      const getAll = transaction.objectStore("recordings").getAll();
      getAll.onsuccess = () => resolve((getAll.result as RecordingManifest[]).filter((item) => item.chunkCount > 0 || item.status === "ready"));
      getAll.onerror = () => resolve([]);
      transaction.oncomplete = () => request.result.close();
    };
  });
}

export async function persistRecordingStill(recordingId: string, eventSequence: number, blob: Blob): Promise<boolean> {
  if (!("indexedDB" in globalThis)) return false;
  return new Promise<boolean>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 3);
    request.onerror = () => resolve(false);
    request.onsuccess = () => {
      const transaction = request.result.transaction("stills", "readwrite");
      transaction.objectStore("stills").put({ recordingId, eventSequence, blob });
      transaction.oncomplete = () => { request.result.close(); resolve(true); };
      transaction.onerror = () => { request.result.close(); resolve(false); };
    };
  });
}

export async function loadRecordingStill(recordingId: string, eventSequence: number): Promise<Blob | null> {
  if (!("indexedDB" in globalThis)) return null;
  return new Promise<Blob | null>((resolve) => {
    const request = indexedDB.open("cubos-demo-recordings", 3);
    request.onerror = () => resolve(null);
    request.onsuccess = () => {
      const transaction = request.result.transaction("stills", "readonly");
      const get = transaction.objectStore("stills").get([recordingId, eventSequence]);
      get.onsuccess = () => resolve((get.result as { blob?: Blob } | undefined)?.blob ?? null);
      get.onerror = () => resolve(null);
      transaction.oncomplete = () => request.result.close();
    };
  });
}
