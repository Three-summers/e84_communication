# E84 被动端库 —— 架构详解

> 版本 `e84 0.1.0`　规范 SEMI E84-0301
> 本文讲"**为什么这样分层**"与"**数据怎么流**"，逐条给标准条款依据。
> 面向要接手、审查或移植这套代码的人。

---

## 1. 设计目标（决定了所有分层）

| 目标 | 含义 | 架构上的兑现方式 |
|---|---|---|
| **硬件解耦** | 同一套协议逻辑要能跑在 libgpiod、PLC/Modbus、MCU 串口桥、树莓派旧板、纯内存仿真上 | 协议核心**完全不接触 I/O**；I/O 被抽象成 `DigitalIO` 的"按通道名读写物理电平" |
| **完全可配置** | 引脚、极性、上拉、去抖、载具检测方式、访问模式来源、方向意图来源、定时器、故障策略，全部因现场而异 | 一切现场差异外置到配置；协议核心只吃 `Inputs` 这个纯数据 |
| **可测试 / 可复现** | 时序问题必须能在没有硬件、没有 `sleep` 的情况下逐拍复现 | 协议核心的 `step(now, Inputs) -> Outputs` **无墙钟、无线程、无 I/O**；时间由 `Clock` 注入 |
| **可审计** | 半导体客户要能拿标准条款逐条对 | 状态/迁移/事件/故障都带条款号；`tests/test_reference_data.py` 把标准表格常量钉死 |
| **失效安全** | 软件不跑、跑挂、输出不确定时，对方必须看到"不可交接 + 请求停止" | 输出默认值即安全态；停机/上电保持窗口；看门狗只在轮询推进时翻转 |

**一句话总结分层动机**：把"**协议正确性**"（会变、要审、要测）和"**硬件与现场**"（因客户而异、易错、难测）彻底隔开，让前者的正确性可以用纯粹的、可复现的方式证明。

---

## 2. 分层总览

```
┌───────────────────────────────────────────────────────────────────────────┐
│ L6  运行与工具   ManualRunner / ThreadRunner / cli.py                      │
│      ↳ 决定"谁来推进 poll()"，并保证停机一定回到安全态                       │
├───────────────────────────────────────────────────────────────────────────┤
│ L5  设备门面     equipment.py                                              │
│      ↳ 一台设备 = N 个 PI/O；聚合事件总线/时钟/追踪/看门狗                    │
├───────────────────────────────────────────────────────────────────────────┤
│ L4  控制器       controller.py                                             │
│      ↳ 一个物理 PI/O = 一个 FSM + 一个 HAL + 一个 SensorBank + N 个载口       │
│        职责：读 → 组装 Inputs → step → 写 → 发事件 → 记追踪 → 提供上层 API    │
├───────────────────────────────────────────────────────────────────────────┤
│ L3  协议核心     fsm.py                                    ★ 全库的心脏     │
│      step(now, Inputs) -> (Outputs, Events, Fault)                         │
│      无 I/O · 无墙钟 · 无线程 · 无可变全局 —— 纯逻辑                          │
├───────────────────────────────────────────────────────────────────────────┤
│ L2  信号层       hal.py（逻辑↔物理、极性、去抖、通道复用、安全态）             │
│                  sensors.py（载具/门/夹持/访问模式等本机传感量）              │
│      ↳ 把"配置里的字段"变成"每拍可读的布尔量"                                │
├───────────────────────────────────────────────────────────────────────────┤
│ L1  I/O 后端     io/base.py（抽象）· sim/memory/null · libgpiod · RPi.GPIO   │
│      ↳ 只认"通道名 → 物理电平"，惰性导入，绝不假设哪个电平是 ON               │
├───────────────────────────────────────────────────────────────────────────┤
│ L0  时钟         clock.py   RealClock / VirtualClock                       │
└───────────────────────────────────────────────────────────────────────────┘

横切（每层都可能用，但不反向依赖）：
  signals.py   逻辑信号 + 方向 + 表 9 参考引脚
  model.py     Inputs / Outputs / PortSnapshot / State / Op / 枚举
  timers.py    TP1–TP6 / TA1–TA3 / TD0 / TD1 + 范围强校验
  fault.py     FaultCode + 条款映射 + Fault
  events.py    EventType + Event + EventBus
  trace.py     TraceRecorder（CSV / JSONL）
  config/      配置模型 + 加载器(YAML/JSON/TOML) + 交叉校验

测试专用（不属于产品运行时）：
  active/fsm.py      主动侧参考实现（仅自测/对拖）
  testing/harness.py VirtualRig 虚拟线束
```

