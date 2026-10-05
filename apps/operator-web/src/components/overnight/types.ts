export type OvernightQueueState = "prepared" | "running" | "completed" | "failed" | "cancelled" | "interrupted" | "blocked";
export type OvernightJobState = "pending" | "starting" | "active" | "completed" | "failed" | "cancelled" | "interrupted" | "blocked";

export interface OvernightJob {
  job_id: string;
  index: number;
  name: string;
  state: OvernightJobState;
  target_rgb: [number, number, number];
  optimizer_seed: number;
  color_setup: Record<string, unknown>;
  campaign_id?: string | number | null;
  created_at?: string | number | null;
  started_at?: string | number | null;
  completed_at?: string | number | null;
  best_objective?: number | null;
  best_parameters?: Record<string, unknown> | null;
  trials_completed?: number | null;
  stop_reason?: string | null;
  error?: string | null;
}

export interface OvernightQueueRecord {
  queue_id: string;
  name: string;
  state: OvernightQueueState;
  created_at: string | number;
  updated_at: string | number;
  started_at?: string | number | null;
  completed_at?: string | number | null;
  current_job_index?: number | null;
  stop_reason?: string | null;
  error?: string | null;
  cancel_requested: boolean;
  resource_summary: {
    campaign_count: number;
    sample_count: number;
    tip_count: number;
    total_volume_ul: number;
    candidate_wells: string[];
    fluid_state_id?: string | number | null;
    gantry_file: string;
    deck_file: string;
  };
  jobs: OvernightJob[];
  events: { sequence: number; timestamp: string | number; kind: string; job_index?: number | null; campaign_id?: string | number | null; message: string; data?: Record<string, unknown> | null }[];
}
