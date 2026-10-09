"""本机传感量测试（input / keys(any) / keys(all) / external / constant + 去抖）。

覆盖标准条款：

* §6.1 表1 —— 判定 ``L_REQ``/``U_REQ`` 落下必须依据「载具位于正确位置」，
  现场常用「任意三键=检测到载具」与「三键全落=完整落位」两级判定；
* §6.4.6 —— 传感量同样遵守「OFF = 无电流」的逻辑约定，物理极性可逐通道配置；
* 现场做法 —— 三键/多传感器组合、外部（E87/E30）注入、无机构时的常量。
"""

from __future__ import annotations

import pytest

from e84.config.model import SensorSpec
from e84.io.base import InMemoryIO, PullMode
from e84.sensors import SensorBank


def _spec(**kwargs) -> SensorSpec:
    return SensorSpec(**kwargs)


# --------------------------------------------------------------------------- #
# 四种来源
# --------------------------------------------------------------------------- #
def test_constant_source():
    io = InMemoryIO()
    bank = SensorBank({"always": _spec(source="constant", value=True),
                       "never": _spec(source="constant", value=False)})
    bank.setup(io)
    values = bank.read(io, 0.0)
    assert values["always"] is True
    assert values["never"] is False
    assert bank.channels == {}  # 常量不占用任何通道


def test_input_source_with_polarity():
    io = InMemoryIO()
    bank = SensorBank(
        {
            "present": _spec(source="input", channel="K0", active_high=False),
            "position": _spec(source="input", channel="K1", active_high=True),
        }
    )
    bank.setup(io)
    io.set_input("K0", False)  # 低有效 -> ON
    io.set_input("K1", True)
    values = bank.read(io, 0.0)
    assert values["present"] is True
    assert values["position"] is True

    io.set_input("K0", True)
    io.set_input("K1", False)
    values = bank.read(io, 0.0)
    assert values["present"] is False
    assert values["position"] is False


def test_keys_any_means_carrier_detected():
    io = InMemoryIO()
    bank = SensorBank(
        {"present": _spec(source="keys", channels=("K0", "K1", "K2"), mode="any")}
    )
    bank.setup(io)
    assert bank.read(io, 0.0)["present"] is False

    io.set_input("K1", True)  # 任意一键
    assert bank.read(io, 0.0)["present"] is True

    io.set_input("K1", False)
    assert bank.read(io, 0.0)["present"] is False


def test_keys_all_means_carrier_in_position():
    io = InMemoryIO()
    bank = SensorBank(
        {"position": _spec(source="keys", channels=("K0", "K1", "K2"), mode="all")}
    )
    bank.setup(io)
    io.set_input("K0", True)
    io.set_input("K1", True)
    assert bank.read(io, 0.0)["position"] is False  # 还差一键

    io.set_input("K2", True)
    assert bank.read(io, 0.0)["position"] is True

    io.set_input("K1", False)
    assert bank.read(io, 0.0)["position"] is False


def test_same_keys_for_any_and_all_in_one_bank():
    """同一批键做 any/all 两级判定是现场正常做法，SensorBank 必须接受。"""

    io = InMemoryIO()
    bank = SensorBank(
        {
            "present": _spec(source="keys", channels=("K0", "K1", "K2"), mode="any"),
            "position": _spec(source="keys", channels=("K0", "K1", "K2"), mode="all"),
        }
    )
    bank.setup(io)
    io.set_input("K0", True)
    values = bank.read(io, 0.0)
    assert values["present"] is True
    assert values["position"] is False


def test_external_source_injected():
    io = InMemoryIO()
    bank = SensorBank(
        {
            "present": _spec(source="external", value=False),
            "position": _spec(source="external", value=True),
        }
    )
    bank.setup(io)
    values = bank.read(io, 0.0)
    assert values["present"] is False   # 未注入 -> 配置默认值
    assert values["position"] is True

    values = bank.read(io, 0.0, external={"present": True, "position": False})
    assert values["present"] is True
    assert values["position"] is False


# --------------------------------------------------------------------------- #
# 去抖
# --------------------------------------------------------------------------- #
def test_input_debounce_stable_only():
    io = InMemoryIO()
    bank = SensorBank(
        {"present": _spec(source="input", channel="K0", debounce_ms=20)}
    )
    bank.setup(io)
    # 第一次读取确立基线（与 SignalMap.setup 的"先读一次"一致）
    assert bank.read(io, 0.000)["present"] is False

    io.set_input("K0", True)
    assert bank.read(io, 0.001)["present"] is False   # 立即读：仍是旧值
    assert bank.read(io, 0.010)["present"] is False   # 窗口内：不提交
    assert bank.read(io, 0.020)["present"] is False   # 还差 1ms
    assert bank.read(io, 0.021)["present"] is True    # 稳定满 20ms：提交


def test_keys_debounce_applies_per_channel():
    io = InMemoryIO()
    bank = SensorBank(
        {
            "position": _spec(
                source="keys", channels=("K0", "K1"), mode="all", debounce_ms=10
            )
        }
    )
    bank.setup(io)
    assert bank.read(io, 0.0)["position"] is False  # 确立基线

    io.set_input("K0", True)
    io.set_input("K1", True)
    assert bank.read(io, 0.001)["position"] is False
    assert bank.read(io, 0.009)["position"] is False
    # 注意浮点：0.011-0.001 会略小于 0.01，这里用 0.015 表示"稳定已满"
    assert bank.read(io, 0.015)["position"] is True


# --------------------------------------------------------------------------- #
# 装配与描述
# --------------------------------------------------------------------------- #
def test_setup_configures_inputs_and_channels():
    io = InMemoryIO()
    bank = SensorBank(
        {
            "present": _spec(source="input", channel="K0", pull="up"),
            "position": _spec(source="keys", channels=("K1", "K2"), mode="all"),
        }
    )
    bank.setup(io)
    assert set(bank.channels) == {"K0", "K1", "K2"}
    assert bank.channels["K0"].pull is PullMode.UP
    # 上拉未驱动 -> 高 -> ON（active_high 默认 True）
    assert bank.read(io, 0.0)["present"] is True


def test_last_and_describe():
    io = InMemoryIO()
    bank = SensorBank(
        {
            "present": _spec(source="input", channel="K0"),
            "never": _spec(source="constant", value=False),
        }
    )
    bank.setup(io)
    io.set_input("K0", True)
    values = bank.read(io, 0.0)
    assert bank.last == values
    desc = bank.describe()
    assert desc["present"]["source"] == "input"
    assert desc["never"]["value"] is False


def test_conflicting_channel_polarity_rejected():
    with pytest.raises(ValueError):
        SensorBank(
            {
                "a": _spec(source="input", channel="K0", active_high=True),
                "b": _spec(source="input", channel="K0", active_high=False),
            }
        )


def test_conflicting_channel_pull_rejected():
    with pytest.raises(ValueError):
        SensorBank(
            {
                "a": _spec(source="input", channel="K0", pull="up"),
                "b": _spec(source="keys", channels=("K0",), mode="any", pull="down"),
            }
        )


def test_unknown_source_raises():
    with pytest.raises(ValueError):
        SensorBank({"x": _spec(source="magic")}).read(InMemoryIO(), 0.0)
