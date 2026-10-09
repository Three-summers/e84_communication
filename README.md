# e84 —— SEMI E84-0301 被动端（装备端）并行 I/O 载具交接库

一句话定位：**用 Python 实现 SEMI E84-0301 增强载具交接并行 I/O 接口的「被动端 / 装备端」侧**
（工艺设备、量测设备、储料机这一侧），硬件解耦、完全配置化、可在虚拟时钟上逐拍复现，
并附带一个**主动侧参考实现**用于自测与对拖。

> 明确边界：本库实现的是**被动侧**。主动侧（OHT/AGV/RGV/储料机）只在 `e84/active/` 里提供
> 一个用于闭环自测的参考实现，不是产品代码。信号方向约定 **P = 被动设备**、**A = 主动设备**。

规范中文图文版在 [`docs/e84_standard_document_reference/semi_e84_0301_illustrated_zh.md`](docs/e84_standard_document_reference/semi_e84_0301_illustrated_zh.md)，
设计文档在 [`docs/design/e84_python_library_design.md`](docs/design/e84_python_library_design.md)。

---

## 1. 特性

- **三种交接形态**：单次交接（§6.2.2）、同时交接（§6.2.3，一个 PI/O 两个载口）、
  连续交接（§6.2.4，`CONT` + `TP6`/`TD1`）。
- **`TP1`–`TP6` 可编程定时器**，范围强校验（§6.3.2.1：除 `TD0` 外均为 1–999 s，
  设定值应可由用户编程）；`TP3`/`TP4` 典型值 **60 s**。
- **`HO_AVBL` / `ES` 按标准语义实现**：`HO_AVBL` 由载口可用性、访问模式、安全链合成；
  `ES` 反映本机安全链（`set_es_ok()`），故障时按相关信息 1 拉低。
- **逐信号极性/上拉/去抖可配**；两个逻辑信号映射到同一物理通道（现场接法）会显式声明并做冲突校验。
- **完全配置化**：引脚映射、载具检测方式、访问模式来源、交接方向来源、定时器、故障策略
  全部外置到 YAML/JSON/TOML，标准即默认值。
- **虚拟时钟可测**：协议核心是纯逻辑状态机，不碰 I/O、时钟、线程，可用 `VirtualClock`
  逐拍回放 fig_10–fig_20 的时序；`SimIO` 支持注入卡死/写失败。
- **主动侧参考实现**：`e84/active/` + `e84/testing/harness.py` 的 `VirtualRig` 构成
  不需要任何硬件的闭环。
- **命令行工具**：`validate` / `monitor` / `selftest` / `sim` / `force` / `faults`。

---

## 2. 安装与导入

本库**没有打包**（仓库里没有 `pyproject.toml`），入口统一是 `git clone` 之后把仓库根加入
`sys.path`：

```bash
git clone <repo-url> e84_communication
cd e84_communication
export PYTHONPATH=.            # 或者在代码里 sys.path.insert(0, "e84_communication")
python3 -c "import e84; print(e84.__version__)"
```

依赖：

- **仅标准库**（运行期）；
- 配置用 YAML 时需要 **PyYAML**（可选）：`pip install pyyaml`；
  没装也可以直接用 JSON（JSON 是 YAML 的子集）或 TOML；
- 现场硬件后端 `libgpiod`：

  ```bash
  # Debian/Ubuntu
  sudo apt install gpiod libgpiod-dev python3-libgpiod
  # 或
  pip install gpiod
  ```

  缺依赖时后端会抛出带安装提示的 `RuntimeError`，不会静默失败。
  树莓派也可以通过 `backend: rpi`（`RPi.GPIO` 兼容后端）或直接继续用 `sim`/`memory`。

---

## 3. 快速开始

### 3.1 接真实设备（30 行以内）

```python
from e84 import Equipment, ThreadRunner, EventType, load_file

cfg = load_file("configs/example_2lp_standard.yaml")  # 现场换成自己的配置
eq = Equipment(cfg)                                   # 后端取配置里的 backend
runner = ThreadRunner(eq)                             # 后台线程按 poll_interval_ms 轮询

@eq.bus.on(EventType.INTERLOCK_ENGAGED)               # BUSY=ON：冻结干涉区机构
def _freeze(event):
    robot.freeze()

@eq.bus.on(EventType.FAULT_RAISED)                    # 故障：记录 + 通知上位机
def _on_fault(event):
    logger.error("%s %s", event.clause, event)

runner.start()
try:
    ...
finally:
    runner.stop()        # 幂等；退出前把输出驱动到安全态（ES=OFF / HO_AVBL=OFF）
```

需要注入上层状态时：

