"""ORD-0387 - the AI availability word stays one vocabulary across four artifacts.

Why this gate exists, measured at the tip it was opened against
`3e75ab2bbbac19c47ab042fce9ea90be0f04829b`:

* `backend/app/agent/ai_availability.py` produced four words in `AI_STATES`;
* `contracts/openapi.yaml` and `:1822` declared exactly those four as the `enum` for
  `ai_state` on `/agent/runtime-status` and on `HealthResponse`;
* `frontend/src/generated/api.ts` -- which is produced *from that contract* by
  openapi-typescript -- contained **zero** occurrences of `ai_state`, because the checked-in
  generation predated ORD-0359.  A client written against it could not see the field at all,
  so the typed contract and the human contract disagreed while both looked green.

The defect class is not "one field missing".  It is that four copies of one vocabulary are
kept in four places by hand, and nothing read them against each other.  These tests close that:
each arm compares two artifacts that are supposed to agree, and every comparison is offline --
no provider is contacted, because `ai_state` is a statement about this server's own settings and
answering it by dialling out would make the word about somebody else (see the module docstring
in `ai_availability.py`, and the 7892 proxy lesson: an unreachable-looking answer here would be
an artifact of this network, not of the product).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.agent.ai_availability import AI_STATES, ai_availability_state

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED_TS = ROOT / "frontend/src/generated/api.ts"
API_TS = ROOT / "frontend/src/api/citynexus.ts"
SHEET_TSX = ROOT / "frontend/src/features/planning/PlanWithAiSheet.tsx"

WORDS = frozenset(AI_STATES)


def _read(path: Path) -> str:
    assert path.is_file(), f"expected artifact missing: {path}"
    return path.read_text(encoding="utf-8")


def _enum_at(spec: dict, pointer: list[str]) -> frozenset[str]:
    node = spec
    for step in pointer:
        node = node[step]
    return frozenset(node["enum"])


def test_the_contract_declares_the_same_words_the_server_produces():
    """`AI_STATES` and both contract enums must be one set, not three that happen to match today."""

    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    status_enum = _enum_at(
        spec,
        [
            "paths",
            "/agent/runtime-status",
            "get",
            "responses",
            "200",
            "content",
            "application/json",
            "schema",
            "properties",
            "ai_state",
        ],
    )
    health_enum = _enum_at(
        spec, ["components", "schemas", "HealthResponse", "properties", "ai_state"]
    )
    assert status_enum == WORDS, f"runtime-status enum drifted: {sorted(status_enum ^ WORDS)}"
    assert health_enum == WORDS, f"HealthResponse enum drifted: {sorted(health_enum ^ WORDS)}"


def test_the_generated_typescript_carries_the_field_it_was_generated_from():
    """This is the arm that was red at the opening reading: the generation predated the contract.

    It checks the union openapi-typescript emits for `ai_state`, not merely that the word appears
    somewhere in the file, so a field that exists but lists the wrong words cannot pass.
    """

    text = _read(GENERATED_TS)
    lines = [line for line in text.splitlines() if "ai_state" in line]
    assert lines, "generated/api.ts has no ai_state at all: regenerate it from contracts/openapi.yaml"
    for line in lines:
        found = frozenset(re.findall(r'"([A-Z][A-Z_]+)"', line))
        assert found == WORDS, (
            f"generated union is not the server vocabulary ({sorted(found ^ WORDS)}) in: {line.strip()}"
        )


def test_the_hand_written_client_names_the_field_and_every_word():
    """`AgentRuntimeStatus.ai_state` must exist and `AiStateWord` must list exactly the four."""

    text = _read(API_TS)
    assert re.search(r"ai_state\?:\s*AiStateWord", text), (
        "AgentRuntimeStatus does not type ai_state as AiStateWord -- the mapping is unbound"
    )
    block = re.search(r"export type AiStateWord =([^;]+);", text)
    assert block, "AiStateWord is missing from api/citynexus.ts"
    listed = frozenset(re.findall(r'"([A-Z][A-Z_]+)"', block.group(1)))
    assert listed == WORDS, f"AiStateWord drifted from the server: {sorted(listed ^ WORDS)}"


def test_the_screen_maps_each_word_and_invents_no_extra_one():
    """Every server word has a branch, and no branch exists for a word the server never says.

    An unmapped word still has to be *mentionable* here rather than silently absorbed: the
    function's fall-through is the honest path, so the arm asserts the two sets are equal in both
    directions and a fifth server word becomes a named red instead of a quiet unknown.
    """

    text = _read(SHEET_TSX)
    start = text.index("export function aiStateFromServer")
    body = text[start : start + 1600]
    mapped = frozenset(re.findall(r'case "([A-Z][A-Z_]+)":', body))
    assert mapped == WORDS, f"aiStateFromServer cases disagree with AI_STATES: {sorted(mapped ^ WORDS)}"


def test_the_route_answers_from_settings_alone_and_never_dials_out():
    """Requirement: the value comes from the service's own configuration, not from a probe.

    Only the async transport is severed.  This is the discriminating choice, and it is the one
    this arm got wrong the first time it was written: `starlette.testclient.TestClient` makes its
    in-process call through the *sync* `httpx.Client.send`, so blocking that reads a green test
    harness as an outgoing provider call.  Every outbound call this application makes is an
    `httpx.AsyncClient` one (weather, discovery, routing, the Hermes gateway), so severing the
    async transport asks exactly the question this order asks: does answering `ai_state` reach
    outside the process?
    """

    import httpx

    dialed: list[str] = []

    async def _refuse(self, *args, **kwargs):  # noqa: ANN001 - duck-typed transport hook
        url = str(args[0]) if args else str(kwargs.get("url"))
        dialed.append(url)
        raise AssertionError(
            f"answering ai_state contacted an outbound endpoint: {url!r}"
        )

    real_send = httpx.AsyncClient.send
    httpx.AsyncClient.send = _refuse  # type: ignore[assignment]
    try:
        from app.main import app

        client = TestClient(app)
        response = client.get("/api/v1/agent/runtime-status")
        health = client.get("/api/v1/health")
    finally:
        httpx.AsyncClient.send = real_send  # type: ignore[assignment]

    assert dialed == [], f"outbound calls made while answering the capability read: {dialed}"
    assert response.status_code == 200, f"runtime-status answered {response.status_code}"
    body = response.json()
    assert "ai_state" in body, "runtime-status answered without naming the AI state"
    assert body["ai_state"] in WORDS, f"unrecognised ai_state on the wire: {body['ai_state']!r}"
    assert health.status_code == 200, f"health answered {health.status_code}"
    assert health.json()["ai_state"] == body["ai_state"], (
        "two doors report two different AI states for one server"
    )
    assert body["ai_state"] == ai_availability_state(), (
        "the route and the module of record disagree"
    )
