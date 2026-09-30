import { describe, expect, it } from "vitest";
import { bestAttempt, eventFootageTimeMs, eventIndexForFootage, friendlyEventLabel, isScoredAttempt, visiblePresentation } from "./replay";
import type { CampaignPresentation, DemoAttempt } from "./types";

const attempt = (overrides: Partial<DemoAttempt>): DemoAttempt => ({
  sequence: 1,
  reveal_event_sequence: 1,
  trial_id: "trial-1",
  well: "A1",
  recipe_ul: { red_ul: 100, yellow_ul: 100, blue_ul: 100 },
  status: "succeeded",
  accepted: true,
  score_eligible: true,
  measurement: { delta_e: 8, rgb: [80, 30, 70] },
  ...overrides,
});

describe("demo replay", () => {
  it("excludes rejected and failed measurements from best-so-far", () => {
    const valid = attempt({ trial_id: "valid", measurement: { delta_e: 8 } });
    const rejected = attempt({ trial_id: "rejected", accepted: false, measurement: { delta_e: 0.1 } });
    const failed = attempt({ trial_id: "failed", status: "failed", measurement: { delta_e: 0.01 } });
    expect(isScoredAttempt(rejected)).toBe(false);
    expect(isScoredAttempt(failed)).toBe(false);
    expect(bestAttempt([valid, rejected, failed])?.trial_id).toBe("valid");
  });

  it("never leaks future attempts into a replay point", () => {
    const presentation: CampaignPresentation = {
      schema_version: "1",
      campaign_id: "campaign-1",
      status: "completed",
      target: { source: "rgb", accepted: true, measurement: { rgb: [100, 20, 80] } },
      attempts: [
        attempt({ sequence: 1, reveal_event_sequence: 10, trial_id: "early", measurement: { delta_e: 9 } }),
        attempt({ sequence: 2, reveal_event_sequence: 30, trial_id: "future", measurement: { delta_e: 1 } }),
      ],
      best: { sequence: 3, trial_id: "future", delta_e: 1 },
      events: [
        { sequence: 10, kind: "measurement", server_time: "2026-09-30T12:00:00Z", elapsed_ms: 1000, trial_id: "early", label: "Measured A1" },
        { sequence: 20, kind: "dispense", server_time: "2026-09-30T12:00:30Z", elapsed_ms: 30000, trial_id: "future", label: "Making A2" },
        { sequence: 30, kind: "measurement", server_time: "2026-09-30T12:01:00Z", elapsed_ms: 61000, trial_id: "future", label: "Measured A2" },
      ],
      markers: [], partial: false, missing: [],
    };
    const replay = visiblePresentation(presentation, 0);
    expect(replay.attempts.map((item) => item.trial_id)).toEqual(["early"]);
    expect(replay.best?.trial_id).toBe("early");
    expect(replay.visibleBest?.delta_e).toBe(9);
  });

  it("does not infer a reveal from a trial sequence", () => {
    const future = attempt({ sequence: 1, reveal_event_sequence: 50, trial_id: "future" });
    expect(visiblePresentation({
      schema_version: "1", campaign_id: "x", status: "running",
      target: { source: "rgb", accepted: true }, attempts: [future], best: null,
      events: [{ sequence: 20, kind: "mix", server_time: "2026-09-30T12:00:00Z", elapsed_ms: 2, trial_id: "future", label: "Mixing" }],
      markers: [], partial: false, missing: [],
    }, 0).attempts).toEqual([]);
  });

  it("maps replay events to a saved recording marker", () => {
    const events = [
      { sequence: 10, kind: "mix", server_time: "2026-09-30T12:00:00Z", elapsed_ms: 1000, trial_id: "a", label: "Mix" },
      { sequence: 11, kind: "measurement", server_time: "2026-09-30T12:00:05Z", elapsed_ms: 6000, trial_id: "a", label: "Measure" },
    ];
    const markers = [{ id: "m", sequence: 10, server_time: "2026-09-30T12:00:00Z", label: "sync", timeline_elapsed_ms: 1000, footage_offset_ms: 19_000 }];
    expect(eventFootageTimeMs(events[1], markers, 0)).toBe(25_000);
    expect(eventIndexForFootage(events, markers, 24_000, 0)).toBe(0);
    expect(eventIndexForFootage(events, markers, 25_000, 0)).toBe(1);
  });

  it("keeps technical connection errors out of the broadcast headline", () => {
    expect(friendlyEventLabel("PipetteConnectionError: serial port vanished", "failed")).toBe("Stopped · pipette disconnected");
    expect(friendlyEventLabel("Measured plate.A3", "running")).toBe("Measured plate.A3");
  });
});
