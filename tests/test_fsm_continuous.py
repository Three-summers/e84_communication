"""连续交接测试（fig_16 同载口 UNLOAD→LOAD；fig_17 不同载口 LOAD→LOAD）。

覆盖标准条款：

* §6.2.4.3 —— ``CONT`` 在**首段** ``BUSY``↑ 置 ON、在**末段** ``BUSY``↑ 置 OFF，
  用于告诉被动方"这批活后面还有没有"；
* §6.2.4.3 —— 段与段之间 ``VALID`` 落下再抬起，被动方进入 ``cont_next`` 等下一段；
* §6.2.4.4 —— 不同载口的连续交接只改变 ``CS_x`` 的指定方式；
* 表6 ``TP6`` —— ``VALID`` OFF → ``VALID`` ON 的等待超时；按 ``policy.tp6_timeout``
  决定回 ``idle`` 还是报故障；
* 表7 ``TD1`` —— 段间隔可编程（本库以"监视"模式记录实际间隔）；
* 实现约定 —— ``CONT`` 在 ``BUSY`` 上升沿采样，并带 ``cont_sample_delay_ms``
  的稳定延时以容忍两根线的偏斜。
"""

from __future__ import annotations

import dataclasses

from e84.events import EventType
from e84.fault import FaultCode
from e84.model import Op, PortRole, State
from tests.support import FsmHarness, assert_order, port


def _segment(
    h: FsmHarness,
    *,
    cont: bool,
    cs0: bool = True,
    cs1: bool = False,
    settle_port: str,
    present: bool,
    in_position: bool,
    expect_op: Op,
    sample_delay_s: float = 0.02,
):
    """跑完一段完整的单次握手，返回 **VALID 落下那一拍**的结果。"""

    h.advance(0.1)  # 主动设备选口（TD0）
    h.step(valid=True, cs0=cs0, cs1=cs1)
    r = h.step()
    assert r.op is expect_op, f"方向判定错误：期望 {expect_op}，实际 {r.op}"

    h.step(tr_req=True)
    h.step(busy=True, cont=cont)          # BUSY 上升沿（CONT 在此附近被采样）
    if sample_delay_s:
        h.advance(sample_delay_s)
        h.step()                          # 采样 CONT

    h.set_port(settle_port, present=present, in_position=in_position)
    h.step(busy=False, tr_req=False)      # 请求线已落下，主动退出干涉区
    h.step(compt=True)                    # COMPT ON
    return h.step(valid=False, compt=False)  # 握手闭合


# --------------------------------------------------------------------------- #
# fig_16：同一载口 UNLOAD → LOAD
# --------------------------------------------------------------------------- #
def _fig16_harness(iface):
    return FsmHarness(
        iface,
        [
            port("LP1", PortRole.LEFT, present=True, in_position=True),
            port("LP2", PortRole.RIGHT),
        ],
    )


def test_continuous_same_port_first_cont_on_then_off(iface):
    h = _fig16_harness(iface)

    # 首段：卸载，CONT=ON（后面还有）
    r = _segment(
        h,
        cont=True,
        settle_port="LP1",
        present=False,
        in_position=False,
        expect_op=Op.UNLOAD,
    )
    assert r.state is State.CONT_NEXT          # §6.2.4.3 段间等待
    assert h.fsm.batch_active is True
    assert EventType.BATCH_STARTED in h.types()
    assert EventType.SEGMENT_COMPLETED in h.types()

    # 末段：装载同一载口，CONT=OFF（这是最后一段）
    r = _segment(
        h,
        cont=False,
        settle_port="LP1",
        present=True,
        in_position=True,
        expect_op=Op.LOAD,
    )
    assert r.state is State.IDLE
    assert h.fsm.batch_active is False
    assert h.fsm.fault is None

    types = h.types()
    assert_order(
        types,
        EventType.BATCH_STARTED,
        EventType.SEGMENT_COMPLETED,
        EventType.BATCH_ENDED,
    )
    # 两段各闭合一次握手
    assert types.count(EventType.HANDSHAKE_CLOSED) == 2
    assert types.count(EventType.SEGMENT_COMPLETED) == 2
    # 第二段 HANDSHAKE_STARTED 应带 TD1 观测值
    second_start = [
        e for e in h.events() if e.type is EventType.HANDSHAKE_STARTED
    ][1]
    assert "td1_observed_s" in second_start.data


