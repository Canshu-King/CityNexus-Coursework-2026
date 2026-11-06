"""One place that says why the AI is not answering, in three different words.

`app.main.health` used to fold three unrelated facts into the single string `disabled`: the operator
turned the feature off, nobody installed a credential, and the configured provider actually
refused a call.  A traveller shown one word for three causes gets whichever sentence the screen
had lying around, and the screen's spare sentence was "网络或后端没有响应" - a network excuse for a
server-side setting, and the same class of scapegoat ORD-0355 removed for lodging budgets.

The three names are produced here and nowhere else.  Precedence is fixed and stated because the
three facts can be true at once, and a status endpoint that picked by accident would let two
doors disagree:

1. `PROVIDER_REFUSED` - the provider was reached and refused.  Only a caller that watched the
   refusal can pass this; nothing here infers it from configuration, because "a key exists" says
   nothing about whether the provider answered.
2. `DISABLED_BY_OPS` - `CITYNEXUS_AI_ENABLED` is off.  This describes the operator's switch.  It
   is deliberately not a claim that no call was attempted: the journey path today reaches the
   provider on the strength of a key alone (`agent/runtime.py` and the `main.py` door both test
   the credential, not the flag), so naming the switch is a disclosure about the setting, not a
   cause of a failed run.
3. `NOT_CONFIGURED` - the operator left the feature on but no credential is present for either
   the Responses endpoint or the Hermes gateway.
4. `READY` - none of the above.  A fourth name exists so that "we know it is fine" is also a
   stated fact rather than the absence of a word.

Nothing in this module reads, returns or logs a credential value: it reports whether one exists.
"""

from __future__ import annotations

from app.core.config import settings

# r2 called this READY. Measured on a private backend at the tip 4ef864cfa49b805e3d41b7f079f5fb4184ccec3d,
# a deliberately fake key string produced `ai=enabled, ai_state=READY` - the word promised "it works" on
# the strength of "a string is present". The name now says exactly what is known and nothing more.
AI_CONFIGURED_UNVERIFIED = "CONFIGURED_UNVERIFIED"
AI_NOT_CONFIGURED = "NOT_CONFIGURED"
AI_DISABLED_BY_OPS = "DISABLED_BY_OPS"
AI_PROVIDER_REFUSED = "PROVIDER_REFUSED"

AI_STATES = (
    AI_CONFIGURED_UNVERIFIED,
    AI_NOT_CONFIGURED,
    AI_DISABLED_BY_OPS,
    AI_PROVIDER_REFUSED,
)


def ai_availability_state(*, provider_refused: bool = False) -> str:
    """The one word for why the AI is or is not usable right now."""

    if provider_refused:
        return AI_PROVIDER_REFUSED
    if not settings.ai_enabled:
        return AI_DISABLED_BY_OPS
    if not settings.ai_api_key and not (settings.hermes_api_url and settings.hermes_api_key):
        return AI_NOT_CONFIGURED
    return AI_CONFIGURED_UNVERIFIED


# The sentences this product writes into `AgentRun.failure_reason` / `JourneyMission.failure_reason`.
# They are the server's own words, so mapping them back is quoting, not guessing - which is why an
# unrecognised reason yields None instead of the nearest of the three.
_CONFIGURED_SENTENCES = (
    "DeepSeek API key is not configured",
    "Hermes runtime endpoint is not configured",
)
_OPS_SENTENCE = "AI is switched off by the operator"
_REFUSED_SENTENCES = (
    "DeepSeek Agent completion failed",
    "Hermes Agent completion failed",
)


# One table, so a run's stored sentence and the health endpoint's word cannot drift apart: the
# sentence is written FROM the state and read BACK into the same state.
STATE_SENTENCES = {
    AI_NOT_CONFIGURED: "No AI credential is configured on this server",
    AI_DISABLED_BY_OPS: "AI is switched off by the operator",
    AI_PROVIDER_REFUSED: "The configured AI provider refused this request",
    AI_CONFIGURED_UNVERIFIED: "An AI credential is configured but has not been verified",
}


def reason_sentence(state: str) -> str:
    """The server's own words for a state, from the same table the parser reads."""

    if state not in STATE_SENTENCES:
        raise ValueError(f"unknown AI availability state: {state}")
    return STATE_SENTENCES[state]


def ai_state_from_failure_reason(reason: str | None) -> str | None:
    """Name the cause of an already-recorded failure, or None when this run did not say."""

    text = (reason or "").strip()
    if not text:
        return None
    if any(marker in text for marker in _REFUSED_SENTENCES):
        return AI_PROVIDER_REFUSED
    if any(marker in text for marker in _CONFIGURED_SENTENCES):
        return AI_NOT_CONFIGURED
    if _OPS_SENTENCE in text:
        return AI_DISABLED_BY_OPS
    return None