### 依赖方向（单向，不允许反向）

```
cli / runner / equipment / controller
        │
        ▼
      fsm.py  ──────────►  model.py · timers.py · fault.py · events.py
        │
        ▼
   hal.py · sensors.py ──►  io/base.py ──► clock.py
        │
        ▼
   config/ ──►  signals.py · model.py
```

**硬规则**：`fsm.py` 只 import `model`/`timers`/`fault`/`events`/`config.model`。
它**不**知道 `DigitalIO`、`SignalMap`、`SensorBank`、`LoadPort` 的存在。
违反这条，测试能力就会立刻退化（要么得造硬件，要么得 mock 一堆东西）。

---

## 3. 逐层详解

### L0 时钟 —— `clock.py`（72 行）

```python
class Clock(Protocol):      # now() -> float（单调秒）· sleep(seconds)
class RealClock:            # time.monotonic()
class VirtualClock:         # advance(dt)；sleep() 等价于推进时间，不真阻塞
```

**为什么单独一层**：协议核心吃的不是时间，是 `Inputs.now`。所以"2.5 秒后 TP1 超时"
这件事可以在**微秒级精度**上被断言，而不必真的等 2.5 秒。
这也是整套测试能在 **1.8 秒内跑完 262 项**的原因。

`ThreadRunner` 会检查时钟是不是 `RealClock` 并告警——虚拟时钟配线程只会空转。

---

### L1 I/O 后端 —— `io/`（约 600 行）

```python
class DigitalIO(ABC):
    def setup_input(channel, *, pull=PullMode.NONE) -> None
    def setup_output(channel, *, initial=False) -> None
    def read(channel) -> bool          # 物理电平：True = 高
    def write(channel, level: bool)    # 物理电平
    def read_output(channel) -> bool|None   # 支持回读时用于自检
```

| 后端 | 用途 | 备注 |
|---|---|---|
| `InMemoryIO` | 单元测试 / 仿真基座 | 双向：既当引脚，也当"外部世界"（`set_input`） |
| `SimIO` | 故障注入 | `stuck()` 卡死、`fail_writes()` 写失败、`open_circuit()` 开路 |
| `NullIO` | dry-run / 配置校验 | **按 pull 给出静态电平**，见下文"失效安全" |
| `LibGpiodIO` | 生产（树莓派 / Linux） | 兼容 libgpiod **v1 与 v2** 两代 API |
| `RPiGpioIO` | 旧板卡过渡 | BCM 编号；对应 VOC 现场 |

**关键设计**：后端**只认物理电平**，绝不假设"哪个电平是 ON"。
"ON 是高还是低"是接线事实，属于 L2 的配置。

**惰性导入**：`libgpiod` / `RPi.GPIO` 只在真正实例化时才 import。
所以在一台没有 GPIO 的 x86 上，`import e84` 与整个测试套件都能跑；
真要开真实后端时，抛的是带安装提示的 `RuntimeError`，而不是 `ImportError` 堆栈。

---

### L2 信号层 —— `hal.py`（372 行）

`SignalMap` 负责四件事，全部由配置驱动：

| 职责 | 配置字段 | 说明 |
|---|---|---|
| **映射** | `inputs/outputs: {信号: {channel: ...}}` | 逻辑信号 ↔ 物理通道 |
| **极性** | `active_high` | `ON = 高` 还是 `ON = 低`；**逐信号**可配 |
| **上拉/去抖** | `pull` / `debounce_ms` | 去抖采用"稳定才提交"，不是简单延时 |
| **安全态** | `safe_on` | 掉电/停控时该输出应处的逻辑值 |

再加两件工程上必要的事：

* **通道复用**：现场可能把 `L_REQ`/`U_REQ` 并在一条线上（表 9 原文里它们是独立引脚 1/2，
  但这是常见接法）。HAL 允许两个逻辑信号映射到同一 `channel`，**保证互斥**，
  并在两者同时为 ON 时直接抛错；校验器对这种复用只报**告警**、不拒绝启动。
