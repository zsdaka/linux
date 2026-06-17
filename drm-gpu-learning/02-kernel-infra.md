# 第 02 章 DRM 依赖的内核基础设施

> 所属：第一篇 预备与基础　|　前置：第 00、01 章　|　代码基线：Linux `7.1.0-rc6`
>
> 第 01 章讲了硬件，第 03 章起就要钻进 DRM 框架代码。但 DRM 不是凭空造楼——它**站在内核通用设施
> 的肩膀上**：设备/驱动模型、各种锁（尤其是为多对象加锁而生的 ww-mutex）、引用计数（kref）、
> RCU、DMA 映射、延迟执行（workqueue/threaded IRQ）。不熟这些，读 DRM 代码时会被"这把锁为什么要
> 回退重试""这个对象什么时候被释放""为什么用 kfree_rcu"绊住。本章把读 DRM 必备的内核设施一次补齐，
> 并且**每个设施都落到 DRM/Xe 的真实用法**，而不是泛泛复述内核 API。
>
> 这是连接"硬件"（第 01 章）与"DRM 框架"（第 03 章起）的胶水层。学完它，后面读代码会顺畅得多。

---

## 2.1 学习目标与前置依赖

### 学习目标

学完本章，你应当能够：

1. 说清 Linux **设备/驱动模型**（`struct device`/`device_driver`/`bus_type`/`pci_driver`）与 probe/remove
   生命周期，并指出 DRM 设备如何挂在 PCI（或 faux）总线上。
2. 解释 **托管资源（devm_/drmm_）** 的机制（动作链表 + 逆序自动释放）与价值，能读懂
   `drmm_add_action_or_reset`。
3. 讲清三类锁的适用场景：**spinlock**（原子上下文/短临界区）、**mutex**（可睡眠）、**rw_semaphore**
   （读多写少），并说出选择依据。
4. **重点**：理解 **ww-mutex（wait/wound mutex）** 解决的问题（多对象任意序加锁的死锁），掌握
   "加锁→`-EDEADLK`→回退→先锁争用对象重试"的范式，并能读懂 `drm_exec` 与 `drm_modeset_lock`。
5. 解释 **kref** 引用计数的生命周期（get/put/release），能在 `drm_gem_object` 上指认它。
6. 说清 **RCU/SRCU** 的"读侧无锁、延迟释放"思想，理解 `dma_fence` 为何用 `kfree_rcu`。
7. 描述 **DMA 子系统**：`dma_addr_t`、一致性映射 vs 流式映射、`sg_table`、与 IOMMU 的关系（接 1.6.5）。
8. 区分 **延迟执行**手段：workqueue、threaded IRQ、tasklet、kthread_worker，知道各自何时用。
9. 会用 **ftrace + lockdep** 跟踪一次 ioctl 的函数与锁路径、读懂一条 lockdep 死锁告警。

### 前置依赖

- 第 00 章（vkms、托管资源初见）、第 01 章（DMA/IOMMU/中断的硬件侧）。
- C 与基础并发概念（临界区、原子操作、死锁）。

> **本章定位**：不是"内核 API 大全"，而是"**读 DRM 代码会遇到的那一小撮设施，讲到够用、且对准 DRM
> 的用法**"。每个设施都给一个 DRM/Xe 真实出处。想系统学这些设施，文末"延伸阅读"给了内核权威文档。

---

## 2.2 主题导引：DRM 是一个"重度复用内核设施"的子系统

### 2.2.1 为什么 DRM 要用这么多"高级"设施

回顾前两章建立的事实：GPU 是**异步、并发、共享**的——多个进程并发提交、GPU 异步完成、缓冲跨进程
共享、硬件随时中断。这种场景对内核设施提出了苛刻要求：

| GPU 场景的特性 | 逼出的内核设施 |
|----------------|----------------|
| 一次提交要同时锁住**多个**缓冲（任意顺序） | **ww-mutex**（避免多锁死锁） |
| 缓冲被多方（CPU 进程、GPU、显示、其他驱动）共享，谁都可能最后释放 | **kref** 引用计数 |
| fence 等对象要**无锁地被大量读**（查状态），但偶尔释放 | **RCU**（读侧无锁 + 延迟释放） |
| GPU 自主 DMA 访问系统内存 | **dma-mapping + IOMMU + sg_table** |
| 中断里不能做重活，要挪到可睡眠上下文 | **threaded IRQ / workqueue** |
| 设备热插拔/卸载要可靠回收一大堆资源 | **devm_/drmm_ 托管** |

换句话说：**DRM 的复杂度，很大一部分是"如何在并发共享下既正确又高效"——而这正是这些内核设施存在的
理由。** 看懂设施，就看懂了 DRM 代码里那些"看似绕"的写法背后的必然性。

### 2.2.2 历史视角：这些设施很多是被 GPU"逼"出来的

值得一提的是，**ww-mutex 与 drm_gpuvm/drm_exec、dma-fence/dma-resv 这些设施，相当一部分源于图形/GPU
社区的需求**，后来才上升为内核通用设施：

- **ww-mutex**：最初的强需求来自 TTM/GEM——命令提交要同时锁多个 buffer，朴素加锁必死锁。社区把
  "wound-wait 死锁避免"抽象成通用 `ww_mutex`（`Documentation/locking/ww-mutex-design.rst`）。
- **dma-buf / dma-fence / dma-resv**：跨设备缓冲共享与同步的需求（第 10 章），从 DRM 发起，成为整个
  内核（包括 V4L2、媒体）的通用机制。
- **drm_exec**：把"多对象 ww-mutex 加锁的重试循环"再封装一层，免得每个驱动各写一遍（`drm_exec.c`）。

这条"GPU 需求 → 通用内核设施"的路径，本身就是第 00 章"通用化"演进主题的体现。理解设施的来历，能
帮你预判它的设计取向。

---

## 2.3 原理与全景：DRM 用到的内核设施地图

