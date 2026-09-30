import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import StatePanel from "./StatePanel";
import type {
  CapStateResponse,
  ContainerView,
  FluidStateDetail,
  FluidStateSummary,
  OperationView,
  ReconciliationResponse,
  TipStateResponse,
} from "../../types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SUMMARY: FluidStateSummary = {
  id: 1,
  label: "demo",
  deck_path: "/decks/demo.yaml",
  deck_fingerprint: "a".repeat(64),
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:05:00Z",
  container_count: 2,
  operation_count: 1,
};

const CONTAINERS: ContainerView[] = [
  {
    labware_key: "source",
    location_id: "",
    labware_type: "vial",
    capacity_ul: 500,
    working_volume_ul: 400,
    current_volume_ul: 100,
    composition: { buffer: 100 },
    version: 1,
    updated_at: "2026-07-01T00:05:00Z",
    role: "stock",
    solution: "buffer",
    allowed_solutions: null,
  },
  {
    labware_key: "waste",
    location_id: "",
    labware_type: "vial",
    capacity_ul: 500,
    working_volume_ul: 400,
    current_volume_ul: 0,
    composition: {},
    version: 0,
    updated_at: "2026-07-01T00:05:00Z",
    role: "waste",
    solution: null,
    allowed_solutions: null,
  },
];

const DETAIL: FluidStateDetail = {
  id: 1,
  deck_path: "/decks/demo.yaml",
  deck_fingerprint: "a".repeat(64),
  label: "demo",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:05:00Z",
  containers: CONTAINERS,
  pending_operation_count: 1,
  reconciliation_required_count: 1,
};

const TIPS: TipStateResponse = {
  fluid_state_id: 1,
  containers: [
    { rack_key: "rack", slot_id: "A1", status: "available", tip_length_mm: 50, version: 0, updated_at: "now" },
  ],
  pipette: {
    pipette_key: "pipette",
    rack_key: "rack",
    slot_id: "A2",
    tip_extension_mm: 12.5,
    contents_known_empty: true,
    attachment_uncertain: false,
    updated_at: "now",
  },
};

const CAPS: CapStateResponse = {
  fluid_state_id: 1,
  containers: [
    { labware_key: "source", location_id: "", status: "capped", version: 0, updated_at: "now" },
  ],
};

const RECONCILIATION_ITEM: OperationView = {
  domain: "fluid",
  id: 1,
  operation_key: "indeterminate",
  operation_type: "transfer",
  status: "reconciliation_required",
  campaign_id: 1,
  detail: "process stopped after pipette actuation",
  created_at: "now",
  updated_at: "now",
  applied_at: null,
  context: { source: "source", destination: "waste", volume_ul: 10, composition: {}, parameters: {} },
};

const RECONCILIATION: ReconciliationResponse = {
  fluid_state_id: 1,
  items: [RECONCILIATION_ITEM],
};

function installFetchMock(overrides: { reconciliation?: ReconciliationResponse } = {}) {
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
      "http://localhost",
    );
    const path = url.pathname;
    const method = init?.method ?? "GET";

    if (path === "/api/v1/fluid-states" && method === "GET") return jsonResponse([SUMMARY]);
    if (path === "/api/v1/fluid-states/1" && method === "GET") return jsonResponse(DETAIL);
    if (path === "/api/v1/fluid-states/1/tips") return jsonResponse(TIPS);
    if (path === "/api/v1/fluid-states/1/caps") return jsonResponse(CAPS);
    if (path === "/api/v1/fluid-states/1/reconciliation" && method === "GET") {
      return jsonResponse(overrides.reconciliation ?? RECONCILIATION);
    }
    if (path === "/api/v1/fluid-states/1/reconciliation/resolve" && method === "POST") {
      return jsonResponse({
        domain: "fluid",
        operation_key: "indeterminate",
        status: "applied",
        detail: "[alexc] confirmed via camera review",
      });
    }
    return new Response("Not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <StatePanel />
    </QueryClientProvider>,
  );
}

describe("StatePanel", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders containers with volumes/compositions/roles, tips, caps, and pending count", async () => {
    installFetchMock({ reconciliation: { fluid_state_id: 1, items: [] } });
    renderPanel();

    // Containers section: volume, composition, and role all present.
    // ("source" also appears in the Caps section below, hence AllBy.)
    expect((await screen.findAllByText("source")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("stock")).toBeInTheDocument();
    expect(screen.getByText("100.000 / 400.000")).toBeInTheDocument();
    expect(screen.getByText("buffer: 100.000")).toBeInTheDocument();
    expect(screen.getAllByText("waste").length).toBeGreaterThanOrEqual(1);

    // Tips section: attached pipette + extension, plus the tracked slot.
    expect(await screen.findByText(/tip attached from/)).toBeInTheDocument();
    expect(screen.getByText(/rack\.A2/)).toBeInTheDocument();
    expect(screen.getByText(/12\.50 mm/)).toBeInTheDocument();

    // Caps section.
    expect(await screen.findByText("capped")).toBeInTheDocument();

    // Pending-operation summary line.
    expect(await screen.findByText(/1 pending operation/)).toBeInTheDocument();
  });

  it("shows a prominent reconciliation-required warning with a resolve flow", async () => {
    const fetchMock = installFetchMock();
    const user = userEvent.setup();
    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("1 operation requires reconciliation");
    expect(alert).toHaveTextContent("indeterminate");

    await user.click(screen.getByRole("button", { name: "Resolve" }));
    expect(screen.getByText(/Resolve fluid operation indeterminate/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Submit resolution" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/fluid-states/1/reconciliation/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const [, init] = fetchMock.mock.calls.find(
      ([input]) => input === "/api/v1/fluid-states/1/reconciliation/resolve",
    )!;
    expect(JSON.parse(String(init?.body))).toMatchObject({
      domain: "fluid",
      operation_key: "indeterminate",
      resolution: "applied",
    });
    expect(screen.queryByPlaceholderText("Your name or initials")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/What did you observe/)).not.toBeInTheDocument();
  });

  it("shows an empty state when no fluid state is selected", async () => {
    const fetchMock = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    expect(await screen.findByText(/No fluid state selected/)).toBeInTheDocument();
  });

  // Regression: the placeholder option used to snap straight back to the
  // auto-selected newest state because "no explicit choice" and "explicitly
  // none" shared the same null sentinel.
  it("keeps an explicit 'no state' selection instead of snapping back to the newest state", async () => {
    installFetchMock({ reconciliation: { fluid_state_id: 1, items: [] } });
    const user = userEvent.setup();
    renderPanel();

    // The newest state auto-selects once the list loads.
    expect((await screen.findAllByText("source")).length).toBeGreaterThanOrEqual(1);
    const select = screen.getByLabelText("Fluid state") as HTMLSelectElement;
    expect(select.value).toBe("1");

    await user.selectOptions(select, "");

    expect(await screen.findByText(/No fluid state selected/)).toBeInTheDocument();
    expect(select.value).toBe("");
  });
});
