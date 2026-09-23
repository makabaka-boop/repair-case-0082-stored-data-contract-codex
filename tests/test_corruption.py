"""升级/恢复后既有记录异常：结构损坏、重复身份、顺序冲突。

这些用例直接以 ORM 预置"旧记录"（绕过请求层校验），验证：
* 日历详情、探测、采纳、方案查询、重放对损坏记录一律返回稳定可区分的
  HTTP 500 数据异常（detail.reason 可区分），不静默挑选记录；
* 失败前后 calendars / gates / plans 三张表逐项不变；
* 重复查询结论一致；
* 合法旧记录仍可探测、采纳、跨日历重放，且字段与写入时逐字一致。
真实 PostgreSQL 上的同套验收见 tests_pg/。
"""
from __future__ import annotations

import json

import pytest

from app import database
from app.database import CalendarRow, GateRow, PlanRow
from app.integrity import (
    CAL_DUP_GATE,
    CAL_DUP_POSITION,
    CAL_WIN_INVERTED,
    CAL_WIN_NOT_JSON,
    CAL_WIN_OVERLAP,
    CAL_WIN_RANGE,
    CAL_WIN_SHAPE,
    PLAN_PAYLOAD_NOT_JSON,
    PLAN_PAYLOAD_SHAPE,
    PLAN_WITNESSES_NOT_JSON,
    PLAN_WITNESSES_SHAPE,
)

CAL_ID = "caldamaged0000000000000000000a"
CAL2_ID = "caldamaged0000000000000000000b"
PLAN_ID = "plandamaged00000000000000000001"
GOOD_CAL = "calgood0000000000000000000001"
GOOD_PLAN = "plangood0000000000000000000001"


def _session():
    db = database.SessionLocal()
    db.add(CalendarRow(id=CAL_ID))
    return db


def _snapshot(db):
    from sqlalchemy import select

    calendars = sorted(x.id for x in db.scalars(select(CalendarRow)).all())
    gates = sorted(
        (x.calendar_id, x.id, x.position, x.gate_id, x.windows)
        for x in db.scalars(select(GateRow)).all()
    )
    plans = sorted(
        (x.id, x.calendar_id, x.payload, x.departure, x.witnesses)
        for x in db.scalars(select(PlanRow)).all()
    )
    return calendars, gates, plans


def _seed_gate(db, position, gate_id, windows_text, calendar_id=CAL_ID, rid=None):
    row = GateRow(
        calendar_id=calendar_id,
        position=position,
        gate_id=gate_id,
        windows=windows_text,
    )
    if rid is not None:
        row.id = rid
    db.add(row)


def _assert_calendar_500(resp, reason):
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "CORRUPT_CALENDAR_DATA"
    assert detail["calendar_id"] == CAL_ID
    assert detail["reason"] == reason
    return detail


def _voyage_body(calendar_id=CAL_ID, gates=("G1",)):
    n = len(gates)
    return {
        "calendar_id": calendar_id,
        "gates": list(gates),
        "legs": [1] * (n - 1),
        "max_waits": [100] * n,
        "search_start": 0,
        "search_end": 1000,
    }


@pytest.mark.parametrize(
    "windows_text,reason",
    [
        ("{not json", CAL_WIN_NOT_JSON),
        ("[1, 2]", CAL_WIN_SHAPE),  # 不是二元区间数组
        ("[[1]]", CAL_WIN_SHAPE),
        ("[[1, true]]", CAL_WIN_SHAPE),  # 布尔不是整数
        ("[[-1, 10]]", CAL_WIN_RANGE),
        ("[[0, 1000000000001]]", CAL_WIN_RANGE),
        ("[[20, 10]]", CAL_WIN_INVERTED),
        ("[[10, 10]]", CAL_WIN_INVERTED),
        ("[[0, 10], [9, 20]]", CAL_WIN_OVERLAP),
    ],
)
def test_corrupt_windows_rejected_everywhere(client, windows_text, reason):
    db = _session()
    _seed_gate(db, 0, "G1", windows_text)
    db.commit()
    before = _snapshot(db)

    # 详情、探测、采纳都给出同一个可区分异常
    r1 = client.get(f"/calendars/{CAL_ID}")
    d1 = _assert_calendar_500(r1, reason)
    assert client.get(f"/calendars/{CAL_ID}").json()["detail"] == d1  # 重复查询确定

    r2 = client.post("/voyages/probe", json=_voyage_body())
    assert r2.status_code == 500
    assert r2.json()["detail"]["reason"] == reason

    r3 = client.post(
        "/plans",
        json={
            "calendar_id": CAL_ID,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [100],
            "departure": 5,
        },
    )
    assert r3.status_code == 500
    assert r3.json()["detail"]["reason"] == reason

    db.rollback()
    assert _snapshot(db) == before  # 失败前后表内容逐项不变


