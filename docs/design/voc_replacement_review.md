# VOC_Project E84 实现替换可行性审查

> 审查对象：`/home/say/code/python/VOC_Project`（PySide6 + 树莓派 BCM GPIO）
> 审查方式：**只读**，未改动任何文件。
> 审查日期：对应 `e84` 库 `0.1.0`、VOC 的 `e84_passive.py` 已于 2026-10-09 更新（GO 删除、E84 信息灯停用）。

---

## 0. 结论

**可行。但不应该"直接替换"，而应该加一个适配层。**

替换掉的是 `e84_passive.py`（618 行）与 `gpio_controller.py`（85 行）；
`app.py` 的 `LoadportBridge`、QML 面板、执行机构控制器**都不需要改**——
前提是适配层原样保留它们依赖的三类对外契约。

需要额外做的只有 **3 处小扩展**（都在本库侧，不动 VOC）：

| # | 扩展 | 规模 | 为什么必须 |
|---|---|---|---|
| E1 | `SensorSpec` 增加 `mode: partial` 与 `invert` | ~30 行 | VOC 的 `HO_AVBL` 策略是"**部分落位**就宣告不可交接"，当前四种传感量来源表达不了 |
| E2 | `LoadPortConfig.available: SensorSpec` | ~20 行 | 让"载口可用性"能来自传感器，而不是只能靠 `set_port_available()` 注入 |
| E3 | 故障画像的现场确认（可能只需配置） | 0 行 | 新库按相关信息 1 拉低 `ES`/`HO_AVBL` 并**保持请求线**；VOC 只拉低 `READY` 并立即撤请求线。这是**有意的行为变更**，需现场确认 |

新增一个适配层（约 200–300 行）保留 Qt 信号契约，内部持有新库的
`Equipment` + `ManualRunner`，由**现有 QTimer** 驱动。

---

## 1. 替换范围

| VOC 文件 | 行数 | 处置 |
|---|---|---|
| `src/voc_app/loadport/e84_passive.py` | 618 | **删除**（协议 + 按键 + LED + 业务耦合全在这里） |
| `src/voc_app/loadport/gpio_controller.py` | 85 | **删除**（被 `DigitalIO` 后端取代） |
| `src/voc_app/loadport/e84_thread.py` | 165 | **保留**（线程语义 R17 是对的），仅把构造的类换成适配层 |
| `src/voc_app/loadport/main.py` | 62 | 保留（演示入口，跟着改构造） |
| `src/voc_app/gui/app.py` 的 `LoadportBridge` | 213（393–605 行） | **不动**（它只依赖信号与两个 slot） |
| `tests/test_e84_state_machine.py` | 169 | 改写（依赖私有名 `_process_state`/`E84_InSig_Value`/`SIG_ON`） |
| `tests/test_e84_handshake_safety.py` | 269 | 改写（伪 `RPi.GPIO` 注入 + 读 `E84_OutSig` 字典） |
| `tests/test_e84_thread_shutdown.py` | 191 | **基本不动**（它用 fake controller，只依赖 `start/stop`） |

---

## 2. 必须原样保留的三类对外契约

替换的成败取决于这三点，而不是协议实现本身。

### 契约 A：6 个 Qt 信号（`LoadportBridge` 直接 connect）

```python
state_changed = Signal(str)          # → 标题栏 "E84 状态: <state>"
warning = Signal(str)                # → 报警 WARNING
fatal_error = Signal(str)            # → 报警 FATAL
all_keys_set = Signal()              # → 三键全落边沿，触发采集启动
data_collection_start = Signal()     # → Unload 时序第一步
data_collection_stop = Signal()      # → Load 完成
```

### 契约 B：跨线程可调用的 2 个 slot

`LoadportBridge` 用 `QMetaObject.invokeMethod(controller, "...", QueuedConnection)` 调：

```python
set_ready_low_for_error()            # 执行机构异常 → 拉低 READY
clear_ready_low_error_latch()        # 故障复位
```

**注意**：用 `QueuedConnection` 意味着**目标 QObject 必须住在有 Qt 事件循环的线程里**。
这直接决定了第 5 节的驱动方式选择。

### 契约 C：线程封装契约

`E84ControllerThread` 用 **无参** 构造 `E84Controller()`，并且只调用 `start()` / `stop()`
（其测试里的 `FakeController` 也只实现这两个方法）。适配层必须满足"无参可构造"。

---

## 3. 逐项行为对照

图例：✅ 直接支持 / ⚙️ 需适配层或配置 / ⚠️ 行为有差异需确认 / ❌ 需扩展库

