"""故障与互锁超时测试。

覆盖标准条款：

* §6.3.1.1 / §6.3.2.1 表6 —— 互锁超时 ``TP1``…``TP6`` 用于检测时序错误；
* §6.2.2.1 第8步 —— 主动设备必须在 ``BUSY`` 落下**之前**确认请求线已 OFF；
  否则为 ``BUSY_BEFORE_REQ_OFF``；
* §6.2.2.1 第8/9/10步、注4 —— 收到 ``COMPT`` 时 ``BUSY``/``TR_REQ`` 仍 ON
  属于协议违例（``COMPT_BEFORE_BUSY_OFF``）；
* §6.1.2.1/6.1.2.4 表2/表3 —— 非法 ``CS`` 组合；
* §6.1 表1 ``L_REQ``/``U_REQ`` —— 上层意图与传感器矛盾（``INTENT_MISMATCH``）；
* §6.2.5.1(a)/6.2.5.2 —— ``HO_AVBL`` 喊停：非故障，``ES`` 保持 ON，
  且在 ``VALID`` 落下之后才恢复 ``HO_AVBL``；
* 相关信息1 R1-1.1.2.1 —— 出错时 ``READY``/``ES``/``HO_AVBL`` OFF，
  **其余信号（含请求线）保持在出错时刻状态**；
* §6.3.3.1 —— 标准不定义恢复流程：本库默认**锁存**故障，需显式 ``clear_fault()``。
"""

from __future__ import annotations

import dataclasses

import pytest

from e84.events import EventType
from e84.fault import FaultCode
from e84.model import AccessMode, Op, PortRole, State
from e84.timers import TimerId
from tests.support import FsmHarness, port


# --------------------------------------------------------------------------- #
# 状态推进辅助
# --------------------------------------------------------------------------- #
def _idle_to_select(h: FsmHarness, **signals):
    h.step()
    h.advance(0.1)
    return h.step(valid=True, cs0=True, **signals)


def _to_req_on(h: FsmHarness):
    _idle_to_select(h)
    return h.step()


def _to_wait_busy(h: FsmHarness):
    _to_req_on(h)
    return h.step(tr_req=True)


def _to_transfer(h: FsmHarness):
    _to_wait_busy(h)
    return h.step(busy=True)


def _to_await_compt_load(h: FsmHarness):
    """装载方向推进到「请求线已落下、等 COMPT」。"""

    _to_transfer(h)
    h.set_port("LP1", present=True, in_position=True)
    return h.step(busy=False, tr_req=False)


def _to_closing(h: FsmHarness):
    _to_await_compt_load(h)
    return h.step(compt=True)


@pytest.fixture
def two_ports():
    return _two_ports()


def _two_ports():
    return [port("LP1", PortRole.LEFT), port("LP2", PortRole.RIGHT)]


def _h(iface, ports=None, **policy_changes):
    cfg = iface
    if policy_changes:
        cfg = dataclasses.replace(
            iface, policy=dataclasses.replace(iface.policy, **policy_changes)
        )
    return FsmHarness(cfg, ports if ports is not None else _two_ports())


def _assert_fault_picture(r, code: FaultCode, *, hold_demand: bool):
    """R1-1.1.2.1：READY/ES/HO_AVBL OFF，请求线保持在出错时刻状态。"""

    assert r.state is State.FAULT_WAIT_CLOSE
    assert r.fault is not None and r.fault.code is code
    assert r.outputs.ready is False
    assert r.outputs.es is False
    assert r.outputs.ho_avbl is False
    assert r.outputs.demand_on is hold_demand


# --------------------------------------------------------------------------- #
# TP1 / TP2 超时
# --------------------------------------------------------------------------- #
def test_tp1_timeout_holds_request_line(iface, two_ports):
    """TP1：请求线 ON 后等不到 ``TR_REQ``（表6）。"""

    h = _h(iface, two_ports)
    r = _to_req_on(h)
    assert r.state is State.REQ_ON and r.outputs.l_req is True

    h.advance(2.5)  # TP1 设定值 2s
    r = h.step()
    _assert_fault_picture(r, FaultCode.TP1_TIMEOUT, hold_demand=True)
    assert EventType.TIMER_EXPIRED in [e.type for e in r.events]
    assert EventType.FAULT_RAISED in [e.type for e in r.events]

    # VALID 落下 -> 故障锁存
    r = h.step(valid=False)
    assert r.state is State.FAULT_LATCHED
    assert r.outputs.demand_on is False  # 锁存后连请求线也不再保持
    assert EventType.HANDSHAKE_CLOSED in [e.type for e in r.events]

    # §6.3.3.1：显式清除故障后回 IDLE
    assert h.fsm.clear_fault(h.t) is True
    assert h.fsm.state is State.IDLE
    assert h.fsm.fault is None
    r = h.step()
    assert r.outputs.ho_avbl is True
    assert r.outputs.es is True


