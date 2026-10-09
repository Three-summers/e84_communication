"""E84 被动端库的命令行工具。

入口统一为::

    python -m e84.cli <子命令> [选项]

子命令：

============================  ==================================================
:command:`validate <config>`   加载 + 交叉校验配置；``--show-map`` / ``--dump`` / ``--json``
:command:`monitor <config>`    周期性打印每个 PI/O 的状态/输出/定时器/载口/故障
:command:`selftest [config]`   不接硬件自检：配置、HAL、四个闭环、定时器范围
:command:`sim <config>`        用虚拟线束跑主动↔被动闭环并打印逐拍信号追踪
:command:`force <config>`      现场接线调试：把某个输出引脚强制到指定电平
:command:`faults <config>`     打印各接口当前故障、条款号与故障态期望信号画像
============================  ==================================================

退出码约定（见 :data:`EXIT_OK` / :data:`EXIT_CONFIG` / :data:`EXIT_RUNTIME`）：

* ``0`` —— 成功；
* ``1`` —— 配置或参数错误；
* ``2`` —— 运行期失败（自检不通过、握手未闭合、安全联锁拒绝等）。

安全约定：所有会碰硬件的子命令都在 ``try/finally`` 中保证 ``stop()`` 与
``safe_state()``；真实后端（非 ``null``/``memory``）必须显式确认，避免误动设备。
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from e84.active import ActivePhase, TransferJob
from e84.clock import RealClock, VirtualClock
from e84.config import (
    EquipmentConfig,
    InterfaceConfig,
    TraceConfig,
    ValidationReport,
    load_dict,
    load_file,
)
from e84.config.validate import validate_config
from e84.controller import PiOController
from e84.equipment import Equipment
from e84.fault import FAULT_CLAUSE, ConfigError, Fault, FaultCode
from e84.hal import signal_name
from e84.io.base import NullIO
from e84.io.sim import SimIO
from e84.model import Op, Outputs, PortRole, State
from e84.runner import ManualRunner
from e84.signals import Signal, as_signal_name, db25_pins, direction_of
from e84.testing import VirtualRig
from e84.timers import TIMER_SPECS, TimerId
from e84.trace import TraceRecorder
from e84.version import __standard__, __version__

__all__ = ["main", "build_parser", "EXIT_OK", "EXIT_CONFIG", "EXIT_RUNTIME"]

log = logging.getLogger("e84.cli")

#: 成功。
EXIT_OK = 0
#: 配置或参数错误。
EXIT_CONFIG = 1
#: 运行期失败。
EXIT_RUNTIME = 2

#: 不接触真实硬件的后端名（无需 ``--i-know-what-i-am-doing``）。
#: 其中 ``sim`` 是纯内存仿真后端，同样不会碰任何硬件。
_SAFE_BACKENDS = frozenset({"null", "memory", "inmemory", "sim"})

#: 自检在未提供配置时使用的最小单载口配置。
_MINIMAL_CONFIG: Mapping[str, Any] = {
    "schema_version": 1,
    "name": "e84-selftest-min",
    "backend": "sim",
    "poll_interval_ms": 5.0,
    "boot_safe_hold_ms": 0.0,
    "interfaces": [
        {
            "id": "PIO1",
            "scenario": "standard",
            "topology": "two_load_ports",
            "features": {"simultaneous": True, "continuous": True},
            "load_ports": [
                {
                    "id": "LP1",
                    "role": "left",
                    "carrier_present": {
                        "source": "keys",
                        "channels": ["LP1_K0", "LP1_K1"],
                        "mode": "any",
                    },
                    "carrier_in_position": {
                        "source": "keys",
                        "channels": ["LP1_K0", "LP1_K1"],
                        "mode": "all",
                    },
                    "access_mode": {"source": "static", "value": "automatic"},
                    "operation_intent": {"source": "derived"},
                },
                {
                    "id": "LP2",
                    "role": "right",
                    "carrier_present": {
                        "source": "keys",
                        "channels": ["LP2_K0", "LP2_K1"],
                        "mode": "any",
                    },
                    "carrier_in_position": {
                        "source": "keys",
                        "channels": ["LP2_K0", "LP2_K1"],
                        "mode": "all",
                    },
                    "access_mode": {"source": "static", "value": "automatic"},
                    "operation_intent": {"source": "derived"},
                },
            ],
            "outputs": {
                "L_REQ": {"channel": "OUT1"},
                "U_REQ": {"channel": "OUT1"},
                "READY": {"channel": "OUT4"},
                "HO_AVBL": {"channel": "OUT7"},
                "ES": {"channel": "OUT8"},
            },
            "inputs": {
                "VALID": {"channel": "IN1"},
                "CS_0": {"channel": "IN2"},
                "CS_1": {"channel": "IN3"},
                "TR_REQ": {"channel": "IN5"},
                "BUSY": {"channel": "IN6"},
                "COMPT": {"channel": "IN7"},
                "CONT": {"channel": "IN8"},
            },
            "timers": {"TP1": 2, "TP2": 2, "TP3": 60, "TP4": 60, "TP5": 2, "TP6": 2},
        }
    ],
}


# --------------------------------------------------------------------------- #
# argparse 基础设施
# --------------------------------------------------------------------------- #
class _ArgumentParser(argparse.ArgumentParser):
    """把 argparse 的参数错误退出码从 2 改成 :data:`EXIT_CONFIG`（1）。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: 参数错误: {message}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)