```python
eq.set_access_mode("PIO1", "LP1", AccessMode.MANUAL)  # 手动模式（§5.1/§5.14）
eq.set_port_available("PIO1", "LP1", False)           # 载口不可用 -> 拉低 HO_AVBL
eq.set_operation_intent("PIO1", "LP1", Op.LOAD)       # 或由传感器推导（默认）
eq.clear_fault("PIO1")                                 # 清除锁存故障（§6.3.3.1）
```

> `set_access_mode` / `set_operation_intent` 也接受字符串（`"manual"`、`"auto"`、
> `"load"`、`"unload"`）：库会归一化成枚举，非法值直接抛 `ValueError`，
> 不会"存进去了但永远匹配不上"。

### 3.2 测试用快速开始（虚拟时钟 + 手动轮询）

```python
from e84 import Equipment, ManualRunner, SimIO, VirtualClock, load_file
from e84.active import TransferJob
from e84.model import PortRole
from e84.testing import VirtualRig

cfg = load_file("configs/example_2lp_standard.yaml")

# 方式 A：自己控制节拍（适合嵌进自有实时循环）
clk = VirtualClock()
eq = Equipment(cfg, clock=clk, ios={"PIO1": SimIO()})
runner = ManualRunner(eq)
runner.start()
runner.poll(now=clk.now())
clk.advance(0.01)
runner.poll(now=clk.now())
runner.stop()

# 方式 B：直接跑完整闭环（虚拟线束把主动侧参考实现接上）
rig = VirtualRig(cfg, jobs=[TransferJob.load(PortRole.LEFT)], interface_id="PIO1")
rig.run_to_completion(timeout_s=5.0, dt=0.002)
assert rig.active.phase.value == "done"
assert rig.controller.state.value == "idle"
assert rig.controller.fault is None
```

> 注意运行器名字是 **`ManualRunner`**（不是 `ManualRouter`）：
> 它只在你调用 `poll()` 时推进一拍；`ThreadRunner` 才会自己开线程。

---

## 4. 架构分层

```text
L1 Clock             RealClock / VirtualClock      —— 协议核心只接受 now，不碰墙钟
        │
L2 I/O 后端          open_backend("null"|"memory"|"sim"|"libgpiod"|"rpi")
        │            只处理物理电平（True == 高电平）
L3 HAL (SignalMap)   逻辑信号 ↔ 物理通道、逐信号极性/上拉/去抖、安全态、写变化才写
        │            公用通道（L_REQ/U_REQ）互斥与极性冲突在此拦截
L4 传感量 SensorBank 载具存在 / 完整落位 / 门 / 夹持 / 访问模式
        │            source = input | keys | external | constant
L5 协议核心 PassiveFsm  ★ 纯逻辑状态机：无 I/O、无时钟、无线程（fig_10–fig_20 的可执行形式）
        │            输入 Inputs -> 输出 Outputs + 状态迁移 + 事件 + 故障
L6 控制器 PiOController 读输入 → 跑 FSM → 写输出 → 发事件 → 写追踪
        │
L7 设备门面 Equipment  多 PI/O 聚合、看门狗、故障清除、快照与描述
        │
L8 Runner            ManualRunner（你决定节拍）/ ThreadRunner（后台线程）
```

模块地图见 [`e84/__init__.py`](e84/__init__.py) 的 docstring。

---

## 5. 配置参考

配置文件的顶层结构（`EquipmentConfig`）：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `schema_version` | int | `1` | 目前只支持 `1` |
| `name` | str | `"e84-equipment"` | 设备名，进日志/快照 |
| `backend` | str | `"memory"` | 设备级默认后端；`null`/`memory`/`sim`/`libgpiod`/`rpi` |
| `backend_options` | dict | `{}` | 透传给后端构造函数的参数 |
| `poll_interval_ms` | float | `5.0` | 轮询周期；> 20 ms 时校验器告警（`TD0` 只有 100 ms） |
| `boot_safe_hold_ms` | float | `200.0` | 上电先保持安全态多久（§6.4.6 失效安全） |
| `log_level` | str | `"INFO"` | 日志级别 |
| `log_json` | bool | `false` | 是否输出 JSON 日志 |
| `watchdog` | dict/bool/str | 无 | 轮询卡死看门狗：`enabled`、`channel`、`period_ms`、`interface`、`stall_polls` |
| `trace` | dict/bool/str | 关闭 | 信号跳变追踪：`enabled`、`path`、`format`(`csv`/`jsonl`)、`flush_every`、`include_io` |
| `interfaces` | list | 必填 | 每个 PI/O 接口一项（见下） |

