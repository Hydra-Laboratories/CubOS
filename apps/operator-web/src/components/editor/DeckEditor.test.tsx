import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import DeckEditor from "./DeckEditor";
import type { DeckResponse } from "../../types";

function deckFixture(): DeckResponse {
  return {
    filename: "deck.yaml",
    labware: [
      {
        key: "plate_1",
        config: {
          type: "well_plate",
          name: "Plate A",
          model_name: "model-a",
          rows: 8,
          columns: 12,
          length: 127.76,
          width: 85.47,
          height: 14.22,
          calibration: { a1: { x: 10, y: 20, z: 30 }, a2: { x: 20, y: 20, z: 30 } },
          x_offset: 9,
          y_offset: 9,
          capacity_ul: 200,
          working_volume_ul: 150,
        },
        wells: null,
      },
      {
        key: "vial_1",
        config: {
          type: "vial",
          name: "Vial A",
          model_name: "vial-model",
          height: 66.75,
          diameter: 28,
          location: { x: 30, y: 40, z: 20 },
          capacity_ul: 1500,
          working_volume_ul: 1200,
        },
        wells: null,
      },
    ],
  };
}

function renderDeck(overrides: Partial<React.ComponentProps<typeof DeckEditor>> = {}) {
  const props = {
    configs: ["deck.yaml"],
    selectedFile: "deck.yaml" as string | null,
    onSelectFile: vi.fn(),
    onImportFile: vi.fn(),
    deck: deckFixture(),
    baseline: deckFixture(),
    onSave: vi.fn(),
    onLocalChange: vi.fn(),
    onRefresh: vi.fn(),
    ...overrides,
  };
  render(<DeckEditor {...props} />);
  return props;
}

