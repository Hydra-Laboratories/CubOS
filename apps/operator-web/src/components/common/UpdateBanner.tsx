import React, { useEffect, useRef, useState } from "react";
import { systemApi, type UpdateStatus } from "../../api/client";
import * as theme from "../../theme";
import type { ConfirmOptions } from "./ConfirmDialog";

const CHECK_INTERVAL_MS = 30 * 60 * 1000;
const HEALTH_INTERVAL_MS = 2 * 1000;
const MAX_HEALTH_POLLS = (5 * 60 * 1000) / HEALTH_INTERVAL_MS;
const SLOW_UPDATE_MESSAGE =
  "Update taking longer than expected — check journalctl -u cubos-update";

function httpStatusOf(caught: unknown): number | undefined {
  if (caught && typeof caught === "object" && "status" in caught) {
    const status = (caught as { status: unknown }).status;
    if (typeof status === "number") return status;
  }
  return undefined;
}

type RequestConfirm = (request: string | ConfirmOptions) => Promise<boolean>;

interface Props {
  requestConfirm: RequestConfirm;
  reloadPage?: () => void;
}

export function UpdateBanner({
  requestConfirm,
  reloadPage = () => window.location.reload(),
}: Props) {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const healthTimer = useRef<number | null>(null);
  const healthPolls = useRef(0);
  const serviceWentDown = useRef(false);
  const targetSha = useRef<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const check = async () => {
      try {
        const next = await systemApi.getUpdateStatus();
        if (mounted.current) setStatus(next);
      } catch {
        // Update discovery must never disrupt normal operator work.
      }
    };
    void check();
    const interval = window.setInterval(check, CHECK_INTERVAL_MS);
    return () => {
      mounted.current = false;
      window.clearInterval(interval);
      if (healthTimer.current !== null) window.clearTimeout(healthTimer.current);
    };
  }, []);

  const scheduleHealthPoll = () => {
    healthTimer.current = window.setTimeout(() => {
      void pollHealth();
    }, HEALTH_INTERVAL_MS);
  };

  const pollHealth = async () => {
    if (!mounted.current) return;
    healthPolls.current += 1;
    if (healthPolls.current > MAX_HEALTH_POLLS) {
      setUpdating(false);
      setError(SLOW_UPDATE_MESSAGE);
      return;
    }
    try {
      const next = await systemApi.getUpdateStatus();
      // Reload once the service reports the target revision. A restart can be
      // faster than the poll interval, so the down→up transition alone is not
      // a reliable completion signal; it still catches rollbacks, where the
      // service bounced but the revision never changed.
      if (
        (targetSha.current !== null && next.current_sha === targetSha.current) ||
        serviceWentDown.current
      ) {
        reloadPage();
        return;
      }
    } catch (caught: unknown) {
      if (httpStatusOf(caught) === 404) {
        // Restarted onto a revision that predates this endpoint.
        reloadPage();
        return;
      }
      serviceWentDown.current = true;
    }
    scheduleHealthPoll();
  };

  const startUpdate = async () => {
    const confirmed = await requestConfirm({
      title: "Update CubOS",
      message:
        "Update CubOS and restart the service? Any unsaved work in progress will be interrupted.",
      confirmLabel: "Update & restart",
    });
    if (!confirmed) return;

    setError(null);
    try {
      const applied = await systemApi.applyUpdate();
      if (!mounted.current) return;
      setUpdating(true);
      targetSha.current = applied.target_sha;
      healthPolls.current = 0;
      serviceWentDown.current = false;
      void pollHealth();
    } catch (caught: unknown) {
      if (mounted.current) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    }
  };

  if (!status?.update_available) return null;

  const commitLabel = status.commits_behind === 1 ? "commit" : "commits";
  const fromLabel = status.current_tag ?? status.current_sha.slice(0, 7);
  const toLabel = status.latest_tag ?? status.latest_sha.slice(0, 7);
  return (
    <div role="status" style={bannerStyle}>
      <div style={messageStyle}>
        <span>
          Update available ({fromLabel} → {toLabel}, {status.commits_behind}{" "}
          {commitLabel})
        </span>
        {error && <span style={errorStyle}>{error}</span>}
      </div>
      <button
        type="button"
        onClick={() => void startUpdate()}
        disabled={updating}
        className={updating ? "cubos-pulse" : undefined}
        style={{
          ...theme.btn.secondary,
          ...theme.btnSmall,
          opacity: updating ? 0.65 : 1,
          cursor: updating ? "wait" : "pointer",
        }}
      >
        {updating ? "Updating…" : "Update & restart"}
      </button>
    </div>
  );
}

const bannerStyle: React.CSSProperties = {
  flex: "0 0 auto",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 16,
  minHeight: 36,
  padding: "5px 16px",
  background: theme.color.warningBg,
  borderBottom: `1px solid ${theme.color.warningBorder}`,
  color: theme.color.warningText,
  fontSize: 12,
  fontWeight: 600,
};

const messageStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
};

const errorStyle: React.CSSProperties = {
  color: theme.color.dangerText,
  fontWeight: 500,
};