```
┌──────────────────────────────────────────────────────────────────────┐
│ 设备/驱动模型   struct device / device_driver / bus_type / pci_driver   │
│                probe/remove 生命周期 · sysfs 暴露                       │
│  托管资源       devm_*(挂 struct device) / drmm_*(挂 drm_device)         │
├──────────────────────────────────────────────────────────────────────┤
│ 同步/并发                                                               │
│   锁:   spinlock(原子,短)  mutex(可睡眠)  rw_semaphore(读多写少)         │
│   多锁:  ww_mutex(任意序多锁防死锁) ← drm_modeset_lock / drm_exec       │
│   引用:  kref(原子refcount) ← drm_device.ref / drm_gem_object.refcount   │
│   无锁读: RCU/SRCU(读侧无锁+延迟释放) ← dma_fence kfree_rcu              │
│   等待:  completion / wait_queue                                        │
├──────────────────────────────────────────────────────────────────────┤
│ 内存/DMA       dma-mapping(dma_addr_t, 一致性/流式) · IOMMU · sg_table   │
│                (GPU 自主 DMA 访问系统内存的内核侧, 接第01章 1.6.5)       │
├──────────────────────────────────────────────────────────────────────┤
│ 延迟执行       threaded IRQ · workqueue(work_struct) · tasklet · kthread │
│                (中断下半部 / 异步合成 / 调度 work, 接第01章中断)         │
└──────────────────────────────────────────────────────────────────────┘
        ▲ 这些设施被 DRM 核心(第03章起)与 Xe(第六篇)无处不在地使用
```

本章按这张图自上而下走一遍。每块都会指出"在 DRM 哪里用、为什么这么用"。

---

## 2.4 关键数据结构

### 2.4.1 设备/驱动模型：`pci_driver` 与 probe/remove

Linux 用一套统一的"总线—设备—驱动"模型管理所有硬件。三个角色：

- **`struct device`**：一个具体设备实例（一块 GPU 就是一个 `pci_dev`，内含 `struct device`）。
- **`struct device_driver`**：一个驱动；按总线类型特化，如 PCI 的 `struct pci_driver`。
- **`struct bus_type`**：总线（pci_bus_type、platform、faux…），负责把设备与驱动按 ID 匹配。

`struct pci_driver` 关键字段（你在第 00 章 0.8.4 见过 Xe 的实例 `xe_pci_driver`）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `name` | `const char *` | 驱动名 |
| `id_table` | `const struct pci_device_id *` | 支持的 `8086:xxxx` 列表（匹配用） |
| `probe` | `int (*)(pci_dev*, id*)` | 匹配成功的回调——驱动在此初始化设备 |
| `remove` | `void (*)(pci_dev*)` | 设备移除/卸载时回调——拆除 |
| `driver.pm` | `dev_pm_ops*` | 电源管理回调（suspend/resume，第 22 章） |

生命周期：内核 PCI 子系统枚举到设备 → 在所有 PCI 驱动的 `id_table` 里找匹配 → 命中则调 `probe` →
设备工作期间 → 移除/卸载时调 `remove`。**这就是第 17 章 `xe_pci_probe` 被调用的来历**，也是第 00 章
vkms 用 faux 总线"伪造"一个父设备的原因——`drm_device` 必须挂在某个 `struct device` 上。

### 2.4.2 托管资源：`drmm_`/`devm_` 与动作链表

第 00 章已多次见到 `devm_drm_dev_alloc`、`drmm_*`。其机制核心是 `drmm_add_action`
（`include/drm/drm_managed.h:16`）：

```c
// include/drm/drm_managed.h
#define drmm_add_action(dev, action, data) __drmm_add_action(dev, action, data, #action)
#define drmm_add_action_or_reset(dev, action, data) __drmm_add_action_or_reset(dev, action, data, #action)
```

- 每次 `drmm_*` 分配/注册，都会在 `drm_device.managed.resources` 链表（第 00 章 0.4.1.1 字段表里
  见过）登记一个"释放动作"。
- 设备销毁（引用归零）时，DRM 核心**按登记的逆序**逐个执行释放动作。
- `drmm_add_action_or_reset` 的"or_reset"：如果**登记动作本身失败**，立即执行一次该动作（清理刚
  申请的东西），避免泄漏——所以它常用于"刚做完某初始化、登记其清理"的场景（第 01 章 `xe_mmio_probe_early`
  末尾的 `devm_add_action_or_reset(..., mmio_fini, ...)` 即此模式）。

| 前缀 | 挂在谁的生命周期 | 典型用途 |
|------|------------------|----------|
| `devm_*` | `struct device`（更底层、更早） | PCI 资源、MMIO 映射、IRQ |
| `drmm_*` | `struct drm_device`（DRM 对象） | KMS 对象、mode_config、encoder 等 |

**价值（呼应第 00 章 0.5.8）**：把"申请"与"释放"在登记时就配好对，消除了手写 goto 阶梯里漏释放、
错序释放的整类 bug。代价是有一点运行时记账开销与"释放时机不那么显式"的认知成本。

### 2.4.3 ww-mutex 的核心对象：`ww_mutex` 与 `ww_acquire_ctx`

ww-mutex 是本章最该吃透的设施。两个对象：

| 对象 | 角色 |
|------|------|
| `struct ww_mutex` | 一把"可回退"的互斥锁，保护一个对象（如一个 GEM 的 `dma_resv`、一个 KMS 对象的状态） |
| `struct ww_acquire_ctx` | 一次"加锁事务"的上下文，贯穿"我这次要锁的一批 ww_mutex"，携带全局递增的 **ticket（时间戳）** 用于死锁仲裁 |

关键不变量与语义（算法见 2.6.1）：

- 同一个 `ww_acquire_ctx` 下可以锁**多把** `ww_mutex`，**顺序任意**。
- 若发生争用且按 wound-wait 规则本事务该让步，`ww_mutex_lock` 返回 **`-EDEADLK`**。
- 调用者收到 `-EDEADLK` 必须**回退（解锁已持有的全部）**，然后**先锁那把争用的**、再重锁其余——
  这保证全局不会死锁。

### 2.4.4 kref：原子引用计数

`struct kref`（就是一个 `refcount_t`）配三个操作管理对象生命周期。`drm_gem_object` 是典型
（`include/drm/drm_gem.h:287`）：

```c
// include/drm/drm_gem.h
struct kref refcount;                 // :287, GEM 对象的引用计数
static inline void drm_gem_object_get(struct drm_gem_object *obj)
{ kref_get(&obj->refcount); }         // :555, +1
static inline void drm_gem_object_put(struct drm_gem_object *obj)
{ kref_put(&obj->refcount, drm_gem_object_free); }   // :562, -1, 归零则调 free
```

| 操作 | 作用 |
|------|------|
| `kref_init` | 计数置 1 |
| `kref_get` | 原子 +1（你要持有一个引用前调） |
| `kref_put(&kref, release)` | 原子 -1；**减到 0 时调用 `release` 真正释放** |

**心智模型**：每个"持有者"（一个进程句柄、一次提交、一个 framebuffer 引用……）各持一份引用；最后
一个放手的人负责销毁。GPU 缓冲被多方共享，谁先谁后释放不确定，kref 让"最后一个人关灯"自动且线程安全。

### 2.4.5 dma_addr_t 与 sg_table（DMA 侧）

