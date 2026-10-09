"""配置加载与交叉校验测试。

覆盖标准条款 / 设计约束：

* §6.1 表1、§6.2.3、§6.2.4 —— 拓扑与功能决定**必需的输入/输出信号集合**；
* §6.1.2.1/表3 —— 单载口拓扑下的载口数与角色约束；
* §6.1.2.3 —— 双载口拓扑必须一 left 一 right；
* §6.1.2.4 —— 同时交接需要两个载口；
* §6.3.2.1 表6 / §6.3.2.3 表7 —— 定时器取值范围（TPx 1–999s；TD0 0.1–0.2s）；
* 表9 —— 原文里 ``L_REQ``/``U_REQ`` 是独立引脚 1/2；现场若并成一条线，允许共用通道但只告警；
* §6.4.6 —— 失效安全：``ES``/``HO_AVBL`` 的安全态必须为 OFF（``safe_on=True`` 只告警）；
* 跨区场景（fig_12/13/15/20）本版本未实现，必须报错。
"""

from __future__ import annotations

import copy
import json
import textwrap

import pytest

from e84 import ConfigError, EquipmentConfig, jsonable, load_dict, load_file
from e84.config import loads
from e84.config.model import (
    AccessModeSpec,
    ChannelSpec,
    IoDefaults,
    PolicyConfig,
    SensorSpec,
)
from e84.config.validate import validate_config
from e84.model import AccessMode, PortRole, Scenario, Topology
from tests.support import base_config_dict

# --------------------------------------------------------------------------- #
# 加载路径：dict / YAML / JSON / TOML / 文件
# --------------------------------------------------------------------------- #
_MIN_CONFIG = {
    "schema_version": 1,
    "name": "MIN",
    "backend": "sim",
    "interfaces": [
        {
            "id": "PIO1",
            "topology": "two_load_ports",
            "load_ports": [
                {"id": "LP1", "role": "left"},
                {"id": "LP2", "role": "right"},
            ],
            "inputs": {
                "VALID": "IN1",
                "CS_0": "IN2",
                "CS_1": "IN3",
                "TR_REQ": "IN5",
                "BUSY": "IN6",
                "COMPT": "IN7",
                "CONT": "IN8",
            },
            "outputs": {
                "L_REQ": "OUT1",
                "U_REQ": "OUT1",
                "READY": "OUT4",
                "HO_AVBL": "OUT7",
                "ES": "OUT8",
            },
        }
    ],
}

_MIN_YAML = textwrap.dedent(
    """
    schema_version: 1
    name: MIN
    backend: sim
    interfaces:
      - id: PIO1
        topology: two_load_ports
        load_ports:
          - {id: LP1, role: left}
          - {id: LP2, role: right}
        inputs: {VALID: IN1, CS_0: IN2, CS_1: IN3, TR_REQ: IN5, BUSY: IN6, COMPT: IN7, CONT: IN8}
        outputs: {L_REQ: OUT1, U_REQ: OUT1, READY: OUT4, HO_AVBL: OUT7, ES: OUT8}
    """
).strip()

_MIN_TOML = textwrap.dedent(
    """
    schema_version = 1
    name = "MIN"
    backend = "sim"

    [[interfaces]]
    id = "PIO1"
    topology = "two_load_ports"
    inputs = { VALID = "IN1", CS_0 = "IN2", CS_1 = "IN3", TR_REQ = "IN5", BUSY = "IN6", COMPT = "IN7", CONT = "IN8" }
    outputs = { L_REQ = "OUT1", U_REQ = "OUT1", READY = "OUT4", HO_AVBL = "OUT7", ES = "OUT8" }

    [[interfaces.load_ports]]
    id = "LP1"
    role = "left"

    [[interfaces.load_ports]]
    id = "LP2"
    role = "right"
    """
).strip()


def test_load_dict_and_validate_ok():
    cfg = load_dict(_MIN_CONFIG)
    assert isinstance(cfg, EquipmentConfig)
    assert cfg.name == "MIN"
    assert cfg.interfaces[0].id == "PIO1"
    assert cfg.interfaces[0].topology is Topology.TWO_LP
    assert cfg.interfaces[0].scenario is Scenario.STANDARD


