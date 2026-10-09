"""运行器：决定 :class:`~e84.equipment.Equipment` 怎么被驱动。

两种模式，覆盖两种典型现场：

* :class:`ManualRunner` —— **由调用方决定何时推进**。适合嵌进自有实时循环、
  PLC 扫描周期、Qt/事件循环，也适合单元测试（配 :class:`~e84.clock.VirtualClock`）；
* :class:`ThreadRunner` —— 库自带后台线程按固定周期轮询，适合"接上就能跑"的场景。

两者的安全语义一致：``stop()`` 都是**幂等**的，并且都会在退出前把设备置为安全态
（撤回 ``READY``/``L_REQ``/``U_REQ``，并把 ``ES``/``HO_AVBL`` 驱动到失效安全方向）。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from e84.clock import Clock, RealClock
from e84.equipment import Equipment
from e84.events import Event, EventType

log = logging.getLogger("e84.runner")

__all__ = ["ManualRunner", "ThreadRunner"]


class ManualRunner:
    """手动轮询运行器。"""

    def __init__(self, equipment: Equipment) -> None:
        self.equipment = equipment
        self._started = False

    def start(self, now: Optional[float] = None) -> None:
        if self._started:
            return
        self.equipment.start(now)
        self._started = True

    def poll(self, now: Optional[float] = None):
        """推进一拍。

        **不会**隐式调用 ``start()``：停机之后如果还有一个残留的周期性循环在调
        ``poll()``，隐式启动会让设备悄悄恢复运行（``ES``/``HO_AVBL`` 重新变 ON，
        对外等于宣告"可以来交接了"）。所以这里直接报错，让调用方显式决定。
        """

        if not self._started:
            raise RuntimeError(
                "ManualRunner 尚未 start()。为避免设备被意外重新启用，"
                "poll() 不会隐式启动；请先调用 start()（或先前的 stop() 之后再次 start()）。"
            )
        self.equipment.poll(now)

    def stop(self, now: Optional[float] = None) -> None:
        if not self._started:
            return
        self.equipment.stop(now)
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    def __enter__(self) -> "ManualRunner":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


class ThreadRunner:
    """后台线程运行器。

    :param equipment: 设备实例
    :param poll_interval_ms: 轮询周期；默认取 ``config.poll_interval_ms``
    :param on_stall: 检测到轮询持续超期时的回调（例如触发告警）
    :param stop_timeout_s: ``stop()`` 的有界等待时间
    """

    def __init__(
        self,
        equipment: Equipment,
        *,
        poll_interval_ms: Optional[float] = None,
        on_stall: Optional[Callable[[float], None]] = None,
        stop_timeout_s: float = 5.0,
    ) -> None:
        self.equipment = equipment
        self.clock: Clock = equipment.clock
        if not isinstance(self.clock, RealClock):
            log.warning(
                "ThreadRunner 建议配真实时钟；当前时钟为 %s，"
                "虚拟时钟下线程会以最快速度空转",
                type(self.clock).__name__,
            )
        period_ms = (
            poll_interval_ms
            if poll_interval_ms is not None
            else equipment.config.poll_interval_ms
        )
        self.period_s = max(0.0005, float(period_ms) / 1000.0)
        self.on_stall = on_stall
        self.stop_timeout_s = max(0.1, float(stop_timeout_s))

        watchdog = equipment.config.watchdog
        self.stall_polls = watchdog.stall_polls if watchdog is not None else 20

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._started = False
        self._overruns = 0
        self._max_overrun_s = 0.0
        self._stall_reported = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 控制
    def start(self) -> None:
        """启动轮询线程。已在运行时幂等返回。"""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._pause.clear()
            self._overruns = 0
            self._max_overrun_s = 0.0
            self._stall_reported = False
            self.equipment.start()
            self._thread = threading.Thread(
                target=self._loop, name="e84-poller", daemon=True
            )
            self._thread.start()
            self._started = True
            log.info("轮询线程已启动（周期 %.1f ms）", self.period_s * 1000)

    def stop(self, timeout: Optional[float] = None) -> bool:
        """停止轮询线程并把设备置为安全态。幂等。返回线程是否在时限内退出。"""

        with self._lock:
            thread = self._thread
            if thread is None:
                return True
            self._stop.set()
            self._pause.clear()
        join_timeout = self.stop_timeout_s if timeout is None else timeout
        thread.join(join_timeout)
        alive = thread.is_alive()
        if alive:  # pragma: no cover - 只在极端情况下发生
            log.error("轮询线程未在 %.1fs 内退出", join_timeout)
        with self._lock:
            if not alive:
                self._thread = None
            self._started = False
        # 无论线程是否退出，都必须把输出带回安全态
        try:
            self.equipment.stop()
        except Exception:  # pragma: no cover
            log.exception("停机时进入安全态失败")
        if not alive:
            log.info("轮询线程已停止")
        return not alive

    def pause(self) -> None:
        """暂停轮询（设备保持当前输出，不会进入安全态）。"""

        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def stats(self):
        """返回轮询统计（超期次数、最大超期时长）。"""

        return {
            "overruns": self._overruns,
            "max_overrun_s": self._max_overrun_s,
            "period_s": self.period_s,
        }

    # ------------------------------------------------------------------ 循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._pause.is_set():
                self._stop.wait(self.period_s)
                continue
            t0 = self.clock.now()
            try:
                self.equipment.poll(t0)
            except Exception as exc:  # noqa: BLE001 - 轮询异常不得杀死线程
                log.exception("轮询异常")
                self.equipment.bus.emit(
                    Event(
                        type=EventType.IO_ERROR,
                        timestamp=t0,
                        message=f"轮询异常: {exc!r}",
                    )
                )
            elapsed = self.clock.now() - t0
            remaining = self.period_s - elapsed
            if remaining > 0:
                self._overruns = 0
                self._stall_reported = False
                self._stop.wait(remaining)
            else:
                self._overruns += 1
                self._max_overrun_s = max(self._max_overrun_s, elapsed)
                if self._overruns >= self.stall_polls and not self._stall_reported:
                    self._stall_reported = True
                    log.error(
                        "轮询持续超期 %d 次（单次最长 %.1f ms，周期 %.1f ms）",
                        self._overruns,
                        self._max_overrun_s * 1000,
                        self.period_s * 1000,
                    )
                    self.equipment.bus.emit(
                        Event(
                            type=EventType.WATCHDOG_STALL,
                            timestamp=self.clock.now(),
                            message=(
                                f"轮询持续超期 {self._overruns} 次，"
                                f"单次最长 {self._max_overrun_s * 1000:.1f} ms"
                            ),
                            data=self.stats,
                        )
                    )
                    if self.on_stall is not None:
                        try:
                            self.on_stall(self._max_overrun_s)
                        except Exception:  # pragma: no cover
                            log.exception("on_stall 回调异常")

    def __enter__(self) -> "ThreadRunner":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
