import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { gantryApi } from "../../api/client";
import * as theme from "../../theme";
import type {
  Coordinate3D,
  DeckConfig,
  DeckResponse,
  GantryPosition,
  GantryResponse,
  InstrumentConfig,
  LabwareConfig,
} from "../../types";
import JogPanel, { MIN_JOG_STEP } from "../gantry/JogPanel";
import { createJogPacer, jogPaceMs } from "../gantry/jogPacing";

interface Props {
  open: boolean;
  onClose: () => void;
  deck: DeckResponse | null;
  gantry: GantryResponse | null;
  position: GantryPosition | null;
  onSaveDeck: (filename: string, config: DeckConfig) => Promise<void>;
  initialMode?: "new" | "calibrate";
}

// Deck coordinates are the zero-offset instrument frame: the engine resolves
// gantry targets as x − offset_x, y − offset_y, z + depth + tip_extension
// (cubos.gantry.instrument_mount.InstrumentedGantry.move and
// cubos.validation.bounds agree on this), so a raw position captured with a
// specific instrument maps back by ADDING its XY offsets and SUBTRACTING
// its depth/tip terms.
//
// Those terms are snapshotted here at capture time, not re-read from live
// state when resolving/saving: step 1's "Back" button returns to step 0
// without clearing already-recorded targets, so a reference-instrument or
// tip change after recording some positions must not retroactively change
// what those positions mean.
type CapturedEntry =
  | { raw: Coordinate3D; offsetX: number; offsetY: number; zCompensation: number; instrument: string }
  | { keep: true };

type ResolvedTarget = { point: Coordinate3D; kept: boolean };

type Target = {
  id: string;
  label: string;
  hint: string;
  stored: Coordinate3D | null;
};

const TYPE_LABELS: Record<string, string> = {
  tip_rack: "Tip rack",
  well_plate: "Well plate",
  vial: "Vial",
  vial_grid: "Vial grid",
  vial_holder: "Vial holder",
  well_plate_holder: "Well plate holder",
  tip_disposal: "Tip disposal",
};

// Labware calibrated by two reference points (A1 anchor + A2 pitch/axis).
const TWO_POINT_TYPES = new Set(["well_plate", "vial_grid", "tip_rack"]);

const TEMPLATE_PREFIX = "new:";

type LabwareTemplate = { id: string; label: string; config: Record<string, unknown> };

// Standard labware the modal can add to the deck. Values mirror the backend
// labware definitions registry (cubos/deck/labware/definitions) where an
// entry exists — keep them in sync until the registry is exposed over the
// API and these can be fetched instead. Everything is editable in the deck
// editor afterwards; calibration always comes from the recorded positions.
const LABWARE_TEMPLATES: Record<string, LabwareTemplate[]> = {
  well_plate: [
    {
      id: "sbs_96",
      label: "96-well SBS plate",
      config: { type: "well_plate", model_name: "sbs_96_wellplate", rows: 8, columns: 12, x_offset: 9, y_offset: 9, length: 127.76, width: 85.47, height: 14.35, well_depth: 10.67, capacity_ul: 200, working_volume_ul: 150 },
    },
    {
      id: "sbs_384",
      label: "384-well SBS plate",
      config: { type: "well_plate", model_name: "sbs_384_wellplate", rows: 16, columns: 24, x_offset: 4.5, y_offset: 4.5, length: 127.76, width: 85.47, capacity_ul: 112, working_volume_ul: 80 },
    },
    {
      id: "sbs_24",
      label: "24-well plate",
      config: { type: "well_plate", model_name: "sbs_24_wellplate", rows: 4, columns: 6, x_offset: 19, y_offset: 19, length: 127.76, width: 85.47, height: 20.2, well_depth: 17.4, capacity_ul: 3400, working_volume_ul: 2000 },
    },
    {
      id: "sbs_6",
      label: "6-well plate",
      config: { type: "well_plate", model_name: "sbs_6_wellplate", rows: 2, columns: 3, x_offset: 39.12, y_offset: 39.12, length: 127.76, width: 85.47, capacity_ul: 16800, working_volume_ul: 3000 },
    },
    {
      id: "ursa_12",
      label: "12-well plate (Ursa plate)",
      config: { type: "well_plate", model_name: "ursa_12_well_plate", rows: 3, columns: 4, x_offset: 15, y_offset: 15, length: 127.76, width: 85.47 },
    },
  ],
  tip_rack: [
    {
      id: "ursa_2x15",
      label: "Ursa 2×15 tip rack (8.5 mm pitch)",
      config: { type: "tip_rack", model_name: "panda_2x15_tip_rack", rows: 15, columns: 2, x_offset: 8.5, y_offset: 8.5, tip_length: 59.3 },
    },
    {
      id: "tips_96",
      label: "96-tip rack (SBS, 9 mm pitch)",
      config: { type: "tip_rack", model_name: "96_tip_rack", rows: 8, columns: 12, x_offset: 9, y_offset: 9, tip_length: 59.3 },
    },
  ],
  vial: [
    {
      id: "scint_20ml",
      label: "20 mL scintillation vial",
      config: { type: "vial", model_name: "scint_vial_20ml", height: 57, diameter: 28, capacity_ul: 20000, working_volume_ul: 18000 },
    },
  ],
};

const STEP_LABELS = ["Select labware", "Adjust positions", "Review & save"];
const NEW_STEP_LABELS = ["Define labware", "Adjust positions", "Review & save"];
const RESERVED_LABWARE_KEYS = new Set(["__proto__", "constructor", "prototype"]);

