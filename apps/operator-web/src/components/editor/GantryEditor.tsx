import { useState } from "react";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";
import type {
  GantryResponse,
  GantryConfig,
  GrblSettingsConfig,
  InstrumentConfig,
  InstrumentSchemas,
  InstrumentTypeInfo,
} from "../../types";
import { DirtyMarker, NumberField, SaveButton, SaveTargetHint, SavedStatus, TextField, UnsavedNotice } from "./fields";
import { useSaveShortcut } from "./saveHelpers";
import { normalizeYamlFilename } from "./field-utils";
import { isFieldEqual } from "./field-utils";
import ConfigFilePicker from "./ConfigFilePicker";
import { EMPTY_GANTRY } from "./gantryDefaults";
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
  gantry: GantryResponse | null;
  /** The server-loaded config; used to decide which fields show the
   * amber "*" dirty marker. Differs from ``gantry`` when the parent
   * is passing a local working copy with unsaved edits. */
  baseline: GantryResponse | null;
  instrumentTypes: InstrumentTypeInfo[];
  instrumentSchemas: InstrumentSchemas;
  onSave: (filename: string, body: GantryConfig) => Promise<void> | void;
  /** Called on every local edit so the parent can persist the working
   * copy across tab switches (the editor unmounts on tab-away and would
   * otherwise lose its useState). */
  onLocalChange?: (gantry: GantryResponse) => void;
  /** True when this gantry has local edits not yet saved to disk. The
   * prompt to save lives here (not in the Protocol tab) because this is
   * where the gantry is written. */
  dirty?: boolean;
  onRefresh: () => void;
}


const GRBL_NUMBER_FIELDS: Array<{ key: keyof GrblSettingsConfig; label: string }> = [
  { key: "dir_invert_mask", label: "Dir invert mask" },
  { key: "status_report", label: "Status report" },
  { key: "homing_dir_mask", label: "Homing dir mask" },
  { key: "homing_pull_off", label: "Homing pull-off" },
  { key: "steps_per_mm_x", label: "Steps/mm X" },
  { key: "steps_per_mm_y", label: "Steps/mm Y" },
  { key: "steps_per_mm_z", label: "Steps/mm Z" },
  { key: "max_rate_x", label: "Max rate X" },
  { key: "max_rate_y", label: "Max rate Y" },
  { key: "max_rate_z", label: "Max rate Z" },
  { key: "accel_x", label: "Accel X" },
  { key: "accel_y", label: "Accel Y" },
  { key: "accel_z", label: "Accel Z" },
  { key: "max_travel_x", label: "Max travel X" },
  { key: "max_travel_y", label: "Max travel Y" },
  { key: "max_travel_z", label: "Max travel Z" },
];

const GRBL_BOOLEAN_FIELDS: Array<{ key: keyof GrblSettingsConfig; label: string }> = [
  { key: "soft_limits", label: "Soft limits" },
  { key: "hard_limits", label: "Hard limits" },
  { key: "homing_enable", label: "Homing enable" },
];

const INSTRUMENT_COLORS: Record<string, string> = {
  asmi: theme.categorical.blue,
  uvvis_ccs: theme.categorical.violet,
  pipette: theme.categorical.emerald,
  filmetrics: theme.categorical.amber,
  potentiostat: theme.color.danger,
  uv_curing: theme.categorical.blue,
};

/** Section heading inside a config card (Gantry, Connection, Instruments, …). */
const sectionTitleStyle: React.CSSProperties = {
  ...theme.panelTitle,
  fontSize: 13,
};

