"""ORD-0094 §17E - prove provider HTTP happens outside a SQLite write transaction.

The mission's own §6 order of operations is: short DB read -> release session/transaction ->
provider network -> short persistence transaction -> release -> next phase. The failure this
door exists for already happened: the stay-generation path held the SQLite write lock for ~115
seconds, so a background job that merely *moves* the lock to another thread does not satisfy
it either.

Two design points, because a measuring gate that cannot measure is worse than none:

* the instrument proves itself first (G9a). Zero overlap is meaningless if the probe saw zero
  transactions and zero network calls, so G9a forces one known write path and one known
  provider-shaped call and requires both to be recorded;
* the provider call is observed by wrapping the transport layer, and only hosts the product
  actually talks to are counted (`LIVE_HOSTS`,抄自 app/integrations/**). TestClient traffic
  goes over an ASGI transport, so it neither registers nor pollutes the overlap arithmetic.

Windows are transaction begin -> commit/rollback on this process's engine. Under `TestClient`
the app runs in this process, which is what makes the comparison honest here; it is not a
substitute for a multi-worker measurement, and G9c is the concurrent probe for that shape.
"""

from __future__ import annotations

import concurrent.futures
import re
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db.session import engine
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)

CAPABILITY = "/api/v1/destinations/capability"
HYDRATIONS = "/api/v1/catalog-hydrations"
UNSEEN_COUNTY = "贡山独龙族怒族自治县"
WRITE_VERB = re.compile(r"^\s*(insert|update|delete|replace|create|alter|drop)\b", re.IGNORECASE)
LIVE_HOSTS = (
    "photon.komoot.io",
    "overpass-api.de",
    "overpass.private.coffee",
    "overpass.kumi.systems",
    "nominatim.openstreetmap.org",
    "restapi.amap.com",
    "api.open-meteo.com",
    "api.deepseek.com",
)

_windows: list[dict] = []
_open: dict[int, dict] = {}
_calls: list[dict] = []
_lock = threading.Lock()


def _open_window(conn) -> None:
    with _lock:
        entry = {"started": time.perf_counter(), "conn": id(conn), "write": False}
        _open[id(conn)] = entry


def _close_window(conn) -> None:
    with _lock:
        entry = _open.pop(id(conn), None)
        if entry is not None:
            entry["ended"] = time.perf_counter()
            _windows.append(entry)


@event.listens_for(engine, "begin")
def _on_begin(conn):  # pragma: no cover - instrumentation
    _open_window(conn)


@event.listens_for(engine, "commit")
def _on_commit(conn):  # pragma: no cover
    _close_window(conn)


@event.listens_for(engine, "rollback")
def _on_rollback(conn):  # pragma: no cover
    _close_window(conn)


@event.listens_for(engine, "before_cursor_execute")
def _on_execute(conn, cursor, statement, parameters, context, executemany):  # pragma: no cover
    if not WRITE_VERB.match(statement or ""):
        return
    with _lock:
        entry = _open.get(id(conn))
        if entry is not None:
            entry["write"] = True


def _host_of(request) -> str:
    try:
        return str(request.url.host or "")
    except Exception:  # noqa: BLE001
        return ""


_TRANSPORT_CLASSES = [(httpx.HTTPTransport, "handle_request"), (httpx.AsyncHTTPTransport, "handle_async_request")]
_originals = {}


class _Recorder:
    """Wraps transport handle_request to timestamp provider-shaped calls."""

    @staticmethod
    def _note(host: str, started: float) -> None:
        if host in LIVE_HOSTS or host.endswith(".invalid"):
            with _lock:
                _calls.append({"host": host, "started": started, "ended": time.perf_counter()})


def _install() -> None:
    if _originals:
        return
    for klass, method in _TRANSPORT_CLASSES:
        original = getattr(klass, method)
        _originals[(klass, method)] = original

        if method == "handle_request":

            def patched(self, request, *args, original=original, **kwargs):
                started = time.perf_counter()
                try:
                    return original(self, request, *args, **kwargs)
                finally:
                    _Recorder._note(_host_of(request), started)

        else:

            async def patched(self, request, *args, original=original, **kwargs):
                started = time.perf_counter()
                try:
                    return await original(self, request, *args, **kwargs)
                finally:
                    _Recorder._note(_host_of(request), started)

        setattr(klass, method, patched)


def _uninstall() -> None:
    for (klass, method), original in _originals.items():
        setattr(klass, method, original)
    _originals.clear()


@pytest.fixture(autouse=True)
def _instrumented():
    _windows.clear()
    _open.clear()
    _calls.clear()
    _install()
    try:
        yield
    finally:
        _uninstall()


def _write_windows() -> list[dict]:
    return [w for w in _windows if w.get("write")]


def _overlaps(call: dict, windows: list[dict]) -> list[dict]:
    return [
        w
        for w in windows
        if w.get("ended") is not None and call["started"] < w["ended"] and call["ended"] > w["started"]
    ]


def _summary(response) -> str:
    try:
        return f"{response.status_code} / {str(response.json())[:180]}"
    except Exception:  # noqa: BLE001
        return f"{response.status_code} / <non-json>"


def _reset_seed() -> str:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, f"SETUP 失败：/demo/reset 实际 {_summary(reset)}"
    scene = client.get("/api/v1/demo/scene").json()
    journey_id = scene.get("journey_id")
    assert journey_id, "SETUP 失败：reset 之后 scene 里没有 journey_id"
    return str(journey_id)


