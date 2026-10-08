import { useState } from "react";
import type {
  CommandInfo,
  CompositionSeedRow,
  DeckResponse,
  FluidSeedRow,
  FluidStateChoice,
  FluidStateSummary,
  GantryResponse,
  LabwareResponse,
  ProtocolStep,
  ProtocolConfig,
  ProtocolResponse,
  ProtocolRunResponse,
  InstrumentMeasurementMethods,
  InstrumentMethodParams,
  InstrumentMethodParamField,
} from "../../types";
import { CoordinateField, NumberField, OptionalNumberField, SaveTargetHint, SavedStatus, TextField, UnsavedNotice } from "./fields";
import { useSaveShortcut } from "./saveHelpers";
import ConfigFilePicker from "./ConfigFilePicker";
import { normalizeYamlFilename } from "./field-utils";
import RawYamlPanel from "./RawYamlPanel";
import { useConfirm } from "../common/useConfirm";
import {
  createCompositionRow,
  createSeedRow,
  validateSeedRows,
  volumeContainerKeys,
} from "../../utils/fluidSeeds";
import * as theme from "../../theme";

const SEED_CONTAINER_LIST_ID = "fluid-seed-container-keys";

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
  commands: CommandInfo[];
  deck: DeckResponse;
  gantry: GantryResponse;
  instrumentMethods?: InstrumentMeasurementMethods;
  instrumentMethodParams?: InstrumentMethodParams;
  steps: ProtocolStep[] | null;
  positions?: Record<string, number[]> | null;
  /** The last-saved (server-loaded) protocol, used to reset local edits
   * when the user discards. */
  baseline?: ProtocolResponse | null;
  onSave: (filename: string, body: ProtocolConfig) => Promise<void> | void;
  /** Called on every local edit so the parent can persist the working
   * copy across tab switches. */
  onLocalChange?: (steps: ProtocolStep[]) => void;
  onPositionsChange?: (positions: Record<string, number[]> | null) => void;
  onValidate: (body: ProtocolConfig) => void;
  validationErrors: string[] | null;
  isValidating: boolean;
  onRefresh: () => void;
  onRun: () => void;
  onCancelRun: () => void;
  /** Names of configs (Gantry/Deck/Protocol) with unsaved local edits.
   * Run is blocked while non-empty because it executes the saved files,
   * not these in-memory edits. */
  unsavedConfigs: string[];
  canRun: boolean;
  runDisabledReason?: string | null;
  isRunning: boolean;
  isCancelingRun: boolean;
  runResult: ProtocolRunResponse | null;
  runError: string | null;
  /** Explicit create-new-state vs resume-existing-state choice (Feature 07).
   * Defaults to `{ mode: "none" }` when the caller doesn't opt in, which
   * keeps every pre-Feature-07 run submission byte-identical. */
  fluidStateChoice?: FluidStateChoice;
  onFluidStateChoiceChange?: (choice: FluidStateChoice) => void;
  availableFluidStates?: FluidStateSummary[];
  activeFluidStateId?: number | null;
}

// Categorical accents for step kinds: movement = indigo accent, liquid
// handling = success emerald, scan keeps a distinct violet (the theme has
// no third categorical hue).
const COMMAND_COLORS: Record<string, string> = {
  move: theme.color.accent,
  aspirate: theme.color.success,
  dispense: theme.color.success,
  blowout: theme.color.success,
  mix: theme.color.success,
  pick_up_tip: theme.color.success,
  drop_tip: theme.color.success,
  scan: theme.categorical.violet,
  cure: theme.categorical.amber,
};

type ProtocolChoices = {
  instruments: string[];
  plates: string[];
  positions: string[];
  vials: string[];
  instrumentTypes: Record<string, string>;
  instrumentMethods: InstrumentMeasurementMethods;
  instrumentMethodParams: InstrumentMethodParams;
};

type EditablePosition = {
  id: string;
  name: string;
  x: number;
  y: number;
  z: number;
};

function defaultArgsForCommand(cmd: CommandInfo, choices: ProtocolChoices): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  for (const a of cmd.args) {
    const contextual = defaultArgValue(a.name, choices, args, cmd.name);
    if (contextual !== undefined) {
      args[a.name] = contextual;
    } else if (a.required) {
      args[a.name] = isNumericType(a.type) ? 0 : "";
    } else if (a.default != null) {
      args[a.name] = a.default;
    }
  }
  if (isAsmiIndentationStep(args, choices)) {
    args.method_kwargs = {
      step_size: 0.1,
      force_limit: 10,
      baseline_samples: 10,
      measure_with_return: false,
    };
  } else {
    const params = methodParamsForStep(args, choices);
    if (params.length > 0) {
      args.method_kwargs = defaultMethodKwargs(params);
    }
  }
  return args;
}

