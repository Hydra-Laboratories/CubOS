import { useEffect, useState } from "react";
import type { DeckResponse, GantryPosition, GantryResponse, LabwareConfig, WellPlateConfig, VialConfig, VialGridConfig, TipRackConfig, TipDisposalConfig, WellPlateHolderConfig, Coordinate3D, DeckConfig } from "../../types";
import { CoordinateField, NumberField, OptionalNumberField, SaveButton, SaveTargetHint, SavedStatus, TextField, UnsavedNotice } from "./fields";
import { useSaveShortcut } from "./saveHelpers";
import ConfigFilePicker from "./ConfigFilePicker";
import { normalizeYamlFilename } from "./field-utils";
import LabwareCalibrationModal from "../deck/LabwareCalibrationModal";
import RawYamlPanel from "./RawYamlPanel";
import { useConfirm } from "../common/useConfirm";
import * as theme from "../../theme";

interface Props {
  configs: string[];
  selectedFile: string | null;
  onSelectFile: (f: string) => void;
  onImportFile: (f: string) => void;
  onNewFile?: () => void;
  onDeleteFile?: (f: string) => void;
  deleteDisabledReason?: string | null;
  /** Last successful save on this tab, shown as a "Saved" acknowledgement. */
  lastSaved?: { filename: string; at: Date } | null;
  importedFrom?: string | null;
  deck: DeckResponse | null;
  /** The last-saved (server-loaded) deck, used to reset local edits when
   * the user discards. Differs from `deck` when the parent is passing a
   * local working copy with unsaved edits. */
  baseline?: DeckResponse | null;
  onSave: (filename: string, body: DeckConfig) => Promise<void> | void;
  onLocalChange: (deck: DeckResponse) => void;
  /** True when this deck has local edits not yet saved to disk. The
   * prompt to save lives here (not in the Protocol tab) because this is
   * where the deck is written. */
  dirty?: boolean;
  onRefresh: () => void;
  /** Loaded gantry + live position, for the labware calibration modal.
   * Calibration stays disabled until a gantry config is loaded. */
  gantry?: GantryResponse | null;
  position?: GantryPosition | null;
  isRunning?: boolean;
}

function buildDeckResponse(
  labware: Record<string, LabwareConfig>,
  filename: string,
  previousDeck: DeckResponse | null,
): DeckResponse {
  const previousByKey = new Map(previousDeck?.labware.map((item) => [item.key, item]));
  return {
    filename,
    labware: Object.entries(labware).map(([key, config]) => ({
      ...previousByKey.get(key),
      key,
      config,
      wells: previousByKey.get(key)?.wells ?? null,
    })),
  };
}

function isValid(labware: Record<string, LabwareConfig>): boolean {
  for (const entry of Object.values(labware)) {
    // Only validate editable types; unsupported types are preserved as-is.
    if (!isEditableDeckLabware(entry)) continue;
    if (!entry.name.trim()) return false;
  }
  return true;
}

function isEditableDeckLabware(
  entry: LabwareConfig,
): entry is WellPlateConfig | VialConfig | VialGridConfig | TipRackConfig | TipDisposalConfig | WellPlateHolderConfig {
  return (
    entry.type === "well_plate" ||
    entry.type === "vial" ||
    entry.type === "vial_grid" ||
    entry.type === "tip_rack" ||
    entry.type === "tip_disposal" ||
    entry.type === "well_plate_holder"
  );
}

function toCoordinate3D(value: { x: number; y: number; z?: number } | null | undefined): Coordinate3D {
  if (!value) return { x: 0, y: 0, z: 0 };
  return { x: value.x, y: value.y, z: value.z ?? 0 };
}

function labwareFromDeck(deck: DeckResponse | null): Record<string, LabwareConfig> {
  const obj: Record<string, LabwareConfig> = {};
  deck?.labware.forEach((item) => {
    obj[item.key] = structuredClone(item.config);
  });
  return obj;
}

