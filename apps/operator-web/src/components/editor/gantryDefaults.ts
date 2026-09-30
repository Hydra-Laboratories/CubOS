import type { GantryConfig } from "../../types";

export const EMPTY_GANTRY: GantryConfig = {
  serial_port: "",
  gantry_type: "cub_xl",
  cnc: {
    factory_z_travel_mm: 80,
    calibration_block_height_mm: 35,
    y_axis_motion: "head",
    safe_z: 80,
  },
  working_volume: { x_min: 0, x_max: 300, y_min: 0, y_max: 200, z_min: 0, z_max: 80 },
  grbl_settings: {},
  instruments: {},
};
