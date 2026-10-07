import { useEffect, useRef, useState } from "react";
import { gantryApi } from "../../api/client";
import type { GantryConfig, GantryPosition, GantryResponse } from "../../types";
import * as theme from "../../theme";
import JogPanel, { MIN_JOG_STEP } from "./JogPanel";
import CameraPreview from "./CameraPreview";

type Capture = { x: number; y: number; z: number; tip_length_mm: number; stand_off_mm?: number };
interface Props {
  open: boolean;
  onClose: () => void;
  gantry: GantryResponse | null;
  position: GantryPosition | null;
  isRunning?: boolean;
  onSaveCalibrated: (filename: string, config: GantryConfig) => Promise<void>;
}

export default function InstrumentOffsetCalibrationModal(props: Props) {
  return props.open && props.gantry ? <OffsetCalibration {...props} gantry={props.gantry} /> : null;
}

function OffsetCalibration({ gantry, position, isRunning = false, onClose, onSaveCalibrated }: Props & { gantry: GantryResponse }) {
  const [snapshot] = useState(() => structuredClone(gantry));
  const config = snapshot.config;
  const names = Object.keys(config.instruments).filter(name => config.instruments[name].type !== "lighting");
  const contacts = names.filter(name => config.instruments[name].type !== "camera");
  const [reference, setReference] = useState(contacts[0] ?? "");
  const [confirmed, setConfirmed] = useState(false);
  const [active, setActive] = useState(reference);
  const [captures, setCaptures] = useState<Record<string, Capture>>({});
  const [tipAttached, setTipAttached] = useState(false);
  const [tipLength, setTipLength] = useState("");
  const [standOff, setStandOff] = useState("");
  const [xyStep, setXyStep] = useState("0.5");
  const [zStep, setZStep] = useState("0.5");
  const [output, setOutput] = useState(snapshot.filename.replace(/\.ya?ml$/i, "") + "_offsets.yaml");
  const [preview, setPreview] = useState<GantryConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const operationPending = useRef(false);
  const dialog = useRef<HTMLDivElement>(null);
  const stale = JSON.stringify(gantry) !== JSON.stringify(snapshot);
  const unavailable = !position?.connected || position.status !== "Idle" || position.calibration_active || isRunning || stale;
  const locked = busy || unavailable;
  const pipette = config.instruments[active]?.type === "pipette";
  const camera = config.instruments[active]?.type === "camera";
  const xy = Number(xyStep), z = Number(zStep);
  const xyInvalid = !xyStep.trim() || !Number.isFinite(xy) || xy < MIN_JOG_STEP;
  const zInvalid = !zStep.trim() || !Number.isFinite(z) || z < MIN_JOG_STEP;

  useEffect(() => {
    const priorFocus = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => priorFocus?.focus();
  }, []);

  const run = async (action: () => Promise<void>) => {
    if (operationPending.current || unavailable) return;
    operationPending.current = true;
    setBusy(true);
    setError(null);
    try { await action(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { operationPending.current = false; setBusy(false); }
  };

  const capture = () => run(async () => {
    if (!confirmed) throw new Error("Confirm that the reference instrument is already calibrated.");
    const tip = pipette && tipAttached ? Number(tipLength) : 0;
    if (pipette && tipAttached && (!tipLength.trim() || !Number.isFinite(tip) || tip <= 0)) {
      throw new Error("Enter the tip protrusion below the bare nozzle, greater than 0 mm.");
    }
    const distance = Number(standOff);
    if (camera && (!standOff.trim() || !Number.isFinite(distance) || distance < 0)) {
      throw new Error("Enter a camera stand-off of 0 mm or greater.");
    }
    const live = await gantryApi.getPosition();
    if (!live.connected || live.status !== "Idle" || live.calibration_active) {
      throw new Error("The gantry must be connected and idle with full calibration closed.");
    }
    const values = [live.work_x, live.work_y, live.work_z];
    if (values.some(value => value == null || !Number.isFinite(value))) {
      throw new Error("Work coordinates are unavailable. Establish the deck frame before offset calibration.");
    }
    setCaptures(previous => ({ ...previous, [active]: {
      x: live.work_x!, y: live.work_y!, z: live.work_z!, tip_length_mm: tip,
      ...(camera ? { stand_off_mm: distance } : {}),
    } }));
    setPreview(null);
  });

  const jog = (x: number, y: number, z: number) => run(async () => {
    if (xyInvalid || zInvalid) return;
    await gantryApi.jogBlocking(x, y, z);
  });

  const selectInstrument = (name: string) => {
    setActive(name);
    setTipAttached(false);
    setTipLength("");
    setStandOff("");
    setError(null);
  };

  return <div style={overlay}>
    <div ref={dialog} role="dialog" aria-modal="true" aria-label="Instrument offset calibration" tabIndex={-1} style={modal}
      onKeyDown={event => {
        if (event.key === "Escape" && !busy) onClose();
        if (event.key === "Tab") {
          const items = Array.from(dialog.current?.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), select:not([disabled])") ?? []);
          const first = items[0], last = items[items.length - 1];
          if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }
      }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={theme.panelTitle}>Calibrate instrument offsets</h2>
        <button style={theme.btn.secondary} disabled={busy} onClick={onClose}>Close</button>
      </div>
      <p style={instructions}>Use one fixed mark on a stable surface. Touch it with each contact instrument; center the camera on the same mark. Your current origin, travel limits, and reference mounting values are preserved.</p>
      <p style={instructions}>Start with an already calibrated contact instrument. If its mounting changed, run full gantry calibration first. Raise tools manually before moving sideways.</p>
      {contacts.length === 0 && <p role="alert" style={theme.notice.warning}>Add and fully calibrate a contact instrument first.</p>}
      {unavailable && <p role="alert" style={theme.notice.warning}>{stale ? "The selected configuration changed. Close and reopen this calibration." : "Connect the gantry and wait for Idle. Finish or restore any full calibration before continuing."}</p>}
      {error && <p role="alert" style={theme.notice.error}>{error}</p>}
      <label style={field}>Calibrated reference instrument
        <select style={theme.input} value={reference} disabled={busy} onChange={event => {
          setReference(event.target.value); selectInstrument(event.target.value); setCaptures({}); setPreview(null); setConfirmed(false);
        }}>{contacts.map(name => <option key={name}>{name}</option>)}</select>
      </label>
      <label style={{ display: "flex", gap: 8, margin: "12px 0" }}>
        <input type="checkbox" checked={confirmed} disabled={busy} onChange={event => { setConfirmed(event.target.checked); setPreview(null); }} />
        The reference instrument's existing offsets and depth are calibrated.
      </label>
      <label style={field}>Instrument to record
        <select style={theme.input} value={active} disabled={busy || !reference} onChange={event => selectInstrument(event.target.value)}>
          {[reference, ...names.filter(name => name !== reference)].filter(Boolean).map(name => <option key={name}>{name}</option>)}
        </select>
      </label>
      <div style={{ margin: "12px 0" }}><JogPanel xyStep={xyStep} zStep={zStep} setXyStep={setXyStep} setZStep={setZStep}
        disabled={locked} alarmed={false} onStartJog={jog} onStopJog={() => undefined} xy={xy} z={z}
        stepInvalid={xyInvalid || zInvalid} xyStepInvalid={xyInvalid} zStepInvalid={zInvalid} xyBelowMin={xy < MIN_JOG_STEP} zBelowMin={z < MIN_JOG_STEP} /></div>
      <button style={theme.btn.secondary} disabled={!position?.connected || isRunning} onClick={() => {
        void gantryApi.jogCancel().catch(err => setError(err instanceof Error ? err.message : String(err)));
      }}>Stop jog</button>
      <p style={{ ...theme.mono, fontSize: 12 }}>WPos: X {position?.work_x ?? "—"}, Y {position?.work_y ?? "—"}, Z {position?.work_z ?? "—"} mm. Each jog is one step.</p>
      {pipette && <><label style={{ display: "flex", gap: 8 }}><input type="checkbox" checked={tipAttached} disabled={busy} onChange={event => setTipAttached(event.target.checked)} />Tip attached for this capture</label>
        {tipAttached && <label style={field}>Tip protrusion below bare nozzle (mm)<input style={theme.input} value={tipLength} disabled={busy} onChange={event => setTipLength(event.target.value)} inputMode="decimal" /></label>}</>}
      {camera && <><label style={field}>Camera stand-off from fixed mark (mm)<input style={theme.input} value={standOff} disabled={busy} onChange={event => setStandOff(event.target.value)} inputMode="decimal" /></label><CameraPreview instrument={active} /></>}
      <button style={theme.btn.primary} disabled={locked || !confirmed || !active || (active !== reference && !captures[reference])} onClick={capture}>Record {active || "instrument"} at fixed mark</button>
      <div style={{ margin: "14px 0", fontSize: 12 }}>{names.map(name => <p key={name}>
        {name}: {captures[name] ? `recorded (${captures[name].x}, ${captures[name].y}, ${captures[name].z}) mm${captures[name].tip_length_mm ? `; tip ${captures[name].tip_length_mm} mm` : ""}${captures[name].stand_off_mm != null ? `; stand-off ${captures[name].stand_off_mm} mm` : ""}` : "not recorded"}
      </p>)}<p>Tip length and camera stand-off are saved with each capture. Re-record a position after changing either.</p></div>
      <button style={theme.btn.secondary} disabled={locked || !confirmed || names.length === 0 || names.some(name => !captures[name])} onClick={() => run(async () => {
        setPreview(await gantryApi.previewInstrumentOffsets({ config, reference_instrument: reference, captures }));
      })}>Review offsets</button>
      {preview && <><p style={instructions}>Saving creates a copy and keeps the current connection unchanged. Select the saved configuration and reconnect explicitly before using the new offsets.</p><table style={{ width: "100%", margin: "12px 0", fontSize: 12 }}><thead><tr><th>Instrument</th><th>X offset</th><th>Y offset</th><th>Depth (mm)</th></tr></thead><tbody>
        {Object.entries(preview.instruments).map(([name, entry]) => <tr key={name}><td>{name}{name === reference ? " (preserved)" : ""}</td><td>{entry.offset_x}</td><td>{entry.offset_y}</td><td>{entry.depth ?? 0}</td></tr>)}
      </tbody></table><label style={field}>Save configuration as<input style={theme.input} value={output} disabled={busy} onChange={event => setOutput(event.target.value)} /></label>
        {output.trim() === snapshot.filename && <p role="alert" style={theme.notice.warning}>Choose a new filename to preserve the connected configuration.</p>}
        <button style={theme.btn.primary} disabled={locked || !confirmed || !output.trim() || output.trim() === snapshot.filename} onClick={() => run(async () => {
          if (output.trim() === snapshot.filename) throw new Error("Save to a new filename to preserve the connected configuration.");
          await onSaveCalibrated(output.trim(), preview); onClose();
        })}>Save instrument offsets</button></>}
    </div>
  </div>;
}

const field: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 5, margin: "10px 0", fontSize: 12 };
const instructions: React.CSSProperties = { color: theme.color.textSecondary, fontSize: 13, lineHeight: 1.5 };
const overlay: React.CSSProperties = { position: "fixed", inset: 0, zIndex: 50, background: theme.chrome.backdrop, display: "flex", alignItems: "center", justifyContent: "center", padding: 20 };
const modal: React.CSSProperties = { width: "min(720px, 96vw)", maxHeight: "92vh", overflow: "auto", padding: 20, background: theme.color.surface, border: `1px solid ${theme.color.border}`, borderRadius: theme.radius.lg, boxShadow: theme.shadow.overlay };
