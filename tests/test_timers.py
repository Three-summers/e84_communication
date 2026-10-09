"""定时器语义测试。

覆盖标准条款：

* §6.3.2.1 —— ``TAx`` 属主动设备、``TPx`` 属被动设备；除 ``TD0`` 外范围 1–999s；
  所有设定值应可由用户编程；
* §6.3.2.1 表6 —— ``TP3``/``TP4`` 典型值 **60s**，其余 ``TPx`` 典型 2s；
* §6.3.2.2/§6.3.2.3 表7 —— ``TD0`` 范围 0.1–0.2s、典型 0.1s；``TD1`` 范围 1–999s、典型 1s；
* 实现约定 —— 使用**绝对截止时刻**而非累加；``expired()`` 每个定时器只触发一次。
"""

from __future__ import annotations

import pytest

from e84.timers import (
    ACTIVE_TIMERS,
    PASSIVE_TIMERS,
    TIMER_SPECS,
    TimerId,
    TimerSet,
)


# --------------------------------------------------------------------------- #
# 规格表
# --------------------------------------------------------------------------- #
def test_timer_specs_typical_values():
    # 表6：TP3/TP4 典型 60s，其余 TPx 典型 2s
    assert TIMER_SPECS[TimerId.TP1].typical_s == 2
    assert TIMER_SPECS[TimerId.TP2].typical_s == 2
    assert TIMER_SPECS[TimerId.TP3].typical_s == 60
    assert TIMER_SPECS[TimerId.TP4].typical_s == 60
    assert TIMER_SPECS[TimerId.TP5].typical_s == 2
    assert TIMER_SPECS[TimerId.TP6].typical_s == 2
    # 表7：TD0 0.1–0.2s（典型 0.1），TD1 1–999s（典型 1）
    assert TIMER_SPECS[TimerId.TD0].minimum_s == 0.1
    assert TIMER_SPECS[TimerId.TD0].maximum_s == 0.2
    assert TIMER_SPECS[TimerId.TD0].typical_s == 0.1
    assert TIMER_SPECS[TimerId.TD1].minimum_s == 1
    assert TIMER_SPECS[TimerId.TD1].maximum_s == 999


def test_timer_specs_ranges_and_owners():
    for tid in PASSIVE_TIMERS:
        spec = TIMER_SPECS[tid]
        assert spec.owner == "passive"
        assert (spec.minimum_s, spec.maximum_s) == (1, 999)  # §6.3.2.1
    for tid in ACTIVE_TIMERS:
        spec = TIMER_SPECS[tid]
        assert spec.owner == "active"
        if tid is TimerId.TD0:
            assert (spec.minimum_s, spec.maximum_s) == (0.1, 0.2)
        else:
            assert (spec.minimum_s, spec.maximum_s) == (1, 999)


def test_timer_spec_validate_boundaries():
    spec = TIMER_SPECS[TimerId.TP1]
    spec.validate(1)
    spec.validate(999)
    with pytest.raises(ValueError):
        spec.validate(0.999)
    with pytest.raises(ValueError):
        spec.validate(1000)

    td0 = TIMER_SPECS[TimerId.TD0]
    td0.validate(0.1)
    td0.validate(0.2)
    with pytest.raises(ValueError):
        td0.validate(0.5)
    with pytest.raises(ValueError):
        td0.validate(0.0)


# --------------------------------------------------------------------------- #
# 构造与运行时编程
# --------------------------------------------------------------------------- #
def test_from_typical_passive_only():
    timers = TimerSet.from_typical(owner="passive")
    assert set(timers.durations) == set(PASSIVE_TIMERS)
    assert TimerId.TA1 not in timers.durations
    assert timers.duration_of(TimerId.TP3) == 60


def test_from_typical_overrides():
    timers = TimerSet.from_typical(owner="passive", overrides={TimerId.TP1: 5})
    assert timers.duration_of(TimerId.TP1) == 5
    assert timers.duration_of(TimerId.TP2) == 2


def test_constructor_rejects_out_of_range():
    with pytest.raises(ValueError):
        TimerSet({TimerId.TP1: 0.5})


def test_set_duration_runtime_programming():
    """§6.3.2.1：所有设定值应可由用户编程。"""

    timers = TimerSet.from_typical(owner="passive")
    timers.set_duration(TimerId.TP2, 12.5)
    assert timers.duration_of(TimerId.TP2) == 12.5

    with pytest.raises(ValueError):
        timers.set_duration(TimerId.TP2, 0)
    with pytest.raises(ValueError):
        timers.set_duration(TimerId.TP2, 1000)
    with pytest.raises(ValueError):
        timers.set_duration(TimerId.TD0, 0.5)


