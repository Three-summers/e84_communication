"""PiOController / Equipment 门面测试。

覆盖标准条款与设计约束：

* §5.1 / §5.14 / §6.1 表1 ``HO_AVBL`` —— 手动访问模式下应表达"不可交接"；
* §6.2.5.1(a) / §6.2.5.2 —— 运行中把载口置为不可用 -> ``ho_abort``（非故障）；
* §6.3.1.1 / §6.3.3.1 —— 外部上报故障后必须**锁存**，只能显式清除；
* §6.4.6 / R1-1.1.2.1 —— ``stop()`` 后所有输出回到安全态（``ES``/``HO_AVBL`` OFF）；
* 实现约定 —— ``poll()`` 不可重入（事件回调里再调 ``poll()`` 必须安全返回）；
* 现场自定义信号（如板级 ``GO``）通过 ``Inputs.extra`` 参与前置条件。
"""

from __future__ import annotations

import dataclasses

import pytest

from e84 import Equipment, SimIO, VirtualClock, load_file
from e84.config.model import ChannelSpec
from e84.events import EventType
from e84.fault import FaultCode
from e84.model import AccessMode, State
from tests.support import assert_outputs_safe


# --------------------------------------------------------------------------- #
# 驱动辅助
# --------------------------------------------------------------------------- #
def _set_carrier(io, controller, port_id: str, *, present: bool, in_position: bool = False):
    """按示例配置的三键接线，把某个载口的载具状态写进输入通道。"""

    port_cfg = controller.cfg.port(port_id)
    spec = port_cfg.carrier_in_position
    if spec.source not in ("keys", "input"):
        spec = port_cfg.carrier_present
    channels = spec.all_channels
    if in_position:
        logicals = [True] * len(channels)
    elif present:
        logicals = [True] + [False] * (len(channels) - 1)
    else:
        logicals = [False] * len(channels)
    for channel, logical in zip(channels, logicals):
        level = logical if spec.active_high else (not logical)
        io.set_input(channel, level)


def _drive_to_req_on(h, *, cs0: bool = True) -> None:
    """把控制器推进到「请求线已断言」。"""

    h.poll()                       # 启动 + IDLE
    h.set("VALID", True)
    h.set("CS_0", cs0)
    for _ in range(3):              # 去抖窗口 + 选口
        h.poll()
    assert h.controller.state is State.REQ_ON, h.controller.state


# --------------------------------------------------------------------------- #
# 访问模式 / 可用性
# --------------------------------------------------------------------------- #
def test_manual_access_mode_pulls_ho_avbl_low(make_controller):
    h = make_controller()
    c = h.controller
    h.poll()
    assert h.logical_out("HO_AVBL") is True

    c.set_access_mode("LP1", AccessMode.MANUAL)
    h.poll()
    assert c.outputs().ho_avbl is False       # §5.14 / 表1
    assert c.outputs().es is True
    assert c.state is State.IDLE

    # 恢复自动模式后 HO_AVBL 恢复 ON
    c.set_access_mode("LP1", None)
    h.poll()
    assert c.outputs().ho_avbl is True


def test_set_port_available_false_triggers_ho_abort(make_controller):
    h = make_controller()
    c = h.controller
    _drive_to_req_on(h)
    assert c.outputs().l_req is True

    c.set_port_available("LP1", False)
    h.poll()
    assert c.state is State.HO_ABORT          # §6.2.5.1(a)/6.2.5.2
    assert c.outputs().ho_avbl is False
    assert c.outputs().es is True
    # 规范图 19：中止期间请求线**保持**，等握手闭合后才撤、然后才恢复 HO_AVBL
    assert c.outputs().demand_on is True
    assert c.fault is None

    # 主动侧撤下 VALID -> 请求线随之落下，然后 HO_AVBL 才恢复
    # （示例配置的输入去抖为 2ms，poll 步长 20ms，所以第一拍只是把候选值记下）
    h.set("VALID", False)
    h.poll()
    h.poll()
    assert c.outputs().demand_on is False
    assert c.outputs().ho_avbl is False, "撤请求线的同一拍还不得恢复 HO_AVBL"
    c.set_port_available("LP1", True)
    h.poll()
    assert c.state is State.IDLE
    assert c.outputs().ho_avbl is True


def test_es_ok_false_pulls_es_and_ho_low(make_controller):
    h = make_controller()
    c = h.controller
    h.poll()
    c.set_es_ok(False)
    h.poll()
    assert c.outputs().es is False
    assert c.outputs().ho_avbl is False


