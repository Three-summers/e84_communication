# E84 被动端（装备端）Python 库 —— 设计草案 v0.1

> 状态：**待评审**。本文只做设计，不含实现代码。
> 依据：`docs/e84_standard_document_reference/semi_e84_0301_illustrated_zh.md`（SEMI E84-0301 中文图文版），
> 并对原文时序图 fig_10 / fig_12 / fig_14 / fig_16 / fig_18 / fig_20 做了逐图复核。
> 目标：可配置、可测试、可审计、可现场排故的**被动侧（Passive / 装备侧）**E84 PI/O 协议栈。

---

## 1. 范围与目标

### 1.1 做什么

| 编号 | 目标 | 说明 |
|---|---|---|
| G1 | 被动侧信号时序完整实现 | 单次、同时、连续三类交接；装载与卸载两个方向 |
| G2 | 被动侧定时器 | `TP1`…`TP6` 全部实现，且**全部可由用户编程**（§6.3.2.1） |
| G3 | 错误指示 | `HO_AVBL`（交接不可用）、`ES`（停止请求）、互锁超时三类（§6.3.1.1） |
| G4 | 两套场景 | 标准场景（装备=被动，OHT/AGV/RGV=主动）；跨区场景（OHS=被动，储料机=主动） |
| G5 | 完全可配置 | 载口拓扑、引脚映射、极性、定时器、载具检测、访问模式、故障策略全部走配置 |
| G6 | 与硬件解耦 | 协议核心不含任何 I/O 与真实时钟，可离线仿真与回归测试 |
| G7 | 可审计 | 每个状态/迁移/测试都能追溯回标准条款 |

### 1.2 明确不做

- **不做上位机接口**：E87 CMS / E30 GEM / E23 的报文层不在范围内（§2.3：交接由两侧设备共同管理，主机不参与交接）。库对外只提供"意图输入"与"事件输出"的窄接口，便于上层适配 E87/E30。
- **不做交换式交接（swap handoff）**：§3.6 明确超出标准；同一载口"先卸后装"必须走连续交接序列。
- **不定义错误恢复流程**：§6.3.3.1 明确不定义。库提供"中止 / 重试 / 清故障"这类**原语**，恢复策略交给上层或操作员。
- **不做主动侧产品实现**。但会提供一个**测试用的主动侧模拟器**（仅测试依赖，不进运行时）。

---

## 2. 规范复核结论（直接影响设计的 12 个发现）

| # | 发现 | 出处 | 设计影响 |
|---|---|---|---|
| F1 | 时序图只对一个 PI/O 有效，各 PI/O 相互独立 | §3.4 | 每个 PI/O 一个独立 FSM 实例 + 独立定时器；设备级只做聚合 |
| F2 | 跨区场景**角色反转**且换一套信令（`VA`/`VS_0`/`VS_1`/`AM_AVBL`），且由被动方先发话 | §6.2.2.2、fig_12 | 场景是配置项；FSM 的"首发言方/触发条件"参数化 |
| F3 | ~~表 9 中 `READY` 与 `VS_0` 共用引脚 4~~ → **核对 SEMI E84-0301 原文 Table 9 后发现：原文每个信号各占一个引脚，没有任何共用**。此前依据的是一份错排该表的中文译文；正确映射为 `U_REQ`=2、`VA`=3、`READY`=4、`VS_0`=5 | 原文 Table 9 | 结论不变且更强：**逻辑信号与物理引脚必须解耦**，库不得硬编码引脚表。也说明「只依据二手译文」本身就是风险 |
| F4 | ~~表 9 中 `L_REQ` 与 `U_REQ` 共用引脚 1~~ → **原文为 `L_REQ`=1、`U_REQ`=2，各自独立** | 原文 Table 9 | 仍支持「一条线 + 方向语义」这种**现场接法**（有些设备把两根并在一条线上），但那是接线选项，不是标准引脚分配 |
| F5 | `ES` **低有效**（ON=正常，OFF=请求停止）；`HO_AVBL` 故障拉低，是 fail-safe | §6.1.1 表 1、fig_18 | 极性逐信号可配；上电/故障的安全态必须是 `ES=OFF` + `HO_AVBL=OFF` |
| F6 | **连续交接不是"`VALID` 一直为 ON"**：fig_16 显示两段之间 `VALID` 与 `CS_x` 都落下再抬起；`TD1`/`TP6` 正是"`VALID` OFF→ON"的延时/监视定时器 | fig_16 vs 译文脚注 | 连续交接 = **N 次完整单次交接的拼接**；FSM 每段都回到"等 `VALID`↑" |
| F7 | `CONT` 在**第一次**交接的 `BUSY`↑ 置 ON、在**最后一次**交接的 `BUSY`↑ 置 OFF | §6.2.4.3、fig_16 | 被动侧在**每个 `BUSY`↑ 采样并锁存 `CONT`**：ON=后面还有，OFF=这是最后一段 |
| F8 | `VALID` 置 ON 前不得校验 `CS_x`；`COMPT`/`VALID`/`CS_x` 释放顺序任意，不得报错 | 注 3、注 5 | 选口解码只在 `VALID`↑ 后做；收尾态不得对释放顺序做断言 |
| F9 | 同时交接时 `L_REQ`/`U_REQ` 是**多载口的与语义**（两个都空了才 ON，两个都到位了才 OFF） | §6.2.3.2、fig_14 | 请求信号由"所选载口集合"聚合计算，而不是单载口 |
| F10 | 被动侧出错时：`READY`↓、`ES`↓、`HO_AVBL`↓，**其余信号保持在出错时刻状态** | 相关信息 1，R1-1.1.2.1 / R1-2.1.1.2 | 故障态输出画像固定；"保持"是默认策略但可配 |
| F11 | `HO_AVBL` 恢复必须在 `VALID`↓ **之后**，且此时 `L_REQ`/`U_REQ` 已 OFF | §6.2.5.2/§6.2.5.3/§6.2.6.2 | 复位链路必须串行：先闭合握手，再恢复可用 |
| F12 | `TP3`/`TP4` 典型值 **60 s**，其余多为 2 s；`TD0` 唯一以 0.1 s 为量级 | 表 6/表 7 | 定时器分级；默认值按标准填写，并做范围校验（1–999 s，`TD0` 0.1–0.2 s） |