def build_parser() -> argparse.ArgumentParser:
    """构建完整的命令行解析器。"""

    parser = _ArgumentParser(
        prog="python -m e84.cli",
        description=(
            "SEMI E84-0301 被动端（装备端）并行 I/O 接口工具。\n"
            f"实现规范: {__standard__}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"e84 {__version__}\n规范: {__standard__}",
        help="打印库版本与所实现的规范号",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="提高日志详细度（-v=INFO，-vv=DEBUG）",
    )
    sub = parser.add_subparsers(dest="command", metavar="<子命令>", parser_class=_ArgumentParser)

    # ------------------------------------------------------------ validate
    p_val = sub.add_parser(
        "validate",
        help="加载并交叉校验配置文件",
        description="加载配置文件，打印告警与错误，并可选输出映射表或 JSON。",
    )
    p_val.add_argument("config", help="配置文件路径（.yaml/.yml/.json/.toml）")
    p_val.add_argument("--show-map", action="store_true", help="打印信号↔通道映射、传感量来源与定时器表")
    p_val.add_argument("--dump", action="store_true", help="把解析后的配置以 JSON 打印到 stdout")
    p_val.add_argument("--json", action="store_true", help="以机器可读 JSON 输出校验结果")
    p_val.set_defaults(func=_cmd_validate)

    # ------------------------------------------------------------- monitor
    p_mon = sub.add_parser(
        "monitor",
        help="周期性打印各 PI/O 状态（手动轮询，Ctrl-C 退出）",
        description="用 ManualRunner 自行控制轮询循环，周期打印状态/输出/定时器/载口/故障。",
    )
    p_mon.add_argument("config", help="配置文件路径")
    p_mon.add_argument(
        "--interval-ms",
        type=float,
        default=None,
        help="打印间隔（毫秒）；默认取配置 poll_interval_ms 的 20 倍，至少 200ms",
    )
    p_mon.add_argument("--backend", default=None, help="覆盖 I/O 后端名（默认用配置里的）")
    p_mon.add_argument(
        "--dry-run",
        action="store_true",
        help="强制使用 null 后端，完全不接触硬件",
    )
    p_mon.add_argument(
        "--duration-s",
        type=float,
        default=None,
        help="运行多少秒后自动退出；默认无限运行，Ctrl-C 退出",
    )
    p_mon.add_argument(
        "--i-know-what-i-am-doing",
        dest="i_know",
        action="store_true",
        help="允许使用真实后端（libgpiod/rpi 等）；不指定时会自动降级为 null",
    )
    p_mon.set_defaults(func=_cmd_monitor)

    # ------------------------------------------------------------ selftest
    p_self = sub.add_parser(
        "selftest",
        help="不接硬件自检（配置/HAL/四个闭环/定时器范围）",
        description=(
            "用 SimIO 与 VirtualRig 做一组自检并逐项打印 PASS/FAIL：\n"
            "  (a) 配置校验\n"
            "  (b) HAL 极性 / 共用通道 / 安全态\n"
            "  (c) 单次 LOAD、单次 UNLOAD、同时 LOAD、连续 UNLOAD→LOAD 四个闭环\n"
            "  (d) 定时器范围检查（配置 TPx 与 TIMER_SPECS 比对）"
        ),
    )
    p_self.add_argument(
        "config",
        nargs="?",
        default=None,
        help="配置文件路径；省略时使用内置最小单载口配置",
    )
    p_self.set_defaults(func=_cmd_selftest)

    # ----------------------------------------------------------------- sim
    p_sim = sub.add_parser(
        "sim",
        help="虚拟线束闭环仿真并打印逐拍信号追踪",
        description="用 VirtualRig 让主动侧参考实现跑指定作业序列，打印逐拍信号表。",
    )
    p_sim.add_argument("config", help="配置文件路径")
    p_sim.add_argument("--interface", default=None, help="接口 id（配置含多个 PI/O 时必填）")
    p_sim.add_argument(
        "--job",
        action="append",
        default=[],
        metavar="OP:ROLES",
        help="作业，可重复。格式 load:left / unload:right / load:left,right（逗号分隔=同时交接）",
    )
    p_sim.add_argument(
        "--continuous",
        action="store_true",
        help="把全部作业当作一次连续交接（用 CONT 串起来）",
    )
    p_sim.add_argument("--dt-ms", type=float, default=2.0, help="仿真步长（毫秒，默认 2）")
    p_sim.add_argument("--timeout-s", type=float, default=30.0, help="仿真超时（秒，默认 30）")
    p_sim.add_argument(
        "--trace-out",
        default=None,
        help="把逐拍追踪写成宽表 CSV（默认只打印到屏幕）",
    )
    p_sim.add_argument(
        "--every-tick",
        action="store_true",
        help="打印每一拍（默认只打印信号/状态发生变化的关键拍）",
    )
    p_sim.set_defaults(func=_cmd_sim)

    # --------------------------------------------------------------- force
    p_force = sub.add_parser(
        "force",
        help="现场接线调试：强制某个输出引脚到指定电平",
        description=(
            "把一个输出信号强制到指定逻辑电平，保持 --hold-ms 后回到安全态。\n"
            "安全联锁：必须显式传 --i-know-what-i-am-doing，且当前不得处于握手中。"
        ),
    )
    p_force.add_argument("config", help="配置文件路径")
    p_force.add_argument("--interface", default=None, help="接口 id（默认取第一个接口）")
    p_force.add_argument("--signal", required=True, help="输出信号名，例如 READY")
    p_force.add_argument("--logical", type=int, choices=(0, 1), required=True, help="逻辑电平：0=OFF，1=ON")
    p_force.add_argument("--backend", default=None, help="覆盖 I/O 后端名（默认用配置里的）")
    p_force.add_argument("--hold-ms", type=float, default=1000.0, help="保持时间（毫秒，默认 1000）")
    p_force.add_argument(
        "--i-know-what-i-am-doing",
        dest="i_know",
        action="store_true",
        help="确认已了解风险（必需）",
    )
    p_force.set_defaults(func=_cmd_force)

    # -------------------------------------------------------------- faults
    p_faults = sub.add_parser(
        "faults",
        help="打印各接口当前故障、条款号与故障态期望信号画像",
        description="用于现场排故：列出故障码、条款号，并给出相关信息 1 的期望信号画像。",
    )
    p_faults.add_argument("config", help="配置文件路径")
    p_faults.add_argument("--interface", default=None, help="只打印指定接口")
    p_faults.set_defaults(func=_cmd_faults)

    return parser


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _load_raw(path: str) -> EquipmentConfig:
    """加载配置但不做交叉校验（解析错误抛 :class:`ConfigError`）。"""

    return load_file(path, validate=False)


def _disabled_trace() -> TraceRecorder:
    """返回一个不落盘的追踪记录器（供只读诊断与自检使用，避免产生副作用文件）。"""

    return TraceRecorder("e84_trace.disabled", enabled=False)


def _without_trace(cfg: EquipmentConfig) -> EquipmentConfig:
    """去掉配置里的追踪落盘（``sim``/``selftest`` 用 ``--trace-out`` 自己记录）。"""

    if not cfg.trace.enabled:
        return cfg
    return dataclasses.replace(cfg, trace=TraceConfig(enabled=False))


def _apply_backend(cfg: EquipmentConfig, name: str) -> EquipmentConfig:
    """把设备级后端名覆盖到整份配置（同时清掉接口级覆盖，保证 CLI 覆盖生效）。"""

    return dataclasses.replace(
        cfg,
        backend=name,
        interfaces=tuple(dataclasses.replace(i, backend=None) for i in cfg.interfaces),
    )


def _choose_backend(
    cfg: EquipmentConfig,
    requested: Optional[str],
    *,
    dry_run: bool,
    confirmed: bool,
) -> Tuple[str, Optional[str], bool]:
    """决定实际使用的后端。

    返回 ``(后端名, 提示信息或 None, 是否需要覆盖配置)``。
    """

    name = (requested or cfg.backend).strip()
    if dry_run:
        return "null", "已启用 --dry-run：强制使用 null 后端（不接触硬件）", True
    if name.lower() in _SAFE_BACKENDS:
        return name, None, bool(requested)
    if confirmed:
        return name, f"使用真实硬件后端 {name!r}", bool(requested)
    return (
        "null",
        f"后端 {name!r} 属于真实硬件；未指定 --i-know-what-i-am-doing，已自动降级为 null",
        True,
    )


def _bool_text(value: bool) -> str:
    return "ON" if value else "OFF"


def _pin_hint(name: str) -> str:
    """返回标准信号的参考引脚号提示（表 9）。"""

    sig = as_signal_name(name)
    if sig is None:
        return ""
    try:
        return f"  [表9 引脚 {db25_pins(sig)}]"
    except KeyError:  # pragma: no cover - 信号表与引脚表应保持一致
        return ""


def _resolve_interface(cfg: EquipmentConfig, requested: Optional[str]) -> InterfaceConfig:
    """解析接口：未指定时要求配置里只有一个接口。"""

    if requested:
        iface = cfg.interface(requested)
        if iface is None:
            known = ", ".join(i.id for i in cfg.interfaces)
            raise ConfigError(f"配置里没有接口 {requested!r}（已知: {known}）")
        return iface
    if len(cfg.interfaces) == 1:
        return cfg.interfaces[0]
    if not cfg.interfaces:
        raise ConfigError("配置里没有任何 PI/O 接口")
    known = ", ".join(i.id for i in cfg.interfaces)
    raise ConfigError(f"配置含多个接口，请用 --interface 指定（已知: {known}）")


def _report_lines(report: ValidationReport) -> List[str]:
    lines: List[str] = []
    for warning in report.warnings:
        lines.append(f"[告警] {warning}")
    for error in report.errors:
        lines.append(f"[错误] {error}")
    return lines


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #
def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        cfg = _load_raw(args.config)
    except ConfigError as exc:
        if args.json:
            print(json.dumps({"ok": False, "file": args.config, "errors": [str(exc)], "warnings": []},
                             ensure_ascii=False, indent=2))
        else:
            print(f"配置错误: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    report = validate_config(cfg, raise_on_error=False)

    if args.json:
        payload: Dict[str, Any] = {
            "ok": report.ok,
            "file": str(args.config),
            "error_count": len(report.errors),
            "warning_count": len(report.warnings),
            "errors": list(report.errors),
            "warnings": list(report.warnings),
        }
        if args.dump:
            payload["config"] = cfg.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK if report.ok else EXIT_CONFIG

    # --dump：stdout 只放 JSON，保证 json.loads() 可直接解析；报告走 stderr。
    stream = sys.stderr if args.dump else sys.stdout
    print(f"配置文件: {args.config}", file=stream)
    print(f"设备名  : {cfg.name}", file=stream)
    print(f"接口数  : {len(cfg.interfaces)}", file=stream)
    for line in _report_lines(report):
        print(f"  {line}", file=stream)
    if report.ok:
        print(f"校验通过：0 个错误，{len(report.warnings)} 个告警", file=stream)
    else:
        print(f"校验失败：{len(report.errors)} 个错误，{len(report.warnings)} 个告警", file=stream)

    if args.show_map:
        _print_signal_map(cfg, stream)

    if args.dump:
        print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))

    return EXIT_OK if report.ok else EXIT_CONFIG


