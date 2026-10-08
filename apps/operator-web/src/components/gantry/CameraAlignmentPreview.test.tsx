import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CameraAlignmentPreview from "./CameraAlignmentPreview";
import { cameraMonitorApi } from "./cameraMonitorApi";
import type { CameraMonitorStatus } from "./cameraMonitorTypes";

vi.mock("./cameraMonitorApi", () => ({ cameraMonitorApi: {
  start: vi.fn(), get: vi.fn(), heartbeat: vi.fn(), stop: vi.fn(),
  frameUrl: vi.fn(() => "/camera-frame.png"),
} }));

const status = (age: number): CameraMonitorStatus => ({
  state: "running", frame_id: 7, lease_id: "owned-lease", error: null,
  frame_age_seconds: age, actual_resolution: { width: 800, height: 600 },
} as CameraMonitorStatus);

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(cameraMonitorApi.stop).mockResolvedValue(status(0));
});

describe("CameraAlignmentPreview", () => {
  it("uses the shared camera lease and releases only its own subscription on unmount", async () => {
    vi.mocked(cameraMonitorApi.start).mockResolvedValue(status(0.2));
    vi.mocked(cameraMonitorApi.get).mockResolvedValue(status(0.2));
    const onStatus = vi.fn();
    const view = render(<CameraAlignmentPreview instrument="camera" onAlignmentStatusChange={onStatus} />);
    expect(await screen.findByAltText("Live alignment preview from camera")).toHaveAttribute("src", "/camera-frame.png");
    await waitFor(() => expect(onStatus).toHaveBeenCalledWith({ ready: true, frameAgeSeconds: 0.2, error: null }));
    view.unmount();
    expect(cameraMonitorApi.stop).toHaveBeenCalledWith("camera", "owned-lease");
  });

  it("marks stale frames unavailable for alignment", async () => {
    vi.mocked(cameraMonitorApi.start).mockResolvedValue(status(4.2));
    vi.mocked(cameraMonitorApi.get).mockResolvedValue(status(4.2));
    const onStatus = vi.fn();
    render(<CameraAlignmentPreview instrument="camera" onAlignmentStatusChange={onStatus} />);
    expect(await screen.findByText("4.2 s old · stale")).toBeInTheDocument();
    expect(onStatus).toHaveBeenCalledWith({ ready: false, frameAgeSeconds: 4.2, error: null });
  });
});