`interfaces[]`（`InterfaceConfig`）：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | str | 必填 | 接口 id（也是 I/O 后端实例的键） |
| `scenario` | str | `standard` | `standard`（已实现）/ `interbay_passive_ohs`（**校验器拒绝**） |
| `topology` | str | `two_load_ports` | `one_load_port`（表 3）/ `two_load_ports`（表 2） |
| `features.simultaneous` | bool | `true` | 是否支持同时交接（§6.1.2.4，需要两个载口） |
| `features.continuous` | bool | `true` | 是否支持连续交接（§6.2.4，需要绑定 `CONT`） |
| `preconditions` | list[str] | `[VALID]` | 握手前置条件；现场可加板级 `GO` 等自定义信号 |
| `io_defaults` | dict | `active_high=true, pull=none, debounce_ms=0, safe_on=false` | 接口级 I/O 默认值 |
| `load_ports` | list | 必填 | 载口列表 |
| `inputs` | dict | `{}` | 逻辑信号 → 通道绑定（A→P） |
| `outputs` | dict | `{}` | 逻辑信号 → 通道绑定（P→A） |
| `timers` | dict | `{}` | `TP1`…`TP6` / `TD1` 的设定值（秒） |
| `policy` | dict | 见下 | 时序之外的行为策略 |
| `backend` | str | 继承设备级 | 接口级后端覆盖 |
| `backend_options` | dict | `{}` | 接口级后端参数 |

`load_ports[]`（`LoadPortConfig`）：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `id` | 必填 | 载口 id |
| `role` | `single` | `single` / `left` / `right`（§6.1.2.3：面向设备载口时的左右手） |
| `carrier_present` | 常量 `false` | 「检测到载具」（任意位置），用于判断能否卸载 |
| `carrier_in_position` | 常量 `false` | 「载具位于正确位置」——**§6.1 表 1 判定 `L_REQ`/`U_REQ` 落下的依据** |
| `ready_for_transfer` | 常量 `true` | 机构就绪 |
| `door_open` | 常量 `true` | 门已开到位 |
| `clamp_released` | 常量 `true` | 夹持已释放 |
| `enabled` | `true` | 为 `false` 时该载口恒不可用（等价持续拉低 `HO_AVBL`） |
| `access_mode` | `{source: static, value: automatic}` | 访问模式来源：`static` / `api` / `input`（§5.1、§5.14） |
| `operation_intent` | `{source: derived}` | 交接方向来源：`derived` / `api` / `static` |

### 5.1 载具传感量的四种来源

`carrier_present` / `carrier_in_position`（以及 `ready_for_transfer`、`door_open`、
`clamp_released`）都接受同一个 `SensorSpec`，四种 `source`：

```yaml
load_ports:
  # 1) input —— 单个 GPIO 输入通道（可简写成字符串 "LP1_DET"）
  - id: LP1
    carrier_present: {source: input, channel: LP1_DET, active_high: false, pull: up, debounce_ms: 10}

  # 2) keys —— 多通道组合：any = 检测到载具；all = 完整落位（现场三键方案）
  - id: LP2
    carrier_present:     {source: keys, channels: [K0, K1, K2], mode: any}
    carrier_in_position: {source: keys, channels: [K0, K1, K2], mode: all, debounce_ms: 20}

  # 3) external —— 由上层 API 注入（例如来自 E87/E30 或设备主控）
  #    controller.set_external_sensor("LP3", "carrier_present", True)
  - id: LP3
    carrier_present:     {source: external}
    carrier_in_position: {source: external}

  # 4) constant —— 固定值（没有该机构的载口）
  - id: LP4
    carrier_present:     {source: constant, value: false}
    carrier_in_position: {source: constant, value: false}
```

> `keys` 的 `any` 与 `all` **可以共用同一批通道**（这正是三键方案的用法）；
> 但跨载口复用同一通道、或极性/上拉不一致会被校验器判为**错误**。

`policy` 常用字段（`PolicyConfig`，默认值全部取「标准 + 保守安全」）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `invalid_cs` | `fault` | `CS_0/CS_1` 组合非法：`fault` / `ignore` |
| `intent_mismatch` | `fault` | 上层意图与传感器矛盾：`fault` / `follow_sensor` |
| `unsupported_simultaneous` | `fault` | 对方要求同时交接但未启用：`fault` / `ho_abort` |
| `tp6_timeout` | `idle` | 连续交接中场等待超时：`idle` / `fault` |
| `on_timeout` | `fault` | 互锁超时：`fault` / `ho_abort` |
| `request_hold_on_fault` | `true` | 故障时保持请求线（相关信息 1 R1-1.1.2.1） |
| `es_latched_on_fault` | `true` | 故障锁存期间持续拉低 `ES` |
| `ho_avbl_when_manual` | `false` | 手动访问模式下是否也拉低 `HO_AVBL` |
| `ho_avbl_scope` | `all_ports` | `all_ports` / `selected_ports` |
| `not_ready_timeout_s` | `1.5` | 选口后载口不就绪多久拉低 `HO_AVBL`（**必须 < 对方 `TA1` 的典型值 2 s**，否则来不及表态对方就已超时） |
| `cont_sample_delay_ms` | `10.0` | `BUSY` 上升沿后延迟多久采样 `CONT` |
| `abort_on_precondition_loss` | `true` | 前置条件丢失时中止并回 `IDLE` |
| `interlock_freeze` | `true` | `BUSY=ON` 期间通过事件通知上层冻结机构 |
| `single_lp_require_cs1_off` | `true` | 单载口拓扑下要求 `CS_1` 恒为 OFF（表 3） |