def test_load_yaml_text():
    from e84 import load_yaml

    cfg = load_yaml(_MIN_YAML)
    assert cfg.interfaces[0].port("LP1").role is PortRole.LEFT
    assert cfg.interfaces[0].port("LP2").role is PortRole.RIGHT


def test_load_json_text():
    from e84 import load_json

    cfg = load_json(json.dumps(_MIN_CONFIG))
    assert cfg.interfaces[0].port("LP2").role is PortRole.RIGHT


def test_load_toml_text():
    """TOML：Python ≥3.11 用标准库，3.10 需要 tomli；二者都没有则跳过。"""

    try:
        import tomllib  # noqa: F401
    except ModuleNotFoundError:
        pytest.importorskip("tomli", reason="Python 3.10 缺少 tomllib，需要 tomli")

    from e84 import load_toml

    cfg = load_toml(_MIN_TOML)
    assert cfg.interfaces[0].port("LP1").role is PortRole.LEFT


def test_load_file_by_extension(tmp_path):
    """``load_file`` 按扩展名分派 yaml / json / toml。"""

    try:
        import tomllib  # noqa: F401

        has_toml = True
    except ModuleNotFoundError:
        try:
            import tomli  # noqa: F401

            has_toml = True
        except ModuleNotFoundError:
            has_toml = False

    cases = {
        "cfg.yaml": _MIN_YAML,
        "cfg.json": json.dumps(_MIN_CONFIG),
    }
    if has_toml:
        cases["cfg.toml"] = _MIN_TOML

    for filename, text in cases.items():
        path = tmp_path / filename
        path.write_text(text, encoding="utf-8")
        cfg = load_file(path)
        assert cfg.interfaces[0].id == "PIO1", filename


