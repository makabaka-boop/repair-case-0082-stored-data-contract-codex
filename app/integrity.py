"""既有持久化记录的结构完整性校验。

调度员可能在服务升级或从备份恢复后继续使用升级前写入的闸门日历与采纳方案。
若持久化记录已经不满足当前域约束（重复 gate_id、重复 position、窗口 JSON
损坏、越界/倒置/重叠窗口、方案航程定义或见证损坏），任何读取路径都必须在
使用记录**之前**完成校验：

* 校验失败抛 :class:`CalendarCorrupt` / :class:`PlanCorrupt`，由路由层统一
  转写为稳定、可区分的 HTTP 500 数据异常响应；
* 绝不静默挑选其中一条记录（例如重复 gate_id 时 dict 覆盖），也不让
  ``json.loads`` / 下标取值之类的内部异常逃逸成不确定的 500；
* 校验过程纯函数、只读，结论只取决于表内数据，与行返回顺序或服务重启无关。

合法旧记录与升级前写入时逐字段一致，这里的校验对它们全部放行。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

MAX_TIME = 10**12
MAX_GATES = 200
MAX_GATE_ID_LEN = 128

# 与 schemas.ASCII_ID 保持同一域约束。
ASCII_ID = re.compile(r"^[\x21-\x7e]{1,%d}$" % MAX_GATE_ID_LEN)

# 日历原因码（稳定、可区分；可以追加新码，既有码含义不得变）。
CAL_DUP_GATE = "DUPLICATE_GATE_ID"
CAL_DUP_POSITION = "DUPLICATE_POSITION"
CAL_GATE_ID_INVALID = "GATE_ID_INVALID"
CAL_POSITION_INVALID = "POSITION_INVALID"
CAL_WIN_NOT_JSON = "GATE_WINDOWS_NOT_JSON"
CAL_WIN_SHAPE = "GATE_WINDOWS_SHAPE"  # 非二元整数区间
CAL_WIN_RANGE = "GATE_WINDOWS_OUT_OF_RANGE"
CAL_WIN_INVERTED = "GATE_WINDOW_INVERTED"
CAL_WIN_OVERLAP = "GATE_WINDOWS_OVERLAP"
CAL_NO_GATES = "CALENDAR_HAS_NO_GATES"

# 方案原因码。
PLAN_PAYLOAD_NOT_JSON = "PLAN_PAYLOAD_NOT_JSON"
PLAN_PAYLOAD_SHAPE = "PLAN_PAYLOAD_SHAPE"
PLAN_WITNESSES_NOT_JSON = "PLAN_WITNESSES_NOT_JSON"
PLAN_WITNESSES_SHAPE = "PLAN_WITNESSES_SHAPE"


def _is_int(value: object) -> bool:
    """JSON 里的布尔不是整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class ValidatedGate:
    """通过校验的一道闸门（位置、身份、二元整数窗口列表）。"""

    position: int
    gate_id: str
    windows: list[tuple[int, int]]


@dataclass(frozen=True)
class ValidatedPlan:
    """通过校验的方案快照（航程定义 + 逐闸见证）。"""

    gates: list[str]
    legs: list[int]
    max_waits: list[int]
    witnesses: list[dict]


class CalendarCorrupt(Exception):
    """日历持久化记录存在结构损坏、重复身份或顺序冲突。"""

    def __init__(
        self,
        calendar_id: str,
        reason: str,
        *,
        gate_id: str | None = None,
        position: int | None = None,
    ) -> None:
        super().__init__(f"{calendar_id}: {reason}")
        self.calendar_id = calendar_id
        self.reason = reason
        self.gate_id = gate_id
        self.position = position


class PlanCorrupt(Exception):
    """方案持久化记录的航程定义或见证损坏。"""

    def __init__(self, plan_id: str, reason: str, *, field: str | None = None):
        super().__init__(f"{plan_id}: {reason}")
        self.plan_id = plan_id
        self.reason = reason
        self.field = field


def validate_windows(
    raw: str, calendar_id: str, gate_id: str, position: int
) -> list[tuple[int, int]]:
    """把窗口 JSON 文本解析并校验为 ``[(start, end), ...]``。"""

    def fail(reason: str) -> CalendarCorrupt:
        return CalendarCorrupt(
            calendar_id, reason, gate_id=gate_id, position=position
        )

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise fail(CAL_WIN_NOT_JSON) from None
    if not isinstance(data, list) or not data:
        raise fail(CAL_WIN_SHAPE)
    prev_end: int | None = None
    for pair in data:
        if not isinstance(pair, list) or len(pair) != 2:
            raise fail(CAL_WIN_SHAPE)
        start, end = pair
        if not (_is_int(start) and _is_int(end)):
            raise fail(CAL_WIN_SHAPE)
        if not (0 <= start <= MAX_TIME and 0 <= end <= MAX_TIME):
            raise fail(CAL_WIN_RANGE)
        if start >= end:
            raise fail(CAL_WIN_INVERTED)
        if prev_end is not None and start < prev_end:
            raise fail(CAL_WIN_OVERLAP)
        prev_end = end
    return [(int(s), int(e)) for s, e in data]