完整字段以 [`e84/config/model.py`](e84/config/model.py) 为准；用
`python3 -m e84.cli validate <config> --dump` 可以打印解析后的完整配置 JSON。

---

## 6. 信号与引脚

全部信号（§6.1 表 1）。引脚号由 `e84.signals.db25_pins()` 给出，**仅作参考**（SEMI E84-0301 表 9，被动设备侧连接器 A）：

| 信号 | 方向 | 表 9 引脚 | 说明 |
|---|---|---|---|
| `L_REQ` | P→A | **1** | 装载请求（载具由主动→被动） |
| `U_REQ` | P→A | **2** | 卸载请求（载具由被动→主动） |
| `VA` | P→A | 3 | 被动 OHS 车辆已到达（跨区专用） |
| `READY` | P→A | **4** | 已就绪，可交接 |
| `VS_0` | P→A | **5** | 跨区载口选择 0（跨区专用） |
| `VS_1` | P→A | 6 | 跨区载口选择 1（跨区专用） |
| `HO_AVBL` | P→A | 7 | 可交接；OFF = 不可交接/有错误（低有效语义） |
| `ES` | P→A | 8 | 紧急停止请求；**OFF = 请求停止**（低有效） |
| `VALID` | A→P | 14 | 接口通信有效（不用于跨区 AMHS） |
| `CS_0` | A→P | 15 | 载口选择 0（不用于跨区 AMHS） |
| `CS_1` | A→P | 16 | 载口选择 1（不用于跨区 AMHS） |
| `AM_AVBL` | A→P | 17 | 搬运臂可用（跨区专用） |
| `TR_REQ` | A→P | 18 | 传输请求 |
| `BUSY` | A→P | 19 | 交接进行中；**只要为 ON，被动设备不得在干涉区做机械动作** |
| `COMPT` | A→P | 20 | 交接完成 |
| `CONT` | A→P | 21 | 连续交接 |

引脚 9 / 13 为 NC，10 / 11 / 12 为 Reserved，22–25 为电源与地
（其中 **24/25 是 Power COM 与 Signal COM 的交叉连接**；硬线配置下电源域必须与信号域隔离，§6.4.2.2）。

### ⚠ 关于表 9 的两个提醒

1. **原文中每个信号各占一个引脚，没有任何两个信号共用引脚。**
   网上流传的一份中文译文把该表错排成了 `L_REQ`/`U_REQ` 共用引脚 1、
   `READY`/`VS_0` 共用引脚 4 —— **不要照那份译文接线**。
   本库的 `e84.signals.db25_pins()` 已按 SEMI E84-0301 原文（Table 9）修正，
   并有回归测试 `tests/test_reference_data.py` 钉住这些值。
2. **引脚顺序与逻辑顺序不同，且跨区信号夹在中间**：
   `VA` 在引脚 3、`VS_0` 在引脚 5，夹在 `U_REQ`(2) 与 `READY`(4) / `VS_1`(6) 之间。
   所以"OUT 编号"与信号清单**不是顺序对应**的，做线必须逐针核对。

> 即便如此，本库仍然把**引脚映射完全外置到配置**，`db25_pins()` **只用于文档与提示**，
> 不参与任何逻辑判断——因为实际接线以现场规格书为准，而且**有些设备会把
> `L_REQ`/`U_REQ` 并在一条线上**（旧 VOC 板则是两根独立线，与原文一致）。
> HAL 因此支持"两个逻辑信号映射到同一 `channel`"这种现场接法：
> 校验器只报**告警**，且在两者被同时置 ON 时会直接抛错。

跨区被动 OHS 的信号（`VA`/`VS_0`/`VS_1`/`AM_AVBL`）已在 `e84.signals.INTERBAY_SIGNALS`
里定义并带 `not_for_interbay()` 标注，配置位也预留好了。

---

## 7. 定时器

