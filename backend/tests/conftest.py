"""Process-wide opt-in for the doors that are now simulation-gated (ORD-0058),
plus a hygiene pass that makes the suite independent of the developer's shell (ORD-AR1).

`POST /api/v1/demo/reset` deletes 52 tables and refuses a caller that has not claimed the
stage, which is right in production and is also what roughly thirty test modules use as
their fixture door.  Rather than have those modules grow a flag each (or, worse, have the
guard quietly fail open), the test process declares itself a simulation once, here, before
`app.main` reads the setting.

This is deliberately not a change to the product default: run the app without this and the
reset route is a 403, which is the behaviour the order asked for and which
`test_qoder_simulation_mode.py` pins in both states.

The second half is the arbiter's ORD-AR1 fix, merged rather than dropped-in: pytest imports
this file before collecting test modules, so provider cleanup happens before any test can
import `app` and freeze environment-derived settings at module import time.  Without it, a
terminal that happens to export a real key flips the whole "no provider configured -> use the
deterministic fallback" family onto the network path, which is how a green suite turned into
33/13 phantom failures twice tonight.  Tests that intentionally exercise configured providers
must set their own environment explicitly after collection starts.

MERGE NOTE (Commander, measured): the arbiter's branch version of this file does NOT contain
the `CITYNEXUS_DEMO_MODE` line, and the authoritative version does not contain the credential
list.  Overwriting either side wholesale would either re-lock the 140 test modules that use
`/demo/reset` as their fixture door, or leave the suite credential-dependent.  Both halves
have to survive in one file.

ORD-0211 (arbiter `5772261512`) adds the third half, and its boundaries are the contract:

```text
backend pytest/unit-integration  => isolated temp DB allowed
authoritative E2E / preview      => still its own declared exact DB/context (09-20 ban stands)
never point tests at _live.db or the user DB
the resolved absolute DB path must be printed / receipted
```

This does **not** close the isolation question: it stops runs feeding each other through
`./citynexus.db` (measured: rows accumulate there, so a door can be green after a dirty run and
red on a clean one).  Modules feeding each other *within* one run is ORD-0210's job, and its
doors stay the judge.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import uuid

os.environ.setdefault("CITYNEXUS_DEMO_MODE", "true")

# ORD-0389: demo mode says the deployment may be a stage; it does not say who may empty it.  The
# reset route additionally requires a token that is read from the environment at call time, and
# the product default is "not configured", which makes the route refuse every caller.  The suite
# is a caller that can claim the door, so it declares one here -- once, before `app.main` is
# imported -- instead of letting ~90 modules grow a secret each.  Modules that want to test the
# refusal simply do not import `demo_stage.demo_reset_headers`.
# Generated, never written into the tree: a literal here would be a credential-shaped string under
# version control, which is exactly what `test_qoder_ord0307_credential_gate.py` C07 scans for.
# An externally declared value is respected, as with the database target below.
if "CITYNEXUS_DEMO_RESET_TOKEN" not in os.environ:
    os.environ["CITYNEXUS_DEMO_RESET_TOKEN"] = uuid.uuid4().hex

# --- ORD-0211: which database does this pytest process actually open? -----------------------
# `app.core.config` reads `CITYNEXUS_DATABASE_URL` (NOT the unprefixed `DATABASE_URL`, which the
# app ignores - measured twice by the commander), and its default is `sqlite:///./citynexus.db`,
# i.e. resolved against whatever directory started the process.  So the pin has to happen here,
# before any test module imports `app`, and it has to be judged on the *resolved* value.
_FORBIDDEN_BASENAMES = {"_live.db"}


def _resolved_url() -> str:
    return os.environ.get("CITYNEXUS_DATABASE_URL") or "sqlite:///./citynexus.db"


def _guard_not_a_real_target(url: str) -> None:
    """Refuse to run the suite against the preview DB or the developer's working database."""

    path = url.replace("sqlite:///", "", 1)
    name = pathlib.PurePath(path).name
    if name in _FORBIDDEN_BASENAMES:
        raise RuntimeError(
            f"ORD-0211 boundary: the test suite must never open {name!r} (preview/live target) "
            f"— resolved to {url!r}"
        )
    if not url.startswith("sqlite:///"):
        return  # a non-SQLite target is declared elsewhere and is not this guard's business
    working = (pathlib.Path.cwd() / "citynexus.db").resolve()
    if pathlib.Path(path).resolve() == working and "pytest" not in str(working.parent):
        raise RuntimeError(
            f"ORD-0211 boundary: the suite resolved to the working database {working} instead of "
            "an isolated temporary one"
        )


_EXTERNALLY_DECLARED = "CITYNEXUS_DATABASE_URL" in os.environ
if not _EXTERNALLY_DECLARED:
    _TEMP_DIR = pathlib.Path(tempfile.mkdtemp(prefix="cnx_pytest_"))
    os.environ["CITYNEXUS_DATABASE_URL"] = f"sqlite:///{(_TEMP_DIR / 'suite.db').as_posix()}"
    RESOLVED_TEST_DATABASE = os.environ["CITYNEXUS_DATABASE_URL"]
else:
    # Declared by the runner (authoritative E2E, a lane's own export, or a deliberate probe).
    # Respected as declared - and still checked, because "I set the variable" is not isolation.
    RESOLVED_TEST_DATABASE = _resolved_url()
_guard_not_a_real_target(RESOLVED_TEST_DATABASE)


def pytest_report_header() -> list[str]:
    """Print the app-resolved absolute target at collection time (receipt-grade, not a name)."""

    absolute = pathlib.Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()
    return [
        "ORD-0211 test database: "
        f"resolved={absolute} | exists={absolute.exists()} | "
        f"declared_by={'runner' if _EXTERNALLY_DECLARED else 'conftest temp dir'}"
    ]

# Real-provider credentials and provider-selection knobs that can make tests leave the
# deterministic fallback path merely because a developer's terminal happens to be configured.
# Keep this list limited to provider configuration: product/runtime code under backend/app is
# intentionally untouched.
_INHERITED_PROVIDER_ENV = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "CITYNEXUS_AI_API_KEY",
    "CITYNEXUS_AI_ENABLED",
    "CITYNEXUS_AI_PROVIDER",
    "CITYNEXUS_AI_MODEL",
    "CITYNEXUS_AI_VISION_MODEL",
    "CITYNEXUS_HERMES_API_KEY",
    "CITYNEXUS_HERMES_API_SERVER_KEY",
    "CITYNEXUS_HERMES_MODEL",
    "CITYNEXUS_HERMES_NATIVE_PROVIDER",
    "CITYNEXUS_HERMES_NATIVE_MODEL",
)

for _name in _INHERITED_PROVIDER_ENV:
    os.environ.pop(_name, None)

del _name
