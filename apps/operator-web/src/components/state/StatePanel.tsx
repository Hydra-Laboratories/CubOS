import { useMemo, useState } from "react";
import type { CSSProperties } from "react";
import * as theme from "../../theme";
import {
  useFluidStates,
  useFluidState,
  useTipState,
  useCapState,
  useReconciliation,
  useResolveReconciliation,
} from "../../hooks/useFluidState";
import type { OperationView } from "../../types";

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
  operation: OperationView;
  resolution: string;
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

  const [resolveForm, setResolveForm] = useState<ResolveFormState | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const reconciliationItems = useMemo(
    () => reconciliation.data?.items ?? [],
    [reconciliation.data],
  );

  const openResolveForm = (operation: OperationView) => {
    setResolveError(null);
    setResolveForm({ operation, resolution: "applied" });
  };

  const submitResolve = async () => {
    if (!resolveForm) return;
    setResolveError(null);
    try {
      await resolveMutation.mutateAsync({
        domain: resolveForm.operation.domain,
        operation_key: resolveForm.operation.operation_key,
        resolution: resolveForm.resolution,
      });
      setResolveForm(null);
      reconciliation.refetch();
    } catch (err) {
      setResolveError(err instanceof Error ? err.message : String(err));
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
            onChange={(event) => setExplicitSelectedId(event.target.value ? Number(event.target.value) : null)}
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

          {resolveForm && (
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
                    {detail.data.containers.map((container) => (
                      <tr key={`${container.labware_key}.${container.location_id}`}>
                        <td style={tdStyle}>
                          <span style={theme.mono}>
                            {container.labware_key}
                            {container.location_id ? `.${container.location_id}` : ""}
                          </span>
                        </td>
                        <td style={tdStyle}>{container.role ?? "—"}</td>
                        <td style={tdNumericStyle}>
                          {formatVolume(container.current_volume_ul)} / {formatVolume(container.working_volume_ul)}
                        </td>
                        <td style={tdStyle}>{formatComposition(container.composition)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
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
