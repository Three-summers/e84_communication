"""``e84.cli`` 命令行测试。

覆盖硬性要求里的行为：

* ``validate`` 对示例配置返回 0，对故意写坏的配置返回 1；
* ``validate --dump`` 的 stdout 可被 :func:`json.loads` 直接解析；
* ``selftest`` 返回 0；
* ``sim --job load:left`` 返回 0 且输出包含 ``PASS``；
* ``force`` 不带 ``--i-know-what-i-am-doing`` 时返回非 0 且不构造设备（不接触硬件）；
* ``python -m e84.cli --help`` 返回 0。

测试直接调用 :func:`e84.cli.main`，避免进程启动开销；只有 ``-m`` 入口用 subprocess。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from e84 import cli

ROOT = Path(__file__).resolve().parents[1]
CONFIG_2LP = ROOT / "configs" / "example_2lp_standard.yaml"
CONFIG_1LP = ROOT / "configs" / "example_1lp_standard.yaml"
CONFIG_VOC = ROOT / "configs" / "example_voc_compat.yaml"


def test_validate_example_2lp_returns_0():
    assert cli.main(["validate", str(CONFIG_2LP)]) == cli.EXIT_OK


def test_validate_example_1lp_returns_0():
    assert cli.main(["validate", str(CONFIG_1LP)]) == cli.EXIT_OK


def test_validate_broken_config_returns_1(tmp_path: Path):
    """故意写坏的配置：接口为空 + schema_version 不支持 -> 退出码 1。"""

    broken = tmp_path / "broken.json"
    broken.write_text(
        json.dumps({"schema_version": 99, "interfaces": []}),
        encoding="utf-8",
    )
    assert cli.main(["validate", str(broken)]) == cli.EXIT_CONFIG


def test_validate_missing_file_returns_1(tmp_path: Path):
    assert cli.main(["validate", str(tmp_path / "不存在.yaml")]) == cli.EXIT_CONFIG


def test_validate_dump_stdout_is_json(capsys):
    rc = cli.main(["validate", str(CONFIG_2LP), "--dump"])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_OK
    payload = json.loads(captured.out)  # 必须能直接解析
    assert payload["name"] == "EXAMPLE-ETCHER-01"
    assert payload["interfaces"][0]["id"] == "PIO1"
    assert payload["poll_interval_ms"] == 5.0


def test_validate_json_report(capsys):
    rc = cli.main(["validate", str(CONFIG_2LP), "--json"])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_OK
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_validate_show_map_mentions_shared_channel(capsys):
    rc = cli.main(["validate", str(CONFIG_2LP), "--show-map"])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "L_REQ" in out and "U_REQ" in out
    assert "共用通道" in out
    assert "载口传感量来源" in out


def test_selftest_returns_0():
    assert cli.main(["selftest"]) == cli.EXIT_OK


def test_selftest_with_config_returns_0():
    assert cli.main(["selftest", str(CONFIG_2LP)]) == cli.EXIT_OK


def test_validate_voc_compat_returns_0():
    """现场板卡示例（含自定义 GO 前置条件）也必须能通过配置校验。"""

    assert cli.main(["validate", str(CONFIG_VOC)]) == cli.EXIT_OK


def test_validate_voc_compat_show_map_returns_0(capsys):
    rc = cli.main(["validate", str(CONFIG_VOC), "--show-map"])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "GO" in out  # 现场自定义输入信号应出现在映射表里


def test_sim_voc_compat_without_go_fails_cleanly(capsys):
    """sim 不会驱动板级 GO，因此握手无法开始——预期 FAIL（退出码 2），但不能崩。"""

    rc = cli.main(["sim", str(CONFIG_VOC), "--job", "load:single"])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_RUNTIME
    assert "FAIL" in out
    assert "被动侧: idle" in out


def test_sim_load_left_passes(capsys):
    rc = cli.main(["sim", str(CONFIG_2LP), "--job", "load:left"])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "PASS" in out
    assert "被动状态" in out


def test_sim_continuous_unload_then_load_passes(capsys):
    rc = cli.main(
        [
            "sim",
            str(CONFIG_2LP),
            "--job",
            "unload:left",
            "--job",
            "load:left",
            "--continuous",
        ]
    )
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "PASS" in out


def test_sim_trace_out_writes_csv(tmp_path: Path, capsys):
    trace = tmp_path / "trace.csv"
    rc = cli.main(
        ["sim", str(CONFIG_2LP), "--job", "load:left", "--trace-out", str(trace)]
    )
    capsys.readouterr()
    assert rc == cli.EXIT_OK
    assert trace.is_file()
    lines = trace.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",")[0] == "t_s"
    assert "passive_state" in lines[0]
    assert len(lines) > 1


def test_sim_bad_job_returns_nonzero(capsys):
    rc = cli.main(["sim", str(CONFIG_2LP), "--job", "飞:left"])
    capsys.readouterr()
    assert rc == cli.EXIT_CONFIG


def test_force_without_confirmation_returns_nonzero_and_never_builds_equipment(
    capsys, monkeypatch
):
    """没有 --i-know-what-i-am-doing 时必须拒绝，并且**不构造 Equipment**（不碰硬件）。"""

    built = []

    class _Boom:
        def __init__(self, *args, **kwargs):  # pragma: no cover - 不应被调用
            built.append(args)

    monkeypatch.setattr(cli, "Equipment", _Boom)

    rc = cli.main(
        [
            "force",
            str(CONFIG_2LP),
            "--interface",
            "PIO1",
            "--signal",
            "READY",
            "--logical",
            "1",
        ]
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert built == []
    assert "i-know-what-i-am-doing" in captured.err


def test_faults_returns_0(capsys):
    rc = cli.main(["faults", str(CONFIG_2LP)])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "READY" in out and "HO_AVBL" in out


def test_no_arguments_prints_help_and_subcommands(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "validate" in out and "monitor" in out and "sim" in out
    assert "可用子命令" in out


def test_module_help_via_subprocess():
    """``python -m e84.cli --help`` 必须返回 0。"""

    result = subprocess.run(
        [sys.executable, "-m", "e84.cli", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "validate" in result.stdout


def test_version_flag_via_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "e84.cli", "--version"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "E84-0301" in (result.stdout + result.stderr)


def test_unknown_subcommand_returns_1():
    """参数错误统一使用退出码 1（而不是 argparse 默认的 2）。"""

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["不存在的子命令"])
    assert excinfo.value.code == cli.EXIT_CONFIG
