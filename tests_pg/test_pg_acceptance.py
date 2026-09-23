"""真实 PostgreSQL 验收：升级/恢复后既有闸门日历与方案的完整性语义。

覆盖（数据全部由原生 SQL 预置到真实 PostgreSQL）：
1. 重复 gate_id；2. 重复 position；3. 损坏窗口（非法 JSON/非二元整数）；
4. 非法边界（越界/倒置/重叠）；5. 损坏方案（航程定义/见证）；
6. 完整合法旧记录。

每个异常场景：详情/探测/采纳/查询/重放返回确定且可区分的 500，
重复查询与真实重启服务后结论一致，失败前后三表逐项不变；
合法旧记录仍可探测、采纳，并完成跨日历重放。
"""
from __future__ import annotations

import json

import httpx
import pytest

from conftest import (
    PG_URL,
    assert_calendar_500,
    assert_plan_500,
    fid,
    insert_calendar,
    insert_gate,
    insert_plan,
    snapshot,
)

pytestmark = pytest.mark.skipif(
    PG_URL is None,
    reason="未提供 PostgreSQL 连接串（TIDE_PG_URL/DATABASE_URL），跳过真实 PG 验收",
)

# 损坏原因码（与 app.integrity 一致；显式字符串避免测试对实现的过度耦合）
DUP_GATE = "DUPLICATE_GATE_ID"
DUP_POSITION = "DUPLICATE_POSITION"
WIN_NOT_JSON = "GATE_WINDOWS_NOT_JSON"
WIN_SHAPE = "GATE_WINDOWS_SHAPE"
WIN_RANGE = "GATE_WINDOWS_OUT_OF_RANGE"
WIN_INVERTED = "GATE_WINDOW_INVERTED"
WIN_OVERLAP = "GATE_WINDOWS_OVERLAP"
PAYLOAD_NOT_JSON = "PLAN_PAYLOAD_NOT_JSON"
PAYLOAD_SHAPE = "PLAN_PAYLOAD_SHAPE"
WIT_NOT_JSON = "PLAN_WITNESSES_NOT_JSON"
WIT_SHAPE = "PLAN_WITNESSES_SHAPE"

VOYAGE = {
    "gates": ["G1"],
    "legs": [],
    "max_waits": [100],
    "search_start": 0,
    "search_end": 1000,
}


def _probe(base, cid, gates=("G1",)):
    n = len(gates)
    return httpx.post(
        f"{base}/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": list(gates),
            "legs": [1] * (n - 1),
            "max_waits": [100] * n,
            "search_start": 0,
            "search_end": 1000,
        },
    )


def _adopt(base, cid, departure=5, gates=("G1",)):
    n = len(gates)
    return httpx.post(
        f"{base}/plans",
        json={
            "calendar_id": cid,
            "gates": list(gates),
            "legs": [1] * (n - 1),
            "max_waits": [100] * n,
            "departure": departure,
        },
    )


def _corruption_hits(base, cid, reason, gates=("G1",)):
    """详情/探测/采纳都返回同一可区分 500，并返回详情供重启后比对。"""
    d1 = assert_calendar_500(httpx.get(f"{base}/calendars/{cid}"), cid, reason)
    assert httpx.get(f"{base}/calendars/{cid}").json()["detail"] == d1
    assert _probe(base, cid, gates=gates).json()["detail"]["reason"] == reason
    assert _adopt(base, cid, gates=gates).json()["detail"]["reason"] == reason
    return d1


def test_duplicate_gate_id(engine, server):
    cid = fid("caldup", 1)
    insert_calendar(engine, cid)
    insert_gate(engine, cid, 0, "G1", "[[10, 20]]")
    insert_gate(engine, cid, 1, "G1", "[[90, 100]]")
    base = server["base"]
    before = snapshot(engine)

    d1 = _corruption_hits(base, cid, DUP_GATE)

    # 真实重启后结论依旧
    base2 = server["restart"]()
    assert _corruption_hits(base2, cid, DUP_GATE) == d1

    # 两项原始记录都仍在，未被删除或挑选；三表逐项不变
    assert snapshot(engine) == before
    gates = snapshot(engine)[1]
    assert [g[2] for g in gates if g[0] == cid] == ["G1", "G1"]
    assert [g[3] for g in gates if g[0] == cid] == [
        "[[10, 20]]",
        "[[90, 100]]",
    ]


