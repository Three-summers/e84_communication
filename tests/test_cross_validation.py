"""交叉验证（cross-validation）——打破"同源自测"的循环论证。

前面那些测试有一个共同的弱点：**驱动方也是我自己写的**。
``test_golden_figures.py`` 用 ``e84/active/fsm.py`` 驱动 ``e84/fsm.py``，
两侧出自同一份理解；如果我在某张图上错了两处，它们会**一起错**，测试照样全绿。
这只证明了"两侧互相兼容"，不能证明"任一侧符合规范"。

本文件用四种**互相独立**的参照来交叉验证：

============================  ====================================================
X1 层级差分交叉              同一逻辑输入序列，分别喂给「纯 FSM 直驱」与
                             「完整栈（HAL 极性/映射/传感量/控制器）」，
                             逐拍断言状态、逻辑输出、**物理电平**三者一致。
                             用来抓"逻辑算对了但引脚写错了"这一类层级 bug。
X2 表示交叉（矩阵）          同一逻辑场景在 {低有效, 高有效} × {请求线并线, 独立两线}
                             四种**表示方式**下，逻辑轨迹必须逐拍完全相同。
                             用来抓"把表示差异漏进逻辑"这一类 bug。
X3 图驱动交叉                把 §6.2.2.1 / §6.2.3.2 / §6.2.4.3 的编号步骤
                             逐条手写成**显式电平波形**（完全不经过 ActiveFsm），
                             驱动完整栈并断言被动侧响应符合规范。
X4 反向交叉                  用**手写的被动侧响应**（同样不经过 PassiveFsm）
                             驱动 ActiveFsm，断言主动侧输出符合规范。
                             X3 与 X4 合起来，两侧都被独立波形约束过。
============================  ====================================================
"""

from __future__ import annotations

import random
from typing import Dict, List, Tuple

import pytest

from e84 import (
    AccessMode,
    Equipment,
    EventType,
    Inputs,
    ManualRunner,
    Op,
    PassiveFsm,
    PortRole,
    SimIO,
    State,
    VirtualClock,
    load_dict,
)
from e84.active import ActiveConfig, ActiveFsm, ActiveInputs, TransferJob
from e84.config.model import InterfaceConfig
from e84.model import Outputs, PortSnapshot
from e84.signals import Signal
from tests.support import level_for_logical, logical_from_level

# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #

#: X1/X2 专用配置：**去抖为 0**，这样两条执行路径看到的输入逐拍严格相同；
#: 载口传感量走 ``external``，由测试直接注入，避免传感量合成差异干扰差分结论。
_DEBOUNCE_FREE_CONFIG = {
    "schema_version": 1,
    "name": "cross-validation",
    "backend": "sim",
    "poll_interval_ms": 5,
    "boot_safe_hold_ms": 0,
    "interfaces": [
        {
            "id": "PIO1",
            "scenario": "standard",
            "topology": "two_load_ports",
            "features": {"simultaneous": True, "continuous": True},
            "preconditions": ["VALID"],
            "load_ports": [
                {
                    "id": "LP1",
                    "role": "left",
                    "carrier_present": {"source": "external"},
                    "carrier_in_position": {"source": "external"},
                    "ready_for_transfer": {"source": "external", "value": True},
                    "door_open": {"source": "constant", "value": True},
                    "clamp_released": {"source": "constant", "value": True},
                    "operation_intent": {"source": "derived"},
                },
                {
                    "id": "LP2",
                    "role": "right",
                    "carrier_present": {"source": "external"},
                    "carrier_in_position": {"source": "external"},
                    "ready_for_transfer": {"source": "external", "value": True},
                    "operation_intent": {"source": "derived"},
                },
            ],
            "inputs": {
                "VALID": {"channel": "IN1"},
                "CS_0": {"channel": "IN2"},
                "CS_1": {"channel": "IN3"},
                "TR_REQ": {"channel": "IN5"},
                "BUSY": {"channel": "IN6"},
                "COMPT": {"channel": "IN7"},
                "CONT": {"channel": "IN8"},
            },
            "outputs": {
                "L_REQ": {"channel": "OUT1"},
                "U_REQ": {"channel": "OUT1"},
                "READY": {"channel": "OUT4"},
                "HO_AVBL": {"channel": "OUT7"},
                "ES": {"channel": "OUT8"},
            },
            "timers": {"TP1": 2, "TP2": 2, "TP3": 60, "TP4": 60, "TP5": 2, "TP6": 2},
        }
    ],
}

