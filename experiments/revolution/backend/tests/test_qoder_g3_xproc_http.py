"""ORD-0224 / A-2 · ``G3_XPROC_LOSER_READBACK`` in its **closure** shape: two real uvicorn
processes, one fresh temporary database, and the callers speaking HTTP like a traveller does.

Why this file exists at all (arbiter A-2, bus ``5788111742``, landed by the commander at
2026-09-23 10:5x): the store-level cross-process door in ``test_qoder_anywhere_gates.py`` was ruled
``ACCEPTED_STORE_LEVEL_SUPPORTING_EVIDENCE``.  It is real - five OS processes, a socket latch, one
operation - but it calls ``submit_operation`` inside a bare interpreter, so it crosses no HTTP /
app-process boundary, *and* it buys its interesting interleaving by answering the first two
open-operation lookups with ``None``.  Blinding a read is not measuring the race; it is writing the
answer in.  So that door stays as supporting evidence and this one carries the closure claim.

What makes the shape here measurable rather than assumed:

* **the processes are the product's own entry point** - ``uvicorn`` serving ``app.main:app`` on two
  ports, each a separate OS process that imports, migrates and opens the database for itself;
* **the port belongs to this run** - triple proof, no "200 means it is mine": the PID the child
  reports for itself equals what ``netstat`` names as the listener on that port, that child's parent
  is the handle this door spawned, and the database file the child resolved is the fresh file this
  door created (a foreign backend answering on a recycled port fails at least one of the three);
* **the race is scheduled, not gambled on** - the second arm holds SQLite's single write lock from a
  third process while both requests are in flight, so both commits demonstrably block (each caller's
  wall time exceeds the hold), which is the only state in which neither can have seen the other at
  its pre-check read.  Nothing in the product is told what to return: the pre-check runs honestly and
  finds nothing, and the only thing left that can still deliver "two callers, one operation" is the
  partial unique index plus the loser's read-back - which is what mutant **Y3** breaks.

The one thing this file does change about the product is made by the worker script it spawns: the
background worker is suppressed, so an accepted job stays OPEN instead of being answered and
terminalised by the process that accepted it (a terminal job would legitimately start a *new*
generation, per the signed tail-A rule, and the generation under test would evaporate mid-measurement).
That is downstream of the decision being measured and is declared where it happens.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

TABLE = "catalog_hydration_operations"
INDEX = "ux_catalog_hydration_one_open_job"
HYDRATIONS = "/api/v1/catalog-hydrations"
HEALTH = "/api/v1/health"
CALLERS = 2
READY_DEADLINE_S = 120.0
LOCK_HOLD_S = 2.0
REQUEST_TIMEOUT_S = 90.0
OPEN_STATUSES = {"QUEUED", "RUNNING", "ABANDON_REQUESTED"}

BACKEND = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).resolve().parent / "_qoder_g3_http_worker.py"


def _own_question(tag: str) -> str:
    """A question this run has never asked, so no earlier door can supply its answer."""

    return f"贡山独龙族怒族自治县·{tag}·HTTP·{uuid.uuid4().hex[:8]}"


def _http(method: str, port: int, path: str, body: dict | None = None,
          timeout: float = REQUEST_TIMEOUT_S) -> tuple[int, dict, float]:
    """One real HTTP round trip.  4xx/5xx are readings, not exceptions to swallow."""

    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            code = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        code = exc.code
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"_raw": raw[:400]}
    return code, parsed, round(elapsed_ms, 1)


def _netstat_rows() -> list[tuple[int, str, str]]:
    """(local port, state, owning pid) for every TCP line the kernel reports.

    Decoded with ``errors="replace"`` on purpose: under ``python -X utf8`` - which is how the
    mutation campaign and any UTF-8-mode runner starts pytest - asking for text mode makes netstat's
    GBK banner raise inside the reader thread, ``stdout`` comes back ``None`` and this door would
    crash on an AttributeError.  A gate whose verdict depends on the runner's ambient encoding is
    not a gate, so the bytes are decoded here and the only thing lost is the banner's glyphs.
    """

    proc = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"期望：netstat 能列出 TCP 表（端口归属三联证明靠它，不是靠 200）；实际 "
            f"exit={proc.returncode}，stderr="
            f"{(proc.stderr or b'').decode('utf-8', errors='replace')[:200]!r}"
        )
    rows: list[tuple[int, str, str]] = []
    for line in (proc.stdout or b"").decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        # `  TCP  127.0.0.1:52780  0.0.0.0:0  LISTENING  33688` - protocol, LOCAL address, remote
        # address, state, pid.  Count the tail from the end and take the local address by name, or the
        # state column silently becomes the remote address and no listener is ever found (measured).
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        pid, state, local = parts[-1], parts[-2], parts[1]
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        rows.append((port, state, pid))
    return rows


LISTENING_STATES = {"LISTENING", "LISTEN", "侦听", "侦听中"}


def _listening_pid(port: int) -> int | None:
    """Who owns ``port`` right now, straight from the kernel's table - not from a 200."""

    for row_port, state, pid in _netstat_rows():
        if row_port == port and state.upper() in LISTENING_STATES:
            return int(pid) if pid.isdigit() else None
    return None


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _spawn_worker(port: int, database_url: str, log_dir: Path):
    env = dict(os.environ)
    env["CITYNEXUS_DATABASE_URL"] = database_url
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(BACKEND), env.get("PYTHONPATH", "")) if p
    )
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CITYNEXUS_AI_API_KEY", "CITYNEXUS_AI_ENABLED"):
        env.pop(name, None)
    log = log_dir / f"worker_{port}.log"
    handle = log.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(WORKER), str(port), database_url],
        cwd=str(BACKEND), stdout=handle, stderr=subprocess.STDOUT, env=env,
    )
    return proc, handle, log


