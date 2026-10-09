"""时钟抽象。

协议核心 :mod:`e84.fsm` 完全不接触墙钟：它只接受 :class:`Inputs` 里的 ``now``，
因此所有时序行为都可以在 :class:`VirtualClock` 上确定性地回放与断言。
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "RealClock", "VirtualClock"]


@runtime_checkable
class Clock(Protocol):
    """单调时钟协议，单位秒。"""

    def now(self) -> float:
        """返回单调递增的时间戳（秒）。"""

    def sleep(self, seconds: float) -> None:
        """等待 ``seconds`` 秒。"""


class RealClock:
    """基于 :func:`time.monotonic` 的真实时钟。"""

    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class VirtualClock:
    """可手动推进的虚拟时钟（测试与离线回放用）。

    典型用法::

        clk = VirtualClock()
        ctrl.poll(now=clk.now())
        clk.advance(2.5)          # 触发 TP1/TP2 之类的超时
        ctrl.poll(now=clk.now())
    """

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        """把虚拟时间向前推进 ``seconds`` 秒，返回新的当前时间。"""

        if seconds < 0:
            raise ValueError("虚拟时钟不能倒退")
        self._t += float(seconds)
        return self._t

    def sleep(self, seconds: float) -> None:
        """虚拟时钟下 ``sleep`` 等价于推进时间（不真正阻塞）。"""

        self.advance(seconds)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"VirtualClock(t={self._t:.6f})"
