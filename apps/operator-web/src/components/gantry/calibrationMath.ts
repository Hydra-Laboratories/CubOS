import type { FinalizeOriginResponse, GantryConfig, WorkingVolume } from "../../types";

type CapturedPosition = {
  x: number;
  y: number;
  z: number;
};

export type ZCalibrationResult = {
  blockHeight: number;
  factoryZTravel: number;
  homeZ: number;
  blockTouchZ: number;
  homeToBlockTravel: number;
  remainingBelowBlock: number;
  canReachDeckBottom: boolean;
  zMin: number;
  zMax: number;
  maxTravelZ: number;
};

export function getFactoryZTravel(config: GantryConfig): number {
  const value = Number(config.cnc.factory_z_travel_mm ?? config.cnc.total_z_range ?? config.cnc.total_z_height);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error("Gantry config must seed cnc.factory_z_travel_mm before calibration.");
  }
  return roundMm(value);
}

export function getCalibrationBlockHeight(config: GantryConfig): number {
  const value = Number(config.cnc.calibration_block_height_mm);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error("Gantry config must define cnc.calibration_block_height_mm before block calibration.");
  }
  return roundMm(value);
}

export function getCalculatedZRange(config: GantryConfig): number {
  return getFactoryZTravel(config);
}

// GRBL's standard $27 homing pull-off default (mm). Used when the seed
// YAML doesn't specify one so calibration isn't blocked — the calibrated
// config we write back includes this value, so it self-heals on save.
export const DEFAULT_HOMING_PULL_OFF_MM = 1;

export function getConfiguredHomingPullOff(config: GantryConfig): number {
  const raw = config.grbl_settings?.homing_pull_off;
  if (raw == null) {
    return DEFAULT_HOMING_PULL_OFF_MM;
  }
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0) {
    // Present but garbage (negative/NaN) is a real config error worth
    // surfacing rather than silently overriding.
    throw new Error(
      `grbl_settings.homing_pull_off must be a non-negative finite number (got ${raw}). Fix the gantry YAML and save.`,
    );
  }
  return roundMm(value);
}

export function calculateSingleInstrumentZCalibration({
  homeZ,
  blockTouchZ,
  blockHeight,
  factoryZTravel,
  homedZ,
}: {
  homeZ: number;
  blockTouchZ: number;
  blockHeight: number;
  factoryZTravel: number;
  homedZ?: number;
}): ZCalibrationResult {
  for (const [label, value] of [
    ["home Z", homeZ],
    ["block touch Z", blockTouchZ],
    ["block height", blockHeight],
    ["factory Z travel", factoryZTravel],
  ] as const) {
    if (!Number.isFinite(value)) {
      throw new Error(`${label} must be a finite number.`);
    }
  }
  if (blockHeight <= 0) {
    throw new Error("Calibration reference height must be positive.");
  }
  if (factoryZTravel <= 0) {
    throw new Error("Factory Z travel must be positive.");
  }
  const travelFromHomeToBlock = roundMm(homeZ - blockTouchZ);
  if (travelFromHomeToBlock <= 0) {
    throw new Error("Block touch Z must be below the homed Z position.");
  }
  if (travelFromHomeToBlock > roundMm(factoryZTravel + 0.001)) {
    throw new Error(`Home-to-block travel exceeds the configured factory Z travel (${travelFromHomeToBlock} mm measured; ${roundMm(factoryZTravel)} mm configured). Verify the mechanical travel and that both positions use the same coordinate frame.`);
  }
  const remainingBelowBlock = roundMm(factoryZTravel - travelFromHomeToBlock);
  const canReachDeckBottom = remainingBelowBlock + 0.001 >= blockHeight;
  const zMin = canReachDeckBottom ? 0 : roundMm(blockHeight - remainingBelowBlock);
  const zMax = roundMm(homedZ ?? travelFromHomeToBlock + blockHeight);
  const maxTravelZ = roundMm(zMax - zMin);
  if (maxTravelZ <= 0) {
    throw new Error("Calibrated Z travel span must be positive.");
  }
  return {
    blockHeight: roundMm(blockHeight),
    factoryZTravel: roundMm(factoryZTravel),
    homeZ: roundMm(homeZ),
    blockTouchZ: roundMm(blockTouchZ),
    homeToBlockTravel: travelFromHomeToBlock,
    remainingBelowBlock,
    canReachDeckBottom,
    zMin,
    zMax,
    maxTravelZ,
  };
}