| # | VOC 现行为 | 依据 | 新库对应物 | 结论 |
|---|---|---|---|---|
| 1 | 握手：IDLE→WAIT_TR_REQ→WAIT_BUSY→WAIT_L/U_REQ→WAIT_COMPT→WAIT_DONE | §6.2.2.1 | `PassiveFsm` 12 状态，完整实现 | ✅ 且更完整（多出 `SELECT`/`CLOSING`/`CONT_NEXT`） |
| 2 | 握手前提 = `CS_0 ∧ VALID`（GO 已于 2026-10-09 删除） | R01 | `preconditions: [VALID, CS_0]` | ✅ 一比一 |
| 3 | 撤销前提即撤输出回 IDLE | R01 | `_on_precondition_lost`：`VALID` 已落→IDLE；仍为 ON→HO_ABORT | ⚠️ 第二种情况新库会先拉低 `HO_AVBL` 等对方闭合，比"无条件回 IDLE"更安全；现场观察到的现象会不同 |
| 4 | 三键：任意键=有料、三键全落=落位 | R02 | `keys(any)` / `keys(all)` | ✅ 一比一 |
| 5 | 200 ms 轮询 + 三键 200 ms 非阻塞去抖 | — | `poll_interval_ms` 5 ms + 逐信号 `debounce_ms` | ✅ 改善（`TD0` 只有 100 ms，200 ms 轮询会漏采） |
| 6 | 方向在握手瞬间锁定（B6 修复） | — | `SELECT` 阶段定方向并锁定 | ✅ 一比一 |
| 7 | **完整落位**才撤 `L_REQ`（R02 修复） | §6.1 表 1 | `carrier_in_position` 与 `carrier_present` 分离 | ✅ 一比一 |
| 8 | 计时：`ShortTimer=2s` / `LongTimer=60s` 各阶段复用 | 表 6 | `TP1`–`TP6` 独立命名、独立可配 | ✅ 改善（旧实现 `WAIT_DONE` 的超时是死代码） |
| 9 | `HO_AVBL` / `ES` 由三键状态**每次扫描静态重写** | — | 由可用性/安全链合成 | ❌ 见扩展 E1/E2 |
| 10 | 无 `CS_1`、无 `CONT`、无同时/连续交接 | — | 全支持 | ✅ 可选用；单载口配置即可 |
| 11 | E84 信息灯（CODE/CHARGE/PLACED/LOAD/UNLOAD/ALARM_LED） | — | 新库不含 LED | ✅ **现场已停用**（`_InfoKeyOnlyController` 空操作），不需要移植 |
| 12 | `all_keys_set`：三键首次全落的上升沿只发一次 | — | `PortSnapshot.carrier_in_position` 的上升沿事件 | ⚙️ 适配层订阅 `CARRIER_SETTLED` 或自己边沿检测 |
| 13 | `data_collection_start`：Unload 且进入 `WAIT_TR_REQ`（`VALID`↑ 之后、`TR_REQ` 之前） | 现场硬约束 | `EventType.DEMAND_ASSERTED`（`op=unload`）**恰好同一拍** | ⚙️ 事件映射，时序对齐 |
| 14 | `data_collection_stop`：Load 且进入 `WAIT_DONE`（`COMPT`↑ 之后、`FOUP_status` 为真） | 现场硬约束 | `EventType.COMPT_RECEIVED`（`op=load`）**恰好同一拍** | ⚙️ 事件映射，时序对齐 |
| 15 | `_actuator_error_latched`：强制 IDLE + 撤 `L_REQ/U_REQ/READY`，`HO_AVBL`/`ES` 仍按三键 | R03 | `raise_fault(EXTERNAL)` → `FAULT_WAIT_CLOSE`→`FAULT_LATCHED`：`READY`/`ES`/`HO_AVBL` 全拉低、**请求线保持**、需 `clear_fault()` | ⚠️ 见扩展 E3 |
| 16 | `stop()` 撤回 `L_REQ/U_REQ/READY`，**刻意不动** `HO_AVBL`/`ES` | R04 | `DISABLED` 状态把**全部**输出驱到安全态（含 `ES`/`HO_AVBL` 拉低） | ⚠️ 有意的失效安全变更，需现场确认 |
| 17 | `E84_TestOutPin()`：`sleep(1)` 阻塞翻转 | — | `python -m e84.cli force ... --i-know-what-i-am-doing` | ✅ 替代且不阻塞 |
| 18 | 引脚在 `__init__` 里申请；LED 引脚与状态灯冲突（GPIO7/GPIO25） | — | 引脚在 `start()` 申请；**不配置就不申请** | ✅ 冲突自然消失（只配 E84 那几个通道即可） |
| 19 | 导入期硬依赖 `RPi.GPIO`（测试必须伪造 `sys.modules["RPi"]`） | — | 后端惰性导入 | ✅ 改善 |

