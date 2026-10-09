"""设备门面：管理一台设备上的多个并行 I/O 接口。

* 每个 PI/O 一个 :class:`~e84.controller.PiOController`（§3.4：各 PI/O 相互独立）；
* 共用一个 :class:`~e84.events.EventBus`、一个时钟、一个追踪记录器；
* 提供设备级 API：启停、轮询、故障清除、快照、看门狗。

驱动方式由 :mod:`e84.runner` 决定（手动 ``poll()`` 或后台线程）。
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Mapping, Optional

from e84.clock import Clock, RealClock
from e84.config.model import EquipmentConfig, WatchdogConfig
from e84.config.validate import ValidationReport, validate_config
from e84.controller import PiOController
from e84.events import EventBus
from e84.fault import Fault, FaultCode
from e84.io import open_backend
from e84.io.base import DigitalIO
from e84.loadport import LoadPort
from e84.model import AccessMode, Op, State
from e84.trace import TraceRecorder

log = logging.getLogger("e84.equipment")

__all__ = ["Equipment"]


class _Watchdog:
    """硬件看门狗通道翻转（由 :meth:`Equipment.poll` 驱动）。

    语义：**看门狗只在轮询推进时才翻转**。一旦调用方的轮询停止（进程卡死、
    上层循环异常、线程崩溃），WDI 停止翻转，硬件看门狗会把外部电路拉到安全态。
    """

    def __init__(self, cfg: WatchdogConfig, io: DigitalIO) -> None:
        self.cfg = cfg
        self.io = io
        self._initialised = False
        self._level = False
        self._next_at = 0.0

    def tick(self, now: float) -> None:
        if not self.cfg.enabled or not self.cfg.channel:
            return
        if not self._initialised:
            self.io.setup_output(self.cfg.channel, initial=False)
            self._initialised = True
            self._next_at = now + self.cfg.period_ms / 1000.0
            return
        if now >= self._next_at:
            self._level = not self._level
            self.io.write(self.cfg.channel, self._level)
            self._next_at = now + self.cfg.period_ms / 1000.0


class Equipment:
    """一台设备（可含多个 PI/O 接口）的被动侧 E84 实现。"""

    def __init__(
        self,
        config: EquipmentConfig,
        *,
        bus: Optional[EventBus] = None,
        clock: Optional[Clock] = None,
        trace: Optional[TraceRecorder] = None,
        ios: Optional[Mapping[str, DigitalIO]] = None,
        backend_factory: Optional[Callable[[str, Mapping[str, object]], DigitalIO]] = None,
        validate: bool = True,
    ) -> None:
        self.config = config
        self.bus = bus if bus is not None else EventBus()
        self.clock = clock if clock is not None else RealClock()
        if trace is not None:
            self.trace = trace
        elif config.trace.enabled:
            self.trace = TraceRecorder(
                config.trace.path,
                fmt=config.trace.fmt,
                flush_every=config.trace.flush_every,
            )
        else:
            self.trace = None

        if validate:
            report = validate_config(config, raise_on_error=True)
            for warning in report.warnings:
                log.warning("配置告警: %s", warning)

        self._backend_factory = backend_factory or _default_backend_factory
        ios = dict(ios or {})
        self.controllers: List[PiOController] = []
        for iface in config.interfaces:
            self.controllers.append(
                PiOController(
                    iface,
                    io=ios.get(iface.id),
                    bus=self.bus,
                    clock=self.clock,
                    trace=self.trace,
                    backend_factory=self._backend_factory,
                    default_backend=config.backend,
                    boot_hold_s=config.boot_safe_hold_ms / 1000.0,
                    equipment_name=config.name,
                )
            )
        self._by_id = {c.interface_id: c for c in self.controllers}

        # 看门狗：默认挂在第一个接口的后端上，可用 watchdog.interface 指定
        self._watchdog: Optional[_Watchdog] = None
        if config.watchdog is not None and config.watchdog.enabled:
            target = self._resolve_watchdog_controller(config.watchdog)
            self._watchdog = _Watchdog(config.watchdog, target.io)
        self._started = False
        self._stopped_explicitly = False

    # ==================================================================== #
    def _resolve_watchdog_controller(self, cfg: WatchdogConfig) -> PiOController:
        if cfg.interface:
            if cfg.interface not in self._by_id:
                raise ValueError(
                    f"watchdog.interface={cfg.interface!r} 不存在"
                    f"（已知: {', '.join(self._by_id)}）"
                )
            return self._by_id[cfg.interface]
        if not self.controllers:
            raise ValueError("没有可用接口，无法挂载看门狗")
        return self.controllers[0]

    # ==================================================================== #
    # 生命周期
    # ==================================================================== #
    def start(self, now: Optional[float] = None) -> None:
        """启动所有接口。"""

        t = self.clock.now() if now is None else now
        for controller in self.controllers:
            controller.start(t)
        self._started = True
        self._stopped_explicitly = False
        log.info("设备 %s 已启动（%d 个 PI/O）", self.config.name, len(self.controllers))

    def stop(self, now: Optional[float] = None) -> None:
        """停机：所有接口进入安全态。"""

        t = self.clock.now() if now is None else now
        for controller in self.controllers:
            controller.stop(t)
        if self.trace is not None:
            self.trace.close()
        self._started = False
        self._stopped_explicitly = True
        log.info("设备 %s 已停机", self.config.name)

    def poll(self, now: Optional[float] = None) -> None:
        """推进所有接口一拍。

        安全语义：如果之前显式调用过 :meth:`stop`，这里**不会**把设备重新启动，
        只会把输出维持在安全态。这样"已经停机"就不会被上层残留的定时器/循环
        悄悄重新拉起来（那会让 ``ES``/``HO_AVBL`` 重新变回 ON，等于对外宣告
        "可以来交接了"）。要恢复运行必须显式再次 :meth:`start`。
        """

        t = self.clock.now() if now is None else now
        if self._stopped_explicitly:
            for controller in self.controllers:
                controller.hal.safe_state()
            return
        for controller in self.controllers:
            controller.poll(t)
        if self._watchdog is not None:
            self._watchdog.tick(t)

    # ==================================================================== #
    # 查询与操作
    # ==================================================================== #
    def controller(self, interface_id: str) -> PiOController:
        try:
            return self._by_id[interface_id]
        except KeyError as exc:
            known = ", ".join(self._by_id)
            raise KeyError(f"未知接口 {interface_id!r}（已知: {known}）") from exc

    def load_port(self, interface_id: str, port_id: str) -> LoadPort:
        return self.controller(interface_id).load_port(port_id)

    def set_access_mode(
        self, interface_id: str, port_id: str, mode: Optional[AccessMode]
    ) -> None:
        self.controller(interface_id).set_access_mode(port_id, mode)

    def set_operation_intent(
        self, interface_id: str, port_id: str, op: Optional[Op]
    ) -> None:
        self.controller(interface_id).set_operation_intent(port_id, op)

    def set_port_available(
        self, interface_id: str, port_id: str, available: Optional[bool]
    ) -> None:
        self.controller(interface_id).set_port_available(port_id, available)

    def clear_fault(self, interface_id: Optional[str] = None, *, force: bool = False) -> Dict[str, bool]:
        """清除故障。``interface_id=None`` 表示清除所有接口。"""

        targets = (
            [self.controller(interface_id)] if interface_id else self.controllers
        )
        now = self.clock.now()
        return {c.interface_id: c.clear_fault(force=force, now=now) for c in targets}

    def abort(self, interface_id: Optional[str] = None, reason: str = "operator") -> None:
        """中止握手。``interface_id=None`` 表示所有接口。"""

        targets = [self.controller(interface_id)] if interface_id else self.controllers
        now = self.clock.now()
        for controller in targets:
            controller.abort(reason, now=now)

    def raise_fault(
        self, interface_id: str, code: FaultCode, message: str
    ) -> None:
        self.controller(interface_id).raise_fault(code, message, now=self.clock.now())

    # ==================================================================== #
    @property
    def faults(self) -> Mapping[str, Optional[Fault]]:
        return {c.interface_id: c.fault for c in self.controllers}

    @property
    def interlock_engaged(self) -> bool:
        """是否有任一接口正处于 ``BUSY=ON``（干涉区冻结）状态。"""

        return any(c.interlock_engaged for c in self.controllers)

    @property
    def states(self) -> Mapping[str, State]:
        return {c.interface_id: c.state for c in self.controllers}

    def validation_report(self) -> ValidationReport:
        """对当前配置重新做一次校验（不抛异常）。"""

        return validate_config(self.config, raise_on_error=False)

    def snapshot(self, now: Optional[float] = None) -> Dict[str, object]:
        """设备级只读快照。"""

        t = self.clock.now() if now is None else now
        return {
            "equipment": self.config.name,
            "started": self._started,
            "now": t,
            "interfaces": [c.snapshot(t) for c in self.controllers],
            "interlock_engaged": self.interlock_engaged,
        }

    def describe(self) -> Dict[str, object]:
        """返回配置/映射摘要（供 ``e84-validate --show-map`` 使用）。"""

        return {
            "equipment": self.config.name,
            "backend": self.config.backend,
            "poll_interval_ms": self.config.poll_interval_ms,
            "interfaces": [
                {
                    "id": c.interface_id,
                    "scenario": c.cfg.scenario.value,
                    "topology": c.cfg.topology.value,
                    "simultaneous": c.cfg.enable_simultaneous,
                    "continuous": c.cfg.enable_continuous,
                    "preconditions": list(c.cfg.preconditions),
                    "signals": c.hal.describe(),
                    "sensors": c._sensors.describe(),  # noqa: SLF001 - 同包内诊断用
                    "ports": [p.describe() for p in c.ports],
                }
                for c in self.controllers
            ],
        }


def _default_backend_factory(name: str, options: Mapping[str, object]) -> DigitalIO:
    """默认后端工厂：按名字惰性打开。"""

    return open_backend(name, **options)