def _print_signal_map(cfg: EquipmentConfig, stream) -> None:
    """打印信号↔通道映射、传感量来源、定时器取值与共用通道提示。"""

    print("\n" + "=" * 78, file=stream)
    print(f"信号映射与定时器表（设备 {cfg.name}，后端 {cfg.backend}）", file=stream)
    print("=" * 78, file=stream)

    try:
        equipment = Equipment(
            cfg,
            ios={i.id: NullIO() for i in cfg.interfaces},
            validate=False,
            trace=_disabled_trace(),
        )
        described = equipment.describe()
    except Exception as exc:  # noqa: BLE001 - 映射无法构建时给出原因而不是堆栈
        print(f"无法构建信号映射: {exc}", file=stream)
        return

    for entry in described["interfaces"]:
        iface_cfg = cfg.interface(entry["id"])
        print(f"\n接口 {entry['id']}", file=stream)
        print(
            f"  场景={entry['scenario']}  拓扑={entry['topology']}"
            f"  同时交接={entry['simultaneous']}  连续交接={entry['continuous']}"
            f"  前置条件={','.join(entry['preconditions'])}",
            file=stream,
        )

        signals = entry["signals"]
        print("  ── 被动侧输出（P→A，本库驱动）", file=stream)
        for name, info in signals["outputs"].items():
            sig = as_signal_name(name)
            direction = direction_of(sig).value if sig is not None else "自定义"
            print(
                f"    {name:<8} -> {info['channel']:<10}"
                f" 方向={direction:<5} active_high={info['active_high']}"
                f" safe_on={info['safe_on']}{_pin_hint(name)}",
                file=stream,
            )
        print("  ── 主动侧输入（A→P，本库读取）", file=stream)
        for name, info in signals["inputs"].items():
            sig = as_signal_name(name)
            direction = direction_of(sig).value if sig is not None else "自定义"
            print(
                f"    {name:<8} <- {info['channel']:<10}"
                f" 方向={direction:<5} active_high={info['active_high']}"
                f" pull={info['pull']} debounce_ms={info['debounce_ms']}{_pin_hint(name)}",
                file=stream,
            )

        shared = signals["shared_channels"]
        if shared:
            print("  ── 共用通道提示", file=stream)
            for channel, users in shared.items():
                note = ""
                if set(users) == {"L_REQ", "U_REQ"}:
                    note = "（现场把两根请求线并在一条线上；表 9 原文是引脚 1 与引脚 2）"
                print(f"    {channel}: {', '.join(users)}{note}", file=stream)
        else:
            print("  ── 共用通道：无", file=stream)

        print("  ── 载口传感量来源", file=stream)
        for port in iface_cfg.load_ports:
            role = port.role.value
            print(f"    载口 {port.id}（role={role}, enabled={port.enabled}）", file=stream)
            for name in ("carrier_present", "carrier_in_position", "ready_for_transfer",
                         "door_open", "clamp_released"):
                key = f"{port.id}.{name}"
                info = entry["sensors"].get(key, {})
                source = info.get("source", "?")
                channels = info.get("channels") or []
                mode = info.get("mode")
                value = info.get("value")
                extra = ""
                if channels:
                    extra = f" channels={channels}" + (f" mode={mode}" if mode else "")
                if source == "constant":
                    extra = f" value={value}"
                print(f"      {name:<20} source={source}{extra}", file=stream)
            print(
                f"      access_mode        source={port.access_mode.source}"
                f" value={port.access_mode.value.value}",
                file=stream,
            )
            print(
                f"      operation_intent   source={port.operation_intent.source}"
                f" value={port.operation_intent.value.value}",
                file=stream,
            )

        print("  ── 定时器取值（§6.3.2.1 表 5/6/7）", file=stream)
        configured = iface_cfg.timers
        if not configured:
            print("    未显式配置，全部使用标准典型值", file=stream)
        for name in (t.value for t in TimerId):
            spec = TIMER_SPECS[TimerId(name)]
            if name in configured:
                value = float(configured[name])
                mark = "配置值"
            else:
                value = spec.typical_s
                mark = "典型值"
            flag = ""
            if spec.owner != "passive":
                flag = "（主动设备定时器）"
            print(
                f"    {name:<4} = {value:>7.3f} s  [{spec.minimum_s}, {spec.maximum_s}]"
                f" 典型={spec.typical_s}  {spec.owner}  {spec.clause}  ({mark}){flag}",
                file=stream,
            )

        print("  ── 策略要点", file=stream)
        policy = iface_cfg.policy
        print(
            f"    not_ready_timeout_s={policy.not_ready_timeout_s}"
            f"  cont_sample_delay_ms={policy.cont_sample_delay_ms}"
            f"  ho_avbl_scope={policy.ho_avbl_scope}",
            file=stream,
        )
        print(
            f"    invalid_cs={policy.invalid_cs}  intent_mismatch={policy.intent_mismatch}"
            f"  on_timeout={policy.on_timeout}  tp6_timeout={policy.tp6_timeout}",
            file=stream,
        )


