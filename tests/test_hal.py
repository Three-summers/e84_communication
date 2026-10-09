"""信号层 HAL 测试（逻辑 ↔ 物理、极性、去抖、安全态、共用通道）。

覆盖标准条款：

* §6.1 表1 / 表9 —— 现场可能把 ``L_REQ`` 与 ``U_REQ`` 并在一条线上：允许共用同一物理通道，
  但不能被**同时**置 ON（协议逻辑错误）；
* §6.2.5.2 —— ``HO_AVBL`` 在无效/异常时 OFF；
* §6.4.6 —— OFF = 无电流/无光；输出安全态即 ``safe_on``（默认 OFF，失效安全）；
* 实现约定 —— 「写变化才写」（write-on-change）降低总线/GPIO 负载；
  输出回读用于 I/O 自检。
"""

from __future__ import annotations

import pytest

from e84.hal import (
    InputBinding,
    OutputBinding,
    SignalMap,
    as_signal,
    signal_name,
)
from e84.io.base import InMemoryIO, NullIO, PullMode
from e84.model import Outputs
from e84.signals import Signal
from tests.support import level_for_logical, logical_from_level


def _map(
    io,
    inputs=None,
    outputs=None,
):
    return SignalMap(io, inputs or {}, outputs or {})


# --------------------------------------------------------------------------- #
# 名称与方向解析
# --------------------------------------------------------------------------- #
def test_signal_name_and_as_signal():
    assert signal_name("l_req") == "L_REQ"
    assert signal_name(Signal.CS_0) == "CS_0"
    assert signal_name("ho-avbl") == "HO_AVBL"
    assert as_signal("valid") is Signal.VALID
    assert as_signal("GO") is None  # 现场自定义信号


# --------------------------------------------------------------------------- #
# 极性
# --------------------------------------------------------------------------- #
def test_input_polarity_active_high_and_active_low():
    io = InMemoryIO()
    sm = _map(
        io,
        inputs={
            "VALID": InputBinding("IN1", active_high=True),
            "BUSY": InputBinding("IN2", active_high=False),
        },
    )
    sm.setup()

    # active_high=True：高电平 = ON
    io.set_input("IN1", True)
    assert sm.read(0.0)["VALID"] is True
    io.set_input("IN1", False)
    assert sm.read(0.0)["VALID"] is False

    # active_high=False：低电平 = ON（示例配置的现场接法）
    io.set_input("IN2", False)
    assert sm.read(0.0)["BUSY"] is True
    io.set_input("IN2", True)
    assert sm.read(0.0)["BUSY"] is False


def test_output_polarity_active_high_and_active_low():
    io = InMemoryIO()
    sm = _map(
        io,
        outputs={
            "L_REQ": OutputBinding("OUT1", active_high=True),
            "ES": OutputBinding("OUT8", active_high=False),
        },
    )
    sm.setup()

    sm.apply(Outputs(l_req=True, es=True))
    assert io.read("OUT1") is True          # 高有效：ON -> 高
    assert io.read("OUT8") is False         # 低有效：ON -> 低

    sm.apply(Outputs(l_req=False, es=False))
    assert io.read("OUT1") is False
    assert io.read("OUT8") is True


def test_polarity_roundtrip_helper_matches_hal_convention():
    for active_high in (True, False):
        for logical in (True, False):
            level = level_for_logical(active_high, logical)
            assert logical_from_level(active_high, level) is logical


# --------------------------------------------------------------------------- #
# 去抖："稳定才提交"
# --------------------------------------------------------------------------- #
def test_debounce_commits_only_after_stable_window():
    io = InMemoryIO()
    sm = _map(io, inputs={"VALID": InputBinding("IN1", active_high=True, debounce_ms=10)})
    sm.setup()  # 初值：低电平 -> OFF

    io.set_input("IN1", True)  # 抖动开始
    assert sm.read(0.000)["VALID"] is False   # 立即读：仍是旧值
    assert sm.read(0.005)["VALID"] is False   # 窗口内：不提交
    assert sm.read(0.009)["VALID"] is False
    assert sm.read(0.010)["VALID"] is True    # 稳定满 10ms：提交