def test_load_file_missing_and_bad_extension(tmp_path):
    with pytest.raises(ConfigError):
        load_file(tmp_path / "nope.yaml")

    bad = tmp_path / "cfg.ini"
    bad.write_text("[x]\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_file(bad)

    no_ext = tmp_path / "cfg"
    no_ext.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_file(no_ext)


def test_loads_rejects_unknown_format():
    with pytest.raises(ConfigError):
        loads("{}", "xml")


def test_load_bad_json_and_empty_yaml():
    with pytest.raises(ConfigError):
        loads("{ not json", "json")
    with pytest.raises(ConfigError):
        loads("", "yaml")


def test_validate_raises_by_default():
    """``load_dict`` 默认 ``validate=True``，校验失败直接抛 ConfigError。"""

    data = base_config_dict()
    data["interfaces"][0]["scenario"] = "interbay_passive_ohs"
    with pytest.raises(ConfigError):
        load_dict(data)
    # 关闭校验则可以通过（把错误留给调用方自行处理）
    cfg = load_dict(data, validate=False)
    assert cfg.interfaces[0].scenario is Scenario.INTERBAY_PASSIVE_OHS


# --------------------------------------------------------------------------- #
# EquipmentConfig.parse 的简写形式
# --------------------------------------------------------------------------- #
def test_parse_single_interface_shorthand():
    """根字典直接写 inputs/load_ports 时，等价于只有一个接口。"""

    root = copy.deepcopy(_MIN_CONFIG["interfaces"][0])
    root["backend"] = "sim"  # 非接口字段会被忽略
    cfg = EquipmentConfig.parse(root)
    assert len(cfg.interfaces) == 1
    assert cfg.interfaces[0].id == "PIO1"


def test_parse_requires_interfaces_or_shorthand():
    with pytest.raises(ConfigError):
        EquipmentConfig.parse({"name": "X"})


def test_sensorspec_shorthands():
    d = IoDefaults(active_high=False, pull="up", debounce_ms=3)

    assert SensorSpec.parse(None, "s", d).source == "constant"
    assert SensorSpec.parse(None, "s", d).value is False
    assert SensorSpec.parse(True, "s", d).source == "constant"
    assert SensorSpec.parse(True, "s", d).value is True

    single = SensorSpec.parse("K1", "s", d)
    assert single.source == "input" and single.channel == "K1"
    assert single.active_high is False and single.pull == "up"

    keys = SensorSpec.parse(["K0", "K1"], "s", d)
    assert keys.source == "keys" and keys.mode == "any"
    assert keys.channels == ("K0", "K1")

    full = SensorSpec.parse(
        {"source": "keys", "channels": ["A", "B"], "mode": "all"}, "s", d
    )
    assert full.mode == "all"

    with pytest.raises(ConfigError):
        SensorSpec.parse({"source": "keys"}, "s", d)
    with pytest.raises(ConfigError):
        SensorSpec.parse({"source": "magic"}, "s", d)


def test_channelspec_and_accessmode_shorthands():
    d = IoDefaults(active_high=False, pull="up", debounce_ms=1)
    spec = ChannelSpec.parse("IN9", "o", d)
    assert spec.channel == "IN9" and spec.active_high is False
    spec2 = ChannelSpec.parse({"pin": "GPIO17", "active_high": "yes"}, "o", d)
    assert spec2.channel == "GPIO17" and spec2.active_high is True
    with pytest.raises(ConfigError):
        ChannelSpec.parse({}, "o", d)

    assert AccessModeSpec.parse("manual", "am", d).value is AccessMode.MANUAL
    assert AccessModeSpec.parse("auto", "am", d).value is AccessMode.AUTOMATIC
    am = AccessModeSpec.parse("AM_IN", "am", d)
    assert am.source == "input" and am.channel == "AM_IN"
    with pytest.raises(ConfigError):
        AccessModeSpec.parse({"source": "input"}, "am", d)


def test_policy_parse_rejects_unknown_and_bad_values():
    with pytest.raises(ConfigError):
        PolicyConfig.parse({"no_such_knob": 1})
    with pytest.raises(ConfigError):
        PolicyConfig.parse({"on_timeout": "explode"})
    ok = PolicyConfig.parse({"on_timeout": "ho_abort", "not_ready_timeout_s": 1.5})
    assert ok.on_timeout == "ho_abort" and ok.not_ready_timeout_s == 1.5


def test_to_dict_is_json_serializable(cfg):
    data = cfg.to_dict()
    assert json.loads(json.dumps(data))["name"] == cfg.name
    assert jsonable(cfg.interfaces[0].topology) == "two_load_ports"


def test_example_config_loads(example_config_path):
    cfg = load_file(example_config_path)
    report = validate_config(cfg, raise_on_error=False)
    # 示例配置应当零错误（允许有告警）
    assert report.ok, report.errors
    assert cfg.interfaces[0].enable_simultaneous
    assert cfg.interfaces[0].enable_continuous


# --------------------------------------------------------------------------- #
# 必须报错的用例
# --------------------------------------------------------------------------- #
def _bad(mutate):
    data = base_config_dict()
    mutate(data)
    with pytest.raises(ConfigError):
        load_dict(data)


def _iface(d):
    return d["interfaces"][0]


def test_error_interbay_scenario_not_implemented():
    _bad(lambda d: _iface(d).__setitem__("scenario", "interbay_passive_ohs"))


def test_error_two_lp_requires_exactly_two_ports():
    _bad(lambda d: _iface(d).__setitem__("load_ports", _iface(d)["load_ports"][:1]))
    _bad(
        lambda d: _iface(d)["load_ports"].append({"id": "LP3", "role": "left"})
    )


def test_error_two_lp_roles_must_be_left_and_right():
    def mutate(d):
        for p in _iface(d)["load_ports"]:
            p["role"] = "left"

    _bad(mutate)

    def mutate2(d):
        _iface(d)["load_ports"][0]["role"] = "single"

    _bad(mutate2)


def test_error_one_lp_requires_one_port_and_not_right():
    def mutate(d):
        _iface(d)["topology"] = "one_load_port"

    _bad(mutate)  # 仍有 2 个载口

    def mutate2(d):
        _iface(d)["topology"] = "one_load_port"
        _iface(d)["load_ports"] = [{"id": "LP1", "role": "right"}]

    _bad(mutate2)


def test_error_duplicate_port_and_interface_ids():
    def dup_port(d):
        _iface(d)["load_ports"][1]["id"] = "LP1"

    _bad(dup_port)

    def dup_iface(d):
        d["interfaces"].append(copy.deepcopy(_iface(d)))

    _bad(dup_iface)


def test_error_missing_required_signals():
    for name in ("VALID", "CS_0", "CS_1", "TR_REQ", "BUSY", "COMPT", "CONT"):
        _bad(lambda d, n=name: _iface(d)["inputs"].pop(n, None))
    for name in ("READY", "HO_AVBL", "ES"):
        _bad(lambda d, n=name: _iface(d)["outputs"].pop(n, None))
    # 「请求」至少要有 L_REQ 或 U_REQ 之一
    _bad(lambda d: [_iface(d)["outputs"].pop("L_REQ"), _iface(d)["outputs"].pop("U_REQ")])


def test_error_continuous_requires_cont_but_feature_can_be_off():
    def no_cont(d):
        _iface(d)["inputs"].pop("CONT")

    _bad(no_cont)

    # 关闭 continuous 功能后 CONT 不再是必需信号
    def no_cont_feature_off(d):
        _iface(d)["inputs"].pop("CONT")
        _iface(d)["features"] = {"continuous": False}

    cfg = load_dict(_build(no_cont_feature_off))
    assert cfg.interfaces[0].enable_continuous is False


def _build(mutate):
    data = base_config_dict()
    mutate(data)
    return data


def test_error_timer_out_of_range():
    # TD0 只允许 0.1–0.2s（§6.3.2.3 表7）
    _bad(lambda d: _iface(d)["timers"].__setitem__("TD0", 0.5))
    _bad(lambda d: _iface(d)["timers"].__setitem__("TD0", 0.05))
    # TPx 范围 1–999s（§6.3.2.1 表6）
    _bad(lambda d: _iface(d)["timers"].__setitem__("TP1", 0))
    _bad(lambda d: _iface(d)["timers"].__setitem__("TP3", 1000))
    # 未知定时器名
    _bad(lambda d: _iface(d)["timers"].__setitem__("TP9", 2))


def test_error_output_channel_shared_by_others():
    # 除 L_REQ/U_REQ 之外不允许输出通道复用
    _bad(lambda d: _iface(d)["outputs"].__setitem__("READY", "OUT1"))
    _bad(lambda d: _iface(d)["outputs"].__setitem__("ES", "OUT7"))


def test_error_input_output_channel_conflict():
    _bad(lambda d: _iface(d)["outputs"].__setitem__("READY", "IN1"))
    _bad(lambda d: _iface(d)["inputs"].__setitem__("TR_REQ", "OUT4"))


def test_error_e84_channel_conflicts_with_sensor_channel():
    # 传感量通道与 E84 输入线冲突
    _bad(lambda d: _iface(d)["load_ports"][0].__setitem__("carrier_present", "IN1"))
    # 传感量通道与 E84 输出线冲突
    _bad(lambda d: _iface(d)["load_ports"][0].__setitem__("carrier_in_position", "OUT4"))


def test_error_sensor_channel_cross_port_conflict():
    def mutate(d):
        ports = _iface(d)["load_ports"]
        ports[0]["carrier_present"] = "SHARED_K"
        ports[1]["carrier_present"] = "SHARED_K"

    _bad(mutate)


def test_error_sensor_channel_polarity_conflict():
    def mutate(d):
        lp = _iface(d)["load_ports"][0]
        lp["carrier_present"] = {"source": "input", "channel": "K1", "active_high": True}
        lp["carrier_in_position"] = {
            "source": "input",
            "channel": "K1",
            "active_high": False,
        }

    _bad(mutate)


def test_error_input_signal_direction_wrong():
    # 输入表里出现被动侧输出信号；输出表里出现 A->P 信号
    _bad(lambda d: _iface(d)["inputs"].__setitem__("L_REQ", "IN9"))
    _bad(lambda d: _iface(d)["outputs"].__setitem__("VALID", "OUT9"))
    _bad(lambda d: _iface(d)["outputs"].__setitem__("NONSENSE", "OUT9"))


def test_error_equipment_level_fields():
    _bad(lambda d: d.__setitem__("schema_version", 2))
    _bad(lambda d: d.__setitem__("poll_interval_ms", 0))
    _bad(lambda d: d.__setitem__("boot_safe_hold_ms", -1))
    _bad(lambda d: d.__setitem__("interfaces", []))


def test_error_duplicate_input_channel():
    _bad(lambda d: _iface(d)["inputs"].__setitem__("TR_REQ", "IN1"))


# --------------------------------------------------------------------------- #
# 只应告警、不应报错的用例
# --------------------------------------------------------------------------- #
def _report(data):
    cfg = load_dict(data, validate=False)
    return validate_config(cfg, raise_on_error=False)


def test_warn_shared_demand_channel_is_allowed():
    """现场把 L_REQ/U_REQ 并在一条线上，是允许的接法，只能告警（原文并非共用引脚）。"""

    report = _report(base_config_dict())
    assert report.ok
    assert any("OUT1" in w and "共用" in w for w in report.warnings)


def test_warn_es_safe_on_is_not_an_error():
    """§6.4.6：安全态应为 OFF；配成 ON 只告警，不阻止启动。"""

    data = base_config_dict()
    _iface(data)["outputs"]["ES"] = {"channel": "OUT8", "safe_on": True}
    report = _report(data)
    assert report.ok
    assert any("safe_on" in w and "ES" in w for w in report.warnings)


def test_same_keys_for_any_and_all_is_normal():
    """同一载口把同一批键同时用于 any（检测到）与 all（完整落位）是正常现场做法。"""

    data = base_config_dict()
    lp = _iface(data)["load_ports"][0]
    lp["carrier_present"] = {
        "source": "keys",
        "channels": ["LP1_K0", "LP1_K1", "LP1_K2"],
        "mode": "any",
        "pull": "up",
    }
    lp["carrier_in_position"] = {
        "source": "keys",
        "channels": ["LP1_K0", "LP1_K1", "LP1_K2"],
        "mode": "all",
        "pull": "up",
    }
    report = _report(data)
    assert report.ok, report.errors
    # 不应出现"通道被不同载口共用"或"极性/上拉不一致"之类的错误
    assert not any("共用" in e for e in report.errors)


def test_warn_large_poll_interval_and_not_ready_timeout():
    data = base_config_dict()
    data["poll_interval_ms"] = 50
    _iface(data)["policy"]["not_ready_timeout_s"] = 2.0
    report = _report(data)
    assert report.ok
    assert any("poll_interval_ms" in w for w in report.warnings)
    assert any("not_ready_timeout_s" in w for w in report.warnings)


def test_warn_manual_mode_setting_ineffective():
    report = _report(base_config_dict())
    assert any("ho_avbl_when_manual" in w for w in report.warnings)


def test_warn_api_sources_and_custom_inputs():
    data = base_config_dict()
    lp = _iface(data)["load_ports"][0]
    lp["access_mode"] = {"source": "api"}
    lp["operation_intent"] = {"source": "api"}
    _iface(data)["inputs"]["GO"] = "IN9"
    _iface(data)["preconditions"] = ["VALID", "GO"]
    report = _report(data)
    assert report.ok
    text = " ".join(report.warnings)
    assert "访问模式来源是 api" in text
    assert "交接意图来源是 api" in text
    assert "GO" in text  # 现场自定义信号提示


def test_warn_unknown_extra_input_is_not_error():
    data = base_config_dict()
    _iface(data)["inputs"]["MY_SIGNAL"] = "IN10"
    report = _report(data)
    assert report.ok
    assert any("MY_SIGNAL" in w for w in report.warnings)


def test_warn_symbolic_precondition_not_bound():
    data = base_config_dict()
    _iface(data)["preconditions"] = ["VALID", "GO"]
    report = _report(data)
    assert report.ok
    assert any("GO" in w for w in report.warnings)


def test_error_standard_signal_as_precondition_but_unbound():
    data = base_config_dict()
    _iface(data)["preconditions"] = ["VALID", "BUSY"]  # BUSY 已绑定，OK
    report = _report(data)
    assert report.ok

    data2 = base_config_dict()
    _iface(data2)["preconditions"] = ["VALID", "TR_REQ", "AM_AVBL"]
    report2 = _report(data2)
    assert not report2.ok
    assert any("AM_AVBL" in e for e in report2.errors)
