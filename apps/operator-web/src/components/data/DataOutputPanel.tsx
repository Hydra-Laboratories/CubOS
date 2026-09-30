import { useState } from "react";
import type { CSSProperties } from "react";
import { dataApi } from "../../api/client";
import type { CampaignSummary } from "../../types";
import * as theme from "../../theme";

interface Props {
  campaigns: CampaignSummary[];
  isLoading: boolean;
  error: unknown;
  onRefresh: () => void;
}

export default function DataOutputPanel({
  campaigns,
  isLoading,
  error,
  onRefresh,
}: Props) {
  const [exporting, setExporting] = useState<number | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const handleDownload = async (campaign: CampaignSummary) => {
    setExporting(campaign.campaign_id);
    setExportError(null);
    try {
      const blob = await dataApi.downloadCampaignData(campaign.campaign_id);
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = `campaign_${campaign.campaign_id}_data.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(href);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting(null);
    }
  };

  return (
    <section style={panelStyle} aria-label="Campaign results output">
      <div style={headerStyle}>
        <div>
          <h3 style={titleStyle}>Campaign Results</h3>
          <div style={subtitleStyle}>Stored instrument measurement output</div>
        </div>
        <button onClick={onRefresh} style={secondaryButtonStyle}>
          Refresh
        </button>
      </div>

      {Boolean(error) && (
        <div style={errorStyle}>Data load failed: {error instanceof Error ? error.message : String(error)}</div>
      )}
      {exportError && (
        <div style={errorStyle}>Export failed: {exportError}</div>
      )}
      {isLoading && <div style={emptyStyle}>Loading campaigns...</div>}
      {!isLoading && campaigns.length === 0 && (
        <div style={emptyStyle}>No campaigns yet — run a protocol to create one.</div>
      )}
      {!isLoading && campaigns.length > 0 && (
        <div style={tableFrameStyle}>
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>Campaign</th>
                <th style={thStyle}>Last measured</th>
                <th style={thStyle}>Experiments</th>
                <th style={thStyle}>Wells</th>
                <th style={thStyle}>Measurements</th>
                <th style={thStyle}>Data</th>
              </tr>
            </thead>
            <tbody>
              {campaigns.map((campaign) => {
                const downloadDisabled = campaign.measurement_count === 0 || exporting !== null;
                const timestamp = campaign.latest_measurement_at ?? campaign.created_at;
                return (
                  <tr key={campaign.campaign_id}>
                    <td style={tdStyle}>
                      <div style={strongTextStyle}>Campaign #{campaign.campaign_id}</div>
                      <div style={metaTextStyle}>{campaign.campaign_description}</div>
                    </td>
                    <td style={tdStyle}>{formatTimestamp(timestamp)}</td>
                    <td style={tdNumericStyle}>{campaign.experiment_count}</td>
                    <td style={tdNumericStyle}>{campaign.well_count}</td>
                    <td style={tdNumericStyle}>{campaign.measurement_count}</td>
                    <td style={tdStyle}>
                      <button
                        onClick={() => void handleDownload(campaign)}
                        disabled={downloadDisabled}
                        style={primaryButtonStyle}
                      >
                        {exporting === campaign.campaign_id ? "Preparing..." : "Download Data"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

// The panel sits inside the shared card shell, so it stays borderless-clean.
const panelStyle: CSSProperties = {
  overflow: "hidden",
};

const headerStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "12px 14px",
  borderBottom: `1px solid ${theme.color.border}`,
};

const titleStyle: CSSProperties = {
  ...theme.panelTitle,
};

const subtitleStyle: CSSProperties = {
  marginTop: 2,
  color: theme.color.textMuted,
  fontSize: 12,
};

const tableFrameStyle: CSSProperties = {
  overflowX: "auto",
};

const tableStyle: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: 13,
  color: theme.color.text,
};

const thStyle: CSSProperties = {
  ...theme.sectionLabel,
  padding: "9px 12px",
  textAlign: "left",
  borderBottom: `1px solid ${theme.color.border}`,
};

const tdStyle: CSSProperties = {
  padding: "10px 12px",
  borderBottom: `1px solid ${theme.color.border}`,
  verticalAlign: "middle",
};

const tdNumericStyle: CSSProperties = {
  ...tdStyle,
  ...theme.mono,
};

const strongTextStyle: CSSProperties = {
  fontWeight: 600,
  color: theme.color.ink,
};

const metaTextStyle: CSSProperties = {
  marginTop: 2,
  color: theme.color.textMuted,
  fontSize: 12,
};

const emptyStyle: CSSProperties = {
  padding: "24px 16px",
  color: theme.color.textMuted,
  fontSize: 13,
  textAlign: "center",
};

const errorStyle: CSSProperties = {
  ...theme.notice.error,
  margin: 12,
};

const primaryButtonStyle: CSSProperties = {
  ...theme.btn.primary,
  ...theme.btnSmall,
};

const secondaryButtonStyle: CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
};