def test_debounce_ignores_short_glitch():
    io = InMemoryIO()
    sm = _map(io, inputs={"VALID": InputBinding("IN1", debounce_ms=20)})
    sm.setup()

    io.set_input("IN1", True)
    sm.read(0.0)
    sm.read(0.005)
    io.set_input("IN1", False)  # 5ms 后回到原值：毛刺被吞掉
    assert sm.read(0.010)["VALID"] is False
    assert sm.read(0.100)["VALID"] is False


def test_debounce_disabled_is_immediate():
    io = InMemoryIO()
    sm = _map(io, inputs={"VALID": InputBinding("IN1", debounce_ms=0)})
    sm.setup()
    io.set_input("IN1", True)
    assert sm.read(0.0)["VALID"] is True


def test_setup_registers_pull_and_default_level():
    io = InMemoryIO()
    sm = _map(
        io,
        inputs={
            "VALID": InputBinding("IN1", active_high=True, pull=PullMode.UP),
            "TR_REQ": InputBinding("IN2", active_high=True, pull=PullMode.DOWN),
        },
    )
    sm.setup()
    # 上拉：未驱动 -> 高 -> ON；下拉：未驱动 -> 低 -> OFF
    values = sm.read(0.0)
    assert values["VALID"] is True
    assert values["TR_REQ"] is False


def test_logical_inputs_snapshot_cached():
    io = InMemoryIO()
    sm = _map(io, inputs={"VALID": InputBinding("IN1")})
    sm.setup()
    io.set_input("IN1", True)
    sm.read(0.0)
    assert sm.logical_inputs["VALID"] is True
    assert sm.input_names == ("VALID",)
    assert sm.input_channels == ("IN1",)


# --------------------------------------------------------------------------- #
# 写变化才写
# --------------------------------------------------------------------------- #
def test_write_on_change_count_does_not_grow_on_repeat_apply():
    io = InMemoryIO()
    sm = _map(io, outputs={"L_REQ": OutputBinding("OUT1"), "ES": OutputBinding("OUT8")})
    sm.setup()
    assert sm.write_count == 0

    sm.apply(Outputs(l_req=True))
    assert sm.write_count == 1
    assert "OUT1" in sm.last_written

    # 第二次 apply 同样的逻辑值：不应再写
    written = sm.apply(Outputs(l_req=True))
    assert written == {}
    assert sm.write_count == 1

    # 逻辑值变化才写
    sm.apply(Outputs(l_req=False))
    assert sm.write_count == 2
    assert io.read("OUT1") is False


def test_write_on_change_multiple_channels():
    io = InMemoryIO()
    sm = _map(
        io,
        outputs={
            "L_REQ": OutputBinding("OUT1"),
            "READY": OutputBinding("OUT4"),
        },
    )
    sm.setup()
    written = sm.apply(Outputs(l_req=True, ready=True))
    assert set(written) == {"OUT1", "OUT4"}
    assert sm.write_count == 2


# --------------------------------------------------------------------------- #
# 共用通道（现场把两根请求线并在一起）
# --------------------------------------------------------------------------- #
def test_shared_channel_single_signal_on():
    io = InMemoryIO()
    sm = _map(
        io,
        outputs={
            "L_REQ": OutputBinding("OUT1", active_high=False),
            "U_REQ": OutputBinding("OUT1", active_high=False),
        },
    )
    sm.setup()
    assert sm.shared_channels() == {"OUT1": ("L_REQ", "U_REQ")}

    sm.apply(Outputs(l_req=True, u_req=False))
    # active_high=False：ON -> 低电平
    assert io.read("OUT1") is False

    sm.apply(Outputs(l_req=False, u_req=True))
    assert io.read("OUT1") is False

    sm.apply(Outputs())
    assert io.read("OUT1") is True  # OFF -> 高


