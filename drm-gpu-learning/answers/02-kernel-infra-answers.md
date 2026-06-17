# 第 02 章 课后练习题参考答案

> 对应正文：[`02-kernel-infra.md`](../02-kernel-infra.md) 第 2.13 节。先独立完成再核对。
> 每题给出**标准答案 / 关键点 / 常见错误 / 源码锚点**。

---

## L1 概念辨析

### 2.13.1 总线-设备-驱动模型

**标准答案**：三角色——`struct device`（设备实例，如 `pci_dev`）、`device_driver`（驱动，PCI 特化为
`pci_driver`）、`bus_type`（总线，负责按 ID 匹配设备与驱动）。流程：枚举设备 → 在各驱动 `id_table`
找匹配 → 命中调 `probe` → 工作 → 移除调 `remove`。DRM 设备必须挂在某 `struct device` 上是因为它要
借总线模型管理生命周期、sysfs、电源等；vkms 无真实硬件，故用 faux 总线伪造一个父 `device`。
**锚点**：2.4.1、第 00 章 0.5.2。

### 2.13.2 or_reset 语义

**标准答案**：`drmm_add_action_or_reset` 在**登记动作本身失败**时，立即执行一次该动作以清理刚申请的
资源，避免泄漏。它解决了手写 goto 阶梯里"刚申请就出错、要记得只回滚这一步"的繁琐与易错。
**锚点**：2.4.2、`drm_managed.h`。

### 2.13.3 三类锁

**标准答案**：spinlock——不睡眠、护极短临界区、可在中断/原子上下文取，争用时自旋耗 CPU；mutex——可
睡眠、护较长临界区、只在进程上下文取，争用时睡眠让出；rw_semaphore——读多写少，多读者可并发持读锁、
写者独占。**关键约束**：原子/中断上下文只能 spinlock。**锚点**：2.6.5。

### 2.13.4 wound-wait

**标准答案**：每个加锁事务有全局递增 ticket（资历）；争用时"新"事务被 wound（下次访问争用锁返回
`-EDEADLK`）让路给"老"事务，老事务直接等待→无饥饿。ticket 用于判定资历；"回退后先锁争用对象"保证
下一轮在那把锁上不再输给同一对手→系统必能前进、无死锁。**锚点**：2.6.1。

### 2.13.5 kref

**标准答案**：`kref_init` 置 1；`kref_get` 原子 +1；`kref_put(&kref, release)` 原子 -1，**减到 0 调
release**。kref 保证**计数操作**线程安全，但"该不该 get/put、借用当拥有"是逻辑问题，kref 管不了，
漏 put 泄漏、多 put UAF。**锚点**：2.4.4、2.6.3。

### 2.13.6 RCU vs SRCU

**标准答案**：RCU 读侧无锁 + 延迟释放（grace period 后才 free）；SRCU 是允许**读临界区内睡眠**的
变体。普通 RCU 读临界区**禁忌睡眠**（破坏 grace period 语义）。**锚点**：2.6.2。

---

## L2 数据结构与源码阅读

### 2.13.7 `#action` 字符串

**标准答案**：`drmm_add_action(dev, action, data)` 展开传入 `#action`（动作函数名的字符串），用于
调试——内核可在资源列表/出错时打印是哪个动作，便于定位"哪个登记的清理出了问题"。
**锚点**：`drm_managed.h:25`、2.4.2。

### 2.13.8 drm_exec 范式逐句

**标准答案**：`drm_exec_until_all_locked`——会重试的循环，直到全部锁成功；`drm_exec_prepare_obj(obj,n)`
——锁该对象的 dma_resv 并预留 n 个 fence 槽；`drm_exec_retry_on_contention`——若返回 `-EDEADLK` 则
回退并跳回循环开头；`drm_exec_for_each_locked_object`——遍历已锁的全部对象（此刻可安全加 fence/改
状态）。**锚点**：`drm_exec.c` DOC、2.5.1。

### 2.13.9 逆序解锁 + put

**标准答案**：`drm_exec_unlock_all` 逆序 `dma_resv_unlock` 并 `drm_gem_object_put`。逆序解锁是良好
习惯（与加锁相反，减少抖动）；每个对象 put 是因为加锁时 `_get` 过引用以保证锁期间对象不被释放
（借用→拥有），解锁时配对 `_put` 归还。**锚点**：`drm_exec.c` `drm_exec_unlock_all`、2.5.3/2.6.3。

