import React, { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { demoApi } from "./api";
import { sha256File } from "./hashFile";
import { eventFootageTimeMs, eventIndexForFootage, friendlyEventLabel, visiblePresentation } from "./replay";
import { useDemoRecording } from "./useDemoRecording";
import { campaignToAssociate } from "./recording";
import type { DemoAttempt, DemoColorMeasurement } from "./types";
import "./DemoPresentation.css";

function formatDelta(value?: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "—";
}

function colorStyle(measurement?: DemoColorMeasurement | null): React.CSSProperties {
  const rgb = measurement?.rgb;
  return rgb ? { background: `rgb(${rgb.join(",")})` } : {};
}

function AssetImage({ campaignId, assetId, alt }: { campaignId: string | number; assetId?: string | null; alt: string }) {
  if (!assetId) return <div className="demo-image-missing">Image unavailable</div>;
  return <img src={demoApi.assetUrl(campaignId, assetId)} alt={alt} />;
}

function WellImage({
  campaignId,
  rawAssetId,
  fallbackAssetId,
  measurement,
  alt,
}: {
  campaignId: string | number;
  rawAssetId?: string | null;
  fallbackAssetId?: string | null;
  measurement?: DemoColorMeasurement | null;
  alt: string;
}) {
  const roi = measurement?.roi;
  const frame = measurement?.frame;
  if (
    rawAssetId && roi && frame
    && frame.width_px > 0 && frame.height_px > 0
    && roi.radius_px > 0
  ) {
    const cropRadius = roi.radius_px * 1.35;
    return (
      <svg className="demo-well-crop" viewBox={`${roi.center_x_px - cropRadius} ${roi.center_y_px - cropRadius} ${cropRadius * 2} ${cropRadius * 2}`}
        preserveAspectRatio="xMidYMid slice" role="img" aria-label={alt}>
        <image href={demoApi.assetUrl(campaignId, rawAssetId)} width={frame.width_px} height={frame.height_px} />
      </svg>
    );
  }
  return (
    <div className="demo-full-frame">
      <AssetImage campaignId={campaignId} assetId={rawAssetId ?? fallbackAssetId} alt={alt} />
      {(rawAssetId || fallbackAssetId) && <span>Full frame · crop unavailable</span>}
    </div>
  );
}

function ResultTile({
  label,
  campaignId,
  attempt,
  target,
}: {
  label: string;
  campaignId: string | number;
  attempt?: DemoAttempt | null;
  target?: { source: string; accepted: boolean; image_asset_id?: string | null; raw_image_asset_id?: string | null; measurement?: DemoColorMeasurement | null; well?: string | null };
}) {
  const measurement = attempt?.measurement ?? (target?.accepted ? target.measurement : null);
  const assetId = attempt?.image_asset_id ?? (target?.accepted ? target.image_asset_id : null);
  const rawAssetId = attempt?.raw_image_asset_id ?? (target?.accepted ? target.raw_image_asset_id : null);
  const well = attempt?.well ?? target?.well;
  return (
    <section className="demo-result-tile">
      <header>
        <span>{label}</span>
        {well && <strong>{well}</strong>}
      </header>
      <div className="demo-result-image">
        {!assetId && label === "Target" && measurement?.rgb ? (
          <div className="demo-selected-target" style={colorStyle(measurement)}><span>{target?.source === "selected_srgb" ? "Selected sRGB" : "Selected target"}</span></div>
        ) : (
          <WellImage campaignId={campaignId} rawAssetId={rawAssetId} fallbackAssetId={assetId} measurement={measurement} alt={`${label}${well ? ` ${well}` : ""}`} />
        )}
      </div>
      <footer className={label === "Target" ? "is-target" : ""}>
        <span className="demo-swatch" style={colorStyle(measurement)} aria-hidden="true" />
        {label === "Target" ? <span className="demo-reference-label">Reference color</span> : <div>
          <small>ΔE00</small>
          <strong>{formatDelta(measurement?.delta_e)}</strong>
        </div>}
      </footer>
    </section>
  );
}

function recipeText(recipe: Record<string, number>): string {
  const entries = Object.entries(recipe);
  return entries.length ? entries.map(([name, volume]) => `${name.replace(/_ul$/, "")} ${volume} µL`).join(" · ") : "Recipe unavailable";
}

function AttemptStrip({ campaignId, attempts, bestId }: { campaignId: string | number; attempts: DemoAttempt[]; bestId?: string }) {
  return (
    <ol className="demo-attempt-strip" aria-label="Visible campaign attempts">
      {attempts.length === 0 && <li className="demo-attempt-empty">Waiting for the first measured well</li>}
      {attempts.map((attempt, index) => (
        <li key={attempt.trial_id} className={attempt.trial_id === bestId ? "is-best" : ""}>
          <div className="demo-attempt-number">{String(index + 1).padStart(2, "0")}</div>
          <div className="demo-attempt-photo">
            <WellImage campaignId={campaignId} rawAssetId={attempt.raw_image_asset_id} fallbackAssetId={attempt.image_asset_id}
              measurement={attempt.measurement} alt={`Attempt ${index + 1}, ${attempt.well ?? "unknown well"}`} />
          </div>
          <div className="demo-attempt-copy">
            <strong>{attempt.well ?? "Pending"}</strong>
            <span>{attempt.accepted ? `ΔE ${formatDelta(attempt.measurement?.delta_e)}` : attempt.status}</span>
          </div>
          {attempt.trial_id === bestId && <span className="demo-best-flag">Best</span>}
        </li>
      ))}
    </ol>
  );
}

function BestTrace({ attempts }: { attempts: DemoAttempt[] }) {
  const points = useMemo(() => {
    return attempts.reduce<{ best: number; points: { index: number; value: number }[] }>((accumulator, attempt, index) => {
      const value = attempt.accepted && (attempt.status === "succeeded" || attempt.status === "completed")
        ? attempt.measurement?.delta_e
        : null;
      if (typeof value !== "number" || !Number.isFinite(value)) return accumulator;
      const nextBest = Math.min(accumulator.best, value);
      return { best: nextBest, points: [...accumulator.points, { index, value: nextBest }] };
    }, { best: Infinity, points: [] }).points;
  }, [attempts]);
  if (points.length < 2) return <div className="demo-trace-empty">Progress appears after two accepted measurements</div>;
  const max = Math.max(...points.map((point) => point.value), 1);
  const path = points.map((point, index) => {
    const x = (point.index / Math.max(attempts.length - 1, 1)) * 100;
    const y = 14 + (1 - point.value / max) * 78;
    return `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return (
    <svg className="demo-trace" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Best color difference over visible attempts">
      <path className="demo-trace-grid" d="M0,92 L100,92" />
      <path className="demo-trace-line" d={path} />
    </svg>
  );
}

export default function DemoPresentation() {
  const initialParams = useMemo(() => new URLSearchParams(window.location.search), []);
  const initialCampaign = initialParams.get("campaign") ?? "";
  const overlay = initialParams.get("overlay") === "1";
  const [campaignId, setCampaignId] = useState(initialCampaign);
  const [live, setLive] = useState(true);
  const [eventIndex, setEventIndex] = useState(0);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [videoName, setVideoName] = useState("");
  const [videoError, setVideoError] = useState<string | null>(null);
  const [recordingIdentity, setRecordingIdentity] = useState<{ name: string; size: number; lastModified: number; sha256?: string } | null>(null);
  const [footageOffsetMs, setFootageOffsetMs] = useState(0);
  const [markerState, setMarkerState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [pollingUncertaintyMs, setPollingUncertaintyMs] = useState(500);
  const [armedForCampaign, setArmedForCampaign] = useState(false);
  const [serverNowEstimateMs, setServerNowEstimateMs] = useState<number | null>(null);
  const [serverClockObservedPerformanceMs, setServerClockObservedPerformanceMs] = useState<number | null>(null);
  const campaignBaselineRef = useRef(new Map<string, string | undefined>());
  const videoRef = useRef<HTMLVideoElement>(null);
  const markerClientIdRef = useRef<string | null>(null);
  const lastPresentationResponseRef = useRef<number | null>(null);

  const campaigns = useQuery({
    queryKey: ["demo-campaigns"],
    queryFn: demoApi.listCampaigns,
    refetchInterval: armedForCampaign ? 1000 : false,
  });
  const presentation = useQuery({
    queryKey: ["campaign-presentation", campaignId],
    queryFn: async () => {
      const started = performance.now();
      const result = await demoApi.presentation(campaignId);
      const completed = performance.now();
      const observationWindow = lastPresentationResponseRef.current == null
        ? completed - started
        : completed - lastPresentationResponseRef.current;
      lastPresentationResponseRef.current = completed;
      setPollingUncertaintyMs(Math.max(1, observationWindow));
      setServerNowEstimateMs(typeof result.server_now_epoch_ms === "number"
        ? result.server_now_epoch_ms + (completed - started)
        : null);
      setServerClockObservedPerformanceMs(completed);
      return result;
    },
    enabled: campaignId.length > 0,
    refetchInterval: (query) => live && (!query.state.data || query.state.data.status === "running") ? 1000 : false,
  });
  const {
    devices: cameraDevices,
    deviceId: cameraDeviceId,
    captureResolution,
    setDeviceId: setCameraDeviceId,
    stream: cameraStream,
    enabling: cameraEnabling,
    recording: cameraRecording,
    finalizing: cameraFinalizing,
    recoverable: recoverableRecordings,
    error: cameraError,
    setPreviewElement,
    enableCamera,
    startRecording,
    stopRecording,
    downloadRecoverable,
    downloadRecoverableStill,
    discardRecoverable,
  } = useDemoRecording(campaignId, presentation.data?.events ?? [], pollingUncertaintyMs,
    serverNowEstimateMs, serverClockObservedPerformanceMs);

  useEffect(() => {
    if (!armedForCampaign || !campaigns.data) return;
    const candidate = campaignToAssociate(campaigns.data, campaignBaselineRef.current);
    if (!candidate) return;
    const value = String(candidate.campaign_id);
    setCampaignId(value);
    setLive(true);
    setArmedForCampaign(false);
    const params = new URLSearchParams(window.location.search);
    params.set("view", "demo");
    params.set("campaign", value);
    window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
  }, [armedForCampaign, campaigns.data]);

  useEffect(() => {
    if (!overlay) return;
    document.documentElement.classList.add("demo-overlay-active");
    document.body.classList.add("demo-overlay-active");
    document.getElementById("root")?.classList.add("demo-overlay-active");
    return () => {
      document.documentElement.classList.remove("demo-overlay-active");
      document.body.classList.remove("demo-overlay-active");
      document.getElementById("root")?.classList.remove("demo-overlay-active");
    };
  }, [overlay]);

  useEffect(() => () => { if (videoUrl) URL.revokeObjectURL(videoUrl); }, [videoUrl]);
  const replay = presentation.data
    ? visiblePresentation(presentation.data, live ? Math.max(presentation.data.events.length - 1, 0) : eventIndex)
    : null;
  const currentStatus = replay?.event
    ? friendlyEventLabel(replay.event.label, presentation.data?.status ?? "")
    : (presentation.data ? presentation.data.status : "Select a campaign");
  const activeAttempt = presentation.data?.attempts.find((attempt) => attempt.trial_id === replay?.event?.trial_id) ?? null;
  const latestRecordedAttempt = replay?.attempts.filter((attempt) => attempt.image_asset_id).at(-1) ?? null;
  const activeMarkers = useMemo(() => {
    if (!recordingIdentity?.sha256) return [];
    return (presentation.data?.markers ?? []).filter((marker) =>
      marker.recording_sha256 === recordingIdentity.sha256
      && marker.recording_name === recordingIdentity.name
      && marker.recording_size === recordingIdentity.size
      && marker.recording_last_modified === recordingIdentity.lastModified
    );
  }, [presentation.data?.markers, recordingIdentity]);

  useEffect(() => {
    if (live || !replay?.event || !videoRef.current) return;
    videoRef.current.currentTime = Math.max(0, eventFootageTimeMs(replay.event, activeMarkers, footageOffsetMs) / 1000);
  }, [activeMarkers, footageOffsetMs, live, replay?.event]);

  const selectCampaign = (value: string) => {
    setCampaignId(value);
    setLive(true);
    const params = new URLSearchParams(window.location.search);
    params.set("view", "demo");
    params.set("campaign", value);
    window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
  };

  const chooseVideo = async (file?: File) => {
    if (!file) return;
    if (file.size > 512 * 1024 * 1024) {
      setVideoError("Choose a recording smaller than 512 MB so the browser can fingerprint it safely.");
      return;
    }
    setVideoError(null);
    markerClientIdRef.current = null;
    setVideoUrl(URL.createObjectURL(file));
    setVideoName(file.name);
    setFootageOffsetMs(0);
    setMarkerState("idle");
    const identity = { name: file.name, size: file.size, lastModified: file.lastModified };
    setRecordingIdentity(identity);
    try {
      const sha256 = await sha256File(file);
      setRecordingIdentity({ ...identity, sha256 });
    } catch {
      setRecordingIdentity(identity);
      setMarkerState("error");
      setVideoError("This browser could not fingerprint the recording. Try a shorter MP4 or WebM file.");
    }
  };

  const saveMarker = async () => {
    if (!campaignId || !replay?.event || !recordingIdentity?.sha256 || !videoRef.current) return;
    setMarkerState("saving");
    const clientId = markerClientIdRef.current ?? globalThis.crypto?.randomUUID?.()
      ?? `marker-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    markerClientIdRef.current = clientId;
    try {
      await demoApi.addMarker(campaignId, {
        client_id: clientId,
        label: replay?.event?.label ?? "Broadcast sync",
        client_time: Date.now() / 1000,
        timeline_elapsed_ms: replay.event.elapsed_ms,
        video_time_ms: Math.round(videoRef.current.currentTime * 1000),
        recording_name: recordingIdentity?.name,
        recording_size: recordingIdentity?.size,
        recording_last_modified: recordingIdentity?.lastModified,
        recording_sha256: recordingIdentity?.sha256,
      });
      setMarkerState("saved");
      markerClientIdRef.current = null;
      void presentation.refetch();
    } catch {
      setMarkerState("error");
    }
  };

  return (
    <main className={`demo-shell${overlay ? " demo-shell--overlay" : ""}`}>
      {!overlay && (
        <header className="demo-producer-bar">
          <a className="demo-brand" href="/" aria-label="Back to CubOS Operator"><span aria-hidden="true">C</span><strong>CubOS Color Lab</strong></a>
          <label>Campaign
            <select value={campaignId} disabled={cameraRecording || cameraFinalizing} onChange={(event) => selectCampaign(event.target.value)}>
              <option value="">Choose a campaign…</option>
              {(campaigns.data ?? []).map((campaign) => (
                <option key={campaign.campaign_id} value={campaign.campaign_id}>{campaign.spec?.name ?? `Campaign ${campaign.campaign_id}`}</option>
              ))}
            </select>
          </label>
          <label className="demo-file-button">Add camera recording<input type="file" accept="video/*" onChange={(event) => void chooseVideo(event.target.files?.[0])} /></label>
          {!cameraStream ? (
            <button type="button" disabled={cameraEnabling} onClick={() => void enableCamera()}>
              {cameraEnabling ? "Waiting for camera permission…" : "Enable Mac camera"}
            </button>
          ) : (
            <>
              <label>Camera
                <select value={cameraDeviceId} disabled={cameraRecording || cameraFinalizing} onChange={(event) => {
                  setCameraDeviceId(event.target.value);
                  void enableCamera(event.target.value);
                }}>
                  {cameraDevices.map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || `Camera ${index + 1}`}</option>)}
                </select>
              </label>
              <button type="button" disabled={cameraFinalizing} className={cameraRecording ? "demo-recording-stop" : ""}
                onClick={cameraRecording ? () => { setArmedForCampaign(false); stopRecording(); } : () => {
                  if (!campaignId) {
                    campaignBaselineRef.current = new Map((campaigns.data ?? []).map((campaign) => [String(campaign.campaign_id), campaign.state]));
                    setArmedForCampaign(true);
                  }
                  startRecording();
                }}>
                {cameraFinalizing ? "Preparing files…" : cameraRecording ? "Stop & download" : campaignId ? "Start recording" : "Record next campaign"}
              </button>
            </>
          )}
          {cameraError && <span className="demo-video-error" role="alert">{cameraError}</span>}
          {cameraRecording && armedForCampaign && <span className="demo-arm-status" role="status">Recording · waiting for the next campaign</span>}
          {recoverableRecordings.length > 0 && (
            <details className="demo-recoveries">
              <summary>Saved recordings ({recoverableRecordings.length})</summary>
              {recoverableRecordings.map((item) => (
                <div key={item.recordingId}>
                  <span>{item.status === "ready" ? "Saved" : "Interrupted"} · {item.campaignId ?? "Unassociated"} · {new Date(item.startedWallTime).toLocaleString()}</span>
                  <button type="button" onClick={() => void downloadRecoverable(item)}>Download</button>
                  <button type="button" onClick={() => void discardRecoverable(item.recordingId)}>Discard</button>
                  {(item.stills ?? []).map((still) => (
                    <button key={still.eventSequence} type="button" onClick={() => void downloadRecoverableStill(item, still.eventSequence, still.filename)}>
                      Download still {still.eventSequence}
                    </button>
                  ))}
                </div>
              ))}
            </details>
          )}
          {videoError && <span className="demo-video-error" role="alert">{videoError}</span>}
          <label>Video offset
            <input type="number" value={footageOffsetMs} step={100} onChange={(event) => setFootageOffsetMs(Number(event.target.value) || 0)} />
            <span>ms</span>
          </label>
          <button type="button" onClick={saveMarker} disabled={!campaignId || !videoUrl || !recordingIdentity?.sha256 || markerState === "saving"}>{markerState === "saving" ? "Saving…" : !videoUrl ? "Load video to sync" : !recordingIdentity?.sha256 ? "Preparing video…" : "Add sync marker"}</button>
          {campaignId && <a className="demo-export" href={demoApi.exportUrl(campaignId)}>Export data</a>}
        </header>
      )}

      {presentation.isLoading && !cameraStream && <section className="demo-message"><strong>Loading campaign presentation</strong><span>Collecting the timeline and measured well images.</span></section>}
      {presentation.isError && <section className="demo-message demo-message--error"><strong>Presentation unavailable</strong><span>{presentation.error instanceof Error ? presentation.error.message : String(presentation.error)}</span></section>}
      {!campaignId && !presentation.isLoading && !cameraStream && <section className="demo-message"><strong>Choose a campaign</strong><span>The presentation is read-only and cannot move the robot.</span></section>}
      {cameraStream && !presentation.data && (
        <section className="demo-camera-preflight" aria-label="Camera preview before campaign">
          <video className="demo-live-camera-preview" ref={setPreviewElement} autoPlay muted playsInline aria-label="Live Mac camera preview" />
          <div><strong>Camera ready</strong><span>{cameraRecording ? "Recording locally · waiting for the next campaign" : "Frame the shot, then record the next campaign."}{captureResolution ? ` · ${captureResolution}` : ""}</span></div>
        </section>
      )}

      {presentation.data && replay && (
        <>
          <section className="demo-stage">
            <div className="demo-hardware" aria-label="Hardware camera recording">
              {cameraStream ? <video className="demo-live-camera-preview" ref={setPreviewElement} autoPlay muted playsInline aria-label="Live Mac camera preview" />
                : videoUrl ? <video ref={videoRef} src={videoUrl} controls={!overlay} playsInline
                onPlay={() => setLive(false)}
                onTimeUpdate={(event) => {
                  if (!presentation.data || live) return;
                  setEventIndex(eventIndexForFootage(
                    [...presentation.data.events].sort((left, right) => left.sequence - right.sequence),
                    activeMarkers,
                    event.currentTarget.currentTime * 1000,
                    footageOffsetMs,
                  ));
                }} /> : latestRecordedAttempt ? (
                <div className="demo-recorded-well">
                  <AssetImage campaignId={presentation.data.campaign_id}
                    assetId={latestRecordedAttempt.raw_image_asset_id ?? latestRecordedAttempt.image_asset_id}
                    alt={`Recorded plate camera ${latestRecordedAttempt.well ?? ""}`} />
                  <span>Recorded plate camera · {latestRecordedAttempt.well ?? "measured sample"}</span>
                </div>
              ) : (
                <div className="demo-camera-placeholder">
                  <span>Camera recording</span>
                  <strong>Add a camera recording</strong>
                  <small>The campaign results remain synchronized and exportable without video.</small>
                </div>
              )}
              <div className="demo-live-badge"><span />{cameraRecording ? "Recording locally" : presentation.data.status === "running" ? (live ? "Live campaign" : "Replay") : "Recorded run"}</div>
              {videoName && <div className="demo-video-name">{videoName}</div>}
              {cameraStream && captureResolution && <div className="demo-video-name">{captureResolution}</div>}
              <div className="demo-now">
                <small>Now</small>
                <strong>{currentStatus}</strong>
                {activeAttempt && <span>{recipeText(activeAttempt.recipe_ul)}</span>}
              </div>
            </div>
            <aside className="demo-scoreboard">
              <div className="demo-scoreboard-heading">
                <div title={String(presentation.data.campaign_id)}><small>Campaign</small><strong>{presentation.data.campaign_name || "Color matching run"}</strong></div>
                <span className={`demo-status demo-status--${presentation.data.status}`}>{presentation.data.status.replaceAll("_", " ")}</span>
              </div>
              <div className="demo-result-grid">
                <ResultTile label="Target" campaignId={presentation.data.campaign_id} target={presentation.data.target} />
                <ResultTile label="Best so far" campaignId={presentation.data.campaign_id} attempt={replay.best} />
              </div>
              <p className="demo-measurement-note">Well colors are camera-estimated measurements.</p>
              <div className="demo-best-number"><small>Best color difference</small><strong>{formatDelta(replay.visibleBest?.delta_e)}</strong><span>ΔE00</span></div>
              <section className="demo-trace-wrap"><header><span>Convergence</span><strong>{replay.attempts.length} attempts</strong></header><BestTrace attempts={replay.attempts} /></section>
            </aside>
          </section>

          <section className="demo-history">
            <header>
              <div><small>Experiment history</small><strong>Every attempt, in order</strong></div>
              {!overlay && (
                <div className="demo-replay-controls">
                  <button type="button" className={live ? "is-active" : ""} onClick={() => setLive(true)}>{presentation.data.status === "running" ? "Live" : "Latest"}</button>
                  <input aria-label="Replay timeline" type="range" min={0} max={Math.max(replay.events.length - 1, 0)} value={replay.safeIndex}
                    onChange={(event) => { setLive(false); setEventIndex(Number(event.target.value)); }} />
                  <time>{replay.event ? `${Math.round(replay.event.elapsed_ms / 1000)}s` : "0s"}</time>
                </div>
              )}
            </header>
            <AttemptStrip campaignId={presentation.data.campaign_id} attempts={replay.attempts} bestId={replay.best?.trial_id} />
          </section>

          {(presentation.data.partial || presentation.data.missing.length > 0) && (
            <div className="demo-data-note" role="status">Partial presentation data{presentation.data.missing.length ? ` · Missing: ${presentation.data.missing.join(", ")}` : ""}</div>
          )}
        </>
      )}
    </main>
  );
}