> 另外记录一处**原文/译文不一致**：译文脚注称"连续交接期间 `VALID` 保持 ON"，与 fig_16 及 `TD1`/`TP6` 定义矛盾。**以图与定时器表为准**（F6）。
> 另记录**表 9 与 fig_12 矛盾**（F3）：实际接线必须以现场规格书为准，因此本设计把引脚映射完全外置。

---

## 3. 架构：六层，I/O 与协议彻底分离

```
┌──────────────────────────────────────────────────────────────┐
│ L6 运行与工具：ManualRunner / ThreadRunner / CLI 监视器 / 追踪  │
├──────────────────────────────────────────────────────────────┤
│ L5 设备聚合：Equipment（N 个 PI/O）、配置加载与校验、事件总线    │
├──────────────────────────────────────────────────────────────┤
│ L4 PI/O 控制器：一个物理口 = 一个 FSM + 定时器 + 载口模型 + 故障锁存 │
├──────────────────────────────────────────────────────────────┤
│ L3 协议核心（纯逻辑）：step(now, Inputs) -> (Outputs, Events)     │
│    无 I/O、无墙钟、无线程；完全确定性                             │
├──────────────────────────────────────────────────────────────┤
│ L2 信号层 HAL：逻辑信号 ↔ 物理通道、逐信号极性、写变化才写、读缓存 │
├──────────────────────────────────────────────────────────────┤
│ L1 I/O 后端：DigitalIO 抽象 + sim / null / libgpiod / sysfs /    │
│    Modbus-TCP / 串口桥 / 自定义回调                               │
├──────────────────────────────────────────────────────────────┤
│ L0 时钟：Clock 抽象 + RealClock / VirtualClock（测试用）          │
└──────────────────────────────────────────────────────────────┘
```

**分层的第一原则**：L3 是纯函数式的状态机，`step()` 的输入输出全是不可变 dataclass。
这样做的收益：

- 全部时序场景可以在**虚拟时钟**上以微秒级精度回放并断言，无需真实硬件、无需 `sleep`；
- 可以把 fig_10–fig_20 逐图写成"黄金时序"测试；
- 换 I/O 后端（GPIO → PLC → 仿真）零改动；
- 现场可以录一段输入波形喂给核心复现故障（离线复现）。

---

## 4. 目录结构（建议）

```
e84_communication/
├── pyproject.toml
├── README.md
├── docs/
│   ├── design/e84_python_library_design.md      # 本文
│   └── e84_standard_document_reference/         # 已有规范
├── src/e84/
│   ├── __init__.py            # 公开 API 导出
│   ├── version.py
│   ├── signals.py             # LogicalSignal 枚举、方向、语义、默认极性
│   ├── config/
│   │   ├── model.py           # dataclass 配置模型（Equipment/PiOInterface/LoadPort/Timers/SignalMap/FaultPolicy）
│   │   ├── loader.py          # YAML / JSON / TOML / dict 加载
│   │   └── validate.py        # 交叉校验（拓扑 × 场景 × 信号集 × 引脚唯一性 × 定时器范围）
│   ├── io/
│   │   ├── base.py            # DigitalIO / OutputChannel / InputChannel 抽象
│   │   ├── sim.py             # SimIO：内存引脚 + 可注入毛刺/延迟
│   │   ├── null.py            # NullIO：空实现，用于 dry-run
│   │   ├── libgpiod.py        # 可选依赖，import 失败时惰性报错
│   │   ├── sysfs.py           # 老内核兜底
│   │   ├── modbus.py          # 远程 I/O / PLC
│   │   └── loopback.py        # 引脚回环自检
│   ├── hal.py                 # SignalMap：逻辑信号 → 物理通道 + 极性 + 反相 + 互斥
│   ├── clock.py               # Clock / RealClock / VirtualClock
│   ├── timers.py              # Deadline 定时器集合、TP1..TP6、TD0/TD1
│   ├── fsm.py                 # ★ 协议核心（纯逻辑状态机）
│   ├── model.py               # Inputs / Outputs / PortSnapshot / Events 等不可变数据
│   ├── loadport.py            # 载口模型：载具状态、门/夹持、访问模式、可用性聚合
│   ├── fault.py               # 故障分类、锁存、故障态输出画像、清除条件
│   ├── events.py              # 事件总线（同步回调 + 可选异步队列）
│   ├── trace.py               # 信号跳变记录器（CSV / VCD / JSONL）
│   ├── controller.py          # ★ PiOController：L4 组装（读 HAL → step → 写 HAL）
│   ├── equipment.py           # Equipment 门面（多 PI/O 聚合）
│   ├── runner.py              # ManualRunner / ThreadRunner
│   ├── testing/
│   │   ├── active_sim.py      # 主动侧模拟器（按标准实现，仅测试用）
│   │   ├── scenarios.py       # fig_10..fig_20 的黄金时序脚本
│   │   └── harness.py         # 虚拟线束：被动端 ↔ 主动端对接
│   └── cli.py                 # e84-monitor / e84-sim / e84-selftest / e84-validate
├── configs/
│   ├── example_1lp_standard.yaml
│   ├── example_2lp_standard.yaml
│   └── example_interbay_ohs_passive.yaml
└── tests/
    ├── unit/                  # FSM、定时器、配置校验、HAL 极性
    ├── golden/                # 逐图时序回放（fig_10..fig_20）
    ├── integration/           # 虚拟线束端到端
    └── hardware/              # 打上 hardware 标记，CI 默认跳过
```

---

## 5. 数据模型（L3 的输入输出契约）

