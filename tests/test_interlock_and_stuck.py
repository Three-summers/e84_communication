"""干涉区冻结的**释放**事件，以及「半路接上残留 VALID」的兜底。

覆盖两个在评审里被指出的缺口：

1. ``EventType.INTERLOCK_RELEASED`` 原先只在 ``events.py`` 里定义、**没有任何发射点**。
   上层是"听到 ``INTERLOCK_ENGAGED`` 才冻结机构"的（§6.1 表1 ``BUSY`` 说明），
   释放时不通知就会把机构**永久冻住**。本文件把它钉死：
   ``BUSY`` 上升沿发 ``INTERLOCK_ENGAGED``，``BUSY`` 下降沿必须发 ``INTERLOCK_RELEASED``；
   控制器停机时如果还冻着，也要补发一次。

2. ``VALID_STUCK``：IDLE 且"前提整体（``VALID`` ∧ 前置条件）已成立却没有上升沿"
   时累计超时。触发路径是"运维在 ``VALID`` 仍为 ON 时强制清除故障"——
   这时无法判断它是新握手还是旧握手的尾巴，只能拒绝"半路接上"。
"""

from __future__ import annotations

import pytest

from e84 import (
    AccessMode,
    EventType,
    FaultCode,
    ManualRunner,
    SimIO,
    State,
    VirtualClock,
    load_file,
)
from e84.config.model import EquipmentConfig


@pytest.fixture
def rig_at_idle(cfg: EquipmentConfig):
    """起一个 SimIO 设备，且两个载口都是空的（可直接装载）。"""

    from e84 import Equipment

    io, clk = SimIO(), VirtualClock()
    eq = Equipment(cfg, clock=clk, ios={"PIO1": io}, validate=False)
    runner = ManualRunner(eq)
    runner.start(now=clk.now())
    controller = eq.controller("PIO1")
    for key in ("LP1_K0", "LP1_K1", "LP1_K2", "LP2_K0", "LP2_K1", "LP2_K2"):
        io.set_input(key, True)          # 高电平 = 无载具（示例配置 active_high=false）
    events = []
    eq.bus.subscribe(None, events.append)
    return io, clk, eq, runner, controller, events


def _tick(runner, clk, count=1, dt=0.02):
    for _ in range(count):
        clk.advance(dt)
        runner.poll(now=clk.now())


def _drive_to_transfer(io, runner, clk):
    """把一次单次装载推进到 BUSY=ON（干涉区冻结生效）。"""

    io.set_input("IN2", False)   # CS_0 = ON
    io.set_input("IN1", False)   # VALID = ON
    _tick(runner, clk, 3)
    io.set_input("IN5", False)   # TR_REQ = ON
    _tick(runner, clk, 2)
    io.set_input("IN6", False)   # BUSY = ON
    _tick(runner, clk, 2)


# --------------------------------------------------------------------------- #
# 1) 干涉区：冻结与释放必须成对
# --------------------------------------------------------------------------- #
def test_interlock_engaged_then_released_on_normal_handoff(rig_at_idle):
    """一次完整交接：BUSY↑ 发 ENGAGED，BUSY↓ 必须发 RELEASED。"""

    io, clk, eq, runner, controller, events = rig_at_idle
    _drive_to_transfer(io, runner, clk)
    assert controller.interlock_engaged is True
    assert [e.type for e in events].count(EventType.INTERLOCK_ENGAGED) == 1
    assert [e.type for e in events].count(EventType.INTERLOCK_RELEASED) == 0

    # 载具完整落位 -> 请求线落 -> BUSY 落 -> COMPT -> VALID 落
    for key in ("LP1_K0", "LP1_K1", "LP1_K2"):
        io.set_input(key, False)
    _tick(runner, clk, 4)
    io.set_input("IN6", True)    # BUSY = OFF
    io.set_input("IN5", True)    # TR_REQ = OFF
    io.set_input("IN7", False)   # COMPT = ON
    _tick(runner, clk, 2)
    io.set_input("IN1", True)
    io.set_input("IN7", True)
    io.set_input("IN2", True)
    _tick(runner, clk, 3)

    assert controller.state is State.IDLE
    assert controller.interlock_engaged is False
    released = [e for e in events if e.type is EventType.INTERLOCK_RELEASED]
    assert len(released) == 1, "BUSY 下降沿必须发且只发一次 INTERLOCK_RELEASED"
    assert released[0].clause == "6.1.1 表1 BUSY"
    runner.stop(now=clk.now())


def test_interlock_follows_busy_level_not_handshake_closure(rig_at_idle):
    """冻结必须**跟随 BUSY 电平**，而不是跟随状态复位或握手闭合。

    §6.1 表1 BUSY 的措辞是"只要本信号为 ON，被动设备就不应在交接干涉区内执行任何
    机械动作"。所以：

    * 中止握手时，若对方的 BUSY 仍为 ON，**必须继续冻结**（对方机构可能还在干涉区里）；
    * BUSY 一落下，就必须立刻解除冻结并发事件——不能拖到握手闭合或超时。
    """

    io, clk, eq, runner, controller, events = rig_at_idle
    _drive_to_transfer(io, runner, clk)
    assert controller.interlock_engaged is True

    # 中止握手，但对方 BUSY 还没落下 -> 仍然冻结
    controller.abort("test", now=clk.now())
    _tick(runner, clk, 2)
    assert controller.state is State.HO_ABORT
    assert controller.interlock_engaged is True, (
        "BUSY 仍为 ON 时不得解除冻结：对方机构可能还在交接干涉区里"
    )
    assert not any(e.type is EventType.INTERLOCK_RELEASED for e in events)

    # 对方撤下 BUSY -> 立刻解除冻结
    io.set_input("IN6", True)
    _tick(runner, clk, 2)
    assert controller.interlock_engaged is False
    assert any(e.type is EventType.INTERLOCK_RELEASED for e in events)
    runner.stop(now=clk.now())


