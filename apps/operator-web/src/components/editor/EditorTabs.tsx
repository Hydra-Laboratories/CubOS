import * as theme from "../../theme";

const TABS = ["Gantry", "Deck", "Protocol"] as const;
type TabName = (typeof TABS)[number];

interface Props {
  activeTab: string;
  onTabChange: (tab: string) => void;
  /** Tabs whose editor has unsaved local edits. Renders an amber dot
   * next to the label so the user knows that tab changed but isn't
   * persisted yet. */
  dirtyTabs?: string[];
  disabledTabs?: string[];
  disabledMessage?: string | null;
  /**
   * Filename to show underneath each tab label once a config has been
   * successfully loaded (or saved) on that tab. A missing or empty
   * value leaves the tab with only its section label. The tab bar
   * reserves vertical space for the second line either way so the bar
   * does not jump when a filename appears.
   */
  loadedFilenames?: Partial<Record<TabName, string | null>>;
}

export default function EditorTabs({
  activeTab,
  onTabChange,
  dirtyTabs = [],
  disabledTabs = [],
  disabledMessage,
  loadedFilenames,
}: Props) {
  return (
    <div>
      <div
        style={{
          display: "flex",
          gap: 4,
          borderBottom: `1px solid ${theme.color.border}`,
          marginBottom: disabledMessage && disabledTabs.includes(activeTab) ? 0 : 18,
        }}
      >
        {TABS.map((tab) => {
          const disabled = disabledTabs.includes(tab);
          const dirty = dirtyTabs.includes(tab);
          const active = activeTab === tab;
          const filename = loadedFilenames?.[tab] || null;
          return (
            <button
              key={tab}
              onClick={() => {
                if (disabled && disabledMessage) {
                  onTabChange(tab);
                } else if (!disabled) {
                  onTabChange(tab);
                }
              }}
              style={{
                background: "transparent",
                color: disabled
                  ? theme.color.textFaint
                  : active
                    ? theme.color.ink
                    : theme.color.textMuted,
                border: "none",
                borderBottom: active
                  ? `2px solid ${theme.color.accent}`
                  : "2px solid transparent",
                marginBottom: -1,
                padding: "8px 18px 7px",
                cursor: disabled ? "default" : "pointer",
                fontSize: 13.5,
                fontWeight: active ? 600 : 500,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                lineHeight: 1.25,
                minWidth: 118,
                opacity: disabled ? 0.6 : 1,
              }}
            >
              <span>
                {tab}
                {dirty && (
                  // aria-hidden so the button's accessible name stays the
                  // bare section label ("Deck", ...) — the dot is a
                  // sighted-only unsaved-changes cue.
                  <span
                    aria-hidden="true"
                    title="Unsaved changes"
                    style={{ color: theme.color.warning, marginLeft: 6, fontSize: 10, fontWeight: 700 }}
                  >
                    ●
                  </span>
                )}
              </span>
              <span
                // Hide the filename line from the accessibility tree so
                // the button's accessible name stays exactly the section
                // label ("Gantry", "Deck", ...). Screen readers still get
                // a clean tab name; sighted users still see the filename.
                aria-hidden="true"
                style={{
                  ...theme.mono,
                  fontSize: 10.5,
                  fontWeight: 400,
                  color: theme.color.textFaint,
                  marginTop: 2,
                  maxWidth: 180,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {filename ?? " "}
              </span>
            </button>
          );
        })}
      </div>
      {disabledMessage && disabledTabs.includes(activeTab) && (
        <div
          style={{
            ...theme.notice.warning,
            marginBottom: 18,
            borderTop: "none",
            borderRadius: `0 0 ${theme.radius.md}px ${theme.radius.md}px`,
            padding: "10px 14px",
          }}
        >
          {disabledMessage}
        </div>
      )}
    </div>
  );
}
