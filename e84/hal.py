"""信号层（HAL）：逻辑信号 ↔ 物理通道。

这一层是「可配置性」的核心。它把四件事从协议逻辑里彻底剥离：

1. **映射**：逻辑信号（``VALID``/``CS_0``/… 以及现场自定义信号如 ``GO``）对应哪个物理通道；
2. **极性**：物理高电平算 ON 还是 OFF（现场光耦/硬线接线差异，逐信号可配）；
3. **上拉与去抖**：输入引脚上拉/下拉方向、去抖时间；
4. **安全态与互斥**：每个输出掉电/停控时应处的逻辑值；共用同一物理通道的
   信号（例如现场把 ``L_REQ``/``U_REQ`` 并在一条线上）必须显式声明且极性一致，
   冲突在加载配置时就会报错。（注：SEMI E84-0301 表 9 原文中每个信号各占一个引脚。）

另附 **写变化才写**（write-on-change）优化与写入计数，便于降低 GPIO/总线负载
并于事后核对输出行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, MutableMapping, Optional, Tuple, Union

from e84.io.base import DigitalIO, PullMode
from e84.model import Outputs
from e84.signals import Signal

__all__ = [
    "InputBinding",
    "OutputBinding",
    "SignalMap",
    "signal_name",
    "as_signal",
]

SignalKey = Union[Signal, str]


def signal_name(key: SignalKey) -> str:
    """把信号键规范化为大写字符串（``Signal`` 或任意名称）。"""

    if isinstance(key, Signal):
        return key.value
    return str(key).strip().upper().replace("-", "_")


def as_signal(key: SignalKey) -> Optional[Signal]:
    """尝试把键解析为 :class:`Signal`，失败返回 ``None``（现场自定义信号）。"""

    if isinstance(key, Signal):
        return key
    try:
        return Signal(signal_name(key))
    except ValueError:
        return None


@dataclass(frozen=True)
class InputBinding:
    """一个输入信号的物理绑定。"""

    channel: str
    active_high: bool = True
    pull: PullMode = PullMode.NONE
    debounce_ms: float = 0.0

    @property
    def debounce_s(self) -> float:
        return max(0.0, self.debounce_ms) / 1000.0


@dataclass(frozen=True)
class OutputBinding:
    """一个输出信号的物理绑定。

    :param active_high: 物理高电平是否代表逻辑 ON。
    :param safe_on: **安全态**下该输出应处的逻辑值。默认 ``False``（OFF）——
        对 ``ES``/``HO_AVBL`` 而言 OFF 即「请求停止 / 不可交接」，是失效安全方向。
    """

    channel: str
    active_high: bool = True
    safe_on: bool = False


@dataclass
class _Debounced:
    """单通道去抖状态。"""

    stable: bool = False
    candidate: bool = False
    since: float = 0.0
    initialised: bool = False


class SignalMap:
    """逻辑信号与物理通道之间的双向映射。"""

    def __init__(
        self,
        io: DigitalIO,
        inputs: Mapping[SignalKey, InputBinding],
        outputs: Mapping[SignalKey, OutputBinding],
    ) -> None:
        self._io = io
        self._inputs: Dict[str, InputBinding] = {
            signal_name(k): v for k, v in inputs.items()
        }
        self._outputs: Dict[str, OutputBinding] = {
            signal_name(k): v for k, v in outputs.items()
        }
        self._debounce: Dict[str, _Debounced] = {
            name: _Debounced() for name, b in self._inputs.items() if b.debounce_s > 0
        }
        self._logical_inputs: Dict[str, bool] = {}
        self._last_written: Dict[str, bool] = {}
        self._write_count = 0
        self._setup_done = False
        self._validate()

    # ---------------------------------------------------------------- 校验
    def _validate(self) -> None:
        """静态校验：极性冲突、通道复用、名称冲突。"""

        by_channel: MutableMapping[str, List[Tuple[str, bool]]] = {}
        for name, binding in self._outputs.items():
            by_channel.setdefault(binding.channel, []).append(
                (name, binding.active_high)
            )
        for channel, users in by_channel.items():
            polarities = {pol for _, pol in users}
            if len(polarities) > 1:
                names = ", ".join(n for n, _ in users)
                raise ValueError(
                    f"输出通道 {channel!r} 被多个信号共用但极性不一致（{names}）；"
                    "共用同一物理通道的信号必须使用相同的 active_high"
                )
        input_channels = {b.channel for b in self._inputs.values()}
        for name, binding in self._outputs.items():
            if binding.channel in input_channels:
                raise ValueError(
                    f"通道 {binding.channel!r} 同时被输出 {name!r} 与某个输入占用；"
                    "E84 的每个物理引脚方向唯一，请检查配置"
                )

    # ---------------------------------------------------------------- 装配
    def setup(self) -> None:
        """按配置初始化后端的所有通道，输出先落到安全态。"""

        for name, binding in self._inputs.items():
            self._io.setup_input(binding.channel, pull=binding.pull)
        for name, binding in self._outputs.items():
            self._io.setup_output(
                binding.channel, initial=self._level_for(binding, binding.safe_on)
            )
            self._last_written[binding.channel] = self._level_for(binding, binding.safe_on)
        self._setup_done = True
        # 输入初值先填一次，避免去抖在启动瞬间误判
        self.read(0.0)

    @property
    def io(self) -> DigitalIO:
        return self._io

    @property
    def input_names(self) -> Tuple[str, ...]:
        return tuple(self._inputs)

    @property
    def output_names(self) -> Tuple[str, ...]:
        return tuple(self._outputs)

    @property
    def input_channels(self) -> Tuple[str, ...]:
        """所有被输入信号占用的物理通道（去重，保持配置顺序）。"""

        seen: List[str] = []
        for binding in self._inputs.values():
            if binding.channel not in seen:
                seen.append(binding.channel)
        return tuple(seen)

    @property
    def output_channels(self) -> Tuple[str, ...]:
        """所有被输出信号占用的物理通道（去重，保持配置顺序）。"""

        seen: List[str] = []
        for binding in self._outputs.values():
            if binding.channel not in seen:
                seen.append(binding.channel)
        return tuple(seen)

    def channel_of(self, key: SignalKey) -> Optional[str]:
        """返回信号对应的物理通道名（输入或输出）。"""

        name = signal_name(key)
        if name in self._outputs:
            return self._outputs[name].channel
        if name in self._inputs:
            return self._inputs[name].channel
        return None

    def shared_channels(self) -> Mapping[str, Tuple[str, ...]]:
        """返回被多个信号共用的通道（例如引脚 1 的 ``L_REQ``/``U_REQ``）。"""

        by_channel: Dict[str, List[str]] = {}
        for name, binding in self._outputs.items():
            by_channel.setdefault(binding.channel, []).append(name)
        return {ch: tuple(names) for ch, names in by_channel.items() if len(names) > 1}

    # ------------------------------------------------------------ 读（输入）
    @staticmethod
    def _level_for(binding, logical: bool) -> bool:
        """逻辑值 -> 物理电平。"""

        return bool(logical) if binding.active_high else (not logical)

    def _logical_for(self, active_high: bool, level: bool) -> bool:
        """物理电平 -> 逻辑值。"""

        return bool(level) if active_high else (not level)

    def read(self, now: float) -> Mapping[str, bool]:
        """读取全部输入信号（含去抖），返回 ``{信号名: 逻辑值}``。"""

        result: Dict[str, bool] = {}
        for name, binding in self._inputs.items():
            level = self._io.read(binding.channel)
            logical = self._logical_for(binding.active_high, level)
            result[name] = self._debounce_value(name, logical, now, binding)
        self._logical_inputs = result
        return result

    def _debounce_value(
        self, name: str, logical: bool, now: float, binding: InputBinding
    ) -> bool:
        if binding.debounce_s <= 0:
            return logical
        state = self._debounce[name]
        if not state.initialised:
            state.stable = logical
            state.candidate = logical
            state.since = now
            state.initialised = True
            return logical
        if logical == state.stable:
            state.candidate = logical
            state.since = now
            return state.stable
        if logical != state.candidate:
            state.candidate = logical
            state.since = now
            return state.stable
        if now - state.since >= binding.debounce_s:
            state.stable = state.candidate
        return state.stable

    @property
    def logical_inputs(self) -> Mapping[str, bool]:
        """最近一次 :meth:`read` 的结果。"""

        return dict(self._logical_inputs)

    # ------------------------------------------------------------ 写（输出）
    def apply(self, outputs: Outputs) -> Mapping[str, bool]:
        """把 :class:`Outputs` 落到物理通道，返回 ``{通道: 电平}``（仅实际写入的）。"""

        desired = outputs.as_dict()
        per_channel: Dict[str, Tuple[bool, list]] = {}
        for name, binding in self._outputs.items():
            logical = bool(desired.get(name, binding.safe_on))
            channel = binding.channel
            if channel not in per_channel:
                per_channel[channel] = (binding.active_high, [])
            per_channel[channel][1].append((name, logical))

        written: Dict[str, bool] = {}
        for channel, (active_high, users) in per_channel.items():
            ons = [(n, v) for n, v in users if v]
            if len(ons) > 1:
                names = ", ".join(n for n, _ in ons)
                raise ValueError(
                    f"共用通道 {channel!r} 的信号被同时置 ON：{names}；"
                    "这属于协议逻辑错误（例如 L_REQ 与 U_REQ 同时有效）"
                )
            logical = ons[0][1] if ons else False
            level = logical if active_high else (not logical)
            if self._last_written.get(channel) != level:
                self._io.write(channel, level)
                self._last_written[channel] = level
                self._write_count += 1
                written[channel] = level
        return written

    def safe_state(self) -> Mapping[str, bool]:
        """把所有输出驱动到各自的安全态（``safe_on``）。"""

        written: Dict[str, bool] = {}
        for name, binding in self._outputs.items():
            level = self._level_for(binding, binding.safe_on)
            if self._last_written.get(binding.channel) != level:
                self._io.write(binding.channel, level)
                self._last_written[binding.channel] = level
                self._write_count += 1
                written[binding.channel] = level
        return written

    def force_output(self, key: SignalKey, logical: bool) -> bool:
        """强制某个输出的逻辑值（**仅供现场调试**，绕过协议逻辑）。

        返回实际写入的物理电平。若该通道与其它信号共用，只在本次调用内生效，
        下一次 :meth:`apply` 会按协议逻辑覆盖。
        """

        name = signal_name(key)
        binding = self._outputs.get(name)
        if binding is None:
            raise KeyError(f"未绑定的输出信号: {name!r}")
        level = self._level_for(binding, logical)
        self._io.write(binding.channel, level)
        self._last_written[binding.channel] = level
        self._write_count += 1
        return level

    @property
    def last_written(self) -> Mapping[str, bool]:
        """最近一次写入后各物理通道的电平。"""

        return dict(self._last_written)

    @property
    def write_count(self) -> int:
        """累计实际写入次数（写变化才写，可用于验证优化生效）。"""

        return self._write_count

    # ---------------------------------------------------------------- 自检
    def verify_outputs(self, outputs: Outputs) -> Mapping[str, Tuple[bool, Optional[bool]]]:
        """回读校验：返回 ``{通道: (期望电平, 实际电平)}``。

        后端不支持回读时实际值为 ``None``。用于 :command:`e84-selftest`。
        """

        desired = outputs.as_dict()
        result: Dict[str, Tuple[bool, Optional[bool]]] = {}
        for name, binding in self._outputs.items():
            logical = bool(desired.get(name, binding.safe_on))
            expected = self._level_for(binding, logical)
            actual = self._io.read_output(binding.channel)
            result[binding.channel] = (expected, actual)
        return result

    def describe(self) -> Dict[str, object]:
        """返回人类可读的映射摘要（用于日志与 `e84-validate --show-map`）。"""

        return {
            "inputs": {
                name: {
                    "channel": b.channel,
                    "active_high": b.active_high,
                    "pull": b.pull.value,
                    "debounce_ms": b.debounce_ms,
                }
                for name, b in self._inputs.items()
            },
            "outputs": {
                name: {
                    "channel": b.channel,
                    "active_high": b.active_high,
                    "safe_on": b.safe_on,
                }
                for name, b in self._outputs.items()
            },
            "shared_channels": {k: list(v) for k, v in self.shared_channels().items()},
        }