# --------------------------------------------------------------------------- #
# monitor
# --------------------------------------------------------------------------- #
def _cmd_monitor(args: argparse.Namespace) -> int:
    cfg = _load_raw(args.config)
    backend, note, override = _choose_backend(
        cfg, args.backend, dry_run=args.dry_run, confirmed=args.i_know
    )
    if note:
        print(f"[后端] {note}", file=sys.stderr)
    if override:
        cfg = _apply_backend(cfg, backend)

    poll_ms = max(0.1, float(cfg.poll_interval_ms))
    period_s = poll_ms / 1000.0
    if args.interval_ms is not None:
        interval_s = max(period_s, float(args.interval_ms) / 1000.0)
    else:
        interval_s = max(0.2, period_s * 20.0)
    every_polls = max(1, int(round(interval_s / period_s)))

    equipment = Equipment(cfg, trace=_disabled_trace() if args.dry_run else None)
    runner = ManualRunner(equipment)

    polls = 0
    prints = 0
    t_start = time.monotonic()
    deadline = None if args.duration_s is None else t_start + max(0.0, args.duration_s)

    print(
        f"开始监视 {cfg.name}：后端={cfg.backend} 轮询周期={poll_ms:.1f}ms"
        f" 打印间隔={interval_s * 1000:.0f}ms"
        + (f" 运行时长={args.duration_s}s" if deadline else " 运行时长=无限（Ctrl-C 退出）")
    )
    runner.start()
    try:
        while True:
            runner.poll()
            polls += 1
            if polls == 1 or polls % every_polls == 0:
                _print_monitor_frame(equipment, polls, time.monotonic() - t_start)
                prints += 1
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(period_s)
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C，正在优雅停机…")
    finally:
        runner.stop()

    elapsed = time.monotonic() - t_start
    states = ", ".join(f"{k}={v.value}" for k, v in equipment.states.items())
    faulted = [k for k, f in equipment.faults.items() if f is not None]
    print(
        f"监视结束：轮询 {polls} 拍（打印 {prints} 帧），用时 {elapsed:.2f}s，"
        f"轮询周期 {period_s * 1000:.2f}s；最终状态 {states}；"
        f"故障接口={faulted if faulted else '无'}；"
        f"干涉区冻结={equipment.interlock_engaged}"
    )
    return EXIT_OK


