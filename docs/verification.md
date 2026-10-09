# E84 被动端库 —— 验证记录

> 版本：`e84 0.1.0`　规范：SEMI E84-0301　日期：本轮开发
> 目的：把"这个库凭什么说它符合标准"写成可复核的证据，而不是口头保证。
>
> 复现方式见文末「如何自己跑一遍」。

---

## 1. 验证手段

| 层次 | 手段 | 说明 |
|---|---|---|
| 协议核心 | **纯逻辑状态机** `PassiveFsm.step()` | 无 I/O、无墙钟、无线程；可用 `VirtualClock` 逐拍回放 |
| 时序闭环 | `VirtualRig` 虚拟线束 | 主动侧参考实现 ↔ 被动侧库，中间按**物理电平**连线 |
| 现场事实模拟 | 逐信号 `active_high`、上拉、去抖、**通道复用** | 支持现场把两个逻辑信号并在一条线上（表 9 原文里各信号独立，见 §4 B12） |
| 载口模拟 | 多键组合（`keys/any` 与 `keys/all`） | 对应现场"检测到载具"与"完整落位"两个不同事实 |
| 故障注入 | `SimIO.stuck()` / `fail_writes()` / `NullIO` / 手工置不可用 | 覆盖断线、写失败、卡死、手动模式 |

**关键点**：虚拟线束搬运的是**物理电平**而不是逻辑值。所以逐信号极性、`ES`/`HO_AVBL`
的低有效、以及"两个逻辑信号并到一条线上"这类**最容易在纸面上被忽略的
电学事实**，都在每一拍真实地走了一遍。如果极性写反、或者把 `L_REQ`/`U_REQ` 当成两根线，
闭环会立刻失败。

---

## 2. 标准时序场景：逐图闭环验证

命令等价于：

```bash
python3 -m e84.cli sim configs/example_2lp_standard.yaml --job load:left
python3 -m e84.cli sim configs/example_2lp_standard.yaml --job unload:left
python3 -m e84.cli sim configs/example_2lp_standard.yaml --job load:left,right
python3 -m e84.cli sim configs/example_2lp_standard.yaml --job unload:left --job load:left --continuous
python3 -m e84.cli sim configs/example_2lp_standard.yaml --job load:left --job load:right --continuous
```

| # | 场景 | 标准依据 | 结果 |
|---|---|---|---|
| V1 | 单次 LOAD（空载口 → 落料） | 图 10、§6.2.2.1 | ✅ 被动回 `idle`，主动 `done`，无故障 |
| V2 | 单次 UNLOAD（有载口 → 取料） | 图 11、§6.2.2.1 | ✅ 载具被取走后请求线落下 |
| V3 | 同时 LOAD（两载口一次动作） | 图 14、§6.2.3.2 | ✅ `CS_0+CS_1` 选中两口；请求线仅在**两口都到位**后落下 |
| V4 | 连续 UNLOAD → LOAD（**同一载口**） | 图 16、§6.2.4.3 | ✅ `batch_started` → 段间 `VALID` 落下再抬起 → 第二段自动判定为 LOAD → `batch_ended` |
| V5 | 连续 LOAD → LOAD（**不同载口**） | 图 17、§6.2.4.4 | ✅ `CS_0` 段 → `CS_1` 段，两口最终都落位 |

### V1 的实际事件序列（单次 LOAD）

```
 t(s)  事件                 说明                                       条款
0.000  enabled             控制器启动，输出先落安全态                 §6.4.6
0.002  output_changed      OUT7/HO_AVBL=0, OUT8/ES=0（低有效=ON）
0.104  handshake_started   VALID 上升沿                               §6.2.2.1(2)
0.104  state_changed       idle -> select
0.106  ports_selected      选中载口 LP1（CS_0=ON, CS_1=OFF）           §6.1.2.3、表3
0.106  demand_asserted     断言 L_REQ                                  §6.2.2.1(3)
0.106  state_changed       select -> req_on
0.108  ready_asserted      TR_REQ 到且机构就绪，断言 READY              §6.2.2.1(5)
0.108  state_changed       req_on -> wait_busy
0.112  transfer_started    BUSY=ON                                     §6.2.2.1(6)
0.112  interlock_engaged   BUSY=ON 期间禁止干涉区动作                  §6.1 表1 BUSY
0.180  carrier_settled     载具"位于正确位置"（三键全落）               §6.2.2.1(7)
0.180  demand_released     撤回 L_REQ
0.186  compt_received      COMPT=ON                                    §6.2.2.1(10)
0.186  ready_released      撤回 READY                                  §6.2.2.1(11)
0.190  segment_completed   —                                           §6.2.4.3
0.190  handshake_closed    VALID=OFF，握手闭合                         §6.2.2.1(13)
0.190  state_changed       closing -> idle
```