- **`dma_addr_t`**：**设备视角**的地址——GPU 做 DMA 时用的地址，由 DMA API（经 IOMMU）从 CPU 物理
  地址翻译而来。**不等于** CPU 物理地址（第 01 章 1.6.5）。
- **`struct sg_table`（scatter-gather 表）**：描述"一块逻辑缓冲 = 若干物理不连续的段"，每段有
  `(page/dma_addr, length)`。系统内存大缓冲天然碎片化，用它表达。

这两者你在第 01 章 1.6.5 已见 Xe 的用法（`sg_alloc_table_from_pages_segment` + `dma_map_sgtable`），
第 11（TTM 页）、18（VM-bind）章会反复出现。

---

## 2.5 核心代码走读

### 2.5.1 多对象加锁的"标准范式"：`drm_exec`

`drm_exec`（`drivers/gpu/drm/drm_exec.c`）把 ww-mutex 的回退重试封装成一个优雅的循环。它的文件头
doc 给了**典范用法**（这段值得背下来，第 12、18、19 章到处是它的变体）：

```c
// drivers/gpu/drm/drm_exec.c (DOC: Overview)
struct drm_exec exec;
unsigned long index;
int ret;

drm_exec_init(&exec, DRM_EXEC_INTERRUPTIBLE_WAIT);
drm_exec_until_all_locked(&exec) {                 // (1) 重试循环: 直到全部锁成功
    ret = drm_exec_prepare_obj(&exec, boA, 1);     // (2) 锁 boA + 预留 1 个 fence 槽
    drm_exec_retry_on_contention(&exec);           // (3) 若争用(-EDEADLK), 跳回 (1) 重来
    if (ret) goto error;

    ret = drm_exec_prepare_obj(&exec, boB, 1);     // 锁 boB
    drm_exec_retry_on_contention(&exec);
    if (ret) goto error;
}

drm_exec_for_each_locked_object(&exec, index, obj) {   // (4) 此刻所有对象都已锁住
    dma_resv_add_fence(obj->resv, fence, DMA_RESV_USAGE_READ);
    ...
}
drm_exec_fini(&exec);                              // (5) 解锁全部、放引用
```

逐点：

- **(1)** `drm_exec_until_all_locked` 是个会"重来"的循环：一旦中途检测到争用，就回到循环开头**从头
  再锁一遍**（但下一轮会先锁上次争用那把，见 2.6.1）。
- **(2)** `drm_exec_prepare_obj(exec, obj, n)`：锁住该对象的 `dma_resv`（一把 ww_mutex），并在其上
  **预留 n 个 fence 槽**（为后面 `dma_resv_add_fence` 备位，第 10 章）。
- **(3)** `drm_exec_retry_on_contention`：若 (2) 返回 `-EDEADLK`，它触发"回退 + 重来"。这一句把
  2.4.3 的"收到 EDEADLK 要回退"封装掉了，调用者不必手写。
- **(4)** 循环正常结束后，**保证所有对象都已被同一事务锁住**，可以安全地批量加 fence、改状态。
- **(5)** `drm_exec_fini` 统一解锁、放引用（内部 `drm_exec_unlock_all`：逆序 `dma_resv_unlock` +
  `drm_gem_object_put`）。

> 这段"init → until_all_locked{ prepare; retry_on_contention } → for_each_locked → fini"是 GPU 提交/
> 页表更新的**通用骨架**。现在记住它的形状，第 18 章 Xe VM-bind、第 19 章 exec 都是它。

### 2.5.2 ww-mutex 的"裸"用法：`drm_modeset_lock` 的回退

KMS 的原子提交要锁住一堆显示对象（CRTC/plane/connector），同样用 ww-mutex。`drm_modeset_lock.c` 的
doc 给了不带 drm_exec 封装的"裸"回退写法：

```c
// drivers/gpu/drm/drm_modeset_lock.c (DOC, 概念)
drm_modeset_acquire_init(&ctx, ...);
retry:
    ret = drm_modeset_lock(lock, &ctx);
    if (ret == -EDEADLK) {
        ret = drm_modeset_backoff(&ctx);   // 回退: 解锁全部, 准备重锁争用对象
        if (!ret) goto retry;              // 重来
    }
    ... /* 干活 */
    drm_modeset_drop_locks(&ctx);
    drm_modeset_acquire_fini(&ctx);
```

对照 2.5.1，你能看出 `drm_exec` 只是把这套 `retry:`/`-EDEADLK`/`backoff`/`goto retry` 模式封装成了
更声明式的宏循环。**两者背后是同一个 ww-mutex 算法**（2.6.1）。第 06 章会精读 modeset 加锁。

### 2.5.3 引用计数实战：GEM 对象的 get/put

第 09 章会详讲 GEM，这里先看引用计数如何决定生命周期：

```c
// include/drm/drm_gem.h:555
static inline void drm_gem_object_get(struct drm_gem_object *obj)
{
    kref_get(&obj->refcount);                          // 多一个持有者
}
// :562
static inline void drm_gem_object_put(struct drm_gem_object *obj)
{
    kref_put(&obj->refcount, drm_gem_object_free);     // 少一个; 归零则真正 free
}
```

`drm_gem_object_free`（`drm_gem.c`）就是 release 回调：当**最后一个**引用被 `put` 掉，它被调用，
执行真正的销毁（解除映射、释放后端页、调驱动的 `free` 回调）。你在 2.5.1 的 `drm_exec_unlock_all`
里也见到 `drm_gem_object_put`——锁对象时 `_get`、解锁时 `_put`，保证锁期间对象不被释放。

### 2.5.4 托管动作实战：登记一个自动清理

回看第 01 章 `xe_mmio_probe_early` 的收尾：

```c
return devm_add_action_or_reset(xe->drm.dev, mmio_fini, xe);
```

它把 `mmio_fini`（内部 `pci_iounmap`）登记为该 `struct device` 的释放动作。设备移除时，内核逆序
执行所有登记动作，`mmio_fini` 被调用、BAR 被 unmap——**没有任何手写的 unmap 调用，却保证了释放**。
这就是 2.4.2 机制的运行时兑现。`drmm_*` 同理，只是挂在 `drm_device` 上。

### 2.5.5 延迟执行实战：vkms 的 composer work 与 Xe 的 worker

第 01 章说中断里不能干重活，要挪到可睡眠下半部；同理，很多"耗时但不紧急"的活也用 **workqueue** 异步
做。看两个真实例子。

**vkms 的软件合成**（`drivers/gpu/drm/vkms/vkms_drv.h:188`）：

