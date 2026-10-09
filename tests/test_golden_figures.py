"""黄金时序图测试（fig_10 / fig_11 / fig_14 / fig_16 / fig_17 / fig_18）。

本文件用 :class:`e84.testing.VirtualRig` 把「被动侧库 + 主动侧参考实现 + 载口传感器」
接成闭环，逐毫秒回放标准时序图，并断言**关键事件的先后顺序**。

各图对应的标准条款：

* **fig_10** 单次交接（LOAD）—— §6.2.2.1 第 1)–13) 步、注 3/注 4/注 5；
  互锁定器 ``TP1``–``TP5``（§6.3.2.1 表6）。
* **fig_11** 单次交接（UNLOAD）—— 同 §6.2.2.1；第 7) 步为"载口上的载具被取走"。
* **fig_14** 同时交接（LOAD）—— §6.2.3.1、§6.2.3.2 第 1)–4) 步
  （``CS_0`` 与 ``CS_1`` 同时 ON；请求线是**两个载口**的与语义）。
* **fig_16** 同一载口连续交接（UNLOAD → LOAD）—— §6.2.4.3；
  ``CONT`` 在首段 ``BUSY``↑ 置 ON、末段 ``BUSY``↑ 置 OFF；段间 ``VALID`` 落下再抬起，
  由 ``TP6``（表6）与 ``TD1``（表7）约束。
* **fig_17** 不同载口连续交接（LOAD → LOAD）—— §6.2.4.4（只改变 ``CS_x`` 指定方式）。
* **fig_18** ``HO_AVBL`` 喊停（情况 a）—— §6.2.5.1(a)、§6.2.5.2：
  主动设备发现 ``HO_AVBL`` OFF 后停手并把 ``VALID`` 置 OFF；
  被动设备在 ``VALID`` 落下之后才恢复 ``HO_AVBL``。

事件顺序断言使用「子序列」语义（允许中间夹杂其它事件）。
"""

from __future__ import annotations

from e84.active import TransferJob
from e84.events import EventType
from e84.model import PortRole
from tests.support import assert_order

#: fig_10/fig_11 主干事件顺序（§6.2.2.1 第 2)–13) 步）
_SINGLE_ORDER = (
    EventType.HANDSHAKE_STARTED,   # 第 2) 步
    EventType.PORTS_SELECTED,      # 第 1) 步解码
    EventType.DEMAND_ASSERTED,     # 第 3) 步
    EventType.READY_ASSERTED,      # 第 5) 步
    EventType.TRANSFER_STARTED,    # 第 6) 步
    EventType.CARRIER_SETTLED,     # 第 7) 步
    EventType.DEMAND_RELEASED,     # 第 7) 步
    EventType.COMPT_RECEIVED,      # 第 10) 步
    EventType.READY_RELEASED,      # 第 11) 步
    EventType.HANDSHAKE_CLOSED,    # 第 13) 步
)


def _types(rig):
    return [e.type for e in rig.events]


# --------------------------------------------------------------------------- #
# fig_10：单次 LOAD
# --------------------------------------------------------------------------- #
def test_golden_fig_10_single_load(make_rig):
    rig = make_rig(jobs=[TransferJob.load(PortRole.LEFT)])
    rig.start()
    assert rig.run_to_completion(timeout_s=40, dt=0.002) is True

    # 被动侧回到健康待命，主动侧完成
    assert rig.state == "idle"
    assert rig.active.snapshot()["phase"] == "done"
    assert rig.active.snapshot()["aborted"] is False

    # 载具最终完整落位在左载口
    lp1 = rig.port_state("LP1")
    assert lp1.present is True and lp1.in_position is True
    assert rig.port_state("LP2").present is False

    # 方向为装载：断言的是 L_REQ（§6.2.2.1 第 3) 步）
    demand = rig.events_of(EventType.DEMAND_ASSERTED)
    assert len(demand) == 1
    assert demand[0].data["op"] == "load"
    assert demand[0].port_ids == ("LP1",)

    # 关键事件顺序
    assert_order(_types(rig), *_SINGLE_ORDER)
    assert _types(rig).count(EventType.HANDSHAKE_CLOSED) == 1
    assert _types(rig).count(EventType.SEGMENT_COMPLETED) == 1
    # 单次交接不应出现批次事件（§6.2.4）
    assert EventType.BATCH_STARTED not in _types(rig)