def _child_events(*logs: Path) -> list[dict]:
    events: list[dict] = []
    for log in logs:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"kind": "unparsed", "line": line[:200]})
    return events


def _wait_for_serving(proc, port: int, log: Path) -> dict:
    """Poll the health route, then return the child's own self-report.  Never a bare sleep."""

    started = time.perf_counter()
    while time.perf_counter() - started < READY_DEADLINE_S:
        if proc.poll() is not None:
            tail = log.read_text(encoding="utf-8", errors="replace")[-600:]
            raise AssertionError(
                f"期望：:{port} 上的 uvicorn 子进程活着并答 200；实际它先退了"
                f"（exit={proc.returncode}，仪器故障不是产品结论），日志尾={tail!r}"
            )
        status, body, _ms = _http("GET", port, HEALTH, timeout=5.0)
        if status == 200:
            serving = next(
                (e for e in _child_events(log) if e.get("kind") == "serving" and e.get("port") == port),
                None,
            )
            if serving is not None:
                serving["health"] = {k: body.get(k) for k in list(body)[:4]}
                return serving
        time.sleep(0.1)
    raise AssertionError(
        f"期望：:{port} 在 {READY_DEADLINE_S} s 内答出 200 并自报 serving 行；实际没有"
        f"（仪器故障，不是产品结论），日志尾="
        f"{log.read_text(encoding='utf-8', errors='replace')[-600:]!r}"
    )


def _prove_port_ownership(label: str, proc, port: int, report: dict, database: Path) -> None:
    """PID / port / marker - three, or the door is talking to somebody else's backend."""

    listener = _listening_pid(port)
    assert listener is not None, (
        f"期望：{label} 的 :{port} 有一个 LISTENING 进程；实际 netstat 上没有"
    )
    assert int(report["pid"]) == int(listener), (
        f"期望：{label} 自报的 pid 就是内核记在 :{port} 上的监听者；实际自报 {report['pid']}、"
        f"netstat {listener} —— 端口不是我的，读数就不是这个门的"
    )
    assert int(report["ppid"]) == int(proc.pid), (
        f"期望：{label} 的父进程就是本门 spawn 的那个句柄（venv 启动器会 re-exec，所以子 pid "
        f"≠ 句柄 pid，但谱系必须接得上）；实际 ppid={report['ppid']}、句柄 pid={proc.pid}"
    )
    assert proc.poll() is None, f"期望：{label} 仍在运行；实际句柄已退出"
    resolved = Path(str(report["resolved_db_file"])).expanduser()
    assert resolved.as_posix().lower() == database.as_posix().lower(), (
        f"期望：{label} 打开的就是本门新建的临时库 {database}；实际它解析到 {resolved} "
        f"—— 一个还活着的外来后端能给出 200，但不会给出这个文件"
    )


