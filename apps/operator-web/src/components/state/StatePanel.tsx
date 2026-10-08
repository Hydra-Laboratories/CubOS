import { useState } from "react";
import type { CSSProperties } from "react";
import * as theme from "../../theme";
import {
  useFluidStates,
  useFluidState,
  useTipState,
  useCapState,
} from "../../hooks/useFluidState";

function formatComposition(composition: Record<string, number>): string {
  const entries = Object.entries(composition);
  if (entries.length === 0) return "—";
  return entries.map(([name, ul]) => `${name}: ${ul.toFixed(3)}`).join(", ");
}

function formatVolume(value: number): string {
  return value.toFixed(3);
}

interface Props {
  onStartNewState: () => void;
  onResumeState: (fluidStateId: number) => void;
}

export default function StatePanel({ onStartNewState, onResumeState }: Props) {
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
  const canResume = !detail.isLoading
    && !detail.isError
    && detail.data !== undefined
    && detail.data.pending_operation_count === 0
    && detail.data.reconciliation_required_count === 0;
  const hasPendingOrUncertainOperations = detail.data !== undefined
    && (detail.data.pending_operation_count > 0 || detail.data.reconciliation_required_count > 0);

  const refresh = () => {
    void fluidStates.refetch();
    if (selectedId !== null) {
      void detail.refetch();
      void tips.refetch();
      void caps.refetch();
    }
  };

  return (
    <section style={panelStyle} aria-label="Fluid, tip, and cap state">
      <div style={headerStyle}>
        <div>
          <h3 style={theme.panelTitle}>Liquid-Handling State</h3>
          <div style={subtitleStyle}>Saved liquids, tips, and caps</div>
        </div>
        <div style={headerActionsStyle}>
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
          <button onClick={refresh} style={secondaryButtonStyle}>
            Refresh
          </button>
          <button onClick={onStartNewState} style={primaryButtonStyle}>
            Start new state
          </button>
          <button
            disabled={selectedId === null || !canResume}
            onClick={() => selectedId !== null && onResumeState(selectedId)}
            style={secondaryButtonStyle}
          >
            Resume state
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
          No saved state selected. Choose one to inspect, or use Start new state to set up a fresh run.
        </div>
      )}

      {selectedId !== null && (
        <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 16 }}>
          {hasPendingOrUncertainOperations && (
            <div style={reconciliationBannerStyle} role="alert">
              This state cannot be resumed because it has pending or uncertain operations. Start a new state instead.
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
  flexDirection: "column",
  alignItems: "stretch",
  padding: "12px 14px",
  borderBottom: `1px solid ${theme.color.border}`,
  gap: 12,
  flexWrap: "wrap",
};

const headerActionsStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
  alignItems: "center",
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
