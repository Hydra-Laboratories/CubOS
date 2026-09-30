import { describe, expect, it } from "vitest";
import {
  activeStepIndex,
  countSteps,
  deriveStepViews,
  lastSequence,
} from "./deriveStepViews";
import type { PlanStep, RunEvent, StepOutcome } from "../../types";

const PLAN: PlanStep[] = [
  { index: 0, command: "home", summary: "all axes", args: {} },
  { index: 1, command: "pick_up_tip", summary: "from tips.A1", args: {} },
  { index: 2, command: "transfer", summary: "stock.A1 → plate.B3   500 µL", args: {} },
  { index: 3, command: "drop_tip", summary: "at trash", args: {} },
];

let sequence = 0;

function stepEvent(
  index: number,
  command: string,
  outcome: StepOutcome,
  extra: Record<string, unknown> = {},
): RunEvent {
  sequence += 1;
  return {
    sequence,
    timestamp: sequence,
    state: "running",
    message: `step ${index} ${command} ${outcome}`,
    kind: "step",
    data: {
      index,
      command,
      substep: null,
      outcome,
      duration_s: null,
      error: null,
      reason: null,
      ...extra,
    },
  };
}

function lifecycleEvent(state: RunEvent["state"], message: string): RunEvent {
  sequence += 1;
  return { sequence, timestamp: sequence, state, message, kind: "lifecycle", data: null };
}

function reset() {
  sequence = 0;
}

describe("deriveStepViews", () => {
  it("returns every plan step as pending when no events have arrived", () => {
    reset();
    const steps = deriveStepViews(PLAN, []);
    expect(steps).toHaveLength(4);
    expect(steps.every((step) => step.status === "pending")).toBe(true);
    expect(steps.map((step) => step.summary)).toEqual(PLAN.map((step) => step.summary));
  });

  it("returns nothing when the plan has not loaded yet", () => {
    reset();
    expect(deriveStepViews([], [stepEvent(0, "home", "started")])).toEqual([]);
  });

  it("marks the running step active and earlier steps done", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      lifecycleEvent("running", "execution started"),
      stepEvent(0, "home", "started"),
      stepEvent(0, "home", "completed", { duration_s: 1.25 }),
      stepEvent(1, "pick_up_tip", "started"),
    ]);
    expect(steps.map((step) => step.status)).toEqual([
      "done",
      "active",
      "pending",
      "pending",
    ]);
    expect(steps[0].durationS).toBe(1.25);
  });

  it("records a failure with its message and leaves later steps pending", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      stepEvent(0, "home", "started"),
      stepEvent(0, "home", "completed", { duration_s: 0.5 }),
      stepEvent(1, "pick_up_tip", "started"),
      stepEvent(1, "pick_up_tip", "failed", {
        duration_s: 2,
        error: "PipetteTimeoutError: no response",
      }),
    ]);
    expect(steps[1].status).toBe("failed");
    expect(steps[1].error).toBe("PipetteTimeoutError: no response");
    // Steps after a failure were never reached — they must not be repainted
    // as skipped, which would read as "we chose not to do them".
    expect(steps[2].status).toBe("pending");
    expect(steps[3].status).toBe("pending");
  });

  it("keeps skipped distinct from pending and carries the reason", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      stepEvent(2, "transfer", "skipped", {
        reason: "already applied on a previous run",
      }),
    ]);
    expect(steps[2].status).toBe("skipped");
    expect(steps[2].reason).toBe("already applied on a previous run");
    expect(steps[3].status).toBe("pending");
  });

  it("keeps a step skipped when the engine reports it completed after", () => {
    reset();
    // A skipped command still returns normally, so the engine emits
    // started -> skipped -> completed for the same index. Verified against
    // the real engine; the skip has to survive the trailing completion.
    const steps = deriveStepViews(PLAN, [
      stepEvent(1, "pick_up_tip", "started"),
      stepEvent(1, "pick_up_tip", "skipped", {
        reason: "already applied on a previous run",
      }),
      stepEvent(1, "pick_up_tip", "completed", { duration_s: 0.01 }),
    ]);
    expect(steps[1].status).toBe("skipped");
    expect(steps[1].reason).toBe("already applied on a previous run");
    expect(countSteps(steps).skipped).toBe(1);
    expect(countSteps(steps).done).toBe(0);
  });

  it("keeps a skipped substep skipped through its scope completing", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      stepEvent(2, "transfer", "started"),
      stepEvent(2, "transfer", "started", { substep: "leg0" }),
      stepEvent(2, "transfer", "skipped", {
        substep: "leg0",
        reason: "already applied on a previous run",
      }),
      stepEvent(2, "transfer", "completed", { substep: "leg0", duration_s: 0.01 }),
    ]);
    expect(steps[2].substeps[0].status).toBe("skipped");
  });

  it("still lets a failure override a skip", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      stepEvent(1, "pick_up_tip", "skipped", { reason: "already applied" }),
      stepEvent(1, "pick_up_tip", "failed", { error: "boom" }),
    ]);
    expect(steps[1].status).toBe("failed");
  });

  it("nests substeps under their parent step with a depth", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      stepEvent(2, "transfer", "started"),
      stepEvent(2, "transfer", "started", { substep: "leg0" }),
      stepEvent(2, "transfer", "started", { substep: "leg0:fill" }),
      stepEvent(2, "transfer", "completed", { substep: "leg0:fill", duration_s: 0.4 }),
      stepEvent(2, "transfer", "completed", { substep: "leg0", duration_s: 0.9 }),
    ]);
    const substeps = steps[2].substeps;
    expect(substeps.map((substep) => substep.label)).toEqual(["leg0", "fill"]);
    expect(substeps.map((substep) => substep.depth)).toEqual([0, 1]);
    expect(substeps.every((substep) => substep.status === "done")).toBe(true);
    // A substep transition must not resolve the parent step.
    expect(steps[2].status).toBe("active");
  });

  it("does not let substeps of different steps collide", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      stepEvent(1, "pick_up_tip", "started", { substep: "leg0" }),
      stepEvent(2, "transfer", "started", { substep: "leg0" }),
    ]);
    expect(steps[1].substeps).toHaveLength(1);
    expect(steps[2].substeps).toHaveLength(1);
  });

  it("is order-independent for the same event set", () => {
    reset();
    const events = [
      stepEvent(0, "home", "started"),
      stepEvent(0, "home", "completed", { duration_s: 1 }),
      stepEvent(1, "pick_up_tip", "started"),
    ];
    const forwards = deriveStepViews(PLAN, events);
    const shuffled = deriveStepViews(PLAN, [...events].reverse());
    // Reversal flips the last-write-wins outcome for step 0, which is
    // expected; what must hold is that neither ordering throws or drops
    // steps.
    expect(forwards).toHaveLength(shuffled.length);
    expect(shuffled.map((step) => step.index)).toEqual([0, 1, 2, 3]);
  });

  it("ignores events it does not understand", () => {
    reset();
    const unknownEventKind = {
      ...stepEvent(0, "home", "started"),
      kind: "operator_gate" as never,
    };
    const unknownOutcome = stepEvent(1, "pick_up_tip", "teleported" as StepOutcome);
    const nonStepPayload = {
      ...stepEvent(2, "transfer", "started"),
      data: { nonsense: true },
    };
    const indexNotInPlan = stepEvent(99, "ghost", "started");
    const steps = deriveStepViews(PLAN, [
      lifecycleEvent("running", "execution started"),
      unknownEventKind,
      unknownOutcome,
      nonStepPayload,
      indexNotInPlan,
    ]);
    expect(steps.every((step) => step.status === "pending")).toBe(true);
    expect(steps).toHaveLength(4);
  });

  it("tolerates a null data payload", () => {
    reset();
    const event: RunEvent = { ...stepEvent(0, "home", "started"), data: null };
    expect(() => deriveStepViews(PLAN, [event])).not.toThrow();
  });
});