### 2.13.10 get/put 触发 free

**标准答案**：`drm_gem_object_put` 经 `kref_put(&obj->refcount, drm_gem_object_free)`，在计数**减到 0**
（最后一个引用）时触发 `drm_gem_object_free`。`drm_gem_object_get` 只 +1，不触发。
**锚点**：`drm_gem.h:555/562`。

### 2.13.11 ★ 两种封装同一算法

**标准答案**：`drm_modeset_lock` 用显式 `retry:` 标签 + 判 `-EDEADLK` + `drm_modeset_backoff` +
`goto retry`；`drm_exec` 用 `until_all_locked{ prepare; retry_on_contention }` 声明式宏循环。两者底层
都是同一个 ww-mutex（ticket + wound-wait + 回退先锁争用对象），只是封装层次不同。**锚点**：2.5.2、2.6.1。

---

## L3 机制分析

### 2.13.12 AB-BA → ww 解开

**标准答案**：死锁场景——T1 lock(A) 求 B、T2 lock(B) 求 A，互锁。ww 解法：设 T1 老、T2 新；T2 求 A
时发现 A 被老 T1 持有→T2 被 wound，其持有的 B 锁下次操作返回 `-EDEADLK`→T2 回退(解锁 B)→下一轮 T2
先 lock(A)(等 T1 放)、再 lock(B)→不再 AB-BA。**评分**：标出谁 wound(T2)、谁回退(T2)、回退后先锁谁(A)。
**锚点**：2.6.1。

### 2.13.13 无锁查询 + kfree_rcu

**标准答案**：查询者在 `rcu_read_lock()` 临界区内取 fence 指针并读状态；即便此时另一方在释放该 fence，
`kfree_rcu` 也会把真正 free 推迟到所有当前读者退出（grace period 之后），故查询者在临界区内拿到的
指针始终有效。这样查询无需上锁→读侧近零成本。**锚点**：2.6.2、`dma-fence.h`。

### 2.13.14 中断里要睡眠

**标准答案**：硬中断上下文不能睡眠/取睡眠锁，否则系统挂死。应把重活挪到 **threaded IRQ**（中断线程）
或 **workqueue**。数据从上半部传到下半部：上半部读硬件状态存进设备结构/work 的数据字段、调度下半部
（`queue_work`/唤醒中断线程），下半部在可睡眠上下文取数据干活。**锚点**：2.5.5、第 01 章 1.5.4。

---

## L4 设计与对比

### 2.13.15 drm_exec 封装价值

**标准答案**：裸 `retry:`/`goto` 写法每个驱动各写一遍，易漏处理 `-EDEADLK`、易写错回退；drm_exec 把
回退重试封装成声明式循环，调用者只声明"锁哪些 + 拿到后干啥"，**易写对、易读、可复用**，降低整类锁
bug。**锚点**：2.5.1/2.5.2、2.11 谬误 2。

### 2.13.16 一致性 vs 流式

**标准答案**：一致性映射长期存在、CPU/设备都一致可见，适合描述符环/固件区，但占用一致内存且数量有限；
流式映射临时映射一次传输、用完 unmap、含缓存同步，适合一次性大缓冲。Xe 给 BO 系统内存页选流式，因为
BO 是按需映射/解映射的大块用户数据、生命周期与一次使用绑定，用流式 + IOMMU 更合适。**锚点**：2.6.4、1.6.5。

### 2.13.17 借用 vs 拥有

**标准答案**：高频路径上每次 get/put 都是原子操作（数十纳秒 + 缓存行争用），累积可观；若调用者已持有
引用/锁保证对象存活，被调方可"借用"而不 get/put，省开销。风险：借用前提（对象不会在使用期内死）若
不成立则 UAF；规避：明确接口契约（注释/命名）、用 lockdep/KASAN 验证、只在确有上层保证时借用。
**锚点**：2.6.3、2.7。

---

## L5 动手 / 调试

### 2.13.18 ★ ftrace 看锁

