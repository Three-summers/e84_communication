"""运行器测试（ManualRunner / ThreadRunner）。

覆盖设计约束：

* ``stop()`` 幂等，并且**退出前必须把设备置为安全态**
  （撤回 ``READY``/``L_REQ``/``U_REQ``，``ES``/``HO_AVBL`` 到失效安全方向，§6.4.6）；
* ``ManualRunner`` 由调用方决定推进节奏，可嵌入自有实时循环；
* ``ThreadRunner`` 后台线程按固定周期轮询，周期可配；线程异常不得杀死轮询；
* 停机后不得留下任何 ON 的输出。

本文件里 ``ThreadRunner`` 使用**真实时钟**，因此周期取小值（2ms）以保证整套测试仍然很快。
"""

from __future__ import annotations

import time

import pytest

from e84 import Equipment, ManualRunner, SimIO, VirtualClock
from e84.runner import ThreadRunner
from tests.support import assert_outputs_safe


@pytest.fixture
def manual(cfg):
    """(VirtualClock + SimIO + Equipment + ManualRunner) 组合。"""

    io = SimIO("runner-manual")
    clk = VirtualClock()
    equipment = Equipment(cfg, clock=clk, ios={"PIO1": io}, validate=False)
    return equipment, ManualRunner(equipment), clk, io


# --------------------------------------------------------------------------- #
# ManualRunner
# --------------------------------------------------------------------------- #
def test_manual_runner_idempotent_start_stop(manual):
    equipment, runner, clk, io = manual
    assert runner.running is False

    runner.start(now=clk.now())
    runner.start(now=clk.now())          # 幂等
    assert runner.running is True

    runner.stop(now=clk.now())
    runner.stop(now=clk.now())           # 幂等
    assert runner.running is False
    assert equipment.controller("PIO1").state.value == "disabled"


def test_manual_runner_poll_requires_explicit_start(manual):
    """``poll()`` 不隐式启动：残留循环不得让设备悄悄恢复运行。"""

    equipment, runner, clk, io = manual
    assert runner.running is False

    clk.advance(0.02)
    with pytest.raises(RuntimeError):
        runner.poll(now=clk.now())       # 未 start 时明确报错

    runner.start(now=clk.now())
    clk.advance(0.02)
    runner.poll(now=clk.now())
    assert equipment.controller("PIO1").poll_count == 1

    clk.advance(0.02)
    runner.poll(now=clk.now())
    assert equipment.controller("PIO1").poll_count == 2

    # 停机后再次 poll 同样必须报错，而不是悄悄重新启用
    runner.stop(now=clk.now())
    with pytest.raises(RuntimeError):
        runner.poll(now=clk.now())
    assert_outputs_safe(equipment.controller("PIO1"))


def test_manual_runner_context_manager(manual):
    equipment, runner, clk, io = manual
    with runner as r:
        assert r.running is True
        clk.advance(0.02)
        r.poll(now=clk.now())
    assert runner.running is False
    assert_outputs_safe(equipment.controller("PIO1"))


def test_manual_runner_stop_drives_safe_state(manual):
    equipment, runner, clk, io = manual
    c = equipment.controller("PIO1")
    runner.start(now=clk.now())
    clk.advance(0.02)
    runner.poll(now=clk.now())
    assert c.outputs().ho_avbl is True   # 运行中 HO_AVBL 为 ON

    runner.stop(now=clk.now())
    assert c.state.value == "disabled"
    # 物理输出必须全部回到安全态（逻辑输出对象不再代表物理态，故只校验通道）
    assert_outputs_safe(c)


# --------------------------------------------------------------------------- #
# ThreadRunner（真实时钟）
# --------------------------------------------------------------------------- #
def _wait_until(predicate, timeout_s: float = 1.0, interval_s: float = 0.002) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def test_thread_runner_starts_polls_and_stops(cfg):
    started = time.monotonic()
    io = SimIO("runner-thread")
    equipment = Equipment(cfg, ios={"PIO1": io}, validate=False)
    controller = equipment.controller("PIO1")
    runner = ThreadRunner(equipment, poll_interval_ms=2)

    try:
        assert runner.running is False
        runner.start()
        runner.start()  # 幂等
        assert runner.running is True

        # 轮询计数必须增长
        assert _wait_until(lambda: controller.poll_count >= 3, timeout_s=1.0), (
            f"轮询未推进：poll_count={controller.poll_count}"
        )
        assert runner.stats["period_s"] == pytest.approx(0.002)

        # 停机幂等，并且把所有输出带回安全态
        assert runner.stop() is True
        assert runner.running is False
        assert runner.stop() is True          # 再一次也必须是幂等的
        assert_outputs_safe(controller)
        assert controller.state.value == "disabled"
    finally:
        runner.stop()

    assert time.monotonic() - started < 2.0


def test_thread_runner_pause_resume(cfg):
    io = SimIO("runner-thread-pause")
    equipment = Equipment(cfg, ios={"PIO1": io}, validate=False)
    controller = equipment.controller("PIO1")
    runner = ThreadRunner(equipment, poll_interval_ms=2)
    try:
        runner.start()
        assert _wait_until(lambda: controller.poll_count >= 2, timeout_s=1.0)

        runner.pause()
        assert runner.paused is True
        time.sleep(0.03)                      # 让在途的一次 poll 结束
        frozen = controller.poll_count
        time.sleep(0.05)
        assert controller.poll_count == frozen, "暂停期间不应继续轮询"

        runner.resume()
        assert runner.paused is False
        assert _wait_until(
            lambda: controller.poll_count > frozen, timeout_s=1.0
        ), "恢复后轮询应继续"
    finally:
        runner.stop()


def test_thread_runner_stop_without_start_is_safe(cfg):
    io = SimIO("runner-thread-idle")
    equipment = Equipment(cfg, ios={"PIO1": io}, validate=False)
    runner = ThreadRunner(equipment, poll_interval_ms=2)
    assert runner.running is False
    assert runner.stop() is True             # 从未启动也能安全停机
    assert runner.running is False


def test_thread_runner_context_manager(cfg):
    io = SimIO("runner-thread-ctx")
    equipment = Equipment(cfg, ios={"PIO1": io}, validate=False)
    controller = equipment.controller("PIO1")
    with ThreadRunner(equipment, poll_interval_ms=2) as runner:
        assert runner.running is True
        assert _wait_until(lambda: controller.poll_count >= 1, timeout_s=1.0)
    assert runner.running is False
    assert_outputs_safe(controller)