def test_continuous_first_segment_cont_off_finishes_immediately(iface):
    """CONT 在首段 BUSY↑ 为 OFF -> 不进入连续交接：闭合后直接回 IDLE。"""

    h = _fig16_harness(iface)
    r = _segment(
        h,
        cont=False,
        settle_port="LP1",
        present=False,
        in_position=False,
        expect_op=Op.UNLOAD,
    )
    assert r.state is State.IDLE
    assert h.fsm.batch_active is False
    assert EventType.BATCH_STARTED not in h.types()
    assert EventType.BATCH_ENDED not in h.types()


# --------------------------------------------------------------------------- #
# fig_17：不同载口 LOAD → LOAD
# --------------------------------------------------------------------------- #
def test_continuous_different_ports_load_then_load(iface):
    h = FsmHarness(
        iface, [port("LP1", PortRole.LEFT), port("LP2", PortRole.RIGHT)]
    )

    r = _segment(
        h,
        cont=True,
        cs0=True,
        cs1=False,
        settle_port="LP1",
        present=True,
        in_position=True,
        expect_op=Op.LOAD,
    )
    assert r.state is State.CONT_NEXT
    assert h.fsm.batch_active is True

    # 第二段换到 CS_1（右载口），CONT=OFF
    r = _segment(
        h,
        cont=False,
        cs0=False,
        cs1=True,
        settle_port="LP2",
        present=True,
        in_position=True,
        expect_op=Op.LOAD,
    )
    assert r.state is State.IDLE
    assert h.fsm.batch_active is False

    # 两个载口最终都应落位
    lp1 = next(p for p in h.ports if p.port_id == "LP1")
    lp2 = next(p for p in h.ports if p.port_id == "LP2")
    assert (lp1.carrier_present, lp1.carrier_in_position) == (True, True)
    assert (lp2.carrier_present, lp2.carrier_in_position) == (True, True)

    selected = [e for e in h.events() if e.type is EventType.PORTS_SELECTED]
    assert selected[0].port_ids == ("LP1",)
    assert selected[1].port_ids == ("LP2",)


# --------------------------------------------------------------------------- #
# CONT 的边沿采样语义
# --------------------------------------------------------------------------- #
def test_cont_only_sampled_at_busy_rising_edge(iface):
    """CONT 在 SELECT/REQ_ON 期间的电平无意义；只有 BUSY↑ 附近那一拍才算数。"""

    h = FsmHarness(iface, [port("LP1", PortRole.LEFT), port("LP2", PortRole.RIGHT)])
    h.step()
    h.advance(0.1)
    h.step(valid=True, cs0=True, cont=True)   # VALID 期间就抬 CONT...
    h.step()
    h.step(tr_req=True)
    h.step(busy=True, cont=False)             # ...但 BUSY↑ 时是 OFF -> 非连续
    h.advance(0.02)
    h.step()
    assert h.fsm.batch_active is False
    assert EventType.BATCH_STARTED not in h.types()

    h.set_port("LP1", present=True, in_position=True)
    h.step(busy=False, tr_req=False)
    h.step(compt=True)
    r = h.step(valid=False, compt=False)
    assert r.state is State.IDLE


