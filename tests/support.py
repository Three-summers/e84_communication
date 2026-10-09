"""测试专用支撑代码（不属于库运行时，也不属于产品代码）。

这里集中放三类东西，避免在每个测试文件里重复：

1. :func:`port` —— 构造 :class:`e84.model.PortSnapshot` 的小工具；
2. :class:`FsmHarness` —— **直接逐拍驱动** :class:`e84.fsm.PassiveFsm` 的薄封装。
   比虚拟线束更快、更精确，适合"逐状态断言"；
3. 配置字典构造器与若干断言辅助（文档里称为"黄金时序"的辅助）。
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from e84.config.model import InterfaceConfig
from e84.controller import PiOController
from e84.events import Event, EventType
from e84.fsm import PassiveFsm, StepResult
from e84.model import (
    AccessMode,
    Inputs,
    Op,
    Outputs,
    PortRole,
    PortSnapshot,
    State,
)

__all__ = [
    "port",
    "FsmHarness",
    "logical_from_level",
    "level_for_logical",
    "event_types",
    "first_index",
    "assert_order",
    "base_config_dict",
    "assert_outputs_safe",
]


# --------------------------------------------------------------------------- #
# 载口快照 / 逻辑电平
# --------------------------------------------------------------------------- #
def port(
    port_id: str,
    role: PortRole = PortRole.SINGLE,
    *,
    present: bool = False,
    in_position: bool = False,
    available: bool = True,
    access_mode: AccessMode = AccessMode.AUTOMATIC,
    expected_op: Op = Op.UNKNOWN,
    door_open: bool = True,
    clamp_released: bool = True,
    ready_for_transfer: bool = True,
) -> PortSnapshot:
    """构造一个载口快照（默认：机构可用、无载具）。"""

    return PortSnapshot(
        port_id=port_id,
        role=role,
        carrier_present=present,
        carrier_in_position=in_position,
        door_open=door_open,
        clamp_released=clamp_released,
        ready_for_transfer=ready_for_transfer,
        available=available,
        access_mode=access_mode,
        expected_op=expected_op,
    )


def logical_from_level(active_high: bool, level: bool) -> bool:
    """物理电平 -> 逻辑值（与 :class:`e84.hal.SignalMap` 的约定一致）。"""

    return bool(level) if active_high else (not level)


def level_for_logical(active_high: bool, logical: bool) -> bool:
    """逻辑值 -> 物理电平。"""

    return bool(logical) if active_high else (not logical)


# --------------------------------------------------------------------------- #
# 事件顺序辅助
# --------------------------------------------------------------------------- #
def event_types(results_or_events: Iterable) -> List[EventType]:
    """从若干 :class:`StepResult`（或 :class:`Event`）里抽出事件类型序列。"""

    out: List[EventType] = []
    for item in results_or_events:
        if isinstance(item, StepResult):
            out.extend(e.type for e in item.events)
        else:
            out.append(item.type)
    return out


def first_index(sequence: Sequence, value: Any) -> int:
    """返回 ``value`` 在 ``sequence`` 中首次出现的下标；不存在返回 ``-1``。"""

    for i, item in enumerate(sequence):
        if item == value:
            return i
    return -1


def assert_order(sequence: Sequence, *expected: Any) -> None:
    """断言 ``expected`` 在 ``sequence`` 中**按此先后顺序**出现（可夹杂其它元素）。"""

    cursor = 0
    for want in expected:
        idx = -1
        for i in range(cursor, len(sequence)):
            if sequence[i] == want:
                idx = i
                break
        assert idx >= 0, f"事件顺序不符合期望：在 {sequence[cursor:]} 中没有找到 {want!r}"
        cursor = idx + 1


# --------------------------------------------------------------------------- #
# 直接驱动 PassiveFsm
# --------------------------------------------------------------------------- #
class FsmHarness:
    """逐拍驱动 :class:`PassiveFsm` 的小工具。

    用法::

        h = FsmHarness(iface, [port("LP1", PortRole.LEFT), port("LP2", PortRole.RIGHT)])
        h.step()                                  # IDLE
        r = h.step(valid=True, cs0=True)          # -> SELECT
        r = h.step()                              # SELECT 内解码并断言请求 -> REQ_ON

    **信号是电平且会被记住**（与真实总线一致）：``step`` 只覆盖显式给出的信号，
    其余保持上一拍的值。载口状态用 :meth:`set_port` 修改。
    """

    _SIGNAL_DEFAULTS: Mapping[str, Any] = {
        "valid": False,
        "cs0": False,
        "cs1": False,
        "tr_req": False,
        "busy": False,
        "compt": False,
        "cont": False,
        "am_avbl": False,
        "va": False,
        "vs0": False,
        "vs1": False,
        "es_ok": True,
        "external_ho_ok": True,
    }

    def __init__(
        self,
        iface: InterfaceConfig,
        ports: Sequence[PortSnapshot],
        *,
        now: float = 0.0,
        enabled: bool = True,
    ) -> None:
        self.iface = iface
        self.fsm = PassiveFsm(iface)
        self._ports: List[PortSnapshot] = list(ports)
        self.t = float(now)
        self.signals: Dict[str, Any] = dict(self._SIGNAL_DEFAULTS)
        self.extra: Dict[str, bool] = {}
        self.results: List[StepResult] = []
        if enabled:
            self.fsm.enable(self.t)

    # -------------------------------------------------------------- 状态
    @property
    def ports(self) -> Tuple[PortSnapshot, ...]:
        return tuple(self._ports)

    @property
    def state(self) -> State:
        return self.fsm.state

    def set_port(self, port_id: str, **changes: Any) -> PortSnapshot:
        """替换某个载口快照的字段（模拟传感器/机构变化）。

        支持 ``present`` / ``in_position`` 作为 ``carrier_present`` /
        ``carrier_in_position`` 的简写。
        """

        aliases = {"present": "carrier_present", "in_position": "carrier_in_position"}
        resolved = {aliases.get(k, k): v for k, v in changes.items()}
        for i, p in enumerate(self._ports):
            if p.port_id == port_id:
                self._ports[i] = dataclasses.replace(p, **resolved)
                return self._ports[i]
        raise KeyError(f"未知载口 {port_id!r}")

    def advance(self, dt: float) -> float:
        self.t += float(dt)
        return self.t

    # -------------------------------------------------------------- 推进
    def step(self, dt: float = 0.0, **signals: Any) -> StepResult:
        """推进一拍；``dt>0`` 时先推进虚拟时间，未给出的信号保持上一拍电平。"""

        if dt:
            self.advance(dt)
        extra = signals.pop("extra", None)
        unknown = set(signals) - set(self.signals)
        if unknown:
            raise TypeError(f"未知的 Inputs 字段: {sorted(unknown)}")
        self.signals.update(signals)
        if extra is not None:
            self.extra = dict(extra)
        inp = Inputs(
            now=self.t,
            ports=tuple(self._ports),
            extra=dict(self.extra),
            **self.signals,
        )
        result = self.fsm.step(inp)
        self.results.append(result)
        return result

    # -------------------------------------------------------------- 事件
    def events(self) -> List[Event]:
        return [e for r in self.results for e in r.events]

    def types(self) -> List[EventType]:
        return [e.type for e in self.events()]

    def last(self) -> StepResult:
        return self.results[-1]


# --------------------------------------------------------------------------- #
# 最小可用配置字典（校验器测试用）
# --------------------------------------------------------------------------- #
def base_config_dict() -> Dict[str, Any]:
    """一份可以通过全部校验的最小 2 载口标准配置（返回可自由修改的副本）。"""

    return copy.deepcopy(
        {
            "schema_version": 1,
            "name": "UNIT-TEST",
            "backend": "sim",
            "poll_interval_ms": 5,
            "interfaces": [
                {
                    "id": "PIO1",
                    "scenario": "standard",
                    "topology": "two_load_ports",
                    "load_ports": [
                        {
                            "id": "LP1",
                            "role": "left",
                            "carrier_present": "LP1_K0",
                            "carrier_in_position": "LP1_K0",
                        },
                        {"id": "LP2", "role": "right"},
                    ],
                    "inputs": {
                        "VALID": "IN1",
                        "CS_0": "IN2",
                        "CS_1": "IN3",
                        "TR_REQ": "IN5",
                        "BUSY": "IN6",
                        "COMPT": "IN7",
                        "CONT": "IN8",
                    },
                    "outputs": {
                        "L_REQ": "OUT1",
                        "U_REQ": "OUT1",
                        "READY": "OUT4",
                        "HO_AVBL": "OUT7",
                        "ES": "OUT8",
                    },
                    "timers": {"TP1": 2},
                    "policy": {"not_ready_timeout_s": 1.0},
                }
            ],
        }
    )


# --------------------------------------------------------------------------- #
# 输出安全态断言
# --------------------------------------------------------------------------- #
def assert_outputs_safe(controller: PiOController) -> None:
    """断言控制器的所有输出都在逻辑 OFF（上电安全态，§6.4.6 / R1-1.1.2.1）。"""

    report = controller.hal.verify_outputs(Outputs())
    for channel, (expected, actual) in report.items():
        assert actual is not None, f"后端不支持回读，无法校验 {channel!r}"
        assert actual == expected, (
            f"通道 {channel!r} 未落在安全态：期望电平 {expected}，实际 {actual}"
        )
