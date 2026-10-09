#!/usr/bin/env python3
"""E84 被动端库 —— 最小可用示例。

运行（不需要任何硬件）：

    cd /home/say/github_project/e84_communication
    PYTHONPATH=. python3 examples/01_quickstart.py

它做三件事：
1. 从 YAML 加载配置并打印校验告警；
2. 用 `SimIO` + `VirtualClock` 起一个不接硬件的设备（生产上换成 `libgpiod`）；
3. 注册几个事件回调，然后手工把一个「单次装载」握手走完，并打印每一步的信号。

生产环境的写法只在两处不同：把配置里的 ``backend`` 换成 ``libgpiod``，
并用 :class:`~e84.runner.ThreadRunner` 代替 ``ManualRunner``。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from e84 import (  # noqa: E402
    Equipment,
    EventType,
    ManualRunner,
    SimIO,
    VirtualClock,
    load_file,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

CONFIG = Path(__file__).resolve().parent.parent / "configs" / "example_2lp_standard.yaml"


def main() -> int:
    # ---------------------------------------------------------------- 1. 配置
    config = load_file(CONFIG)
    print(f"设备: {config.name}")

    # ---------------------------------------------------------------- 2. 组装
    io = SimIO("passive")          # 生产上：backend: libgpiod
    clock = VirtualClock()         # 生产上：默认 RealClock
    equipment = Equipment(config, clock=clock, ios={"PIO1": io})
    runner = ManualRunner(equipment)

    # ---------------------------------------------------------------- 3. 事件
    def on_state(event) -> None:
        print(f"  [{event.timestamp:6.3f}] 状态 {event.data['from']} -> {event.data['to']}")

    def on_fault(event) -> None:
        print(f"  [{event.timestamp:6.3f}] ★ 故障 {event.message}（条款 {event.data['clause']}）")

    equipment.bus.subscribe(EventType.STATE_CHANGED, on_state)
    equipment.bus.subscribe(EventType.FAULT_RAISED, on_fault)
    equipment.bus.subscribe(
        EventType.INTERLOCK_ENGAGED,
        lambda e: print("  ★ BUSY=ON：此刻禁止本机在交接干涉区内动作"),
    )

    controller = equipment.controller("PIO1")

    # 初始状态：两个载口都空（三键在上拉下都是高电平 = 逻辑 OFF）
    for key in ("LP1_K0", "LP1_K1", "LP1_K2", "LP2_K0", "LP2_K1", "LP2_K2"):
        io.set_input(key, True)          # 高电平 = 无载具（配置里 active_high: false）
    runner.start(now=clock.now())

    def tick(count: int = 1, dt: float = 0.02) -> None:
        for _ in range(count):
            clock.advance(dt)
            runner.poll(now=clock.now())

    def show(tag: str) -> None:
        o = controller.outputs()
        req = "L_REQ" if o.l_req else ("U_REQ" if o.u_req else "-")
        print(
            f"{tag:22s} state={controller.state.value:14s} {req:6s} "
            f"READY={int(o.ready)} HO_AVBL={int(o.ho_avbl)} ES={int(o.es)}"
        )

    tick(2)
    show("空闲")

    # ---------------------------------------------------------------- 4. 握手
    # 主动设备：先选口（CS_0 = ON = 低电平），再抬 VALID（注 3：CS 先于 VALID）
    print("\n主动设备到达：CS_0 = ON")
    io.set_input("IN2", False)
    print("主动设备抬 VALID")
    io.set_input("IN1", False)
    tick(3)
    show("选口并断言 L_REQ")

    io.set_input("IN5", False)       # TR_REQ = ON
    tick(2)
    show("收到 TR_REQ")

    io.set_input("IN6", False)       # BUSY = ON
    tick(2)
    show("BUSY=ON（机构进场）")

    print("载具完整落位（三键全落）")
    for key in ("LP1_K0", "LP1_K1", "LP1_K2"):
        io.set_input(key, False)
    tick(4)
    show("载具到位，撤回请求")

    io.set_input("IN6", True)        # BUSY = OFF
    io.set_input("IN5", True)        # TR_REQ = OFF
    io.set_input("IN7", False)       # COMPT = ON
    tick(2)
    show("收到 COMPT")

    io.set_input("IN1", True)        # VALID = OFF：握手闭合
    io.set_input("IN7", True)
    io.set_input("IN2", True)
    tick(3)
    show("握手闭合")

    # ---------------------------------------------------------------- 5. 停机
    runner.stop(now=clock.now())
    print(f"\n停机后物理电平（低电平 = ON）：{io.output_levels()}")
    print("注意 OUT7(HO_AVBL) 与 OUT8(ES) 都是 True(高) = OFF —— 失效安全方向。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
