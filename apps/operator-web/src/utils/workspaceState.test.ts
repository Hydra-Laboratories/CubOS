import { describe, expect, it } from "vitest";
import { EMPTY_WORKSPACE, loadWorkspaceState, saveWorkspaceState } from "./workspaceState";

function memoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k) => map.get(k) ?? null,
    key: (i) => Array.from(map.keys())[i] ?? null,
    removeItem: (k) => {
      map.delete(k);
    },
    setItem: (k, v) => {
      map.set(k, v);
    },
  };
}

describe("workspaceState", () => {
  it("round-trips a workspace per config directory", () => {
    const storage = memoryStorage();
    const state = {
      activeTab: "Protocol",
      gantryFile: "cubos.yaml",
      deckFile: "cub_deck.yaml",
      protocolFile: "move.yaml",
      deckImportedFrom: "deck.yaml",
    };
    saveWorkspaceState("/a", state, storage);
    expect(loadWorkspaceState("/a", storage)).toEqual(state);
    expect(loadWorkspaceState("/b", storage)).toEqual(EMPTY_WORKSPACE);
  });

  it("returns an empty workspace for corrupt or missing storage", () => {
    const storage = memoryStorage();
    storage.setItem("cubos-workspace:/a", "{not json");
    expect(loadWorkspaceState("/a", storage)).toEqual(EMPTY_WORKSPACE);
    expect(loadWorkspaceState("/a", null)).toEqual(EMPTY_WORKSPACE);
  });

  it("drops non-string fields", () => {
    const storage = memoryStorage();
    storage.setItem("cubos-workspace:/a", JSON.stringify({ activeTab: 3, gantryFile: "", deckFile: "d.yaml" }));
    expect(loadWorkspaceState("/a", storage)).toEqual({ ...EMPTY_WORKSPACE, deckFile: "d.yaml" });
  });
});