```python
class AccessMode(Enum):     AUTOMATIC = "automatic";  MANUAL = "manual"
class Topology(Enum):       ONE_LP = "one_load_port"; TWO_LP = "two_load_ports"
class Scenario(Enum):       STANDARD = "standard";    INTERBAY_PASSIVE_OHS = "interbay_passive_ohs"
class Op(Enum):             LOAD = "load";            UNLOAD = "unload";  UNKNOWN = "unknown"
class PortRole(Enum):       SINGLE = "single"; LEFT = "left"; RIGHT = "right"

@dataclass(frozen=True)
class PortSnapshot:
    port_id: str
    role: PortRole
    carrier_present: bool          # 载具存在（任意位置）
    carrier_in_position: bool      # 载具位于正确位置
    door_open: bool                # 门/闸门已开（无门时恒 True）
    clamp_released: bool           # 夹持已释放（无夹持时恒 True）
    ready_for_transfer: bool       # 本载口自身机构已就绪（可含机器人空闲等外部条件）
    available: bool                # 本载口无错误、非手动、未被禁用
    access_mode: AccessMode

@dataclass(frozen=True)
class Inputs:                      # 全部来自主动侧信号 + 本机传感器/上层
    valid: bool = False
    cs0: bool = False
    cs1: bool = False
    tr_req: bool = False
    busy: bool = False
    compt: bool = False
    cont: bool = False
    am_avbl: bool = False          # 仅跨区被动 OHS
    va: bool = False               # 仅跨区
    es_ok: bool = True             # 本机安全链健康（E-Stop 未按、互锁正常）
    external_ho_ok: bool = True    # 可选的外部"可交接"条件
    now: float = 0.0               # 单调秒
    ports: tuple[PortSnapshot, ...] = ()

@dataclass(frozen=True)
class Outputs:                     # 被动侧驱动的全部信号（逻辑态）
    l_req: bool = False
    u_req: bool = False
    ready: bool = False
    ho_avbl: bool = False          # 注意：逻辑上 ON=可交接；物理上失效拉低
    es: bool = False               # 逻辑上 ON=正常
    va: bool = False
    vs0: bool = False
    vs1: bool = False
```

`Outputs` 是**逻辑电平**。物理极性（`active_high: false` 表示"ON ⇒ 输出低"）由 L2 HAL 处理。

---

## 6. 配置模型

设计原则：**一切会因现场接线/机型而变的东西都必须可配**；配置有 schema 版本号；加载时做交叉校验并在错误信息里给出人类可读的原因与所在字段。

```yaml
schema_version: 1
equipment:
  name: "ETCH-01"
  poll_interval_ms: 5          # 轮询周期，需远小于 TD0(100ms)
  boot_safe_hold_ms: 200       # 上电后先保持安全态的时间
  watchdog: {gpio: WDI, period_ms: 100}   # 可选
  trace: {enabled: true, format: csv, path: /var/log/e84/trace.csv, rotate_mb: 32}
  log: {level: INFO, json: true}

interfaces:
  - id: PIO1
    scenario: standard
    topology: two_load_ports
    features: {simultaneous: true, continuous: true}
    load_ports:
      - id: LP1
        role: left
        carrier_present:   {source: gpio, channel: CARRIER1, active_high: true, debounce_ms: 20}
        carrier_in_position:{source: gpio, channel: CPOS1,   active_high: true}
        door:              {source: gpio, channel: DOOR1,    active_high: false, control: output}
        ready_for_transfer:{source: gpio, channel: LP1RDY,   active_high: true}
        access_mode:       {source: static, value: automatic}   # 或 host / gpio
        operation_intent:  {source: derived}                    # derived|host|static
      - id: LP2
        role: right
        # ...同上
    signals:
      outputs:
        L_REQ:   {channel: OUT1, active_high: false}   # 表 9 原文：引脚 1
        U_REQ:   {channel: OUT2, active_high: false}   # 表 9 原文：引脚 2（现场若并成一条线则写同一个 channel）
        READY:   {channel: OUT4, active_high: false}
        HO_AVBL: {channel: OUT7, active_high: false}
        ES:      {channel: OUT8, active_high: false, fail_safe: false}
      inputs:
        VALID: {channel: IN1, active_high: false, debounce_ms: 3}
        CS_0:  {channel: IN2, active_high: false}
        CS_1:  {channel: IN3, active_high: false}
        TR_REQ:{channel: IN5, active_high: false}
        BUSY:  {channel: IN6, active_high: false}
        COMPT: {channel: IN7, active_high: false}
        CONT:  {channel: IN8, active_high: false, sample_on: busy_rising}
    timers:
      TP1: {value_s: 2}
      TP2: {value_s: 2}
      TP3: {value_s: 60}
      TP4: {value_s: 60}
      TP5: {value_s: 2}
      TP6: {value_s: 2}
      TD1: {value_s: 1, mode: monitor}     # 监视用；产生方是主动侧
    policy:
      invalid_cs: fault            # fault | ignore
      intent_mismatch: fault       # fault | follow_sensor
      simultaneous_mismatch: fault
      tp6_timeout: idle            # idle | fault
      on_timeout: fault            # fault | ho_abort
      request_hold_on_fault: true  # 遵循 R1-1.1.2.1"其余信号保持"
      es_latched_on_fault: true
      ho_avbl_when_manual: false
      close_needs_compt_off: false # 注 5：释放顺序任意
```

交叉校验清单（加载即报错）：

1. `topology=one_load_port` 时不得配 `role: right`；`CS_1` 必须恒 OFF（§6.1.2.1）。
2. `scenario=interbay_passive_ohs` 时必须配 `VA/VS_0/VS_1/AM_AVBL`，且**禁止**把 `VALID/CS_0/CS_1` 作为握手信号（§6.1 脚注）。
3. 同一物理通道不得被两个输出同时占用，除非显式声明 `shared`（如 `REQ` 的 `L_REQ/U_REQ`）。
4. `simultaneous=true` 需要 `two_load_ports`（§6.1.2.4）。
5. 定时器范围：`TP*`/`TD1` ∈ [1, 999] s，`TD0` ∈ [0.1, 0.2] s（表 5–表 7）。
6. `poll_interval_ms` 必须显著小于 `TD0`，否则给出告警（§6.3.2.3 的精确互锁意图）。
7. 极性/失效方向检查：`ES`、`HO_AVBL` 的 `fail_safe` 必须与现场安全链一致，否则告警。

---

## 7. 状态机（L3 核心）

### 7.1 状态与输出