def test_tp1_clear_fault_refused_before_handshake_closed(iface, two_ports):
    h = _h(iface, two_ports)
    _to_req_on(h)
    h.advance(2.5)
    h.step()
    assert h.fsm.state is State.FAULT_WAIT_CLOSE
    assert h.fsm.clear_fault(h.t) is False          # 默认拒绝
    assert h.fsm.clear_fault(h.t, force=True) is True  # 人工强制复位
    assert h.fsm.state is State.IDLE


def test_tp1_timeout_can_pull_ho_avbl_instead_of_faulting(iface, two_ports):
    """``policy.on_timeout=ho_abort``：超时改用 ``HO_AVBL`` 喊停，不锁存故障。"""

    h = _h(iface, two_ports, on_timeout="ho_abort")
    _to_req_on(h)
    h.advance(2.5)
    r = h.step()
    assert r.state is State.HO_ABORT
    assert r.outputs.ho_avbl is False
    assert r.outputs.es is True
    assert r.fault is None


def test_tp2_timeout(iface, two_ports):
    """TP2：``READY`` ON 后等不到 ``BUSY``（表6）。"""

    h = _h(iface, two_ports)
    r = _to_wait_busy(h)
    assert r.state is State.WAIT_BUSY and r.outputs.ready is True

    h.advance(2.5)
    r = h.step()
    _assert_fault_picture(r, FaultCode.TP2_TIMEOUT, hold_demand=True)


# --------------------------------------------------------------------------- #
# TP3 / TP4 / TP5 超时
# --------------------------------------------------------------------------- #
def test_tp3_timeout(iface, two_ports):
    """TP3：``BUSY`` ON 后等不到载具到位（典型 60s，表6）。"""

    h = _h(iface, two_ports)
    _to_transfer(h)
    h.advance(61)
    r = h.step()
    _assert_fault_picture(r, FaultCode.TP3_TIMEOUT, hold_demand=True)
    assert h.fsm.timers.duration_of(TimerId.TP3) == 60  # 表6 典型值


def test_tp4_timeout(iface, two_ports):
    """TP4：请求线落下后等不到 ``COMPT``（典型 60s，表6）。"""

    h = _h(iface, two_ports)
    r = _to_await_compt_load(h)
    assert r.state is State.AWAIT_COMPT and r.outputs.demand_on is False

    h.advance(61)
    r = h.step()
    _assert_fault_picture(r, FaultCode.TP4_TIMEOUT, hold_demand=False)


def test_tp5_timeout(iface, two_ports):
    """TP5：``READY`` OFF 后等不到 ``VALID`` OFF（表6）。"""

    h = _h(iface, two_ports)
    r = _to_closing(h)
    assert r.state is State.CLOSING and r.outputs.ready is False

    h.advance(2.5)  # VALID 仍然保持 ON
    r = h.step()
    _assert_fault_picture(r, FaultCode.TP5_TIMEOUT, hold_demand=False)


# --------------------------------------------------------------------------- #
# 顺序违例
# --------------------------------------------------------------------------- #
def test_busy_off_before_request_released(iface, two_ports):
    """§6.2.2.1 第8步：请求线仍 ON 时对方撤下 ``BUSY``。"""

    h = _h(iface, two_ports)
    _to_transfer(h)
    r = h.step(busy=False, tr_req=False)  # 载具还没到位
    _assert_fault_picture(r, FaultCode.BUSY_BEFORE_REQ_OFF, hold_demand=True)


def test_busy_off_before_request_can_be_tolerated(iface, two_ports):
    h = _h(iface, two_ports, busy_before_req_off="tolerate")
    _to_transfer(h)
    r = h.step(busy=False, tr_req=False)
    assert r.state is State.TRANSFER      # 容忍：继续等载具到位/TP3
    assert r.fault is None


