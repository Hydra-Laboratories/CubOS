import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import DeckVisualization from "./DeckVisualization";
import type { DeckResponse } from "../../types";

const deck: DeckResponse = {
  filename: "panda_deck.yaml",
  labware: [
    {
      key: "rack_a",
      config: {
        type: "tip_rack",
        name: "Rack A",
        model_name: "panda_2x2_tip_rack",
        rows: 2,
        columns: 2,
        pickup_z: 30,
        drop_z: 24,
        tip_length: 59.3,
        calibration: {
          a1: { x: 10, y: 20 },
          a2: { x: 19, y: 20 },
        },
        x_offset: 9,
        y_offset: 9,
      },
      wells: null,
      location: { x: 10, y: 20, z: 30 },
      geometry: { length: 9, width: 9, height: 6 },
      positions: {
        A1: { x: 10, y: 20, z: 30 },
        A2: { x: 19, y: 20, z: 30 },
        B1: { x: 10, y: 11, z: 30 },
        B2: { x: 19, y: 11, z: 30 },
      },
    },
    {
      key: "well_plate_holder",
      config: {
        type: "well_plate_holder",
        name: "Plate Holder",
        location: { x: 100, y: 120, z: 40 },
        well_plate: {
          name: "Panda Plate",
          model_name: "panda_96_wellplate",
          rows: 2,
          columns: 2,
          calibration: {
            a1: { x: 100, y: 120, z: 45 },
            a2: { x: 109, y: 120, z: 45 },
          },
          x_offset: 9,
          y_offset: 9,
        },
      },
      wells: null,
      location: { x: 100, y: 120, z: 40 },
      geometry: { length: 100, width: 155, height: 14.8 },
      positions: {
        plate: { x: 100, y: 120, z: 45 },
        "plate.A1": { x: 100, y: 120, z: 45 },
        "plate.A2": { x: 109, y: 120, z: 45 },
        "plate.B1": { x: 100, y: 111, z: 45 },
        "plate.B2": { x: 109, y: 111, z: 45 },
      },
    },
    {
      key: "tip_disposal",
      config: {
        type: "tip_disposal",
        name: "Panda Trash",
        model_name: "panda_black_tip_disposal",
        location: { x: 250, y: 118, z: 38 },
        length: 58,
        width: 150,
        height: 38,
      },
      wells: null,
      location: { x: 250, y: 118, z: 38 },
      geometry: { length: 58, width: 150, height: 38 },
      positions: {
        discard: { x: 250, y: 118, z: 38 },
      },
    },
    {
      key: "vial_holder",
      config: {
        type: "vial_holder",
        name: "Panda Vials",
        location: { x: 30, y: 60, z: 8 },
        vials: {
          vial_1: {
            name: "Sample 1",
            model_name: "20ml_vial",
            height: 57,
            diameter: 28,
            location: { x: 30, y: 60 },
            capacity_ul: 20000,
            working_volume_ul: 15000,
          },
        },
      },
      wells: null,
      location: { x: 30, y: 60, z: 8 },
      geometry: { length: 36.2, width: 300.2, height: 35.1 },
      positions: {
        vial_1: { x: 30, y: 60, z: 26 },
      },
    },
  ],
};

