import { useCallback, useEffect, useRef, useState } from "react";
import type { DemoEvent } from "./types";
import {
  captureVideoStill,
  deleteRecordingChunks,
  downloadBlob,
  isPhotoPauseEvent,
  loadRecordingChunks,
  loadRecordingStill,
  listRecordingManifests,
  persistRecordingChunk,
  persistRecordingStill,
  saveRecordingManifest,
  stopMediaStream,
  supportedRecorderMime,
  syncEvent,
  hasVerifiedPhotoCaptureWindow,
  type DemoSyncEvent,
  type RecordingManifest,
} from "./recording";

interface StillRecord {
  event_sequence: number;
  requested_performance_ms: number;
  captured_performance_ms?: number;
  filename?: string;
  status: "scheduled" | "stored" | "unavailable";
  reason?: string;
}

function makeSidecar(campaignId: string, recordingId: string, startedWallTime: string,
  startedPerformanceMs: number, mimeType: string, events: DemoSyncEvent[], stills: StillRecord[]) {
  return {
    schema_version: "1", campaign_id: campaignId || null, recording_id: recordingId,
    recording_started_wall_time: startedWallTime, recording_started_performance_ms: startedPerformanceMs,
    mime_type: mimeType, audio: false,
    clock_note: "video_elapsed_ms is poll-observation time; observation_uncertainty_ms is a one-sided upper bound measured since the prior successful presentation response",
    events, stills,
  };
}

