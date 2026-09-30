import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import CalibrationWizard from "./CalibrationWizard";
import type { GantryConfig, GantryPosition } from "../../types";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function noInstrumentsConfig(): GantryConfig {
  return {
    serial_port: "/dev/ttyUSB0",
    gantry_type: "cub_xl",
    cnc: {
      factory_z_travel_mm: 110,
      calibration_block_height_mm: 35,
      y_axis_motion: "head",
      safe_z: 110,
    },
    working_volume: { x_min: 0, x_max: 400, y_min: 0, y_max: 300, z_min: 0, z_max: 110 },
    grbl_settings: {},
    instruments: {},
  };
}

function singleInstrumentConfig(): GantryConfig {
  return { ...noInstrumentsConfig(), instruments: { asmi: { type: "asmi", vendor: "vernier", offset_x: 0, offset_y: 0, depth: 0 } } };
}

function position(): GantryPosition {
  return {
    x: 0,
    y: 0,
    z: 20,
    work_x: 0,
    work_y: 0,
    work_z: 20,
    status: "Idle",
    connected: true,
    calibration_active: false,
  };
}

function installFetch() {
  const fetchMock = vi.fn(async () => jsonResponse(position()));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("CalibrationWizard zero-instrument guidance", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("explains why Continue is disabled", async () => {
    installFetch();
    const config: GantryConfig = noInstrumentsConfig();

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(
      screen.getByText(/Add and save at least one mounted instrument in the Gantry configuration before calibrating\./),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  });

  it("does not show the guidance message once an instrument is configured", async () => {
    installFetch();
    const config: GantryConfig = singleInstrumentConfig();

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(
      screen.queryByText(/Add and save at least one mounted instrument in the Gantry configuration before calibrating\./),
    ).not.toBeInTheDocument();
  });
});
