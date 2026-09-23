"""领域服务：日历落库、可行性探测、方案采纳与重放。

读取既有记录（升级/恢复前写入）时一律先经 :mod:`app.integrity` 校验：
合法旧记录行为与升级前完全一致；结构损坏、重复身份或顺序冲突抛出
``CalendarCorrupt`` / ``PlanCorrupt``，由路由层转成稳定的 500 数据异常，
绝不静默挑选记录，也不在任何失败路径上新增或改写数据。
"""
from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import integrity, scheduling
from .database import CalendarRow, GateRow, PlanRow
from .schemas import CalendarIn, PlanIn, VoyageIn


def _new_id() -> str:
    return uuid.uuid4().hex


def create_calendar(db: Session, payload: CalendarIn) -> CalendarRow:
    """整版原子写入：请求体已由 schema 整版校验，任一异常则全部不落库。"""
    cal = CalendarRow(id=_new_id())
    db.add(cal)
    for pos, gate in enumerate(payload.gates):
        db.add(
            GateRow(
                calendar_id=cal.id,
                position=pos,
                gate_id=gate.gate_id,
                windows=json.dumps([list(w) for w in gate.windows]),
            )
        )
    db.commit()
    db.refresh(cal)
    return cal


def get_calendar(db: Session, calendar_id: str) -> CalendarRow:
    cal = db.get(CalendarRow, calendar_id)
    if cal is None:
        raise HTTPException(status_code=404, detail="日历不存在")
    return cal


def _load_validated_gates(
    db: Session, calendar_id: str
) -> list[integrity.ValidatedGate]:
    """日历存在性（404）之后做整日历结构校验（损坏 -> CalendarCorrupt）。"""
    get_calendar(db, calendar_id)
    rows = db.scalars(
        select(GateRow).where(GateRow.calendar_id == calendar_id)
    ).all()
    return integrity.validate_calendar_rows(calendar_id, list(rows))


def _window_arrays(
    gates: list[integrity.ValidatedGate], gate_ids: list[str]
) -> list[tuple[list[int], list[int]]]:
    by_id = {g.gate_id: g for g in gates}
    windows_by_gate: list[tuple[list[int], list[int]]] = []
    for gid in gate_ids:
        gate = by_id.get(gid)
        if gate is None:
            # 日历本身合法，仅是本次航程引用了不存在的闸门 -> 422（既有语义）。
            raise HTTPException(status_code=422, detail=f"闸门 {gid!r} 不在日历中")
        starts = [s for s, _e in gate.windows]
        ends = [e for _s, e in gate.windows]
        windows_by_gate.append((starts, ends))
    return windows_by_gate


def validated_calendar_out(
    db: Session, calendar_id: str
) -> list[dict]:
    """供日历详情使用：校验后按 position 稳定输出。"""
    gates = _load_validated_gates(db, calendar_id)
    return [
        {"gate_id": g.gate_id, "windows": [[s, e] for s, e in g.windows]}
        for g in gates
    ]


def probe(db: Session, voyage: VoyageIn) -> list[list[int]]:
    gates = _load_validated_gates(db, voyage.calendar_id)
    windows_by_gate = _window_arrays(gates, voyage.gates)
    return scheduling.feasible_departures(
        list(voyage.legs),
        list(voyage.max_waits),
        windows_by_gate,
        (voyage.search_start, voyage.search_end),
    )


def _witnesses(
    gate_ids: list[str], trace: scheduling.Trace
) -> list[dict]:
    return [
        {
            "gate_id": gate_ids[i],
            "arrival": trace.arrivals[i],
            "entry": trace.entries[i],
            "wait": trace.entries[i] - trace.arrivals[i],
        }
        for i in range(len(gate_ids))
    ]


def adopt_plan(db: Session, payload: PlanIn) -> PlanRow:
    gates = _load_validated_gates(db, payload.calendar_id)
    windows_by_gate = _window_arrays(gates, payload.gates)
    result = scheduling.forward_trace(
        payload.departure,
        list(payload.legs),
        list(payload.max_waits),
        windows_by_gate,
    )
    if isinstance(result, scheduling.GateFailure):
        deadline = result.arrival + payload.max_waits[result.gate_index]
        raise HTTPException(
            status_code=409,
            detail={
                "error": "DEPARTURE_INFEASIBLE",
                "failed_gate_id": payload.gates[result.gate_index],
                "failed_index": result.gate_index,
                "arrival": result.arrival,
                "wait_deadline": deadline,
                "reason": result.reason,
            },
        )

    witnesses = _witnesses(payload.gates, result)
    plan = PlanRow(
        id=_new_id(),
        calendar_id=payload.calendar_id,
        payload=json.dumps(
            {
                "gates": payload.gates,
                "legs": list(payload.legs),
                "max_waits": list(payload.max_waits),
            }
        ),
        departure=payload.departure,
        witnesses=json.dumps(witnesses),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _load_validated_plan(db: Session, plan_id: str) -> tuple[PlanRow, integrity.ValidatedPlan]:
    plan = db.get(PlanRow, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="方案不存在")
    snapshot = integrity.validate_plan_snapshot(
        plan_id, plan.payload, plan.witnesses, plan.departure
    )
    return plan, snapshot


def replay_plan(
    db: Session, plan_id: str, new_calendar_id: str
) -> dict:
    """在新日历上重放方案；只读，原方案不改写。

    方案快照先校验（损坏 -> PlanCorrupt 500），再加载并校验新日历
    （损坏 -> CalendarCorrupt 500）；结论确定，与行序、重启无关。
    """
    plan, snapshot = _load_validated_plan(db, plan_id)
    gates = _load_validated_gates(db, new_calendar_id)

    gate_ids = snapshot.gates
    by_id = {g.gate_id: g for g in gates}
    windows_by_gate: list[tuple[list[int], list[int]]] = []
    for i, gid in enumerate(gate_ids):
        gate = by_id.get(gid)
        if gate is None:
            return {
                "status": "INVALID",
                "failed_gate_id": gid,
                "failed_index": i,
                "arrival": None,
                "wait_deadline": None,
                "reason": "GATE_NOT_IN_CALENDAR",
            }
        windows_by_gate.append(
            ([s for s, _e in gate.windows], [e for _s, e in gate.windows])
        )

    result = scheduling.forward_trace(
        plan.departure,
        snapshot.legs,
        snapshot.max_waits,
        windows_by_gate,
    )
    if isinstance(result, scheduling.Trace):
        return {
            "status": "STILL_VALID",
            "failed_gate_id": None,
            "failed_index": None,
            "arrival": None,
            "wait_deadline": None,
            "reason": None,
        }

    i = result.gate_index
    return {
        "status": "INVALID",
        "failed_gate_id": gate_ids[i],
        "failed_index": i,
        "arrival": result.arrival,
        "wait_deadline": result.arrival + snapshot.max_waits[i],
        "reason": result.reason,
    }


def plan_to_dict(plan: PlanRow) -> dict:
    snapshot = integrity.validate_plan_snapshot(
        plan.id, plan.payload, plan.witnesses, plan.departure
    )
    return {
        "id": plan.id,
        "calendar_id": plan.calendar_id,
        "gates": snapshot.gates,
        "legs": snapshot.legs,
        "max_waits": snapshot.max_waits,
        "departure": plan.departure,
        "witnesses": snapshot.witnesses,
    }