#: 逻辑输入信号（与物理表示无关）
_LOGIC_INPUTS: Tuple[str, ...] = (
    "VALID", "CS_0", "CS_1", "TR_REQ", "BUSY", "COMPT", "CONT",
)


def _build_env(config_dict, *, active_high: bool, separate_req: bool):
    """按给定的**物理表示**造一个环境：完整栈 + 并行的纯 FSM。"""

    import copy

    data = copy.deepcopy(config_dict)
    iface = data["interfaces"][0]
    iface["io_defaults"] = {
        "active_high": active_high,
        "pull": "up" if not active_high else "none",
        "debounce_ms": 0,
    }
    if separate_req:
        iface["outputs"]["U_REQ"] = {"channel": "OUT2"}

    cfg = load_dict(data, validate=False)
    io, clk = SimIO("cross"), VirtualClock()
    equipment = Equipment(cfg, clock=clk, ios={"PIO1": io}, validate=False)
    runner = ManualRunner(equipment)
    runner.start(now=clk.now())
    controller = equipment.controller("PIO1")

    # 与控制器**完全独立**的第二条执行路径：直接持有同一份 InterfaceConfig 的纯 FSM
    pure = PassiveFsm(cfg.interfaces[0])
    pure.enable(clk.now())

    return cfg, io, clk, equipment, runner, controller, pure


def _set_logical(io, cfg, name: str, logical: bool) -> None:
    """按逻辑值驱动一个 E84 输入（自己换算电平，不经过被测代码）。"""

    spec = cfg.interfaces[0].inputs[name]
    io.set_input(spec.channel, level_for_logical(spec.active_high, logical))


def _read_logical(io, cfg, name: str) -> bool:
    spec = cfg.interfaces[0].outputs[name]
    return logical_from_level(spec.active_high, io.read(spec.channel))


def _expected_levels(cfg: InterfaceConfig, outputs: Outputs) -> Dict[str, bool]:
    """由逻辑输出推导每个物理通道**应当**出现的电平（共用通道取逻辑或）。"""

    desired = outputs.as_dict()
    per_channel: Dict[str, List[bool]] = {}
    for name, spec in cfg.outputs.items():
        per_channel.setdefault(spec.channel, []).append(bool(desired.get(name, False)))
    return {
        channel: level_for_logical(cfg.outputs[next(iter(cfg.outputs))].active_high, any(values))
        for channel, values in per_channel.items()
    } if False else {
        channel: (
            any(values) if _active_high_of(cfg, channel) else (not any(values))
        )
        for channel, values in per_channel.items()
    }


def _active_high_of(cfg: InterfaceConfig, channel: str) -> bool:
    for spec in cfg.outputs.values():
        if spec.channel == channel:
            return spec.active_high
    raise KeyError(channel)


def _ports_for(controller, now: float) -> Tuple[PortSnapshot, ...]:
    return tuple(controller.port_snapshots(now))


def _inputs_from(logical: Dict[str, bool], ports, now: float) -> Inputs:
    return Inputs(
        now=now,
        valid=logical["VALID"],
        cs0=logical["CS_0"],
        cs1=logical["CS_1"],
        tr_req=logical["TR_REQ"],
        busy=logical["BUSY"],
        compt=logical["COMPT"],
        cont=logical["CONT"],
        es_ok=True,
        external_ho_ok=True,
        ports=tuple(ports),
        extra={},
    )