# --------------------------------------------------------------------------- #
# fig_11：单次 UNLOAD
# --------------------------------------------------------------------------- #
def test_golden_fig_11_single_unload(make_rig):
    rig = make_rig(jobs=[TransferJob.unload(PortRole.LEFT)])
    rig.set_carrier("LP1", present=True)  # 初始：载具在正确位置
    rig.start()
    assert rig.run_to_completion(timeout_s=40, dt=0.002) is True

    assert rig.state == "idle"
    assert rig.active.snapshot()["phase"] == "done"

    lp1 = rig.port_state("LP1")
    assert lp1.present is False and lp1.in_position is False

    demand = rig.events_of(EventType.DEMAND_ASSERTED)
    assert len(demand) == 1
    assert demand[0].data["op"] == "unload"   # §6.2.2.1 第 3) 步：U_REQ

    assert_order(_types(rig), *_SINGLE_ORDER)
    assert _types(rig).count(EventType.HANDSHAKE_CLOSED) == 1


# --------------------------------------------------------------------------- #
# fig_14：同时交接 LOAD
# --------------------------------------------------------------------------- #
def test_golden_fig_14_simultaneous_load(make_rig):
    rig = make_rig(jobs=[TransferJob.load(PortRole.LEFT, PortRole.RIGHT)])
    rig.start()
    assert rig.run_to_completion(timeout_s=40, dt=0.002) is True

    assert rig.state == "idle"
    assert rig.active.snapshot()["phase"] == "done"

    # 两个载口都应落位（§6.2.3.2 第 4) 步）
    assert rig.port_state("LP1").in_position is True
    assert rig.port_state("LP2").in_position is True

    selected = rig.events_of(EventType.PORTS_SELECTED)
    assert len(selected) == 1
    assert selected[0].port_ids == ("LP1", "LP2")  # §6.2.3.2 第 1) 步
    assert selected[0].data["cs0"] is True and selected[0].data["cs1"] is True

    assert_order(_types(rig), *_SINGLE_ORDER)
    # 请求线只落下一次，且发生在两个载口都到位之后
    assert _types(rig).count(EventType.DEMAND_RELEASED) == 1
    settled = _types(rig).index(EventType.CARRIER_SETTLED)
    released = _types(rig).index(EventType.DEMAND_RELEASED)
    assert settled < released


# --------------------------------------------------------------------------- #
# fig_16：同一载口连续交接（UNLOAD → LOAD）
# --------------------------------------------------------------------------- #
def test_golden_fig_16_continuous_unload_then_load(make_rig):
    rig = make_rig(
        jobs=[TransferJob.unload(PortRole.LEFT), TransferJob.load(PortRole.LEFT)],
        continuous=True,
    )
    rig.set_carrier("LP1", present=True)
    rig.start()
    assert rig.run_to_completion(timeout_s=40, dt=0.002) is True

    assert rig.state == "idle"
    assert rig.active.snapshot()["phase"] == "done"
    assert rig.controller.fsm.batch_active is False  # 末段已结束批次

    # 先卸载后装载：最终载具在位
    lp1 = rig.port_state("LP1")
    assert lp1.present is True and lp1.in_position is True

    types = _types(rig)
    # 两段各一次单次握手（§6.2.4.3：连续交接是单次交接的拼接）
    assert types.count(EventType.SEGMENT_COMPLETED) == 2
    assert types.count(EventType.HANDSHAKE_CLOSED) == 2
    assert types.count(EventType.DEMAND_ASSERTED) == 2
    assert types.count(EventType.COMPT_RECEIVED) == 2

    # CONT 语义：首段 ON -> batch_started；末段 OFF -> batch_ended
    assert_order(
        types,
        EventType.BATCH_STARTED,      # 首段 BUSY↑ 时 CONT=ON
        EventType.SEGMENT_COMPLETED,
        EventType.BATCH_ENDED,        # 末段 BUSY↑ 时 CONT=OFF
    )
    # 两段方向分别为 unload / load（§6.2.4.3）
    ops = [e.data["op"] for e in rig.events_of(EventType.DEMAND_ASSERTED)]
    assert ops == ["unload", "load"]
    # 段间第二次握手带 TD1 观测值（表7）
    starts = rig.events_of(EventType.HANDSHAKE_STARTED)
    assert "td1_observed_s" in starts[1].data