def test_external_ho_ok_false_pulls_ho_low(make_controller):
    h = make_controller()
    c = h.controller
    h.poll()
    c.set_external_ho_ok(False)
    h.poll()
    assert c.outputs().ho_avbl is False
    c.set_external_ho_ok(None)
    h.poll()
    assert c.outputs().ho_avbl is True


# --------------------------------------------------------------------------- #
# 故障上报与锁存
# --------------------------------------------------------------------------- #
def test_raise_fault_latches_and_requires_explicit_clear(make_controller, collect_events):
    h = make_controller()
    c = h.controller
    faults = collect_events(c.bus, EventType.FAULT_RAISED)
    h.poll()

    c.raise_fault(FaultCode.EXTERNAL, "执行机构报错")
    assert c.state is State.FAULT_WAIT_CLOSE
    assert c.fault is not None and c.fault.code is FaultCode.EXTERNAL
    assert len(faults) == 1                   # 控制器立即上报

    h.poll()                                  # VALID 本就 OFF -> 立即锁存
    assert c.state is State.FAULT_LATCHED
    assert c.outputs().ready is False
    assert c.outputs().ho_avbl is False
    assert c.outputs().es is False            # R1-1.1.2.1

    # §6.3.3.1：标准不定义恢复流程，必须显式清除
    assert c.clear_fault() is True
    assert c.state is State.IDLE
    assert c.fault is None
    h.poll()
    assert c.outputs().ho_avbl is True


def test_clear_fault_force_before_handshake_closed(make_controller):
    h = make_controller()
    c = h.controller
    _drive_to_req_on(h)
    c.raise_fault(FaultCode.EXTERNAL, "boom")
    assert c.state is State.FAULT_WAIT_CLOSE
    assert c.clear_fault() is False            # 握手未闭合，默认拒绝
    assert c.clear_fault(force=True) is True   # 人工强制复位
    assert c.state is State.IDLE


def test_abort_pulls_ho_avbl_and_recovers(make_controller):
    h = make_controller()
    c = h.controller
    _drive_to_req_on(h)

    c.abort("operator")
    h.poll()
    assert c.state is State.HO_ABORT
    assert c.outputs().ho_avbl is False
    # 图 19：中止期间保持请求线
    assert c.outputs().demand_on is True

    # 主动侧撤销 VALID -> 请求线落下；再恢复可用后回 IDLE（注意 2ms 去抖需两拍）
    h.set("VALID", False)
    h.poll()
    h.poll()
    assert c.outputs().demand_on is False, "握手闭合后必须撤下请求线"
    h.set("CS_0", False)
    h.poll()
    h.poll()
    assert c.state is State.IDLE
    assert c.outputs().ho_avbl is True


# --------------------------------------------------------------------------- #
# poll() 不可重入
# --------------------------------------------------------------------------- #
def test_poll_is_not_reentrant_inside_callback(make_controller):
    """在事件回调里再调 ``poll()`` 必须安全返回，而不是死锁或崩溃。"""

    h = make_controller()
    c = h.controller
    observed = []

    def handler(event):
        # 回调在 poll() 内部被同步调用：这里再进一次必须被挡住
        result = c.poll(now=h.clock.now())
        observed.append(result)

    c.bus.subscribe(EventType.OUTPUT_CHANGED, handler)

    h.poll()  # 首次 poll 会让 HO_AVBL/ES 从安全态变到 ON -> 触发 OUTPUT_CHANGED

    assert observed, "回调没有被触发，测试前提不成立"
    assert observed[0] is not None
    assert h.equipment.bus.errors == []       # 回调没有抛异常
    assert c.poll_count >= 1
    assert c.state is State.IDLE


def test_poll_before_any_result_raises_on_reentry():
    """尚无上一次结果时的重入应给出明确错误（防御性契约）。"""

    from e84.controller import PiOController

    cfg = load_file("configs/example_2lp_standard.yaml")
    ctrl = PiOController(
        cfg.interfaces[0], io=SimIO(), clock=VirtualClock(), default_backend="sim"
    )
    assert ctrl._poll_lock.acquire(blocking=False)  # noqa: SLF001 - 测试重入契约
    try:
        with pytest.raises(RuntimeError):
            ctrl.poll(now=0.0)
    finally:
        ctrl._poll_lock.release()  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 停机安全态