* **写变化才写**：`apply()` 只在电平真的变了才调 `write()`，并累计 `write_count`。
  既降低总线负载，也让"是否发生多余写"变成可测的。

```python
# 逻辑 → 物理
level = logical if binding.active_high else (not logical)
# 物理 → 逻辑
logical = level if binding.active_high else (not level)
```

**失效安全的关键点**：`safe_state()` 把每个输出驱动到 `safe_on`（默认 `False`）。
对 `ES`/`HO_AVBL` 而言 OFF 就是"请求停止 / 不可交接"——**软件不跑的时候，
对方必须看到不可交接**，这是 §6.4.6（OFF = 无电流/无光）的直接兑现。

---

### L2.5 传感量 —— `sensors.py`（163 行）

E84 的 11 个信号线之外，还有一批**本机量**：载具在不在、坐没坐稳、门开没开、
夹持松没松、机构就绪没就绪、是自动还是手动。它们不该和 E84 信号混为一谈。

`SensorBank` 把配置里的 `SensorSpec` 变成每拍可读的布尔量，支持四种来源：

| `source` | 用途 | 现场例子 |
|---|---|---|
| `input` | 单个 GPIO 通道 | 一个到位传感器 |
| `keys` | 多通道组合，`mode: any \| all` | **三键方案**：任意键=检测到载具，三键全落=完整落位 |
| `external` | 上层通过 API 注入 | 来自 E87/E30 的载具状态 |
| `constant` | 固定值 | 没有该机构的载口 |

**为什么 `keys(any|all)` 是必需的**：SEMI E84 §6.1 表 1 说 `L_REQ` 要在"载具**位于正确位置**"
时才落下。"检测到载具"和"载具坐稳"是两个不同的事实——只用一个传感器（或把任意一键
当落位）就会在载具没坐稳时撤掉请求线，天车松手 → **掉片**。
这条是现场经验，也是旧实现里被专门修过的一个 bug（R02）。

同一个载口把同一批键同时用于 `any` 与 `all` 是**正常写法**，校验器放行。

---

### L3 协议核心 —— `fsm.py`（1152 行）★

**全库唯一真正复杂的地方，也是唯一没有 I/O 的地方。**

```python
@dataclass(frozen=True)
class Inputs:    # 主动侧信号 + 本机安全链 + 载口快照 + 现场自定义信号
    now: float
    valid, cs0, cs1, tr_req, busy, compt, cont: bool     # A→P
    am_avbl, va, vs0, vs1: bool                          # 跨区预留
    es_ok, external_ho_ok: bool                          # 本机安全链 / 外部许可
    ports: tuple[PortSnapshot, ...]
    extra: Mapping[str, bool]                            # 现场信号（如板级 GO）

@dataclass(frozen=True)
class Outputs:   # 逻辑电平；**默认值就是上电安全态**
    l_req, u_req, ready, ho_avbl, es: bool
    va, vs0, vs1: bool                                   # 跨区预留

class PassiveFsm:
    def step(self, inp: Inputs) -> StepResult            # ← 全部对外行为就这一个入口
```

`step()` 的骨架：

```
step(inp):
    try:
        outputs = _step_inner(inp, events)        # 状态分派，见下
    except _Raise as r:                           # 内部用异常表达"判定为故障"
        _enter_fault(r.code, r.message, inp, events)
        outputs = _fault_outputs(inp)
    _emit_derived_events(inp, events, outputs)    # 状态变化 / HO_AVBL / ES / 干涉区两个边沿
    _prev_valid = inp.valid; _prev_busy = inp.busy; _prev_armed = ...
    return StepResult(state, outputs, events, fault, selected, op, interlock_engaged)
```

#### 12 个状态与标准条款的对应

