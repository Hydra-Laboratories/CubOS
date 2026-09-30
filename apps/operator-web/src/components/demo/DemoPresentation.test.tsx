import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import DemoPresentation from "./DemoPresentation";
import type { CampaignPresentation } from "./types";

const presentation: CampaignPresentation = {
  schema_version: "1",
  campaign_id: "demo-7",
  campaign_name: "Eight well challenge",
  status: "running",
  target: { source: "selected_srgb", accepted: true, measurement: { rgb: [130, 20, 100], lab: [31, 51, -9] } },
  attempts: [
    { sequence: 1, reveal_event_sequence: 1, trial_id: "trial-1", well: "A3", recipe_ul: { red_ul: 150, yellow_ul: 50, blue_ul: 100 }, status: "succeeded", accepted: true, score_eligible: true, measurement: { delta_e: 8.2, rgb: [91, 31, 76] }, image_asset_id: "image-1" },
    { sequence: 2, reveal_event_sequence: 3, trial_id: "trial-2", well: "A4", recipe_ul: { red_ul: 125, yellow_ul: 50, blue_ul: 125 }, status: "succeeded", accepted: true, score_eligible: true, measurement: { delta_e: 2.4, rgb: [116, 26, 95], frame: { width_px: 800, height_px: 600 }, roi: { center_x_px: 400, center_y_px: 300, radius_px: 22 } }, image_asset_id: "image-2", raw_image_asset_id: "raw-2" },
  ],
  best: { trial_id: "trial-2", sequence: 3, delta_e: 2.4 },
  events: [
    { sequence: 1, kind: "measurement", server_time: "2026-09-30T12:00:00Z", elapsed_ms: 1000, trial_id: "trial-1", label: "Measured A3" },
    { sequence: 2, kind: "dispense", server_time: "2026-09-30T12:00:30Z", elapsed_ms: 30000, trial_id: "trial-2", label: "Making attempt 2" },
    { sequence: 3, kind: "measurement", server_time: "2026-09-30T12:01:00Z", elapsed_ms: 60000, trial_id: "trial-2", label: "Measured A4" },
  ],
  markers: [], partial: false, missing: [],
};

function renderDemo() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><DemoPresentation /></QueryClientProvider>);
}

beforeEach(() => {
  window.history.replaceState(null, "", "/?view=demo&campaign=demo-7");
});

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("DemoPresentation", () => {
  it("renders the latest real result and exports the presentation bundle", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/campaigns")) return new Response(JSON.stringify([{ campaign_id: "demo-7", spec: { name: "Eight well challenge" }, state: "running" }]), { status: 200 });
      return new Response(JSON.stringify(presentation), { status: 200 });
    });
    renderDemo();
    expect((await screen.findAllByText("2.40")).length).toBeGreaterThan(0);
    expect(screen.getByText("Selected sRGB")).toBeInTheDocument();
    expect(screen.queryByText("#demo-7")).not.toBeInTheDocument();
    expect((document.querySelectorAll(".demo-swatch")[1] as HTMLElement).style.backgroundImage).toBe("none");
    expect(screen.getByRole("link", { name: "Export data" })).toHaveAttribute("href", "/api/v1/campaigns/demo-7/presentation/export.zip");
    const best = screen.getByRole("img", { name: "Best so far A4" });
    expect(best.querySelector("image")).toHaveAttribute("href", "/api/v1/campaigns/demo-7/presentation/assets/raw-2");
    expect(screen.getByRole("img", { name: "Best color difference over visible attempts" }).querySelector(".demo-trace-line")?.getAttribute("d"))
      .toMatch(/^M0\.00,14\.00 L100\.00,/);
  });

  it("scrubs without exposing later attempts", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => String(input).endsWith("/campaigns")
      ? new Response("[]", { status: 200 })
      : new Response(JSON.stringify(presentation), { status: 200 }));
    renderDemo();
    await screen.findByRole("img", { name: "Best so far A4" });
    fireEvent.change(screen.getByLabelText("Replay timeline"), { target: { value: "0" } });
    expect((await screen.findAllByText("8.20")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("img", { name: "Attempt 2, A4" })).not.toBeInTheDocument();
    expect(screen.getByText("Measured A3")).toBeInTheDocument();
  });

  it("uses a local video object URL without camera or instrument requests", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => String(input).endsWith("/campaigns")
      ? new Response("[]", { status: 200 })
      : new Response(JSON.stringify(presentation), { status: 200 }));
    const createObjectURL = vi.fn(() => "blob:local-demo-video");
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    const { container } = renderDemo();
    await screen.findByRole("img", { name: "Best so far A4" });
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [new File(["video"], "wide-shot.mp4", { type: "video/mp4" })] } });
    expect(createObjectURL).toHaveBeenCalledOnce();
    const video = container.querySelector("video") as HTMLVideoElement;
    expect(video).toHaveAttribute("src", "blob:local-demo-video");
    fireEvent.change(screen.getByLabelText(/Video offset/), { target: { value: "500" } });
    fireEvent.change(screen.getByLabelText("Replay timeline"), { target: { value: "0" } });
    await waitFor(() => expect(video.currentTime).toBe(1.5));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls.some(([input, init]) => String(input).includes("instrument") || init?.method === "POST")).toBe(false);
  });
});