> 注意 `0.180 carrier_settled` 用的是"三键**全落**"（`carrier_in_position`），而不是
> "任意一键"（`carrier_present`）。这正是现场"载具没坐稳就撤请求线 → 天车松手 → 掉片"
> 的防护点，对应规范 §6.1 表 1 的措辞"载具**位于正确位置**"。

### V4 的连续交接语义

```
第一段（UNLOAD，CONT=ON → batch_started）
  VALID↑ → CS_0↑ → U_REQ↑ → TR_REQ↑ → READY↑ → BUSY↑（此刻采样 CONT=ON ⇒ 后面还有）
  → 载具被取走 ⇒ U_REQ↓ → BUSY↓/TR_REQ↓ → COMPT↑ → READY↓ → VALID↓
  ⇒ 因为本段 CONT=ON，被动进入 cont_next 等待下一段（TP6 监视）
第二段（LOAD，CONT=OFF → 这是末段）
  VALID↑ → CS_0↑ → L_REQ↑ … BUSY↑（此刻采样 CONT=OFF ⇒ 末段）
  → 载具落位 ⇒ L_REQ↓ → … → VALID↓ ⇒ batch_ended → idle
```

**这里纠正了译文脚注的一个错误**：脚注称"连续交接期间 `VALID` 保持 ON 不断开"，
但规范图 16 明确画出 `VALID` 与 `CS_x` 在两段之间**落下再抬起**；表 7 的 `TD1`
（`VALID` OFF → `VALID` ON）与表 6 的 `TP6`（同区间，被动侧）也只在这个解释下才成立。
本库以**图与定时器表**为准。

---

## 3. 边界与故障路径

