# 第 01 章 课后练习题参考答案

> 对应正文：[`01-gpu-hardware.md`](../01-gpu-hardware.md) 第 1.13 节。先独立完成再核对。
> 每题给出**标准答案 / 关键点 / 常见错误 / 源码或规格锚点**。

---

## L1 概念辨析

### 1.13.1 四条通路

**标准答案**：① **配置空间**——开机枚举/配置（读身份、申报 BAR、声明能力）；② **MMIO**——CPU→GPU
方向，CPU 用 load/store 经 BAR 窗口读写 GPU 寄存器/GGTT；③ **DMA**——GPU→系统内存方向，GPU 作总线
主控自己读写系统内存（取命令/数据、写结果）；④ **中断（MSI-X）**——GPU→CPU 方向，回报完成/错误。
**关键点**：方向 + 用途要对应准。**锚点**：1.3.1。

### 1.13.2 posted write vs non-posted read + posting read

**标准答案**：posted write——CPU 把 MMIO 写交给总线即返回，写异步到达，**返回≠到达**；non-posted
read——CPU 必须等数据返回才继续。posting read 能保证前写已送达，是因为总线序保证"读会把它前面排队的
写挤到设备后才返回"，于是读返回时前写必已到达。**常见错误**：以为写返回就生效。**锚点**：1.6.1。

### 1.13.3 UC/WC/WB(LLC)

**标准答案**：UC 完全不缓存、每次直达、强一致但慢，用于寄存器/需立即可见的少量数据；WC 写被合并成
突发、CPU 写吞吐高但弱序读慢，用于"CPU 大批量写、GPU 读"的缓冲；WB(LLC) 正常可缓存，集显上若 GPU
也接 LLC 可达 CPU↔GPU 一致，用于双方频繁读写且需一致的缓冲。**锚点**：1.6.3。

### 1.13.4 集显 vs 独显一致性

**标准答案**：集显 GPU 与 CPU 共享系统内存且常共享 LLC，故某些缓冲可硬件缓存一致；独显 VRAM 在 PCIe
另一端，CPU 缓存与 GPU 间无硬件一致性，须显式管理（刷缓存/选 WC-UC/DMA 同步）。根源是**硬件结构**：
集显共享内存+LLC，独显独立 VRAM 跨 PCIe。**锚点**：1.2.3、1.6.3、1.6.6。

### 1.13.5 吞吐哲学

**标准答案**：GPU 用海量简单 EU、每 EU 用 SIMD 一次算一批数据、线程多到能用"切到就绪线程"盖住内存
延迟（超额并发）。CPU 则用大缓存+乱序+分支预测降低**单任务**延迟。前者最大化总吞吐，后者最小化单任务
延迟。**锚点**：1.2.1、1.8.4。

### 1.13.6 head/tail

**标准答案**：tail 由软件（CPU/驱动）推进（加完命令推 tail）；head 由硬件（CS）推进（消费命令推
head）。`head==tail` 表示环空、没有待执行命令。**锚点**：1.8.1。

---

## L2 数据结构与源码阅读

### 1.13.7 `__iomem` 为何不能直接解引用

**标准答案**：`regs` 是 `void __iomem *`，指向**设备内存（MMIO）**而非普通 RAM。设备内存访问有特殊
语义（非缓存、可能触发副作用、字节序/宽度敏感），必须经 `readl/writel` 等访问器，由它们生成正确的
访问指令；直接解引用会绕过这些保证（且 sparse 静态检查会报警）。**锚点**：1.4.1、`xe_mmio.c:122`。

### 1.13.8 `xe_mmio_write32` 四步

**标准答案**：(1) `xe_mmio_adjusted_addr` 算偏移；(2) `trace_xe_reg_rw` 打 tracepoint；(3) SR-IOV VF
重定向分支；(4) `writel(val, mmio->regs+addr)` **真正写硬件**。tracepoint 价值：可用 ftrace 逐条看到
驱动对硬件的每次寄存器读写，是调试硬件交互的利器。**锚点**：`xe_mmio.c:179`、1.5.2。

### 1.13.9 哑写与内存序

**标准答案**：`mmio_flush_pending_writes` 在读前做 4 次哑写（针对硬件 WA `15015404425`），把挂起的
posted 写挤出去，确保读到的是最新状态。它本质是 1.6.1"写是 posted、需手段强制落地"的体现——硬件写
不即时，驱动须额外动作保证顺序。**锚点**：`xe_mmio.c:131,192`、1.6.1。

