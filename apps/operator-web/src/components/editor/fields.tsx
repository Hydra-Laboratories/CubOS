import { useState } from "react";
import * as theme from "../../theme";

function tryParse(s: string): number | null {
  if (s === "" || s === "-" || s === "." || s === "-.") return null;
  const n = parseFloat(s);
  return isNaN(n) ? null : n;
}

function formatNumberInput(value: number): string {
  return Number.isFinite(value) ? String(value) : "";
}

/** Amber "*" rendered next to a field label when the field's current
 * value differs from the last-saved value. Signals "unsaved local
 * edit" — distinct from the red "*" used for required-field markers.
 * Exported so inline <label> blocks (the ones that use a raw <select>
 * rather than NumberField/TextField) can reuse it without duplicating
 * the color/title/margin style inline. */
export function DirtyMarker() {
  return (
    <span
      style={{ color: theme.color.warning, fontWeight: 700, marginLeft: 2 }}
      title="Unsaved local edit"
    >
      *
    </span>
  );
}

/** Amber "unsaved changes" banner shown inside an editor when that
 * editor's config has local edits not yet written to disk. Each tab
 * owns the prompt for its own config (that's where Save lives), so the
 * user is told to save where the change happened. */
export function UnsavedNotice({ children }: { children: React.ReactNode }) {
  return (
    <div role="alert" style={unsavedNoticeStyle}>
      {children}
    </div>
  );
}

const unsavedNoticeStyle: React.CSSProperties = {
  ...theme.notice.warning,
  marginBottom: 12,
};

interface NumberFieldProps {
  id?: string;
  name?: string;
  label: string;
  value: number | null | undefined;
  onChange: (v: number) => void;
  step?: number;
  required?: boolean;
  disabled?: boolean;
  /** If set, renders an amber "*" next to the label when true. */
  dirty?: boolean;
}

export function NumberField({ id, name, label, value, onChange, required, disabled, dirty }: NumberFieldProps) {
  const normalizedValue = typeof value === "number" && Number.isFinite(value) ? value : NaN;
  const [state, setState] = useState({ raw: formatNumberInput(normalizedValue), value: normalizedValue });
  if (!Object.is(normalizedValue, state.value)) {
    setState({ raw: formatNumberInput(normalizedValue), value: normalizedValue });
  }
  const raw = state.raw;
  const setRaw = (next: string) => setState({ raw: next, value: normalizedValue });

  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
      <span style={theme.fieldLabel}>
        {label}
        {required && <span style={{ color: theme.color.danger }}> *</span>}
        {dirty && <DirtyMarker />}
      </span>
      <input
        id={id}
        name={name}
        type="text"
        inputMode="decimal"
        value={raw}
        disabled={disabled}
        onChange={(e) => {
          setRaw(e.target.value);
          const n = tryParse(e.target.value);
          if (n !== null) onChange(n);
        }}
        onBlur={() => setRaw(formatNumberInput(normalizedValue))}
        style={inputStyle}
      />
    </label>
  );
}

interface OptionalNumberFieldProps {
  id?: string;
  name?: string;
  label: string;
  /** `null`/`undefined` means unset — distinct from `NumberField`, which
   * always has a concrete numeric value. */
  value: number | null | undefined;
  /** Called with `null` when the field is cleared, so the caller can omit
   * the argument entirely instead of saving an empty string or a stale
   * number. */
  onChange: (v: number | null) => void;
  disabled?: boolean;
  /** If set, renders an amber "*" next to the label when true. */
  dirty?: boolean;
}

/** Like `NumberField`, but for optional numeric args: clearing the input
 * reports `null` (omit the argument) instead of being blocked or silently
 * reverting to the last valid number. */
export function OptionalNumberField({ id, name, label, value, onChange, disabled, dirty }: OptionalNumberFieldProps) {
  const normalizedValue = typeof value === "number" && Number.isFinite(value) ? value : null;
  const [state, setState] = useState({
    raw: normalizedValue === null ? "" : formatNumberInput(normalizedValue),
    value: normalizedValue,
  });
  if (!Object.is(normalizedValue, state.value)) {
    setState({ raw: normalizedValue === null ? "" : formatNumberInput(normalizedValue), value: normalizedValue });
  }
  const raw = state.raw;
  const setRaw = (next: string) => setState({ raw: next, value: normalizedValue });

  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
      <span style={theme.fieldLabel}>
        {label}
        {dirty && <DirtyMarker />}
      </span>
      <input
        id={id}
        name={name}
        type="text"
        inputMode="decimal"
        value={raw}
        disabled={disabled}
        onChange={(e) => {
          setRaw(e.target.value);
          if (e.target.value.trim() === "") {
            onChange(null);
            return;
          }
          const n = tryParse(e.target.value);
          if (n !== null) onChange(n);
        }}
        onBlur={() => setRaw(normalizedValue === null ? "" : formatNumberInput(normalizedValue))}
        style={inputStyle}
      />
    </label>
  );
}


