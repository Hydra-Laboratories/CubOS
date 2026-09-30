import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import LabwareCalibrationModal from "./LabwareCalibrationModal";
import type {
  DeckConfig,
  DeckResponse,
  GantryPosition,
  GantryResponse,
  LabwareConfig,
  TipRackConfig,
  VialConfig,
  WellPlateConfig,
} from "../../types";

function wellPlate(): WellPlateConfig {
  return {
    type: "well_plate",
    name: "plate",
    model_name: "demo_plate",
    rows: 3,
    columns: 4,
    calibration: {
      a1: { x: 10, y: 20, z: -50 },
      a2: { x: 10, y: 35, z: -50 },
    },
    x_offset: 15,
    y_offset: 15,
  };
}

function vial(): VialConfig {
  return {
    type: "vial",
    name: "s1",
    model_name: "demo_vial",
    height: 80,
    diameter: 20,
    location: { x: 100, y: 100, z: -40 },
    capacity_ul: 20000,
    working_volume_ul: 15000,
  };
}

function tipRack(): TipRackConfig {
  return {
    type: "tip_rack",
    name: "tips",
    model_name: "demo_tips",
    rows: 2,
    columns: 12,
    calibration: {
      a1: { x: -229, y: -12.4, z: -180 },
      a2: { x: -229, y: -21.4, z: -180 },
    },
    pickup_z: -180,
    drop_z: -175,
    tip_length: 59.3,
    x_offset: 9,
    y_offset: 9,
  };
}

function deckResponse(): DeckResponse {
  return {
    filename: "demo_deck.yaml",
    labware: [
      { key: "plate", config: wellPlate(), wells: null },
      { key: "s1", config: vial(), wells: null },
      { key: "tips", config: tipRack(), wells: null },
    ],
  };
}

function gantryResponse(multi = true): GantryResponse {
  const instruments: GantryResponse["config"]["instruments"] = {
    camera: { type: "camera", vendor: "mount_only", offset_x: 0, offset_y: 0, depth: 0 },
  };
  if (multi) {
    instruments.pipette = {
      type: "pipette",
      vendor: "opentrons",
      offset_x: 10,
      offset_y: 5,
      depth: 20,
    };
  }
  return {
    filename: "gantry.yaml",
    config: {
      serial_port: "/dev/ttyUSB0",
      gantry_type: "cub_xl",
      cnc: { factory_z_travel_mm: 110 },
      working_volume: { x_min: -400, x_max: 0, y_min: -300, y_max: 0, z_min: -110, z_max: 0 },
      instruments,
    },
  };
}

function position(overrides: Partial<GantryPosition> = {}): GantryPosition {
  return {
    x: 0,
    y: 0,
    z: 0,
    work_x: 0,
    work_y: 0,
    work_z: 0,
    status: "Idle",
    connected: true,
    calibration_active: false,
    ...overrides,
  };
}