# =========================================================================== #
# X1 层级差分交叉
# =========================================================================== #
def test_x1_layer_differential_random_waveform():
    """随机波形下，「纯 FSM 直驱」与「完整栈」必须逐拍一致（状态/逻辑/物理电平）。

    这条专门用来抓层级 bug：逻辑算对了、但 HAL 把极性/映射/共用通道写错了。
    已经被它抓到过的真实缺陷类型：``NullIO`` 静态电平、``GO`` 未进 ``Inputs``。
    """

    cfg, io, clk, equipment, runner, controller, pure = _build_env(
        _DEBOUNCE_FREE_CONFIG, active_high=False, separate_req=False
    )
    rng = random.Random(20260301)
    logical = {name: False for name in _LOGIC_INPUTS}

    # 载口初始：都空、机构可用
    for port_id in ("LP1", "LP2"):
        controller.set_external_sensor(port_id, "carrier_present", False)
        controller.set_external_sensor(port_id, "carrier_in_position", False)
        controller.set_external_sensor(port_id, "ready_for_transfer", True)

    divergences: List[str] = []
    for tick in range(3000):
        # 随机翻转若干逻辑输入
        for _ in range(rng.randint(0, 2)):
            logical[rng.choice(_LOGIC_INPUTS)] = rng.random() < 0.5
        for name, value in logical.items():
            _set_logical(io, cfg, name, value)
        # 随机改变载口传感量（走 external 注入，两条路径共用同一份载口快照）
        if rng.random() < 0.15:
            pid = rng.choice(["LP1", "LP2"])
            present = rng.random() < 0.5
            controller.set_external_sensor(pid, "carrier_present", present)
            controller.set_external_sensor(
                pid, "carrier_in_position", present and rng.random() < 0.7
            )

        dt = rng.choice([0.001, 0.005, 0.02, 0.2, 2.5])
        clk.advance(dt)
        now = clk.now()

        # --- 路径 A：完整栈 ---
        res_ctrl = controller.poll(now=now)
        # --- 路径 B：纯 FSM（同一份载口快照 = 同一组输入）---
        res_pure = pure.step(_inputs_from(logical, _ports_for(controller, now), now))

        if res_ctrl.state is not res_pure.state:
            divergences.append(f"t{tick}: 状态 {res_ctrl.state} vs {res_pure.state}")
        if res_ctrl.outputs != res_pure.outputs:
            divergences.append(f"t{tick}: 输出 {res_ctrl.outputs} vs {res_pure.outputs}")
        # 物理电平必须与逻辑输出按极性推出的期望完全一致
        expected = _expected_levels(cfg.interfaces[0], res_pure.outputs)
        for channel, level in expected.items():
            actual = io.read(channel)
            if actual != level:
                divergences.append(
                    f"t{tick}: 通道 {channel} 电平 {actual} != 期望 {level}"
                )
        if divergences:
            break

    runner.stop(now=clk.now())
    assert not divergences, "层级差分出现分歧：\n" + "\n".join(divergences[:5])


# =========================================================================== #
# X2 表示交叉（矩阵）
# =========================================================================== #
def _run_waveform_and_record(active_high: bool, separate_req: bool, seed: int = 4242):
    """跑同一段**逻辑**波形，记录逐拍的 (状态, 逻辑输出)。"""

    cfg, io, clk, equipment, runner, controller, _pure = _build_env(
        _DEBOUNCE_FREE_CONFIG, active_high=active_high, separate_req=separate_req
    )
    rng = random.Random(seed)
    logical = {name: False for name in _LOGIC_INPUTS}
    for port_id in ("LP1", "LP2"):
        controller.set_external_sensor(port_id, "carrier_present", False)
        controller.set_external_sensor(port_id, "carrier_in_position", False)
        controller.set_external_sensor(port_id, "ready_for_transfer", True)

    trace: List[Tuple[str, str]] = []
    for _ in range(1200):
        for _ in range(rng.randint(0, 2)):
            logical[rng.choice(_LOGIC_INPUTS)] = rng.random() < 0.5
        for name, value in logical.items():
            _set_logical(io, cfg, name, value)
        if rng.random() < 0.15:
            pid = rng.choice(["LP1", "LP2"])
            present = rng.random() < 0.5
            controller.set_external_sensor(pid, "carrier_present", present)
            controller.set_external_sensor(
                pid, "carrier_in_position", present and rng.random() < 0.7
            )
        clk.advance(rng.choice([0.001, 0.005, 0.02, 0.2, 2.5]))
        res = controller.poll(now=clk.now())
        o = res.outputs
        demand = "L" if o.l_req else ("U" if o.u_req else "-")
        trace.append(
            (
                res.state.value,
                f"{demand}|R{int(o.ready)}|H{int(o.ho_avbl)}|E{int(o.es)}",
            )
        )
    runner.stop(now=clk.now())
    return trace