describe("DeckVisualization", () => {
  it("renders tip racks, holders, and nested holder labware", () => {
    render(
      <DeckVisualization
        deck={deck}
        instruments={null}
        gantryPosition={null}
        machineXRange={[0, 300]}
        machineYRange={[0, 200]}
      />,
    );

    expect(screen.getByText("Rack A")).toBeInTheDocument();
    expect(screen.getByText("Panda Trash")).toBeInTheDocument();
    expect(screen.getByText("Plate Holder")).toBeInTheDocument();
    expect(screen.getByText("Panda Plate")).toBeInTheDocument();
    expect(screen.getByText("Panda Vials")).toBeInTheDocument();
    expect(screen.getByText("Sample 1")).toBeInTheDocument();
  });

  it("renders an unsaved tip disposal from its config alone", () => {
    render(
      <DeckVisualization
        deck={{
          filename: "unsaved",
          labware: [
            {
              key: "tipdisposal_1",
              config: {
                type: "tip_disposal",
                name: "New Trash",
                model_name: "tip_disposal",
                location: { x: 200, y: 100, z: 38 },
                length: 58,
                width: 150,
                height: 38,
              },
              wells: null,
            },
          ],
        }}
        instruments={null}
        gantryPosition={null}
        machineXRange={[0, 300]}
        machineYRange={[0, 200]}
      />,
    );

    expect(screen.getByText("New Trash")).toBeInTheDocument();
    expect(screen.getByTestId("deck-visualization").outerHTML).not.toContain("NaN");
  });

  it("letterboxes unequal machine ranges and scales well radii from mm", () => {
    const scaledDeck: DeckResponse = {
      filename: "scaled.yaml",
      labware: [
        {
          key: "single_plate",
          config: {
            type: "well_plate",
            name: "Scaled Plate",
            model_name: "single",
            rows: 1,
            columns: 1,
            length: 10,
            width: 10,
            height: 5,
            calibration: {
              a1: { x: 0, y: 100, z: 10 },
              a2: { x: 9, y: 100, z: 10 },
            },
            x_offset: 9,
            y_offset: 9,
            capacity_ul: 200,
            working_volume_ul: 100,
          },
          wells: {
            A1: { x: 0, y: 100, z: 10 },
          },
        },
      ],
    };

    const { container } = render(
      <DeckVisualization
        deck={scaledDeck}
        instruments={null}
        gantryPosition={null}
        machineXRange={[0, 100]}
        machineYRange={[0, 200]}
      />,
    );

    const well = container.querySelector("circle");
    expect(well).not.toBeNull();
    expect(Number(well?.getAttribute("cx"))).toBeCloseTo(222.27, 2);
    expect(Number(well?.getAttribute("cy"))).toBeCloseTo(210, 2);
    expect(Number(well?.getAttribute("r"))).toBeCloseTo(5.18, 2);
  });

  it("keeps the coordinate scale fixed as the gantry moves", () => {
    render(
      <DeckVisualization
        deck={{ filename: "empty.yaml", labware: [] }}
        instruments={{ pipette: { type: "mock_pipette", vendor: "mock", offset_x: 40, offset_y: 0 } }}
        gantryPosition={{
          connected: true,
          status: "Idle",
          x: 490,
          y: 20,
          z: 0,
          work_x: 490,
          work_y: 20,
          work_z: 0,
          calibration_active: false,
        }}
        machineXRange={[0, 300]}
        machineYRange={[0, 400]}
      />,
    );

    expect(screen.getAllByText("300").length).toBeGreaterThan(0);
    expect(screen.getAllByText("350").length).toBeGreaterThan(0);
    expect(screen.queryByText("500")).not.toBeInTheDocument();
  });

  it("expands the visible range for labware wider than the working volume", () => {
    const wideDeck: DeckResponse = {
      filename: "wide_deck.yaml",
      labware: [
        {
          key: "wide_plate",
          config: {
            type: "well_plate",
            name: "Wide Plate",
            model_name: "wide_96_wellplate",
            rows: 1,
            columns: 2,
            length: 100,
            width: 50,
            height: 10,
            calibration: {
              a1: { x: 250, y: 50, z: 10 },
              a2: { x: 330, y: 50, z: 10 },
            },
            x_offset: 80,
            y_offset: 9,
            capacity_ul: 200,
            working_volume_ul: 100,
          },
          wells: {
            A1: { x: 250, y: 50, z: 10 },
            A2: { x: 330, y: 50, z: 10 },
          },
        },
      ],
    };

    render(
      <DeckVisualization
        deck={wideDeck}
        instruments={null}
        gantryPosition={null}
        machineXRange={[0, 306]}
        machineYRange={[0, 300]}
      />,
    );

    expect(screen.getByText("Wide Plate")).toBeInTheDocument();
    expect(screen.getByText("350")).toBeInTheDocument();
  });

  // Regression: bed mode used to translate the deck by -gantryY, moving it
  // up-screen. The head marker is pinned at deck-frame Y=0 (bottom of the
  // frame), so when WPos.y=50 the deck must shift down-screen (+sy) to put
  // deck point y=50 under the fixed marker — the sign was inverted.
  it("shifts the deck down-screen as gantry Y increases in bed mode", () => {
    const { container } = render(
      <DeckVisualization
        deck={{ filename: "empty.yaml", labware: [] }}
        instruments={null}
        gantryPosition={{
          connected: true,
          status: "Idle",
          x: 0,
          y: 50,
          z: 0,
          work_x: 0,
          work_y: 50,
          work_z: 0,
          calibration_active: false,
        }}
        machineXRange={[0, 300]}
        machineYRange={[0, 200]}
        yAxisMotion="bed"
      />,
    );

    expect(screen.getByText("bed moves Y")).toBeInTheDocument();
    const deckGroup = container.querySelector("g[transform]");
    expect(deckGroup).not.toBeNull();
    const match = /translate\(0,\s*(-?[\d.]+)\)/.exec(deckGroup!.getAttribute("transform")!);
    expect(match).not.toBeNull();
    const translateY = Number(match![1]);
    // Visual bounds pad [0,300]x[0,200] to [-10,310]x[-10,210]; the 420px-high
    // SVG letterboxes to scale = (420 - 40) / 220 px/mm.
    const expectedScale = (420 - 2 * 20) / 220;
    expect(translateY).toBeCloseTo(50 * expectedScale, 3);
  });

  it("renders holder labware with current CubOS dimension keys without NaN attributes", () => {
    const currentDeck: DeckResponse = {
      filename: "legacy_holder.yaml",
      labware: [
        {
          key: "plate_holder",
          config: {
            type: "well_plate_holder",
            name: "Legacy Plate Holder",
            location: { x: 100, y: 100, z: 10 },
            well_plate: {
              name: "Legacy Plate",
              model_name: "legacy_plate",
              rows: 2,
              columns: 2,
              calibration: {
                a1: { x: 100, y: 100 },
                a2: { x: 109, y: 100 },
              },
              length: 127.76,
              width: 85.47,
              height: 14.22,
              x_offset: 9,
              y_offset: 9,
            },
          },
          wells: null,
          location: { x: 100, y: 100, z: 10 },
          geometry: { length: 100, width: 155, height: 14.8 },
          positions: {
            "plate.A1": { x: 100, y: 100, z: 15 },
            "plate.A2": { x: 109, y: 100, z: 15 },
            "plate.B1": { x: 100, y: 91, z: 15 },
            "plate.B2": { x: 109, y: 91, z: 15 },
          },
        },
        {
          key: "vial_holder",
          config: {
            type: "vial_holder",
            name: "Legacy Vials",
            location: { x: 30, y: 60, z: 8 },
            vials: {
              vial_1: {
                name: "Legacy Vial",
                model_name: "20ml_vial",
                height: 57,
                diameter: 28,
                location: { x: 30, y: 60 },
                capacity_ul: 20000,
                working_volume_ul: 15000,
              },
            },
          },
          wells: null,
          location: { x: 30, y: 60, z: 8 },
          geometry: { length: 36.2, width: 300.2, height: 35.1 },
          positions: {
            vial_1: { x: 30, y: 60, z: 26 },
          },
        },
      ],
    };

    render(
      <DeckVisualization
        deck={currentDeck}
        instruments={null}
        gantryPosition={null}
        machineXRange={[0, 300]}
        machineYRange={[0, 200]}
      />,
    );

    expect(screen.getByText("Legacy Plate")).toBeInTheDocument();
    expect(screen.getByText("Legacy Vial")).toBeInTheDocument();
    expect(screen.getByTestId("deck-visualization").outerHTML).not.toContain("NaN");
  });
});
