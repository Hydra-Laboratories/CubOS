import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import InstrumentOffsetCalibrationModal from "./InstrumentOffsetCalibrationModal";
import { gantryApi } from "../../api/client";
import type { GantryConfig, GantryPosition } from "../../types";

vi.mock("./CameraPreview", () => ({ default: () => <div>Camera preview</div> }));
const config: GantryConfig = {
  serial_port: "/dev/offline", gantry_type: "cub_xl", origin_policy: "home_origin",
  cnc: { factory_z_travel_mm: 110 },
  working_volume: { x_min: -300, x_max: 0, y_min: -200, y_max: 0, z_min: -110, z_max: 0 },
  instruments: {
    pipette: { type: "pipette", vendor: "sartorius", offset_x: 12, offset_y: -3, depth: -8 },
    camera: { type: "camera", vendor: "usb", offset_x: 0, offset_y: 0 },
  },
};
const position: GantryPosition = { x: -100, y: -50, z: -20, work_x: -100, work_y: -50, work_z: -20, connected: true, status: "Idle", calibration_active: false };
function setup(overrides: Partial<GantryPosition> = {}) {
  const save = vi.fn(async () => undefined);
  const close = vi.fn();
  const live = vi.spyOn(gantryApi, "getPosition").mockResolvedValue({ ...position, ...overrides });
  const review = vi.spyOn(gantryApi, "previewInstrumentOffsets").mockResolvedValue(config);
  render(<InstrumentOffsetCalibrationModal open gantry={{ filename: "hein.yaml", config }} position={{ ...position, ...overrides }} onClose={close} onSaveCalibrated={save} />);
  return { save, close, live, review, user: userEvent.setup() };
}
afterEach(() => vi.restoreAllMocks());
const confirm = () => screen.getByRole("checkbox", { name: /existing offsets and depth are calibrated/ });

describe("offset-only calibration", () => {
  it("records tip compensation as a snapshot, preserves it through settings changes, and saves the reviewed config", async () => {
    const { user, live, review, save, close } = setup();
    const home = vi.spyOn(gantryApi, "home");
    const origin = vi.spyOn(gantryApi, "setWorkCoordinates");
    const limits = vi.spyOn(gantryApi, "prepareCalibrationOrigin");
    await user.click(confirm());
    await user.click(screen.getByRole("checkbox", { name: "Tip attached for this capture" }));
    const tip = screen.getByLabelText("Tip protrusion below bare nozzle (mm)");
    await user.type(tip, "40");
    await user.click(screen.getByRole("button", { name: "Record pipette at fixed mark" }));
    await screen.findByText(/pipette: recorded/);
    await user.clear(tip);
    await user.type(tip, "65");
    await user.click(screen.getByRole("checkbox", { name: "Tip attached for this capture" }));
    await user.selectOptions(screen.getByLabelText("Instrument to record"), "camera");
    await user.type(screen.getByLabelText("Camera stand-off from fixed mark (mm)"), "30");
    live.mockResolvedValue({ ...position, work_x: -90, work_y: -55, work_z: -10 });
    await user.click(screen.getByRole("button", { name: "Record camera at fixed mark" }));
    await screen.findByText(/camera: recorded/);
    await user.click(screen.getByRole("button", { name: "Review offsets" }));
    expect(review).toHaveBeenCalledWith({ config, reference_instrument: "pipette", captures: {
      pipette: { x: -100, y: -50, z: -20, tip_length_mm: 40 },
      camera: { x: -90, y: -55, z: -10, tip_length_mm: 0, stand_off_mm: 30 },
    } });
    await user.click(await screen.findByRole("button", { name: "Save instrument offsets" }));
    expect(save).toHaveBeenCalledWith("hein_offsets.yaml", config);
    expect(close).toHaveBeenCalled();
    expect(home).not.toHaveBeenCalled(); expect(origin).not.toHaveBeenCalled(); expect(limits).not.toHaveBeenCalled();
  });

  it("requires reference confirmation and validates attached-tip length before fetching coordinates", async () => {
    const { user, live } = setup();
    expect(screen.getByRole("button", { name: "Record pipette at fixed mark" })).toBeDisabled();
    await user.click(confirm());
    await user.click(screen.getByRole("checkbox", { name: "Tip attached for this capture" }));
    await user.click(screen.getByRole("button", { name: "Record pipette at fixed mark" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("tip protrusion");
    expect(live).not.toHaveBeenCalled();
  });

  it("rejects stale moving live positions and never records missing work coordinates", async () => {
    const { user, live } = setup();
    await user.click(confirm());
    live.mockResolvedValue({ ...position, status: "Jog" });
    await user.click(screen.getByRole("button", { name: "Record pipette at fixed mark" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("connected and idle");
    live.mockResolvedValue({ ...position, work_z: null });
    await user.click(screen.getByRole("button", { name: "Record pipette at fixed mark" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Work coordinates are unavailable");
    expect(screen.queryByText(/pipette: recorded/)).not.toBeInTheDocument();
  });

  it.each(["Alarm", "Run", "Hold"])("locks captures and jogs while %s", status => {
    setup({ status });
    expect(screen.getByRole("button", { name: "Record pipette at fixed mark" })).toBeDisabled();
    expect(screen.getByTitle("Z-")).toBeDisabled();
  });

  it("sends only a normal bounded blocking jog when requested", async () => {
    setup();
    const jog = vi.spyOn(gantryApi, "jogBlocking").mockResolvedValue(position);
    fireEvent.mouseDown(screen.getByTitle("X+"));
    await waitFor(() => expect(jog).toHaveBeenCalledWith(0.5, 0, 0));
    expect(jog).toHaveBeenCalledTimes(1);
  });

  it("displays save failures and retains captures for retry", async () => {
    const { user, save } = setup();
    await user.click(confirm());
    await user.click(screen.getByRole("button", { name: "Record pipette at fixed mark" }));
    await screen.findByText(/pipette: recorded/);
    await user.selectOptions(screen.getByLabelText("Instrument to record"), "camera");
    await user.type(screen.getByLabelText("Camera stand-off from fixed mark (mm)"), "0");
    await user.click(screen.getByRole("button", { name: "Record camera at fixed mark" }));
    await screen.findByText(/camera: recorded/);
    await user.click(screen.getByRole("button", { name: "Review offsets" }));
    save.mockRejectedValueOnce(new Error("Disk full"));
    await user.click(await screen.findByRole("button", { name: "Save instrument offsets" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Disk full");
    expect(screen.getByText(/pipette: recorded/)).toBeInTheDocument();
  });
});