| 定时器 | 监视区间（信号状态） | 范围 (s) | 典型值 (s) | 条款 |
|---|---|---|---|---|
| `TP1` | `L_REQ` ON — `TR_REQ` ON / `U_REQ` ON — `TR_REQ` ON | 1–999 | 2 | §6.3.2.1 表 6 |
| `TP2` | `READY` ON — `BUSY` ON | 1–999 | 2 | §6.3.2.1 表 6 |
| `TP3` | `BUSY` ON — 载具到位 / 载具被取走 | 1–999 | **60** | §6.3.2.1 表 6 |
| `TP4` | `L_REQ` OFF — `BUSY` OFF / `U_REQ` OFF — `BUSY` OFF | 1–999 | **60** | §6.3.2.1 表 6 |
| `TP5` | `READY` OFF — `VALID` OFF | 1–999 | 2 | §6.3.2.1 表 6 |
| `TP6` | `VALID` OFF — `VALID` ON（连续交接） | 1–999 | 2 | §6.3.2.1 表 6 |
| `TA1` | `VALID` ON — `L_REQ` ON / `VALID` ON — `U_REQ` ON | 1–999 | 2 | §6.3.2.1 表 5 |
| `TA2` | `TR_REQ` ON — `READY` ON | 1–999 | 2 | §6.3.2.1 表 5 |
| `TA3` | `COMPT` ON — `READY` OFF | 1–999 | 2 | §6.3.2.1 表 5 |
| `TD0` | `CS` ON — `VALID` ON | **0.1–0.2** | 0.1 | §6.3.2.3 表 7 |
| `TD1` | `VALID` OFF — `VALID` ON | 1–999 | 1 | §6.3.2.2 表 7 |

要点：

- **`TP3`/`TP4` 的典型值是 60 s**（等机械动作），其余被动定时器是 2 s；
- `TD0` 是唯一以 0.1 s 为量级的定时器，它让被动侧能**预测** `VALID` 的出现时刻，
  所以 `poll_interval_ms` 必须远小于 100 ms（默认 5 ms，> 20 ms 时校验器告警）；
- 所有定时器设定值都可由用户编程（`timers:` 配置，或运行时 `TimerSet.set_duration()`）；
- 越界值在加载配置时就会被拒绝（`TimerSpec.validate()`）。

---

## 8. 场景支持矩阵

| 场景 | 状态 | 说明 |
|---|---|---|
| **标准场景**（装备 = 被动，OHT/AGV/RGV = 主动） | ✅ 已实现 | `scenario: standard`；单次 / 同时 / 连续交接，`HO_AVBL`/`ES`，fig_10–fig_19 主路径 |
| **跨区被动 OHS**（`VA`/`VS_0`/`VS_1`/`AM_AVBL`） | 🟡 部分 | 信号定义（`INTERBAY_SIGNALS`）、方向、引脚、配置位**已预留**；但状态机**尚未实现**，`scenario: interbay_passive_ohs` 会被 `validate_config()` **明确拒绝**（错误，退出码 1） |
| **交换式交接（swap handoff）** | ❌ 超出范围 | 标准 §3.6 明确把「同一载口同时装载与卸载」排除在外；请改用连续交接（卸→装） |

---

## 9. 安全说明

- **上电/停机安全态 = `ES=OFF` + `HO_AVBL=OFF`**。依据 §6.4.6：OFF 在硬线下是「无电流」、
  在光耦下是「无光」，是**失效安全**方向；`ES` 本身是低有效（OFF = 请求停止）。
  `Outputs()` 的字段默认值即安全态，`SignalMap.safe_state()` 把每个输出驱动到
  `ChannelSpec.safe_on`（默认 `false`）；校验器会对 `ES`/`HO_AVBL` 的 `safe_on=true` 告警。
- **故障按相关信息 1 锁存**（R1-1.1.2.1 / R1-2.1.1.2）：`READY=OFF`、`ES=OFF`、`HO_AVBL=OFF`，
  **其余信号保持在出错时刻的状态**（`policy.request_hold_on_fault=true`）。
  §6.3.3.1 不定义恢复流程，因此本库默认把故障**锁存**，只通过
  `Equipment.clear_fault()` / `PiOController.clear_fault(force=...)` 显式恢复。
- **`BUSY=ON` 期间禁止干涉区机械动作**：`PiOController.interlock_engaged` /
  `Equipment.interlock_engaged` **严格跟随 `BUSY` 电平**——`BUSY`↑ 发
  `EventType.INTERLOCK_ENGAGED`，`BUSY`↓ 立刻发 `INTERLOCK_RELEASED`
  （**不会**拖到握手闭合，也不会因为对方卡在后续步骤而让机构一直冻着）；
  `BUSY=ON` 期间即使发生故障也保持冻结（对方机构可能还在干涉区里）。上层据此冻结/释放机构。
