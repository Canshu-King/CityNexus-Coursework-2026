"""Product-facing reasoning-runtime state for the Autonomous Journey Operator."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.db.models import OperatorRecoveryResume


UNCONFIGURED = "UNCONFIGURED"
CONFIGURED_UNKNOWN = "CONFIGURED_UNKNOWN"
AVAILABLE = "AVAILABLE"
TEMPORARILY_FAILED = "TEMPORARILY_FAILED"
RESUME_PENDING = "PENDING"
RESUME_RUNNING = "RUNNING"
RESUME_RESUMED = "RESUMED"
RESUME_UNSAFE = "UNSAFE_TO_FAILOVER"
RESUME_FAILED = "FAILED"
BACKOFF_SECONDS = (60, 120, 300, 600)


@dataclass(frozen=True)
class RuntimeCandidate:
    key: str
    configured: bool
    provider_label: str
    model_label: str | None = None
    status: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_failure_reason: str | None = None
    next_retry_at: str | None = None

    @property
    def availability(self) -> str:
        return self.status or (CONFIGURED_UNKNOWN if self.configured else UNCONFIGURED)


def configured_runtime_candidates(settings: Any) -> list[RuntimeCandidate]:
    """Return supported external reasoning runtimes without probing providers."""

    candidates: list[RuntimeCandidate] = []

    hermes_compat = bool(
        getattr(settings, "hermes_api_url", None)
        and getattr(settings, "hermes_api_key", None)
    )
    candidates.append(
        RuntimeCandidate(
            key="hermes",
            configured=hermes_compat,
            provider_label="Hermes-compatible",
            model_label=getattr(settings, "hermes_model", None),
            status=CONFIGURED_UNKNOWN if hermes_compat else UNCONFIGURED,
        )
    )

    deepseek = bool(getattr(settings, "ai_api_key", None))
    candidates.append(
        RuntimeCandidate(
            key="deepseek",
            configured=deepseek,
            provider_label="DeepSeek",
            model_label=getattr(settings, "ai_model", None),
            status=CONFIGURED_UNKNOWN if deepseek else UNCONFIGURED,
        )
    )

    native_hermes = bool(
        getattr(settings, "hermes_native_api_url", None)
        and getattr(settings, "hermes_api_server_key", None)
    )
    candidates.append(
        RuntimeCandidate(
            key="hermes-native",
            configured=native_hermes,
            provider_label="Hermes native",
            model_label=getattr(settings, "hermes_native_model", None),
            status=CONFIGURED_UNKNOWN if native_hermes else UNCONFIGURED,
        )
    )
    return candidates


def runtime_availability(
    *,
    configured: bool,
    last_success_at: str | None = None,
    last_failure_at: str | None = None,
    next_retry_at: str | None = None,
    now: datetime | None = None,
) -> str:
    """Classify a runtime without mistaking credentials for network health."""

    if not configured:
        return UNCONFIGURED
    stamp = now or datetime.now(timezone.utc)
    if next_retry_at:
        try:
            eligible = datetime.fromisoformat(str(next_retry_at))
            if eligible.tzinfo is None:
                eligible = eligible.replace(tzinfo=timezone.utc)
            if eligible > stamp:
                return TEMPORARILY_FAILED
        except ValueError:
            pass
    if last_success_at:
        if not last_failure_at:
            return AVAILABLE
        try:
            success = datetime.fromisoformat(str(last_success_at))
            failure = datetime.fromisoformat(str(last_failure_at))
            if success >= failure:
                return AVAILABLE
        except ValueError:
            return AVAILABLE
    return CONFIGURED_UNKNOWN


def retry_delay_seconds(retry_count: int) -> int:
    """Return a bounded server-owned delay for the next external attempt."""

    index = max(0, min(int(retry_count), len(BACKOFF_SECONDS) - 1))
    return BACKOFF_SECONDS[index]


def next_retry_at(*, retry_count: int, now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    return (stamp + timedelta(seconds=retry_delay_seconds(retry_count))).isoformat()


def resume_is_due(resume: OperatorRecoveryResume, *, now: datetime | None = None) -> bool:
    if resume.status != RESUME_PENDING or not resume.safe_to_resume:
        return False
    if not resume.next_retry_at:
        return True
    try:
        due = datetime.fromisoformat(str(resume.next_retry_at))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return due <= (now or datetime.now(timezone.utc))
    except ValueError:
        return True


def claim_resume(
    session,
    resume: OperatorRecoveryResume,
    *,
    runtime_key: str,
    now: datetime | None = None,
    lease_seconds: int = 180,
) -> str | None:
    """Claim one Journey+epoch so Hermes/DeepSeek cannot race each other."""

    stamp = now or datetime.now(timezone.utc)
    if not resume_is_due(resume, now=stamp):
        return None
    if resume.owner_claimed_at:
        try:
            claimed = datetime.fromisoformat(str(resume.owner_claimed_at))
            if claimed.tzinfo is None:
                claimed = claimed.replace(tzinfo=timezone.utc)
            if claimed + timedelta(seconds=lease_seconds) > stamp:
                return None
        except ValueError:
            pass
    token = str(uuid4())
    resume.status = RESUME_RUNNING
    resume.owner_runtime_key = runtime_key
    resume.owner_claim_token = token
    resume.owner_claimed_at = stamp.isoformat()
    resume.last_attempt_at = stamp.isoformat()
    resume.updated_at = stamp.isoformat()
    session.flush()
    return token


def release_resume(
    resume: OperatorRecoveryResume,
    *,
    retry: bool,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    stamp = now or datetime.now(timezone.utc)
    resume.owner_runtime_key = None
    resume.owner_claim_token = None
    resume.owner_claimed_at = None
    resume.updated_at = stamp.isoformat()
    if retry:
        resume.status = RESUME_PENDING
        resume.retry_count = int(resume.retry_count or 0) + 1
        resume.next_retry_at = next_retry_at(retry_count=resume.retry_count, now=stamp)
        if reason:
            resume.blocked_reason = reason
    else:
        resume.status = RESUME_RESUMED
        resume.resumed_at = stamp.isoformat()
        resume.next_retry_at = None


def find_recovery_resume(session, *, journey_id: str, recovery_epoch_id: str | None):
    if not recovery_epoch_id:
        return None
    return session.scalar(
        select(OperatorRecoveryResume).where(
            OperatorRecoveryResume.journey_id == str(journey_id),
            OperatorRecoveryResume.recovery_epoch_id == str(recovery_epoch_id),
        )
    )


def upsert_recovery_resume(
    session,
    *,
    journey_id: str,
    recovery_epoch_id: str,
    event_id: str | None,
    incident_fingerprint: str,
    standing_mission_id: str | None,
    recovery_mission_id: str | None,
    guardian_run_id: str | None,
    active_plan_version_id: str | None,
    material_world_epoch: str | None,
    blocked_reason: str,
    failure_classification: str,
    provider_key: str | None,
    model_key: str | None,
    safe_to_resume: bool,
    now: datetime | None = None,
) -> OperatorRecoveryResume:
    """Create one durable blocked recovery or update its latest failure."""

    stamp = now or datetime.now(timezone.utc)
    resume = find_recovery_resume(
        session, journey_id=journey_id, recovery_epoch_id=recovery_epoch_id
    )
    if resume is None:
        resume = OperatorRecoveryResume(
            id=str(uuid4()),
            journey_id=str(journey_id),
            recovery_epoch_id=str(recovery_epoch_id),
            event_id=event_id,
            incident_fingerprint=str(incident_fingerprint),
            standing_mission_id=standing_mission_id,
            recovery_mission_id=recovery_mission_id,
            guardian_run_id=guardian_run_id,
            active_plan_version_id_at_block=active_plan_version_id,
            material_world_epoch_at_block=material_world_epoch,
            blocked_reason=blocked_reason,
            failure_classification=failure_classification,
            provider_key=provider_key,
            model_key=model_key,
            status=RESUME_PENDING,
            next_retry_at=next_retry_at(retry_count=0, now=stamp),
            retry_count=0,
            safe_to_resume=bool(safe_to_resume),
            created_at=stamp.isoformat(),
            updated_at=stamp.isoformat(),
        )
        session.add(resume)
    else:
        resume.blocked_reason = blocked_reason
        resume.failure_classification = failure_classification
        resume.provider_key = provider_key or resume.provider_key
        resume.model_key = model_key or resume.model_key
        resume.safe_to_resume = bool(safe_to_resume)
        resume.event_id = event_id or resume.event_id
        resume.guardian_run_id = guardian_run_id or resume.guardian_run_id
        resume.recovery_mission_id = recovery_mission_id or resume.recovery_mission_id
        resume.updated_at = stamp.isoformat()
        if resume.status in {RESUME_RESUMED, RESUME_UNSAFE, RESUME_FAILED} and safe_to_resume:
            resume.status = RESUME_PENDING
            resume.next_retry_at = next_retry_at(retry_count=resume.retry_count, now=stamp)
    session.flush()
    return resume


def resume_projection(resume: OperatorRecoveryResume | None) -> dict[str, Any]:
    if resume is None:
        return {
            "pending": False,
            "safe_to_resume": False,
            "status": None,
        }
    return {
        "pending": resume.status in {RESUME_PENDING, RESUME_RUNNING},
        "status": resume.status,
        "recovery_resume_id": resume.id,
        "recovery_epoch_id": resume.recovery_epoch_id,
        "incident_id": resume.event_id,
        "incident_fingerprint": resume.incident_fingerprint,
        "safe_to_resume": bool(resume.safe_to_resume),
        # The user-facing projection carries a stable classification only;
        # provider exception text stays in the durable advanced record.
        "blocked_reason": resume.failure_classification,
        "failure_classification": resume.failure_classification,
        "retry_count": int(resume.retry_count or 0),
        "next_retry_at": resume.next_retry_at,
        "owner_runtime": resume.owner_runtime_key,
        "last_attempt_at": resume.last_attempt_at,
        "resumed_at": resume.resumed_at,
    }


def runtime_candidates_for_session(session, settings: Any) -> list[RuntimeCandidate]:
    """Project candidate health from durable attempts without provider probes."""

    from app.db.models import AgentRun

    candidates = configured_runtime_candidates(settings)
    rows = session.scalars(
        select(AgentRun).order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
    ).all()
    latest: dict[str, Any] = {}
    for row in rows:
        latest.setdefault(str(row.provider_key), row)
    resumes = session.scalars(
        select(OperatorRecoveryResume)
        .where(OperatorRecoveryResume.status.in_((RESUME_PENDING, RESUME_RUNNING)))
        .order_by(OperatorRecoveryResume.updated_at.desc(), OperatorRecoveryResume.id.desc())
    ).all()
    latest_resume: dict[str, OperatorRecoveryResume] = {}
    for row in resumes:
        if row.provider_key:
            latest_resume.setdefault(str(row.provider_key), row)
    projected: list[RuntimeCandidate] = []
    for candidate in candidates:
        run = latest.get(candidate.key)
        resume = latest_resume.get(candidate.key)
        last_success = None
        last_attempt = None
        failure = None
        retry_at = None
        status = candidate.availability
        if run is not None:
            last_attempt = str(run.completed_at or run.started_at or run.created_at)
            if run.status == "COMPLETED" and not run.failure_reason:
                status = AVAILABLE
                last_success = last_attempt
            elif run.status in {"FAILED_EXTERNAL", "PARTIAL"}:
                failure = str(run.failure_reason or "MODEL_EXTERNAL_ERROR")
                status = TEMPORARILY_FAILED
        if resume is not None:
            retry_at = resume.next_retry_at
            failure = failure or resume.failure_classification
            if resume.next_retry_at:
                status = runtime_availability(
                    configured=candidate.configured,
                    last_success_at=last_success,
                    last_failure_at=last_attempt,
                    next_retry_at=resume.next_retry_at,
                )
                if status == CONFIGURED_UNKNOWN:
                    status = TEMPORARILY_FAILED
        projected.append(
            RuntimeCandidate(
                key=candidate.key,
                configured=candidate.configured,
                provider_label=candidate.provider_label,
                model_label=candidate.model_label,
                status=status,
                last_attempt_at=last_attempt,
                last_success_at=last_success,
                last_failure_reason=failure,
                next_retry_at=retry_at,
            )
        )
    return projected


def safe_external_handoff(*, tool_step_count: int, approval_created: bool) -> bool:
    """Cross-provider handoff is safe only before the run influenced domain action."""

    return int(tool_step_count) == 0 and not approval_created


def operator_brain_state(
    *,
    configured_candidates: list[RuntimeCandidate],
    selected_runtime: str | None = None,
    active_agent_status: str | None = None,
    blocked_recovery_pending: bool = False,
    last_failure_reason: str | None = None,
    safe_to_resume: bool = False,
) -> dict[str, Any]:
    configured = [item for item in configured_candidates if item.configured]

    if active_agent_status in {"RUNNING", "PLANNING"}:
        state = "ACTIVE"
        headline = "I’m working out the best recovery."
    elif (
        blocked_recovery_pending
        and configured
        and safe_to_resume
        and (
            any(item.availability == AVAILABLE for item in configured_candidates)
            # Keep the pure contract helper backwards-compatible for callers
            # that omit durable availability and explicitly mean "returned".
            or all(item.status is None for item in configured_candidates)
        )
    ):
        state = "RECOVERING"
        headline = "Planning is available again. I can continue the recovery."
    elif not configured:
        state = "UNCONFIGURED"
        headline = "I’m watching your trip, but no planning AI is connected."
    elif blocked_recovery_pending:
        state = "TEMPORARILY_UNAVAILABLE"
        headline = (
            "I noticed a problem. My planning AI is unavailable, so I’m holding "
            "the trip state and will retry safely."
        )
    elif last_failure_reason:
        state = "TEMPORARILY_UNAVAILABLE"
        headline = "My planning AI is temporarily unavailable."
    else:
        state = "READY"
        headline = "Planning AI is ready."

    return {
        "state": state,
        "headline": headline,
        "configured_runtimes": [item.key for item in configured],
        "preferred_runtime": selected_runtime or (configured[0].key if configured else None),
        "runtime_candidates": [
            {
                "key": item.key,
                "provider": item.provider_label,
                "model": item.model_label,
                "status": item.availability,
                "last_attempt_at": item.last_attempt_at,
                "last_success_at": item.last_success_at,
                "last_failure_reason": item.last_failure_reason,
                "next_retry_at": item.next_retry_at,
            }
            for item in configured_candidates
        ],
        "last_failure_reason": last_failure_reason,
        "recovery_resume": {
            "pending": bool(blocked_recovery_pending),
            "safe_to_resume": bool(safe_to_resume),
        },
    }