export default function DeckEditor({ configs, selectedFile, onSelectFile, onImportFile, onNewFile, onDeleteFile, deleteDisabledReason, lastSaved, importedFrom, deck, baseline, onSave, onLocalChange, dirty, onRefresh, gantry = null, position = null, isRunning = false }: Props) {
  const [labware, setLabware] = useState<Record<string, LabwareConfig>>(() => labwareFromDeck(deck));
  const [calibrateOpen, setCalibrateOpen] = useState(false);
  const [calibrationMode, setCalibrationMode] = useState<"new" | "calibrate">("calibrate");
  const [saveAs, setSaveAs] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [requestConfirm, confirmDialog] = useConfirm();

  useEffect(() => {
    setLabware(labwareFromDeck(deck));
  }, [deck]);

  const syncViz = (next: Record<string, LabwareConfig>) => {
    setSaveError(null);
    onLocalChange(buildDeckResponse(next, selectedFile ?? "unsaved", deck));
  };

  const updateLabware = (key: string, updated: LabwareConfig) => {
    const next = { ...labware, [key]: updated };
    setLabware(next);
    syncViz(next);
  };

  const removeLabware = (key: string) => {
    const next = { ...labware };
    delete next[key];
    setLabware(next);
    syncViz(next);
  };

  const hasItems = Object.keys(labware).length > 0;
  const valid = hasItems && isValid(labware);
  const canSave = valid && (!!saveAs.trim() || !!selectedFile) && !saving;
  const canCalibrateLabware = !!deck && !!gantry && !isRunning;
  // The modal calibrates what the editor currently shows (including unsaved
  // edits), so build its deck view from the local labware state.
  const calibrationDeck: DeckResponse | null = deck
    ? { ...deck, labware: Object.entries(labware).map(([key, config]) => ({ key, config, wells: null })) }
    : null;

  const handleCalibrationSave = async (filename: string, body: DeckConfig) => {
    await Promise.resolve(onSave(filename, body));
    setLabware(body.labware);
    setSaveError(null);
  };

  const saveAsFilename = normalizeYamlFilename(saveAs);
  const saveAsExists = !!saveAsFilename && configs.includes(saveAsFilename);

  const handleSave = async () => {
    if (!canSave) return;
    const normalized = saveAsFilename || selectedFile || "";
    if (!normalized) return;
    if (saveAsExists && normalized !== selectedFile && !(await confirmOverwrite(normalized))) return;
    setSaving(true);
    try {
      await Promise.resolve(onSave(normalized, { labware }));
      onSelectFile(normalized);
      setSaveAs("");
      setSaveError(null);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const confirmOverwrite = (filename: string) => requestConfirm({
    title: "Overwrite file?",
    message: `${filename} already exists. Overwrite it?`,
    confirmLabel: "Overwrite",
    danger: true,
  });

  useSaveShortcut(handleSave, canSave);

  const handleDiscard = async () => {
    const confirmed = await requestConfirm({
      title: "Discard changes?",
      message: "Discard unsaved deck changes?",
      confirmLabel: "Discard",
      danger: true,
    });
    if (!confirmed) return;
    setLabware(labwareFromDeck(baseline ?? null));
    setSaveError(null);
    onRefresh();
  };

  return (
    <div>
      <ConfigFilePicker
        kind="Deck"
        configs={configs}
        selectedFile={importedFrom ?? selectedFile}
        onSelectFile={onImportFile}
        onNew={onNewFile}
        onDelete={onDeleteFile}
        deleteDisabledReason={deleteDisabledReason}
        note={importedFrom && selectedFile
          ? <>Opened as a working copy: edits save to <span style={theme.mono}>{selectedFile}</span>, not to {importedFrom}.</>
          : undefined}
      />

      <div style={{ display: "flex", gap: 8, margin: "12px 0" }}>
        <button
          onClick={() => { setCalibrationMode("new"); setCalibrateOpen(true); }}
          disabled={!canCalibrateLabware}
          style={{
            ...addBtnStyle,
            opacity: canCalibrateLabware ? 1 : 0.45,
            cursor: canCalibrateLabware ? "pointer" : "not-allowed",
          }}
          title={canCalibrateLabware
            ? "Create and calibrate new labware"
            : isRunning
              ? "Protocol running"
              : !deck
                ? "Load a deck config first"
                : "Load a gantry config first"}
        >
          New Labware
        </button>
        <button
          onClick={() => { setCalibrationMode("calibrate"); setCalibrateOpen(true); }}
          disabled={!canCalibrateLabware}
          style={{
            ...calibrateBtnStyle,
            opacity: canCalibrateLabware ? 1 : 0.45,
            cursor: canCalibrateLabware ? "pointer" : "not-allowed",
          }}
          title={canCalibrateLabware
            ? "Open labware calibration"
            : isRunning
              ? "Protocol running"
              : !deck
                ? "Load a deck config first"
                : "Load a gantry config first"}
        >
          Calibrate labware
        </button>
      </div>

      {Object.entries(labware).map(([key, entry]) => (
        <div key={key} style={cardStyle}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <h4 style={{ ...theme.panelTitle, ...theme.mono, fontSize: 13 }}>{key}</h4>
            <button onClick={() => removeLabware(key)} style={removeBtnStyle}>Remove</button>
          </div>
          {isEditableDeckLabware(entry) ? (
            <>
              <TextField id={`${key}-name`} name={`${key}_name`} label="Component ID" value={entry.name} onChange={(v) => updateLabware(key, { ...entry, name: v })} required />
              <TextField id={`${key}-model`} name={`${key}_model`} label="Model" value={entry.model_name ?? ""} onChange={(v) => updateLabware(key, { ...entry, model_name: v })} />
              {entry.type === "well_plate" && <WellPlateFields entry={entry} onChange={(v) => updateLabware(key, v)} parentKey={key} />}
              {entry.type === "vial" && <VialFields entry={entry} onChange={(v) => updateLabware(key, v)} parentKey={key} />}
              {entry.type === "vial_grid" && <VialGridFields entry={entry} onChange={(v) => updateLabware(key, v)} parentKey={key} />}
              {entry.type === "tip_rack" && <TipRackFields entry={entry} onChange={(v) => updateLabware(key, v)} parentKey={key} />}
              {entry.type === "tip_disposal" && <TipDisposalFields entry={entry} onChange={(v) => updateLabware(key, v)} parentKey={key} />}
              {entry.type === "well_plate_holder" && <HolderFields entry={entry} onChange={(v) => updateLabware(key, v)} parentKey={key} />}
            </>
          ) : (
            <div style={unsupportedNoteStyle}>
              <strong>{entry.type}</strong> — editing not supported. This entry will be passed through to CubOS unchanged on save; the visualization updates after saving.
            </div>
          )}
        </div>
      ))}

      <RawYamlPanel
        value={{ labware }}
        onApply={(parsed) => {
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            return "Top level must be a mapping with a `labware:` key.";
          }
          const lw = (parsed as { labware?: unknown }).labware;
          if (!lw || typeof lw !== "object" || Array.isArray(lw)) {
            return "`labware:` must be a mapping of deck keys.";
          }
          const next = lw as Record<string, LabwareConfig>;
          setLabware(next);
          syncViz(next);
          return null;
        }}
      />

      <div style={{ marginTop: 12 }}>
        {dirty && (
          <UnsavedNotice>
            <strong>Unsaved changes.</strong>{" "}
            Save this deck before running a protocol — runs use the saved file, not your edits.
          </UnsavedNotice>
        )}
        {saveError && (
          <div style={saveErrorStyle}>Save failed: {saveError}</div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <input
            aria-label="Save as filename"
            value={saveAs}
            onChange={(e) => setSaveAs(e.target.value)}
            placeholder={selectedFile ?? "my_deck.yaml"}
            style={filenameInputStyle}
          />
          <SaveButton
            disabled={!canSave}
            onClick={handleSave}
          />
          {dirty && (
            <button onClick={handleDiscard} style={discardBtnStyle}>Discard changes</button>
          )}
          {lastSaved && !dirty && <SavedStatus filename={lastSaved.filename} at={lastSaved.at} />}
        </div>
        <SaveTargetHint saveAs={saveAsFilename} selectedFile={selectedFile} exists={saveAsExists} />
        {!hasItems && (
          <p style={hintTextStyle}>Add at least one well plate or vial before saving.</p>
        )}
      </div>
      {confirmDialog}
      <LabwareCalibrationModal
        open={calibrateOpen}
        onClose={() => setCalibrateOpen(false)}
        deck={calibrationDeck}
        gantry={gantry}
        position={position}
        onSaveDeck={handleCalibrationSave}
        initialMode={calibrationMode}
      />
    </div>
  );
}

function WellPlateFields({ entry, onChange, parentKey }: { entry: WellPlateConfig; onChange: (v: WellPlateConfig) => void; parentKey: string }) {
  const a1 = entry.calibration.a1 ?? { x: 0, y: 0, z: 0 };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-rows`} name={`${parentKey}_rows`} label="Rows" value={entry.rows} step={1} onChange={(v) => onChange({ ...entry, rows: v })} required />
        <NumberField id={`${parentKey}-cols`} name={`${parentKey}_cols`} label="Columns" value={entry.columns} step={1} onChange={(v) => onChange({ ...entry, columns: v })} required />
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-length`} name={`${parentKey}_length`} label="Length (mm)" value={entry.length} onChange={(v) => onChange({ ...entry, length: v })} />
        <NumberField id={`${parentKey}-width`} name={`${parentKey}_width`} label="Width (mm)" value={entry.width} onChange={(v) => onChange({ ...entry, width: v })} />
        <NumberField id={`${parentKey}-height`} name={`${parentKey}_height`} label="Height (mm)" value={entry.height} onChange={(v) => onChange({ ...entry, height: v })} />
      </div>
      <CoordinateField id={`${parentKey}-a1`} name={`${parentKey}_a1`} label="Calibration A1" value={a1} onChange={(v) => onChange({ ...entry, calibration: { ...entry.calibration, a1: v } })} required />
      <CoordinateField id={`${parentKey}-a2`} name={`${parentKey}_a2`} label="Calibration A2" value={entry.calibration.a2} onChange={(v) => onChange({ ...entry, calibration: { ...entry.calibration, a2: v } })} required />
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-xoffset`} name={`${parentKey}_xoffset`} label="Well pitch X (mm)" value={entry.x_offset} onChange={(v) => onChange({ ...entry, x_offset: v })} required />
        <NumberField id={`${parentKey}-yoffset`} name={`${parentKey}_yoffset`} label="Well pitch Y (mm)" value={entry.y_offset} onChange={(v) => onChange({ ...entry, y_offset: v })} required />
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-capacity`} name={`${parentKey}_capacity`} label="Capacity (uL)" value={entry.capacity_ul} onChange={(v) => onChange({ ...entry, capacity_ul: v })} />
        <NumberField id={`${parentKey}-workingvol`} name={`${parentKey}_workingvol`} label="Working vol (uL)" value={entry.working_volume_ul} onChange={(v) => onChange({ ...entry, working_volume_ul: v })} />
      </div>
    </div>
  );
}