export function calculateSingleInstrumentZRange({
  homeZ,
  blockTouchZ,
  blockHeight,
}: {
  homeZ: number;
  blockTouchZ: number;
  blockHeight: number;
}): number {
  return roundMm(homeZ - blockTouchZ + blockHeight);
}

export function buildCalibratedConfig({
  config,
  measuredVolume,
  zMin,
  zMax,
  maxTravel,
  isMulti,
  instruments,
  instrumentPositions,
  referenceInstrument,
  lowestInstrument,
  cameraBlockDistances,
  tipLengths,
}: {
  config: GantryConfig;
  measuredVolume: CapturedPosition;
  zMin: number;
  zMax: number;
  maxTravel: CapturedPosition;
  isMulti: boolean;
  instruments: string[];
  instrumentPositions: Record<string, CapturedPosition>;
  referenceInstrument: string;
  lowestInstrument: string;
  cameraBlockDistances?: Record<string, number>;
  // Pipettes that can't reach the calibration block bare-nozzle are touched
  // with a tip attached instead; this is the tip length (mm) to subtract so
  // the stored `depth` still reflects the bare-nozzle TCP, matching
  // effective_depth = depth + attached_tip_extension at protocol runtime.
  tipLengths?: Record<string, number>;
}): GantryConfig {
  const next = structuredClone(config);
  const originPolicy = config.origin_policy ?? "deck_origin";
  next.origin_policy = originPolicy;

  if (originPolicy === "home_origin") {
    // home_origin: WPos zero is the homed back-right-top corner, so the
    // whole reachable volume is non-positive. The physical span per axis
    // is unchanged from deck_origin — X: measuredVolume.x, Y: measuredVolume.y,
    // Z: (zMax - zMin) — only the reference corner flips, remapping
    // [0, span] -> [-span, 0] instead of scaling or re-measuring anything.
    next.working_volume = {
      x_min: -roundMm(measuredVolume.x),
      x_max: 0,
      y_min: -roundMm(measuredVolume.y),
      y_max: 0,
      z_min: -roundMm(zMax - zMin),
      z_max: 0,
    };
  } else {
    next.working_volume = {
      x_min: 0,
      x_max: roundMm(measuredVolume.x),
      y_min: 0,
      y_max: roundMm(measuredVolume.y),
      z_min: roundMm(zMin),
      z_max: roundMm(zMax),
    };
  }
  if (next.cnc.safe_z != null) {
    next.cnc.safe_z = Math.min(
      Math.max(roundMm(next.cnc.safe_z), next.working_volume.z_min),
      next.working_volume.z_max,
    );
  }
  next.grbl_settings = {
    ...(next.grbl_settings ?? {}),
    status_report: 0,
    soft_limits: true,
    homing_enable: true,
    max_travel_x: maxTravel.x,
    max_travel_y: maxTravel.y,
    max_travel_z: maxTravel.z,
  };

  if (!isMulti && instruments.length === 1) {
    const name = instruments[0];
    if (next.instruments[name]?.type === "pipette") {
      next.instruments[name].depth = -requireFinite(tipLengths?.[name] ?? 0, `${name} tip length`);
    }
  }

  if (isMulti) {
    const reference = instrumentPositions[referenceInstrument];
    const lowest = instrumentPositions[lowestInstrument];
    if (!reference || !lowest) {
      throw new Error("Reference and lowest instrument positions are required.");
    }
    for (const name of instruments) {
      const coords = instrumentPositions[name];
      if (!coords || !next.instruments[name]) continue;
      if (next.instruments[name].type === "lighting") continue;
      if (next.instruments[name].type === "camera") {
        const distance = cameraBlockDistances?.[name];
        if (!Number.isFinite(distance) || distance == null || distance < 0) {
          throw new Error(`Distance from calibration block is required for ${name}.`);
        }
        next.instruments[name] = {
          ...next.instruments[name],
          offset_x: roundMm(requireFinite(reference.x - coords.x, `${name} offset_x`)),
          offset_y: roundMm(requireFinite(reference.y - coords.y, `${name} offset_y`)),
          depth: roundMm(requireFinite(coords.z - (lowest.z + distance), `${name} depth`)),
        };
        continue;
      }
      const tipLength = tipLengths?.[name];
      const bareNozzleZ = tipLength != null
        ? requireFinite(coords.z - tipLength, `${name} tip-adjusted touch Z`)
        : coords.z;
      next.instruments[name] = {
        ...next.instruments[name],
        offset_x: roundMm(requireFinite(reference.x - coords.x, `${name} offset_x`)),
        offset_y: roundMm(requireFinite(reference.y - coords.y, `${name} offset_y`)),
        depth: roundMm(requireFinite(bareNozzleZ - lowest.z, `${name} depth`)),
      };
    }
    // Lighting is co-mounted with the camera, so it inherits the camera's
    // calibration instead of getting its own step.
    const cameraSource = instruments.find(
      (name) => next.instruments[name]?.type === "camera" && instrumentPositions[name],
    );
    if (cameraSource) {
      const camera = next.instruments[cameraSource];
      for (const name of instruments) {
        const entry = next.instruments[name];
        if (!entry || entry.type !== "lighting") continue;
        next.instruments[name] = {
          ...entry,
          offset_x: camera.offset_x,
          offset_y: camera.offset_y,
          depth: camera.depth,
        };
      }
    }
  }

  return next;
}