# --------------------------------------------------------------------------- #
def test_stop_drives_all_outputs_to_safe_state(make_controller):
    h = make_controller()
    c = h.controller
    _drive_to_req_on(h)
    assert c.outputs().l_req is True          # 确实有输出处于 ON

    c.stop()
    assert c.state is State.DISABLED
    assert_outputs_safe(c)                     # §6.4.6 / R1-1.1.2.1
    # ES / HO_AVBL 必须是逻辑 OFF
    assert h.logical_out("ES") is False
    assert h.logical_out("HO_AVBL") is False
    assert h.logical_out("L_REQ") is False


def test_equipment_stop_is_idempotent(make_controller):
    h = make_controller()
    h.poll()
    c = h.controller
    c.stop()
    c.stop()                                   # 再次停机不应抛错
    assert c.state is State.DISABLED
    assert_outputs_safe(c)


# --------------------------------------------------------------------------- #
# 现场自定义信号（GO）
# --------------------------------------------------------------------------- #
def test_precondition_go_uses_extra_input(make_controller):
    """绑定在 inputs 里的非标准信号进入 ``Inputs.extra``，可作握手前置条件。"""

    h = make_controller()
    c = h.controller

    # 现场接线：GO 未绑定在配置里，用 API 注入
    c.set_extra_input("GO", True)
    h.poll()
    snap = c.snapshot()
    assert snap["extra_inputs"] == {"GO": True}


def test_go_precondition_gates_handshake(example_config_path):
    """``preconditions: [VALID, GO]``：GO 未成立时不得开始握手（END-TO-END）。

    这是「握手触发条件是 VALID ∧ 前置条件 的整体上升沿」这条设计约定的回归保护：
    GO 可能在 VALID 之后才成立，此时仍必须能正常开始这次握手。
    """

    cfg = load_file(example_config_path)
    iface = cfg.interfaces[0]
    go = ChannelSpec(channel="INGO", active_high=False, pull="up", debounce_ms=2)
    inputs = dict(iface.inputs)
    inputs["GO"] = go
    iface2 = dataclasses.replace(
        iface, inputs=inputs, preconditions=("VALID", "GO")
    )
    cfg2 = dataclasses.replace(cfg, interfaces=(iface2,))

    io, clk = SimIO(), VirtualClock()
    eq = Equipment(cfg2, clock=clk, ios={"PIO1": io}, validate=False)
    c = eq.controller("PIO1")

    def setv(name: str, logical: bool) -> None:
        spec = c.cfg.inputs[name]
        io.set_input(spec.channel, logical if spec.active_high else (not logical))

    def poll(dt: float = 0.02):
        clk.advance(dt)
        return c.poll(now=clk.now())

    poll()  # 启动
    setv("VALID", True)
    setv("CS_0", True)
    for _ in range(3):
        poll()
    # GO 尚未成立：前提整体不成立 -> 停在 IDLE，绝不能凭空开始握手
    assert c.state is State.IDLE
    assert c.outputs().demand_on is False

    # GO 成立（在 VALID 之后）：整体上升沿使握手开始
    setv("GO", True)
    for _ in range(3):
        poll()
    assert c.state is State.REQ_ON
    assert c.outputs().l_req is True


# --------------------------------------------------------------------------- #
# 快照与诊断
# --------------------------------------------------------------------------- #
def test_snapshot_contains_ports_and_io(make_controller):
    h = make_controller()
    c = h.controller
    h.poll()
    snap = c.snapshot()
    assert snap["state"] == "idle"
    assert snap["outputs"]["HO_AVBL"] is True
    assert {p["id"] for p in snap["ports"]} == {"LP1", "LP2"}
    assert snap["write_count"] >= 1

    eq_snap = h.equipment.snapshot()
    assert eq_snap["interfaces"][0]["state"] == "idle"
    desc = h.equipment.describe()
    assert desc["interfaces"][0]["id"] == "PIO1"


# --------------------------------------------------------------------------- #
# 真实 GPIO（默认不跑）
# --------------------------------------------------------------------------- #
@pytest.mark.hardware
def test_real_gpio_loopback_requires_hardware():
    """需要真实 GPIO 的回环自检；默认由 ``-m "not hardware"`` 排除。"""

    from e84 import open_backend

    io = open_backend("rpi_gpio")  # pragma: no cover - 需要真实硬件
    try:
        io.setup_output("OUT1", initial=False)
        assert io.read_output("OUT1") in (False, None)
    finally:
        io.close()
