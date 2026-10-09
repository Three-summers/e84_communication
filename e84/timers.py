"""E84 定时器。

标准要点：

* §6.3.2.1 —— ``TAx`` 为主动设备定时器，``TPx`` 为被动设备定时器；
  **除 ``TD0`` 外**所有定时器取值范围 1–999 s，且**设定值都应可由用户编程**；
* 表 5 —— 主动侧 ``TA1``/``TA2``/``TA3``（典型 2 s）；
* 表 6 —— 被动侧 ``TP1``…``TP6``（``TP3``/``TP4`` 典型 **60 s**，其余 2 s）；
* 表 7 —— 延时定时器 ``TD0``（0.1–0.2 s，主动侧「选口 → VALID」）、
  ``TD1``（1–999 s，「VALID OFF → VALID ON」，用于连续交接）。

实现上使用**绝对截止时刻**而非累加，避免轮询抖动被累积放大。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple

__all__ = [
    "TimerId",
    "TimerSpec",
    "TIMER_SPECS",
    "PASSIVE_TIMERS",
    "ACTIVE_TIMERS",
    "TimerSet",
]


class TimerId(str, Enum):
    """全部 11 个 E84 定时器。"""

    # 被动设备（本库核心使用）
    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"
    TP4 = "TP4"
    TP5 = "TP5"
    TP6 = "TP6"
    # 主动设备（主动侧参考实现使用）
    TA1 = "TA1"
    TA2 = "TA2"
    TA3 = "TA3"
    # 延时定时器
    TD0 = "TD0"
    TD1 = "TD1"


@dataclass(frozen=True)
class TimerSpec:
    """定时器的静态规格：监视区间、取值范围、典型值、归属方。"""

    timer_id: TimerId
    owner: str  # "passive" | "active"
    interval: str
    minimum_s: float
    maximum_s: float
    typical_s: float
    clause: str

    def validate(self, value: float) -> None:
        """校验用户设定值是否落在标准允许范围内。"""

        if not (self.minimum_s - 1e-9 <= value <= self.maximum_s + 1e-9):
            raise ValueError(
                f"{self.timer_id.value}={value}s 超出标准范围 "
                f"[{self.minimum_s}, {self.maximum_s}]s（{self.clause}）"
            )


TIMER_SPECS: Mapping[TimerId, TimerSpec] = {
    TimerId.TP1: TimerSpec(
        TimerId.TP1, "passive",
        "L_REQ ON — TR_REQ ON / U_REQ ON — TR_REQ ON",
        1, 999, 2, "6.3.2.1 表6",
    ),
    TimerId.TP2: TimerSpec(
        TimerId.TP2, "passive", "READY ON — BUSY ON", 1, 999, 2, "6.3.2.1 表6"
    ),
    TimerId.TP3: TimerSpec(
        TimerId.TP3, "passive",
        "BUSY ON — 载具到位 / 载具被取走", 1, 999, 60, "6.3.2.1 表6",
    ),
    TimerId.TP4: TimerSpec(
        TimerId.TP4, "passive",
        "L_REQ OFF — BUSY OFF / U_REQ OFF — BUSY OFF", 1, 999, 60, "6.3.2.1 表6",
    ),
    TimerId.TP5: TimerSpec(
        TimerId.TP5, "passive", "READY OFF — VALID OFF", 1, 999, 2, "6.3.2.1 表6"
    ),
    TimerId.TP6: TimerSpec(
        TimerId.TP6, "passive", "VALID OFF — VALID ON（连续交接）",
        1, 999, 2, "6.3.2.1 表6",
    ),
    TimerId.TA1: TimerSpec(
        TimerId.TA1, "active",
        "VALID ON — L_REQ ON / VALID ON — U_REQ ON", 1, 999, 2, "6.3.2.1 表5",
    ),
    TimerId.TA2: TimerSpec(
        TimerId.TA2, "active", "TR_REQ ON — READY ON", 1, 999, 2, "6.3.2.1 表5"
    ),
    TimerId.TA3: TimerSpec(
        TimerId.TA3, "active", "COMPT ON — READY OFF", 1, 999, 2, "6.3.2.1 表5"
    ),
    TimerId.TD0: TimerSpec(
        TimerId.TD0, "active", "CS ON — VALID ON", 0.1, 0.2, 0.1, "6.3.2.3 表7"
    ),
    TimerId.TD1: TimerSpec(
        TimerId.TD1, "active", "VALID OFF — VALID ON", 1, 999, 1, "6.3.2.2 表7"
    ),
}

#: 被动侧需要监视的定时器（本库核心）。``TD1`` 以「监视」模式可选启用。
PASSIVE_TIMERS: Tuple[TimerId, ...] = (
    TimerId.TP1,
    TimerId.TP2,
    TimerId.TP3,
    TimerId.TP4,
    TimerId.TP5,
    TimerId.TP6,
)

ACTIVE_TIMERS: Tuple[TimerId, ...] = (TimerId.TA1, TimerId.TA2, TimerId.TA3, TimerId.TD0, TimerId.TD1)


class TimerSet:
    """一组可编程的 E84 定时器。

    典型用法::

        timers = TimerSet.from_typical(owner="passive")
        timers.start(TimerId.TP1, now=0.0)
        timers.expired(TimerId.TP1, now=2.5)   # -> True
    """

    __slots__ = ("_durations", "_deadlines", "_fired")

    def __init__(self, durations: Mapping[TimerId, float]) -> None:
        self._durations: Dict[TimerId, float] = {}
        for tid, value in durations.items():
            TIMER_SPECS[tid].validate(float(value))
            self._durations[tid] = float(value)
        self._deadlines: Dict[TimerId, float] = {}
        self._fired: Dict[TimerId, float] = {}

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_typical(
        cls, owner: Optional[str] = None, overrides: Optional[Mapping[TimerId, float]] = None
    ) -> "TimerSet":
        """按标准典型值构造。``owner="passive"`` 只含 ``TPx``。"""

        durations: Dict[TimerId, float] = {}
        for tid, spec in TIMER_SPECS.items():
            if owner is not None and spec.owner != owner:
                continue
            durations[tid] = spec.typical_s
        if overrides:
            durations.update({k: float(v) for k, v in overrides.items()})
        return cls(durations)

    # ------------------------------------------------------------ 运行时编程
    def set_duration(self, timer_id: TimerId, seconds: float) -> None:
        """运行时修改定时器设定值（§6.3.2.1：所有设定值应可由用户编程）。"""

        TIMER_SPECS[timer_id].validate(float(seconds))
        self._durations[timer_id] = float(seconds)

    @property
    def durations(self) -> Mapping[TimerId, float]:
        return dict(self._durations)

    def duration_of(self, timer_id: TimerId) -> float:
        """返回定时器当前的设定值（秒）。"""

        return self._durations[timer_id]

    # ------------------------------------------------------------------ 操作
    def start(self, timer_id: TimerId, now: float) -> None:
        """启动定时器（重复启动会重新计时）。"""

        self._deadlines[timer_id] = now + self._durations[timer_id]
        self._fired.pop(timer_id, None)

    def stop(self, timer_id: TimerId) -> None:
        self._deadlines.pop(timer_id, None)
        self._fired.pop(timer_id, None)

    def stop_all(self) -> None:
        self._deadlines.clear()
        self._fired.clear()

    def running(self, timer_id: TimerId) -> bool:
        return timer_id in self._deadlines

    def remaining(self, timer_id: TimerId, now: float) -> Optional[float]:
        """剩余秒数；未运行时返回 ``None``。已到期返回 ``0.0``。"""

        deadline = self._deadlines.get(timer_id)
        if deadline is None:
            return None
        return max(0.0, deadline - now)

    def expired(self, timer_id: TimerId, now: float) -> bool:
        """是否已到期。到期后自动注销，保证每个定时器只触发一次。"""

        deadline = self._deadlines.get(timer_id)
        if deadline is None:
            return False
        if now >= deadline:
            self._deadlines.pop(timer_id, None)
            self._fired[timer_id] = now
            return True
        return False

    def due(self, now: float) -> Tuple[TimerId, ...]:
        """返回并注销所有已到期的定时器（按到期先后排序）。"""

        ready = sorted(
            (tid for tid, deadline in self._deadlines.items() if now >= deadline),
            key=lambda tid: self._deadlines[tid],
        )
        for tid in ready:
            self._deadlines.pop(tid, None)
            self._fired[tid] = now
        return tuple(ready)

    def snapshot(self, now: float) -> Dict[str, Optional[float]]:
        """供监视器/日志使用的只读快照：``{定时器名: 剩余秒数}``。"""

        return {
            tid.value: self.remaining(tid, now) for tid in self._durations
        }