function HolderFields({ entry, onChange, parentKey }: { entry: WellPlateHolderConfig; onChange: (v: WellPlateHolderConfig) => void; parentKey: string }) {
  const plate = entry.well_plate;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
      <CoordinateField
        id={`${parentKey}-location`}
        name={`${parentKey}_location`}
        label="Location"
        value={toCoordinate3D(entry.location)}
        onChange={(v) => onChange({ ...entry, location: v })}
        required
      />
      {plate ? (
        <>
          <div style={{ ...theme.mono, fontSize: 12, color: theme.color.textMuted, marginTop: 4 }}>Nested well plate</div>
          <div style={{ display: "flex", gap: 8 }}>
            <NumberField id={`${parentKey}-plate-rows`} name={`${parentKey}_plate_rows`} label="Rows" value={plate.rows} step={1} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, rows: v } })} required />
            <NumberField id={`${parentKey}-plate-cols`} name={`${parentKey}_plate_cols`} label="Columns" value={plate.columns} step={1} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, columns: v } })} required />
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <NumberField id={`${parentKey}-plate-length`} name={`${parentKey}_plate_length`} label="Length (mm)" value={plate.length ?? null} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, length: v } })} />
            <NumberField id={`${parentKey}-plate-width`} name={`${parentKey}_plate_width`} label="Width (mm)" value={plate.width ?? null} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, width: v } })} />
            <NumberField id={`${parentKey}-plate-height`} name={`${parentKey}_plate_height`} label="Height (mm)" value={plate.height ?? null} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, height: v } })} />
          </div>
          <CoordinateField
            id={`${parentKey}-plate-a1`}
            name={`${parentKey}_plate_a1`}
            label="Calibration A1"
            value={toCoordinate3D(plate.calibration.a1)}
            onChange={(v) => onChange({ ...entry, well_plate: { ...plate, calibration: { ...plate.calibration, a1: v } } })}
            required
          />
          <CoordinateField
            id={`${parentKey}-plate-a2`}
            name={`${parentKey}_plate_a2`}
            label="Calibration A2"
            value={toCoordinate3D(plate.calibration.a2)}
            onChange={(v) => onChange({ ...entry, well_plate: { ...plate, calibration: { ...plate.calibration, a2: v } } })}
            required
          />
          <div style={{ display: "flex", gap: 8 }}>
            <NumberField id={`${parentKey}-plate-xoffset`} name={`${parentKey}_plate_xoffset`} label="Well pitch X (mm)" value={plate.x_offset} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, x_offset: v } })} required />
            <NumberField id={`${parentKey}-plate-yoffset`} name={`${parentKey}_plate_yoffset`} label="Well pitch Y (mm)" value={plate.y_offset} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, y_offset: v } })} required />
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <NumberField id={`${parentKey}-plate-capacity`} name={`${parentKey}_plate_capacity`} label="Capacity (uL)" value={plate.capacity_ul ?? null} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, capacity_ul: v } })} />
            <NumberField id={`${parentKey}-plate-workingvol`} name={`${parentKey}_plate_workingvol`} label="Working vol (uL)" value={plate.working_volume_ul ?? null} onChange={(v) => onChange({ ...entry, well_plate: { ...plate, working_volume_ul: v } })} />
          </div>
        </>
      ) : (
        <div style={unsupportedNoteStyle}>No nested well plate on this holder — add one via Raw YAML.</div>
      )}
    </div>
  );
}

