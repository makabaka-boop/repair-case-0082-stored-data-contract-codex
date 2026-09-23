"""真实 PostgreSQL 验收夹具：预置旧记录 + 真实 uvicorn 服务（可重启）。

与内存 SQLite 套件不同，这里：
* 用 psycopg / SQLAlchemy 直连真实 PostgreSQL 预置"升级/恢复前旧记录"；
* 被测对象是独立进程的 uvicorn 服务，``restart_server`` 会真正杀掉并重启，
  用于核对结论在重启后保持确定；
* 所有损坏数据都由原生 SQL 写入，完全绕过应用请求层校验。

连接串取 ``TIDE_PG_URL``，回退 ``DATABASE_URL``；二者都不是 PostgreSQL 时
整套验收跳过（由 ./verify-pg 启动嵌入式 PG 后自动满足）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent


def _pg_url() -> str | None:
    url = os.environ.get("TIDE_PG_URL") or os.environ.get("DATABASE_URL", "")
    return url if url.startswith("postgresql") else None


PG_URL = _pg_url()
pytestmark = pytest.mark.skipif(
    PG_URL is None,
    reason="未提供 PostgreSQL 连接串（TIDE_PG_URL/DATABASE_URL），跳过真实 PG 验收",
)


def _wait_healthy(base: str, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as exc:  # 启动竞态
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"服务未就绪: {last}")


@pytest.fixture()
def engine():
    eng = create_engine(PG_URL, future=True)
    # 干净起点 + 确保表结构存在。
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS gates"))
        conn.execute(text("DROP TABLE IF EXISTS plans"))
        conn.execute(text("DROP TABLE IF EXISTS calendars"))
    from app.database import Base

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def server():
    # 固定端口：每次 start 前上一个进程已终止，避免动态端口频繁分配的竞态。
    port = int(os.environ.get("TIDE_TEST_PORT", "55901"))
    proc: subprocess.Popen | None = None
    log = open(os.environ.get("TIDE_UVICORN_LOG", "/dev/null"), "a")
    env = dict(os.environ, DATABASE_URL=PG_URL, PYTHONPATH=str(ROOT))

    def start() -> str:
        nonlocal proc
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        _wait_healthy(base)
        return base

    base = start()
    try:
        yield {"base": base, "restart": lambda: (_stop(proc), start())[1]}
    finally:
        _stop(proc)
        log.close()


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# ---- 原生 SQL 预置（绕过应用层校验，模拟升级/恢复前旧记录） ----


def insert_calendar(engine, calendar_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO calendars (id) VALUES (:id)"), {"id": calendar_id}
        )


def insert_gate(
    engine, calendar_id, position, gate_id, windows_json
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gates (calendar_id, position, gate_id, windows) "
                "VALUES (:c, :p, :g, :w)"
            ),
            {
                "c": calendar_id,
                "p": position,
                "g": gate_id,
                "w": windows_json,
            },
        )


def insert_plan(
    engine, plan_id, calendar_id, payload_json, departure, witnesses_json
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, witnesses) "
                "VALUES (:id, :c, :p, :d, :w)"
            ),
            {
                "id": plan_id,
                "c": calendar_id,
                "p": payload_json,
                "d": departure,
                "w": witnesses_json,
            },
        )


def snapshot(engine):
    """三表逐项快照（含原始 JSON 文本，避免比较时被规范化）。"""
    with engine.begin() as conn:
        calendars = [r[0] for r in conn.execute(text("SELECT id FROM calendars"))]
        gates = [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT calendar_id, position, gate_id, windows FROM gates "
                    "ORDER BY calendar_id, id"
                )
            )
        ]
        plans = [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT id, calendar_id, payload, departure, witnesses "
                    "FROM plans ORDER BY id"
                )
            )
        ]
    return sorted(calendars), gates, plans


def fid(prefix: str, n: int) -> str:
    """32 字符定长可读 ID（表列为 String(32)）。"""
    return f"{prefix}{n:0{32 - len(prefix)}d}"


def assert_calendar_500(resp, calendar_id, reason):
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert detail == {
        "error": "CORRUPT_CALENDAR_DATA",
        "calendar_id": calendar_id,
        "reason": reason,
        "gate_id": detail["gate_id"],
        "position": detail["position"],
    }
    return detail


def assert_plan_500(resp, plan_id, reason):
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "CORRUPT_PLAN_DATA"
    assert detail["plan_id"] == plan_id
    assert detail["reason"] == reason
    return detail
