"""ORD-0094 lane F - the ANYWHERE draft contract must not drift away from the running app.

Two failures are equally bad and this file is written to catch both:

* the contract promises an operation the backend does not answer, which is how a screen gets
  built against a route nobody shipped and the user later sees a silent 404;
* the backend ships the route but the contract still says "draft-unimplemented", which is the
  same lie pointing the other way.

The probe is a real HTTP call through the ASGI app, not a scan of the route table: a path can
be present in `app.routes` and still be shadowed, and a router-level absence is exactly the
fact we want (`{"detail":"Not Found"}` from Starlette, which the app's own 404s never look
like - those carry "X <id> was not found" or the `{error:{code,...}}` envelope).

Measured at authoritative HEAD `4c8c911`: every one of the four operations below answers the
router's 404, so this file is red by design and each red names the operation, the expectation
and the actual status/body.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

DRAFT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi-anywhere-draft.yaml"

# (operation id, HTTP method, path with `{param}` placeholders, a concrete probe path)
DRAFT_OPERATIONS = [
    ("getDestinationCapability", "get", "/destinations/capability", "/api/v1/destinations/capability?query=Beijing"),
    ("createCatalogHydration", "post", "/catalog-hydrations", "/api/v1/catalog-hydrations"),
    (
        "getCatalogHydration",
        "get",
        "/catalog-hydrations/{operation_id}",
        "/api/v1/catalog-hydrations/11111111-1111-4111-8111-111111111111",
    ),
    (
        "abandonCatalogHydration",
        "post",
        "/catalog-hydrations/{operation_id}/abandon",
        "/api/v1/catalog-hydrations/11111111-1111-4111-8111-111111111111/abandon",
    ),
]

# A control that is known to exist today: without it, "404 everywhere" would be indistinguishable
# from "the app object never served a request".
CONTROL_PATH = "/api/v1/demo/scene"


def _router_absent(response) -> bool:
    """Starlette's own 'no such route' answer, as opposed to the app refusing a known route."""
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON 404 is still not the router's answer
        return False
    return isinstance(body, dict) and body.get("detail") == "Not Found"


def _draft() -> dict:
    assert DRAFT_PATH.exists(), f"SETUP 失败：契约草案不存在 {DRAFT_PATH}"
    with DRAFT_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_control_route_is_served_so_a_404_means_something():
    """Precondition for every other assertion here: the app does answer known routes."""
    response = client.get(CONTROL_PATH)
    assert not _router_absent(response), (
        f"SETUP 失败：已实现的 {CONTROL_PATH} 也返回路由级 404，"
        "说明这个 TestClient 根本没在服务当前 app，下面的红就不能当差集证据用"
    )


def test_annex_declares_no_operations_of_its_own():
    """The arbiter owns the operations now; a second definition here would be the next lie.

    Demoting `openapi-anywhere-draft.yaml` to an annex is only honest if the annex really
    stops defining endpoints: two sources of truth drift, and whichever is read later wins.
    """
    draft = _draft()
    assert (draft.get("paths") or {}) == {}, (
        f"期望：附件不声明任何 path（真值源是 arbiter/anywhere-contract 的 "
        f"contracts/anywhere_v1.openapi.yaml）；实际：又声明了 {sorted(draft['paths'])}"
    )
    authority = (draft.get("info") or {}).get("x-authority") or {}
    for key in ("branch", "contract-file", "introduced-by"):
        assert authority.get(key), f"期望：附件写明真值源 {key}；实际 x-authority={authority!r}"
    recorded = ((draft.get("info") or {}).get("x-differential-evidence") or {}).get("targets") or []
    assert len(recorded) == len(DRAFT_OPERATIONS), (
        f"期望：附件把被门的四个操作连同实测读数一起登记（{len(DRAFT_OPERATIONS)} 条）；"
        f"实际：{len(recorded)} 条"
    )


def test_every_draft_operation_is_reachable_on_the_running_app():
    """Contract/implementation parity, one assertion per operation so a red names the culprit."""
    missing = []
    for operation_id, method, _template, probe in DRAFT_OPERATIONS:
        response = getattr(client, method)(probe)
        if _router_absent(response):
            missing.append(f"{method.upper()} {probe} -> 404 Not Found（{operation_id}）")
    assert not missing, (
        "期望：契约（arbiter v1，附件按它）登记的四个操作在应用上可达（非路由级 404）；"
        f"实际：仍缺 {len(missing)}/4 —— " + " ; ".join(missing) + "。"
        "这条红就是车道 C 的收口条件之一，不许改成 skip，也不许把断言放宽成'状态码是数字'"
    )


def test_contract_label_matches_reality_once_the_routes_land():
    """The other direction of parity: shipped routes must not stay labelled as a draft."""
    still_draft = str((_draft().get("info") or {}).get("x-status", "")) == "draft-unimplemented"
    all_present = all(
        not _router_absent(getattr(client, method)(probe))
        for _id, method, _template, probe in DRAFT_OPERATIONS
    )
    assert not (all_present and still_draft), (
        "期望：四个操作都已实现时，草案必须去掉 x-status: draft-unimplemented 并并入 "
        "contracts/openapi.yaml；实际：路由都在了，契约还写着未实现。"
        "一份长期说自己是草稿的已实现契约，就是下一个「契约里有、实现里没有」的产地"
    )
