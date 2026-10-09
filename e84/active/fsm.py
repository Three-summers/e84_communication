"""主动侧参考实现（**仅供自测/回放/对拖，不是产品代码**）。

被动侧库要能自证正确，就必须有一个"合法对手"来跟它握手。本模块按 SEMI E84-0301
实现主动设备侧的单次/同时/连续交接时序，包括：

* ``TD0`` 选口 → ``VALID``（§6.3.2.3）；
* ``TA1`` ``VALID``→请求线（表5）；``TA2`` ``TR_REQ``→``READY``；``TA3`` ``COMPT``→``READY`` OFF；
* ``BUSY`` 与请求线落下的先后（§6.2.2.1 第8步）；
* ``COMPT`` 之后释放 ``COMPT``/``VALID``/``CS_x``（第12步）；
* ``CONT`` 在**首段** ``BUSY``↑ 置 ON、在**末段** ``BUSY``↑ 置 OFF（§6.2.4.3）；
* 连续交接段间 ``VALID`` 落下再抬起，并遵守 ``TD1``（表7）；
* ``HO_AVBL`` 的两个检查窗口（§6.2.5.1 a/b）：发现对方拉低就结束握手。

电气层面由 :mod:`e84.testing.harness` 负责，这里只输出**逻辑**信号。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from e84.model import Op, PortRole
from e84.timers import TimerId, TimerSet

__all__ = [
    "TransferJob",
    "ActiveConfig",
    "ActiveInputs",
    "ActiveOutputs",
    "ActiveEvent",
    "ActiveStepResult",
    "ActiveFsm",
]


class ActivePhase(str, Enum):
    """主动侧阶段。"""

    DONE = "done"
    IDLE = "idle"
    SELECT = "select"
    VALID_ON = "valid_on"
    WAIT_DEMAND = "wait_demand"
    WAIT_READY = "wait_ready"
    TRANSFER = "transfer"
    AWAIT_DEMAND_OFF = "await_demand_off"
    COMPT_ON = "compt_on"
    WAIT_READY_OFF = "wait_ready_off"
    NEXT_JOB = "next_job"
    ABORTED = "aborted"
    FAULT = "fault"


@dataclass(frozen=True)
class TransferJob:
    """一次交接作业。"""

    roles: Tuple[PortRole, ...]
    op: Op

    @classmethod
    def load(cls, *roles: PortRole) -> "TransferJob":
        return cls(tuple(roles), Op.LOAD)

    @classmethod
    def unload(cls, *roles: PortRole) -> "TransferJob":
        return cls(tuple(roles), Op.UNLOAD)


@dataclass
class ActiveConfig:
    """主动侧行为参数（默认取标准典型值）。"""

    jobs: List[TransferJob] = field(default_factory=list)
    #: 是否把全部作业当作**一次连续交接**（用 CONT 串起来）
    continuous: bool = False
    #: 是否检查 HO_AVBL 的两个窗口（§6.2.5.1）
    check_ho_avbl: bool = True
    #: 主动设备实际搬运一个载具所需时间（仿真用）
    transfer_duration_s: float = 0.05
    td0_s: float = 0.1      # §6.3.2.3 表7：CS ON -> VALID ON，范围 0.1–0.2s（强制校验）
    td1_s: float = 1.0
    ta1_s: float = 2.0
    ta2_s: float = 2.0
    ta3_s: float = 2.0


@dataclass(frozen=True)
class ActiveInputs:
    """主动侧看到的被动侧信号（逻辑值）。"""

    now: float = 0.0
    #: 引脚 1 那条「请求」线的逻辑状态（表9：L_REQ/U_REQ 共用一根线）
    demand_on: bool = False
    ready: bool = False
    ho_avbl: bool = True
    es: bool = True


@dataclass(frozen=True)
class ActiveOutputs:
    """主动侧驱动的信号（逻辑值）。"""

    valid: bool = False
    cs0: bool = False
    cs1: bool = False
    tr_req: bool = False
    busy: bool = False
    compt: bool = False
    cont: bool = False


@dataclass(frozen=True)
class ActiveEvent:
    kind: str
    message: str
    timestamp: float = 0.0


@dataclass(frozen=True)
class ActiveStepResult:
    phase: ActivePhase
    outputs: ActiveOutputs
    events: Tuple[ActiveEvent, ...] = ()
    #: 本拍是否「搬运动作完成」——虚拟线束据此改变载口传感器状态
    transfer_due: bool = False
    job: Optional[TransferJob] = None
    aborted: bool = False
    fault: Optional[str] = None


class ActiveFsm:
    """主动侧状态机（参考实现）。"""

    def __init__(self, config: ActiveConfig) -> None:
        self.cfg = config
        self.timers = TimerSet.from_typical(owner="active")
        self.timers.set_duration(TimerId.TD0, config.td0_s)
        self.timers.set_duration(TimerId.TD1, config.td1_s)
        self.timers.set_duration(TimerId.TA1, config.ta1_s)
        self.timers.set_duration(TimerId.TA2, config.ta2_s)
        self.timers.set_duration(TimerId.TA3, config.ta3_s)

        self.phase = ActivePhase.IDLE if config.jobs else ActivePhase.DONE
        self.job_index = 0
        self.transfer_started_at = 0.0
        self.transfer_done = False
        self.aborted = False
        self.fault: Optional[str] = None
        #: 最近一次的 :class:`ActiveStepResult`（便于监视/追踪；不改动行为）
        self.last_result: Optional[ActiveStepResult] = None

    # ------------------------------------------------------------------ 工具
    @property
    def job(self) -> Optional[TransferJob]:
        if 0 <= self.job_index < len(self.cfg.jobs):
            return self.cfg.jobs[self.job_index]
        return None

    @property
    def batch(self) -> bool:
        return self.cfg.continuous and len(self.cfg.jobs) > 1

    def _cs(self, job: TransferJob) -> Tuple[bool, bool]:
        roles = set(job.roles)
        if roles == {PortRole.LEFT, PortRole.RIGHT}:
            return True, True
        if roles == {PortRole.LEFT} or roles == {PortRole.SINGLE}:
            return True, False
        if roles == {PortRole.RIGHT}:
            return False, True
        return False, False

    def _out(self, **kwargs) -> ActiveOutputs:
        return ActiveOutputs(**kwargs)

    # ------------------------------------------------------------------ 主循环
    def step(self, inp: ActiveInputs) -> ActiveStepResult:
        """推进一拍，并记录最近一次结果。"""

        result = self._step(inp)
        self.last_result = result
        return result

    @property
    def outputs(self) -> ActiveOutputs:
        """最近一拍驱动的信号（首次 ``step()`` 之前返回全 OFF）。"""

        return self.last_result.outputs if self.last_result is not None else ActiveOutputs()

    def _step(self, inp: ActiveInputs) -> ActiveStepResult:
        now = inp.now
        events: List[ActiveEvent] = []
        transfer_due = False
        phase = self.phase

        if phase in (ActivePhase.DONE, ActivePhase.FAULT, ActivePhase.ABORTED):
            return ActiveStepResult(phase, ActiveOutputs(), (), job=self.job,
                                    aborted=self.aborted, fault=self.fault)

        if phase is ActivePhase.IDLE:
            if not self.cfg.jobs:
                self.phase = ActivePhase.DONE
                return ActiveStepResult(self.phase, ActiveOutputs())
            self.job_index = 0
            self.phase = ActivePhase.SELECT
            self.timers.start(TimerId.TD0, now)
            events.append(ActiveEvent("select", "选口并等待 TD0", now))
            job = self.job
            assert job is not None
            cs0, cs1 = self._cs(job)
            return ActiveStepResult(
                self.phase, self._out(cs0=cs0, cs1=cs1), tuple(events), job=job
            )

        job = self.job
        assert job is not None

        if phase is ActivePhase.SELECT:
            cs0, cs1 = self._cs(job)
            if self.timers.expired(TimerId.TD0, now):
                self.phase = ActivePhase.VALID_ON
                events.append(ActiveEvent("valid_on", "TD0 到，VALID=ON", now))
                return ActiveStepResult(
                    self.phase,
                    self._out(valid=True, cs0=cs0, cs1=cs1),
                    tuple(events),
                    job=job,
                )
            return ActiveStepResult(
                self.phase, self._out(cs0=cs0, cs1=cs1), tuple(events), job=job
            )

        cs0, cs1 = self._cs(job)

        if phase is ActivePhase.VALID_ON:
            # 窗口 a：VALID ON -> 请求线 ON，其间检查 HO_AVBL
            if self.cfg.check_ho_avbl and not inp.ho_avbl:
                return self._abort(inp, events, "窗口 a 内 HO_AVBL=OFF")
            self.timers.start(TimerId.TA1, now)
            self.phase = ActivePhase.WAIT_DEMAND
            return ActiveStepResult(
                self.phase,
                self._out(valid=True, cs0=cs0, cs1=cs1),
                tuple(events),
                job=job,
            )

        if phase is ActivePhase.WAIT_DEMAND:
            if self.cfg.check_ho_avbl and not inp.ho_avbl:
                return self._abort(inp, events, "窗口 a 内 HO_AVBL=OFF")
            if inp.demand_on:
                self.timers.stop(TimerId.TA1)
                self.timers.start(TimerId.TA2, now)
                self.phase = ActivePhase.WAIT_READY
                events.append(ActiveEvent("tr_req", "请求线已到，TR_REQ=ON", now))
                return ActiveStepResult(
                    self.phase,
                    self._out(valid=True, cs0=cs0, cs1=cs1, tr_req=True),
                    tuple(events),
                    job=job,
                )
            if self.timers.expired(TimerId.TA1, now):
                return self._fault(inp, events, "TA1 超时：等待请求线失败")
            return ActiveStepResult(
                self.phase,
                self._out(valid=True, cs0=cs0, cs1=cs1, tr_req=True),
                tuple(events),
                job=job,
            )

        if phase is ActivePhase.WAIT_READY:
            # 窗口 b：TR_REQ ON -> READY ON
            if self.cfg.check_ho_avbl and not inp.ho_avbl:
                return self._abort(inp, events, "窗口 b 内 HO_AVBL=OFF")
            if inp.ready:
                self.timers.stop(TimerId.TA2)
                self.transfer_started_at = now
                self.transfer_done = False
                self.phase = ActivePhase.TRANSFER
                cont = self.batch and self.job_index < len(self.cfg.jobs) - 1
                events.append(
                    ActiveEvent(
                        "busy",
                        f"BUSY=ON（CONT={'ON' if cont else 'OFF'}）",
                        now,
                    )
                )
                return ActiveStepResult(
                    self.phase,
                    self._out(
                        valid=True, cs0=cs0, cs1=cs1, tr_req=True, busy=True, cont=cont
                    ),
                    tuple(events),
                    job=job,
                )
            if self.timers.expired(TimerId.TA2, now):
                return self._fault(inp, events, "TA2 超时：等待 READY 失败")
            return ActiveStepResult(
                self.phase,
                self._out(valid=True, cs0=cs0, cs1=cs1, tr_req=True),
                tuple(events),
                job=job,
            )

        if phase is ActivePhase.TRANSFER:
            last = self.job_index >= len(self.cfg.jobs) - 1
            cont = self.batch and not last
            if not self.transfer_done and now - self.transfer_started_at >= self.cfg.transfer_duration_s:
                self.transfer_done = True
                transfer_due = True
                events.append(ActiveEvent("moved", "搬运动作完成", now))
            if self.transfer_done and not inp.demand_on:
                self.phase = ActivePhase.COMPT_ON
                events.append(
                    ActiveEvent("busy_off", "请求线已 OFF，BUSY=OFF / TR_REQ=OFF", now)
                )
                return ActiveStepResult(
                    self.phase,
                    self._out(valid=True, cs0=cs0, cs1=cs1, cont=cont),
                    tuple(events),
                    transfer_due=transfer_due,
                    job=job,
                )
            return ActiveStepResult(
                self.phase,
                self._out(
                    valid=True, cs0=cs0, cs1=cs1, tr_req=True, busy=True, cont=cont
                ),
                tuple(events),
                transfer_due=transfer_due,
                job=job,
            )

        if phase is ActivePhase.COMPT_ON:
            last = self.job_index >= len(self.cfg.jobs) - 1
            cont = self.batch and not last
            self.timers.start(TimerId.TA3, now)
            self.phase = ActivePhase.WAIT_READY_OFF
            events.append(ActiveEvent("compt", "COMPT=ON", now))
            return ActiveStepResult(
                self.phase,
                self._out(valid=True, cs0=cs0, cs1=cs1, compt=True, cont=cont),
                tuple(events),
                job=job,
            )

        if phase is ActivePhase.WAIT_READY_OFF:
            last = self.job_index >= len(self.cfg.jobs) - 1
            cont = self.batch and not last
            if not inp.ready:
                self.timers.stop(TimerId.TA3)
                events.append(ActiveEvent("release", "READY=OFF，释放 COMPT/VALID/CS", now))
                if self.batch and not last:
                    self.timers.start(TimerId.TD1, now)
                    self.phase = ActivePhase.NEXT_JOB
                else:
                    self.phase = ActivePhase.DONE
                    events.append(ActiveEvent("done", "全部作业完成", now))
                return ActiveStepResult(self.phase, ActiveOutputs(), tuple(events), job=job)
            if self.timers.expired(TimerId.TA3, now):
                return self._fault(inp, events, "TA3 超时：等待 READY OFF 失败")
            return ActiveStepResult(
                self.phase,
                self._out(valid=True, cs0=cs0, cs1=cs1, compt=True, cont=cont),
                tuple(events),
                job=job,
            )

        if phase is ActivePhase.NEXT_JOB:
            if self.timers.expired(TimerId.TD1, now):
                self.job_index += 1
                self.phase = ActivePhase.SELECT
                self.timers.start(TimerId.TD0, now)
                events.append(
                    ActiveEvent("next_job", f"连续交接第 {self.job_index + 1} 段", now)
                )
                return ActiveStepResult(self.phase, ActiveOutputs(), tuple(events), job=self.job)
            return ActiveStepResult(self.phase, ActiveOutputs(), tuple(events), job=job)

        raise AssertionError(f"未处理阶段: {self.phase}")  # pragma: no cover

    # ------------------------------------------------------------------ 收尾
    def _abort(self, inp: ActiveInputs, events: List[ActiveEvent], reason: str) -> ActiveStepResult:
        self.aborted = True
        self.phase = ActivePhase.ABORTED
        self.timers.stop_all()
        events.append(ActiveEvent("aborted", f"中止交接：{reason}", inp.now))
        return ActiveStepResult(
            self.phase, ActiveOutputs(cont=False), tuple(events), job=self.job, aborted=True
        )

    def _fault(self, inp: ActiveInputs, events: List[ActiveEvent], reason: str) -> ActiveStepResult:
        self.fault = reason
        self.phase = ActivePhase.FAULT
        self.timers.stop_all()
        events.append(ActiveEvent("fault", reason, inp.now))
        return ActiveStepResult(
            self.phase, ActiveOutputs(cont=False), tuple(events), job=self.job, fault=reason
        )

    @property
    def completed(self) -> bool:
        return self.phase in (ActivePhase.DONE, ActivePhase.ABORTED, ActivePhase.FAULT)

    def snapshot(self) -> dict:
        return {
            "phase": self.phase.value,
            "job_index": self.job_index,
            "jobs": len(self.cfg.jobs),
            "continuous": self.batch,
            "aborted": self.aborted,
            "fault": self.fault,
        }
