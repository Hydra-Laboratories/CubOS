import type { PlanStep, RunEvent, StepEventData, StepOutcome } from "../../types";

export type StepStatus = "pending" | "active" | "done" | "failed" | "skipped";

export interface SubstepView {
  /** Colon-joined scope path, e.g. "leg2" or "cycle0:fill". */
  key: string;
  /** Display label: the last segment, with parents shown by indent depth. */
  label: string;
  depth: number;
  status: StepStatus;
  durationS: number | null;
  error: string | null;
  reason: string | null;
}

export interface StepView {
  index: number;
  command: string;
  summary: string;
  status: StepStatus;
  durationS: number | null;
  error: string | null;
  reason: string | null;
  substeps: SubstepView[];
}

const OUTCOME_STATUS: Record<StepOutcome, StepStatus> = {
  started: "active",
  completed: "done",
  failed: "failed",
  skipped: "skipped",
};

function isStepEventData(value: unknown): value is StepEventData {
  if (typeof value !== "object" || value === null) return false;
  const data = value as Record<string, unknown>;
  return typeof data.index === "number" && typeof data.command === "string";
}

function statusFor(outcome: unknown): StepStatus | null {
  if (typeof outcome !== "string") return null;
  return OUTCOME_STATUS[outcome as StepOutcome] ?? null;
}

/**
 * Resolve a new status against the one already recorded.
 *
 * A skipped command still returns normally, so the engine emits
 * `step_completed` right after `step_skipped` for the same scope. The skip is
 * the more specific fact and has to survive that, or a resumed run renders as
 * though it did the work in this process. A later failure still wins, since
 * that is a real outcome rather than a bookkeeping artifact.
 */
function mergeStatus(current: StepStatus, incoming: StepStatus): StepStatus {
  if (current === "skipped" && incoming === "done") return current;
  return incoming;
}

/**
 * Reduce a run's compiled plan plus its event stream into a renderable step list.
 *
 * Deliberately a pure function of `(plan, events)` — both fetched from the
 * server by `run_id`. Nothing about which step is running is held in component
 * state, so remounting mid-run (a page refresh, a tab switch) reconstructs
 * exactly the same view. Any run progress that cannot be rebuilt from these
 * two inputs is a bug, not a feature.
 *
 * Unrecognized event kinds and outcomes are ignored rather than thrown on, so
 * a server that starts emitting new event types (an operator-intervention
 * gate, preflight phases) degrades to "not shown here" instead of breaking the
 * run view.
 *
 * A `skipped` step is not a step that never ran — it is one the durable
 * fluid/tip journal reports as already applied by an earlier run. Callers
 * must keep the two visually distinct, so `reason` is always carried through.
 */
export function deriveStepViews(plan: PlanStep[], events: RunEvent[]): StepView[] {
  const views = new Map<number, StepView>();
  for (const step of plan) {
    views.set(step.index, {
      index: step.index,
      command: step.command,
      summary: step.summary,
      status: "pending",
      durationS: null,
      error: null,
      reason: null,
      substeps: [],
    });
  }

  // Substeps are keyed per (step index, scope path) so a repeated scope in a
  // later step does not collide with an earlier one.
  const substeps = new Map<string, SubstepView>();

  for (const event of events) {
    if (event.kind !== "step") continue;
    if (!isStepEventData(event.data)) continue;
    const data = event.data as unknown as StepEventData;
    const status = statusFor(data.outcome);
    if (status === null) continue;

    const durationS = typeof data.duration_s === "number" ? data.duration_s : null;
    const error = typeof data.error === "string" ? data.error : null;
    const reason = typeof data.reason === "string" ? data.reason : null;

    if (data.substep) {
      const view = views.get(data.index);
      if (!view) continue;
      const key = `${data.index}:${data.substep}`;
      const segments = data.substep.split(":");
      const existing = substeps.get(key);
      if (existing) {
        existing.status = mergeStatus(existing.status, status);
        existing.durationS = durationS ?? existing.durationS;
        existing.error = error ?? existing.error;
        existing.reason = reason ?? existing.reason;
      } else {
        const substep: SubstepView = {
          key,
          label: segments[segments.length - 1],
          depth: segments.length - 1,
          status,
          durationS,
          error,
          reason,
        };
        substeps.set(key, substep);
        view.substeps.push(substep);
      }
      continue;
    }

    const view = views.get(data.index);
    if (!view) continue;
    view.status = mergeStatus(view.status, status);
    view.durationS = durationS ?? view.durationS;
    view.error = error ?? view.error;
    view.reason = reason ?? view.reason;
  }

  return [...views.values()].sort((a, b) => a.index - b.index);
}

/** Index of the step to keep in view: the running one, else the failed one. */
export function activeStepIndex(steps: StepView[]): number | null {
  const active = steps.find((step) => step.status === "active");
  if (active) return active.index;
  const failed = steps.find((step) => step.status === "failed");
  return failed ? failed.index : null;
}

export interface StepCounts {
  total: number;
  done: number;
  skipped: number;
  failed: number;
  pending: number;
}

export function countSteps(steps: StepView[]): StepCounts {
  const counts: StepCounts = {
    total: steps.length,
    done: 0,
    skipped: 0,
    failed: 0,
    pending: 0,
  };
  for (const step of steps) {
    if (step.status === "done") counts.done += 1;
    else if (step.status === "skipped") counts.skipped += 1;
    else if (step.status === "failed") counts.failed += 1;
    else if (step.status === "pending") counts.pending += 1;
  }
  return counts;
}

/** Highest sequence number seen, for the `?after=` event cursor. */
export function lastSequence(events: RunEvent[]): number {
  let highest = 0;
  for (const event of events) {
    if (event.sequence > highest) highest = event.sequence;
  }
  return highest;
}
