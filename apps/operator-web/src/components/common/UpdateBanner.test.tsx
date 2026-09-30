import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { systemApi } from "../../api/client";
import { UpdateBanner } from "./UpdateBanner";

vi.mock("../../api/client", () => ({
  systemApi: {
    getUpdateStatus: vi.fn(),
    applyUpdate: vi.fn(),
    health: vi.fn(),
  },
}));

const update = {
  current_sha: "1111111111111111111111111111111111111111",
  latest_sha: "2222222222222222222222222222222222222222",
  commits_behind: 2,
  update_available: true,
  checked_at: 1,
  summary: ["2222222 update"],
  error: null,
  current_tag: null,
  latest_tag: null,
};

describe("UpdateBanner", () => {
  beforeEach(() => {
    vi.mocked(systemApi.getUpdateStatus).mockReset();
    vi.mocked(systemApi.applyUpdate).mockReset();
    vi.mocked(systemApi.health).mockReset();
  });

  it("is hidden when no update is available", async () => {
    vi.mocked(systemApi.getUpdateStatus).mockResolvedValue({
      ...update,
      latest_sha: update.current_sha,
      commits_behind: 0,
      update_available: false,
      summary: [],
    });

    render(<UpdateBanner requestConfirm={vi.fn()} />);

    await waitFor(() => expect(systemApi.getUpdateStatus).toHaveBeenCalled());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows the current and latest short SHAs", async () => {
    vi.mocked(systemApi.getUpdateStatus).mockResolvedValue(update);

    render(<UpdateBanner requestConfirm={vi.fn()} />);

    expect(await screen.findByText(/1111111 → 2222222, 2 commits/)).toBeInTheDocument();
  });

  it("prefers release tag names over SHAs when in tag mode", async () => {
    vi.mocked(systemApi.getUpdateStatus).mockResolvedValue({
      ...update,
      current_tag: "v2026.08.03",
      latest_tag: "v2026.08.10",
    });

    render(<UpdateBanner requestConfirm={vi.fn()} />);

    expect(
      await screen.findByText(/v2026\.08\.03 → v2026\.08\.10, 2 commits/),
    ).toBeInTheDocument();
  });

  it("applies the update after confirmation", async () => {
    const user = userEvent.setup();
    const requestConfirm = vi.fn().mockResolvedValue(true);
    vi.mocked(systemApi.getUpdateStatus).mockResolvedValue(update);
    vi.mocked(systemApi.applyUpdate).mockResolvedValue({
      status: "updating",
      target_sha: update.latest_sha,
    });
    render(<UpdateBanner requestConfirm={requestConfirm} />);

    await user.click(await screen.findByRole("button", { name: "Update & restart" }));

    expect(requestConfirm).toHaveBeenCalledWith({
      title: "Update CubOS",
      message:
        "Update CubOS and restart the service? Any unsaved work in progress will be interrupted.",
      confirmLabel: "Update & restart",
    });
    await waitFor(() => expect(systemApi.applyUpdate).toHaveBeenCalledOnce());
    expect(await screen.findByText("Updating…")).toBeInTheDocument();
  });

  it("reloads once the service reports the target revision", async () => {
    const user = userEvent.setup();
    const reloadPage = vi.fn();
    vi.mocked(systemApi.getUpdateStatus)
      .mockResolvedValueOnce(update)
      .mockResolvedValue({ ...update, current_sha: update.latest_sha });
    vi.mocked(systemApi.applyUpdate).mockResolvedValue({
      status: "updating",
      target_sha: update.latest_sha,
    });
    render(
      <UpdateBanner
        requestConfirm={vi.fn().mockResolvedValue(true)}
        reloadPage={reloadPage}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Update & restart" }));

    await waitFor(() => expect(reloadPage).toHaveBeenCalled());
  });

  it("reloads when the restarted service predates the update endpoint", async () => {
    const user = userEvent.setup();
    const reloadPage = vi.fn();
    vi.mocked(systemApi.getUpdateStatus)
      .mockResolvedValueOnce(update)
      .mockRejectedValue(Object.assign(new Error("Not Found"), { status: 404 }));
    vi.mocked(systemApi.applyUpdate).mockResolvedValue({
      status: "updating",
      target_sha: update.latest_sha,
    });
    render(
      <UpdateBanner
        requestConfirm={vi.fn().mockResolvedValue(true)}
        reloadPage={reloadPage}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Update & restart" }));

    await waitFor(() => expect(reloadPage).toHaveBeenCalled());
  });

  it("renders an apply error inline", async () => {
    const user = userEvent.setup();
    vi.mocked(systemApi.getUpdateStatus).mockResolvedValue(update);
    vi.mocked(systemApi.applyUpdate).mockRejectedValue(
      Object.assign(new Error("cannot update while run run-123 is active"), { status: 409 }),
    );
    render(<UpdateBanner requestConfirm={vi.fn().mockResolvedValue(true)} />);

    await user.click(await screen.findByRole("button", { name: "Update & restart" }));

    expect(
      await screen.findByText("cannot update while run run-123 is active"),
    ).toBeInTheDocument();
  });
});