def test_compt_while_busy_still_on(iface, two_ports):
    """注4 与第8/9/10步：收到 ``COMPT`` 时 ``BUSY`` 仍 ON。"""

    h = _h(iface, two_ports)
    _to_transfer(h)                       # BUSY=ON
    h.set_port("LP1", present=True, in_position=True)
    r = h.step()                          # 请求线落下，但 BUSY 仍 ON
    assert r.state is State.AWAIT_COMPT
    r = h.step(compt=True)
    _assert_fault_picture(r, FaultCode.COMPT_BEFORE_BUSY_OFF, hold_demand=False)


def test_compt_while_busy_on_can_warn_only(iface, two_ports):
    h = _h(iface, two_ports, compt_before_busy_off="warn")
    _to_transfer(h)
    h.set_port("LP1", present=True, in_position=True)
    assert h.step().state is State.AWAIT_COMPT
    r = h.step(compt=True)
    assert r.state is State.CLOSING
    assert r.fault is None
    assert EventType.NOT_READY in [e.type for e in r.events]


# --------------------------------------------------------------------------- #
# 非法 CS / 意图矛盾
# --------------------------------------------------------------------------- #
def test_invalid_cs_combination_faults(iface, two_ports):
    h = _h(iface, two_ports)
    h.step()
    h.advance(0.1)
    h.step(valid=True, cs0=False, cs1=False)
    r = h.step()
    assert r.state is State.FAULT_WAIT_CLOSE
    assert r.fault.code is FaultCode.INVALID_CS_COMBINATION  # 表2/表3


def test_invalid_cs_combination_can_be_ignored(iface, two_ports):
    h = _h(iface, two_ports, invalid_cs="ignore")
    h.step()
    h.advance(0.1)
    h.step(valid=True, cs0=False, cs1=False)
    r = h.step()
    assert r.state is State.IDLE
    assert r.fault is None
    assert EventType.NOT_READY in [e.type for e in r.events]
    assert EventType.HANDSHAKE_CLOSED in [e.type for e in r.events]


def test_intent_mismatch_faults(iface):
    """上层指定 LOAD，但载口上已有载具（传感器说 UNLOAD）。"""

    h = _h(
        iface,
        [port("LP1", PortRole.LEFT, present=True, in_position=True, expected_op=Op.LOAD),
         port("LP2", PortRole.RIGHT)],
    )
    _idle_to_select(h)
    r = h.step()
    assert r.state is State.FAULT_WAIT_CLOSE
    assert r.fault.code is FaultCode.INTENT_MISMATCH
    # 尚未断言请求线
    assert r.outputs.demand_on is False
    assert r.outputs.ready is False


def test_intent_mismatch_can_follow_sensor(iface):
    h = _h(
        iface,
        [port("LP1", PortRole.LEFT, present=True, in_position=True, expected_op=Op.LOAD),
         port("LP2", PortRole.RIGHT)],
        intent_mismatch="follow_sensor",
    )
    _idle_to_select(h)
    r = h.step()
    assert r.state is State.REQ_ON
    assert r.op is Op.UNLOAD
    assert r.outputs.u_req is True
    assert r.fault is None


# --------------------------------------------------------------------------- #
# HO_AVBL 喊停（fig_18 窗口 a）
# --------------------------------------------------------------------------- #
def test_ho_unavailable_during_req_on_is_not_a_fault(iface, two_ports):
    """§6.2.5.1(a)/6.2.5.2：请求线 ON 后发现载口不可用 -> 拉低 ``HO_AVBL``。"""

    h = _h(iface, two_ports)
    r = _to_req_on(h)
    assert r.outputs.l_req is True
    assert r.outputs.ho_avbl is True

    h.set_port("LP1", available=False)
    r = h.step()
    assert r.state is State.HO_ABORT
    assert r.outputs.ho_avbl is False
    assert r.outputs.es is True            # 非故障：ES 保持 ON
    # 规范图 19：中止期间请求线**保持**（不提前撤），等握手闭合后才撤
    assert r.outputs.demand_on is True
    assert r.fault is None
    assert EventType.HO_ABORTED in [e.type for e in r.events]

    # VALID 落下 -> 请求线随之落下；载口仍不可用，所以 HO_AVBL 不得恢复（§6.2.5.2）
    r = h.step(valid=False)
    assert r.state is State.HO_ABORT
    assert r.outputs.demand_on is False, "握手闭合后必须撤下请求线"
    assert r.outputs.ho_avbl is False
    assert r.outputs.ho_avbl is False

    # 载口恢复可用后才回 IDLE 且 HO_AVBL 恢复 ON
    h.set_port("LP1", available=True)
    r = h.step()
    assert r.state is State.IDLE
    assert r.outputs.ho_avbl is True
    assert r.outputs.es is True