function VialFields({ entry, onChange, parentKey }: { entry: VialConfig; onChange: (v: VialConfig) => void; parentKey: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-height`} name={`${parentKey}_height`} label="Height (mm)" value={entry.height} onChange={(v) => onChange({ ...entry, height: v })} />
        <NumberField id={`${parentKey}-diameter`} name={`${parentKey}_diameter`} label="Diameter (mm)" value={entry.diameter} onChange={(v) => onChange({ ...entry, diameter: v })} />
      </div>
      <CoordinateField id={`${parentKey}-location`} name={`${parentKey}_location`} label="Location" value={entry.location} onChange={(v) => onChange({ ...entry, location: v })} required />
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-capacity`} name={`${parentKey}_capacity`} label="Capacity (uL)" value={entry.capacity_ul} onChange={(v) => onChange({ ...entry, capacity_ul: v })} />
        <NumberField id={`${parentKey}-workingvol`} name={`${parentKey}_workingvol`} label="Working vol (uL)" value={entry.working_volume_ul} onChange={(v) => onChange({ ...entry, working_volume_ul: v })} />
      </div>
    </div>
  );
}

function VialGridFields({ entry, onChange, parentKey }: { entry: VialGridConfig; onChange: (v: VialGridConfig) => void; parentKey: string }) {
  const a1 = entry.calibration.a1 ?? { x: 0, y: 0, z: 0 };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-rows`} name={`${parentKey}_rows`} label="Rows" value={entry.rows} step={1} onChange={(v) => onChange({ ...entry, rows: v })} required />
        <NumberField id={`${parentKey}-cols`} name={`${parentKey}_cols`} label="Columns" value={entry.columns} step={1} onChange={(v) => onChange({ ...entry, columns: v })} required />
      </div>
      <CoordinateField id={`${parentKey}-a1`} name={`${parentKey}_a1`} label="Calibration A1 (vial rim)" value={a1} onChange={(v) => onChange({ ...entry, calibration: { ...entry.calibration, a1: v } })} required />
      <CoordinateField id={`${parentKey}-a2`} name={`${parentKey}_a2`} label="Calibration A2" value={entry.calibration.a2} onChange={(v) => onChange({ ...entry, calibration: { ...entry.calibration, a2: v } })} required />
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-xoffset`} name={`${parentKey}_xoffset`} label="Vial pitch A1->A2 (mm)" value={entry.x_offset} onChange={(v) => onChange({ ...entry, x_offset: v })} required />
        <NumberField id={`${parentKey}-yoffset`} name={`${parentKey}_yoffset`} label="Row pitch (mm)" value={entry.y_offset} onChange={(v) => onChange({ ...entry, y_offset: v })} required />
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-vialheight`} name={`${parentKey}_vialheight`} label="Vial height (mm)" value={entry.vial_height ?? 0} onChange={(v) => onChange({ ...entry, vial_height: v })} />
        <NumberField id={`${parentKey}-vialdiameter`} name={`${parentKey}_vialdiameter`} label="Vial diameter (mm)" value={entry.vial_diameter ?? 0} onChange={(v) => onChange({ ...entry, vial_diameter: v })} />
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-capacity`} name={`${parentKey}_capacity`} label="Capacity (uL)" value={entry.capacity_ul} onChange={(v) => onChange({ ...entry, capacity_ul: v })} required />
        <NumberField id={`${parentKey}-workingvol`} name={`${parentKey}_workingvol`} label="Working vol (uL)" value={entry.working_volume_ul} onChange={(v) => onChange({ ...entry, working_volume_ul: v })} required />
      </div>
    </div>
  );
}

