import { useEffect, useState } from "react";
import { cameraMonitorApi } from "./cameraMonitorApi";
import type { CameraMonitorStatus } from "./cameraMonitorTypes";

const POLL_MS = 800;
const STALE_AFTER_SECONDS = 3;

interface AlignmentPreviewStatus {
  ready: boolean;
  frameAgeSeconds: number | null;
  error: string | null;
}

export default function CameraAlignmentPreview({
  instrument,
  onAlignmentStatusChange,
}: {
  instrument: string;
  onAlignmentStatusChange?: (status: AlignmentPreviewStatus) => void;
}) {
  const [status, setStatus] = useState<CameraMonitorStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
    let acquireTimer: ReturnType<typeof setTimeout> | null = null;
    let leaseId: string | null = null;
    let acquiring = false;

    const publishAlignmentStatus = (next: CameraMonitorStatus | null, nextError: string | null) => {
      if (!onAlignmentStatusChange) return;
      const age = next?.frame_age_seconds ?? null;
      onAlignmentStatusChange({
        ready: next?.state === "running" && next.frame_id !== null && age !== null && age <= STALE_AFTER_SECONDS && !nextError,
        frameAgeSeconds: age,
        error: nextError,
      });
    };

    const poll = async () => {
      try {
        const next = await cameraMonitorApi.get(instrument);
        if (cancelled) return;
        setStatus(next);
        setError(next.error);
        publishAlignmentStatus(next, next.error);
      } catch (caught) {
        if (!cancelled) {
          const message = caught instanceof Error ? caught.message : String(caught);
          setError(message);
          publishAlignmentStatus(null, message);
        }
      }
      if (!cancelled) pollTimer = setTimeout(() => void poll(), POLL_MS);
    };

    const heartbeat = async () => {
      if (!leaseId || cancelled) return;
      try {
        const next = await cameraMonitorApi.heartbeat(instrument, leaseId);
        if (!cancelled) {
          setStatus(next);
          setError(next.error);
          publishAlignmentStatus(next, next.error);
        }
      } catch (caught) {
        if (!cancelled) {
          setError(`Camera monitor heartbeat: ${caught instanceof Error ? caught.message : String(caught)}. Reacquiring preview lease…`);
          publishAlignmentStatus(null, caught instanceof Error ? caught.message : String(caught));
          leaseId = null;
          void acquire();
        }
        return;
      }
      if (!cancelled) heartbeatTimer = setTimeout(() => void heartbeat(), 5000);
    };

    async function acquire() {
      if (cancelled || acquiring || leaseId) return;
      acquiring = true;
      try {
        const next = await cameraMonitorApi.start(instrument);
        if (cancelled) {
          if (next.lease_id) void cameraMonitorApi.stop(instrument, next.lease_id).catch(() => undefined);
          return;
        }
        if (!next.lease_id) throw new Error("Camera monitor did not return a lease.");
        leaseId = next.lease_id;
        setStatus(next);
        setError(next.error);
        publishAlignmentStatus(next, next.error);
        heartbeatTimer = setTimeout(() => void heartbeat(), 5000);
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : String(caught));
          publishAlignmentStatus(null, caught instanceof Error ? caught.message : String(caught));
          acquireTimer = setTimeout(() => void acquire(), 1000);
        }
      } finally {
        acquiring = false;
      }
    }

    void acquire();
    void poll();

    return () => {
      cancelled = true;
      if (pollTimer) clearTimeout(pollTimer);
      if (heartbeatTimer) clearTimeout(heartbeatTimer);
      if (acquireTimer) clearTimeout(acquireTimer);
      if (leaseId) void cameraMonitorApi.stop(instrument, leaseId).catch(() => undefined);
      publishAlignmentStatus(null, null);
    };
  }, [instrument, onAlignmentStatusChange]);

  const frameAge = status?.frame_age_seconds ?? null;
  const stale = frameAge !== null && frameAge > STALE_AFTER_SECONDS;
  const resolution = status?.actual_resolution;
  return (
      <section className="camera-camera camera-camera-alignment" aria-label="Live camera alignment preview">
        <div className="camera-toolbar">
          <h4>Live camera</h4>
          <span className={stale ? "camera-camera-stale" : "camera-note"}>
            {frameAge === null ? "Waiting for first frame" : `${frameAge.toFixed(1)} s old${stale ? " · stale" : ""}`}
          </span>
        </div>
        {status?.frame_id !== null && status?.frame_id !== undefined ? (
          <div className="camera-camera-frame" style={resolution ? { aspectRatio: `${resolution.width} / ${resolution.height}` } : undefined}>
            <img src={cameraMonitorApi.frameUrl(instrument, status.frame_id)} alt={`Live alignment preview from ${instrument}`} />
            <svg viewBox={`0 0 ${resolution?.width ?? 100} ${resolution?.height ?? 100}`} preserveAspectRatio="none" aria-hidden="true">
              <line x1={(resolution?.width ?? 100) / 2} y1="0" x2={(resolution?.width ?? 100) / 2} y2={resolution?.height ?? 100} className="camera-optical-center" />
              <line x1="0" y1={(resolution?.height ?? 100) / 2} x2={resolution?.width ?? 100} y2={(resolution?.height ?? 100) / 2} className="camera-optical-center" />
            </svg>
          </div>
        ) : <div className="camera-note">Waiting for a frame from the shared camera monitor.</div>}
        <div className="camera-note">Center the selected well on the red optical crosshair by manually jogging in Gantry Control. This preview never moves the gantry.</div>
        {error && <div className="camera-banner camera-error" role="alert">Camera monitor: {error}</div>}
      </section>
  );
}
