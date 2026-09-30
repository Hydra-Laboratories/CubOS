import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { gantryApi } from "../api/client";
import type { GantryConfig, InstrumentSchemas } from "../types";

type SaveGantryArgs = {
  filename: string;
  body: GantryConfig;
};

export function useGantryPosition(enabled = true) {
  return useQuery({
    queryKey: ["gantry", "position"],
    queryFn: gantryApi.getPosition,
    refetchInterval: enabled ? 200 : false,
    enabled,
  });
}

export function useGantryConfigs() {
  return useQuery({ queryKey: ["gantry", "configs"], queryFn: gantryApi.listConfigs });
}

export function useInstrumentTypes() {
  return useQuery({
    queryKey: ["gantry", "instrument-types"],
    queryFn: gantryApi.listInstrumentTypes,
    staleTime: Infinity,
  });
}

export function usePipetteModels() {
  return useQuery({
    queryKey: ["gantry", "pipette-models"],
    queryFn: gantryApi.listPipetteModels,
    staleTime: Infinity,
  });
}

export function useInstrumentSchemas() {
  return useQuery<InstrumentSchemas>({
    queryKey: ["gantry", "instrument-schemas"],
    queryFn: gantryApi.getInstrumentSchemas,
    staleTime: Infinity,
  });
}

export function useInstrumentMethods() {
  return useQuery({
    queryKey: ["gantry", "instrument-methods"],
    queryFn: gantryApi.getInstrumentMethods,
    staleTime: Infinity,
  });
}

export function useInstrumentMethodParams() {
  return useQuery({
    queryKey: ["gantry", "instrument-method-params"],
    queryFn: gantryApi.getInstrumentMethodParams,
    staleTime: Infinity,
  });
}

export function useGantry(filename: string | null) {
  return useQuery({
    queryKey: ["gantry", filename],
    queryFn: () => gantryApi.get(filename!),
    enabled: !!filename,
  });
}

export function useSaveGantry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ filename, body }: SaveGantryArgs) => gantryApi.put(filename, body),
    onSuccess: (data, { filename }) => {
      qc.setQueryData(["gantry", filename], data);
      qc.invalidateQueries({ queryKey: ["gantry", "configs"] });
      qc.invalidateQueries({ queryKey: ["gantry", filename] });
    },
  });
}

export function useDeleteGantry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (filename: string) => gantryApi.delete(filename),
    onSuccess: (_data, filename) => {
      qc.removeQueries({ queryKey: ["gantry", filename] });
      qc.invalidateQueries({ queryKey: ["gantry", "configs"] });
    },
  });
}
