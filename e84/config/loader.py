"""配置加载。

支持五种入口：

* :func:`load_dict` —— 直接给字典；
* :func:`load_json` / :func:`load_toml` / :func:`load_yaml` —— 给文本；
* :func:`load_file` —— 按扩展名自动选择（``.yaml/.yml``、``.json``、``.toml``）。

**仅依赖标准库**：YAML 需要 PyYAML，缺失时会给出明确提示并建议改用 JSON/TOML。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Union

from e84.config.model import EquipmentConfig
from e84.config.validate import validate_config
from e84.fault import ConfigError

__all__ = ["load_dict", "load_json", "load_toml", "load_yaml", "load_file", "loads"]


def load_dict(data: Mapping[str, Any], *, validate: bool = True) -> EquipmentConfig:
    """从字典构建配置。"""

    cfg = EquipmentConfig.parse(data)
    if validate:
        validate_config(cfg, raise_on_error=True)
    return cfg


def load_json(text: str, *, validate: bool = True) -> EquipmentConfig:
    """从 JSON 文本构建配置。"""

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON 解析失败: {exc}") from exc
    return load_dict(data, validate=validate)


def load_toml(text: str, *, validate: bool = True) -> EquipmentConfig:
    """从 TOML 文本构建配置（Python ≥3.11 用标准库 ``tomllib``，3.10 需要 ``tomli``）。"""

    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - 取决于 Python 版本
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise ConfigError(
                "当前 Python 没有 tomllib。请升级到 3.11+，或安装 tomli：pip install tomli"
            ) from exc
    try:
        data = tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001 - tomllib 的异常类型随版本而变
        raise ConfigError(f"TOML 解析失败: {exc}") from exc
    return load_dict(data, validate=validate)


def load_yaml(text: str, *, validate: bool = True) -> EquipmentConfig:
    """从 YAML 文本构建配置。"""

    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "未安装 PyYAML，无法解析 YAML 配置。三种选择：\n"
            "  1) pip install pyyaml\n"
            "  2) 把配置改写成 JSON（JSON 是 YAML 的子集，语义一致）\n"
            "  3) 直接在代码里用 dict 传给 load_dict()"
        ) from exc
    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - PyYAML 异常类型多样
        raise ConfigError(f"YAML 解析失败: {exc}") from exc
    if data is None:
        raise ConfigError("YAML 配置为空")
    return load_dict(data, validate=validate)


def loads(text: str, fmt: str, *, validate: bool = True) -> EquipmentConfig:
    """按显式格式加载。"""

    key = fmt.strip().lower().lstrip(".")
    if key in ("yaml", "yml"):
        return load_yaml(text, validate=validate)
    if key == "json":
        return load_json(text, validate=validate)
    if key == "toml":
        return load_toml(text, validate=validate)
    raise ConfigError(f"不支持的配置格式: {fmt!r}（可选: yaml / json / toml）")


def load_file(path: Union[str, os.PathLike], *, validate: bool = True) -> EquipmentConfig:
    """按扩展名加载配置文件。

    找不到文件或扩展名不认识时抛 :class:`e84.fault.ConfigError`。
    """

    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"配置文件不存在: {p}")
    suffix = p.suffix.lower().lstrip(".")
    if not suffix:
        raise ConfigError(f"无法从文件名判断配置格式（缺少扩展名）: {p}")
    text = p.read_text(encoding="utf-8")
    return loads(text, suffix, validate=validate)