@pytest.mark.parametrize(
    "active_high,separate_req",
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["低有效+并线", "高有效+并线", "低有效+独立两线", "高有效+独立两线"],
)
def test_x2_logical_trace_is_representation_independent(active_high, separate_req):
    """逻辑轨迹必须与物理表示无关：四种表示方式下逐拍完全一致。"""

    baseline = _run_waveform_and_record(False, False)
    other = _run_waveform_and_record(active_high, separate_req)

    assert len(baseline) == len(other)
    for i, (a, b) in enumerate(zip(baseline, other)):
        assert a == b, f"第 {i} 拍逻辑轨迹不一致：基线={a} 本表示={b}"
    # 顺带确认这段波形确实跑出了内容（不是全程 idle 的假通过）
    assert any(state != "idle" for state, _ in baseline), "波形没有产生任何握手，测试无意义"


# =========================================================================== #
# X3 图驱动交叉：手写波形驱动完整栈（完全不经过 ActiveFsm）
# =========================================================================== #
def _keys(io, port_id: str, *, present: bool, in_position: bool) -> None:
    """按现场三键接法驱动落位键：**低有效 + 上拉**，非完整落位时只压一个键。

    （极性在这里写死，正是为了不让"被测代码告诉我该怎么驱动"。）
    """

    for i in range(3):
        on = in_position or (present and i == 0)
        io.set_input(f"{port_id}_K{i}", not on)


def _poll_until(ctrl, clk, pred, *, limit=200, dt=0.005):
    for _ in range(limit):
        clk.advance(dt)
        ctrl.poll(now=clk.now())
        if pred():
            return True
    return False


def test_x3_fig10_single_load_by_handwritten_waveform(make_controller):
    """图 10 / §6.2.2.1 单次装载：按编号步骤手写波形驱动完整栈。

    驱动方不是 ``ActiveFsm``，而是把规范正文的 1)–13) 逐条翻译成电平变化，
    因此与 ``e84/fsm.py`` 的实现**没有共享代码**。
    """

    env = make_controller()
    ctrl, io, clk = env.controller, env.io, env.clock
    _keys(io, "LP1", present=False, in_position=False)
    _keys(io, "LP2", present=False, in_position=False)
    env.poll(0.005)

    # ---- 第 1 步：主动先选口（CS_0 = ON）。注 3：被动此时**不得**看 CS ----
    env.set("CS_0", True)
    for _ in range(5):
        env.poll(0.005)
    assert ctrl.state is State.IDLE, "注 3：VALID 之前不得因 CS_0 而进入选口"

    # ---- 第 2 步：主动抬 VALID ----
    env.set("VALID", True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.REQ_ON), "未进入 REQ_ON"

    # ---- 第 3 步：空载口 ⇒ 被动断言 L_REQ（且不得同时置 U_REQ）----
    out = ctrl.outputs()
    assert out.l_req and not out.u_req, "装载方向必须只置 L_REQ"
    assert out.ready is False
    assert out.ho_avbl is True and out.es is True

    # ---- 第 4 步：主动抬 TR_REQ ----
    env.set("TR_REQ", True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.WAIT_BUSY)
    # ---- 第 5 步：被动断言 READY ----
    assert ctrl.outputs().ready is True

    # ---- 第 6 步：主动抬 BUSY ⇒ 进入交接，干涉区冻结 ----
    env.set("BUSY", True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.TRANSFER)
    assert ctrl.interlock_engaged is True

    # ---- 第 7 步：载具「位于正确位置」⇒ 被动撤 L_REQ ----
    _keys(io, "LP1", present=True, in_position=True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.AWAIT_COMPT)
    assert ctrl.outputs().l_req is False, "§6.2.2.1 第7步：载具到位后撤请求线"
    assert ctrl.outputs().ready is True, "READY 要留到 COMPT 之后（第11步）才撤"

    # ---- 第 8/9 步：主动必须先看到请求线 OFF，才撤 BUSY 与 TR_REQ ----
    env.set("BUSY", False)
    env.set("TR_REQ", False)
    env.poll(0.005)

    # ---- 第 10 步：主动抬 COMPT ----
    env.set("COMPT", True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.CLOSING)
    # ---- 第 11 步：被动撤 READY ----
    assert ctrl.outputs().ready is False
    assert ctrl.interlock_engaged is False, "退出干涉区后必须解除冻结"

    # ---- 第 12/13 步：主动撤 COMPT/VALID/CS_0；握手由 VALID↓ 闭合 ----
    env.set("COMPT", False)
    env.set("VALID", False)
    env.set("CS_0", False)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.IDLE)
    assert ctrl.fault is None
    env.runner.stop(now=clk.now())


