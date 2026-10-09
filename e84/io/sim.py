"""仿真后端：内存后端 + 故障注入。

仅用于测试/演示。它不模拟时间，时间的推进由 :class:`e84.clock.VirtualClock`
或虚拟线束（:mod:`e84.testing.harness`）负责。
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Set

from e84.io.base import InMemoryIO, PullMode

__all__ = ["SimIO"]


class SimIO(InMemoryIO):
    """可注入故障的内存后端。

    支持三类注入：

    * **输入卡死**（:meth:`stuck`）：模拟现场断线、光耦损坏、被短接；
    * **输出写失败**（:meth:`fail_writes`）：模拟总线/驱动错误；
    * **输入开路**：回到上拉/下拉的默认电平（:meth:`open_circuit`）。
    """

    def __init__(self, name: str = "sim") -> None:
        super().__init__(name=name)
        self._stuck: Dict[str, bool] = {}
        self._write_failures: Set[str] = set()
        self.write_errors: int = 0

    # ------------------------------------------------------------ 故障注入
    def stuck(self, channel: str, level: bool) -> None:
        """让某个输入通道永远读到 ``level``，忽略外部驱动。"""

        self._stuck[channel] = bool(level)

    def unstick(self, channel: str) -> None:
        """解除卡死注入。"""

        self._stuck.pop(channel, None)

    def is_stuck(self, channel: str) -> bool:
        return channel in self._stuck

    def open_circuit(self, channel: str) -> None:
        """模拟输入开路：回到上拉/下拉决定的默认电平。"""

        pull = self._pulls.get(channel, PullMode.NONE)
        self.set_input(channel, pull is PullMode.UP)

    def fail_writes(self, channel: str, fail: bool = True) -> None:
        """让某个输出通道的写入抛错。"""

        if fail:
            self._write_failures.add(channel)
        else:
            self._write_failures.discard(channel)

    # ------------------------------------------------------------ 覆盖读写
    def read(self, channel: str) -> bool:
        if channel in self._stuck:
            return self._stuck[channel]
        return super().read(channel)

    def write(self, channel: str, level: bool) -> None:
        if channel in self._write_failures:
            self.write_errors += 1
            raise OSError(f"注入的输出写失败: {channel!r}")
        super().write(channel, level)

    # ---------------------------------------------------------------- 诊断
    def injections(self) -> Mapping[str, object]:
        return {
            "stuck": dict(self._stuck),
            "write_failures": sorted(self._write_failures),
            "write_errors": self.write_errors,
        }

    def clear_injections(self) -> None:
        self._stuck.clear()
        self._write_failures.clear()
        self.write_errors = 0

    def describe(self) -> Mapping[str, str]:
        info = dict(super().describe())
        info["backend"] = "SimIO"
        info["stuck"] = ",".join(sorted(self._stuck))
        return info

    # 保持类型检查友好
    def read_output(self, channel: str) -> Optional[bool]:
        return super().read_output(channel)
