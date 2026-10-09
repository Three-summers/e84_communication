"""同时交接测试（fig_14 同时 LOAD，UNLOAD 同理）。

覆盖标准条款：

* §6.1.2.4 / §6.2.3.2(1)(2) —— ``CS_0`` 与 ``CS_1`` 同时 ON 表示一次同时交接，
  在跳变生效之后再用 ``VALID``↑ 使其有效；
* §6.2.3.2(3) —— 只有**所有**被选中载口都具备条件时，``L_REQ``/``U_REQ`` 才 ON
  （请求线是所选中载口集合的**与**语义）；
* §6.2.3.2(4) —— 只有**所有**载口都检测到载具（或都被取走）时请求线才 OFF；
* §6.2.3.1 —— 同时交接能力可通过配置关闭（``features.simultaneous=false``）。
"""

from __future__ import annotations

import dataclasses

from e84.events import EventType
from e84.fault import FaultCode
from e84.model import Op, PortRole, State
from tests.support import FsmHarness, assert_order, port


def _two_ports(**kwargs):
    return [port("LP1", PortRole.LEFT, **kwargs), port("LP2", PortRole.RIGHT, **kwargs)]


def _to_simultaneous_req_on(h: FsmHarness):
    h.step()
    h.advance(0.1)
    assert h.step(valid=True, cs0=True, cs1=True).state is State.SELECT
    return h.step()


# --------------------------------------------------------------------------- #
# 同时 LOAD：两个载口都为空
# --------------------------------------------------------------------------- #
def test_simultaneous_load_selects_both_and_and_semantics(iface):
    h = FsmHarness(iface, _two_ports())
    r = _to_simultaneous_req_on(h)

    assert r.state is State.REQ_ON
    assert r.op is Op.LOAD
    assert r.selected_port_ids == ("LP1", "LP2")   # §6.2.3.2(1)
    assert r.outputs.l_req is True                 # §6.2.3.2(3)

    r = h.step(tr_req=True)
    assert r.outputs.ready is True
    r = h.step(busy=True)
    assert r.state is State.TRANSFER

    # 只有 LP1 落位：请求线**不得**落下（§6.2.3.2(4)）
    h.set_port("LP1", present=True, in_position=True)
    r = h.step()
    assert r.state is State.TRANSFER
    assert r.outputs.l_req is True
    assert EventType.CARRIER_SETTLED not in [e.type for e in r.events]

    # 两个都落位：请求线才落下
    h.set_port("LP2", present=True, in_position=True)
    r = h.step(busy=False, tr_req=False)
    assert r.state is State.AWAIT_COMPT
    assert r.outputs.l_req is False
    assert EventType.CARRIER_SETTLED in [e.type for e in r.events]

    r = h.step(compt=True)
    assert r.state is State.CLOSING
    r = h.step(valid=False, compt=False)
    assert r.state is State.IDLE

    assert_order(
        h.types(),
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


def test_simultaneous_load_demand_requires_both_ports_ready(iface):
    """其中一个载口已有载具时，不能断言 ``L_REQ``（与语义的选口侧）。"""

    h = FsmHarness(
        iface,
        [
            port("LP1", PortRole.LEFT),
            port("LP2", PortRole.RIGHT, present=True, in_position=True),
        ],
    )
    h.step()
    h.advance(0.1)
    assert h.step(valid=True, cs0=True, cs1=True).state is State.SELECT

    r = h.step()
    assert r.state is State.SELECT
    assert r.outputs.demand_on is False
    assert EventType.NOT_READY in h.types()

    # 宽限期内一直等待；超时后拉低 HO_AVBL（不是故障）
    h.advance(2.0)
    r = h.step()
    assert r.state is State.HO_ABORT
    assert r.outputs.ho_avbl is False
    assert h.fsm.fault is None


# --------------------------------------------------------------------------- #
# 同时 UNLOAD：两个载口都有载具
# --------------------------------------------------------------------------- #
def test_simultaneous_unload_and_semantics(iface):
    h = FsmHarness(iface, _two_ports(present=True, in_position=True))
    r = _to_simultaneous_req_on(h)

    assert r.state is State.REQ_ON
    assert r.op is Op.UNLOAD
    assert r.selected_port_ids == ("LP1", "LP2")
    assert r.outputs.u_req is True

    h.step(tr_req=True)
    h.step(busy=True)
    assert h.state is State.TRANSFER

    # 只取走一个：请求线保持 ON
    h.set_port("LP1", present=False, in_position=False)
    r = h.step()
    assert r.state is State.TRANSFER
    assert r.outputs.u_req is True

    # 两个都取走：请求线 OFF
    h.set_port("LP2", present=False, in_position=False)
    r = h.step(busy=False, tr_req=False)
    assert r.state is State.AWAIT_COMPT
    assert r.outputs.u_req is False


# --------------------------------------------------------------------------- #
# 单载口选择（CS_0 / CS_1 分别选左右）
# --------------------------------------------------------------------------- #
def test_single_cs_selects_one_port(iface):
    h = FsmHarness(iface, _two_ports())
    h.step()
    h.advance(0.1)
    h.step(valid=True, cs0=False, cs1=True)
    r = h.step()
    assert r.selected_port_ids == ("LP2",)
    assert r.op is Op.LOAD

    h2 = FsmHarness(iface, _two_ports())
    h2.step()
    h2.advance(0.1)
    h2.step(valid=True, cs0=True, cs1=False)
    r2 = h2.step()
    assert r2.selected_port_ids == ("LP1",)


def test_cs_combination_is_validated_only_after_valid(iface):
    """注3：``VALID`` 之前不校验 ``CS_x``；只有 ``VALID`` ON 后才解码。"""

    h = FsmHarness(iface, _two_ports())
    r = h.step(cs0=True, cs1=True)  # 没有 VALID
    assert r.state is State.IDLE
    assert h.fsm.fault is None


# --------------------------------------------------------------------------- #
# 关闭同时交接能力
# --------------------------------------------------------------------------- #
def test_simultaneous_unsupported_faults_by_default(iface):
    cfg = dataclasses.replace(iface, enable_simultaneous=False)
    h = FsmHarness(cfg, _two_ports())
    h.step()
    h.advance(0.1)
    h.step(valid=True, cs0=True, cs1=True)
    r = h.step()  # SELECT 内解码 -> 报故障
    assert r.state is State.FAULT_WAIT_CLOSE
    assert r.fault is not None
    assert r.fault.code is FaultCode.SIMULTANEOUS_NOT_SUPPORTED  # §6.1.2.4


def test_simultaneous_unsupported_can_ho_abort(iface):
    policy = dataclasses.replace(iface.policy, unsupported_simultaneous="ho_abort")
    cfg = dataclasses.replace(iface, enable_simultaneous=False, policy=policy)
    h = FsmHarness(cfg, _two_ports())
    h.step()
    h.advance(0.1)
    h.step(valid=True, cs0=True, cs1=True)
    r = h.step()
    assert r.state is State.HO_ABORT
    assert r.outputs.ho_avbl is False
    assert r.outputs.es is True
    assert h.fsm.fault is None
    assert EventType.HO_ABORTED in h.types()