### 1.13.10 BAR 编号

**标准答案**：`GTTMMADR_BAR=0`（MMIO+GTT）、`LMEM_BAR=2`（VRAM）、`VF_LMEM_BAR=9`（VF VRAM）。
`xe_mmio.c:100` 用 `pci_iomap(pdev, GTTMMADR_BAR, 0)` 映射 BAR0；`xe_vram.c:49` 用
`pci_resource_start/len(pdev, LMEM_BAR)` 取 VRAM 窗口。**锚点**：`regs/xe_bars.h:8`。

### 1.13.11 ★ 六类引擎

**标准答案**：RENDER(0,RCS,3D+部分计算)、VIDEO_DECODE(1,VCS,解码)、VIDEO_ENHANCE(2,VECS,视频后
处理)、COPY(3,BCS,拷贝/填充)、OTHER(4,如 GSC)、COMPUTE(5,CCS,GPGPU)。分类原因：不同引擎能力/前端
管线不同，调度、提交、能力查询都要按 class 分支。可 `git grep "engine->class"` 或
`xe_hw_engine_class_to_str` 找分支处。**锚点**：`xe_hw_engine_types.h:15`、1.8.3。

---

## L3 机制分析

### 1.13.12 内存视角全序列

**标准答案**（每步防的错误）：① 在 WC 缓冲写命令（吞吐高）；② `wmb()`/sfence——防"通知先于数据
可见"；③ 更新 tail / 敲 doorbell（posted MMIO 写）；④ posting read——防"doorbell 未真正送达"；
⑤ GPU DMA 读命令并执行；⑥ 完成→写完成标记(DMA)+MSI-X 中断（中断作为内存写，序在数据写之后→保证
CPU 进中断时数据已到）；⑦ CPU 读状态前 `rmb()`/`dma_rmb()`——防读到旧值。**锚点**：1.6.4、1.3.4。

### 1.13.13 主中断寄存器分层 demux

**标准答案**：`xelp_irq_handler` 先读 `GFX_MSTR_IRQ` 得 `master_ctl` 位图并关总开关；按位分发到
各 GT 引擎（`gt_irq_handler`）、显示（`xe_display_irq_handler`）、杂项，每层再读自己的 IIR 定位具体
来源并 ack；最后重开总开关。`master_ctl==0` 表示"不是本设备中断"，在共享 IRQ 线（`IRQF_SHARED`）下
返回 `IRQ_NONE`，让内核继续询问其他设备。**锚点**：`xe_irq.c:412,101`、1.5.4/1.5.5。

### 1.13.14 高频路径少做 MMIO 读

**标准答案**：单次 MMIO 读 ≈ 数百纳秒的 PCIe 往返（1.7.1），高频提交里频繁读寄存器会累积成可观开销
并阻塞 CPU。故现代提交（GuC，第 19/20 章）用"写内存工作项 + 敲 doorbell + 由内存里的状态/中断得知
完成"替代轮询寄存器，把 MMIO 读降到最少。**锚点**：1.7.1、1.8.2.1。

---

## L4 设计与对比

### 1.13.15 集显 vs 独显优化方向

**标准答案**：集显要省"内存带宽争用 + 拷贝"——数据本在系统内存，零拷贝/共享映射是关键，且可利用
LLC 一致性；独显要省"PCIe 往返 + VRAM 容量"——热数据驻留 VRAM（TTM 迁移）、用 rebar 缩短上传路径、
内存压力下 LRU 驱逐。**锚点**：1.2.3、1.7.1、1.7.2。

### 1.13.16 small vs resizable BAR

**标准答案**：small BAR 下 CPU 仅能看到 VRAM 的一小片（如 256MB），上传大缓冲要在"可见窗口"分片中转，
多一次拷贝、更慢、代码更复杂；rebar 把 BAR 扩到覆盖全 VRAM，CPU 可直接映射整块（配 WC），上传路径
更短。总结："硬件能力（BAR 大小）直接改变软件数据路径与性能"。**锚点**：1.7.2、`xe_pci.c:1103`。

### 1.13.17 为何包装 `xe_reg`

**标准答案**：上万寄存器 + 多代硬件 + 虚拟化场景下，用裸偏移易混淆、难携带元数据。`struct xe_reg`
带来类型安全（编译器区分寄存器与整数）与元数据携带（如 `reg.vf` 决定 VF 是否直接访问）；配
`REG_BIT`/`REG_GENMASK` 描述位域，统一可读。权衡：少量包装成本换大幅降低低级 bug 与可维护性。
**锚点**：1.4.2、1.7.3。