function TipRackFields({ entry, onChange, parentKey }: { entry: TipRackConfig; onChange: (v: TipRackConfig) => void; parentKey: string }) {
  const a1 = entry.calibration?.a1 ?? { x: 0, y: 0, z: 0 };
  const a2 = entry.calibration?.a2 ?? { x: 0, y: 0, z: 0 };
  const setCal = (point: "a1" | "a2", v: { x: number; y: number; z: number }) =>
    onChange({ ...entry, calibration: { a1, a2, ...entry.calibration, [point]: v } });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-rows`} name={`${parentKey}_rows`} label="Rows" value={entry.rows ?? 0} step={1} onChange={(v) => onChange({ ...entry, rows: v })} required />
        <NumberField id={`${parentKey}-cols`} name={`${parentKey}_cols`} label="Columns" value={entry.columns ?? 0} step={1} onChange={(v) => onChange({ ...entry, columns: v })} required />
      </div>
      <CoordinateField id={`${parentKey}-a1`} name={`${parentKey}_a1`} label="Calibration A1 (tip top)" value={a1} onChange={(v) => setCal("a1", v)} required />
      <CoordinateField id={`${parentKey}-a2`} name={`${parentKey}_a2`} label="Calibration A2" value={a2} onChange={(v) => setCal("a2", v)} required />
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-xoffset`} name={`${parentKey}_xoffset`} label="Tip pitch A1->A2 (mm)" value={entry.x_offset ?? 0} onChange={(v) => onChange({ ...entry, x_offset: v })} required />
        <NumberField id={`${parentKey}-yoffset`} name={`${parentKey}_yoffset`} label="Row pitch (mm)" value={entry.y_offset ?? 0} onChange={(v) => onChange({ ...entry, y_offset: v })} required />
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <NumberField id={`${parentKey}-pickupz`} name={`${parentKey}_pickupz`} label="Pickup Z (press)" value={entry.pickup_z ?? 0} onChange={(v) => onChange({ ...entry, pickup_z: v })} required />
        <OptionalNumberField id={`${parentKey}-dropz`} name={`${parentKey}_dropz`} label="Drop Z (eject)" value={entry.drop_z ?? null} onChange={(v) => onChange({ ...entry, drop_z: v })} />
        <NumberField id={`${parentKey}-tiplength`} name={`${parentKey}_tiplength`} label="Tip length (mm)" value={entry.tip_length ?? 0} onChange={(v) => onChange({ ...entry, tip_length: v })} required />
      </div>
    </div>
  );
}