```c
// drivers/gpu/drm/vkms/vkms_drv.h
struct work_struct composer_work;          // :188 一个待执行的"活"
struct workqueue_struct *composer_workq;   // :222 一条有序工作队列(每实例一条)
void vkms_composer_worker(struct work_struct *work);  // :319 这个活具体干啥(CPU 合成 + 算 CRC)
```

回忆第 00 章 0.5.6：vkms 的 `atomic_commit_tail` 末尾 `flush_work(&vkms_state->composer_work)` 等的就是
这个 work。流程是：原子提交时把"合成这一帧"作为一个 work **排进** `composer_workq`，由内核 worker
线程在可睡眠上下文执行 `vkms_composer_worker`（CPU 逐像素合成、算 CRC），提交侧用 `flush_work` 等它
做完。**为什么用 workqueue 而不是当场做**：合成是 CPU 密集活，放进可睡眠的 worker 线程，既不阻塞
提交路径的关键段，也能与 vblank 节奏解耦。

`composer_workq` 用的是**有序（ordered）工作队列**（注释 "Ordered workqueue"）：保证同一队列里的 work
**按提交顺序、一次一个**地执行——这对"帧要按顺序合成"至关重要。普通 workqueue 则可能并发执行多个
work。**选有序 vs 并发，是 workqueue 的一个关键决策。**

**Xe 的 worker**（`drivers/gpu/drm/xe/xe_gt_types.h:208`）：

```c
struct work_struct worker;             // :208 某类异步处理
struct workqueue_struct *ordered_wq;   // :240 GT 级有序工作队列
```

Xe 在复位、TLB 失效、GuC 消息处理等多处用 work + ordered_wq 把活从中断/原子上下文挪到可睡眠线程，
并保证顺序。第 12 章的 `drm_gpu_scheduler` 本质也建立在 workqueue/kthread 之上。

**延迟执行手段速查**：

| 手段 | 上下文 | 可睡眠 | 典型用途 |
|------|--------|--------|----------|
| 硬中断处理 | 中断上下文 | 否 | 只读 IIR、ack、调度下半部（第 01 章 1.5.4） |
| **threaded IRQ** | 内核线程 | 是 | 中断重活（需睡眠/锁）下半部 |
| **workqueue（work_struct）** | worker 线程 | 是 | 异步耗时活（vkms 合成、Xe 复位/TLB） |
| **ordered workqueue** | 单线程有序 | 是 | 需保序的活（逐帧合成） |
| tasklet | 软中断 | 否 | 轻量下半部（新代码渐少用） |
| kthread_worker | 专属内核线程 | 是 | 需独立线程/优先级的长期任务 |

---

## 2.6 机制深入

### 2.6.1 ww-mutex 的死锁避免算法（wound-wait）

这是本章的算法核心。**问题**：两个提交事务 T1、T2 都要锁 boA 和 boB，但加锁顺序可能不同：

```
T1: lock(boA) ... lock(boB)
T2: lock(boB) ... lock(boA)
   → 经典 AB-BA 死锁: T1 持 A 等 B, T2 持 B 等 A, 互锁
```

朴素解法是"全局规定加锁顺序"，但 GPU 提交里对象集合是运行时才知道的、无法预先排序。**ww-mutex 的
解法**：给每个事务发一个全局递增的 **ticket（acquire context 的时间戳）**，越早开始的事务 ticket
越小、"资历越老"。当发生争用：

- **wound-wait 规则**（Linux ww_mutex 默认）：若"老"事务想要的锁被"新"事务持有，则**新事务被
  wound（创伤）**——它在下次访问那把锁时会收到 `-EDEADLK`，必须回退让路；老事务则直接等待，保证
  老事务总能推进（无饥饿）。
- 收到 `-EDEADLK` 的事务**回退**（解锁全部），然后**先锁那把刚争用的**，再重锁其余。"先锁争用对象"
  保证下一轮在那把锁上不会再输给同一个对手 → 系统整体一定能前进。

```
T2(新)请求 boA, 但 boA 被 T1(老)持有
   → wound-wait: T2 被判负, 它持有的 boB 上的锁在下次操作时返回 -EDEADLK
   → T2 回退(解锁 boB), 下一轮先 lock(boA)(等 T1 放), 再 lock(boB)
   → 不再 AB-BA, 死锁消除
```

**为什么 DRM 离不开它**：命令提交（第 19 章）、VM-bind（第 18 章）、原子显示（第 06 章）都要锁运行时
才确定的一组对象，ww-mutex 是唯一既正确又不必全局定序的办法。`drm_exec`（2.5.1）把这套回退自动化，
你只管写"锁哪些"，回退重试交给框架。

> 权威细节见 `Documentation/locking/ww-mutex-design.rst`。记住三件事：**ticket 定资历、新者让路
> （wound）、回退后先锁争用对象**。

### 2.6.2 RCU：读侧无锁 + 延迟释放，以及 dma_fence 为何用它

**RCU（Read-Copy-Update）** 适合"读极多、写极少、读侧要快"的场景。核心思想：

- **读者无锁**：进入 `rcu_read_lock()`/`rcu_read_unlock()` 临界区（极廉价，基本是禁抢占），直接读
  指针，不加锁、不写共享状态。
- **写者**：更新时不就地改，而是"复制—改新—原子换指针"；旧版本不立即释放，而是**等所有当前读者
  退出后**（一个 grace period）才释放（`kfree_rcu`/`call_rcu`）。

`dma_fence`（`include/linux/dma-fence.h`）就是典型：fence 会被**大量并发查询状态**（"信号了吗？"），
但很少销毁。其释放走 **`kfree_rcu`**（头注释 `@rcu: used for releasing fence with kfree_rcu`），
让查询侧能在 `rcu_read_lock()` 下安全地拿 fence、读状态，而不必为每次查询都上锁。第 10 章会精读
fence 的无锁查询路径。

**SRCU（Sleepable RCU）**：允许读临界区内**睡眠**的 RCU 变体。DRM 在一些"读侧可能睡眠"的场景用它
（如设备热拔 `drm_dev_enter`/`drm_dev_exit` 保护），第 03 章会遇到。

### 2.6.3 kref 生命周期与"借用 vs 拥有"

引用计数最常见的 bug 是"拥有"与"借用"混淆：

- **拥有（owned）**：你 `kref_get` 过，用完必须 `kref_put`。
- **借用（borrowed）**：别人保证在你使用期间对象不死（如调用者持锁/持引用），你**不**额外 get/put。

读 DRM 代码要时刻问："我这个指针是拥有的还是借用的？"函数注释/命名常有暗示（`_get`/`_lookup` 通常
返回**拥有**的引用，要配对 `_put`）。kref 把"减到 0 才释放"做成原子的，但**逻辑上的配对**仍要你保证——
漏 put 泄漏、多 put 提前释放（UAF）。lockdep 管不了这个，靠 KASAN/代码审查（2.11）。