| 状态 | 含义 | `REQ` | `READY` | `HO_AVBL` | `ES` |
|---|---|---|---|---|---|
| `DISABLED` | 未启用/已停止（上电安全态） | 0 | 0 | 0 | 0 |
| `IDLE` | 健康待命 | 0 | 0 | 1 | 1 |
| `SELECT` | `VALID`↑ 后解码 `CS_x`（注 3：此前不看 `CS`） | 0 | 0 | 1 | 1 |
| `REQ_ON` | 已发"请求"（装/卸由意图+载具状态决定） | 1 | 0 | 1 | 1 |
| `WAIT_BUSY` | `READY` 已 ON，等 `BUSY`↑（TP2） | 1 | 1 | 1 | 1 |
| `TRANSFER` | `BUSY`＝ON，等载具到位/取走（TP3） | 1→0 | 1 | 1 | 1 |
| `AWAIT_COMPT` | 请求已落，等 `COMPT`↑（TP4 同时监视 `BUSY`↓/`TR_REQ`↓） | 0 | 1 | 1 | 1 |
| `WAIT_COMPT` | 等 `COMPT`↑ | 0 | 1 | 1 | 1 |
| `CLOSING` | `COMPT`↑→`READY`↓，等 `VALID`↓（TP5） | 0 | 0 | 1 | 1 |
| `CONT_NEXT` | 连续交接中场，等下一次 `VALID`↑（TP6/TD1） | 0 | 0 | 1 | 1 |
| `HO_ABORT` | 本机拉低 `HO_AVBL`，等 `VALID`↓ 后恢复 | 0 | 0 | 0 | 1 |
| `FAULT_WAIT_CLOSE` | 故障中且握手未闭合 | 保持 | 0 | 0 | 0 |
| `FAULT_LATCHED` | 握手已闭合，故障锁存，需显式清除 | 保持 | 0 | 0 | 0 |

`L_REQ` / `U_REQ` 在原文里是**两个独立引脚**（1 / 2）。现场若把两者并在一条线上，
配置里写同一个 `channel`，HAL 会保证互斥。

### 7.2 主流程（标准场景 · 单次 · 装载，对应 fig_10）

```
IDLE ─ VALID↑ ─▶ SELECT
  SELECT: 解码 CS_x → 选中载口集合 S；校验组合合法性
          按载具状态与上层意图定 op（LOAD/UNLOAD）
          计算 REQ = AND_over_S(可装载) 或 AND_over_S(可卸载)
          REQ=1 → REQ_ON（启动 TP1）；REQ 无法成立 → FAULT(HO_UNAVAILABLE)
  REQ_ON:  等 TR_REQ↑（TP1 超时 → FAULT(TP1)）
           S 全部 ready_for_transfer → READY=1，启动 TP2 → WAIT_BUSY
  WAIT_BUSY: 等 BUSY↑（TP2 超时 → FAULT(TP2)）
           BUSY↑ 时锁存 CONT：
              cont_seen = CONT ； is_last_segment = (CONT == 0)
           启动 TP3 → TRANSFER
  TRANSFER: 装载 → 等 S 全部 carrier_in_position；卸载 → 等 S 全部 !carrier_present
           （TP3 超时 → FAULT(TP3)）
           到位后 REQ=0，启动 TP4 → WAIT_BUSY_OFF
  AWAIT_COMPT: 等 COMPT↑（TP4 超时 → FAULT(TP4)）
           （注 4：只在 COMPT↑ 之后才校验 BUSY/TR_REQ 是否已落下）
           → READY=0，启动 TP5 → CLOSING
  CLOSING:  等 VALID↓（TP5 超时 → FAULT(TP5)）
           （注 5：COMPT/VALID/CS 落下顺序任意，不做断言）
           若 cont_seen 且非末段 → CONT_NEXT（启动 TP6）
           否则 → IDLE
  CONT_NEXT: 等下一次 VALID↑（TP6 超时 → policy.tp6_timeout）
           回到 SELECT（重新解码 CS_x：fig_16 显示同一口/另一口都可能）
```

### 7.3 同时交接（fig_14）

与单次**唯一**差别在 `SELECT` 与 `TRANSFER` 两处聚合逻辑：

- `CS_0=1 且 CS_1=1` ⇒ 选中集合 `S = {左, 右}`；
- 装载：`REQ=1` 当且仅当 **两个载口都为空且可装载**；`REQ=0` 当且仅当 **两个载口都检测到载具到位**；
- 卸载：`REQ=1` 当且仅当 **两个载口都载具在位**；`REQ=0` 当且仅当 **两个载口都被取走**；
- 单个 PI/O 的 `CONT` 语义不变。

### 7.4 连续交接（fig_16 / fig_17）

- 连续 = N 次完整单次握手，**段间 `VALID` 与 `CS_x` 都落下再抬起**（F6）；
- 末段判定：在每段 `BUSY`↑ 采样 `CONT`，`CONT=OFF` 即末段（F7）；
- 中场由 `TP6` 监视下一次 `VALID`↑；`TD1`（监视模式）记录实际间隔用于排故；
- 有门/闸门的载口：整批过程中门保持开启（§6.2.4.2）。库通过事件 `ContinuousBatchStarted/BatchEnded` 通知上层，由上层或本库的 `door` 输出控制；门开合动作本身**必须在 `BUSY`＝ON 期间冻结**（§6.1.1 `BUSY` 说明）。

### 7.5 `HO_AVBL` 与故障（fig_18 / fig_19 / fig_20）

- `HO_AVBL` = `available` 的合成：`es_ok ∧ external_ho_ok ∧ ¬fault_latched ∧ access_mode==AUTO ∧ 所有载口 available`；
- 一旦在窗口 a（`VALID`↑→`REQ`↑）或窗口 b（`TR_REQ`↑→`READY`↑）内可用性丢失，进入 `HO_ABORT`：
  `READY=0`、`HO_AVBL=0`，而请求线**保持原状**（窗口 b 里它已经是 ON，就继续保持；窗口 a 里它还没断言，自然就是 OFF）；
- 顺序严格按**图 19**：`VALID`↓ → 请求线↓ → `HO_AVBL`↑。也就是仅在 `VALID`↓ **且** `REQ=0` 之后才恢复 `HO_AVBL=1`（F11）；
- 跨区场景（fig_20）改为：拉低 `HO_AVBL` → 主动侧关 `AM_AVBL` 结束时序 → `VS_0/VS_1`↓ → `VA`↓ → 才恢复 `HO_AVBL`。

### 7.6 故障画像（F10 / 相关信息 1）

- 被动侧自身出错：`READY=0`、`ES=0`（请求停止）、`HO_AVBL=0`，其余输出**保持在出错时刻的状态**（可配 `request_hold_on_fault`）；
- 握手未闭合时保持 `FAULT_WAIT_CLOSE`，不做任何自主动作（对应"防止二次事故"）；
- 握手闭合后进入 `FAULT_LATCHED`，只接受显式 `clear_fault()`（可选要求 `VALID=OFF` 且无物理危险）；
- 故障分类：`TP1..TP6` 超时、`CS` 组合非法、意图与传感器矛盾、同时交接条件不满足、外部安全链断开、载口不可用、`VALID` 在非法状态出现等。