---

## L5 动手 / 调试

### 1.13.18 ★ lspci 对应

**预期/要点**：`[8086:xxxx]`↔配置空间身份(1.3.2/0.8.4)；两块 Memory↔BAR0(16M,MMIO+GTT)/BAR2(VRAM,
可能 256M)(1.3.3)；`MSI-X`↔中断方式(1.3.4/1.5.5)；`Resizable BAR`↔rebar(1.7.2)；`driver in use`↔
被哪个驱动认领。无 Intel 硬件时对任意 PCIe 设备做，并指出 GPU 特有的是"大 prefetchable VRAM BAR +
rebar + 显示类 class"。**锚点**：1.9.1。

### 1.13.19 ★ reg_rw tracepoint

**要点**：`ls /sys/kernel/tracing/events/xe/` 找寄存器读写事件并 enable，跑负载后看记录；`read=1`
为读（含 posting read），`read=0` 为写；`addr` 可配 Bspec 查具体寄存器。**锚点**：1.5.2、1.9.2。

### 1.13.20 ★ /proc/interrupts

**要点**：空闲 vs 跑负载两次采样，GPU(xe/i915)行计数随负载上升，反映 `xelp_irq_handler` 被调用频率
（每批完成发一次 MSI-X）。**锚点**：1.9.3、1.5.4。

### 1.13.21 ★ 缺 wmb 的偶发 hang

**要点**：定位手段——① 复查 doorbell/tail 更新前是否有 `wmb()`/`dma_wmb()`（对照 1.6.2/1.6.4）；
② 用 reg_rw trace 看写序；③ 现象是"偶发、负载高时更易现"。修复：在写命令与敲 doorbell 之间加写
屏障，必要时补 posting read。**锚点**：1.6.2、1.11 谬误 3。

---

## 综合大题

### 1.13.22 ★ 硬件层面叙述一次绘制

**参考框架**：① CPU(Mesa) 在 WC 系统内存/VRAM 里写好 batch buffer（命令）；② 这些缓冲已通过 sg_table
+ DMA 映射、并在 GPU 页表(GTT/PPGTT)里有映射（1.3.5/1.6.5）；③ `wmb()` 后更新 ring tail 或敲
doorbell（MMIO 写，posted），必要 posting read；④ GPU 的 CS 从 ring DMA 读命令、跳进 batch、派发到
EU 阵列执行（3D/compute 管线，1.8.4/1.8.5）；⑤ 期间 GPU 经 GTT 翻译访问纹理/写结果到 VRAM/sysmem；
⑥ 完成后刷必要的 L3（1.6.6）、写完成标记、发 MSI-X；⑦ CPU 进 `xelp_irq_handler` 分层 demux、
`rmb()` 后读状态、唤醒等待者。**评分**：四条通路 + 内存序 + 页表翻译 + 引擎执行齐全。

### 1.13.23 ★ 量化

**参考**：① 上传 1GB 走 16GB/s PCIe ≈ 1/16 s ≈ **62.5ms**；在 400GB/s VRAM 内拷贝 1GB ≈ 1/400 s ≈
**2.5ms**——相差 25 倍，印证"少过 PCIe、热数据驻留 VRAM"。② 每帧 1000 次 MMIO 读 ×400ns = **0.4ms**，
占 60fps 每帧 16.6ms 预算约 **2.4%**——单看不多，但若每帧上万次或在多引擎累加则迅速吃满，故"少 MMIO
读、用 doorbell+内存状态"。**评分**：算式正确 + 能推出两条原则。**锚点**：1.7.1。

### 1.13.24 ★ Bspec 热身

**参考**：如 `drivers/gpu/drm/xe/xe_hw_engine.c:428` 的 `Bspec: 72161`（引擎相关）。记录 file:line +
编号，按 0.8.2 去 Intel Graphics Documentation 检索。i915 里 Bspec 锚点更多（~150）。**评分**：能
`git grep` 定位、记录锚点、给出检索路径。**锚点**：1.8.7、0.8.2。

---

> 自评：L1–L2 应全对；L3 能独立画内存序时序图、讲中断 demux；L4 能用量化数据论证设计取舍；L5 能
> 真正用 lspci/trace/proc 观察并把现象关联回源码。1.6（内存序与一致性）是本章最易错也最关键，若该
> 部分题吃力，务必重读 1.6 全节再做一遍。
