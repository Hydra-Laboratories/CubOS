import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { deckApi } from "../api/client";
import type { DeckConfig } from "../types";

type SaveDeckArgs = {
  filename: string;
  body: DeckConfig;
};

export function useDeckConfigs() {
  return useQuery({ queryKey: ["deck", "configs"], queryFn: deckApi.listConfigs });
}

export function useDeck(filename: string | null) {
  return useQuery({
    queryKey: ["deck", filename],
    queryFn: () => deckApi.get(filename!),
    enabled: !!filename,
  });
}

export function useSaveDeck() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ filename, body }: SaveDeckArgs) => deckApi.put(filename, body),
    onSuccess: (data, { filename }) => {
      qc.setQueryData(["deck", filename], data);
      qc.invalidateQueries({ queryKey: ["deck", "configs"] });
      qc.invalidateQueries({ queryKey: ["deck", filename] });
    },
  });
}

export function useDeleteDeck() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (filename: string) => deckApi.delete(filename),
    onSuccess: (_data, filename) => {
      qc.removeQueries({ queryKey: ["deck", filename] });
      qc.invalidateQueries({ queryKey: ["deck", "configs"] });
    },
  });
}