def validate_calendar_rows(
    calendar_id: str, rows: list  # list[GateRow]，此处不导入 ORM 模型
) -> list[ValidatedGate]:
    """整道日历校验：返回按 position 升序的稳定闸门列表。

    rows 的返回顺序无关：内部按 (position, 行主键) 排序后再校验，
    因此重复 position 等异常结论不依赖数据库行序，重启后保持确定。
    """

    def order_key(row) -> tuple[int, int]:
        pos = row.position
        norm = pos if isinstance(pos, int) and not isinstance(pos, bool) else 0
        return (norm, row.id)

    ordered = sorted(rows, key=order_key)
    if not ordered:
        # 日历行存在却没有任何闸门：不满足"航程含 1..200 闸"的域约束。
        raise CalendarCorrupt(calendar_id, CAL_NO_GATES)

    # 1) 顺序字段、身份格式与窗口文本（按固定顺序给出第一个异常）。
    parsed: list[tuple[object, ValidatedGate]] = []
    for row in ordered:
        if (
            not isinstance(row.position, int)
            or isinstance(row.position, bool)
            or row.position < 0
        ):
            raise CalendarCorrupt(calendar_id, CAL_POSITION_INVALID)
        gate_id = row.gate_id
        if not isinstance(gate_id, str) or not ASCII_ID.fullmatch(gate_id):
            raise CalendarCorrupt(calendar_id, CAL_GATE_ID_INVALID, gate_id=gate_id)
        windows = validate_windows(
            row.windows, calendar_id, gate_id, row.position
        )
        parsed.append(
            (
                row,
                ValidatedGate(
                    position=row.position, gate_id=gate_id, windows=windows
                ),
            )
        )

    # 2) 顺序冲突：重复 position（取数值最小者，保证跨库稳定）。
    seen_pos: set[int] = set()
    dup_positions: list[int] = []
    for row, _gate in parsed:
        if row.position in seen_pos:
            dup_positions.append(row.position)
        seen_pos.add(row.position)
    if dup_positions:
        raise CalendarCorrupt(
            calendar_id, CAL_DUP_POSITION, position=min(dup_positions)
        )

    # 3) 身份冲突：重复 gate_id（按字典序取最小者）。
    seen_ids: set[str] = set()
    dup_ids: list[str] = []
    for _row, gate in parsed:
        if gate.gate_id in seen_ids:
            dup_ids.append(gate.gate_id)
        seen_ids.add(gate.gate_id)
    if dup_ids:
        raise CalendarCorrupt(
            calendar_id, CAL_DUP_GATE, gate_id=min(dup_ids)
        )

    return [gate for _row, gate in parsed]


def _payload_shape(plan_id: str, field: str | None = None) -> PlanCorrupt:
    return PlanCorrupt(plan_id, PLAN_PAYLOAD_SHAPE, field=field)


def validate_plan_snapshot(
    plan_id: str,
    raw_payload: str,
    raw_witnesses: str,
    departure: object,
) -> ValidatedPlan:
    """校验方案的航程定义、出发时刻与见证快照，返回结构化快照。"""
    if not _is_int(departure) or not (0 <= departure <= MAX_TIME):
        raise PlanCorrupt(plan_id, PLAN_PAYLOAD_SHAPE, field="departure")
    try:
        payload = json.loads(raw_payload)
    except (ValueError, TypeError):
        raise PlanCorrupt(plan_id, PLAN_PAYLOAD_NOT_JSON) from None
    if not isinstance(payload, dict):
        raise _payload_shape(plan_id)

    gates = payload.get("gates")
    legs = payload.get("legs")
    max_waits = payload.get("max_waits")

    if not isinstance(gates, list) or not (1 <= len(gates) <= MAX_GATES):
        raise _payload_shape(plan_id, "gates")
    if any(not isinstance(g, str) or not ASCII_ID.fullmatch(g) for g in gates):
        raise _payload_shape(plan_id, "gates")
    if len(set(gates)) != len(gates):
        raise _payload_shape(plan_id, "gates")
    if not isinstance(legs, list) or len(legs) != len(gates) - 1:
        raise _payload_shape(plan_id, "legs")
    if not isinstance(max_waits, list) or len(max_waits) != len(gates):
        raise _payload_shape(plan_id, "max_waits")
    if any(
        not _is_int(x) or not (0 <= x <= MAX_TIME)
        for seq in (legs, max_waits)
        for x in seq
    ):
        raise _payload_shape(plan_id, "times")

    try:
        witnesses = json.loads(raw_witnesses)
    except (ValueError, TypeError):
        raise PlanCorrupt(plan_id, PLAN_WITNESSES_NOT_JSON) from None
    if not isinstance(witnesses, list) or len(witnesses) != len(gates):
        raise PlanCorrupt(plan_id, PLAN_WITNESSES_SHAPE, field="length")
    for i, item in enumerate(witnesses):
        if not isinstance(item, dict):
            raise PlanCorrupt(plan_id, PLAN_WITNESSES_SHAPE, field=str(i))
        if item.get("gate_id") != gates[i]:
            raise PlanCorrupt(plan_id, PLAN_WITNESSES_SHAPE, field="gate_id")
        arrival = item.get("arrival")
        entry = item.get("entry")
        wait = item.get("wait")
        if not all(_is_int(v) for v in (arrival, entry, wait)):
            raise PlanCorrupt(plan_id, PLAN_WITNESSES_SHAPE, field="times")
        if not (0 <= arrival <= MAX_TIME and 0 <= entry <= MAX_TIME):
            raise PlanCorrupt(plan_id, PLAN_WITNESSES_SHAPE, field="range")
        if entry < arrival or wait != entry - arrival:
            raise PlanCorrupt(plan_id, PLAN_WITNESSES_SHAPE, field="wait")

    return ValidatedPlan(
        gates=list(gates),
        legs=list(legs),
        max_waits=list(max_waits),
        witnesses=witnesses,
    )
