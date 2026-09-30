"""Versioned CubOS fluid/tip/cap state resource request and response models.

These mirror the ``cubos.data`` TypedDict snapshots (``FluidStateSnapshot``,
``TipStateSnapshot``, ``CapStateSnapshot``) rather than inventing a new
shape, so the API stays a thin, faithful projection of CubOS's own public
state surface.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


StateDomain = Literal["fluid", "tip", "cap"]


# ── Seed payloads (shared by run submission and direct state creation) ──


class FluidSeedItem(BaseModel):
    """One ``initial-fluids``-style seed entry for a single container."""

    volume_ul: float
    composition: Optional[Dict[str, float]] = None


class InitialStateSeed(BaseModel):
    """A full ``initial-fluids``-style seed payload for a new fluid state."""

    label: Optional[str] = None
    fluids: Dict[str, FluidSeedItem] = Field(default_factory=dict)


class RunStateSelection(BaseModel):
    """A run's choice of fluid-state linkage: create new, or resume existing.

    Exactly one of ``initial_state``/``fluid_state_id`` must be set. This
    model is only evaluated when a run submission opts into state tracking
    at all — omitting ``RunSubmission.state`` entirely keeps a run
    stateless, matching every run submitted before Feature 07.
    """

    initial_state: Optional[InitialStateSeed] = None
    fluid_state_id: Optional[int] = None

    @model_validator(mode="after")
    def validate_exclusive(self) -> "RunStateSelection":
        has_initial = self.initial_state is not None
        has_existing = self.fluid_state_id is not None
        if has_initial == has_existing:
            raise ValueError(
                "state requires exactly one of initial_state or fluid_state_id"
            )
        return self


# ── Fluid-state creation/listing ─────────────────────────────────────────


class CreateFluidStateRequest(BaseModel):
    """Bounded state creation: a deck reference plus an optional seed."""

    deck_file: str
    label: Optional[str] = None
    fluids: Dict[str, FluidSeedItem] = Field(default_factory=dict)
    tips: Dict[str, bool] = Field(default_factory=dict)

    @field_validator("tips", mode="before")
    @classmethod
    def require_boolean_tip_presence(cls, value):
        if isinstance(value, dict) and any(
            not isinstance(present, bool) for present in value.values()
        ):
            raise ValueError("tip presence values must be true or false")
        return value


class FluidStateSummaryResponse(BaseModel):
    id: int
    label: Optional[str]
    deck_path: str
    deck_fingerprint: str
    created_at: str
    updated_at: str
    container_count: int
    operation_count: int


# ── Container / operation resource shapes ────────────────────────────────


class ContainerView(BaseModel):
    """A fluid container's volume, composition, and (when known) role."""

    labware_key: str
    location_id: str
    labware_type: str
    capacity_ul: float
    working_volume_ul: float
    current_volume_ul: float
    composition: Dict[str, float]
    version: int
    updated_at: str
    role: Optional[str] = None
    solution: Optional[str] = None
    allowed_solutions: Optional[List[str]] = None


class FluidStateDetailResponse(BaseModel):
    id: int
    deck_path: str
    deck_fingerprint: str
    label: Optional[str]
    created_at: str
    updated_at: str
    containers: List[ContainerView]
    pending_operation_count: int
    reconciliation_required_count: int


class TipContainerView(BaseModel):
    rack_key: str
    slot_id: str
    status: str
    tip_length_mm: float
    version: int
    updated_at: str


class TipRefillRequest(BaseModel):
    rack_key: str
    pipette_bare_confirmed: bool
    operator: str
    reason: str
    operation_key: str

    @model_validator(mode="after")
    def validate_refill_confirmation(self) -> "TipRefillRequest":
        if not self.rack_key.strip():
            raise ValueError("rack_key is required")
        if not self.operation_key.strip():
            raise ValueError("operation_key is required")
        if not self.operator.strip():
            raise ValueError("operator is required")
        if not self.reason.strip():
            raise ValueError("reason is required")
        if not self.pipette_bare_confirmed:
            raise ValueError(
                "pipette_bare_confirmed must be true to refill a rack"
            )
        return self


class TipRefillView(BaseModel):
    id: int
    operation_key: str
    rack_key: str
    operator: str
    reason: str
    changed_slots: List[str]
    preserved_slots: List[str]
    created_at: str


class PipetteAttachmentView(BaseModel):
    pipette_key: str
    rack_key: Optional[str] = None
    slot_id: Optional[str] = None
    tip_extension_mm: Optional[float] = None
    contents_known_empty: bool
    attachment_uncertain: bool
    updated_at: str


class TipStateResponse(BaseModel):
    fluid_state_id: int
    containers: List[TipContainerView]
    pipette: PipetteAttachmentView
    refills: List[TipRefillView] = Field(default_factory=list)


class TipRefillResponse(TipRefillView):
    fluid_state_id: int
    status: str = "applied"


class FluidStockReconciliationRequest(BaseModel):
    target: str
    volume_ul: float
    composition: Optional[Dict[str, float]] = None
    operation_key: str
    operator: str
    reason: str

    @model_validator(mode="after")
    def validate_audit_fields(self) -> "FluidStockReconciliationRequest":
        for name in ("target", "operation_key", "operator", "reason"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        return self


class FluidStockReconciliationResponse(BaseModel):
    fluid_state_id: int
    operation_key: str
    target: str
    volume_ul: float
    composition: Dict[str, float]
    operator: str
    reason: str
    status: str = "applied"


class CapContainerView(BaseModel):
    labware_key: str
    location_id: str
    status: str
    version: int
    updated_at: str


class CapStateResponse(BaseModel):
    fluid_state_id: int
    containers: List[CapContainerView]


class OperationView(BaseModel):
    """One journaled fluid/tip/cap operation, tagged with its subsystem."""

    domain: StateDomain
    id: int
    operation_key: str
    operation_type: str
    status: str
    campaign_id: Optional[int] = None
    detail: Optional[str] = None
    created_at: str
    updated_at: str
    applied_at: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)


class OperationsResponse(BaseModel):
    fluid_state_id: int
    operations: List[OperationView]


class ReconciliationResponse(BaseModel):
    fluid_state_id: int
    items: List[OperationView]


class ResolveReconciliationRequest(BaseModel):
    """An auditable operator decision resolving one pending operation.

    ``operator``/``reason`` are recorded into the journal's existing
    ``detail`` free-text field (there is no operator-identity column in
    ``cubos.data`` to persist them separately) — this is the "who/what/why"
    audit trail Feature 07 exposes over HTTP.
    """

    domain: StateDomain
    operation_key: str
    resolution: str
    operator: str
    reason: str
    source_volume_ul: Optional[float] = None
    source_composition: Optional[Dict[str, float]] = None
    destination_volume_ul: Optional[float] = None
    destination_composition: Optional[Dict[str, float]] = None
    final_slot_status: Optional[str] = None
    final_status: Optional[str] = None

    @model_validator(mode="after")
    def validate_identity(self) -> "ResolveReconciliationRequest":
        if not self.operator.strip():
            raise ValueError("operator is required")
        if not self.reason.strip():
            raise ValueError("reason is required")
        return self


class ResolveReconciliationResponse(BaseModel):
    domain: StateDomain
    operation_key: str
    status: str
    detail: Optional[str]