**要点**：`trace-cmd record -p function_graph -l 'drm_*' -l 'ww_mutex_*' -l 'drm_modeset_*' modetest
-M vkms -s ...`；在调用图里 `drm_ioctl`→atomic→`drm_modeset_lock`/`ww_mutex_lock` 处即在锁 CRTC/
plane/connector 等 KMS 对象。**锚点**：2.9.1。

### 2.13.19 ★ lockdep

**要点**：开 `PROVE_LOCKING` 跑操作，dmesg 无告警即锁序健康；分析样例告警时按"两条相反的持X求Y链
= AB-BA"定位，修复为统一锁序或改 ww-mutex/drm_exec。**锚点**：2.9.2、2.11"如何读"。

### 2.13.20 ★ dma_fence tracepoint

**要点**：enable `dma_fence` 事件，观察 init/enable_signal/signal；"读侧无锁查询状态"本身不产生
tracepoint（只是读），只有状态变更（signal）等才有事件——印证 RCU 读侧的轻量。**锚点**：2.9.3、2.6.2。

### 2.13.21 ★ 引用泄漏定位

**要点**：手段——kmemleak 报未释放对象；读码核对每个 `_lookup`/`_get` 是否在所有分支配对 `_put`
（尤其错误分支）；KASAN 在后续 UAF 时给栈。修复原则：在拿到引用后用统一出口（goto 释放）保证所有
路径都 put；或缩小引用持有范围。**锚点**：2.6.3、2.11。

---

## 综合大题

### 2.13.22 ★ 三缓冲提交全流程

**参考框架**：① `drm_exec_init`；② `until_all_locked{ prepare_obj(boA); retry; prepare_obj(boB);
retry; prepare_obj(boC); retry }`——若中途 `-EDEADLK`，`retry_on_contention` 回退全部、下轮先锁争用
对象（2.6.1）；③ 加锁时每个对象被 `_get`，锁期间 kref 保证不被释放；④ `for_each_locked_object` 里
`dma_resv_add_fence(obj->resv, fence, ...)` 把提交的完成 fence 加到每个缓冲的 dma_resv（第 10 章）；
⑤ `drm_exec_fini`→`unlock_all` 逆序 `dma_resv_unlock` + `drm_gem_object_put`。**评分**：加锁回退 +
kref 保护 + fence + 解锁释放齐全。**锚点**：2.5.1、2.6.1/2.6.3。

### 2.13.23 ★ 量化

**参考**：① 全用 refcount+锁：8 对象 ×2 次原子(get+put)×20ns + 16 fence ×40ns 锁 = 320 + 640 =
**960ns/次**；② 借用 + fence RCU 读：对象借用省去 get/put（≈0）、16 fence ×2ns = **32ns/次**。差异
约 **30 倍**。在每秒数十万次提交时，前者光同步开销就吃掉可观 CPU。→ 印证 2.7 两结论：读极多用 RCU、
高频路径能借用就别 get/put。**评分**：算式合理 + 结论对应。**锚点**：2.7。

### 2.13.24 ★ 硬件需求→内核设施图

**参考**：
- **DMA 通路**（GPU 自主读写系统内存）→ `pci_set_master`、`dma_set_mask`、`dma_map_sgtable`/sg_table、
  一致性/流式映射、IOMMU。
- **中断通路**（GPU→CPU）→ MSI-X 申请(`pci_alloc_irq_vectors`)、`request_irq`、threaded IRQ/workqueue
  下半部、completion/wait_queue 唤醒等待者。
- **并发共享**（多进程/多方共享缓冲）→ ww-mutex/drm_exec（多锁）、kref（引用计数）、RCU（无锁查询）、
  rw_semaphore（读多写少）。
- **生命周期/可靠性** → drmm_/devm_ 托管、lockdep/KASAN。
**评分**：每条硬件需求都映射到正确设施，体现"硬件需求逼出内核设施"。**锚点**：2.2.1、2.8、2.10。

---

> 自评：L1–L2 应全对；L3 能独立画 AB-BA→ww 解开、讲清无锁查询；L4 能论证封装/选锁/借用的权衡；
> L5 能用 ftrace/lockdep/tracepoint 实际观察。ww-mutex（2.6.1）与 RCU（2.6.2）是本章两大难点，吃力
> 就重读对应小节 + 内核文档 `ww-mutex-design.rst` / `Documentation/RCU/`。