### 2.6.4 一致性映射 vs 流式映射（DMA）

DMA API 提供两种映射，对应不同生命周期：

| 类型 | API | 特点 | 用途 |
|------|-----|------|------|
| **一致性（coherent）** | `dma_alloc_coherent` | 分配一块 CPU 与设备都能一致看到的内存（必要时 uncached/WC），长期存在 | 描述符环、固件通信区等长期共享结构 |
| **流式（streaming）** | `dma_map_single`/`dma_map_sgtable` | 把已有内存临时映射给设备一次传输，用完 `unmap`；含必要的缓存同步 | 一次性 DMA 传输（如把一块缓冲交给 GPU 读） |

流式映射还需在 CPU/设备交接时 `dma_sync_*_for_cpu/device` 做缓存同步（非一致架构上）。**选错会
导致数据不一致**（呼应第 01 章 1.6.3/1.6.6 的缓存属性）。Xe 给 BO 系统内存页用的是流式
`dma_map_sgtable`（1.6.5）。

### 2.6.5 锁的实战选择：在 DRM 里到底用哪把

光知道"spinlock/mutex/rw_semaphore"不够，要会**按场景选**。看 Xe 的设备/VM 结构里如何混用
（`xe_device_types.h`、`xe_vm_types.h`），体会选择逻辑：

| 选择 | DRM/Xe 真实出处 | 为什么选它 |
|------|------------------|------------|
| **spinlock_t** | `xe_vm_types.h:223/369`、`xe_device_types.h:245/323`；`dma_fence` 的锁 | 临界区**极短**、**不睡眠**、可能在中断/原子上下文取（如保护一个链表/位图的瞬时修改） |
| **struct mutex** | `xe_device_types.h:376/425/440`、`drm_device.master_mutex`/`filelist_mutex`（第 00 章 0.4.1.1） | 临界区可能**较长或会睡眠**（如改设备级配置、遍历文件列表），且只在进程上下文取 |
| **struct rw_semaphore** | `xe_vm_types.h:273/346`、`xe_device_types.h:303` | **读多写少**：大量并发读（如遍历 VM 的映射做提交）、偶尔写（rebind/改地址空间），读锁可并发提升吞吐 |
| **ww_mutex** | `dma_resv`（GEM 缓冲锁）、KMS 对象锁 | 要**任意序锁多个**对象（提交/原子显示），用它防死锁（2.6.1） |

选择三问：① 会在中断/原子上下文取吗？→ 只能 spinlock。② 临界区会睡眠/很长吗？→ mutex。
③ 是不是读多写少、读要并发？→ rw_semaphore。④ 要任意序锁多个对象？→ ww-mutex。

**`xe_vm` 的 `rw_semaphore lock`（`xe_vm_types.h:273`）是个好例子**：一个 VM 的地址映射会被"提交时
遍历（读）"很多次、被"VM-bind 改映射（写）"较少次，用读写信号量让多个提交能并发持读锁，写时才独占
（第 18 章详解）。如果这里用普通 mutex，所有提交会被无谓地串行化——**选锁直接影响并发度与性能**。

### 2.6.6 等待：completion 与 wait_queue

GPU 操作天生异步，CPU 常要"等某件事完成"。两种基础等待原语：

- **`struct completion`**：一次性的"事件已发生"信号。等待方 `wait_for_completion[_timeout]()` 睡眠，
  完成方 `complete()` 唤醒。适合"等一个明确的一次性事件"（如等一次复位完成、等固件就绪）。
- **`wait_queue_head_t` + `wait_event*()`**：更通用的条件等待——等待方睡在队列上直到某条件为真，
  改变条件的一方 `wake_up()`。适合"等一个可重复变化的条件"。

`dma_fence_wait()`（第 10 章）内部就建立在这类等待 + fence 的 signal 回调之上：等待者睡眠，GPU 完成
触发 `dma_fence_signal()` 唤醒。**带超时的等待（`_timeout`）在 GPU 驱动里几乎是标配**——因为硬件可能
hang，无限等会挂死调用者，必须超时后走复位/报错路径（第 22 章）。这也是可靠性主题的体现：**永远不要
无超时地等硬件**。

---

## 2.7 量化分析：设施的成本

H&P 精神：每个设施都有成本，选型要算账。

| 设施 | 典型成本量级 | 含义/取舍 |
|------|--------------|-----------|
| 原子操作（kref get/put、refcount） | 数纳秒~数十纳秒（一次原子指令 + 可能的缓存行争用） | 比普通自增贵；高频路径上"无谓的 get/put"会累积，故有"借用"优化（2.6.3） |
| spinlock（无争用） | 数十纳秒 | 争用时更贵且自旋耗 CPU；只护**短**临界区 |
| mutex（无争用） | 类似，但争用时**睡眠**让出 CPU | 临界区可长/可睡眠时用 |
| ww-mutex 回退一次 | = 解锁已持 + 重锁（与对象数成正比） | 争用越多回退越多；多数提交一轮成功，回退是少数路径 |
| RCU 读侧 | 接近零（禁抢占级别） | 这正是 dma_fence 等"读极多"对象用它的原因 |
| RCU 释放延迟 | 一个 grace period（毫秒级，异步） | 释放不即时，换读侧零成本——典型空间/延迟换读吞吐 |
| workqueue 调度一个 work | 微秒级唤醒延迟 | 把活从中断挪到可睡眠上下文的代价 |
| dma_map（流式，有 IOMMU） | 取决于 IOMMU 映射开销 | 大缓冲分摊小；频繁小映射昂贵，故倾向批量/长期映射 |

**两条可操作结论**：

1. **读极多的对象用 RCU、不用锁**：dma_fence 状态查询若每次上锁，在高并发提交下会成为瓶颈；RCU 读侧
   近零成本是关键设计（2.6.2）。
2. **高频路径慎用引用计数与锁**：能"借用"就不 get/put（2.6.3），能持一把粗锁批量做就不要反复细锁——
   这解释了 `drm_exec` 一次锁一批、批量加 fence 的设计（2.5.1）。

---

## 2.8 硬件视角：DMA 设施如何对应第 01 章的硬件通路

本章的 DMA 设施不是抽象 API，它直接对应第 01 章的硬件 DMA 通路：

