export type WorkspaceState = {
  activeTab: string | null;
  gantryFile: string | null;
  deckFile: string | null;
  protocolFile: string | null;
  deckImportedFrom: string | null;
};

const PREFIX = "cubos-workspace:";

export const EMPTY_WORKSPACE: WorkspaceState = {
  activeTab: null,
  gantryFile: null,
  deckFile: null,
  protocolFile: null,
  deckImportedFrom: null,
};

function storageKey(configDir: string): string {
  return `${PREFIX}${configDir}`;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

export function loadWorkspaceState(configDir: string, storage: Storage | null = safeStorage()): WorkspaceState {
  if (!storage) return { ...EMPTY_WORKSPACE };
  try {
    const raw = storage.getItem(storageKey(configDir));
    if (!raw) return { ...EMPTY_WORKSPACE };
    const parsed = JSON.parse(raw) as Partial<Record<keyof WorkspaceState, unknown>>;
    return {
      activeTab: asString(parsed.activeTab),
      gantryFile: asString(parsed.gantryFile),
      deckFile: asString(parsed.deckFile),
      protocolFile: asString(parsed.protocolFile),
      deckImportedFrom: asString(parsed.deckImportedFrom),
    };
  } catch {
    return { ...EMPTY_WORKSPACE };
  }
}

export function saveWorkspaceState(configDir: string, state: WorkspaceState, storage: Storage | null = safeStorage()): void {
  if (!storage) return;
  try {
    storage.setItem(storageKey(configDir), JSON.stringify(state));
  } catch {
    // Storage full or unavailable: the workspace just won't survive reload.
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}