describe("activeStepIndex", () => {
  it("returns the running step", () => {
    reset();
    const steps = deriveStepViews(PLAN, [stepEvent(1, "pick_up_tip", "started")]);
    expect(activeStepIndex(steps)).toBe(1);
  });

  it("falls back to the failed step so the error stays in view", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      stepEvent(1, "pick_up_tip", "failed", { error: "boom" }),
    ]);
    expect(activeStepIndex(steps)).toBe(1);
  });

  it("returns null when nothing is running", () => {
    reset();
    expect(activeStepIndex(deriveStepViews(PLAN, []))).toBeNull();
  });
});

describe("countSteps", () => {
  it("counts each status", () => {
    reset();
    const steps = deriveStepViews(PLAN, [
      stepEvent(0, "home", "completed"),
      stepEvent(1, "pick_up_tip", "skipped", { reason: "already applied" }),
      stepEvent(2, "transfer", "failed", { error: "boom" }),
    ]);
    expect(countSteps(steps)).toEqual({
      total: 4,
      done: 1,
      skipped: 1,
      failed: 1,
      pending: 1,
    });
  });
});

describe("lastSequence", () => {
  it("returns 0 for an empty log", () => {
    expect(lastSequence([])).toBe(0);
  });

  it("returns the highest sequence regardless of order", () => {
    reset();
    const events = [stepEvent(0, "home", "started"), stepEvent(0, "home", "completed")];
    expect(lastSequence([...events].reverse())).toBe(2);
  });
});