interface TextFieldProps {
  id?: string;
  name?: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  required?: boolean;
  /** If set, renders an amber "*" next to the label when true. */
  dirty?: boolean;
}

export function TextField({ id, name, label, value, onChange, required, dirty }: TextFieldProps) {
  const hasError = required && !value.trim();
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
      <span style={theme.fieldLabel}>
        {label}
        {required && <span style={{ color: theme.color.danger }}> *</span>}
        {dirty && <DirtyMarker />}
      </span>
      <input
        id={id}
        name={name}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={hasError ? { ...inputStyle, borderColor: theme.color.danger } : inputStyle}
      />
    </label>
  );
}

interface CoordinateFieldProps {
  id?: string;
  name?: string;
  label: string;
  value: { x: number; y: number; z: number };
  onChange: (v: { x: number; y: number; z: number }) => void;
  required?: boolean;
}

export function CoordinateField({ id, name, label, value, onChange, required }: CoordinateFieldProps) {
  const [state, setState] = useState({
    rx: String(value.x),
    ry: String(value.y),
    rz: String(value.z),
    x: value.x,
    y: value.y,
    z: value.z,
  });
  if (value.x !== state.x || value.y !== state.y || value.z !== state.z) {
    setState({
      rx: String(value.x),
      ry: String(value.y),
      rz: String(value.z),
      x: value.x,
      y: value.y,
      z: value.z,
    });
  }
  const { rx, ry, rz } = state;
  const setRx = (next: string) => setState({ ...state, rx: next });
  const setRy = (next: string) => setState({ ...state, ry: next });
  const setRz = (next: string) => setState({ ...state, rz: next });

  return (
    <div style={{ fontSize: 12 }}>
      <span style={theme.fieldLabel}>{label}{required && <span style={{ color: theme.color.danger }}> *</span>}</span>
      <div style={coordinateGridStyle}>
        <label style={axisFieldStyle}>
          <span style={axisLabelStyle}>X</span>
          <input
            id={id ? `${id}-x` : undefined}
            name={name ? `${name}_x` : undefined}
            aria-label={`${label} X`}
            type="text"
            inputMode="decimal"
            value={rx}
            onChange={(e) => { setRx(e.target.value); const n = tryParse(e.target.value); if (n !== null) onChange({ ...value, x: n }); }}
            onBlur={() => setRx(String(value.x))}
            style={{ ...inputStyle, width: "100%" }}
          />
        </label>
        <label style={axisFieldStyle}>
          <span style={axisLabelStyle}>Y</span>
          <input
            id={id ? `${id}-y` : undefined}
            name={name ? `${name}_y` : undefined}
            aria-label={`${label} Y`}
            type="text"
            inputMode="decimal"
            value={ry}
            onChange={(e) => { setRy(e.target.value); const n = tryParse(e.target.value); if (n !== null) onChange({ ...value, y: n }); }}
            onBlur={() => setRy(String(value.y))}
            style={{ ...inputStyle, width: "100%" }}
          />
        </label>
        <label style={axisFieldStyle}>
          <span style={axisLabelStyle}>Z</span>
          <input
            id={id ? `${id}-z` : undefined}
            name={name ? `${name}_z` : undefined}
            aria-label={`${label} Z`}
            type="text"
            inputMode="decimal"
            value={rz}
            onChange={(e) => { setRz(e.target.value); const n = tryParse(e.target.value); if (n !== null) onChange({ ...value, z: n }); }}
            onBlur={() => setRz(String(value.z))}
            style={{ ...inputStyle, width: "100%" }}
          />
        </label>
      </div>
    </div>
  );
}

interface Coordinate2DFieldProps {
  id?: string;
  name?: string;
  label: string;
  value: { x: number; y: number };
  onChange: (v: { x: number; y: number }) => void;
  required?: boolean;
}

/** XY-only variant of CoordinateField for calibration points whose Z is
 * defined elsewhere (e.g. a tip rack's pickup_z). */