def test_shared_channel_both_on_raises():
    """L_REQ 与 U_REQ 同时有效是协议逻辑错误，必须显式失败。"""

    io = InMemoryIO()
    sm = _map(
        io,
        outputs={
            "L_REQ": OutputBinding("OUT1"),
            "U_REQ": OutputBinding("OUT1"),
        },
    )
    sm.setup()
    with pytest.raises(ValueError):
        sm.apply(Outputs(l_req=True, u_req=True))


def test_shared_channel_polarity_conflict_rejected_at_construction():
    io = InMemoryIO()
    with pytest.raises(ValueError):
        _map(
            io,
            outputs={
                "L_REQ": OutputBinding("OUT1", active_high=True),
                "U_REQ": OutputBinding("OUT1", active_high=False),
            },
        )


def test_input_output_channel_conflict_rejected_at_construction():
    io = InMemoryIO()
    with pytest.raises(ValueError):
        _map(
            io,
            inputs={"VALID": InputBinding("P1")},
            outputs={"READY": OutputBinding("P1")},
        )


def test_channel_of_and_output_channels():
    io = InMemoryIO()
    sm = _map(
        io,
        inputs={"VALID": InputBinding("IN1")},
        outputs={
            "L_REQ": OutputBinding("OUT1"),
            "U_REQ": OutputBinding("OUT1"),
            "READY": OutputBinding("OUT4"),
        },
    )
    assert sm.channel_of("VALID") == "IN1"
    assert sm.channel_of("l_req") == "OUT1"
    assert sm.channel_of("NOPE") is None
    assert sm.output_channels == ("OUT1", "OUT4")  # 去重、保持配置顺序


# --------------------------------------------------------------------------- #
# 安全态与回读
# --------------------------------------------------------------------------- #
def test_safe_state_drives_es_and_ho_avbl_off():
    """§6.4.6 / R1-1.1.2.1：安全态下 ES、HO_AVBL 必须为 OFF。"""

    io = InMemoryIO()
    sm = _map(
        io,
        outputs={
            "ES": OutputBinding("OUT8", active_high=True, safe_on=False),
            "HO_AVBL": OutputBinding("OUT7", active_high=True, safe_on=False),
        },
    )
    sm.setup()
    sm.apply(Outputs(es=True, ho_avbl=True))
    assert io.read("OUT8") is True
    assert io.read("OUT7") is True

    written = sm.safe_state()
    assert set(written) == {"OUT7", "OUT8"}
    assert io.read("OUT8") is False   # OFF
    assert io.read("OUT7") is False   # OFF


def test_safe_state_with_active_low_outputs_is_logically_off():
    io = InMemoryIO()
    sm = _map(
        io,
        outputs={
            "ES": OutputBinding("OUT8", active_high=False, safe_on=False),
            "HO_AVBL": OutputBinding("OUT7", active_high=False, safe_on=False),
        },
    )
    sm.setup()
    sm.apply(Outputs(es=True, ho_avbl=True))
    sm.safe_state()
    # 低有效：逻辑 OFF == 物理高
    assert io.read("OUT8") is True
    assert io.read("OUT7") is True
    assert logical_from_level(False, io.read("OUT8")) is False
    assert logical_from_level(False, io.read("OUT7")) is False


def test_safe_state_honours_safe_on_true():
    io = InMemoryIO()
    sm = _map(io, outputs={"READY": OutputBinding("OUT4", safe_on=True)})
    sm.setup()
    assert io.read("OUT4") is True  # setup 即安全态
    sm.apply(Outputs())
    sm.safe_state()
    assert io.read("OUT4") is True


