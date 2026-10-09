"""信号跳变追踪测试（CSV / JSONL、只写变化、flush_every、控制器集成）。

覆盖设计约束：

* 追踪文件用于现场排故与离线复现：长表 ``timestamp_s,interface,source,state,name,value``
  对任意信号集合都成立；
* **只有发生变化的信号才会被写出**，长时间运行不会因每拍重复写入而膨胀；
* ``flush_every`` 控制落盘频率；
* 逻辑信号（``source="output"``）与物理通道电平（``source="channel"``）都要可追踪，
  这样"协议说 ON、引脚却是 OFF"这类电气问题才能被事后发现（§6.4.4 表9）。
"""

from __future__ import annotations

import csv
import json
import pathlib

import pytest

from e84 import Equipment, SimIO, VirtualClock
from e84.trace import TraceRecorder


def _read_csv(path: pathlib.Path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def test_csv_header_and_rows(tmp_path):
    path = tmp_path / "trace.csv"
    with TraceRecorder(str(path), fmt="csv") as rec:
        written = rec.record(
            timestamp=0.0,
            interface_id="PIO1",
            state="idle",
            signals={"HO_AVBL": True, "ES": True},
        )
        assert written == 2
        rec.record(
            timestamp=0.1,
            interface_id="PIO1",
            state="select",
            signals={"HO_AVBL": True, "ES": True},  # 无变化 -> 不写
        )
        assert rec.rows_written == 2

    assert path.read_text(encoding="utf-8").splitlines()[0] == (
        "timestamp_s,interface,source,state,name,value"
    )
    rows = _read_csv(path)
    assert len(rows) == 2
    assert rows[0]["timestamp_s"] == "0.000000"
    assert rows[0]["interface"] == "PIO1"
    assert rows[0]["source"] == "output"
    assert rows[0]["name"] in ("HO_AVBL", "ES")
    assert rows[0]["value"] in ("0", "1")


def test_only_changes_are_written(tmp_path):
    path = tmp_path / "trace.csv"
    with TraceRecorder(str(path)) as rec:
        assert rec.record(timestamp=0.0, interface_id="I", state="s",
                          signals={"A": False, "B": False}) == 2
        # 完全重复 -> 0
        assert rec.record(timestamp=0.1, interface_id="I", state="s",
                          signals={"A": False, "B": False}) == 0
        # 只有 A 变化 -> 1
        assert rec.record(timestamp=0.2, interface_id="I", state="s",
                          signals={"A": True, "B": False}) == 1
        assert rec.rows_written == 3


def test_force_and_reset_baseline(tmp_path):
    path = tmp_path / "trace.csv"
    with TraceRecorder(str(path)) as rec:
        rec.record(timestamp=0.0, interface_id="I", state="s", signals={"A": True})
        assert rec.record(timestamp=0.1, interface_id="I", state="s",
                          signals={"A": True}) == 0
        # force=True 绕过变更检测
        assert rec.record(timestamp=0.2, interface_id="I", state="s",
                          signals={"A": True}, force=True) == 1
        # 重置基线后，同样的值会被当作"变化"再写一次
        rec.reset_baseline()
        assert rec.record(timestamp=0.3, interface_id="I", state="s",
                          signals={"A": True}) == 1
        assert rec.rows_written == 3


def test_channel_rows_recorded_with_channel_source(tmp_path):
    path = tmp_path / "trace.csv"
    with TraceRecorder(str(path)) as rec:
        rec.record(
            timestamp=0.0,
            interface_id="PIO1",
            state="idle",
            signals={"HO_AVBL": True},
        )
        rec.record(
            timestamp=0.0,
            interface_id="PIO1",
            state="idle",
            signals={},
            source="channel",
            channels={"OUT7": False},
        )
    rows = _read_csv(path)
    by_source = {row["source"] for row in rows}
    assert by_source == {"output", "channel"}
    channel_row = next(r for r in rows if r["source"] == "channel")
    assert channel_row["name"] == "OUT7"
    assert channel_row["value"] == "0"


def test_different_interfaces_and_signals_have_independent_baselines(tmp_path):
    path = tmp_path / "trace.csv"
    with TraceRecorder(str(path)) as rec:
        assert rec.record(timestamp=0.0, interface_id="A", state="s",
                          signals={"X": True}) == 1
        assert rec.record(timestamp=0.0, interface_id="B", state="s",
                          signals={"X": True}) == 1
        assert rec.rows_written == 2


# --------------------------------------------------------------------------- #
# JSONL
# --------------------------------------------------------------------------- #
def test_jsonl_output(tmp_path):
    path = tmp_path / "trace.jsonl"
    with TraceRecorder(str(path), fmt="jsonl") as rec:
        rec.record(timestamp=1.25, interface_id="PIO1", state="transfer",
                   signals={"L_REQ": True, "READY": True})
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2
    payload = json.loads(lines[0])
    assert set(payload) == {"t", "interface", "source", "state", "name", "value"}
    assert payload["t"] == 1.25
    assert payload["source"] == "output"
    assert payload["value"] == 1
    assert {json.loads(line)["name"] for line in lines} == {"L_REQ", "READY"}


def test_jsonl_channel_rows(tmp_path):
    path = tmp_path / "trace.jsonl"
    with TraceRecorder(str(path), fmt="jsonl") as rec:
        rec.record(timestamp=0.0, interface_id="I", state="s", signals={},
                   source="channel", channels={"OUT1": True})
    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["source"] == "channel"
    assert payload["name"] == "OUT1"


# --------------------------------------------------------------------------- #
# flush_every
# --------------------------------------------------------------------------- #
def test_flush_every_batches_writes(tmp_path):
    path = tmp_path / "trace.csv"
    rec = TraceRecorder(str(path), fmt="csv", flush_every=3)
    try:
        rec.record(timestamp=0.0, interface_id="I", state="s", signals={"A": True})
        rec.record(timestamp=0.1, interface_id="I", state="s", signals={"B": True})
        # 未达到 flush_every，仍在用户态缓冲里
        assert path.stat().st_size == 0
        rec.record(timestamp=0.2, interface_id="I", state="s", signals={"C": True})
        # 达到 flush_every，已经落盘
        assert path.stat().st_size > 0
    finally:
        rec.close()

    rows = _read_csv(path)
    assert {row["name"] for row in rows} == {"A", "B", "C"}


def test_flush_every_minimum_is_one(tmp_path):
    rec = TraceRecorder(str(tmp_path / "t.csv"), flush_every=0)
    assert rec.flush_every == 1
    rec.close()


# --------------------------------------------------------------------------- #
# 生命周期与错误
# --------------------------------------------------------------------------- #
def test_disabled_recorder_writes_nothing(tmp_path):
    path = tmp_path / "trace.csv"
    rec = TraceRecorder(str(path), enabled=False)
    assert rec.record(timestamp=0.0, interface_id="I", state="s",
                      signals={"A": True}) == 0
    assert not path.exists()
    rec.close()


def test_invalid_format_rejected(tmp_path):
    with pytest.raises(ValueError):
        TraceRecorder(str(tmp_path / "t.xml"), fmt="xml")


def test_parent_directory_is_created(tmp_path):
    path = tmp_path / "nested" / "deep" / "trace.csv"
    with TraceRecorder(str(path)):
        pass
    assert path.is_file()


def test_close_is_idempotent(tmp_path):
    rec = TraceRecorder(str(tmp_path / "t.csv"))
    rec.close()
    rec.close()
    # 关闭后 record 安全返回 0
    assert rec.record(timestamp=0.0, interface_id="I", state="s",
                      signals={"A": True}) == 0


def test_context_manager_closes(tmp_path):
    path = tmp_path / "t.csv"
    with TraceRecorder(str(path)) as rec:
        rec.record(timestamp=0.0, interface_id="I", state="s", signals={"A": True})
    assert path.read_text(encoding="utf-8").count("\n") == 2  # 表头 + 1 行


# --------------------------------------------------------------------------- #
# 控制器集成：逻辑信号与物理通道都应被追踪
# --------------------------------------------------------------------------- #
def test_controller_records_output_and_channel_rows(tmp_path, cfg):
    path = tmp_path / "controller_trace.csv"
    trace = TraceRecorder(str(path), fmt="csv", flush_every=1)
    io, clk = SimIO("trace-sim"), VirtualClock()
    equipment = Equipment(cfg, clock=clk, ios={"PIO1": io}, trace=trace, validate=False)
    controller = equipment.controller("PIO1")

    clk.advance(0.02)
    controller.poll(now=clk.now())
    clk.advance(0.02)
    controller.poll(now=clk.now())
    equipment.stop(now=clk.now())   # 关闭追踪文件

    rows = _read_csv(path)
    sources = {row["source"] for row in rows}
    assert "output" in sources
    assert "channel" in sources, "物理通道电平必须被追踪（否则电气问题无法事后定位）"
    # 两个输出通道各发生两次跳变（首拍 ON、停机回安全态 OFF）；
    # 中间那拍空闲时**没有**重复写同一通道，正是"只写变化"的效果。
    channel_rows = [r for r in rows if r["source"] == "channel"]
    channel_names = {r["name"] for r in channel_rows}
    assert {"OUT7", "OUT8"} <= channel_names
    assert len(channel_rows) == 2 * len(channel_names)
    assert "HO_AVBL" in {r["name"] for r in rows if r["source"] == "output"}