function TipDisposalFields({ entry, onChange, parentKey }: { entry: TipDisposalConfig; onChange: (v: TipDisposalConfig) => void; parentKey: string }) {
  const location = (entry.location ?? { x: 0, y: 0, z: 0 }) as { x: number; y: number; z: number };
  const setLocation = (v: { x: number; y: number; z: number }) => {
    const next: TipDisposalConfig = { ...entry, location: v };
    const slots = entry.slots as Record<string, { location?: unknown }> | undefined;
    if (slots?.discard) {
      // Keep the discard slot glued to the disposal reference point.
      next.slots = { ...slots, discard: { ...slots.discard, location: v } };
    }
    onChange(next);
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
      <CoordinateField id={`${parentKey}-location`} name={`${parentKey}_location`} label="Drop point (tip-end height)" value={location} onChange={setLocation} required />
    </div>
  );
}

/** Muted group panel for each labware item block. */
const cardStyle: React.CSSProperties = {
  background: theme.color.surfaceMuted,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 12,
  marginTop: 8,
};

const addBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
};

const calibrateBtnStyle: React.CSSProperties = {
  ...theme.btn.primary,
  ...theme.btnSmall,
  padding: "5px 16px",
  marginLeft: "auto",
};

const removeBtnStyle: React.CSSProperties = {
  ...theme.btn.danger,
  ...theme.btnSmall,
  fontSize: 11,
  padding: "2px 10px",
};

const filenameInputStyle: React.CSSProperties = {
  ...theme.input,
  ...theme.mono,
  flex: 1,
};

const unsupportedNoteStyle: React.CSSProperties = {
  ...theme.notice.warning,
  marginTop: 8,
};

const saveErrorStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginBottom: 8,
};

const discardBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
};

const hintTextStyle: React.CSSProperties = {
  marginTop: 6,
  color: theme.color.textMuted,
  fontSize: 12,
};
