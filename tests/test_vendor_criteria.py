"""以**第三方公开的符合性判据**为准的测试。

背景：我搜过公开渠道，**不存在开源的第三方 E84 实现**（GitHub 仓库搜索
``HO_AVBL`` / ``semi e84 handoff`` / ``wafer carrier handoff`` 均为 0 结果，
PyPI 无 ``e84`` 包）。能作为独立参照的只有商业仿真器，例如
`Get Control / GCI 的 E84 Emulator <https://www.getcontrol.com/downloads/E84_Emulator_Application_Users_Manual.pdf>`_
与 `E84 SPC <https://getcontrol.com/downloads/E84_SPC_SW_Tech_Ref_Manual_5E.pdf>`_。

这些工具的**代码**拿不到，但它们的**判据**是公开的——手册里以
"Failure - ..." 的形式列出了判定失败的信号行为。这些判据来自第三方对同一份标准
的实现经验，因此**不与我共享同一份理解**，正好可以用来打破"同源自测"的循环。

下面每一条都注明它对应手册里的哪句判据。
"""

from __future__ import annotations

import pytest

from e84 import (
    AccessMode,
    EventType,
    ManualRunner,
    SimIO,
    State,
    VirtualClock,
    load_file,
)
from e84.config.model import EquipmentConfig


@pytest.fixture
def env(cfg: EquipmentConfig):
    """SimIO + VirtualClock 的控制器环境（两载口都空）。"""

    from e84 import Equipment

    io, clk = SimIO(), VirtualClock()
    eq = Equipment(cfg, clock=clk, ios={"PIO1": io}, validate=False)
    runner = ManualRunner(eq)
    runner.start(now=clk.now())
    controller = eq.controller("PIO1")
    for key in ("LP1_K0", "LP1_K1", "LP1_K2", "LP2_K0", "LP2_K1", "LP2_K2"):
        io.set_input(key, True)
    events: list = []
    eq.bus.subscribe(None, events.append)
    return io, clk, eq, runner, controller, events


def _tick(runner, clk, n=1, dt=0.02):
    for _ in range(n):
        clk.advance(dt)
        runner.poll(now=clk.now())


# --------------------------------------------------------------------------- #
# 判据 1（厂商手册）:
#   "Failure - HO_AVBL signal did not turn ON following operator switch to
#    automated access mode"
#   → 操作员把载口从「手动」切回「自动」之后，HO_AVBL 必须重新变 ON。
#     这是在测**恢复方向**：只测"手动时拉低"是不够的，拉低之后回不来同样是故障。
# --------------------------------------------------------------------------- #
def test_ho_avbl_returns_on_after_switch_back_to_automated_access_mode(env):
    io, clk, eq, runner, controller, events = env
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is True

    # 操作员切到手动 -> HO_AVBL 必须拉低（§6.1 表1：切到手动访问模式属异常条件）
    controller.set_access_mode("LP1", AccessMode.MANUAL)
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is False

    # 操作员切回自动 -> HO_AVBL 必须恢复 ON
    controller.set_access_mode("LP1", AccessMode.AUTOMATIC)
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is True, (
        "操作员切回自动访问模式后 HO_AVBL 必须重新 ON"
        "（厂商手册列为 'Failure' 的行为）"
    )
    # 恢复过程应当有可观测的边沿事件，便于上位机记录
    changed = [e for e in events if e.type is EventType.HO_AVBL_CHANGED]
    assert len(changed) >= 2
    runner.stop(now=clk.now())


