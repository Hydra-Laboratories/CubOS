import * as theme from "../../theme";

interface Props {
  /** "Gantry" | "Deck" | "Protocol" — used for labels and accessible names. */
  kind: string;
  configs: string[];
  selectedFile: string | null;
  onSelectFile: (f: string) => void;
  onNew?: () => void;
  onDelete?: (f: string) => void;
  /** Optional line rendered under the picker (e.g. where deck edits go). */
  note?: React.ReactNode;
  deleteDisabledReason?: string | null;
}

export default function ConfigFilePicker({
  kind,
  configs,
  selectedFile,
  onSelectFile,
  onNew,
  onDelete,
  note,
  deleteDisabledReason,
}: Props) {
  const lower = kind.toLowerCase();
  const placeholder = configs.length > 0 ? `Open ${lower} config...` : "No configs found";
  const value = selectedFile && configs.includes(selectedFile) ? selectedFile : "";
  const canDelete = !!onDelete && !!value && !deleteDisabledReason;

  return (
    <div style={rowStyle}>
      <label style={wrapperStyle}>
        <span style={labelStyle}>{kind} config</span>
        <select
          aria-label={`${kind} config`}
          value={value}
          onChange={(e) => {
            if (e.target.value) onSelectFile(e.target.value);
          }}
          style={selectStyle}
        >
          <option value="" disabled>{placeholder}</option>
          {configs.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        {note && <span style={noteStyle}>{note}</span>}
      </label>
      {onNew && (
        <button type="button" onClick={onNew} style={actionBtnStyle} title={`Start an empty ${lower} config`}>
          New
        </button>
      )}
      {onDelete && (
        <button
          type="button"
          onClick={() => value && onDelete(value)}
          disabled={!canDelete}
          style={{ ...deleteBtnStyle, opacity: canDelete ? 1 : 0.5, cursor: canDelete ? "pointer" : "not-allowed" }}
          title={deleteDisabledReason ?? (value ? `Delete ${value}` : `Open a ${lower} config to delete it`)}
        >
          Delete
        </button>
      )}
    </div>
  );
}

const rowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "end",
  gap: 8,
  flexWrap: "wrap",
  marginBottom: 12,
};

const wrapperStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,
  minWidth: 0,
  width: "min(100%, 340px)",
  flex: "0 1 340px",
};

const labelStyle: React.CSSProperties = {
  ...theme.sectionLabel,
};

const selectStyle: React.CSSProperties = {
  ...theme.input,
  height: 34,
  padding: "0 10px",
  width: "100%",
};

const noteStyle: React.CSSProperties = {
  fontSize: 11.5,
  color: theme.color.textMuted,
  lineHeight: 1.35,
};

const actionBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  fontSize: 12,
  height: 34,
  padding: "0 12px",
};

const deleteBtnStyle: React.CSSProperties = {
  ...theme.btn.danger,
  fontSize: 12,
  height: 34,
  padding: "0 12px",
};