def test_ho_unavailable_at_handshake_start(iface, two_ports):
    h = _h(iface, two_ports)
    h.set_port("LP1", available=False)
    h.step()
    h.advance(0.1)
    r = h.step(valid=True, cs0=True)
    assert r.state is State.HO_ABORT
    assert r.outputs.ho_avbl is False
    assert r.fault is None


def test_manual_access_mode_pulls_ho_avbl_off(iface):
    """§5.14 / 表1 ``HO_AVBL``：手动模式（且策略未允许）应表达"不可交接"。"""

    h = _h(
        iface,
        [port("LP1", PortRole.LEFT, access_mode=AccessMode.MANUAL),
         port("LP2", PortRole.RIGHT)],
    )
    r = h.step()
    assert r.state is State.IDLE
    assert r.outputs.ho_avbl is False
    assert r.outputs.es is True
    assert r.fault is None

    # VALID↑ 时不可用 -> 直接进入 HO_ABORT，而不是 fault
    h.advance(0.1)
    r = h.step(valid=True, cs0=True)
    assert r.state is State.HO_ABORT
    assert r.fault is None


def test_external_ho_not_ok_pulls_ho_avbl_off(iface, two_ports):
    h = _h(iface, two_ports)
    r = h.step(external_ho_ok=False)
    assert r.outputs.ho_avbl is False
    assert r.outputs.es is True


def test_es_chain_open_pulls_es_and_ho_off(iface, two_ports):
    """§6.1 表1 ``ES``：安全链断开时 ``ES`` OFF（请求对方停止）。"""

    h = _h(iface, two_ports)
    r = h.step(es_ok=False)
    assert r.outputs.es is False
    assert r.outputs.ho_avbl is False
    assert r.fault is None


# --------------------------------------------------------------------------- #
# 前置条件丢失
# --------------------------------------------------------------------------- #
def test_precondition_lost_aborts_handshake(iface):
    cfg = dataclasses.replace(iface, preconditions=("VALID", "GO"))
    h = FsmHarness(cfg, _two_ports())
    h.step()
    h.advance(0.1)
    # 前置条件成立，握手可以开始
    r = h.step(valid=True, cs0=True, extra={"GO": True})
    assert r.state is State.SELECT

    r = h.step(extra={"GO": True})
    assert r.state is State.REQ_ON

    # 板级联锁丢失 -> 拉低 HO_AVBL 中止（非故障）
    r = h.step(extra={"GO": False})
    assert r.state is State.HO_ABORT
    assert r.outputs.ho_avbl is False
    assert r.fault is None


# --------------------------------------------------------------------------- #
# 外部故障上报与锁存
# --------------------------------------------------------------------------- #
def test_external_raise_fault_latches(iface, two_ports):
    h = _h(iface, two_ports)
    h.step()
    h.fsm.raise_fault(FaultCode.EXTERNAL, "执行机构报错", h.t)
    assert h.fsm.state is State.FAULT_WAIT_CLOSE
    assert h.fsm.fault.code is FaultCode.EXTERNAL

    r = h.step()  # VALID 本就为 OFF -> 立即锁存
    assert r.state is State.FAULT_LATCHED
    assert r.outputs.ready is False
    assert r.outputs.ho_avbl is False
    assert r.outputs.es is False
    assert h.fsm.clear_fault(h.t) is True
    assert h.fsm.state is State.IDLE


def test_raise_fault_is_idempotent(iface, two_ports):
    h = _h(iface, two_ports)
    h.step()
    h.fsm.raise_fault(FaultCode.EXTERNAL, "first", h.t)
    h.fsm.raise_fault(FaultCode.IO_ERROR, "second", h.t)
    assert h.fsm.fault.code is FaultCode.EXTERNAL
