import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { overnightApi } from "./api";
import type { OvernightJob, OvernightQueueRecord } from "./types";
import "./OvernightQueuePanel.css";

const terminalStates = new Set(["completed", "failed", "cancelled", "interrupted", "blocked"]);

function words(value?: string | null): string {
  return value ? value.replaceAll("_", " ") : "—";
}

function outcome(job: OvernightJob): string {
  if (job.state !== "completed") return words(job.error || job.stop_reason || job.state);
  const reason = words(job.stop_reason);
  if (/target reached/i.test(reason)) return "Target reached";
  if (/budget|trials|max/i.test(reason)) return "Budget exhausted";
  return reason === "—" ? "Completed" : reason;
}

export default function OvernightQueuePanel() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState("");
  const startingRef = useRef(false);
  const cancellingRef = useRef(false);
  const queues = useQuery({ queryKey: ["overnight-queues"], queryFn: overnightApi.list, refetchInterval: 5000 });
  const effectiveId = selectedId || queues.data?.[0]?.queue_id || "";
  const queue = useQuery({
    queryKey: ["overnight-queue", effectiveId],
    queryFn: () => overnightApi.get(effectiveId),
    enabled: Boolean(effectiveId),
    refetchInterval: (query) => query.state.data && !terminalStates.has(query.state.data.state) ? 2000 : false,
  });
  const update = (record: OvernightQueueRecord) => {
    queryClient.setQueryData(["overnight-queue", record.queue_id], record);
    void queryClient.invalidateQueries({ queryKey: ["overnight-queues"] });
  };
  const start = useMutation({ mutationFn: overnightApi.start, onSuccess: update });
  const cancel = useMutation({ mutationFn: overnightApi.cancel, onSuccess: update });
  const startQueue = (queueId: string) => {
    if (startingRef.current) return;
    startingRef.current = true;
    start.mutate(queueId, { onSettled: () => { startingRef.current = false; } });
  };
  const cancelQueue = (queueId: string) => {
    if (cancellingRef.current) return;
    cancellingRef.current = true;
    cancel.mutate(queueId, { onSettled: () => { cancellingRef.current = false; } });
  };
  const record = queue.data;
  const error = start.error ?? cancel.error ?? queue.error ?? queues.error;

  return (
    <section className="overnight-panel">
      <header className="overnight-header">
        <div><span>Autonomous queue</span><h2>Overnight color matching</h2><p>Five independent targets run on the Pi without this Mac remaining connected.</p></div>
        <label>Prepared queue
          <select aria-label="Prepared overnight queue" value={effectiveId} onChange={(event) => setSelectedId(event.target.value)}>
            <option value="">Choose a prepared queue…</option>
            {(queues.data ?? []).map((item) => <option key={item.queue_id} value={item.queue_id}>{item.name} · {words(item.state)}</option>)}
          </select>
        </label>
      </header>

      <div className="overnight-camera-note">Mac webcam recording needs this browser to stay open. Pi well captures and the overnight queue continue without it.</div>
      {error && <div className="overnight-banner overnight-error" role="alert">{error instanceof Error ? error.message : String(error)}</div>}
      {!effectiveId && !queues.isLoading && <div className="overnight-empty">No queue selected. Prepare the five targets first, then return here to review and start.</div>}
      {queue.isLoading && <div className="overnight-empty">Loading prepared queue…</div>}

      {record && (
        <>
          <div className="overnight-toolbar">
            <div><strong>{record.name}</strong><span className={`overnight-state overnight-state-${record.state}`}>{words(record.state)}</span></div>
            <div>
              {record.state === "prepared" && <button type="button" className="overnight-primary" disabled={start.isPending} onClick={() => startQueue(record.queue_id)}>{start.isPending ? "Starting…" : "Start overnight queue"}</button>}
              {record.state === "running" && <button type="button" disabled={cancel.isPending || record.cancel_requested} onClick={() => cancelQueue(record.queue_id)}>{cancel.isPending || record.cancel_requested ? "Cancel requested" : "Cancel queue"}</button>}
            </div>
          </div>

          <div className="overnight-resources" aria-label="Queue resource plan">
            <div><strong>{record.resource_summary.campaign_count}</strong><span>targets</span></div>
            <div><strong>{record.resource_summary.sample_count}</strong><span>samples</span></div>
            <div><strong>{record.resource_summary.total_volume_ul.toLocaleString()} µL</strong><span>total volume</span></div>
            <div><strong>{record.resource_summary.tip_count}</strong><span>tips</span></div>
            <div><strong>{record.resource_summary.candidate_wells.length}</strong><span>wells</span></div>
          </div>

          {(record.error || record.stop_reason || record.state === "blocked") && (
            <div className={`overnight-banner ${record.state === "completed" ? "overnight-info" : "overnight-error"}`}>
              <strong>{record.state === "blocked" ? "Queue stopped for inspection" : words(record.state)}</strong>
              <span>{record.error || words(record.stop_reason)}</span>
            </div>
          )}

          <div className="overnight-table-wrap">
            <table className="overnight-table">
              <thead><tr><th>Target</th><th>Color</th><th>Status</th><th>Samples</th><th>Best ΔE00</th><th>Outcome</th></tr></thead>
              <tbody>{record.jobs.map((job) => (
                <tr key={job.job_id} className={job.index === record.current_job_index ? "is-active" : ""}>
                  <td><strong>{job.index + 1}. {job.name}</strong></td>
                  <td><span className="overnight-swatch" style={{ background: `rgb(${job.target_rgb.join(",")})` }} />{job.target_rgb.join(", ")}</td>
                  <td>{words(job.state)}</td>
                  <td>{job.trials_completed ?? 0} / 8</td>
                  <td>{typeof job.best_objective === "number" ? job.best_objective.toFixed(2) : "—"}</td>
                  <td>{outcome(job)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <footer className="overnight-footnote">150 µL per sample · 3 + 3 + 2 sample batches · {record.resource_summary.gantry_file} · {record.resource_summary.deck_file}</footer>
        </>
      )}
    </section>
  );
}
