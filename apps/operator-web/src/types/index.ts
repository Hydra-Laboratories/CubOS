// Mirrors backend Pydantic models

export interface Coordinate3D {
  x: number;
  y: number;
  z: number;
}

export interface Coordinate2D {
  x: number;
  y: number;
}

export interface CalibrationPoints {
  a1: Coordinate3D | null;
  a2: Coordinate3D;
}

export interface WellPlateConfig {
  type: "well_plate";
  name: string;
  model_name: string;
  rows: number;
  columns: number;
  length?: number | null;
  width?: number | null;
  height?: number | null;
  well_depth?: number | null;
  calibration: CalibrationPoints;
  x_offset: number;
  y_offset: number;
  capacity_ul?: number | null;
  working_volume_ul?: number | null;
}

export interface VialConfig {
  type: "vial";
  name: string;
  model_name: string;
  height: number;
  diameter: number;
  location: Coordinate3D;
  capacity_ul: number;
  working_volume_ul: number;
}

export interface VialGridConfig {
  type: "vial_grid";
  name: string;
  model_name: string;
  rows: number;
  columns: number;
  calibration: CalibrationPoints;
  x_offset: number;
  y_offset: number;
  row_direction?: "positive" | "negative" | null;
  vial_model_name?: string;
  vial_height?: number | null;
  vial_diameter?: number | null;
  capacity_ul: number;
  working_volume_ul: number;
  [key: string]: unknown;
}

export interface TipRackConfig {
  type: "tip_rack";
  name: string;
  model_name: string;
  load_name?: string;
  rows?: number;
  columns?: number;
  calibration?: CalibrationPoints;
  pickup_z?: number;
  drop_z?: number | null;
  tip_length?: number;
  x_offset?: number;
  y_offset?: number;
  z_pickup?: number;
  z_drop?: number;
  tips?: Record<string, WellPosition>;
  tip_present?: Record<string, boolean>;
  [key: string]: unknown;
}

export interface NestedWellPlateConfig {
  name?: string;
  model_name: string;
  rows: number;
  columns: number;
  calibration: {
    a1: Coordinate2D | Coordinate3D | null;
    a2: Coordinate2D | Coordinate3D;
  };
  x_offset: number;
  y_offset: number;
  length?: number | null;
  width?: number | null;
  height?: number | null;
  well_depth?: number | null;
  capacity_ul?: number;
  working_volume_ul?: number;
  [key: string]: unknown;
}

export interface NestedVialConfig {
  name?: string;
  model_name: string;
  height: number;
  diameter: number;
  location: Coordinate2D | Coordinate3D;
  capacity_ul: number;
  working_volume_ul: number;
  [key: string]: unknown;
}

export interface WellPlateHolderConfig {
  type: "well_plate_holder";
  name: string;
  model_name?: string;
  location?: Coordinate3D;
  well_plate?: NestedWellPlateConfig | null;
  [key: string]: unknown;
}

export interface VialHolderConfig {
  type: "vial_holder";
  name: string;
  model_name?: string;
  location?: Coordinate3D;
  vials?: Record<string, NestedVialConfig>;
  [key: string]: unknown;
}

export interface TipDisposalConfig {
  type: "tip_disposal";
  name: string;
  model_name?: string;
  location?: Coordinate3D;
  [key: string]: unknown;
}

export type UnsupportedDeckConfig =
  | TipRackConfig
  | WellPlateHolderConfig
  | VialHolderConfig
  | TipDisposalConfig;

export type LabwareConfig = WellPlateConfig | VialConfig | VialGridConfig | UnsupportedDeckConfig;

export interface WellPosition {
  x: number;
  y: number;
  z: number;
}

export interface GeometryResponse {
  length: number | null;
  width: number | null;
  height: number | null;
}

export interface LabwareResponse {
  key: string;
  config: LabwareConfig;
  wells: Record<string, WellPosition> | null;
  location?: Coordinate3D;
  geometry?: GeometryResponse;
  positions?: Record<string, WellPosition>;
}