def _rows_for_question(database: Path, question: str) -> list[tuple[str, str, str]]:
    """Read the file with ``sqlite3``, not the ORM: the claim is about durable rows."""

    connection = sqlite3.connect(str(database))
    try:
        return [
            (str(row[0]), str(row[1]), str(row[2])[:12])
            for row in connection.execute(
                f"SELECT id, status, fingerprint FROM {TABLE} WHERE request_json LIKE ?",
                (f"%{question}%",),
            ).fetchall()
        ]
    finally:
        connection.close()


def _fire_together(ports: list[int], question: str, release=None, dwell_s: float = LOCK_HOLD_S,
                   ) -> list[dict]:
    """Two callers, one barrier, no sleep used as a synchroniser.

    ``release`` is for the scheduled arm: a third barrier party sits at the same gate as the two
    callers and, once all three are there - i.e. both requests are genuinely in flight - lets go of
    the write lock they are blocked on.  The ``dwell_s`` after the barrier is not the synchroniser
    (the barrier is); it is the time allowed for two in-flight requests to *reach* the blocked
    commit.  Whether they got there is asserted afterwards from each request's own wall time, so a
    dwell that is too short is a visible red, never a quiet green.
    """

    results: list[dict] = []
    lock = threading.Lock()
    gate = threading.Barrier(CALLERS + (1 if release is not None else 0))

    def call(port: int) -> None:
        try:
            gate.wait()
            status, body, ms = _http("POST", port, HYDRATIONS, {"query": question})
            row = {
                "port": port, "status": status, "elapsed_ms": ms,
                "operation_id": body.get("operation_id"), "reused": body.get("reused"),
                "code": body.get("code"), "body": str(body)[:240],
            }
        except Exception as exc:  # noqa: BLE001 - a refused connection is a reading too
            row = {"port": port, "status": None, "elapsed_ms": None, "operation_id": None,
                   "reused": None, "code": type(exc).__name__, "body": str(exc)[:240]}
        with lock:
            results.append(row)

    def releaser() -> None:
        gate.wait()
        time.sleep(dwell_s)
        release()

    threads = [threading.Thread(target=call, args=(p,)) for p in ports]
    if release is not None:
        threads.append(threading.Thread(target=releaser, daemon=True))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=REQUEST_TIMEOUT_S + 30)
    assert not any(t.is_alive() for t in threads), (
        f"期望：{CALLERS} 发请求都在 {REQUEST_TIMEOUT_S + 30} s 内归来；实际有线程仍活着"
        f"（仪器故障，不是产品结论）"
    )
    return sorted(results, key=lambda r: str(r["port"]))


