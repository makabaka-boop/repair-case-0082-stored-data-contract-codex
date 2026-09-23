"""潮汐闸门调度核心算法。

确定性模型（最早可入原则）：
* 首闸到达时刻 = 出发时刻；
* 第 i 闸到达时刻 = 第 i-1 闸入闸时刻 + 相邻航行时长 legs[i-1]；
* 到达时若落在某窗口 [start, end) 内则立即入闸，否则等待下一个窗口开始；
* 恰在 end 到达视为闸门已关闭；
* 等待时长 = 入闸 - 到达，必须不超过该闸最大等待时长。

可行出发时刻集合通过从末闸向前传播"可行入闸时刻区间集"得到，
每个闸门只做一次线性双指针扫描，总复杂度 O(总窗口数 + 闸数)，
二十万窗口、二百闸门可在毫秒级完成。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

# 时间域为 [0, 10^12]，传播时用一个足够大的右端点作为末闸哨兵。
INF = 10**30


@dataclass(frozen=True)
class GateFailure:
    """前向模拟在某闸失败。"""

    gate_index: int
    arrival: int
    reason: str  # NO_OPEN_WINDOW | WAIT_EXCEEDED


@dataclass(frozen=True)
class Trace:
    """前向模拟成功后的逐闸见证数据。"""

    arrivals: list[int]
    entries: list[int]


def earliest_entry(arrival: int, starts: list[int], ends: list[int]) -> int | None:
    """按最早可入原则返回入闸时刻；无未来窗口时返回 None。

    窗口按 start 升序、互不重叠。恰在 end 到达判为关闭（左闭右开）。
    """
    j = bisect_right(starts, arrival) - 1
    if j >= 0 and arrival < ends[j]:
        return arrival  # 已在窗口内，立即入闸
    j += 1  # 等待下一个窗口开始
    if j < len(starts):
        return starts[j]
    return None


def _preimage(
    starts: list[int],
    ends: list[int],
    max_wait: int,
    downstream: list[list[int]],
) -> list[list[int]]:
    """求本闸"可行到达区间"。

    downstream 是后续航程要求的入闸时刻集合（已合并、升序、左闭右开）。
    本闸可行到达分两类片段（按到达时刻升序）：

    1. 等待片段 [max(prev_end, s-wait), s)：入闸时刻恒为窗口起点 s，
       仅当 s ∈ downstream 时整段可行（首窗之前 prev_end 视为 -∞）；
    2. 窗口内部 [s, e)：入闸时刻 = 到达时刻，可行部分为与 downstream
       的交集。

    两类片段可能首尾相接，输出时就地合并。两个指针各只前进一次，
    交集输出总数不超过窗口数与 downstream 区间数之和。
    """
    out: list[list[int]] = []

    def emit(lo: int, hi: int) -> None:
        if lo >= hi:
            return
        if out and lo <= out[-1][1]:  # 相接或重叠 -> 合并
            if hi > out[-1][1]:
                out[-1][1] = hi
        else:
            out.append([lo, hi])

    k = 0  # 窗口内部交集扫描指针
    m = 0  # 等待片段常量目标 membership 扫描指针
    size = len(downstream)

    for j in range(len(starts)):
        s, e = starts[j], ends[j]

        # 等待进入窗口 j 的片段
        gap_lo = s - max_wait if j == 0 else max(ends[j - 1], s - max_wait)
        if gap_lo < s:
            while m < size and downstream[m][1] <= s:
                m += 1
            if m < size and downstream[m][0] <= s < downstream[m][1]:
                emit(gap_lo, s)

        # 窗口内部：入闸 == 到达，直接与 downstream 求交
        while k < size and downstream[k][1] <= s:
            k += 1
        q = k
        while q < size and downstream[q][0] < e:
            emit(max(s, downstream[q][0]), min(e, downstream[q][1]))
            q += 1

    return out


def feasible_departures(
    legs: list[int],
    waits: list[int],
    gate_windows: list[tuple[list[int], list[int]]],
    search: tuple[int, int],
) -> list[list[int]]:
    """返回出发搜索区间内全部可行出发时刻的合并区间（左闭右开）。"""
    n = len(waits)
    # 末闸之后：任何入闸时刻都被接受（哨兵区间）。
    values: list[list[int]] = [[0, INF]]

    for i in range(n - 1, -1, -1):
        starts, ends = gate_windows[i]
        if not starts:
            return []  # 该闸永不开放
        values = _preimage(starts, ends, waits[i], values)
        if not values:
            return []
        if i > 0:
            # 到达本闸 = 上一闸入闸 + legs[i-1]，平移到上一闸的入闸时刻域。
            d = legs[i - 1]
            values = [[lo - d, hi - d] for lo, hi in values]

    # 此时 values 即首闸可行到达域，也就是可行出发时刻域。
    slo, shi = search
    result: list[list[int]] = []
    for lo, hi in values:
        lo = max(lo, slo)
        hi = min(hi, shi)
        if lo < hi:
            if result and lo <= result[-1][1]:
                if hi > result[-1][1]:
                    result[-1][1] = hi
            else:
                result.append([lo, hi])
    return result


def forward_trace(
    departure: int,
    legs: list[int],
    waits: list[int],
    gate_windows: list[tuple[list[int], list[int]]],
) -> Trace | GateFailure:
    """按最早可入原则逐闸模拟；返回见证 Trace 或首个失败 GateFailure。"""
    arrivals: list[int] = []
    entries: list[int] = []
    arrival = departure

    for i in range(len(waits)):
        starts, ends = gate_windows[i]
        entry = earliest_entry(arrival, starts, ends) if starts else None
        if entry is None:
            return GateFailure(i, arrival, "NO_OPEN_WINDOW")
        if entry - arrival > waits[i]:
            return GateFailure(i, arrival, "WAIT_EXCEEDED")
        arrivals.append(arrival)
        entries.append(entry)
        if i < len(waits) - 1:
            arrival = entry + legs[i]

    return Trace(arrivals, entries)
