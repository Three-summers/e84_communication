# 第三方 E84 实现调研（能否做独立对拖）

> 目的：交叉验证里最后一个缺口是——**被动端与主动端都是我写的**，两侧共享同一份
> 对标准的理解。要彻底打破这个循环，需要一份**第三方实现**做对拖。
> 这份文档记录调研结果，供后续决定是否需要采购/借用。

---

## 1. 结论

**不存在开源的第三方 SEMI E84 实现。**

| 检索方式 | 查询 | 结果 |
|---|---|---|
| GitHub 仓库搜索（API） | `HO_AVBL` | **0** |
| GitHub 仓库搜索（API） | `semi e84 handoff` | **0** |
| GitHub 仓库搜索（API） | `wafer carrier handoff` | **0** |
| GitHub 仓库搜索（API） | `E84 loadport OR load port handoff` | **0** |
| GitHub 仓库搜索（API） | `load port semiconductor simulator` | **0** |
| PyPI | 包名 `e84` | **404（不存在）** |
| 通用网页搜索 | `open source SEMI E84 implementation` 等 10 余组查询 | 只出现**商业产品**与**厂商文档**，无任何代码仓库 |

（`e84 in:readme` 返回 628 条结果，但全部是十六进制串里的巧合匹配，与本标准无关。）

## 2. 为什么开源实现很少——这不是巧合

E84 是一条**硬线 24 V / 光耦的并行 I/O 握手**，它的落点是：

* PLC 梯形图；
* 设备厂商的板级固件；
* 少部分用 MCU/树莓派自制的接口板（例如本仓库要替换掉的 VOC 实现）。

