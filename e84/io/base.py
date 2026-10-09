"""I/O 后端抽象与内存实现。

约定：后端只处理**物理电平**，``True`` 表示引脚为高电平。
「哪个电平算 E84 的 ON」由 :mod:`e84.hal` 依据每个信号的 ``active_high`` 决定。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, List, Mapping, Optional

__all__ = ["PullMode", "DigitalIO", "InMemoryIO", "NullIO"]


class PullMode(str, Enum):
    """输入引脚的上拉/下拉配置。"""

    NONE = "none"
    UP = "up"
    DOWN = "down"


class DigitalIO(ABC):
    """数字 I/O 后端抽象。

    实现者只需保证「按通道名读写物理电平」。通道名是任意字符串，
    例如 ``"gpio17"``、``"OUT1"``、``"modbus:40001"``。
    """

    #: 该后端是否支持回读输出（用于 I/O 自检）。
    supports_readback: bool = False

    @abstractmethod
    def setup_input(self, channel: str, *, pull: PullMode = PullMode.NONE) -> None:
        """把一个通道配置为输入。"""

    @abstractmethod
    def setup_output(self, channel: str, *, initial: bool = False) -> None:
        """把一个通道配置为输出，并给出初始电平。"""

    @abstractmethod
    def read(self, channel: str) -> bool:
        """读取通道的物理电平（``True`` == 高）。"""

    @abstractmethod
    def write(self, channel: str, level: bool) -> None:
        """写入通道的物理电平（``True`` == 高）。仅对输出通道有效。"""

    def read_output(self, channel: str) -> Optional[bool]:
        """回读输出通道当前电平；不支持时返回 ``None``。"""

        return None

    def close(self) -> None:
        """释放资源。默认无操作。"""

    def describe(self) -> Mapping[str, str]:
        """返回后端描述信息，用于日志与诊断。"""

        return {"backend": type(self).__name__}


class InMemoryIO(DigitalIO):
    """内存后端：用于单元测试、仿真与离线回放。

    它同时扮演「引脚」与「外部世界」：
    用 :meth:`set_input` 模拟外部电路把某个输入通道拉高/拉低，
    用 :meth:`read` 观察本侧输出。
    """

    supports_readback = True

    def __init__(self, name: str = "memory") -> None:
        self._name = name
        self._levels: Dict[str, bool] = {}
        self._directions: Dict[str, str] = {}
        self._pulls: Dict[str, PullMode] = {}
        self._inputs: set[str] = set()
        self._outputs: set[str] = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 配置接口
    def setup_input(self, channel: str, *, pull: PullMode = PullMode.NONE) -> None:
        with self._lock:
            self._directions[channel] = "in"
            self._pulls[channel] = PullMode(pull)
            self._inputs.add(channel)
            self._outputs.discard(channel)
            # 上拉/下拉决定未驱动时的默认电平
            self._levels.setdefault(
                channel, pull is PullMode.UP
            )

    def setup_output(self, channel: str, *, initial: bool = False) -> None:
        with self._lock:
            self._directions[channel] = "out"
            self._outputs.add(channel)
            self._inputs.discard(channel)
            self._levels[channel] = bool(initial)

    # ------------------------------------------------------------ 读写接口
    def read(self, channel: str) -> bool:
        with self._lock:
            try:
                return self._levels[channel]
            except KeyError as exc:  # pragma: no cover - 配置错误
                raise KeyError(f"通道未配置: {channel!r}") from exc

    def write(self, channel: str, level: bool) -> None:
        with self._lock:
            if channel in self._inputs and channel not in self._outputs:
                raise ValueError(f"通道 {channel!r} 是输入，不能写")
            self._levels[channel] = bool(level)

    def read_output(self, channel: str) -> Optional[bool]:
        with self._lock:
            return self._levels.get(channel)

    # -------------------------------------------------------- 仿真辅助接口
    def set_input(self, channel: str, level: bool) -> None:
        """模拟外部电路驱动一个输入通道。"""

        with self._lock:
            if channel not in self._inputs:
                # 允许先设值后配置（方便测试里声明顺序自由）
                self._levels[channel] = bool(level)
                return
            self._levels[channel] = bool(level)

    def set_inputs(self, mapping: Mapping[str, bool]) -> None:
        """批量驱动输入通道。"""

        for channel, level in mapping.items():
            self.set_input(channel, level)

    def output_levels(self) -> Dict[str, bool]:
        """返回所有输出通道的当前电平。"""

        with self._lock:
            return {ch: self._levels.get(ch, False) for ch in sorted(self._outputs)}

    def input_levels(self) -> Dict[str, bool]:
        """返回所有输入通道的当前电平。"""

        with self._lock:
            return {ch: self._levels.get(ch, False) for ch in sorted(self._inputs)}

    def channels(self) -> List[str]:
        with self._lock:
            return sorted(self._levels)

    def describe(self) -> Mapping[str, str]:
        return {
            "backend": "InMemoryIO",
            "name": self._name,
            "inputs": ",".join(sorted(self._inputs)),
            "outputs": ",".join(sorted(self._outputs)),
        }

    def close(self) -> None:  # pragma: no cover - 内存后端无需释放
        return None


class NullIO(DigitalIO):
    """空后端：不接任何硬件，仅用于 dry-run 与配置校验。

    **安全语义（重要）**：不接硬件的输入应当读成"开路时的静态电平"，
    而不是一律读成低电平。因为在 SEMI E84 的标准接线里（光耦/干接点 + 上拉、
    低电平有效），低电平意味着 **ON**——若一律读低，dry-run 会让被动侧
    误以为对方已经把 ``VALID``/``CS_0`` 拉起来，从而凭空开始一次握手。

    因此本后端按输入通道的**上拉/下拉**决定静态电平：

    * ``pull=up``   → 高电平（在低有效接线里 = OFF）
    * ``pull=down`` → 低电平（在高有效接线里 = OFF）
    * ``pull=none`` → 由构造参数 ``idle_level`` 决定，默认 ``True``（高），
      即按本标准的光耦/上拉现场假设为 OFF。

    如果你的接线是反的（高有效且未配上拉），请显式设置 ``pull: down``，
    或直接使用真实后端。
    """

    supports_readback = False

    def __init__(self, idle_level: bool = True) -> None:
        self._idle_level = bool(idle_level)
        self._pulls: Dict[str, PullMode] = {}

    def setup_input(self, channel: str, *, pull: PullMode = PullMode.NONE) -> None:
        self._pulls[channel] = PullMode(pull)

    def setup_output(self, channel: str, *, initial: bool = False) -> None:
        return None

    def read(self, channel: str) -> bool:
        pull = self._pulls.get(channel, PullMode.NONE)
        if pull is PullMode.UP:
            return True
        if pull is PullMode.DOWN:
            return False
        return self._idle_level

    def write(self, channel: str, level: bool) -> None:
        return None

    def describe(self) -> Mapping[str, str]:
        return {"backend": "NullIO", "idle_level": "1" if self._idle_level else "0"}
