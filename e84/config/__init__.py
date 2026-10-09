"""配置子包：数据模型、加载器与交叉校验。"""

from __future__ import annotations

from e84.config.loader import (
    load_dict,
    load_file,
    load_json,
    load_toml,
    load_yaml,
    loads,
)
from e84.config.model import (
    AccessModeSpec,
    ChannelSpec,
    EquipmentConfig,
    IntentSpec,
    InterfaceConfig,
    IoDefaults,
    LoadPortConfig,
    PolicyConfig,
    SensorSpec,
    TraceConfig,
    WatchdogConfig,
    jsonable,
)
from e84.config.validate import ValidationReport, validate_config

__all__ = [
    "AccessModeSpec",
    "ChannelSpec",
    "EquipmentConfig",
    "IntentSpec",
    "InterfaceConfig",
    "IoDefaults",
    "LoadPortConfig",
    "PolicyConfig",
    "SensorSpec",
    "TraceConfig",
    "WatchdogConfig",
    "jsonable",
    "load_dict",
    "load_file",
    "load_json",
    "load_toml",
    "load_yaml",
    "loads",
    "validate_config",
    "ValidationReport",
]
