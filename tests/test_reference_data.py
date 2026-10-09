"""参考数据回归测试：把「从标准原文抄下来的常量」钉死。

立项原因：本项目最初依据的是一份**中文图文译文**，其中 Table 9（连接器引脚分配）
被错排成了 ``L_REQ``/``U_REQ`` 共用引脚 1、``READY``/``VS_0`` 共用引脚 4。
拿到 SEMI E84-0301 **原文 PDF** 后核对发现：**原文中每个信号各占一个引脚，没有任何共用**。

这个错误没有影响状态机逻辑，但污染了引脚常量、文档与配置注释——
属于"二手资料"造成的典型风险。本文件把从原文抄来的值全部钉住，
以后再有人改动 `db25_pins()` 或 `TIMER_SPECS` 会立刻失败。

数据来源：SEMI E84-0301（SEMI 1999, 2001）原文 PDF
* Table 1 —— 信号定义
* Table 5/6/7 —— 主动/被动/延时定时器
* Table 9 —— 被动设备侧连接器引脚分配
"""

from __future__ import annotations

import pytest

from e84.signals import (
    INTERBAY_SIGNALS,
    PASSIVE_INPUT_SIGNALS,
    PASSIVE_OUTPUT_SIGNALS,
    STANDARD_SIGNALS,
    Signal,
    db25_pins,
    direction_of,
    not_for_interbay,
)
from e84.signals import Direction
from e84.timers import TIMER_SPECS, ACTIVE_TIMERS, PASSIVE_TIMERS, TimerId

