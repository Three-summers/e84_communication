"""PI/O 控制器：把协议核心与真实世界接起来。

一个 :class:`PiOController` 对应**一个物理并行 I/O 接口**（§3.4：时序图只对一个
PI/O 有效，各 PI/O 相互独立），它负责：

1. 按配置准备 I/O 后端、信号层（HAL）与传感量读取；
2. 每拍：读传感量 + 读 E84 输入 → 组装 :class:`~e84.model.Inputs`；
3. 调 :meth:`e84.fsm.PassiveFsm.step` → 拿到 :class:`~e84.model.Outputs`；
4. 把输出落到物理通道（**写变化才写**），分发事件、写追踪；
5. 提供上层 API：访问模式/可用性/交接意图注入、故障清除、中止、快照。

控制器**不拥有线程**：由 :mod:`e84.runner` 决定怎么驱动它。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from e84.clock import Clock, RealClock
from e84.config.model import InterfaceConfig
from e84.events import Event, EventBus, EventType
from e84.fault import ConfigError, Fault, FaultCode
from e84.fsm import PassiveFsm, StepResult
from e84.hal import InputBinding, OutputBinding, SignalMap
from e84.io.base import DigitalIO, PullMode
from e84.loadport import LoadPort, build_ports
from e84.model import AccessMode, Inputs, Op, Outputs, PortSnapshot, State
from e84.sensors import SensorBank
from e84.trace import TraceRecorder

log = logging.getLogger("e84.controller")

__all__ = ["PiOController"]

_PULL = {"up": PullMode.UP, "down": PullMode.DOWN, "none": PullMode.NONE}

#: 可选的外部「可交接」输入信号名：若在 inputs 里绑定，则自动用于 HO_AVBL 合成。
EXTERNAL_HO_SIGNAL = "HO_OK"

#: 标准 E84 输入信号名。绑定在 ``inputs`` 里、但不属于此集合的通道，
#: 一律作为「现场自定义信号」（如板级 ``GO``）放进 :attr:`Inputs.extra`，
#: 供 ``preconditions`` 与上层逻辑使用。
STANDARD_INPUT_SIGNALS = frozenset(
    {"VALID", "CS_0", "CS_1", "TR_REQ", "BUSY", "COMPT", "CONT", "AM_AVBL", "VA", "VS_0", "VS_1"}
)


class PiOController:
    """单个并行 I/O 接口的被动侧控制器。"""

    def __init__(
        self,
        config: InterfaceConfig,
        *,
        io: Optional[DigitalIO] = None,
        bus: Optional[EventBus] = None,
        clock: Optional[Clock] = None,
        trace: Optional[TraceRecorder] = None,
        backend_factory: Optional[Callable[[str, Mapping[str, object]], DigitalIO]] = None,
        default_backend: str = "memory",
        boot_hold_s: float = 0.0,
        equipment_name: str = "",
    ) -> None:
        self.cfg = config
        self.bus = bus if bus is not None else EventBus()
        self.clock = clock if clock is not None else RealClock()
        self.trace = trace
        self.equipment_name = equipment_name
        self.boot_hold_s = max(0.0, float(boot_hold_s))

        # ---- 载口 ----
        self.ports: List[LoadPort] = build_ports(config.load_ports)
        self._port_by_id = {p.id: p for p in self.ports}

        # ---- I/O 后端 ----
        self._io_provided = io is not None
        self.io: DigitalIO = io if io is not None else self._open_backend(
            backend_factory, default_backend
        )

        # ---- 传感量 ----
        self._sensors = SensorBank(self._build_sensor_specs())

        # ---- 信号层 ----
        self.hal = SignalMap(
            self.io,
            inputs=self._build_input_bindings(),
            outputs=self._build_output_bindings(),
        )
        self._check_channel_conflicts()

        # ---- 协议核心 ----
        self.fsm = PassiveFsm(config)

        # ---- 运行时状态 ----
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._started = False
        self._ready_at = 0.0
        self._extra_inputs: Dict[str, bool] = {}
        self._external_sensors: Dict[str, bool] = {}
        self._es_ok = True
        self._external_ho_ok: Optional[bool] = None
        self._poll_count = 0
        self._last_result: Optional[StepResult] = None

    # ==================================================================== #
    # 构建
    # ==================================================================== #
    def _open_backend(
        self,
        factory: Optional[Callable[[str, Mapping[str, object]], DigitalIO]],
        default_backend: str,
    ) -> DigitalIO:
        name = self.cfg.backend or default_backend
        options = dict(self.cfg.backend_options)
        if factory is not None:
            return factory(name, options)
        from e84.io import open_backend

        return open_backend(name, **options)

    def _build_input_bindings(self) -> Dict[str, InputBinding]:
        bindings: Dict[str, InputBinding] = {}
        for name, spec in self.cfg.inputs.items():
            bindings[name] = InputBinding(
                channel=spec.channel,
                active_high=spec.active_high,
                pull=_PULL.get(spec.pull, PullMode.NONE),
                debounce_ms=spec.debounce_ms,
            )
        return bindings

    def _build_output_bindings(self) -> Dict[str, OutputBinding]:
        return {
            name: OutputBinding(
                channel=spec.channel,
                active_high=spec.active_high,
                safe_on=spec.safe_on,
            )
            for name, spec in self.cfg.outputs.items()
        }

    def _build_sensor_specs(self):
        from e84.config.model import SensorSpec

        specs = {}
        for port in self.ports:
            for name, spec in port.cfg.sensor_specs.items():
                specs[port.sensor_key_map()[name]] = spec
            if port.cfg.access_mode.source == "input" and port.cfg.access_mode.channel:
                specs[port.access_mode_sensor_key()] = SensorSpec(
                    source="input",
                    channel=port.cfg.access_mode.channel,
                    active_high=port.cfg.access_mode.active_high,
                )
        return specs

    def _check_channel_conflicts(self) -> None:
        hal_channels = set(self.hal.output_channels) | set(self.hal.input_channels)
        sensor_channels = set(self._sensors.channels)
        overlap = hal_channels & sensor_channels
        if overlap:
            raise ConfigError(
                f"[{self.cfg.id}] 以下通道同时被 E84 信号与本机传感量占用："
                f"{', '.join(sorted(overlap))}。E84 信号线与传感器必须使用不同引脚。"
            )

    # ==================================================================== #
    # 生命周期
    # ==================================================================== #
    def start(self, now: Optional[float] = None) -> None:
        """初始化 I/O 与信号层，并把输出置为安全态后启用。"""

        with self._lock:
            if self._started:
                return
            t = self.clock.now() if now is None else now
            self._sensors.setup(self.io)
            self.hal.setup()          # 输出落到安全态
            self.hal.safe_state()
            self.hal.read(t)
            if self.boot_hold_s > 0:
                self._ready_at = t + self.boot_hold_s
            else:
                self._ready_at = t
                self.fsm.enable(t)
            self._started = True
            log.info(
                "[%s] 控制器已启动（后端=%s，拓扑=%s，载口=%s）",
                self.cfg.id,
                type(self.io).__name__,
                self.cfg.topology.value,
                ",".join(p.id for p in self.ports),
            )
            self.bus.emit(
                Event(
                    type=EventType.ENABLED,
                    timestamp=t,
                    interface_id=self.cfg.id,
                    message="控制器已启动",
                )
            )

    def stop(self, now: Optional[float] = None) -> None:
        """停机：进入安全态并把所有输出置为各自的失效安全方向。"""

        with self._lock:
            if not self._started:
                return
            t = self.clock.now() if now is None else now
            was_engaged = self.fsm.interlock_engaged
            self.fsm.disable(t)
            written = self.hal.safe_state()
            self._record_outputs(t, Outputs(), written)
            if was_engaged:
                # 停机时如果还冻着干涉区，必须补发释放事件，否则上层机构会一直被冻住
                self.bus.emit(
                    Event(
                        type=EventType.INTERLOCK_RELEASED,
                        timestamp=t,
                        interface_id=self.cfg.id,
                        state=self.fsm.state.value,
                        message="控制器停机，解除交接干涉区冻结",
                    )
                )
            self.bus.emit(
                Event(
                    type=EventType.DISABLED,
                    timestamp=t,
                    interface_id=self.cfg.id,
                    message="控制器已停机，输出回到安全态",
                )
            )
            if not self._io_provided:
                try:
                    self.io.close()
                except Exception:  # pragma: no cover - 关闭失败不应阻断停机
                    log.exception("[%s] 关闭 I/O 后端失败", self.cfg.id)
            self._started = False
            log.info("[%s] 控制器已停机", self.cfg.id)

    # ==================================================================== #
    # 主循环
    # ==================================================================== #
    def poll(self, now: Optional[float] = None) -> StepResult:
        """推进一拍：读输入 → 跑协议 → 写输出 → 发事件。"""

        if not self._poll_lock.acquire(blocking=False):
            # 重入保护：同一控制器的 poll() 不可重入（例如回调里再调 poll）。
            if self._last_result is None:
                raise RuntimeError(
                    f"[{self.cfg.id}] poll() 重入，且尚无上一次结果可用"
                )
            return self._last_result
        try:
            with self._lock:
                return self._poll_locked(now)
        finally:
            self._poll_lock.release()

    def _poll_locked(self, now: Optional[float]) -> StepResult:
        t = self.clock.now() if now is None else now
        if not self._started:
            self.start(t)

        # 上电安全保持窗口（§6.4.6 失效安全）
        if t < self._ready_at:
            self.hal.safe_state()
            result = StepResult(
                state=State.DISABLED,
                outputs=Outputs(),
                events=(),
                fault=None,
                selected_port_ids=(),
                op=Op.UNKNOWN,
                interlock_engaged=False,
            )
            self._last_result = result
            self._poll_count += 1
            return result
        if self.fsm.state is State.DISABLED and t >= self._ready_at:
            self.fsm.enable(t)

        inp = self._build_inputs(t)
        result = self.fsm.step(inp)

        written = self.hal.apply(result.outputs)
        self._record_outputs(t, result.outputs, written)
        self._last_result = result
        self._poll_count += 1

        if result.events:
            self.bus.emit_all(result.events)
        if written:
            self.bus.emit(
                Event(
                    type=EventType.OUTPUT_CHANGED,
                    timestamp=t,
                    interface_id=self.cfg.id,
                    state=result.state.value,
                    message=", ".join(
                        f"{ch}={'1' if lvl else '0'}" for ch, lvl in written.items()
                    ),
                    data={"channels": dict(written)},
                )
            )
        return result

    def _build_inputs(self, now: float) -> Inputs:
        signals = self.hal.read(now)
        sensors = self._sensors.read(self.io, now, self._external_sensors)
        ports: Sequence[PortSnapshot] = tuple(
            p.snapshot(sensors, self._external_sensors) for p in self.ports
        )

        external_ho_ok = self._external_ho_ok
        if external_ho_ok is None:
            external_ho_ok = bool(signals.get(EXTERNAL_HO_SIGNAL, True))

        def sig(name: str, default: bool = False) -> bool:
            return bool(signals.get(name, default))

        # 现场自定义信号（例如板级 GO）：从已绑定的 HAL 输入里取，再叠加运行时注入值
        extra = {
            name: bool(value)
            for name, value in signals.items()
            if name not in STANDARD_INPUT_SIGNALS
        }
        extra.update(self._extra_inputs)

        return Inputs(
            now=now,
            valid=sig("VALID"),
            cs0=sig("CS_0"),
            cs1=sig("CS_1"),
            tr_req=sig("TR_REQ"),
            busy=sig("BUSY"),
            compt=sig("COMPT"),
            cont=sig("CONT"),
            am_avbl=sig("AM_AVBL"),
            va=sig("VA"),
            vs0=sig("VS_0"),
            vs1=sig("VS_1"),
            es_ok=self._es_ok,
            external_ho_ok=external_ho_ok,
            ports=tuple(ports),
            extra=extra,
        )

    def _record_outputs(self, now: float, outputs: Outputs, written: Mapping[str, bool]) -> None:
        if self.trace is None:
            return
        # 逻辑信号（source="output"）与物理通道（source="channel"）分两行记录：
        # TraceRecorder 只在 source=="channel" 时才写 channels，因此必须显式分派，
        # 否则物理通道的跳变会被静默丢弃。
        self.trace.record(
            timestamp=now,
            interface_id=self.cfg.id,
            state=self.fsm.state.value,
            signals=outputs.as_dict(),
        )
        if written:
            self.trace.record(
                timestamp=now,
                interface_id=self.cfg.id,
                state=self.fsm.state.value,
                signals={},
                source="channel",
                channels=written,
            )

    # ==================================================================== #
    # 上层注入 API
    # ==================================================================== #
    @property
    def interface_id(self) -> str:
        return self.cfg.id

    def load_port(self, port_id: str) -> LoadPort:
        """按 id 取载口运行时对象，用于注入访问模式/可用性/方向意图。"""

        try:
            return self._port_by_id[port_id]
        except KeyError as exc:
            known = ", ".join(self._port_by_id)
            raise KeyError(f"[{self.cfg.id}] 未知载口 {port_id!r}（已知: {known}）") from exc

    def set_access_mode(
        self, port_id: str, mode: Optional[AccessMode], *, strict: bool = False
    ) -> None:
        """设置载口访问模式（``None`` 恢复配置来源）。手动模式会按策略拉低 ``HO_AVBL``。

        :param strict: 为 ``True`` 时，若该接口正处于载具交接之中（SEMI E87 §11.1.2：
            访问模式不得在 carrier transfer 期间切换），抛 :class:`ValueError` 而不是
            照改。默认 ``False``——因为"操作员切手动"往往正是要**立即**中止交接的安全
            动作，不能被守卫挡住；需要 E87 一致性的上位机可以显式打开。
        """

        if strict and self.fsm.transfer_in_progress:
            raise ValueError(
                f"[{self.cfg.id}] 载具交接进行中（state={self.fsm.state.value}），"
                "按 SEMI E87 §11.1.2 不得切换访问模式；"
                "若这是操作员的安全动作，请用 strict=False 直接切换。"
            )
        self.load_port(port_id).set_access_mode(mode)

    def set_port_available(self, port_id: str, available: Optional[bool]) -> None:
        """设置载口可用性（``None`` 恢复配置值）。"""

        self.load_port(port_id).set_available(available)

    def set_operation_intent(self, port_id: str, op: Optional[Op]) -> None:
        """设置该载口的交接方向意图（``None`` 表示由传感器推导）。"""

        self.load_port(port_id).set_operation_intent(op)

    def set_external_sensor(self, port_id: str, sensor_name: str, value: bool) -> None:
        """注入 ``source="external"`` 的传感量取值。"""

        port = self.load_port(port_id)
        port.set_external(sensor_name, value)
        self._external_sensors.update(port.external_values())

    def set_extra_input(self, name: str, value: bool) -> None:
        """注入现场自定义输入信号（例如板级 ``GO``）。"""

        self._extra_inputs[name.strip().upper()] = bool(value)

    def set_es_ok(self, ok: bool) -> None:
        """本机安全链是否健康。``False`` 会同时拉低 ``ES`` 与 ``HO_AVBL``。"""

        self._es_ok = bool(ok)

    def set_external_ho_ok(self, ok: Optional[bool]) -> None:
        """外部「可交接」许可（``None`` 表示回到由 ``HO_OK`` 输入或默认真值决定）。"""

        self._external_ho_ok = None if ok is None else bool(ok)

    def clear_fault(self, *, force: bool = False, now: Optional[float] = None) -> bool:
        """清除锁存故障。返回是否真的清除了。"""

        with self._lock:
            t = self.clock.now() if now is None else now
            cleared = self.fsm.clear_fault(t, force=force)
            if cleared:
                self.bus.emit(
                    Event(
                        type=EventType.FAULT_CLEARED,
                        timestamp=t,
                        interface_id=self.cfg.id,
                        message="故障已清除",
                    )
                )
                log.warning("[%s] 故障已清除", self.cfg.id)
            return cleared

    def abort(self, reason: str = "operator", now: Optional[float] = None) -> None:
        """中止当前握手（§6.3.3.1 的「中止互锁时序」原语）。"""

        with self._lock:
            t = self.clock.now() if now is None else now
            self.fsm.abort(t, reason)
            self.bus.emit(
                Event(
                    type=EventType.HO_ABORTED,
                    timestamp=t,
                    interface_id=self.cfg.id,
                    state=self.fsm.state.value,
                    message=f"已请求中止握手：{reason}",
                )
            )
            log.warning("[%s] 已请求中止握手：%s", self.cfg.id, reason)

    def raise_fault(self, code: FaultCode, message: str, now: Optional[float] = None) -> None:
        """外部（如执行机构）上报故障，进入锁存流程并拉低 ``READY``/``ES``/``HO_AVBL``。"""

        with self._lock:
            t = self.clock.now() if now is None else now
            self.fsm.raise_fault(code, message, t)
            self.bus.emit(
                Event(
                    type=EventType.FAULT_RAISED,
                    timestamp=t,
                    interface_id=self.cfg.id,
                    state=self.fsm.state.value,
                    message=f"{code.value}: {message}",
                    data={"code": code.value, "clause": self.fsm.fault.clause if self.fsm.fault else ""},
                )
            )
            log.error("[%s] 外部故障上报：%s %s", self.cfg.id, code.value, message)

    # ==================================================================== #
    # 只读查询
    # ==================================================================== #
    @property
    def state(self) -> State:
        return self.fsm.state

    @property
    def fault(self) -> Optional[Fault]:
        return self.fsm.fault

    @property
    def transfer_in_progress(self) -> bool:
        """是否正处于一次载具交接之中（SEMI E87 Table 8 的 AUTO 交接区间）。"""

        return self.fsm.transfer_in_progress

    @property
    def handshake_active(self) -> bool:
        """握手是否已打开且尚未闭合。"""

        return self.fsm.handshake_active

    @property
    def interlock_engaged(self) -> bool:
        """``BUSY=ON`` 期间为 ``True``：本机不得在交接干涉区内动作。"""

        return self.fsm.interlock_engaged

    @property
    def last_result(self) -> Optional[StepResult]:
        return self._last_result

    @property
    def poll_count(self) -> int:
        return self._poll_count

    def outputs(self) -> Outputs:
        """最近一次的输出（逻辑值）。"""

        return self.fsm.last_outputs

    def port_snapshots(self, now: Optional[float] = None) -> Sequence[PortSnapshot]:
        """返回当前载口快照（会重新读取传感量）。"""

        t = self.clock.now() if now is None else now
        sensors = self._sensors.read(self.io, t, self._external_sensors)
        return tuple(p.snapshot(sensors, self._external_sensors) for p in self.ports)

    def snapshot(self, now: Optional[float] = None) -> Dict[str, object]:
        """返回用于监视器/日志的完整只读快照。"""

        t = self.clock.now() if now is None else now
        data = self.fsm.snapshot(t)
        data.update(
            {
                "equipment": self.equipment_name,
                "started": self._started,
                "poll_count": self._poll_count,
                "es_ok": self._es_ok,
                "external_ho_ok": self._external_ho_ok,
                "extra_inputs": dict(self._extra_inputs),
                "ports": [
                    {
                        "id": p.port_id,
                        "role": p.role.value,
                        "carrier_present": p.carrier_present,
                        "carrier_in_position": p.carrier_in_position,
                        "ready_for_transfer": p.ready_for_transfer,
                        "door_open": p.door_open,
                        "clamp_released": p.clamp_released,
                        "available": p.available,
                        "access_mode": p.access_mode.value,
                        "expected_op": p.expected_op.value,
                    }
                    for p in self.port_snapshots(t)
                ],
                "io": dict(self.io.describe()),
                "last_written": dict(self.hal.last_written),
                "write_count": self.hal.write_count,
            }
        )
        return data
