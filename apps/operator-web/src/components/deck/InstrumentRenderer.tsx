import type { GantryPosition, InstrumentConfig } from "../../types";
import { categorical as themeCategorical, viz as themeViz } from "../../theme";
import { machineToSvg } from "../../utils/coordinates";

interface Props {
  label: string;
  instrument: InstrumentConfig;
  gantryPosition: GantryPosition | null;
  svgWidth: number;
  svgHeight: number;
  machineXRange: [number, number];
  machineYRange: [number, number];
}

const INSTRUMENT_COLORS: Record<string, string> = {
  uvvis_ccs: themeCategorical.violet,
  mock_uvvis_ccs: themeCategorical.violet,
  pipette: themeCategorical.emerald,
  mock_pipette: themeCategorical.emerald,
  filmetrics: themeCategorical.amber,
  mock_filmetrics: themeCategorical.amber,
};

const INSTRUMENT_FALLBACK_COLOR = themeCategorical.slate;

export default function InstrumentRenderer({
  label,
  instrument,
  gantryPosition,
  svgWidth,
  svgHeight,
  machineXRange,
  machineYRange,
}: Props) {
  const color = INSTRUMENT_COLORS[instrument.type] ?? INSTRUMENT_FALLBACK_COLOR;

  // When gantry is connected and WPos is available, show instrument at WPos + offset
  if (gantryPosition?.connected && gantryPosition.work_x != null && gantryPosition.work_y != null) {
    const instX = gantryPosition.work_x + (instrument.offset_x ?? 0);
    const instY = gantryPosition.work_y + (instrument.offset_y ?? 0);
    const { sx, sy } = machineToSvg(instX, instY, svgWidth, svgHeight, machineXRange, machineYRange);
    const labelY = Math.max(12, sy - 10);

    return (
      <g>
        <rect x={sx - 7} y={sy - 7} width={14} height={14} rx={2} fill={color} opacity={0.7}>
          <title>
            {label} ({instrument.type}) at ({instX.toFixed(1)}, {instY.toFixed(1)})
          </title>
        </rect>
        <text x={sx} y={labelY} fill={color} fontSize={9} textAnchor="middle" fontWeight={600} stroke={themeViz.halo} strokeWidth={3} paintOrder="stroke" strokeLinejoin="round">
          {label}
        </text>
      </g>
    );
  }

  // When not connected (or no WPos), show instrument offset as a vector from origin
  const originSvg = machineToSvg(0, 0, svgWidth, svgHeight, machineXRange, machineYRange);
  const offsetSvg = machineToSvg(
    instrument.offset_x ?? 0,
    instrument.offset_y ?? 0,
    svgWidth,
    svgHeight,
    machineXRange,
    machineYRange
  );
  const labelY = Math.max(12, offsetSvg.sy - 10);

  return (
    <g>
      <line
        x1={originSvg.sx}
        y1={originSvg.sy}
        x2={offsetSvg.sx}
        y2={offsetSvg.sy}
        stroke={color}
        strokeWidth={1}
        strokeDasharray="4 2"
        opacity={0.5}
      />
      <rect x={offsetSvg.sx - 7} y={offsetSvg.sy - 7} width={14} height={14} rx={2} fill="none" stroke={color} strokeWidth={1.5} opacity={0.6}>
        <title>
          {label} ({instrument.type}) offset: ({instrument.offset_x}, {instrument.offset_y})
        </title>
      </rect>
      <text x={offsetSvg.sx} y={labelY} fill={color} fontSize={9} textAnchor="middle" fontWeight={600} opacity={0.7} stroke={themeViz.halo} strokeWidth={3} paintOrder="stroke" strokeLinejoin="round">
        {label}
      </text>
    </g>
  );
}