def test_x3_fig14_simultaneous_load_by_handwritten_waveform(make_controller):
    """图 14 / §6.2.3.2 同时装载：请求线是**两个载口都就绪**的与语义。"""

    env = make_controller()
    ctrl, io, clk = env.controller, env.io, env.clock
    _keys(io, "LP1", present=False, in_position=False)
    _keys(io, "LP2", present=False, in_position=False)
    env.poll(0.005)

    # 第 1 步：CS_0 与 CS_1 **同时** ON ⇒ 这是一次同时交接
    env.set("CS_0", True)
    env.set("CS_1", True)
    # 第 2 步：再抬 VALID
    env.set("VALID", True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.REQ_ON)
    assert ctrl.outputs().l_req is True
    assert set(ctrl.fsm.selected) == {"LP1", "LP2"}

    env.set("TR_REQ", True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.WAIT_BUSY)
    env.set("BUSY", True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.TRANSFER)

    # 第 4 步的与语义：只到位**一个**载口时，请求线必须仍然保持 ON
    _keys(io, "LP1", present=True, in_position=True)
    for _ in range(10):
        env.poll(0.02)
    assert ctrl.state is State.TRANSFER, "只到位一个载口时不得离开 TRANSFER"
    assert ctrl.outputs().l_req is True, "与语义：两个载口都到位才允许撤请求线"

    # 两个都到位 ⇒ 请求线落下
    _keys(io, "LP2", present=True, in_position=True)
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.AWAIT_COMPT)
    assert ctrl.outputs().l_req is False
    assert ctrl.fault is None
    env.runner.stop(now=clk.now())


def test_x3_fig16_continuous_unload_then_load_by_handwritten_waveform(make_controller):
    """图 16 / §6.2.4.3 连续交接（同一载口 卸→装）。

    独立手写波形用来钉死三件事：
    1. ``CONT`` 在**首段** ``BUSY``↑ 为 ON、在**末段** ``BUSY``↑ 为 OFF；
    2. 两段之间 ``VALID`` **落下再抬起**（不是整批保持 ON）；
    3. 第二段方向自动变成 LOAD（载口已经空了）。
    """

    env = make_controller()
    ctrl, io, clk = env.controller, env.io, env.clock
    _keys(io, "LP1", present=True, in_position=True)      # 初始：LP1 有料
    _keys(io, "LP2", present=False, in_position=False)
    env.poll(0.005)

    ts = []
    env.equipment.bus.subscribe(None, ts.append)

    def one_segment(*, cont: bool, op: str) -> None:
        """按图 10 的 13 步跑完一段；``cont`` 决定 BUSY↑ 时的 CONT 电平。"""

        env.set("CS_0", True)
        env.set("VALID", True)
        assert _poll_until(ctrl, clk, lambda: ctrl.state is State.REQ_ON)
        out = ctrl.outputs()
        assert (out.u_req if op == "unload" else out.l_req) is True, f"方向判定错误: {op}"

        env.set("TR_REQ", True)
        assert _poll_until(ctrl, clk, lambda: ctrl.state is State.WAIT_BUSY)

        env.set("CONT", cont)                      # CONT 必须在 BUSY↑ 之前建立
        env.set("BUSY", True)
        assert _poll_until(ctrl, clk, lambda: ctrl.state is State.TRANSFER)

        if op == "unload":
            _keys(io, "LP1", present=False, in_position=False)
        else:
            _keys(io, "LP1", present=True, in_position=True)
        assert _poll_until(ctrl, clk, lambda: ctrl.state is State.AWAIT_COMPT)

        # ---- 第 8/9 步：先撤 BUSY 与 TR_REQ ----
        env.set("BUSY", False)
        env.set("TR_REQ", False)
        # ---- 第 10 步：再抬 COMPT ----
        env.set("COMPT", True)
        assert _poll_until(ctrl, clk, lambda: ctrl.state is State.CLOSING)
        assert ctrl.outputs().ready is False, "第11步：COMPT 之后撤 READY"
        # ---- 第 12/13 步：释放 COMPT/VALID/CS ----
        env.set("COMPT", False)
        env.set("VALID", False)
        env.set("CS_0", False)

    # ---- 第一段：卸载，CONT=ON ⇒ 后面还有 ----
    one_segment(cont=True, op="unload")
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.CONT_NEXT), (
        "CONT=ON 时本段结束后应进入 cont_next 等待下一段"
    )
    assert ctrl.fsm.batch_active is True

    # ---- 第二段：装载，CONT=OFF ⇒ 这是末段 ----
    one_segment(cont=False, op="load")
    assert _poll_until(ctrl, clk, lambda: ctrl.state is State.IDLE)
    assert ctrl.fsm.batch_active is False, "末段结束后批次必须收尾"

    types = [e.type for e in ts]
    assert types.count(EventType.BATCH_STARTED) == 1
    assert types.count(EventType.BATCH_ENDED) == 1
    assert types.index(EventType.BATCH_STARTED) < types.index(EventType.BATCH_ENDED)
    # 两段之间 VALID 确实落下过（说明是两次握手，而不是一次长握手）
    assert types.count(EventType.HANDSHAKE_STARTED) >= 2
    assert ctrl.fault is None
    env.runner.stop(now=clk.now())