| 内核设施 | 对应的硬件事实（第 01 章） |
|----------|---------------------------|
| `pci_set_master` | 置位配置空间 Bus Master Enable，GPU 才能发起 DMA（1.3.4） |
| `dma_set_mask(64)` | 声明 GPU 能寻址的 DMA 地址位宽，决定是否需 bounce buffer（1.6.5） |
| `dma_map_sgtable` → `dma_addr_t` | 经 IOMMU 把 CPU 物理地址翻译成 GPU 能用的设备地址（1.6.5） |
| 一致性 vs 流式 + sync | 对应 GPU↔CPU 缓存一致性管理（1.6.3/1.6.6） |
| threaded IRQ / workqueue | 把 MSI-X 中断（1.5.4）的重活挪到可睡眠下半部 |

**所以**：第 01 章讲"硬件能做 DMA、要翻译地址、要管一致性"，本章讲"内核用哪些 API 把这些落地"。
两章合起来，你才完整理解"一块系统内存缓冲如何安全地交给 GPU 读"。第 11、18 章会把这条链走到底。

---

## 2.9 综合实例（Lab）：用 ftrace 跟踪一次 ioctl 的函数与锁

把本章设施在一次真实调用里"看"出来。目标：跟踪一次简单 ioctl（如 vkms 上的 `DRM_IOCTL_MODE_ATOMIC`
或更简单的 `GET_CAP`），观察它经过的函数与锁。

### 2.9.1 用 function_graph 看调用与锁

```
# 1. 加载 vkms（第 00 章）
$ sudo modprobe vkms
# 2. 用 trace-cmd 跟踪一次 modetest 的 atomic 提交, 限定 drm/mutex 相关函数
$ sudo trace-cmd record -p function_graph \
    -l 'drm_*' -l 'drm_modeset_*' -l 'ww_mutex_*' -l 'mutex_*' \
    modetest -M vkms -s <conn>@<crtc>:1024x768
$ trace-cmd report | less
```

你会在调用图里看到：`drm_ioctl` → atomic 入口 → `drm_modeset_lock`/`ww_mutex_lock`（2.5.2）→ 驱动
`atomic_check`/`commit`。**亲眼确认"一次显示 ioctl 要拿一串 ww-mutex"**。

### 2.9.2 用 lockdep 验证锁序正确

若内核开了 `CONFIG_PROVE_LOCKING`（第 00 章 0.9.6），lockdep 会在后台验证所有锁的获取顺序。正常时
无声；一旦出现潜在死锁/错误锁序，dmesg 立刻打出告警（见 2.11）。跑上面的 trace 时若 dmesg 干净，
说明这条路径锁序健康。

### 2.9.3 用 tracepoint 看引用/fence（可选）

```
$ echo 1 > /sys/kernel/tracing/events/dma_fence/enable   # 第 00 章 0.11 提过
# 跑点负载, 观察 fence 的 init/signal/wait —— 对应 2.6.2 的 RCU 保护对象
```

> 这个 Lab 把本章三大设施（ww-mutex 加锁、lockdep 校验、RCU 保护的 fence）在一次真实操作里串了起来。
> 完整步骤与排错见[附录 A](./appendix-a-labs.md)。

---

## 2.10 横向关联

| 本章设施 | 在哪深用 |
|----------|----------|
| pci_driver / probe / remove | 第 17 章 Xe 设备初始化 |
| drmm_/devm_ 托管 | 全书所有驱动初始化 |
| ww-mutex / drm_modeset_lock | 第 06 章 原子显示加锁 |
| ww-mutex / drm_exec | 第 12 章 drm_exec、第 18 章 VM-bind、第 19 章 exec |
| kref 引用计数 | 第 09 章 GEM、第 10 章 dma-buf |
| RCU/SRCU | 第 03 章 drm_dev_enter、第 10 章 dma_fence |
| dma-mapping/sg_table | 第 11 章 TTM 页、第 18 章 VM-bind |
| workqueue/threaded IRQ | 第 04 章 vblank work、第 06 章 commit work、第 12 章 scheduler |

**跨切面主题**：可靠性（托管资源防泄漏、lockdep 防死锁）；性能（RCU 读侧零成本、借用避免原子开销）；
保护与隔离（IOMMU 限制 DMA 范围）；编程模型（drm_exec 把复杂回退变成声明式循环）。

---

## 2.11 谬误与陷阱 + 调试

### 谬误与陷阱

- **谬误 1：「多对象加锁，自己规定个固定顺序就不会死锁」。**
  GPU 提交的对象集合运行时才确定、无法预先全局排序。这正是 ww-mutex 存在的理由（2.6.1）。硬定序在
  动态对象集上要么做不到，要么严重限制并发。

- **谬误 2：「收到 `-EDEADLK` 是出错了」。**
  不是错误，是 ww-mutex 的**正常信号**，告诉你"回退重来"。漏处理 `-EDEADLK`（不回退就继续）才是 bug。
  用 `drm_exec`（2.5.1）能避免手写漏掉。

- **谬误 3：「kref 是原子的，所以引用计数不会出错」。**
  kref 保证**计数操作**线程安全，但"该 get 没 get / 该 put 没 put / 借用当拥有"是**逻辑**错误，kref
  管不了。漏 put → 泄漏；多 put → use-after-free（2.6.3）。

- **谬误 4：「RCU 读临界区里可以睡眠」。**
  普通 RCU 读临界区**不能睡眠**（会破坏 grace period 语义）。要睡眠用 **SRCU**。

- **谬误 5：「把 CPU 物理地址直接给 GPU 做 DMA」。**
  有 IOMMU 时设备地址 ≠ CPU 物理地址，必须经 DMA API 拿 `dma_addr_t`（2.4.5、2.8）。

- **陷阱 6：在中断/原子上下文里用了会睡眠的锁（mutex/ww_mutex）。**
  原子上下文只能用 spinlock 等非睡眠原语。在中断处理里需要重活/睡眠锁 → 挪到 threaded IRQ 或
  workqueue（2.6/延迟执行）。

- **陷阱 7：`drmm_`/`devm_` 挂错对象生命周期。**
  `drmm_` 跟 `drm_device`、`devm_` 跟 `struct device`。挂错会导致释放时机不对（过早/过晚）。

### lockdep 死锁告警怎么读

开了 `PROVE_LOCKING`，潜在死锁会在 dmesg 打出类似：

```
======================================================
WARNING: possible circular locking dependency detected
------------------------------------------------------
   ... CPU0 持有 lockA, 想要 lockB
   ... CPU1 持有 lockB, 想要 lockA
   possible unsafe locking scenario: ... *** DEADLOCK ***
```

读法：找出告警里两条相反的"持有 X 求 Y"链，那就是潜在的 AB-BA。修复通常是统一锁序，或——对动态
对象集——改用 ww-mutex/drm_exec。**lockdep 在第一次出现危险顺序时就报，哪怕实际还没死锁**，是开发期
最有价值的设施之一（第 00 章 0.9.6 建议常开）。

