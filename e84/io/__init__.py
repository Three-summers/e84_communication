"""I/O 后端包。

对外只暴露 :class:`e84.io.base.DigitalIO` 抽象。上层（HAL、控制器）永远只按
**通道名**读写**物理电平**（``True`` == 高电平），极性/上拉/去抖等现场差异
全部由 :mod:`e84.hal` 依据配置处理。
"""

from __future__ import annotations

from e84.io.base import DigitalIO, InMemoryIO, NullIO, PullMode
from e84.io.sim import SimIO

__all__ = ["DigitalIO", "InMemoryIO", "NullIO", "SimIO", "PullMode", "open_backend"]


def open_backend(name: str, **kwargs):
    """按名字惰性打开一个后端。

    已知后端：``"memory"``、``"sim"``、``"null"``、``"libgpiod"``、``"rpi"``。
    可选后端在缺少第三方依赖或被沙箱限制时会抛出带安装提示的 ``RuntimeError``。
    """

    key = name.strip().lower()
    if key in ("memory", "inmemory"):
        return InMemoryIO()
    if key == "sim":
        return SimIO()
    if key == "null":
        return NullIO()
    if key in ("libgpiod", "gpiod"):
        from e84.io.libgpiod_backend import LibGpiodIO

        return LibGpiodIO(**kwargs)
    if key in ("rpi", "rpi.gpio", "rpigpio", "rpi_gpio"):
        from e84.io.rpi_gpio_backend import RPiGpioIO

        return RPiGpioIO(**kwargs)
    raise ValueError(
        f"未知 I/O 后端: {name!r}"
        "（可选: memory / sim / null / libgpiod / rpi）"
    )
