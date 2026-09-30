import type { CSSProperties } from "react";
import * as theme from "../../theme";
import type { StepStatus, StepView, SubstepView } from "./deriveStepViews";

const STATUS_COLOR: Record<StepStatus, string> = {
  pending: theme.color.textFaint,
  active: theme.color.accent,
  done: theme.color.success,
  failed: theme.color.danger,
  skipped: theme.color.warning,
};

// Status is carried by a glyph as well as a color: an operator reading a run
// at a glance shouldn't have to distinguish hues, and screen readers get the
// label from the row's aria-label.
const STATUS_GLYPH: Record<StepStatus, string> = {
  pending: "○",
  active: "◐",
  done: "●",
  failed: "✕",
  skipped: "⤼",
};

const STATUS_LABEL: Record<StepStatus, string> = {
  pending: "pending",
  active: "running",
  done: "done",
  failed: "failed",
  skipped: "skipped",
};

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  // Round once, then split: rounding the remainder independently of the
  // minutes floor renders 119.6 s as "1m 60s", and 59.95 s as "60.0 s".
  const tenths = Math.round(seconds * 10) / 10;
  if (tenths < 60) return `${tenths.toFixed(1)} s`;
  const total = Math.round(tenths);
  return `${Math.floor(total / 60)}m ${total % 60}s`;
}

const rowStyle = (status: StepStatus): CSSProperties => ({
  display: "grid",
  gridTemplateColumns: "1.4rem 2.6rem minmax(0, 1fr) auto",
  alignItems: "baseline",
  gap: "0.6rem",
  padding: "0.32rem 0.6rem",
  borderRadius: theme.radius.sm,
  background: status === "active" ? theme.color.accentTint : "transparent",
  color: status === "pending" ? theme.color.textMuted : theme.color.text,
});

const glyphStyle = (status: StepStatus): CSSProperties => ({
  color: STATUS_COLOR[status],
  fontSize: 12,
  lineHeight: 1.6,
  textAlign: "center",
});

const indexStyle: CSSProperties = {
  ...theme.mono,
  fontSize: 11,
  color: theme.color.textFaint,
  fontVariantNumeric: "tabular-nums",
  textAlign: "right",
};

const commandStyle: CSSProperties = {
  ...theme.mono,
  fontSize: 12,
  fontWeight: 600,
};

const summaryStyle: CSSProperties = {
  fontSize: 12,
  color: theme.color.textSecondary,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const durationStyle: CSSProperties = {
  ...theme.mono,
  fontSize: 11,
  color: theme.color.textFaint,
  fontVariantNumeric: "tabular-nums",
  whiteSpace: "nowrap",
};

const noteStyle = (color: string): CSSProperties => ({
  gridColumn: "3 / span 2",
  fontSize: 11,
  color,
  whiteSpace: "normal",
});

export function SubstepRow({ substep }: { substep: SubstepView }) {
  return (
    <div
      style={{
        ...rowStyle(substep.status),
        paddingLeft: `${1.9 + substep.depth * 0.9}rem`,
      }}
      aria-label={`substep ${substep.label} ${STATUS_LABEL[substep.status]}`}
    >
      <span style={glyphStyle(substep.status)} aria-hidden="true">
        {STATUS_GLYPH[substep.status]}
      </span>
      <span style={indexStyle} />
      <span style={{ ...summaryStyle, ...theme.mono, fontSize: 11 }}>
        {substep.label}
      </span>
      <span style={durationStyle}>{formatDuration(substep.durationS)}</span>
      {substep.reason && (
        <span style={noteStyle(theme.color.warningText)}>{substep.reason}</span>
      )}
      {substep.error && (
        <span style={noteStyle(theme.color.dangerText)}>{substep.error}</span>
      )}
    </div>
  );
}

export default function StepRow({ step }: { step: StepView }) {
  return (
    <div data-testid={`step-row-${step.index}`}>
      <div
        style={rowStyle(step.status)}
        aria-label={`step ${step.index} ${step.command} ${STATUS_LABEL[step.status]}`}
      >
        <span style={glyphStyle(step.status)} aria-hidden="true">
          {STATUS_GLYPH[step.status]}
        </span>
        <span style={indexStyle}>{step.index}</span>
        <span style={{ display: "flex", gap: "0.7rem", minWidth: 0 }}>
          <span style={commandStyle}>{step.command}</span>
          <span style={summaryStyle}>{step.summary}</span>
        </span>
        <span style={durationStyle}>{formatDuration(step.durationS)}</span>
        {/* A skipped step ran on an earlier attempt — spelling out why keeps
            it from reading as "never happened". */}
        {step.reason && (
          <span style={noteStyle(theme.color.warningText)}>{step.reason}</span>
        )}
        {step.error && (
          <span style={noteStyle(theme.color.dangerText)}>{step.error}</span>
        )}
      </div>
      {step.substeps.map((substep) => (
        <SubstepRow key={substep.key} substep={substep} />
      ))}
    </div>
  );
}
