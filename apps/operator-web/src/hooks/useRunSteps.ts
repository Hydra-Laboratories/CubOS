import { useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { runsApi } from "../api/client";
import type { RunEvent, RunRecord } from "../types";
import {
  countSteps,
  deriveStepViews,
  lastSequence,
  type StepCounts,
  type StepView,
} from "../components/run/deriveStepViews";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);

/** Fast cadence while steps are arriving. */
const ACTIVE_INTERVAL_MS = 400;
/** Backed-off cadence for long quiet stretches (a slow measure, a long move). */
const IDLE_INTERVAL_MS = 2000;
/** Consecutive polls with no new events before backing off. */
const IDLE_POLLS_BEFORE_BACKOFF = 10;

/**
 * Accumulated event log for one run, held in the react-query cache.
 *
 * Keeping it in the cache rather than component state means it is keyed by
 * run id for free (switching runs cannot show the previous run's progress),
 * survives remounts, and needs no effects to maintain.
 */
interface EventLog {
  events: RunEvent[];
  quietPolls: number;
  /** True once a poll has run after the run reached a terminal state. */
  sawTerminal: boolean;
}

const EMPTY_LOG: EventLog = { events: [], quietPolls: 0, sawTerminal: false };

export interface RunStepsState {
  steps: StepView[];
  counts: StepCounts;
  record: RunRecord | null;
  isTerminal: boolean;
  planError: Error | null;
}

/**
 * Poll a run's event stream and reduce it against its compiled plan.
 *
 * The plan is fetched once per run id (derived from the run's stored
 * protocol, so it never changes). Events are fetched incrementally with an
 * `after` cursor, which keeps a multi-hour run from re-reading its whole
 * history every 400 ms.
 *
 * Everything returned is a function of the server's `(plan, events)`, so a
 * remount mid-run rebuilds the identical view.
 */
export function useRunSteps(runId: string | null): RunStepsState {
  const queryClient = useQueryClient();

  const plan = useQuery({
    queryKey: ["runs", runId, "plan"],
    queryFn: () => runsApi.plan(runId!),
    enabled: !!runId,
    staleTime: Infinity,
    retry: 1,
  });

  const record = useQuery({
    queryKey: ["runs", runId, "record"],
    queryFn: () => runsApi.get(runId!),
    enabled: !!runId,
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state && TERMINAL_STATES.has(state) ? false : ACTIVE_INTERVAL_MS;
    },
  });

  const isTerminal = !!record.data && TERMINAL_STATES.has(record.data.state);
  const eventsKey = useMemo(() => ["runs", runId, "events"], [runId]);

  const log = useQuery({
    queryKey: eventsKey,
    queryFn: async (): Promise<EventLog> => {
      const previous = queryClient.getQueryData<EventLog>(eventsKey) ?? EMPTY_LOG;
      // On the first poll after the run finishes, re-read from the start:
      // the last few step events can land between the final incremental poll
      // and the state flip. Merging is idempotent, so this is safe to repeat.
      const cursor = isTerminal ? 0 : lastSequence(previous.events);
      const response = await runsApi.events(runId!, cursor);
      const seen = new Set(previous.events.map((event) => event.sequence));
      const fresh = response.events.filter((event) => !seen.has(event.sequence));
      if (fresh.length === 0) {
        return {
          events: previous.events,
          quietPolls: previous.quietPolls + 1,
          sawTerminal: isTerminal,
        };
      }
      return {
        events: [...previous.events, ...fresh].sort(
          (a, b) => a.sequence - b.sequence,
        ),
        quietPolls: 0,
        sawTerminal: isTerminal,
      };
    },
    enabled: !!runId,
    refetchInterval: (query) => {
      const data = query.state.data;
      // Stop only after one poll has completed post-terminal, so the tail of
      // the log is never lost to a race with the state change.
      if (isTerminal && data?.sawTerminal) return false;
      const quiet = data?.quietPolls ?? 0;
      return quiet >= IDLE_POLLS_BEFORE_BACKOFF ? IDLE_INTERVAL_MS : ACTIVE_INTERVAL_MS;
    },
  });

  const events = log.data?.events ?? EMPTY_LOG.events;

  const steps = useMemo(
    () => deriveStepViews(plan.data?.steps ?? [], events),
    [plan.data, events],
  );

  return {
    steps,
    counts: countSteps(steps),
    record: record.data ?? null,
    isTerminal,
    planError: plan.error as Error | null,
  };
}