export interface DeckResponse {
  filename: string;
  labware: LabwareResponse[];
}

export interface DeckConfig {
  labware: Record<string, LabwareConfig>;
}

export interface InstrumentConfig {
  type: string;
  vendor: string;
  offset_x: number;
  offset_y: number;
  depth?: number;
  offline?: boolean;
  [key: string]: unknown;
}

export interface WorkingVolume {
  x_min: number;
  x_max: number;
  y_min: number;
  y_max: number;
  z_min: number;
  z_max: number;
}

export interface CncConfig {
  factory_z_travel_mm: number;
  total_z_range?: number;
  total_z_height?: number;
  calibration_block_height_mm?: number | null;
  y_axis_motion?: "head" | "bed";
  safe_z?: number | null;
}

export interface GrblSettingsConfig {
  dir_invert_mask?: number | null;
  status_report?: number | null;
  soft_limits?: boolean | null;
  hard_limits?: boolean | null;
  homing_enable?: boolean | null;
  homing_dir_mask?: number | null;
  homing_pull_off?: number | null;
  steps_per_mm_x?: number | null;
  steps_per_mm_y?: number | null;
  steps_per_mm_z?: number | null;
  max_rate_x?: number | null;
  max_rate_y?: number | null;
  max_rate_z?: number | null;
  accel_x?: number | null;
  accel_y?: number | null;
  accel_z?: number | null;
  max_travel_x?: number | null;
  max_travel_y?: number | null;
  max_travel_z?: number | null;
}

export type OriginPolicy = "deck_origin" | "home_origin";

export interface GantryConfig {
  serial_port: string;
  gantry_type: "cub" | "cub_xl";
  cnc: CncConfig;
  working_volume: WorkingVolume;
  grbl_settings?: GrblSettingsConfig | null;
  instruments: Record<string, InstrumentConfig>;
  // Selects which physical corner WPos zero is calibrated to. Absent means
  // "deck_origin" (existing behavior: WPos zero at the front-left-bottom,
  // workspace nonnegative). See calibrationMath.buildCalibratedConfig for
  // how this changes the emitted working_volume sign convention.
  origin_policy?: OriginPolicy;
}

export interface GantryResponse {
  filename: string;
  config: GantryConfig;
}

export interface GantryPosition {
  x: number;
  y: number;
  z: number;
  work_x: number | null;
  work_y: number | null;
  work_z: number | null;
  status: string;
  connected: boolean;
  calibration_active: boolean;
  calibration_warning?: string | null;
  move_error?: string | null;
}

export interface ZCalibrationSummary {
  block_height: number;
  total_z_range: number;
  home_z: number;
  block_touch_z: number;
  home_to_block_travel: number;
  remaining_below_block: number;
  can_reach_deck_bottom: boolean;
  z_min: number;
  z_max: number;
  max_travel_z: number;
}

export interface FinalizeOriginResponse {
  measured_volume: Coordinate3D;
  z_calibration: ZCalibrationSummary;
  max_travel: Coordinate3D;
  position: Coordinate3D;
  homing_pull_off_mm?: number | null;
}

export interface GrblSettingsResponse {
  settings: Record<string, string>;
}

export interface CampaignSummary {
  campaign_id: number;
  campaign_description: string;
  created_at: string;
  latest_measurement_at: string | null;
  experiment_count: number;
  well_count: number;
  measurement_count: number;
  measurement_counts: Record<string, number>;
  asmi_measurement_count: number;
}

export interface ProtocolRunResponse {
  status: string;
  steps_executed: number;
  campaign_id: number;
}

export interface ProtocolRunStatus {
  active: boolean;
  protocol_file: string | null;
}

// Gantry-mounted instrument introspection (from CubOS)

export interface InstrumentTypeInfo {
  type: string;
  vendors: string[];
  is_mock: boolean;
}

