"""单次交接（LOAD / UNLOAD）状态机测试。

覆盖标准条款（fig_10 装载 / fig_11 卸载）：

* §6.2.2.1(1)(2)、注3 —— 主动先给 ``CS_x``，再用 ``VALID``↑ 使其生效；
  被动方在此之前不校验 ``CS_x``；
* §6.2.2.1(3) —— 载口可装载则 ``L_REQ`` ON；可卸载则 ``U_REQ`` ON；
* §6.2.2.1(4)(5) —— ``TR_REQ``↑ 后被动方置 ``READY`` ON；
* §6.2.2.1(6) —— ``BUSY``↑ 表示主动机构进入交接干涉区（互锁）；
* §6.2.2.1(7) —— 载具到位/被取走后，请求线落下（判定依据是「载具位于正确位置」，§6.1 表1）；
* §6.2.2.1(10)(11) —— 收到 ``COMPT`` 后 ``READY`` 落下；
* §6.2.2.1(12)(13)、注5 —— ``VALID`` 落下即握手闭合；
* §6.1 表1 —— ``HO_AVBL``/``ES`` 在正常空闲时为 ON；
* 设计约定 —— 交接方向在 ``SELECT`` 一次性锁定，之后不再读实时传感器。
"""

from __future__ import annotations

import pytest

from e84.events import EventType
from e84.model import Op, PortRole, State
from tests.support import FsmHarness, assert_order, port


@pytest.fixture
def harness(iface):
    """两个空载口（LP1=left / LP2=right）的 FSM 驱动器。"""

    return FsmHarness(
        iface, [port("LP1", PortRole.LEFT), port("LP2", PortRole.RIGHT)]
    )


def _run_to_req_on(h: FsmHarness, *, cs0: bool = True, cs1: bool = False):
    """推进到「请求线已断言」：IDLE -> SELECT -> REQ_ON。"""

    h.step()  # IDLE
    h.advance(0.1)  # 主动设备选口（TD0）
    assert h.step(valid=True, cs0=cs0, cs1=cs1).state is State.SELECT  # §6.2.2.1(1)(2)
    return h.step()  # SELECT 内解码 + 断言请求