| 状态 | 标准依据 | 输出（`REQ`/`READY`/`HO_AVBL`/`ES`） |
|---|---|---|
| `DISABLED` | §6.4.6 失效安全 | 0/0/0/0（**全 OFF，含 ES**） |
| `IDLE` | §6.2.2.1 起点 | 0/0/1/1 |
| `SELECT` | §6.2.2.1(1)(2)、注 3 | 0/0/1/1 |
| `REQ_ON` | §6.2.2.1(3)(4)、`TP1` | 1/0/1/1 |
| `WAIT_BUSY` | §6.2.2.1(5)(6)、`TP2` | 1/1/1/1 |
| `TRANSFER` | §6.2.2.1(7)、`TP3` | 1→0/1/1/1 |
| `AWAIT_COMPT` | §6.2.2.1(8)(9)(10)、`TP4`、注 4 | 0/1/1/1 |
| `CLOSING` | §6.2.2.1(11)(12)(13)、`TP5`、注 5 | 0/0/1/1 |
| `CONT_NEXT` | §6.2.4.3、`TP6`/`TD1` | 0/0/1/1 |
| `HO_ABORT` | §6.2.5.2/.3、**图 19** | **保持**/0/0/1 |
| `FAULT_WAIT_CLOSE` | 相关信息 1 R1-1.1.2.1 | **保持**/0/0/0 |
| `FAULT_LATCHED` | §6.3.3.1 + 相关信息 1 | 0/0/0/0 |

#### 迁移图（主路径）

```
                    ┌──────────────────────────────────────────────┐
                    │                                              │
   DISABLED ──enable──► IDLE ──(VALID ∧ 前置条件)↑──► SELECT       │
                         ▲                              │         │
                         │                              │ 选口 + 定方向(锁定)
                         │                              ▼         │
                         │                          REQ_ON ──TP1──┤
                         │                              │ TR_REQ↑ │
                         │                              ▼         │
                         │                        WAIT_BUSY ──TP2─┤
                         │                              │ BUSY↑   │
                         │                              ▼         │
                         │                         TRANSFER ──TP3─┤
                         │                              │ 载具到位/取走 ⇒ 撤请求
                         │                              ▼         │
                         │                      AWAIT_COMPT ──TP4─┤
                         │                              │ COMPT↑  │
                         │                              ▼         │
                         │                         CLOSING ──TP5──┤
                         │                              │ VALID↓  │
                         │              ┌───────────────┴──────┐  │
                         │      段 CONT=ON                 段 CONT=OFF
                         │              ▼                     ▼  │
                         └──────── CONT_NEXT ◄─ TP6      ──► IDLE ┘
                                      │ 下一段 VALID↑
                                      └──► SELECT（重新选口）

   任意"检查窗口"内可用性丢失：SELECT / REQ_ON / WAIT_BUSY ──► HO_ABORT
   任意状态判定故障：        ──► FAULT_WAIT_CLOSE ──(VALID↓)──► FAULT_LATCHED
```

#### 五条实现约定（每条都是踩过坑才写下的）

1. **进度优先于超时**。每拍先看期望信号是否已到，再判定时器是否到期。
   否则"事件恰好落在截止时刻"会被误判成超时。
2. **触发条件是「`VALID` ∧ 全部前置条件」的**整体**上升沿**，不是 `VALID` 单独的。
   标准场景下两者等价；但现场一旦把板级联锁（如旧板的 `GO`）加进 `preconditions`，
   而 `GO` 在 `VALID` **之后**才成立，只看 `VALID` 边沿就会**永久漏掉这次握手**。
   "半路接上残留的 `VALID`"由 `VALID_STUCK` 超时兜底。
3. **`CONT` 是边沿采样**（§6.2.4.3）：在**每个** `BUSY` 上升沿采样并锁存；
   ON = 后面还有，OFF = 这是末段。带一个可配的稳定延时（`cont_sample_delay_ms`）
   容忍主动侧两根线的偏斜。
4. **方向在 `SELECT` 一次性确定并锁定**。之后不再读实时传感器——
   因为天车可能在 `BUSY` 阶段已经把载具提走了，按实时状态二次判定会把
   卸载走成装载。
5. **干涉区冻结严格由 `BUSY` 电平推导**（§6.1 表 1 `BUSY`："只要本信号为 ON…"）。
   不挂在"握手闭合"或"状态复位"上——那会让机构比标准要求多冻一段，
   或者在被卡住时一直冻到超时。

#### 定时器

`TP1`–`TP6` 全部实现，取值运行时可编程（`TimerSet.set_duration()`），
加载配置时按表 6 强校验范围。用**绝对截止时刻**而不是累加，避免轮询抖动被累积放大。
只有 `TP3`/`TP4` 的典型值是 **60 s**（等机械动作），其余是 2 s。

#### 故障与恢复

