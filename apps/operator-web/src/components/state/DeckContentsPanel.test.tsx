import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, afterEach } from "vitest";
import DeckContentsPanel from "./DeckContentsPanel";

const detail = {
  id: 1,
  deck_path: "deck.yaml",
  deck_fingerprint: "abc",
  label: "Morning setup",
  created_at: "2026-09-21T10:00:00Z",
  updated_at: "2026-09-21T10:00:00Z",
  pending_operation_count: 0,
  reconciliation_required_count: 0,
  containers: [{
    labware_key: "reservoir",
    location_id: "A1",
    labware_type: "vial",
    capacity_ul: 2000,
    working_volume_ul: 1800,
    current_volume_ul: 900,
    volume_known: true,
    composition: { water: 900 },
    version: 3,
    updated_at: "2026-09-21T10:00:00Z",
    role: "source",
    solution: "water",
    allowed_solutions: null,
  }],
};

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

function renderPanel(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><DeckContentsPanel deckFile="deck.yaml" /></QueryClientProvider>);
}

describe("DeckContentsPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the active setup, preserves unknown volume, and records a manual correction", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/fluid-states/active")) return json({ fluid_state_id: 1, revision: 4, updated_at: detail.updated_at });
      if (path.endsWith("/fluid-states")) return json([{ id: 1, label: detail.label, deck_path: "deck.yaml", deck_fingerprint: "abc", created_at: detail.created_at, updated_at: detail.updated_at, container_count: 1, operation_count: 0 }]);
      if (path.endsWith("/fluid-states/1")) return json(detail);
      if (path.endsWith("/fluid-states/1/manual-edits") && init?.method === "POST") return json(detail);
      if (path.endsWith("/fluid-states/1/manual-edits")) return json([]);
      throw new Error(`unexpected ${path}`);
    });
    renderPanel(fetchMock);
    expect((await screen.findAllByText("Active setup #1")).length).toBeGreaterThan(0);
    expect(await screen.findByText("900.0 µL")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Record manual change" }));
    await userEvent.clear(screen.getByLabelText(/Observed volume/));
    await userEvent.type(screen.getByLabelText(/Observed volume/), "850");
    await userEvent.click(screen.getByRole("button", { name: "Save record" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/fluid-states/1/manual-edits"), expect.objectContaining({ method: "POST" })));
    const request = fetchMock.mock.calls.find(([, options]) => (options as RequestInit | undefined)?.method === "POST");
    expect(JSON.parse(String((request?.[1] as RequestInit).body))).toMatchObject({ expected_active_revision: 4, expected_revisions: { "reservoir.A1": 3 } });
  });

  it("keeps an historical setup read-only until explicitly activated", async () => {
    const historical = { ...detail, id: 2, label: "Yesterday" };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/fluid-states/active")) return json({ fluid_state_id: 1, revision: 4, updated_at: detail.updated_at });
      if (path.endsWith("/fluid-states")) return json([{ id: 1, label: detail.label, deck_path: "deck.yaml", deck_fingerprint: "abc", created_at: detail.created_at, updated_at: detail.updated_at, container_count: 1, operation_count: 0 }, { id: 2, label: "Yesterday", deck_path: "deck.yaml", deck_fingerprint: "abc", created_at: detail.created_at, updated_at: detail.updated_at, container_count: 1, operation_count: 0 }]);
      if (path.endsWith("/fluid-states/1")) return json(detail);
      if (path.endsWith("/fluid-states/2")) return json(historical);
      if (path.includes("/manual-edits")) return json([]);
      throw new Error(`unexpected ${path}`);
    });
    renderPanel(fetchMock);
    const history = await screen.findByRole("combobox", { name: "Contents history" });
    await waitFor(() => expect(history.querySelector('option[value="2"]')).not.toBeNull());
    await userEvent.selectOptions(history, "2");
    expect(await screen.findByText("Historical view · viewing does not activate this setup")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record manual change" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Activate this setup" })).toBeEnabled();
  });
});