| # | 用例 | 期望 | 实测 |
|---|---|---|---|
| E1 | `CS_0=0, CS_1=0`（都没选口） | `INVALID_CS_COMBINATION` 故障 | ✅ |
| E2 | 对方要求同时交接但本接口未启用 | `SIMULTANEOUS_NOT_SUPPORTED` 故障 | ✅ |
| E3 | 上层指定"卸载"但载口是空的 | `INTENT_MISMATCH` 故障 | ✅ |
| E4 | 请求线还没落下，对方就撤 `BUSY` | `BUSY_BEFORE_REQ_OFF` 故障（§6.2.2.1 第8步） | ✅ |
| E5 | `COMPT` 已 ON 但 `BUSY` 仍 ON | `COMPT_BEFORE_BUSY_OFF` 故障（注 4 允许在 COMPT 之后校验） | ✅ |
| E6 | 载口切到手动访问模式 | `HO_AVBL` 拉低 | ✅ |
| E7 | `BUSY` 后 60 s 载具始终不落位 | `TP3_TIMEOUT` 故障 | ✅ |
| E8 | 连续交接中场等不到下一次 `VALID`，`tp6_timeout=idle` | 回 `idle`，**不报故障** | ✅ |
| E9 | `NullIO` dry-run（不接硬件） | 保持 `idle`，不得凭空开始握手 | ✅（见 §4 B1） |
| E10 | `stop()` 后的物理电平 | 全部输出回到 OFF 方向（`ES`/`HO_AVBL` 也是 OFF） | ✅ |
| E11 | 现场联锁 `GO` 在 `VALID` **之后**才成立 | 必须仍然开始握手（不能用 `VALID` 单独的边沿） | ✅（见 §4 B6） |
| E12 | 握手期间 `GO` 被撤销、`VALID` 仍在 | 撤回请求并拉低 `HO_AVBL`，等对方按标准闭合；`VALID`↓ 后才恢复 | ✅（见 §4 B5） |
| E13 | 显式 `stop()` 之后，上层残留的循环继续调用 `poll()` | **不得**把设备重新启用（否则 `ES`/`HO_AVBL` 悄悄变回 ON，等于宣告"可以来交接"） | ✅（见 §4 B7） |
| E14 | `stop()` 之后读取 `outputs()` / `snapshot()` | 必须如实反映安全态（全 OFF），不能停留在停机前的值 | ✅（见 §4 B7） |
| E15 | 24000 拍随机输入 + 随机模式/可用性/安全链变化 | 7 条不变式零违例 | ✅（见 §3bis） |
| E16 | `set_access_mode("LP1", "manual")` 传字符串 | 必须真正生效（拉低 `HO_AVBL`），非法值必须报错 | ✅（见 §4 B9） |
| E17 | 开启 `trace.enabled` 跑一次握手 | 追踪文件里**逻辑信号与物理电平都要有** | ✅（见 §4 B8） |
| E18 | 一次完整交接 / 交接被中止 / `BUSY=ON` 期间直接停机 | `INTERLOCK_ENGAGED` 与 `INTERLOCK_RELEASED` 必须**成对**出现（停机时也要补发） | ✅（见 §4 B10） |
| E19 | 载口传感量通道误配成 E84 **输出**通道（如 `carrier_in_position: OUT4`） | 配置校验阶段就必须报错，而不是等到构造控制器才炸 | ✅ |
| E20 | 在 `VALID` 仍为 ON 时强制 `clear_fault(force=True)` | 不得"半路接上"旧握手；超时后报 `VALID_STUCK` | ✅ |
| E21 | `VALID` 先到、现场联锁 `GO` 后到，中间等待远超 `valid_stuck_timeout_s` | **不得**误判为 `VALID_STUCK`；`GO` 一成立必须立刻开始握手 | ✅ |
| E22 | 同一逻辑波形在「低有效/高有效」×「请求线共用/分开」四种表示下 | 逻辑轨迹必须逐拍完全相同 | ✅（交叉验证 X2） |
| E23 | 「纯 FSM 直驱」与「完整栈」并排跑 3000 拍随机波形 | 状态、逻辑输出、物理电平三者逐拍一致 | ✅（交叉验证 X1） |
| E24 | 只给 `CS_0` 不给 `VALID` | 被动必须保持 `IDLE`（规范注 3） | ✅（交叉验证 X3） |
| E25 | `BUSY` 落下但握手尚未闭合（或握手已中止） | 干涉区冻结**必须立刻**跟随 `BUSY` 解除 | ✅（见 §4 B11） |
| E26 | 交接进行中调用 `set_access_mode(..., strict=True)` | 必须拒绝，且不得改掉访问模式（SEMI E87 §11.1.2） | ✅ |
| E27 | `transfer_in_progress` 的区间 | 必须等于 E87 Table 8 的 AUTO 交接边界：READY 有效 → 交接完成 | ✅ |
| E28 | 手动访问模式下 AMHS 强行交接 | 不得断言请求线；必须留下可告警的事件（E87 §11.3.3.2） | ✅ |

### 故障态输出画像（相关信息 1 R1-1.1.2.1）

`TP1` 超时后的实测：

| 信号 | 期望 | 实测 |
|---|---|---|
| `READY` | OFF | ✅ |
| `ES` | OFF（请求对方立即停止） | ✅ |
| `HO_AVBL` | OFF（交接不可用） | ✅ |
| 请求线 `L_REQ`/`U_REQ` | **保持在出错时刻的状态**（这里保持 ON） | ✅ |
| 状态迁移 | `fault_wait_close` →（`VALID`↓）→ `fault_latched` | ✅ |
| 恢复 | 只能通过 `clear_fault()`；清除后回 `idle` 且 `HO_AVBL`/`ES` 恢复 | ✅ |

`raise_fault()` 外部上报的实测事件数 = **1**（不重复上报），且带条款号。

---

## 3cross. 交叉验证（打破"同源自测"的循环论证）

前面几节的测试有一个共同的弱点：**驱动方也是同一个作者写的**。
`test_golden_figures.py` 用 `e84/active/fsm.py` 驱动 `e84/fsm.py`——两侧出自同一份
对标准的理解；如果这张图被理解错了两处，它们会**一起错**，测试照样全绿。
这只能证明"两侧互相兼容"，**不能**证明"任一侧符合规范"。

