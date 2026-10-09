# E84 被动端库 —— 现场投运与调试清单

> 面向第一次把本库接到真机上的人。目标是**在上电之前就把接线错误找出来**，
> 而不是等天车来了才发现 `ES` 接反。
>
> 每个步骤都写清"看什么现象 = 说明什么"，以及**判据**（不符合就不要往下走）。

---

## 0. 为什么 E84 的接线特别容易出错

三条必须记住的事实，全部来自 SEMI E84-0301：

| 事实 | 依据 | 后果 |
|---|---|---|
| **`ES` 与 `HO_AVBL` 是低有效**（ON = 正常，OFF = 请求停止 / 不可交接） | §6.1 表 1 | 接反 → 正常运行时对方看到"请求停止"；故障时反而看到"一切正常" |
| **标准把信号 ON 定义为 ≤1.8 Vdc**，而常见 TTL 输入要 ≤0.8 V 才判低 | 附录 A1-7 | 硬线直连 TTL 时，1.0–1.8 V 这段**既不判 ON 也不判 OFF**；需要一级输入接收电路（比较器/施密特） |
| **表 9 原文中每个信号各占一个引脚，没有共用**；且跨区信号夹在中间（`VA`=3、`VS_0`=5），所以「OUT 编号」与信号清单**不是顺序对应**的 | 表 9 原文 | 做线必须逐针核对。另注意：一份流传的中文译文把该表错排成 `L_REQ`/`U_REQ` 共用引脚 1、`READY`/`VS_0` 共用引脚 4，**不要照那份接线** |

---

## 1. 准备阶段（不接现场总线，先离线跑）

### 1.1 配置校验

```bash
PYTHONPATH=. python3 -m e84.cli validate <你的配置>.yaml --show-map
```

**判据**：`errors = 0`。逐条读 `warnings`，尤其是这几类：

| 告警 | 含义 | 处理 |
|---|---|---|
| `输出通道 'X' 被多个信号共用: L_REQ, U_REQ` | 现场把两根请求线并在一条线上（表 9 原文是引脚 1 与引脚 2，各自独立） | 确认现场确实这样接；HAL 会保证互斥 |
| `输出通道 'X' 被多个信号共用: ...`（其他信号） | 除 `L_REQ/U_REQ` 外不允许复用 | **必须改**，否则两个信号会互相打架 |
| `输入 X 配置为 active_high=true 且 pull=up` / `active_high=false 且 pull=down` | 空闲(开路)电平会被判成 ON | **必须改**极性或上下拉，否则上电即误判 |
| `输出 ES 的 safe_on=True` | 安全态下 `ES` 会处于 ON | 按 §6.4.6 与失效安全原则，`ES`/`HO_AVBL` 的 `safe_on` 应为 `false` |
| `载口 carrier_present 与 carrier_in_position 使用完全相同的来源` | "检测到载具"被当成"完整落位" | **强烈建议区分**：否则载具没坐稳就撤请求线 → 掉片风险 |
| `poll_interval_ms=... 偏大` | 轮询周期接近 `TD0`(100 ms) | 建议 ≤10 ms |

`--show-map` 会把「信号 → 通道 → 极性 → 上拉 → 去抖」和「载口传感量来源」、
「定时器取值」全部列出来。**打印出来跟现场接线表逐行对一遍**，这一步比在现场用表笔量半天便宜得多。

### 1.2 离线自检

```bash
PYTHONPATH=. python3 -m e84.cli selftest <你的配置>.yaml
PYTHONPATH=. python3 -m pytest -q
```

**判据**：`selftest` 全 `PASS`，`pytest` 全绿。这一步用不到任何硬件，
它验证的是"配置 + 协议逻辑"这一半。

### 1.3 用 dry-run 看能不能凭空动作

```bash
PYTHONPATH=. python3 -m e84.cli monitor <你的配置>.yaml --dry-run --duration-s 5
```

**判据**：状态稳定停在 `idle`，`HO_AVBL=ON`、`ES=ON`，请求线 OFF。

> 如果这里就看到 `select`/`req_on` 甚至请求线被拉起，说明**输入极性配反了**：
> 不接硬件时输入应当读成"无效"。本库的 `NullIO` 已按上拉/下拉给出静态电平来防这件事，
> 但它只能防到"配置层面"，真实的极性还得靠下面第 2 步量。

---

## 2. 上电阶段（接真机，但先不要让天车来）

### 2.1 只读监视，先什么都不驱动

```bash
PYTHONPATH=. python3 -m e84.cli monitor <你的配置>.yaml --interval-ms 200
```

一边看屏幕，一边**手动**制造现场条件：

