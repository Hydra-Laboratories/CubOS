import { useCallback, useEffect, useRef, useState } from "react";
import type { DemoEvent } from "./types";
import {
  captureVideoStill,
  downloadBlob,
  isPhotoPauseEvent,
  loadRecordingChunks,
  persistRecordingChunk,
  stopMediaStream,
  supportedRecorderMime,
  syncEvent,
  type DemoSyncEvent,
} from "./recording";

interface StillRecord {
  event_sequence: number;
  requested_performance_ms: number;
  captured_performance_ms?: number;
  filename?: string;
  status: "scheduled" | "captured" | "unavailable";
}

export function useDemoRecording(campaignId: string, events: DemoEvent[], pollingUncertaintyMs: number) {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const previewRef = useRef<HTMLVideoElement | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const chunkWritesRef = useRef<Promise<void>[]>([]);
  const syncRef = useRef<DemoSyncEvent[]>([]);
  const stillsRef = useRef<StillRecord[]>([]);
  const observedSequencesRef = useRef(new Set<number>());
  const photoSequencesRef = useRef(new Set<number>());
  const timersRef = useRef<number[]>([]);
  const startedPerformanceRef = useRef(0);
  const startedWallRef = useRef("");
  const recordingIdRef = useRef("");
  const campaignIdRef = useRef(campaignId);

  useEffect(() => { campaignIdRef.current = campaignId; }, [campaignId]);

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
    if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setError("Camera access requires a secure browser origin. Open CubOS through an SSH tunnel at http://localhost:8742; direct Pi HTTP addresses cannot access the Mac camera.");
      return;
    }
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
    }
  }, [deviceId, refreshDevices]);

  const startRecording = useCallback(() => {
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
    chunksRef.current = [];
    chunkWritesRef.current = [];
    syncRef.current = [];
    stillsRef.current = [];
    observedSequencesRef.current = new Set(events.map((event) => event.sequence));
    photoSequencesRef.current = new Set();
    recorder.ondataavailable = (event) => {
      if (!event.data.size) return;
      const index = chunksRef.current.push(event.data) - 1;
      chunkWritesRef.current.push(persistRecordingChunk(recordingIdRef.current, index, event.data));
    };
    recorder.onerror = () => {
      setError("Recording stopped because the browser reported a recorder error.");
      setRecording(false);
    };
    recorder.onstop = async () => {
      const associatedCampaignId = campaignIdRef.current;
      const base = `cubos-${associatedCampaignId || "unassociated-campaign"}-${startedWallRef.current.replaceAll(":", "-")}`;
      const type = recorder.mimeType || mimeType || "video/webm";
      await Promise.allSettled(chunkWritesRef.current);
      const persisted = await loadRecordingChunks(recordingIdRef.current);
      downloadBlob(new Blob(persisted.length ? persisted : chunksRef.current, { type }), `${base}.${type.includes("mp4") ? "mp4" : "webm"}`);
      downloadBlob(new Blob([JSON.stringify({
        schema_version: "1",
        campaign_id: associatedCampaignId || null,
        recording_id: recordingIdRef.current,
        recording_started_wall_time: startedWallRef.current,
        recording_started_performance_ms: startedPerformanceRef.current,
        mime_type: type,
        audio: false,
        clock_note: "video_elapsed_ms uses performance.now(); polling_uncertainty_ms is half the presentation request duration",
        events: syncRef.current,
        stills: stillsRef.current,
      }, null, 2)], { type: "application/json" }), `${base}.sync.json`);
      setRecording(false);
    };
    recorderRef.current = recorder;
    recorder.start(2000);
    setRecording(true);
    setError(null);
  }, [events, stream]);

  const stopRecording = useCallback(() => {
    if (recorderRef.current?.state !== "inactive") recorderRef.current?.stop();
  }, []);

  useEffect(() => {
    if (!recording) return;
    const newlyObserved = events.filter((event) => !observedSequencesRef.current.has(event.sequence));
    if (!newlyObserved.length) return;
    const observed = performance.now();
    newlyObserved.forEach((event) => {
      observedSequencesRef.current.add(event.sequence);
      syncRef.current.push(syncEvent(event, startedPerformanceRef.current, observed, pollingUncertaintyMs));
      if (!isPhotoPauseEvent(event) || photoSequencesRef.current.has(event.sequence)) return;
      photoSequencesRef.current.add(event.sequence);
      const still: StillRecord = { event_sequence: event.sequence, requested_performance_ms: observed, status: "scheduled" };
      stillsRef.current.push(still);
      const serverMs = typeof event.server_time === "number"
        ? (event.server_time < 1e12 ? event.server_time * 1000 : event.server_time)
        : Date.parse(event.server_time);
      const eventAgeMs = Number.isFinite(serverMs) ? Math.max(0, Date.now() - serverMs) : Infinity;
      if (eventAgeMs > Math.max(2500, pollingUncertaintyMs * 2 + 500)) {
        still.status = "unavailable";
        return;
      }
      const filename = `cubos-${campaignId || "campaign"}-event-${event.sequence}.jpg`;
      const captured = previewRef.current ? captureVideoStill(previewRef.current, filename) : false;
      still.captured_performance_ms = performance.now();
      still.filename = captured ? filename : undefined;
      still.status = captured ? "captured" : "unavailable";
    });
  }, [campaignId, events, pollingUncertaintyMs, recording]);

  useEffect(() => () => {
    timersRef.current.forEach(window.clearTimeout);
    if (recorderRef.current?.state !== "inactive") recorderRef.current?.stop();
    stopMediaStream(stream);
  }, [stream]);

  return {
    devices, deviceId, setDeviceId, stream, recording, error,
    setPreviewElement, enableCamera, startRecording, stopRecording,
  };
}