def _print_monitor_frame(equipment: Equipment, poll_index: int, elapsed: float) -> None:
    print(f"\n── t=+{elapsed:8.3f}s  第 {poll_index} 拍 " + "─" * 40)
    for controller in equipment.controllers:
        snap = controller.snapshot()
        fault = snap.get("fault")
        fault_text = "-" if not fault else f"{fault['code']} [{fault['clause']}] {fault['message']}"
        print(
            f"[{snap['interface']}] 状态={snap['state']} 方向={snap['op']}"
            f" 选中载口={snap['selected_ports'] or '-'}"
            f" 干涉区冻结={snap['interlock_engaged']}"
            f" 连续批次={snap['batch_active']}"
        )
        outputs = snap["outputs"]
        print(
            "    输出: "
            + " ".join(f"{k}={_bool_text(bool(v))}" for k, v in outputs.items())
        )
        timers = {k: v for k, v in snap["timers"].items() if v is not None}
        if timers:
            print(
                "    定时器(剩余s): "
                + " ".join(f"{k}={v:.3f}" for k, v in timers.items())
            )
        else:
            print("    定时器(剩余s): -")
        for port in snap["ports"]:
            print(
                f"    载口 {port['id']}({port['role']}): 载具={_bool_text(port['carrier_present'])}"
                f" 到位={_bool_text(port['carrier_in_position'])}"
                f" 可用={_bool_text(port['available'])}"
                f" 访问模式={port['access_mode']} 期望方向={port['expected_op']}"
            )
        print(f"    故障: {fault_text}")


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def _cmd_selftest(args: argparse.Namespace) -> int:
    results: List[Tuple[str, bool, str]] = []

    # ---- 加载配置 -------------------------------------------------------
    if args.config:
        try:
            cfg = _load_raw(args.config)
        except ConfigError as exc:
            print(f"[FAIL] (a) 配置加载: {exc}")
            return EXIT_CONFIG
        source = args.config
    else:
        cfg = load_dict(_MINIMAL_CONFIG, validate=False)
        source = "内置最小双载口配置"

    # 自检不落盘追踪文件（避免在仓库里产生 e84_trace.csv 之类的副作用）
    cfg = _without_trace(cfg)

    print(f"E84 自检：配置来源 = {source}")

    # ---- (a) 配置校验 ---------------------------------------------------
    report = validate_config(cfg, raise_on_error=False)
    results.append(
        (
            "(a) 配置校验",
            report.ok,
            f"{len(report.errors)} 错误 / {len(report.warnings)} 告警",
        )
    )
    for warning in report.warnings:
        print(f"      告警: {warning}")
    for error in report.errors:
        print(f"      错误: {error}")
    if not report.ok:
        _print_selftest_summary(results)
        return EXIT_CONFIG

    # ---- (b) HAL 极性 / 共用通道 / 安全态 -------------------------------
    for check in _check_hal(cfg):
        results.append(check)

    # ---- (c) 四个虚拟闭环 ----------------------------------------------
    for check in _check_closed_loops(cfg):
        results.append(check)

    # ---- (d) 定时器范围 -------------------------------------------------
    for check in _check_timers(cfg):
        results.append(check)

    _print_selftest_summary(results)
    return EXIT_OK if all(ok for _, ok, _ in results) else EXIT_RUNTIME


def _print_selftest_summary(results: Sequence[Tuple[str, bool, str]]) -> None:
    print("\n" + "=" * 78)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 78)
    print(f"自检结果: {passed}/{len(results)} 项通过")


def _check_hal(cfg: EquipmentConfig) -> List[Tuple[str, bool, str]]:
    """(b) HAL 极性、共用通道与安全态检查。"""

    checks: List[Tuple[str, bool, str]] = []
    clock = VirtualClock()
    equipment = Equipment(
        cfg,
        clock=clock,
        ios={i.id: SimIO(name=i.id) for i in cfg.interfaces},
        validate=False,
        trace=_disabled_trace(),
    )
    try:
        equipment.start(clock.now())
        for controller in equipment.controllers:
            hal = controller.hal
            described = hal.describe()
            outputs = described["outputs"]
            prefix = f"[{controller.interface_id}]"

            ok = True
            problems: List[str] = []

            # 极性：force_output 的逻辑值 -> 物理电平必须与 active_high 一致
            for name, info in outputs.items():
                for logical in (False, True):
                    level = hal.force_output(name, logical)
                    expected = bool(logical) if info["active_high"] else (not logical)
                    if level != expected:
                        ok = False
                        problems.append(
                            f"{name} 极性错误（logical={logical} 期望电平={expected} 实际={level}）"
                        )
                    actual = controller.io.read_output(info["channel"])
                    if actual is not None and actual != expected:
                        ok = False
                        problems.append(
                            f"{name} 回读不一致（通道={info['channel']} 期望={expected} 实际={actual}）"
                        )
            checks.append(
                (f"(b1) HAL 极性 {prefix}", ok, "；".join(problems) or f"{len(outputs)} 个输出信号")
            )

            # 安全态
            hal.safe_state()
            ok = True
            problems = []
            for name, info in outputs.items():
                expected = info["safe_on"] if info["active_high"] else (not info["safe_on"])
                actual = controller.io.read_output(info["channel"])
                if actual is not None and actual != expected:
                    ok = False
                    problems.append(f"{name} 安全态电平={actual} 期望={expected}")
            for name in ("ES", "HO_AVBL"):
                if name in outputs and outputs[name]["safe_on"]:
                    ok = False
                    problems.append(f"{name}.safe_on=True，违反 §6.4.6 失效安全（应为 OFF）")
            checks.append(
                (f"(b2) 安全态 {prefix}", ok, "；".join(problems) or "全部输出处于失效安全方向")
            )

            # 共用通道
            shared = hal.shared_channels()
            ok = True
            problems = []
            for channel, users in shared.items():
                if set(users) != {"L_REQ", "U_REQ"}:
                    ok = False
                    problems.append(f"通道 {channel} 被非法共用: {users}")
                polarities = {outputs[u]["active_high"] for u in users}
                if len(polarities) > 1:
                    ok = False
                    problems.append(f"通道 {channel} 共用信号极性不一致")
            if shared:
                try:
                    hal.apply(Outputs(l_req=True, u_req=True))
                    ok = False
                    problems.append("L_REQ/U_REQ 同时 ON 未被拒绝")
                except ValueError:
                    pass
                finally:
                    hal.safe_state()
            # 输入/输出通道不得重叠
            overlap = set(hal.input_channels) & set(hal.output_channels)
            if overlap:
                ok = False
                problems.append(f"输入输出通道重叠: {sorted(overlap)}")
            checks.append(
                (
                    f"(b3) 共用通道/方向 {prefix}",
                    ok,
                    "；".join(problems)
                    or (f"共用通道 {list(shared)}" if shared else "无共用通道，输入输出不重叠"),
                )
            )
    finally:
        equipment.stop(clock.now())
    return checks