export function Coordinate2DField({ id, name, label, value, onChange, required }: Coordinate2DFieldProps) {
  const [state, setState] = useState({
    rx: String(value.x),
    ry: String(value.y),
    x: value.x,
    y: value.y,
  });
  if (value.x !== state.x || value.y !== state.y) {
    setState({ rx: String(value.x), ry: String(value.y), x: value.x, y: value.y });
  }
  const { rx, ry } = state;
  const setRx = (next: string) => setState({ ...state, rx: next });
  const setRy = (next: string) => setState({ ...state, ry: next });

  return (
    <div style={{ fontSize: 12 }}>
      <span style={theme.fieldLabel}>{label}{required && <span style={{ color: theme.color.danger }}> *</span>}</span>
      <div style={{ ...coordinateGridStyle, gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
        <label style={axisFieldStyle}>
          <span style={axisLabelStyle}>X</span>
          <input
            id={id ? `${id}-x` : undefined}
            name={name ? `${name}_x` : undefined}
            aria-label={`${label} X`}
            type="text"
            inputMode="decimal"
            value={rx}
            onChange={(e) => { setRx(e.target.value); const n = tryParse(e.target.value); if (n !== null) onChange({ ...value, x: n }); }}
            onBlur={() => setRx(String(value.x))}
            style={{ ...inputStyle, width: "100%" }}
          />
        </label>
        <label style={axisFieldStyle}>
          <span style={axisLabelStyle}>Y</span>
          <input
            id={id ? `${id}-y` : undefined}
            name={name ? `${name}_y` : undefined}
            aria-label={`${label} Y`}
            type="text"
            inputMode="decimal"
            value={ry}
            onChange={(e) => { setRy(e.target.value); const n = tryParse(e.target.value); if (n !== null) onChange({ ...value, y: n }); }}
            onBlur={() => setRy(String(value.y))}
            style={{ ...inputStyle, width: "100%" }}
          />
        </label>
      </div>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  ...theme.input,
};

const coordinateGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
  gap: 6,
  marginTop: 3,
};

const axisFieldStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "18px minmax(0, 1fr)",
  alignItems: "center",
  gap: 4,
  minWidth: 0,
};

const axisLabelStyle: React.CSSProperties = {
  color: theme.color.textSecondary,
  fontSize: 11,
  fontWeight: 700,
  textAlign: "center",
};

export function SelectField({
  id,
  name,
  label,
  value,
  options,
  onChange,
}: {
  id?: string;
  name?: string;
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
      <span style={theme.fieldLabel}>{label}</span>
      <select id={id} name={name} value={value} onChange={(e) => onChange(e.target.value)} style={inputStyle}>
        {options.length === 0 && <option value="">No configs found</option>}
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

export function SaveButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        ...theme.btn.primary,
        padding: "7px 22px",
        cursor: disabled ? "not-allowed" : "pointer",
        marginTop: 12,
      }}
    >
      Save
    </button>
  );
}

import { formatSavedAt } from "./saveHelpers";

/** "Saved <file> at <time>" acknowledgement shown after a successful save. */
export function SavedStatus({ filename, at }: { filename: string; at: Date }) {
  return (
    <span role="status" style={savedStatusStyle}>
      ✓ Saved <span style={theme.mono}>{filename}</span> at {formatSavedAt(at)}
    </span>
  );
}

const savedStatusStyle: React.CSSProperties = {
  fontSize: 12,
  color: theme.color.successText,
  whiteSpace: "nowrap",
};

/** One-line hint under the filename box saying where Save will write. */
export function SaveTargetHint({ saveAs, selectedFile, exists }: { saveAs: string; selectedFile: string | null; exists: boolean }) {
  if (saveAs) {
    return (
      <span style={saveHintStyle}>
        Save writes <span style={theme.mono}>{saveAs}</span>
        {exists && saveAs !== selectedFile ? " — that file already exists and will be overwritten." : " as a new file."}
      </span>
    );
  }
  if (selectedFile) {
    return (
      <span style={saveHintStyle}>
        Save overwrites <span style={theme.mono}>{selectedFile}</span>. Type a name to save a copy instead.
      </span>
    );
  }
  return <span style={saveHintStyle}>Type a filename to save this config.</span>;
}

const saveHintStyle: React.CSSProperties = {
  fontSize: 11.5,
  color: theme.color.textMuted,
  lineHeight: 1.35,
};
