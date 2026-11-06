from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ImpactType = Literal["CLOSURE", "ACCESS_RISK", "DELAY", "WEATHER_RISK", "SERVICE_RISK"]
ImpactMaturity = Literal[
    "AI_ONLY",
    "STRUCTURALLY_ELIGIBLE",
    "CORROBORATED",
    "VALIDATED_IMPACT",
]


class ImpactCandidate(BaseModel):
    """Strict, non-authoritative model output accepted from an AI provider."""

    model_config = ConfigDict(extra="forbid")

    activity_id: str = Field(min_length=1, max_length=128)
    impact_type: ImpactType
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list, max_length=12)
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    human_explanation: str = Field(default="", max_length=600)


class ImpactProviderResponse(BaseModel):
    """The only provider response shape allowed past the provider boundary."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[ImpactCandidate] = Field(default_factory=list, max_length=20)
    uncertain: bool = False
    semantic_reason: str = Field(default="", max_length=600)


@dataclass(frozen=True)
class ImpactActivityContext:
    activity_id: str
    sequence_no: int
    title: str
    activity_type: str
    place_id: str | None
    place_name: str | None
    place_category: str | None
    latitude: float | None
    longitude: float | None
    start_at: datetime
    end_at: datetime
    status: str
    locked: bool
    is_current: bool
    is_c4: bool
    city_service_id: str | None = None
    place_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatedImpactCandidate:
    activity_id: str
    impact_type: str
    confidence: float
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    human_explanation: str
    actionable: bool
    corroboration_stage: ImpactMaturity
    corroboration_refs: tuple[str, ...]
    independent_evidence_count: int
    minimum_evidence_required: int
    lifecycle_path: tuple[str, ...]


@dataclass(frozen=True)
class ImpactValidationResult:
    candidates: tuple[ValidatedImpactCandidate, ...]
    selected_activity_id: str | None
    rejected_activity_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    status: str
    selected_stage: ImpactMaturity | None = None
    selected_corroboration_refs: tuple[str, ...] = ()
    selected_independent_evidence_count: int = 0


@dataclass(frozen=True)
class ImpactResolution:
    event_id: str
    journey_id: str
    provider_key: str
    model_key: str
    status: str
    selected_activity_id: str | None
    validation: ImpactValidationResult
    uncertain: bool
    semantic_reason: str
    failure_code: str | None
    response_hash: str | None
    maturity_stage: ImpactMaturity | None
    selected_corroboration_refs: tuple[str, ...]
    independent_evidence_count: int
    context: dict[str, Any]
    primary_recovery_target_id: str | None = None
    secondary_risk_activity_ids: tuple[str, ...] = ()