# --------------------------------------------------------------------------- #
# 判据 2（厂商手册）:
#   "Failure - VALID signal did not turn OFF following drop of HO_AVBL signal"
#   → 被动方拉低 HO_AVBL 之后，**必须等主动方把 VALID 落下**才算握手闭合；
#     在那之前，即使本机条件已经恢复正常，也**不得**把 HO_AVBL 重新拉高。
#     （§6.2.5.2："被动设备在 VALID 信号被置 OFF 之后，才把 HO_AVBL 重新置 ON"）
# --------------------------------------------------------------------------- #
def test_ho_avbl_is_not_restored_while_valid_is_still_on(env):
    io, clk, eq, runner, controller, events = env

    # 建立握手并断言请求线（窗口 a）
    io.set_input("IN2", False)   # CS_0 = ON
    io.set_input("IN1", False)   # VALID = ON
    _tick(runner, clk, 3)
    assert controller.outputs().demand_on is True

    # 载口转为不可用 -> 被动拉低 HO_AVBL 并撤回输出（§6.2.5.2）
    controller.set_port_available("LP1", False)
    _tick(runner, clk, 2)
    assert controller.state is State.HO_ABORT
    assert controller.outputs().ho_avbl is False
    # 规范图 19 + 仿真器 Functionality Test G：中止期间请求线**保持**
    assert controller.outputs().demand_on is True
    assert controller.outputs().es is True, "这不是故障，ES 必须保持 ON"

    # 本机条件恢复正常，但主动方还没撤 VALID -> **不得**恢复 HO_AVBL，也不得提前撤请求线
    controller.set_port_available("LP1", True)
    _tick(runner, clk, 5)
    assert controller.state is State.HO_ABORT
    assert controller.outputs().ho_avbl is False, (
        "VALID 仍为 ON 时不得恢复 HO_AVBL（§6.2.5.2；厂商手册列为 Failure）"
    )
    assert controller.outputs().demand_on is True, (
        "VALID 仍为 ON 时不得提前撤下请求线（规范图 19）"
    )

    # 主动方撤下 VALID -> 现在才允许恢复
    io.set_input("IN1", True)
    io.set_input("IN2", True)
    _tick(runner, clk, 3)
    assert controller.state is State.IDLE
    assert controller.outputs().ho_avbl is True
    runner.stop(now=clk.now())


# --------------------------------------------------------------------------- #
# 判据 3：厂商手册把检查窗口 a 与 b 分开描述（§6.2.5.1 a/b、图 18/图 19）。
#   窗口 a = VALID ON  → 请求线 ON
#   窗口 b = TR_REQ ON → READY ON
#   本用例补的是**窗口 b**（已有测试覆盖窗口 a）。
# --------------------------------------------------------------------------- #
def test_ho_avbl_drop_inspection_window_b_aborts_handshake(env):
    io, clk, eq, runner, controller, events = env

    io.set_input("IN2", False)   # CS_0
    io.set_input("IN1", False)   # VALID
    _tick(runner, clk, 3)
    io.set_input("IN5", False)   # TR_REQ -> 进入窗口 b
    _tick(runner, clk, 2)
    assert controller.state is State.WAIT_BUSY
    assert controller.outputs().ready is True, "窗口 b 的起点是 READY 已 ON"

    # 窗口 b 内可用性丢失 -> 必须中止
    controller.set_port_available("LP1", False)
    _tick(runner, clk, 2)
    assert controller.state is State.HO_ABORT
    assert controller.outputs().ho_avbl is False
    assert controller.outputs().ready is False, "中止时必须撤回 READY"
    assert controller.outputs().es is True
    assert controller.fault is None, "可用性丢失不是锁存故障，应走 HO_ABORT 而非 FAULT"
    runner.stop(now=clk.now())


# --------------------------------------------------------------------------- #
# 判据 4：厂商手册明确"这些主动模式测试同样可以跑在**同时交接**模式下"。
#   等价要求：同时交接时，§6.2.5 的可用性语义与单次交接完全一致。
# --------------------------------------------------------------------------- #
def test_ho_avbl_semantics_identical_in_simultaneous_handoff(env):
    io, clk, eq, runner, controller, events = env

    # CS_0 + CS_1 同时 ON = 同时交接；两个载口都空
    io.set_input("IN2", False)
    io.set_input("IN3", False)
    io.set_input("IN1", False)
    _tick(runner, clk, 3)
    assert controller.state is State.REQ_ON
    assert set(controller.fsm.selected) == {"LP1", "LP2"}

    # 只把**其中一个**载口置为不可用 -> 整个 PI/O 都必须宣告不可交接
    # （§6.1 表1："当被动设备的**其他**载口检测到异常时，本信号也可能被保持为 OFF"，
    #   policy.ho_avbl_scope 默认 all_ports）
    controller.set_port_available("LP2", False)
    _tick(runner, clk, 2)
    assert controller.state is State.HO_ABORT
    assert controller.outputs().ho_avbl is False
    # 与单次交接一致：中止期间保持请求线（图 19 的时序对两种模式同样成立）
    assert controller.outputs().demand_on is True
    runner.stop(now=clk.now())