def test_duplicate_gate_id_never_silently_picked(client):
    db = _session()
    # 同名两闸、不同窗口：详情会同时出现两项；探测/采纳旧实现只会挑一项。
    _seed_gate(db, 0, "G1", "[[10, 20]]", rid=1)
    _seed_gate(db, 1, "G1", "[[90, 100]]", rid=2)
    db.commit()
    before = _snapshot(db)

    detail = _assert_calendar_500(
        client.get(f"/calendars/{CAL_ID}"), CAL_DUP_GATE
    )
    assert detail["gate_id"] == "G1"
    assert client.get(f"/calendars/{CAL_ID}").json()["detail"] == detail

    assert (
        client.post("/voyages/probe", json=_voyage_body()).status_code == 500
    )
    adopt = client.post(
        "/plans",
        json={
            "calendar_id": CAL_ID,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [5],
            "departure": 15,
        },
    )
    assert adopt.status_code == 500
    assert adopt.json()["detail"]["reason"] == CAL_DUP_GATE

    db.rollback()
    assert _snapshot(db) == before


def test_duplicate_position_is_unstable_order_error(client):
    db = _session()
    _seed_gate(db, 0, "G1", "[[10, 20]]", rid=1)
    _seed_gate(db, 0, "G2", "[[30, 40]]", rid=2)
    db.commit()
    before = _snapshot(db)

    detail = _assert_calendar_500(
        client.get(f"/calendars/{CAL_ID}"), CAL_DUP_POSITION
    )
    assert detail["position"] == 0
    assert client.post("/voyages/probe", json=_voyage_body(gates=("G1",))).status_code == 500
    assert client.post("/voyages/probe", json=_voyage_body(gates=("G2",))).status_code == 500

    db.rollback()
    assert _snapshot(db) == before


def _seed_good_calendar(db, calendar_id=GOOD_CAL):
    db.add(CalendarRow(id=calendar_id))
    db.add(
        GateRow(
            calendar_id=calendar_id,
            position=0,
            gate_id="G1",
            windows="[[10, 20]]",
        )
    )


@pytest.mark.parametrize(
    "payload_text,witnesses_text,reason",
    [
        ("not json", "[]", PLAN_PAYLOAD_NOT_JSON),
        ("{}", "[]", PLAN_PAYLOAD_SHAPE),
        (
            json.dumps({"gates": ["G1"], "legs": [1], "max_waits": [1]}),
            "[]",
            PLAN_PAYLOAD_SHAPE,
        ),
        (
            json.dumps({"gates": ["G1", "G1"], "legs": [1], "max_waits": [1, 1]}),
            "[]",
            PLAN_PAYLOAD_SHAPE,
        ),
        (
            json.dumps({"gates": ["G1"], "legs": [], "max_waits": [5]}),
            "broken",
            PLAN_WITNESSES_NOT_JSON,
        ),
        (
            json.dumps({"gates": ["G1"], "legs": [], "max_waits": [5]}),
            "[]",
            PLAN_WITNESSES_SHAPE,
        ),
        (
            json.dumps({"gates": ["G1"], "legs": [], "max_waits": [5]}),
            json.dumps(
                [{"gate_id": "G2", "arrival": 15, "entry": 15, "wait": 0}]
            ),
            PLAN_WITNESSES_SHAPE,
        ),
        (
            json.dumps({"gates": ["G1"], "legs": [], "max_waits": [5]}),
            # wait 与 entry-arrival 不一致：见证损坏
            json.dumps(
                [{"gate_id": "G1", "arrival": 15, "entry": 18, "wait": 0}]
            ),
            PLAN_WITNESSES_SHAPE,
        ),
    ],
)
def test_corrupt_plan_rejected_on_read_and_replay(
    client, payload_text, witnesses_text, reason
):
    db = database.SessionLocal()
    _seed_good_calendar(db)
    db.add(
        PlanRow(
            id=PLAN_ID,
            calendar_id=GOOD_CAL,
            payload=payload_text,
            departure=15,
            witnesses=witnesses_text,
        )
    )
    db.commit()
    before = _snapshot(db)

    r = client.get(f"/plans/{PLAN_ID}")
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "CORRUPT_PLAN_DATA"
    assert detail["plan_id"] == PLAN_ID
    assert detail["reason"] == reason
    assert client.get(f"/plans/{PLAN_ID}").json()["detail"] == detail

    r = client.post(f"/plans/{PLAN_ID}/replay/{GOOD_CAL}")
    assert r.status_code == 500
    assert r.json()["detail"]["reason"] == reason

    db.rollback()
    assert _snapshot(db) == before