| 你做的事 | 期望看到 | 不符合说明什么 |
|---|---|---|
| 不放载具 | 两个载口 `carrier_present=False`、`carrier_in_position=False`；`HO_AVBL=ON` | 键极性反了 |
| 放上载具但**不坐稳**（只压到一个键） | `carrier_present=True` 且 `carrier_in_position=False` | 三键的 any/all 接反了，或键装反了 |
| 载具完全坐稳 | 两者都 `True` | — |
| 按下急停 / 打开安全门 | `ES=OFF`（注意是 OFF！）且 `HO_AVBL=OFF` | `ES` 安全链没接进来（旧实现常见问题：`ES` 由落位键推导，与真实安全链无关） |
| 切到手动访问模式（调用 `set_access_mode(MANUAL)` 或现场开关） | `HO_AVBL=OFF` | 访问模式来源配错 |

### 2.2 用 `force` 逐个验证输出通道（**谨慎**）

```bash
PYTHONPATH=. python3 -m e84.cli force <你的配置>.yaml \
    --signal READY --logical 1 --hold-ms 1000 --i-know-what-i-am-doing
```

在对方（AMHS 侧）用表/示波器或对方的状态面板确认：

| 你 force 的信号 | 对方应当看到 |
|---|---|
| `READY` | `READY` = ON（**低电平**） |
| `HO_AVBL` = 0 | `HO_AVBL` = OFF（高电平）→ 对方应判定"不可交接" |
| `ES` = 0 | 对方应判定"请求停止" |

**判据**：对方看到的 ON/OFF 与你 force 的逻辑值一致。任何一个反了，
就是那个信号的 `active_high` 配错——**回到 1.1 改配置，不要在代码里反相**。

> `force` 只在设备处于 `idle`/`disabled` 时可用，且必须显式带 `--i-know-what-i-am-doing`
> 之类的确认开关；退出时一定会把输出恢复到安全态。

### 2.3 上电/掉电安全态

**判据**（用表在输出端量）：

1. 设备**上电但软件未启动**时：`ES` 应为 OFF、`HO_AVBL` 应为 OFF、请求线 OFF、`READY` OFF。
   也就是说，任何"软件没在跑"的时刻，对方都应看到"不可交接 + 请求停止"。
2. **杀掉进程**（`kill -9`）：输出应保持在上面的安全态（由外部上拉/硬件决定），
   不应出现"握手信号卡在有效状态"。
3. 如果你用了硬件看门狗：软件停止轮询后，看门狗通道停止翻转，外部电路应把上述信号拉到安全态。

> 这一条是 §6.4.6（OFF = 无电流/无光）与整份标准"失效安全"取向的直接体现。
> 旧实现里 `stop()` **刻意不动** `HO_AVBL`/`ES`（它想让它们"反映物理在位"），
> 本库按失效安全原则改成全部拉到 OFF——**如果现场要求"软件停机后仍保持可交接"，
> 那是接线/硬件层面的事，不要靠软件留着信号**。

---

## 3. 联调阶段（让天车真的来一次）

### 3.1 先只跑一次"单次装载"

在 `monitor` 里盯住状态迁移，正常应当是（括号里是标准条款）：

```
idle → select → req_on → wait_busy → transfer → await_compt → closing → idle
        (2)       (3)         (6)         (7)          (10)        (11-13)
```

同时看事件流：

```
handshake_started → ports_selected → demand_asserted → ready_asserted
→ transfer_started → interlock_engaged → carrier_settled → demand_released
→ compt_received → ready_released → segment_completed → handshake_closed
```

**判据**：顺序一致、无 `fault_raised`、最终回 `idle`。

### 3.2 三个最容易被忽略的顺序约束

| 约束 | 依据 | 从哪里看 |
|---|---|---|
| `CS_x` 必须先于 `VALID` 建立（TD0 ≈ 0.1 s） | 表 7、注 3 | 如果 `select` 阶段报 `INVALID_CS_COMBINATION`，多半是对方的 `CS_x` 与 `VALID` 同时到、采样到了中间态 |
| 主动设备必须**先看到请求线 OFF** 才允许撤 `BUSY` | §6.2.2.1 第 8 步 | 报 `BUSY_BEFORE_REQ_OFF` = 对方违例 |
| 主动设备必须在 `BUSY`/`TR_REQ` 落下之后才抬 `COMPT` | §6.2.2.1 第 8/9/10 步、注 4 | 报 `COMPT_BEFORE_BUSY_OFF` = 对方违例 |

> 这三条如果报出来，**不要急着在配置里把它们放宽**。它们是在保护你：
> 第 8 步违例意味着对方机构可能还没退出交接干涉区；另两条意味着对方的状态机有问题。

### 3.3 三条容易漏的现场经验

1. **`TP3`/`TP4` 的典型值是 60 s，不是 2 s**（表 6）。因为这两段在等机械动作。
   如果你把 `TP3` 设成 2 s，慢一点的机构就会被误判成互锁超时。
