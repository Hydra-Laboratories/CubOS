import type { TouchEvent } from "react";
import * as theme from "../../theme";

export const MIN_JOG_STEP = 0.001;

export default function JogPanel({
  xyStep,
  zStep,
  setXyStep,
  setZStep,
  disabled,
  alarmed,
  onStartJog,
  onStopJog,
  xy,
  z,
  stepInvalid,
  xyStepInvalid,
  zStepInvalid,
  xyBelowMin,
  zBelowMin,
}: {
  xyStep: string;
  zStep: string;
  setXyStep: (value: string) => void;
  setZStep: (value: string) => void;
  disabled: boolean;
  alarmed: boolean;
  onStartJog: (x: number, y: number, z: number) => void;
  onStopJog: () => void;
  xy: number;
  z: number;
  stepInvalid: boolean;
  xyStepInvalid: boolean;
  zStepInvalid: boolean;
  xyBelowMin: boolean;
  zBelowMin: boolean;
}) {
  const jogLocked = disabled || alarmed || stepInvalid;
  const props = (x: number, y: number, dz: number) => ({
    onMouseDown: () => !jogLocked && onStartJog(x, y, dz),
    onMouseUp: onStopJog,
    onMouseLeave: onStopJog,
    onTouchStart: (event: TouchEvent) => {
      event.preventDefault();
      if (!jogLocked) onStartJog(x, y, dz);
    },
    onTouchEnd: onStopJog,
  });

  return (
    <div style={jogPanelStyle}>
      <div style={dpadStyle}>
        <div />
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(0, xy, 0)} title="Y+">↑</button>
        <div />
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(-xy, 0, 0)} title="X-">←</button>
        <div style={padCenterStyle}>XY</div>
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(xy, 0, 0)} title="X+">→</button>
        <div />
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(0, -xy, 0)} title="Y-">↓</button>
        <div />
      </div>
      <div style={zPadStyle}>
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(0, 0, z)} title="Z+">Z+</button>
        <div style={padCenterStyle}>Z</div>
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(0, 0, -z)} title="Z-">Z-</button>
      </div>
      <div style={stepFieldsStyle}>
        <label style={stepFieldStyle}>
          <span style={labelStyle}>XY mm</span>
          <input
            value={xyStep}
            onChange={(event) => setXyStep(event.target.value)}
            disabled={disabled || alarmed}
            inputMode="decimal"
            style={buttonStateStyle({ ...smallInputStyle, borderColor: xyStepInvalid || xyBelowMin ? theme.color.danger : undefined }, disabled || alarmed)}
          />
        </label>
        <label style={stepFieldStyle}>
          <span style={labelStyle}>Z mm</span>
          <input
            value={zStep}
            onChange={(event) => setZStep(event.target.value)}
            disabled={disabled || alarmed}
            inputMode="decimal"
            style={buttonStateStyle({ ...smallInputStyle, borderColor: zStepInvalid || zBelowMin ? theme.color.danger : undefined }, disabled || alarmed)}
          />
        </label>
        {(stepInvalid || xyBelowMin || zBelowMin) && (
          <div style={stepHintStyle}>
            {stepInvalid ? "Enter step sizes greater than 0." : `Minimum jog step is ${MIN_JOG_STEP} mm.`}
          </div>
        )}
      </div>
    </div>
  );
}

function buttonStateStyle(base: React.CSSProperties, disabled: boolean): React.CSSProperties {
  if (!disabled) return base;
  return {
    ...base,
    opacity: 0.45,
    cursor: "not-allowed",
  };
}

const labelStyle: React.CSSProperties = {
  ...theme.fieldLabel,
};

const smallInputStyle: React.CSSProperties = {
  ...theme.input,
  ...theme.mono,
  minWidth: 0,
  width: 58,
  padding: "4px 6px",
  fontSize: 12,
};

const jogPanelStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 18,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 12,
  width: "fit-content",
  maxWidth: "100%",
  flexWrap: "wrap",
};

const dpadStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(3, 40px)",
  gridTemplateRows: "repeat(3, 40px)",
  gap: 2,
};

const zPadStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
};

const jogButtonStyle: React.CSSProperties = {
  width: 40,
  height: 40,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: theme.color.surface,
  border: `1px solid ${theme.color.borderStrong}`,
  borderRadius: theme.radius.md,
  color: theme.color.text,
  fontWeight: 600,
  cursor: "pointer",
};

const padCenterStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  color: theme.color.textFaint,
  fontSize: 10,
};

const stepFieldsStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 6,
};

const stepFieldStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
};

const stepHintStyle: React.CSSProperties = {
  color: theme.color.danger,
  fontSize: 11,
};
