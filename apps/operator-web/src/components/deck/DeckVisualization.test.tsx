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

  it("plots actual Y=145 without shifting calibrated labware when gantry Y changes", () => {
    const instruments = {pipette:{type:"pipette",vendor:"sartorius",offset_x:0,offset_y:0}};
    const pos = (y: number) => ({connected:true,status:"Idle",x:100,y,z:0,work_x:100,work_y:y,work_z:0,calibration_active:false});
    const {container,rerender} = render(<DeckVisualization deck={deck} instruments={instruments}
      gantryPosition={pos(0)} machineXRange={[0,258]} machineYRange={[0,145]} yAxisMotion="bed" />);
    const labwareGroup = screen.getByText("Rack A").closest("svg")!.querySelectorAll(":scope > g")[1];
    const initialLabware = labwareGroup.outerHTML;
    const headY0 = Number(screen.getByText("HEAD").parentElement!.querySelector("circle")!.getAttribute("cy"));
    rerender(<DeckVisualization deck={deck} instruments={instruments}
      gantryPosition={pos(145)} machineXRange={[0,258]} machineYRange={[0,145]} yAxisMotion="bed" />);
    expect(labwareGroup.outerHTML).toBe(initialLabware);
    expect(container.querySelector("g[transform]")).toBeNull();
    expect(screen.queryByText("bed moves Y")).not.toBeInTheDocument();
    const headY145 = Number(screen.getByText("HEAD").parentElement!.querySelector("circle")!.getAttribute("cy"));
    const ticks = Array.from(container.querySelectorAll("text[text-anchor='end']"));
    const first = ticks[0];
    const second = ticks.find(node => Number(node.textContent) !== Number(first.textContent))!;
    const scale = Math.abs((Number(second.getAttribute("y"))-Number(first.getAttribute("y"))) /
      (Number(second.textContent)-Number(first.textContent)));
    expect(headY145-headY0).toBeCloseTo(-145*scale,8);
    const pipette = screen.getByText("pipette").parentElement!.querySelector("rect")!;
    expect(Number(pipette.getAttribute("y"))+7).toBeCloseTo(headY145,8);
    expect(screen.getByText("pipette").parentElement!.querySelector("title")!.textContent).toContain("(100.0, 145.0)");
  });

  it.each([
    ["bed", 0, [0, 258], [0, 145]],
    ["bed", 72.5, [0, 258], [0, 145]],
    ["bed", 145, [0, 258], [0, 145]],
    ["bed", -145, [-258, 0], [-145, 0]],
    ["bed", -72.5, [-258, 0], [-145, 0]],
    ["bed", 0, [-258, 0], [-145, 0]],
    ["head", 145, [0, 258], [0, 145]],
    ["head", -72.5, [-258, 0], [-145, 0]],
  ] as const)("keeps zero-offset pipette on HEAD in %s mode at Y=%s", (mode, y, xRange, yRange) => {
    const x = xRange[0] < 0 ? -100 : 100;
    const { container } = render(<DeckVisualization
      deck={{filename:"empty.yaml",labware:[]}}
      instruments={{
        pipette:{type:"pipette",vendor:"sartorius",offset_x:0,offset_y:0},
        camera:{type:"camera",vendor:"usb",offset_x:2.5,offset_y:-28.75},
      }}
      gantryPosition={{connected:true,status:"Idle",x,y,z:0,work_x:x,work_y:y,work_z:0,calibration_active:false}}
      machineXRange={[...xRange]} machineYRange={[...yRange]} yAxisMotion={mode}
    />);
    const head = screen.getByText("HEAD").parentElement!.querySelector("circle")!;
    const pipetteGroup = screen.getByText("pipette").parentElement!;
    const cameraGroup = screen.getByText("camera").parentElement!;
    const pipette = pipetteGroup.querySelector("rect")!;
    const camera = cameraGroup.querySelector("rect")!;
    const center = (rect: SVGRectElement) => ({x:Number(rect.getAttribute("x"))+7,y:Number(rect.getAttribute("y"))+7});
    const pipetteCenter = center(pipette);
    const cameraCenter = center(camera);
    expect(pipetteCenter.x).toBeCloseTo(Number(head.getAttribute("cx")),8);
    expect(pipetteCenter.y).toBeCloseTo(Number(head.getAttribute("cy")),8);
    expect(container.querySelector("g[transform]")).toBeNull();
    const gridTicks = Array.from(container.querySelectorAll("text[text-anchor='end']"));
    const y0 = gridTicks[0];
    const otherTick = gridTicks.find(node => Number(node.textContent) !== Number(y0.textContent))!;
    const scale = Math.abs((Number(otherTick.getAttribute("y"))-Number(y0.getAttribute("y"))) /
      (Number(otherTick.textContent)-Number(y0.textContent)));
    // Grid labels sit 3 pixels below their coordinate lines.
    const expectedHeadY = Number(y0.getAttribute("y"))-3-(y-Number(y0.textContent))*scale;
    expect(Number(head.getAttribute("cy"))).toBeCloseTo(expectedHeadY,8);
    expect(cameraCenter.x-pipetteCenter.x).toBeCloseTo(2.5*scale,8);
    expect(cameraCenter.y-pipetteCenter.y).toBeCloseTo(28.75*scale,8);
    expect(cameraGroup.querySelector("title")!.textContent).toBe(`camera (camera) at (${(x+2.5).toFixed(1)}, ${(y-28.75).toFixed(1)})`);
    expect(pipetteGroup.querySelector("title")!.textContent).toBe(`pipette (pipette) at (${x.toFixed(1)}, ${y.toFixed(1)})`);
    expect(Number(pipetteGroup.querySelector("text")!.getAttribute("y"))).toBeCloseTo(Math.max(12,pipetteCenter.y-10),8);
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