# --------------------------------------------------------------------------- #
# 判据 5（厂商手册 Passive Mode Functionality Test G — Handoff Available 3）
#   手册给出的步骤序（作为被动端的中止时序）：
#     4. Setting L_REQ signal ON.
#     5. TR_REQ signal verified ON（TP1 内）
#     6. Setting HO_AVBL signal OFF.
#     7. VALID signal verified OFF      （Failure - VALID did not turn OFF ...）
#     8. TR_REQ signal verified OFF
#     9. CS_0/CS_1 verified OFF
#    10. Setting L_REQ signal OFF.
#    11. Setting HO_AVBL signal ON.
#   这与规范**图 19** 画的一致：VALID↓ → L_REQ↓ → HO_AVBL↑。
#   即：中止期间请求线要**保持**，不能提前撤。
# --------------------------------------------------------------------------- #
def test_emulator_test_g_abort_holds_demand_until_handshake_closes(env):
    io, clk, eq, runner, controller, events = env

    # 步骤 1-4：建立握手并断言 L_REQ（检查窗口 b 的起点）
    io.set_input("IN2", False)   # CS_0
    io.set_input("IN1", False)   # VALID
    _tick(runner, clk, 3)
    io.set_input("IN5", False)   # TR_REQ -> 进入窗口 b
    _tick(runner, clk, 2)
    assert controller.outputs().demand_on is True

    # 步骤 6：被动拉低 HO_AVBL
    controller.set_port_available("LP1", False)
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is False

    # 步骤 7 之前：请求线必须**保持**（图 19；手册步骤 10 在 7-9 之后）
    assert controller.outputs().demand_on is True

    # 步骤 7-9：主动撤 VALID / TR_REQ / CS
    io.set_input("IN1", True)
    _tick(runner, clk, 2)
    io.set_input("IN5", True)
    io.set_input("IN2", True)
    _tick(runner, clk, 2)

    # 步骤 10：握手闭合后被动才撤 L_REQ
    assert controller.outputs().demand_on is False

    # 步骤 11：然后才恢复 HO_AVBL
    controller.set_port_available("LP1", True)
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is True
    assert controller.state is State.IDLE
    assert controller.fault is None
    runner.stop(now=clk.now())


# --------------------------------------------------------------------------- #
# 判据 6（厂商手册 Passive Mode Functionality Test F — Handoff Available 2）
#   "Setting HO_AVBL signal OFF ... Verify that the AMHS equipment arrives at the
#    Load Port and errors out." 即：**在主动设备到达之前**就宣告不可交接，
#   对方应当拒绝开始。被动方在 IDLE 期间必须保持 HO_AVBL=OFF，
#   且即便对方硬抬 VALID 也不得断言请求线。
# --------------------------------------------------------------------------- #
def test_emulator_test_f_unavailable_before_arrival_never_starts(env):
    io, clk, eq, runner, controller, events = env
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is True

    controller.set_port_available("LP1", False)
    _tick(runner, clk, 2)
    assert controller.state is State.IDLE
    assert controller.outputs().ho_avbl is False, "到达前就应宣告不可交接"

    # 对方无视 HO_AVBL 硬抬 VALID + CS
    io.set_input("IN2", False)
    io.set_input("IN1", False)
    _tick(runner, clk, 3)
    assert controller.state is State.HO_ABORT
    assert controller.outputs().demand_on is False, "不可交接时不得断言请求线"
    assert controller.outputs().ho_avbl is False
    runner.stop(now=clk.now())


# --------------------------------------------------------------------------- #
# 判据 7（厂商手册 Passive Mode Functionality Test E — Handoff Available 1）
#   "Setting HO_AVBL signal OFF" 发生在 VALID ON 之后、请求线 ON 之前（检查窗口 a）。
#   此时请求线**本来就还没抬起来**，所以中止期间它必须是 OFF——
#   与窗口 b（Test G）形成对照，说明"保持请求线"只对已断言的情况成立。
# --------------------------------------------------------------------------- #
def test_emulator_test_e_window_a_abort_has_no_demand_to_hold(env):
    io, clk, eq, runner, controller, events = env

    io.set_input("IN2", False)   # CS_0
    io.set_input("IN1", False)   # VALID -> 进入窗口 a
    _tick(runner, clk, 2)
    assert controller.state is State.SELECT

    controller.set_port_available("LP1", False)
    _tick(runner, clk, 2)
    assert controller.state is State.HO_ABORT
    assert controller.outputs().ho_avbl is False
    assert controller.outputs().demand_on is False, "窗口 a 里请求线还没断言，谈不上保持"
    assert controller.outputs().es is True

    # 对方撤 VALID/CS -> 恢复
    io.set_input("IN1", True)
    io.set_input("IN2", True)
    controller.set_port_available("LP1", True)
    _tick(runner, clk, 3)
    assert controller.outputs().ho_avbl is True
    runner.stop(now=clk.now())