- **访问模式与交接中的约束（SEMI E87）**：`Equipment.transfer_in_progress` /
  `PiOController.transfer_in_progress` 按 E87 Table 8 给出"是否正在交接"；
  交接期间调用 `set_access_mode(..., strict=True)` 会被拒绝（E87 §11.1.2 不允许在
  carrier transfer 期间切换访问模式）。默认 `strict=False`，因为"操作员切手动"往往
  正是要**立即**中止交接的安全动作。手动模式下 AMHS 硬来时本库绝不断言请求线，
  并发 `HO_ABORTED` 事件供上层告警（E87 §11.3.3.2）。
- **看门狗只在轮询推进时翻转**：`WatchdogConfig.channel` 的翻转由 `Equipment.poll()` 驱动，
  一旦调用方的轮询停止（进程卡死、线程崩溃），WDI 停摆，外部看门狗可把电路拉回安全态。
- **`boot_safe_hold_ms`** 让上电后先保持安全态一段时间，等外部电路稳定。
- CLI 侧：真实后端（非 `null`/`memory`/`sim`）必须显式传 `--i-know-what-i-am-doing`，
  否则 `monitor` 会自动降级为 `null`、`force` 直接拒绝。

---

## 10. 工具（CLI）

统一入口：`python -m e84.cli <子命令> [选项]`。
退出码：`0` 成功 / `1` 配置或参数错误 / `2` 运行期失败。

| 子命令 | 作用 |
|---|---|
| `validate <config>` | 加载 + 交叉校验；`--show-map` 打印映射表/传感量来源/定时器表，`--dump` 打印配置 JSON，`--json` 输出机器可读结果 |
| `monitor <config>` | 手动轮询周期打印状态/输出/定时器/载口/故障；`--interval-ms`、`--backend`、`--dry-run`、`--duration-s`、`--i-know-what-i-am-doing` |
| `selftest [config]` | 不接硬件自检并逐项 PASS/FAIL：配置校验、HAL 极性/共用通道/安全态、四个闭环、定时器范围 |
| `sim <config>` | 用 `VirtualRig` 跑作业序列并打印**逐拍信号追踪**；`--job`（可重复）、`--continuous`、`--dt-ms`、`--timeout-s`、`--trace-out`、`--every-tick` |
| `force <config>` | 现场接线调试：`--interface`、`--signal READY`、`--logical 0\|1`、`--backend`、`--hold-ms`，**必须** `--i-know-what-i-am-doing`，且握手进行中会拒绝 |
| `faults <config>` | 打印各接口当前故障、条款号与故障态期望信号画像 |

示例：

```bash
python3 -m e84.cli --help
python3 -m e84.cli --version

# 校验 + 查看映射表 / 导出解析后的配置
python3 -m e84.cli validate configs/example_2lp_standard.yaml --show-map
python3 -m e84.cli validate configs/example_2lp_standard.yaml --dump | python3 -m json.tool
python3 -m e84.cli validate configs/example_1lp_standard.yaml --json

# 不接硬件的自检（9 项）
python3 -m e84.cli selftest

# 闭环仿真：单次装载 / 同时装载 / 连续（卸→装）
python3 -m e84.cli sim configs/example_2lp_standard.yaml --job load:left
python3 -m e84.cli sim configs/example_2lp_standard.yaml --job load:left,right
python3 -m e84.cli sim configs/example_2lp_standard.yaml \
    --job unload:left --job load:left --continuous --trace-out /tmp/trace.csv

# 监视（dry-run 用 null 后端，不碰硬件）
python3 -m e84.cli monitor configs/example_2lp_standard.yaml --dry-run --duration-s 5

# 排故：故障画像；接线调试（务必确认现场安全）
python3 -m e84.cli faults configs/example_2lp_standard.yaml
python3 -m e84.cli force configs/example_voc_compat.yaml \
    --interface PIO1 --signal READY --logical 1 --hold-ms 500 --i-know-what-i-am-doing
```

`sim --job` 的格式：`OP:ROLES`，`OP` ∈ `load`/`unload`，
`ROLES` ∈ `left`/`right`/`single`/`both`，逗号分隔多个载口表示**同时交接**
（`load:left,right`）。`--trace-out` 写宽表 CSV（列：`t_s,active_phase,VALID,CS_0,CS_1,
TR_REQ,BUSY,COMPT,CONT,L_REQ,U_REQ,READY,HO_AVBL,ES,passive_state`）。

---

## 11. 测试

```bash
python3 -m pytest -q            # 全部默认测试（pytest.ini 已设 -m "not hardware"）
python3 -m pytest -q tests/test_cli.py
python3 -m pytest -q -m hardware   # 需要真实 GPIO / 物理硬件的测试
```

