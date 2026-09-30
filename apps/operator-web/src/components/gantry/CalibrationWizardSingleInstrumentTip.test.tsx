import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import CalibrationWizard from "./CalibrationWizard";
import type { GantryConfig, GantryPosition } from "../../types";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

function singlePipetteConfig(): GantryConfig {
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
    instruments: {
      pipette: { type: "pipette", vendor: "opentrons", offset_x: 0, offset_y: 0, depth: 0 },
    },
  };
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

describe("CalibrationWizard single-instrument tip compensation", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a tip-attached option when the sole instrument is a pipette, and subtracts it from the block touch sent to the backend", async () => {
    const user = userEvent.setup();
    const finalizeCalls: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/position") {
        // The pipette needs a 12mm tip to reach the block; the raw touch
        // (with tip attached) sits 12mm higher (closer to home) than a
        // bare-nozzle touch would.
        return jsonResponse({ ...position(), z: 63, work_z: 63 });
      }
      if (url.pathname === "/api/v1/gantry/work-coordinates" && init?.method === "POST") {
        return jsonResponse({ ...position(), x: 0, y: 0, z: 35, work_x: 0, work_y: 0, work_z: 35 });
      }
      if (url.pathname === "/api/v1/gantry/calibration/restore-soft-limits" && init?.method === "POST") {
        return jsonResponse({ status: "ok" });
      }
      if (url.pathname === "/api/v1/gantry/calibration/finalize-origin" && init?.method === "POST") {
        finalizeCalls.push(JSON.parse(String(init.body)));
        return jsonResponse({
          measured_volume: { x: 400, y: 300, z: 110 },
          max_travel: { x: 400, y: 300, z: 110 },
          z_calibration: { block_height: 35, z_min: 0, z_max: 110 },
          homing_pull_off_mm: 1,
        });
      }
      if (url.pathname === "/api/v1/gantry/soft-limits" && init?.method === "POST") {
        return jsonResponse({ status: "ok" });
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    const onSaveCalibrated = vi.fn<(filename: string, config: GantryConfig) => Promise<void>>(async () => undefined);

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config: singlePipetteConfig() }}
        position={position()}
        onSaveCalibrated={onSaveCalibrated}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Continue" })); // Prepare -> Home
    await user.click(await screen.findByRole("button", { name: "Home gantry" })); // -> Reference height
    await user.click(await screen.findByRole("button", { name: "Continue" })); // -> Set Origin

    expect(await screen.findByText("Set Origin")).toBeInTheDocument();
    const setOriginButton = screen.getByRole("button", { name: "Set origin and continue" });

    const tipCheckbox = screen.getByLabelText("Calibrating with a tip attached");
    await user.click(tipCheckbox);
    expect(setOriginButton).toBeDisabled();

    const tipLength = screen.getByLabelText("Tip length (mm)");
    await user.type(tipLength, "12");
    expect(setOriginButton).toBeEnabled();
    await user.click(setOriginButton);

    expect(await screen.findByRole("heading", { name: "Save" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSaveCalibrated).toHaveBeenCalled());
    expect(finalizeCalls).toHaveLength(1);
    // Raw touch (63) minus the 12mm tip = 51: without the subtraction the
    // backend would calibrate the Z frame as if the bare nozzle reached
    // 12mm further down than it actually can.
    expect(finalizeCalls[0].block_touch_z).toBe(51);
  });
});