2. **`BUSY=ON` 期间禁止本机在交接干涉区内做任何动作**（§6.1 表 1 `BUSY` 说明）。
   本库通过 `equipment.interlock_engaged` 与 `EventType.INTERLOCK_ENGAGED/RELEASED`
   暴露这个窗口；**载口门/夹持/机器人必须挂到这两个事件上**，不要凭自己的时序猜。
3. **有闸门的载口在连续交接期间要整批保持开启**（§6.2.4.2）。
   订阅 `EventType.BATCH_STARTED` / `BATCH_ENDED` 来开关门，
   而不是每段开关一次——这正是不用连续交接会浪费的时间。

### 3.4 排除"卡死"类故障

E84 现场最典型的"卡死"是**双方都在等对方**。本库的 6 个被动侧定时器
（`TP1`–`TP6`）会各自在超时后报出带条款号的故障：

| 看到 | 说明卡在哪 |
|---|---|
| `TP1_TIMEOUT` | 已经发出请求线，但对方一直没抬 `TR_REQ`（表 6：`L_REQ/U_REQ ON → TR_REQ ON`） |
| `TP2_TIMEOUT` | `READY` 已 ON，但对方一直没抬 `BUSY` |
| `TP3_TIMEOUT` | `BUSY` 已 ON，但载具一直没到位/没被取走（机械或传感器问题） |
| `TP4_TIMEOUT` | 请求线已落，但对方一直没撤 `BUSY`（对方可能卡住，或它没看到请求线落） |
| `TP5_TIMEOUT` | `READY` 已落，但对方一直没撤 `VALID`（握手没闭合） |
| `TP6_TIMEOUT` | 连续交接中场，下一段的 `VALID` 一直不来 |

故障是**锁存**的（§6.3.3.1 不定义恢复流程，本库取保守做法）：

```bash
PYTHONPATH=. python3 -m e84.cli faults <你的配置>.yaml   # 看当前故障与条款号
```

确认现场安全、并让握手闭合（`VALID` 落下）之后，再显式清除：

```python
controller.clear_fault()          # 握手已闭合才允许
controller.clear_fault(force=True)  # 人工强制复位：请自行确认机械已安全
```

**故障期间对方应当看到**：`READY=OFF`、`ES=OFF`、`HO_AVBL=OFF`，
而请求线**保持在出错时刻的状态**（相关信息 1 R1-1.1.2.1）。如果对方看到请求线突然掉了，
它可能会以为交接正常完成——这也是"钉住请求线"这条规则的用意：**防止对方自主动作造成二次事故**。

---

## 4. 投运前的最终验收清单

- [ ] `python3 -m e84.cli validate <配置>` 无 error，所有 warning 都已被解释或修掉
- [ ] `python3 -m e84.cli selftest <配置>` 全 PASS
- [ ] `pytest -q` 全绿（含黄金时序）
- [ ] `monitor --dry-run` 稳定 `idle`，不凭空动作
- [ ] 逐信号 `force` 验证过极性，对方看到的 ON/OFF 与逻辑值一致
- [ ] 上电未启动软件时、以及 `kill -9` 后，输出处于安全态
- [ ] 急停/安全门 → `ES=OFF`（低有效）
- [ ] 载具"只是压到一个键"时 `carrier_in_position=False`（不会提前撤请求线）
- [ ] 手动访问模式 → `HO_AVBL=OFF`
- [ ] 真实跑通一次单次装载、一次单次卸载
- [ ] 真实跑通一次同时交接（如果机型支持）
- [ ] 真实跑通一次连续交接，并确认闸门整批只开关一次
- [ ] 人为制造一次 `TP3` 超时，确认故障被锁存、对方看到正确的故障画像、且能通过 `clear_fault()` 恢复
- [ ] 交接干涉区的机械动作已挂到 `INTERLOCK_ENGAGED/RELEASED` 事件上
- [ ] 现场抓一段时序波形留档，作为下次改动的回归基线

---

## 5. 把现场波形变成回归测试

本库的协议核心是 `step(now, Inputs) -> Outputs` 的纯函数式状态机，所以可以**离线复现**现场问题：

1. 用 `e84.TraceRecorder`（配置里 `trace.enabled: true`）录一段 CSV；
2. 或者直接把示波器/逻辑分析仪导出的跳变时间戳整理成 `(t, 信号, 电平)`；
3. 写一个测试，用 `VirtualClock` 按时间戳推进、把输入喂给 `controller.poll(now=...)`，
   断言输出与现场一致。

这样"上次那台设备在 3 月 12 日 14:07 卡了一下"就变成了一个**能反复跑**的测试用例，
而不是一段谁也说不清的现场记忆。
