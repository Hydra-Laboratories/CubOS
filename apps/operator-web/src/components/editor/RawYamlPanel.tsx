import { useState } from "react";
import YAML from "yaml";
import * as theme from "../../theme";

interface Props {
  /** Current structured value; stringified when the panel opens or refreshes. */
  value: unknown;
  /** Push parsed YAML back into the editor state. Return an error string to reject. */
  onApply: (parsed: unknown) => string | null;
}

/** Collapsible raw-YAML view of the editor state. Apply feeds the parsed
 * document back through the editor's normal state, so the regular Save
 * button (and its server-side validation) still governs persistence. */
export default function RawYamlPanel({ value, onApply }: Props) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    setText(YAML.stringify(value));
    setError(null);
  };

  const toggle = () => {
    if (!open) refresh();
    setOpen(!open);
  };

  const apply = () => {
    let parsed: unknown;
    try {
      parsed = YAML.parse(text);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    setError(onApply(parsed));
  };

  return (
    <div style={{ marginTop: 12 }}>
      <button onClick={toggle} style={{ ...theme.btn.secondary, ...theme.btnSmall }}>
        {open ? "Hide raw YAML" : "Edit raw YAML"}
      </button>
      {open && (
        <div style={{ marginTop: 8 }}>
          <textarea
            value={text}
            onChange={(e) => { setText(e.target.value); setError(null); }}
            spellCheck={false}
            style={textareaStyle}
            aria-label="Raw YAML"
          />
          {error && <div style={{ ...theme.notice.error, marginTop: 6 }}>{error}</div>}
          <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
            <button onClick={apply} style={{ ...theme.btn.primary, ...theme.btnSmall }}>
              Apply to form
            </button>
            <button onClick={refresh} style={{ ...theme.btn.secondary, ...theme.btnSmall }}>
              Reload from form
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

const textareaStyle: React.CSSProperties = {
  ...theme.input,
  ...theme.mono,
  width: "100%",
  minHeight: 260,
  fontSize: 12,
  lineHeight: 1.5,
  resize: "vertical",
  boxSizing: "border-box",
};