export interface PipetteModelInfo {
  name: string;
  family: string;
  channels: number;
  max_volume: number;
  min_volume: number;
}

export interface InstrumentFieldInfo {
  name: string;
  type: string;
  required: boolean;
  default: unknown;
  choices: string[] | null;
}

export type InstrumentSchemas = Record<string, Record<string, InstrumentFieldInfo[]>>;

export type InstrumentMeasurementMethods = Record<string, string[]>;

export interface InstrumentMethodParamField {
  name: string;
  type: string;
  required: boolean;
  default: unknown;
  fields: InstrumentMethodParamField[] | null;
}

// type -> method -> ordered method_kwargs parameter specs. A param with
// non-null `fields` is a dataclass the engine builds from a nested mapping.
export type InstrumentMethodParams = Record<string, Record<string, InstrumentMethodParamField[]>>;

// Protocol

export interface CommandArg {
  name: string;
  type: string;
  required: boolean;
  default: unknown;
}

export interface CommandInfo {
  name: string;
  args: CommandArg[];
  description: string;
}

export interface ProtocolStep {
  command: string;
  args: Record<string, unknown>;
}

export interface ProtocolConfig {
  positions?: Record<string, number[]> | null;
  protocol: ProtocolStep[];
}

export interface ProtocolResponse {
  filename: string;
  positions?: Record<string, number[]> | null;
  steps: ProtocolStep[];
}

export interface ProtocolValidationResponse {
  valid: boolean;
  errors: string[];
  output?: string;
}

export interface ProtocolSetupValidationRequest {
  gantry_file: string;
  deck_file: string;
  protocol_file: string;
}

// ── Fluid/tip/cap state (Feature 07) ────────────────────────────────────

export type RunState = "queued" | "running" | "cancel_requested" | "succeeded" | "failed" | "cancelled";

export interface RunRecord {
  run_id: string;
  state: RunState;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  mock_mode: boolean;
  metadata: Record<string, unknown>;
  digests: Record<string, string>;
  result: unknown;
  error: string | null;
  artifacts: string[];
  fluid_state_id: number | null;
}

// Step-execution progress. `kind`/`data` are additive on RunEvent: events
// written before they existed default to "lifecycle"/null, and an unknown
// future kind must be ignored rather than treated as an error.
export type RunEventKind = "lifecycle" | "step";

export type StepOutcome = "started" | "completed" | "failed" | "skipped";

export interface StepEventData {
  index: number;
  command: string;
  /** Colon-joined nested scope for compound commands, e.g. "leg2:fill". */
  substep: string | null;
  outcome: StepOutcome;
  duration_s: number | null;
  error: string | null;
  reason: string | null;
}

export interface RunEvent {
  sequence: number;
  timestamp: number;
  state: RunState;
  message: string;
  kind: RunEventKind;
  data: Record<string, unknown> | null;
}

export interface RunEventsResponse {
  run_id: string;
  events: RunEvent[];
}

export interface PlanStep {
  index: number;
  command: string;
  summary: string;
  args: Record<string, unknown>;
}

export interface RunPlanResponse {
  run_id: string;
  steps: PlanStep[];
}

export interface FluidSeedItem {
  volume_ul: number;
  composition?: Record<string, number> | null;
}

export interface InitialStateSeed {
  label?: string | null;
  fluids: Record<string, FluidSeedItem>;
}

export type RunStateSelection =
  | { initial_state: InitialStateSeed; fluid_state_id?: undefined }
  | { fluid_state_id: number; initial_state?: undefined };

export interface RunSubmissionBody {
  run_id: string;
  gantry_file: string;
  deck_file: string;
  protocol_file: string;
  mock_mode?: boolean;
  metadata?: Record<string, unknown>;
  state?: RunStateSelection;
}

export interface FluidStateSummary {
  id: number;
  label: string | null;
  deck_path: string;
  deck_fingerprint: string;
  created_at: string;
  updated_at: string;
  container_count: number;
  operation_count: number;
}

