"""故障分类与故障对象。

设计依据：

* §6.3.1.1 —— 接口上必须能指示「交接不可用」「紧急停止请求」「交接超时错误」；
* §6.3.2.1 —— 用互锁超时检测时序错误，被动侧定时器为 ``TP1``…``TP6``；
* 相关信息 1（R1-1.1.2.1 / R1-2.1.1.2）—— 被动侧出错时的信号画像：
  ``READY`` 变 OFF、``ES`` 变 OFF、``HO_AVBL`` 变 OFF，**其余信号保持在出错时刻的状态**；
* §6.3.3.1 —— 标准**不定义**错误恢复流程，因此本库默认把故障**锁存**，
  只通过显式 :meth:`e84.controller.PiOController.clear_fault` 恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

__all__ = ["FaultCode", "Fault", "ConfigError"]


class FaultCode(str, Enum):
    """被动侧可能产生的故障。字符串值稳定，可直接用于日志/告警键。"""

    # ---- 互锁超时（§6.3.2.1 表 6）----
    TP1_TIMEOUT = "TP1_TIMEOUT"
    TP2_TIMEOUT = "TP2_TIMEOUT"
    TP3_TIMEOUT = "TP3_TIMEOUT"
    TP4_TIMEOUT = "TP4_TIMEOUT"
    TP5_TIMEOUT = "TP5_TIMEOUT"
    TP6_TIMEOUT = "TP6_TIMEOUT"

    # ---- 时序/协议违例 ----
    INVALID_CS_COMBINATION = "INVALID_CS_COMBINATION"
    SIMULTANEOUS_NOT_SUPPORTED = "SIMULTANEOUS_NOT_SUPPORTED"
    CARRIER_STATE_INCONSISTENT = "CARRIER_STATE_INCONSISTENT"
    INTENT_MISMATCH = "INTENT_MISMATCH"
    PORT_NOT_READY = "PORT_NOT_READY"
    COMPT_BEFORE_BUSY_OFF = "COMPT_BEFORE_BUSY_OFF"
    BUSY_BEFORE_REQ_OFF = "BUSY_BEFORE_REQ_OFF"
    HANDOFF_ABORTED_BY_ACTIVE = "HANDOFF_ABORTED_BY_ACTIVE"
    VALID_STUCK = "VALID_STUCK"

    # ---- 可用性/安全 ----
    HO_UNAVAILABLE = "HO_UNAVAILABLE"
    PRECONDITION_LOST = "PRECONDITION_LOST"
    ACCESS_MODE_MANUAL = "ACCESS_MODE_MANUAL"
    ES_CHAIN_OPEN = "ES_CHAIN_OPEN"
    EXTERNAL = "EXTERNAL"

    # ---- I/O ----
    IO_MISMATCH = "IO_MISMATCH"
    IO_ERROR = "IO_ERROR"


#: 故障码 -> 相关标准条款（写进日志便于审计）。
FAULT_CLAUSE: Mapping[FaultCode, str] = {
    FaultCode.TP1_TIMEOUT: "6.3.2.1 表6",
    FaultCode.TP2_TIMEOUT: "6.3.2.1 表6",
    FaultCode.TP3_TIMEOUT: "6.3.2.1 表6",
    FaultCode.TP4_TIMEOUT: "6.3.2.1 表6",
    FaultCode.TP5_TIMEOUT: "6.3.2.1 表6",
    FaultCode.TP6_TIMEOUT: "6.3.2.1 表6",
    FaultCode.INVALID_CS_COMBINATION: "6.1.2.1/6.1.2.4 表2/表3",
    FaultCode.SIMULTANEOUS_NOT_SUPPORTED: "6.1.2.4",
    FaultCode.CARRIER_STATE_INCONSISTENT: "6.2.3.2(3)(4)",
    FaultCode.INTENT_MISMATCH: "6.1.1 表1 L_REQ/U_REQ",
    FaultCode.PORT_NOT_READY: "6.2.5.1(a)",
    FaultCode.COMPT_BEFORE_BUSY_OFF: "6.2.2.1(10) 注4",
    FaultCode.BUSY_BEFORE_REQ_OFF: "6.2.2.1(8)",
    FaultCode.HANDOFF_ABORTED_BY_ACTIVE: "6.2.5.2/6.2.5.3",
    FaultCode.VALID_STUCK: "6.2.2.1(13)",
    FaultCode.HO_UNAVAILABLE: "6.1.1 表1 HO_AVBL",
    FaultCode.PRECONDITION_LOST: "现场安全约束",
    FaultCode.ACCESS_MODE_MANUAL: "5.14/6.1.1 表1 HO_AVBL",
    FaultCode.ES_CHAIN_OPEN: "6.1.1 表1 ES",
    FaultCode.EXTERNAL: "现场",
    FaultCode.IO_MISMATCH: "6.4",
    FaultCode.IO_ERROR: "6.4",
}


@dataclass(frozen=True)
class Fault:
    """一次故障的记录。"""

    code: FaultCode
    message: str = ""
    state: str = ""
    timestamp: float = 0.0
    #: 出错时刻的输入输出快照，便于现场排故与离线复现。
    context: Mapping[str, Any] = field(default_factory=dict)

    @property
    def clause(self) -> str:
        """该故障对应的标准条款。"""

        return FAULT_CLAUSE.get(self.code, "")

    def __str__(self) -> str:  # pragma: no cover - 展示用
        clause = f" [{self.clause}]" if self.clause else ""
        return f"{self.code.value}{clause}: {self.message} (state={self.state})"


class ConfigError(ValueError):
    """配置非法。加载阶段即抛出，避免带病上线。"""