export default function GantryEditor({
  configs,
  selectedFile,
  onSelectFile,
  onImportFile,
  onNewFile,
  onDeleteFile,
  deleteDisabledReason,
  lastSaved,
  gantry,
  baseline,
  instrumentTypes,
  instrumentSchemas,
  onSave,
  onLocalChange,
  dirty,
  onRefresh,
}: Props) {
  const [config, setConfig] = useState<GantryConfig | null>(() => (
    gantry ? structuredClone(gantry.config) : null
  ));
  const [addType, setAddType] = useState<string>("");
  const [saveAs, setSaveAs] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [requestConfirm, confirmDialog] = useConfirm();
  // GRBL lives under a collapsed "Advanced settings" panel. Start it
  // open only when GRBL already has unsaved edits (e.g. coming back to
  // this tab) so hidden changes are never silently buried.
  const [advancedOpen, setAdvancedOpen] = useState(() => (
    grblFieldsDiffer(gantry?.config.grbl_settings, baseline?.config.grbl_settings, baseline != null)
  ));
  // Raw YAML mode: an alternate view of the same `config` state, so Save
  // always goes through the identical onSave -> server-schema-validation
  // path as the structured form. `rawText` only drives `config` forward
  // (via commit) when it parses to a plain mapping; an unparsable or
  // malformed edit is held locally as `rawError` instead of corrupting
  // the last-known-good config.
  const [rawMode, setRawMode] = useState(false);
  const [rawText, setRawText] = useState("");
  const [rawError, setRawError] = useState<string | null>(null);

  const selectedAddType = addType || instrumentTypes[0]?.type || "";

  const commit = (next: GantryConfig) => {
    setConfig(next);
    setSaveError(null);
    onLocalChange?.({ filename: selectedFile ?? "unsaved", config: next });
  };

  const startNew = onNewFile ?? (() => {
    commit(structuredClone(EMPTY_GANTRY));
  });

  const enterRawMode = () => {
    if (!config) return;
    setRawText(stringifyYaml(config));
    setRawError(null);
    setRawMode(true);
  };

  // Leaving raw mode while the text doesn't parse would either discard the
  // operator's in-progress edit or silently fall back to the last-parsed
  // config — both surprising. Block the switch until the YAML is valid
  // again (or the operator discards).
  const exitRawMode = () => {
    if (rawError) return;
    setRawMode(false);
  };

  const handleRawChange = (text: string) => {
    setRawText(text);
    let parsed: unknown;
    try {
      parsed = parseYaml(text);
    } catch (err) {
      setRawError(err instanceof Error ? err.message : String(err));
      return;
    }
    if (parsed === null || parsed === undefined || typeof parsed !== "object" || Array.isArray(parsed)) {
      setRawError("Gantry YAML must be a mapping (key: value) at the top level.");
      return;
    }
    setRawError(null);
    commit(parsed as GantryConfig);
  };

  const updateInstrument = (key: string, inst: InstrumentConfig) => {
    if (!config) return;
    commit({
      ...config,
      instruments: { ...config.instruments, [key]: inst },
    });
  };

  const removeInstrument = (key: string) => {
    if (!config) return;
    const next = { ...config.instruments };
    delete next[key];
    commit({ ...config, instruments: next });
  };

  const addInstrument = () => {
    if (!config || !selectedAddType) return;
    let idx = Object.keys(config.instruments).length + 1;
    let key = `${selectedAddType}_${idx}`;
    while (config.instruments[key]) {
      idx += 1;
      key = `${selectedAddType}_${idx}`;
    }

    const vendors = vendorsForType(instrumentTypes, selectedAddType);
    const template: InstrumentConfig = {
      type: selectedAddType,
      vendor: vendors[0] ?? "",
      offset_x: 0,
      offset_y: 0,
      depth: 0,
    };
    commit({
      ...config,
      instruments: {
        ...config.instruments,
        [key]: applyInstrumentFieldDefaults(template, instrumentSchemas),
      },
    });
  };

  const updateGrblSetting = (field: keyof GrblSettingsConfig, value: number | boolean | null) => {
    if (!config) return;
    const nextSettings = { ...(config.grbl_settings ?? {}) };
    if (value === null) {
      delete nextSettings[field];
    } else {
      (nextSettings as Record<string, number | boolean>)[field] = value;
    }
    commit({ ...config, grbl_settings: nextSettings });
  };

  // Per-field dirty compared against the last-saved config. A missing
  // baseline means there's nothing saved yet (brand-new config).
  const base = baseline?.config;
  const notDirty = (a: unknown, b: unknown) => !base || isFieldEqual(a, b);
  // Aggregate GRBL dirtiness (drives the Advanced-settings dirty marker);
  // same helper as the auto-expand initializer so the two never disagree.
  const grblDirty = !!config && grblFieldsDiffer(config.grbl_settings, base?.grbl_settings, !!base);
  const wv = config?.working_volume;
  const bwv = base?.working_volume;
  const cnc = config?.cnc;
  const bcnc = base?.cnc;
  const d = {
    serial_port: !!config && !notDirty(config.serial_port, base?.serial_port ?? ""),
    gantry_type: !!config && !notDirty(config.gantry_type, base?.gantry_type),
    factory_z_travel_mm: !!cnc && !notDirty(cnc.factory_z_travel_mm, bcnc?.factory_z_travel_mm),
    calibration_block_height_mm: !!cnc && !notDirty(cnc.calibration_block_height_mm, bcnc?.calibration_block_height_mm),
    safe_z: !!cnc && !notDirty(cnc.safe_z, bcnc?.safe_z),
    x_min: !!wv && !notDirty(wv.x_min, bwv?.x_min),
    x_max: !!wv && !notDirty(wv.x_max, bwv?.x_max),
    y_min: !!wv && !notDirty(wv.y_min, bwv?.y_min),
    y_max: !!wv && !notDirty(wv.y_max, bwv?.y_max),
    z_min: !!wv && !notDirty(wv.z_min, bwv?.z_min),
    z_max: !!wv && !notDirty(wv.z_max, bwv?.z_max),
  };

  const canSave = !!config && isValidGantry(config) && (!!saveAs.trim() || !!selectedFile) && !saving && !(rawMode && !!rawError);

  const saveAsFilename = normalizeYamlFilename(saveAs);
  const saveAsExists = !!saveAsFilename && configs.includes(saveAsFilename);

  const handleSave = async () => {
    if (!config || !canSave) return;
    const normalized = saveAsFilename || selectedFile || "";
    if (!normalized) return;
    if (saveAsExists && normalized !== selectedFile) {
      const ok = await requestConfirm({
        title: "Overwrite file?",
        message: `${normalized} already exists. Overwrite it?`,
        confirmLabel: "Overwrite",
        danger: true,
      });
      if (!ok) return;
    }
    setSaving(true);
    try {
      await Promise.resolve(onSave(normalized, config));
      onSelectFile(normalized);
      setSaveAs("");
      setSaveError(null);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  useSaveShortcut(handleSave, canSave);

  const handleDiscard = async () => {
    const confirmed = await requestConfirm({
      title: "Discard changes?",
      message: "Discard unsaved gantry changes?",
      confirmLabel: "Discard",
      danger: true,
    });
    if (!confirmed) return;
    setConfig(baseline ? structuredClone(baseline.config) : null);
    setSaveError(null);
    setRawMode(false);
    setRawError(null);
    onRefresh();
  };

  return (
    <div>
      <div style={configPickerRowStyle}>
        <ConfigFilePicker
          kind="Gantry"
          configs={configs}
          selectedFile={selectedFile}
          onSelectFile={onImportFile}
          onNew={startNew}
          onDelete={onDeleteFile}
          deleteDisabledReason={deleteDisabledReason}
        />
        {!config && <button onClick={startNew} style={newConfigBtnStyle}>+ New config</button>}
      </div>

      {config && (
        <>
          <div style={rawToggleRowStyle}>
            <button
              type="button"
              onClick={() => (rawMode ? exitRawMode() : enterRawMode())}
              disabled={rawMode && !!rawError}
              style={rawModeToggleStyle}
              title={rawMode && rawError ? "Fix the YAML error before switching back to the form" : undefined}
            >
              {rawMode ? "Back to form" : "Edit raw YAML"}
            </button>
          </div>

          {rawMode ? (
            <div style={cardStyle}>
              <h4 style={{ ...sectionTitleStyle, margin: "0 0 8px" }}>Raw YAML</h4>
              <textarea
                aria-label="Raw gantry YAML"
                value={rawText}
                onChange={(e) => handleRawChange(e.target.value)}
                spellCheck={false}
                style={rawTextareaStyle}
              />
              {rawError && (
                <div style={saveErrorStyle}>{rawError}</div>
              )}
              <div style={rawHintStyle}>
                Saved through the same path as the form editor — the server re-validates this YAML against the
                gantry schema before writing it to disk.
              </div>
            </div>
          ) : (
            <>
              {/* Top-level identity: the only two things an operator needs
                  at a glance — which machine this is, and where it's
                  plugged in. */}
              <div style={cardStyle}>
                <h4 style={{ ...sectionTitleStyle, margin: "0 0 8px" }}>Gantry</h4>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  <SelectField
                    id="gantry-type"
                    name="gantry_type"
                    label="Gantry type"
                    value={config.gantry_type}
                    options={[
                      { value: "cub", label: "Cub" },
                      { value: "cub_xl", label: "Cub XL" },
                    ]}
                    onChange={(v) => commit({ ...config, gantry_type: v as "cub" | "cub_xl" })}
                    dirty={d.gantry_type}
                    required
                  />
                  <TextField
                    id="gantry-serial-port"
                    name="serial_port"
                    label="Serial port"
                    value={config.serial_port}
                    onChange={(v) => commit({ ...config, serial_port: v })}
                    dirty={d.serial_port}
                  />
                </div>
              </div>

              <div style={cardStyle}>
                <h4 style={{ ...sectionTitleStyle, margin: "0 0 8px" }}>Connection</h4>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  <NumberField
                    id="gantry-factory-z-travel"
                    name="factory_z_travel_mm"
                    label="Factory Z travel"
                    value={config.cnc.factory_z_travel_mm}
                    onChange={(v) => commit({ ...config, cnc: { ...config.cnc, factory_z_travel_mm: v } })}
                    dirty={d.factory_z_travel_mm}
                    required
                  />
                  <NumberField
                    id="gantry-calibration-block-height"
                    name="calibration_block_height_mm"
                    label="Block height"
                    value={Number(config.cnc.calibration_block_height_mm ?? 0)}
                    onChange={(v) => commit({ ...config, cnc: { ...config.cnc, calibration_block_height_mm: v } })}
                    dirty={d.calibration_block_height_mm}
                    required
                  />
                  <NumberField
                    id="gantry-safe-z"
                    name="safe_z"
                    label="Safe Z"
                    value={Number(config.cnc.safe_z ?? config.working_volume.z_max)}
                    onChange={(v) => commit({ ...config, cnc: { ...config.cnc, safe_z: v } })}
                    dirty={d.safe_z}
                  />
                </div>
              </div>

              <div style={cardStyle}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                  <h4 style={sectionTitleStyle}>Instruments</h4>
                  <div style={{ display: "flex", gap: 8 }}>
                    <select value={selectedAddType} onChange={(e) => setAddType(e.target.value)} style={selectStyle}>
                      {instrumentTypes.map((it) => (
                        <option key={it.type} value={it.type}>{typeLabel(it.type)}{it.is_mock ? " (mock)" : ""}</option>
                      ))}
                    </select>
                    <button onClick={addInstrument} style={addBtnStyle} disabled={!selectedAddType}>+ Add</button>
                  </div>
                </div>

                {Object.keys(config.instruments).length === 0 && (
                  <div style={emptyInstrumentNoticeStyle}>
                    No mounted instruments yet. Choose the instruments installed on this machine, then save the gantry config.
                  </div>
                )}

                {Object.entries(config.instruments).map(([key, inst]) => {
                  const color = INSTRUMENT_COLORS[inst.type] ?? INSTRUMENT_COLORS[inst.type.replace("mock_", "")] ?? theme.color.textMuted;
                  const fields = fieldsForInstrument(instrumentSchemas, inst.type, inst.vendor);
                  const vendors = vendorsForType(instrumentTypes, inst.type);
                  return (
                    <div key={key} style={instrumentCardStyle}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                        <h4 style={{ ...sectionTitleStyle, color, ...theme.mono }}>
                          {key} <span style={{ fontWeight: 400, color: theme.color.textMuted, fontSize: 11, fontFamily: theme.font.ui }}>({typeLabel(inst.type)})</span>
                          {inst.offline === true && <span style={simulatedBadgeStyle}>simulated</span>}
                        </h4>
                        <button onClick={() => removeInstrument(key)} style={removeBtnStyle}>Remove</button>
                      </div>

                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                        <TextField
                          id={`${key}-type`}
                          name={`${key}_type`}
                          label="Type"
                          value={inst.type}
                          onChange={(v) => {
                            const nextVendors = vendorsForType(instrumentTypes, v);
                            const nextVendor = nextVendors.includes(inst.vendor)
                              ? inst.vendor
                              : nextVendors[0] ?? inst.vendor;
                            updateInstrument(
                              key,
                              rebuildInstrumentForType(inst, v, nextVendor, instrumentSchemas),
                            );
                          }}
                          dirty={isInstrumentFieldDirty(baseline, key, "type", inst.type)}
                          required
                        />
                        {vendors.length > 0 ? (
                          <SelectField
                            id={`${key}-vendor`}
                            name={`${key}_vendor`}
                            label="Vendor"
                            value={inst.vendor}
                            options={vendors.map((v) => ({ value: v, label: v }))}
                            onChange={(v) => updateInstrument(
                              key,
                              applyInstrumentFieldDefaults(
                                { ...inst, vendor: v },
                                instrumentSchemas,
                              ),
                            )}
                            dirty={isInstrumentFieldDirty(baseline, key, "vendor", inst.vendor)}
                            required
                          />
                        ) : (
                          <TextField
                            id={`${key}-vendor`}
                            name={`${key}_vendor`}
                            label="Vendor"
                            value={inst.vendor}
                            onChange={(v) => updateInstrument(key, { ...inst, vendor: v })}
                            dirty={isInstrumentFieldDirty(baseline, key, "vendor", inst.vendor)}
                            required
                          />
                        )}
                        <NumberField id={`${key}-offset-x`} name={`${key}_offset_x`} label="Offset X" value={inst.offset_x} onChange={(v) => updateInstrument(key, { ...inst, offset_x: v })} dirty={isInstrumentFieldDirty(baseline, key, "offset_x", inst.offset_x)} />
                        <NumberField id={`${key}-offset-y`} name={`${key}_offset_y`} label="Offset Y" value={inst.offset_y} onChange={(v) => updateInstrument(key, { ...inst, offset_y: v })} dirty={isInstrumentFieldDirty(baseline, key, "offset_y", inst.offset_y)} />
                        <NumberField id={`${key}-depth`} name={`${key}_depth`} label="Depth" value={Number(inst.depth ?? 0)} onChange={(v) => updateInstrument(key, { ...inst, depth: v })} dirty={isInstrumentFieldDirty(baseline, key, "depth", inst.depth)} />
                        <SelectField
                          id={`${key}-offline`}
                          name={`${key}_offline`}
                          label="Hardware"
                          value={String(inst.offline === true)}
                          options={[{ value: "false", label: "Connected" }, { value: "true", label: "Simulated (offline)" }]}
                          onChange={(v) => updateInstrument(key, { ...inst, offline: v === "true" })}
                          dirty={isInstrumentFieldDirty(baseline, key, "offline", inst.offline)}
                        />
                      </div>

                      {fields.length > 0 && (
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 8 }}>
                          {fields.map((field) => {
                            const value = (inst as Record<string, unknown>)[field.name];
                            const fieldDirty = isInstrumentFieldDirty(baseline, key, field.name, value);
                            if (field.choices) {
                              return (
                                <SelectField
                                  key={field.name}
                                  id={`${key}-${field.name}`}
                                  name={`${key}_${field.name}`}
                                  label={fieldLabel(field.name) + (field.required ? " *" : "")}
                                  value={String(value ?? field.default ?? "")}
                                  options={field.choices.map((c) => ({ value: c, label: c }))}
                                  onChange={(v) => updateInstrument(key, { ...inst, [field.name]: v })}
                                  dirty={fieldDirty}
                                />
                              );
                            }
                            if (field.type === "bool") {
                              return (
                                <SelectField
                                  key={field.name}
                                  id={`${key}-${field.name}`}
                                  name={`${key}_${field.name}`}
                                  label={fieldLabel(field.name) + (field.required ? " *" : "")}
                                  value={String(value ?? field.default ?? false)}
                                  options={[{ value: "true", label: "true" }, { value: "false", label: "false" }]}
                                  onChange={(v) => updateInstrument(key, { ...inst, [field.name]: v === "true" })}
                                  dirty={fieldDirty}
                                />
                              );
                            }
                            if (field.type === "float" || field.type === "int") {
                              return (
                                <NumberField
                                  key={field.name}
                                  id={`${key}-${field.name}`}
                                  name={`${key}_${field.name}`}
                                  label={fieldLabel(field.name) + (field.required ? " *" : "")}
                                  value={Number(value ?? field.default ?? 0)}
                                  onChange={(v) => updateInstrument(key, { ...inst, [field.name]: v })}
                                  dirty={fieldDirty}
                                />
                              );
                            }
                            return (
                              <TextField
                                key={field.name}
                                id={`${key}-${field.name}`}
                                name={`${key}_${field.name}`}
                                label={fieldLabel(field.name) + (field.required ? " *" : "")}
                                value={String(value ?? field.default ?? "")}
                                onChange={(v) => updateInstrument(key, { ...inst, [field.name]: v })}
                                dirty={fieldDirty}
                              />
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              <div style={cardStyle}>
                <h4 style={{ ...sectionTitleStyle, margin: "0 0 8px" }}>Working Volume</h4>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  <NumberField id="wv-xmin" name="x_min" label="X min" value={config.working_volume.x_min} onChange={(v) => commit({ ...config, working_volume: { ...config.working_volume, x_min: v } })} dirty={d.x_min} />
                  <NumberField id="wv-xmax" name="x_max" label="X max" value={config.working_volume.x_max} onChange={(v) => commit({ ...config, working_volume: { ...config.working_volume, x_max: v } })} dirty={d.x_max} />
                  <NumberField id="wv-ymin" name="y_min" label="Y min" value={config.working_volume.y_min} onChange={(v) => commit({ ...config, working_volume: { ...config.working_volume, y_min: v } })} dirty={d.y_min} />
                  <NumberField id="wv-ymax" name="y_max" label="Y max" value={config.working_volume.y_max} onChange={(v) => commit({ ...config, working_volume: { ...config.working_volume, y_max: v } })} dirty={d.y_max} />
                  <NumberField id="wv-zmin" name="z_min" label="Z min" value={config.working_volume.z_min} onChange={(v) => commit({ ...config, working_volume: { ...config.working_volume, z_min: v } })} dirty={d.z_min} />
                  <NumberField id="wv-zmax" name="z_max" label="Z max" value={config.working_volume.z_max} onChange={(v) => commit({ ...config, working_volume: { ...config.working_volume, z_max: v } })} dirty={d.z_max} />
                </div>
              </div>

              <div style={cardStyle}>
                <button
                  type="button"
                  onClick={() => setAdvancedOpen((open) => !open)}
                  aria-expanded={advancedOpen}
                  style={advancedHeaderStyle}
                >
                  <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ color: theme.color.textMuted, fontSize: 12, width: 12, display: "inline-block" }}>
                      {advancedOpen ? "▾" : "▸"}
                    </span>
                    Advanced settings
                    {grblDirty && <DirtyMarker />}
                  </span>
                  <span style={theme.sectionLabel}>GRBL Settings</span>
                </button>
                {advancedOpen && (
                  <div style={{ marginTop: 12 }}>
                    <h4 style={{ ...sectionTitleStyle, margin: "0 0 8px" }}>GRBL Settings</h4>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                      {GRBL_BOOLEAN_FIELDS.map(({ key, label }) => (
                        <OptionalBooleanField
                          key={key}
                          id={`grbl-${key}`}
                          name={key}
                          label={label}
                          value={config.grbl_settings?.[key] as boolean | null | undefined}
                          onChange={(v) => updateGrblSetting(key, v)}
                          dirty={!notDirty(config.grbl_settings?.[key], base?.grbl_settings?.[key])}
                        />
                      ))}
                      {GRBL_NUMBER_FIELDS.map(({ key, label }) => (
                        <OptionalNumberField
                          key={key}
                          id={`grbl-${key}`}
                          name={key}
                          label={label}
                          value={config.grbl_settings?.[key] as number | null | undefined}
                          onChange={(v) => updateGrblSetting(key, v)}
                          dirty={!notDirty(config.grbl_settings?.[key], base?.grbl_settings?.[key])}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </>
          )}

          {dirty && (
            <div style={{ marginTop: 12 }}>
              <UnsavedNotice>
                <strong>Unsaved changes.</strong>{" "}
                Save this gantry before running a protocol — runs use the saved file, not your edits.
              </UnsavedNotice>
            </div>
          )}
          {saveError && (
            <div style={saveErrorStyle}>Save failed: {saveError}</div>
          )}

          <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
            <input
              aria-label="Save as filename"
              value={saveAs}
              onChange={(e) => setSaveAs(e.target.value)}
              placeholder={selectedFile ?? "my_gantry.yaml"}
              style={filenameInputStyle}
            />
            <SaveButton onClick={handleSave} disabled={!canSave} />
            {dirty && (
              <button onClick={handleDiscard} style={discardBtnStyle}>Discard changes</button>
            )}
            {lastSaved && !dirty && <SavedStatus filename={lastSaved.filename} at={lastSaved.at} />}
          </div>
          <SaveTargetHint saveAs={saveAsFilename} selectedFile={selectedFile} exists={saveAsExists} />
        </>
      )}
      {confirmDialog}
    </div>
  );
}

// Structural + range checks mirroring the server's Pydantic schema closely
// enough to gate the Save button. Raw-YAML mode can hand this a config with
// missing/mistyped fields (a key deleted or a string typed where a number
// belongs), so every access is defensive — this must never throw, only
// return false. The server's GantryYamlSchema is still the source of truth
// and re-validates on every PUT regardless of what this returns.
function isValidGantry(config: GantryConfig | null | undefined): boolean {
  if (!config || typeof config !== "object") return false;
  if (typeof config.serial_port !== "string") return false;
  if (config.gantry_type !== "cub" && config.gantry_type !== "cub_xl") return false;

  const wv = config.working_volume;
  if (!wv || typeof wv !== "object") return false;
  const wvFields = [wv.x_min, wv.x_max, wv.y_min, wv.y_max, wv.z_min, wv.z_max];
  if (!wvFields.every(isFiniteNumber)) return false;
  if (wv.x_min >= wv.x_max || wv.y_min >= wv.y_max || wv.z_min >= wv.z_max) return false;

  const cnc = config.cnc;
  if (!cnc || typeof cnc !== "object" || !isFiniteNumber(cnc.factory_z_travel_mm)) return false;
  if (cnc.factory_z_travel_mm <= 0 || cnc.factory_z_travel_mm < wv.z_max - wv.z_min) return false;
  if (cnc.calibration_block_height_mm != null
    && (!isFiniteNumber(cnc.calibration_block_height_mm) || cnc.calibration_block_height_mm <= 0)) return false;
  if (cnc.safe_z != null
    && (!isFiniteNumber(cnc.safe_z) || cnc.safe_z < wv.z_min || cnc.safe_z > wv.z_max)) return false;

  const instruments = config.instruments;
  if (instruments == null || typeof instruments !== "object") return false;
  for (const inst of Object.values(instruments)) {
    if (!inst || typeof inst.type !== "string" || !inst.type.trim()) return false;
    if (typeof inst.vendor !== "string" || !inst.vendor.trim()) return false;
  }
  return true;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

// True when any GRBL field in `current` differs from `saved`. A missing
// baseline (hasBaseline false) means nothing is saved yet, so there is
// nothing to be dirty against — matches the per-field `notDirty` rule.
function grblFieldsDiffer(
  current: GrblSettingsConfig | null | undefined,
  saved: GrblSettingsConfig | null | undefined,
  hasBaseline: boolean,
): boolean {
  if (!hasBaseline) return false;
  const c = (current ?? {}) as Record<string, unknown>;
  const s = (saved ?? {}) as Record<string, unknown>;
  return [...GRBL_BOOLEAN_FIELDS, ...GRBL_NUMBER_FIELDS].some(({ key }) => !isFieldEqual(c[key], s[key]));
}

function isInstrumentFieldDirty(
  baseline: GantryResponse | null,
  key: string,
  field: string,
  current: unknown,
): boolean {
  const base = baseline?.config.instruments?.[key];
  if (base === undefined) return false;
  return !isFieldEqual((base as Record<string, unknown>)[field], current);
}

function vendorsForType(instrumentTypes: InstrumentTypeInfo[], type: string): string[] {
  return instrumentTypes.find((it) => it.type === type)?.vendors ?? [];
}

function fieldsForInstrument(
  instrumentSchemas: InstrumentSchemas,
  type: string,
  vendor: string,
) {
  return instrumentSchemas[type]?.[vendor] ?? [];
}

// On a type change, stale fields from the old type's schema must not leak into
// the saved YAML: keep core keys, carry over only values whose field names the
// new type/vendor schema declares, then fill schema defaults.
function rebuildInstrumentForType(
  inst: InstrumentConfig,
  type: string,
  vendor: string,
  instrumentSchemas: InstrumentSchemas,
): InstrumentConfig {
  const next: InstrumentConfig = {
    type,
    vendor,
    offset_x: inst.offset_x,
    offset_y: inst.offset_y,
  };
  if (inst.depth !== undefined) next.depth = inst.depth;
  for (const field of fieldsForInstrument(instrumentSchemas, type, vendor)) {
    const prev = (inst as Record<string, unknown>)[field.name];
    if (prev !== undefined) {
      (next as Record<string, unknown>)[field.name] = prev;
    }
  }
  return applyInstrumentFieldDefaults(next, instrumentSchemas);
}

function applyInstrumentFieldDefaults(
  inst: InstrumentConfig,
  instrumentSchemas: InstrumentSchemas,
): InstrumentConfig {
  const next = { ...inst };
  for (const field of fieldsForInstrument(instrumentSchemas, inst.type, inst.vendor)) {
    if (field.default != null && (next as Record<string, unknown>)[field.name] === undefined) {
      (next as Record<string, unknown>)[field.name] = field.default;
    }
  }
  return next;
}

function typeLabel(type: string): string {
  return type.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function fieldLabel(name: string): string {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function SelectField({
  id,
  name,
  label,
  value,
  options,
  onChange,
  dirty,
  required,
}: {
  id?: string;
  name?: string;
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
  dirty?: boolean;
  required?: boolean;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
      <span style={theme.fieldLabel}>
        {label}
        {required && <span style={{ color: theme.color.danger }}> *</span>}
        {dirty && <DirtyMarker />}
      </span>
      <select id={id} name={name} value={value} onChange={(e) => onChange(e.target.value)} style={selectStyle}>
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
    </label>
  );
}

function OptionalNumberField({
  id,
  name,
  label,
  value,
  onChange,
  dirty,
}: {
  id?: string;
  name?: string;
  label: string;
  value: number | null | undefined;
  onChange: (value: number | null) => void;
  dirty?: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "end", gap: 4 }}>
      <div style={{ flex: 1 }}>
        <NumberField
          id={id}
          name={name}
          label={label}
          value={Number(value ?? 0)}
          onChange={(v) => onChange(v)}
          dirty={dirty}
        />
      </div>
      <button onClick={() => onChange(null)} disabled={value == null} style={clearBtnStyle}>Clear</button>
    </div>
  );
}

function OptionalBooleanField({
  id,
  name,
  label,
  value,
  onChange,
  dirty,
}: {
  id?: string;
  name?: string;
  label: string;
  value: boolean | null | undefined;
  onChange: (value: boolean | null) => void;
  dirty?: boolean;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
      <span style={theme.fieldLabel}>
        {label}
        {dirty && <DirtyMarker />}
      </span>
      <select
        id={id}
        name={name}
        value={value == null ? "" : value ? "true" : "false"}
        onChange={(e) => {
          if (e.target.value === "") onChange(null);
          else onChange(e.target.value === "true");
        }}
        style={selectStyle}
      >
        <option value="">unset</option>
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    </label>
  );
}

/** Muted section group (Gantry, Connection, Instruments, Working Volume, Advanced). */
const cardStyle: React.CSSProperties = {
  background: theme.color.surfaceMuted,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 12,
  marginTop: 8,
};

const advancedHeaderStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  width: "100%",
  background: "transparent",
  border: "none",
  padding: 0,
  margin: 0,
  cursor: "pointer",
  color: theme.color.text,
  fontSize: 13,
  fontWeight: 600,
};

/** Surface panel for each instrument block, nested inside the muted section. */
const instrumentCardStyle: React.CSSProperties = {
  background: theme.color.surface,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 12,
  marginTop: 10,
};

const emptyInstrumentNoticeStyle: React.CSSProperties = {
  marginTop: 12,
  padding: "10px 12px",
  borderRadius: theme.radius.md,
  border: `1px solid ${theme.color.border}`,
  background: theme.color.surfaceSunken,
  color: theme.color.textSecondary,
  fontSize: 12,
  lineHeight: 1.45,
};

const configPickerRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "end",
  gap: 10,
  marginBottom: 12,
  flexWrap: "wrap",
};

const rawToggleRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  marginBottom: 8,
};

const rawModeToggleStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
};

const rawTextareaStyle: React.CSSProperties = {
  ...theme.input,
  ...theme.mono,
  width: "100%",
  minHeight: 360,
  resize: "vertical",
  fontSize: 12,
  lineHeight: 1.5,
  whiteSpace: "pre",
};

const rawHintStyle: React.CSSProperties = {
  marginTop: 8,
  fontSize: 11,
  color: theme.color.textMuted,
  lineHeight: 1.45,
};

const selectStyle: React.CSSProperties = {
  ...theme.input,
};

const addBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
};

const newConfigBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  fontSize: 12,
  height: 34,
  padding: "0 12px",
};

const simulatedBadgeStyle: React.CSSProperties = {
  marginLeft: 8,
  padding: "1px 6px",
  borderRadius: 4,
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: 0.4,
  textTransform: "uppercase",
  fontFamily: theme.font.ui,
  color: theme.color.warningText,
  background: theme.color.warningBg,
  border: `1px solid ${theme.color.warningBorder}`,
};

const removeBtnStyle: React.CSSProperties = {
  ...theme.btn.danger,
  ...theme.btnSmall,
  fontSize: 11,
  padding: "2px 10px",
};

const clearBtnStyle: React.CSSProperties = {
  ...theme.btn.ghost,
  ...theme.btnSmall,
  fontSize: 11,
  padding: "4px 8px",
};

const filenameInputStyle: React.CSSProperties = {
  ...theme.input,
  ...theme.mono,
  flex: 1,
};

const saveErrorStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginTop: 12,
};

const discardBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
};
