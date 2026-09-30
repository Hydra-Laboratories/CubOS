import { useEffect, useMemo, useState } from "react";
import * as theme from "../../theme";
import { campaignApi, reconcileCampaignStock } from "./api";
import type { CampaignRecord, RefillRequirement } from "./types";

interface Props {
  campaign: CampaignRecord;
  onUpdated: (campaign: CampaignRecord) => void;
}

interface RefillDraft {
  volume: string;
  composition: string;
}

export const refillOperationKey = (campaign: CampaignRecord, requirement: RefillRequirement) => {
  const pendingBatch = campaign.pending_batch;
  const batchIdentity = pendingBatch?.run_id || `batch-${pendingBatch?.batch_index ?? "unknown"}`;
  return `campaign-${campaign.campaign_id}-${batchIdentity}-refill-${String(requirement.target).replace(/[^a-zA-Z0-9_.-]/g, "_")}`;
};

export default function CampaignRefillRecovery({ campaign, onUpdated }: Props) {
  const requirements = campaign.refill_requirements ?? [];
  const [drafts, setDrafts] = useState<Record<string, RefillDraft>>({});
  const [operator, setOperator] = useState("");
  const [reason, setReason] = useState("Stocks refilled for active-learning campaign");
  const [confirmed, setConfirmed] = useState<Set<string>>(() => new Set());
  const [busyTarget, setBusyTarget] = useState<string | null>(null);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDrafts((current) => Object.fromEntries(requirements.map((requirement) => [
      requirement.target,
      current[requirement.target] ?? { volume: "", composition: "" },
    ])));
    setConfirmed((current) => new Set([...current].filter((target) => requirements.some((item) => item.target === target))));
  }, [requirements]);

  const allConfirmed = requirements.length > 0 && requirements.every((item) => confirmed.has(item.target));
  const shortageSummary = useMemo(() => requirements.map((item) =>
    `${item.target}: ${item.available_ul.toFixed(1)} µL available; ${item.required_ul.toFixed(1)} µL required`,
  ), [requirements]);

  const confirm = async (requirement: RefillRequirement) => {
    const draft = drafts[requirement.target] ?? { volume: "", composition: "" };
    const volume = Number(draft.volume);
    if (!Number.isFinite(volume) || volume < requirement.required_ul || volume > requirement.capacity_ul) {
      setError(`${requirement.target}: confirmed final volume must be between ${requirement.required_ul} and ${requirement.capacity_ul} µL.`);
      return;
    }
    let composition: Record<string, number> | undefined;
    if (draft.composition.trim()) {
      try {
        const parsed = JSON.parse(draft.composition) as unknown;
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("not an object");
        composition = Object.fromEntries(Object.entries(parsed as Record<string, unknown>).map(([name, amount]) => {
          const numeric = Number(amount);
          if (!name.trim() || !Number.isFinite(numeric) || numeric < 0) throw new Error("invalid component");
          return [name, numeric];
        }));
      } catch {
        setError(`${requirement.target}: composition must be JSON such as {"red": 1000}.`);
        return;
      }
      const compositionTotal = Object.values(composition).reduce((sum, amount) => sum + amount, 0);
      if (Math.abs(compositionTotal - volume) > 1e-6) {
        setError(`${requirement.target}: composition volumes must sum to the confirmed final volume.`);
        return;
      }
    }
    if (!operator.trim() || !reason.trim()) {
      setError("Operator and refill reason are required for the inventory audit trail.");
      return;
    }
    if (campaign.spec.fluid_state_id === null) {
      setError("This campaign has no durable fluid state to reconcile.");
      return;
    }
    setBusyTarget(requirement.target);
    setError(null);
    try {
      await reconcileCampaignStock(campaign.spec.fluid_state_id, {
        target: requirement.target,
        volume_ul: volume,
        ...(composition ? { composition } : {}),
        operation_key: refillOperationKey(campaign, requirement),
        operator: operator.trim(),
        reason: reason.trim(),
      });
      setConfirmed((current) => new Set(current).add(requirement.target));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusyTarget(null);
    }
  };

  const resume = async () => {
    if (!allConfirmed) return;
    setResuming(true);
    setError(null);
    try {
      onUpdated(await campaignApi.resume(campaign.campaign_id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setResuming(false);
    }
  };

  return <section className="campaign-banner campaign-info" aria-label="Campaign stock refill">
    <strong>Campaign paused for stock refill</strong>
    <p>{campaign.error || "The exact pending batch needs more stock."}</p>
    {shortageSummary.map((line) => <div key={line}>{line}</div>)}
    <p><strong>Enter the final total volume now present in each stock container.</strong> This is not the amount you added.</p>
    <div className="campaign-fields">
      <label className="campaign-field">Operator<input aria-label="Refill operator" value={operator} onChange={(event) => setOperator(event.target.value)} /></label>
      <label className="campaign-field">Reason<input aria-label="Refill reason" value={reason} onChange={(event) => setReason(event.target.value)} /></label>
    </div>
    {requirements.map((requirement) => {
      const draft = drafts[requirement.target] ?? { volume: "", composition: "" };
      const done = confirmed.has(requirement.target);
      return <div className="campaign-card" key={requirement.target}>
        <strong>{requirement.target}</strong>
        <div>{requirement.available_ul.toFixed(1)} µL recorded · at least {requirement.required_ul.toFixed(1)} µL needed · {requirement.capacity_ul.toFixed(1)} µL capacity</div>
        <div className="campaign-fields">
          <label className="campaign-field">Confirmed final volume (µL)<input aria-label={`${requirement.target} confirmed final volume`} type="number" min={requirement.required_ul} max={requirement.capacity_ul} step="any" value={draft.volume} disabled={done} onChange={(event) => setDrafts((current) => ({ ...current, [requirement.target]: { ...draft, volume: event.target.value } }))} /></label>
          <label className="campaign-field">Composition JSON (optional)<input aria-label={`${requirement.target} composition`} value={draft.composition} disabled={done} placeholder="Leave blank to preserve the stock composition" onChange={(event) => setDrafts((current) => ({ ...current, [requirement.target]: { ...draft, composition: event.target.value } }))} /></label>
        </div>
        <button type="button" style={theme.btn.secondary} disabled={done || busyTarget !== null || resuming} onClick={() => void confirm(requirement)}>{done ? "Refill confirmed" : busyTarget === requirement.target ? "Saving…" : `Confirm ${requirement.target} refill`}</button>
      </div>;
    })}
    {requirements.length === 0 && <div className="campaign-banner campaign-error">No structured refill requirements were supplied. Refresh the campaign before editing inventory.</div>}
    {error && <div className="campaign-banner campaign-error" role="alert">{error}</div>}
    <button type="button" style={theme.btn.primary} disabled={!allConfirmed || busyTarget !== null || resuming} onClick={() => void resume()}>{resuming ? "Resuming…" : "Resume pending batch"}</button>
  </section>;
}
