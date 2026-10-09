"""配置交叉校验。

目标：**把现场错误拦在启动之前**，并给出人能读懂的原因与所依据的标准条款。

校验分两级：

* **错误（error）** —— 无法安全运行，直接抛 :class:`e84.fault.ConfigError`；
* **告警（warning）** —— 可疑但不致命，返回给调用方（``e84-validate`` 会打印）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Mapping, Set, Tuple

from e84.config.model import EquipmentConfig, InterfaceConfig, LoadPortConfig
from e84.fault import ConfigError
from e84.model import AccessMode, PortRole, Scenario, Topology
from e84.signals import (
    PASSIVE_OUTPUT_SIGNALS,
    Signal,
    as_signal_name,
)
from e84.timers import TIMER_SPECS, TimerId

__all__ = ["ValidationReport", "validate_config"]

#: 标准场景下被动侧**必须**绑定的输入信号。
REQUIRED_INPUTS_BASE: Tuple[str, ...] = ("VALID", "CS_0", "TR_REQ", "BUSY", "COMPT")
#: 标准场景下被动侧**必须**绑定的输出信号。
REQUIRED_OUTPUTS_BASE: Tuple[str, ...] = ("READY", "HO_AVBL", "ES")
#: 同时交接（两载口共用一个 PI/O）必需。
REQUIRED_INPUTS_TWO_LP: Tuple[str, ...] = ("CS_1",)
#: 连续交接必需。
REQUIRED_INPUTS_CONTINUOUS: Tuple[str, ...] = ("CONT",)


@dataclass
class ValidationReport:
    """校验结果。"""

    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def raise_if_bad(self) -> None:
        if self.errors:
            bullets = "\n".join(f"  - {e}" for e in self.errors)
            raise ConfigError(f"配置校验失败（{len(self.errors)} 项）：\n{bullets}")


def validate_config(
    config: EquipmentConfig, *, raise_on_error: bool = True
) -> ValidationReport:
    """对整份设备配置做交叉校验。"""

    report = ValidationReport()

    if config.schema_version != 1:
        report.error(
            f"不支持的 schema_version={config.schema_version}（当前只支持 1）"
        )
    if not config.interfaces:
        report.error("配置里没有任何 PI/O 接口（interfaces 为空）")
    if config.poll_interval_ms <= 0:
        report.error(f"poll_interval_ms 必须为正数，实际 {config.poll_interval_ms}")
    if config.poll_interval_ms > 20:
        report.warn(
            f"poll_interval_ms={config.poll_interval_ms}ms 偏大。"
            "§6.3.2.3 的 TD0 只有 100 ms，轮询周期过大会让互锁精度明显下降（建议 ≤10ms）"
        )
    if config.boot_safe_hold_ms < 0:
        report.error("boot_safe_hold_ms 不能为负")

    seen_ids: Set[str] = set()
    for iface in config.interfaces:
        if iface.id in seen_ids:
            report.error(f"接口 id 重复: {iface.id!r}")
        seen_ids.add(iface.id)
        _validate_interface(iface, report)

    if raise_on_error:
        report.raise_if_bad()
    return report


# --------------------------------------------------------------------------- #
# 单接口校验
# --------------------------------------------------------------------------- #
def _validate_interface(iface: InterfaceConfig, report: ValidationReport) -> None:
    prefix = f"[{iface.id}]"

    # ---- 场景 ----
    if iface.scenario is Scenario.INTERBAY_PASSIVE_OHS:
        report.error(
            f"{prefix} scenario=interbay_passive_ohs 在本版本尚未实现。"
            "跨区场景（VA/VS_0/VS_1/AM_AVBL，fig_12/13/15/20）已预留配置位与信号定义，"
            "但状态机尚未实现，请先用 scenario=standard"
        )

    # ---- 拓扑与载口 ----
    ports = list(iface.load_ports)
    if not ports:
        report.error(f"{prefix} 没有配置任何载口（load_ports 为空）")
    port_ids = [p.id for p in ports]
    if len(set(port_ids)) != len(port_ids):
        report.error(f"{prefix} 载口 id 重复: {port_ids}")

    if iface.topology is Topology.ONE_LP:
        if len(ports) != 1:
            report.error(
                f"{prefix} topology=one_load_port 要求恰好 1 个载口，实际 {len(ports)} 个"
            )
        if ports and ports[0].role is PortRole.RIGHT:
            report.error(
                f"{prefix} single-PI/O 拓扑下的载口 role 不应是 right（§6.1.2.1/表3）"
            )
    else:  # TWO_LP
        if len(ports) != 2:
            report.error(
                f"{prefix} topology=two_load_ports 要求恰好 2 个载口，实际 {len(ports)} 个"
            )
        roles = {p.role for p in ports}
        if roles != {PortRole.LEFT, PortRole.RIGHT}:
            report.error(
                f"{prefix} topology=two_load_ports 的两个载口 role 必须分别是 "
                f"left 与 right（§6.1.2.3：面向设备载口时的左右手），实际 {sorted(r.value for r in roles)}"
            )
        if iface.enable_simultaneous and len(ports) != 2:
            report.error(f"{prefix} 启用同时交接需要 2 个载口（§6.1.2.4）")

    # ---- 输入信号 ----
    inputs = dict(iface.inputs)
    outputs = dict(iface.outputs)

    for name in inputs:
        sig = as_signal_name(name)
        if sig is not None and sig in PASSIVE_OUTPUT_SIGNALS:
            report.error(
                f"{prefix} 输入表里出现了被动侧输出信号 {name!r}"
                f"（{name} 是 P→A 方向，应写在 outputs 里）"
            )

    for name in outputs:
        sig = as_signal_name(name)
        if sig is None:
            report.error(
                f"{prefix} outputs 里的 {name!r} 不是标准的被动侧输出信号"
                f"（合法值: {', '.join(sorted(s.value for s in PASSIVE_OUTPUT_SIGNALS))}）"
            )
        elif sig not in PASSIVE_OUTPUT_SIGNALS:
            report.error(
                f"{prefix} outputs 里的 {name!r} 方向为 A→P，不能作为被动侧输出"
            )

    required_inputs = list(REQUIRED_INPUTS_BASE)
    if iface.topology is Topology.TWO_LP:
        required_inputs += list(REQUIRED_INPUTS_TWO_LP)
    if iface.enable_continuous:
        required_inputs += list(REQUIRED_INPUTS_CONTINUOUS)
    for name in required_inputs:
        if name not in inputs:
            report.error(
                f"{prefix} 缺少必需的输入信号 {name!r}。"
                "所需集合由拓扑与功能决定（§6.1 表1、§6.2.3、§6.2.4）"
            )

    required_outputs = list(REQUIRED_OUTPUTS_BASE)
    if not ({"L_REQ", "U_REQ"} & set(outputs)):
        report.error(
            f"{prefix} 至少需要绑定 L_REQ 或 U_REQ 之一作为「请求」输出"
            "（§6.1 表1；表9 原文中 L_REQ=引脚1、U_REQ=引脚2 各自独立，\n"
            "   现场若把两者并在一条线上，可以写同一个 channel）"
        )
    for name in required_outputs:
        if name not in outputs:
            report.error(f"{prefix} 缺少必需的输出信号 {name!r}")

    # ---- 前置条件 ----
    bound_inputs = set(inputs)
    for name in iface.preconditions:
        if name in bound_inputs:
            continue
        if as_signal_name(name) is not None:
            report.error(
                f"{prefix} 前置条件 {name!r} 已绑定为 E84 标准信号但未出现在 inputs 里"
            )
        else:
            report.warn(
                f"{prefix} 前置条件 {name!r} 是现场自定义信号，需要在 inputs 里绑定"
                "并在运行时通过 Inputs.extra 提供取值"
            )

    # ---- 通道唯一性 ----
    _validate_channels(iface, report)

    # ---- 定时器 ----
    for name, value in iface.timers.items():
        try:
            timer_id = TimerId(name)
        except ValueError:
            valid = ", ".join(t.value for t in TimerId)
            report.error(f"{prefix} 未知定时器 {name!r}（合法值: {valid}）")
            continue
        spec = TIMER_SPECS[timer_id]
        if spec.owner != "passive" and timer_id is not TimerId.TD1:
            report.warn(
                f"{prefix} 定时器 {name} 属于主动设备（{spec.clause}），"
                "在被动侧配置它通常没有意义"
            )
        try:
            spec.validate(float(value))
        except ValueError as exc:
            report.error(f"{prefix} {exc}")

    if iface.policy.not_ready_timeout_s >= 2.0:
        report.warn(
            f"{prefix} policy.not_ready_timeout_s={iface.policy.not_ready_timeout_s}s "
            "已达到主动设备 TA1 的典型值 2s（表5）。若该值不小于对方的 TA1，"
            "被动方还没来得及用 HO_AVBL 表达「不可交接」，对方就已超时了；建议取 1~1.5s"
        )

    # ---- 安全方向 ----
    for name in ("ES", "HO_AVBL"):
        spec = outputs.get(name)
        if spec is not None and spec.safe_on:
            report.warn(
                f"{prefix} 输出 {name} 的 safe_on=True：安全态下该信号会处于 ON。"
                "按 §6.4.6（OFF = 无电流/无光）与失效安全原则，"
                f"{name} 的安全态应为 OFF（safe_on=False）"
            )

    # ---- 手动模式与 HO_AVBL ----
    any_manual_capable = any(
        p.access_mode.source != "static" or p.access_mode.value is AccessMode.MANUAL
        for p in ports
    )
    if not iface.policy.ho_avbl_when_manual and not any_manual_capable:
        report.warn(
            f"{prefix} policy.ho_avbl_when_manual=False，但没有任何载口可能进入手动模式，"
            "该设置当前不会生效"
        )

    # ---- 现场自定义信号提示 ----
    extra_inputs = sorted(n for n in inputs if as_signal_name(n) is None)
    if extra_inputs:
        report.warn(
            f"{prefix} 使用了现场自定义输入信号 {', '.join(extra_inputs)}："
            "它们会照常从绑定的通道读取，并作为 Inputs.extra 提供给 preconditions "
            "与上层逻辑；只有在没有绑定通道、需要运行时注入时才用 "
            "PiOController.set_extra_input()"
        )

    # ---- 极性与上拉的配合（A1-7 那个坑）----
    for name, spec in inputs.items():
        if spec.pull == "up" and spec.active_high:
            report.warn(
                f"{prefix} 输入 {name} 配置为 active_high=true 且 pull=up："
                "空闲(开路)时引脚为高，会被判成 ON。"
                "若要表达「断开=无效」，应改为 pull=down 或 active_high=false"
            )
        if spec.pull == "down" and not spec.active_high:
            report.warn(
                f"{prefix} 输入 {name} 配置为 active_high=false 且 pull=down："
                "空闲(开路)时引脚为低，会被判成 ON。"
                "§6.4.6 规定 OFF = 无电流/无光，请确认现场接线"
            )

    # ---- 单载口拓扑下 CS_1 的处理 ----
    if iface.topology is Topology.ONE_LP and iface.policy.single_lp_require_cs1_off:
        if "CS_1" not in inputs:
            report.warn(
                f"{prefix} policy.single_lp_require_cs1_off=True 但未绑定 CS_1，"
                "该检查不会生效（表3 要求单载口时 CS_1 恒为 OFF）"
            )


def _validate_channels(iface: InterfaceConfig, report: ValidationReport) -> None:
    prefix = f"[{iface.id}]"

    input_channels: Mapping[str, str] = {
        name: spec.channel for name, spec in iface.inputs.items()
    }
    # 同一物理通道被两个输入共用：几乎总是配置错误
    reverse: dict = {}
    for name, channel in input_channels.items():
        if channel in reverse:
            report.error(
                f"{prefix} 输入通道 {channel!r} 被 {reverse[channel]!r} 与 {name!r} 同时占用；"
                "每个物理引脚方向唯一，请检查配置"
            )
        reverse[channel] = name

    # 输出：只允许 L_REQ/U_REQ 共用一个通道（现场并线的接法）
    out_by_channel: dict = {}
    for name, spec in iface.outputs.items():
        out_by_channel.setdefault(spec.channel, []).append(name)
    for channel, names in out_by_channel.items():
        if len(names) > 1:
            allowed = set(names) == {"L_REQ", "U_REQ"}
            message = (
                f"{prefix} 输出通道 {channel!r} 被多个信号共用: {', '.join(names)}"
            )
            if allowed:
                report.warn(
                    message + "：表 9 原文中 L_REQ(引脚1) 与 U_REQ(引脚2) 是**各自独立**的引脚；"
                    "共用同一个 channel 属于现场接法（有些设备把两根请求线并在一条线上），"
                    "HAL 会保证两者互斥"
                )
            else:
                report.error(
                    message + "；除 L_REQ/U_REQ 外不允许输出通道复用"
                )

    # 输入与输出不得共用同一通道
    for channel, names in out_by_channel.items():
        if channel in reverse:
            report.error(
                f"{prefix} 通道 {channel!r} 同时被输出 {names[0]!r} 与输入 {reverse[channel]!r} 占用"
            )

    # 传感量通道：允许同一个载口把同一批通道同时用于「任意」与「全部」判定
    # （现场常见的三键方案：任意键=检测到载具，三键全落=完整落位），
    # 但**跨载口复用**或**极性/上拉不一致**属于配置错误。
    sensor_channels: dict = {}
    for port in iface.load_ports:
        for sensor_name, spec in port.sensor_specs.items():
            for channel in spec.all_channels:
                owner = sensor_channels.get(channel)
                if owner is None:
                    sensor_channels[channel] = (
                        port.id,
                        f"{port.id}.{sensor_name}",
                        spec.active_high,
                        spec.pull,
                    )
                    continue
                owner_port, owner_name, owner_active_high, owner_pull = owner
                if owner_port != port.id:
                    report.error(
                        f"{prefix} 传感量通道 {channel!r} 被不同载口共用："
                        f"{owner_name!r} 与 {port.id + '.' + sensor_name!r}"
                    )
                elif (owner_active_high, owner_pull) != (spec.active_high, spec.pull):
                    report.error(
                        f"{prefix} 传感量通道 {channel!r} 被 {owner_name!r} 与 "
                        f"{port.id + '.' + sensor_name!r} 共用但极性/上拉不一致"
                    )
        if port.access_mode.source == "input" and port.access_mode.channel:
            channel = port.access_mode.channel
            owner = sensor_channels.get(channel)
            if owner is not None and owner[0] != port.id:
                report.error(
                    f"{prefix} 访问模式通道 {channel!r} 与传感量 {owner[1]!r} 冲突"
                )
            sensor_channels.setdefault(
                channel,
                (port.id, f"{port.id}.access_mode", port.access_mode.active_high, "none"),
            )

    overlap = set(sensor_channels) & (set(reverse) | set(out_by_channel))
    for channel in sorted(overlap):
        e84_users = []
        if channel in reverse:
            e84_users.append(reverse[channel])
        e84_users.extend(out_by_channel.get(channel, ()))
        report.error(
            f"{prefix} 通道 {channel!r} 同时被 E84 信号 {', '.join(e84_users)} 与传感量 "
            f"{sensor_channels[channel]!r} 占用；E84 信号线与本机传感器必须使用不同引脚"
        )

    # 传感量来源提示
    for port in iface.load_ports:
        _validate_port_sensors(port, report, prefix)
        if port.access_mode.source == "api":
            report.warn(
                f"{prefix} 载口 {port.id} 的访问模式来源是 api："
                "需要通过 LoadPort.set_access_mode() 注入，否则取配置默认值"
            )
        if port.operation_intent.source == "api":
            report.warn(
                f"{prefix} 载口 {port.id} 的交接意图来源是 api："
                "需要通过 LoadPort.set_operation_intent() 注入，否则由传感器推导"
            )


def _validate_port_sensors(
    port: LoadPortConfig, report: ValidationReport, prefix: str
) -> None:
    for name, spec in port.sensor_specs.items():
        where = f"{prefix} 载口 {port.id} 的 {name}"
        if spec.source == "keys" and len(spec.channels) < 2:
            report.warn(
                f"{where} 使用 keys 来源但只有 {len(spec.channels)} 个通道，等价于单个输入"
            )
        if spec.pull not in ("up", "down", "none"):
            report.error(f"{where} 的 pull 取值非法: {spec.pull!r}（可选: up/down/none）")
    if (
        port.carrier_in_position.source != "constant"
        and port.carrier_present.source != "constant"
        and port.carrier_in_position == port.carrier_present
    ):
        report.warn(
            f"{prefix} 载口 {port.id} 的 carrier_present 与 carrier_in_position 使用完全相同的来源；"
            "这会退化为「检测到载具即视为完整落位」。"
            "§6.1 表1 要求以「载具位于正确位置」判定 L_REQ/U_REQ 落下，"
            "若不区分，载具未落稳就可能撤掉请求线（现场掉片风险）"
        )