除了常规单元/集成测试，还有一组 **交叉验证**（[`tests/test_cross_validation.py`](tests/test_cross_validation.py)），
专门用来打破"同源自测"的循环论证（否则 `test_golden_figures.py` 只是拿我自己的主动端
去测我自己的被动端）：

| | 内容 |
|---|---|
| X1 层级差分 | 「纯 FSM 直驱」vs「完整栈（极性/映射/传感量/HAL）」，3000 拍随机波形逐拍比对状态、逻辑输出与**物理电平** |
| X2 表示交叉 | 同一逻辑波形在 {低有效, 高有效} × {请求线共用, 分开} 四种表示下逻辑轨迹必须一致 |
| X3 图驱动 | 把规范正文的编号步骤**手写成显式电平波形**（不经过 `ActiveFsm`）驱动完整栈 |
| X4 反向交叉 | 用**手写被动响应**（不经过 `PassiveFsm`）驱动 `ActiveFsm`，核对主动侧顺序约束 |
| X5 第三方判据 | 从**商业仿真器公开手册**里提取的符合性判据（厂商列为 "Failure - ..." 的那些），见 [`tests/test_vendor_criteria.py`](tests/test_vendor_criteria.py) |

X1 抓到过"逻辑对但引脚写错"这一类层级 bug；X3 抓到过干涉区冻结挂错了触发点（见
[`docs/verification.md`](docs/verification.md) §4 B11）。

> 公开渠道**不存在开源的第三方 E84 实现**（调研过程与可用商业方案见
> [`docs/third_party_references.md`](docs/third_party_references.md)）。
> 因此"与第三方实现对拖"这一项仍未完成，残余风险已如实标注。

`pytest.ini` 里注册了 `hardware` 标记（默认跳过）与 `slow` 标记。
`conftest.py` 提供 `cfg` / `iface` / `make_controller` / `make_rig` / `collect_events` 等 fixture，
仓库根会被插入 `sys.path`，所以无需安装包。

---

## 12. 与既有 VOC 实现的关系

旧实现（`VOC_Project`：树莓派 + `RPi.GPIO`，200 ms 轮询、无 `CS_1`/`CONT`、`HO_AVBL`/`ES`
由三键状态静态推导、超时只告警不锁存）**只是参考物，不是基线**：本库一律以标准为准重写，
旧实现的**现场事实**（板级 `GO` 存在、三键语义、输出低有效、`ShortTimer`/`LongTimer` 两档时限）
全部通过配置表达，而不是写进协议栈。例如

```yaml
# configs/example_voc_compat.yaml
topology: one_load_port            # 现场只有一个载口、没接 CS_1
preconditions: [VALID, CS_0, GO]   # 保留板级 GO 联锁；默认标准行为只有 [VALID]
features: {continuous: false}      # 现场未使用连续交接
outputs:                           # 现场 L_REQ/U_REQ 是两根独立线（与表 9 原文一致）
  L_REQ: {channel: "2"}
  U_REQ: {channel: "3"}
```

而**安全相关的默认值不做成开关**：完整落位判定（`carrier_in_position` 与 `carrier_present`
分离）、停机安全态、握手方向锁定、故障锁存都是无开关的默认行为。
差异清单（D1–D14）见 [`docs/design/e84_python_library_design.md`](docs/design/e84_python_library_design.md) §16。

配置文件对照：

| 文件 | 拓扑 | 特点 |
|---|---|---|
| [`configs/example_2lp_standard.yaml`](configs/example_2lp_standard.yaml) | 两载口共用 PI/O | 标准场景完整示例：`CS_0`/`CS_1` 选口、同时 + 连续交接、`L_REQ`/`U_REQ` 共用 `OUT1` |
| [`configs/example_1lp_standard.yaml`](configs/example_1lp_standard.yaml) | 单载口独占 PI/O | 纯标准单载口：`CS_0` 恒 ON、`CS_1` 恒 OFF（表 3）、无同时交接；`sim` 可直接跑通 |
| [`configs/example_voc_compat.yaml`](configs/example_voc_compat.yaml) | 单载口 | **现场板卡接线复刻**：额外 `GO` 前置条件、低有效、独立 `L_REQ`/`U_REQ` 引脚、`access_mode: api`；`sim` 不会驱动 `GO`，因此会停在 `idle`/`ho_abort` 而**不是** PASS——这是预期行为 |

---

## 13. 追溯矩阵

