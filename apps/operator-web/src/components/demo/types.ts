export interface DemoColorMeasurement {
  rgb?: [number, number, number] | null;
  lab?: [number, number, number] | null;
  delta_e?: number | null;
  quality?: Record<string, unknown> | null;
  profile?: Record<string, unknown> | null;
  frame?: { width_px: number; height_px: number } | null;
  roi?: {
    center_x_px: number;
    center_y_px: number;
    radius_px: number;
  } | null;
}

export interface DemoTarget {
  source: string;
  well?: string | null;
  accepted: boolean;
  measurement?: DemoColorMeasurement | null;
  image_asset_id?: string | null;
  raw_image_asset_id?: string | null;
  annotated_image_asset_id?: string | null;
  quality?: Record<string, unknown> | null;
}

export interface DemoAttempt {
  sequence: number;
  reveal_event_sequence: number | null;
  trial_id: string;
  well?: string | null;
  recipe_ul: Record<string, number>;
  status: string;
  accepted: boolean;
  score_eligible: boolean;
  measurement?: DemoColorMeasurement | null;
  image_asset_id?: string | null;
  raw_image_asset_id?: string | null;
  annotated_image_asset_id?: string | null;
  started_at?: string | number | null;
  completed_at?: string | number | null;
}

export interface DemoBest {
  trial_id: string;
  sequence: number;
  delta_e: number;
}

export interface DemoEvent {
  sequence: number;
  kind: string;
  server_time: string | number;
  elapsed_ms: number;
  trial_id?: string | null;
  label: string;
  data?: Record<string, unknown> | null;
}

export interface DemoMarker {
  id: string;
  sequence: number;
  server_time: string | number;
  client_time?: string | number | null;
  label: string;
  footage_offset_ms?: number | null;
  timeline_elapsed_ms?: number | null;
  video_time_ms?: number | null;
  recording_name?: string | null;
  recording_size?: number | null;
  recording_last_modified?: number | null;
  recording_sha256?: string | null;
}

export interface CampaignPresentation {
  schema_version: "1";
  campaign_id: string | number;
  campaign_name?: string | null;
  status: string;
  target: DemoTarget;
  attempts: DemoAttempt[];
  best: DemoBest | null;
  events: DemoEvent[];
  markers: DemoMarker[];
  partial: boolean;
  missing: string[];
}

export interface DemoCampaignChoice {
  campaign_id: string | number;
  spec?: { name?: string };
  state?: string;
  created_at?: string | number;
}