function stubPositions(positions: Array<{ x: number; y: number; z: number }>) {
  let call = 0;
  const fetchMock = vi.fn(async () => {
    const next = positions[Math.min(call, positions.length - 1)];
    call += 1;
    return new Response(
      JSON.stringify(position({ work_x: next.x, work_y: next.y, work_z: next.z })),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LabwareCalibrationModal", () => {
  it("lists labware types from the deck and filters items by type", async () => {
    const user = userEvent.setup();
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position()}
        onSaveDeck={async () => undefined}
      />,
    );

    const typeSelect = screen.getByLabelText("Labware type");
    expect(typeSelect).toHaveTextContent("Tip rack");
    expect(typeSelect).toHaveTextContent("Well plate");
    expect(typeSelect).toHaveTextContent("Vial");

    await user.selectOptions(typeSelect, "well_plate");
    // The only well plate is auto-selected and its targets are announced.
    expect(screen.getByText("A1, A2")).toBeInTheDocument();
  });

  it("shows the reference instrument picker only for multi-instrument gantries", () => {
    const { rerender } = render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse(true)}
        position={position()}
        onSaveDeck={async () => undefined}
      />,
    );
    expect(screen.getByLabelText("Reference instrument")).toBeInTheDocument();

    rerender(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse(false)}
        position={position()}
        onSaveDeck={async () => undefined}
      />,
    );
    expect(screen.queryByLabelText("Reference instrument")).not.toBeInTheDocument();
  });

  it("offers tip options for pipettes and defaults the tip length from the deck's tip rack", async () => {
    const user = userEvent.setup();
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position()}
        onSaveDeck={async () => undefined}
      />,
    );

    expect(screen.queryByText("Calibrate with a tip attached")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Reference instrument"), "pipette");
    const checkbox = screen.getByRole("checkbox", { name: /tip attached/i });
    await user.click(checkbox);
    expect(screen.getByLabelText("Tip length (mm)")).toHaveValue("59.3");
  });

  it("records positions, applies instrument and tip compensation, and snaps A2 on save", async () => {
    const user = userEvent.setup();
    stubPositions([
      { x: 100, y: 50, z: -30 },
      { x: 109.9, y: 50.2, z: -29 },
    ]);
    const onSaveDeck = vi.fn<(filename: string, config: DeckConfig) => Promise<undefined>>(async () => undefined);
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position()}
        onSaveDeck={onSaveDeck}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Labware type"), "well_plate");
    await user.selectOptions(screen.getByLabelText("Reference instrument"), "pipette");
    await user.click(screen.getByRole("checkbox", { name: /tip attached/i }));
    const tipInput = screen.getByLabelText("Tip length (mm)");
    await user.clear(tipInput);
    await user.type(tipInput, "60");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await user.click(screen.getByRole("button", { name: "Record A1" }));
    // Deck frame inverts InstrumentedGantry.move (gantry = deck − offset,
    // deck z = raw − depth − tip):
    // raw (100, 50, -30) + offsets (10, 5) on XY, − depth 20 − tip 60 on Z
    // = (110, 55, -110)
    await waitFor(() => expect(screen.getByText(/110\.000, 55\.000, -110\.000/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Record A2" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await user.click(screen.getByRole("button", { name: "Save labware calibration" }));
    await waitFor(() => expect(onSaveDeck).toHaveBeenCalledTimes(1));

    const [filename, config] = onSaveDeck.mock.calls[0];
    expect(filename).toBe("demo_deck.yaml");
    const plate = config.labware.plate as WellPlateConfig;
    expect(plate.calibration.a1).toEqual({ x: 110, y: 55, z: -110 });
    // A2's raw delta is dominated by +X, so it snaps to exactly one x_offset
    // pitch from A1 (the loader requires the step to equal the pitch).
    expect(plate.calibration.a2).toEqual({ x: 125, y: 55 });
    // Untouched labware rides along unchanged.
    expect(config.labware.s1).toEqual(vial());
    expect(config.labware.tips).toEqual(tipRack());
  });

  it("updates a vial location and keeps saved values when requested", async () => {
    const user = userEvent.setup();
    stubPositions([{ x: 90, y: 80, z: -35 }]);
    const onSaveDeck = vi.fn<(filename: string, config: DeckConfig) => Promise<undefined>>(async () => undefined);
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position()}
        onSaveDeck={onSaveDeck}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Labware type"), "vial");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Record Vial top center" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Save labware calibration" }));
    await waitFor(() => expect(onSaveDeck).toHaveBeenCalledTimes(1));

    // Default reference is the camera (zero offsets), so raw values persist.
    const saved = onSaveDeck.mock.calls[0][1].labware.s1 as VialConfig;
    expect(saved.location).toEqual({ x: 90, y: 80, z: -35 });
  });

  it("shifts tip rack pickup and drop heights with a re-recorded A1", async () => {
    const user = userEvent.setup();
    stubPositions([{ x: -230, y: -12, z: -178 }]);
    const onSaveDeck = vi.fn<(filename: string, config: DeckConfig) => Promise<undefined>>(async () => undefined);
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position()}
        onSaveDeck={onSaveDeck}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Labware type"), "tip_rack");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Record A1" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Keep saved value" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Keep saved value" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Save labware calibration" }));
    await waitFor(() => expect(onSaveDeck).toHaveBeenCalledTimes(1));

    const saved = onSaveDeck.mock.calls[0][1].labware.tips as LabwareConfig;
    const calibration = (saved as Record<string, unknown>).calibration as Record<string, unknown>;
    expect(calibration.a1).toEqual({ x: -230, y: -12, z: -178 });
    // A2 kept its saved value untouched.
    expect(calibration.a2).toEqual({ x: -229, y: -21.4, z: -180 });
    // pickup_z follows the new A1 Z; drop_z shifts by the same +2mm delta.
    expect((saved as Record<string, unknown>).pickup_z).toBe(-178);
    expect((saved as Record<string, unknown>).drop_z).toBe(-173);
  });

  it("adds new labware from a template under a chosen name", async () => {
    const user = userEvent.setup();
    stubPositions([
      { x: 100, y: 50, z: -30 },
      { x: 109.5, y: 50.1, z: -30 },
    ]);
    const onSaveDeck = vi.fn<(filename: string, config: DeckConfig) => Promise<undefined>>(async () => undefined);
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position()}
        onSaveDeck={onSaveDeck}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Labware type"), "well_plate");
    await user.selectOptions(screen.getByLabelText("Labware"), "new:sbs_96");
    await user.type(screen.getByLabelText("Name"), "Plate 2");
    expect(screen.getByText(/adds this 96-well SBS plate to the deck as "plate_2"/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // A template has no saved positions, so both points must be recorded.
    expect(screen.queryByRole("button", { name: "Keep saved value" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Record A1" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Record A2" })).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Record A2" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Save labware calibration" }));
    await waitFor(() => expect(onSaveDeck).toHaveBeenCalledTimes(1));

    const config = onSaveDeck.mock.calls[0][1];
    const added = config.labware.plate_2 as WellPlateConfig;
    expect(added).toBeDefined();
    expect(added.name).toBe("Plate 2");
    expect(added.rows).toBe(8);
    expect(added.columns).toBe(12);
    expect(added.calibration.a1).toEqual({ x: 100, y: 50, z: -30 });
    // Snapped to one 9 mm SBS pitch along the dominant +X jog.
    expect(added.calibration.a2).toEqual({ x: 109, y: 50 });
    // Existing labware rides along untouched.
    expect(config.labware.plate).toEqual(wellPlate());
  });

  it("rejects a template name that collides with an existing key", async () => {
    const user = userEvent.setup();
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position()}
        onSaveDeck={async () => undefined}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Labware type"), "well_plate");
    await user.selectOptions(screen.getByLabelText("Labware"), "new:sbs_96");
    await user.type(screen.getByLabelText("Name"), "Plate");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getAllByText(/"plate" already exists on this deck/).length).toBeGreaterThan(0);
    // Still on the select step.
    expect(screen.getByLabelText("Labware type")).toBeInTheDocument();
  });

  it("renames existing labware on save", async () => {
    const user = userEvent.setup();
    stubPositions([{ x: 90, y: 80, z: -35 }]);
    const onSaveDeck = vi.fn<(filename: string, config: DeckConfig) => Promise<undefined>>(async () => undefined);
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position()}
        onSaveDeck={onSaveDeck}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Labware type"), "vial");
    const nameInput = screen.getByLabelText("Name");
    expect(nameInput).toHaveValue("s1");
    await user.clear(nameInput);
    await user.type(nameInput, "Sample 1");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Record Vial top center" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Save labware calibration" }));
    await waitFor(() => expect(onSaveDeck).toHaveBeenCalledTimes(1));

    // The key is stable; only the display name changes.
    const saved = onSaveDeck.mock.calls[0][1].labware.s1 as VialConfig;
    expect(saved.name).toBe("Sample 1");
    expect(saved.location).toEqual({ x: 90, y: 80, z: -35 });
  });

  it("blocks recording while disconnected", async () => {
    const user = userEvent.setup();
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse()}
        position={position({ connected: false })}
        onSaveDeck={async () => undefined}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Labware type"), "vial");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByText(/not connected/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record Vial top center" })).toBeDisabled();
  });

  it("keeps a recorded position's own instrument offset after Back changes the reference instrument", async () => {
    // Regression test: step 1's "Back" button returns to step 0 without
    // clearing already-recorded targets. Before this fix, the offset was
    // re-read live at save time, so changing the reference instrument
    // after recording (then continuing without re-recording) silently
    // reinterpreted the already-recorded raw position with the NEW
    // instrument's offset instead of the one active when it was captured.
    const user = userEvent.setup();
    stubPositions([{ x: 90, y: 80, z: -35 }]);
    const onSaveDeck = vi.fn<(filename: string, config: DeckConfig) => Promise<undefined>>(async () => undefined);
    render(
      <LabwareCalibrationModal
        open
        onClose={() => undefined}
        deck={deckResponse()}
        gantry={gantryResponse(true)}
        position={position()}
        onSaveDeck={onSaveDeck}
      />,
    );

    await user.selectOptions(screen.getByLabelText("Labware type"), "vial");
    // Record with the pipette selected (non-zero offset/depth).
    await user.selectOptions(screen.getByLabelText("Reference instrument"), "pipette");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Record Vial top center" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());

    // Go back and switch to the camera (zero offset) WITHOUT re-recording.
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.selectOptions(screen.getByLabelText("Reference instrument"), "camera");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    // Back in step 1 with the position already recorded ("All positions
    // set.") — Continue again to reach the save step.
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Save labware calibration" }));
    await waitFor(() => expect(onSaveDeck).toHaveBeenCalledTimes(1));

    // Must still reflect the pipette's offset (10, 5) and depth (20) that
    // were active when the raw position was actually captured, not the
    // camera's zero offset that's live at save time:
    // raw (90, 80, -35) + XY offsets − depth = (100, 85, -55).
    const saved = onSaveDeck.mock.calls[0][1].labware.s1 as VialConfig;
    expect(saved.location).toEqual({ x: 100, y: 85, z: -55 });
  });
});