def test_verify_outputs_readback():
    io = InMemoryIO()
    sm = _map(
        io,
        outputs={
            "L_REQ": OutputBinding("OUT1", active_high=False),
            "READY": OutputBinding("OUT4", active_high=True),
        },
    )
    sm.setup()
    outputs = Outputs(l_req=True, ready=False)
    sm.apply(outputs)

    report = sm.verify_outputs(outputs)
    for channel, (expected, actual) in report.items():
        assert actual is not None
        assert expected == actual, channel

    # 期望与实际不一致时能被发现（模拟现场写失败）
    report_bad = sm.verify_outputs(Outputs(l_req=False, ready=True))
    assert any(expected != actual for expected, actual in report_bad.values())


# --------------------------------------------------------------------------- #
# 强制输出与描述
# --------------------------------------------------------------------------- #
def test_force_output_bypasses_logic():
    io = InMemoryIO()
    sm = _map(io, outputs={"READY": OutputBinding("OUT4", active_high=True)})
    sm.setup()
    level = sm.force_output("READY", True)
    assert level is True
    assert io.read("OUT4") is True

    with pytest.raises(KeyError):
        sm.force_output("NOPE", True)


def test_describe_contains_mapping_and_shared_channels():
    io = InMemoryIO()
    sm = _map(
        io,
        inputs={"VALID": InputBinding("IN1", pull=PullMode.UP, debounce_ms=2)},
        outputs={
            "L_REQ": OutputBinding("OUT1"),
            "U_REQ": OutputBinding("OUT1"),
        },
    )
    desc = sm.describe()
    assert desc["inputs"]["VALID"]["channel"] == "IN1"
    assert desc["inputs"]["VALID"]["pull"] == "up"
    assert desc["outputs"]["L_REQ"]["channel"] == "OUT1"
    assert desc["shared_channels"] == {"OUT1": ["L_REQ", "U_REQ"]}


# --------------------------------------------------------------------------- #
# NullIO：dry-run 后端不得凭空开始握手（失效安全）
# --------------------------------------------------------------------------- #
def test_nullio_static_levels_follow_pull():
    """``NullIO`` 按输入通道的 pull 给出静态电平。

    §6.4.6 的现场接法是「光耦/干接点 + 上拉、低电平有效」：
    开路（无人驱动）必须被读成 **OFF**，否则 dry-run 会让被动侧以为对方
    已经把 ``VALID``/``CS_0`` 拉起来，凭空开始一次握手。
    """

    io = NullIO()
    io.setup_input("UP", pull=PullMode.UP)
    io.setup_input("DOWN", pull=PullMode.DOWN)
    io.setup_input("NONE", pull=PullMode.NONE)
    assert io.read("UP") is True          # 上拉 -> 高电平
    assert io.read("DOWN") is False       # 下拉 -> 低电平
    assert io.read("NONE") is True        # pull=none -> 默认空闲电平（高）
    assert NullIO(idle_level=False).read("NONE") is False


def test_nullio_does_not_phantom_start_handshake():
    """低有效 + 上拉的标准接线下，NullIO 读到的 VALID/CS_x 都必须是 OFF。"""

    io = NullIO()
    sm = SignalMap(
        io,
        inputs={
            "VALID": InputBinding("IN1", active_high=False, pull=PullMode.UP),
            "CS_0": InputBinding("IN2", active_high=False, pull=PullMode.UP),
            "TR_REQ": InputBinding("IN5", active_high=False, pull=PullMode.UP),
        },
        outputs={},
    )
    sm.setup()
    values = sm.read(0.0)
    assert values["VALID"] is False
    assert values["CS_0"] is False
    assert values["TR_REQ"] is False

    # 高有效接线必须显式配 pull=down，否则默认空闲电平（高）会被读成 ON
    io2 = NullIO()
    sm2 = SignalMap(
        io2,
        inputs={"VALID": InputBinding("IN1", active_high=True, pull=PullMode.DOWN)},
        outputs={},
    )
    sm2.setup()
    assert sm2.read(0.0)["VALID"] is False
