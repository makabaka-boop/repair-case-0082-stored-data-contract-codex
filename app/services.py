"""领域服务：日历落库、可行性探测、方案采纳与重放。"""
from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import scheduling
from .database import CalendarRow, GateRow, PlanRow
from .schemas import CalendarIn, PlanIn, VoyageIn


def _new_id() -> str:
    return uuid.uuid4().hex


def create_calendar(db: Session, payload: CalendarIn) -> CalendarRow:
    """整版原子写入：任一异常则全部不落库。"""
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


def _load_gate_map(db: Session, calendar_id: str) -> dict[str, GateRow]:
    get_calendar(db, calendar_id)
    rows = db.scalars(
        select(GateRow).where(GateRow.calendar_id == calendar_id)
    ).all()
    return {r.gate_id: r for r in rows}


def _window_arrays(
    gate_map: dict[str, GateRow], gate_ids: list[str]
) -> list[tuple[list[int], list[int]]]:
    windows_by_gate: list[tuple[list[int], list[int]]] = []
    for gid in gate_ids:
        row = gate_map.get(gid)
        if row is None:
            raise HTTPException(status_code=422, detail=f"闸门 {gid!r} 不在日历中")
        windows = json.loads(row.windows)
        starts = [w[0] for w in windows]
        ends = [w[1] for w in windows]
        windows_by_gate.append((starts, ends))
    return windows_by_gate


def probe(db: Session, voyage: VoyageIn) -> list[list[int]]:
    gate_map = _load_gate_map(db, voyage.calendar_id)
    windows_by_gate = _window_arrays(gate_map, voyage.gates)
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
    gate_map = _load_gate_map(db, payload.calendar_id)
    windows_by_gate = _window_arrays(gate_map, payload.gates)
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


def replay_plan(
    db: Session, plan_id: str, new_calendar_id: str
) -> dict:
    """在新日历上重放方案；只读，原方案不改写。"""
    plan = db.get(PlanRow, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="方案不存在")
    gate_map = _load_gate_map(db, new_calendar_id)

    definition = json.loads(plan.payload)
    gate_ids: list[str] = definition["gates"]
    windows_by_gate: list[tuple[list[int], list[int]]] = []
    for i, gid in enumerate(gate_ids):
        row = gate_map.get(gid)
        if row is None:
            return {
                "status": "INVALID",
                "failed_gate_id": gid,
                "failed_index": i,
                "arrival": None,
                "wait_deadline": None,
                "reason": "GATE_NOT_IN_CALENDAR",
            }
        windows = json.loads(row.windows)
        windows_by_gate.append(
            ([w[0] for w in windows], [w[1] for w in windows])
        )

    result = scheduling.forward_trace(
        plan.departure,
        definition["legs"],
        definition["max_waits"],
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
        "wait_deadline": result.arrival + definition["max_waits"][i],
        "reason": result.reason,
    }


def plan_to_dict(plan: PlanRow) -> dict:
    definition = json.loads(plan.payload)
    return {
        "id": plan.id,
        "calendar_id": plan.calendar_id,
        "gates": definition["gates"],
        "legs": definition["legs"],
        "max_waits": definition["max_waits"],
        "departure": plan.departure,
        "witnesses": json.loads(plan.witnesses),
    }