因此另加 9 项交叉验证（[`tests/test_cross_validation.py`](../tests/test_cross_validation.py)），
用四种**互相独立**的参照：

### X1 层级差分交叉（随机波形，3000 拍）

同一串逻辑输入，分别喂给两条执行路径：

| 路径 | 经过的层 |
|---|---|
| A：完整栈 | `SimIO` → 逐信号极性/上拉/映射 → `SensorBank` → `PiOController` → `PassiveFsm` |
| B：纯 FSM | 直接构造 `Inputs` → `PassiveFsm` |

逐拍断言三件事全部一致：**状态**、**逻辑输出**、以及**每个物理通道的电平**（按配置的
`active_high` 由逻辑输出反推）。这条专门抓"逻辑算对了、但引脚写错了"这一类层级 bug——
`NullIO` 凭空开始握手、现场信号 `GO` 进不了 `Inputs`，都属于这一类。

### X2 表示交叉（4 种物理表示 × 1200 拍）

同一段逻辑波形跑四遍：{低有效, 高有效} × {`L_REQ`/`U_REQ` 共用一条线, 分成两条线}。
四种表示下的**逻辑轨迹必须逐拍完全相同**——物理表示不该渗进逻辑。
（并断言这段波形确实跑出过握手，避免"全程 idle 的假通过"。）

### X3 图驱动交叉：手写波形驱动完整栈（**完全不经过 `ActiveFsm`**）

把 §6.2.2.1 的 13 步、§6.2.3.2 的同时交接、§6.2.4.3 的连续交接，**逐条手写成显式电平
变化**，再驱动完整栈。驱动方与 `e84/fsm.py` 没有共享代码，所以它是真正的独立参照。

用图 10 钉死的几条（都是"看图才会注意"的细节）：

* **注 3**：只给 `CS_0`、不给 `VALID` 时，被动必须保持 `IDLE`（不得提前采样 `CS`）；
* 第 3 步：空载口 ⇒ 只置 `L_REQ`，且 `U_REQ` 必须为 OFF；
* 第 7 步：载具"**位于正确位置**"（三键全落）才撤请求线，而 `READY` 要留到第 11 步；
* 第 8/9/10 步：先撤 `BUSY`/`TR_REQ`，再抬 `COMPT`；
* 第 11 步：`COMPT` 之后才撤 `READY`，**同时解除干涉区冻结**；
* 第 13 步：`VALID`↓ 才算握手闭合。

用图 14 钉死"与语义"：只到位**一个**载口时，请求线必须**仍然保持 ON**。

用图 16 钉死连续交接：`CONT` 首段 ON / 末段 OFF、**两段之间 `VALID` 落下再抬起**、
第二段方向自动变成 LOAD、`BATCH_STARTED`/`BATCH_ENDED` 各一次。

### X4 反向交叉：手写被动响应驱动 `ActiveFsm`

用**手写的被动侧响应**（同样不经过 `PassiveFsm`）驱动主动侧参考实现，断言主动侧符合
§6.3.2.3 与 §6.2.2.1 的顺序约束：`CS` 先于 `VALID`、`VALID` 先于 `TR_REQ`、
`TR_REQ` 先于 `BUSY`、`BUSY` 先于 `COMPT`，且抬 `COMPT` 的同一拍 `BUSY`/`TR_REQ` 已落下。

X3 与 X4 合起来：**被动端与主动端各自都被独立的、非同一来源的波形约束过**，
循环论证被打破。

---

## 3bis. 随机模糊测试（不变式）

除了逐用例断言，还跑了一轮随机模糊：40 个独立会话 × 600 拍 = **24000 次 `poll()`**，
每拍随机翻转 0–3 个输入（7 个 E84 输入 + 6 个落位键），并按小概率随机改变
访问模式、载口可用性、本机安全链状态、以及强制清除故障；时间步长在
5 ms / 10 ms / 50 ms / 300 ms / 2.5 s 之间随机跳变（故意制造定时器边界）。

覆盖到：140 次握手开始、61 次故障、75 次 `HO_ABORT`、4 次连续交接批次。