def _assert_one_operation(question: str, posts: list[dict], database: Path, events: list[dict],
                          arm: str) -> dict:
    """The claim itself, applied to one arm: two HTTP callers in two processes, one operation."""

    errors = [p for p in posts if p["operation_id"] is None]
    assert not errors, (
        f"期望：{arm} 两发都被受理并拿到 operation_id（跨进程幂等不许把人推开）；实际 "
        f"{[(p['port'], p['status'], p['code'], p['body'][:120]) for p in errors]}"
    )
    ids = {str(p["operation_id"]) for p in posts}
    assert len(ids) == 1, (
        f"期望：两个真 uvicorn 进程、同一份库、两发同一个问题 ⇒ 同一枚作业；实际 {len(ids)} 枚："
        f"{[(p['port'], p['status'], str(p['operation_id'])[:8], p['reused']) for p in posts]}"
    )
    winners = [p for p in posts if p["reused"] is False]
    joiners = [p for p in posts if p["reused"] is True]
    assert len(winners) == 1 and len(joiners) == CALLERS - 1, (
        f"期望：恰好 1 发开作业（202）+ {CALLERS - 1} 发复用（200）；实际逐发="
        f"{[(p['port'], p['status'], p['reused']) for p in posts]}"
    )
    rows = _rows_for_question(database, question)
    assert len(rows) == 1, (
        f"期望：库里这一代只有 1 行（durable 层，不是两进程各握一份内存握手）；实际 {rows}"
    )
    operation_id, status, _fingerprint = rows[0]
    assert status in OPEN_STATUSES, (
        f"期望：那一行仍是开放代次（{sorted(OPEN_STATUSES)} 之一），否则两发的答复根本不在同一代里；"
        f"实际 {status}"
    )
    created = [e for e in events if e.get("kind") == "dispatch_suppressed"
               and str(e.get("operation_id")) == operation_id]
    assert len(created) == 1, (
        f"期望：恰好一个进程为这枚作业申请过 worker（谁开的作业由**那个进程的日志**说话，不是只信响应体）；"
        f"实际 {len(created)} 条：{created}"
    )
    winner_port = int(winners[0]["port"])
    winner_pid = next(int(e["pid"]) for e in events
                      if e.get("kind") == "serving" and e.get("port") == winner_port)
    assert int(created[0]["pid"]) == winner_pid, (
        f"期望：202 那一发所在进程就是落行的进程（pid {winner_pid}）；实际 "
        f"日志说 pid={created[0]['pid']} 在 :{created[0]['port']} 上开的作业"
    )
    return {"arm": arm, "operation_id": operation_id, "status": status, "store_rows": rows,
            "posts": [{k: p[k] for k in ("port", "status", "reused", "elapsed_ms")} for p in posts],
            "creator_pid": int(created[0]["pid"]), "creator_port": int(created[0]["port"])}


