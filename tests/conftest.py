"""测试目录级 pytest 配置。

职责：

1. 把仓库根插入 ``sys.path``，这样测试可以直接 ``import e84`` 与
   ``from tests.support import ...``，无需安装包；
2. 提供全局通用 fixture：``cfg``（示例 2 载口标准配置）、
   ``iface``（该配置的 PI/O 接口）、``make_rig``（虚拟线束工厂）、
   ``make_controller``（PiOController 工厂，带 SimIO + VirtualClock）。
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from e84 import Equipment, ManualRunner, SimIO, VirtualClock, load_file  # noqa: E402
from e84.controller import PiOController  # noqa: E402
from e84.events import EventType  # noqa: E402
from e84.testing import VirtualRig  # noqa: E402
from tests.support import logical_from_level  # noqa: E402

EXAMPLE_CONFIG = ROOT / "configs" / "example_2lp_standard.yaml"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def example_config_path() -> pathlib.Path:
    """可直接加载的示例配置路径。"""

    return EXAMPLE_CONFIG


@pytest.fixture(scope="session")
def cfg():
    """示例 2 载口标准配置（frozen dataclass，会话级共享安全）。"""

    return load_file(EXAMPLE_CONFIG)


@pytest.fixture(scope="session")
def iface(cfg):
    """示例配置里的单个 PI/O 接口配置。"""

    return cfg.interfaces[0]


# --------------------------------------------------------------------------- #
# 控制器工厂（SimIO + VirtualClock）
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_controller(cfg):
    """返回 ``make_controller(...)`` 工厂。

    产物是一个 :class:`types.SimpleNamespace`：

    * ``io``         —— ``SimIO``（物理电平）
    * ``clock``      —— ``VirtualClock``
    * ``equipment``  —— ``Equipment``
    * ``runner``     —— ``ManualRunner``
    * ``controller`` —— ``PiOController``
    * ``poll(dt)``   —— 推进虚拟时间并轮询一拍
    * ``set(signal, logical)`` —— 按**逻辑值**驱动一个 E84 输入信号
      （自动换算物理电平，测试里不必关心 active_high）
    * ``logical_out(signal)`` —— 按逻辑值回读一个 E84 输出信号
    """

    def _make(interface_id: str = "PIO1", *, validate: bool = False):
        io = SimIO("test-sim")
        clk = VirtualClock()
        equipment = Equipment(cfg, clock=clk, ios={interface_id: io}, validate=validate)
        runner = ManualRunner(equipment)
        controller: PiOController = equipment.controller(interface_id)

        def set_signal(name: str, logical: bool) -> None:
            spec = controller.cfg.inputs.get(name)
            if spec is None:
                raise KeyError(f"配置里没有输入信号 {name!r}")
            level = bool(logical) if spec.active_high else (not logical)
            io.set_input(spec.channel, level)

        def logical_out(name: str) -> bool:
            spec = controller.cfg.outputs.get(name)
            if spec is None:
                raise KeyError(f"配置里没有输出信号 {name!r}")
            return logical_from_level(spec.active_high, io.read(spec.channel))

        def poll(dt: float = 0.02):
            clk.advance(dt)
            return controller.poll(now=clk.now())

        return SimpleNamespace(
            io=io,
            clock=clk,
            equipment=equipment,
            runner=runner,
            controller=controller,
            set=set_signal,
            logical_out=logical_out,
            poll=poll,
        )

    return _make


# --------------------------------------------------------------------------- #
# 虚拟线束工厂
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_rig(cfg):
    """返回 ``make_rig(jobs=..., continuous=..., ...)`` 工厂（默认不自动启动）。"""

    def _make(*, jobs=None, continuous: bool = False, autostart: bool = False, **kwargs):
        return VirtualRig(
            cfg,
            jobs=list(jobs or []),
            continuous=continuous,
            clock=VirtualClock(),
            autostart=autostart,
            **kwargs,
        )

    return _make


# --------------------------------------------------------------------------- #
# 事件收集器
# --------------------------------------------------------------------------- #
@pytest.fixture
def collect_events():
    """返回订阅用的收集器工厂：``collect_events(bus, event_type=None) -> list``。"""

    def _collect(bus, event_type: EventType | None = EventType.OUTPUT_CHANGED):
        events: list = []
        bus.subscribe(event_type, events.append)
        return events

    return _collect
