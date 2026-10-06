import { useCallback, useState } from "react";
import { instrumentsApi } from "../../api/client";
import type { CameraAlignmentProposal, DeckResponse, GantryPosition, GantryResponse } from "../../types";
import * as theme from "../../theme";
import CameraAlignmentPreview from "./CameraAlignmentPreview";
import "./CameraAlignmentPanel.css";

interface Props {
  gantryFile: string;
  deckFile: string;
  gantry: GantryResponse;
  deck: DeckResponse;
  position: GantryPosition | null;
  disabledReason?: string | null;
  onSaved: () => void;
}

function referenceTargets(deck: DeckResponse) {
  return deck.labware.flatMap((item) => {
    const positions = item.wells ?? item.positions ?? {};
    return Object.entries(positions).map(([location, coordinates]) => ({
      key: `${item.key}.${location}`,
      coordinates,
    }));
  });
}

// TODO(iter): test camera-only alignment proposal, stale-proposal rejection, and save acknowledgement.
export default function CameraAlignmentPanel({
  gantryFile,
  deckFile,
  gantry,
  deck,
  position,
  disabledReason,
  onSaved,
}: Props) {
  const cameras = Object.entries(gantry.config.instruments)
    .filter(([, config]) => config.type === "camera")
    .map(([name]) => name);
  const targets = referenceTargets(deck);
  const [camera, setCamera] = useState(cameras[0] ?? "");
  const [target, setTarget] = useState(targets[0]?.key ?? "");
  const [previewOpen, setPreviewOpen] = useState(false);
  const [proposal, setProposal] = useState<CameraAlignmentProposal | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [previewStatus, setPreviewStatus] = useState<{ ready: boolean; frameAgeSeconds: number | null; error: string | null }>({ ready: false, frameAgeSeconds: null, error: null });

  const handlePreviewStatus = useCallback((next: { ready: boolean; frameAgeSeconds: number | null; error: string | null }) => {
    setPreviewStatus(next);
    if (!next.ready) setConfirmed(false);
  }, []);

  const clearProposal = () => {
    setProposal(null);
    setConfirmed(false);
    setError(null);
    setSaved(null);
  };

  const propose = async () => {
    if (!camera || !target || disabledReason || !previewStatus.ready) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      setProposal(await instrumentsApi.previewCameraAlignment({
        gantry_file: gantryFile,
        deck_file: deckFile,
        camera_instrument: camera,
        target_position: target,
      }));
      setConfirmed(false);
    } catch (caught) {
      setProposal(null);
      setConfirmed(false);
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!proposal || !confirmed || !previewStatus.ready) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      await instrumentsApi.saveCameraAlignment(proposal);
      setSaved(`Saved ${camera} X/Y offsets in ${gantryFile}.`);
      setConfirmed(false);
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const currentHead = position?.connected && position.work_x !== null && position.work_y !== null
    ? `${position.work_x.toFixed(3)}, ${position.work_y.toFixed(3)}, ${position.work_z?.toFixed(3) ?? "—"}`
    : "Unavailable — connect the selected gantry";

  return (
    <section className="camera-alignment-panel" aria-label="Camera XY alignment">
      <div className="camera-alignment-header">
        <div>
          <h3>Camera XY alignment</h3>
          <p>Manually jog until the selected well is centered in the preview, then calculate and explicitly save camera X/Y offsets.</p>
        </div>
        <button type="button" style={theme.btn.secondary} onClick={() => {
          setPreviewOpen((open) => !open);
          setPreviewStatus({ ready: false, frameAgeSeconds: null, error: null });
          setConfirmed(false);
        }} disabled={!camera}>
          {previewOpen ? "Stop preview" : "Start live preview"}
        </button>
      </div>

      <div className="camera-alignment-fields">
        <label>Camera<select aria-label="Alignment camera" value={camera} onChange={(event) => { setCamera(event.target.value); clearProposal(); }}>{cameras.map((name) => <option key={name}>{name}</option>)}</select></label>
        <label>Calibrated reference<select aria-label="Alignment reference" value={target} onChange={(event) => { setTarget(event.target.value); clearProposal(); }}>{targets.map((item) => <option key={item.key}>{item.key}</option>)}</select></label>
        <div><span>Current head WPos X, Y, Z</span><code>{currentHead}</code></div>
      </div>

      {previewOpen && camera && <CameraAlignmentPreview instrument={camera} onAlignmentStatusChange={handlePreviewStatus} />}
      {previewOpen && <div className={previewStatus.ready ? "camera-alignment-success" : "camera-alignment-warning"}>
        {previewStatus.ready
          ? `Live frame is fresh (${previewStatus.frameAgeSeconds?.toFixed(1)} s old).`
          : previewStatus.error ? `Preview unavailable: ${previewStatus.error}` : "Wait for a fresh live frame before calculating offsets."}
      </div>}
      {disabledReason && <div className="camera-alignment-warning">{disabledReason}</div>}
      {!cameras.length && <div className="camera-alignment-warning">The selected gantry has no camera instrument.</div>}
      {!targets.length && <div className="camera-alignment-warning">The selected deck has no calibrated reference positions.</div>}

      <div className="camera-alignment-actions">
        <button type="button" style={theme.btn.primary} onClick={() => void propose()} disabled={busy || !!disabledReason || !camera || !target || !previewStatus.ready}>
          {busy && !proposal ? "Reading current position…" : "Calculate X/Y offsets from current head position"}
        </button>
        <span>No motion, homing, capture command, Z, depth, pipette, or bounds change is performed.</span>
      </div>

      {proposal && (
        <div className="camera-alignment-proposal">
          <div><strong>Reference</strong><code>{proposal.target_position}: X {proposal.target.x.toFixed(3)}, Y {proposal.target.y.toFixed(3)}, Z {proposal.target.z.toFixed(3)}</code></div>
          <div><strong>Recorded head WPos</strong><code>X {proposal.head.work_x.toFixed(3)}, Y {proposal.head.work_y.toFixed(3)}, Z {proposal.head.work_z.toFixed(3)} · {proposal.head.status}</code></div>
          <div><strong>Reviewed camera frame</strong><code>#{proposal.camera_frame_id} · {proposal.camera_frame_age_seconds.toFixed(1)} s old when calculated</code></div>
          <div><strong>Before</strong><code>offset_x {proposal.before.offset_x.toFixed(3)} · offset_y {proposal.before.offset_y.toFixed(3)}</code></div>
          <div><strong>Proposed</strong><code>offset_x {proposal.after.offset_x.toFixed(3)} · offset_y {proposal.after.offset_y.toFixed(3)}</code></div>
          <div className="camera-alignment-formula">X: {proposal.target.x.toFixed(3)} − {proposal.head.work_x.toFixed(3)} = {proposal.after.offset_x.toFixed(3)} · Y: {proposal.target.y.toFixed(3)} − {proposal.head.work_y.toFixed(3)} = {proposal.after.offset_y.toFixed(3)}</div>
          {proposal.calibration_warning && <div className="camera-alignment-warning">{proposal.calibration_warning}</div>}
          <label className="camera-alignment-confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> I confirm the camera is centered on {proposal.target_position} in the live preview.</label>
          <div className="camera-alignment-actions">
            <button type="button" style={theme.btn.primary} onClick={() => void save()} disabled={busy || !confirmed || !previewStatus.ready}>{busy ? "Saving…" : "Save camera X/Y offsets"}</button>
            <span>Proposal expires {new Date(proposal.expires_at * 1000).toLocaleTimeString()} and is rejected if the setup or head position changes.</span>
          </div>
          <div className="camera-alignment-warning">Saved alignment applies to new captures and new campaigns. Existing campaigns retain their immutable gantry snapshot; create a fresh campaign to use the new offsets.</div>
        </div>
      )}

      {error && <div className="camera-alignment-error" role="alert">{error}</div>}
      {saved && <div className="camera-alignment-success" role="status">{saved}</div>}
    </section>
  );
}
