import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import CalibrationModeChooser from "./CalibrationModeChooser";

describe("calibration mode chooser", () => {
  it("offers both modes and routes each choice", async () => {
    const user = userEvent.setup();
    const full = vi.fn(), offsets = vi.fn();
    render(<CalibrationModeChooser onClose={vi.fn()} onFull={full} onOffsets={offsets} offsetsDisabledReason={null} />);
    expect(screen.getByRole("dialog", { name: "Choose calibration mode" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Full calibration" }));
    await user.click(screen.getByRole("button", { name: "Calibrate offsets only" }));
    expect(full).toHaveBeenCalledTimes(1);
    expect(offsets).toHaveBeenCalledTimes(1);
  });

  it("explains unavailable offsets while leaving full calibration available", () => {
    render(<CalibrationModeChooser onClose={vi.fn()} onFull={vi.fn()} onOffsets={vi.fn()} offsetsDisabledReason="Connect the gantry before calibrating offsets." />);
    expect(screen.getByRole("button", { name: "Full calibration" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Calibrate offsets only" })).toBeDisabled();
    expect(screen.getByText("Connect the gantry before calibrating offsets.")).toBeInTheDocument();
  });

  it("traps focus, supports cancellation and Escape, and restores the trigger focus", async () => {
    const user = userEvent.setup();
    const close = vi.fn();
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();
    const { unmount } = render(<CalibrationModeChooser onClose={close} onFull={vi.fn()} onOffsets={vi.fn()} offsetsDisabledReason={null} />);
    const full = screen.getByRole("button", { name: "Full calibration" });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(full).toHaveFocus();
    fireEvent.keyDown(full, { key: "Tab", shiftKey: true });
    expect(cancel).toHaveFocus();
    fireEvent.keyDown(cancel, { key: "Tab" });
    expect(full).toHaveFocus();
    fireEvent.keyDown(full, { key: "Escape" });
    await user.click(cancel);
    expect(close).toHaveBeenCalledTimes(2);
    unmount();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });
});
