import { useEffect, useRef } from "react";
import * as theme from "../../theme";

interface Props {
  onClose: () => void;
  onFull: () => void;
  onOffsets: () => void;
  fullDisabled?: boolean;
  offsetsDisabledReason: string | null;
}

export default function CalibrationModeChooser({ onClose, onFull, onOffsets, fullDisabled = false, offsetsDisabledReason }: Props) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLElement>("button:not([disabled])")?.focus();
    return () => previous?.focus();
  }, []);

  return <div style={overlay}>
    <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="calibration-mode-title" aria-describedby="calibration-mode-description" style={modal}
      onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); onClose(); }
        if (event.key === "Tab") {
          const items = Array.from(dialog.current?.querySelectorAll<HTMLElement>("button:not([disabled])") ?? []);
          const first = items[0], last = items[items.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }
      }}>
      <h2 id="calibration-mode-title" style={theme.panelTitle}>Choose calibration mode</h2>
      <p id="calibration-mode-description" style={description}>Choose what you want to calibrate.</p>
      <div style={choice}>
        <button style={theme.btn.primary} disabled={fullDisabled} onClick={onFull}>Full calibration</button>
        <p style={description}>Set the gantry origin, travel limits, and instrument mounting offsets.</p>
      </div>
      <div style={choice}>
        <button style={theme.btn.secondary} disabled={offsetsDisabledReason !== null} aria-describedby={offsetsDisabledReason ? "offset-mode-unavailable" : undefined} onClick={onOffsets}>Calibrate offsets only</button>
        <p style={description}>Measure instrument offsets against a calibrated reference. Keep the existing origin and travel limits.</p>
        {offsetsDisabledReason && <p id="offset-mode-unavailable" style={{ ...description, color: theme.color.textMuted }}>{offsetsDisabledReason}</p>}
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}><button style={theme.btn.secondary} onClick={onClose}>Cancel</button></div>
    </div>
  </div>;
}

const overlay: React.CSSProperties = { position: "fixed", inset: 0, zIndex: 50, background: theme.chrome.backdrop, display: "flex", alignItems: "center", justifyContent: "center", padding: 20 };
const modal: React.CSSProperties = { width: "min(540px, 96vw)", maxHeight: "92vh", overflow: "auto", padding: 20, background: theme.color.surface, border: `1px solid ${theme.color.border}`, borderRadius: theme.radius.lg, boxShadow: theme.shadow.overlay };
const description: React.CSSProperties = { fontSize: 13, lineHeight: 1.5, color: theme.color.textSecondary, margin: "10px 0 0" };
const choice: React.CSSProperties = { borderTop: `1px solid ${theme.color.border}`, paddingTop: 16, marginTop: 16 };