def test_interlock_released_on_stop_while_engaged(rig_at_idle):
    """BUSY=ON 期间直接停机，也必须补发释放事件，否则机构会被永久冻住。"""

    io, clk, eq, runner, controller, events = rig_at_idle
    _drive_to_transfer(io, runner, clk)
    assert controller.interlock_engaged is True

    runner.stop(now=clk.now())

    released = [e for e in events if e.type is EventType.INTERLOCK_RELEASED]
    assert len(released) == 1, "停机时若仍处于冻结状态，必须补发一次释放事件"
    assert controller.interlock_engaged is False


# --------------------------------------------------------------------------- #
# 2) VALID_STUCK：拒绝"半路接上"残留的 VALID
# --------------------------------------------------------------------------- #
def test_valid_stuck_when_force_clearing_fault_with_valid_still_on(rig_at_idle):
    """运维在 VALID 仍为 ON 时强制清故障：不得"半路接上"，应超时报 VALID_STUCK。"""

    io, clk, eq, runner, controller, events = rig_at_idle
    io.set_input("IN2", False)   # CS_0 = ON
    io.set_input("IN1", False)   # VALID = ON
    _tick(runner, clk, 3)
    assert controller.state is State.REQ_ON       # 握手已经起来

    # 制造一次故障（对方不抬 TR_REQ，TP1 超时）
    clk.advance(2.5)
    runner.poll(now=clk.now())
    assert controller.fault is not None
    assert controller.fault.code is FaultCode.TP1_TIMEOUT

    # VALID 仍为 ON，运维强制清除故障
    assert controller.clear_fault(force=True, now=clk.now()) is True
    _tick(runner, clk, 2)
    assert controller.state is State.IDLE
    assert controller.fault is None

    # 前提整体已成立却没有上升沿 -> 超过 valid_stuck_timeout_s 后报 VALID_STUCK
    clk.advance(controller.cfg.policy.valid_stuck_timeout_s + 0.1)
    runner.poll(now=clk.now())
    assert controller.fault is not None
    assert controller.fault.code is FaultCode.VALID_STUCK
    runner.stop(now=clk.now())


def test_valid_stuck_does_not_fire_while_waiting_for_site_interlock(tmp_path):
    """现场联锁（GO）迟迟不成立时，**不能**被误判成 VALID_STUCK。

    这是"握手触发改为前提整体上升沿"这条修复的守护测试：VALID 可以先来、
    GO 后到，中间等多久都不该报错。
    """

    import yaml

    from e84 import Equipment, load_file

    config_path = tmp_path / "go.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "backend": "sim",
                "poll_interval_ms": 5,
                "boot_safe_hold_ms": 0,
                "interfaces": [
                    {
                        "id": "PIO1",
                        "topology": "one_load_port",
                        "preconditions": ["VALID", "GO"],
                        # 与现场一致：低有效 + 上拉（ON = 引脚低）
                        "io_defaults": {"active_high": False, "pull": "up"},
                        "load_ports": [{"id": "LP1", "role": "single"}],
                        "outputs": {
                            "L_REQ": {"channel": "OUT1"},
                            "READY": {"channel": "OUT4"},
                            "HO_AVBL": {"channel": "OUT7"},
                            "ES": {"channel": "OUT8"},
                        },
                        "inputs": {
                            "VALID": {"channel": "IN1"},
                            "CS_0": {"channel": "IN2"},
                            "TR_REQ": {"channel": "IN5"},
                            "BUSY": {"channel": "IN6"},
                            "COMPT": {"channel": "IN7"},
                            "GO": {"channel": "IN22"},
                        },
                        "features": {"simultaneous": False, "continuous": False},
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    cfg = load_file(config_path)

    io, clk = SimIO(), VirtualClock()
    eq = Equipment(cfg, clock=clk, ios={"PIO1": io}, validate=False)
    runner = ManualRunner(eq)
    runner.start(now=clk.now())
    controller = eq.controller("PIO1")

    io.set_input("IN2", False)   # CS_0 = ON
    io.set_input("IN1", False)   # VALID = ON，但 GO 还没来

    # 远超 valid_stuck_timeout_s 的等待
    for _ in range(60):
        clk.advance(0.2)
        runner.poll(now=clk.now())
    assert controller.state is State.IDLE
    assert controller.fault is None, "等待现场联锁期间不得报 VALID_STUCK"

    # GO 后置成立 -> 必须立刻开始握手
    io.set_input("IN22", False)
    _tick(runner, clk, 3)
    assert controller.state in (State.REQ_ON, State.WAIT_BUSY, State.SELECT)
    assert controller.outputs().demand_on is True
    runner.stop(now=clk.now())


def test_manual_mode_string_is_coerced(rig_at_idle):
    """``set_access_mode`` 传字符串也必须真正生效（否则是静默失败）。"""

    io, clk, eq, runner, controller, events = rig_at_idle
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is True

    controller.set_access_mode("LP1", "manual")     # 字符串，非枚举
    _tick(runner, clk, 2)
    assert controller.port_snapshots(clk.now())[0].access_mode is AccessMode.MANUAL
    assert controller.outputs().ho_avbl is False, "手动模式必须拉低 HO_AVBL"

    controller.set_access_mode("LP1", AccessMode.AUTOMATIC)
    _tick(runner, clk, 2)
    assert controller.outputs().ho_avbl is True

    with pytest.raises(ValueError):
        controller.set_access_mode("LP1", "not-a-mode")
    runner.stop(now=clk.now())