export function useDemoRecording(campaignId: string, events: DemoEvent[], pollingUncertaintyMs: number,
  serverNowEstimateMs: number | null, serverClockObservedPerformanceMs: number | null) {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [enabling, setEnabling] = useState(false);
  const [recording, setRecording] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [recoverable, setRecoverable] = useState<RecordingManifest[]>([]);
  const [error, setError] = useState<string | null>(null);
  const previewRef = useRef<HTMLVideoElement | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const chunkWritesRef = useRef<Promise<boolean>[]>([]);
  const stillWritesRef = useRef<Promise<void>[]>([]);
  const syncRef = useRef<DemoSyncEvent[]>([]);
  const stillsRef = useRef<StillRecord[]>([]);
  const observedSequencesRef = useRef(new Set<number>());
  const photoSequencesRef = useRef(new Set<number>());
  const timersRef = useRef<number[]>([]);
  const startedPerformanceRef = useRef(0);
  const startedWallRef = useRef("");
  const recordingIdRef = useRef("");
  const manifestWriteRef = useRef<Promise<boolean>>(Promise.resolve(false));
  const manifestRef = useRef<RecordingManifest | null>(null);
  const campaignIdRef = useRef(campaignId);

  useEffect(() => { campaignIdRef.current = campaignId; }, [campaignId]);
  useEffect(() => { void listRecordingManifests().then(setRecoverable); }, []);

  const setPreviewElement = useCallback((element: HTMLVideoElement | null) => {
    previewRef.current = element;
    if (element) element.srcObject = stream;
  }, [stream]);

  useEffect(() => {
    if (previewRef.current) previewRef.current.srcObject = stream;
  }, [stream]);

  const refreshDevices = useCallback(async () => {
    const available = await navigator.mediaDevices.enumerateDevices();
    setDevices(available.filter((device) => device.kind === "videoinput"));
  }, []);

  const enableCamera = useCallback(async (selectedDeviceId = deviceId) => {
    if (enabling) return;
    if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setError("Camera access requires a secure browser origin. Open CubOS through an SSH tunnel at http://localhost:8742; direct Pi HTTP addresses cannot access the Mac camera.");
      return;
    }
    setEnabling(true);
    setError(null);
    let nextStream: MediaStream | null = null;
    try {
      nextStream = await navigator.mediaDevices.getUserMedia({
        video: selectedDeviceId ? { deviceId: { exact: selectedDeviceId } } : true,
        audio: false,
      });
      setStream((previous) => { stopMediaStream(previous); return nextStream; });
      await refreshDevices();
      const actualDeviceId = nextStream.getVideoTracks()[0]?.getSettings().deviceId;
      if (actualDeviceId) setDeviceId(actualDeviceId);
    } catch (reason) {
      stopMediaStream(nextStream);
      setError(`Camera unavailable: ${reason instanceof Error ? reason.message : String(reason)}`);
    } finally {
      setEnabling(false);
    }
  }, [deviceId, enabling, refreshDevices]);

  const startRecording = useCallback(() => {
    if (finalizing) return;
    if (!stream || !("MediaRecorder" in globalThis)) {
      setError("This browser cannot record the camera preview.");
      return;
    }
    const mimeType = supportedRecorderMime();
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const now = performance.now();
    startedPerformanceRef.current = now;
    startedWallRef.current = new Date().toISOString();
    recordingIdRef.current = globalThis.crypto?.randomUUID?.() ?? `recording-${Date.now()}`;
    const recordingId = recordingIdRef.current;
    chunksRef.current = [];
    chunkWritesRef.current = [];
    stillWritesRef.current = [];
    syncRef.current = [];
    stillsRef.current = [];
    observedSequencesRef.current = new Set(events.map((event) => event.sequence));
    photoSequencesRef.current = new Set();
    manifestRef.current = {
      recordingId,
      campaignId: campaignIdRef.current || null,
      startedWallTime: startedWallRef.current,
      mimeType: recorder.mimeType || mimeType || "video/webm",
      chunkCount: 0,
      status: "recording",
    };
    manifestWriteRef.current = saveRecordingManifest(manifestRef.current);
    void manifestWriteRef.current.then((saved) => { if (!saved) setError("Persistent recording storage is unavailable; this recording is using browser memory until download."); });
    recorder.ondataavailable = (event) => {
      if (!event.data.size) return;
      const index = chunksRef.current.push(event.data) - 1;
      const write = persistRecordingChunk(recordingId, index, event.data);
      chunkWritesRef.current.push(write);
      void write.then((persisted) => {
        if (!persisted) return;
        chunksRef.current[index] = new Blob();
        if (!manifestRef.current || manifestRef.current.recordingId !== recordingId) return;
        manifestRef.current = { ...manifestRef.current, chunkCount: Math.max(manifestRef.current.chunkCount, index + 1),
          sidecar: makeSidecar(campaignIdRef.current, recordingId, startedWallRef.current,
            startedPerformanceRef.current, recorder.mimeType || mimeType || "video/webm", syncRef.current, stillsRef.current) };
        manifestWriteRef.current = manifestWriteRef.current.then(() => saveRecordingManifest({ ...manifestRef.current! }));
      });
    };
    recorder.onerror = () => {
      setError("Recording stopped because the browser reported a recorder error.");
      setRecording(false);
    };
    recorder.onstop = async () => {
      setFinalizing(true);
      const associatedCampaignId = campaignIdRef.current;
      const base = `cubos-${associatedCampaignId || "unassociated-campaign"}-${startedWallRef.current.replaceAll(":", "-")}`;
      const type = recorder.mimeType || mimeType || "video/webm";
      await Promise.allSettled([manifestWriteRef.current, ...chunkWritesRef.current, ...stillWritesRef.current]);
      const persisted = await loadRecordingChunks(recordingId);
      const persistedByIndex = new Map(persisted.map((entry) => [entry.index, entry.chunk]));
      const merged = Array.from({ length: chunksRef.current.length }, (_, index) => persistedByIndex.get(index) ?? chunksRef.current[index]);
      const complete = merged.length === chunksRef.current.length && merged.every((chunk) => chunk?.size);
      const sidecar = makeSidecar(associatedCampaignId, recordingId, startedWallRef.current,
        startedPerformanceRef.current, type, syncRef.current, stillsRef.current);
      if (complete) {
        downloadBlob(new Blob(merged, { type }), `${base}.${type.includes("mp4") ? "mp4" : "webm"}`);
        downloadBlob(new Blob([JSON.stringify(sidecar, null, 2)], { type: "application/json" }), `${base}.sync.json`);
      } else setError("A recording chunk is missing. No truncated download was created; inspect Saved recordings.");
      manifestRef.current = {
        recordingId,
        campaignId: associatedCampaignId || null,
        startedWallTime: startedWallRef.current,
        mimeType: type,
        chunkCount: chunksRef.current.length,
        status: complete ? "ready" : "incomplete",
        sidecar,
        stills: manifestRef.current?.stills,
      };
      manifestWriteRef.current = manifestWriteRef.current.then(() => saveRecordingManifest({ ...manifestRef.current! }));
      await manifestWriteRef.current;
      setRecoverable(await listRecordingManifests());
      setRecording(false);
      setFinalizing(false);
    };
    recorderRef.current = recorder;
    recorder.start(2000);
    setRecording(true);
    setError(null);
  }, [events, finalizing, stream]);

  const stopRecording = useCallback(() => {
    if (recorderRef.current?.state !== "inactive") recorderRef.current?.stop();
  }, []);

  const downloadRecoverable = useCallback(async (manifest: RecordingManifest) => {
    const entries = await loadRecordingChunks(manifest.recordingId);
    if (entries.length !== manifest.chunkCount || !entries.every((entry, index) => entry.index === index && entry.chunk.size > 0)) {
      setError("The recoverable recording is incomplete and was not downloaded.");
      return;
    }
    const extension = manifest.mimeType.includes("mp4") ? "mp4" : "webm";
    downloadBlob(new Blob(entries.map((entry) => entry.chunk), { type: manifest.mimeType }), `cubos-${manifest.campaignId ?? "unassociated-campaign"}-${manifest.startedWallTime.replaceAll(":", "-")}.${extension}`);
    if (manifest.sidecar) downloadBlob(new Blob([JSON.stringify(manifest.sidecar, null, 2)], { type: "application/json" }), `cubos-${manifest.campaignId ?? "unassociated-campaign"}-${manifest.startedWallTime.replaceAll(":", "-")}.sync.json`);
  }, []);

  const downloadRecoverableStill = useCallback(async (manifest: RecordingManifest, eventSequence: number, filename: string) => {
    const blob = await loadRecordingStill(manifest.recordingId, eventSequence);
    if (!blob) {
      setError("The saved still is unavailable.");
      return;
    }
    downloadBlob(blob, filename);
  }, []);

  const discardRecoverable = useCallback(async (recordingId: string) => {
    await deleteRecordingChunks(recordingId);
    setRecoverable(await listRecordingManifests());
  }, []);

  useEffect(() => {
    if (!recording) return;
    const newlyObserved = events.filter((event) => !observedSequencesRef.current.has(event.sequence));
    if (!newlyObserved.length) return;
    const observed = performance.now();
    newlyObserved.forEach((event) => {
      observedSequencesRef.current.add(event.sequence);
      syncRef.current.push(syncEvent(event, startedPerformanceRef.current, observed, pollingUncertaintyMs));
      if (manifestRef.current) {
        manifestRef.current.sidecar = makeSidecar(campaignIdRef.current, recordingIdRef.current,
          startedWallRef.current, startedPerformanceRef.current, manifestRef.current.mimeType, syncRef.current, stillsRef.current);
        manifestWriteRef.current = manifestWriteRef.current.then(() => saveRecordingManifest({ ...manifestRef.current! }));
      }
      if (!isPhotoPauseEvent(event) || photoSequencesRef.current.has(event.sequence)) return;
      photoSequencesRef.current.add(event.sequence);
      const still: StillRecord = { event_sequence: event.sequence, requested_performance_ms: observed, status: "scheduled" };
      stillsRef.current.push(still);
      const serverNowAtCapture = serverNowEstimateMs == null || serverClockObservedPerformanceMs == null
        ? null
        : serverNowEstimateMs + Math.max(0, performance.now() - serverClockObservedPerformanceMs);
      const hasVerifiedWindow = hasVerifiedPhotoCaptureWindow(event, serverNowAtCapture);
      if (hasVerifiedWindow && previewRef.current) {
        const filename = `cubos-${campaignIdRef.current || "campaign"}-event-${event.sequence}.jpg`;
        const captureRecordingId = recordingIdRef.current;
        const stillWrite = captureVideoStill(previewRef.current).then(async (blob) => {
          still.captured_performance_ms = performance.now();
          if (!blob) {
            still.status = "unavailable";
            still.reason = "preview_frame_unavailable";
            return;
          }
          const stored = await persistRecordingStill(captureRecordingId, event.sequence, blob);
          still.filename = stored ? filename : undefined;
          still.status = stored ? "stored" : "unavailable";
          still.reason = stored ? undefined : "still_storage_failed";
          if (!manifestRef.current || manifestRef.current.recordingId !== captureRecordingId) return;
          manifestRef.current.stills = stored
            ? [...(manifestRef.current.stills ?? []), { eventSequence: event.sequence, filename }]
            : manifestRef.current.stills;
          manifestRef.current.sidecar = makeSidecar(campaignIdRef.current, captureRecordingId,
            startedWallRef.current, startedPerformanceRef.current, manifestRef.current.mimeType, syncRef.current, stillsRef.current);
          manifestWriteRef.current = manifestWriteRef.current.then(() => saveRecordingManifest({ ...manifestRef.current! }));
        });
        stillWritesRef.current.push(stillWrite);
      } else {
        still.status = "unavailable";
        still.reason = "pose_window_unverified";
      }
      if (manifestRef.current) {
        manifestRef.current.sidecar = makeSidecar(campaignIdRef.current, recordingIdRef.current,
          startedWallRef.current, startedPerformanceRef.current, manifestRef.current.mimeType, syncRef.current, stillsRef.current);
        manifestWriteRef.current = manifestWriteRef.current.then(() => saveRecordingManifest({ ...manifestRef.current! }));
      }
    });
  }, [campaignId, events, pollingUncertaintyMs, recording, serverClockObservedPerformanceMs, serverNowEstimateMs]);

  useEffect(() => () => {
    timersRef.current.forEach(window.clearTimeout);
    if (recorderRef.current?.state !== "inactive") recorderRef.current?.stop();
    stopMediaStream(stream);
  }, [stream]);

  return {
    devices, deviceId, setDeviceId, stream, enabling, recording, finalizing, recoverable, error,
    setPreviewElement, enableCamera, startRecording, stopRecording, downloadRecoverable, downloadRecoverableStill, discardRecoverable,
  };
}
