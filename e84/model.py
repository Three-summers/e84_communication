"""协议核心的数据契约。

本模块定义 :mod:`e84.fsm` 的输入输出类型。核心约定：

* :class:`Inputs` / :class:`Outputs` / :class:`PortSnapshot` 全部是**不可变**的，
  便于测试断言、事件携带与离线回放；
* 所有时刻使用单调秒（``now``），由 :class:`e84.clock.Clock` 提供；
* 逻辑 ``True`` == 标准中的 **ON**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

__all__ = [
    "AccessMode",
    "Topology",
    "Scenario",
    "PortRole",
    "Op",
    "State",
    "PortSnapshot",
    "Inputs",
    "Outputs",
]


class AccessMode(str, Enum):
    """访问模式（§5.1 / §5.4 / §5.14）。"""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


class Topology(str, Enum):
    """一个并行 I/O 接口对应几个载口（§6.1.2、A1-5）。"""

    ONE_LP = "one_load_port"
    TWO_LP = "two_load_ports"


class Scenario(str, Enum):
    """接口场景。v1 只实现 ``STANDARD``；跨区场景保留枚举与配置校验位。"""

    STANDARD = "standard"
    INTERBAY_PASSIVE_OHS = "interbay_passive_ohs"


class PortRole(str, Enum):
    """载口在共用一个 PI/O 时的相对位置（§6.1.2.3：面向设备载口时的左右手）。"""

    SINGLE = "single"
    LEFT = "left"
    RIGHT = "right"


class Op(str, Enum):
    """交接方向。``L_REQ``/``U_REQ`` 始终以「载具流向」定义（§6.1 表 1）。"""

    LOAD = "load"       # 主动 -> 被动
    UNLOAD = "unload"   # 被动 -> 主动
    UNKNOWN = "unknown"


class State(str, Enum):
    """被动侧状态机的状态。每个状态都可追溯回标准条款（见各注释）。"""

    DISABLED = "disabled"                 # 未启用 / 已停机（上电安全态）
    IDLE = "idle"                         # 健康待命，等 VALID 上升沿
    SELECT = "select"                     # VALID ON 后解码 CS_x（注 3）
    REQ_ON = "req_on"                     # L_REQ/U_REQ 已 ON，等 TR_REQ（TP1）
    WAIT_BUSY = "wait_busy"               # READY 已 ON，等 BUSY（TP2）
    TRANSFER = "transfer"                 # BUSY ON，等载具到位/取走（TP3）
    AWAIT_COMPT = "await_compt"           # 请求已 OFF，等 BUSY OFF 与 COMPT ON（TP4）
    CLOSING = "closing"                   # READY OFF，等 VALID OFF（TP5）
    CONT_NEXT = "cont_next"               # 连续交接中场，等下一次 VALID ON（TP6）
    HO_ABORT = "ho_abort"                 # 本机拉低 HO_AVBL，等握手闭合后恢复
    FAULT_WAIT_CLOSE = "fault_wait_close"  # 故障中且握手未闭合：钉住状态
    FAULT_LATCHED = "fault_latched"       # 握手已闭合，故障锁存，等待显式清除


@dataclass(frozen=True)
class PortSnapshot:
    """某一时刻某个载口的状态快照。

    ``carrier_present`` 与 ``carrier_in_position`` 是两个不同的事实
    （对应现场常见的「检测到载具」与「完整落位」两个传感器/三键组合）：

    * ``carrier_present``：载口上存在载具（任意位置）；
    * ``carrier_in_position``：载具位于**正确位置**——这才是 §6.1 表 1 中判定
      ``L_REQ``/``U_REQ`` 落下的依据。
    """

    port_id: str
    role: PortRole = PortRole.SINGLE
    carrier_present: bool = False
    carrier_in_position: bool = False
    door_open: bool = True
    clamp_released: bool = True
    ready_for_transfer: bool = True
    available: bool = True
    access_mode: AccessMode = AccessMode.AUTOMATIC
    expected_op: Op = Op.UNKNOWN

    # ------------------------------------------------------------ 派生判定
    @property
    def usable(self) -> bool:
        """机构层面是否可以立刻参与交接。"""

        return (
            self.available
            and self.ready_for_transfer
            and self.door_open
            and self.clamp_released
        )

    def can_load(self) -> bool:
        """能否作为「装载」目标（载口必须为空且机构可用）。"""

        return self.usable and not self.carrier_present

    def can_unload(self) -> bool:
        """能否作为「卸载」来源（载具必须在正确位置且机构可用）。"""

        return self.usable and self.carrier_in_position

    def carrier_settled_for(self, op: Op) -> bool:
        """该载口在方向 ``op`` 下是否已经「完成物理到位」。"""

        if op is Op.LOAD:
            return self.carrier_in_position
        if op is Op.UNLOAD:
            return not self.carrier_present
        return False


@dataclass(frozen=True)
class Inputs:
    """一次 :meth:`e84.fsm.PassiveFsm.step` 的完整输入。

    主动侧信号 + 本机传感器/上层状态，全部是**电平**语义；
    ``CONT`` 例外——它按标准需在 ``BUSY`` 上升沿采样，
    采样动作由 :class:`e84.controller.PiOController` 负责（含 ``cont_sample_delay_ms``）。
    """

    now: float = 0.0
    # ---- 主动设备 -> 被动设备（§6.1 表 1）----
    valid: bool = False
    cs0: bool = False
    cs1: bool = False
    tr_req: bool = False
    busy: bool = False
    compt: bool = False
    cont: bool = False
    am_avbl: bool = False
    va: bool = False
    vs0: bool = False
    vs1: bool = False
    # ---- 本机安全链与外部许可 ----
    es_ok: bool = True
    external_ho_ok: bool = True
    # ---- 载口 ----
    ports: Tuple[PortSnapshot, ...] = ()
    # ---- 现场自定义信号（如板级 ``GO``）----
    extra: Mapping[str, bool] = field(default_factory=dict)

    _SIGNAL_FIELDS: Mapping[str, str] = field(
        default=None, init=False, repr=False, compare=False
    )

    def signal(self, name: str) -> bool:
        """按信号名取值，支持标准信号与 ``extra`` 里的现场自定义信号。

        :param name: 例如 ``"VALID"``、``"CS_0"``、``"GO"``（大小写不敏感）
        """

        key = name.strip().upper().replace("-", "_")
        standard = {
            "VALID": self.valid,
            "CS_0": self.cs0,
            "CS_1": self.cs1,
            "TR_REQ": self.tr_req,
            "BUSY": self.busy,
            "COMPT": self.compt,
            "CONT": self.cont,
            "AM_AVBL": self.am_avbl,
            "VA": self.va,
            "VS_0": self.vs0,
            "VS_1": self.vs1,
        }
        if key in standard:
            return standard[key]
        for k, v in self.extra.items():
            if k.strip().upper().replace("-", "_") == key:
                return bool(v)
        return False

    def port(self, port_id: str) -> Optional[PortSnapshot]:
        """按 id 取载口快照。"""

        for p in self.ports:
            if p.port_id == port_id:
                return p
        return None

    def ports_with_roles(self, roles: Sequence[PortRole]) -> Tuple[PortSnapshot, ...]:
        """按角色筛选载口，保持 ``ports`` 中的原始顺序。"""

        wanted = set(roles)
        return tuple(p for p in self.ports if p.role in wanted)


@dataclass(frozen=True)
class Outputs:
    """被动侧驱动的信号（逻辑电平，``True`` == ON）。

    默认值即**上电安全态**：请求全落、``READY`` 落、``HO_AVBL`` 落（不可交接）、
    ``ES`` 落（请求停止）。这与 §6.4.6「OFF = 无电流/无光」一致，
    也保证任何掉电/崩溃都会让对方看到「不可交接 + 请求停止」。
    """

    l_req: bool = False
    u_req: bool = False
    ready: bool = False
    ho_avbl: bool = False
    es: bool = False
    va: bool = False
    vs0: bool = False
    vs1: bool = False

    @property
    def demand_on(self) -> bool:
        """引脚 1 那条「请求」线是否有效。"""

        return self.l_req or self.u_req

    def as_dict(self) -> Dict[str, bool]:
        """返回便于日志/追踪的字典。"""

        return {
            "L_REQ": self.l_req,
            "U_REQ": self.u_req,
            "READY": self.ready,
            "HO_AVBL": self.ho_avbl,
            "ES": self.es,
            "VA": self.va,
            "VS_0": self.vs0,
            "VS_1": self.vs1,
        }

    def with_demand(self, op: Op) -> "Outputs":
        """返回把「请求」置为指定方向后的输出（互斥保证）。"""

        if op is Op.LOAD:
            return Outputs(
                l_req=True, u_req=False, ready=self.ready, ho_avbl=self.ho_avbl,
                es=self.es, va=self.va, vs0=self.vs0, vs1=self.vs1,
            )
        if op is Op.UNLOAD:
            return Outputs(
                l_req=False, u_req=True, ready=self.ready, ho_avbl=self.ho_avbl,
                es=self.es, va=self.va, vs0=self.vs0, vs1=self.vs1,
            )
        return Outputs(
            l_req=False, u_req=False, ready=self.ready, ho_avbl=self.ho_avbl,
            es=self.es, va=self.va, vs0=self.vs0, vs1=self.vs1,
        )
