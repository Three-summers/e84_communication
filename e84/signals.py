"""E84 逻辑信号定义。

本模块只描述 **协议语义**，不涉及任何物理引脚或电平约定：

* 逻辑 ``True`` == 标准中的 **ON**（有效）；
* 逻辑 ``False`` == 标准中的 **OFF**（无效）。

物理电平（哪个电平算 ON）、上拉方向、去抖等全部由 :mod:`e84.hal` 依据配置处理。
引脚号仅作为 **参考信息**（SEMI E84-0301 表 9 原文），不参与任何逻辑判断——
实际接线以现场规格书为准，映射完全外置到配置。

.. note::

   **表 9 原文中每个信号各占一个引脚，没有任何两信号共用引脚。**

   曾经有一份中文译文把该表错排成了 ``L_REQ``/``U_REQ`` 共用引脚 1、
   ``READY``/``VS_0`` 共用引脚 4。核对 SEMI E84-0301 原文（Table 9）后的正确映射是：

   ============ ===== ============ =====
   信号          引脚   信号          引脚
   ============ ===== ============ =====
   ``L_REQ``     1     ``VALID``     14
   ``U_REQ``     2     ``CS_0``      15
   ``VA``        3     ``CS_1``      16
   ``READY``     4     ``AM_AVBL``   17
   ``VS_0``      5     ``TR_REQ``    18
   ``VS_1``      6     ``BUSY``      19
   ``HO_AVBL``   7     ``COMPT``     20
   ``ES``        8     ``CONT``      21
   ============ ===== ============ =====

   引脚 9/13 为 NC，10/11/12 为 Reserved，22–25 为电源与地。
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet

__all__ = [
    "Direction",
    "Signal",
    "PASSIVE_OUTPUT_SIGNALS",
    "PASSIVE_INPUT_SIGNALS",
    "STANDARD_SIGNALS",
    "INTERBAY_SIGNALS",
    "DEMAND_SIGNALS",
    "direction_of",
    "db25_pins",
    "not_for_interbay",
    "as_signal_name",
]


class Direction(str, Enum):
    """信号方向。P = 被动设备（装备端），A = 主动设备（AMHS 端）。"""

    P2A = "P->A"
    A2P = "A->P"


class Signal(str, Enum):
    """E84 并行 I/O 接口的全部逻辑信号（§6.1 表 1）。"""

    # ---- 被动设备 -> 主动设备（被动侧的 **输出**，本库驱动）----
    L_REQ = "L_REQ"
    U_REQ = "U_REQ"
    READY = "READY"
    HO_AVBL = "HO_AVBL"
    ES = "ES"
    # 跨区场景（被动 OHS）专用
    VA = "VA"
    VS_0 = "VS_0"
    VS_1 = "VS_1"

    # ---- 主动设备 -> 被动设备（被动侧的 **输入**）----
    VALID = "VALID"
    CS_0 = "CS_0"
    CS_1 = "CS_1"
    TR_REQ = "TR_REQ"
    BUSY = "BUSY"
    COMPT = "COMPT"
    CONT = "CONT"
    # 跨区场景专用
    AM_AVBL = "AM_AVBL"


PASSIVE_OUTPUT_SIGNALS: FrozenSet[Signal] = frozenset(
    {
        Signal.L_REQ,
        Signal.U_REQ,
        Signal.READY,
        Signal.HO_AVBL,
        Signal.ES,
        Signal.VA,
        Signal.VS_0,
        Signal.VS_1,
    }
)

PASSIVE_INPUT_SIGNALS: FrozenSet[Signal] = frozenset(
    {
        Signal.VALID,
        Signal.CS_0,
        Signal.CS_1,
        Signal.TR_REQ,
        Signal.BUSY,
        Signal.COMPT,
        Signal.CONT,
        Signal.AM_AVBL,
    }
)

#: 标准场景（装备 = 被动，OHT/AGV/RGV = 主动）使用的信号集合。
STANDARD_SIGNALS: FrozenSet[Signal] = frozenset(
    {
        Signal.VALID,
        Signal.CS_0,
        Signal.CS_1,
        Signal.TR_REQ,
        Signal.BUSY,
        Signal.COMPT,
        Signal.CONT,
        Signal.L_REQ,
        Signal.U_REQ,
        Signal.READY,
        Signal.HO_AVBL,
        Signal.ES,
    }
)

#: 跨区场景（被动 OHS）使用的信号集合。
#: 注意 §6.1 脚注：``VALID``/``CS_0``/``CS_1`` **不用于**跨区场景。
INTERBAY_SIGNALS: FrozenSet[Signal] = frozenset(
    {
        Signal.VA,
        Signal.VS_0,
        Signal.VS_1,
        Signal.AM_AVBL,
        Signal.TR_REQ,
        Signal.BUSY,
        Signal.COMPT,
        Signal.CONT,
        Signal.L_REQ,
        Signal.U_REQ,
        Signal.READY,
        Signal.HO_AVBL,
        Signal.ES,
    }
)

#: 「请求」信号：语义上互斥。表 9 原文中 ``L_REQ``=引脚 1、``U_REQ``=引脚 2（各自独立）；
#: 现场可能把它们并在一条线上，此时二者共享同一个物理通道。
DEMAND_SIGNALS: FrozenSet[Signal] = frozenset({Signal.L_REQ, Signal.U_REQ})

_DIRECTION: Dict[Signal, Direction] = {
    **{s: Direction.P2A for s in PASSIVE_OUTPUT_SIGNALS},
    **{s: Direction.A2P for s in PASSIVE_INPUT_SIGNALS},
}

#: 参考引脚号（SEMI E84-0301 表 9，被动设备侧连接器 A）。仅文档用途。
_DB25_PINS: Dict[Signal, int] = {
    Signal.L_REQ: 1,
    Signal.U_REQ: 2,
    Signal.VA: 3,
    Signal.READY: 4,
    Signal.VS_0: 5,
    Signal.VS_1: 6,
    Signal.HO_AVBL: 7,
    Signal.ES: 8,
    Signal.VALID: 14,
    Signal.CS_0: 15,
    Signal.CS_1: 16,
    Signal.AM_AVBL: 17,
    Signal.TR_REQ: 18,
    Signal.BUSY: 19,
    Signal.COMPT: 20,
    Signal.CONT: 21,
}

#: 表 9 中标记为「不用于跨区 AMHS」的信号。
_NOT_FOR_INTERBAY: FrozenSet[Signal] = frozenset(
    {Signal.VALID, Signal.CS_0, Signal.CS_1}
)


def direction_of(signal: Signal) -> Direction:
    """返回信号方向。"""

    return _DIRECTION[signal]


def db25_pins(signal: Signal) -> int:
    """返回该信号在 SEMI E84-0301 表 9（原文）中的参考引脚号。

    仅用于文档与提示；**不参与任何逻辑判断**。注意原文里没有两个信号共用引脚。
    """

    return _DB25_PINS[signal]


def not_for_interbay(signal: Signal) -> bool:
    """该信号是否被标准明确标注为「不用于跨区 AMHS」。"""

    return signal in _NOT_FOR_INTERBAY


def as_signal_name(name: str) -> "Signal | None":
    """把字符串解析为 :class:`Signal`；不是标准信号时返回 ``None``。

    用于区分「E84 标准信号」与「现场自定义信号」（如板级 ``GO``）。
    """

    try:
        return Signal(str(name).strip().upper().replace("-", "_"))
    except ValueError:
        return None
