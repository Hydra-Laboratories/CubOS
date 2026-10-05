import type { DeckResponse, FluidStateSummary, GantryResponse } from "../../types";

export type CampaignState =
  | "running" | "paused" | "awaiting_observation" | "awaiting_refill" | "completed"
  | "stopped" | "failed" | "interrupted";

export interface RefillRequirement {
  target: string;
  available_ul: number;
  required_ul: number;
  capacity_ul: number;
}
export interface PendingCampaignBatch {
  batch_index: number;
  run_id?: string | null;
  [key: string]: unknown;
}

export interface CampaignBinding { step_index: number; argument: string }
export interface CampaignParameter {
  name: string; minimum: number; maximum: number; step: number; bindings: CampaignBinding[];
}
export interface CampaignSequence { name: string; values: string[]; bindings: CampaignBinding[] }
export interface CampaignSpec {
  name: string; gantry_file: string; deck_file: string; protocol_file: string;
  batch_size?: number;
  source_protocol_file?: string | null;
  target_mode?: "camera" | "rgb";
  target_rgb?: [number, number, number] | null;
  parameters: CampaignParameter[]; sequences: CampaignSequence[];
  objective: { mode: "result" | "manual"; path: string; direction: "minimize" | "maximize" };
  optimizer: {
    method: "ei" | "lcb" | "random";
    kernel: "matern52" | "rbf";
    initial_trials: number;
    initial_points: Record<string, number>[];
    exploration: number;
    seed: number;
  };
  stop: { max_trials: number; target_value: number | null; patience: number; min_improvement: number; max_seconds: number | null };
  sum_constraint?: { parameters: string[]; total: number } | null;
  mock_mode: boolean; fluid_state_id: number | null;
}
export interface CampaignTrial { index: number; parameters: Record<string, number | string>; run_id: string; state: string; objective: number | null; measurement?: Record<string, unknown> | null; error?: string | null; objective_status?: "pending" | "accepted" | "unverified" | "rejected"; objective_path?: string | null; sample_well?: string | null; batch_index?: number | null }
export interface CampaignRecord {
  campaign_id: string | number; spec: CampaignSpec; state: CampaignState; created_at: string | number; updated_at: string | number;
  active_run_id: string | null; trials: CampaignTrial[]; best_objective: number | null; stop_reason: string | null;
  error: string | null; pause_requested: boolean; stop_requested: boolean;
  pause_reason?: "operator" | "inventory_refill" | null;
  pending_batch?: PendingCampaignBatch | null;
  refill_requirements?: RefillRequirement[];
}
export interface ProtocolStep { command: string; args: Record<string, unknown> }
export interface ColorCampaignSetup {
  gantry_file: string; deck_file: string; source_protocol_file: string; batch_size: number; target_well: string;
  target_mode?: "camera" | "rgb";
  target_rgb?: [number, number, number] | null;
  target_lab?: [number, number, number] | null; red_source: string;
  yellow_source: string; blue_source: string;
  diluent_source?: string | null;
  component_min_ul?: number;
  component_max_ul?: number;
  total_volume_ul?: number;
  candidate_wells: string[];
  camera_instrument: string; roi_fraction: number;
  image_height?: number | null;
  photo_position?: [number, number, number] | null;
  expected_center?: [number, number] | null;
  expected_center_source?: "operator_selected" | null;
  reference_processing_profile_id?: string | null;
  target_run_id?: string | null;
  target_analysis_revision?: number | null;
  fluid_state_id: number | null; mock_mode: boolean;
}
export interface ColorTargetRun {
  run_id: string; state: "queued" | "running" | "cancel_requested" | "succeeded" | "failed" | "cancelled";
  result: unknown; error: string | null;
}

export interface ColorSetupDraft {
  source_protocol_file?: string;
  batch_size?: number;
  target_mode?: "camera" | "rgb";
  target_rgb?: [number, number, number] | null;
  target_well: string;
  red_source: string;
  yellow_source: string;
  blue_source: string;
  diluent_source?: string | null;
  component_min_ul?: number;
  component_max_ul?: number;
  total_volume_ul?: number;
  candidate_wells: string[];
  camera_instrument: string;
  roi_fraction: number;
  image_height: number | null;
  photo_position?: [number, number, number] | null;
}

export interface CampaignPreset {
  schema_version: "cubos.campaign-preset.v1";
  name: string;
  spec: CampaignSpec;
  color_setup: ColorSetupDraft | null;
  requires_fresh_state: true;
  requires_fresh_target: true;
}

export interface CampaignPresetSummary { filename: string; name: string; modified_at: number }
export interface CampaignPresetResponse { filename: string; preset: CampaignPreset }

export interface CameraMonitorStatus {
  instrument: string;
  state: "stopped" | "running" | "failed";
  connected: boolean;
  camera_id: number | null;
  lease_id?: string | null;
  lease_expires_at?: number | null;
  subscriber_count?: number;
  requested_resolution: { width: number; height: number } | null;
  actual_resolution: { width: number; height: number } | null;
  requested_pixel_format: string | null;
  actual_pixel_format: string | null;
  frame_id: number | null;
  received_at: number | null;
  frame_age_seconds: number | null;
  image_url: string | null;
  control_fingerprint: string | null;
  capture_profile?: Record<string, unknown> | null;
  run_id: string | null;
  campaign_id: string | null;
  trial_number: number | null;
  step_index: number | null;
  step_command: string | null;
  step_substep: string | null;
  expected_well: string | null;
  expected_center?: { x: number; y: number } | null;
  expected_center_source?: string | null;
  well_identity_verification: "not_verified_by_cv";
  roi: Record<string, unknown> | null;
  quality: Record<string, unknown> | null;
  processing_profile: Record<string, unknown> | null;
  analysis_source_image_path?: string | null;
  analysis_image_url?: string | null;
  analysis_source_run_id?: string | null;
  analysis_source_well?: string | null;
  analysis_source_frame_id?: number | null;
  analysis_source_received_at?: number | null;
  analysis_is_current_frame?: boolean;
  latest_analysis?: Record<string, unknown> | null;
  warnings: string[];
  error: string | null;
}
export interface CampaignPanelProps {
  gantryFile: string | null; deckFile: string | null; protocolFile: string | null;
  protocolSteps?: ProtocolStep[]; deck?: DeckResponse | null; gantry?: GantryResponse | null; availableFluidStates?: FluidStateSummary[];
  disabledReason?: string | null;
  onRunSelected?: (runId: string) => void; onCampaignChange?: (record: CampaignRecord | null) => void;
}