def test_legacy_records_keep_pre_upgrade_behavior(client):
    """完整合法旧记录：区间、见证、跨日历重放与升级前一致。"""
    db = database.SessionLocal()
    # 旧日历 A：G1 [10,20)，G2 [25,35)
    db.add(CalendarRow(id=GOOD_CAL))
    db.add_all(
        [
            GateRow(calendar_id=GOOD_CAL, position=0, gate_id="G1",
                    windows="[[10, 20]]"),
            GateRow(calendar_id=GOOD_CAL, position=1, gate_id="G2",
                    windows="[[25, 35]]"),
        ]
    )
    # 旧日历 B：G2 右移到 26
    db.add(CalendarRow(id=CAL2_ID))
    db.add_all(
        [
            GateRow(calendar_id=CAL2_ID, position=0, gate_id="G1",
                    windows="[[10, 20]]"),
            GateRow(calendar_id=CAL2_ID, position=1, gate_id="G2",
                    windows="[[26, 35]]"),
        ]
    )
    # 旧方案：出发 15，见证 G1(15,15,0) G2(20,25,5)
    db.add(
        PlanRow(
            id=GOOD_PLAN,
            calendar_id=GOOD_CAL,
            payload=json.dumps(
                {"gates": ["G1", "G2"], "legs": [5], "max_waits": [5, 5]}
            ),
            departure=15,
            witnesses=json.dumps(
                [
                    {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
                    {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
                ]
            ),
        )
    )
    db.commit()

    # 详情逐字段一致且顺序稳定
    detail = client.get(f"/calendars/{GOOD_CAL}").json()
    assert detail == {
        "id": GOOD_CAL,
        "gates": [
            {"gate_id": "G1", "windows": [[10, 20]]},
            {"gate_id": "G2", "windows": [[25, 35]]},
        ],
    }
    assert client.get(f"/calendars/{GOOD_CAL}").json() == detail

    # 探测区间与升级前一致
    probe = client.post(
        "/voyages/probe",
        json={
            "calendar_id": GOOD_CAL,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "search_start": 0,
            "search_end": 50,
        },
    )
    assert probe.status_code == 200
    assert probe.json()["intervals"] == [[15, 20]]

    # 旧方案见证逐字读回
    plan = client.get(f"/plans/{GOOD_PLAN}").json()
    assert plan["departure"] == 15
    assert plan["witnesses"] == [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
    ]

    # 跨日历重放：A->A 有效；A->B 失效且首失效闸确定
    assert (
        client.post(f"/plans/{GOOD_PLAN}/replay/{GOOD_CAL}").json()["status"]
        == "STILL_VALID"
    )
    body = client.post(f"/plans/{GOOD_PLAN}/replay/{CAL2_ID}").json()
    assert body == {
        "status": "INVALID",
        "failed_gate_id": "G2",
        "failed_index": 1,
        "arrival": 20,
        "wait_deadline": 25,
        "reason": "WAIT_EXCEEDED",
    }

    # 合法旧日历上仍可采纳新方案
    adopted = client.post(
        "/plans",
        json={
            "calendar_id": GOOD_CAL,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 16,
        },
    )
    assert adopted.status_code == 201, adopted.text
    assert adopted.json()["witnesses"][1]["entry"] == 25

    # 重放不改写原方案与日历
    again = client.get(f"/plans/{GOOD_PLAN}").json()
    assert again["departure"] == 15 and again["witnesses"] == plan["witnesses"]