# --------------------------------------------------------------------------- #
# 单次 LOAD（fig_10）
# --------------------------------------------------------------------------- #
def test_single_load_state_by_state(harness):
    h = harness

    # --- IDLE：健康待命，HO_AVBL/ES 均为 ON，请求全落（§6.1 表1）---
    r = h.step()
    assert r.state is State.IDLE
    assert r.outputs.ho_avbl is True
    assert r.outputs.es is True
    assert r.outputs.demand_on is False
    assert r.outputs.ready is False

    # --- 1)2) CS_0 先给，VALID 上升沿使其有效（注3：此前不校验 CS）---
    h.advance(0.1)
    r = h.step(valid=True, cs0=True)
    assert r.state is State.SELECT
    assert r.outputs.demand_on is False  # 还没有断言请求
    assert EventType.HANDSHAKE_STARTED in h.types()

    # --- 3) 载口为空 -> L_REQ ON（方向由传感器推导）---
    r = h.step()
    assert r.state is State.REQ_ON
    assert r.op is Op.LOAD
    assert r.outputs.l_req is True
    assert r.outputs.u_req is False
    assert r.outputs.ready is False
    assert r.selected_port_ids == ("LP1",)

    # --- 4)5) TR_REQ↑ -> READY ON ---
    r = h.step(tr_req=True)
    assert r.state is State.WAIT_BUSY        # §6.2.2.1(5)
    assert r.outputs.ready is True
    assert r.outputs.l_req is True

    # --- 6) BUSY↑ -> 进入交接，互锁生效（§6.1 表1 BUSY）---
    r = h.step(busy=True)
    assert r.state is State.TRANSFER
    assert r.outputs.ready is True
    assert r.outputs.l_req is True
    assert r.interlock_engaged is True

    # --- 7) 载具仅「检测到」还不够，必须完整落位才撤请求线（§6.1 表1）---
    h.set_port("LP1", present=True, in_position=False)
    r = h.step()
    assert r.state is State.TRANSFER
    assert r.outputs.l_req is True

    # --- 7) 完整落位 -> 请求线 OFF，进入等 COMPT ---
    h.set_port("LP1", present=True, in_position=True)
    r = h.step(busy=False, tr_req=False)
    assert r.state is State.AWAIT_COMPT      # §6.2.2.1(7)
    assert r.outputs.l_req is False
    assert r.outputs.ready is True

    # --- 10)11) COMPT↑ -> READY 落下 ---
    r = h.step(compt=True)
    assert r.state is State.CLOSING          # §6.2.2.1(11)
    assert r.outputs.ready is False
    assert r.outputs.ho_avbl is True
    assert r.outputs.es is True

    # --- 12)13) VALID↓ -> 握手闭合，回 IDLE ---
    r = h.step(valid=False, compt=False)
    assert r.state is State.IDLE             # §6.2.2.1(13)
    assert r.outputs.demand_on is False
    assert r.outputs.ready is False
    assert r.outputs.ho_avbl is True
    assert r.interlock_engaged is False

    # 关键事件顺序（fig_10 主干）
    assert_order(
        h.types(),
        EventType.HANDSHAKE_STARTED,
        EventType.PORTS_SELECTED,
        EventType.DEMAND_ASSERTED,
        EventType.READY_ASSERTED,
        EventType.TRANSFER_STARTED,
        EventType.CARRIER_SETTLED,
        EventType.DEMAND_RELEASED,
        EventType.COMPT_RECEIVED,
        EventType.READY_RELEASED,
        EventType.HANDSHAKE_CLOSED,
    )


def test_single_load_uses_l_req_never_u_req(harness):
    h = harness
    _run_to_req_on(h)
    for _ in range(12):
        r = h.step()
        assert r.outputs.u_req is False
        assert not (r.outputs.l_req and r.outputs.u_req)


# --------------------------------------------------------------------------- #
# 单次 UNLOAD（fig_11）
# --------------------------------------------------------------------------- #
def test_single_unload_state_by_state(iface):
    h = FsmHarness(
        iface,
        [port("LP1", PortRole.LEFT, present=True, in_position=True), port("LP2", PortRole.RIGHT)],
    )
    h.step()
    h.advance(0.1)
    assert h.step(valid=True, cs0=True).state is State.SELECT

    # 载具在正确位置 -> U_REQ ON（§6.2.2.1(3)）
    r = h.step()
    assert r.state is State.REQ_ON
    assert r.op is Op.UNLOAD
    assert r.outputs.u_req is True
    assert r.outputs.l_req is False

    r = h.step(tr_req=True)
    assert r.state is State.WAIT_BUSY
    assert r.outputs.ready is True

    r = h.step(busy=True)
    assert r.state is State.TRANSFER
    assert r.interlock_engaged is True

    # 载具只是被「检测到」还没被取走：请求线必须保持 ON
    h.set_port("LP1", present=True, in_position=False)
    assert h.step().outputs.u_req is True

    # 载具被取走（carrier_present=False）-> U_REQ OFF（§6.2.2.1(7)）
    h.set_port("LP1", present=False, in_position=False)
    r = h.step(busy=False, tr_req=False)
    assert r.state is State.AWAIT_COMPT
    assert r.outputs.u_req is False
    assert EventType.CARRIER_SETTLED in h.types()

    r = h.step(compt=True)
    assert r.state is State.CLOSING
    r = h.step(valid=False, compt=False)
    assert r.state is State.IDLE

    assert_order(
        h.types(),
        EventType.DEMAND_ASSERTED,
        EventType.READY_ASSERTED,
        EventType.TRANSFER_STARTED,
        EventType.CARRIER_SETTLED,
        EventType.DEMAND_RELEASED,
        EventType.COMPT_RECEIVED,
        EventType.READY_RELEASED,
        EventType.HANDSHAKE_CLOSED,
    )