def test_cont_sampled_after_configured_delay(iface):
    """``cont_sample_delay_ms`` 决定 BUSY↑ 之后多久采样 CONT（容忍线间偏斜）。"""

    h = FsmHarness(iface, [port("LP1", PortRole.LEFT), port("LP2", PortRole.RIGHT)])
    h.step()
    h.advance(0.1)
    h.step(valid=True, cs0=True)
    h.step()
    h.step(tr_req=True)

    r = h.step(busy=True, cont=True)
    assert r.state is State.TRANSFER
    # 默认 cont_sample_delay_ms=10ms：上升沿这一拍尚未采样
    assert h.fsm.batch_active is False
    assert EventType.BATCH_STARTED not in [e.type for e in r.events]

    h.advance(0.02)  # 超过稳定延时
    r = h.step()
    assert h.fsm.batch_active is True
    assert EventType.BATCH_STARTED in [e.type for e in r.events]


def test_zero_cont_sample_delay_samples_immediately(iface):
    policy = dataclasses.replace(iface.policy, cont_sample_delay_ms=0)
    cfg = dataclasses.replace(iface, policy=policy)
    h = FsmHarness(cfg, [port("LP1", PortRole.LEFT), port("LP2", PortRole.RIGHT)])
    h.step()
    h.advance(0.1)
    h.step(valid=True, cs0=True)
    h.step()
    h.step(tr_req=True)
    r = h.step(busy=True, cont=True)
    assert h.fsm.batch_active is True
    assert EventType.BATCH_STARTED in [e.type for e in r.events]


# --------------------------------------------------------------------------- #
# 段间 TP6 超时
# --------------------------------------------------------------------------- #
def test_tp6_timeout_policy_idle(iface):
    """``policy.tp6_timeout=idle``：段间隔超时后安静地结束批次。"""

    assert iface.policy.tp6_timeout == "idle"
    h = _fig16_harness(iface)
    r = _segment(
        h,
        cont=True,
        settle_port="LP1",
        present=False,
        in_position=False,
        expect_op=Op.UNLOAD,
    )
    assert r.state is State.CONT_NEXT

    h.advance(2.5)  # TP6 典型值 2s
    r = h.step()
    assert r.state is State.IDLE
    assert r.fault is None
    assert h.fsm.batch_active is False
    assert EventType.TIMER_EXPIRED in [e.type for e in r.events]
    assert EventType.BATCH_ENDED in [e.type for e in r.events]


def test_tp6_timeout_policy_fault(iface):
    """``policy.tp6_timeout=fault``：段间隔超时按互锁超时故障处理（表6 TP6）。"""

    policy = dataclasses.replace(iface.policy, tp6_timeout="fault")
    cfg = dataclasses.replace(iface, policy=policy)
    h = FsmHarness(
        cfg,
        [
            port("LP1", PortRole.LEFT, present=True, in_position=True),
            port("LP2", PortRole.RIGHT),
        ],
    )
    r = _segment(
        h,
        cont=True,
        settle_port="LP1",
        present=False,
        in_position=False,
        expect_op=Op.UNLOAD,
    )
    assert r.state is State.CONT_NEXT

    h.advance(2.5)
    r = h.step()
    assert r.state is State.FAULT_WAIT_CLOSE
    assert r.fault is not None
    assert r.fault.code is FaultCode.TP6_TIMEOUT
    # 故障画像（R1-1.1.2.1）：READY/ES/HO_AVBL 全 OFF
    assert r.outputs.ready is False
    assert r.outputs.es is False
    assert r.outputs.ho_avbl is False


def test_batch_terminated_when_port_becomes_unavailable(iface):
    """段间载口转为不可用 -> 结束批次并拉低 HO_AVBL，不锁存故障。"""

    h = _fig16_harness(iface)
    r = _segment(
        h,
        cont=True,
        settle_port="LP1",
        present=False,
        in_position=False,
        expect_op=Op.UNLOAD,
    )
    assert r.state is State.CONT_NEXT
    assert h.fsm.batch_active is True

    h.set_port("LP1", available=False)
    r = h.step()
    assert r.state is State.IDLE
    assert r.outputs.ho_avbl is False
    assert h.fsm.batch_active is False
    assert r.fault is None
    assert EventType.BATCH_ENDED in [e.type for e in r.events]
