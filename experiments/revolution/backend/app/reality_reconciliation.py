"""Domain-scoped reality reconciliation for conflicting Journey facts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable


@dataclass(frozen=True)
class RealityClaimView:
    claim_id: str
    fact_domain: str
    value: Any
    provenance: str
    observed_at: str | None
    exact_binding: bool = True


@dataclass(frozen=True)
class ReconciliationDecision:
    state: str
    winning_claim_id: str | None
    contested: bool
    reason: str


def reservation_time_claim_fingerprint(
    *, journey_id: str, reservation_id: str, value: Any, provenance: str, source_ref: str
) -> str:
    """Stable identity for a bound reservation-time claim, independent of processing runs."""
    body = {
        "journey_id": str(journey_id), "reservation_id": str(reservation_id),
        "value": value, "provenance": str(provenance).upper(), "source_ref": str(source_ref),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reservation_time_authority_class(provenance: str) -> str:
    """Explain authority within RESERVATION_TIME without exposing a global rank."""
    source = str(provenance or "").upper()
    if source in {"LIVE_PROVIDER", "LIVE_PROVIDER_CAPTURE"}:
        return "EXACT_LIVE_PROVIDER"
    if source in {"USER_PASTED_TEXT", "USER_REPORTED", "USER_UPLOADED_IMAGE"}:
        return "EXPLICIT_USER_EVIDENCE"
    if source == "USER_CONFIRMED_REALITY_RESOLUTION":
        return "TRAVELLER_SELECTED_INTERNAL_TRUTH"
    if source in {"SEALED_REAL_REPLAY", "CAPTURED_REPLAY"}:
        return "CAPTURED_PROVIDER_REPLAY"
    if source == "ESTIMATED_AGENT":
        return "NON_AUTHORITATIVE_ESTIMATE"
    return "UNCLASSIFIED_RESERVATION_TIME_SOURCE"


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _reservation_time_authority(provenance: str) -> int:
    """Domain-specific authority, not a global provenance ranking."""
    key = str(provenance or "").upper()
    return {
        "LIVE_PROVIDER": 100,
        "LIVE_PROVIDER_CAPTURE": 95,
        "SYSTEM_DERIVED_PROVIDER_FACT": 90,
        "USER_CONFIRMED_REALITY_RESOLUTION": 88,
        "USER_PASTED_TEXT": 75,
        "USER_UPLOADED_IMAGE": 70,
        "USER_REPORTED": 70,
        "SEALED_REAL_REPLAY": 55,
        "CAPTURED_REPLAY": 50,
        "LAB_FIXTURE": 20,
        "ESTIMATED_AGENT": 0,
    }.get(key, 10)


def reconcile_reservation_time_claims(
    claims: Iterable[RealityClaimView],
    *,
    material_delta_minutes: int = 15,
    now: datetime | None = None,
) -> ReconciliationDecision:
    """Resolve reservation-time claims or surface a material contest.

    A newer user update may supersede an older captured snapshot. A fresh
    live provider claim can supersede lower-authority evidence. Two current,
    materially different claims of comparable practical authority remain
    contested rather than becoming last-write-wins.
    """

    rows = [item for item in claims if item.exact_binding]
    if not rows:
        return ReconciliationDecision(
            state="NO_CONFLICT",
            winning_claim_id=None,
            contested=False,
            reason="NO_EXACT_CLAIMS",
        )

    rows = [item for item in rows if _reservation_time_authority(item.provenance) > 0]
    if not rows:
        return ReconciliationDecision(
            state="NO_CONFLICT",
            winning_claim_id=None,
            contested=False,
            reason="NO_AUTHORITATIVE_CLAIM",
        )

    # Stable newest-first tie breaking only after domain authority.
    rows.sort(
        key=lambda item: (
            _reservation_time_authority(item.provenance),
            _stamp(item.observed_at) or datetime.min.replace(tzinfo=timezone.utc),
            item.claim_id,
        ),
        reverse=True,
    )
    winner = rows[0]
    if len(rows) == 1:
        return ReconciliationDecision(
            state="OLDER_CLAIM_SUPERSEDED",
            winning_claim_id=winner.claim_id,
            contested=False,
            reason="SINGLE_AUTHORITATIVE_CLAIM",
        )

    top_authority = _reservation_time_authority(winner.provenance)
    competing = [
        item for item in rows[1:]
        if item.value != winner.value
    ]
    if not competing:
        return ReconciliationDecision(
            state="NO_CONFLICT",
            winning_claim_id=winner.claim_id,
            contested=False,
            reason="CLAIMS_AGREE",
        )

    runner = competing[0]
    runner_authority = _reservation_time_authority(runner.provenance)
    winner_time = _stamp(winner.value) if isinstance(winner.value, str) else None
    runner_time = _stamp(runner.value) if isinstance(runner.value, str) else None
    delta = (
        abs(int((winner_time - runner_time).total_seconds() // 60))
        if winner_time is not None and runner_time is not None
        else material_delta_minutes
    )

    winner_observed = _stamp(winner.observed_at)
    runner_observed = _stamp(runner.observed_at)

    if (
        top_authority >= 95
        and top_authority > runner_authority
        and winner_observed is not None
        and runner_observed is not None
        and winner_observed > runner_observed
    ):
        return ReconciliationDecision(
            state="NEWER_HIGHER_AUTHORITY_WINS",
            winning_claim_id=winner.claim_id,
            contested=False,
            reason="FRESH_PROVIDER_AUTHORITY",
        )

    if (
        str(winner.provenance).upper() in {"USER_PASTED_TEXT", "USER_REPORTED", "USER_UPLOADED_IMAGE"}
        and runner_authority < top_authority
        and runner_authority < 95
        and winner_observed is not None
        and runner_observed is not None
        and winner_observed > runner_observed
    ):
        return ReconciliationDecision(
            state="TEMPORARY_USER_OVERRIDE",
            winning_claim_id=winner.claim_id,
            contested=False,
            reason="NEWER_USER_UPDATE_OVER_STALE_REPLAY",
        )

    if delta < material_delta_minutes:
        return ReconciliationDecision(
            state="NONMATERIAL_CONFLICT",
            winning_claim_id=winner.claim_id,
            contested=False,
            reason="DIFFERENCE_BELOW_MATERIAL_THRESHOLD",
        )

    return ReconciliationDecision(
        state="UNRESOLVED_MATERIAL_CONFLICT",
        winning_claim_id=None,
        contested=True,
        reason="CURRENT_AUTHORITATIVE_CLAIMS_DISAGREE",
    )


def dependent_action_allowed(*, contested: bool, depends_on_fact: bool) -> bool:
    """Freeze only actions whose safety depends on the contested fact."""
    return not (contested and depends_on_fact)
