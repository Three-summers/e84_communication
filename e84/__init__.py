"""E84 被动端（装备端）并行 I/O 接口库。

**一句话**：按 SEMI E84-0301 实现被动侧的载具交接时序，硬件解耦、完全可配置、
可在虚拟时钟上逐拍复现。

最小用法::

    from e84 import Equipment, load_file

    cfg = load_file("configs/example_2lp_standard.yaml")
    eq = Equipment(cfg)
    from e84 import ThreadRunner
    runner = ThreadRunner(eq)
    runner.start()
    ...
    runner.stop()

测试/仿真用法::

    from e84 import Equipment, VirtualClock, ManualRunner, SimIO

    clk = VirtualClock()
    eq = Equipment(cfg, clock=clk, ios={"PIO1": SimIO()})
    runner = ManualRunner(eq)
    runner.poll(now=clk.now())
    clk.advance(0.01)
    runner.poll(now=clk.now())

模块地图：

===========================  ==================================================
:mod:`e84.signals`           逻辑信号定义与方向（§6.1 表1）
:mod:`e84.model`             ``Inputs`` / ``Outputs`` / ``PortSnapshot`` / ``State``
:mod:`e84.fsm`               **协议核心**（纯逻辑状态机）
:mod:`e84.timers`            ``TP1``…``TP6`` / ``TD0`` / ``TD1`` 与可编程设定值
:mod:`e84.hal`               逻辑信号 ↔ 物理通道、极性、去抖、安全态
:mod:`e84.sensors`           载具/门/夹持/访问模式等本机传感量
:mod:`e84.config`            配置模型、加载器（YAML/JSON/TOML）、交叉校验
:mod:`e84.controller`        单个 PI/O 的控制器
:mod:`e84.equipment`         设备门面（多 PI/O 聚合）
:mod:`e84.runner`            手动 / 后台线程运行器
:mod:`e84.fault`             故障分类与故障对象
:mod:`e84.events`            事件与事件总线
:mod:`e84.trace`             信号跳变追踪（CSV/JSONL）
:mod:`e84.active`            主动侧参考实现（仅用于自测/回放，不是产品代码）
:mod:`e84.testing`           虚拟线束与黄金时序场景
:mod:`e84.cli`               ``python -m e84.cli validate|monitor|selftest|sim|force|faults``
                             （本项目**不做打包**，因此统一用 ``python -m e84.cli``，
                             而不是 ``e84-validate`` 这类 console_scripts 入口）
===========================  ==================================================
"""

from __future__ import annotations

from e84.clock import Clock, RealClock, VirtualClock
from e84.config import (
    AccessModeSpec,
    ChannelSpec,
    EquipmentConfig,
    IntentSpec,
    InterfaceConfig,
    IoDefaults,
    LoadPortConfig,
    PolicyConfig,
    SensorSpec,
    TraceConfig,
    ValidationReport,
    WatchdogConfig,
    jsonable,
    load_dict,
    load_file,
    load_json,
    load_toml,
    load_yaml,
    validate_config,
)
from e84.controller import PiOController
from e84.equipment import Equipment
from e84.events import Event, EventBus, EventType
from e84.fault import ConfigError, Fault, FaultCode
from e84.fsm import PassiveFsm, StepResult
from e84.hal import InputBinding, OutputBinding, SignalMap
from e84.io import DigitalIO, InMemoryIO, NullIO, SimIO, open_backend
from e84.io.base import PullMode
from e84.loadport import LoadPort
from e84.model import (
    AccessMode,
    Inputs,
    Op,
    Outputs,
    PortRole,
    PortSnapshot,
    Scenario,
    State,
    Topology,
)
from e84.runner import ManualRunner, ThreadRunner
from e84.sensors import SensorBank
from e84.signals import Direction, Signal, as_signal_name, db25_pins, direction_of
from e84.timers import PASSIVE_TIMERS, TIMER_SPECS, TimerId, TimerSet
from e84.trace import TraceRecorder
from e84.version import __version__

__all__ = [
    "__version__",
    # 顶层门面
    "Equipment",
    "PiOController",
    "ManualRunner",
    "ThreadRunner",
    # 配置
    "EquipmentConfig",
    "InterfaceConfig",
    "LoadPortConfig",
    "PolicyConfig",
    "SensorSpec",
    "ChannelSpec",
    "AccessModeSpec",
    "IntentSpec",
    "IoDefaults",
    "TraceConfig",
    "WatchdogConfig",
    "load_dict",
    "load_file",
    "load_json",
    "load_toml",
    "load_yaml",
    "validate_config",
    "ValidationReport",
    "jsonable",
    # 协议核心与数据模型
    "PassiveFsm",
    "StepResult",
    "Inputs",
    "Outputs",
    "PortSnapshot",
    "LoadPort",
    "AccessMode",
    "Op",
    "PortRole",
    "Scenario",
    "State",
    "Topology",
    # 信号与定时器
    "Signal",
    "Direction",
    "direction_of",
    "db25_pins",
    "as_signal_name",
    "TimerId",
    "TimerSet",
    "TIMER_SPECS",
    "PASSIVE_TIMERS",
    # I/O 与时钟
    "DigitalIO",
    "InMemoryIO",
    "SimIO",
    "NullIO",
    "PullMode",
    "open_backend",
    "SignalMap",
    "InputBinding",
    "OutputBinding",
    "SensorBank",
    "Clock",
    "RealClock",
    "VirtualClock",
    # 故障与事件
    "Fault",
    "FaultCode",
    "ConfigError",
    "Event",
    "EventBus",
    "EventType",
    "TraceRecorder",
]
