import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { fluidStateApi } from "../api/client";
import type { CreateFluidStateRequest, ResolveReconciliationRequest } from "../types";

export function useFluidStates() {
  return useQuery({
    queryKey: ["fluid-states"],
    queryFn: fluidStateApi.list,
  });
}

export function useFluidState(fluidStateId: number | null) {
  return useQuery({
    queryKey: ["fluid-states", fluidStateId],
    queryFn: () => fluidStateApi.get(fluidStateId!),
    enabled: fluidStateId != null,
  });
}

export function useTipState(fluidStateId: number | null) {
  return useQuery({
    queryKey: ["fluid-states", fluidStateId, "tips"],
    queryFn: () => fluidStateApi.getTips(fluidStateId!),
    enabled: fluidStateId != null,
  });
}

export function useCapState(fluidStateId: number | null) {
  return useQuery({
    queryKey: ["fluid-states", fluidStateId, "caps"],
    queryFn: () => fluidStateApi.getCaps(fluidStateId!),
    enabled: fluidStateId != null,
  });
}

export function useOperations(fluidStateId: number | null) {
  return useQuery({
    queryKey: ["fluid-states", fluidStateId, "operations"],
    queryFn: () => fluidStateApi.getOperations(fluidStateId!),
    enabled: fluidStateId != null,
  });
}

// Reconciliation-required items are the operator-facing safety signal —
// poll so a new one raised by a running protocol shows up without a
// manual refresh.
export function useReconciliation(fluidStateId: number | null) {
  return useQuery({
    queryKey: ["fluid-states", fluidStateId, "reconciliation"],
    queryFn: () => fluidStateApi.getReconciliation(fluidStateId!),
    enabled: fluidStateId != null,
    refetchInterval: 3000,
  });
}

export function useCreateFluidState() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateFluidStateRequest) => fluidStateApi.create(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["fluid-states"] });
    },
  });
}

export function useResolveReconciliation(fluidStateId: number | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ResolveReconciliationRequest) =>
      fluidStateApi.resolveReconciliation(fluidStateId!, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["fluid-states", fluidStateId] });
    },
  });
}
