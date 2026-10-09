"""载口运行时模型。

把一个 :class:`e84.config.model.LoadPortConfig` 加上「上层通过 API 注入的状态」
（访问模式、可用性、交接意图、外部传感量）合成为协议核心需要的
:class:`e84.model.PortSnapshot`。

优先级规则：**API 覆盖 > 配置来源**。这样现场既可以完全由配置与 GPIO 决定，
也可以随时由上层（E87/E30 主机或设备主控）接管。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from e84.config.model import LoadPortConfig
from e84.model import AccessMode, Op, PortSnapshot, PortRole

__all__ = ["LoadPort", "build_ports"]


def _sensor_key(port_id: str, name: str) -> str:
    return f"{port_id}.{name}"


def _normalise_enum_text(value: object, aliases: Mapping[str, str]) -> str:
    """把枚举或字符串统一成小写文本，并接受少量常用别名。

    现场代码里经常直接写字符串（``"manual"``、``"load"``）。如果不做归一化，
    字符串会被原样存下来，之后与枚举比较时永远不相等——表现为"我设置了但没生效"，
    属于最难查的一类静默失败。
    """

    text = str(getattr(value, "value", value)).strip().lower()
    return aliases.get(text, text)


class LoadPort:
    """单个载口的运行时状态。"""

    __slots__ = ("cfg", "_api_access_mode", "_api_available", "_api_intent", "_external")

    def __init__(self, cfg: LoadPortConfig) -> None:
        self.cfg = cfg
        self._api_access_mode: Optional[AccessMode] = None
        self._api_available: Optional[bool] = None
        self._api_intent: Optional[Op] = None
        self._external: Dict[str, bool] = {}

    # ------------------------------------------------------------------ 属性
    @property
    def id(self) -> str:
        return self.cfg.id

    @property
    def role(self) -> PortRole:
        return self.cfg.role

    @property
    def sensor_keys(self) -> Tuple[str, ...]:
        """该载口拥有的传感量键名（用于向 :class:`e84.sensors.SensorBank` 注册）。"""

        return tuple(
            _sensor_key(self.cfg.id, name) for name in self.cfg.sensor_specs
        )

    def sensor_key_map(self) -> Mapping[str, str]:
        """``{传感量名: 传感量键名}``。"""

        return {name: _sensor_key(self.cfg.id, name) for name in self.cfg.sensor_specs}

    def access_mode_sensor_key(self) -> str:
        return _sensor_key(self.cfg.id, "access_mode")

    # ------------------------------------------------------------ API 覆盖
    def set_access_mode(self, mode: Optional[AccessMode]) -> None:
        """设置/清除访问模式覆盖（``None`` 表示恢复配置来源）。

        接受 :class:`~e84.model.AccessMode` 或字符串 ``"manual"``/``"automatic"``
        （现场代码里直接写字符串很常见；若不转换，字符串会存进去却永远匹配不上
        ``is AccessMode.MANUAL``，表现为"手动模式没生效"这种很难查的静默失败）。
        """

        self._api_access_mode = None if mode is None else AccessMode(
            _normalise_enum_text(mode, {"auto": "automatic"})
        )

    def set_available(self, available: Optional[bool]) -> None:
        """设置/清除可用性覆盖（``None`` 表示恢复配置值）。"""

        self._api_available = available

    def set_operation_intent(self, op: Optional[Op]) -> None:
        """设置/清除交接意图覆盖（``None`` 表示恢复配置来源）。

        同样接受 :class:`~e84.model.Op` 或字符串 ``"load"``/``"unload"``。
        """

        self._api_intent = None if op is None else Op(
            _normalise_enum_text(op, {"loading": "load", "unloading": "unload"})
        )

    def set_external(self, sensor_name: str, value: bool) -> None:
        """注入 ``source="external"`` 传感量的取值。"""

        self._external[_sensor_key(self.cfg.id, sensor_name)] = bool(value)

    def external_values(self) -> Mapping[str, bool]:
        return dict(self._external)

    @property
    def external(self) -> Mapping[str, bool]:
        return dict(self._external)

    # ---------------------------------------------------------------- 快照
    def snapshot(self, sensors: Mapping[str, bool], external: Mapping[str, bool]) -> PortSnapshot:
        """合成当前快照。

        :param sensors: :meth:`e84.sensors.SensorBank.read` 的结果
        :param external: 全局外部传感量取值（含本载口注入值）
        """

        keys = self.sensor_key_map()

        def value(name: str, default: bool) -> bool:
            key = keys[name]
            spec = self.cfg.sensor_specs[name]
            if spec.source == "external":
                if key in external:
                    return bool(external[key])
                if key in self._external:
                    return bool(self._external[key])
                return bool(spec.value)
            return bool(sensors.get(key, default))

        access_mode = self._resolve_access_mode(sensors)
        expected_op = self._resolve_intent()
        available = self.cfg.enabled and (
            self._api_available if self._api_available is not None else True
        )

        return PortSnapshot(
            port_id=self.cfg.id,
            role=self.cfg.role,
            carrier_present=value("carrier_present", False),
            carrier_in_position=value("carrier_in_position", False),
            door_open=value("door_open", True),
            clamp_released=value("clamp_released", True),
            ready_for_transfer=value("ready_for_transfer", True),
            available=available,
            access_mode=access_mode,
            expected_op=expected_op,
        )

    def _resolve_access_mode(self, sensors: Mapping[str, bool]) -> AccessMode:
        if self._api_access_mode is not None:
            return self._api_access_mode
        spec = self.cfg.access_mode
        if spec.source == "static":
            return spec.value
        if spec.source == "api":
            return spec.value
        # source == "input"
        logical = bool(sensors.get(self.access_mode_sensor_key(), False))
        manual = logical if spec.manual_when_on else (not logical)
        return AccessMode.MANUAL if manual else AccessMode.AUTOMATIC

    def _resolve_intent(self) -> Op:
        if self._api_intent is not None:
            return self._api_intent
        spec = self.cfg.operation_intent
        if spec.source == "static":
            return spec.value
        # "derived" 与 "api"（未注入时）都交给 FSM 依据传感器推导
        return Op.UNKNOWN

    # ---------------------------------------------------------------- 描述
    def describe(self) -> Dict[str, object]:
        return {
            "id": self.cfg.id,
            "role": self.cfg.role.value,
            "enabled": self.cfg.enabled,
            "access_mode": {
                "source": self.cfg.access_mode.source,
                "value": self.cfg.access_mode.value.value,
                "api_override": self._api_access_mode.value if self._api_access_mode else None,
            },
            "sensors": {
                name: spec.source for name, spec in self.cfg.sensor_specs.items()
            },
        }


def build_ports(configs) -> List[LoadPort]:
    """按配置顺序构建载口运行时对象。"""

    return [LoadPort(cfg) for cfg in configs]
