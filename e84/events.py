"""事件模型与事件总线。

协议核心不打印日志、不调用回调，只**产出事件**；由 :mod:`e84.controller`
交给 :class:`EventBus` 分发给上层（日志、告警、上位机、机构控制）。

每个事件都带 ``clause`` 字段（标准条款号），便于审计与现场定位。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

__all__ = ["EventType", "Event", "EventBus", "EVENT_CLAUSE"]

log = logging.getLogger("e84.events")


class EventType(str, Enum):
    """被动侧可观测的事件。"""

    STATE_CHANGED = "state_changed"
    ENABLED = "enabled"
    DISABLED = "disabled"

    HANDSHAKE_STARTED = "handshake_started"
    PORTS_SELECTED = "ports_selected"
    DEMAND_ASSERTED = "demand_asserted"
    DEMAND_RELEASED = "demand_released"
    READY_ASSERTED = "ready_asserted"
    READY_RELEASED = "ready_released"

    TRANSFER_STARTED = "transfer_started"
    CARRIER_SETTLED = "carrier_settled"
    COMPT_RECEIVED = "compt_received"
    HANDSHAKE_CLOSED = "handshake_closed"
    SEGMENT_COMPLETED = "segment_completed"

    BATCH_STARTED = "batch_started"
    BATCH_ENDED = "batch_ended"
    INTERLOCK_ENGAGED = "interlock_engaged"
    INTERLOCK_RELEASED = "interlock_released"

    HO_ABORTED = "ho_aborted"
    HO_AVBL_CHANGED = "ho_avbl_changed"
    ES_CHANGED = "es_changed"

    FAULT_RAISED = "fault_raised"
    FAULT_CLEARED = "fault_cleared"
    TIMER_EXPIRED = "timer_expired"
    NOT_READY = "not_ready"

    OUTPUT_CHANGED = "output_changed"
    WATCHDOG_STALL = "watchdog_stall"
    IO_ERROR = "io_error"


EVENT_CLAUSE: Mapping[EventType, str] = {
    EventType.HANDSHAKE_STARTED: "6.2.2.1(2)",
    EventType.PORTS_SELECTED: "6.1.2.3/6.1.2.4",
    EventType.DEMAND_ASSERTED: "6.2.2.1(3)",
    EventType.DEMAND_RELEASED: "6.2.2.1(7)",
    EventType.READY_ASSERTED: "6.2.2.1(5)",
    EventType.READY_RELEASED: "6.2.2.1(11)",
    EventType.TRANSFER_STARTED: "6.2.2.1(6)",
    EventType.CARRIER_SETTLED: "6.2.2.1(7)",
    EventType.COMPT_RECEIVED: "6.2.2.1(10)",
    EventType.HANDSHAKE_CLOSED: "6.2.2.1(13)",
    EventType.SEGMENT_COMPLETED: "6.2.4.3",
    EventType.BATCH_STARTED: "6.2.4.3",
    EventType.BATCH_ENDED: "6.2.4.3",
    EventType.INTERLOCK_ENGAGED: "6.1.1 表1 BUSY",
    EventType.INTERLOCK_RELEASED: "6.1.1 表1 BUSY",
    EventType.HO_ABORTED: "6.2.5.2/6.2.5.3",
    EventType.HO_AVBL_CHANGED: "6.1.1 表1 HO_AVBL",
    EventType.ES_CHANGED: "6.1.1 表1 ES",
    EventType.NOT_READY: "6.2.5.1(a)",
    EventType.FAULT_RAISED: "6.3.1.1",
    EventType.FAULT_CLEARED: "6.3.3.1",
}


@dataclass(frozen=True)
class Event:
    """一条协议事件。"""

    type: EventType
    timestamp: float = 0.0
    interface_id: str = ""
    state: str = ""
    port_ids: Tuple[str, ...] = ()
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def clause(self) -> str:
        """该事件对应的标准条款号。"""

        return EVENT_CLAUSE.get(self.type, "")

    def __str__(self) -> str:  # pragma: no cover - 展示用
        parts = [f"[{self.interface_id}]", self.type.value]
        if self.state:
            parts.append(f"state={self.state}")
        if self.port_ids:
            parts.append("ports=" + ",".join(self.port_ids))
        if self.message:
            parts.append(self.message)
        return " ".join(parts)


Handler = Callable[[Event], None]


class EventBus:
    """同步事件总线。

    回调在调用线程（即 Runner 线程）内**同步**执行；回调抛出的异常会被捕获并
    记录，绝不会破坏时序。可用 :attr:`errors` 检查是否有回调出错。
    """

    def __init__(self) -> None:
        self._handlers: Dict[Optional[EventType], List[Handler]] = {}
        self._lock = threading.RLock()
        self._history: List[Event] = []
        self._history_limit = 0
        self.errors: List[Tuple[Event, BaseException]] = []

    # ---------------------------------------------------------------- 订阅
    def subscribe(self, event_type: Optional[EventType], handler: Handler) -> Handler:
        """订阅某类事件；``event_type=None`` 表示订阅全部。"""

        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)
        return handler

    def unsubscribe(self, event_type: Optional[EventType], handler: Handler) -> bool:
        with self._lock:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)
                return True
        return False

    def on(self, event_type: Optional[EventType]) -> Callable[[Handler], Handler]:
        """装饰器写法：``@bus.on(EventType.FAULT_RAISED)``。"""

        def decorator(handler: Handler) -> Handler:
            self.subscribe(event_type, handler)
            return handler

        return decorator

    # ---------------------------------------------------------------- 分发
    def emit(self, event: Event) -> None:
        """分发一条事件，同时记录历史（若启用）。"""

        if self._history_limit:
            self._history.append(event)
            if len(self._history) > self._history_limit:
                del self._history[0 : len(self._history) - self._history_limit]
        with self._lock:
            handlers = list(self._handlers.get(event.type, ()))
            handlers += list(self._handlers.get(None, ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - 回调异常不得影响时序
                self.errors.append((event, exc))
                log.exception("事件回调异常: %s", handler)

    def emit_all(self, events) -> None:
        for event in events:
            self.emit(event)

    # ---------------------------------------------------------------- 历史
    def enable_history(self, limit: int = 2000) -> None:
        """启用事件历史环形缓冲（便于故障后取证）。"""

        with self._lock:
            self._history_limit = max(1, int(limit))

    @property
    def history(self) -> Tuple[Event, ...]:
        with self._lock:
            return tuple(self._history)

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