* **三类可指示的错误**（§6.3.1.1）：`HO_AVBL`（交接不可用）、`ES`（停止请求）、互锁超时。
* **被动侧出错时的输出画像**（相关信息 1 R1-1.1.2.1）：
  `READY`↓、`ES`↓、`HO_AVBL`↓，**其余信号保持在出错时刻的状态**（含请求线）。
  钉住请求线是为了**防止对方自主动作造成二次事故**。
* **锁存**：标准 §6.3.3.1 明确不定义恢复流程，所以默认锁存到显式
  `clear_fault()`。中途的 `FAULT_WAIT_CLOSE` 保证"故障后不做任何自主动作"。

---

### L4 控制器 —— `controller.py`（577 行）

一个物理 PI/O = 一个 `PiOController`（§3.4：时序图只对一个 PI/O 有效，各 PI/O 独立）。

它把 L1/L2/L2.5/L3 缝起来，**一拍做六件事**：

```
poll(now):
  ① 重入保护（非阻塞锁；回调里再调 poll 会返回上次结果而不是死锁）
  ② 上电安全保持窗口：t < _ready_at 时只写安全态，不进协议
  ③ 读传感量  sensors.read(io, now, external)      → {端口.量: bool}
  ④ 读 E84 输入 hal.read(now)（含去抖，带极性换算）+ 组装 PortSnapshot
       → 构建 Inputs（含把非标准信号塞进 extra，供 preconditions 用）
  ⑤ result = fsm.step(inp)
  ⑥ written = hal.apply(result.outputs)   # 写变化才写
       记追踪 / 发事件（含 OUTPUT_CHANGED）
```

对外 API 分三类：

| 类别 | 方法 |
|---|---|
| 生命周期 | `start` / `stop` / `poll` |
| **上层注入** | `set_access_mode` / `set_port_available` / `set_operation_intent` / `set_external_sensor` / `set_extra_input` / `set_es_ok` / `set_external_ho_ok` |
| 恢复原语 | `clear_fault(force=)` / `abort(reason)` / `raise_fault(code, msg)` |
| 只读 | `state` / `fault` / `outputs()` / `snapshot()` / `transfer_in_progress` / `handshake_active` / `interlock_engaged` |

**优先级规则**：API 覆盖 > 配置来源。这样现场既可以全由配置与 GPIO 决定，
也可以随时由上层（E87/E30 主机或设备主控）接管。

新增的 `transfer_in_progress` 来自 **SEMI E87 Table 8**（AUTO 交接边界 = `READY` 有效 →
交接完成），配套的 `set_access_mode(..., strict=True)` 用来满足 E87 §11.1.2
"访问模式不得在 carrier transfer 期间切换"。**默认 `strict=False`**，
因为"操作员切手动"往往正是要立即中止交接的安全动作，不能被守卫挡住。

---

### L5 设备门面 —— `equipment.py`（303 行）

一台设备 = N 个 PI/O。它做的事很少但必要：

* 按配置为每个接口建一个 `PiOController`（各自独立的后端实例）；
* 共用**一个** `EventBus`、**一个** `Clock`、**一个** `TraceRecorder`；
* 聚合：`faults` / `states` / `interlock_engaged` / `transfer_in_progress`；
* `clear_fault(interface_id=None)` → 清所有；`abort(None)` → 中止所有；
* **看门狗**：WDI 通道的翻转由 `Equipment.poll()` 驱动。
  一旦调用方的轮询停止（进程卡死、线程崩溃），WDI 停摆，外部看门狗把电路拉回安全态。
* **显式停机保护**：`stop()` 之后 `poll()` **不会**把设备重新拉起来，
  只把输出维持在安全态（否则残留的周期循环会让 `ES`/`HO_AVBL` 悄悄变回 ON，
  对外等于宣告"可以来交接了"）。

---

### L6 运行与工具

| 组件 | 说明 |
|---|---|
| `ManualRunner` | 由调用方决定何时推进。**`poll()` 不会隐式 `start()`**（同上，防意外重启）。适合嵌进自有实时循环 / PLC 扫描周期 / Qt 事件循环 / 单元测试 |
| `ThreadRunner` | 后台线程按 `poll_interval_ms` 轮询。幂等 `stop()`、有界 `join`、`pause/resume`、**卡死检测**（连续超期 → `WATCHDOG_STALL` 事件 + `on_stall` 回调） |
| `cli.py`（1366 行） | `validate`（含 `--show-map` 映射表）· `monitor` · `selftest` · `sim`（逐拍信号表 + `--trace-out`）· `force`（带安全联锁）· `faults` |

