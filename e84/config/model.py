"""配置数据模型。

设计原则（对应「可配置性」需求）：

* **标准是默认值**：不写任何配置时，行为就是 SEMI E84-0301 规定的被动侧行为；
* **现场差异显式化**：引脚映射、极性、上拉、去抖、载具检测方式、访问模式来源、
  载具方向意图来源、定时器、故障策略全部是配置项；
* **旧实现可复现**：VOC 项目那类现场做法（板级 ``GO`` 前置条件、
  ``HO_AVBL``/``ES`` 由三键状态驱动等）可以**纯粹通过配置**表达，
  而不需要把非标准行为写进代码。

所有模型都是 ``frozen dataclass``，可安全地在多线程/多实例间共享。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from e84.fault import ConfigError
from e84.model import AccessMode, Op, PortRole, Scenario, Topology

__all__ = [
    "ChannelSpec",
    "SensorSpec",
    "AccessModeSpec",
    "IntentSpec",
    "IoDefaults",
    "LoadPortConfig",
    "PolicyConfig",
    "TraceConfig",
    "WatchdogConfig",
    "InterfaceConfig",
    "EquipmentConfig",
    "jsonable",
]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} 必须是映射（字典），实际为 {type(value).__name__}")
    return value


def _as_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "on", "yes", "1"):
            return True
        if low in ("false", "off", "no", "0"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ConfigError(f"{where} 需要布尔值，实际为 {value!r}")


def _enum(enum_cls, value: Any, where: str):
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ConfigError(f"{where} 取值非法: {value!r}（可选: {allowed}）") from exc


def jsonable(obj: Any) -> Any:
    """把（可能嵌套的）配置对象转换为可 JSON 序列化的结构。"""

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Mapping):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# 通道级配置
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChannelSpec:
    """一个逻辑信号到物理通道的绑定。"""

    channel: str
    active_high: bool = True
    pull: str = "none"          # "up" | "down" | "none"
    debounce_ms: float = 0.0
    safe_on: bool = False       # 仅输出有意义：安全态的逻辑值

    @classmethod
    def parse(cls, value: Any, where: str, defaults: "IoDefaults") -> "ChannelSpec":
        """支持 ``"IN1"`` 简写与完整字典两种写法。"""

        if isinstance(value, str):
            return cls(channel=value, active_high=defaults.active_high,
                       pull=defaults.pull, debounce_ms=defaults.debounce_ms,
                       safe_on=defaults.safe_on)
        data = _require_mapping(value, where)
        channel = data.get("channel") or data.get("pin") or data.get("gpio")
        if channel is None:
            raise ConfigError(f"{where} 缺少 'channel' 字段")
        return cls(
            channel=str(channel),
            active_high=_as_bool(data.get("active_high", defaults.active_high), f"{where}.active_high"),
            pull=str(data.get("pull", defaults.pull)).strip().lower(),
            debounce_ms=float(data.get("debounce_ms", defaults.debounce_ms)),
            safe_on=_as_bool(data.get("safe_on", defaults.safe_on), f"{where}.safe_on"),
        )


@dataclass(frozen=True)
class IoDefaults:
    """接口级 I/O 默认值，避免逐信号重复书写。"""

    active_high: bool = True
    pull: str = "none"
    debounce_ms: float = 0.0
    safe_on: bool = False

    @classmethod
    def parse(cls, value: Any, where: str = "io_defaults") -> "IoDefaults":
        if value is None:
            return cls()
        data = _require_mapping(value, where)
        return cls(
            active_high=_as_bool(data.get("active_high", True), f"{where}.active_high"),
            pull=str(data.get("pull", "none")).strip().lower(),
            debounce_ms=float(data.get("debounce_ms", 0.0)),
            safe_on=_as_bool(data.get("safe_on", False), f"{where}.safe_on"),
        )


@dataclass(frozen=True)
class SensorSpec:
    """本机传感量（载具存在/完整落位/就绪/门/夹持）的来源。

    ``source`` 取值：

    * ``"input"``    —— 单个 GPIO 输入通道；
    * ``"keys"``     —— 多个通道的逻辑组合（现场常见的三键/多传感器方案）：
      ``mode="any"`` 表示「检测到载具」，``mode="all"`` 表示「完整落位」；
    * ``"external"`` —— 由上层通过 API 设置（例如来自 E87/E30 或设备主控）；
    * ``"constant"`` —— 固定值（用于没有该机构的载口）。
    """

    source: str = "constant"
    channel: Optional[str] = None
    channels: Tuple[str, ...] = ()
    mode: str = "any"
    value: bool = False
    active_high: bool = True
    pull: str = "none"
    debounce_ms: float = 0.0

    @classmethod
    def parse(cls, value: Any, where: str, defaults: "IoDefaults") -> "SensorSpec":
        # 简写 1: None -> 常量 False
        if value is None:
            return cls(source="constant", value=False)
        # 简写 2: 布尔 -> 常量
        if isinstance(value, bool):
            return cls(source="constant", value=value)
        # 简写 3: 字符串 -> 单通道输入
        if isinstance(value, str):
            return cls(
                source="input", channel=value, active_high=defaults.active_high,
                pull=defaults.pull, debounce_ms=defaults.debounce_ms,
            )
        # 简写 4: 列表 -> 多通道 "any"
        if isinstance(value, (list, tuple)):
            return cls(
                source="keys", channels=tuple(str(c) for c in value), mode="any",
                active_high=defaults.active_high, pull=defaults.pull,
                debounce_ms=defaults.debounce_ms,
            )
        data = _require_mapping(value, where)
        source = str(data.get("source", "input")).strip().lower()
        if source not in ("input", "keys", "external", "constant"):
            raise ConfigError(
                f"{where}.source 取值非法: {source!r}"
                "（可选: input / keys / external / constant）"
            )
        channels = tuple(str(c) for c in data.get("channels", ()) or ())
        channel = data.get("channel")
        if source == "input" and channel is None:
            if len(channels) == 1:
                channel = channels[0]
            else:
                raise ConfigError(f"{where} 的 source=input 需要 'channel' 字段")
        if source == "keys" and not channels:
            raise ConfigError(f"{where} 的 source=keys 需要非空的 'channels' 列表")
        mode = str(data.get("mode", "any")).strip().lower()
        if mode not in ("any", "all"):
            raise ConfigError(f"{where}.mode 取值非法: {mode!r}（可选: any / all）")
        return cls(
            source=source,
            channel=str(channel) if channel is not None else None,
            channels=channels,
            mode=mode,
            value=_as_bool(data.get("value", False), f"{where}.value"),
            active_high=_as_bool(data.get("active_high", defaults.active_high), f"{where}.active_high"),
            pull=str(data.get("pull", defaults.pull)).strip().lower(),
            debounce_ms=float(data.get("debounce_ms", defaults.debounce_ms)),
        )

    @property
    def all_channels(self) -> Tuple[str, ...]:
        if self.source == "input" and self.channel:
            return (self.channel,)
        if self.source == "keys":
            return tuple(self.channels)
        return ()


@dataclass(frozen=True)
class AccessModeSpec:
    """访问模式（自动/手动）的来源（§5.1、§5.14）。"""

    source: str = "static"      # static | api | input
    value: AccessMode = AccessMode.AUTOMATIC
    channel: Optional[str] = None
    active_high: bool = True
    #: ``source="input"`` 时，逻辑值 True 代表手动模式。
    manual_when_on: bool = True

    @classmethod
    def parse(cls, value: Any, where: str, defaults: "IoDefaults") -> "AccessModeSpec":
        if value is None:
            return cls()
        if isinstance(value, AccessMode):
            return cls(source="static", value=value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("automatic", "auto", "manual"):
                mode = AccessMode.MANUAL if low == "manual" else AccessMode.AUTOMATIC
                return cls(source="static", value=mode)
            # 其它字符串视为输入通道
            return cls(source="input", channel=value, active_high=defaults.active_high)
        data = _require_mapping(value, where)
        source = str(data.get("source", "static")).strip().lower()
        if source not in ("static", "api", "input"):
            raise ConfigError(f"{where}.source 取值非法: {source!r}（可选: static / api / input）")
        raw_value = data.get("value", "automatic")
        mode = _enum(AccessMode, raw_value, f"{where}.value") if not isinstance(raw_value, AccessMode) else raw_value
        channel = data.get("channel")
        if source == "input" and channel is None:
            raise ConfigError(f"{where} 的 source=input 需要 'channel' 字段")
        return cls(
            source=source,
            value=mode,
            channel=str(channel) if channel is not None else None,
            active_high=_as_bool(data.get("active_high", defaults.active_high), f"{where}.active_high"),
            manual_when_on=_as_bool(data.get("manual_when_on", True), f"{where}.manual_when_on"),
        )


@dataclass(frozen=True)
class IntentSpec:
    """交接方向（装/卸）的意图来源。

    ``derived``：由载具传感器推导（空位→装载，完整落位→卸载）；
    ``api``：由上层显式指定（例如来自 E87/E30 的搬运任务）；
    ``static``：固定方向（调试用）。
    """

    source: str = "derived"
    value: Op = Op.UNKNOWN

    @classmethod
    def parse(cls, value: Any, where: str) -> "IntentSpec":
        if value is None:
            return cls()
        if isinstance(value, Op):
            return cls(source="static", value=value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("derived", "sensor", "auto"):
                return cls(source="derived")
            if low in ("api", "host", "external"):
                return cls(source="api")
            return cls(source="static", value=_enum(Op, low, where))
        data = _require_mapping(value, where)
        source = str(data.get("source", "derived")).strip().lower()
        if source not in ("derived", "api", "static"):
            raise ConfigError(f"{where}.source 取值非法: {source!r}（可选: derived / api / static）")
        return cls(source=source, value=_enum(Op, data.get("value", "unknown"), f"{where}.value"))


# --------------------------------------------------------------------------- #
# 载口
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoadPortConfig:
    """一个载口的配置。"""

    id: str
    role: PortRole = PortRole.SINGLE
    #: 「检测到载具」（任意位置）——用于判断卸载是否可以开始。
    carrier_present: SensorSpec = field(default_factory=SensorSpec)
    #: 「载具位于正确位置」——§6.1 表 1 判定 ``L_REQ``/``U_REQ`` 落下的依据。
    carrier_in_position: SensorSpec = field(default_factory=SensorSpec)
    #: 机构就绪（例如上料机器人空闲、门已开到位）。
    ready_for_transfer: SensorSpec = field(default_factory=lambda: SensorSpec(source="constant", value=True))
    door_open: SensorSpec = field(default_factory=lambda: SensorSpec(source="constant", value=True))
    clamp_released: SensorSpec = field(default_factory=lambda: SensorSpec(source="constant", value=True))
    #: 该载口是否被启用（false 时恒不可用，等价于持续拉低 HO_AVBL）。
    enabled: bool = True
    access_mode: AccessModeSpec = field(default_factory=AccessModeSpec)
    operation_intent: IntentSpec = field(default_factory=IntentSpec)

    @classmethod
    def parse(cls, value: Any, where: str, defaults: "IoDefaults") -> "LoadPortConfig":
        if isinstance(value, str):
            return cls(id=value)
        data = _require_mapping(value, where)
        port_id = data.get("id") or data.get("name")
        if not port_id:
            raise ConfigError(f"{where} 缺少 'id' 字段")
        role = _enum(PortRole, data.get("role", "single"), f"{where}.role")
        return cls(
            id=str(port_id),
            role=role,
            carrier_present=SensorSpec.parse(data.get("carrier_present"), f"{where}.carrier_present", defaults),
            carrier_in_position=SensorSpec.parse(data.get("carrier_in_position"), f"{where}.carrier_in_position", defaults),
            ready_for_transfer=SensorSpec.parse(
                data.get("ready_for_transfer", {"source": "constant", "value": True}),
                f"{where}.ready_for_transfer", defaults),
            door_open=SensorSpec.parse(
                data.get("door_open", {"source": "constant", "value": True}),
                f"{where}.door_open", defaults),
            clamp_released=SensorSpec.parse(
                data.get("clamp_released", {"source": "constant", "value": True}),
                f"{where}.clamp_released", defaults),
            enabled=_as_bool(data.get("enabled", True), f"{where}.enabled"),
            access_mode=AccessModeSpec.parse(data.get("access_mode"), f"{where}.access_mode", defaults),
            operation_intent=IntentSpec.parse(data.get("operation_intent"), f"{where}.operation_intent"),
        )

    @property
    def sensor_specs(self) -> Mapping[str, SensorSpec]:
        return {
            "carrier_present": self.carrier_present,
            "carrier_in_position": self.carrier_in_position,
            "ready_for_transfer": self.ready_for_transfer,
            "door_open": self.door_open,
            "clamp_released": self.clamp_released,
        }


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyConfig:
    """时序之外的行为策略。默认值全部取「标准 + 保守安全」。"""

    #: `CS_0/CS_1` 组合非法时的处理：``fault`` | ``ignore``（忽略本次握手）
    invalid_cs: str = "fault"
    #: 上层意图与传感器矛盾时：``fault`` | ``follow_sensor``
    intent_mismatch: str = "fault"
    #: 拓扑不支持同时交接但对方要求时：``fault`` | ``ho_abort``
    unsupported_simultaneous: str = "fault"
    #: 连续交接中场等待下一次 VALID 超时（TP6）后：``idle`` | ``fault``
    tp6_timeout: str = "idle"
    #: 互锁超时的处理：``fault`` | ``ho_abort``
    on_timeout: str = "fault"
    #: 故障时是否按 R1-1.1.2.1「其余信号保持在出错时刻状态」钉住请求线
    request_hold_on_fault: bool = True
    #: 故障锁存期间是否持续拉低 ES（请求停止）
    es_latched_on_fault: bool = True
    #: 手动访问模式时是否拉低 HO_AVBL
    ho_avbl_when_manual: bool = False
    #: HO_AVBL 的判定范围：``all_ports``（任一本 PI/O 的载口异常即拉低，贴合 §6.1 表 1
    #: 「其他载口检测到异常时也可能保持 OFF」）或 ``selected_ports``（只看本次选中的载口）
    ho_avbl_scope: str = "all_ports"
    #: 是否要求 COMPT 已落下才算握手闭合（注 5 允许任意顺序，默认不要求）
    require_compt_off: bool = False
    #: 选口后若载口迟迟不就绪，多长时间判定「不可交接」并拉低 HO_AVBL（秒）。
    #: 默认 1.5 s —— **必须小于对方 ``TA1`` 的典型值 2 s**（表 5），否则被动方
    #: 还没来得及用 ``HO_AVBL`` 表达"不可交接"，对方就已经超时了。
    not_ready_timeout_s: float = 1.5
    #: BUSY 上升沿后延迟多久采样 CONT（容忍主动侧两根线的微小偏斜，毫秒）
    cont_sample_delay_ms: float = 10.0
    #: 已闭合的握手中 VALID 仍长时间为 ON 的判定超时（秒）
    valid_stuck_timeout_s: float = 5.0
    #: BUSY 在请求线落下之前就变 OFF：``fault`` | ``tolerate``
    busy_before_req_off: str = "fault"
    #: 收到 COMPT 时 BUSY/TR_REQ 仍为 ON（违反 §6.2.2.1 第8/9/10 步顺序）：
    #: ``fault`` | ``warn``（注 4 允许在 COMPT ON 之后校验这两个信号）
    compt_before_busy_off: str = "fault"
    #: 握手前置条件（如现场 `GO`）丢失时是否立即中止并回 IDLE
    abort_on_precondition_loss: bool = True
    #: BUSY=ON 期间是否通过事件通知上层冻结干涉区机构
    interlock_freeze: bool = True
    #: 单载口拓扑下是否要求 CS_1 恒为 OFF（表 3）
    single_lp_require_cs1_off: bool = True

    @classmethod
    def parse(cls, value: Any, where: str = "policy") -> "PolicyConfig":
        if value is None:
            return cls()
        data = _require_mapping(value, where)
        known = {f.name: f for f in dataclasses.fields(cls)}
        unknown = set(data) - set(known)
        if unknown:
            raise ConfigError(f"{where} 含未知字段: {', '.join(sorted(unknown))}")
        kwargs: Dict[str, Any] = {}
        for key, raw in data.items():
            field_def = known[key]
            if field_def.type in ("bool", bool) or isinstance(raw, bool):
                kwargs[key] = _as_bool(raw, f"{where}.{key}")
            elif key in ("not_ready_timeout_s", "cont_sample_delay_ms", "valid_stuck_timeout_s"):
                kwargs[key] = float(raw)
            else:
                kwargs[key] = str(raw).strip().lower()
        policy = cls(**kwargs)
        for name in ("invalid_cs", "intent_mismatch", "unsupported_simultaneous", "on_timeout"):
            allowed = {"invalid_cs": {"fault", "ignore"},
                       "intent_mismatch": {"fault", "follow_sensor"},
                       "unsupported_simultaneous": {"fault", "ho_abort"},
                       "on_timeout": {"fault", "ho_abort"}}[name]
            if getattr(policy, name) not in allowed:
                raise ConfigError(f"{where}.{name} 取值非法: {getattr(policy, name)!r}（可选: {', '.join(sorted(allowed))}）")
        if policy.tp6_timeout not in ("idle", "fault"):
            raise ConfigError(f"{where}.tp6_timeout 取值非法: {policy.tp6_timeout!r}（可选: idle / fault）")
        if policy.busy_before_req_off not in ("fault", "tolerate"):
            raise ConfigError(f"{where}.busy_before_req_off 取值非法: {policy.busy_before_req_off!r}（可选: fault / tolerate）")
        if policy.compt_before_busy_off not in ("fault", "warn"):
            raise ConfigError(f"{where}.compt_before_busy_off 取值非法: {policy.compt_before_busy_off!r}（可选: fault / warn）")
        if policy.ho_avbl_scope not in ("all_ports", "selected_ports"):
            raise ConfigError(f"{where}.ho_avbl_scope 取值非法: {policy.ho_avbl_scope!r}（可选: all_ports / selected_ports）")
        return policy


@dataclass(frozen=True)
class TraceConfig:
    """信号跳变追踪配置（现场排故 / 离线复现）。"""

    enabled: bool = False
    path: str = "e84_trace.csv"
    fmt: str = "csv"            # csv | jsonl
    flush_every: int = 1
    include_io: bool = True

    @classmethod
    def parse(cls, value: Any, where: str = "trace") -> "TraceConfig":
        if value is None:
            return cls()
        if isinstance(value, bool):
            return cls(enabled=value)
        if isinstance(value, str):
            return cls(enabled=True, path=value)
        data = _require_mapping(value, where)
        fmt = str(data.get("format", data.get("fmt", "csv"))).strip().lower()
        if fmt not in ("csv", "jsonl"):
            raise ConfigError(f"{where}.format 取值非法: {fmt!r}（可选: csv / jsonl）")
        return cls(
            enabled=_as_bool(data.get("enabled", True), f"{where}.enabled"),
            path=str(data.get("path", "e84_trace.csv")),
            fmt=fmt,
            flush_every=int(data.get("flush_every", 1)),
            include_io=_as_bool(data.get("include_io", True), f"{where}.include_io"),
        )


@dataclass(frozen=True)
class WatchdogConfig:
    """轮询卡死看门狗（可选）。"""

    enabled: bool = False
    channel: Optional[str] = None
    period_ms: float = 100.0
    #: 该看门狗通道挂在哪个 PI/O 的后端上（不写则用第一个接口）
    interface: Optional[str] = None
    #: 超过多少个轮询周期没有推进就判定卡死（供 Runner 报警，单位：个轮询周期）
    stall_polls: int = 20

    @classmethod
    def parse(cls, value: Any, where: str = "watchdog") -> Optional["WatchdogConfig"]:
        if value is None:
            return None
        if isinstance(value, bool):
            return cls(enabled=value)
        if isinstance(value, str):
            return cls(enabled=True, channel=value)
        data = _require_mapping(value, where)
        return cls(
            enabled=_as_bool(data.get("enabled", True), f"{where}.enabled"),
            channel=data.get("channel") or data.get("gpio"),
            period_ms=float(data.get("period_ms", 100.0)),
            interface=data.get("interface"),
            stall_polls=int(data.get("stall_polls", 20)),
        )


# --------------------------------------------------------------------------- #
# 接口与设备
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InterfaceConfig:
    """一个并行 I/O 接口的完整配置。"""

    id: str
    scenario: Scenario = Scenario.STANDARD
    topology: Topology = Topology.TWO_LP
    load_ports: Tuple[LoadPortConfig, ...] = ()
    inputs: Mapping[str, ChannelSpec] = field(default_factory=dict)
    outputs: Mapping[str, ChannelSpec] = field(default_factory=dict)
    timers: Mapping[str, float] = field(default_factory=dict)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    io_defaults: IoDefaults = field(default_factory=IoDefaults)
    #: 握手前置条件（信号名）。标准默认只有 ``VALID``；现场可加 ``GO`` 等。
    preconditions: Tuple[str, ...] = ("VALID",)
    enable_simultaneous: bool = True
    enable_continuous: bool = True
    #: 后端覆盖（不写则用设备级默认）
    backend: Optional[str] = None
    backend_options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, value: Any, where: str = "interface") -> "InterfaceConfig":
        data = _require_mapping(value, where)
        interface_id = data.get("id") or data.get("name")
        if not interface_id:
            raise ConfigError(f"{where} 缺少 'id' 字段")
        defaults = IoDefaults.parse(data.get("io_defaults"), f"{where}.io_defaults")

        raw_ports = data.get("load_ports") or data.get("ports") or []
        if isinstance(raw_ports, Mapping):
            raw_ports = [dict(v, id=k) if isinstance(v, Mapping) else v for k, v in raw_ports.items()]
        ports = tuple(
            LoadPortConfig.parse(p, f"{where}.load_ports[{i}]", defaults)
            for i, p in enumerate(raw_ports)
        )

        raw_inputs = _require_mapping(data.get("inputs", {}), f"{where}.inputs")
        inputs = {
            str(name).strip().upper(): ChannelSpec.parse(spec, f"{where}.inputs.{name}", defaults)
            for name, spec in raw_inputs.items()
        }
        raw_outputs = _require_mapping(data.get("outputs", {}), f"{where}.outputs")
        outputs = {
            str(name).strip().upper(): ChannelSpec.parse(spec, f"{where}.outputs.{name}", defaults)
            for name, spec in raw_outputs.items()
        }

        raw_timers = _require_mapping(data.get("timers", {}), f"{where}.timers")
        timers = {
            str(name).strip().upper(): float(val)
            for name, val in raw_timers.items()
        }

        raw_pre = data.get("preconditions", ["VALID"])
        if isinstance(raw_pre, str):
            raw_pre = [raw_pre]
        preconditions = tuple(str(p).strip().upper() for p in raw_pre)

        features = data.get("features") or {}
        if features and not isinstance(features, Mapping):
            raise ConfigError(f"{where}.features 必须是映射")

        return cls(
            id=str(interface_id),
            scenario=_enum(Scenario, data.get("scenario", "standard"), f"{where}.scenario"),
            topology=_enum(Topology, data.get("topology", "two_load_ports"), f"{where}.topology"),
            load_ports=ports,
            inputs=inputs,
            outputs=outputs,
            timers=timers,
            policy=PolicyConfig.parse(data.get("policy"), f"{where}.policy"),
            io_defaults=defaults,
            preconditions=preconditions,
            enable_simultaneous=_as_bool(
                (features or {}).get("simultaneous", data.get("simultaneous", True)),
                f"{where}.features.simultaneous"),
            enable_continuous=_as_bool(
                (features or {}).get("continuous", data.get("continuous", True)),
                f"{where}.features.continuous"),
            backend=data.get("backend"),
            backend_options=dict(_require_mapping(data.get("backend_options", {}), f"{where}.backend_options")),
        )

    def port(self, port_id: str) -> Optional[LoadPortConfig]:
        for p in self.load_ports:
            if p.id == port_id:
                return p
        return None

    def port_by_role(self, role: PortRole) -> Optional[LoadPortConfig]:
        for p in self.load_ports:
            if p.role is role:
                return p
        return None

    def timer(self, name: str, default: float) -> float:
        return float(self.timers.get(name.strip().upper(), default))


@dataclass(frozen=True)
class EquipmentConfig:
    """整台设备（可含多个 PI/O）的配置。"""

    schema_version: int = 1
    name: str = "e84-equipment"
    interfaces: Tuple[InterfaceConfig, ...] = ()
    backend: str = "memory"
    backend_options: Mapping[str, Any] = field(default_factory=dict)
    poll_interval_ms: float = 5.0
    boot_safe_hold_ms: float = 200.0
    log_level: str = "INFO"
    log_json: bool = False
    watchdog: Optional[WatchdogConfig] = None
    trace: TraceConfig = field(default_factory=TraceConfig)

    @classmethod
    def parse(cls, value: Any) -> "EquipmentConfig":
        data = _require_mapping(value, "config 根")
        raw_interfaces = data.get("interfaces")
        if raw_interfaces is None:
            # 允许只写一个接口（无 interfaces 列表）
            if "inputs" in data or "load_ports" in data:
                raw_interfaces = [data]
            else:
                raise ConfigError("配置缺少 'interfaces' 列表")
        if not isinstance(raw_interfaces, Sequence) or isinstance(raw_interfaces, (str, bytes)):
            raise ConfigError("'interfaces' 必须是列表")
        interfaces = tuple(
            InterfaceConfig.parse(item, f"interfaces[{i}]")
            for i, item in enumerate(raw_interfaces)
        )
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            name=str(data.get("name", "e84-equipment")),
            interfaces=interfaces,
            backend=str(data.get("backend", "memory")),
            backend_options=dict(_require_mapping(data.get("backend_options", {}), "backend_options")),
            poll_interval_ms=float(data.get("poll_interval_ms", 5.0)),
            boot_safe_hold_ms=float(data.get("boot_safe_hold_ms", 200.0)),
            log_level=str(data.get("log_level", "INFO")).upper(),
            log_json=_as_bool(data.get("log_json", False), "log_json"),
            watchdog=WatchdogConfig.parse(data.get("watchdog")),
            trace=TraceConfig.parse(data.get("trace")),
        )

    def interface(self, interface_id: str) -> Optional[InterfaceConfig]:
        for i in self.interfaces:
            if i.id == interface_id:
                return i
        return None

    def to_dict(self) -> Mapping[str, Any]:
        """返回可 JSON/YAML 序列化的字典（用于 ``e84-validate --dump``）。"""

        return jsonable(self)
