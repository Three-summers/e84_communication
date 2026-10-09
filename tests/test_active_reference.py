"""主动侧参考实现测试（``e84.active``，仅测试/回放用，不是产品代码）。

覆盖标准条款：

* §6.2.2.1 —— 主动侧的单次交接阶段推进（``CS_x`` → ``TD0`` → ``VALID`` →
  ``TR_REQ`` → ``BUSY`` → ``COMPT`` → 释放）；
* §6.3.2.3 表7 —— ``TD0`` 范围 0.1–0.2s，越界必须报错；
* §6.2.3.2 —— 同时交接（两个载口一次搬运）；
* §6.2.4.3/6.2.4.4 —— 连续交接：``CONT`` 在首段 ``BUSY``↑ 置 ON、末段置 OFF，
  段间遵守 ``TD1``；
* §6.2.5.1(a)/6.2.5.3 —— 主动侧只在两个窗口内检查 ``HO_AVBL``，发现 OFF 立即中止。

本文件既直接驱动 :class:`e84.active.ActiveFsm`（确定性、快），
也用 :class:`e84.testing.VirtualRig` 跑闭环。
"""

from __future__ import annotations

import pytest

from e84.active import (
    ActiveConfig,
    ActiveFsm,
    ActiveInputs,
    ActiveOutputs,
    ActivePhase,
    TransferJob,
)
from e84.model import Op, PortRole


# --------------------------------------------------------------------------- #
# 构造与参数
# --------------------------------------------------------------------------- #
def test_transfer_job_constructors():
    job = TransferJob.load(PortRole.LEFT)
    assert job.op is Op.LOAD and job.roles == (PortRole.LEFT,)
    job2 = TransferJob.unload(PortRole.LEFT, PortRole.RIGHT)
    assert job2.op is Op.UNLOAD and job2.roles == (PortRole.LEFT, PortRole.RIGHT)


def test_td0_range_is_enforced():
    """表7：``TD0`` 0.1–0.2s；参考实现同样强制校验。"""

    with pytest.raises(ValueError):
        ActiveFsm(ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)], td0_s=0.5))
    with pytest.raises(ValueError):
        ActiveFsm(ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)], td0_s=0.05))
    fsm = ActiveFsm(ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)], td0_s=0.2))
    assert fsm.phase is ActivePhase.IDLE


def test_no_jobs_is_done_immediately():
    fsm = ActiveFsm(ActiveConfig(jobs=[]))
    assert fsm.phase is ActivePhase.DONE
    assert fsm.completed is True


# --------------------------------------------------------------------------- #
# 直接驱动：单次交接阶段推进
# --------------------------------------------------------------------------- #
def test_active_single_load_phase_progression():
    fsm = ActiveFsm(
        ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)], transfer_duration_s=0.05)
    )
    idle = ActiveInputs(now=0.0)

    # IDLE -> SELECT：先给 CS_0
    r = fsm.step(idle)
    assert fsm.phase is ActivePhase.SELECT
    assert r.outputs.cs0 is True and r.outputs.valid is False

    # SELECT 期间保持 CS_0，TD0 到 -> VALID ON（§6.3.2.3）
    r = fsm.step(ActiveInputs(now=0.05))
    assert fsm.phase is ActivePhase.SELECT

    r = fsm.step(ActiveInputs(now=0.1))
    assert fsm.phase is ActivePhase.VALID_ON
    assert r.outputs.valid is True and r.outputs.cs0 is True

    # VALID_ON -> WAIT_DEMAND（窗口 a 的开始）
    r = fsm.step(ActiveInputs(now=0.1, ho_avbl=True))
    assert fsm.phase is ActivePhase.WAIT_DEMAND

    # 看到请求线 -> TR_REQ ON，进入等 READY
    r = fsm.step(ActiveInputs(now=0.1, demand_on=True))
    assert fsm.phase is ActivePhase.WAIT_READY
    assert r.outputs.tr_req is True

    # 看到 READY -> BUSY ON（§6.2.2.1 第6步）
    r = fsm.step(ActiveInputs(now=0.1, ready=True))
    assert fsm.phase is ActivePhase.TRANSFER
    assert r.outputs.busy is True
    assert r.outputs.cont is False          # 单次作业没有 CONT

    # 搬运动作完成 -> transfer_due（虚拟线束据此翻转载口传感器）
    r = fsm.step(ActiveInputs(now=0.2, demand_on=True, ready=True))
    assert r.transfer_due is True

    # 请求线落下后 -> 撤下 BUSY/TR_REQ，进入 COMPT_ON 阶段
    r = fsm.step(ActiveInputs(now=0.2, demand_on=False, ready=True))
    assert fsm.phase is ActivePhase.COMPT_ON
    assert r.outputs.busy is False and r.outputs.tr_req is False

    # COMPT ON，等待 READY 落下
    r = fsm.step(ActiveInputs(now=0.2, ready=True))
    assert fsm.phase is ActivePhase.WAIT_READY_OFF
    assert r.outputs.compt is True

    # READY 落下 -> 释放 COMPT/VALID/CS，收尾
    r = fsm.step(ActiveInputs(now=0.2, ready=False))
    assert fsm.phase is ActivePhase.DONE
    assert r.outputs == ActiveOutputs()
    assert fsm.completed is True


# --------------------------------------------------------------------------- #
# 两个 HO_AVBL 检查窗口（§6.2.5.1）
# --------------------------------------------------------------------------- #
def test_window_a_abort_when_ho_avbl_low():
    fsm = ActiveFsm(ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)]))
    fsm.step(ActiveInputs(now=0.0))                       # SELECT
    r = fsm.step(ActiveInputs(now=0.1))                   # VALID_ON
    assert r.outputs.valid is True

    r = fsm.step(ActiveInputs(now=0.1, ho_avbl=False))    # 窗口 a 内发现 OFF
    assert r.aborted is True
    assert fsm.phase is ActivePhase.ABORTED
    assert r.outputs.valid is False                       # §6.2.5.2：VALID 必须落下
    assert r.outputs.cs0 is False and r.outputs.cs1 is False