---

## 4. 三处必须补的扩展

### E1 + E2：`HO_AVBL` 的现场策略表达不了

VOC 的真值表（`Refresh_Input`）：

| 三键 | `FOUP_status` | `FOUP_docked` | `HO_AVBL` | `ES` |
|---|---|---|---|---|
| 全落 | True | True | **ON** | ON |
| 部分落（1–2 键） | True | False | **OFF** | OFF |
| 无键 | False | False | **ON** | ON |

即 `HO_AVBL = NOT(部分落) = (全落) OR (无键)`。

新库当前**表达不了这个**，原因有二：

1. 传感量来源只有 `input` / `keys(any)` / `keys(all)` / `external` / `constant`，
   没有"部分落位"（`any AND NOT all`）这个组合；
2. 载口可用性只能靠 `set_port_available()` 从上层注入，不能来自传感器。

**建议的最小扩展**（本库侧，通用且不贵）：

```python
# sensors.py
mode: "any" | "all" | "partial"     # 新增 partial = any and not all
invert: bool = False                 # 新增：对组合结果取反

# config/model.py
class LoadPortConfig:
    available: SensorSpec = constant(True)   # 新增
```

于是 VOC 的现场策略可以纯配置表达，一行不改协议核心：

```yaml
load_ports:
  - id: LP1
    carrier_present:     {source: keys, channels: [K0,K1,K2], mode: any}
    carrier_in_position: {source: keys, channels: [K0,K1,K2], mode: all, debounce_ms: 200}
    # HO_AVBL = NOT 部分落位 = 全落 OR 无键
    available:           {source: keys, channels: [K0,K1,K2], mode: partial, invert: true}
```

> 注意 `ES`：VOC 把 `ES` 也和 `HO_AVBL` 一起按三键驱动。新库的 `ES` 语义是
> "本机安全链健康"（`set_es_ok()`）。**建议不要把 `ES` 接回落位键**——旧做法
> 让真实急停/安全门无法通过 `ES` 表达（这是旧实现的一个已知缺陷）。
> 如果现场坚持要"部分落位也拉低 `ES`"，那属于把落位条件接进安全链，应由硬件联锁完成。

### E3：故障态信号画像不同（需现场确认，不一定要改代码）

| | VOC | 新库 |
|---|---|---|
| `READY` | OFF | OFF |
| `L_REQ`/`U_REQ` | **立即 OFF** | **保持**到 `VALID`↓（相关信息 1 R1-1.1.2.1） |
| `ES` | 仍按三键 | **OFF**（请求停止） |
| `HO_AVBL` | 仍按三键 | **OFF** |
| 恢复 | `clear_ready_low_error_latch()` | `clear_fault()`（握手闭合后才允许） |

新库的行为**更符合标准**（"钉住请求线防止对方二次动作"、故障时宣告不可交接+请求停止），
但它改变了对方向看到的画像。**这一条必须在天车侧确认过再上线**，否则可能出现
"对方等一个我们不再发的信号"这类对接问题。

---

## 5. 驱动方式：这是最大的一个技术决策

VOC 现在用 **QTimer + QThread**：控制器住在 worker 线程，靠该线程的 Qt 事件循环跑
`refresh_timer`，并用 `QMetaObject.invokeMethod(..., QueuedConnection)` 跨线程调 slot。

新库有两种接法：

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| **(a) 推荐**：`ManualRunner` + 现有 QTimer | 适配层保留 `refresh_timer`，`timeout` 里调 `runner.poll()`；`invokeMethod` 的 slot 直接转发到 `Equipment`/`PiOController` | 改动最小；Qt 事件循环语义不变；R17 线程测试基本不用改 | 轮询周期受 Qt 事件循环调度影响（可设 5 ms，实际抖动仍在毫秒级，远小于 `TD0`=100 ms） |
| (b) `ThreadRunner` + 去掉 `QueuedConnection` | 新库自带线程驱动；跨线程调用改为直接调用 | 不依赖 Qt 事件循环 | `QueuedConnection` 就失效了（目标 QObject 不在 Qt 线程），`LoadportBridge` 的 `invokeMethod` 必须改成直接调用；R17 线程语义需重新验证 |

两者都可行。**(a) 是推荐路径**——它把"替换协议实现"这件事的影响面压到最小，
不动线程模型、不动信号投递语义。

> 补充：新库的 `PiOController.poll()` 有非阻塞重入保护，
> `clear_fault`/`raise_fault`/`abort` 都在 `RLock` 内，所以即使用方案 (b) 跨线程调也安全。