describe("custom labware creation", () => {
  function renderNew(onSaveDeck = vi.fn<(filename: string, config: DeckConfig) => Promise<undefined>>(async () => undefined)) {
    const props = { open: true, initialMode: "new" as const, onClose: vi.fn(), deck: deckResponse(), gantry: gantryResponse(false), position: position(), onSaveDeck };
    return { ...render(<LabwareCalibrationModal {...props} />), props };
  }

  async function fillCustom(name = "My 20 well plate", rows = "4", columns = "5", x = "12.5", y = "14") {
    const user = userEvent.setup();
    for (const [label, value] of [["Labware name", name], ["Rows", rows], ["Columns", columns], ["Well spacing X (mm)", x], ["Well spacing Y (mm)", y]]) {
      const field = screen.getByLabelText(label);
      await user.clear(field);
      if (value) await user.type(field, value);
    }
    return user;
  }

  it("saves a calibrated custom 4 by 5 grid and preserves all other deck entries", async () => {
    stubPositions([{ x: 100, y: 50, z: 20 }, { x: 112.4, y: 50.1, z: 20 }]);
    const onSaveDeck = vi.fn<(filename: string, config: DeckConfig) => Promise<undefined>>(async () => undefined);
    renderNew(onSaveDeck);
    const user = await fillCustom();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(onSaveDeck).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Keep saved value" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Record A1" }));
    await user.click(await screen.findByRole("button", { name: "Record A2" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Save labware calibration" }));
    await waitFor(() => expect(onSaveDeck).toHaveBeenCalledTimes(1));
    const [filename, body] = onSaveDeck.mock.calls[0];
    expect(filename).toBe("demo_deck.yaml");
    expect(body.labware.my_20_well_plate).toMatchObject({
      type: "well_plate", name: "My 20 well plate", rows: 4, columns: 5,
      x_offset: 12.5, y_offset: 14,
      calibration: { a1: { x: 100, y: 50, z: 20 }, a2: { x: 112.5, y: 50 } },
    });
    for (const item of deckResponse().labware) expect(body.labware[item.key]).toEqual(item.config);
    for (const field of ["length", "width", "height", "capacity_ul", "working_volume_ul", "a1"])
      expect(body.labware.my_20_well_plate).not.toHaveProperty(field);
  });

  it.each([
    ["", "4", "5", "12.5", "14"],
    ["!!!", "4", "5", "12.5", "14"],
    ["plate", "4", "5", "12.5", "14"],
    ["constructor", "4", "5", "12.5", "14"],
    ["custom", "0", "5", "12.5", "14"],
    ["custom", "1.5", "5", "12.5", "14"],
    ["custom", "4", "-2", "12.5", "14"],
    ["custom", "4", "5", "", "14"],
    ["custom", "4", "5", "0", "14"],
    ["custom", "4", "5", "12.5", "-1"],
  ])("rejects invalid or colliding definition %j %j %j %j %j", async (name, rows, columns, x, y) => {
    const { props } = renderNew();
    const user = await fillCustom(name, rows, columns, x, y);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.queryByRole("button", { name: "Record A1" })).not.toBeInTheDocument();
    expect(props.onSaveDeck).not.toHaveBeenCalled();
  });

  it("uses an orientation reference for a single-column plate and clears captures after geometry edits", async () => {
    stubPositions([{ x: 100, y: 50, z: 20 }]);
    renderNew();
    const user = await fillCustom("column plate", "4", "1");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Record A1" }));
    expect(await screen.findByText(/direction a second column would occupy/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.clear(screen.getByLabelText("Rows"));
    await user.type(screen.getByLabelText("Rows"), "5");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByRole("button", { name: "Record A1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  });

  it("resets a cancelled definition on reopen", async () => {
    const { props, rerender } = renderNew();
    await fillCustom();
    rerender(<LabwareCalibrationModal {...props} open={false} />);
    rerender(<LabwareCalibrationModal {...props} open />);
    expect(screen.getByLabelText("Labware name")).toHaveValue("");
    expect(props.onSaveDeck).not.toHaveBeenCalled();
  });
});
