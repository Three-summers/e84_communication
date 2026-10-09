"""本机传感量读取。

E84 的信号线（``VALID``/``CS_0``/…）由 :mod:`e84.hal` 管理；而载具检测、
门/夹持状态、访问模式开关这类**本机传感量**在本模块统一处理。

它把配置里的 :class:`e84.config.model.SensorSpec` 变成可读的布尔量，支持四种来源：

* ``input``    —— 单个 GPIO 输入通道（含逐通道极性、上拉、去抖）；
* ``keys``     —— 多通道组合：``any`` 表示「检测到」（常见于三键/多传感器「有料」判定），
  ``all`` 表示「完整落位」（这才是判定 ``L_REQ``/``U_REQ`` 落下的依据）；
* ``external`` —— 由上层通过 API 注入（例如来自 E87/E30 或设备主控）；
* ``constant`` —— 固定值。
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from e84.config.model import SensorSpec
from e84.io.base import DigitalIO, PullMode

__all__ = ["SensorBank", "SensorChannel"]


class _Debounce:
    __slots__ = ("stable", "candidate", "since", "initialised")

    def __init__(self) -> None:
        self.stable = False
        self.candidate = False
        self.since = 0.0
        self.initialised = False

    def update(self, logical: bool, now: float, window: float) -> bool:
        if not self.initialised:
            self.stable = self.candidate = logical
            self.since = now
            self.initialised = True
            return self.stable
        if logical == self.stable:
            self.candidate = logical
            self.since = now
            return self.stable
        if logical != self.candidate:
            self.candidate = logical
            self.since = now
            return self.stable
        if now - self.since >= window:
            self.stable = self.candidate
        return self.stable


class SensorChannel:
    """一个原始输入通道在传感量语境下的绑定。"""

    __slots__ = ("channel", "active_high", "pull", "debounce_s")

    def __init__(self, channel: str, active_high: bool, pull: PullMode, debounce_ms: float) -> None:
        self.channel = channel
        self.active_high = active_high
        self.pull = pull
        self.debounce_s = max(0.0, debounce_ms) / 1000.0


class SensorBank:
    """把一组 :class:`SensorSpec` 组合成一个可读的传感量集合。"""

    def __init__(self, specs: Mapping[str, SensorSpec]) -> None:
        self._specs: Dict[str, SensorSpec] = dict(specs)
        self._channels: Dict[str, SensorChannel] = {}
        for key, spec in self._specs.items():
            for channel in spec.all_channels:
                existing = self._channels.get(channel)
                binding = SensorChannel(
                    channel=channel,
                    active_high=spec.active_high,
                    pull=PullMode(spec.pull) if spec.pull in PullMode._value2member_map_ else PullMode.NONE,
                    debounce_ms=spec.debounce_ms,
                )
                if existing is not None and (
                    existing.active_high != binding.active_high
                    or existing.pull != binding.pull
                ):
                    raise ValueError(
                        f"传感量通道 {channel!r} 被多个传感量共用但极性/上拉不一致"
                        f"（涉及 {key!r}）"
                    )
                self._channels[channel] = binding
        self._debounce: Dict[str, _Debounce] = {
            ch: _Debounce() for ch, b in self._channels.items() if b.debounce_s > 0
        }
        self._last: Dict[str, bool] = {}

    # ------------------------------------------------------------------ 装配
    @property
    def channels(self) -> Mapping[str, SensorChannel]:
        """需要向 I/O 后端申请的所有原始输入通道。"""

        return dict(self._channels)

    def setup(self, io: DigitalIO) -> None:
        """在后端上把所有传感量通道配置为输入。"""

        for binding in self._channels.values():
            io.setup_input(binding.channel, pull=binding.pull)

    # ------------------------------------------------------------------ 读取
    def read(
        self, io: DigitalIO, now: float, external: Optional[Mapping[str, bool]] = None
    ) -> Mapping[str, bool]:
        """读取全部传感量。

        :param external: ``source="external"`` 的传感量取值来源
        """

        external = external or {}
        raw: Dict[str, bool] = {}
        for channel, binding in self._channels.items():
            level = io.read(channel)
            logical = level if binding.active_high else (not level)
            if binding.debounce_s > 0:
                logical = self._debounce[channel].update(logical, now, binding.debounce_s)
            raw[channel] = logical

        result: Dict[str, bool] = {}
        for key, spec in self._specs.items():
            result[key] = self._evaluate(key, spec, raw, external)
        self._last = result
        return result

    @staticmethod
    def _evaluate(
        key: str, spec: SensorSpec, raw: Mapping[str, bool], external: Mapping[str, bool]
    ) -> bool:
        if spec.source == "constant":
            return bool(spec.value)
        if spec.source == "external":
            return bool(external.get(key, spec.value))
        if spec.source == "input":
            if spec.channel is None:  # pragma: no cover - 配置校验已拦截
                return False
            return bool(raw.get(spec.channel, False))
        if spec.source == "keys":
            values = [bool(raw.get(ch, False)) for ch in spec.channels]
            if not values:
                return False
            return all(values) if spec.mode == "all" else any(values)
        raise ValueError(f"未知传感量来源: {spec.source!r}")  # pragma: no cover

    @property
    def last(self) -> Mapping[str, bool]:
        return dict(self._last)

    def describe(self) -> Dict[str, object]:
        return {
            key: {
                "source": spec.source,
                "channels": list(spec.all_channels),
                "mode": spec.mode if spec.source == "keys" else None,
                "value": spec.value if spec.source == "constant" else None,
            }
            for key, spec in self._specs.items()
        }
