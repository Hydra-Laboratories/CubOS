import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import DataOutputPanel from "./DataOutputPanel";
import type { CampaignSummary } from "../../types";

function campaign(overrides: Partial<CampaignSummary> = {}): CampaignSummary {
  return {
    campaign_id: 1,
    campaign_description: "ASMI sample campaign",
    created_at: "2026-07-01T12:00:00Z",
    latest_measurement_at: "2026-07-01T12:05:00Z",
    experiment_count: 1,
    well_count: 1,
    measurement_count: 1,
    measurement_counts: {
      uvvis: 0,
      filmetrics: 0,
      uv_curing: 0,
      camera: 0,
      asmi: 1,
      potentiostat: 0,
    },
    asmi_measurement_count: 1,
    ...overrides,
  };
}

function renderPanel(overrides: Partial<React.ComponentProps<typeof DataOutputPanel>> = {}) {
  const props: React.ComponentProps<typeof DataOutputPanel> = {
    campaigns: [campaign()],
    isLoading: false,
    error: null,
    onRefresh: vi.fn(),
    ...overrides,
  };
  render(<DataOutputPanel {...props} />);
  return props;
}

describe("DataOutputPanel", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders load errors", () => {
    renderPanel({ campaigns: [], error: new Error("db locked") });
    expect(screen.getByText("Data load failed: db locked")).toBeInTheDocument();
  });

  it("renders the current empty state when no campaigns exist", () => {
    renderPanel({ campaigns: [] });
    expect(screen.getByText("No campaigns yet — run a protocol to create one.")).toBeInTheDocument();
  });

  it("disables Download Data when a campaign has no measurements", () => {
    renderPanel({
      campaigns: [campaign({ measurement_count: 0, asmi_measurement_count: 0 })],
    });

    expect(screen.getByRole("button", { name: "Download Data" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "ASMI ZIP" })).not.toBeInTheDocument();
  });

  it("shows export failures and re-enables the export button", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      new Response("zip writer failed", {
        status: 500,
        statusText: "Internal Server Error",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    const exportButton = screen.getByRole("button", { name: "Download Data" });
    await user.click(exportButton);

    expect(await screen.findByText("Export failed: zip writer failed")).toBeInTheDocument();
    await waitFor(() => expect(exportButton).toBeEnabled());
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/data/campaigns/1/data.zip");
  });
});
