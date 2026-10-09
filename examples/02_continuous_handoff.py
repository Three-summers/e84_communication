#!/usr/bin/env python3
"""E84 被动端库 —— 连续交接（continuous handoff）示例。

对应 SEMI E84-0301 §6.2.4.3 与图 16：**同一载口**先卸载、再装载。

这个例子用 :class:`~e84.testing.VirtualRig` 把本库与「主动侧参考实现」接在一根
虚拟 DB-25 上对拖，并把逐拍波形打印出来。运行：

    cd /home/say/github_project/e84_communication
    PYTHONPATH=. python3 examples/02_continuous_handoff.py

要观察的三个关键点：

1. **`CONT` 是边沿采样的**：它在**第一段**的 `BUSY` 上升沿为 ON（表示"后面还有"），
   在**末段**的 `BUSY` 上升沿为 OFF（表示"这是最后一段"）。被动侧在每段
   `BUSY` 上升沿把这个值锁存下来，据此决定交接完成后是回 IDLE 还是等下一段。
2. **段与段之间 `VALID` 会落下再抬起**（规范图 16 画得很清楚），不是"整批保持 ON"。
   被动侧用 `TP6`（表 6，典型 2 s）监视"下一次 `VALID` 什么时候来"。
   注意看 `L/U_REQ`：第一段是 `U_REQ`（卸载），第二段自动变成 `L_REQ`（装载）
   —— 因为此时载口已经空了。
3. **`batch_started` / `batch_ended` 事件**成对出现，上层可以据此"整批只开一次门"
   （§6.2.4.2：有闸门的载口在连续交接期间应保持开启，避免每颗料开关一次）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from e84 import EventType, VirtualClock, load_file  # noqa: E402
from e84.active import TransferJob  # noqa: E402
from e84.model import PortRole  # noqa: E402
from e84.testing import VirtualRig  # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "configs" / "example_2lp_standard.yaml"


def main() -> int:
    config = load_file(CONFIG)

    # 两个作业，串成**一次**连续交接：先把 LP1 上的载具取走，再往 LP1 放一个新载具。
    rig = VirtualRig(
        config,
        jobs=[
            TransferJob.unload(PortRole.LEFT),
            TransferJob.load(PortRole.LEFT),
        ],
        continuous=True,
        clock=VirtualClock(),
        autostart=False,
    )

    # 初始条件：LP1 上有一个"完整落位"的载具（三键全落）
    rig.set_carrier("LP1", present=True)

    # 把批事件和故障打印出来
    rig.equipment.bus.subscribe(
        EventType.BATCH_STARTED, lambda e: print(f"  ★ {e.timestamp:6.3f} 连续交接开始")
    )
    rig.equipment.bus.subscribe(
        EventType.BATCH_ENDED, lambda e: print(f"  ★ {e.timestamp:6.3f} 连续交接结束")
    )
    rig.equipment.bus.subscribe(
        EventType.FAULT_RAISED, lambda e: print(f"  ★ 故障 {e.message}")
    )

    rig.start()

    header = (
        f"{'t':>7} {'主动阶段':<12} "
        f"{'VALID':>5} {'CS0':>4} {'CS1':>4} {'TRQ':>4} {'BUSY':>4} {'CMPT':>4} {'CONT':>4} | "
        f"{'REQ':>4} {'RDY':>4} {'HO':>4} {'ES':>3} | {'被动状态':<14} 说明"
    )
    print(header)
    print("-" * len(header))

    # 逐拍推进并打印；只在"有信号变化"时打一行，避免刷屏
    last_key = None
    for _ in range(40000):
        rig.tick(0.002)
        a = rig.active
        outs = a.outputs
        po = rig.outputs()
        req = "L" if po.l_req else ("U" if po.u_req else "-")
        key = (
            a.phase.value, outs.valid, outs.cs0, outs.cs1, outs.tr_req, outs.busy,
            outs.compt, outs.cont, req, po.ready, po.ho_avbl, po.es, rig.state,
        )
        if key != last_key:
            last_key = key
            note = ""
            if outs.cont and outs.busy:
                note = "CONT=ON ⇒ 后面还有一段"
            elif outs.busy and not outs.cont:
                note = "CONT=OFF ⇒ 这是末段"
            if req == "U":
                note = note or "卸载：请求线 ON，等载具被取走"
            elif req == "L":
                note = note or "装载：请求线 ON，等载具完整落位"
            print(
                f"{rig.clk.now():7.3f} {a.phase.value:<12} "
                f"{int(outs.valid):>5} {int(outs.cs0):>4} {int(outs.cs1):>4} "
                f"{int(outs.tr_req):>4} {int(outs.busy):>4} {int(outs.compt):>4} {int(outs.cont):>4} | "
                f"{req:>4} {int(po.ready):>4} {int(po.ho_avbl):>4} {int(po.es):>3} | "
                f"{rig.state:<14} {note}"
            )
        if a.completed and rig.state == "idle":
            break

    print("-" * len(header))
    print(f"主动侧: {rig.active.snapshot()}")
    print(f"被动侧: state={rig.state} 故障={rig.controller.fault}")
    print(f"LP1 最终载具状态: 存在={rig.port_state('LP1').present} 完整落位={rig.port_state('LP1').in_position}")
    ok = rig.active.phase.value == "done" and rig.state == "idle" and rig.controller.fault is None
    print("PASS" if ok else "FAIL")
    rig.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
