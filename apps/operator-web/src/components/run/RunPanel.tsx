import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import * as theme from "../../theme";
import { useRunSteps } from "../../hooks/useRunSteps";
import type { RunState } from "../../types";
import StepRow from "./StepRow";
import { activeStepIndex } from "./deriveStepViews";

const STATE_LABEL: Record<RunState, string> = {
  queued: "Queued",
  running: "Running",
  cancel_requested: "Cancelling",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

const STATE_COLOR: Record<RunState, string> = {
  queued: theme.color.textMuted,
  running: theme.color.accent,
  cancel_requested: theme.color.warning,
  succeeded: theme.color.success,
  failed: theme.color.danger,
  cancelled: theme.color.textMuted,
};

const panelStyle: CSSProperties = {
  ...theme.card,
  display: "flex",
  flexDirection: "column",
  gap: "0.75rem",
  padding: "0.9rem 1rem",
};

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.75rem",
  flexWrap: "wrap",
};

const listStyle: CSSProperties = {
  overflowY: "auto",
  display: "flex",
  flexDirection: "column",
  gap: 1,
  borderTop: `1px solid ${theme.color.border}`,
  paddingTop: "0.5rem",
};

const countsStyle: CSSProperties = {
  ...theme.mono,
  fontSize: 11,
  color: theme.color.textMuted,
  fontVariantNumeric: "tabular-nums",
  marginLeft: "auto",
};

function statePill(state: RunState): CSSProperties {
  return {
    ...theme.pill,
    color: STATE_COLOR[state],
    border: `1px solid ${STATE_COLOR[state]}`,
  };
}

interface RunResult {
  steps_executed?: number;
  campaign_id?: number;
}

/** One-line outcome for a finished run, or null while it is still going. */
function runSummary(state: RunState | undefined, result: unknown): string | null {
  if (state !== "succeeded") return null;
  const parts = ["Protocol complete"];
  const typed = (result ?? {}) as RunResult;
  if (typeof typed.steps_executed === "number") {
    parts.push(`${typed.steps_executed} steps executed`);
  }
  if (typeof typed.campaign_id === "number") {
    parts.push(`campaign #${typed.campaign_id} created`);
  }
  return `${parts.join(" — ")}.`;
}

export interface RunPanelProps {
  runId: string;
  onCancel?: () => void;
  isCancelling?: boolean;
  /**
   * Fill the available height instead of capping the step list. Set when the
   * panel owns a whole view rather than sitting inside a scrolling column.
   */
  fill?: boolean;
}

/**
 * Live step-by-step view of one run.
 *
 * Everything rendered here derives from `(plan, events)` fetched by `runId`
 * (see `useRunSteps`), so this panel can mount at any point in a run — after a
 * page refresh, or on a run that already finished — and show the same thing.
 */
export default function RunPanel({
  runId,
  onCancel,
  isCancelling,
  fill = false,
}: RunPanelProps) {
  const { steps, counts, record, isTerminal, planError } = useRunSteps(runId);
  const listRef = useRef<HTMLDivElement | null>(null);
  const activeRowRef = useRef<HTMLDivElement | null>(null);
  // Auto-scroll follows the run until the operator scrolls themselves; taking
  // the view back from someone reading an earlier step is worse than losing
  // the follow.
  const [following, setFollowing] = useState(true);
  const active = activeStepIndex(steps);

  useEffect(() => {
    if (!following || active === null) return;
    // Optional-call: jsdom (and some older browsers) do not implement it,
    // and losing auto-scroll must never break the run view.
    activeRowRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [active, following]);

  const state = record?.state ?? "queued";
  const summary = runSummary(record?.state, record?.result);

  return (
    <section
      style={fill ? { ...panelStyle, flex: "1 1 auto", minHeight: 0 } : panelStyle}
      aria-label="Run progress"
    >
      <div style={headerStyle}>
        <span style={statePill(state)}>{STATE_LABEL[state]}</span>
        <span style={{ ...theme.mono, fontSize: 11, color: theme.color.textMuted }}>
          {runId}
        </span>
        {record?.mock_mode && (
          <span
            style={{
              ...theme.pill,
              color: theme.color.textMuted,
              border: `1px solid ${theme.color.border}`,
            }}
          >
            Mock
          </span>
        )}
        <span style={countsStyle}>
          {counts.done + counts.skipped + counts.failed} / {counts.total} steps
          {counts.skipped > 0 && ` · ${counts.skipped} skipped`}
        </span>
        {!isTerminal && onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={isCancelling}
            style={{ ...theme.btn.danger, ...theme.btnSmall }}
          >
            {isCancelling ? "Cancelling…" : "Cancel run"}
          </button>
        )}
      </div>

      {planError && (
        <div style={{ ...theme.notice.error, fontSize: 12 }}>
          Could not load the step plan: {planError.message}
        </div>
      )}

      {record?.error && (
        <div style={{ ...theme.notice.error, fontSize: 12 }} role="alert">
          {record.error}
        </div>
      )}

      {/* A finished run has to report its outcome where the operator is
          actually looking, not only in the workflow footer they navigated
          away from. */}
      {summary && (
        <div style={{ ...theme.notice.success, fontSize: 12 }} role="status">
          {summary}
        </div>
      )}

      {steps.length === 0 && !planError ? (
        <div style={{ fontSize: 12, color: theme.color.textMuted }}>
          Compiling steps…
        </div>
      ) : (
        <div
          ref={listRef}
          style={fill ? { ...listStyle, flex: "1 1 auto", minHeight: 0 } : { ...listStyle, maxHeight: "22rem" }}
          onWheel={() => setFollowing(false)}
          onTouchMove={() => setFollowing(false)}
          data-testid="step-list"
        >
          {steps.map((step) => (
            <div
              key={step.index}
              ref={step.index === active ? activeRowRef : undefined}
            >
              <StepRow step={step} />
            </div>
          ))}
        </div>
      )}

      {!following && active !== null && (
        <button
          type="button"
          style={{ ...theme.btn.ghost, ...theme.btnSmall, alignSelf: "flex-start" }}
          onClick={() => setFollowing(true)}
        >
          Follow current step
        </button>
      )}
    </section>
  );
}