def _roles_for_ports(cfg: InterfaceConfig) -> Tuple[PortRole, ...]:
    return tuple(p.role for p in cfg.load_ports)


def _check_closed_loops(cfg: EquipmentConfig) -> List[Tuple[str, bool, str]]:
    """(c) 用 VirtualRig 跑四个闭环。"""

    checks: List[Tuple[str, bool, str]] = []
    if not cfg.interfaces:
        return [("(c) 虚拟闭环", False, "配置里没有接口")]
    iface = cfg.interfaces[0]
    roles = _roles_for_ports(iface)
    left = PortRole.LEFT if PortRole.LEFT in roles else roles[0]
    right = PortRole.RIGHT if PortRole.RIGHT in roles else None
    single = PortRole.SINGLE if PortRole.SINGLE in roles else roles[0]

    def identify(role: PortRole) -> Optional[str]:
        for port in iface.load_ports:
            if port.role is role:
                return port.id
        return None

    scenarios: List[Tuple[str, List[TransferJob], bool, List[str]]] = []

    if len(roles) == 1:
        scenarios.append(("单次 LOAD", [TransferJob.load(single)], False, []))
        scenarios.append(
            ("单次 UNLOAD", [TransferJob.unload(single)], False, [identify(single) or ""])
        )
    else:
        scenarios.append(("单次 LOAD", [TransferJob.load(left)], False, []))
        scenarios.append(
            ("单次 UNLOAD", [TransferJob.unload(left)], False, [identify(left) or ""])
        )
        if right is not None and iface.enable_simultaneous:
            scenarios.append(("同时 LOAD", [TransferJob.load(left, right)], False, []))
        scenarios.append(
            (
                "连续 UNLOAD→LOAD",
                [TransferJob.unload(left), TransferJob.load(left)],
                True,
                [identify(left) or ""],
            )
        )

    for name, jobs, continuous, seed in scenarios:
        if any(not port for port in seed):
            checks.append((f"(c) {name}", False, "找不到对应载口"))
            continue
        detail = ""
        ok = False
        try:
            rig = VirtualRig(
                cfg,
                jobs=jobs,
                continuous=continuous,
                interface_id=iface.id,
                boot_hold_s=0.0,
            )
            for port_id in seed:
                rig.set_carrier(port_id, present=True, in_position=True)
            completed = rig.run_to_completion(timeout_s=20.0, dt=0.001)
            active_done = rig.active.phase is ActivePhase.DONE
            passive_idle = rig.controller.state.value == "idle"
            fault = rig.controller.fault
            ok = bool(completed and active_done and passive_idle and fault is None)
            detail = (
                f"主动={rig.active.phase.value} 被动={rig.controller.state.value}"
                f" 故障={'无' if fault is None else fault.code.value}"
            )
            rig.stop()
        except Exception as exc:  # noqa: BLE001 - 自检不得因单个场景崩溃
            detail = f"异常: {exc!r}"
        checks.append((f"(c) {name}", ok, detail))

    return checks


def _check_timers(cfg: EquipmentConfig) -> List[Tuple[str, bool, str]]:
    """(d) 定时器范围检查。"""

    checks: List[Tuple[str, bool, str]] = []
    for iface in cfg.interfaces:
        ok = True
        problems: List[str] = []
        passive_seen = set()
        for name, value in iface.timers.items():
            try:
                timer_id = TimerId(name)
            except ValueError:
                ok = False
                problems.append(f"未知定时器 {name!r}")
                continue
            spec = TIMER_SPECS[timer_id]
            try:
                spec.validate(float(value))
            except ValueError as exc:
                ok = False
                problems.append(str(exc))
            if spec.owner == "passive":
                passive_seen.add(timer_id)
        missing = [t.value for t in (TimerId.TP1, TimerId.TP2, TimerId.TP3,
                                     TimerId.TP4, TimerId.TP5, TimerId.TP6)
                   if t not in passive_seen]
        note = f"已配置 {len(iface.timers)} 个定时器"
        if missing:
            note += f"；未显式配置（用典型值）: {', '.join(missing)}"
        note += "；TP3/TP4 典型值 60s"
        checks.append(
            (f"(d) 定时器范围 [{iface.id}]", ok, "；".join(problems) or note)
        )
    if not checks:
        checks.append(("(d) 定时器范围", False, "没有接口可检查"))
    return checks


# --------------------------------------------------------------------------- #
# sim
# --------------------------------------------------------------------------- #
_ROLE_ALIASES: Mapping[str, Tuple[PortRole, ...]] = {
    "left": (PortRole.LEFT,),
    "l": (PortRole.LEFT,),
    "right": (PortRole.RIGHT,),
    "r": (PortRole.RIGHT,),
    "single": (PortRole.SINGLE,),
    "lp": (PortRole.SINGLE,),
    "both": (PortRole.LEFT, PortRole.RIGHT),
    "all": (PortRole.LEFT, PortRole.RIGHT),
}

_TRACE_COLUMNS = (
    "t_s",
    "active_phase",
    "VALID",
    "CS_0",
    "CS_1",
    "TR_REQ",
    "BUSY",
    "COMPT",
    "CONT",
    "L_REQ",
    "U_REQ",
    "READY",
    "HO_AVBL",
    "ES",
    "passive_state",
)


