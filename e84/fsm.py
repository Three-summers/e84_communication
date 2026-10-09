"""★ 被动侧协议核心：纯逻辑状态机。

本模块**不接触任何 I/O、时钟、线程或硬件**。它只做一件事：

    给定上一拍的状态 + 本拍的 :class:`~e84.model.Inputs`，算出本拍的
    :class:`~e84.model.Outputs`、状态迁移、事件与故障。

因此 fig_10 – fig_20 的全部时序都可以在虚拟时钟上逐拍精确回放与断言。

## 状态与标准条款对应

| 状态 | 标准依据 | 本拍输出（``REQ``/``READY``/``HO_AVBL``/``ES``） |
|---|---|---|
| ``DISABLED`` | §6.4.6 失效安全 | 0/0/0/0（全 OFF） |
| ``IDLE`` | §6.2.2.1 起点 | 0/0/1/1 |
| ``SELECT`` | §6.2.2.1(1)(2)、注3 | 0/0/1/1 |
| ``REQ_ON`` | §6.2.2.1(3)(4)、TP1 | 1/0/1/1 |
| ``WAIT_BUSY`` | §6.2.2.1(5)(6)、TP2 | 1/1/1/1 |
| ``TRANSFER`` | §6.2.2.1(7)、TP3 | 1→0/1/1/1 |
| ``AWAIT_COMPT`` | §6.2.2.1(8)(9)(10)、TP4、注4 | 0/1/1/1 |
| ``CLOSING`` | §6.2.2.1(11)(12)(13)、TP5、注5 | 0/0/1/1 |
| ``CONT_NEXT`` | §6.2.4.3、TP6/TD1 | 0/0/1/1 |
| ``HO_ABORT`` | §6.2.5.2/§6.2.5.3 | 0/0/**0**/1 |
| ``FAULT_WAIT_CLOSE`` | 相关信息1 R1-1.1.2.1 | 保持/0/0/0 |
| ``FAULT_LATCHED`` | §6.3.3.1 + 相关信息1 | 0/0/0/0 |

## 关键实现约定

1. **进度优先于超时**：每拍先看期望信号是否已到，再判定时器是否到期。
   这样"事件恰好发生在截止时刻"不会被误判为超时。
2. **上升沿触发**：进入握手靠 ``VALID`` 上升沿，不靠电平，避免在残留的
   ``VALID`` 上"半路接上"一次握手的尾巴。
3. **``CONT`` 边沿采样**：按 §6.2.4.3，``CONT`` 在 ``BUSY`` 上升沿采样，
   并带一个可配置的稳定延时（``cont_sample_delay_ms``）以容忍两根线的偏斜。
4. **方向锁定**：装载/卸载方向在 ``SELECT`` 一次性确定并锁定，
   之后不再读实时传感器（载具可能已被取走）。
5. **同时交接的与语义**：两载口共用 PI/O 时，「请求」是所选中载口集合的
   逻辑与（§6.2.3.2 第 3/4 点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from e84.config.model import InterfaceConfig
from e84.events import Event, EventType
from e84.fault import Fault, FaultCode
from e84.model import (
    AccessMode,
    Inputs,
    Op,
    Outputs,
    PortRole,
    PortSnapshot,
    State,
    Topology,
)
from e84.timers import TimerId, TimerSet

__all__ = ["StepResult", "PassiveFsm"]


class _Raise(Exception):
    """内部信号：本拍判定为故障。"""

    def __init__(self, code: FaultCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StepResult:
    """一次 :meth:`PassiveFsm.step` 的结果。"""

    state: State
    outputs: Outputs
    events: Tuple[Event, ...] = ()
    fault: Optional[Fault] = None
    selected_port_ids: Tuple[str, ...] = ()
    op: Op = Op.UNKNOWN
    interlock_engaged: bool = False


_TIMEOUT_FAULT: Mapping[TimerId, FaultCode] = {
    TimerId.TP1: FaultCode.TP1_TIMEOUT,
    TimerId.TP2: FaultCode.TP2_TIMEOUT,
    TimerId.TP3: FaultCode.TP3_TIMEOUT,
    TimerId.TP4: FaultCode.TP4_TIMEOUT,
    TimerId.TP5: FaultCode.TP5_TIMEOUT,
    TimerId.TP6: FaultCode.TP6_TIMEOUT,
}


class PassiveFsm:
    """被动侧（装备端）协议状态机。"""

    def __init__(self, config: InterfaceConfig) -> None:
        self.cfg = config
        self.policy = config.policy
        self.topology = config.topology
        self.interface_id = config.id

        #: 可编程定时器（§6.3.2.1）。只使用被动侧 ``TPx``，另加可选的 ``TD1`` 监视。
        overrides: Dict[TimerId, float] = {}
        for name, value in config.timers.items():
            try:
                overrides[TimerId(name)] = float(value)
            except ValueError:  # pragma: no cover - 配置校验已拦截
                continue
        self.timers = TimerSet.from_typical(owner="passive", overrides=overrides)
        if TimerId.TD1 in overrides:
            self.timers.set_duration(TimerId.TD1, overrides[TimerId.TD1])

        self.state: State = State.DISABLED
        self.fault: Optional[Fault] = None
        self.op: Op = Op.UNKNOWN
        self.selected: Tuple[str, ...] = ()
        self.interlock_engaged = False

        # ---- 边沿与锁存 ----
        self._prev_valid = False
        self._prev_armed = False
        self._prev_busy = False
        self._prev_state: State = State.DISABLED
        self._prev_ho_avbl: Optional[bool] = None
        self._prev_es: Optional[bool] = None
        self._prev_interlock = False
        self._last_outputs = Outputs()

        # ---- 本次握手/本段的上下文 ----
        self._select_started_at = 0.0
        self._busy_rise_at = 0.0
        self._cont_sampled = False
        self._segment_cont = False
        self._batch_active = False
        self._armed_since: Optional[float] = None
        self._not_ready_reported = False

        # ---- 中止 / 故障 ----
        self._abort_closed = False
        self._abort_reason = ""
        self._abort_demand = Op.UNKNOWN
        self._held_outputs = Outputs()
        self._fault_announced = False

    # ==================================================================== #
    # 生命周期
    # ==================================================================== #
    def enable(self, now: float) -> None:
        """启用接口。有未清除的故障时进入 ``FAULT_LATCHED``。"""

        self.timers.stop_all()
        if self.fault is not None:
            self.state = State.FAULT_LATCHED
        else:
            self.state = State.IDLE
            self._armed_since = None
        self._prev_state = self.state

    def disable(self, now: float) -> None:
        """停机：进入上电安全态（全部输出 OFF，含 ``ES`` 与 ``HO_AVBL``）。"""

        self.timers.stop_all()
        self._reset_selection()
        self._abort_demand = Op.UNKNOWN
        self.interlock_engaged = False
        self.state = State.DISABLED
        # 让只读查询（outputs()/snapshot()）如实反映安全态，而不是停留在停机前的值
        self._last_outputs = Outputs()
        self._prev_ho_avbl = False
        self._prev_es = False

    def clear_fault(self, now: float, *, force: bool = False) -> bool:
        """清除锁存故障。

        默认只允许在握手已闭合（``FAULT_LATCHED``）时清除；
        ``force=True`` 可强制清除（供人工复位用，需自行确认现场安全）。
        """

        if self.fault is None:
            return False
        if self.state is State.FAULT_WAIT_CLOSE and not force:
            return False
        self.fault = None
        self._fault_announced = False
        self._held_outputs = Outputs()
        self._reset_selection()
        self.state = State.IDLE
        self._armed_since = None
        return True

    def abort(self, now: float, reason: str = "operator") -> None:
        """主动中止一次握手：拉低 ``HO_AVBL``，等对方闭合后恢复。"""

        self._abort_closed = False
        self._abort_demand = self.op if self.op in (Op.LOAD, Op.UNLOAD) else Op.UNKNOWN
        self._reset_selection()
        self.timers.stop_all()
        self.state = State.HO_ABORT
        self._abort_reason = reason

    def raise_fault(self, code: FaultCode, message: str, now: float) -> None:
        """外部（例如执行机构）上报故障，进入锁存流程。

        本方法**不发事件**（协议核心不持有事件总线）；调用方
        :class:`e84.controller.PiOController` 会立刻发出 ``FAULT_RAISED``，
        因此这里把 ``_fault_announced`` 置位，避免下一拍重复上报。
        """

        if self.fault is not None:
            return
        self._fault_announced = True
        self.fault = Fault(code=code, message=message, state=self.state.value, timestamp=now)
        self._held_outputs = self._last_outputs
        self._reset_selection()
        self.timers.stop_all()
        self.state = State.FAULT_WAIT_CLOSE

    # ==================================================================== #
    # 主循环
    # ==================================================================== #
    def step(self, inp: Inputs) -> StepResult:
        """推进一拍。"""

        events: List[Event] = []
        try:
            outputs = self._step_inner(inp, events)
        except _Raise as raised:
            self._enter_fault(raised.code, raised.message, inp, events)
            outputs = self._fault_outputs(inp)

        # ---- 派生事件：状态变化 / HO_AVBL / ES ----
        self._emit_derived_events(inp, events, outputs)

        self._prev_valid = inp.valid
        self._prev_armed = inp.valid and self._preconditions_ok(inp)
        self._prev_busy = inp.busy
        self._last_outputs = outputs

        return StepResult(
            state=self.state,
            outputs=outputs,
            events=tuple(events),
            fault=self.fault,
            selected_port_ids=self.selected,
            op=self.op,
            interlock_engaged=self.interlock_engaged,
        )

    # ------------------------------------------------------------------ #
    def _step_inner(self, inp: Inputs, events: List[Event]) -> Outputs:
        now = inp.now
        pre_ok = self._preconditions_ok(inp)
        # 握手触发的是「VALID ∧ 全部前置条件」这个整体的上升沿，而不是 VALID 单独的上升沿。
        # 原因：现场前置条件（例如板级 GO 联锁）可能在 VALID 之后才成立；若只看 VALID 的边沿，
        # 这次握手会被永久漏掉，直到 VALID 先落下再抬起——那是现场排故里很难查的一类"卡死"。
        # 标准场景下前置条件只有 VALID，两者等价。
        armed = inp.valid and pre_ok
        armed_rising = armed and not self._prev_armed
        available = self._available(inp)
        es_out = self._es_output(inp)
        state = self.state

        # ---------------------------------------------------------- DISABLED
        if state is State.DISABLED:
            return Outputs()

        # 交接干涉区的冻结**完全由 BUSY 电平决定**（§6.1 表1 BUSY：
        # "只要本信号为 ON，被动设备就不应在交接干涉区内执行任何机械动作"）。
        # 不能挂在"握手闭合"或"状态复位"上——那会让上层的机构比标准要求多冻住一段时间，
        # 更糟的是：对方撤了 BUSY 却卡在后续步骤时，机构会被冻到超时为止。
        # 故障期间同样要跟随 BUSY：对方机构可能还在干涉区里。
        if self.policy.interlock_freeze:
            self.interlock_engaged = bool(inp.busy)

        # -------------------------------------------------------------- IDLE
        if state is State.IDLE:
            out = self._make_outputs(ho=available, es=es_out)
            if armed:
                if armed_rising:
                    self._select_started_at = now
                    self._not_ready_reported = False
                    self.selected = ()
                    self.op = Op.UNKNOWN
                    events.append(
                        self._event(
                            EventType.HANDSHAKE_STARTED,
                            inp,
                            "握手前提（VALID ∧ 前置条件）上升沿，握手开始",
                            data={"preconditions": list(self.cfg.preconditions)},
                        )
                    )
                    if available:
                        self.state = State.SELECT
                    else:
                        self._begin_abort("HO_UNAVAILABLE", inp, events)
                else:
                    # 前提已成立却没有上升沿：说明我们是在一次握手中途进入 IDLE 的，
                    # 不能"半路接上"，超时后按故障处理。
                    if self._armed_since is None:
                        self._armed_since = now
                    elif now - self._armed_since > self.policy.valid_stuck_timeout_s:
                        raise _Raise(
                            FaultCode.VALID_STUCK,
                            f"VALID（及前置条件）持续 ON 超过 "
                            f"{self.policy.valid_stuck_timeout_s}s 却没有上升沿；"
                            "无法确定这是一次新的握手",
                        )
            else:
                self._armed_since = None
            return out

        # ------------------------------------------------------------ SELECT
        if state is State.SELECT:
            return self._handle_select(inp, events, available, es_out)

        # ------------------------------------------------------------ REQ_ON
        if state is State.REQ_ON:
            return self._handle_req_on(inp, events, available, es_out)

        # --------------------------------------------------------- WAIT_BUSY
        if state is State.WAIT_BUSY:
            return self._handle_wait_busy(inp, events, available, es_out)

        # ---------------------------------------------------------- TRANSFER
        if state is State.TRANSFER:
            return self._handle_transfer(inp, events, available, es_out)

        # ------------------------------------------------------- AWAIT_COMPT
        if state is State.AWAIT_COMPT:
            return self._handle_await_compt(inp, events, available, es_out)

        # ----------------------------------------------------------- CLOSING
        if state is State.CLOSING:
            return self._handle_closing(inp, events, available, es_out)

        # --------------------------------------------------------- CONT_NEXT
        if state is State.CONT_NEXT:
            return self._handle_cont_next(inp, events, available, es_out, armed_rising)

        # --------------------------------------------------------- HO_ABORT
        if state is State.HO_ABORT:
            return self._handle_ho_abort(inp, events, available, es_out)

        # ------------------------------------------------- FAULT_WAIT_CLOSE
        if state is State.FAULT_WAIT_CLOSE:
            if self.fault is not None and not self._fault_announced:
                # 外部通过 raise_fault() 上报的故障，在这里补发一次事件
                self._fault_announced = True
                events.append(
                    self._event(
                        EventType.FAULT_RAISED,
                        inp,
                        f"{self.fault.code.value}: {self.fault.message}",
                        data={"code": self.fault.code.value, "clause": self.fault.clause},
                    )
                )
            if not inp.valid:
                self.state = State.FAULT_LATCHED
                self.timers.stop_all()
                events.append(
                    self._event(
                        EventType.HANDSHAKE_CLOSED, inp, "故障后握手已闭合，等待显式清除"
                    )
                )
            return self._fault_outputs(inp)

        # ---------------------------------------------------- FAULT_LATCHED
        if state is State.FAULT_LATCHED:
            return self._fault_outputs(inp)

        raise AssertionError(f"未处理的状态: {self.state}")  # pragma: no cover

    # ==================================================================== #
    # 各状态处理
    # ==================================================================== #
    def _handle_select(
        self, inp: Inputs, events: List[Event], available: bool, es_out: bool
    ) -> Outputs:
        now = inp.now
        if not available:
            return self._begin_abort("HO_UNAVAILABLE", inp, events)

        if not inp.valid:
            # 对方在被动方断言请求之前就闭合了握手（例如它自己发现 HO_AVBL=OFF，
            # 或车已撤离）。这不算错误：撤回并回 IDLE。
            return self._withdraw_and_idle(inp, events, "VALID 在选口阶段被撤销")

        if not self.selected:
            roles, problem = self._decode_cs(inp)
            if problem == "invalid":
                if self.policy.invalid_cs == "ignore":
                    events.append(
                        self._event(
                            EventType.NOT_READY, inp, "CS_0/CS_1 组合非法，按策略忽略本次握手"
                        )
                    )
                    return self._withdraw_and_idle(inp, events, "CS 组合非法（已忽略）")
                raise _Raise(
                    FaultCode.INVALID_CS_COMBINATION,
                    f"CS_0={inp.cs0} CS_1={inp.cs1} 在 {self.topology.value} 拓扑下非法",
                )
            if problem == "simultaneous_unsupported":
                message = "对方要求同时交接，但本接口未启用该能力（§6.1.2.4）"
                if self.policy.unsupported_simultaneous == "ho_abort":
                    return self._begin_abort("SIMULTANEOUS_NOT_SUPPORTED", inp, events)
                raise _Raise(FaultCode.SIMULTANEOUS_NOT_SUPPORTED, message)

            ports = inp.ports_with_roles(roles)
            if not ports:
                raise _Raise(
                    FaultCode.INVALID_CS_COMBINATION,
                    f"CS 指定了 {[r.value for r in roles]}，但接口没有对应载口",
                )
            self.selected = tuple(p.port_id for p in ports)
            self._select_started_at = now
            events.append(
                self._event(
                    EventType.PORTS_SELECTED,
                    inp,
                    "选中载口: " + ", ".join(self.selected),
                    port_ids=self.selected,
                    data={"roles": [r.value for r in roles], "cs0": inp.cs0, "cs1": inp.cs1},
                )
            )

        ports = self._selected_snapshots(inp)
        op, ready, problem = self._select_operation(ports)
        if ready:
            self.op = op
            self.timers.start(TimerId.TP1, now)
            events.append(
                self._event(
                    EventType.DEMAND_ASSERTED,
                    inp,
                    f"断言 {'L_REQ' if op is Op.LOAD else 'U_REQ'}（方向由传感器/上层意图确定）",
                    data={"op": op.value},
                )
            )
            self.state = State.REQ_ON
            return self._make_outputs(demand=op, ho=available, es=es_out)

        if problem == "intent_mismatch":
            raise _Raise(
                FaultCode.INTENT_MISMATCH,
                "上层指定的交接方向与载口传感器状态矛盾",
            )

        # 尚未就绪：在宽限期内等待，超时则拉低 HO_AVBL 让对方向标准定义的方式退让
        if not self._not_ready_reported:
            self._not_ready_reported = True
            events.append(
                self._event(
                    EventType.NOT_READY,
                    inp,
                    "载口尚未就绪，暂不断言请求",
                    data={"selected": list(self.selected)},
                )
            )
        if now - self._select_started_at >= self.policy.not_ready_timeout_s:
            return self._begin_abort("PORT_NOT_READY", inp, events)
        return self._make_outputs(ho=available, es=es_out)

    def _handle_req_on(
        self, inp: Inputs, events: List[Event], available: bool, es_out: bool
    ) -> Outputs:
        # 进度优先：对方已请求交接
        if inp.tr_req:
            if self._ports_operable(inp):
                self.timers.start(TimerId.TP2, inp.now)
                events.append(
                    self._event(EventType.READY_ASSERTED, inp, "载口机构就绪，断言 READY")
                )
                self.state = State.WAIT_BUSY
                return self._make_outputs(demand=self.op, ready=True, ho=available, es=es_out)
            # 机构尚未就绪：继续等待，由对方的 TA2（表5，典型 2s）决定后续

        if not available:
            return self._begin_abort("HO_UNAVAILABLE", inp, events)
        if self._precondition_lost(inp):
            return self._on_precondition_lost(inp, events)
        if self.timers.expired(TimerId.TP1, inp.now):
            return self._timeout(TimerId.TP1, inp, events)
        return self._make_outputs(demand=self.op, ho=available, es=es_out)

    def _handle_wait_busy(
        self,
        inp: Inputs,
        events: List[Event],
        available: bool,
        es_out: bool,
    ) -> Outputs:
        if inp.busy:
            self._busy_rise_at = inp.now
            delay_s = max(0.0, self.policy.cont_sample_delay_ms) / 1000.0
            self._cont_sampled = delay_s <= 0.0
            self._segment_cont = bool(inp.cont) if self._cont_sampled else False
            self.timers.start(TimerId.TP3, inp.now)
            if self._cont_sampled and self._segment_cont and not self._batch_active:
                self._batch_active = True
                events.append(
                    self._event(
                        EventType.BATCH_STARTED, inp, "CONT=ON：这是一次连续交接的首段"
                    )
                )
            events.append(
                self._event(
                    EventType.TRANSFER_STARTED,
                    inp,
                    "BUSY=ON：对方机构进入交接干涉区",
                    data={"cont": self._segment_cont if self._cont_sampled else None},
                )
            )
            self.state = State.TRANSFER
            return self._make_outputs(demand=self.op, ready=True, ho=available, es=es_out)

        if not available:
            return self._begin_abort("HO_UNAVAILABLE", inp, events)
        if self._precondition_lost(inp):
            return self._on_precondition_lost(inp, events)
        if self.timers.expired(TimerId.TP2, inp.now):
            return self._timeout(TimerId.TP2, inp, events)
        return self._make_outputs(demand=self.op, ready=True, ho=available, es=es_out)

    def _handle_transfer(
        self, inp: Inputs, events: List[Event], available: bool, es_out: bool
    ) -> Outputs:
        now = inp.now
        delay_s = max(0.0, self.policy.cont_sample_delay_ms) / 1000.0
        if not self._cont_sampled and now - self._busy_rise_at >= delay_s:
            self._segment_cont = bool(inp.cont)
            self._cont_sampled = True
            if self._segment_cont and not self._batch_active:
                self._batch_active = True
                events.append(
                    self._event(
                        EventType.BATCH_STARTED, inp, "CONT=ON：这是一次连续交接的首段"
                    )
                )

        # 载具到位（装载）或被取走（卸载）→ 撤下请求线（§6.2.2.1 第7步）
        if self._carrier_settled(inp):
            events.append(
                self._event(
                    EventType.CARRIER_SETTLED,
                    inp,
                    "载具已到位" if self.op is Op.LOAD else "载具已被取走",
                    port_ids=self.selected,
                )
            )
            events.append(
                self._event(EventType.DEMAND_RELEASED, inp, "撤回请求线")
            )
            self.timers.start(TimerId.TP4, now)
            self.state = State.AWAIT_COMPT
            return self._make_outputs(ready=True, ho=available, es=es_out)

        if self.timers.expired(TimerId.TP3, now):
            return self._timeout(TimerId.TP3, inp, events)
        if not inp.busy and self.policy.busy_before_req_off == "fault":
            raise _Raise(
                FaultCode.BUSY_BEFORE_REQ_OFF,
                "请求线仍为 ON，对方却已撤下 BUSY（§6.2.2.1 第8步要求先看到请求线 OFF）",
            )
        return self._make_outputs(demand=self.op, ready=True, ho=available, es=es_out)

    def _handle_await_compt(
        self, inp: Inputs, events: List[Event], available: bool, es_out: bool
    ) -> Outputs:
        if inp.compt:
            # 注4：只有在 COMPT 置 ON 之后才校验 BUSY / TR_REQ
            if inp.busy or inp.tr_req:
                message = (
                    "收到 COMPT 时 BUSY/TR_REQ 仍为 ON，违反 §6.2.2.1 第8/9/10 步顺序"
                )
                if self.policy.compt_before_busy_off == "fault":
                    raise _Raise(FaultCode.COMPT_BEFORE_BUSY_OFF, message)
                events.append(self._event(EventType.NOT_READY, inp, message))
            self.timers.start(TimerId.TP5, inp.now)
            events.append(
                self._event(EventType.COMPT_RECEIVED, inp, "COMPT=ON：对方完成交接")
            )
            events.append(
                self._event(EventType.READY_RELEASED, inp, "撤回 READY（§6.2.2.1 第11步）")
            )
            self.state = State.CLOSING
            return self._make_outputs(ho=available, es=es_out)

        if self.timers.expired(TimerId.TP4, inp.now):
            return self._timeout(TimerId.TP4, inp, events)
        return self._make_outputs(ready=True, ho=available, es=es_out)

    def _handle_closing(
        self, inp: Inputs, events: List[Event], available: bool, es_out: bool
    ) -> Outputs:
        closed = not inp.valid and (not self.policy.require_compt_off or not inp.compt)
        if closed:
            was_continuous = self._segment_cont
            self.timers.stop(TimerId.TP5)
            events.append(
                self._event(EventType.SEGMENT_COMPLETED, inp, "本段交接完成")
            )
            events.append(
                self._event(
                    EventType.HANDSHAKE_CLOSED, inp, "VALID=OFF：握手闭合（§6.2.2.1 第13步）"
                )
            )
            self._reset_selection()
            if was_continuous:
                self.timers.start(TimerId.TP6, inp.now)
                if TimerId.TD1 in self.timers.durations:
                    self.timers.start(TimerId.TD1, inp.now)
                self.state = State.CONT_NEXT
            else:
                if self._batch_active:
                    self._batch_active = False
                    events.append(
                        self._event(EventType.BATCH_ENDED, inp, "连续交接结束（末段）")
                    )
                self.state = State.IDLE
                self._armed_since = None
            return self._make_outputs(ho=available, es=es_out)

        if self.timers.expired(TimerId.TP5, inp.now):
            return self._timeout(TimerId.TP5, inp, events)
        return self._make_outputs(ho=available, es=es_out)

    def _handle_cont_next(
        self,
        inp: Inputs,
        events: List[Event],
        available: bool,
        es_out: bool,
        armed_rising: bool,
    ) -> Outputs:
        if not available:
            if self._batch_active:
                self._batch_active = False
                events.append(
                    self._event(EventType.BATCH_ENDED, inp, "本机转为不可用，连续交接终止")
                )
            self.timers.stop_all()
            self.state = State.IDLE
            self._armed_since = None
            return self._make_outputs(ho=False, es=es_out)

        if armed_rising:
            self.timers.stop(TimerId.TP6)
            if TimerId.TD1 in self.timers.durations:
                elapsed = self.timers.duration_of(TimerId.TD1) - (
                    self.timers.remaining(TimerId.TD1, inp.now) or 0.0
                )
                events.append(
                    self._event(
                        EventType.HANDSHAKE_STARTED,
                        inp,
                        "连续交接下一段开始",
                        data={"td1_observed_s": round(elapsed, 6)},
                    )
                )
            else:  # pragma: no cover - 未配置 TD1
                events.append(
                    self._event(EventType.HANDSHAKE_STARTED, inp, "连续交接下一段开始")
                )
            self._select_started_at = inp.now
            self._not_ready_reported = False
            self.selected = ()
            self.op = Op.UNKNOWN
            self.state = State.SELECT
            return self._make_outputs(ho=available, es=es_out)

        if self.timers.expired(TimerId.TP6, inp.now):
            events.append(
                self._event(
                    EventType.TIMER_EXPIRED,
                    inp,
                    f"TP6 超时（{self.timers.duration_of(TimerId.TP6)}s）：连续交接未继续",
                )
            )
            if self.policy.tp6_timeout == "fault":
                raise _Raise(
                    FaultCode.TP6_TIMEOUT,
                    "连续交接中场等待下一次 VALID 超时",
                )
            if self._batch_active:
                self._batch_active = False
                events.append(
                    self._event(EventType.BATCH_ENDED, inp, "TP6 超时，连续交接结束")
                )
            self.state = State.IDLE
            self._armed_since = None
            return self._make_outputs(ho=available, es=es_out)

        return self._make_outputs(ho=available, es=es_out)

    def _handle_ho_abort(
        self, inp: Inputs, events: List[Event], available: bool, es_out: bool
    ) -> Outputs:
        """``HO_AVBL`` 中止：保持请求线 → 等握手闭合 → 撤请求线 → 再恢复 ``HO_AVBL``。

        顺序依据：

        * **规范图 19**（检查窗口 b 的示例）：``VALID``↓ 与 ``CS``↓/``TR_REQ``↓ 在前，
          随后 ``L_REQ``↓，最后 ``HO_AVBL``↑；
        * §6.2.5.2/§6.2.5.3 正文："The passive equipment turns the HO_AVBL signal ON
          after the VALID signal is turned to OFF (**L_REQ or U_REQ must be set to OFF**)";
        * 商业仿真器 GCI E84 Emulator 的 Passive Mode Functionality Test G 用同样的
          步骤顺序（6 拉低 HO_AVBL → 7-9 验证对方撤 VALID/TR_REQ/CS → 10 才撤 L_REQ
          → 11 才恢复 HO_AVBL）。

        换言之：**中止期间不得提前撤下请求线**。窗口 a（请求线尚未断言）时请求线本来就
        是 OFF，所以只有窗口 b 才会看到"保持"。
        """

        if not inp.valid:
            self._abort_closed = True

        if not self._abort_closed:
            # 握手还没闭合：按图 19 保持请求线，只把 HO_AVBL 拉低
            return self._make_outputs(
                demand=self._abort_demand, ho=False, es=es_out
            )

        # 握手已闭合：先撤请求线（必须早于 HO_AVBL 恢复），下一拍再恢复 HO_AVBL
        if self._abort_demand is not Op.UNKNOWN:
            self._abort_demand = Op.UNKNOWN
            events.append(
                self._event(
                    EventType.DEMAND_RELEASED,
                    inp,
                    "握手闭合后撤下请求线（图 19：VALID↓ → 请求线↓ → HO_AVBL↑）",
                )
            )
            return self._make_outputs(ho=False, es=es_out)

        if available:
            self.timers.stop_all()
            self.state = State.IDLE
            self._armed_since = None
            return self._make_outputs(ho=True, es=es_out)
        return self._make_outputs(ho=False, es=es_out)

    # ==================================================================== #
    # 判定辅助
    # ==================================================================== #
    def _decode_cs(self, inp: Inputs) -> Tuple[Tuple[PortRole, ...], Optional[str]]:
        """把 ``CS_0``/``CS_1`` 解码为被选中的载口角色集合。"""

        if self.topology is Topology.ONE_LP:
            if self.policy.single_lp_require_cs1_off and inp.cs1:
                return (), "invalid"
            if not inp.cs0:
                return (), "invalid"
            return (self.cfg.load_ports[0].role,), None

        if inp.cs0 and inp.cs1:
            if not self.cfg.enable_simultaneous:
                return (), "simultaneous_unsupported"
            return (PortRole.LEFT, PortRole.RIGHT), None
        if inp.cs0:
            return (PortRole.LEFT,), None
        if inp.cs1:
            return (PortRole.RIGHT,), None
        return (), "invalid"

    def _select_operation(
        self, ports: Sequence[PortSnapshot]
    ) -> Tuple[Op, bool, Optional[str]]:
        """确定交接方向并判断请求线能否断言。

        返回 ``(op, ready, problem)``，``problem`` ∈ ``{None, "not_ready", "intent_mismatch"}``。
        """

        if not ports:
            return Op.UNKNOWN, False, "not_ready"

        expected = {p.expected_op for p in ports if p.expected_op is not Op.UNKNOWN}
        if len(expected) > 1:
            return Op.UNKNOWN, False, "intent_mismatch"
        exp = next(iter(expected)) if expected else None

        # 同时交接时，请求线是「所有选中载口都就绪」的与语义（§6.2.3.2 第3点）
        load_ok = all(p.can_load() for p in ports)
        unload_ok = all(p.can_unload() for p in ports)

        if exp is Op.LOAD:
            if load_ok:
                return Op.LOAD, True, None
            if unload_ok:
                if self.policy.intent_mismatch == "follow_sensor":
                    return Op.UNLOAD, True, None
                return Op.UNKNOWN, False, "intent_mismatch"
            return Op.UNKNOWN, False, "not_ready"
        if exp is Op.UNLOAD:
            if unload_ok:
                return Op.UNLOAD, True, None
            if load_ok:
                if self.policy.intent_mismatch == "follow_sensor":
                    return Op.LOAD, True, None
                return Op.UNKNOWN, False, "intent_mismatch"
            return Op.UNKNOWN, False, "not_ready"

        if load_ok and not unload_ok:
            return Op.LOAD, True, None
        if unload_ok and not load_ok:
            return Op.UNLOAD, True, None
        return Op.UNKNOWN, False, "not_ready"

    def _selected_snapshots(self, inp: Inputs) -> Tuple[PortSnapshot, ...]:
        if not self.selected:
            return ()
        wanted = set(self.selected)
        return tuple(p for p in inp.ports if p.port_id in wanted)

    def _ports_operable(self, inp: Inputs) -> bool:
        """所选载口的机构是否已经可以承载交接。"""

        ports = self._selected_snapshots(inp)
        return bool(ports) and all(p.usable for p in ports)

    def _carrier_settled(self, inp: Inputs) -> bool:
        """载具是否已完成本次方向要求的物理到位。"""

        ports = self._selected_snapshots(inp)
        return bool(ports) and all(p.carrier_settled_for(self.op) for p in ports)

    def _preconditions_ok(self, inp: Inputs) -> bool:
        return all(inp.signal(name) for name in self.cfg.preconditions)

    def _precondition_lost(self, inp: Inputs) -> bool:
        if not self.policy.abort_on_precondition_loss:
            return False
        return not self._preconditions_ok(inp)

    def _available(self, inp: Inputs) -> bool:
        """``HO_AVBL`` 的逻辑取值（§6.1 表1）。"""

        if self.fault is not None:
            return False
        if not inp.es_ok or not inp.external_ho_ok:
            return False
        scope = self.policy.ho_avbl_scope
        if scope == "selected_ports" and self.selected:
            wanted = set(self.selected)
            relevant = tuple(p for p in inp.ports if p.port_id in wanted)
        else:
            relevant = tuple(inp.ports)
        for port in relevant:
            if not port.available:
                return False
            if (
                not self.policy.ho_avbl_when_manual
                and port.access_mode is AccessMode.MANUAL
            ):
                return False
        return True

    def _es_output(self, inp: Inputs) -> bool:
        """``ES`` 的逻辑取值。``ON`` = 正常运行，``OFF`` = 请求对方立即停止。"""

        if not inp.es_ok:
            return False
        if self.fault is not None and self.policy.es_latched_on_fault:
            return False
        return True

    # ==================================================================== #
    # 迁移动作
    # ==================================================================== #
    def _make_outputs(
        self, *, demand: Op = Op.UNKNOWN, ready: bool = False, ho: bool = False, es: bool = False
    ) -> Outputs:
        return Outputs(
            l_req=demand is Op.LOAD,
            u_req=demand is Op.UNLOAD,
            ready=ready,
            ho_avbl=ho,
            es=es,
        )

    def _idle_outputs(self, available: bool, es_out: bool) -> Outputs:
        return self._make_outputs(ho=available, es=es_out)

    def _reset_selection(self) -> None:
        self.selected = ()
        self.op = Op.UNKNOWN
        self._cont_sampled = False
        self._segment_cont = False
        self._not_ready_reported = False

    def _withdraw_and_idle(
        self, inp: Inputs, events: List[Event], reason: str, *, keep_batch: bool = False
    ) -> Outputs:
        had_demand = self._last_outputs.demand_on
        had_ready = self._last_outputs.ready
        self.timers.stop_all()
        self._reset_selection()
        if had_demand:
            events.append(self._event(EventType.DEMAND_RELEASED, inp, reason))
        if had_ready:
            events.append(self._event(EventType.READY_RELEASED, inp, reason))
        events.append(
            self._event(EventType.HANDSHAKE_CLOSED, inp, f"握手结束：{reason}")
        )
        if self._batch_active and not keep_batch:
            self._batch_active = False
            events.append(self._event(EventType.BATCH_ENDED, inp, reason))
        self.state = State.IDLE
        self._armed_since = None
        return self._idle_outputs(self._available(inp), self._es_output(inp))

    def _begin_abort(
        self, reason: str, inp: Inputs, events: List[Event]
    ) -> Outputs:
        if self.state is not State.HO_ABORT:
            events.append(self._event(EventType.HO_ABORTED, inp, f"拉低 HO_AVBL：{reason}"))
        self._abort_reason = reason
        self._abort_closed = not inp.valid
        # 规范图 19：中止期间请求线要**保持**，等握手闭合后才撤、然后才恢复 HO_AVBL。
        # 所以这里先把当前的请求方向记下来，别让它随 _reset_selection() 一起丢掉。
        self._abort_demand = self.op if self.op in (Op.LOAD, Op.UNLOAD) else Op.UNKNOWN
        self.timers.stop_all()
        self._reset_selection()
        self.state = State.HO_ABORT
        return self._make_outputs(demand=self._abort_demand, ho=False, es=self._es_output(inp))

    def _on_precondition_lost(self, inp: Inputs, events: List[Event]) -> Outputs:
        """握手前置条件（如现场 ``GO``）丢失。"""

        if inp.valid:
            return self._begin_abort("PRECONDITION_LOST", inp, events)
        return self._withdraw_and_idle(inp, events, "前置条件丢失且 VALID 已落下")

    def _timeout(self, timer_id: TimerId, inp: Inputs, events: List[Event]) -> Outputs:
        seconds = self.timers.duration_of(timer_id)
        events.append(
            self._event(
                EventType.TIMER_EXPIRED,
                inp,
                f"{timer_id.value} 超时（{seconds}s）",
                data={"timer": timer_id.value, "seconds": seconds},
            )
        )
        if self.policy.on_timeout == "ho_abort":
            return self._begin_abort(f"{timer_id.value}_TIMEOUT", inp, events)
        raise _Raise(_TIMEOUT_FAULT[timer_id], f"{timer_id.value} 超时（{seconds}s）")

    def _enter_fault(
        self, code: FaultCode, message: str, inp: Inputs, events: List[Event]
    ) -> None:
        self.fault = Fault(
            code=code,
            message=message,
            state=self.state.value,
            timestamp=inp.now,
            context={
                "selected": list(self.selected),
                "op": self.op.value,
                "inputs": {
                    "valid": inp.valid,
                    "cs0": inp.cs0,
                    "cs1": inp.cs1,
                    "tr_req": inp.tr_req,
                    "busy": inp.busy,
                    "compt": inp.compt,
                    "cont": inp.cont,
                    "es_ok": inp.es_ok,
                    "external_ho_ok": inp.external_ho_ok,
                },
                "outputs": self._last_outputs.as_dict(),
            },
        )
        # 相关信息 1 R1-1.1.2.1：出错时其余信号保持在出错时刻的状态
        self._held_outputs = self._last_outputs
        self.timers.stop_all()
        self._reset_selection()
        self.state = State.FAULT_WAIT_CLOSE
        self._fault_announced = True
        events.append(
            self._event(
                EventType.FAULT_RAISED,
                inp,
                f"{code.value}: {message}",
                data={"code": code.value, "clause": self.fault.clause},
            )
        )

    def _fault_outputs(self, inp: Inputs) -> Outputs:
        """故障态输出画像（相关信息 1）。"""

        latched = self.state is State.FAULT_LATCHED
        if self.policy.request_hold_on_fault and not latched:
            demand = Op.LOAD if self._held_outputs.l_req else (
                Op.UNLOAD if self._held_outputs.u_req else Op.UNKNOWN
            )
        else:
            demand = Op.UNKNOWN
        return self._make_outputs(
            demand=demand,
            ready=False,
            ho=False,
            es=self._es_output(inp),
        )

    # ==================================================================== #
    # 事件
    # ==================================================================== #
    def _event(
        self,
        event_type: EventType,
        inp: Inputs,
        message: str = "",
        *,
        port_ids: Sequence[str] = (),
        data: Optional[Mapping[str, object]] = None,
    ) -> Event:
        return Event(
            type=event_type,
            timestamp=inp.now,
            interface_id=self.interface_id,
            state=self.state.value,
            port_ids=tuple(port_ids) or tuple(self.selected),
            message=message,
            data=dict(data or {}),
        )

    def _emit_derived_events(
        self, inp: Inputs, events: List[Event], outputs: Outputs
    ) -> None:
        if self.state is not self._prev_state:
            events.append(
                self._event(
                    EventType.STATE_CHANGED,
                    inp,
                    f"{self._prev_state.value} -> {self.state.value}",
                    data={"from": self._prev_state.value, "to": self.state.value},
                )
            )
            self._prev_state = self.state
        if self._prev_ho_avbl is not None and self._prev_ho_avbl != outputs.ho_avbl:
            events.append(
                self._event(
                    EventType.HO_AVBL_CHANGED,
                    inp,
                    f"HO_AVBL -> {'ON' if outputs.ho_avbl else 'OFF'}",
                    data={"value": outputs.ho_avbl},
                )
            )
        if self._prev_es is not None and self._prev_es != outputs.es:
            events.append(
                self._event(
                    EventType.ES_CHANGED,
                    inp,
                    f"ES -> {'ON(正常)' if outputs.es else 'OFF(请求停止)'}",
                    data={"value": outputs.es},
                )
            )
        # 干涉区冻结的**两个边沿**都必须发事件：上层是"听到 INTERLOCK_ENGAGED 才冻结
        # 机构"的，释放时不通知就会把机构**永久冻住**（或只能靠上层自己猜 BUSY 的下降，
        # 等于把标准明确要求的互锁交给应用层猜）。
        if not self._prev_interlock and self.interlock_engaged:
            events.append(
                self._event(
                    EventType.INTERLOCK_ENGAGED,
                    inp,
                    "BUSY=ON：禁止本机在交接干涉区内动作（§6.1 表1 BUSY）",
                )
            )
        elif self._prev_interlock and not self.interlock_engaged:
            events.append(
                self._event(
                    EventType.INTERLOCK_RELEASED,
                    inp,
                    "BUSY=OFF：已退出交接干涉区，可恢复本机机械动作（§6.1 表1 BUSY）",
                )
            )
        self._prev_interlock = self.interlock_engaged
        self._prev_ho_avbl = outputs.ho_avbl
        self._prev_es = outputs.es

    # ==================================================================== #
    # 只读快照
    # ==================================================================== #
    def snapshot(self, now: float) -> Dict[str, object]:
        """返回用于监视/排故的只读快照。"""

        return {
            "interface": self.interface_id,
            "state": self.state.value,
            "op": self.op.value,
            "selected_ports": list(self.selected),
            "fault": None if self.fault is None else {
                "code": self.fault.code.value,
                "message": self.fault.message,
                "clause": self.fault.clause,
                "at": self.fault.timestamp,
            },
            "timers": self.timers.snapshot(now),
            "outputs": self._last_outputs.as_dict(),
            "interlock_engaged": self.interlock_engaged,
            "abort_demand": self._abort_demand.value,
            "transfer_in_progress": self.transfer_in_progress,
            "handshake_active": self.handshake_active,
            "batch_active": self._batch_active,
            "segment_continuous": self._segment_cont,
        }

    #: SEMI E87 §11.1.2 意义上的"载具交接进行中"。
    #: E87 Table 8 把 AUTO 交接的边界定义为：「PIO 的 READY 信号有效」→「PIO 指示交接完成」。
    _TRANSFER_STATES = (State.WAIT_BUSY, State.TRANSFER, State.AWAIT_COMPT)
    #: 从选口到握手闭合的整个区间（期间不宜改变访问模式/可用性等配置）。
    _HANDSHAKE_STATES = (
        State.SELECT, State.REQ_ON, State.WAIT_BUSY, State.TRANSFER,
        State.AWAIT_COMPT, State.CLOSING, State.CONT_NEXT,
    )

    @property
    def transfer_in_progress(self) -> bool:
        """是否正处于一次载具交接之中（SEMI E87 Table 8 的 AUTO 交接区间）。

        E87 §11.1.2：访问模式"may be switched at anytime ... **except** when the Load
        Port Reservation State Model ... is in the RESERVED state or **during carrier
        transfer**"。上层应在改动访问模式/载口可用性之前先看这个属性。
        """

        return self.state in self._TRANSFER_STATES

    @property
    def handshake_active(self) -> bool:
        """握手是否已经打开且尚未闭合（比 :attr:`transfer_in_progress` 更宽）。"""

        return self.state in self._HANDSHAKE_STATES

    @property
    def batch_active(self) -> bool:
        """是否正处于一次连续交接批次中。"""

        return self._batch_active

    @property
    def last_outputs(self) -> Outputs:
        """最近一拍算出的输出（逻辑值）。"""

        return self._last_outputs

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"PassiveFsm({self.interface_id!r}, state={self.state.value}, "
            f"op={self.op.value}, ports={list(self.selected)})"
        )
