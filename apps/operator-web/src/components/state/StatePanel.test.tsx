import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import StatePanel from "./StatePanel";
import type {
  CapStateResponse,
  FluidStateDetail,
  FluidStateSummary,
  TipStateResponse,
} from "../../types";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

const SUMMARY: FluidStateSummary = {
  id: 1,
  label: "older run",
  deck_path: "/decks/demo.yaml",
  deck_fingerprint: "a".repeat(64),
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:05:00Z",
  container_count: 1,
  operation_count: 0,
};

const NEWEST_SUMMARY: FluidStateSummary = { ...SUMMARY, id: 2, label: "newest run" };

const DETAIL: FluidStateDetail = {
  ...SUMMARY,
  containers: [{
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
  }],
  pending_operation_count: 0,
  reconciliation_required_count: 0,
};

const TIPS: TipStateResponse = {
  fluid_state_id: 1,
  containers: [{ rack_key: "rack", slot_id: "A1", status: "available", tip_length_mm: 50, version: 0, updated_at: "now" }],
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
  containers: [{ labware_key: "source", location_id: "", status: "capped", version: 0, updated_at: "now" }],
};

function installFetchMock(options: { pending?: boolean } = {}) {
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
      "http://localhost",
    );
    const path = url.pathname;
    const method = init?.method ?? "GET";
    if (path === "/api/v1/fluid-states" && method === "GET") return jsonResponse([NEWEST_SUMMARY, SUMMARY]);
    if (path === "/api/v1/fluid-states/1" && method === "GET") return jsonResponse(DETAIL);
    if (path === "/api/v1/fluid-states/2" && method === "GET") {
      return jsonResponse({
        ...DETAIL,
        ...NEWEST_SUMMARY,
        pending_operation_count: options.pending ? 1 : 0,
        reconciliation_required_count: options.pending ? 1 : 0,
      });
    }
    if (path.match(/^\/api\/v1\/fluid-states\/[12]\/tips$/)) return jsonResponse(TIPS);
    if (path.match(/^\/api\/v1\/fluid-states\/[12]\/caps$/)) return jsonResponse(CAPS);
    return new Response("Not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPanel(options: { pending?: boolean } = {}) {
  const onStartNewState = vi.fn();
  const onResumeState = vi.fn();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  const fetchMock = installFetchMock(options);
  render(
    <QueryClientProvider client={client}>
      <StatePanel onStartNewState={onStartNewState} onResumeState={onResumeState} />
    </QueryClientProvider>,
  );
  return { fetchMock, onStartNewState, onResumeState };
}

describe("StatePanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a populated state as read-only containers, tips, and caps", async () => {
    const { fetchMock } = renderPanel();

    expect((await screen.findAllByText("source")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("100.000 / 400.000")).toBeInTheDocument();
    expect(screen.getByText("buffer: 100.000")).toBeInTheDocument();
    expect(screen.getByText(/tip attached from/)).toBeInTheDocument();
    expect(screen.getByText("capped")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start new state" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Resume state" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Resolve" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Correct volume/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
  });

  it("starts a fresh state through the workflow callback", async () => {
    const { onStartNewState } = renderPanel();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Start new state" }));
    expect(onStartNewState).toHaveBeenCalledOnce();
  });

  it("resumes the selected state instead of the newest state", async () => {
    const { onResumeState } = renderPanel();
    const user = userEvent.setup();

    const select = screen.getByLabelText("Fluid state");
    await waitFor(() => expect(select).toHaveValue("2"));
    await user.selectOptions(select, "1");
    await user.click(screen.getByRole("button", { name: "Resume state" }));
    expect(onResumeState).toHaveBeenCalledWith(1);
  });

  it("blocks a pending state from resuming while still allowing a new state", async () => {
    const { onStartNewState } = renderPanel({ pending: true });
    const user = userEvent.setup();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This state cannot be resumed because it has pending or uncertain operations. Start a new state instead.",
    );
    expect(screen.getByRole("button", { name: "Resume state" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Start new state" }));
    expect(onStartNewState).toHaveBeenCalledOnce();
  });
});