def _parse_job(text: str) -> TransferJob:
    """解析 ``load:left`` / ``unload:right`` / ``load:left,right``。"""

    if ":" not in text:
        raise ConfigError(f"作业格式非法: {text!r}（应为 OP:ROLES，例如 load:left）")
    op_text, roles_text = text.split(":", 1)
    op_key = op_text.strip().lower()
    try:
        op = Op(op_key)
    except ValueError as exc:
        raise ConfigError(
            f"作业方向非法: {op_text!r}（可选: load / unload）"
        ) from exc
    if op is Op.UNKNOWN:
        raise ConfigError(f"作业方向非法: {op_text!r}（可选: load / unload）")

    roles: List[PortRole] = []
    for raw in roles_text.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        if key not in _ROLE_ALIASES:
            raise ConfigError(
                f"载口角色非法: {raw!r}（可选: left / right / single / both）"
            )
        for role in _ROLE_ALIASES[key]:
            if role not in roles:
                roles.append(role)
    if not roles:
        raise ConfigError(f"作业 {text!r} 未指定载口角色")
    return TransferJob(tuple(roles), op)


def _seed_for_jobs(
    iface: InterfaceConfig, jobs: Sequence[TransferJob]
) -> Dict[str, Tuple[bool, bool]]:
    """按作业序列推导每个载口的初始载具状态，保证第一个作业可执行。"""

    role_to_port = {port.role: port.id for port in iface.load_ports}
    seed: Dict[str, Tuple[bool, bool]] = {}
    for job in jobs:
        for role in job.roles:
            port_id = role_to_port.get(role)
            if port_id is None or port_id in seed:
                continue
            if job.op is Op.UNLOAD:
                seed[port_id] = (True, True)
            else:
                seed[port_id] = (False, False)
    for port in iface.load_ports:
        seed.setdefault(port.id, (False, False))
    return seed


def _capture_trace_row(rig: VirtualRig) -> Tuple[Any, ...]:
    logical_inputs = dict(rig.controller.hal.logical_inputs)
    outputs = rig.controller.outputs().as_dict()
    return (
        round(rig.clk.now(), 6),
        rig.active.phase.value,
        bool(logical_inputs.get("VALID", False)),
        bool(logical_inputs.get("CS_0", False)),
        bool(logical_inputs.get("CS_1", False)),
        bool(logical_inputs.get("TR_REQ", False)),
        bool(logical_inputs.get("BUSY", False)),
        bool(logical_inputs.get("COMPT", False)),
        bool(logical_inputs.get("CONT", False)),
        bool(outputs.get("L_REQ", False)),
        bool(outputs.get("U_REQ", False)),
        bool(outputs.get("READY", False)),
        bool(outputs.get("HO_AVBL", False)),
        bool(outputs.get("ES", False)),
        rig.controller.state.value,
    )


def _cmd_sim(args: argparse.Namespace) -> int:
    cfg = _without_trace(_load_raw(args.config))
    iface = _resolve_interface(cfg, args.interface)

    jobs = [_parse_job(text) for text in (args.job or [])]
    if not jobs:
        raise ConfigError("至少需要一个 --job，例如 --job load:left")

    available_roles = {port.role for port in iface.load_ports}
    for job in jobs:
        for role in job.roles:
            if role not in available_roles:
                raise ConfigError(
                    f"作业需要 role={role.value} 的载口，但接口 {iface.id} 只有 "
                    f"{sorted(r.value for r in available_roles)}"
                )
    if len(jobs) > 1 and not args.continuous:
        # 非连续：逐个跑，但这里为了逐拍追踪仍放在同一个 rig 里顺序执行
        log.info("多个 --job 但未指定 --continuous：将作为独立单次交接顺序执行")

    rig = VirtualRig(
        cfg,
        jobs=jobs,
        continuous=bool(args.continuous),
        interface_id=iface.id,
        boot_hold_s=0.0,
    )
    active_done = False
    passive_idle = False
    passive_state = "?"
    fault: Optional[Fault] = None
    try:
        for port_id, (present, in_position) in _seed_for_jobs(iface, jobs).items():
            rig.set_carrier(port_id, present=present, in_position=in_position)

        dt = max(1e-4, float(args.dt_ms) / 1000.0)
        steps = max(1, int(round(max(0.1, float(args.timeout_s)) / dt)))
        rows: List[Tuple[Any, ...]] = []
        for _ in range(steps):
            rig.tick(dt)
            rows.append(_capture_trace_row(rig))
            if rig.active.completed and rig.controller.state.value == "idle":
                break

        _print_trace(rows, every_tick=args.every_tick)

        if args.trace_out:
            _write_trace_csv(Path(args.trace_out), rows)
            print(f"\n逐拍追踪已写入 CSV: {args.trace_out}（共 {len(rows)} 拍）")

        active_done = rig.active.phase is ActivePhase.DONE
        passive_state = rig.controller.state.value
        passive_idle = passive_state == "idle"
        fault = rig.controller.fault
        ok = bool(active_done and passive_idle and fault is None)
    finally:
        rig.stop()
    print(
        f"\n主动侧: {'done' if active_done else rig.active.phase.value}"
        f"（aborted={rig.active.aborted} fault={rig.active.fault}）"
        f"  被动侧: {passive_state}"
        f"  故障: {'无' if fault is None else fault.code.value}"
    )
    print(f"结果: {'PASS' if ok else 'FAIL'}")
    return EXIT_OK if ok else EXIT_RUNTIME


def _print_trace(rows: Sequence[Tuple[Any, ...]], *, every_tick: bool) -> None:
    header = (
        f"{'t':>8}  {'主动阶段':<14} "
        f"{'VALID':>5} {'CS_0':>5} {'CS_1':>5} {'TR_REQ':>6} {'BUSY':>5} {'COMPT':>5} {'CONT':>5} "
        f"{'L_REQ':>5} {'U_REQ':>5} {'READY':>5} {'HO_AVBL':>7} {'ES':>4} "
        f"{'被动状态':<14}"
    )
    print("\n逐拍信号追踪（1=ON / 0=OFF）")
    print(header)
    print("-" * len(header))

    def fmt(row: Tuple[Any, ...]) -> str:
        t = row[0]
        bits = "".join(f"{'1' if v else '0':>5} " for v in row[2:9])
        outs = "".join(f"{'1' if v else '0':>5} " for v in row[9:14])
        return f"{t:>8.3f}  {row[1]:<14} {bits}{outs}{row[14]:<14}"

    if every_tick or not rows:
        for row in rows:
            print(fmt(row))
        return

    shown = 0
    previous: Optional[Tuple[Any, ...]] = None
    for row in rows:
        if previous is None or row[1:] != previous[1:]:
            print(fmt(row))
            shown += 1
        previous = row
    if rows and shown and rows[-1] is not previous:
        print(fmt(rows[-1]))
    print(f"（共 {len(rows)} 拍，只打印信号/状态变化的 {shown} 拍；--every-tick 可打印全部）")