| 标准条款 | 内容 | 实现模块 | 测试 |
|---|---|---|---|
| §6.1 表 1 | 信号定义、方向、`HO_AVBL`/`ES` 语义 | [`e84/signals.py`](e84/signals.py)、[`e84/model.py`](e84/model.py)、[`e84/hal.py`](e84/hal.py) | `tests/test_hal.py` |
| §6.1.2.3 / §6.1.2.4 | 载口指定（左/右手）、同时交接的 `CS_0`+`CS_1` | [`e84/fsm.py`](e84/fsm.py) `_decode_cs`、[`e84/config/validate.py`](e84/config/validate.py) | `tests/test_fsm_simultaneous.py`、`tests/test_config.py` |
| §6.2.2.1 | 单次交接时序（13 步、注 3/4/5） | [`e84/fsm.py`](e84/fsm.py) | `tests/test_fsm_single.py` |
| §6.2.3 | 同时交接（请求线「与」语义） | [`e84/fsm.py`](e84/fsm.py) `_select_operation` | `tests/test_fsm_simultaneous.py` |
| §6.2.4 | 连续交接（`CONT` 边沿采样、`TP6`/`TD1`） | [`e84/fsm.py`](e84/fsm.py)、[`e84/active/fsm.py`](e84/active/fsm.py) | `tests/test_fsm_continuous.py`、`tests/test_cli.py`（`sim --continuous`） |
| §6.2.5 | `HO_AVBL` 两个检查窗口与中止时序 | [`e84/fsm.py`](e84/fsm.py) `_available`/`_begin_abort`/`_handle_ho_abort`、[`e84/active/fsm.py`](e84/active/fsm.py) `check_ho_avbl` | `tests/test_fsm_single.py`、`tests/test_fsm_faults.py`、`tests/test_cli.py` |
| §6.3.2.1 | `TAx`/`TPx` 定时器与可编程设定值 | [`e84/timers.py`](e84/timers.py)、[`e84/fsm.py`](e84/fsm.py) | `tests/test_timers.py`、`tests/test_fsm_faults.py` |
| §6.3.3.1 | 标准不定义恢复 → 故障锁存 + 显式清除 | [`e84/fault.py`](e84/fault.py)、[`e84/fsm.py`](e84/fsm.py) `clear_fault` | `tests/test_fsm_faults.py`、`tests/test_cli.py`（`faults`） |
| §6.4.2.2 | 电源/信号隔离（硬线时被动侧与主动侧隔离） | 属**硬件接线约束**，库不实现电路；后端层与配置文档说明 | `configs/example_voc_compat.yaml` 注释、`tests/test_cli.py` |
| §6.4.6 | OFF = 无电流/无光（失效安全） | [`e84/model.py`](e84/model.py) `Outputs` 默认值、[`e84/hal.py`](e84/hal.py) `safe_state`、[`e84/config/validate.py`](e84/config/validate.py) 告警 | `tests/test_hal.py`、`tests/test_cli.py`（`selftest`） |
| §5.1 / §5.14 | 自动 / 手动访问模式 | [`e84/model.py`](e84/model.py) `AccessMode`、[`e84/loadport.py`](e84/loadport.py)、`policy.ho_avbl_when_manual` | `tests/test_config.py`、`tests/test_fsm_faults.py`、`tests/test_sensors.py` |

---

## 14. 目录速览

```text
e84/
├── __init__.py        公开 API 总览
├── cli.py             命令行工具（python -m e84.cli）
├── clock.py           Clock / RealClock / VirtualClock
├── signals.py         逻辑信号与方向（§6.1 表 1）、db25_pins（表 9，仅文档）
├── model.py           Inputs / Outputs / PortSnapshot / 枚举
├── fsm.py             ★ 纯逻辑状态机（协议核心）
├── timers.py          TP1–TP6 / TA1–TA3 / TD0 / TD1 与范围校验
├── hal.py             逻辑信号 ↔ 物理通道、极性、去抖、安全态
├── sensors.py         载具/门/夹持/访问模式传感量
├── loadport.py        载口运行时模型
├── controller.py      单个 PI/O 控制器
├── equipment.py       设备门面（多 PI/O）
├── runner.py          ManualRunner / ThreadRunner
├── fault.py           FaultCode / Fault / ConfigError
├── events.py          EventType / Event / EventBus
├── trace.py           TraceRecorder（CSV / JSONL）
├── config/            配置模型、加载器、交叉校验
├── io/                后端：null / memory / sim / libgpiod / rpi
├── active/            主动侧参考实现（仅自测/回放）
└── testing/           VirtualRig 虚拟线束
configs/               示例配置（2LP / 1LP / VOC 现场兼容）
tests/                 pytest 测试
docs/                  规范中文图文版、设计文档、验证记录、现场投运清单
examples/              可直接运行的示例（最小握手 / 连续交接）
```