# --------------------------------------------------------------------------------------
# G9a - 仪器自证（今天应当绿：它证明后面两条"零重叠"不是"零观测"）
# --------------------------------------------------------------------------------------
def test_g9a_the_instrument_actually_sees_write_transactions_and_provider_calls():
    _reset_seed()  # 一条确定的写路径：reset 会 seed 若干表
    writes = _write_windows()
    assert writes, (
        "期望：仪器在已知写路径上至少抓到 1 个写事务窗口（否则 G9b 的「零重叠」等于没测）；"
        f"实际：写窗口 {len(writes)}，全部事务窗口 {len(_windows)}"
    )

    with pytest.raises(Exception):  # noqa: BLE001 - 不可解析域，预期失败，只要窗口被记录
        httpx.get("http://instrument-selftest.invalid/api/interpreter", timeout=5.0)
    assert _calls, (
        "期望：provider 形态的调用被记录到（证明 transport 挂钩真的挂上了）；"
        f"实际：记录 {len(_calls)} 条"
    )
    assert any(call["host"].endswith(".invalid") for call in _calls), (
        f"期望：至少一条 provider 形态调用被记录到；实际 hosts：{sorted({c['host'] for c in _calls})}"
    )


# --------------------------------------------------------------------------------------
# G9b - 水合期间 provider 调用不得落在写事务窗口里
# --------------------------------------------------------------------------------------
def test_g9b_provider_http_never_runs_inside_an_open_write_transaction():
    """§6/§7: 短读 → 放锁 → 网络 → 短写。这条就是那句顺序的代码形式。"""
    _reset_seed()
    started = client.post(HYDRATIONS, json={"query": UNSEEN_COUNTY})
    assert not (started.status_code == 404 and started.json().get("detail") == "Not Found"), (
        f"期望：{HYDRATIONS} 已实现（§17E 要量的就是它跑网络时握不握写锁）；实际：{_summary(started)}"
    )
    assert started.status_code in (200, 202), f"期望 200/202 受理；实际：{_summary(started)}"
    operation_id = started.json().get("operation_id")

    deadline = time.perf_counter() + 60.0
    body: dict = {}
    while time.perf_counter() < deadline:
        poll = client.get(f"{HYDRATIONS}/{operation_id}")
        assert poll.status_code == 200, f"期望 GET 操作态 200；实际：{_summary(poll)}"
        body = poll.json()
        if body.get("terminal") is True or body.get("status") in {"COMPLETED", "FAILED", "UNAVAILABLE", "ABANDONED"}:
            break
        time.sleep(0.2)

    provider_calls = [c for c in _calls if c["host"] in LIVE_HOSTS]
    assert provider_calls, (
        "期望：这次水合真的走过 provider（否则「没重叠」是假的——它什么都没做）；"
        f"实际：observed provider calls = 0，操作态 {str(body)[:180]}"
    )
    offenders = [c for c in provider_calls if _overlaps(c, _write_windows())]
    assert not offenders, (
        f"期望：{len(provider_calls)} 次 provider 调用全部落在写事务窗口之外；"
        f"实际：{len(offenders)} 次与写事务重叠。stay-generation 的 115 秒独占写锁不许重演——"
        "把锁挪到后台线程也不等于满足这条"
    )


# --------------------------------------------------------------------------------------
# G9c - 并发探针：水合跑网络时，别的旅程的 plans/generate 不能被锁住
# --------------------------------------------------------------------------------------
def test_g9c_hydration_network_does_not_starve_a_concurrent_plan_generate():
    """The observable version of the same rule: another journey still gets served promptly."""
    journey_id = _reset_seed()
    # 不发明请求体：`POST /api/v1/journeys` 的字段由产品定，我编一个只会得到 422，
    # 那红是在测我自己。并发探针用种子里已有那条旅程 —— 锁被握住时它一样会卡住。
    other_id = journey_id

    probe = client.post(HYDRATIONS, json={"query": UNSEEN_COUNTY})
    assert not (probe.status_code == 404 and probe.json().get("detail") == "Not Found"), (
        f"期望：{HYDRATIONS} 已实现，否则这条门没有东西可锁 —— 它会因为「什么都没跑」而 "
        "漂亮地通过，那正是恒真门；实际：" + _summary(probe)
    )
    assert probe.status_code in (200, 202), f"期望 200/202 受理；实际：{_summary(probe)}"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        hydration = pool.submit(client.post, HYDRATIONS, json={"query": UNSEEN_COUNTY, "journey_id": journey_id})
        time.sleep(0.05)
        clock_start = time.perf_counter()
        generate = pool.submit(client.post, f"/api/v1/journeys/{other_id}/plans/generate")
        try:
            plan_response = generate.result(timeout=8.0)
        except concurrent.futures.TimeoutError:
            pytest.fail(
                "期望：水合在跑网络时，另一条旅程的 plans/generate 仍在 8 秒内被服务；"
                "实际：8 秒没返回 —— 这就是写锁被长时间握住的形状（§17E 的并发形态）"
            )
        elapsed_ms = round((time.perf_counter() - clock_start) * 1000, 1)

    assert not (plan_response.status_code == 404 and plan_response.json().get("detail") == "Not Found"), (
        f"期望：{HYDRATIONS} 已实现才能测这条；实际：{_summary(hydration.result())}"
    )
    assert elapsed_ms < 5000.0, (
        f"期望：plans/generate 在 5 秒内返回（水合不得把它锁在后面）；实际：{elapsed_ms} ms，"
        f"状态 {plan_response.status_code}。注意：这个数字只在两件事都成立时才有意义——"
        "水合确实跑了网络，且 plans/generate 确实是被服务的那一条"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