### 7.7 定时器

| 定时器 | 起 | 止 | 典型 | 超时后果 |
|---|---|---|---|---|
| `TP1` | `REQ`↑ | `TR_REQ`↑ | 2 s | `FAULT` |
| `TP2` | `READY`↑ | `BUSY`↑ | 2 s | `FAULT` |
| `TP3` | `BUSY`↑ | 载具到位/取走 | 60 s | `FAULT` |
| `TP4` | `REQ`↓ | `BUSY`↓ | 60 s | `FAULT` |
| `TP5` | `READY`↓ | `VALID`↓ | 2 s | `FAULT` |
| `TP6` | `VALID`↓ | `VALID`↑（连续） | 2 s | 可配：`IDLE`/`FAULT` |
| `TD1` | `VALID`↓ | `VALID`↑ | 1 s | 监视（产生方为主动侧） |

定时器全部由配置编程、范围校验；使用**绝对截止时刻**（`deadline = start + value`）而非累加，避免抖动累积。

---

## 8. 并发与调度

**推荐模型：核心同步 + 可插拔 Runner。**

- `ManualRunner`：用户在自有循环里调 `equipment.poll()`——确定性最好，适合嵌进实时线程或 PLC 扫描周期；
- `ThreadRunner`：库起一个线程按 `poll_interval_ms` 轮询，支持 `pause/resume/stop`、唤醒抖动统计；
- 预留 `AsyncRunner`（asyncio）接口，等需求确认再实现。

无论哪种 Runner，**同一 PI/O 的 `poll()` 不可重入**，写输出"变化才写"以减少总线负载。所有回调在 Runner 线程同步执行；回调抛异常被捕获并记为事件，不破坏时序。

---

## 9. 安全设计

| 项 | 措施 |
|---|---|
| 上电/异常安全态 | 库启动前硬件应呈现 `ES=OFF`（请求停止）+ `HO_AVBL=OFF`；库先进入 `DISABLED` 并保持 `boot_safe_hold_ms` 再转 `IDLE` |
| 失效方向 | `ES`/`HO_AVBL` 逐信号可配 `fail_safe`，默认"故障=OFF"（§6.4.6：OFF=无电流/无光） |
| 看门狗 | 可选硬件 WDI 翻转 + 轮询卡死检测（超过 `N × poll_interval` 未推进则进入 `DISABLED` 并拉低 `ES`/`HO_AVBL`） |
| 互斥 | 若现场把 `L_REQ`/`U_REQ` 并在同一通道，保证两者不会同时驱动；`BUSY=ON` 期间禁止任何"干涉区"动作（门/夹持/机器人）——由库发出"冻结"事件，上层机构必须遵守 |
| 故障不自恢复 | 默认锁存，避免"错误被静默吃掉"（§6.3.3.1 未定义恢复，故保守） |
| 输出核验 | 可选周期性回读（`loopback`），发现输出与期望不符则报 `IO_MISMATCH` |
| 单一所有者 | 可选 PID 文件锁，防止两个进程同时驱动同一组引脚 |

---

## 10. 可观测性

- **事件总线**：`HandshakeOpened/Closed`、`SegmentStarted/Completed`、`BatchStarted/Ended`、`CarrierDetected/Removed`、`HandoffAborted`、`FaultRaised/Cleared`、`TimerExpired`、`StateChanged`（含触发条款号）。
- **结构化日志**：JSON 行，字段含 `pi_o`、`state`、`event`、`clause`（如 `6.2.2.1#8`）、`signals`、`elapsed_ms`。
- **时序追踪**：`trace.py` 输出 CSV/VCD/JSONL，任意信号跳变带时间戳；可直接与 fig_10–fig_20 目视比对，也用于事后离线复现（把 CSV 回灌给虚拟时钟）。
- **CLI**：
  - `e84-validate config.yaml` —— 配置校验；
  - `e84-monitor --config ...` —— 实时状态/信号/定时器面板；
  - `e84-sim --config ... --scenario fig16` —— 被动端 + 主动端模拟器对跑，打印时序；
  - `e84-selftest` —— 回环/极性/去抖自检；
  - `e84-force` —— **带安全联锁**的引脚强制（仅在 `DISABLED` 且显式 `--i-know-what-i-am-doing` 下可用），用于现场接线调试。

---

## 11. 测试策略

| 层次 | 手段 | 覆盖 |
|---|---|---|
| 单元 | 虚拟时钟直驱 `fsm.step()` | 每个状态的迁移、每个定时器超时、每个故障分支、极性/去抖/`REQ` 互斥 |
| 黄金时序 | 逐图脚本化断言（`tests/golden/`） | fig_10 LOAD、fig_11 UNLOAD、fig_12/13 跨区、fig_14 同时、fig_16 连续卸→装、fig_17 连续装→装、fig_18/19 `HO_AVBL` a/b、fig_20 跨区 `HO_AVBL` |
| 集成 | 虚拟线束：被动端 ↔ `testing/active_sim.py` | 端到端握手；含"主动端提前释放信号""顺序乱序释放"（注 5）等鲁棒性用例 |
| 边界/性质 | 随机 + 时序扰动（去抖阈值附近抖动、`VALID` 毛刺、`CONT` 在 `BUSY` 前后的边缘情况） | 不出现死锁、`REQ` 不抖动、`HO_AVBL` 恢复顺序不被违反 |
| 硬件在环 | `pytest -m hardware`，回环 + 抓波形 | 真实 GPIO 延迟、极性接反探测 |

每个测试标注对应条款号，生成**追溯矩阵**（条款 → 状态/模块 → 测试）交付给客户审计。

---

## 12. 对外 API 草案

```python
from e84 import Equipment, EquipmentConfig, AccessMode, Op, Event

cfg = EquipmentConfig.from_yaml("configs/example_2lp_standard.yaml")
eq  = Equipment(cfg)                      # 默认自动选择后端；可 io=... 注入
eq.on(Event.HANDOFF_COMPLETED, lambda e: log.info("done %s", e.port_ids))
eq.on(Event.FAULT_RAISED,     lambda e: alarm.raise_(e.code, e.clause))
eq.start()                                # 内部起 ThreadRunner；或 runner=ManualRunner 自行 poll()

lp1 = eq.load_port("LP1")
lp1.set_access_mode(AccessMode.MANUAL)    # 上层/E87 调用；自动联动 HO_AVBL
lp1.set_operation_intent(Op.LOAD)         # 可选：由 host 指定当次意图

eq.poll()                                 # ManualRunner 场景
snap  = eq.snapshot()                     # 只读状态快照（状态机 + 信号 + 定时器剩余）
eq.clear_fault("PIO1")                    # 显式恢复原语
eq.abort("PIO1", reason="operator")       # 中止互锁时序（§6.3.3.1 的"中止"语义）
eq.stop()                                 # 回到 DISABLED 安全态
```