# --------------------------------------------------------------------------- #
# 死线语义
# --------------------------------------------------------------------------- #
def test_start_and_expired_deadline():
    timers = TimerSet.from_typical(owner="passive", overrides={TimerId.TP1: 2})
    timers.start(TimerId.TP1, now=0.0)
    assert timers.running(TimerId.TP1)

    assert timers.expired(TimerId.TP1, now=1.999) is False
    # 「恰好到截止时刻」也算到期（FSM 采用"进度优先"，这里只测定时器本身）
    assert timers.expired(TimerId.TP1, now=2.0) is True
    assert not timers.running(TimerId.TP1)


def test_expired_triggers_only_once():
    timers = TimerSet.from_typical(owner="passive", overrides={TimerId.TP1: 1})
    timers.start(TimerId.TP1, now=0.0)
    assert timers.expired(TimerId.TP1, now=1.0) is True
    # 到期后自动注销，不会重复触发
    assert timers.expired(TimerId.TP1, now=100.0) is False
    assert not timers.running(TimerId.TP1)


def test_expired_on_non_running_timer_is_false():
    timers = TimerSet.from_typical(owner="passive")
    assert timers.expired(TimerId.TP2, now=1000.0) is False


def test_restart_resets_deadline():
    timers = TimerSet.from_typical(owner="passive", overrides={TimerId.TP5: 2})
    timers.start(TimerId.TP5, now=0.0)
    timers.start(TimerId.TP5, now=1.0)  # 重新计时
    assert timers.expired(TimerId.TP5, now=2.5) is False
    assert timers.expired(TimerId.TP5, now=3.0) is True


def test_remaining_and_snapshot():
    timers = TimerSet.from_typical(owner="passive", overrides={TimerId.TP1: 2})
    assert timers.remaining(TimerId.TP1, now=0.0) is None
    timers.start(TimerId.TP1, now=10.0)
    assert timers.remaining(TimerId.TP1, now=11.0) == pytest.approx(1.0)
    assert timers.remaining(TimerId.TP1, now=12.0) == 0.0  # 已到期
    snap = timers.snapshot(now=11.0)
    assert snap["TP1"] == pytest.approx(1.0)
    assert snap["TP3"] is None  # 未运行 -> None
    assert timers.duration_of(TimerId.TP3) == 60


def test_stop_and_stop_all():
    timers = TimerSet.from_typical(owner="passive", overrides={TimerId.TP1: 1})
    timers.start(TimerId.TP1, now=0.0)
    timers.start(TimerId.TP2, now=0.0)
    timers.stop(TimerId.TP1)
    assert not timers.running(TimerId.TP1)
    assert not timers.expired(TimerId.TP1, now=5.0)
    timers.stop_all()
    assert not timers.running(TimerId.TP2)


def test_runtime_set_duration_after_start_uses_new_deadline():
    """修改设定值只影响后续 start（死线在启动时冻结）。"""

    timers = TimerSet.from_typical(owner="passive", overrides={TimerId.TP1: 2})
    timers.start(TimerId.TP1, now=0.0)
    timers.set_duration(TimerId.TP1, 10)
    assert timers.expired(TimerId.TP1, now=2.0) is True

    timers.start(TimerId.TP1, now=10.0)
    assert timers.expired(TimerId.TP1, now=19.9) is False
    assert timers.expired(TimerId.TP1, now=20.0) is True


# --------------------------------------------------------------------------- #
# due()
# --------------------------------------------------------------------------- #
def test_due_returns_and_deregisters_sorted():
    timers = TimerSet.from_typical(
        owner="passive", overrides={TimerId.TP1: 5, TimerId.TP2: 1, TimerId.TP5: 3}
    )
    timers.start(TimerId.TP1, now=0.0)  # 截止 5
    timers.start(TimerId.TP2, now=0.0)  # 截止 1
    timers.start(TimerId.TP5, now=0.0)  # 截止 3

    assert timers.due(now=0.5) == ()
    ready = timers.due(now=4.0)
    # 按到期先后排序：TP2(1) -> TP5(3)
    assert ready == (TimerId.TP2, TimerId.TP5)
    assert not timers.running(TimerId.TP2)
    assert not timers.running(TimerId.TP5)
    assert timers.running(TimerId.TP1)

    assert timers.due(now=5.0) == (TimerId.TP1,)
    assert timers.due(now=99.0) == ()


def test_due_is_idempotent():
    timers = TimerSet.from_typical(owner="passive", overrides={TimerId.TP1: 1})
    timers.start(TimerId.TP1, now=0.0)
    assert timers.due(now=1.0) == (TimerId.TP1,)
    assert timers.due(now=1.0) == ()