describe("DeckEditor", () => {
  it("renders well plate and vial fields and reports edits", async () => {
    const user = userEvent.setup();
    const props = renderDeck();

    expect(screen.getByDisplayValue("Plate A")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Vial A")).toBeInTheDocument();
    // well-plate-only field (required label renders "Rows *")
    expect(screen.getByLabelText(/^Rows/)).toHaveValue("8");
    // vial-only field
    expect(screen.getByLabelText("Diameter (mm)")).toHaveValue("28");

    await user.type(screen.getByDisplayValue("Plate A"), "!");
    expect(props.onLocalChange).toHaveBeenCalled();
  });

  it("shows the unsaved banner only when dirty", () => {
    const { unmount } = render(
      <DeckEditor {...baseProps()} dirty={false} />,
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    unmount();

    render(<DeckEditor {...baseProps()} dirty />);
    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent(/Unsaved changes/i);
    expect(banner).toHaveTextContent(/save this deck/i);
  });

  it("replaces legacy shortcuts and removes existing labware", async () => {
    const user = userEvent.setup();
    const props = renderDeck();
    expect(screen.queryByRole("button", { name: "+ Well Plate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Vial" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New Labware" })).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    expect(screen.queryByDisplayValue("Plate A")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Vial A")).toBeInTheDocument();
    expect(props.onLocalChange).toHaveBeenCalled();
  });

  it("saves the deck and disables Save when a required name is blank", async () => {
    const user = userEvent.setup();
    const props = renderDeck();

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(props.onSave).toHaveBeenCalledWith(
      "deck.yaml",
      expect.objectContaining({ labware: expect.objectContaining({ plate_1: expect.anything() }) }),
    );

    await user.clear(screen.getByDisplayValue("Plate A"));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("edits well-plate and vial detail fields", async () => {
    const user = userEvent.setup();
    const props = renderDeck();

    // well-plate-only numeric fields + calibration coordinate
    await user.clear(screen.getByLabelText(/^Columns/));
    await user.type(screen.getByLabelText(/^Columns/), "6");
    await user.clear(screen.getByLabelText("Length (mm)"));
    await user.type(screen.getByLabelText("Length (mm)"), "120");
    await user.clear(screen.getByLabelText(/^Well pitch X/));
    await user.type(screen.getByLabelText(/^Well pitch X/), "10");
    await user.clear(screen.getByLabelText("Calibration A1 X"));
    await user.type(screen.getByLabelText("Calibration A1 X"), "5");

    // vial-only fields
    await user.clear(screen.getByLabelText("Diameter (mm)"));
    await user.type(screen.getByLabelText("Diameter (mm)"), "30");
    await user.clear(screen.getByLabelText("Location Z"));
    await user.type(screen.getByLabelText("Location Z"), "25");

    // shared label (well plate + vial both have "Capacity (uL)")
    await user.clear(screen.getAllByLabelText("Capacity (uL)")[1]);
    await user.type(screen.getAllByLabelText("Capacity (uL)")[1], "1600");

    expect(props.onLocalChange).toHaveBeenCalled();
  });

  it("passes unsupported labware through with an explanatory note", () => {
    render(
      <DeckEditor
        {...baseProps()}
        deck={{
          filename: "deck.yaml",
          labware: [
            { key: "holder_1", config: { type: "vial_holder", name: "Holder" }, wells: null },
          ],
        }}
      />,
    );
    expect(screen.getByText(/editing not supported/i)).toBeInTheDocument();
    expect(screen.getByText(/visualization updates after saving/i)).toBeInTheDocument();
    expect(screen.getByText("vial_holder")).toBeInTheDocument();
  });

  it("opens custom creation without adding a placeholder and cancels cleanly", async () => {
    const user = userEvent.setup();
    const props = renderDeck({ gantry: gantryFixture() });
    await user.click(screen.getByRole("button", { name: "New Labware" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("Labware name")).toBeInTheDocument();
    expect(props.onSave).not.toHaveBeenCalled();
    expect(props.onLocalChange).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(props.onSave).not.toHaveBeenCalled();
    expect(props.onLocalChange).not.toHaveBeenCalled();
  });

  it.each([
    { gantry: null },
    { gantry: gantryFixture(), deck: null },
    { gantry: gantryFixture(), isRunning: true },
  ])("disables custom creation without prerequisites or during a run: %j", (overrides) => {
    renderDeck(overrides);
    expect(screen.getByRole("button", { name: "New Labware" })).toBeDisabled();
  });

  it("always renders the action bar and disables Save with a hint when there are no items", () => {
    renderDeck({ deck: { filename: "deck.yaml", labware: [] } });

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByText(/Add.*labware|New Labware/i)).toBeInTheDocument();
  });

  it("shows a save-failed banner and clears it on the next edit or successful save", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn()
      .mockRejectedValueOnce(new Error("400: duplicate name"))
      .mockResolvedValueOnce(undefined);
    renderDeck({ onSave });

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Save failed/i)).toHaveTextContent("400: duplicate name");

    // Any further edit clears the stale error, even before a retry.
    await user.type(screen.getByDisplayValue("Plate A"), "!");
    expect(screen.queryByText(/Save failed/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/Save failed/i)).not.toBeInTheDocument();
  });

  it("discards local edits back to the last-saved baseline and notifies the parent", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    renderDeck({ dirty: true, onRefresh });

    const nameField = screen.getByDisplayValue("Plate A");
    await user.clear(nameField);
    await user.type(nameField, "Edited Plate");
    expect(screen.getByDisplayValue("Edited Plate")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Discard unsaved deck changes?");
    await user.click(screen.getByRole("button", { name: "Discard" }));

    expect(onRefresh).toHaveBeenCalled();
    expect(screen.getByDisplayValue("Plate A")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Edited Plate")).not.toBeInTheDocument();
  });

  it("keeps edits when the user cancels the discard confirm", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    renderDeck({ dirty: true, onRefresh });

    const nameField = screen.getByDisplayValue("Plate A");
    await user.clear(nameField);
    await user.type(nameField, "Edited Plate");

    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Edited Plate")).toBeInTheDocument();
  });

  it("disables labware calibration until a gantry is loaded", () => {
    renderDeck();
    expect(screen.getByRole("button", { name: "Calibrate labware" })).toBeDisabled();
  });

  it("opens the labware calibration modal from the deck tab", async () => {
    const user = userEvent.setup();
    renderDeck({ gantry: gantryFixture() });

    const button = screen.getByRole("button", { name: "Calibrate labware" });
    expect(button).toBeEnabled();
    await user.click(button);

    const dialog = screen.getByRole("dialog", { name: "Labware calibration" });
    expect(dialog).toBeInTheDocument();
    // The type dropdown is built from the deck's labware types.
    expect(screen.getByLabelText("Labware type")).toHaveTextContent("Well plate");
    expect(screen.getByLabelText("Labware type")).toHaveTextContent("Vial");
  });

  it("calibrates the editor's unsaved labware edits, not the last-saved deck", async () => {
    const user = userEvent.setup();
    renderDeck({ gantry: gantryFixture() });

    await user.type(screen.getByDisplayValue("Plate A"), "!");
    await user.click(screen.getByRole("button", { name: "Calibrate labware" }));
    await user.selectOptions(screen.getByLabelText("Labware type"), "well_plate");

    // The modal announces the two-point targets for the locally edited plate.
    expect(screen.getByText("A1, A2")).toBeInTheDocument();
  });
});

function gantryFixture() {
  return {
    filename: "gantry.yaml",
    config: {
      serial_port: "/dev/ttyUSB0",
      gantry_type: "cub" as const,
      cnc: { factory_z_travel_mm: 110 },
      working_volume: { x_min: 0, x_max: 400, y_min: 0, y_max: 300, z_min: 0, z_max: 110 },
      instruments: {
        pipette: { type: "pipette", vendor: "opentrons", offset_x: 0, offset_y: 0, depth: 0 },
      },
    },
  };
}

function baseProps() {
  return {
    configs: ["deck.yaml"],
    selectedFile: "deck.yaml" as string | null,
    onSelectFile: vi.fn(),
    onImportFile: vi.fn(),
    deck: deckFixture(),
    baseline: deckFixture(),
    onSave: vi.fn(),
    onLocalChange: vi.fn(),
    onRefresh: vi.fn(),
  };
}