export interface ContainerView {
  labware_key: string;
  location_id: string;
  labware_type: string;
  capacity_ul: number;
  working_volume_ul: number;
  current_volume_ul: number;
  composition: Record<string, number>;
  version: number;
  updated_at: string;
  role?: string | null;
  solution?: string | null;
  allowed_solutions?: string[] | null;
}

export interface FluidStateDetail {
  id: number;
  deck_path: string;
  deck_fingerprint: string;
  label: string | null;
  created_at: string;
  updated_at: string;
  containers: ContainerView[];
  pending_operation_count: number;
  reconciliation_required_count: number;
}

export interface TipContainerView {
  rack_key: string;
  slot_id: string;
  status: string;
  tip_length_mm: number;
  version: number;
  updated_at: string;
}

export interface PipetteAttachmentView {
  pipette_key: string;
  rack_key: string | null;
  slot_id: string | null;
  tip_extension_mm: number | null;
  contents_known_empty: boolean;
  attachment_uncertain: boolean;
  updated_at: string;
}

export interface TipStateResponse {
  fluid_state_id: number;
  containers: TipContainerView[];
  pipette: PipetteAttachmentView;
}

export interface CapContainerView {
  labware_key: string;
  location_id: string;
  status: string;
  version: number;
  updated_at: string;
}

export interface CapStateResponse {
  fluid_state_id: number;
  containers: CapContainerView[];
}

export type StateDomain = "fluid" | "tip" | "cap";

export interface OperationView {
  domain: StateDomain;
  id: number;
  operation_key: string;
  operation_type: string;
  status: string;
  campaign_id: number | null;
  detail: string | null;
  created_at: string;
  updated_at: string;
  applied_at: string | null;
  context: Record<string, unknown>;
}

export interface OperationsResponse {
  fluid_state_id: number;
  operations: OperationView[];
}

export interface ReconciliationResponse {
  fluid_state_id: number;
  items: OperationView[];
}

export interface ResolveReconciliationRequest {
  domain: StateDomain;
  operation_key: string;
  resolution: string;
  operator: string;
  reason: string;
  source_volume_ul?: number | null;
  source_composition?: Record<string, number> | null;
  destination_volume_ul?: number | null;
  destination_composition?: Record<string, number> | null;
  final_slot_status?: string | null;
  final_status?: string | null;
}

export interface ResolveReconciliationResponse {
  domain: StateDomain;
  operation_key: string;
  status: string;
  detail: string | null;
}

export interface CreateFluidStateRequest {
  deck_file: string;
  label?: string | null;
  fluids?: Record<string, FluidSeedItem>;
}

// Run-submission state choice, owned by App.tsx and threaded into
// ProtocolEditor. "none" preserves the exact pre-Feature-07 run flow;
// "new"/"resume" require an explicit operator choice before Run is
// enabled, and route submission through the versioned /api/v1/runs
// resource instead of the legacy synchronous endpoint.
export type FluidStateChoiceMode = "none" | "new" | "resume";

// One component of a seed row's composition, kept as raw input strings so
// the controlled inputs can hold partial/empty values while editing. Parsed
// and validated only when the run payload is assembled (see utils/fluidSeeds).
export interface CompositionSeedRow {
  id: string;
  component: string;
  volume: string;
}

// One "New fluid state" seed row: a container target plus its starting
// volume and optional composition breakdown. Volumes stay as strings for
// the same controlled-input reason as CompositionSeedRow.
export interface FluidSeedRow {
  id: string;
  container: string;
  volume: string;
  composition: CompositionSeedRow[];
}

export interface FluidStateChoice {
  mode: FluidStateChoiceMode;
  newLabel: string;
  resumeId: number | null;
  // Per-container starting volumes for a "new" fluid state. Empty (the
  // default) sends `fluids: {}`, preserving the pre-Feature-07b empty-state
  // behavior. Only consulted when `mode === "new"`.
  seeds: FluidSeedRow[];
}
