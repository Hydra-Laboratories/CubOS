import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import EditorTabs from "./EditorTabs";

describe("EditorTabs", () => {
  it("renders all tabs and switches on click", async () => {
    const user = userEvent.setup();
    const onTabChange = vi.fn();
    render(<EditorTabs activeTab="Gantry" onTabChange={onTabChange} />);

    expect(screen.getByRole("button", { name: "Gantry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deck" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Protocol" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Active Learning" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Deck" }));
    expect(onTabChange).toHaveBeenCalledWith("Deck");
  });

  it("shows an unsaved dot on dirty tabs without changing the accessible name", () => {
    render(<EditorTabs activeTab="Deck" onTabChange={vi.fn()} dirtyTabs={["Deck", "Protocol"]} />);

    // Accessible name stays the bare label (dot is aria-hidden).
    expect(screen.getByRole("button", { name: "Deck" })).toBeInTheDocument();
    expect(screen.getAllByTitle("Unsaved changes")).toHaveLength(2);
  });

  it("renders the loaded filename under the tab label", () => {
    render(
      <EditorTabs
        activeTab="Gantry"
        onTabChange={vi.fn()}
        loadedFilenames={{ Gantry: "cubos.yaml" }}
      />,
    );
    expect(screen.getByText("cubos.yaml")).toBeInTheDocument();
  });

  it("does not switch to a disabled tab when there is no message", async () => {
    const user = userEvent.setup();
    const onTabChange = vi.fn();
    render(
      <EditorTabs activeTab="Gantry" onTabChange={onTabChange} disabledTabs={["Protocol"]} />,
    );
    await user.click(screen.getByRole("button", { name: "Protocol" }));
    expect(onTabChange).not.toHaveBeenCalled();
  });

  it("switches to a disabled tab when a disabled message is provided, and renders it", async () => {
    const user = userEvent.setup();
    const onTabChange = vi.fn();
    render(
      <EditorTabs
        activeTab="Protocol"
        onTabChange={onTabChange}
        disabledTabs={["Protocol"]}
        disabledMessage="Please load Gantry, Deck configs first."
      />,
    );
    expect(screen.getByText("Please load Gantry, Deck configs first.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Protocol" }));
    expect(onTabChange).toHaveBeenCalledWith("Protocol");
  });
});
