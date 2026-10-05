import { describe, expect, it, vi } from "vitest";
import { campaignToAssociate, isPhotoPauseEvent, stopMediaStream, supportedRecorderMime, syncEvent } from "./recording";
import type { DemoEvent } from "./types";

const photoEvent: DemoEvent = {
  sequence: 9,
  kind: "photo_pause",
  server_time: "2026-10-05T15:00:00Z",
  elapsed_ms: 42000,
  trial_id: "trial-3",
  label: "Photo pause complete",
  data: { capture_still: true, wait_completed: true },
};

describe("demo browser recording", () => {
  it("selects the first MediaRecorder MIME type supported by the browser", () => {
    const isTypeSupported = vi.fn((mime: string) => mime === "video/webm;codecs=vp8");
    expect(supportedRecorderMime({ isTypeSupported } as unknown as typeof MediaRecorder)).toBe("video/webm;codecs=vp8");
  });

  it("closes every media source track", () => {
    const tracks = [{ stop: vi.fn() }, { stop: vi.fn() }];
    stopMediaStream({ getTracks: () => tracks } as unknown as MediaStream);
    expect(tracks.every((track) => track.stop.mock.calls.length === 1)).toBe(true);
  });

  it("records an event against the monotonic video clock with explicit poll uncertainty", () => {
    const observed = syncEvent(photoEvent, 1000, 2750, 43, "2026-10-05T15:00:00.043Z");
    expect(observed).toMatchObject({
      sequence: 9,
      timeline_elapsed_ms: 42000,
      video_elapsed_ms: 1750,
      observation_uncertainty_ms: 43,
      video_time_basis: "poll_observation",
    });
    expect(isPhotoPauseEvent(photoEvent)).toBe(true);
    expect(isPhotoPauseEvent({ ...photoEvent, kind: "measurement" })).toBe(false);
    expect(isPhotoPauseEvent({ ...photoEvent, data: { capture_still: false } })).toBe(false);
    expect(isPhotoPauseEvent({ ...photoEvent, data: { capture_still: true, wait_completed: false } })).toBe(false);
  });

  it("associates only a newly created running campaign", () => {
    const baseline = new Map([["old", "completed"], ["prepared", "pending"]]);
    expect(campaignToAssociate([
      { campaign_id: "old", state: "completed", created_at: "2026-10-05T10:00:00Z" },
      { campaign_id: "prepared", state: "running", created_at: "2026-10-05T11:00:00Z" },
      { campaign_id: "new", state: "running", created_at: "2026-10-05T12:00:00Z" },
    ], baseline)?.campaign_id).toBe("new");
    expect(campaignToAssociate([
      { campaign_id: "prepared", state: "running" },
    ], baseline)).toBeNull();
    expect(campaignToAssociate([
      { campaign_id: "old", state: "completed" },
    ], baseline)).toBeNull();
  });
});