### 调试小抄

| 想知道 | 手段 |
|--------|------|
| 一次 ioctl 的函数/锁路径 | `trace-cmd record -p function_graph -l 'drm_*' -l 'ww_mutex_*'`（2.9.1） |
| 是否有锁序问题 | 开 `CONFIG_PROVE_LOCKING`，看 dmesg lockdep 告警 |
| use-after-free / 越界 | 开 `CONFIG_KASAN`，崩溃栈直指出错点 |
| 引用计数泄漏 | kmemleak、或在 free 回调打点统计 |
| fence/RCU 行为 | tracepoint `dma_fence`（2.9.3） |
| 谁持有某把锁 | lockdep 的 held-locks 列表（崩溃/告警时打印） |

---

## 2.12 随堂练习（Practice Problems）

**练习 2.1** `drmm_*` 与 `devm_*` 分别把资源挂在谁的生命周期上？各举一个 DRM 中的用途。

**练习 2.2** 为什么 GPU 命令提交"自己规定固定加锁顺序"行不通？ww-mutex 用什么机制避免死锁？

**练习 2.3** 收到 `ww_mutex_lock` 返回 `-EDEADLK` 时正确的做法是什么？`drm_exec` 里哪一句替你做了这件事？

**练习 2.4** `kref_put(&obj->refcount, release)` 什么时候才真正调用 `release`？"借用"与"拥有"引用的
区别是什么？

**练习 2.5** dma_fence 为什么用 `kfree_rcu` 释放？这对"查询 fence 状态"的性能有什么好处？

**练习 2.6** 一致性映射（`dma_alloc_coherent`）与流式映射（`dma_map_sgtable`）各适合什么场景？

**练习 2.7（量化）** 已知 RCU 读侧近零成本、而每次原子 get/put 有数十纳秒开销。对一个"每秒被查询
百万次状态、极少销毁"的对象（如 fence），用 RCU 读 vs 用 refcount/锁保护读，定性比较开销，说明为何
选 RCU。

### 随堂练习即时解答

**解 2.1** `drmm_` 挂在 `struct drm_device`（如 `drmm_mode_config_init`、`drmm_encoder_init`）；
`devm_` 挂在 `struct device`（如 BAR 映射的 `devm_add_action_or_reset(mmio_fini)`、IRQ）。两者都在
对象销毁时**逆序**自动执行登记的释放动作（2.4.2、2.5.4）。

**解 2.2** 提交的对象集合运行时才确定、无法预先全局排序，硬定序做不到（2.6.1 谬误 1）。ww-mutex 用
ticket（事务时间戳）定资历 + wound-wait（新者让路）+ 回退后先锁争用对象，保证无死锁、无饥饿。

**解 2.3** 回退：解锁已持有的全部，然后下一轮先锁那把争用的、再重锁其余。`drm_exec` 里
`drm_exec_retry_on_contention(&exec)` 替你做了回退并跳回 `drm_exec_until_all_locked` 循环开头
（2.5.1、2.5.2）。

**解 2.4** 当计数**减到 0**（最后一个引用被 put）时才调 `release`。"拥有"=你 get 过、负责 put；
"借用"=他人保证使用期内对象不死，你不 get/put。混淆会泄漏或 UAF（2.6.3）。

**解 2.5** fence 被大量并发查询状态、极少销毁。`kfree_rcu` 让释放延迟到所有读者退出后，于是查询侧
可在 `rcu_read_lock()` 下无锁读，避免为每次查询上锁——读侧近零成本（2.6.2、2.7）。

**解 2.6** 一致性映射用于长期、CPU 与设备都要一致访问的结构（描述符环、固件通信区）；流式映射用于
一次性 DMA 传输（把一块缓冲临时交给 GPU 读），用完 unmap，并在交接时做缓存同步（2.6.4）。

**解 2.7** RCU 读：每次查询近零成本（禁抢占级别），百万次/秒几乎不增负担；refcount/锁读：每次查询
数十纳秒原子操作 + 可能的缓存行争用，百万次/秒累积成可观开销且在多核上争用加剧。故"读极多、写极少"
选 RCU（2.6.2、2.7）。

---

## 2.13 课后练习题

> 仅题目。答案见 [`answers/02-kernel-infra-answers.md`](./answers/02-kernel-infra-answers.md)。★ 为动手/综合题。

### L1 概念辨析

**2.13.1** 描述 Linux"总线—设备—驱动"模型三个角色及匹配/probe 流程；DRM 设备为何必须挂在某个
`struct device` 上（联系第 00 章 vkms 的 faux 设备）。

**2.13.2** 解释 `drmm_add_action_or_reset` 的"or_reset"语义，以及它解决了手写 goto 清理的什么问题。

**2.13.3** 比较 spinlock / mutex / rw_semaphore 的适用场景与关键约束（可否睡眠、读写并发）。

**2.13.4** 用自己的话解释 ww-mutex 的 wound-wait 规则；"ticket"和"先锁争用对象"各起什么作用。

**2.13.5** kref 的 get/put/release 三操作语义；为什么说 kref 保证计数安全但不保证逻辑配对正确。

**2.13.6** RCU 与 SRCU 的区别是什么？普通 RCU 读临界区有什么禁忌？

### L2 数据结构与源码阅读

**2.13.7** 阅读 `include/drm/drm_managed.h` 的 `drmm_add_action`/`_or_reset` 宏，说明 `#action` 参数
（动作名字符串）有什么调试价值。

**2.13.8** 阅读 `drm_exec.c` 头部 DOC 的用法范式，逐句解释 `drm_exec_until_all_locked` /
`drm_exec_prepare_obj` / `drm_exec_retry_on_contention` / `drm_exec_for_each_locked_object` 各做什么。

**2.13.9** 阅读 `drm_exec_unlock_all`（`drm_exec.c`），说明它为什么**逆序**解锁、并对每个对象
`drm_gem_object_put`——这与 2.5.3 的引用计数有何关系？

**2.13.10** 阅读 `include/drm/drm_gem.h:555/562` 的 `drm_gem_object_get/put`，指出哪个会触发
`drm_gem_object_free`，以及触发条件。

**2.13.11 ★** 阅读 `drm_modeset_lock.c` 头部 DOC 的 `retry:`/`-EDEADLK`/`drm_modeset_backoff` 写法，
和 `drm_exec` 的封装做对比，论证"两者是同一 ww-mutex 算法的不同封装"。

### L3 机制分析