也就是说，能被"开源"的那一层（应用侧协议库）**本来就不存在社区**：每家设备厂
都为自己的 FAT/SAT 私有实现一遍，做完就封存。加上 SEMI 标准本身是要付费购买的
（[SEMI E84 官方页面](https://store-us.semi.org/products/e08400-semi-e84-specification-for-enhanced-carrier-handoff-parallel-i-o-interface)），
这也是生态碎片化的原因之一。

**这恰好解释了为什么本库有价值**：把这一层从"每家重写一遍"变成可复用、
可测试、可审计的库。

## 3. 可作为独立参照的**商业**方案

代码拿不到，但**行为判据是公开的**，而且这些判据来自第三方对同一份标准的实现经验。

| 方案 | 说明 | 手册 |
|---|---|---|
| **GCI / Get Control E84 Emulator** | 主动/被动双模式，内置 **17 项主动模式符合性测试**，且明确支持在**同时交接模式**下跑同一组测试 | [Application Users Manual](https://www.getcontrol.com/downloads/E84_Emulator_Application_Users_Manual.pdf)、[User Manual](https://getcontrol.com/downloads/E84_Emulator_User_Manual.pdf)、[Getting Started](https://www.getcontrol.com/downloads/E84_Emulator_Getting_Started_Guide.pdf) |
| **GCI E84 SPC** | PI/O 接口控制器 + 软件，含 E23 模式 | [Tech Ref](https://getcontrol.com/downloads/E84_SPC_SW_Tech_Ref_Manual_5E.pdf)、[E23 Addendum](https://getcontrol.com/downloads/E84_SPC_v5_E23_Mode_Addendum.pdf) |
| **GCI E84 Analysis** | 时序分析 | [Manual](https://getcontrol.com/downloads/E84_Analysis_User_Manual.pdf) |
| 光收发器 / HHT 手持仿真器 | 硬件，用于现场对拖 | [代理商页面](https://www.bihec.com/getcontrole84/) |
| 厂商 Load Port 自带的 E84 自检 | 例如 [Fortrend](https://www.fortrend.com/Data/fortrend/upload/file/20250508/SEMICONDUCTOR%20CORE%20COMPONENTS.pdf) 等载口厂商的产品通常内置 | 各家规格书 |

> 这些手册**不能替代标准原文**，但它们的"Failure - ..."条目是**独立于我的理解**
> 的判据，因此可以用来做交叉核对。

## 3bis. 项目方提供的本地资料（已用上）

项目方在 `/mnt/c/.../E84协议相关资料` 下提供了一批资料，本轮**逐份核对**并用上了三类：

| 资料 | 用途 | 结果 |
|---|---|---|
| **SEMI E84-0301 原文 PDF** | 逐条核对实现，取代此前的二手译文 | **抓出 B12（引脚表错误）**；确认连续交接中 `VALID` 会落下再抬起（`TP6`/`TD1` 的定义只在"每段是完整单次交接"下成立）；确认 Table 1/5/6/7 的定时器取值范围与典型值全部正确；确认单次交接 13 步与注 3/4/5 的实现逐条吻合 |
| **GCI E84 Emulator Application Users Manual 2.4a** | 提取第三方符合性判据 | **抓出 B13（中止时抢跑撤请求线）**；提取出 15 项被动模式功能测试的**完整步骤序**，落成 X5 的 Test E/F/G 测试 |
| **SEMI E23-1104** | 前身标准（Cassette Transfer Parallel I/O）；**引脚分配的独立第二证据** | **印证了 B12 的修正**：E23 的引脚表是 `1 CS1 L_REQ / 2 CS1 U_REQ / 3 CS1 READY`——**L_REQ 与 U_REQ 是两个独立引脚**。同时可看出 E84 相对 E23 的演进：E23 是 `CS_0~CS_2` 三位、每个载口一组 `L_REQ/U_REQ/READY`；E84 改为两位 `CS_0/CS_1` + 共用请求线，并新增 `HO_AVBL`/`ES`/`CONT`（E23 中不存在这三个信号，已核对为 0 命中） |
| **SEMI E87-0301** | CMS：访问模式与载口可用性 | **发现可补的约束并已实现**：① 访问模式是**每个载口**各自拥有的状态模型（与本库 `LoadPort.access_mode` 一致）；② §11.1.2 规定访问模式"可在任何时候切换，**但载具交接期间除外**" → 新增 `transfer_in_progress` 属性（按 E87 Table 8 的 AUTO 交接边界 = READY 有效 → 交接完成）与 `set_access_mode(..., strict=True)` 守卫；③ §11.3.3.2 规定 MANUAL 下只允许人工交接、且**设备需具备"AMHS 硬来时告警"的能力** → 本库在手动模式下把 `HO_AVBL` 拉低、绝不断言请求线，并在 AMHS 硬来时发 `HO_ABORTED` 事件供上层告警 |
| SEMI E15.1-0600 / SEMI E57-0299 | **纯机械**标准（载口几何 / 运动学联轴器） | 均无任何协议内容（按 `L_REQ`/`U_REQ`/`VALID`/`HO_AVBL` 检索为 0 命中）。E84 §6.5 引用 E15.1 只为规定接口传感器单元的**安装空间** |
| **GB/T 44375—2024** | 国标《300 mm 半导体设备装载端口要求》 | 同样**不含协议内容**：§4.5 给出与地面搬运系统交互的**光电传感器安装空间尺寸**（`D7`≤450、`D7`…`H7`=250、`D8`≥30、`H8`≥50、`W8`≥100、`H9`≤12、`W9`≤22 mm，光轴须落在 `W9×H9` 内）。已摘录到 [bringup_guide.md](bringup_guide.md) §1.4 作为现场安装判据 |

### 从仿真器手册提取的「15 项被动模式功能测试」

厂商手册把这 15 项分为静态与功能两类。**能落到软件测试的**已在 `tests/test_vendor_criteria.py` 覆盖：

| # | 测试 | 性质 | 本库覆盖 |
|---|---|---|---|
| A | 主动设备定时器 `TA1`–`TA3` 可在 1–999 s 内配置（§6.3.2.1） | 配置检查 | ✅ `test_reference_data.py::test_timer_ranges_follow_the_1_to_999_rule` |
| B | +24 V 满载/空载电压在 18–30 V 之间（§6.4.2.1） | **电气**，需硬件 | ❌ 需万用表/手持测试器，属接线验收 |
| C | 单次交接 **装载**时序（19 步，含 `TP1`–`TP5` 各项失败判据） | 功能 | ✅ 图 10 波形测试 X3 + `test_fsm_*` + `test_reference_data` |
| D | 单次交接 **卸载**时序 | 功能 | ✅ 图 11 波形测试 + `test_fsm_single` |
| E | Handoff Available **1**（`VALID` ON 后、请求线 ON 前拉低 `HO_AVBL`，即窗口 a） | 功能 | ✅ `test_emulator_test_e_window_a_abort_has_no_demand_to_hold` |
| F | Handoff Available **2**（主动设备**到达之前**就拉低 `HO_AVBL`） | 功能 | ✅ `test_emulator_test_f_unavailable_before_arrival_never_starts` |
| G | Handoff Available **3**（`TR_REQ` ON 后拉低 `HO_AVBL`，即窗口 b） | 功能 | ✅ `test_emulator_test_g_abort_holds_demand_until_handshake_closes`（**该测试抓出 B13**） |
| H/I | `TA1` / `TA2` 超时告警 | 主动侧行为 | ✅ 本库主动侧参考实现有 `TA1`/`TA2` 超时（`test_active_reference.py`） |
| — | 紧急停止（`ES`）装载/卸载操作 | 主动侧行为 | ✅ 本库把 `ES` 作为输出按本机安全链驱动（`set_es_ok`），并有测试；主动侧对 `ES` 的响应在参考实现中 |
| — | 访问模式切换 / 交接中禁止切换 / 手动模式下拒绝自动交接（**SEMI E87** §11.1.2、§11.3.3.2） | 功能 | ✅ 新增 3 项测试：`test_e87_transfer_in_progress_window`、`test_e87_access_mode_change_rejected_during_transfer_when_strict`、`test_e87_manual_mode_never_asserts_demand_on_amhs_attempt` |
| J | WIPS Jeopardy Test 1–3 | 厂商/AMHS 专有 | ❌ 与本标准无关，属特定 AMHS 厂商的附加要求 |

> 静态测试 A/B/C（连接器标签、插头位置、恢复流程文档）是**人工目视项**，
> 见 [bringup_guide.md](bringup_guide.md) 的验收清单。

## 4. 已经用上的独立判据（无需采购）

从公开手册里提取到的判据已经落成可执行测试，见
[`tests/test_vendor_criteria.py`](../tests/test_vendor_criteria.py)：

| 来源判据（厂商手册原文） | 对应要求 | 测试 |
|---|---|---|
| *Failure - HO_AVBL signal did not turn ON following operator switch to automated access mode* | 手动→自动切换后，`HO_AVBL` 必须**恢复** ON（测的是恢复方向，不只是"手动时拉低"） | `test_ho_avbl_returns_on_after_switch_back_to_automated_access_mode` |
| *Failure - VALID signal did not turn OFF following drop of HO_AVBL signal* | 被动方拉低 `HO_AVBL` 后必须**等 `VALID` 落下**才恢复；期间即使本机条件恢复也不得抢跑（§6.2.5.2） | `test_ho_avbl_is_not_restored_while_valid_is_still_on` |
| 检查窗口 a / b 被分开描述（§6.2.5.1、图 18/19） | 补上**窗口 b**（`TR_REQ` ON → `READY` ON）内的中止路径 | `test_ho_avbl_drop_inspection_window_b_aborts_handshake` |
| *The 17 active mode tests … can be run in Simultaneous Handoff Mode as well* | 同时交接下 §6.2.5 的可用性语义必须与单次交接一致，且任一载口异常即宣告整个 PI/O 不可交接 | `test_ho_avbl_semantics_identical_in_simultaneous_handoff` |

这 4 条**与我的实现没有共享同一份理解**，因此是真正意义上的交叉核对。
（如果你能提供厂商手册的完整 17 项判据清单，可以继续补齐。）

## 5. 建议

按性价比排序：

1. **最高性价比:拿到现场真实波形。** 一次真实交接的逻辑分析仪/示波器记录，
   比任何仿真器都更有说服力——它包含了真实的光耦延迟、抖动、对方实现的怪癖。
   拿到后我可以直接变成黄金数据回放（见 [bringup_guide.md](bringup_guide.md) §5）。
2. **借用一台商用 E84 Emulator 做 FAT/SAT。** GCI 的仿真器支持主动/被动双模式与
   同时交接，正好能对我们这个被动端做对拖。这属于一次性设备验证，不要求开源。
3. **暂时维持现状并明确标注风险。** 当前已经有 4 类交叉验证
   （层级差分 / 表示矩阵 / 图驱动波形 / 反向交叉）+ 第三方判据测试，
   循环论证的**大部分**已经被打破；残余风险是"我对标准的理解"仍是单点，
   这一点在文档里已如实标注（[verification.md](verification.md) §5「尚未验证的部分」）。
