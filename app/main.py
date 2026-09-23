"""FastAPI 应用：潮汐闸门航程调度。"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import services
from .database import PlanRow, SessionLocal, init_db
from .integrity import CalendarCorrupt, PlanCorrupt
from .schemas import (
    CalendarIn,
    CalendarOut,
    PlanIn,
    PlanOut,
    ProbeResponse,
    ReplayResponse,
    VoyageIn,
)

logger = logging.getLogger("tide")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 数据库容器可能稍后就绪：建表前短暂重试。
    last_error: Exception | None = None
    for _ in range(30):
        try:
            init_db()
            break
        except Exception as exc:  # pragma: no cover - 仅容器启动竞态
            last_error = exc
            time.sleep(1)
    else:
        raise RuntimeError(f"数据库不可用: {last_error}")
    yield


app = FastAPI(title="潮汐闸门航程调度 API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(CalendarCorrupt)
async def calendar_corrupt_handler(
    request: Request, exc: CalendarCorrupt
) -> JSONResponse:
    """持久化日历不满足域约束：稳定可区分的 500 数据异常，绝不静默挑选。"""
    logger.error("日历数据异常 %s: %s", exc.calendar_id, exc.reason)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "error": "CORRUPT_CALENDAR_DATA",
                "calendar_id": exc.calendar_id,
                "reason": exc.reason,
                "gate_id": exc.gate_id,
                "position": exc.position,
            }
        },
    )


@app.exception_handler(PlanCorrupt)
async def plan_corrupt_handler(
    request: Request, exc: PlanCorrupt
) -> JSONResponse:
    """持久化方案的航程定义或见证损坏：稳定可区分的 500 数据异常。"""
    logger.error("方案数据异常 %s: %s", exc.plan_id, exc.reason)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "error": "CORRUPT_PLAN_DATA",
                "plan_id": exc.plan_id,
                "reason": exc.reason,
                "field": exc.field,
            }
        },
    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/calendars", response_model=CalendarOut, status_code=201)
def publish_calendar(
    payload: CalendarIn, db: Session = Depends(get_db)
) -> CalendarOut:
    cal = services.create_calendar(db, payload)
    return CalendarOut(
        id=cal.id, gates=services.validated_calendar_out(db, cal.id)
    )


@app.get("/calendars/{calendar_id}", response_model=CalendarOut)
def read_calendar(
    calendar_id: str, db: Session = Depends(get_db)
) -> CalendarOut:
    return CalendarOut(
        id=calendar_id,
        gates=services.validated_calendar_out(db, calendar_id),
    )


@app.post("/voyages/probe", response_model=ProbeResponse)
def probe_voyage(
    voyage: VoyageIn, db: Session = Depends(get_db)
) -> ProbeResponse:
    intervals = services.probe(db, voyage)
    return ProbeResponse(intervals=intervals)


@app.post("/plans", response_model=PlanOut, status_code=201)
def adopt_plan(payload: PlanIn, db: Session = Depends(get_db)) -> PlanOut:
    plan = services.adopt_plan(db, payload)
    return PlanOut(**services.plan_to_dict(plan))


@app.get("/plans/{plan_id}", response_model=PlanOut)
def read_plan(plan_id: str, db: Session = Depends(get_db)) -> PlanOut:
    plan = db.get(PlanRow, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="方案不存在")
    return PlanOut(**services.plan_to_dict(plan))


@app.post(
    "/plans/{plan_id}/replay/{new_calendar_id}",
    response_model=ReplayResponse,
)
def replay_plan(
    plan_id: str, new_calendar_id: str, db: Session = Depends(get_db)
) -> ReplayResponse:
    return ReplayResponse(
        **services.replay_plan(db, plan_id, new_calendar_id)
    )