**2.13.12** 用 boA/boB 两个对象、T1/T2 两个事务，画出 AB-BA 死锁场景，再画出 ww-mutex 如何用
wound-wait + 回退把它解开（标注谁 wound、谁回退、回退后先锁谁）。

**2.13.13** 解释 dma_fence 的"无锁查询 + kfree_rcu 释放"如何协作：查询者如何安全地拿到一个可能正在
被释放的 fence？（提示：`rcu_read_lock` 与 grace period）

**2.13.14** 给定一段"在 MSI-X 中断处理里需要拿 mutex 并睡眠等待"的需求，说明为什么不能直接在硬中断
上下文做，应改用什么（threaded IRQ / workqueue），并说明数据如何从上半部传到下半部。

### L4 设计与对比

**2.13.15** drm_exec（声明式循环）vs 裸 ww-mutex `retry:` 写法（2.5.2）：从"易写对、易读、可复用"
角度论述为什么社区要封装 drm_exec。

**2.13.16** 一致性映射 vs 流式映射：从"生命周期、缓存同步成本、IOMMU 占用"三方面对比，并说明 Xe 给
BO 系统内存页选流式（`dma_map_sgtable`）的理由。

**2.13.17** "借用 vs 拥有"引用模型：论述在高频提交路径上尽量"借用"（不 get/put）的性能动机，以及它
带来的正确性风险与规避手段。

### L5 动手 / 调试

**2.13.18 ★** 完成 2.9 的 Lab：用 `trace-cmd -p function_graph` 跟踪一次 `modetest -M vkms -s` 的
atomic 提交，在调用图里找出 `ww_mutex_lock`/`drm_modeset_lock` 出现的位置，截取片段并说明它在锁哪些
对象。

**2.13.19 ★** 在开了 `CONFIG_PROVE_LOCKING` 的内核上跑上述操作，确认 dmesg 无 lockdep 告警；再查阅
一条公开的 DRM lockdep 告警样例（或构造理解），按 2.11"如何读"分析其 AB-BA 链。

**2.13.20 ★** 启用 `dma_fence` tracepoint，跑一点负载，观察 fence 的 init/enable_signal/signal 事件
序列，结合 2.6.2 解释哪些是"读侧无锁查询"不会产生事件、哪些是状态变更才有事件。

**2.13.21 ★（调试）** 设想一个引用计数 bug：某路径 `drm_gem_object_lookup` 拿到引用后在错误分支
忘了 `_put`。描述你会用什么手段（kmemleak / 读码 / KASAN）定位这种泄漏，以及修复原则。

### 综合大题

**2.13.22 ★（综合）** 以"一次 GPU 命令提交要锁住 boA、boB、boC 三个缓冲、加 fence、再提交"为例，
用本章设施完整叙述：drm_exec 如何加锁（含一次争用回退）、kref 如何在加锁期间保护对象、fence 如何
加到 dma_resv、最后如何解锁释放。贯通 2.5/2.6，并预接第 12、18、19 章。

**2.13.23 ★（综合·量化）** 一个提交热路径每次要 get/put 8 个对象、查询 16 个 fence 状态。若每次原子
操作约 20ns、每次加锁约 40ns、RCU 读约 2ns：① 估算"全用 refcount+锁"与"对象借用 + fence 用 RCU 读"
两种方案在该路径上的同步开销差异；② 据此论证 2.7 的两条结论。

**2.13.24 ★（综合）** 把本章的设施按"第 01 章硬件通路"重新组织：哪些设施服务于 DMA 通路、哪些服务于
中断通路、哪些服务于并发共享。画一张"硬件需求 → 内核设施"的对应图，作为进入第 03 章前的收口。

---

## 2.14 本章小结

- DRM 是**重度复用内核设施**的子系统；它的很多"绕"写法，是并发共享场景下"既正确又高效"的必然。
- **设备/驱动模型 + 托管资源**：`pci_driver`/probe/remove 是设备入口；`drmm_`/`devm_` 用动作链表
  逆序自动释放，消除漏释放整类 bug。
- **ww-mutex 是重点**：为"多对象任意序加锁"而生，用 ticket + wound-wait + 回退避免死锁；`drm_exec`
  把它封装成声明式循环（init→until_all_locked{prepare;retry}→for_each_locked→fini）。`-EDEADLK`
  是正常信号不是错误。
- **kref**：原子引用计数，最后一个 put 触发 release；逻辑上要分清"借用 vs 拥有"。
- **RCU/SRCU**：读侧无锁 + 延迟释放，dma_fence 用 `kfree_rcu` 实现高并发无锁状态查询。
- **DMA 设施**：`dma_addr_t`/sg_table/一致性 vs 流式映射，是第 01 章硬件 DMA 通路的内核侧落地。
- **延迟执行**：threaded IRQ/workqueue 把中断重活挪到可睡眠下半部。
- **调试**：lockdep 查锁序、KASAN 查 UAF、ftrace 看函数/锁路径——开发期常开。

### 知识点清单（自检）

- [ ] 总线-设备-驱动模型、probe/remove、drmm_/devm_ 托管与逆序释放。
- [ ] ww-mutex 的 wound-wait 算法、`-EDEADLK` 回退范式、drm_exec 封装。
- [ ] kref get/put/release 与借用 vs 拥有。
- [ ] RCU 读侧无锁 + 延迟释放、SRCU 可睡眠、dma_fence 的 kfree_rcu。
- [ ] dma_addr_t/sg_table、一致性 vs 流式映射，与硬件 DMA 通路的对应。
- [ ] 用 ftrace 看锁路径、读 lockdep 告警。

### 延伸阅读

- 源码：`drivers/gpu/drm/drm_exec.c`（多对象加锁范式）、`drm_modeset_lock.c`（裸 ww-mutex）、
  `include/drm/drm_managed.h`、`include/drm/drm_gem.h`（kref）、`include/linux/dma-fence.h`（RCU）。
- 内核权威文档（强烈建议各读一遍）：`Documentation/locking/ww-mutex-design.rst`（ww-mutex 算法）、
  `Documentation/RCU/`（RCU 全套）、`Documentation/core-api/dma-api.rst`（DMA 一致性/流式）、
  `Documentation/driver-api/driver-model/`（设备驱动模型）、`Documentation/core-api/refcount-vs-atomic.rst`。

> **下一章预告**：第 03 章《DRM 设备模型与 ioctl》。设施齐备，正式进入 DRM 框架——逐字段拆解
> `drm_device`/`drm_driver`/`drm_file`/`drm_minor`，精读设备注册、字符设备创建、ioctl 分发与鉴权。
> 本章的托管资源、kref、锁会在那里处处现身。