def test_duplicate_position(engine, server):
    cid = fid("calpos", 1)
    insert_calendar(engine, cid)
    insert_gate(engine, cid, 0, "G1", "[[10, 20]]")
    insert_gate(engine, cid, 0, "G2", "[[30, 40]]")
    base = server["base"]
    before = snapshot(engine)

    d1 = _corruption_hits(base, cid, DUP_POSITION, gates=("G1",))
    # 引用 G2 同样 500（不依赖闸门顺序挑选）
    assert _probe(base, cid, gates=("G2",)).status_code == 500
    assert _adopt(base, cid, gates=("G2",)).status_code == 500

    base2 = server["restart"]()
    assert _corruption_hits(base2, cid, DUP_POSITION, gates=("G1",)) == d1
    assert _probe(base2, cid, gates=("G2",)).status_code == 500

    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "windows_text,reason",
    [
        ("{broken", WIN_NOT_JSON),
        ("[1, 2]", WIN_SHAPE),
        ("[[1]]", WIN_SHAPE),
        ("[[1, \"2\"]]", WIN_SHAPE),
        ("[[-1, 10]]", WIN_RANGE),
        ("[[0, 1000000000001]]", WIN_RANGE),
        ("[[30, 10]]", WIN_INVERTED),
        ("[[10, 10]]", WIN_INVERTED),
        ("[[0, 10], [9, 20]]", WIN_OVERLAP),
    ],
)
def test_corrupt_or_illegal_windows(engine, server, windows_text, reason):
    cid = fid("calwin", abs(hash(windows_text)) % 10**8)
    insert_calendar(engine, cid)
    insert_gate(engine, cid, 0, "G1", windows_text)
    base = server["base"]

    before = snapshot(engine)
    d1 = assert_calendar_500(
        httpx.get(f"{base}/calendars/{cid}"), cid, reason
    )
    assert _probe(base, cid).json()["detail"]["reason"] == reason
    assert _adopt(base, cid).json()["detail"]["reason"] == reason
    # 重复请求结论一致
    assert httpx.get(f"{base}/calendars/{cid}").json()["detail"] == d1

    # 真实重启
    base2 = server["restart"]()
    d2 = assert_calendar_500(
        httpx.get(f"{base2}/calendars/{cid}"), cid, reason
    )
    assert d2 == d1
    assert _probe(base2, cid).status_code == 500
    assert _adopt(base2, cid).status_code == 500

    # 失败前后三表逐项不变（窗口文本原样保留）
    assert snapshot(engine) == before
    assert snapshot(engine)[1][0][3] == windows_text


@pytest.mark.parametrize(
    "payload_text,witnesses_text,reason",
    [
        ("not-json", "[]", PAYLOAD_NOT_JSON),
        ("{}", "[]", PAYLOAD_SHAPE),
        (
            json.dumps({"gates": ["G1"], "legs": [1], "max_waits": [1]}),
            "[]",
            PAYLOAD_SHAPE,
        ),
        (
            json.dumps(
                {"gates": ["G1", "G1"], "legs": [1], "max_waits": [1, 1]}
            ),
            "[]",
            PAYLOAD_SHAPE,
        ),
        (
            json.dumps({"gates": ["G1"], "legs": [], "max_waits": [5]}),
            "broken",
            WIT_NOT_JSON,
        ),
        ("[]", "[]", PAYLOAD_SHAPE),
        (
            json.dumps({"gates": ["G1"], "legs": [], "max_waits": [5]}),
            "[]",
            WIT_SHAPE,
        ),
        (
            json.dumps({"gates": ["G1"], "legs": [], "max_waits": [5]}),
            json.dumps(
                [{"gate_id": "GX", "arrival": 5, "entry": 5, "wait": 0}]
            ),
            WIT_SHAPE,
        ),
        (
            json.dumps({"gates": ["G1"], "legs": [], "max_waits": [5]}),
            json.dumps(
                [{"gate_id": "G1", "arrival": 5, "entry": 9, "wait": 0}]
            ),
            WIT_SHAPE,
        ),
    ],
)
def test_corrupt_plan(
    engine, server, payload_text, witnesses_text, reason
):
    cid = fid("calpln", 1)
    insert_calendar(engine, cid)
    insert_gate(engine, cid, 0, "G1", "[[0, 100]]")
    pid = fid("plndmg", abs(hash(payload_text + witnesses_text)) % 10**8)
    insert_plan(engine, pid, cid, payload_text, 5, witnesses_text)
    base = server["base"]

    before = snapshot(engine)
    d1 = assert_plan_500(httpx.get(f"{base}/plans/{pid}"), pid, reason)
    assert (
        httpx.get(f"{base}/plans/{pid}").json()["detail"] == d1
    )
    assert (
        httpx.post(f"{base}/plans/{pid}/replay/{cid}")
        .json()["detail"]["reason"]
        == reason
    )

    base2 = server["restart"]()
    d2 = assert_plan_500(httpx.get(f"{base2}/plans/{pid}"), pid, reason)
    assert d2 == d1
    assert (
        httpx.post(f"{base2}/plans/{pid}/replay/{cid}").status_code == 500
    )

    assert snapshot(engine) == before