每一步都检查以下**不变式**，结果是 **0 条违例**：

| 不变式 | 依据 |
|---|---|
| `L_REQ` 与 `U_REQ` 不同时 ON | 语义互斥（现场若并线则电学上也互斥） |
| `DISABLED` 状态下所有输出为 OFF | §6.4.6 失效安全 |
| 故障态下 `READY`/`ES`/`HO_AVBL` 全部为 OFF | 相关信息 1 R1-1.1.2.1 |
| `HO_ABORT` 状态下 `HO_AVBL` 为 OFF | §6.2.5.2 |
| `CONT_NEXT` 状态下请求线已落 | §6.2.4.3 |
| **每一拍物理通道电平和逻辑输出一致**（含共用通道取"或"） | 逐信号 `active_high` 契约 |
| `poll()` 不抛异常 | 鲁棒性 |

> 最后一条最有价值：它把"逻辑值算对了、但写进引脚时极性搞反了"这类错误，
> 在 24000 次随机场景里逐拍对了一遍。这也是为什么虚拟线束必须搬运**物理电平**
> 而不是逻辑值。

---

## 4. 本轮发现并修掉的问题

| # | 问题 | 危害 | 修法 |
|---|---|---|---|
| **B1** | `NullIO` 把所有输入一律读成低电平；而标准接线是**低有效**，于是逻辑上全变成 ON | dry-run / 只用校验器时，被动侧会**凭空开始一次握手**并拉起 `L_REQ`/`READY`——如果这套空后端被误接到现场，就是真事故 | `NullIO` 改为按输入通道的**上拉/下拉**给出静态电平（`up`→高、`down`→低、`none`→构造参数，默认高）；并新增校验告警：`active_high=true` 配 `pull=up`、或 `active_high=false` 配 `pull=down` 时提示"空闲电平会被判成 ON"（附录 A1-7 那个坑） |
| **B2** | 外部 `raise_fault()` 只改变状态，不发 `FAULT_RAISED` 事件 | 上层告警系统收不到机械/通信类故障 | FSM 的 `raise_fault` 置 `_fault_announced`，由控制器立即发事件；随后修掉重复上报（实测由 2 条降为 1 条） |
| **B3** | 配置校验器把"同一个载口把同一批键同时用于 `any` 与 `all`"误判为通道冲突 | 这是现场三键方案的**正常写法**，会被误拒 | 冲突判定改为：同通道**跨载口复用**或**极性/上拉不一致**才报错；同载口内多用途放行 |
| **B4** | 主动侧参考实现的 `TD0` 默认写成 0.5 s | 违反表 7 的 0.1–0.2 s 范围（被 `TIMER_SPECS` 校验拦下） | 改为 0.1 s；`TD0`/`TP1` 之类的越界值由 `TimerSet` 直接拒绝 |
| **B5** | 绑定在 `inputs:` 里的**现场自定义信号**（如板级 `GO`）被 HAL 读进来了，却没有被传进 `Inputs` | 任何把 `GO` 写进 `preconditions` 的配置都会**永远无法开始握手**——配置看起来完全合法，现场却"死等" | `PiOController._build_inputs()` 把非标准输入信号统一放进 `Inputs.extra`；校验器的相关提示也改成"照常从通道读取，不是走 `set_extra_input`" |
| **B13** | **中止（`HO_ABORT`）时"抢跑"撤下了请求线**：进入 `HO_ABORT` 立即把 `L_REQ`/`U_REQ` 置 OFF | 与**规范图 19** 不符。图 19 画的是 `VALID`↓（连同 `CS`↓/`TR_REQ`↓）在前、随后 `L_REQ`↓、最后 `HO_AVBL`↑；§6.2.5.2/.3 正文也写明恢复 `HO_AVBL` 时"`L_REQ`/`U_REQ` must be set to OFF"（即恢复之前才要求它们为 OFF，而不是中止一开始就撤）。提前撤会让主动设备在 `HO_AVBL` 仍为 OFF 时就看不到"这个载口还在等我"的状态，与现场验证过的商业仿真器行为不一致 | `HO_ABORT` 期间**保持**请求线，握手闭合后才撤，再恢复 `HO_AVBL`（窗口 a 里请求线本就没断言，行为不变）。依据同时来自规范图 19 与 GCI E84 Emulator 手册 Passive Mode Functionality Test G 的步骤序；新增 3 项测试固化 Test E/F/G 的步骤序 |
| **B12** | **引脚表抄错了**：`L_REQ`/`U_REQ` 被当成共用引脚 1、`READY`/`VS_0` 被当成共用引脚 4（依据是**一份中文图文译文**） | 这是"二手资料"造成的典型风险。**SEMI E84-0301 原文 Table 9 里每个信号各占一个引脚，没有任何共用**：`L_REQ`=1、`U_REQ`=2、`VA`=3、`READY`=4、`VS_0`=5、`VS_1`=6。错表的直接后果是**做线会错**；间接后果是它让一份本来更"标准"的现场配置（VOC 板：`L_REQ`/`U_REQ` 是两根独立线）被误判为"非标接法"。状态机逻辑本身不受影响（库从不按引脚号做判断） | 拿到原文 PDF 后逐条核对并修正 `e84.signals.db25_pins()`；同步修正 README、两份示例配置、bringup_guide、HAL/validate 的提示文案；把两个示例配置改成原文的标准引脚（独立两线），把"并线"降级为注释里的现场选项；新增 `tests/test_reference_data.py`（19 项）把 Table 1/5/6/7/9 的常量全部钉死，防止再次漂移 |
| **B11** | **交接干涉区的冻结被挂在"握手闭合"上，而不是挂在 `BUSY` 电平上**（由交叉验证 X3 发现） | §6.1 表1 `BUSY` 的措辞是"**只要本信号为 ON**，被动设备就不应在交接干涉区内执行任何机械动作"。原实现只在 `_reset_selection()`（握手闭合/状态复位）时解除冻结，于是：① 上层的机构比标准要求**多冻住**一整段时间（从 `BUSY`↓ 一直冻到 `VALID`↓）；② 如果对方撤了 `BUSY` 却卡在后续步骤，机构会被冻到超时为止。两种都是"该能动的时候不让动" | 冻结改为**每拍由 `BUSY` 电平推导**（`interlock_engaged = inp.busy`），两个边沿都在 `_emit_derived_events()` 发事件；故障期间同样跟随 `BUSY`（对方机构可能还在干涉区里）。模糊测试里 `interlock_engaged == BUSY` 在 **21000 拍上全部成立**，`ENGAGED`/`RELEASED` 由修复前的 7 : 0 变成严格配对 629 : 629 |
| **B10** | `EventType.INTERLOCK_RELEASED` 在 `events.py` 里定义了，但 `fsm.py` **没有任何发射点** | 上层是"听到 `INTERLOCK_ENGAGED` 才冻结干涉区机构"的（§6.1 表1 `BUSY`）。释放时不通知，机构就会被**永久冻住**——或者上层只能靠自己去猜 `BUSY` 的下降沿，等于把标准明确要求的互锁交给了应用层猜 | `_emit_derived_events()` 在冻结状态的下降沿发 `INTERLOCK_RELEASED`；`PiOController.stop()` 在"停机时仍处于冻结"的情况下补发一次。模糊测试里 `ENGAGED`/`RELEASED` 现在是严格配对的（修复前是 7 : 0） |
| **B9** | `LoadPort.set_access_mode()` / `set_operation_intent()` 原样保存传入值，不做类型归一化 | 现场代码里写 `"manual"` / `"load"` 这种字符串很常见；字符串会被存下来，之后与枚举比较时**永远不相等**——表现为"我设置了手动模式但 `HO_AVBL` 没拉低"，属于最难查的一类静默失败 | 归一化为枚举，并接受少量常用别名（`auto`→`automatic`、`loading`→`load` 等）；非法值直接抛 `ValueError` |
| **B8** | `PiOController._record_outputs()` 把「物理通道跳变」当默认 `source="output"` 传给了 `TraceRecorder`，而 recorder 只在 `source=="channel"` 时才处理 `channels` | 追踪文件里**只有逻辑信号、没有物理电平**；而"逻辑对、引脚写反"恰恰是现场最需要靠追踪文件查的问题——等于把最有用的那半份证据静默丢了 | 拆成两次记录：逻辑信号走 `source="output"`，物理通道走 `source="channel"` |
| **B7** | `ManualRunner.poll()` 会在未 `start()` 时**隐式启动**；`Equipment.poll()` 也会把已 `stop()` 的设备重新拉起 | 显式停机后，上层残留的周期性循环只要再调一次 `poll()`，设备就"复活"了——`ES`/`HO_AVBL` 变回 ON，对外等于宣告"可以来交接"。这是**"停机"与"周期任务"竞争**的典型现场事故 | `ManualRunner.poll()` 未启动时直接报错（不再隐式启动）；`Equipment` 记录"被显式停机"，此后 `poll()` 只把输出维持在安全态、不再推进协议，必须显式 `start()` 才恢复。同时修掉停机后 `outputs()`/`snapshot()` 仍返回停机前旧值的问题（`PassiveFsm.disable()` 现在会把只读快照同步为安全态） |
| **B6** | 握手只用 `VALID` 的上升沿触发 | 若现场联锁（`GO`）在 `VALID` 之后才成立，这次握手会被**永久漏掉**，要等对方把 `VALID` 先落下再抬起才能恢复——现场极难定位的一类"卡死" | 触发条件改为「`VALID` ∧ 全部前置条件」这个**整体**的上升沿（`armed_rising`）；标准场景前置条件只有 `VALID`，行为与原来等价；"半路接上"仍由 `VALID_STUCK` 超时兜底 |