# --------------------------------------------------------------------------- #
# fig_17：不同载口连续交接（LOAD → LOAD）
# --------------------------------------------------------------------------- #
def test_golden_fig_17_continuous_load_then_load(make_rig):
    rig = make_rig(
        jobs=[TransferJob.load(PortRole.LEFT), TransferJob.load(PortRole.RIGHT)],
        continuous=True,
    )
    rig.start()
    assert rig.run_to_completion(timeout_s=40, dt=0.002) is True

    assert rig.state == "idle"
    assert rig.active.snapshot()["phase"] == "done"
    assert rig.controller.fsm.batch_active is False
    assert rig.port_state("LP1").in_position is True
    assert rig.port_state("LP2").in_position is True

    selected = rig.events_of(EventType.PORTS_SELECTED)
    assert selected[0].port_ids == ("LP1",)
    assert selected[1].port_ids == ("LP2",)  # §6.2.4.4：只改变 CS_x 指定方式

    types = _types(rig)
    assert_order(
        types,
        EventType.BATCH_STARTED,
        EventType.SEGMENT_COMPLETED,
        EventType.BATCH_ENDED,
    )
    assert types.count(EventType.HANDSHAKE_CLOSED) == 2


# --------------------------------------------------------------------------- #
# fig_18：HO_AVBL 喊停（窗口 a）
# --------------------------------------------------------------------------- #
def test_golden_fig_18_ho_avbl_window_a(make_rig):
    """§6.2.5.1(a)/6.2.5.2：请求线已 ON 后把载口置为不可用。"""

    rig = make_rig(jobs=[TransferJob.load(PortRole.LEFT)])
    rig.start()

    # 推进到被动方已断言请求线（窗口 a 内）
    assert rig.run_until(
        lambda r: r.outputs().demand_on, timeout_s=5, dt=0.002
    ), "被动侧未在预期时间内断言请求线"
    assert rig.controller.state.value == "req_on"

    # 载口变为不可用 -> 被动方拉低 HO_AVBL，主动方中止并把 VALID 置 OFF
    rig.controller.set_port_available("LP1", False)
    rig.run_for(0.1, dt=0.002)

    assert rig.controller.state.value == "ho_abort"
    assert rig.outputs().ho_avbl is False
    assert rig.outputs().es is True            # 非故障
    assert rig.outputs().demand_on is False
    assert rig.controller.fault is None
    assert rig.active.snapshot()["aborted"] is True  # 主动侧按 6.2.5.2 停手

    types = _types(rig)
    assert_order(types, EventType.DEMAND_ASSERTED, EventType.HO_ABORTED, EventType.HO_AVBL_CHANGED)

    # VALID 落下后恢复可用 -> HO_AVBL 恢复 ON，回到 IDLE
    types_before = len(rig.events)
    rig.controller.set_port_available("LP1", True)
    rig.run_for(0.1, dt=0.002)
    assert rig.controller.state.value == "idle"
    assert rig.outputs().ho_avbl is True
    # 恢复 HO_AVBL 的事件发生在 VALID 落下之后（§6.2.5.2）
    new_events = [e.type for e in rig.events[types_before:]]
    assert EventType.HO_AVBL_CHANGED in new_events