---

## 13. 里程碑

| 里程碑 | 内容 | 产出 |
|---|---|---|
| M1 | 骨架：配置模型/校验、信号与 HAL、`Clock`、`SimIO`、日志、pyproject、CI | 可加载配置并跑通空转 |
| M2 | 核心 FSM：单次装载/卸载 + `TP1`–`TP5` + 故障态 | fig_10/fig_11 黄金测试通过 |
| M3 | 同时交接 + 连续交接（`TP6`/`TD1`/`CONT` 锁存） | fig_14/16/17 黄金测试通过 |
| M4 | `HO_AVBL`/`ES` 策略、故障锁存与恢复原语、跨区场景（`VA/VS_*/AM_AVBL`） | fig_12/13/15/18/19/20 通过 |
| M5 | 运行器完善、事件/追踪、CLI、文档（中英）、追溯矩阵、打包发布 | 可交付 v1.0 |

---

## 14. 待确认问题（见对话）

1. 目标硬件与 I/O 后端
2. v1 需要覆盖的场景范围
3. 并发/Runner 模型
4. 依赖与配置格式约束
5. 载具检测与载口机械（门/夹持/机器人）
6. 装/卸意图从哪来（决定 `L_REQ`/`U_REQ` 语义）
7. 访问模式来源与 `HO_AVBL` 策略
8. 故障与恢复策略
9. 验证与工具链交付期望
10. Python 版本 / 包名 / 许可


---

## 15. 实现状态（v0.1.0）

包位于仓库根目录 `e84/`（按需求**不打包**，`PYTHONPATH=.` 或 `sys.path` 即可 import）。

| 模块 | 作用 | 状态 |
|---|---|---|
| `e84/signals.py` | 逻辑信号、方向、表 9 参考引脚 | ✅ |
| `e84/model.py` | `Inputs`/`Outputs`/`PortSnapshot`/`State`/`Op`/`PortRole` | ✅ |
| `e84/clock.py` | `RealClock` / `VirtualClock` | ✅ |
| `e84/fault.py` | 故障码 + 条款映射 + 锁存对象 | ✅ |
| `e84/timers.py` | `TP1`–`TP6`/`TD0`/`TD1`/`TA1`–`TA3`，范围强校验、运行时可编程 | ✅ |
| `e84/io/` | `DigitalIO` 抽象 + `memory`/`sim`/`null`/`libgpiod`/`RPi.GPIO` | ✅（后两者需现场验证） |
| `e84/hal.py` | 逻辑↔物理、逐信号极性、去抖、共用通道互斥、安全态、写变化才写 | ✅ |
| `e84/sensors.py` | `input`/`keys(any|all)`/`external`/`constant` 四种传感量来源 | ✅ |
| `e84/loadport.py` | 载口运行时：访问模式/可用性/意图的 API 覆盖优先级 | ✅ |
| `e84/config/` | 模型 + YAML/JSON/TOML/dict 加载 + 交叉校验 | ✅ |
| `e84/fsm.py` | ★ 纯逻辑被动侧状态机（单次/同时/连续 + 故障锁存） | ✅ |
| `e84/controller.py` | 单 PI/O 控制器（读→算→写→事件/追踪） | ✅ |
| `e84/equipment.py` | 多 PI/O 聚合 + 看门狗 | ✅ |
| `e84/runner.py` | `ManualRunner` / `ThreadRunner`（幂等停机 + 有界等待 + 卡死检测） | ✅ |
| `e84/events.py` / `e84/trace.py` | 事件总线 / CSV·JSONL 追踪 | ✅ |
| `e84/active/` | 主动侧**参考实现**（仅自测/对拖） | ✅ |
| `e84/testing/` | `VirtualRig` 虚拟线束（含真实极性、通道复用、载口仿真） | ✅ |
| `e84/cli.py` | `validate`/`monitor`/`selftest`/`sim`/`force` | ✅ |
| 跨区被动 OHS 场景 | `VA`/`VS_0`/`VS_1`/`AM_AVBL` 的 FSM | ⛔ 未实现（配置位已预留，校验器明确拒绝） |

### 15.1 已实测通过的闭环（主动侧参考实现 ↔ 本库）

`VirtualRig` 会把**物理电平**在两个后端之间搬运，因此逐信号极性、`ES`/`HO_AVBL` 的低有效、
以及**两个逻辑信号映射到同一物理通道**（现场接法）都被真实走了一遍：

| 场景 | 结果 |
|---|---|
| 单次 LOAD（fig_10） | ✅ 事件顺序 `demand_asserted → ready_asserted → transfer_started → carrier_settled → compt_received → handshake_closed` |
| 单次 UNLOAD（fig_11） | ✅ 载具被取走后请求线落下 |
| 同时 LOAD（fig_14） | ✅ `CS_0+CS_1` 选中两口；请求线仅在**两口都到位**后落下 |
| 连续 UNLOAD→LOAD 同载口（fig_16） | ✅ `batch_started` → 段间 `VALID` 落下再抬起 → 第二段自动判定为 LOAD → `batch_ended` → `idle` |
| 连续 LOAD→LOAD 不同载口（fig_17） | ✅ `CS_0` 段 → `CS_1` 段，两口均落位 |
| `TP1` 超时 | ✅ `FAULT_WAIT_CLOSE` → `FAULT_LATCHED`；`READY`/`ES`/`HO_AVBL` 拉低、请求线保持；`clear_fault()` 后回 IDLE |
| `HO_AVBL` 窗口 a 喊停（fig_18） | ✅ 撤请求 → `HO_ABORT`；`ES` 保持 ON；`VALID`↓ 且恢复可用后才回 IDLE |

---

## 16. 与 VOC_Project 既有实现的差异（为什么这不是"重写一遍旧代码"）