def test_legacy_valid_records_probe_adopt_replay_across_calendars(
    engine, server
):
    """完整旧记录：与升级前一致的区间、见证，跨日历重放，且重启后仍成立。"""
    cid_a = fid("calleg", 1)
    cid_b = fid("calleg", 2)
    pid = fid("planleg", 1)
    for cid, g2 in ((cid_a, "[[25, 35]]"), (cid_b, "[[26, 35]]")):
        insert_calendar(engine, cid)
        insert_gate(engine, cid, 0, "G1", "[[10, 20]]")
        insert_gate(engine, cid, 1, "G2", g2)
    insert_plan(
        engine,
        pid,
        cid_a,
        json.dumps(
            {"gates": ["G1", "G2"], "legs": [5], "max_waits": [5, 5]}
        ),
        15,
        json.dumps(
            [
                {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
                {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
            ]
        ),
    )
    base = server["base"]
    before = snapshot(engine)

    # 详情逐字段一致、顺序稳定
    detail = httpx.get(f"{base}/calendars/{cid_a}").json()
    assert detail == {
        "id": cid_a,
        "gates": [
            {"gate_id": "G1", "windows": [[10, 20]]},
            {"gate_id": "G2", "windows": [[25, 35]]},
        ],
    }

    # 探测区间与升级前一致
    probe = httpx.post(
        f"{base}/voyages/probe",
        json={
            "calendar_id": cid_a,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "search_start": 0,
            "search_end": 50,
        },
    ).json()
    assert probe["intervals"] == [[15, 20]]

    # 旧方案见证逐字读回
    plan = httpx.get(f"{base}/plans/{pid}").json()
    assert plan["departure"] == 15
    assert plan["witnesses"] == [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
    ]

    # 跨日历重放：A->A 有效；A->B 失效，首失效闸确定
    assert (
        httpx.post(f"{base}/plans/{pid}/replay/{cid_a}").json()["status"]
        == "STILL_VALID"
    )
    invalid = httpx.post(f"{base}/plans/{pid}/replay/{cid_b}").json()
    assert invalid == {
        "status": "INVALID",
        "failed_gate_id": "G2",
        "failed_index": 1,
        "arrival": 20,
        "wait_deadline": 25,
        "reason": "WAIT_EXCEEDED",
    }

    # 合法旧日历上仍可采纳新方案（新增一条方案，日历不变）
    adopted = httpx.post(
        f"{base}/plans",
        json={
            "calendar_id": cid_a,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 16,
        },
    )
    assert adopted.status_code == 201, adopted.text
    new_pid = adopted.json()["id"]
    assert adopted.json()["witnesses"][1] == {
        "gate_id": "G2",
        "arrival": 21,
        "entry": 25,
        "wait": 4,
    }

    # 真实重启：旧记录结论不变，新方案持久化，且跨日历重放一致
    base2 = server["restart"]()
    assert httpx.get(f"{base2}/calendars/{cid_a}").json() == detail
    assert httpx.get(f"{base2}/plans/{pid}").json() == plan
    assert (
        httpx.post(f"{base2}/plans/{pid}/replay/{cid_b}").json()
        == invalid
    )
    assert httpx.get(f"{base2}/plans/{new_pid}").status_code == 200

    # 原方案/日历未被重放或采纳改写：原方案 departure 仍 15，
    # 日历窗口文本逐项等于初始，仅 plans 表多出新采纳的一条
    after = snapshot(engine)
    cals0, gates0, plans0 = before
    cals1, gates1, plans1 = after
    assert cals0 == cals1
    assert gates0 == gates1
    assert len(plans1) == len(plans0) + 1
    assert any(p[0] == pid and p[3] == 15 for p in plans1)
    old = sorted(plans0)
    kept = sorted(p for p in plans1 if p[0] != new_pid)
    assert kept == old


def test_damaged_calendar_blocks_replay_without_touching_rows(engine, server):
    """重放目标日历损坏时返回 500；方案本身合法且不被改写。"""
    good_cid = fid("calrpl", 1)
    bad_cid = fid("calrpl", 2)
    pid = fid("planrpl", 1)
    insert_calendar(engine, good_cid)
    insert_gate(engine, good_cid, 0, "G1", "[[0, 100]]")
    insert_calendar(engine, bad_cid)
    insert_gate(engine, bad_cid, 0, "G1", "[[oops]]")
    insert_plan(
        engine,
        pid,
        good_cid,
        json.dumps({"gates": ["G1"], "legs": [], "max_waits": [0]}),
        50,
        json.dumps(
            [{"gate_id": "G1", "arrival": 50, "entry": 50, "wait": 0}]
        ),
    )
    base = server["base"]
    before = snapshot(engine)

    r = httpx.post(f"{base}/plans/{pid}/replay/{bad_cid}")
    assert r.status_code == 500
    assert r.json()["detail"]["reason"] == WIN_NOT_JSON
    # 在好日历上仍有效
    assert (
        httpx.post(f"{base}/plans/{pid}/replay/{good_cid}").json()["status"]
        == "STILL_VALID"
    )

    base2 = server["restart"]()
    assert httpx.post(f"{base2}/plans/{pid}/replay/{bad_cid}").status_code == 500
    assert snapshot(engine) == before
