"""ORD-0389: `POST /api/v1/demo/reset` deletes the whole library, so the suite now claims it.

`conftest.py` declares the token once, before `app.main` freezes anything environment-derived, and
every module that uses the reset route as its fixture door hands it back through this helper.  The
point of putting it here rather than inline in ninety-odd modules is that the *shape* stays visible
at each call site -- `headers=demo_reset_headers()` says the door needs a claim -- while the value
stays in one place, and a module that wants to test the refusal can simply not call this.

This is deliberately not a way of making the guard fail open: run the app without the environment
variable set and the route answers a named 403 for every caller, which is what
`test_qoder_ord0389_reset_token_gate.py` pins.
"""

from __future__ import annotations

import os

# The names live in the product so the header an operator must send is the same one the route
# reads; importing them here would force every test module's import order through `app.main`.
DEMO_RESET_ENV = "CITYNEXUS_DEMO_RESET_TOKEN"
DEMO_RESET_HEADER = "X-Demo-Reset-Token"


def demo_reset_token() -> str:
    token = os.environ.get(DEMO_RESET_ENV)
    if not token:
        raise RuntimeError(
            f"{DEMO_RESET_ENV} is not set in this pytest process; conftest declares it "
            "before app.main is imported, and the reset route refuses without it"
        )
    return token


def demo_reset_headers() -> dict[str, str]:
    """Headers that claim the whole-library reset door."""

    return {DEMO_RESET_HEADER: demo_reset_token()}