# --------------------------------------------------------------------------- #
# Table 9 —— 引脚分配（原文）
# --------------------------------------------------------------------------- #
#: 逐字抄自 SEMI E84-0301 原文 Table 9。
#: 注意 U_REQ=2、VA=3、READY=4、VS_0=5 —— 「OUT 编号」与信号清单不是顺序对应的。
TABLE_9_PINS = {
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

#: 原文中未分配给逻辑信号的引脚：9/13 为 NC，10/11/12 为 Reserved，22–25 为电源与地。
TABLE_9_NON_SIGNAL_PINS = {9, 10, 11, 12, 13, 22, 23, 24, 25}


def test_table9_pin_assignment_matches_original_standard():
    """每个信号的参考引脚必须等于原文 Table 9 的值。"""

    actual = {sig: db25_pins(sig) for sig in Signal}
    assert actual == TABLE_9_PINS, (
        "引脚表与 SEMI E84-0301 原文 Table 9 不一致。\n"
        f"  差异: { {k: (actual.get(k), v) for k, v in TABLE_9_PINS.items() if actual.get(k) != v} }"
    )


def test_table9_has_no_shared_pins():
    """原文中**没有任何两个信号共用引脚**。

    这条专门防住那份错排的中文译文（它让 L_REQ/U_REQ 同为引脚 1、
    READY/VS_0 同为引脚 4）。库仍然支持"现场把两个逻辑信号并在一条线上"，
    但那是接线选项，不是标准的引脚分配。
    """

    pins = [db25_pins(sig) for sig in Signal]
    assert len(pins) == len(set(pins)), (
        "有信号共用了引脚；SEMI E84-0301 原文 Table 9 中每个信号各占一个引脚。"
        f"重复: {sorted({p for p in pins if pins.count(p) > 1})}"
    )


def test_table9_pins_are_within_connector_and_avoid_reserved():
    """引脚号必须落在 DB-25 内，且不占用 NC/Reserved/电源脚。"""

    for sig in Signal:
        pin = db25_pins(sig)
        assert 1 <= pin <= 25, f"{sig.value} 的引脚号 {pin} 超出 DB-25 范围"
        assert pin not in TABLE_9_NON_SIGNAL_PINS, (
            f"{sig.value} 占用了非信号引脚 {pin}（NC / Reserved / 电源）"
        )
    assert {db25_pins(s) for s in Signal} | TABLE_9_NON_SIGNAL_PINS == set(range(1, 26)), (
        "Table 9 的 25 个引脚应当被「信号 + NC/Reserved/电源」完全覆盖，没有遗漏也没有重复"
    )


# --------------------------------------------------------------------------- #
# Table 1 —— 信号方向与场景归属
# --------------------------------------------------------------------------- #
def test_table1_directions():
    """P→A / A→P 的方向必须与原文 Table 1 一致。"""

    p2a = {
        Signal.L_REQ, Signal.U_REQ, Signal.READY, Signal.HO_AVBL, Signal.ES,
        Signal.VA, Signal.VS_0, Signal.VS_1,
    }
    a2p = {
        Signal.VALID, Signal.CS_0, Signal.CS_1, Signal.TR_REQ,
        Signal.BUSY, Signal.COMPT, Signal.CONT, Signal.AM_AVBL,
    }
    assert PASSIVE_OUTPUT_SIGNALS == p2a
    assert PASSIVE_INPUT_SIGNALS == a2p
    for sig in p2a:
        assert direction_of(sig) is Direction.P2A
    for sig in a2p:
        assert direction_of(sig) is Direction.A2P


def test_table1_interbay_exclusions():
    """``VALID``/``CS_0``/``CS_1`` 被原文明确标注为「不用于跨区 AMHS」。"""

    assert {s for s in Signal if not_for_interbay(s)} == {
        Signal.VALID, Signal.CS_0, Signal.CS_1
    }
    # 跨区专属信号必须只出现在 INTERBAY 集合里
    for sig in (Signal.VA, Signal.VS_0, Signal.VS_1, Signal.AM_AVBL):
        assert sig in INTERBAY_SIGNALS
        assert sig not in STANDARD_SIGNALS
    # 两套信令互不重叠地占用不同引脚（原文 Table 9 的 Remarks 印证）
    assert not (STANDARD_SIGNALS & {Signal.VA, Signal.AM_AVBL})


# --------------------------------------------------------------------------- #
# Table 5 / 6 / 7 —— 定时器
# --------------------------------------------------------------------------- #
#: 逐字抄自原文：``定时器: (监视区间, 范围下限, 范围上限, 典型值)``
TABLE_5_6_7_TIMERS = {
    # Table 5 Active Equipment Timer
    TimerId.TA1: ("VALID ON - L_REQ ON / VALID ON - U_REQ ON", 1, 999, 2),
    TimerId.TA2: ("TR_REQ ON - READY ON", 1, 999, 2),
    TimerId.TA3: ("COMPT ON - READY OFF", 1, 999, 2),
    # Table 6 Passive Equipment Timer
    TimerId.TP1: ("L_REQ ON - TR_REQ ON / U_REQ ON - TR_REQ ON", 1, 999, 2),
    TimerId.TP2: ("READY ON - BUSY ON", 1, 999, 2),
    TimerId.TP3: ("BUSY ON - CARRIER DETECT / BUSY ON - CARRIER REMOVE", 1, 999, 60),
    TimerId.TP4: ("U_REQ OFF - BUSY OFF / L_REQ OFF - BUSY OFF", 1, 999, 60),
    TimerId.TP5: ("READY OFF - VALID OFF", 1, 999, 2),
    TimerId.TP6: ("VALID OFF - VALID ON (Continuous handoff)", 1, 999, 2),
    # Table 7 Delay Timer
    TimerId.TD0: ("CS ON - VALID ON", 0.1, 0.2, 0.1),
    TimerId.TD1: ("VALID OFF - VALID ON", 1, 999, 1),
}


@pytest.mark.parametrize("timer_id", list(TABLE_5_6_7_TIMERS))
def test_timer_spec_matches_original_tables(timer_id):
    """范围与典型值必须等于原文 Table 5/6/7。"""

    interval, low, high, typical = TABLE_5_6_7_TIMERS[timer_id]
    spec = TIMER_SPECS[timer_id]
    assert spec.minimum_s == low, f"{timer_id.value} 范围下限不符"
    assert spec.maximum_s == high, f"{timer_id.value} 范围上限不符"
    assert spec.typical_s == typical, f"{timer_id.value} 典型值不符"
    # 监视区间：只做关键词核对，避免抄写差异导致的假失败
    for token in ("ON", "OFF"):
        if token in interval:
            assert token in spec.interval, f"{timer_id.value} 监视区间缺少 {token}"


def test_timer_ranges_follow_the_1_to_999_rule():
    """原文：除 ``TD0`` 外所有定时器范围都是 1–999 s（§6.3.2.1）。"""

    for timer_id, spec in TIMER_SPECS.items():
        if timer_id is TimerId.TD0:
            assert (spec.minimum_s, spec.maximum_s) == (0.1, 0.2)
        else:
            assert (spec.minimum_s, spec.maximum_s) == (1, 999), (
                f"{timer_id.value} 的范围应为 1–999 s"
            )


def test_timer_ownership():
    """``TAx`` 属主动设备、``TPx`` 属被动设备（§6.3.2.1）。"""

    assert set(PASSIVE_TIMERS) == {
        TimerId.TP1, TimerId.TP2, TimerId.TP3, TimerId.TP4, TimerId.TP5, TimerId.TP6
    }
    assert set(ACTIVE_TIMERS) == {
        TimerId.TA1, TimerId.TA2, TimerId.TA3, TimerId.TD0, TimerId.TD1
    }
    for tid in PASSIVE_TIMERS:
        assert TIMER_SPECS[tid].owner == "passive"
    for tid in ACTIVE_TIMERS:
        assert TIMER_SPECS[tid].owner == "active"


def test_tp3_tp4_typicals_are_60_seconds():
    """原文里只有 ``TP3``/``TP4`` 的典型值是 60 s（等机械动作），最容易抄错。"""

    assert TIMER_SPECS[TimerId.TP3].typical_s == 60
    assert TIMER_SPECS[TimerId.TP4].typical_s == 60
    assert [t.value for t in PASSIVE_TIMERS if TIMER_SPECS[t].typical_s == 60] == ["TP3", "TP4"]