function roundMm(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function requireFinite(value: number, label: string): number {
  if (!Number.isFinite(value)) {
    throw new Error(`${label} is not a valid number (${value}); captured position data may be incomplete.`);
  }
  return value;
}


export function validateFinalizedOrigin(config: GantryConfig, result: FinalizeOriginResponse): WorkingVolume {
  const policy = config.origin_policy ?? "deck_origin";
  if (result.origin_policy !== policy) {
    throw new Error("The controller did not verify the selected coordinate origin. Update CubOS before saving.");
  }
  const measured = result.measured_volume;
  const calibration = result.z_calibration;
  const limits = result.max_travel;
  const volume = result.working_volume;
  const position = result.position;
  if (!measured || !calibration || !limits || !volume || !position) {
    throw new Error("Verified calibration bounds and controller position are required before saving.");
  }
  const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
  if (![measured.x, measured.y, measured.z, calibration.z_min, calibration.z_max,
    calibration.block_height, limits.x, limits.y, limits.z, position.x, position.y, position.z,
    ...Object.values(volume)].every(finite) || measured.x <= 0 || measured.y <= 0 ||
    calibration.z_min < 0 || calibration.z_max <= calibration.z_min ||
    calibration.block_height <= 0 || limits.x <= 0 || limits.y <= 0 || limits.z <= 0 ||
    Math.abs(measured.z - calibration.z_max) > 0.25) {
    throw new Error("The controller returned invalid calibration measurements.");
  }
  const expected: WorkingVolume = policy === "home_origin"
    ? { x_min: -measured.x, x_max: 0, y_min: -measured.y, y_max: 0,
      z_min: calibration.z_min - calibration.z_max, z_max: 0 }
    : { x_min: 0, x_max: measured.x, y_min: 0, y_max: measured.y,
      z_min: calibration.z_min, z_max: calibration.z_max };
  for (const key of Object.keys(expected) as (keyof WorkingVolume)[]) {
    if (!finite(volume[key]) || Math.abs(volume[key] - expected[key]) > 0.25) {
      throw new Error("Verified controller bounds do not match the selected coordinate origin.");
    }
  }
  for (const axis of ["x", "y", "z"] as const) {
    const maximum = volume[`${axis}_max`];
    const minimum = volume[`${axis}_min`];
    if (maximum <= minimum || position[axis] < minimum || position[axis] > maximum ||
      Math.abs(position[axis] - maximum) > 0.25) {
      throw new Error("The controller position does not verify the calibrated home corner.");
    }
  }
  if (!finite(result.safe_z) || result.safe_z < volume.z_min || result.safe_z > volume.z_max) {
    throw new Error("Verified safe travel height must be within the calibrated working volume.");
  }
  return structuredClone(volume);
}