按需求确认：**旧实现只是参考物，可能有问题，一律以标准为准**。旧实现真正值得保留的是
"现场事实"（板级接线、三键语义、`GO` 信号存在、执行机构失败要联锁）与几条安全直觉；
它的时序逻辑与可配置性都需要换掉。

| # | 旧实现 | 标准依据 | 本库做法 |
|---|---|---|---|
| D1 | 只有 `CS_0`，没有载口选择 | §6.1.2.3 / §6.1.2.4、表 2/表 3 | 支持 1 口/2 口拓扑；`CS_0`/`CS_1` 解码为 left / right / 同时 |
| D2 | 无 `CONT`，无连续交接 | §6.2.4 | 支持连续交接；`CONT` 在**每个 `BUSY` 上升沿**采样并锁存 |
| D3 | 无同时交接 | §6.2.3 | 支持；请求线是所选中载口集合的**与**语义 |
| D4 | 只有 `ShortTimer=2s`/`LongTimer=60s`，各阶段复用 | 表 6/表 7 | `TP1`–`TP6`/`TD1` 独立命名、独立可编程、范围强校验 |
| D5 | 无 `HO_AVBL` 时序窗口；`HO_AVBL`/`ES` 由三键状态每 200 ms 静态重写 | §6.2.5.1 a/b、§6.1 表 1 | `HO_AVBL` = 可用性合成；仅在窗口 a/b 触发中止；**恢复严格在 `VALID`↓ 之后** |
| D6 | `ES` 不接真实安全链（由三键推导） | §6.1 表 1 `ES` 语义 | `ES` = 本机安全链健康（`set_es_ok`），故障时按相关信息 1 拉低 |
| D7 | 任意阶段 `GO`/`CS_0`/`VALID` 撤销即撤输出回 IDLE，且 `GO` 是**必需**前提 | §6.1（`GO` 不是标准信号） | 前置条件列表可配，**默认仅 `VALID`**；旧板行为可用 `preconditions: [VALID, CS_0, GO]` 一比一复现 |
| D8 | 超时/异常只发中文 warning 并回 IDLE，不锁存（只有执行机构错误锁存） | §6.3.3.1 不定义恢复 + 相关信息 1 | 故障**锁存**：`FAULT_WAIT_CLOSE` → `FAULT_LATCHED`，输出画像按 R1-1.1.2.1 |
| D9 | 上电默认"载具在位"，于是首个握手被判为 Unload | §6.1 表 1（应以"载具**位于正确位置**"判定） | 无隐式默认值；"检测到载具"与"完整落位"分离，来源全部显式配置 |
| D10 | 200 ms 轮询 + E84 输入零滤波 | §6.3.2.3（`TD0`=0.1 s） | 默认 5 ms 轮询；逐输入可配去抖；轮询周期过大时校验器告警 |
| D11 | 引脚/极性/扫描率硬编码；模块导入期硬依赖 `RPi.GPIO` | §6.4 | 全部外置到配置；后端**惰性导入**，`libgpiod` 优先、`RPi.GPIO` 兼容 |
| D12 | 协议与业务耦合（状态机直接发 `data_collection_start/stop`） | §2.3（交接由两侧设备共同管理） | 协议核心只发**语义事件**；采集/机械顺序由上层订阅事件实现 |
| D13 | 一次 `_process_state()` 同步推进 + Qt 定时器 | — | 纯函数式 `step(now, Inputs)`：无 I/O、无墙钟、无线程，可用虚拟时钟逐拍回放 |
| D14 | 状态机直接写真实 GPIO；测试必须伪造 `RPi` 模块 | — | HAL/后端抽象；测试用 `SimIO` + `VirtualClock` |

**保留下来的安全语义**（旧实现里做对了、这里继续强化的）：

1. **方向在握手瞬间锁定**（旧 B6）→ `SELECT` 阶段一次性确定并锁定，之后不再读实时传感器；
2. **装载完成必须"完整落位"而非"检测到载具"**（旧 R02）→ `carrier_in_position` 与
   `carrier_present` 分离，`L_REQ` 只在 `carrier_in_position` 成立后落下（否则会掉片）；
3. **停机必须撤回握手输出**（旧 R04）→ `stop()` 进入 `DISABLED`，全部输出回安全态；
4. **执行机构/通信失败必须进入故障锁存**（旧 R03）→ `PiOController.raise_fault()`。

**明确不照搬的**：旧项目文档里"安全项不得做成配置开关"这条约束，是针对**它自己的**安全需求
（R01/R02/R04）而写的；本库里与之等价的行为（完整落位判定、停机安全态、方向锁定）**同样是
无开关的默认行为**，但 `GO` 这类**现场新增**的前置条件属于接线事实，必须可配，否则无法适配
不同机型。

---

## 17. 待办

- 跨区被动 OHS 场景（`VA`/`VS_0`/`VS_1`/`AM_AVBL`，fig_12/13/15/20）的状态机；
- `AsyncRunner`；
- 硬件在环测试（`pytest -m hardware`）+ 与真实 AMHS 对拖;
- 打包（`pyproject.toml`）与 `console_scripts` 入口；
- 与现场真实抓取波形做逐点比对（把波形作为黄金数据）。


---

## 18. 需求确认记录（设计评审问答）

