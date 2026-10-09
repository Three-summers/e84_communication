"""RPi.GPIO 后端（旧板卡过渡用）。

对应现场既有接线：``GPIO.setmode(BCM)``、E84 输入**上拉**（光耦导通把线拉低 = ON）、
输出低有效。**注意**：RPi.GPIO 只支持 BCM 编号，通道名必须是纯数字字符串。

新项目推荐使用 :class:`e84.io.libgpiod_backend.LibGpiodIO`；本后端仅为兼容
既有板卡（例如 VOC 项目的接线）保留。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from e84.io.base import DigitalIO, PullMode

__all__ = ["RPiGpioIO"]


def _import_rpi() -> Any:
    try:
        import RPi.GPIO as GPIO  # type: ignore
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "未安装 RPi.GPIO。请安装后重试：\n"
            "  pip install RPi.GPIO\n"
            "（在非树莓派平台上建议使用 'libgpiod' 或 'memory' 后端。）"
        ) from exc
    return GPIO


class RPiGpioIO(DigitalIO):
    """基于 RPi.GPIO（BCM 编号）的后端。"""

    supports_readback = True
    _shared_lock = threading.RLock()
    _instances = 0

    def __init__(self) -> None:
        self._GPIO = _import_rpi()
        self._lock = threading.RLock()
        self._directions: Dict[str, str] = {}
        with RPiGpioIO._shared_lock:
            self._GPIO.setmode(self._GPIO.BCM)
            self._GPIO.setwarnings(False)
            RPiGpioIO._instances += 1

    @staticmethod
    def _pin(channel: str) -> int:
        if not channel.strip().isdigit():
            raise ValueError(
                f"RPi.GPIO 后端要求通道名为 BCM 编号（纯数字），收到 {channel!r}"
            )
        return int(channel)

    def setup_input(self, channel: str, *, pull: PullMode = PullMode.NONE) -> None:
        with self._lock:
            pud = {
                PullMode.UP: self._GPIO.PUD_UP,
                PullMode.DOWN: self._GPIO.PUD_DOWN,
                PullMode.NONE: self._GPIO.PUD_OFF,
            }[pull]
            self._GPIO.setup(self._pin(channel), self._GPIO.IN, pull_up_down=pud)
            self._directions[channel] = "in"

    def setup_output(self, channel: str, *, initial: bool = False) -> None:
        with self._lock:
            self._GPIO.setup(self._pin(channel), self._GPIO.OUT)
            self._GPIO.output(self._pin(channel), self._GPIO.HIGH if initial else self._GPIO.LOW)
            self._directions[channel] = "out"

    def read(self, channel: str) -> bool:
        with self._lock:
            return bool(self._GPIO.input(self._pin(channel)))

    def write(self, channel: str, level: bool) -> None:
        with self._lock:
            if self._directions.get(channel) != "out":
                raise ValueError(f"通道 {channel!r} 不是输出，不能写")
            self._GPIO.output(self._pin(channel), self._GPIO.HIGH if level else self._GPIO.LOW)

    def read_output(self, channel: str) -> Optional[bool]:
        if self._directions.get(channel) != "out":
            return None
        try:
            return self.read(channel)
        except Exception:  # pragma: no cover
            return None

    def close(self) -> None:
        with RPiGpioIO._shared_lock:
            RPiGpioIO._instances -= 1
            if RPiGpioIO._instances <= 0:
                try:
                    self._GPIO.cleanup()
                except Exception:  # pragma: no cover
                    pass
                RPiGpioIO._instances = 0

    def describe(self) -> Dict[str, str]:
        return {"backend": "RPiGpioIO(BCM)", "lines": str(len(self._directions))}