# --------------------------------------------------------------------------- #
# SELECT 阶段 VALID 落下 -> 回 IDLE
# --------------------------------------------------------------------------- #
def test_valid_dropped_during_select_returns_to_idle(harness):
    h = harness
    h.step()
    h.advance(0.1)
    assert h.step(valid=True, cs0=True).state is State.SELECT

    # 对方在被动方断言请求之前撤销 VALID（例如它自己发现 HO_AVBL=OFF）
    r = h.step(valid=False)
    assert r.state is State.IDLE
    assert r.outputs.demand_on is False
    assert r.outputs.ho_avbl is True
    assert EventType.HANDSHAKE_CLOSED in h.types()
    # 不产生任何故障
    assert h.fsm.fault is None


# --------------------------------------------------------------------------- #
# 方向在 SELECT 锁定
# --------------------------------------------------------------------------- #
def test_operation_locked_at_select_sensors_later_ignored(iface):
    """装载方向在选口时锁定；之后传感器翻转（载具已放上）不改变方向。"""

    h = FsmHarness(
        iface, [port("LP1", PortRole.LEFT), port("LP2", PortRole.RIGHT)]
    )
    r = _run_to_req_on(h)
    assert r.op is Op.LOAD and r.outputs.l_req is True

    # 选口后传感器"翻转"成"载具已在位"（本应被理解为卸载）
    h.set_port("LP1", present=True, in_position=True)
    r = h.step()
    assert r.op is Op.LOAD
    assert r.outputs.l_req is True
    assert r.outputs.u_req is False

    r = h.step(tr_req=True)
    assert r.op is Op.LOAD
    r = h.step(busy=True)
    assert r.op is Op.LOAD


def test_operation_locked_by_static_intent(iface):
    """上层用 ``expected_op`` 显式指定方向时，方向来源即被锁定。"""

    h = FsmHarness(
        iface,
        [
            port("LP1", PortRole.LEFT, expected_op=Op.UNLOAD, present=True, in_position=True),
            port("LP2", PortRole.RIGHT),
        ],
    )
    r = _run_to_req_on(h)
    assert r.op is Op.UNLOAD
    assert r.outputs.u_req is True


# --------------------------------------------------------------------------- #
# 载口不就绪：先等待，超时拉低 HO_AVBL（非故障）
# --------------------------------------------------------------------------- #
def test_port_not_ready_waits_then_pulls_ho_avbl(iface):
    """§6.2.5.1(a)：选口后若载口迟迟不就绪，用 ``HO_AVBL`` 表达"不可交接"。"""

    h = FsmHarness(
        iface,
        [
            port("LP1", PortRole.LEFT, ready_for_transfer=False),
            port("LP2", PortRole.RIGHT),
        ],
    )
    h.step()
    h.advance(0.1)
    assert h.step(valid=True, cs0=True).state is State.SELECT

    # 尚未就绪：不断言请求，只发 NOT_READY
    r = h.step()
    assert r.state is State.SELECT
    assert r.outputs.demand_on is False
    assert r.outputs.ho_avbl is True
    assert EventType.NOT_READY in h.types()

    # 宽限期（policy.not_ready_timeout_s，示例配置 1.5s）内仍然等待
    h.advance(1.0)
    r = h.step()
    assert r.state is State.SELECT

    # 超过宽限期 -> 拉低 HO_AVBL 中止（不是故障！）
    h.advance(0.6)
    r = h.step()
    assert r.state is State.HO_ABORT
    assert r.outputs.ho_avbl is False
    assert r.outputs.es is True          # 非故障：ES 保持 ON
    assert h.fsm.fault is None
    assert EventType.HO_ABORTED in h.types()