export default function ProtocolEditor({
  configs,
  selectedFile,
  onSelectFile,
  onImportFile,
  onNewFile,
  onDeleteFile,
  deleteDisabledReason,
  lastSaved,
  commands,
  deck,
  gantry,
  instrumentMethods = {},
  instrumentMethodParams = {},
  steps: loadedSteps,
  positions,
  baseline,
  onSave,
  onLocalChange,
  onPositionsChange,
  onValidate,
  validationErrors,
  isValidating,
  onRefresh,
  onRun,
  onCancelRun,
  unsavedConfigs,
  canRun,
  runDisabledReason,
  isRunning,
  isCancelingRun,
  runResult,
  runError,
  fluidStateChoice = { mode: "none", newLabel: "", resumeId: null, seeds: [] },
  onFluidStateChoiceChange,
  availableFluidStates = [],
  activeFluidStateId = null,
}: Props) {
  const [steps, setSteps] = useState<ProtocolStep[]>(() => (
    loadedSteps ? structuredClone(loadedSteps) : []
  ));
  const [positionRows, setPositionRows] = useState<EditablePosition[]>(() => (
    positionsToRows(positions)
  ));
  const [addCommand, setAddCommand] = useState(commands[0]?.name ?? "move");
  const [saveAs, setSaveAs] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [requestConfirm, confirmDialog] = useConfirm();

  const commandsByName = Object.fromEntries(commands.map((c) => [c.name, c]));
  const choices = buildProtocolChoices(deck, gantry, positionRows, instrumentMethods, instrumentMethodParams);
  const positionErrors = validatePositionRows(positionRows);
  const hasPositionErrors = positionErrors.length > 0;

  const commit = (next: ProtocolStep[]) => {
    setSteps(next);
    setSaveError(null);
    onLocalChange?.(next);
  };

  const commitPositions = (next: EditablePosition[]) => {
    setPositionRows(next);
    setSaveError(null);
    onPositionsChange?.(rowsToPositions(next));
  };

  const addStep = () => {
    const cmd = commandsByName[addCommand];
    if (!cmd) return;
    commit([...steps, { command: addCommand, args: defaultArgsForCommand(cmd, choices) }]);
  };

  const removeStep = (i: number) => {
    commit(steps.filter((_, idx) => idx !== i));
  };

  const moveStep = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= steps.length) return;
    const next = [...steps];
    [next[i], next[j]] = [next[j], next[i]];
    commit(next);
  };

  const updateStepArg = (i: number, argName: string, value: unknown) => {
    const next = [...steps];
    const updatedArgs = { ...next[i].args };
    if (value === null) {
      // Optional field cleared by the operator: omit the argument rather
      // than saving an empty string or a stale number.
      delete updatedArgs[argName];
    } else {
      updatedArgs[argName] = value;
    }
    const declaresMethod = commandsByName[next[i].command]?.args.some((a) => a.name === "method") ?? false;
    if (argName === "instrument" && declaresMethod) {
      const methods = measurementMethodsForInstrument(String(value), choices);
      if (methods.length > 0 && !methods.includes(String(updatedArgs.method ?? ""))) {
        updatedArgs.method = methods[0];
      }
    }
    if (argName === "brightness" && value !== null) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) updatedArgs.brightness = parsed;
    }
    if (argName === "channel") {
      const levels = LIGHTING_LEVELS[String(value)];
      if (levels && !levels.includes(Number(updatedArgs.brightness))) {
        updatedArgs.brightness = 0;
      }
    }
    if ((argName === "instrument" || argName === "method") && isAsmiIndentationStep(updatedArgs, choices)) {
      updatedArgs.method_kwargs = {
        step_size: 0.1,
        force_limit: 10,
        baseline_samples: 10,
        measure_with_return: false,
        ...(isRecord(updatedArgs.method_kwargs) ? updatedArgs.method_kwargs : {}),
      };
    }
    if ((argName === "instrument" || argName === "method") && !isAsmiIndentationStep(updatedArgs, choices)) {
      delete updatedArgs.indentation_limit_height;
      const params = methodParamsForStep(updatedArgs, choices);
      if (params.length > 0) {
        updatedArgs.method_kwargs = defaultMethodKwargs(params);
      } else {
        delete updatedArgs.method_kwargs;
      }
    }
    next[i] = { ...next[i], args: updatedArgs };
    commit(next);
  };

  const addPosition = () => {
    commitPositions([
      ...positionRows,
      {
        id: `new:${Date.now()}:${positionRows.length}`,
        name: uniquePositionName(positionRows),
        x: 0,
        y: 0,
        z: 0,
      },
    ]);
  };

  const updatePosition = (id: string, patch: Partial<Omit<EditablePosition, "id">>) => {
    const current = positionRows.find((row) => row.id === id);
    if (!current) return;
    const oldName = current.name.trim();
    const nextRows = positionRows.map((row) => (
      row.id === id ? { ...row, ...patch } : row
    ));
    commitPositions(nextRows);
    if (typeof patch.name === "string") {
      const newName = patch.name.trim();
      if (oldName && newName && oldName !== newName) {
        renamePositionReferences(oldName, newName);
      }
    }
  };

  const removePosition = (id: string) => {
    commitPositions(positionRows.filter((row) => row.id !== id));
  };

  const renamePositionReferences = (oldName: string, newName: string) => {
    let changed = false;
    const next = steps.map((step) => {
      const args = Object.fromEntries(
        Object.entries(step.args).map(([name, value]) => {
          if (isPositionArg(name) && value === oldName) {
            changed = true;
            return [name, newName];
          }
          return [name, value];
        }),
      );
      return changed ? { ...step, args } : step;
    });
    if (changed) commit(next);
  };

  const buildConfig = (): ProtocolConfig => (
    rowsToPositions(positionRows) ? { positions: rowsToPositions(positionRows), protocol: steps } : { protocol: steps }
  );

  const handleValidate = () => onValidate(buildConfig());

  const saveAsFilename = normalizeYamlFilename(saveAs);
  const saveAsExists = !!saveAsFilename && configs.includes(saveAsFilename);

  const handleSave = async () => {
    const normalized = saveAsFilename || selectedFile || "";
    if (!normalized || saving || hasPositionErrors) return;
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
      await Promise.resolve(onSave(normalized, buildConfig()));
      onSelectFile(normalized);
      setSaveAs("");
      setSaveError(null);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleDiscard = async () => {
    const confirmed = await requestConfirm({
      title: "Discard changes?",
      message: "Discard unsaved protocol changes?",
      confirmLabel: "Discard",
      danger: true,
    });
    if (!confirmed) return;
    setSteps(baseline?.steps ? structuredClone(baseline.steps) : []);
    setPositionRows(positionsToRows(baseline?.positions));
    setSaveError(null);
    onRefresh();
  };

  const hasSteps = steps.length > 0;
  const hasUnsaved = unsavedConfigs.length > 0;
  const protocolDirty = unsavedConfigs.includes("Protocol");
  // Deck/Gantry edits are saved in their own tabs, so the protocol-tab
  // banner only points the user there rather than asking them to save
  // those configs from here.
  const otherDirty = unsavedConfigs.filter((name) => name !== "Protocol");
  const canSave = hasSteps && (!!saveAs.trim() || !!selectedFile) && !saving && !hasPositionErrors;
  useSaveShortcut(handleSave, canSave);
  // "new"/"resume" both need an explicit, complete choice before Run is
  // enabled — resume specifically needs a picked state id. "none" (the
  // default) never blocks Run, so every pre-Feature-07 flow is unaffected.
  const stateChoiceIncomplete = (fluidStateChoice.mode === "resume" && fluidStateChoice.resumeId === null)
    || (fluidStateChoice.mode === "active" && activeFluidStateId === null);
  // "new" fluid-state seed rows are validated the same way the server would
  // (Feature 07b): a negative volume, a composition that doesn't sum, or a
  // duplicate container blocks Run with an inline message instead of a 4xx.
  const seedErrors = fluidStateChoice.mode === "new"
    ? validateSeedRows(fluidStateChoice.seeds)
    : [];
  const seedRowsInvalid = seedErrors.length > 0;
  const seedContainerOptions = volumeContainerKeys(deck);
  const runDisabled = isRunning || !hasSteps || !canRun || hasUnsaved || stateChoiceIncomplete || seedRowsInvalid;
  const runButtonTitle = hasUnsaved
    ? "Save your changes before running"
    : stateChoiceIncomplete
      ? fluidStateChoice.mode === "active" ? "Activate a physical setup in Deck contents before running" : "Select a fluid state to resume before running"
      : seedRowsInvalid
        ? "Fix the fluid-state seed rows before running"
        : runDisabledReason ?? undefined;

  // Seed-row mutations flow up through onFluidStateChoiceChange so the
  // parent (App) stays the single owner of the choice. Each helper rebuilds
  // the choice immutably; they no-op when the callback isn't wired.
  const updateSeeds = (nextSeeds: FluidSeedRow[]) => {
    onFluidStateChoiceChange?.({ ...fluidStateChoice, seeds: nextSeeds });
  };
  const addSeedRow = () => updateSeeds([...fluidStateChoice.seeds, createSeedRow()]);
  const patchSeedRow = (id: string, patch: Partial<Omit<FluidSeedRow, "id">>) => {
    updateSeeds(fluidStateChoice.seeds.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  };
  const removeSeedRow = (id: string) => {
    updateSeeds(fluidStateChoice.seeds.filter((row) => row.id !== id));
  };
  const addCompositionRow = (seedId: string) => {
    updateSeeds(fluidStateChoice.seeds.map((row) => (
      row.id === seedId ? { ...row, composition: [...row.composition, createCompositionRow()] } : row
    )));
  };
  const patchCompositionRow = (seedId: string, compId: string, patch: Partial<Omit<CompositionSeedRow, "id">>) => {
    updateSeeds(fluidStateChoice.seeds.map((row) => (
      row.id === seedId
        ? { ...row, composition: row.composition.map((comp) => (comp.id === compId ? { ...comp, ...patch } : comp)) }
        : row
    )));
  };
  const removeCompositionRow = (seedId: string, compId: string) => {
    updateSeeds(fluidStateChoice.seeds.map((row) => (
      row.id === seedId ? { ...row, composition: row.composition.filter((comp) => comp.id !== compId) } : row
    )));
  };
  const runButtonLabel = isCancelingRun
    ? "Cancelling — waiting for protocol to stop"
    : isRunning
      ? "Running..."
      : "Run Protocol";

  return (
    <div>
      <div style={protocolPickerStyle}>
        <ConfigFilePicker
          kind="Protocol"
          configs={configs}
          selectedFile={selectedFile}
          onSelectFile={onImportFile}
          onNew={onNewFile}
          onDelete={onDeleteFile}
          deleteDisabledReason={deleteDisabledReason}
        />
      </div>

      {!hasSteps && (
            <div style={emptyProtocolStyle}>
              Load a protocol or add steps.
            </div>
          )}

          <div style={namedPositionsStyle}>
            <div style={namedPositionsHeaderStyle}>
              <div>
                <h3 style={sectionTitleStyle}>Named Positions</h3>
                <p style={sectionSubtextStyle}>Protocol-level targets such as park_position.</p>
              </div>
              <button onClick={addPosition} style={addBtnStyle}>
                Add Position
              </button>
            </div>

            {positionRows.length === 0 ? (
              <div style={emptyNamedPositionsStyle}>No named positions.</div>
            ) : (
              <div style={positionRowsStyle}>
                {positionRows.map((position, i) => (
                  <div key={position.id} style={positionRowStyle}>
                    <label style={positionNameFieldStyle}>
                      <span style={theme.fieldLabel}>Name</span>
                      <input
                        id={`pos-${i}-name`}
                        name={`pos_${i}_name`}
                        aria-label={`Position ${i + 1} name`}
                        type="text"
                        value={position.name}
                        onChange={(event) => updatePosition(position.id, { name: event.target.value })}
                        style={inputStyle}
                      />
                    </label>
                    <CoordinateField
                      id={`pos-${i}-coord`}
                      name={`pos_${i}_coord`}
                      label={`${position.name.trim() || `Position ${i + 1}`} coordinates`}
                      value={{ x: position.x, y: position.y, z: position.z }}
                      onChange={(value) => updatePosition(position.id, value)}
                    />
                    <button
                      onClick={() => removePosition(position.id)}
                      style={removeBtnStyle}
                      aria-label={`Remove ${position.name.trim() || `position ${i + 1}`}`}
                      title="Remove position"
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            )}

            {positionErrors.length > 0 && (
              <div style={positionErrorStyle}>
                {positionErrors.map((error) => (
                  <div key={error}>{error}</div>
                ))}
              </div>
            )}
          </div>

          {steps.map((step, i) => {
            const cmd = commandsByName[step.command];
            const color = COMMAND_COLORS[step.command] ?? theme.color.textMuted;

            return (
              <div key={i} style={{ ...cardStyle, borderLeft: `3px solid ${color}` }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <h4 style={{ margin: 0, fontSize: 13, display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={stepBadgeStyle}>Step {i + 1}:</span>{" "}
                    <span style={{ color, fontWeight: 600 }}>{step.command}</span>
                  </h4>
                  <div style={{ display: "flex", gap: 4 }}>
                    <button onClick={() => moveStep(i, -1)} disabled={i === 0} style={reorderBtnStyle} title="Move up">
                      ↑
                    </button>
                    <button onClick={() => moveStep(i, 1)} disabled={i === steps.length - 1} style={reorderBtnStyle} title="Move down">
                      ↓
                    </button>
                    <button onClick={() => removeStep(i)} style={removeBtnStyle}>
                      ✕
                    </button>
                  </div>
                </div>

                {cmd ? (
                  <div style={stepArgsGridStyle}>
                    {cmd.args.map((arg) => {
                      if (isHiddenArgForStep(arg.name, step.args, choices)) {
                        return null;
                      }
                      const val = step.args[arg.name];
                      const contextualOptions = optionsForArg(arg.name, step.args, choices, step.command);
                      const argOptions = contextualOptions.length > 0
                        ? includeCurrentOption(contextualOptions, val)
                        : [];
                      if (argOptions.length > 0) {
                        return (
                          <SmartSelectField
                            key={arg.name}
                            id={`step-${i}-${arg.name}`}
                            name={`step_${i}_${arg.name}`}
                            label={argLabel(arg.name)}
                            value={String(val ?? "")}
                            options={argOptions}
                            onChange={(v) => updateStepArg(i, arg.name, v)}
                            required={arg.required}
                          />
                        );
                      }
                      if (arg.name === "method_kwargs") {
                        if (isAsmiIndentationStep(step.args, choices)) {
                          return (
                            <MethodOptionsField
                              key={arg.name}
                              idPrefix={`step-${i}-method`}
                              namePrefix={`step_${i}_method`}
                              value={val}
                              asmiIndentation
                              onChange={(v) => updateStepArg(i, arg.name, v)}
                            />
                          );
                        }
                        return (
                          <MethodParamsFields
                            key={arg.name}
                            idPrefix={`step-${i}-method`}
                            namePrefix={`step_${i}_method`}
                            params={methodParamsForStep(step.args, choices)}
                            value={val}
                            onChange={(v) => updateStepArg(i, arg.name, v)}
                          />
                        );
                      }
                      if (isNumericType(arg.type)) {
                        if (!arg.required) {
                          return (
                            <OptionalNumberField
                              key={arg.name}
                              id={`step-${i}-${arg.name}`}
                              name={`step_${i}_${arg.name}`}
                              label={argLabel(arg.name)}
                              value={typeof val === "number" ? val : null}
                              onChange={(v) => updateStepArg(i, arg.name, v)}
                            />
                          );
                        }
                        return (
                          <NumberField
                            key={arg.name}
                            id={`step-${i}-${arg.name}`}
                            name={`step_${i}_${arg.name}`}
                            label={argLabel(arg.name)}
                            value={Number(val ?? 0)}
                            onChange={(v) => updateStepArg(i, arg.name, v)}
                            required={arg.required}
                          />
                        );
                      }
                      return (
                        <TextField
                          key={arg.name}
                          id={`step-${i}-${arg.name}`}
                          name={`step_${i}_${arg.name}`}
                          label={argLabel(arg.name)}
                          value={String(val ?? "")}
                          onChange={(v) => updateStepArg(i, arg.name, v)}
                          required={arg.required}
                        />
                      );
                    })}
                  </div>
                ) : (
                  <p style={{ color: theme.color.danger, fontSize: 12, margin: 0 }}>Unknown command: {step.command}</p>
                )}

                {validationErrors &&
                  validationErrors
                    .filter((e) => stepErrorPattern(i).test(e))
                    .map((e, j) => (
                      <p key={j} style={{ color: theme.color.danger, fontSize: 11, margin: "4px 0 0" }}>
                        {displayStepError(e)}
                      </p>
                    ))}
              </div>
            );
          })}

          <div style={addStepPanelStyle}>
            <label style={toolbarFieldStyle}>
              <span style={toolbarLabelStyle}>Add step</span>
              <select value={addCommand} onChange={(e) => setAddCommand(e.target.value)} style={selectStyle}>
                {commands.map((c) => (
                  <option key={c.name} value={c.name}>
                    {commandLabel(c.name)}
                  </option>
                ))}
              </select>
            </label>
            <button onClick={addStep} style={addBtnStyle}>
              Add
            </button>
          </div>

      <div style={{ marginTop: 12 }}>
        {onFluidStateChoiceChange && (
          <div style={stateChoicePanelStyle}>
            <div style={theme.sectionLabel}>Fluid state tracking</div>
            <div style={stateChoiceOptionsStyle}>
              <label style={stateChoiceOptionStyle}>
                <input
                  type="radio"
                  name="fluid-state-choice"
                  checked={fluidStateChoice.mode === "active"}
                  onChange={() => onFluidStateChoiceChange({ ...fluidStateChoice, mode: "active" })}
                />
                Use active deck contents
              </label>
              <label style={stateChoiceOptionStyle}>
                <input
                  type="radio"
                  name="fluid-state-choice"
                  checked={fluidStateChoice.mode === "none"}
                  onChange={() => onFluidStateChoiceChange({ ...fluidStateChoice, mode: "none" })}
                />
                No state tracking
              </label>
              <label style={stateChoiceOptionStyle}>
                <input
                  type="radio"
                  name="fluid-state-choice"
                  checked={fluidStateChoice.mode === "new"}
                  onChange={() => onFluidStateChoiceChange({ ...fluidStateChoice, mode: "new" })}
                />
                New fluid state
              </label>
              <label style={stateChoiceOptionStyle}>
                <input
                  type="radio"
                  name="fluid-state-choice"
                  checked={fluidStateChoice.mode === "resume"}
                  onChange={() => onFluidStateChoiceChange({ ...fluidStateChoice, mode: "resume" })}
                />
                Resume existing state
              </label>
            </div>
            {fluidStateChoice.mode === "new" && (
              <div style={{ marginTop: 8 }}>
                <input
                  aria-label="Label for the new fluid state"
                  value={fluidStateChoice.newLabel}
                  onChange={(e) => onFluidStateChoiceChange({ ...fluidStateChoice, newLabel: e.target.value })}
                  placeholder="Label for the new fluid state (optional)"
                  style={{ ...theme.input, width: "100%", maxWidth: 360 }}
                />
                <datalist id={SEED_CONTAINER_LIST_ID}>
                  {seedContainerOptions.map((key) => (
                    <option key={key} value={key} />
                  ))}
                </datalist>
                <div style={seedSectionHeaderStyle}>
                  <div>
                    <div style={seedSectionTitleStyle}>Starting volumes</div>
                    <p style={seedSectionSubtextStyle}>
                      Seed each container's volume (µL). Containers you leave out start at 0 µL.
                    </p>
                  </div>
                  <button type="button" onClick={addSeedRow} style={addBtnStyle}>
                    Add container
                  </button>
                </div>
                {fluidStateChoice.seeds.length === 0 ? (
                  <div style={emptySeedsStyle}>No starting volumes — every container starts empty.</div>
                ) : (
                  <div style={seedRowsStyle}>
                    {fluidStateChoice.seeds.map((row, i) => (
                      <div key={row.id} style={seedRowStyle}>
                        <div style={seedRowMainStyle}>
                          <label style={seedFieldStyle}>
                            <span style={theme.fieldLabel}>Container</span>
                            <input
                              aria-label={`Seed container ${i + 1}`}
                              list={SEED_CONTAINER_LIST_ID}
                              value={row.container}
                              onChange={(e) => patchSeedRow(row.id, { container: e.target.value })}
                              placeholder="e.g. s1 or plate_1.A1"
                              style={inputStyle}
                            />
                          </label>
                          <label style={seedVolumeFieldStyle}>
                            <span style={theme.fieldLabel}>Volume (µL)</span>
                            <input
                              aria-label={`Seed volume ${i + 1}`}
                              type="number"
                              min={0}
                              value={row.volume}
                              onChange={(e) => patchSeedRow(row.id, { volume: e.target.value })}
                              placeholder="0"
                              style={inputStyle}
                            />
                          </label>
                          <button
                            type="button"
                            onClick={() => removeSeedRow(row.id)}
                            style={removeBtnStyle}
                            aria-label={`Remove container row ${i + 1}`}
                            title="Remove container"
                          >
                            Remove
                          </button>
                        </div>
                        <div style={seedCompositionStyle}>
                          {row.composition.map((comp, j) => (
                            <div key={comp.id} style={seedCompositionRowStyle}>
                              <input
                                aria-label={`Seed ${i + 1} component ${j + 1} name`}
                                value={comp.component}
                                onChange={(e) => patchCompositionRow(row.id, comp.id, { component: e.target.value })}
                                placeholder="component (e.g. water)"
                                style={inputStyle}
                              />
                              <input
                                aria-label={`Seed ${i + 1} component ${j + 1} volume`}
                                type="number"
                                min={0}
                                value={comp.volume}
                                onChange={(e) => patchCompositionRow(row.id, comp.id, { volume: e.target.value })}
                                placeholder="µL"
                                style={inputStyle}
                              />
                              <button
                                type="button"
                                onClick={() => removeCompositionRow(row.id, comp.id)}
                                style={reorderBtnStyle}
                                aria-label={`Remove component ${j + 1} from container ${i + 1}`}
                                title="Remove component"
                              >
                                ✕
                              </button>
                            </div>
                          ))}
                          <button
                            type="button"
                            onClick={() => addCompositionRow(row.id)}
                            style={addComponentBtnStyle}
                            aria-label={`Add component to container ${i + 1}`}
                          >
                            + Add component
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {seedErrors.length > 0 && (
                  <div role="alert" style={seedErrorStyle}>
                    {seedErrors.map((message, index) => (
                      <div key={index}>{message}</div>
                    ))}
                  </div>
                )}
              </div>
            )}
            {fluidStateChoice.mode === "resume" && (
              <select
                aria-label="Fluid state to resume"
                value={fluidStateChoice.resumeId ?? ""}
                onChange={(e) => onFluidStateChoiceChange({
                  ...fluidStateChoice,
                  resumeId: e.target.value ? Number(e.target.value) : null,
                })}
                style={{ ...theme.input, marginTop: 8, width: "100%", maxWidth: 360 }}
              >
                <option value="">Select a fluid state…</option>
                {availableFluidStates.map((state) => (
                  <option key={state.id} value={state.id}>
                    #{state.id} {state.label ? `— ${state.label}` : ""}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}
        <RawYamlPanel
          value={(() => {
            const doc: Record<string, unknown> = {};
            const rawPositions = rowsToPositions(positionRows);
            if (rawPositions) doc.positions = rawPositions;
            doc.protocol = steps.map((s) => (
              { [s.command]: Object.keys(s.args).length ? s.args : null }
            ));
            return doc;
          })()}
          onApply={(parsed) => {
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
              return "Top level must be a mapping with a `protocol:` list.";
            }
            const doc = parsed as { protocol?: unknown; positions?: unknown };
            if (!Array.isArray(doc.protocol)) {
              return "`protocol:` must be a list of steps.";
            }
            const nextSteps: ProtocolStep[] = [];
            for (const [i, item] of doc.protocol.entries()) {
              if (typeof item === "string") {
                nextSteps.push({ command: item, args: {} });
                continue;
              }
              if (!item || typeof item !== "object" || Array.isArray(item)) {
                return `Step ${i + 1} must be a single-command mapping.`;
              }
              const keys = Object.keys(item);
              if (keys.length !== 1) {
                return `Step ${i + 1} must have exactly one command key, got ${keys.length}.`;
              }
              const command = keys[0];
              const args = (item as Record<string, unknown>)[command];
              if (args !== null && args !== undefined && (typeof args !== "object" || Array.isArray(args))) {
                return `Step ${i + 1} (${command}) args must be a mapping.`;
              }
              nextSteps.push({ command, args: { ...((args ?? {}) as Record<string, unknown>) } });
            }
            if (doc.positions !== undefined && doc.positions !== null
                && (typeof doc.positions !== "object" || Array.isArray(doc.positions))) {
              return "`positions:` must be a mapping of named points.";
            }
            commit(nextSteps);
            commitPositions(positionsToRows(
              (doc.positions ?? null) as Record<string, number[]> | null,
            ));
            return null;
          }}
        />
        {hasUnsaved && (
          <UnsavedNotice>
            <strong>Unsaved changes.</strong>{" "}
            {protocolDirty && "Save this protocol before running. "}
            {otherDirty.length > 0 && (
              `${otherDirty.join(" and ")} ${otherDirty.length > 1 ? "have" : "has"} unsaved edits — `
                + `save ${otherDirty.length > 1 ? "them in their tabs" : `it in the ${otherDirty[0]} tab`}. `
            )}
            Run Protocol uses the saved files, not your edits.
          </UnsavedNotice>
        )}
        {saveError && (
          <div style={saveErrorStyle}>Save failed: {saveError}</div>
        )}
        <div style={protocolActionBarStyle}>
          <input
            aria-label="Save as filename"
            value={saveAs}
            onChange={(e) => setSaveAs(e.target.value)}
            placeholder={selectedFile ?? "my_protocol.yaml"}
            style={filenameInputStyle}
          />
          <div style={protocolButtonGroupStyle}>
            <button onClick={handleValidate} disabled={isValidating || hasPositionErrors} style={validateBtnStyle}>
              {isValidating ? "..." : "Validate"}
            </button>
            <button onClick={handleSave} disabled={!canSave} style={saveBtnStyle}>
              Save
              {protocolDirty && (
                // aria-hidden so the accessible name stays "Save"; the
                // amber asterisk is a sighted-only unsaved-edit cue.
                <span aria-hidden="true" title="Unsaved changes" style={{ marginLeft: 4, fontWeight: 700, color: theme.color.warning }}>*</span>
              )}
            </button>
            {protocolDirty && (
              <button onClick={handleDiscard} style={discardBtnStyle}>Discard changes</button>
            )}
            {lastSaved && !protocolDirty && <SavedStatus filename={lastSaved.filename} at={lastSaved.at} />}
            {isRunning && (
              <button
                onClick={onCancelRun}
                disabled={isCancelingRun}
                style={isCancelingRun ? { ...cancelRunBtnStyle, opacity: 0.65, cursor: "not-allowed" } : cancelRunBtnStyle}
              >
                {isCancelingRun ? "Cancelling..." : "Cancel Run"}
              </button>
            )}
            <button
              onClick={onRun}
              disabled={runDisabled}
              title={runButtonTitle}
              style={runDisabled ? { ...runBtnStyle, opacity: 0.5, cursor: "not-allowed" } : runBtnStyle}
            >
              {runButtonLabel}
            </button>
          </div>
        </div>
        <SaveTargetHint saveAs={saveAsFilename} selectedFile={selectedFile} exists={saveAsExists} />

        {!hasSteps && (
          <p style={hintTextStyle}>Add at least one step before saving.</p>
        )}
        {!canRun && runDisabledReason && (
          <p style={{ ...theme.notice.warning, margin: "6px 0 0" }}>{runDisabledReason}</p>
        )}
        {validationErrors !== null && validationErrors.length === 0 && (
          <p style={{ ...theme.notice.success, margin: "6px 0 0" }}>Protocol is valid.</p>
        )}
        {validationErrors !== null && validationErrors.length > 0 && (
          <div style={{ ...theme.notice.error, margin: "6px 0 0" }}>
            {validationErrors.map((error, i) => (
              <div key={i}>{displayStepError(error)}</div>
            ))}
          </div>
        )}
        {runResult && (
          <p style={{ ...theme.notice.success, margin: "6px 0 0" }}>
            Protocol complete — {runResult.steps_executed} steps executed; campaign #{runResult.campaign_id} created.
          </p>
        )}
        {runError && (
          <p style={{ ...theme.notice.error, margin: "6px 0 0" }}>{runError}</p>
        )}
      </div>
      {confirmDialog}
    </div>
  );
}

function defaultArgValue(name: string, choices: ProtocolChoices, current: Record<string, unknown>, command?: string): unknown {
  if (command === "rinse" && name === "instrument") return choices.instruments.find((instrument) => choices.instrumentTypes[instrument] === "potentiostat") ?? "";
  if (command === "rinse" && name === "vial") return choices.vials[0] ?? "";
  if (name === "instrument") return choices.instruments[0] ?? "";
  if (name === "plate") return choices.plates[0] ?? "";
  if (isPositionArg(name)) return choices.positions[0] ?? "";
  if (name === "method") {
    const instrument = String(current.instrument ?? choices.instruments[0] ?? "");
    return measurementMethodsForInstrument(instrument, choices)[0] ?? "measure";
  }
  if (name === "light") return "white";
  return undefined;
}

function positionsToRows(positions: Record<string, number[]> | null | undefined): EditablePosition[] {
  return Object.entries(positions ?? {}).map(([name, value]) => ({
    id: `loaded:${name}`,
    name,
    x: finiteNumber(value[0]),
    y: finiteNumber(value[1]),
    z: finiteNumber(value[2]),
  }));
}

function rowsToPositions(rows: EditablePosition[]): Record<string, number[]> | null {
  const positions: Record<string, number[]> = {};
  for (const row of rows) {
    const name = row.name.trim();
    if (name) positions[name] = [row.x, row.y, row.z];
  }
  return Object.keys(positions).length > 0 ? positions : null;
}

function validatePositionRows(rows: EditablePosition[]): string[] {
  const errors: string[] = [];
  const seen = new Map<string, number>();
  rows.forEach((row, index) => {
    const name = row.name.trim();
    if (!name) {
      errors.push(`Position ${index + 1} needs a name.`);
    } else {
      seen.set(name, (seen.get(name) ?? 0) + 1);
    }
    if (![row.x, row.y, row.z].every(Number.isFinite)) {
      errors.push(`${name || `Position ${index + 1}`} needs numeric X, Y, and Z values.`);
    }
  });
  for (const [name, count] of seen) {
    if (count > 1) errors.push(`Position "${name}" is duplicated.`);
  }
  return errors;
}

function uniquePositionName(rows: EditablePosition[]): string {
  const existing = new Set(rows.map((row) => row.name.trim()).filter(Boolean));
  let i = 1;
  while (existing.has(`position_${i}`)) i += 1;
  return `position_${i}`;
}

function finiteNumber(value: unknown): number {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : 0;
}

function buildProtocolChoices(
  deck: DeckResponse,
  gantry: GantryResponse,
  protocolPositions: EditablePosition[],
  instrumentMethods: InstrumentMeasurementMethods,
  instrumentMethodParams: InstrumentMethodParams,
): ProtocolChoices {
  const instruments = Object.keys(gantry.config.instruments);
  const instrumentTypes = Object.fromEntries(
    Object.entries(gantry.config.instruments).map(([name, instrument]) => [name, instrument.type]),
  );
  const plates = deck.labware
    .filter((item) => item.config.type === "well_plate" || item.wells)
    .map((item) => item.key);
  const positions = uniqueStrings([
    ...deck.labware.flatMap(targetsForLabware),
    ...protocolPositions.map((position) => position.name.trim()),
  ]);
  const vials = uniqueStrings(deck.labware
    .filter((item) => ["vial", "vial_grid", "vial_holder"].includes(item.config.type))
    .flatMap(targetsForLabware));
  return { instruments, plates, positions, vials, instrumentTypes, instrumentMethods, instrumentMethodParams };
}

function targetsForLabware(item: LabwareResponse): string[] {
  const targets: string[] = [];
  const wells = item.wells ? Object.keys(item.wells).sort(wellSort) : generatedWellIds(item);
  if (wells.length > 0) {
    targets.push(...wells.map((well) => `${item.key}.${well}`));
  }
  const positions = item.positions ? Object.keys(item.positions).sort(wellSort) : [];
  if (positions.length > 0) {
    targets.push(...positions.map((position) => `${item.key}.${position}`));
  }
  if (targets.length === 0) {
    targets.push(item.key);
  }
  return targets;
}

function generatedWellIds(item: LabwareResponse): string[] {
  if (item.config.type !== "well_plate") return [];
  const rows = Math.max(0, Number(item.config.rows) || 0);
  const columns = Math.max(0, Number(item.config.columns) || 0);
  const ids: string[] = [];
  for (let row = 0; row < rows; row += 1) {
    const rowName = String.fromCharCode("A".charCodeAt(0) + row);
    for (let column = 1; column <= columns; column += 1) {
      ids.push(`${rowName}${column}`);
    }
  }
  return ids;
}

function wellSort(left: string, right: string): number {
  const a = /^([A-Za-z]+)(\d+)$/.exec(left);
  const b = /^([A-Za-z]+)(\d+)$/.exec(right);
  if (a && b && a[1] === b[1]) return Number(a[2]) - Number(b[2]);
  return left.localeCompare(right, undefined, { numeric: true });
}

function optionsForArg(
  name: string,
  args: Record<string, unknown>,
  choices: ProtocolChoices,
  command?: string,
): string[] {
  if (command === "rinse" && name === "instrument") return choices.instruments.filter((instrument) => choices.instrumentTypes[instrument] === "potentiostat");
  if (command === "rinse" && name === "vial") return choices.vials;
  if (name === "instrument") return choices.instruments;
  if (name === "plate") return choices.plates;
  if (isPositionArg(name)) return choices.positions;
  if (name === "method") return measurementMethodsForInstrument(String(args.instrument ?? ""), choices);
  if (name === "channel") return Object.keys(LIGHTING_LEVELS);
  if (name === "light") return ["off", ...Object.keys(LIGHTING_LEVELS)];
  if (name === "brightness") {
    // set_lights carries `channel`; image_well carries `light` (falling
    // back to its mode default: standard→white, curvature→contact).
    const channel = String(
      args.channel
      ?? (args.light && args.light !== "off" ? args.light : undefined)
      ?? (args.mode === "curvature" ? "contact" : "white"),
    );
    const levels = LIGHTING_LEVELS[channel];
    return levels ? levels.map(String) : [];
  }
  return [];
}

// Pawduino imaging lights expose discrete brightness levels per channel
// ("contact" = the red+blue LED pair); anything else is rejected by the
// driver, so the editor offers only the supported values. 0 = channel off.
// Mirrors cubos.instruments.lighting.vendors.pawduino._LEVELS — the sole
// lighting vendor today; introspect from the backend if a second appears.
const LIGHTING_LEVELS: Record<string, number[]> = {
  white: [0, 5, 10, 15, 25, 50, 100],
  contact: [0, 5, 10, 20, 30, 50],
};

function includeCurrentOption(options: string[], current: unknown): string[] {
  const value = String(current ?? "");
  if (!value || options.includes(value)) return options;
  return [value, ...options];
}

function methodParamsForStep(args: Record<string, unknown>, choices: ProtocolChoices): InstrumentMethodParamField[] {
  const type = inferInstrumentType(String(args.instrument ?? ""), choices);
  return choices.instrumentMethodParams[type]?.[String(args.method ?? "")] ?? [];
}

// Seed method_kwargs with each parameter's declared defaults so optional
// values show up prefilled; required fields without defaults stay absent
// (the engine rejects the step pre-motion with the field list if unfilled).
function defaultMethodKwargs(params: InstrumentMethodParamField[]): Record<string, unknown> {
  const kwargs: Record<string, unknown> = {};
  for (const param of params) {
    if (param.fields) {
      const nested: Record<string, unknown> = {};
      for (const field of param.fields) {
        if (!field.required && field.default != null) nested[field.name] = field.default;
      }
      kwargs[param.name] = nested;
    } else if (!param.required && param.default != null) {
      kwargs[param.name] = param.default;
    }
  }
  return kwargs;
}

function measurementMethodsForInstrument(instrument: string, choices: ProtocolChoices): string[] {
  const type = inferInstrumentType(instrument, choices);
  const methods = choices.instrumentMethods[type] ?? [];
  if (methods.length > 0) return methods;
  if (type === "asmi") return ["indentation", "measure"];
  if (["filmetrics", "uvvis_ccs", "uv_curing"].includes(type)) return ["measure"];
  if (type === "pipette") return [];
  return ["measure"];
}

function stepErrorPattern(stepIndex: number): RegExp {
  return new RegExp(`^Step ${stepIndex}\\b`);
}

function displayStepError(error: string): string {
  return error.replace(/^Step (\d+)/, (_match, rawIndex: string) => `Step ${Number(rawIndex) + 1}`);
}

function inferInstrumentType(instrument: string, choices: ProtocolChoices): string {
  const raw = choices.instrumentTypes[instrument] ?? instrument;
  const normalized = raw.replace(/^mock_/, "").toLowerCase();
  if (normalized.includes("asmi")) return "asmi";
  if (normalized.includes("filmetrics")) return "filmetrics";
  if (normalized.includes("uvvis")) return "uvvis_ccs";
  if (normalized.includes("uv_curing")) return "uv_curing";
  if (normalized.includes("pipette")) return "pipette";
  return normalized;
}

function isPositionArg(name: string): boolean {
  return ["position", "source", "destination"].includes(name);
}

function isNumericType(type: string): boolean {
  return type.includes("float") || type.includes("int");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAsmiIndentationStep(args: Record<string, unknown>, choices: ProtocolChoices): boolean {
  const instrument = String(args.instrument ?? "");
  const type = inferInstrumentType(instrument, choices);
  return type === "asmi" && String(args.method ?? "") === "indentation";
}

function isHiddenArgForStep(
  argName: string,
  args: Record<string, unknown>,
  choices: ProtocolChoices,
): boolean {
  if (argName === "brightness" && String(args.light ?? "") === "off") return true;
  return argName === "indentation_limit_height" && !isAsmiIndentationStep(args, choices);
}

function uniqueStrings(items: string[]): string[] {
  return Array.from(new Set(items.filter(Boolean)));
}

function argLabel(name: string): string {
  const labels: Record<string, string> = {
    delay_s: "Delay (s)",
    exposure_time: "Cure time (s)",
    indentation_limit_height: "Indentation limit height",
    intensity: "Intensity (%)",
    interwell_scan_height: "Interwell scan height",
    measurement_height: "Measurement height",
    method: "Measurement",
    method_kwargs: "Method options",
    volume_ul: "Volume (uL)",
  };
  return labels[name] ?? commandLabel(name);
}

function commandLabel(name: string): string {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function SmartSelectField({
  id,
  name,
  label,
  value,
  options,
  onChange,
  required,
}: {
  id?: string;
  name?: string;
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
  required?: boolean;
}) {
  const isLarge = options.length > 20;
  const listId = id ? `${id}-list` : `list-${label.replace(/\s+/g, '-')}`;

  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 2, fontSize: 12 }}>
      <span style={theme.fieldLabel}>
        {label}
        {required && <span style={{ color: theme.color.danger }}> *</span>}
      </span>
      {isLarge ? (
        <>
          <input
            id={id}
            name={name}
            list={listId}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            style={selectStyle}
            required={required}
            autoComplete="off"
            placeholder="Search or select..."
          />
          <datalist id={listId}>
            {options.map((option) => (
              <option key={option} value={option} />
            ))}
          </datalist>
        </>
      ) : (
        <select id={id} name={name} value={value} onChange={(event) => onChange(event.target.value)} style={selectStyle} required={required}>
          {options.map((option) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
      )}
    </label>
  );
}

function MethodOptionsField({
  idPrefix = "method",
  namePrefix = "method",
  value,
  asmiIndentation,
  onChange,
}: {
  idPrefix?: string;
  namePrefix?: string;
  value: unknown;
  asmiIndentation: boolean;
  onChange: (value: Record<string, unknown>) => void;
}) {
  if (!asmiIndentation) return null;
  const options = isRecord(value) ? value : {};
  const update = (key: string, nextValue: unknown) => onChange({ ...options, [key]: nextValue });
  const detectSurface = Boolean(options.detect_surface ?? false);
  const setDetectSurface = (enabled: boolean) => {
    if (enabled) {
      onChange({
        ...options,
        detect_surface: true,
        surface_search_step: Number(options.surface_search_step ?? 0.5),
        surface_force_threshold: Number(options.surface_force_threshold ?? 0.01),
        surface_search_max_travel: Number(options.surface_search_max_travel ?? 10),
      });
      return;
    }
    // Strip all surface keys so disabled steps round-trip to the same
    // YAML they had before the feature was toggled on.
    const surfaceKeys = [
      "detect_surface",
      "surface_search_step",
      "surface_force_threshold",
      "surface_search_max_travel",
    ];
    onChange(Object.fromEntries(
      Object.entries(options).filter(([key]) => !surfaceKeys.includes(key)),
    ));
  };
  return (
    <div style={methodOptionsStyle}>
      <div style={methodOptionsTitleStyle}>ASMI indentation options</div>
      <div style={methodOptionsGridStyle}>
        <NumberField
          id={`${idPrefix}-force-limit`}
          name={`${namePrefix}_force_limit`}
          label="Force limit (N)"
          value={Number(options.force_limit ?? 10)}
          onChange={(v) => update("force_limit", v)}
        />
        <NumberField
          id={`${idPrefix}-step-size`}
          name={`${namePrefix}_step_size`}
          label="Step size (mm)"
          value={Number(options.step_size ?? 0.1)}
          onChange={(v) => update("step_size", v)}
        />
        <NumberField
          id={`${idPrefix}-baseline-samples`}
          name={`${namePrefix}_baseline_samples`}
          label="Baseline samples"
          value={Number(options.baseline_samples ?? 10)}
          onChange={(v) => update("baseline_samples", Math.max(1, Math.round(v)))}
        />
        <SmartSelectField
          id={`${idPrefix}-measure-with-return`}
          name={`${namePrefix}_measure_with_return`}
          label="Measure with return"
          value={String(Boolean(options.measure_with_return ?? false))}
          options={["false", "true"]}
          onChange={(v) => update("measure_with_return", v === "true")}
        />
        <SmartSelectField
          id={`${idPrefix}-detect-surface`}
          name={`${namePrefix}_detect_surface`}
          label="Detect surface"
          value={String(detectSurface)}
          options={["false", "true"]}
          onChange={(v) => setDetectSurface(v === "true")}
        />
        {detectSurface && (
          <>
            <NumberField
              id={`${idPrefix}-surface-search-step`}
              name={`${namePrefix}_surface_search_step`}
              label="Surface search step (mm)"
              value={Number(options.surface_search_step ?? 0.5)}
              onChange={(v) => update("surface_search_step", v)}
            />
            <NumberField
              id={`${idPrefix}-surface-force-threshold`}
              name={`${namePrefix}_surface_force_threshold`}
              label="Surface force threshold (N)"
              value={Number(options.surface_force_threshold ?? 0.01)}
              onChange={(v) => update("surface_force_threshold", v)}
            />
            <NumberField
              id={`${idPrefix}-surface-search-max-travel`}
              name={`${namePrefix}_surface_search_max_travel`}
              label="Surface search max travel (mm)"
              value={Number(options.surface_search_max_travel ?? 10)}
              onChange={(v) => update("surface_search_max_travel", v)}
            />
          </>
        )}
      </div>
      <div style={methodOptionsHintStyle}>
        {detectSurface
          ? "Indentation limit height is measured from the detected surface (must be 0 or below)."
          : "Enable Detect surface to configure the surface-search parameters."}
      </div>
    </div>
  );
}

function MethodParamsFields({
  idPrefix,
  namePrefix,
  params,
  value,
  onChange,
}: {
  idPrefix: string;
  namePrefix: string;
  params: InstrumentMethodParamField[];
  value: unknown;
  onChange: (value: Record<string, unknown>) => void;
}) {
  if (params.length === 0) return null;
  const kwargs = isRecord(value) ? value : {};

  const renderField = (
    field: InstrumentMethodParamField,
    fieldValue: unknown,
    setValue: (v: unknown) => void,
    keyPrefix: string,
  ) => {
    const id = `${idPrefix}-${keyPrefix}${field.name}`;
    const name = `${namePrefix}_${keyPrefix}${field.name}`;
    const label = `${argLabel(field.name)}${field.required ? " *" : ""}`;
    // Optional params surface as e.g. "float | None" — match the base type.
    const isInt = /\bint\b/.test(field.type);
    if (isInt || /\bfloat\b/.test(field.type)) {
      return (
        <OptionalNumberField
          key={id}
          id={id}
          name={name}
          label={label}
          value={typeof fieldValue === "number" ? fieldValue : null}
          onChange={(v) => setValue(v === null ? null : (isInt ? Math.round(v) : v))}
        />
      );
    }
    if (/\bbool\b/.test(field.type)) {
      return (
        <SmartSelectField
          key={id}
          id={id}
          name={name}
          label={label}
          value={String(Boolean(fieldValue ?? field.default ?? false))}
          options={["false", "true"]}
          onChange={(v) => setValue(v === "true")}
        />
      );
    }
    return (
      <TextField
        key={id}
        id={id}
        name={name}
        label={label}
        value={String(fieldValue ?? "")}
        onChange={(v) => setValue(v === "" ? null : v)}
      />
    );
  };

  return (
    <div style={methodOptionsStyle}>
      <div style={methodOptionsTitleStyle}>Method parameters</div>
      {params.map((param) => {
        if (param.fields) {
          const nested = isRecord(kwargs[param.name]) ? kwargs[param.name] as Record<string, unknown> : {};
          const setNested = (fieldName: string, v: unknown) => {
            const nextNested = { ...nested };
            if (v === null) {
              delete nextNested[fieldName];
            } else {
              nextNested[fieldName] = v;
            }
            onChange({ ...kwargs, [param.name]: nextNested });
          };
          return (
            <div key={param.name} style={methodOptionsGridStyle}>
              {param.fields.map((field) =>
                renderField(field, nested[field.name], (v) => setNested(field.name, v), `${param.name}-`))}
            </div>
          );
        }
        const setTop = (v: unknown) => {
          const next = { ...kwargs };
          if (v === null) {
            delete next[param.name];
          } else {
            next[param.name] = v;
          }
          onChange(next);
        };
        return (
          <div key={param.name} style={methodOptionsGridStyle}>
            {renderField(param, kwargs[param.name], setTop, "")}
          </div>
        );
      })}
      <div style={methodOptionsHintStyle}>
        Fields marked * are required by the selected method.
      </div>
    </div>
  );
}

const protocolPickerStyle: React.CSSProperties = {
  display: "flex",
  gap: 10,
  alignItems: "end",
  marginBottom: 12,
  flexWrap: "wrap",
};

const addStepPanelStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "end",
  gap: 8,
  minWidth: 0,
  width: "min(100%, 340px)",
  marginTop: 12,
};

const toolbarFieldStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,
  flex: "1 1 220px",
  minWidth: 0,
};

const toolbarLabelStyle: React.CSSProperties = {
  ...theme.sectionLabel,
};

// Step args fill left-to-right in columns (same auto-fit pattern as the
// ASMI indentation options) so a step card stays one or two rows tall
// instead of stacking every parameter full-width.
const stepArgsGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
  gap: 8,
  alignItems: "end",
};

const methodOptionsStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.accentTintBorder}`,
  background: theme.color.accentTint,
  borderRadius: theme.radius.md,
  padding: 10,
  marginTop: 4,
  // Spans the step-args grid so the tinted panel stays a full-width
  // block below the main parameters.
  gridColumn: "1 / -1",
};

const methodOptionsTitleStyle: React.CSSProperties = {
  color: theme.color.accentText,
  fontSize: 12,
  fontWeight: 600,
  marginBottom: 8,
};

const methodOptionsGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
  gap: 8,
};

const methodOptionsHintStyle: React.CSSProperties = {
  color: theme.color.accentText,
  fontSize: 11,
  marginTop: 8,
};

const namedPositionsStyle: React.CSSProperties = {
  background: theme.color.surfaceMuted,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 14,
  marginTop: 8,
};

const namedPositionsHeaderStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "start",
  gap: 12,
  marginBottom: 10,
};

const sectionTitleStyle: React.CSSProperties = {
  ...theme.panelTitle,
  fontSize: 13,
};

const sectionSubtextStyle: React.CSSProperties = {
  color: theme.color.textMuted,
  fontSize: 11,
  margin: "2px 0 0",
};

const positionRowsStyle: React.CSSProperties = {
  display: "grid",
  gap: 8,
};

const positionRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(140px, 1fr) minmax(240px, 2fr) auto",
  gap: 10,
  alignItems: "end",
};

const positionNameFieldStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
  fontSize: 12,
};

const emptyNamedPositionsStyle: React.CSSProperties = {
  color: theme.color.textMuted,
  fontSize: 12,
};

const positionErrorStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginTop: 8,
};

const emptyProtocolStyle: React.CSSProperties = {
  border: `1px dashed ${theme.color.borderStrong}`,
  borderRadius: theme.radius.md,
  color: theme.color.textMuted,
  fontSize: 13,
  padding: "18px 12px",
  marginTop: 8,
  background: theme.color.surfaceMuted,
};

const cardStyle: React.CSSProperties = {
  background: theme.color.surface,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  boxShadow: theme.shadow.card,
  padding: 14,
  marginTop: 8,
};

/** Neutral index badge for step rows ("Step 1:"). */
const stepBadgeStyle: React.CSSProperties = {
  ...theme.pill,
  ...theme.mono,
  background: theme.color.surfaceSunken,
  color: theme.color.textMuted,
  fontSize: 11,
  fontWeight: 600,
};

const selectStyle: React.CSSProperties = {
  ...theme.input,
  height: 34,
  padding: "0 10px",
  width: "100%",
};

const inputStyle: React.CSSProperties = {
  ...theme.input,
};

const addBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  height: 34,
  padding: "0 14px",
  fontSize: 12,
};

const reorderBtnStyle: React.CSSProperties = {
  ...theme.btn.ghost,
  ...theme.btnSmall,
  padding: "2px 6px",
  lineHeight: 1,
  color: theme.color.textMuted,
};

const removeBtnStyle: React.CSSProperties = {
  ...theme.btn.danger,
  ...theme.btnSmall,
  padding: "2px 8px",
  lineHeight: 1,
};

const protocolActionBarStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  alignItems: "center",
  flexWrap: "wrap",
};

const stateChoicePanelStyle: React.CSSProperties = {
  ...theme.card,
  padding: 10,
  marginBottom: 10,
};

const stateChoiceOptionsStyle: React.CSSProperties = {
  display: "flex",
  gap: 16,
  flexWrap: "wrap",
  marginTop: 6,
};

const stateChoiceOptionStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  fontSize: 13,
  color: theme.color.text,
  cursor: "pointer",
};

const seedSectionHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "space-between",
  gap: 10,
  marginTop: 12,
};

const seedSectionTitleStyle: React.CSSProperties = {
  ...theme.sectionLabel,
  fontSize: 11,
};

const seedSectionSubtextStyle: React.CSSProperties = {
  color: theme.color.textMuted,
  fontSize: 11,
  margin: "2px 0 0",
};

const emptySeedsStyle: React.CSSProperties = {
  color: theme.color.textMuted,
  fontSize: 12,
  marginTop: 8,
};

const seedRowsStyle: React.CSSProperties = {
  display: "grid",
  gap: 10,
  marginTop: 8,
};

const seedRowStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  background: theme.color.surfaceMuted,
  padding: 10,
  display: "grid",
  gap: 8,
};

const seedRowMainStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(160px, 2fr) minmax(110px, 1fr) auto",
  gap: 10,
  alignItems: "end",
};

const seedFieldStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
  fontSize: 12,
};

const seedVolumeFieldStyle: React.CSSProperties = {
  ...seedFieldStyle,
};

const seedCompositionStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
  paddingLeft: 4,
  borderLeft: `2px solid ${theme.color.border}`,
};

const seedCompositionRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(140px, 2fr) minmax(90px, 1fr) auto",
  gap: 8,
  alignItems: "center",
};

const addComponentBtnStyle: React.CSSProperties = {
  ...theme.btn.ghost,
  ...theme.btnSmall,
  justifySelf: "start",
  color: theme.color.textMuted,
};

const seedErrorStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginTop: 8,
  display: "grid",
  gap: 2,
};

const protocolButtonGroupStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  alignItems: "center",
  justifyContent: "flex-end",
  flex: "0 0 auto",
  flexWrap: "wrap",
};

const filenameInputStyle: React.CSSProperties = {
  ...theme.input,
  flex: "1 1 220px",
  minWidth: 160,
};

const validateBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
};

// Run is the hero action of this editor, so it alone gets btn.primary;
// Save stays a neutral secondary.
const saveBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  padding: "6px 20px",
};

const runBtnStyle: React.CSSProperties = {
  ...theme.btn.primary,
  padding: "6px 20px",
};

const cancelRunBtnStyle: React.CSSProperties = {
  ...theme.btn.danger,
};

const saveErrorStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginBottom: 8,
};

const discardBtnStyle: React.CSSProperties = {
  ...theme.btn.ghost,
};

const hintTextStyle: React.CSSProperties = {
  marginTop: 6,
  color: theme.color.textMuted,
  fontSize: 12,
};
