import type { CampaignPresentation, DemoAttempt, DemoBest, DemoEvent, DemoMarker } from "./types";

const SCORED_STATUSES = new Set(["succeeded", "completed"]);

export function friendlyEventLabel(label: string, campaignStatus: string): string {
  if (/PipetteConnectionError|pipette.+disconnect/i.test(label)) return "Stopped · pipette disconnected";
  if (/CameraConnectionError|camera.+disconnect/i.test(label)) return "Stopped · camera disconnected";
  if ((campaignStatus === "failed" || campaignStatus === "interrupted") && (/error|traceback/i.test(label) || label.length > 100)) {
    return "Stopped · run needs attention";
  }
  return label;
}

export function isScoredAttempt(attempt: DemoAttempt): boolean {
  const delta = attempt.measurement?.delta_e;
  return attempt.accepted
    && attempt.score_eligible
    && SCORED_STATUSES.has(attempt.status)
    && typeof delta === "number"
    && Number.isFinite(delta);
}

export function attemptsAtSequence(attempts: DemoAttempt[], sequence: number): DemoAttempt[] {
  return attempts.filter((attempt) =>
    typeof attempt.reveal_event_sequence === "number" && attempt.reveal_event_sequence <= sequence
  );
}

export function bestAttempt(attempts: DemoAttempt[]): DemoAttempt | null {
  return attempts.reduce<DemoAttempt | null>((best, attempt) => {
    if (!isScoredAttempt(attempt)) return best;
    if (!best) return attempt;
    return (attempt.measurement?.delta_e ?? Infinity) < (best.measurement?.delta_e ?? Infinity)
      ? attempt
      : best;
  }, null);
}

export function visiblePresentation(presentation: CampaignPresentation, eventIndex: number) {
  const events = [...presentation.events].sort((left, right) => left.sequence - right.sequence);
  const safeIndex = Math.min(Math.max(eventIndex, 0), Math.max(events.length - 1, 0));
  const event: DemoEvent | null = events[safeIndex] ?? null;
  const visibleAttempts = event ? attemptsAtSequence(presentation.attempts, event.sequence) : [];
  const current = visibleAttempts.at(-1) ?? null;
  const best = bestAttempt(visibleAttempts);
  const visibleBest: DemoBest | null = best && typeof best.measurement?.delta_e === "number"
    ? { trial_id: best.trial_id, sequence: best.sequence, delta_e: best.measurement.delta_e }
    : null;
  return { events, safeIndex, event, attempts: visibleAttempts, current, best, visibleBest };
}

function markerForEvent(event: DemoEvent, markers: DemoMarker[]): DemoMarker | null {
  return markers.reduce<DemoMarker | null>((nearest, marker) => {
    if (marker.footage_offset_ms == null || marker.timeline_elapsed_ms == null || !Number.isFinite(marker.footage_offset_ms)) return nearest;
    if (!nearest) return marker;
    return Math.abs(marker.timeline_elapsed_ms - event.elapsed_ms) < Math.abs((nearest.timeline_elapsed_ms ?? 0) - event.elapsed_ms)
      ? marker
      : nearest;
  }, null);
}

export function eventFootageTimeMs(event: DemoEvent, markers: DemoMarker[], fallbackOffsetMs: number): number {
  const marker = markerForEvent(event, markers);
  if (!marker || marker.footage_offset_ms == null) return event.elapsed_ms + fallbackOffsetMs;
  return event.elapsed_ms + marker.footage_offset_ms;
}

export function eventIndexForFootage(events: DemoEvent[], markers: DemoMarker[], footageTimeMs: number, fallbackOffsetMs: number): number {
  let selected = 0;
  events.forEach((event, index) => {
    if (eventFootageTimeMs(event, markers, fallbackOffsetMs) <= footageTimeMs) selected = index;
  });
  return selected;
}
