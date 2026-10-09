"""libgpiod 后端（树莓派 / 通用 Linux GPIO 字符设备）。

同时兼容两代 API：

* **libgpiod v2**（``gpiod.request_lines`` + ``LineSettings``）；
* **libgpiod v1**（``Chip.get_line`` + ``line.request``）。

通道名约定（简单、可预测）：

* 全数字字符串（如 ``"17"``）按 **line offset** 解析；
* 其他字符串按 **line name** 解析（设备树里给 GPIO 命名过的场合）。

安全设计：所有输出在**申请的那一刻**就被驱动到调用方给出的 ``initial`` 电平。
HAL 会把 ``initial`` 算成该信号的安全态（对 ``ES``/``HO_AVBL`` 即 OFF），
因此从进程启动到库接管之间不会出现「意外地宣告可交接」的窗口。

缺少 libgpiod 时抛出 ``RuntimeError``，并提示安装方式。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from e84.io.base import DigitalIO, PullMode

__all__ = ["LibGpiodIO"]


def _import_gpiod() -> Any:
    try:
        import gpiod  # type: ignore
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "未安装 libgpiod 的 Python 绑定。请安装后重试：\n"
            "  Debian/Ubuntu: sudo apt install python3-libgpiod    # 或 pip install gpiod\n"
            "  树莓派        : sudo apt install gpiod libgpiod-dev python3-libgpiod\n"
            "若只想先跑仿真，请使用后端 'memory' 或 'sim'。"
        ) from exc
    return gpiod


class LibGpiodIO(DigitalIO):
    """基于 libgpiod 的后端。"""

    supports_readback = True

    def __init__(self, chip: str = "/dev/gpiochip0", consumer: str = "e84") -> None:
        self._gpiod = _import_gpiod()
        self._chip_path = chip
        self._consumer = consumer
        self._lock = threading.RLock()
        self._api = self._detect_api()
        self._pulls: Dict[str, PullMode] = {}
        self._directions: Dict[str, str] = {}
        self._requests: Dict[str, Any] = {}
        self._chip = None

        if self._api == "v2":
            self._chip = self._gpiod.Chip(chip)
        else:  # v1
            self._chip = self._gpiod.Chip(chip)

    # ------------------------------------------------------------ API 探测
    def _detect_api(self) -> str:
        gpiod = self._gpiod
        if hasattr(gpiod, "request_lines") and hasattr(gpiod, "LineSettings"):
            return "v2"
        if hasattr(gpiod, "LINE_REQ_DIR_IN"):
            return "v1"
        raise RuntimeError(
            "无法识别 libgpiod 版本（既不是 v1 也不是 v2）。"
            "请升级 python3-libgpiod 或改用 'memory' 后端。"
        )

    # ------------------------------------------------------------ 通道解析
    @staticmethod
    def _is_offset(channel: str) -> bool:
        return channel.strip().isdigit()

    def _offset_of(self, channel: str) -> int:
        if self._is_offset(channel):
            return int(channel)
        line = self._chip.find_line(channel)  # v2
        if line is None:  # pragma: no cover - 取决于设备树
            raise ValueError(f"GPIO 芯片上找不到名为 {channel!r} 的线")
        return line.offset()

    # ------------------------------------------------------------ 配置接口
    def setup_input(self, channel: str, *, pull: PullMode = PullMode.NONE) -> None:
        with self._lock:
            self._pulls[channel] = PullMode(pull)
            self._directions[channel] = "in"
            self._request_input(channel)

    def setup_output(self, channel: str, *, initial: bool = False) -> None:
        with self._lock:
            self._directions[channel] = "out"
            self._request_output(channel, initial)

    def _request_input(self, channel: str) -> None:
        gpiod = self._gpiod
        pull = self._pulls.get(channel, PullMode.NONE)
        if self._api == "v2":
            from gpiod.line import Bias, Direction  # type: ignore

            bias = {
                PullMode.UP: Bias.PULL_UP,
                PullMode.DOWN: Bias.PULL_DOWN,
                PullMode.NONE: Bias.AS_IS,
            }[pull]
            offset = self._offset_of(channel)
            settings = gpiod.LineSettings(direction=Direction.INPUT, bias=bias)
            self._requests[channel] = self._chip.request_lines(
                {offset: settings}, consumer=self._consumer
            )
        else:  # v1
            flags = {
                PullMode.UP: gpiod.LINE_REQ_FLAG_BIAS_PULL_UP,
                PullMode.DOWN: gpiod.LINE_REQ_FLAG_BIAS_PULL_DOWN,
                PullMode.NONE: gpiod.LINE_REQ_FLAG_BIAS_DISABLE,
            }[pull]
            line = self._chip.get_line(self._offset_of(channel))
            line.request(
                consumer=self._consumer, type=gpiod.LINE_REQ_DIR_IN, flags=flags
            )
            self._requests[channel] = line

    def _request_output(self, channel: str, initial: bool) -> None:
        gpiod = self._gpiod
        if self._api == "v2":
            from gpiod.line import Direction, Value  # type: ignore

            offset = self._offset_of(channel)
            settings = gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=Value.ACTIVE if initial else Value.INACTIVE,
            )
            self._requests[channel] = self._chip.request_lines(
                {offset: settings}, consumer=self._consumer
            )
        else:  # v1
            line = self._chip.get_line(self._offset_of(channel))
            line.request(
                consumer=self._consumer,
                type=gpiod.LINE_REQ_DIR_OUT,
                default_vals=[1 if initial else 0],
            )
            self._requests[channel] = line

    # ------------------------------------------------------------ 读写接口
    def read(self, channel: str) -> bool:
        with self._lock:
            req = self._requests.get(channel)
            if req is None:
                raise KeyError(f"通道未配置: {channel!r}")
            if self._api == "v2":
                from gpiod.line import Value  # type: ignore

                return req.get_value(self._offset_of(channel)) == Value.ACTIVE
            return bool(req.get_value())

    def write(self, channel: str, level: bool) -> None:
        with self._lock:
            if self._directions.get(channel) != "out":
                raise ValueError(f"通道 {channel!r} 不是输出，不能写")
            req = self._requests[channel]
            if self._api == "v2":
                from gpiod.line import Value  # type: ignore

                req.set_value(
                    self._offset_of(channel), Value.ACTIVE if level else Value.INACTIVE
                )
            else:
                req.set_value(1 if level else 0)

    def read_output(self, channel: str) -> Optional[bool]:
        if self._directions.get(channel) != "out":
            return None
        try:
            return self.read(channel)
        except Exception:  # pragma: no cover - 视芯片能力而定
            return None

    def close(self) -> None:
        with self._lock:
            for req in self._requests.values():
                try:
                    if self._api == "v2":
                        req.release()
                    else:  # pragma: no cover - v1
                        req.release()
                except Exception:
                    pass
            self._requests.clear()
            chip = self._chip
            if chip is not None:
                try:
                    chip.close()
                except Exception:
                    pass
                self._chip = None

    def describe(self) -> Dict[str, str]:
        return {
            "backend": f"LibGpiodIO({self._api})",
            "chip": self._chip_path,
            "lines": str(len(self._requests)),
        }