# =========================================================================== #
# X4 反向交叉：手写被动响应驱动 ActiveFsm（不经过 PassiveFsm）
# =========================================================================== #
def test_x4_active_side_against_handwritten_passive_response():
    """用**手写的被动侧响应**驱动 ActiveFsm，断言主动侧输出符合规范。

    X3 用独立波形约束被动端；这里用另一种独立参照约束主动端。
    两侧都不再依赖对方的实现，循环论证被打破。
    """

    fsm = ActiveFsm(ActiveConfig(jobs=[TransferJob.load(PortRole.LEFT)], td0_s=0.1,
                                 transfer_duration_s=0.05, ta1_s=2.0, ta2_s=2.0, ta3_s=2.0))
    now = 0.0
    # 手写的被动侧状态（模拟一个合规的被动设备）
    passive_demand = False
    passive_ready = False
    carrier_in_position = False
    seen: List[Tuple[str, bool, bool, bool, bool, bool, bool]] = []

    def tick(dt: float) -> None:
        nonlocal now, passive_demand, passive_ready, carrier_in_position
        now += dt
        inp = ActiveInputs(
            now=now, demand_on=passive_demand, ready=passive_ready,
            ho_avbl=True, es=True,
        )
        res = fsm.step(inp)
        o = res.outputs
        seen.append((res.phase.value, o.valid, o.cs0, o.cs1, o.tr_req, o.busy, o.compt))
        # ---- 手写被动侧：只按规范正文的编号步骤响应 ----
        # 第7步：搬运动作完成 ⇒ 载具"位于正确位置"
        if res.transfer_due:
            carrier_in_position = True
        # 第3步：VALID ON 且 CS 指出载口、载口已空 ⇒ 断言请求线
        # 第7步：载具到位 ⇒ 撤下请求线
        passive_demand = o.valid and o.cs0 and not carrier_in_position
        # 第5步：收到 TR_REQ 且请求线已在、机构就绪 ⇒ 断言 READY
        passive_ready = o.tr_req and passive_demand

    # 跑到主动侧完成
    for _ in range(4000):
        tick(0.002)
        if fsm.completed:
            break
    assert fsm.phase.value == "done", f"主动侧未正常完成: {fsm.snapshot()}"

    # ---- 逐条核对规范对主动侧的顺序约束 ----
    first_cs = next(i for i, r in enumerate(seen) if r[2] or r[3])
    first_valid = next(i for i, r in enumerate(seen) if r[1])
    first_tr = next(i for i, r in enumerate(seen) if r[4])
    first_busy = next(i for i, r in enumerate(seen) if r[5])
    first_compt = next(i for i, r in enumerate(seen) if r[6])
    assert first_cs < first_valid, "§6.3.2.3：CS 必须先于 VALID"
    assert first_valid < first_tr, "§6.2.2.1 第2/4步：VALID 先于 TR_REQ"
    assert first_tr < first_busy, "§6.2.2.1 第6步：TR_REQ 先于 BUSY"
    assert first_busy < first_compt, "§6.2.2.1 第8/10步：BUSY 先落、再抬 COMPT"
    # COMPT 抬起的同一拍，BUSY 与 TR_REQ 必须已经落下（第8/9步）
    compt_row = seen[first_compt]
    assert compt_row[5] is False and compt_row[4] is False, (
        "§6.2.2.1 第8/9/10步：抬 COMPT 之前必须先撤 BUSY 与 TR_REQ"
    )
    assert fsm.aborted is False and fsm.fault is None