export default function LabwareCalibrationModal({
  open,
  onClose,
  deck,
  gantry,
  position,
  onSaveDeck,
  initialMode = "calibrate",
}: Props) {
  const [mode, setMode] = useState<"new" | "calibrate">(initialMode);
  const [step, setStep] = useState(0);
  const [labwareType, setLabwareType] = useState("");
  const [labwareChoice, setLabwareChoice] = useState("");
  const [labwareName, setLabwareName] = useState("");
  const [customRows, setCustomRows] = useState("");
  const [customColumns, setCustomColumns] = useState("");
  const [customXOffset, setCustomXOffset] = useState("");
  const [customYOffset, setCustomYOffset] = useState("");
  const [referenceInstrument, setReferenceInstrument] = useState("");
  const [withTip, setWithTip] = useState(false);
  const [tipLength, setTipLength] = useState("");
  const [captured, setCaptured] = useState<Record<string, CapturedEntry>>({});
  const [xyStep, setXyStep] = useState("0.5");
  const [zStep, setZStep] = useState("0.1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusNote, setStatusNote] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const previousOpen = useRef(false);

  const labwareEntries = useMemo(() => deck?.labware ?? [], [deck]);
  const typeOptions = useMemo(() => {
    const present = new Set<string>(labwareEntries.map((item) => item.config.type));
    const known = Object.keys(TYPE_LABELS).filter(
      (type) => present.has(type) || (LABWARE_TEMPLATES[type]?.length ?? 0) > 0,
    );
    const unknown = [...present].filter((type) => !(type in TYPE_LABELS)).sort();
    return [...known, ...unknown];
  }, [labwareEntries]);
  const itemsOfType = useMemo(
    () => labwareEntries.filter((item) => item.config.type === labwareType),
    [labwareEntries, labwareType],
  );
  const templatesOfType = LABWARE_TEMPLATES[labwareType] ?? [];
  const selectedItem = labwareChoice.startsWith(TEMPLATE_PREFIX)
    ? null
    : labwareEntries.find((item) => item.key === labwareChoice) ?? null;
  const selectedTemplate = labwareChoice.startsWith(TEMPLATE_PREFIX)
    ? templatesOfType.find((template) => template.id === labwareChoice.slice(TEMPLATE_PREFIX.length)) ?? null
    : null;
  const trimmedName = labwareName.trim();
  const newKey = labwareKeyFromName(trimmedName);
  const reservedName = RESERVED_LABWARE_KEYS.has(trimmedName.toLowerCase()) || RESERVED_LABWARE_KEYS.has(newKey);
  const keyCollision = !!newKey && (
    reservedName ||
    labwareEntries.some((item) => labwareKeyFromName(item.key) === newKey)
  );
  const parsedCustomRows = Number(customRows);
  const parsedCustomColumns = Number(customColumns);
  const parsedCustomXOffset = Number(customXOffset);
  const parsedCustomYOffset = Number(customYOffset);
  const customGeometryValid = mode === "new" &&
    trimmedName.length > 0 && !!newKey && !keyCollision &&
    isPositiveInteger(parsedCustomRows) && isPositiveInteger(parsedCustomColumns) &&
    isPositiveFinite(parsedCustomXOffset) && isPositiveFinite(parsedCustomYOffset);
  const customConfig = useMemo<LabwareConfig | null>(() => mode === "new" && customGeometryValid
    ? {
      type: "well_plate",
      name: trimmedName,
      model_name: "",
      rows: parsedCustomRows,
      columns: parsedCustomColumns,
      calibration: { a1: null, a2: null as unknown as Coordinate3D },
      x_offset: parsedCustomXOffset,
      y_offset: parsedCustomYOffset,
    }
    : null,
  [customGeometryValid, mode, parsedCustomColumns, parsedCustomRows, parsedCustomXOffset, parsedCustomYOffset, trimmedName]);
  const activeConfig: LabwareConfig | null = useMemo(() => {
    if (mode === "new") return customConfig;
    if (selectedItem) return selectedItem.config;
    if (!selectedTemplate) return null;
    return { ...selectedTemplate.config, name: trimmedName || selectedTemplate.label } as unknown as LabwareConfig;
  }, [customConfig, mode, selectedItem, selectedTemplate, trimmedName]);
  const selectedLabel = selectedItem?.key ?? (selectedTemplate ? `new ${selectedTemplate.label}` : null);

  const instruments = useMemo(
    () => Object.entries(gantry?.config.instruments ?? {}),
    [gantry],
  );
  const isMulti = instruments.length > 1;
  const selectedInstrumentName = referenceInstrument || instruments[0]?.[0] || "";
  const selectedInstrument: InstrumentConfig | null =
    gantry?.config.instruments[selectedInstrumentName] ?? null;
  const isPipette = selectedInstrument?.type === "pipette";
  const tipApplies = isPipette && withTip;

  const defaultTipLength = useMemo(() => {
    const active = activeConfig?.type === "tip_rack" ? activeConfig : null;
    const rack = active ?? labwareEntries.find((item) => item.config.type === "tip_rack")?.config ?? null;
    const length = rack ? Number((rack as Record<string, unknown>).tip_length) : Number.NaN;
    return Number.isFinite(length) && length > 0 ? length : null;
  }, [activeConfig, labwareEntries]);

  const connected = position?.connected ?? false;
  const targets = useMemo(
    () => (activeConfig ? targetsForLabware(activeConfig) : []),
    [activeConfig],
  );
  const nextTarget = targets.find((target) => !captured[target.id]) ?? null;
  const allCaptured = targets.length > 0 && !nextTarget;
  const anyRecorded = targets.some((target) => {
    const entry = captured[target.id];
    return !!entry && "raw" in entry;
  });

  const parsedTipLength = tipApplies ? Number(tipLength) : 0;
  const tipLengthInvalid = tipApplies && (!tipLength.trim() || !Number.isFinite(parsedTipLength) || parsedTipLength <= 0);

  const offsetX = Number(selectedInstrument?.offset_x ?? 0) || 0;
  const offsetY = Number(selectedInstrument?.offset_y ?? 0) || 0;
  const depth = Number(selectedInstrument?.depth ?? 0) || 0;
  const zCompensation = depth + (tipApplies && !tipLengthInvalid ? parsedTipLength : 0);

  // Inverse of InstrumentedGantry.move's gantry = (x − offset_x, y − offset_y,
  // z + depth + tip): deck XY adds the offsets back, deck Z subtracts them.
  const adjust = (raw: Coordinate3D, oX: number, oY: number, zComp: number): Coordinate3D => ({
    x: roundMm(raw.x + oX),
    y: roundMm(raw.y + oY),
    z: roundMm(raw.z - zComp),
  });

  const resolveTarget = (target: Target): ResolvedTarget | null => {
    const entry = captured[target.id];
    if (!entry) return null;
    if ("keep" in entry) {
      return target.stored ? { point: target.stored, kept: true } : null;
    }
    // Use the offset/instrument snapshotted at capture time, not the
    // (possibly since-changed) live selection — see CapturedEntry.
    return { point: adjust(entry.raw, entry.offsetX, entry.offsetY, entry.zCompensation), kept: false };
  };

  useEffect(() => {
    const wasOpen = previousOpen.current;
    previousOpen.current = open;
    if (!open || wasOpen) return;
    setStep(0);
    setMode(initialMode);
    setLabwareType("");
    setLabwareChoice("");
    setLabwareName("");
    setCustomRows("");
    setCustomColumns("");
    setCustomXOffset("");
    setCustomYOffset("");
    setReferenceInstrument("");
    setWithTip(false);
    setTipLength("");
    setCaptured({});
    setBusy(false);
    setError(null);
    setStatusNote(null);
  }, [initialMode, open]);

  useEffect(() => {
    if (mode !== "new") return;
    setCaptured({});
  }, [customRows, customColumns, customXOffset, customYOffset, mode]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const focusTimer = window.setTimeout(() => {
      dialogRef.current?.focus();
    }, 0);
    return () => {
      window.clearTimeout(focusTimer);
      previousFocusRef.current?.focus();
    };
  }, [open]);

  const handleDialogKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      if (!busy) onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = getFocusableElements(dialog);
    if (focusable.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const activeIsFocusable = active ? focusable.includes(active) : false;
    if (event.shiftKey && (!activeIsFocusable || active === first)) {
      event.preventDefault();
      last.focus();
      return;
    }
    if (!event.shiftKey && (!activeIsFocusable || active === last)) {
      event.preventDefault();
      first.focus();
    }
  };

  const selectLabware = (choice: string) => {
    setLabwareChoice(choice);
    if (choice.startsWith(TEMPLATE_PREFIX)) {
      setLabwareName("");
    } else {
      const item = labwareEntries.find((entry) => entry.key === choice) ?? null;
      setLabwareName(item ? String((item.config as Record<string, unknown>).name ?? item.key) : "");
    }
    setCaptured({});
    setError(null);
  };

  const selectType = (type: string) => {
    setLabwareType(type);
    const items = labwareEntries.filter((item) => item.config.type === type);
    selectLabware(items.length === 1 ? items[0].key : "");
  };

  const toggleWithTip = (checked: boolean) => {
    setWithTip(checked);
    if (checked && !tipLength.trim() && defaultTipLength != null) {
      setTipLength(String(defaultTipLength));
    }
  };

  const beginAdjust = () => {
    if (!activeConfig) {
      setError(mode === "new" ? "Complete the labware definition first." : "Select the labware to calibrate first.");
      return;
    }
    if (mode === "new" && !customGeometryValid) {
      setError("Enter a name, positive integer rows and columns, and positive finite X/Y well spacing in mm.");
      return;
    }
    if (selectedTemplate) {
      if (!trimmedName || !newKey) {
        setError("Name the new labware before continuing.");
        return;
      }
      if (keyCollision) {
        setError(`"${newKey}" already exists on this deck - pick another name.`);
        return;
      }
    }
    if (tipLengthInvalid) {
      setError("Enter the attached tip length in mm (greater than 0).");
      return;
    }
    setError(null);
    setStatusNote(null);
    setStep(1);
  };

  // Hold-to-jog pump, mirroring GantryPositionWidget: distinct presses fire
  // immediately, held repeats are paced to the segment's execution time,
  // and release cancels whatever GRBL still has queued.
  const jogHeld = useRef<{ x: number; y: number; z: number } | null>(null);
  const jogPumpActive = useRef(false);
  const jogRequestCount = useRef(0);
  const jogPacer = useRef(createJogPacer()).current;

  const jog = useCallback(async (x: number, y: number, z: number): Promise<boolean> => {
    if (!connected || busy) return false;
    jogRequestCount.current += 1;
    try {
      await gantryApi.jog(x, y, z);
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    }
  }, [busy, connected]);

  // The pump reads jog through a ref so an in-flight hold always uses the
  // latest guards (connected/busy) instead of a stale closure.
  const jogRef = useRef(jog);
  useEffect(() => {
    jogRef.current = jog;
  }, [jog]);

  const stopJog = useCallback(() => {
    if (jogHeld.current === null) return;
    jogHeld.current = null;
    if (jogRequestCount.current > 1) {
      gantryApi.jogCancel().catch(() => undefined);
    }
    jogRequestCount.current = 0;
    jogPacer.wake();
  }, [jogPacer]);

  const startJog = useCallback((x: number, y: number, z: number) => {
    const heldBefore = jogHeld.current !== null;
    jogHeld.current = { x, y, z };
    if (heldBefore && jogPumpActive.current) {
      jogPacer.wake();
      return;
    }
    if (!jogPumpActive.current) {
      jogRequestCount.current = 0;
    }
    const first = jogRef.current(x, y, z);
    if (jogPumpActive.current) return;
    jogPumpActive.current = true;
    const pump = async () => {
      try {
        let sent = { x, y, z };
        let ok = await first;
        while (ok && jogHeld.current) {
          await jogPacer.sleep(jogPaceMs(sent.x, sent.y, sent.z));
          const delta = jogHeld.current;
          if (!delta) break;
          sent = delta;
          ok = await jogRef.current(delta.x, delta.y, delta.z);
        }
        if (!ok) {
          jogHeld.current = null;
        }
      } finally {
        jogPumpActive.current = false;
      }
    };
    void pump();
  }, [jogPacer]);

  useEffect(() => () => stopJog(), [stopJog]);

  const recordTarget = async (target: Target) => {
    setBusy(true);
    setError(null);
    try {
      const result = await gantryApi.getPosition();
      const raw = requireWorkPosition(result);
      // Snapshot the offset/instrument active right now — not read live
      // later — so a later Back + instrument/tip change can't retroactively
      // change what this position means (see CapturedEntry).
      setCaptured((prev) => ({
        ...prev,
        [target.id]: { raw, offsetX, offsetY, zCompensation, instrument: selectedInstrumentName },
      }));
      setStatusNote(
        `Recorded ${target.label}: X=${raw.x.toFixed(3)} Y=${raw.y.toFixed(3)} Z=${raw.z.toFixed(3)} (gantry frame, ` +
        `${selectedInstrumentName}${tipApplies ? " + tip" : ""}).`,
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const keepStored = (target: Target) => {
    setCaptured((prev) => ({ ...prev, [target.id]: { keep: true } }));
    setStatusNote(`${target.label} keeps its saved value.`);
  };

  const clearTarget = (target: Target) => {
    setCaptured((prev) => {
      const next = { ...prev };
      delete next[target.id];
      return next;
    });
  };

  const save = async () => {
    if (!deck || !activeConfig) return;
    setBusy(true);
    setError(null);
    try {
      const labware: Record<string, LabwareConfig> = {};
      for (const item of deck.labware) {
        Object.defineProperty(labware, item.key, {
          value: item.config,
          enumerable: true,
          configurable: true,
          writable: true,
        });
      }
      const targetKey = selectedItem ? selectedItem.key : newKey;
      if (!targetKey || RESERVED_LABWARE_KEYS.has(targetKey)) {
        throw new Error("Choose a labware name that does not normalize to a reserved key.");
      }
      const base = selectedItem && trimmedName
        ? ({ ...(selectedItem.config as unknown as Record<string, unknown>), name: trimmedName } as unknown as LabwareConfig)
        : activeConfig;
      if (!selectedItem && Object.keys(labware).some((key) => labwareKeyFromName(key) === targetKey)) {
        throw new Error(`"${trimmedName}" conflicts with existing labware on this deck.`);
      }
      Object.defineProperty(labware, targetKey, {
        value: buildUpdatedLabware(base, targets, resolveTarget),
        enumerable: true,
        configurable: true,
        writable: true,
      });
      await onSaveDeck(deck.filename, { labware });
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  const parsedXyStep = parsePositiveStep(xyStep);
  const parsedZStep = parsePositiveStep(zStep);
  const xy = parsedXyStep == null ? MIN_JOG_STEP : Math.max(MIN_JOG_STEP, parsedXyStep);
  const z = parsedZStep == null ? MIN_JOG_STEP : Math.max(MIN_JOG_STEP, parsedZStep);
  const stepInvalid = parsedXyStep == null || parsedZStep == null;

  const compensationSummary = [
    `XY offset +(${formatMm(offsetX)}, ${formatMm(offsetY)})`,
    `depth −${formatMm(depth)}`,
    ...(tipApplies && !tipLengthInvalid ? [`tip −${formatMm(parsedTipLength)}`] : []),
  ].join(", ");

  return (
    <div
      ref={dialogRef}
      style={overlayStyle}
      role="dialog"
      aria-modal="true"
      aria-label="Labware calibration"
      tabIndex={-1}
      onKeyDown={handleDialogKeyDown}
    >
      <div style={modalStyle}>
        <div style={headerStyle}>
          <div>
            <h2 style={{ margin: 0, fontSize: 18, color: theme.color.ink, letterSpacing: "-0.01em" }}>{mode === "new" ? "Create custom labware" : "Calibrate labware"}</h2>
            <div style={{ marginTop: 3, fontSize: 12, color: theme.color.textMuted }}>
              {deck?.filename ?? "No deck loaded"}
              {selectedLabel ? ` · ${selectedLabel}` : ""}
            </div>
          </div>
          <button onClick={onClose} style={closeButtonStyle} aria-label="Close labware calibration">
            ×
          </button>
        </div>

        <div style={bodyStyle}>
          <aside style={stepsStyle}>
            {(mode === "new" ? NEW_STEP_LABELS : STEP_LABELS).map((label, index) => (
              <div
                key={label}
                style={index === step ? activeStepStyle : index < step ? completedStepStyle : stepItemStyle}
                aria-current={index === step ? "step" : undefined}
              >
                <span style={stepNumberStyle}>{index + 1}</span>
                {label}
              </div>
            ))}
          </aside>

          <section style={contentStyle}>
            {error && <div style={errorStyle}>{error}</div>}
            {statusNote && <div style={noteStyle}>{statusNote}</div>}

            {step === 0 && (
              <div>
                <h3 style={sectionTitleStyle}>{mode === "new" ? "Define labware" : "Select Labware"}</h3>
                <p style={instructionStyle}>
                  {mode === "new"
                    ? "Define your labware’s layout, then calibrate its position. It will be saved in the current deck configuration."
                    : <>Pick the labware to calibrate, then choose which instrument you will position over it. Recorded positions are converted into the deck frame using that instrument&apos;s configured offsets.</>}
                </p>
                {mode !== "new" && labwareEntries.length === 0 && (
                  <div style={noteStyle}>No labware on this deck yet - pick a type and add one from a template below.</div>
                )}
                <div style={fieldRowStyle}>
                  {mode === "new" ? (
                    <>
                      <label style={fieldStyle}>
                        <span style={labelStyle}>Labware name</span>
                        <input value={labwareName} onChange={(event) => { setLabwareName(event.target.value); setCaptured({}); }} disabled={busy} placeholder="e.g. custom_plate" style={{ ...inputStyle, borderColor: keyCollision ? theme.color.danger : theme.color.borderStrong }} />
                      </label>
                      <label style={fieldStyle}>
                        <span style={labelStyle}>Rows</span>
                        <input value={customRows} onChange={(event) => setCustomRows(event.target.value)} disabled={busy} inputMode="numeric" style={{ ...inputStyle, borderColor: customRows && !isPositiveInteger(parsedCustomRows) ? theme.color.danger : theme.color.borderStrong }} />
                      </label>
                      <label style={fieldStyle}>
                        <span style={labelStyle}>Columns</span>
                        <input value={customColumns} onChange={(event) => setCustomColumns(event.target.value)} disabled={busy} inputMode="numeric" style={{ ...inputStyle, borderColor: customColumns && !isPositiveInteger(parsedCustomColumns) ? theme.color.danger : theme.color.borderStrong }} />
                      </label>
                      <label style={fieldStyle}>
                        <span style={labelStyle}>Well spacing X (mm)</span>
                        <input value={customXOffset} onChange={(event) => setCustomXOffset(event.target.value)} disabled={busy} inputMode="decimal" style={{ ...inputStyle, borderColor: customXOffset && !isPositiveFinite(parsedCustomXOffset) ? theme.color.danger : theme.color.borderStrong }} />
                      </label>
                      <label style={fieldStyle}>
                        <span style={labelStyle}>Well spacing Y (mm)</span>
                        <input value={customYOffset} onChange={(event) => setCustomYOffset(event.target.value)} disabled={busy} inputMode="decimal" style={{ ...inputStyle, borderColor: customYOffset && !isPositiveFinite(parsedCustomYOffset) ? theme.color.danger : theme.color.borderStrong }} />
                      </label>
                    </>
                  ) : null}
                  {mode !== "new" && <>
                  <label style={fieldStyle}>
                    <span style={labelStyle}>Labware type</span>
                    <select
                      value={labwareType}
                      onChange={(event) => selectType(event.target.value)}
                      disabled={busy || !deck}
                      style={inputStyle}
                    >
                      <option value="">Choose type…</option>
                      {typeOptions.map((type) => (
                        <option key={type} value={type}>{TYPE_LABELS[type] ?? type}</option>
                      ))}
                    </select>
                  </label>
                  {labwareType && (
                    <label style={fieldStyle}>
                      <span style={labelStyle}>Labware</span>
                      <select
                        value={labwareChoice}
                        onChange={(event) => selectLabware(event.target.value)}
                        disabled={busy}
                        style={inputStyle}
                      >
                        <option value="">Choose labware…</option>
                        {itemsOfType.length > 0 && (
                          <optgroup label="On deck">
                            {itemsOfType.map((item) => (
                              <option key={item.key} value={item.key}>{item.key}</option>
                            ))}
                          </optgroup>
                        )}
                        {templatesOfType.length > 0 && (
                          <optgroup label="Add new">
                            {templatesOfType.map((template) => (
                              <option key={template.id} value={`${TEMPLATE_PREFIX}${template.id}`}>
                                {template.label}
                              </option>
                            ))}
                          </optgroup>
                        )}
                      </select>
                    </label>
                  )}
                  {labwareChoice && (
                    <label style={fieldStyle}>
                      <span style={labelStyle}>Name</span>
                      <input
                        value={labwareName}
                        onChange={(event) => setLabwareName(event.target.value)}
                        disabled={busy}
                        placeholder={selectedTemplate ? "e.g. plate_2" : undefined}
                        style={{
                          ...inputStyle,
                          borderColor: selectedTemplate && (!trimmedName || keyCollision) ? theme.color.danger : undefined,
                        }}
                      />
                    </label>
                  )}
                  {isMulti && (
                    <label style={fieldStyle}>
                      <span style={labelStyle}>Reference instrument</span>
                      <select
                        value={selectedInstrumentName}
                        onChange={(event) => setReferenceInstrument(event.target.value)}
                        disabled={busy}
                        style={inputStyle}
                      >
                        {instruments.map(([name, config]) => (
                          <option key={name} value={name}>{name} ({config.type})</option>
                        ))}
                      </select>
                    </label>
                  )}
                  </>}
                  {mode === "new" && isMulti && (
                    <label style={fieldStyle}>
                      <span style={labelStyle}>Reference instrument</span>
                      <select
                        value={selectedInstrumentName}
                        onChange={(event) => setReferenceInstrument(event.target.value)}
                        disabled={busy}
                        style={inputStyle}
                      >
                        {instruments.map(([name, config]) => (
                          <option key={name} value={name}>{name} ({config.type})</option>
                        ))}
                      </select>
                    </label>
                  )}
                </div>
                {mode === "new" && <p style={instructionStyle}>
                  Enter center-to-center spacing from the plate specification or your measurements.
                  Calibration records the position and orientation; it does not measure both spacings.
                  Rows and columns must be positive whole numbers.
                </p>}
                {mode === "new" && keyCollision && <div style={errorStyle}>That name conflicts with existing labware or a reserved key.</div>}
                {isPipette && (
                  <div style={tipBoxStyle}>
                    <label style={checkboxRowStyle}>
                      <input
                        type="checkbox"
                        checked={withTip}
                        onChange={(event) => toggleWithTip(event.target.checked)}
                        disabled={busy}
                      />
                      <span>Calibrate with a tip attached</span>
                    </label>
                    {withTip && (
                      <label style={{ ...fieldStyle, maxWidth: 180 }}>
                        <span style={labelStyle}>Tip length (mm)</span>
                        <input
                          value={tipLength}
                          onChange={(event) => setTipLength(event.target.value)}
                          disabled={busy}
                          inputMode="decimal"
                          style={{ ...inputStyle, borderColor: tipLengthInvalid ? theme.color.danger : undefined }}
                        />
                      </label>
                    )}
                    <div style={{ fontSize: 12, color: theme.color.textMuted }}>
                      {withTip
                        ? "Touch the tip end to each position; the tip length is subtracted from recorded Z."
                        : "Touch the bare nozzle to each position."}
                    </div>
                  </div>
                )}
                {selectedTemplate && (
                  <div style={noteStyle}>
                    {keyCollision
                      ? `"${newKey}" already exists on this deck - pick another name.`
                      : `Saving adds this ${selectedTemplate.label} to the deck as "${newKey || "…"}".`}
                  </div>
                )}
                {activeConfig && (
                  <div style={summaryGridStyle}>
                    <Readout label="Positions to adjust" value={targets.map((t) => t.label).join(", ")} />
                    <Readout label="Reference instrument" value={`${selectedInstrumentName || "None"}${selectedInstrument ? ` (${selectedInstrument.type})` : ""}`} />
                  </div>
                )}
                <div style={actionRowStyle}>
                  <button
                    onClick={beginAdjust}
                    disabled={busy || !activeConfig}
                    style={buttonStateStyle(primaryButtonStyle, busy || !activeConfig)}
                  >
                    Continue
                  </button>
                </div>
              </div>
            )}

            {step === 1 && activeConfig && (
              <div>
                <h3 style={sectionTitleStyle}>Adjust Positions</h3>
                <p style={instructionStyle}>
                  Manually adjust each position below with {selectedInstrumentName || "the instrument"}
                  {tipApplies ? " (tip attached)" : ""}, then record it. Positions you do not
                  want to change can keep their saved value.
                </p>
                {!connected && (
                  <div style={warningStyle}>
                    Gantry is not connected — connect and home before recording positions.
                  </div>
                )}
                <div style={targetListStyle}>
                  {targets.map((target) => {
                    const entry = captured[target.id];
                    const resolved = resolveTarget(target);
                    return (
                      <div key={target.id} style={targetRowStyle}>
                        <strong>{target.label}</strong>
                        <span style={{ ...theme.mono, color: theme.color.textMuted, fontSize: 12 }}>
                          {entry && resolved
                            ? `${resolved.point.x.toFixed(3)}, ${resolved.point.y.toFixed(3)}, ${formatZ(resolved.point.z)}${resolved.kept ? " (saved)" : ""}`
                            : target === nextTarget
                              ? "ready"
                              : "pending"}
                        </span>
                        {entry && (
                          <button onClick={() => clearTarget(target)} disabled={busy} style={smallButtonStyle}>
                            Redo
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
                {nextTarget ? (
                  <div style={activeTargetStyle}>
                    <div style={{ marginBottom: 10 }}>
                      <span style={labelStyle}>Now adjusting</span>
                      <h4 style={{ margin: "2px 0 0", fontSize: 15, color: theme.color.ink }}>{nextTarget.label}</h4>
                      <p style={{ ...instructionStyle, margin: "8px 0 0" }}>{nextTarget.hint}</p>
                      {nextTarget.stored && (
                        <div style={{ ...theme.mono, fontSize: 12, color: theme.color.textFaint, marginTop: 6 }}>
                          Saved: {nextTarget.stored.x.toFixed(3)}, {nextTarget.stored.y.toFixed(3)}, {formatZ(nextTarget.stored.z)}
                        </div>
                      )}
                    </div>
                    <JogPanel
                      xyStep={xyStep}
                      zStep={zStep}
                      setXyStep={setXyStep}
                      setZStep={setZStep}
                      disabled={!connected || busy}
                      alarmed={false}
                      onStartJog={startJog}
                      onStopJog={stopJog}
                      xy={xy}
                      z={z}
                      stepInvalid={stepInvalid}
                      xyStepInvalid={parsedXyStep == null}
                      zStepInvalid={parsedZStep == null}
                      xyBelowMin={parsedXyStep != null && parsedXyStep < MIN_JOG_STEP}
                      zBelowMin={parsedZStep != null && parsedZStep < MIN_JOG_STEP}
                    />
                    <div style={actionRowStyle}>
                      <button
                        onClick={() => void recordTarget(nextTarget)}
                        disabled={busy || !connected}
                        style={buttonStateStyle(primaryButtonStyle, busy || !connected)}
                      >
                        Record {nextTarget.label}
                      </button>
                      {(nextTarget.stored || mode === "new") && (
                        <button onClick={() => keepStored(nextTarget)} disabled={busy || !nextTarget.stored} style={secondaryButtonStyle}>
                          Keep saved value
                        </button>
                      )}
                    </div>
                  </div>
                ) : (
                  <div style={noteStyle}>All positions set.</div>
                )}
                <div style={actionRowStyle}>
                  <button onClick={() => setStep(0)} disabled={busy} style={secondaryButtonStyle}>
                    Back
                  </button>
                  <button
                    onClick={() => setStep(2)}
                    disabled={busy || !allCaptured}
                    style={buttonStateStyle(primaryButtonStyle, busy || !allCaptured)}
                  >
                    Continue
                  </button>
                </div>
              </div>
            )}

            {step === 2 && activeConfig && (
              <div>
                <h3 style={sectionTitleStyle}>Review &amp; Save</h3>
                <p style={instructionStyle}>
                  Saving writes these positions into {deck?.filename} for {selectedItem ? selectedItem.key : `new labware “${newKey}”`}.
                  {anyRecorded && ` Recorded values are converted to the deck frame: ${compensationSummary}.`}
                </p>
                <div style={summaryGridStyle}>
                  {targets.map((target) => {
                    const resolved = resolveTarget(target);
                    if (!resolved) return null;
                    let value = `${resolved.point.x.toFixed(3)}, ${resolved.point.y.toFixed(3)}, ${formatZ(resolved.point.z)}`;
                    if (target.id === "a2") {
                      const a1Target = targets.find((other) => other.id === "a1");
                      const a1Resolved = a1Target ? resolveTarget(a1Target) : null;
                      const snapped = !resolved.kept && a1Resolved
                        ? snappedA2(activeConfig as unknown as Record<string, unknown>, a1Resolved.point, resolved.point)
                        : { x: resolved.point.x, y: resolved.point.y };
                      value = `${snapped.x.toFixed(3)}, ${snapped.y.toFixed(3)}`;
                    }
                    return (
                      <Readout
                        key={target.id}
                        label={`${target.label}${resolved.kept ? " (unchanged)" : ""}`}
                        value={value}
                      />
                    );
                  })}
                </div>
                {TWO_POINT_TYPES.has(activeConfig.type) && hasRecorded(captured, "a2") && (
                  <div style={noteStyle}>
                    A2 is snapped to exactly one well/tip pitch from A1 in the direction
                    you jogged — it only sets the labware&apos;s orientation.
                  </div>
                )}
                <div style={actionRowStyle}>
                  <button onClick={() => setStep(1)} disabled={busy} style={secondaryButtonStyle}>
                    Back
                  </button>
                  <button
                    onClick={() => void save()}
                    disabled={busy || !allCaptured}
                    style={buttonStateStyle(primaryButtonStyle, busy || !allCaptured)}
                  >
                    {busy ? "Saving…" : "Save labware calibration"}
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

function Readout({ label, value }: { label: string; value: string }) {
  return (
    <div style={readoutStyle}>
      <span style={readoutLabelStyle}>{label}</span>
      <span style={readoutValueStyle}>{value}</span>
    </div>
  );
}

function targetsForLabware(config: LabwareConfig): Target[] {
  const record = config as unknown as Record<string, unknown>;
  if (TWO_POINT_TYPES.has(config.type)) {
    const calibration = (record.calibration ?? {}) as Record<string, unknown>;
    const noun = config.type === "tip_rack" ? "tip" : "well";
    // The recorded tip-rack A1 Z is saved directly as pickup_z, and
    // pick_up_tip's descent to that Z IS the press — so the operator must
    // record the fully seated depth, not the tip's top surface.
    const a1Hint = config.type === "tip_rack"
      ? "Press the bare nozzle down into tip A1 until it is fully seated "
        + "(as deep as a real pickup), then record — this Z becomes the "
        + "pickup height. Leave 'with tip attached' off."
      : `Jog until the instrument touches the top center of ${noun} A1.`;
    return [
      {
        id: "a1",
        label: "A1",
        hint: a1Hint,
        stored: pointFrom(calibration.a1),
      },
      {
        id: "a2",
        label: "A2",
        hint: `Jog toward the adjacent ${noun} A2 along its row/column. A2 only sets the labware's orientation: it is saved exactly one pitch from A1 in the direction you jogged. ${config.type === "well_plate" && config.columns === 1 ? "This plate has only one column. Use a reference in the direction a second column would occupy; no extra well is added." : ""}`,
        stored: pointFrom(calibration.a2),
      },
    ];
  }
  const labels: Record<string, string> = {
    vial: "Vial top center",
    tip_disposal: "Drop position",
    vial_holder: "Holder reference",
    well_plate_holder: "Holder reference",
  };
  return [
    {
      id: "location",
      label: labels[config.type] ?? "Location",
      hint: config.type === "tip_disposal"
        ? "Jog over the disposal opening at the height tips should be dropped from."
        : "Jog until the instrument touches the labware's reference point (its configured location).",
      stored: pointFrom(record.location),
    },
  ];
}

function buildUpdatedLabware(
  config: LabwareConfig,
  targets: Target[],
  resolve: (target: Target) => ResolvedTarget | null,
): LabwareConfig {
  const record = { ...(config as unknown as Record<string, unknown>) };
  const byId = new Map(targets.map((target) => [target.id, target]));

  if (TWO_POINT_TYPES.has(config.type)) {
    const a1Target = byId.get("a1");
    const a2Target = byId.get("a2");
    const a1 = a1Target ? resolve(a1Target) : null;
    const a2 = a2Target ? resolve(a2Target) : null;
    if (!a1 || !a2) return config;
    const calibration = { ...((record.calibration ?? {}) as Record<string, unknown>) };
    const previousA1 = pointFrom(calibration.a1);
    if (!a1.kept) {
      calibration.a1 = { x: a1.point.x, y: a1.point.y, z: a1.point.z };
    }
    if (!a2.kept) {
      calibration.a2 = snappedA2(record, a1.point, a2.point);
    }
    record.calibration = calibration;
    if (config.type === "tip_rack" && !a1.kept) {
      record.pickup_z = a1.point.z;
      const previousDrop = Number(record.drop_z);
      if (previousA1 && Number.isFinite(previousA1.z) && Number.isFinite(previousDrop)) {
        record.drop_z = roundMm(previousDrop + (a1.point.z - previousA1.z));
      }
    }
    return record as unknown as LabwareConfig;
  }

  const locationTarget = byId.get("location");
  const resolvedLocation = locationTarget ? resolve(locationTarget) : null;
  if (!resolvedLocation || resolvedLocation.kept) return config;
  const location = resolvedLocation.point;
  const previous = pointFrom(record.location);
  record.location = { x: location.x, y: location.y, z: location.z };
  if (!previous) return record as unknown as LabwareConfig;
  const delta = {
    x: location.x - previous.x,
    y: location.y - previous.y,
    z: Number.isFinite(previous.z) ? location.z - previous.z : 0,
  };
  // Slots and nested labware store absolute coordinates (only nested Z is
  // derived from the holder seat) — shift them with the holder/disposal so
  // its contents stay aligned.
  if (record.slots && typeof record.slots === "object") {
    record.slots = mapRecordValues(record.slots as Record<string, unknown>, (slot) =>
      shiftPointField(slot, "location", delta));
  }
  if (record.vials && typeof record.vials === "object") {
    record.vials = mapRecordValues(record.vials as Record<string, unknown>, (vial) =>
      shiftPointField(vial, "location", { ...delta, z: 0 }));
  }
  if (record.well_plate && typeof record.well_plate === "object") {
    const plate = { ...(record.well_plate as Record<string, unknown>) };
    if (plate.calibration && typeof plate.calibration === "object") {
      plate.calibration = mapRecordValues(plate.calibration as Record<string, unknown>, (point) =>
        shiftPoint(point, { ...delta, z: 0 }));
    }
    record.well_plate = plate;
  }
  return record as unknown as LabwareConfig;
}

function mapRecordValues(
  record: Record<string, unknown>,
  transform: (value: unknown) => unknown,
): Record<string, unknown> {
  const next: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    next[key] = transform(value);
  }
  return next;
}

function shiftPointField(
  value: unknown,
  field: string,
  delta: { x: number; y: number; z: number },
): unknown {
  if (!value || typeof value !== "object") return value;
  const record = value as Record<string, unknown>;
  return { ...record, [field]: shiftPoint(record[field], delta) };
}

function shiftPoint(value: unknown, delta: { x: number; y: number; z: number }): unknown {
  const point = pointFrom(value);
  if (!point) return value;
  const next: Record<string, number> = {
    x: roundMm(point.x + delta.x),
    y: roundMm(point.y + delta.y),
  };
  if (Number.isFinite(point.z)) {
    next.z = roundMm(point.z + delta.z);
  }
  return next;
}

// The deck loader requires A2 to sit exactly one x_offset/y_offset pitch
// from A1 along one axis — A2 only encodes the labware's orientation, so
// snap the recorded point to the pitch step in the jogged direction.
function snappedA2(
  record: Record<string, unknown>,
  a1: Coordinate3D,
  a2: Coordinate3D,
): { x: number; y: number } {
  const dx = a2.x - a1.x;
  const dy = a2.y - a1.y;
  const xPitch = Number(record.x_offset);
  const yPitch = Number(record.y_offset);
  if (Math.abs(dx) > Math.abs(dy)) {
    const pitch = Number.isFinite(xPitch) && xPitch > 0 ? xPitch : Math.abs(dx);
    return { x: roundMm(a1.x + (dx < 0 ? -pitch : pitch)), y: a1.y };
  }
  const pitch = Number.isFinite(yPitch) && yPitch > 0 ? yPitch : Math.abs(dy);
  return { x: a1.x, y: roundMm(a1.y + (dy < 0 ? -pitch : pitch)) };
}

function labwareKeyFromName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function pointFrom(value: unknown): Coordinate3D | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const x = Number(record.x);
  const y = Number(record.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const z = record.z == null ? Number.NaN : Number(record.z);
  return { x, y, z };
}

function hasRecorded(captured: Record<string, CapturedEntry>, id: string): boolean {
  const entry = captured[id];
  return !!entry && "raw" in entry;
}

function requireWorkPosition(position: GantryPosition): Coordinate3D {
  if (!position.connected) throw new Error("Gantry is not connected.");
  if (position.work_x == null || position.work_y == null || position.work_z == null) {
    throw new Error("Work coordinate position is not available. Home the gantry before recording positions.");
  }
  return {
    x: Number(position.work_x),
    y: Number(position.work_y),
    z: Number(position.work_z),
  };
}

function parsePositiveStep(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function isPositiveInteger(value: number): boolean {
  return Number.isSafeInteger(value) && value > 0;
}

function isPositiveFinite(value: number): boolean {
  return Number.isFinite(value) && value > 0;
}

function roundMm(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function formatMm(value: number): string {
  return roundMm(value).toString();
}

function formatZ(z: number): string {
  return Number.isFinite(z) ? z.toFixed(3) : "—";
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function getFocusableElements(root: HTMLElement): HTMLElement[] {
  const selector = [
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "a[href]",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");
  return Array.from(root.querySelectorAll<HTMLElement>(selector))
    .filter((element) => element.getAttribute("aria-hidden") !== "true");
}

function buttonStateStyle(base: React.CSSProperties, disabled: boolean): React.CSSProperties {
  if (!disabled) return base;
  return {
    ...base,
    opacity: 0.45,
    cursor: "not-allowed",
  };
}

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: theme.chrome.backdrop,
  zIndex: 50,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 24,
};

const modalStyle: React.CSSProperties = {
  width: "min(860px, 96vw)",
  maxHeight: "92vh",
  background: theme.color.surface,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.lg,
  boxShadow: theme.shadow.overlay,
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  gap: 12,
  padding: "16px 18px",
  borderBottom: `1px solid ${theme.color.border}`,
};

const bodyStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(140px, 170px) minmax(0, 1fr)",
  minHeight: 0,
  overflow: "hidden",
};

const stepsStyle: React.CSSProperties = {
  padding: 12,
  borderRight: `1px solid ${theme.color.border}`,
  background: theme.color.surfaceMuted,
  display: "flex",
  flexDirection: "column",
  gap: 6,
};

const contentStyle: React.CSSProperties = {
  padding: 18,
  overflow: "auto",
};

const stepItemStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  border: "1px solid transparent",
  background: "transparent",
  color: theme.color.textFaint,
  borderRadius: theme.radius.sm,
  padding: "8px 9px",
  fontSize: 12,
  textAlign: "left",
  cursor: "default",
};

const activeStepStyle: React.CSSProperties = {
  ...stepItemStyle,
  background: theme.color.surface,
  border: `1px solid ${theme.color.accentTintBorder}`,
  color: theme.color.accentText,
  fontWeight: 600,
};

const completedStepStyle: React.CSSProperties = {
  ...stepItemStyle,
  color: theme.color.successText,
  background: theme.color.successBg,
  border: `1px solid ${theme.color.successBorder}`,
};

const stepNumberStyle: React.CSSProperties = {
  width: 20,
  height: 20,
  borderRadius: "50%",
  background: theme.color.border,
  color: theme.color.ink,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 11,
  flexShrink: 0,
};

const sectionTitleStyle: React.CSSProperties = {
  ...theme.panelTitle,
  margin: "0 0 10px",
  fontSize: 16,
};

const instructionStyle: React.CSSProperties = {
  margin: "0 0 12px",
  color: theme.color.textSecondary,
  fontSize: 13,
  lineHeight: 1.45,
};

const fieldRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  gap: 10,
  marginBottom: 12,
};

const fieldStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 3,
  minWidth: 0,
};

const labelStyle: React.CSSProperties = {
  ...theme.fieldLabel,
};

const inputStyle: React.CSSProperties = {
  ...theme.input,
  minWidth: 0,
};

const tipBoxStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 12,
  marginBottom: 12,
  display: "flex",
  flexDirection: "column",
  gap: 8,
};

const checkboxRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  fontSize: 13,
  color: theme.color.text,
};

const summaryGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
  gap: 8,
  marginBottom: 12,
};

const readoutStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.sm,
  padding: "7px 9px",
  minWidth: 0,
};

const readoutLabelStyle: React.CSSProperties = {
  ...theme.sectionLabel,
  display: "block",
  marginBottom: 2,
};

const readoutValueStyle: React.CSSProperties = {
  ...theme.mono,
  display: "block",
  fontSize: 13,
  fontWeight: 600,
  overflowWrap: "anywhere",
};

const actionRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
  marginTop: 12,
};

const primaryButtonStyle: React.CSSProperties = {
  ...theme.btn.primary,
};

const secondaryButtonStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
};

const smallButtonStyle: React.CSSProperties = {
  ...theme.btn.ghost,
  ...theme.btnSmall,
  marginLeft: "auto",
};

const closeButtonStyle: React.CSSProperties = {
  background: "transparent",
  border: `1px solid ${theme.color.borderStrong}`,
  borderRadius: theme.radius.sm,
  color: theme.color.textMuted,
  cursor: "pointer",
  fontSize: 18,
  lineHeight: 1,
  width: 28,
  height: 28,
};

const errorStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginBottom: 10,
};

const warningStyle: React.CSSProperties = {
  ...theme.notice.warning,
  marginBottom: 10,
};

const noteStyle: React.CSSProperties = {
  ...theme.notice.info,
  marginBottom: 10,
};

const targetListStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
  gap: 8,
  marginBottom: 12,
};

const targetRowStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.sm,
  padding: "7px 9px",
  display: "flex",
  alignItems: "center",
  gap: 8,
  fontSize: 12,
  color: theme.color.text,
};

const activeTargetStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 12,
  background: theme.color.surfaceMuted,
  marginBottom: 12,
};
