import { useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import * as theme from "../../theme";
import {
  useFluidStates,
  useFluidState,
  useTipState,
  useCapState,
  useReconciliation,
  useResolveReconciliation,
  useCorrectContainer,
} from "../../hooks/useFluidState";
import type { ContainerView, OperationView } from "../../types";

const VOLUME_CHANGE_TOLERANCE_UL = 1e-6;

const RESOLUTIONS: { value: string; label: string }[] = [
  { value: "applied", label: "Applied — confirmed it happened as journaled" },
  { value: "not_applied", label: "Not applied — confirmed it did not happen" },
];

function formatComposition(composition: Record<string, number>): string {
  const entries = Object.entries(composition);
  if (entries.length === 0) return "—";
  return entries.map(([name, ul]) => `${name}: ${ul.toFixed(3)}`).join(", ");
}

function formatVolume(value: number): string {
  return value.toFixed(3);
}

interface ResolveFormState {
  fluid_state_id: number;
  operation: OperationView;
  resolution: string;
  operator: string;
  reason: string;
}

interface EditingCellState {
  fluid_state_id: number;
  labware_key: string;
  location_id: string;
  version: number;
  previousVolume: number;
}

interface CorrectFormState {
  fluid_state_id: number;
  labware_key: string;
  location_id: string;
  version: number;
  previousVolume: number;
  newVolume: number;
  operator: string;
  reason: string;
}

export default function StatePanel() {
  const fluidStates = useFluidStates();
  // undefined = no explicit choice yet (fall back to the newest state once
  // the list loads); null = the operator explicitly picked "no state", which
  // must stick instead of snapping back to the fallback.
  const [explicitSelectedId, setExplicitSelectedId] = useState<number | null | undefined>(undefined);
  const selectedId = explicitSelectedId === undefined
    ? fluidStates.data?.[0]?.id ?? null
    : explicitSelectedId;

  const detail = useFluidState(selectedId);
  const tips = useTipState(selectedId);
  const caps = useCapState(selectedId);
  const reconciliation = useReconciliation(selectedId);
  const resolveMutation = useResolveReconciliation(selectedId);
  const correctMutation = useCorrectContainer(selectedId);

  const [resolveForm, setResolveForm] = useState<ResolveFormState | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const [editingCell, setEditingCell] = useState<EditingCellState | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [correctForm, setCorrectForm] = useState<CorrectFormState | null>(null);
  const [correctError, setCorrectError] = useState<string | null>(null);
  const suppressNextBlurRef = useRef(false);

  const reconciliationItems = useMemo(
    () => reconciliation.data?.items ?? [],
    [reconciliation.data],
  );

  const openResolveForm = (operation: OperationView) => {
    if (selectedId === null) return;
    // Keep the fallback selection stable while the operator is reviewing a
    // state-scoped action.  A refreshed list may otherwise choose a newer
    // state before the form is submitted.
    setExplicitSelectedId(selectedId);
    setResolveError(null);
    setResolveForm({ fluid_state_id: selectedId, operation, resolution: "applied", operator: "", reason: "" });
  };

  const submitResolve = async () => {
    if (!resolveForm) return;
    if (resolveForm.fluid_state_id !== selectedId) {
      setResolveForm(null);
      return;
    }
    if (!resolveForm.operator.trim() || !resolveForm.reason.trim()) {
      setResolveError("Operator and reason are both required.");
      return;
    }
    setResolveError(null);
    try {
      await resolveMutation.mutateAsync({
        domain: resolveForm.operation.domain,
        operation_key: resolveForm.operation.operation_key,
        resolution: resolveForm.resolution,
        operator: resolveForm.operator.trim(),
        reason: resolveForm.reason.trim(),
      });
      setResolveForm(null);
      reconciliation.refetch();
    } catch (err) {
      setResolveError(err instanceof Error ? err.message : String(err));
    }
  };

  const beginEditingVolume = (container: ContainerView) => {
    if (selectedId === null) return;
    // Keep the fallback selection stable while this draft is open; the
    // correction must be sent to the state that supplied this container.
    setExplicitSelectedId(selectedId);
    setCorrectError(null);
    suppressNextBlurRef.current = false;
    setEditingCell({
      fluid_state_id: selectedId,
      labware_key: container.labware_key,
      location_id: container.location_id,
      version: container.version,
      previousVolume: container.current_volume_ul,
    });
    setEditingValue(String(container.current_volume_ul));
  };

  const cancelEditingVolume = () => {
    setEditingCell(null);
  };

  // Use the version/volume captured when editing began, not whatever the
  // row re-rendered with meanwhile (e.g. a background refetch) — otherwise
  // a concurrent change to this container would go undetected instead of
  // rejecting with a 409.
  const commitEditingVolume = (editing: EditingCellState) => {
    const parsed = Number(editingValue);
    const changed =
      Number.isFinite(parsed) &&
      Math.abs(parsed - editing.previousVolume) > VOLUME_CHANGE_TOLERANCE_UL;
    setEditingCell(null);
    if (!changed) return;
    setCorrectForm({
      fluid_state_id: editing.fluid_state_id,
      labware_key: editing.labware_key,
      location_id: editing.location_id,
      version: editing.version,
      previousVolume: editing.previousVolume,
      newVolume: parsed,
      operator: "",
      reason: "",
    });
  };

  const handleVolumeKeyDown = (event: KeyboardEvent<HTMLInputElement>, editing: EditingCellState) => {
    if (event.key === "Enter") {
      suppressNextBlurRef.current = true;
      commitEditingVolume(editing);
    } else if (event.key === "Escape") {
      suppressNextBlurRef.current = true;
      cancelEditingVolume();
    }
  };

  const handleVolumeBlur = (editing: EditingCellState) => {
    if (suppressNextBlurRef.current) {
      suppressNextBlurRef.current = false;
      return;
    }
    commitEditingVolume(editing);
  };

  const submitCorrect = async () => {
    if (!correctForm) return;
    if (correctForm.fluid_state_id !== selectedId) {
      setCorrectForm(null);
      return;
    }
    if (!correctForm.operator.trim() || !correctForm.reason.trim()) {
      setCorrectError("Operator and reason are both required.");
      return;
    }
    setCorrectError(null);
    try {
      await correctMutation.mutateAsync({
        labwareKey: correctForm.labware_key,
        locationId: correctForm.location_id,
        body: {
          new_volume_ul: correctForm.newVolume,
          version: correctForm.version,
          operator: correctForm.operator.trim(),
          reason: correctForm.reason.trim(),
        },
      });
      setCorrectForm(null);
      detail.refetch();
    } catch (err) {
      setCorrectError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <section style={panelStyle} aria-label="Fluid, tip, and cap state">
      <div style={headerStyle}>
        <div>
          <h3 style={theme.panelTitle}>Liquid-Handling State</h3>
          <div style={subtitleStyle}>Containers, tips, caps, and pending operations</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <select
            aria-label="Fluid state"
            value={selectedId ?? ""}
            onChange={(event) => {
              setResolveForm(null);
              setResolveError(null);
              setEditingCell(null);
              setEditingValue("");
              setCorrectForm(null);
              setCorrectError(null);
              suppressNextBlurRef.current = false;
              setExplicitSelectedId(event.target.value ? Number(event.target.value) : null);
            }}
            style={selectStyle}
          >
            <option value="">Select a fluid state…</option>
            {(fluidStates.data ?? []).map((state) => (
              <option key={state.id} value={state.id}>
                #{state.id} {state.label ? `— ${state.label}` : ""}
              </option>
            ))}
          </select>
          <button onClick={() => fluidStates.refetch()} style={secondaryButtonStyle}>
            Refresh
          </button>
        </div>
      </div>

      {fluidStates.isError && (
        <div style={errorStyle}>
          Failed to load fluid states: {fluidStates.error instanceof Error ? fluidStates.error.message : String(fluidStates.error)}
        </div>
      )}

      {selectedId === null && !fluidStates.isLoading && (
        <div style={emptyStyle}>
          No fluid state selected. Create one from Run Protocol, or select an existing state above.
        </div>
      )}

      {selectedId !== null && (
        <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 16 }}>
          {reconciliationItems.length > 0 && (
            <div style={reconciliationBannerStyle} role="alert">
              <strong>
                {reconciliationItems.length} operation{reconciliationItems.length > 1 ? "s" : ""}{" "}
                {reconciliationItems.length > 1 ? "require" : "requires"} reconciliation
              </strong>
              <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 6 }}>
                {reconciliationItems.map((operation) => (
                  <div key={`${operation.domain}:${operation.operation_key}`} style={reconciliationRowStyle}>
                    <div>
                      <span style={theme.pill}>{operation.domain}</span>{" "}
                      <span style={theme.mono}>{operation.operation_key}</span>{" "}
                      <span style={metaTextStyle}>{operation.operation_type}</span>
                      {operation.detail && <div style={metaTextStyle}>{operation.detail}</div>}
                    </div>
                    <button
                      style={primaryButtonStyle}
                      onClick={() => openResolveForm(operation)}
                    >
                      Resolve
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {resolveForm?.fluid_state_id === selectedId && (
            <div style={resolveFormStyle}>
              <div style={theme.sectionLabel}>
                Resolve {resolveForm.operation.domain} operation {resolveForm.operation.operation_key}
              </div>
              <label style={fieldRowStyle}>
                <span style={theme.fieldLabel}>Resolution</span>
                <select
                  value={resolveForm.resolution}
                  onChange={(event) => setResolveForm({ ...resolveForm, resolution: event.target.value })}
                  style={selectStyle}
                >
                  {RESOLUTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label style={fieldRowStyle}>
                <span style={theme.fieldLabel}>Operator</span>
                <input
                  style={theme.input}
                  value={resolveForm.operator}
                  onChange={(event) => setResolveForm({ ...resolveForm, operator: event.target.value })}
                  placeholder="Your name or initials"
                />
              </label>
              <label style={fieldRowStyle}>
                <span style={theme.fieldLabel}>Reason</span>
                <textarea
                  style={{ ...theme.input, minHeight: 60, resize: "vertical" }}
                  value={resolveForm.reason}
                  onChange={(event) => setResolveForm({ ...resolveForm, reason: event.target.value })}
                  placeholder="What did you observe, and why does this resolution match reality?"
                />
              </label>
              {resolveError && <div style={errorStyle}>{resolveError}</div>}
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  style={primaryButtonStyle}
                  disabled={resolveMutation.isPending}
                  onClick={() => void submitResolve()}
                >
                  {resolveMutation.isPending ? "Submitting…" : "Submit resolution"}
                </button>
                <button style={secondaryButtonStyle} onClick={() => setResolveForm(null)}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          <div>
            <div style={theme.sectionLabel}>Containers</div>
            {detail.isLoading && <div style={emptyStyle}>Loading…</div>}
            {detail.data && (
              <div style={tableFrameStyle}>
                <table style={tableStyle}>
                  <thead>
                    <tr>
                      <th style={thStyle}>Container</th>
                      <th style={thStyle}>Role</th>
                      <th style={thStyle}>Volume (µL)</th>
                      <th style={thStyle}>Composition</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.containers.map((container) => {
                      const isEditing =
                        editingCell?.fluid_state_id === selectedId &&
                        editingCell?.labware_key === container.labware_key &&
                        editingCell?.location_id === container.location_id;
                      const editing = isEditing ? editingCell : null;
                      return (
                        <tr key={`${container.labware_key}.${container.location_id}`}>
                          <td style={tdStyle}>
                            <span style={theme.mono}>
                              {container.labware_key}
                              {container.location_id ? `.${container.location_id}` : ""}
                            </span>
                          </td>
                          <td style={tdStyle}>{container.role ?? "—"}</td>
                          <td
                            style={tdNumericStyle}
                            onDoubleClick={() => beginEditingVolume(container)}
                          >
                            {editing ? (
                              <input
                                type="number"
                                step="0.001"
                                min="0"
                                autoFocus
                                aria-label={`Edit volume for ${container.labware_key}${container.location_id ? `.${container.location_id}` : ""}`}
                                style={{ ...theme.input, width: 100 }}
                                value={editingValue}
                                onChange={(event) => setEditingValue(event.target.value)}
                                onKeyDown={(event) => handleVolumeKeyDown(event, editing)}
                                onBlur={() => handleVolumeBlur(editing)}
                              />
                            ) : (
                              <>
                                {formatVolume(container.current_volume_ul)} / {formatVolume(container.working_volume_ul)}
                              </>
                            )}
                          </td>
                          <td style={tdStyle}>{formatComposition(container.composition)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {correctForm?.fluid_state_id === selectedId && (
              <div style={resolveFormStyle}>
                <div style={theme.sectionLabel}>
                  Correct volume for {correctForm.labware_key}
                  {correctForm.location_id ? `.${correctForm.location_id}` : ""}
                </div>
                <div style={metaTextStyle}>
                  {formatVolume(correctForm.previousVolume)} → {formatVolume(correctForm.newVolume)} µL
                </div>
                <label style={fieldRowStyle}>
                  <span style={theme.fieldLabel}>Operator</span>
                  <input
                    style={theme.input}
                    value={correctForm.operator}
                    onChange={(event) => setCorrectForm({ ...correctForm, operator: event.target.value })}
                    placeholder="Your name or initials"
                  />
                </label>
                <label style={fieldRowStyle}>
                  <span style={theme.fieldLabel}>Reason</span>
                  <textarea
                    style={{ ...theme.input, minHeight: 60, resize: "vertical" }}
                    value={correctForm.reason}
                    onChange={(event) => setCorrectForm({ ...correctForm, reason: event.target.value })}
                    placeholder="Why is this correction accurate?"
                  />
                </label>
                {correctError && <div style={errorStyle}>{correctError}</div>}
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    style={primaryButtonStyle}
                    disabled={correctMutation.isPending}
                    onClick={() => void submitCorrect()}
                  >
                    {correctMutation.isPending ? "Saving…" : "Save"}
                  </button>
                  <button style={secondaryButtonStyle} onClick={() => setCorrectForm(null)}>
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>

          <div>
            <div style={theme.sectionLabel}>Tips &amp; Attached Pipette</div>
            {tips.data && (
              <>
                <div style={{ marginBottom: 6 }}>
                  <span style={theme.mono}>{tips.data.pipette.pipette_key}</span>{": "}
                  {tips.data.pipette.rack_key
                    ? (
                      <span>
                        tip attached from <span style={theme.mono}>{tips.data.pipette.rack_key}.{tips.data.pipette.slot_id}</span>
                        {" "}(extension {tips.data.pipette.tip_extension_mm?.toFixed(2)} mm)
                      </span>
                    )
                    : <span style={metaTextStyle}>no tip attached</span>}
                  {tips.data.pipette.attachment_uncertain && (
                    <span style={{ ...theme.pill, ...theme.notice.warning, marginLeft: 8 }}>attachment uncertain</span>
                  )}
                </div>
                <div style={tableFrameStyle}>
                  <table style={tableStyle}>
                    <thead>
                      <tr>
                        <th style={thStyle}>Rack</th>
                        <th style={thStyle}>Slot</th>
                        <th style={thStyle}>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tips.data.containers.map((tip) => (
                        <tr key={`${tip.rack_key}.${tip.slot_id}`}>
                          <td style={tdStyle}><span style={theme.mono}>{tip.rack_key}</span></td>
                          <td style={tdStyle}><span style={theme.mono}>{tip.slot_id}</span></td>
                          <td style={tdStyle}>{tip.status}</td>
                        </tr>
                      ))}
                      {tips.data.containers.length === 0 && (
                        <tr><td style={tdStyle} colSpan={3}>No tip slots tracked for this deck.</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>

          <div>
            <div style={theme.sectionLabel}>Caps</div>
            {caps.data && (
              <div style={tableFrameStyle}>
                <table style={tableStyle}>
                  <thead>
                    <tr>
                      <th style={thStyle}>Container</th>
                      <th style={thStyle}>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {caps.data.containers.map((cap) => (
                      <tr key={`${cap.labware_key}.${cap.location_id}`}>
                        <td style={tdStyle}>
                          <span style={theme.mono}>
                            {cap.labware_key}
                            {cap.location_id ? `.${cap.location_id}` : ""}
                          </span>
                        </td>
                        <td style={tdStyle}>{cap.status}</td>
                      </tr>
                    ))}
                    {caps.data.containers.length === 0 && (
                      <tr><td style={tdStyle} colSpan={2}>No capper-managed containers on this deck.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {detail.data && (
            <div style={metaTextStyle}>
              {detail.data.pending_operation_count} pending operation{detail.data.pending_operation_count === 1 ? "" : "s"} · deck fingerprint <span style={theme.mono}>{detail.data.deck_fingerprint.slice(0, 12)}…</span>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

const panelStyle: CSSProperties = {
  overflow: "hidden",
};

const headerStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "12px 14px",
  borderBottom: `1px solid ${theme.color.border}`,
  gap: 12,
  flexWrap: "wrap",
};

const subtitleStyle: CSSProperties = {
  marginTop: 2,
  color: theme.color.textMuted,
  fontSize: 12,
};

const selectStyle: CSSProperties = {
  ...theme.input,
};

const secondaryButtonStyle: CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
};

const primaryButtonStyle: CSSProperties = {
  ...theme.btn.primary,
  ...theme.btnSmall,
};

const emptyStyle: CSSProperties = {
  padding: "24px 16px",
  color: theme.color.textMuted,
  fontSize: 13,
  textAlign: "center",
};

const errorStyle: CSSProperties = {
  ...theme.notice.error,
  margin: 12,
};

const metaTextStyle: CSSProperties = {
  color: theme.color.textMuted,
  fontSize: 12,
};

const reconciliationBannerStyle: CSSProperties = {
  ...theme.notice.warning,
  padding: 12,
};

const reconciliationRowStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  gap: 12,
  padding: "6px 0",
  borderTop: `1px solid ${theme.color.warningBorder}`,
};

const resolveFormStyle: CSSProperties = {
  ...theme.card,
  padding: 12,
  display: "flex",
  flexDirection: "column",
  gap: 8,
};

const fieldRowStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 4,
};

const tableFrameStyle: CSSProperties = {
  overflowX: "auto",
};

const tableStyle: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: 13,
  color: theme.color.text,
};

const thStyle: CSSProperties = {
  ...theme.sectionLabel,
  padding: "9px 12px",
  textAlign: "left",
  borderBottom: `1px solid ${theme.color.border}`,
};

const tdStyle: CSSProperties = {
  padding: "8px 12px",
  borderBottom: `1px solid ${theme.color.border}`,
  verticalAlign: "middle",
};

const tdNumericStyle: CSSProperties = {
  ...tdStyle,
  ...theme.mono,
};