def _write_trace_csv(path: Path, rows: Sequence[Tuple[Any, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_TRACE_COLUMNS)
        for row in rows:
            writer.writerow(
                [f"{row[0]:.6f}", row[1]]
                + [int(bool(v)) for v in row[2:14]]
                + [row[14]]
            )


# --------------------------------------------------------------------------- #
# force
# --------------------------------------------------------------------------- #
def _cmd_force(args: argparse.Namespace) -> int:
    if not args.i_know:
        print(
            "拒绝执行：force 会直接驱动输出引脚，必须显式传入 --i-know-what-i-am-doing。",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    cfg = _load_raw(args.config)
    iface = _resolve_interface(cfg, args.interface)
    backend, note, override = _choose_backend(
        cfg, args.backend, dry_run=False, confirmed=True
    )
    if note:
        print(f"[后端] {note}", file=sys.stderr)
    if override:
        cfg = _apply_backend(cfg, backend)

    name = signal_name(args.signal)
    if name not in iface.outputs:
        known = ", ".join(sorted(iface.outputs)) or "（无）"
        print(f"接口 {iface.id} 没有输出信号 {name!r}（已知: {known}）", file=sys.stderr)
        return EXIT_CONFIG

    logical = bool(args.logical)
    equipment = Equipment(cfg, trace=_disabled_trace())
    runner = ManualRunner(equipment)
    channel = iface.outputs[name].channel
    try:
        runner.start()
        controller = equipment.controller(iface.id)
        state = controller.state.value
        if state not in ("idle", "disabled") or controller.outputs().demand_on:
            print(
                f"拒绝执行：接口 {iface.id} 当前状态={state}，请求线="
                f"{'ON' if controller.outputs().demand_on else 'OFF'}，"
                "可能存在正在进行的握手，禁止强制输出。",
                file=sys.stderr,
            )
            return EXIT_RUNTIME

        print(
            f"[FORCE] 接口={iface.id} 后端={cfg.backend} 状态={state}"
            f" 信号={name} 逻辑={_bool_text(logical)} 通道={channel}"
        )
        level = controller.hal.force_output(name, logical)
        print(f"[FORCE] 已写入通道 {channel} 物理电平={'1' if level else '0'}")
        if controller.hal.shared_channels().get(channel):
            users = ", ".join(controller.hal.shared_channels()[channel])
            print(f"[FORCE] 注意：通道 {channel} 被共用（{users}），协议运行时会被覆盖")
        time.sleep(max(0.0, float(args.hold_ms)) / 1000.0)
    finally:
        try:
            for controller in equipment.controllers:
                written = controller.hal.safe_state()
                if written:
                    print(
                        "[FORCE] 恢复安全态："
                        + ", ".join(f"{ch}={int(lv)}" for ch, lv in written.items())
                    )
        finally:
            runner.stop()
    print("[FORCE] 完成，输出已回到安全态并停机")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# faults
# --------------------------------------------------------------------------- #
def _cmd_faults(args: argparse.Namespace) -> int:
    cfg = _load_raw(args.config)

    # 故障诊断只读，永远注入 NullIO：不打开任何后端、不接触硬件。
    equipment = Equipment(
        cfg,
        ios={i.id: NullIO() for i in cfg.interfaces},
        validate=False,
        trace=_disabled_trace(),
    )

    targets = (
        [equipment.controller(args.interface)]
        if args.interface
        else equipment.controllers
    )

    print(f"设备 {cfg.name}：接口 {len(equipment.controllers)} 个；诊断后端=null（不接触硬件）")
    for controller in targets:
        fault: Optional[Fault] = controller.fault
        snapshot = controller.snapshot()
        print("\n" + "-" * 70)
        print(f"接口 {controller.interface_id}  状态={snapshot['state']}  方向={snapshot['op']}")
        if fault is None:
            print("  当前故障: 无")
        else:
            print(f"  当前故障: {fault.code.value}  [{fault.clause or '无条款'}]")
            print(f"    说明  : {fault.message}")
            print(f"    出错状态: {fault.state}  时刻: {fault.timestamp:.3f}s")
            if fault.context:
                print(f"    上下文: {json.dumps(dict(fault.context), ensure_ascii=False, default=str)}")
        print("  故障态期望信号画像（相关信息 1 / R1-1.1.2.1，储料机侧出错）:")
        print("    READY   = OFF   （拉低「已就绪」）")
        print("    ES      = OFF   （请求主动侧立即停止）")
        print("    HO_AVBL = OFF   （声明不可交接）")
        print("    请求线  = 保持出错时刻的状态（request_hold_on_fault=True）")
        print("  本接口输出: " + " ".join(
            f"{k}={_bool_text(bool(v))}" for k, v in snapshot["outputs"].items()
        ))
        print("  恢复方式: clear_fault(force=?)（§6.3.3.1 标准不定义恢复流程；本库默认锁存）")
    print("\n可用故障码与条款：")
    for code in FaultCode:
        print(f"  {code.value:<32} {FAULT_CLAUSE.get(code, '')}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。返回退出码（0/1/2），不调用 :func:`sys.exit`。"""

    raw = list(sys.argv[1:] if argv is None else argv)

    parser = build_parser()
    if not raw:
        parser.print_help()
        print("\n可用子命令: validate, monitor, selftest, sim, force, faults")
        print("示例: python -m e84.cli validate configs/example_2lp_standard.yaml --show-map")
        return EXIT_OK

    args = parser.parse_args(raw)

    level = logging.ERROR
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK

    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置/参数错误: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:  # pragma: no cover - 交互场景
        print("\n已中断", file=sys.stderr)
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转成运行期失败
        print(f"运行失败: {exc!r}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