# --------------------------------------------------------------------------- #
# 判据 8（SEMI E87-0301 §11.1.2 + §11.3.3.2）
#   "The access mode for a load port may be switched at anytime by the host or the
#    operator, except when the Load Port Reservation State Model ... is in the
#    RESERVED state or **during carrier transfer**."
#   "MANUAL — ... only manual (non-AMHS) carrier transfers are allowed. The
#    production equipment shall have the capability of generating an alarm if an
#    automated (AMHS) delivery is attempted."
#
#   → 本库需要 (a) 能判断"是否正在交接"（transfer_in_progress），
#          (b) 拒绝在交接期间切换访问模式的能力（strict=True），
#          (c) 手动模式下 AMHS 硬来时能产生可告警的痕迹（不得断言请求线）。
# --------------------------------------------------------------------------- #
def test_e87_transfer_in_progress_window(env):
    """E87 Table 8：AUTO 交接区间 = READY 有效 → 交接完成（COMPT）。"""

    io, clk, eq, runner, controller, events = env
    _tick(runner, clk, 2)
    assert controller.transfer_in_progress is False

    io.set_input("IN2", False)   # CS_0
    io.set_input("IN1", False)   # VALID
    _tick(runner, clk, 3)
    assert controller.state is State.REQ_ON
    assert controller.transfer_in_progress is False, "READY 还没抬，尚未进入交接区间"
    assert controller.handshake_active is True, "但握手已经打开"

    io.set_input("IN5", False)   # TR_REQ -> READY ON
    _tick(runner, clk, 2)
    assert controller.state is State.WAIT_BUSY
    assert controller.transfer_in_progress is True, "READY 有效即进入交接区间"

    io.set_input("IN6", False)   # BUSY
    _tick(runner, clk, 2)
    assert controller.transfer_in_progress is True

    for key in ("LP1_K0", "LP1_K1", "LP1_K2"):
        io.set_input(key, False)
    _tick(runner, clk, 4)
    assert controller.state is State.AWAIT_COMPT
    assert controller.transfer_in_progress is True, "等 COMPT 期间仍属交接中"

    io.set_input("IN6", True)
    io.set_input("IN5", True)
    io.set_input("IN7", False)   # COMPT
    _tick(runner, clk, 2)
    assert controller.transfer_in_progress is False, "COMPT 之后交接结束"
    assert controller.handshake_active is True, "但握手还要等 VALID 落下才闭合"
    runner.stop(now=clk.now())


def test_e87_access_mode_change_rejected_during_transfer_when_strict(env):
    """E87 §11.1.2：交接期间不得切换访问模式（strict=True 时应拒绝）。"""

    io, clk, eq, runner, controller, events = env
    io.set_input("IN2", False)
    io.set_input("IN1", False)
    _tick(runner, clk, 3)
    io.set_input("IN5", False)
    _tick(runner, clk, 2)
    assert controller.transfer_in_progress is True

    # strict=True -> 拒绝
    with pytest.raises(ValueError, match="E87"):
        controller.set_access_mode("LP1", AccessMode.MANUAL, strict=True)
    # 被拒绝后访问模式没有被改掉
    assert controller.port_snapshots(clk.now())[0].access_mode is AccessMode.AUTOMATIC

    # strict=False（默认）-> 允许：因为"操作员切手动"往往正是要立即中止交接的安全动作
    controller.set_access_mode("LP1", AccessMode.MANUAL)
    _tick(runner, clk, 2)
    assert controller.port_snapshots(clk.now())[0].access_mode is AccessMode.MANUAL
    assert controller.state is State.HO_ABORT
    assert controller.outputs().ho_avbl is False
    runner.stop(now=clk.now())


def test_e87_manual_mode_never_asserts_demand_on_amhs_attempt(env):
    """E87 §11.3.3.2：手动模式下只允许人工交接；AMHS 硬来时不得配合。"""

    io, clk, eq, runner, controller, events = env
    _tick(runner, clk, 2)
    controller.set_access_mode("LP1", AccessMode.MANUAL)
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is False, "手动模式必须宣告不可自动交接"

    # AMHS 无视 HO_AVBL 强行握手
    io.set_input("IN2", False)
    io.set_input("IN1", False)
    _tick(runner, clk, 3)
    assert controller.outputs().demand_on is False, "手动模式下不得断言请求线"
    assert controller.state is State.HO_ABORT
    # 上层可据此告警（E87 要求"capability of generating an alarm"）
    assert any(e.type is EventType.HO_ABORTED for e in events)
    assert controller.fault is None
    runner.stop(now=clk.now())