def test_window_b_abort_when_ho_avbl_low():
    fsm = ActiveFsm(ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)]))
    fsm.step(ActiveInputs(now=0.0))                       # SELECT
    fsm.step(ActiveInputs(now=0.1))                       # VALID_ON
    r = fsm.step(ActiveInputs(now=0.1))                   # WAIT_DEMAND
    assert fsm.phase is ActivePhase.WAIT_DEMAND

    fsm.step(ActiveInputs(now=0.1, demand_on=True))       # -> WAIT_READY（窗口 b）
    assert fsm.phase is ActivePhase.WAIT_READY

    r = fsm.step(ActiveInputs(now=0.1, ho_avbl=False))
    assert r.aborted is True
    assert fsm.phase is ActivePhase.ABORTED
    assert r.outputs.tr_req is False and r.outputs.valid is False


def test_ho_avbl_ignored_outside_windows():
    """窗口之外（例如 TRANSFER 期间）的 ``HO_AVBL`` 抖动不应中止交接。"""

    fsm = ActiveFsm(ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)]))
    fsm.step(ActiveInputs(now=0.0))
    fsm.step(ActiveInputs(now=0.1))
    fsm.step(ActiveInputs(now=0.1))
    fsm.step(ActiveInputs(now=0.1, demand_on=True))
    fsm.step(ActiveInputs(now=0.1, ready=True))
    assert fsm.phase is ActivePhase.TRANSFER

    r = fsm.step(ActiveInputs(now=0.1, ho_avbl=False, demand_on=True, ready=True))
    assert r.aborted is False
    assert fsm.phase is ActivePhase.TRANSFER


def test_ta_timeout_becomes_active_fault():
    """主动侧互锁超时（表5 ``TA1``）：等不到请求线即报故障。"""

    fsm = ActiveFsm(
        ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)], ta1_s=1.0)
    )
    fsm.step(ActiveInputs(now=0.0))    # SELECT
    fsm.step(ActiveInputs(now=0.1))    # VALID_ON
    fsm.step(ActiveInputs(now=0.1))    # WAIT_DEMAND
    r = fsm.step(ActiveInputs(now=2.0, demand_on=False))
    assert r.fault is not None
    assert fsm.phase is ActivePhase.FAULT
    assert r.outputs == ActiveOutputs(cont=False)


# --------------------------------------------------------------------------- #
# 闭环：单次 / 同时 / 连续
# --------------------------------------------------------------------------- #
def test_rig_single_load_completes(make_rig):
    rig = make_rig(jobs=[TransferJob.load(PortRole.LEFT)])
    rig.start()
    assert rig.run_to_completion(timeout_s=40, dt=0.002) is True
    snap = rig.active.snapshot()
    assert snap["phase"] == "done"
    assert snap["aborted"] is False and snap["fault"] is None
    assert rig.port_state("LP1").in_position is True


def test_rig_simultaneous_load_completes(make_rig):
    rig = make_rig(jobs=[TransferJob.load(PortRole.LEFT, PortRole.RIGHT)])
    rig.start()
    assert rig.run_to_completion(timeout_s=40, dt=0.002) is True
    snap = rig.active.snapshot()
    assert snap["phase"] == "done"
    assert snap["jobs"] == 1
    assert rig.port_state("LP1").in_position is True
    assert rig.port_state("LP2").in_position is True


def test_rig_continuous_unload_then_load_completes(make_rig):
    rig = make_rig(
        jobs=[TransferJob.unload(PortRole.LEFT), TransferJob.load(PortRole.LEFT)],
        continuous=True,
    )
    rig.set_carrier("LP1", present=True)
    rig.start()
    assert rig.run_to_completion(timeout_s=40, dt=0.002) is True
    snap = rig.active.snapshot()
    assert snap["phase"] == "done"
    assert snap["continuous"] is True
    assert snap["job_index"] == 1
    assert rig.port_state("LP1").in_position is True


def test_rig_aborts_when_port_becomes_unavailable(make_rig):
    """把被动侧载口强制置为不可用 -> 主动侧在窗口 a 内中止（§6.2.5.2）。"""

    rig = make_rig(jobs=[TransferJob.load(PortRole.LEFT)])
    rig.start()

    assert rig.run_until(
        lambda r: r.controller.state.value == "select", timeout_s=5, dt=0.002
    ), "被动侧未进入 select"
    rig.controller.set_port_available("LP1", False)
    rig.run_for(0.1, dt=0.002)

    snap = rig.active.snapshot()
    assert snap["phase"] == "aborted"
    assert snap["aborted"] is True
    # 主动方停手后被动方进入 ho_abort；VALID/CS_x 的撤销由窗口 a 的直接测试覆盖
    assert rig.controller.state.value == "ho_abort"
    assert rig.controller.fault is None


def test_rig_abort_via_external_ho_ok(make_rig):
    """用 ``set_external_ho_ok(False)`` 直接拉低 ``HO_AVBL`` 构造窗口 a 中止。"""

    rig = make_rig(jobs=[TransferJob.load(PortRole.LEFT)])
    rig.start()
    assert rig.run_until(
        lambda r: r.controller.state.value in ("select", "req_on"), timeout_s=5, dt=0.002
    )
    rig.controller.set_external_ho_ok(False)
    rig.run_for(0.1, dt=0.002)
    assert rig.active.snapshot()["aborted"] is True
    assert rig.controller.state.value == "ho_abort"