---

## 6. 三个真正的风险点（按严重度排序）

### ⚠️ R1：`data_collection_start/stop` 的时刻必须逐拍对齐

这是**现场最贵的 bug 类型**。VOC 的注释和测试都在强调：

* `START` 必须在**网络（对插连接器）还连着**的时候发出去，晚了整趟飞行零数据；
* Unload 的顺序是 **先 START → 再断对插/解锁**（`test_loadport_bridge_order.py` 钉死）；
* Load 的顺序是 **先加锁 → 先下载日志 → 再 STOP**（下位机 stop 后 5 s 反挂载 SD 分区）。

新库的对应事件时刻是**同一拍**（`DEMAND_ASSERTED` / `COMPT_RECEIVED`），但这需要
用**真实波形或台架**逐拍验证，不能靠"读代码觉得一样"。

**缓解**：新库有 `TraceRecorder`，可以在台架上把两侧信号都录下来逐点比对。

### ⚠️ R2：`HO_AVBL` 策略差异（E1/E2 未补之前）

不补 E1/E2，现象是：载具只压住一个键时，VOC 会在天车**到达前**就把 `HO_AVBL` 拉低
（天车不来）；新库会在天车**到达并等待 1.5 s 后**才走 `HO_ABORT` 拉低
（天车白跑一趟，且现场会看到一次 `HO_ABORTED`）。

### ⚠️ R3：故障画像差异（E3）

见 4.3。需要天车侧确认。

---

## 7. 建议的替换路径

```
Phase 0  离线回放（不动现场）
        用旧实现录 24h 信号轨迹（TraceRecorder 或现场日志）
        → 喂给新库的 VirtualClock 回放 → 比对状态与输出序列
        目的：在碰硬件之前把行为差异全部找出来

Phase 1  适配层 + 测试
        E84ControllerAdapter（保留 6 信号 + 2 slot + 无参构造）
        + 配置（example_voc_compat 已有雏形）
        + 补 E1/E2 两个库扩展
        + 改写 2 个测试文件、新增 1 个适配层测试
        目的：pytest 全绿，且 R1/R2/R3 有明确结论

Phase 2  台架（dummy load port / 手动按钮模拟天车）
        逐条跑 §6.2.2.1 的 13 步；录波形比对 R1
        目的：确认时序逐拍一致

Phase 3  现场单台 + 影子观察
        保留旧实现的日志输出，新库全量开 trace
        目的：真实天车跑若干趟后比对

Phase 4  全量切换，旧实现与测试删除
```

**不建议的做法**：
* ❌ 直接把 `E84Controller(...)` 换成 `Equipment(...)` —— 信号契约不同，会牵连
  `LoadportBridge` 与 QML
* ❌ 先删旧实现再移植 —— `data_collection` 的时刻约束会在这中间丢掉
* ❌ 把 `ES` 继续接回落位键 —— 那会让真实安全链无法通过 `ES` 表达

---

## 8. 工作量估算

| 项 | 规模 |
|---|---|
| 库扩展 E1（`partial` + `invert`） | ~30 行 + 测试 |
| 库扩展 E2（`available` 传感量） | ~20 行 + 测试 |
| `E84ControllerAdapter`（Qt 信号/slot 契约 + 事件映射） | 200–300 行 |
| VOC 侧配置（YAML 或代码内 dict） | ~60 行 |
| `e84_thread.py` 改动 | ~20 行 |
| 改写/新增测试 | 2 个文件改写 + 1 个新增 |
| 删旧实现 | −618 −85 行 |

**净效果**：VOC 侧自有 E84 代码从 618+85=703 行降到 ~300 行（适配层 + 配置），
协议实现交给一个 8400 行、262 项测试、有交叉验证与标准条款追溯的库。

---

## 9. 顺带发现的两个既有问题（与替换无关，但值得记）

1. **`E84_InfoLED` 的引脚冲突**是"临时"处理的（`_InfoKeyOnlyController` 把
   `set_output` 变成空操作）。这意味着**所有 LED 逻辑都成了死代码**但仍在调用
   （实测 17 处 `E84_InfoPin.set_output` 调用点）。替换时可以直接删掉，不必移植。
2. **`test_e84_handshake_safety.py` 依赖私有名**（`E84_InSig_Value`、`E84_OutSig`、
   `SIG_ON`、`_process_state`）与"伪造 `sys.modules['RPi']`"。这类测试契约会把
   实现细节锁死——新库的测试用 `SimIO` + `VirtualClock` + `VirtualRig` 就没有这个问题。
   替换时这部分测试**建议直接重写而不是移植**。