安全约定：真实后端（非 `null`/`memory`/`sim`）必须显式 `--i-know-what-i-am-doing`，
否则 `monitor` 自动降级为 `null`、`force` 直接拒绝；所有碰 I/O 的路径
`try/finally` + `stop()`/`safe_state()`。

---

### 横切模块

| 模块 | 作用 | 值得一提的设计 |
|---|---|---|
| `config/model.py`（665 行） | 配置 dataclass + 简写解析 | 支持 `VALID: IN1` 这种简写；`io_defaults` 避免逐信号重复；未知字段直接报错（防拼错） |
| `config/validate.py`（417 行） | 交叉校验 | 分**错误**（拒绝启动）与**告警**（提示）。例：`two_load_ports` 必须恰好 2 口且角色为 left/right；`TD0` 必须落在 0.1–0.2；E84 信号通道与传感量通道不得重叠；`ES.safe_on=True` 告警；轮询周期 >20 ms 告警（`TD0` 只有 100 ms） |
| `timers.py`（234 行） | 11 个定时器的规格与运行 | `TIMER_SPECS` 同时承载"监视区间/范围/典型值/归属方/条款"，是配置校验与文档的唯一来源 |
| `fault.py`（106 行） | 故障码 + 条款映射 | `Fault.clause` 自动带出条款号，日志/告警可直接引用 |
| `events.py`（193 行） | 事件总线 | 回调异常被捕获并记录，**绝不破坏时序**；可选历史环形缓冲用于故障后取证 |
| `trace.py`（158 行） | 时序追踪 | 长表格式（`t, interface, source, state, name, value`）；**只写变化**；分 `source=output`（逻辑）与 `source=channel`（物理电平）两类——后者是查"逻辑对但引脚写反"的关键 |

---

## 4. 一次 `poll()` 的完整数据流

以一次"装载"为例，从现场电平到现场电平：

```
 现场引脚
   │  ① setup_input/setup_output（start 时一次）
   ▼
 SimIO / LibGpiodIO                          ← 只有物理电平（True = 高）
   │  ② hal.read(now)：极性换算 + 去抖（稳定才提交）
   ▼
 {VALID: True, CS_0: True, ...}  +  sensors.read() → {LP1.carrier_present: False, ...}
   │  ③ LoadPort.snapshot()：合成 PortSnapshot（含可用性/访问模式/方向意图）
   ▼
 Inputs(now, valid=True, cs0=True, ..., es_ok=True, ports=(...), extra={GO: True})
   │  ④ PassiveFsm.step()  ← 纯逻辑，无副作用
   ▼
 StepResult(state=REQ_ON, outputs=Outputs(l_req=True, ho_avbl=True, es=True), events=[...])
   │  ⑤ hal.apply(outputs)：逻辑 → 物理，按通道分组、检查互斥、写变化才写
   ▼
 SimIO.write("OUT1", False)                   ← 低有效：逻辑 ON = 物理低
   │  ⑥ TraceRecorder.record(outputs) + record(channels) ；EventBus.emit_all(events)
   ▼
 上层：日志 / 告警 / 机构冻结 / 上位机
```

同一份 `Inputs` 序列喂给 `fsm.step()` 直接调用，与喂给这条完整链路，
**必须得到逐拍完全一致的结果** —— 这正是交叉验证 X1 断言的东西。

---

## 5. 可测试性是怎么被"设计"出来的

不是"写完再补测试"，而是三个决定让测试变得便宜：

| 决定 | 换来的能力 |
|---|---|
| 协议核心无 I/O、无墙钟 | 时序场景可在虚拟时钟上以微秒精度回放；262 项测试跑 1.8 秒 |
| 后端可替换 + 物理电平语义 | `VirtualRig` 能把"本库 + 主动侧参考实现 + 载口传感器"接成闭环，**且极性/通道复用真实走一遍** |
| 事件是数据、不是回调 | 可以断言事件的**类型序列**与**顺序**，而不是去 mock 日志 |

测试分层（共 18 个测试文件）：