def test_g3_xproc_two_uvicorn_workers_over_http_share_one_operation() -> None:
    """G3_XPROC_LOSER_READBACK (closure shape): two uvicorn processes over HTTP, one operation.

    Two arms, because the claim has two halves:

    * **natural** - two callers released by one barrier hit two independent servers with the same
      question and are handed one operation.  This is what a traveller does, and it is what must not
      regress; it does not by itself say *which* layer converged them.
    * **scheduled** - a third process holds SQLite's single write lock while both requests are in
      flight, so both commits block past the hold (asserted, not hoped).  Neither caller can have
      seen the other at its pre-check read, so a green here is delivered by the partial unique index
      plus the loser's read-back - the mechanism mutant **Y3** breaks.  ``Y3`` therefore has to kill
      this door, and it has to do it every round, which is why the run above it is a 10-round batch
      on ten fresh databases rather than one lucky measurement.
    """

    assert WORKER.exists(), f"期望：uvicorn 子体 {WORKER.name} 在树上；实际不存在"

    temp_dir = Path(tempfile.mkdtemp(prefix="cnx_g3http_"))
    database = temp_dir / "workers.db"
    assert not database.exists(), "期望：本门的库必须是一个全新文件；实际它已经存在"
    database_url = f"sqlite:///{database.as_posix()}"

    from app.db.session import ensure_schema, make_engine  # noqa: PLC0415 - after conftest pinned env

    engine = make_engine(database_url)
    ensure_schema(engine)
    engine.dispose()
    connection = sqlite3.connect(str(database))
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
        ).fetchone()
        index_sql = row[0] if row else None
    finally:
        connection.close()
    assert index_sql, (
        f"期望：这份新库里 {INDEX} 存在（跨进程收敛靠的就是它）；实际 sqlite_master 里没有"
    )
    assert "WHERE" in index_sql.upper(), (
        f"期望：它是**部分**唯一索引，只约束开放代次；实际 {index_sql!r}"
    )

    served: list[dict] = []  # what this door actually got the kernel to confirm, for teardown
    ports = [_free_port() for _ in range(CALLERS)]
    assert all(_listening_pid(p) is None for p in ports), (
        f"期望：开跑前这些端口无人监听；实际 {[(p, _listening_pid(p)) for p in ports]}"
    )
    spawned = [_spawn_worker(p, database_url, temp_dir) for p in ports]
    procs = [s[0] for s in spawned]
    handles = [s[1] for s in spawned]
    logs = [s[2] for s in spawned]
    try:
        reports = [_wait_for_serving(proc, port, log)
                   for proc, port, log in zip(procs, ports, logs)]
        served.extend(reports)
        for proc, port, report in zip(procs, ports, reports):
            _prove_port_ownership(f":{port}", proc, port, report, database)

        # ---- arm 1: natural, two HTTP callers released together ------------------------------
        question_a = _own_question("自然并发")
        posts_a = _fire_together(ports, question_a)
        events_a = _child_events(*logs)
        arm_a = _assert_one_operation(question_a, posts_a, database, events_a, "natural")

        # ---- arm 2: the same race, but *scheduled* on the storage layer ----------------------
        question_b = _own_question("锁住提交")
        # `check_same_thread=False` because the main thread takes the lock and the releaser thread
        # lets it go - sequentially, one at a time, never from two threads at once.
        holder = sqlite3.connect(str(database), isolation_level=None,
                                 timeout=REQUEST_TIMEOUT_S * 2, check_same_thread=False)
        holder.execute("CREATE TABLE IF NOT EXISTS _g3_http_lockholder (note TEXT)")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO _g3_http_lockholder VALUES ('holding the single write lock')")
        released: list[str] = []

        def _let_go() -> None:
            holder.execute("ROLLBACK")
            released.append("write lock released while both requests were in flight")

        try:
            posts_b = _fire_together(ports, question_b, release=_let_go)
        finally:
            try:  # best effort: a red must never leave the write lock held behind it
                if holder.in_transaction:
                    holder.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            holder.close()
        assert len(released) == 1, (
            "期望：放行线程确实松开过写锁（否则两发是被 busy_timeout 拖红的，不是被安排的）；"
            f"实际 released={released}"
        )
        events_b = _child_events(*logs)
        arm_b = _assert_one_operation(question_b, posts_b, database, events_b, "scheduled")
        blocked = [p for p in posts_b if p["elapsed_ms"] is not None
                   and p["elapsed_ms"] >= LOCK_HOLD_S * 1000 * 0.9]
        assert len(blocked) == CALLERS, (
            f"期望：两发都**被写锁挡住**超过 {LOCK_HOLD_S} s——这是「谁也看不见谁」的显式状态探针；"
            f"没有它，这一臂就退化成赌时间。实际逐发 ms="
            f"{[(p['port'], p['elapsed_ms']) for p in posts_b]}"
        )

        print(
            "\n[G3_XPROC_LOSER_READBACK·HTTP] 2 个真 uvicorn 进程 → 每臂 1 枚作业；"
            f"ports={ports}，serving pids={[r['pid'] for r in reports]}，"
            f"netstat pids={[ _listening_pid(p) for p in ports ]}，"
            f"natural={arm_a['operation_id'][:8]}/{arm_a['status']}（开行于 pid {arm_a['creator_pid']} "
            f"@ :{arm_a['creator_port']}），"
            f"scheduled={arm_b['operation_id'][:8]}/{arm_b['status']}（开行于 pid {arm_b['creator_pid']} "
            f"@ :{arm_b['creator_port']}），提交阻塞 ms={[(p['port'], p['elapsed_ms']) for p in posts_b]}"
        )
    finally:
        # The venv launcher re-execs, so the process holding the port (and the log file, and the
        # database) is a *child* of my handle.  Terminating only the handle leaves the server
        # running - measured here as `PermissionError` on its own log.  Kill the tree, then the
        # recorded serving PIDs, then wait for the kernel to stop naming a listener on the port.
        for proc in procs:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # noqa: PERF203
                proc.kill()
        for report in served:
            subprocess.run(["taskkill", "/PID", str(report["pid"]), "/T", "/F"],
                           capture_output=True, text=True)
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline and any(_listening_pid(p) for p in ports):
            time.sleep(0.1)
        for handle in handles:
            handle.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
        assert not temp_dir.exists(), (
            f"期望：本门的临时目录自己清干净（生产者自清）；实际残留 {temp_dir}"
        )
