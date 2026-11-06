from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CommitmentFacts:
    provider_confirmed: bool = False
    payment_succeeded: bool = False
    fixed_schedule: bool = False
    user_locked: bool = False
    cancellation_loss_minor: int = 0
    easily_replaceable: bool = True


@dataclass(frozen=True)
class StrengthDecision:
    rank: int
    reasons: tuple[str, ...]


def derive_strength_rank(facts: CommitmentFacts) -> StrengthDecision:
    """Deterministic MVP policy over source ranks C0..C4.

    The rank is deliberately derived from multiple factors. In particular:
    - payment alone never anchors;
    - a fixed/unpaid provider-confirmed appointment may outrank a paid but
      low-impact/easily-replaceable item (BR-011 intent);
    - C4 requires provider confirmation + captured payment + fixed/user anchor
      + poor replaceability for the canonical demo.

    Source enum labels are injected separately and are never guessed here.
    """
    reasons: list[str] = []

    if not facts.provider_confirmed and not facts.user_locked:
        return StrengthDecision(0, ("no confirmed or explicit commitment source",))

    rank = 1
    if facts.provider_confirmed:
        reasons.append("provider-confirmed reservation")
        rank = max(rank, 2)

    if facts.cancellation_loss_minor > 0:
        reasons.append("cancellation/rebooking loss")
        rank = max(rank, 2)

    if facts.fixed_schedule:
        reasons.append("fixed schedule")
        rank = max(rank, 3)

    if facts.user_locked:
        reasons.append("traveler marked must-go/locked")
        rank = max(rank, 3)

    if facts.payment_succeeded:
        reasons.append("captured payment")
        # A low-impact, fully replaceable payment is not automatically stronger
        # than a fixed appointment. Material/nonreplaceable payment may reach C3.
        if facts.cancellation_loss_minor > 0 or not facts.easily_replaceable:
            rank = max(rank, 3)
        else:
            rank = max(rank, 2)

    anchored = (
        facts.provider_confirmed
        and facts.payment_succeeded
        and (facts.fixed_schedule or facts.user_locked)
        and not facts.easily_replaceable
    )
    if anchored:
        reasons.append("anchored multi-factor commitment")
        rank = 4

    return StrengthDecision(rank, tuple(reasons))


def source_strength_code(rank: int, source_codes: Sequence[str]) -> str:
    if len(source_codes) != 5:
        raise ValueError("Exactly five source commitment strength codes are required")
    if len(set(source_codes)) != 5:
        raise ValueError("Commitment strength codes must be unique")
    if not 0 <= rank <= 4:
        raise ValueError("Commitment strength rank must be in 0..4")
    return source_codes[rank]