| # | 问题 | 结论 | 对实现的影响 |
|---|---|---|---|
| Q1 | 目标硬件与 I/O 后端 | 树莓派 / Linux + **libgpiod** | 默认后端 `libgpiod`（v1/v2 双兼容）；保留 `RPi.GPIO` 兼容后端与 `memory`/`sim`/`null` |
| Q2 | v1 场景范围 | **标准场景**（装备=被动）+ **主动侧参考实现**（自测/对拖）；跨区被动 OHS 暂不做 | 跨区信号与配置位已预留，但 `scenario: interbay_passive_ohs` 被校验器明确拒绝；`e84/active/` 提供主动侧 |
| Q3 | 并发模型 | 核心状态机 + **手动 `poll()`**，另附可选线程 Runner | `ManualRunner` / `ThreadRunner`；协议核心零 I/O、零墙钟 |
| Q4 | 依赖与配置格式 | **仅标准库**；YAML/JSON/TOML，YAML 缺失时给出降级提示 | 无第三方运行时依赖；`e84/config/loader.py` |
| Q5 | 载具检测 | 载具**到位**传感器必须有；需区分"检测到载具"与"完整落位"；参考 VOC 现场做法 | `carrier_present` 与 `carrier_in_position` 分离；`keys(any\|all)` 来源直接支持三键/多传感器方案 |
| Q6 | 装/卸意图来源 | **两者都支持、可配置**：默认由传感器推导，也允许上层指定并做一致性校验 | `IntentSpec`（`derived`/`api`/`static`）+ `policy.intent_mismatch`（`fault`/`follow_sensor`） |
| Q7 | 访问模式来源 | 上层 API + 配置文件静态值；**手动模式时自动拉低 `HO_AVBL`** | `AccessModeSpec`（`static`/`api`/`input`）+ `policy.ho_avbl_when_manual=False` |
| Q8 | 故障与恢复 | **锁存故障 + 显式 `clear_fault()`** | `FAULT_WAIT_CLOSE` → `FAULT_LATCHED`；`clear_fault(force=...)` |
| Q9 | 验证与工具链 | 虚拟时钟单元测试 + 逐图黄金时序测试 + 主动侧模拟器 + 端到端虚拟线束 + CLI（校验/监视/强制） | `e84/testing/`、`e84/active/`、`e84/cli.py`、`tests/` |
| Q10 | 打包 | **先不打包**，目录里能 import 即可 | 包直接位于仓库根 `e84/`，`PYTHONPATH=.` 即可；打包留待后续 |
| 附加 | 与 VOC_Project 的关系 | 旧实现**只是参考物、可能有错**，一律**以标准为准** | 见 §16 差异清单；现场事实（板级 `GO`、三键）通过配置表达，标准行为做默认 |


---

## 19. 实现期发现的三个设计盲点（已修正）

这三个问题都不是"写错了代码"，而是**设计阶段没想到的场景**；记录下来是因为它们
恰好都只在"接了现场配置"之后才暴露。

### 19.1 握手触发必须用「前提整体」的上升沿，而不是 `VALID` 单独的上升沿

标准场景下 `preconditions` 只有 `VALID`，两者等价；但只要现场把板级联锁（例如旧板的
`GO`）加进 `preconditions`，并且 `GO` 在 `VALID` **之后**才成立，就出问题：进入握手靠的是
`VALID` 上升沿，而那一刻前提还不成立，于是**这次握手被永久漏掉**——必须等对方把 `VALID`
先落下再抬起。配置看起来完全合法，现场表现是"死等"。

修正：触发条件改为 `VALID ∧ 全部前置条件` 这个**整体**的上升沿（`armed_rising`）；
"半路接上残留的 `VALID`" 仍由 `VALID_STUCK` 超时兜底。

### 19.2 绑定了通道的现场自定义信号必须真的送进协议核心

`GO` 这类信号在 `inputs:` 里绑定了通道、HAL 也读到了，但 `Inputs` 只填了 11 个标准信号，
现场信号没被传进去 → `Inputs.signal("GO")` 恒为 `False` → 前置条件永不满足。
修正：控制器把非标准输入信号统一放进 `Inputs.extra`（再叠加 `set_extra_input()` 的运行时注入）。

### 19.3 状态类量不该"手工置位/手工清除"，应当由输入电平推导

`interlock_engaged`（交接干涉区冻结）原本是在 `BUSY` 上升沿手工置位、在
`_reset_selection()`（握手闭合/状态复位）里手工清除。表面自洽，但**触发点挂错了**：

规范 §6.1 表1 对 `BUSY` 的措辞是"**只要本信号为 ON**，被动设备就不应在交接干涉区内
执行任何机械动作"。冻结应当严格等于 `BUSY` 的电平，而不是某个状态迁移的副作用。
挂错的后果有两个，方向相反但都错：上层机构从 `BUSY`↓ 一直冻到 `VALID`↓（比标准要求
多冻一段），而如果对方撤了 `BUSY` 却卡在后续步骤，机构会被冻到超时为止。

修正：改为**每拍由输入推导**（`interlock_engaged = inp.busy`），两个边沿都在派生事件里发出。

> 一般化的教训：凡是"描述外部世界的状态"，都应该由输入电平**推导**，而不是在
> 状态机的若干迁移点上手工维护——手工维护的点一定会漏掉、或者挂错时机。

### 19.4 中止期间不能"抢跑"撤下请求线

`HO_ABORT`（可用性丢失后的中止）最初实现成"进入即撤下请求线"。核对**规范图 19**
与商业仿真器的手册后发现这是错的：

* **图 19** 画的是：`VALID`↓（连同 `CS`↓/`TR_REQ`↓）在前，随后 `L_REQ`↓，最后 `HO_AVBL`↑；
* §6.2.5.2/.3 正文："The passive equipment turns the HO_AVBL signal ON after the VALID
  signal is turned to OFF (**L_REQ or U_REQ must be set to OFF**)"；
* GCI E84 Emulator 的 Passive Mode Functionality Test G 给出的步骤序完全一致
  （6 拉低 `HO_AVBL` → 7-9 验证对方撤 `VALID`/`TR_REQ`/`CS` → 10 才撤 `L_REQ` → 11 才恢复 `HO_AVBL`）。

修正：`HO_ABORT` 期间**保持**请求线，等握手闭合后才撤，再恢复 `HO_AVBL`。
（窗口 a 里请求线尚未断言，行为不变——所以只有窗口 b 会看到"保持"。）

> 教训：这类"什么时候撤信号"的顺序约束，**光读正文容易漏**，必须对着图逐条比。
> 而"二手译文 + 不看图"正是本项目最早那个引脚表错误的同一个根源。

### 19.5 `NullIO`（dry-run）必须按上拉/下拉给出静态电平，不能一律读低

标准接线是**低有效**：低电平 = ON。如果"读不到硬件"就返回低电平，逻辑上就变成
`VALID`/`CS_0`/`TR_REQ` 全部 ON —— 被动侧会**凭空开始一次握手**并拉起请求线。
一个只想"校验一下配置"的 dry-run 变成了会误动作的东西。

修正：`NullIO` 按输入通道的 `pull` 给出静态电平（`up`→高、`down`→低、`none`→构造参数，
默认高即本标准接线下的 OFF）；同时新增校验告警覆盖附录 A1-7 那个坑
（`active_high` 与 `pull` 组合会让空闲电平被判成 ON）。

> 共同教训：**"默认值"和"读不到时的取值"必须按失效安全方向设计**。
> 这三处都与"信息缺失时系统往哪边倒"有关，而 E84 的价值恰恰在于它是一套失效安全协议。
