"""虚拟线束：把被动侧实现与主动侧参考实现接在一根"虚拟 DB-25"上。

它负责四件事：

1. **建被动侧**：用 :class:`~e84.io.sim.SimIO` 起一个完整的
   :class:`~e84.equipment.Equipment`（含 HAL、传感量、协议核心）；
2. **建主动侧**：起一个 :class:`~e84.active.fsm.ActiveFsm`；
3. **连线**：按配置里逐信号的 ``active_high`` 在两个后端之间搬运**物理电平**
   （所以极性、以及「两个逻辑信号并在一条线上」这类现场接法都会被真实地走一遍）；
4. **模拟载口**：把「载具存在 / 完整落位」映射成现场的多键输入电平，
   并在主动侧搬运动作完成时翻转它。

于是「被动侧库 + 合法主动对手 + 载口传感器」构成一个可以逐毫秒回放、
不需要任何硬件的闭环。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from e84.active import ActiveConfig, ActiveFsm, ActiveInputs, TransferJob
from e84.clock import Clock, VirtualClock
from e84.config.model import EquipmentConfig, InterfaceConfig, LoadPortConfig
from e84.controller import PiOController
from e84.equipment import Equipment
from e84.events import Event
from e84.io.sim import SimIO
from e84.model import Op, PortRole
from e84.runner import ManualRunner

__all__ = ["PortSim", "VirtualRig"]

#: 主动侧需要读取的被动侧输出信号。
_ACTIVE_READS = ("L_REQ", "U_REQ", "READY", "HO_AVBL", "ES")
#: 主动侧驱动的信号（被动侧的输入）。
_ACTIVE_DRIVES = ("VALID", "CS_0", "CS_1", "TR_REQ", "BUSY", "COMPT", "CONT")


@dataclass
class PortSim:
    """一个载口的仿真状态。"""

    port_id: str
    role: PortRole
    key_channels: Tuple[str, ...] = ()
    active_high: bool = True
    external: bool = False
    present: bool = False
    in_position: bool = False

    def drive(self, io: SimIO, controller: PiOController) -> None:
        """把当前状态写进输入通道（或外部传感量注入点）。"""

        if self.external:
            controller.set_external_sensor(self.port_id, "carrier_present", self.present)
            controller.set_external_sensor(
                self.port_id, "carrier_in_position", self.in_position
            )
            return
        if not self.key_channels:
            return
        if self.in_position:
            logicals = [True] * len(self.key_channels)
        elif self.present:
            logicals = [True] + [False] * (len(self.key_channels) - 1)
        else:
            logicals = [False] * len(self.key_channels)
        for channel, logical in zip(self.key_channels, logicals):
            io.set_input(channel, logical if self.active_high else (not logical))


class VirtualRig:
    """被动侧 ↔ 主动侧 的虚拟闭环。"""

    def __init__(
        self,
        config: EquipmentConfig,
        *,
        jobs: Optional[Sequence[TransferJob]] = None,
        continuous: bool = False,
        clock: Optional[Clock] = None,
        interface_id: Optional[str] = None,
        transfer_duration_s: float = 0.05,
        check_ho_avbl: bool = True,
        autostart: bool = True,
        boot_hold_s: float = 0.0,
        active_kwargs: Optional[Dict[str, object]] = None,
    ) -> None:
        if not config.interfaces:
            raise ValueError("虚拟线束需要至少一个 PI/O 接口")
        iface = config.interface(interface_id) if interface_id else config.interfaces[0]
        if iface is None:
            raise ValueError(f"配置里没有接口 {interface_id!r}")
        if len(config.interfaces) > 1 and interface_id is None:
            raise ValueError(
                "配置含多个接口，虚拟线束一次只驱动一个；请显式指定 interface_id"
            )
        self.iface: InterfaceConfig = iface
        self.clk: Clock = clock if clock is not None else VirtualClock()

        # ---- 被动侧 ----
        self.passive_io = SimIO("passive")
        self.active_io = SimIO("active")
        passive_config = dataclasses.replace(config, boot_safe_hold_ms=boot_hold_s * 1000.0)
        self.equipment = Equipment(
            passive_config,
            clock=self.clk,
            ios={iface.id: self.passive_io},
            validate=False,
        )
        self.controller = self.equipment.controller(iface.id)
        self.runner = ManualRunner(self.equipment)

        # ---- 主动侧 ----
        active_cfg = ActiveConfig(
            jobs=list(jobs) if jobs is not None else [],
            continuous=continuous,
            check_ho_avbl=check_ho_avbl,
            transfer_duration_s=transfer_duration_s,
            **(active_kwargs or {}),
        )
        self.active = ActiveFsm(active_cfg)

        # ---- 事件采集 ----
        self.events: List[Event] = []
        self.equipment.bus.subscribe(None, self.events.append)

        # ---- 载口仿真 ----
        self.ports: Dict[str, PortSim] = {}
        for port_cfg in iface.load_ports:
            self.ports[port_cfg.id] = self._build_port_sim(port_cfg)

        # ---- 主动侧通道 ----
        for name in _ACTIVE_READS:
            if name in iface.outputs:
                self.active_io.setup_input(self._a_in(name))
        for name in _ACTIVE_DRIVES:
            if name in iface.inputs:
                self.active_io.setup_output(self._a_out(name), initial=iface.inputs[name].active_high is False)

        if autostart:
            self.start()

    # ------------------------------------------------------------------ 构建
    def _build_port_sim(self, port_cfg: LoadPortConfig) -> PortSim:
        key_spec = port_cfg.carrier_in_position
        if key_spec.source not in ("keys", "input"):
            key_spec = port_cfg.carrier_present
        channels: Tuple[str, ...] = ()
        if key_spec.source == "keys":
            channels = tuple(key_spec.channels)
        elif key_spec.source == "input" and key_spec.channel:
            channels = (key_spec.channel,)
        # 两个传感量都取自常量/外部时，走 API 注入路径
        external = not channels and (
            port_cfg.carrier_in_position.source == "external"
            or port_cfg.carrier_present.source == "external"
        )
        return PortSim(
            port_id=port_cfg.id,
            role=port_cfg.role,
            key_channels=channels,
            active_high=key_spec.active_high,
            external=external,
        )

    @staticmethod
    def _a_in(name: str) -> str:
        return f"A_IN_{name}"

    @staticmethod
    def _a_out(name: str) -> str:
        return f"A_OUT_{name}"

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.runner.start(now=self.clk.now())
        for port in self.ports.values():
            port.drive(self.passive_io, self.controller)

    def stop(self) -> None:
        self.runner.stop(now=self.clk.now())

    def __enter__(self) -> "VirtualRig":
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ------------------------------------------------------------------ 连线
    def _wire_passive_to_active(self) -> None:
        for name in _ACTIVE_READS:
            spec = self.iface.outputs.get(name)
            if spec is None:
                continue
            level = self.passive_io.read(spec.channel)
            self.active_io.set_input(self._a_in(name), level)

    def _active_inputs(self) -> ActiveInputs:
        def logical(name: str, default: bool) -> bool:
            spec = self.iface.outputs.get(name)
            if spec is None:
                return default
            level = self.active_io.read(self._a_in(name))
            return level if spec.active_high else (not level)

        demand = logical("L_REQ", False) or logical("U_REQ", False)
        return ActiveInputs(
            now=self.clk.now(),
            demand_on=demand,
            ready=logical("READY", False),
            ho_avbl=logical("HO_AVBL", True),
            es=logical("ES", True),
        )

    def _apply_active(self, outputs) -> None:
        mapping = {
            "VALID": outputs.valid,
            "CS_0": outputs.cs0,
            "CS_1": outputs.cs1,
            "TR_REQ": outputs.tr_req,
            "BUSY": outputs.busy,
            "COMPT": outputs.compt,
            "CONT": outputs.cont,
        }
        for name, logical in mapping.items():
            spec = self.iface.inputs.get(name)
            if spec is None:
                continue
            level = logical if spec.active_high else (not logical)
            self.active_io.write(self._a_out(name), level)
            self.passive_io.set_input(spec.channel, level)

    # ------------------------------------------------------------------ 载口
    def _perform_transfer(self, job: TransferJob) -> None:
        """主动侧搬运动作完成：翻转对应载口的载具状态。"""

        roles = set(job.roles)
        for port in self.ports.values():
            if port.role not in roles:
                continue
            if job.op is Op.LOAD:
                port.present = True
                port.in_position = True
            elif job.op is Op.UNLOAD:
                port.present = False
                port.in_position = False
            port.drive(self.passive_io, self.controller)

    def set_carrier(self, port_id: str, *, present: bool, in_position: Optional[bool] = None) -> None:
        """手工设置载口载具状态（用于构造初始条件）。"""

        port = self.ports[port_id]
        port.present = present
        port.in_position = present if in_position is None else in_position
        port.drive(self.passive_io, self.controller)

    def port_state(self, port_id: str) -> PortSim:
        return self.ports[port_id]

    # ------------------------------------------------------------------ 推进
    def tick(self, dt: float = 0.001) -> None:
        """推进一个仿真步：主动 → 被动 → 载口传感量 → 被动轮询。"""

        if isinstance(self.clk, VirtualClock):
            self.clk.advance(dt)
        now = self.clk.now()

        self._wire_passive_to_active()
        result = self.active.step(self._active_inputs())
        if result.transfer_due and result.job is not None:
            self._perform_transfer(result.job)
        self._apply_active(result.outputs)
        for port in self.ports.values():
            port.drive(self.passive_io, self.controller)
        self.runner.poll(now=now)

    def run_for(self, seconds: float, dt: float = 0.001) -> None:
        """推进指定时长。"""

        steps = max(1, int(round(seconds / dt)))
        for _ in range(steps):
            self.tick(dt)

    def run_until(
        self,
        predicate: Callable[["VirtualRig"], bool],
        *,
        timeout_s: float = 20.0,
        dt: float = 0.001,
    ) -> bool:
        """推进直到条件满足；超时返回 ``False``。"""

        steps = max(1, int(round(timeout_s / dt)))
        for _ in range(steps):
            if predicate(self):
                return True
            self.tick(dt)
        return predicate(self)

    def run_to_completion(self, *, timeout_s: float = 30.0, dt: float = 0.001) -> bool:
        """推进到主动侧结束（完成/中止/故障）。"""

        return self.run_until(
            lambda rig: rig.active.completed and rig.controller.state.value == "idle",
            timeout_s=timeout_s,
            dt=dt,
        )

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self.controller.state.value

    def outputs(self):
        return self.controller.outputs()

    def signal(self, name: str) -> bool:
        """读取被动侧某个输出的逻辑值。"""

        outs = self.controller.outputs()
        return bool(outs.as_dict().get(name, False))

    def events_of(self, event_type) -> List[Event]:
        return [e for e in self.events if e.type == event_type]

    def event_messages(self) -> List[str]:
        return [f"{e.timestamp:7.3f} {e.type.value:20s} {e.message}" for e in self.events]

    def snapshot(self) -> Dict[str, object]:
        return {
            "passive": self.controller.snapshot(self.clk.now()),
            "active": self.active.snapshot(),
        }
