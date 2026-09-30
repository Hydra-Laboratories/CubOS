import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { protocolApi } from "../api/client";
import type { ProtocolConfig, ProtocolSetupValidationRequest } from "../types";

type SaveProtocolArgs = {
  filename: string;
  body: ProtocolConfig;
};

export function useProtocolCommands() {
  return useQuery({
    queryKey: ["protocol", "commands"],
    queryFn: protocolApi.listCommands,
    staleTime: Infinity,
  });
}

export function useProtocolConfigs() {
  return useQuery({
    queryKey: ["protocol", "configs"],
    queryFn: protocolApi.listConfigs,
  });
}

export function useProtocol(filename: string | null) {
  return useQuery({
    queryKey: ["protocol", filename],
    queryFn: () => protocolApi.get(filename!),
    enabled: !!filename,
  });
}

export function useRunStatus() {
  return useQuery({
    queryKey: ["protocol", "run-status"],
    queryFn: protocolApi.runStatus,
    refetchInterval: 2000,
  });
}

export function useSaveProtocol() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ filename, body }: SaveProtocolArgs) => protocolApi.put(filename, body),
    onSuccess: (_data, { filename }) => {
      qc.invalidateQueries({ queryKey: ["protocol", filename] });
      qc.invalidateQueries({ queryKey: ["protocol", "configs"] });
    },
  });
}

export function useValidateProtocol() {
  return useMutation({
    mutationFn: (body: ProtocolConfig) => protocolApi.validate(body),
  });
}

export function useValidateProtocolSetup() {
  return useMutation({
    mutationFn: (body: ProtocolSetupValidationRequest) => protocolApi.validateSetup(body),
  });
}

export function useDeleteProtocol() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (filename: string) => protocolApi.delete(filename),
    onSuccess: (_data, filename) => {
      qc.removeQueries({ queryKey: ["protocol", filename] });
      qc.invalidateQueries({ queryKey: ["protocol", "configs"] });
    },
  });
}
