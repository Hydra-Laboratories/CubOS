import type { ReactNode } from "react";
import "./AppLayout.css";

interface Props {
  banner?: ReactNode;
  header: ReactNode;
  left: ReactNode;
  topRight: ReactNode;
  bottomRight: ReactNode;
  stationOpen: boolean;
  onStationOpenChange: (open: boolean) => void;
}

export default function AppLayout({ banner, header, left, topRight, bottomRight, stationOpen, onStationOpenChange }: Props) {
  return (
    <div className="app-shell">
      {banner}
      <header className="app-header">{header}</header>
      <div className={`app-workspace${stationOpen ? " station-open" : " station-closed"}`}>
        <main className="app-primary" id="main-content">
          {left}
        </main>
        <aside className="app-station" aria-label="Station controls">
          <button
            type="button"
            className="app-station-toggle"
            aria-expanded={stationOpen}
            aria-controls="station-sidebar-content"
            onClick={() => onStationOpenChange(!stationOpen)}
          >
            <span aria-hidden="true">{stationOpen ? "›" : "‹"}</span>
            <strong>{stationOpen ? "Hide station" : "Station"}</strong>
          </button>
          <div id="station-sidebar-content" className="app-station-content" hidden={!stationOpen}>
            <section className="app-station-deck">{topRight}</section>
            <section className="app-station-controls">{bottomRight}</section>
          </div>
        </aside>
      </div>
    </div>
  );
}