| 层次 | 文件 | 抓什么 |
|---|---|---|
| 参考数据 | `test_reference_data.py` | 标准表格常量被抄错（Table 1/5/6/7/9 全部钉死） |
| 单元 | `test_timers` / `test_hal` / `test_sensors` / `test_config` | 各层自身的正确性 |
| 协议 | `test_fsm_single` / `_simultaneous` / `_continuous` / `_faults` | 逐状态、逐故障分支 |
| 集成 | `test_controller` / `test_runner` / `test_trace` / `test_cli` | 装配、生命周期、CLI 与安全联锁 |
| 交叉语义 | `test_interlock_and_stuck.py` | 干涉区冻结/释放配对、`VALID_STUCK`、字符串枚举归一化 |
| 主动侧 | `test_active_reference.py` | 参考实现自身的阶段推进与 `HO_AVBL` 窗口 |
| 黄金时序 | `test_golden_figures.py` | 图 10/11/14/16/17/18 的事件顺序 |
| **交叉验证** | `test_cross_validation.py` | **打破同源自测**：层级差分、表示矩阵、图驱动手写波形、反向交叉 |
| 第三方判据 | `test_vendor_criteria.py` | 商业仿真器手册公开的符合性判据 + E87 约束 |
| 模糊 | （`verification.md` §3bis 记录） | 48000 拍随机输入，7 条不变式零违例 |

---

## 6. 扩展点

想加东西时**只需要动一处**，这是分层是否成功的检验标准：

| 要加的东西 | 动哪里 |
|---|---|
| 新的 I/O 硬件（Modbus / MCU / 厂商 SDK） | 写一个 `DigitalIO` 子类 + 在 `io/__init__.open_backend` 注册；**协议核心一行不动** |
| 新的现场信号（如板级 `GO`） | 配置里加一行 `inputs`，需要时写进 `preconditions`。控制器会自动放进 `Inputs.extra` |
| 新的传感量（如"载具 ID 已读到"） | `LoadPortConfig` 加一个 `SensorSpec` 字段 + 在 FSM 的判定里用 |
| 新的行为策略（如超时后重试） | `PolicyConfig` 加字段 + `validate.py` 校验 + FSM 里读它 |
| **跨区被动 OHS 场景** | 信号与配置位**已预留**（`INTERBAY_SIGNALS`、`Scenario`、`VA/VS_0/VS_1/AM_AVBL`、`Outputs.va/vs0/vs1`），需实现 `_handle_*` 那一套与"用 `VA` 而非 `VALID` 作为闭合信号"的差异。校验器目前会**明确拒绝**这个 scenario，不会让你误以为支持 |
| 主动侧产品实现 | `active/fsm.py` 已经是可用的参考实现，但它明确标注"仅自测/对拖" |

---

## 7. 几条"为什么这样而不是那样"的取舍

| 取舍 | 选择 | 理由 |
|---|---|---|
| 回调 vs 事件对象 | 事件对象 + 总线 | 回调抛异常会炸掉时序；事件可断言、可回放、可序列化 |
| 阻塞 sleep vs 轮询 | 调用方驱动的轮询（默认 5 ms） | `TD0` 只有 100 ms，`sleep` 式等待会把响应抖动放大到不可控；轮询周期可配且被校验器盯着 |
| 状态机用异常表示故障 | 内部 `_Raise` | 故障判定散在几十个分支里，用异常把"判定"与"收尾"分开，避免每个分支都写一遍收尾逻辑 |
| 一个 PI/O 一个 FSM 实例 vs 全局一个 | 每 PI/O 独立 | §3.4 明确时序图只对一个 PI/O 有效；且多口设备的两个口可以同时在不同阶段 |
| 上位机接口（E87/E30）是否内置 | **不内置** | §2.3 明确交接由两侧设备共同管理、主机不参与。库对外只给窄接口（注入 + 事件），由上层适配 E87/E30 |
| 错误恢复流程 | **不定义**，只给原语 | §6.3.3.1 明确不定义；库提供 `abort` / `clear_fault` / `raise_fault` 三个原语，策略留给上层或操作员 |

---

## 8. 一处仍未闭合的缺口

**被动端与主动端都是同一个作者写的。** 已有五类交叉验证与第三方判据测试把
循环论证打破了大部分，但残余风险是"对标准的理解"仍是单点。
彻底闭合需要**现场真实波形**（逻辑分析仪记录）或**商用仿真器对拖**——
详见 [`third_party_references.md`](../third_party_references.md)。
这一点在 [`verification.md`](../verification.md) §5 里也如实标注，没有掩饰。