> B1 值得单独强调：它是**只有把 dry-run 真的跑一遍才会暴露**的问题。
> 光看代码，"读不到硬件就返回 False"看起来完全合理。

---

## 5. 尚未验证的部分（诚实清单）

| 项 | 现状 |
|---|---|
| 真实 GPIO 时序（libgpiod 抖动、光耦延迟） | 未验证；后端已写但环境无 libgpiod，需现场 `pytest -m hardware` |
| 与真实 AMHS/天车对拖 | 未验证；需要现场设备 |
| 跨区被动 OHS 场景（`VA`/`VS_0`/`VS_1`/`AM_AVBL`） | 配置位与信号已定义，**状态机未实现**，校验器会明确拒绝该 `scenario` |
| 与真实抓取波形逐点比对 | 未做；如果拿到现场波形，可以直接喂给 `VirtualClock` 回放 |
| **与第三方实现对拖** | 已调研（见 [third_party_references.md](third_party_references.md)）：**不存在开源的第三方 E84 实现**（GitHub 仓库搜索 `HO_AVBL`/`semi e84 handoff`/`wafer carrier handoff` 均 0 结果，PyPI 无 `e84` 包）。可用的独立参照只有商业仿真器（GCI/Get Control）。其**行为判据**已从公开手册提取并落成 4 项测试（`tests/test_vendor_criteria.py`），但完整对拖仍需设备 |
| 看门狗通道的真实硬件行为 | 逻辑已实现（只在轮询推进时翻转），未接硬件验证 |
| 多 PI/O 并行运行 | 结构上支持（每 PI/O 独立 FSM/定时器，§3.4），未做压力验证 |
| Python 3.11+ / 打包 | 当前在 3.10.12 上开发；按需求未打包 |

---

## 6. 如何自己跑一遍

```bash
cd /home/say/github_project/e84_communication
export PYTHONPATH=.

# 1) 配置校验 + 信号映射表
python3 -m e84.cli validate configs/example_2lp_standard.yaml --show-map

# 2) 不接硬件的自检（含 4 个时序闭环）
python3 -m e84.cli selftest

# 3) 逐拍看一张时序图（这里跑连续交接 UNLOAD→LOAD）
python3 -m e84.cli sim configs/example_2lp_standard.yaml \
    --job unload:left --job load:left --continuous

# 4) 单元 + 黄金时序测试
python3 -m pytest -q

# 5) 只看标准时序闭环
python3 -m pytest -q tests/ -k "golden or rig"
```

`e84-sim` 打印的表格列含义：

```
t        主动阶段   VALID CS_0 CS_1 TR_REQ BUSY COMPT CONT | L_REQ U_REQ READY HO_AVBL ES | 被动状态
```

左半是主动侧驱动的信号，右半是被动侧驱动的信号——直接把这张表和规范图 10/11/14/16/17
并排看，就能逐点核对。
