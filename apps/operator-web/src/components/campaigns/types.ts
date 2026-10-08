export type CampaignState =
  | "running" | "paused" | "awaiting_observation" | "completed"
  | "stopped" | "failed" | "interrupted";

export interface CampaignBinding { step_index: number; argument: string }
export interface CampaignParameter {
  name: string; minimum: number; maximum: number; step: number; bindings: CampaignBinding[];
}
export interface CampaignSequence { name: string; values: string[]; bindings: CampaignBinding[] }
export interface CampaignSpec {
  name: string; gantry_file: string; deck_file: string; protocol_file: string;
  parameters: CampaignParameter[]; sequences: CampaignSequence[];
  objective: { mode: "result" | "manual"; path: string; direction: "minimize" | "maximize" };
  optimizer: { method: "ei" | "lcb" | "random"; initial_trials: number; exploration: number; seed: number };
  stop: { max_trials: number; target_value: number | null; patience: number; min_improvement: number; max_seconds: number | null };
  sum_constraint?: { parameters: string[]; total: number } | null;
  mock_mode: boolean; fluid_state_id: number | null;
}
export interface CampaignTrial { index: number; parameters: Record<string, number | string>; run_id: string; state: string; objective: number | null; error?: string | null }
export interface CampaignRecord {
  campaign_id: string | number; spec: CampaignSpec; state: CampaignState; created_at: string | number; updated_at: string | number;
  active_run_id: string | null; trials: CampaignTrial[]; best_objective: number | null; stop_reason: string | null;
  error: string | null; pause_requested: boolean; stop_requested: boolean;
}
export interface ProtocolStep { command: string; args: Record<string, unknown> }
export interface CampaignPanelProps {
  gantryFile: string | null; deckFile: string | null; protocolFile: string | null;
  protocolSteps?: ProtocolStep[]; disabledReason?: string | null;
  onRunSelected?: (runId: string) => void; onCampaignChange?: (record: CampaignRecord | null) => void;
}
