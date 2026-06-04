# Linux CFS / EEVDF 调度器工作原理深度分析

> 分析对象：Linux 6.18.5 内核源码树（本仓库）
> 主线：以当前代码实际实现的 **EEVDF**（Earliest Eligible Virtual Deadline First）为主，
> 对照其前身 **经典 CFS**（Completely Fair Scheduler）的设计演进。
> 所有代码引用均采用 `文件:行号` 形式，可在仓库中点击跳转。

---

## 目录

- [第 0 章 导读：到底是 CFS 还是 EEVDF？](#第-0-章-导读到底是-cfs-还是-eevdf)
- [第 1 章 核心数据结构](#第-1-章-核心数据结构)
- [第 2 章 EEVDF 核心算法（对照经典 CFS）](#第-2-章-eevdf-核心算法对照经典-cfs)
- [第 3 章 负载均衡与 SMP](#第-3-章-负载均衡与-smp)
- [第 4 章 组调度与带宽控制](#第-4-章-组调度与带宽控制)
- [第 5 章 PELT 与可观测 / 调优](#第-5-章-pelt-与可观测--调优)
- [第 6 章 端到端实例串讲](#第-6-章-端到端实例串讲)
- [第 7 章 调度核心与高级机制源码走读](#第-7-章-调度核心与高级机制源码走读)
- [第 8 章 数值实例：手算几轮 EEVDF 调度](#第-8-章-数值实例手算几轮-eevdf-调度)
- [第 9 章 机制生命周期与配套子系统补遗](#第-9-章-机制生命周期与配套子系统补遗)
- [附录 A 常见误区与排查](#附录-a-常见误区与排查)
- [附录 B 实测命令速查](#附录-b-实测命令速查)
- [附录 C 术语表](#附录-c-术语表)
- [附录 D EEVDF 数学符号与公式速查](#附录-d-eevdf-数学符号与公式速查)
- [附录 E 关键函数速查索引](#附录-e-关键函数速查索引)

---

## 第 0 章 导读：到底是 CFS 还是 EEVDF？

### 0.1 一个容易混淆的事实

如果你打开本仓库的 `kernel/sched/fair.c`，会看到满屏的 `cfs_rq`、`CONFIG_FAIR_GROUP_SCHED`、
`sched_cfs_bandwidth_slice_us` 等以 `cfs`/`fair` 命名的符号，文件头注释也仍写着
"CFS operations on generic schedulable entities"（见 `kernel/sched/fair.c:307`）。
但**真正决定"下一个该跑谁"的算法早已不是经典 CFS 了**。

内核自带文档把这件事说得很清楚（`Documentation/scheduler/sched-eevdf.rst:5`）：

```text
The "Earliest Eligible Virtual Deadline First" (EEVDF) was first introduced
in a scientific publication in 1995 [1]. The Linux kernel began
transitioning to EEVDF in version 6.6 (as a new option in 2024), moving
away from the earlier Completely Fair Scheduler (CFS) ...
```

换句话说：

- **2.6.23 ~ 6.5**：公平调度类 `fair_sched_class` 的核心是 **经典 CFS**——
  "永远挑 `vruntime` 最小的任务"。
- **6.6 起**：核心被替换为 **EEVDF**——"在所有 *eligible*（lag ≥ 0）的任务里，挑 *virtual deadline* 最早的"。
- **命名没改**：为了兼容用户态接口（cgroup 的 `cpu.max`/`cpu.weight`、`/sys/kernel/debug/sched/` 等）、
  减少改动面，文件名、结构体名、sysctl 名仍沿用 `cfs`/`fair`。

所以当有人说"分析 CFS 调度"，在 6.6+ 内核上**实质上就是分析 EEVDF**。本文统一这样处理：
讲清 EEVDF 的实现，并在每个关键点标注它与经典 CFS 的差异。

### 0.2 经典 CFS 一句话回顾

经典 CFS 的设计哲学在 `Documentation/scheduler/sched-design-CFS.rst:18` 被浓缩成一句话：

```text
80% of CFS's design can be summed up in a single sentence: CFS basically models
an "ideal, precise multi-tasking CPU" on real hardware.
```

"理想多任务 CPU" 指一个能让 N 个任务**真正并行、每个各跑 1/N 速度**的虚构硬件。
现实硬件一次只能跑一个任务，于是 CFS 引入 **虚拟运行时间 `vruntime`**：
- 每个任务记录自己已获得的、按权重归一化后的 CPU 时间；
- 调度器**永远挑 `vruntime` 最小的任务**（即"到目前为止跑得最少的"）；
- 用一棵按 `vruntime` 排序的红黑树维护就绪队列，取最左节点即最小 `vruntime`。

这套机制简单、无启发式、抗攻击，但有一个**结构性短板**：它只保证"长期公平"
（累计 CPU 时间趋于均等），却**不直接提供延迟保证**。一个对延迟敏感的小任务，
和一个 CPU 密集的大任务，在 CFS 眼里只要 `vruntime` 接近就同等对待——无法表达
"我只想跑很短一段，但希望尽快被调度上来"这种需求。

### 0.3 EEVDF 解决了什么

EEVDF 在保留"长期公平"的同时，额外引入**延迟维度**。核心概念三件套
（`Documentation/scheduler/sched-eevdf.rst:13` 概述，下文逐一深入）：

| 概念 | 含义 | 经典 CFS 有无对应 |
| --- | --- | --- |
| **lag（滞后）** | 任务"应得"与"已得" CPU 时间之差。lag > 0 表示系统欠它时间，lag < 0 表示它超额了 | CFS 用 `vruntime` 隐含表达，但不显式跟踪 |
| **eligible（合格）** | 仅当 lag ≥ 0（没超额）时，任务才有资格被选中 | CFS 无此门槛 |
| **virtual deadline / VD（虚拟截止时间）** | 由任务请求的时间片 `slice` 推导出的"应当何时跑完这一份"的虚拟时刻 | CFS 无显式 deadline |

选择规则一句话：**在所有 eligible 的任务里，挑 VD 最早的那个**。

它带来的关键能力（`Documentation/scheduler/sched-eevdf.rst:21`）：

```text
... this allows latency-sensitive tasks with shorter time slices to be
prioritized, which helps with their responsiveness.
```

一个请求**更短时间片**的任务，会算出**更早的 VD**，从而在公平的前提下被优先调度——
延迟敏感型负载的响应性因此显著改善。而且，任务可以通过 `sched_setattr(2)` 的
`sched_runtime` 字段**主动请求自定义时间片**（见 `kernel/sched/fair.c:5364` 的
`__setparam_fair()`），这是经典 CFS 完全没有的表达力。

### 0.4 本文的分析路线

```text
第 0 章  概念定位（你正在读）
   │
第 1 章  数据结构 —— 先认识"棋盘和棋子"
   │      sched_entity / cfs_rq / rq
   │
第 2 章  核心算法 —— "怎么走子"（本文重心）
   │      vruntime 记账 → lag/eligible/deadline → pick_eevdf → 入队出队 → 抢占
   │      并贯穿主调度链 schedule() → __schedule() → pick_next_task_fair()
   │
第 3 章  负载均衡 —— 多 CPU 间怎么搬任务
   │
第 4 章  组调度 / 带宽 —— cgroup 层级与 cpu.max 限流
   │
第 5 章  PELT / 可观测 —— 负载怎么量化、怎么观测与调优
   │
第 6 章  端到端串讲 —— 把一个任务的一生走一遍
```

---

## 第 1 章 核心数据结构

理解调度器前，必须先认识三个核心结构体：**调度实体 `sched_entity`**（棋子）、
**CFS 运行队列 `cfs_rq`**（棋盘）、**CPU 运行队列 `rq`**（棋桌）。三者的包含关系是：

```text
  每个 CPU 一个 struct rq
        │
        ├── struct cfs_rq cfs;          ← 该 CPU 的公平调度就绪队列
        │       │
        │       ├── struct rb_root_cached tasks_timeline;   ← 按 deadline 排序的增广红黑树
        │       │         │
        │       │         └── 节点是 struct sched_entity.run_node
        │       │
        │       ├── struct sched_entity *curr;   ← 当前在跑的实体（注意：不在树里）
        │       └── struct sched_entity *next;   ← buddy 候选
        │
        ├── struct task_struct *curr;   ← 当前在跑的任务
        └── struct task_struct *donor;  ← 调度上下文任务（proxy-exec）
```

> **关键直觉**：红黑树里装的是 `sched_entity`，不是 `task_struct`。
> 一个 `sched_entity` 既可以是一个普通任务，也可以是一个**任务组**（group entity）——
> 这正是组调度能层层嵌套的根基（详见第 4 章）。

### 1.1 struct sched_entity —— 调度实体

定义见 `include/linux/sched.h:575`。这是 EEVDF 所有计算的载体，逐字段拆解：

```c
struct sched_entity {
	/* For load-balancing: */
	struct load_weight		load;          // 权重（由 nice 值映射而来）
	struct rb_node			run_node;      // 红黑树节点，挂入 cfs_rq->tasks_timeline
	u64				deadline;      // ★ EEVDF 虚拟截止时间 vd_i
	u64				min_vruntime;  // ★ 增广字段：本子树最小 vruntime（用于剪枝）
	u64				min_slice;     // 增广字段：本子树最小 slice
	u64				max_slice;     // 增广字段：本子树最大 slice

	struct list_head		group_node;
	unsigned char			on_rq;         // 是否在就绪队列上
	unsigned char			sched_delayed; // ★ 是否处于"延迟出队"状态（DELAY_DEQUEUE）
	unsigned char			rel_deadline;  // deadline 当前是否以相对形式保存（迁移用）
	unsigned char			custom_slice;  // 是否用户自定义了 slice

	u64				exec_start;          // 本次上 CPU 的起始时刻
	u64				sum_exec_runtime;    // 累计真实运行时间
	u64				prev_sum_exec_runtime;
	u64				vruntime;            // ★ 虚拟运行时间 v_i（EEVDF 与 CFS 共有）
	/* Approximated virtual lag: */
	s64				vlag;                // ★ 近似虚拟 lag = V - v_i（出队时算好缓存）
	/* 'Protected' deadline, to give out minimum quantums: */
	u64				vprot;               // ★ 受保护截止时间（RUN_TO_PARITY 用）
	u64				slice;               // ★ 请求时间片 r_i（默认 base_slice，可自定义）

	u64				nr_migrations;

#ifdef CONFIG_FAIR_GROUP_SCHED
	int				depth;         // 在组层级中的深度（root=0）
	struct sched_entity		*parent;   // 父实体（上一层组）
	struct cfs_rq			*cfs_rq;   // 本实体被排队到的 cfs_rq
	struct cfs_rq			*my_q;     // 本实体（若是组）自己拥有的 cfs_rq
	unsigned long			runnable_weight;
#endif
	/* 独立 cache line，避免与上面的读多写少字段争用 */
	struct sched_avg		avg;       // ★ PELT 负载跟踪（第 5 章）
};
```

带 ★ 的字段是 EEVDF 的灵魂，必须吃透它们的语义：

- **`vruntime`（v_i）**：虚拟运行时间。任务每跑 `delta` 纳秒真实时间，`vruntime` 增加
  `delta * NICE_0_LOAD / weight`（权重越大涨得越慢）。这是经典 CFS 就有的概念，EEVDF 继承。

- **`vlag`（vl_i）**：近似虚拟 lag，定义 `vl_i = V - v_i`（V 是队列加权平均虚拟时间）。
  - lag > 0：任务"虚拟时间"落后于平均，系统**欠它** CPU 时间 → eligible。
  - lag < 0：任务超额消费了，**暂时不 eligible**。
  - 注意这是**出队时计算并缓存**的近似值（见 `update_entity_lag()`，第 2 章详述），
    用于跨睡眠/唤醒周期保留 lag。

- **`deadline`（vd_i）**：虚拟截止时间，公式 `vd_i = ve_i + r_i / w_i`
  （`ve_i` 是 eligible 起点的虚拟时间，`r_i` 是 slice，`w_i` 是权重）。
  代码里直接实现为 `se->deadline = se->vruntime + calc_delta_fair(se->slice, se)`
  （见 `kernel/sched/fair.c:1254`）。**红黑树就是按这个字段排序的**。

- **`slice`（r_i）**：任务这一轮"想要"的运行时长。默认等于 `sysctl_sched_base_slice`
  （700µs，见 0.5 节），但可由 `sched_setattr()` 自定义（`custom_slice=1`）。
  **slice 越小 → deadline 越早 → 越容易被优先选中**，这正是延迟敏感任务的调优旋钮。

- **`min_vruntime` / `min_slice` / `max_slice`**：**增广红黑树**的聚合字段。
  红黑树按 `deadline` 排序，但 EEVDF 选择时需要按 `vruntime` 做"堆式剪枝"。
  内核让每个节点额外维护"以我为根的子树中最小的 `vruntime`"，从而在 O(log n) 内
  跳过整棵不可能 eligible 的子树（见 `kernel/sched/fair.c:1008` 的 `min_vruntime_update()`）。

- **`vprot`**：受保护的截止时间。开启 `RUN_TO_PARITY` 特性时，当前任务在到达"0-lag 点"
  之前不应被唤醒抢占，`vprot` 就是这个保护边界（见 `set_protect_slice()`，`kernel/sched/fair.c:1084`）。

- **`sched_delayed`**：延迟出队标志。开启 `DELAY_DEQUEUE` 时，一个睡眠的、lag < 0 的任务
  不会立刻从树上摘除，而是留在树里"烧掉"它的负 lag，避免靠"睡一下重置负 lag"作弊
  （`Documentation/scheduler/sched-eevdf.rst:24` 提到的 decaying 机制）。

#### 1.1.1 权重 load_weight 与 nice 的关系

`struct load_weight` 里有两个字段：`weight`（权重）和 `inv_weight`（权重倒数，定点数，加速除法）。
nice 值到 weight 的映射是一张硬编码表 `sched_prio_to_weight[]`（`kernel/sched/core.c:10569` 附近）：

```c
const int sched_prio_to_weight[40] = {
 /* -20 */     88761,     71755,     56483,     46273,     36291,
 /* -15 */     29154,     23254,     18705,     14949,     11916,
 /* -10 */      9548,      7620,      6100,      4904,      3906,
 /*  -5 */      3121,      2501,      1991,      1586,      1277,
 /*   0 */      1024,       820,       655,       526,       423,
 /*   5 */       335,       272,       215,       172,       137,
 /*  10 */       110,        87,        70,        56,        45,
 /*  15 */        36,        29,        23,        18,        15,
};
```

设计要点（详见 `Documentation/scheduler/sched-nice-design.rst`）：
- **nice 0 = 1024**（`NICE_0_LOAD`，基准权重）。
- **相邻 nice 差约 1.25 倍**：每降 1 个 nice，权重 ×1.25，CPU 份额约提升 10%。
  例如两个任务 nice 分别为 0 和 5：权重比 1024:335 ≈ 3:1，CPU 时间约 75%:25%。
- 配套的 `sched_prio_to_wmult[]` 是各权重的 `2^32 / weight` 预计算值，
  把 `calc_delta_fair()` 里的除法变成乘法 + 移位（见 1.2 / 第 2 章）。

### 1.2 calc_delta_fair —— 真实时间到虚拟时间的换算

`vruntime` 的推进依赖一个核心换算函数 `calc_delta_fair()`（`kernel/sched/fair.c:297`）：

```c
/*
 * delta /= w
 */
static inline u64 calc_delta_fair(u64 delta, struct sched_entity *se)
{
	if (unlikely(se->load.weight != NICE_0_LOAD))
		delta = __calc_delta(delta, NICE_0_LOAD, &se->load);

	return delta;
}
```

语义：把真实运行时间 `delta` 换算成"按 nice-0 归一化"的虚拟增量
`delta_v = delta * NICE_0_LOAD / weight`。

- nice 0 任务（weight=1024=NICE_0_LOAD）：**快路径**，`vruntime` 涨得和真实时间一样快。
- nice < 0（weight 更大）：`vruntime` 涨得**更慢** → 更"赖"在 CPU 上 → 拿到更多时间。
- nice > 0（weight 更小）：`vruntime` 涨得**更快** → 更快被别人超过 → 让出 CPU。

底层 `__calc_delta()`（`kernel/sched/fair.c:261`）用 `inv_weight` 做定点乘法，
并用 `fls()`（find-last-set）动态调整移位量防止溢出——这是纯性能优化，理解时
只需记住它等价于"乘 NICE_0_LOAD 再除以 weight"即可。

> **同一个函数的两种用途**，第 2 章会反复看到：
> 1. 把 `delta_exec` 转成 `vruntime` 增量（记账）；
> 2. 把 `slice` 转成 deadline 的虚拟跨度 `r_i / w_i`（定 deadline）。

### 1.3 struct cfs_rq —— 公平调度运行队列

定义见 `kernel/sched/sched.h:678`。每个 CPU 至少有一个（根 `cfs_rq`），
开启组调度后每个任务组在每个 CPU 上还各有一个。关键字段：

```c
struct cfs_rq {
	struct load_weight	load;        // 队列总权重 = Σ 在队实体的 weight
	unsigned int		nr_queued;   // 本层直接在队的实体数
	unsigned int		h_nr_queued; // 层级累计在队任务数（hierarchical）
	unsigned int		h_nr_runnable;
	unsigned int		h_nr_idle;

	s64			sum_w_vruntime;  // ★ Σ (v_i - v0) * w_i  —— 算 avg_vruntime 用
	u64			sum_weight;      // ★ Σ w_i
	u64			zero_vruntime;   // ★ v0：相对基准点（避免 u64 乘法溢出）
	unsigned int		sum_shift;       // 防溢出时对权重的右移量

	struct rb_root_cached	tasks_timeline;  // ★ 增广红黑树（按 deadline 排序，缓存最左节点）

	struct sched_entity	*curr;   // ★ 当前运行实体（不在树中）
	struct sched_entity	*next;   // buddy 候选

	struct sched_avg	avg;     // 队列级 PELT 负载（第 5 章）
	/* ... removed/tg_load/h_load 等负载传播字段 ... */

#ifdef CONFIG_FAIR_GROUP_SCHED
	struct rq		*rq;     // 所属 CPU 的 rq
	struct task_group	*tg;     // "拥有"本队列的任务组
	int			on_list;
	struct list_head	leaf_cfs_rq_list; // 挂到 rq->leaf_cfs_rq_list，负载均衡遍历用
# ifdef CONFIG_CFS_BANDWIDTH
	int			runtime_enabled;  // ★ 是否启用带宽限流
	s64			runtime_remaining;// ★ 本周期剩余配额
	u64			throttled_clock;
	bool			throttled:1;      // ★ 是否已被限流
	int			throttle_count;
	struct list_head	throttled_list;
# endif
#endif
};
```

EEVDF 相关的三个聚合量 `sum_w_vruntime` / `sum_weight` / `zero_vruntime`
是理解 `avg_vruntime()` 的关键，第 2 章会专门推导。这里先记住直觉：

- 加权平均虚拟时间 **V = Σ(w_i · v_i) / Σ w_i**。
- 直接用 u64 的 `v_i` 求和会溢出，所以代码改用相对量：
  以 `zero_vruntime`（v0）为基准，只累加 `(v_i - v0) · w_i`（即 `sum_w_vruntime`），
  最后 `V = sum_w_vruntime / sum_weight + v0`。

### 1.4 struct rq —— CPU 运行队列

定义见 `kernel/sched/sched.h:1131`。它是"每 CPU 一个"的总调度状态容器，嵌入了
各调度类的子队列。与 CFS 最相关的字段：

```c
struct rq {
	/* ... */
	struct cfs_rq		cfs;     // ★ 公平调度子队列（根）
	struct rt_rq		rt;      // 实时调度子队列
	struct dl_rq		dl;      // deadline 调度子队列

	struct task_struct __rcu *curr;   // ★ 当前真正在 CPU 上运行的任务
	struct task_struct	*donor;   // ★ "调度上下文"任务（proxy-exec：被代理执行时 curr≠donor）

	unsigned int		nr_running;       // 本 rq 可运行任务总数
	u64			clock;            // rq 时钟
	u64			clock_task;       // 扣除中断等的任务时钟（记账基准）
	/* ... 负载均衡、idle、capacity 等大量字段 ... */
};
```

> **proxy-exec 提示**：现代内核引入了 `rq->donor`。普通情况下 `rq->curr == rq->donor`；
> 但在优先级继承等场景下，CPU 可能"替"另一个任务（donor）执行其工作。
> 这就是为什么 `update_curr()` 注释（`kernel/sched/fair.c:1409`）特别强调
> "`cfs_rq->curr` 对应被选中的任务（donor.se），未必是真正在跑的 `rq->curr.se`，极易混淆"。
> 初学时可先忽略 proxy-exec，按 `curr == donor` 理解；本文除特别说明外亦如此。

### 1.5 调度类框架：fair_sched_class

EEVDF/CFS 只是内核多个**调度类**之一。调度核心通过 `struct sched_class` 的一组
函数指针（hooks）与具体调度类解耦（设计见 `Documentation/scheduler/sched-design-CFS.rst:151`）。
公平调度类的实例定义在 `kernel/sched/fair.c:14207` 附近：

```c
DEFINE_SCHED_CLASS(fair) = {
	.enqueue_task		= enqueue_task_fair,    // 任务变为可运行 → 入队
	.dequeue_task		= dequeue_task_fair,    // 任务不再可运行 → 出队
	.yield_task		= yield_task_fair,
	.wakeup_preempt		= check_preempt_wakeup_fair, // 唤醒时是否抢占当前
	.pick_task		= pick_task_fair,       // 只挑实体，不做副作用
	.pick_next_task		= pick_next_task_fair,  // 挑下一个 + 上下文准备
	.put_prev_task		= put_prev_task_fair,   // 把被换下的任务放回树
	.set_next_task		= set_next_task_fair,   // 设定新任务为 curr
	.task_tick		= task_tick_fair,       // 周期 tick 驱动抢占
	.task_fork		= task_fork_fair,
	.update_curr		= update_curr_fair,     // 记账
	/* ... 负载均衡、组调度相关 hooks ... */
};
```

调度类之间有**严格优先级**（高 → 低）：`stop → dl（deadline）→ rt（实时）→ fair（CFS/EEVDF）→ idle`。
调度核心从高到低遍历，第一个能给出任务的类胜出。所以 EEVDF 只在没有
更高优先级（实时 / deadline）任务可跑时才起作用——它服务的是 `SCHED_NORMAL`、
`SCHED_BATCH`、`SCHED_IDLE` 这三种普通策略（`Documentation/scheduler/sched-design-CFS.rst:126`）。

各 hook 的语义（摘自 CFS 文档第 6 节，已对照本仓库实现）：

| hook | 触发时机 | 公平类实现 | 位置 |
| --- | --- | --- | --- |
| `enqueue_task` | 任务进入可运行态 | `enqueue_task_fair` | fair.c（递归调用 `enqueue_entity`，5523） |
| `dequeue_task` | 任务离开可运行态 | `dequeue_task_fair` | fair.c（递归调用 `dequeue_entity`，5647） |
| `wakeup_preempt` | 有任务被唤醒 | `check_preempt_wakeup_fair` | fair.c（见第 2 章） |
| `pick_next_task` | 需要挑下一个 | `pick_next_task_fair` | `kernel/sched/fair.c:9234` |
| `set_next_task` | 任务切换类/组或被选中 | `set_next_task_fair` | fair.c:13890 |
| `task_tick` | 周期时钟中断 | `task_tick_fair` | `kernel/sched/fair.c:13687` |
| `update_curr` | 记账当前运行时间 | `update_curr_fair` | `kernel/sched/fair.c:1455` |

> 第 2 章会沿着 `schedule() → __schedule() → pick_next_task → pick_next_task_fair →
> pick_eevdf` 这条主线，把这些 hook 在真实调度流程里的调用次序串起来。

### 1.6 一个关键常量：base_slice

最后补一个贯穿全文的常量。基础时间片定义在 `kernel/sched/fair.c:79` 附近：

```c
/*
 * Minimal preemption granularity for CPU-bound tasks:
 */
unsigned int sysctl_sched_base_slice = 700000ULL;   // 700 µs
```

- 这是任务默认的 `slice`（`r_i`），即"没特殊要求时，一份请求多长"。
- 经典 CFS 文档（`sched-design-CFS.rst:101`）称其为**唯一的中心可调旋钮**
  `/sys/kernel/debug/sched/base_slice_ns`：调小 → 偏"桌面/低延迟"，调大 → 偏"服务器/吞吐"。
- 在 EEVDF 下它的作用更直接：`base_slice` 决定默认 deadline 跨度，
  进而决定一个 nice-0 任务多久重算一次 deadline、多久可能被抢占。
- 注意：若 `CONFIG_HZ` 导致 `base_slice_ns < TICK_NSEC`，则该值影响甚微
  （因为抢占检查最细只到一个 tick）。

至此"棋盘和棋子"认识完毕。下一章进入全文重心——EEVDF 的核心算法。

---

## 第 2 章 EEVDF 核心算法（对照经典 CFS）

本章是全文重心。我们按"数据如何流动"的顺序展开：

```text
任务在跑  ──update_curr()──►  vruntime 累加 + 重算 deadline + 判断是否该抢占
   ▲                                    │
   │                                    ▼
出队/睡眠 ◄──dequeue_entity()──  update_entity_lag() 把 lag 算好缓存进 se->vlag
   │                                    │
   ▼                                    ▼
入队/唤醒 ──enqueue_entity()──► place_entity() 用缓存的 lag 复原 vruntime + 定 deadline
                                        │
                                        ▼
该挑谁了  ──pick_eevdf()──►  在 eligible 的实体里挑 deadline 最早的
```

为了讲清这套流程，必须先建立 EEVDF 的数学模型。

### 2.1 数学基础：lag、V、eligible、deadline

源码注释（`kernel/sched/fair.c:622`）给出了完整推导，这里翻译并补充。

#### 2.1.1 lag 守恒

公平调度的核心不变式是 **lag 守恒**：

```text
  Σ lag_i = 0
```

其中第 i 个任务的 lag 定义为"理想应得 - 实际已得"：

```text
  lag_i = S - s_i = w_i * (V - v_i)
```

- `S` 是理想服务时间，`s_i` 是实际服务时间；
- `v_i` 是任务的 `vruntime`，`V` 是其虚拟时间对应物（加权平均虚拟时间）；
- `w_i` 是权重。

直觉：所有任务的 lag 之和恒为 0——有人超额（lag<0），必有人欠额（lag>0），系统总账平衡。

#### 2.1.2 从 lag 守恒解出 V

把 `lag_i = w_i(V - v_i)` 代入 `Σ lag_i = 0`：

```text
  Σ w_i * (V - v_i) = 0
  Σ (w_i*V - w_i*v_i) = 0
  V * Σ w_i = Σ w_i*v_i

         Σ v_i * w_i     Σ v_i * w_i
  V  =  ------------- = -------------
            Σ w_i             W
```

**所以 V 就是所有在队实体 `vruntime` 的加权平均**（W = Σ w_i 是总权重）。

#### 2.1.3 eligible 判定

任务"合格"（eligible）当且仅当它**没有超额消费**，即 lag ≥ 0：

```text
  lag_i ≥ 0  ⟺  w_i*(V - v_i) ≥ 0  ⟺  V ≥ v_i
```

即：**任务的 `vruntime` 不超过加权平均 V，它就是 eligible 的**。
直觉上——跑得比平均少（或持平）的任务才有资格继续，超额的要先"等等"。

#### 2.1.4 virtual deadline

对每个 eligible 任务，按它请求的时间片 `r_i`（=`slice`）算一个虚拟截止时间：

```text
  vd_i = ve_i + r_i / w_i
```

`ve_i` 是它变为 eligible 的虚拟时刻（≈当前 `vruntime`），`r_i / w_i` 是把真实时间片
换算成虚拟跨度（正是 `calc_delta_fair(slice, se)`）。代码实现极简洁
（`kernel/sched/fair.c:1252`）：

```c
	/*
	 * EEVDF: vd_i = ve_i + r_i / w_i
	 */
	se->deadline = se->vruntime + calc_delta_fair(se->slice, se);
```

**EEVDF 选择规则 = 在 eligible 集合里取 vd_i 最小者**。

> **与经典 CFS 的本质差异**：
> - 经典 CFS：直接取 `vruntime` 最小者（树最左节点），**没有 eligible 门槛、没有 deadline**。
> - EEVDF：先用 eligible（lag≥0）过滤，再按 deadline 排序。
>   slice 小 → `r_i/w_i` 小 → deadline 早 → 优先级实际更高。**这就是延迟控制的来源**。

### 2.2 avg_vruntime —— 高效计算 V（防溢出技巧）

直接套公式 `V = Σ(w_i·v_i)/Σw_i` 有个工程问题：`v_i` 是 u64，`w_i·v_i` 会溢出。
内核的解法（`kernel/sched/fair.c:657` 注释）是**换成相对量**。

令 `v0 = cfs_rq->zero_vruntime`，把 `v_i = (v_i - v0) + v0` 代入：

```text
       Σ ((v_i - v0) + v0) * w_i     Σ (v_i - v0) * w_i
  V =  -------------------------- =  ------------------- + v0
                  W                          W
```

于是只需维护两个小量（增删实体时增量更新）：

```text
  v0              := cfs_rq->zero_vruntime    （基准点，随队列缓慢推进）
  Σ (v_i-v0)*w_i  := cfs_rq->sum_w_vruntime   （相对加权和，量级仅为 lag 尺度）
  Σ w_i           := cfs_rq->sum_weight
```

`entity_key()`（`kernel/sched/fair.c:614`）就是算 `v_i - v0`：

```c
static inline s64 entity_key(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	return vruntime_op(se->vruntime, "-", cfs_rq->zero_vruntime);
}
```

增删时由 `__sum_w_vruntime_add()` / `sum_w_vruntime_sub()`（`kernel/sched/fair.c:685` / `748`）
维护 `sum_w_vruntime` 和 `sum_weight`。`avg_vruntime()` 本体（`kernel/sched/fair.c:780`）：

```c
u64 avg_vruntime(struct cfs_rq *cfs_rq)
{
	struct sched_entity *curr = cfs_rq->curr;
	long weight = cfs_rq->sum_weight;
	s64 delta = 0;

	if (curr && !curr->on_rq)
		curr = NULL;

	if (weight) {
		s64 runtime = cfs_rq->sum_w_vruntime;
		if (curr) {            // curr 不在树里，但参与平均，需临时加上
			unsigned long w = avg_vruntime_weight(cfs_rq, curr->load.weight);
			runtime += entity_key(cfs_rq, curr) * w;
			weight  += w;
		}
		if (runtime < 0)       // 符号决定 floor/ceil，保证 avg+0 仍判 eligible
			runtime -= (weight - 1);
		delta = div64_long(runtime, weight);
	} else if (curr) {
		delta = curr->vruntime - cfs_rq->zero_vruntime;  // 仅一个实体时它就是平均
	}

	update_zero_vruntime(cfs_rq, delta);   // 顺便把 v0 推进到新平均处
	return cfs_rq->zero_vruntime;
}
```

几个工程细节：
- **`curr` 不在红黑树里**（见 1.3：当前运行实体被摘出树），但它显然参与"平均"，
  所以这里临时把它的贡献加回 `runtime`/`weight`。
- **`update_zero_vruntime()`**（`kernel/sched/fair.c:758`）每次顺手把 `zero_vruntime` 推进到
  新算出的平均点，保证 `v_i - v0` 始终维持在很小的量级（约两个 lag 上界），
  这就是注释（`kernel/sched/fair.c:599`）说"`zero_vruntime` 只会轻微滞后"的含义。
- **`avg_vruntime_weight()`** 在极端情况下右移权重（`sum_shift`）进一步防溢出，
  并由 `PARANOID_AVG` 特性提供带溢出检查的"偏执版"加法 `sum_w_vruntime_add_paranoid()`。

### 2.3 entity_lag / update_entity_lag —— 算 lag 并缓存

lag 在**出队时**算好，缓存到 `se->vlag`，供下次入队复原用。先看裸 lag
计算 `entity_lag()`（`kernel/sched/fair.c:832`）：

```c
static s64 entity_lag(struct cfs_rq *cfs_rq, struct sched_entity *se, u64 avruntime)
{
	u64 max_slice = cfs_rq_max_slice(cfs_rq) + TICK_NSEC;
	s64 vlag, limit;

	vlag  = avruntime - se->vruntime;          // vl_i = V - v_i
	limit = calc_delta_fair(max_slice, se);

	return clamp(vlag, -limit, limit);         // 限幅，防止 V 漂移把 lag 放大
}
```

为何要 `clamp`？注释（`kernel/sched/fair.c:818`）解释：V 是加权平均，**增删/改权重会移动 V**，
可能使 lag 比初始更大。EEVDF 给出稳态系统的 lag 边界 `-r_max < lag < max(r_max, q)`，
代码因此把 lag 限制在约"两倍 slice、下限一个 TICK"的范围内。

`update_entity_lag()`（`kernel/sched/fair.c:858`）在此基础上处理延迟出队：

```c
bool update_entity_lag(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	u64 avruntime = avg_vruntime(cfs_rq);
	s64 vlag = entity_lag(cfs_rq, se, avruntime);

	WARN_ON_ONCE(!se->on_rq);

	if (se->sched_delayed) {
		/* 之前 vlag<0 才会被延迟；保证延迟期间负 lag 不再恶化 */
		vlag = max(vlag, se->vlag);
		if (sched_feat(DELAY_ZERO))
			vlag = min(vlag, 0);    // DELAY_ZERO：把 lag 削到 0
	}
	se->vlag = vlag;

	return avruntime - vlag != se->vruntime;   // 返回 vlag 是否被限幅修改过
}
```

### 2.4 entity_eligible / vruntime_eligible —— eligible 判定的精确实现

2.1.3 推出 `eligible ⟺ V ≥ v_i`。但**直接比较 `avg_vruntime() > v_i` 会因除法丢精度**
（注释 `kernel/sched/fair.c:891`）。内核改成**不做除法的等价不等式**。

由 `V = Σ(v_i-v0)w_i / W + v0` 且 `lag_i≥0 ⟺ V≥v_i`，移项得：

```text
  Σ (v_i - v0)*w_i  ≥  (v_query - v0) * (Σ w_i)
```

即 `vruntime_eligible()`（`kernel/sched/fair.c:894`）的核心：

```c
static int vruntime_eligible(struct cfs_rq *cfs_rq, u64 vruntime)
{
	struct sched_entity *curr = cfs_rq->curr;
	s64 key, avg = cfs_rq->sum_w_vruntime;   // 左边：Σ(v_i-v0)w_i
	long load = cfs_rq->sum_weight;          // 右边的 Σw_i

	if (curr && curr->on_rq) {               // curr 不在树里，补回它的贡献
		unsigned long weight = avg_vruntime_weight(cfs_rq, curr->load.weight);
		avg  += entity_key(cfs_rq, curr) * weight;
		load += weight;
	}

	key = vruntime_op(vruntime, "-", cfs_rq->zero_vruntime);  // v_query - v0

#ifdef CONFIG_ARCH_SUPPORTS_INT128
	return avg >= (__int128)key * load;      // 用 128 位避免溢出，纯比较无除法
#else
	/* ... 溢出时用 key 的符号判定 ... */
#endif
}

int entity_eligible(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	return vruntime_eligible(cfs_rq, se->vruntime);
}
```

> **为什么 `pick_eevdf` 要用 `vruntime_eligible(min_vruntime)` 而非 `entity_eligible(se)`？**
> 因为它要判断的是"**整棵子树里有没有可能 eligible 的实体**"——子树最小 `vruntime`
> 都不 eligible，整棵子树就都不 eligible，可以剪掉。这正是 2.6 增广树的精髓。

### 2.5 update_curr —— 记账：vruntime 推进与抢占触发

任务每次"被结算"（tick、出队、主动调度前）都会调用 `update_curr()`
（`kernel/sched/fair.c:1407`）。它是 vruntime 推进的**唯一入口**：

```c
static void update_curr(struct cfs_rq *cfs_rq)
{
	struct sched_entity *curr = cfs_rq->curr;
	struct rq *rq = rq_of(cfs_rq);
	s64 delta_exec;
	bool resched;

	if (unlikely(!curr))
		return;

	delta_exec = update_se(rq, curr);          // ① 算这段真实运行时间
	if (unlikely(delta_exec <= 0))
		return;

	curr->vruntime += calc_delta_fair(delta_exec, curr);   // ② vruntime 按权重推进
	resched = update_deadline(cfs_rq, curr);   // ③ 用满 slice 则重算 deadline 并请求抢占

	if (entity_is_task(curr))
		dl_server_update(&rq->fair_server, delta_exec);    // fair_server 记账（DL 兜底）

	account_cfs_rq_runtime(cfs_rq, delta_exec);            // ④ 带宽限流记账（第 4 章）

	if (cfs_rq->nr_queued == 1)
		return;                                // 队列里就一个，没人可抢，免谈

	if (resched || !protect_slice(curr)) {     // ⑤ slice 耗尽 或 保护期已过 → 抢占
		resched_curr_lazy(rq);
		clear_buddies(cfs_rq, curr);
	}
}
```

逐步看：

1. **`update_se()`**（`kernel/sched/fair.c:1353`）：用 `rq_clock_task(rq)`（扣掉中断时间的任务时钟）
   减去 `se->exec_start`，得到本段真实运行时长 `delta_exec`，并累加到
   `sum_exec_runtime`、做 cgroup 时间记账、更新 `exec_start`。
   （proxy-exec 下，时间记到真正在跑的 `rq->curr` 头上，cgroup 时间记到 donor 头上。）

2. **vruntime 推进**：`curr->vruntime += calc_delta_fair(delta_exec, curr)`。
   这是 2.2 节那个 `V` 之所以会变化的根源——`v_i` 在变。

3. **`update_deadline()`**（见 2.5.1）：若任务已跑满当前 slice，重算 deadline，返回需要抢占。

4. **`account_cfs_rq_runtime()`**：扣减带宽配额 `runtime_remaining`，可能触发限流（第 4 章）。

5. **抢占决策**：两种情况要求重新调度——
   - `resched == true`：slice 耗尽（deadline 已过）；
   - `!protect_slice(curr)`：保护期（`vprot`）已过（`RUN_TO_PARITY` 相关，见 2.7）。
   注意用的是 `resched_curr_lazy()`——**惰性**置位 `TIF_NEED_RESCHED`，
   等下一个安全抢占点再真正切换，而非立刻切。

#### 2.5.1 update_deadline —— slice 耗尽则推进 deadline

`update_deadline()`（`kernel/sched/fair.c:1238`）：

```c
static bool update_deadline(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	if (vruntime_cmp(se->vruntime, "<", se->deadline))
		return false;          // 还没到 deadline，slice 没跑满，不动

	if (!se->custom_slice)
		se->slice = sysctl_sched_base_slice;   // 非自定义则用默认 base_slice

	/* EEVDF: vd_i = ve_i + r_i / w_i */
	se->deadline = se->vruntime + calc_delta_fair(se->slice, se);
	avg_vruntime(cfs_rq);      // 顺手推进 zero_vruntime

	return true;               // slice 已消费完，请求重新调度
}
```

关键点：
- **只有 `vruntime ≥ deadline`（slice 跑满）才推进 deadline**，否则原样返回。
- 推进后任务的 deadline 变远，在红黑树里相应右移，给别人让位。
- 返回 `true` 让 `update_curr` 触发抢占——这是周期性"轮转"的机制来源。

> **对照经典 CFS**：CFS 没有 deadline，它的抢占判据是
> "当前任务 vruntime 比最左任务 vruntime 大出一个 granularity"。
> EEVDF 改为"跑满自己请求的 slice 就重算 deadline / 让位"，**slice 由任务自定义**，
> 因此延迟可控。

### 2.6 红黑树管理与增广 —— pick 的数据结构基础

EEVDF 的红黑树 `cfs_rq->tasks_timeline` **按 `deadline` 排序**
（比较器 `entity_before()`，`kernel/sched/fair.c:589`）：

```c
static inline bool entity_before(const struct sched_entity *a,
				 const struct sched_entity *b)
{
	return vruntime_cmp(a->deadline, "<", b->deadline);
}
```

所以"最左节点 = deadline 最早"。但 EEVDF 选择还需要 eligible 过滤，而 eligible
是按 `vruntime` 判的。为了在一棵按 deadline 排序的树上**也能按 vruntime 剪枝**，
内核把它做成**增广红黑树**：每个节点缓存"以我为根的子树中最小的 vruntime"。

聚合回调 `min_vruntime_update()`（`kernel/sched/fair.c:1010`）：

```c
static inline bool min_vruntime_update(struct sched_entity *se, bool exit)
{
	u64 old_min_vruntime = se->min_vruntime;
	struct rb_node *node = &se->run_node;

	se->min_vruntime = se->vruntime;             // 先取自己
	__min_vruntime_update(se, node->rb_right);   // 再和右子树最小比
	__min_vruntime_update(se, node->rb_left);    // 和左子树最小比
	/* min_slice / max_slice 同理一起维护 */
	...
}
RB_DECLARE_CALLBACKS(static, min_vruntime_cb, struct sched_entity,
		     run_node, min_vruntime, min_vruntime_update);
```

入队 / 出队就用带增广回调的 rbtree 原语（`kernel/sched/fair.c:1040`）：

```c
static void __enqueue_entity(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	sum_w_vruntime_add(cfs_rq, se);              // 维护 avg_vruntime 的聚合量
	se->min_vruntime = se->vruntime;
	se->min_slice = se->slice;
	rb_add_augmented_cached(&se->run_node, &cfs_rq->tasks_timeline,
				__entity_less, &min_vruntime_cb);
}

static void __dequeue_entity(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	rb_erase_augmented_cached(&se->run_node, &cfs_rq->tasks_timeline,
				  &min_vruntime_cb);
	sum_w_vruntime_sub(cfs_rq, se);
}
```

> **一棵树，两种序**：
> - 主序（链接结构）：按 `deadline`（找"最早截止"）。
> - 增广聚合（剪枝）：按 `min_vruntime`（判"子树有无 eligible"）。
> 这让 `pick_eevdf` 能在 O(log n) 内完成"eligible 集合里取最早 deadline"。

### 2.7 pick_eevdf —— 核心选择算法

万事俱备，来看选择本体 `pick_eevdf()`（`kernel/sched/fair.c:1136`）。
注释（`kernel/sched/fair.c:1117`）开宗明义：

```text
 * Earliest Eligible Virtual Deadline First
 * EEVDF selects the best runnable task from two criteria:
 *  1) the task must be eligible (must be owed service)
 *  2) from those tasks that meet 1), we select the one
 *     with the earliest virtual deadline.
 * We can do this in O(log n) time due to an augmented RB-tree.
```

完整代码与逐段注解：

```c
static struct sched_entity *pick_eevdf(struct cfs_rq *cfs_rq, bool protect)
{
	struct rb_node *node = cfs_rq->tasks_timeline.rb_root.rb_node;
	struct sched_entity *se = __pick_first_entity(cfs_rq);   // 最左 = deadline 最早
	struct sched_entity *curr = cfs_rq->curr;
	struct sched_entity *best = NULL;

	/* 【快路径】队列里就一个实体，免做 eligible 检查 */
	if (cfs_rq->nr_queued == 1)
		return curr && curr->on_rq ? curr : se;

	/* 【buddy 优化】PICK_BUDDY：若有 ->next 且它 eligible，直接选它。
	 * 影响延迟但不影响公平性（next 来自唤醒抢占/yield_to/cgroup） */
	if (sched_feat(PICK_BUDDY) && protect &&
	    cfs_rq->next && entity_eligible(cfs_rq, cfs_rq->next)) {
		WARN_ON_ONCE(cfs_rq->next->sched_delayed);
		return cfs_rq->next;
	}

	/* curr 不 eligible 或已离队，就不参与竞争 */
	if (curr && (!curr->on_rq || !entity_eligible(cfs_rq, curr)))
		curr = NULL;

	/* 【RUN_TO_PARITY】curr 还在保护期内（未达 0-lag 点）→ 留住 curr，抑制抢占 */
	if (curr && protect && protect_slice(curr))
		return curr;

	/* 【常见情形】最左节点本身就 eligible → 它 deadline 最早，直接选中 */
	if (se && entity_eligible(cfs_rq, se)) {
		best = se;
		goto found;
	}

	/* 【堆搜索】最左不 eligible，需在树上找"eligible 集合里 deadline 最早者" */
	while (node) {
		struct rb_node *left = node->rb_left;

		/* 左子树里若存在 eligible 实体（用增广 min_vruntime 判断），
		 * 它们 deadline 一定更早，优先下钻左子树 */
		if (left && vruntime_eligible(cfs_rq,
					__node_2_se(left)->min_vruntime)) {
			node = left;
			continue;
		}

		se = __node_2_se(node);

		/* 左子树没有 eligible，则当前节点是"deadline 最早且可能 eligible"的，
		 * 检查它本身 */
		if (entity_eligible(cfs_rq, se)) {
			best = se;
			break;
		}

		/* 当前节点也不 eligible（vruntime 太大），往右子树找 deadline 更大的 */
		node = node->rb_right;
	}
found:
	/* curr 没进树参与比较；若 curr 比 best 的 deadline 还早，选 curr */
	if (!best || (curr && entity_before(curr, best)))
		best = curr;

	return best;
}
```

把堆搜索讲透——它在"按 deadline 排序的树"上找"eligible 子集中 deadline 最早者"：

- 树是 deadline 升序，所以**越靠左 deadline 越早**。我们想要 deadline 最早的 eligible 实体。
- 在任一节点：
  - **先看左子树**。若左子树存在 eligible 实体（`vruntime_eligible(left->min_vruntime)`
    为真，意味着左子树里最小 vruntime 的那个都 eligible），那么左子树里必有比当前节点
    deadline 更早的 eligible 候选 → 下钻左子树。
  - 左子树**无** eligible → 当前节点就是"剩下里 deadline 最早"的，检查它本身是否 eligible：
    - eligible → 选它，结束。
    - 不 eligible → 它 vruntime 太大，往**右**走找 deadline 更大的（右边 vruntime 可能更小）。

这套"看左子树聚合值决定剪枝方向"的逻辑，把朴素 O(n) 扫描降到 **O(log n)**。

> **RUN_TO_PARITY 的意义**（`set_protect_slice` / `protect_slice`，`kernel/sched/fair.c:1084`）：
> 频繁的唤醒抢占会让当前任务反复被打断，损害吞吐和 cache。`RUN_TO_PARITY` 让当前任务
> 在到达"0-lag 点"（即 vruntime 追平 `vprot`）之前，免受唤醒抢占，至少跑出一个有意义的
> quantum。`PREEMPT_SHORT` 则是它的反向阀门：被唤醒的任务若 slice 更短（更急），
> 可取消当前任务的 RUN_TO_PARITY 保护（`cancel_protect_slice`）。

#### 2.7.1 set_protect_slice / protect_slice

```c
static inline void set_protect_slice(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	u64 slice = normalized_sysctl_sched_base_slice;
	u64 vprot = se->deadline;

	if (sched_feat(RUN_TO_PARITY))
		slice = cfs_rq_min_slice(cfs_rq);      // 用队列里最短 slice 作保护窗

	slice = min(slice, se->slice);
	if (slice != se->slice)
		vprot = min_vruntime(vprot, se->vruntime + calc_delta_fair(slice, se));

	se->vprot = vprot;
}

static inline bool protect_slice(struct sched_entity *se)
{
	return vruntime_cmp(se->vruntime, "<", se->vprot);   // 还没追到 vprot = 受保护中
}
```

`set_protect_slice()` 在任务被设为 `curr`（`set_next_entity`，见 2.10）时调用，
确定本次最少能跑多久不被抢。

### 2.8 place_entity —— 入队定位（lag 复原 + 定 deadline）

任务（重新）入队时，必须决定它的 `vruntime` 落点和 deadline。这是 EEVDF 公平性的
另一半关键，由 `place_entity()`（`kernel/sched/fair.c:5380`）完成：

```c
static void place_entity(struct cfs_rq *cfs_rq, struct sched_entity *se, int flags)
{
	u64 vslice, vruntime = avg_vruntime(cfs_rq);   // 以当前平均 V 为基准
	bool update_zero = false;
	s64 lag = 0;

	if (!se->custom_slice)
		se->slice = sysctl_sched_base_slice;
	vslice = calc_delta_fair(se->slice, se);       // 时间片的虚拟跨度 r_i/w_i

	/* 【PLACE_LAG】保留睡前的 lag：把缓存在 se->vlag 的 lag 还原到 vruntime */
	if (sched_feat(PLACE_LAG) && cfs_rq->nr_queued && se->vlag) {
		struct sched_entity *curr = cfs_rq->curr;
		long load, weight;

		lag = se->vlag;
		/* 加入新实体会改变加权平均 V，需要按比例"放大"lag 以抵消该效应，
		 * 否则 lag 会迅速蒸发。推导见源码注释（fair.c:5405-5456）：
		 *   vl_i = (W + w_i) * vl'_i / W
		 */
		load = cfs_rq->sum_weight;
		if (curr && curr->on_rq)
			load += avg_vruntime_weight(cfs_rq, curr->load.weight);
		weight = avg_vruntime_weight(cfs_rq, se->load.weight);
		lag *= load + weight;
		if (WARN_ON_ONCE(!load))
			load = 1;
		lag = div64_long(lag, load);

		if (weight > load)            // 重实体：防溢出，需顺带推进 zero_vruntime
			update_zero = true;
	}

	se->vruntime = vruntime - lag;    // ★ 落点 = 平均 V 减去（放大后的）lag

	if (update_zero)
		update_zero_vruntime(cfs_rq, -lag);

	/* 【PLACE_REL_DEADLINE】迁移场景：deadline 之前以相对形式保存，现加回 vruntime 复原 */
	if (sched_feat(PLACE_REL_DEADLINE) && se->rel_deadline) {
		se->deadline += se->vruntime;
		se->rel_deadline = 0;
		return;
	}

	/* 【PLACE_DEADLINE_INITIAL】全新任务：只给半个 slice，温和加入竞争 */
	if (sched_feat(PLACE_DEADLINE_INITIAL) && (flags & ENQUEUE_INITIAL))
		vslice /= 2;

	/* EEVDF: vd_i = ve_i + r_i/w_i */
	se->deadline = se->vruntime + vslice;
}
```

三个 placement 特性各司其职：

| 特性 | 作用 | 解决的问题 |
| --- | --- | --- |
| `PLACE_LAG` | 入队时把睡前 lag 还原到 vruntime 落点 | 让任务跨睡眠/唤醒**保留公平账**，睡一觉不会清零欠/超 |
| `PLACE_DEADLINE_INITIAL` | 新任务初始 deadline 只用半个 slice | 新任务"温和入场"，不抢占已在跑的任务太狠 |
| `PLACE_REL_DEADLINE` | 迁移时保留相对 deadline | 跨 CPU 迁移后**保持相对紧迫度**不变 |

> **PLACE_LAG 的巧思**：直接令 `se->vruntime = V - vlag` 看似就能复原 lag，但**加入新实体
> 本身会改变 V**（V 是加权平均），导致 lag 立刻缩水。源码用一段代数推导（fair.c:5426 起）
> 反解出"应该用多大的 lag 去放置，才能在放置后得到我们想要的 lag"，即把 lag 预先放大
> `(W+w_i)/W` 倍。这是 EEVDF 公平性在工程上"较真"的体现。

### 2.9 enqueue_entity / dequeue_entity —— 入队与出队全流程

#### 2.9.1 enqueue_entity

`enqueue_entity()`（`kernel/sched/fair.c:5523`）把一个实体放进就绪队列：

```c
static void enqueue_entity(struct cfs_rq *cfs_rq, struct sched_entity *se, int flags)
{
	bool curr = cfs_rq->curr == se;

	if (curr)                          // 若是当前任务（重入队），先 place 再记账
		place_entity(cfs_rq, se, flags);
	update_curr(cfs_rq);               // 先把已运行时间结清

	update_load_avg(cfs_rq, se, UPDATE_TG | DO_ATTACH);  // PELT：把实体负载并入队列
	se_update_runnable(se);
	update_cfs_group(se);              // 组调度：按 group 份额重算权重

	if (!curr)
		place_entity(cfs_rq, se, flags);   // 非当前任务，重算权重后再 place

	account_entity_enqueue(cfs_rq, se);    // 队列总权重 += se->weight

	if (flags & ENQUEUE_MIGRATED)
		se->exec_start = 0;            // 迁移来的，标记"非 cache-hot"

	update_stats_enqueue_fair(cfs_rq, se, flags);
	if (!curr)
		__enqueue_entity(cfs_rq, se);  // ★ 真正插入红黑树（curr 不入树）
	se->on_rq = 1;

	if (cfs_rq->nr_queued == 1) {      // 队列从空变非空
		check_enqueue_throttle(cfs_rq);
		list_add_leaf_cfs_rq(cfs_rq);  // 挂入 leaf 链表（负载均衡/带宽遍历用）
		/* ... 解冻 PELT 时钟 ... */
	}
}
```

注意 `curr` 这条岔路：当前正在跑的任务**不在树里**，所以若它重新入队（如改权重），
处理顺序不同——先 `place_entity` 校准、记账，但**不调用 `__enqueue_entity`**（它仍是 curr）。

#### 2.9.2 dequeue_entity 与 DELAY_DEQUEUE

`dequeue_entity()`（`kernel/sched/fair.c:5646`）是出队，**EEVDF 的延迟出队就在这里**：

```c
static bool dequeue_entity(struct cfs_rq *cfs_rq, struct sched_entity *se, int flags)
{
	bool sleep = flags & DEQUEUE_SLEEP;
	int action = UPDATE_TG;

	update_curr(cfs_rq);               // 先结算
	clear_buddies(cfs_rq, se);

	if (flags & DEQUEUE_DELAYED) {
		WARN_ON_ONCE(!se->sched_delayed);
	} else {
		bool delay = sleep;
		if (flags & (DEQUEUE_SPECIAL | DEQUEUE_THROTTLE))
			delay = false;         // 特殊态/限流不延迟（避免伪唤醒问题）

		/* 【DELAY_DEQUEUE】睡眠且 lag<0（不 eligible）→ 不立刻摘树，
		 * 留下"烧掉"负 lag，防止靠短睡眠重置负账作弊 */
		if (sched_feat(DELAY_DEQUEUE) && delay &&
		    !entity_eligible(cfs_rq, se)) {
			update_load_avg(cfs_rq, se, 0);
			update_entity_lag(cfs_rq, se);   // 算好并缓存 lag
			set_delayed(se);                 // 打 sched_delayed 标记
			return false;                    // ★ 注意：没真正出队
		}
	}

	if (entity_is_task(se) && task_on_rq_migrating(task_of(se)))
		action |= DO_DETACH;

	update_load_avg(cfs_rq, se, action);   // PELT：把实体负载从队列剥离
	se_update_runnable(se);

	update_entity_lag(cfs_rq, se);         // ★ 算 lag 缓存进 se->vlag（供下次 place 复原）
	if (sched_feat(PLACE_REL_DEADLINE) && !sleep) {
		se->deadline -= se->vruntime;  // 转相对 deadline（迁移用）
		se->rel_deadline = 1;
	}

	if (se != cfs_rq->curr)
		__dequeue_entity(cfs_rq, se);  // ★ 真正从红黑树摘除（curr 本就不在树里）
	se->on_rq = 0;
	account_entity_dequeue(cfs_rq, se);    // 队列总权重 -= se->weight

	return_cfs_rq_runtime(cfs_rq);         // 归还多余配额（带宽，第 4 章）
	update_cfs_group(se);

	if (flags & DEQUEUE_DELAYED)
		clear_delayed(se);

	if (cfs_rq->nr_queued == 0)
		update_idle_cfs_rq_clock_pelt(cfs_rq);  // 队列空了，处理 PELT 空闲时钟

	return true;
}
```

**DELAY_DEQUEUE 的价值**（`features.h:49` 注释 + `sched-eevdf.rst:24`）：
一个 lag < 0（超额）的任务若一睡就被摘出队、醒来重新 place，相当于**用睡眠把负 lag 清零**，
对老实排队的任务不公平。延迟出队让它**留在树里继续"虚拟地欠债"**，直到 lag 自然回到 ≥0
（被选中时按定义已是正 lag）或被真正唤醒/选中时再处理。`DELAY_ZERO` 则在出队/唤醒时
直接把 lag 削到 0。

被延迟的实体在 `pick_next_entity()`（`kernel/sched/fair.c:5780`）里若被选中，会触发真正出队：

```c
static struct sched_entity *
pick_next_entity(struct rq *rq, struct cfs_rq *cfs_rq, bool protect)
{
	struct sched_entity *se = pick_eevdf(cfs_rq, protect);
	if (se->sched_delayed) {
		dequeue_entities(rq, se, DEQUEUE_SLEEP | DEQUEUE_DELAYED);
		return NULL;                   // 它其实是要睡的，本轮重挑
	}
	return se;
}
```

### 2.10 set_next_entity / put_prev_entity —— 上 / 下 CPU

被选中的实体要"上 CPU"，由 `set_next_entity()`（`kernel/sched/fair.c:5729`）处理：

```c
static void set_next_entity(struct cfs_rq *cfs_rq, struct sched_entity *se, bool first)
{
	clear_buddies(cfs_rq, se);

	if (se->on_rq) {
		update_stats_wait_end_fair(cfs_rq, se);
		__dequeue_entity(cfs_rq, se);          // ★ 当前任务移出树（curr 不在树里）
		update_load_avg(cfs_rq, se, UPDATE_TG);
		if (first)
			set_protect_slice(cfs_rq, se); // 设定 RUN_TO_PARITY 保护窗
	}

	update_stats_curr_start(cfs_rq, se);
	cfs_rq->curr = se;                             // ★ 成为当前实体
	se->prev_sum_exec_runtime = se->sum_exec_runtime;
}
```

要点：**当前运行实体被移出红黑树**（`__dequeue_entity`），存于 `cfs_rq->curr`。
这解释了前面反复强调的"curr 不在树里、avg/eligible 计算要单独补回 curr 贡献"。

被换下时，`put_prev_entity()`（`kernel/sched/fair.c:5798`）把它放回树：

```c
static void put_prev_entity(struct cfs_rq *cfs_rq, struct sched_entity *prev)
{
	if (prev->on_rq)
		update_curr(cfs_rq);               // 结算
	check_cfs_rq_runtime(cfs_rq);              // 带宽：超额则限流（第 4 章）
	if (prev->on_rq) {
		update_stats_wait_start_fair(cfs_rq, prev);
		__enqueue_entity(cfs_rq, prev);    // ★ 放回红黑树
		update_load_avg(cfs_rq, prev, 0);
	}
	cfs_rq->curr = NULL;
}
```

### 2.11 周期抢占：task_tick_fair → entity_tick

时钟中断驱动周期性抢占检查。调度核心的 `sched_tick()`（`kernel/sched/core.c`）
调用当前任务所属类的 `task_tick`，公平类是 `task_tick_fair()`（`kernel/sched/fair.c:13687`），
它对当前实体（含其所有祖先组实体）调用 `entity_tick()`（`kernel/sched/fair.c:5821`）：

```c
static void entity_tick(struct cfs_rq *cfs_rq, struct sched_entity *curr, int queued)
{
	update_curr(cfs_rq);               // ★ 推进 vruntime + 可能请求抢占（2.5）
	update_load_avg(cfs_rq, curr, UPDATE_TG);   // PELT
	update_cfs_group(curr);            // 组份额
#ifdef CONFIG_SCHED_HRTICK
	/* hrtick 已按 slice 精确排程，直接 resched */
	...
#endif
}
```

所以**周期抢占的核心就是 `update_curr` → `update_deadline`**：tick 到来 → 结算这段运行 →
若 slice 跑满则 deadline 推进并 `resched_curr_lazy()`，下一个抢占点切走。

### 2.12 唤醒抢占：check_preempt_wakeup_fair

任务被唤醒时，调度核心调用 `wakeup_preempt` hook，公平类是
`check_preempt_wakeup_fair()`。它的判据本质上是 EEVDF 的：

- 被唤醒任务（`pse`）与当前任务（`se`）若在不同组层级，先用 `find_matching_se()`
  （`kernel/sched/fair.c:430`）上溯到同一 `cfs_rq` 下的可比实体；
- 若开启 `WAKEUP_PREEMPTION` 且被唤醒任务的 **deadline 比当前任务更早**（`entity_before`），
  且不被 `RUN_TO_PARITY` 保护期挡住（或被 `PREEMPT_SHORT` 取消保护），则抢占；
- 设置 `set_next_buddy(pse)`（若 `NEXT_BUDDY`），让 `pick_eevdf` 倾向选刚唤醒的任务（cache 局部性）。

> **对照经典 CFS**：CFS 的唤醒抢占判据是"被唤醒任务 vruntime 比当前小出一个
> wakeup granularity"。EEVDF 改为比 deadline，并叠加 RUN_TO_PARITY/PREEMPT_SHORT
> 两个特性精细控制——既保护当前任务不被频繁打断，又允许更急的短任务插队。

### 2.13 主调度链：从 schedule() 到 pick_eevdf

把上面所有片段串进真实的调度主流程。入口 `schedule()`（`kernel/sched/core.c:7273` 附近）
最终进入 `__schedule()`（`kernel/sched/core.c:7017`）：

```text
schedule()                                  kernel/sched/core.c:7273
  └─ __schedule_loop(SM_NONE)
       └─ __schedule(sched_mode)            kernel/sched/core.c:7017
            1. local_irq_disable(); rq_lock(rq)
            2. update_rq_clock(rq)            // 刷新 rq->clock / clock_task
            3. prev = rq->curr
            4. 若 prev 要睡：deactivate_task() → dequeue_task_fair() → dequeue_entity()
            5. next = pick_next_task(rq, prev, &rf)   // ★ 选下一个
                 └─ 快路径：若全是 fair 任务 → pick_next_task_fair()
                 └─ 通用路径：按类优先级 stop→dl→rt→fair→idle 遍历
            6. 若 next != prev:
                 context_switch(rq, prev, next)        // 真正切换
            7. balance_callback()                      // 负载均衡回调
```

公平类的选择 `pick_next_task_fair()`（`kernel/sched/fair.c:9234`）：

```text
pick_next_task_fair()                       kernel/sched/fair.c:9234
  └─ (组调度) 自顶向下逐层：
       cfs_rq = &rq->cfs
       do {
           se = pick_next_entity(rq, cfs_rq, protect=true)   // fair.c:5780
                 └─ pick_eevdf(cfs_rq, protect)              // fair.c:1136  ★核心
           cfs_rq = group_cfs_rq(se);    // 若 se 是组实体，下钻它的 my_q
       } while (cfs_rq);
  └─ set_next_entity() 沿层级把选中链路设为 curr        // fair.c:5729
  └─ put_prev_task / put_prev_entity 处理被换下的 prev
```

> **组调度下的层层选择**：开启 `CONFIG_FAIR_GROUP_SCHED` 后，`pick` 是**逐层**的——
> 先在根 `cfs_rq` 用 `pick_eevdf` 选出一个**组实体**，再下钻到该组的 `my_q` 继续 `pick_eevdf`，
> 直到选出一个真正的任务实体。每一层都独立跑 EEVDF（第 4 章详述）。

### 2.14 本章小结：EEVDF vs 经典 CFS 一览

| 维度 | 经典 CFS（≤6.5） | EEVDF（6.6+，本仓库） |
| --- | --- | --- |
| 排序键 | `vruntime`（红黑树按 vruntime 排） | `deadline`（红黑树按 deadline 排，增广 min_vruntime） |
| 选择规则 | 取 vruntime 最小（最左节点） | eligible(lag≥0) 集合里取 deadline 最早 |
| 时间片 | 隐式（granularity，不可逐任务定制） | 显式 `slice`，可经 `sched_setattr` 自定义 |
| 延迟控制 | 弱（仅靠 nice 与 granularity） | 强（小 slice → 早 deadline → 优先） |
| lag | 隐含于 vruntime | 显式跟踪 `vlag`，跨睡眠保留（PLACE_LAG/DELAY_DEQUEUE） |
| 抢占判据 | vruntime 差 > granularity | deadline 更早 + RUN_TO_PARITY/PREEMPT_SHORT |
| 复杂度 | O(log n) 取最左 | O(log n) 增广树堆搜索 |

核心代码索引（便于回查）：

| 概念 | 函数 | 位置 |
| --- | --- | --- |
| 真实→虚拟时间 | `calc_delta_fair` | `kernel/sched/fair.c:297` |
| 加权平均 V | `avg_vruntime` | `kernel/sched/fair.c:780` |
| lag 计算缓存 | `update_entity_lag` | `kernel/sched/fair.c:858` |
| eligible 判定 | `vruntime_eligible` / `entity_eligible` | `kernel/sched/fair.c:894` / `939` |
| 记账+抢占 | `update_curr` | `kernel/sched/fair.c:1407` |
| deadline 推进 | `update_deadline` | `kernel/sched/fair.c:1238` |
| 增广聚合 | `min_vruntime_update` | `kernel/sched/fair.c:1010` |
| **选择算法** | **`pick_eevdf`** | **`kernel/sched/fair.c:1136`** |
| 入队定位 | `place_entity` | `kernel/sched/fair.c:5380` |
| 入队/出队 | `enqueue_entity` / `dequeue_entity` | `kernel/sched/fair.c:5523` / `5646` |
| 上/下 CPU | `set_next_entity` / `put_prev_entity` | `kernel/sched/fair.c:5729` / `5798` |
| 周期抢占 | `task_tick_fair` → `entity_tick` | `kernel/sched/fair.c:13687` / `5821` |
| 主选择 | `pick_next_task_fair` | `kernel/sched/fair.c:9234` |

---

## 第 3 章 负载均衡与 SMP

前两章讲的都是**单个 CPU 内**怎么选任务。但现代机器有几十上百个核，
还要面对 NUMA、SMT（超线程）、大小核（asymmetric capacity）等拓扑。
**负载均衡（load balancing）**负责"把任务在 CPU 之间搬来搬去"，让算力被充分且公平地利用。

负载均衡分两条线：
1. **周期均衡**（periodic / busy / idle balance）：时钟驱动，沿调度域层级把忙 CPU 的任务搬给闲 CPU。
2. **唤醒选核**（wake-up placement）：任务被唤醒时，`select_task_rq_fair()` 决定它落到哪个 CPU。

### 3.1 调度域层级 sched_domain

负载均衡不是"一个 CPU 对所有 CPU"地暴力比较，而是沿一棵**调度域树**分层进行
（设计见 `Documentation/scheduler/sched-domains.rst`）。典型 NUMA + SMT 机器的层级：

```text
   NUMA domain         （跨 socket，均衡代价最高，最不频繁）
      │
   MC / LLC domain     （共享末级缓存的一组核）
      │
   SMT domain          （一个物理核的多个超线程，均衡代价最低，最频繁）
```

- `struct sched_domain`（`kernel/sched/topology.c`、`kernel/sched/sched.h`）：每个 CPU 在每一层都有
  一个 sched_domain，描述"这一层我能和谁均衡、多久均衡一次、迁移代价多大"。
  关键字段：`min_interval`/`max_interval`（均衡间隔）、`busy_factor`、`imbalance_pct`
  （触发均衡的不平衡阈值百分比）、`flags`（`SD_SHARE_LLC`、`SD_ASYM_CPUCAPACITY`、`SD_SERIALIZE` 等）。
- `struct sched_group`：调度域内部把 CPU 再分成若干 group，均衡是"在 group 之间"搬，
  而非"在单个 CPU 之间"搬。
- 层级越高（NUMA），`SD_SERIALIZE` 等标志要求**串行化**均衡（用全局原子量
  `sched_balance_running`，`kernel/sched/fair.c:12095`），因为跨 NUMA 搬任务代价高、要谨慎。

### 3.2 周期均衡的触发：sched_balance_softirq

时钟中断里 `sched_tick()` 会调用 `sched_balance_trigger()`，在满足条件时拉起
`SCHED_SOFTIRQ` 软中断，其处理函数是 `sched_balance_softirq()`（`kernel/sched/fair.c:13333`）：

```text
sched_tick()                              （每 tick）
  └─ sched_balance_trigger()
       └─ 若到了本 CPU 的均衡时刻 → raise_softirq(SCHED_SOFTIRQ)

软中断上下文：
sched_balance_softirq()                   kernel/sched/fair.c:13333
  └─ sched_balance_domains(this_rq, idle) kernel/sched/fair.c:12590
       └─ for_each_domain(cpu, sd):       // 自底向上遍历调度域层级
            若到达该 sd 的均衡间隔：
              sched_balance_rq(cpu, rq, sd, idle, &continue_balancing)
```

`sched_balance_domains()` 遍历当前 CPU 的每一层调度域，对到期的层调用核心均衡函数
`sched_balance_rq()`。

### 3.3 核心均衡：sched_balance_rq

`sched_balance_rq()`（`kernel/sched/fair.c:12101`）是"在某一层调度域内做一次均衡"的主函数：

```c
static int sched_balance_rq(int this_cpu, struct rq *this_rq,
			struct sched_domain *sd, enum cpu_idle_type idle,
			int *continue_balancing)
{
	struct lb_env env = {
		.sd = sd, .dst_cpu = this_cpu, .dst_rq = this_rq,
		.idle = idle, .loop_break = SCHED_NR_MIGRATE_BREAK,
		.cpus = cpus, .fbq_type = all,
		.tasks = LIST_HEAD_INIT(env.tasks),
	};
	...
redo:
	if (!should_we_balance(&env)) {        // ① 该不该由“我”来均衡？
		*continue_balancing = 0;
		goto out_balanced;
	}

	/* NUMA 等层需串行化 */
	if (!need_unlock && (sd->flags & SD_SERIALIZE)) {
		if (!atomic_try_cmpxchg_acquire(&sched_balance_running, &zero, 1))
			goto out_balanced;
		need_unlock = true;
	}

	group = sched_balance_find_src_group(&env);   // ② 找最忙的 group
	if (!group) goto out_balanced;

	busiest = sched_balance_find_src_rq(&env, group);  // ③ 在最忙 group 里找最忙 rq
	if (!busiest) goto out_balanced;

	env.src_cpu = busiest->cpu;
	env.src_rq  = busiest;
	...
	/* ④ 从 busiest 把任务搬到 this_rq */
	cur_ld_moved = detach_tasks(&env);
	... attach_tasks(&env);
	...
}
```

四步骤：

1. **`should_we_balance()`**：避免"所有 CPU 同时去抢同一个忙 CPU 的任务"。
   通常只让 group 里的**第一个 idle CPU**（或没有 idle 时的第一个 CPU）来执行本次均衡。
2. **`sched_balance_find_src_group()`**（`kernel/sched/fair.c:11645`）：扫描本调度域所有 group，
   用 `update_sd_lb_stats()`（`kernel/sched/fair.c:11373`，内部对每个 group 调
   `update_sg_lb_stats()`，`kernel/sched/fair.c:10700`）算出统计，挑出"最忙"的 group。
3. **`sched_balance_find_src_rq()`**（`kernel/sched/fair.c:11783`）：在最忙 group 内挑最忙的那个 rq。
4. **`detach_tasks()` → `attach_tasks()`**：真正搬任务。

### 3.4 不平衡量与迁移类型：calculate_imbalance

均衡前要回答"该搬多少、按什么标准搬"。这由 `calculate_imbalance()`（`kernel/sched/fair.c:11443`）
决定，输出 `env->migration_type` 和 `env->imbalance`。

group 的状态用 `enum group_type`（`kernel/sched/fair.c:9532`）从轻到重分级：

```c
enum group_type {
	group_has_spare = 0,   // 有空闲算力，能再塞任务
	group_fully_busy,      // 满负荷但任务不争抢（恰好够用）
	group_misfit_task,     // 有任务"装不下"，需迁到更强的 CPU（大小核）
	group_smt_balance,     // SMT：忙核 sibling 上的任务可迁到空闲物理核
	group_asym_packing,    // 非对称封装：应迁到更高算力的首选 CPU
	group_imbalanced,      // 亲和性约束导致历史上无法均衡
	group_overloaded       // 过载，无法满足所有任务
};
```

`calculate_imbalance()` 按 busiest group 的类型选择**迁移标准** `migration_type`：

| 场景（busiest group_type） | migration_type | imbalance 含义 |
| --- | --- | --- |
| `group_misfit_task` | `migrate_misfit` / `migrate_load` | 把装不下的任务迁到强核 |
| `group_asym_packing` | `migrate_task` | 迁任务数到首选高算力 CPU |
| `group_smt_balance` | `migrate_task` | 迁 1 个任务离开忙 SMT |
| `group_imbalanced` | `migrate_task` | 迁任意 1 个任务打破僵局 |
| `local 有 spare` 且非共享 LLC | `migrate_util` | 用利用率差额填满本地空闲 |
| 两边都 overloaded | `migrate_load` | 按加权负载差搬运 |

> **migrate_load vs migrate_util vs migrate_task** 是负载均衡的三种"度量"：
> - `migrate_load`：按 PELT 加权负载 `task_h_load` 搬（重活）。
> - `migrate_util`：按利用率（util_avg）搬（让利用率均衡）。
> - `migrate_task`：直接按任务**个数**搬（轻量场景）。

### 3.5 搬运任务：detach_tasks / attach_tasks

`detach_tasks()`（`kernel/sched/fair.c:9904`）从最忙 rq 的 `cfs_tasks` 链表尾部摘任务：

```c
static int detach_tasks(struct lb_env *env)
{
	struct list_head *tasks = &env->src_rq->cfs_tasks;
	...
	if (env->imbalance <= 0)
		return 0;

	while (!list_empty(tasks)) {
		/* 不能把对端搬空，否则可能反过来被搬、形成活锁 */
		if (env->idle && env->src_rq->nr_running <= 1)
			break;

		env->loop++;
		if (env->loop > env->loop_max) break;
		if (env->loop > env->loop_break) {      // 每 nr_migrate 个歇一下，降锁竞争
			env->flags |= LBF_NEED_BREAK;
			break;
		}

		p = list_last_entry(tasks, struct task_struct, se.group_node);

		if (!can_migrate_task(p, env))          // ★ 能不能搬？见 3.6
			goto next;

		switch (env->migration_type) {
		case migrate_load: /* 按 task_h_load 扣减 imbalance */ ...
		case migrate_util: ...
		case migrate_task: ...
		case migrate_misfit: ...
		}
		/* 满足后 detach 该任务，待会 attach 到 dst */
	}
	return detached;
}
```

`attach_tasks()`（`kernel/sched/fair.c:10043`）把摘下的任务挂到目标 rq 上（`activate_task` + 必要时 resched）。

### 3.6 can_migrate_task —— 迁移约束

不是所有任务都能随便搬。`can_migrate_task()` 检查多重约束：

- **CPU 亲和性**：`p->cpus_ptr` 是否允许目标 CPU（`sched_setaffinity` 设置的掩码）。
  不允许则置 `LBF_SOME_PINNED` / `LBF_ALL_PINNED`。
- **cache 热度**：`task_hot()`——刚跑过的任务（`exec_start` 距今短于
  `sysctl_sched_migration_cost`，默认 500µs，`kernel/sched/fair.c:82`）被认为"cache 热"，
  搬走会丢 cache，一般不搬（除非不平衡严重）。
- **正在运行**：当前在 CPU 上跑的任务不能被普通迁移（需 active balance，见 3.7）。

> `CACHE_HOT_BUDDY` 特性（`features.h:43`）让 buddy 任务也被视为 cache 热，
> 降低被迁走的概率，增强 cache 局部性。

### 3.7 active load balance —— 搬运正在跑的任务

如果最忙 rq 上**只剩一个正在跑的任务**（无法用 detach_tasks 搬"排队中"的任务），
但它确实该被迁走（如装不下的 misfit 任务要去大核），就用 **active balance**：
向目标 CPU 发停机任务回调 `active_load_balance_cpu_stop()`（`kernel/sched/fair.c:12431`），
在对端 CPU 上把那个正在跑的任务"硬搬"过来。这是较重的手段，仅在必要时启用。

### 3.8 newidle balance —— CPU 即将空闲时的抢救

当一个 CPU 发现自己**马上无活可干**（`pick_next_task_fair` 选不出任务），
会在真正进入 idle 前做一次 `sched_balance_newidle()`（`kernel/sched/fair.c:5061`），
尝试从别的 CPU **拉**（pull）一些任务过来，避免本核空转而别核排队。

这是延迟敏感的关键路径：newidle balance 太激进会增加唤醒延迟和锁竞争，太保守又浪费算力。
特性 `NI_RANDOM` / `NI_RATE`（`features.h:133`）让 newidle 均衡**按其历史成功率随机化/自适应限频**，
在"抢救负载"与"别白忙活"之间取得平衡。

### 3.9 唤醒选核：select_task_rq_fair

另一条线是**任务被唤醒时落到哪个 CPU**。公平类的 `select_task_rq` hook 是
`select_task_rq_fair()`，核心两步：

1. **wake affine（唤醒亲和）**：倾向把被唤醒任务放到**唤醒者所在 CPU 附近**，
   因为它们很可能共享数据（生产者-消费者），放近点 cache 命中好。
   由 `wake_affine()` 判定，受 `WA_IDLE` / `WA_WEIGHT` / `WA_BIAS` 特性（`features.h:121`）调控。
2. **select_idle_sibling（找空闲兄弟核）**：在共享 LLC 的范围内找一个空闲 CPU 落脚，
   减少唤醒延迟。扫描范围由 `SIS_UTIL` 特性（`features.h:95`）按 LLC 利用率动态限制——
   利用率高时少扫几个，避免在大 LLC 上挨个探测浪费时间。

若以上都不理想，则回退到 `sched_balance_find_dst_cpu()`（`kernel/sched/fair.c:7753`）
在调度域里找一个最空的目标 group/CPU。

> **EUM（能效感知）补充**：在带 EAS（Energy Aware Scheduling）的非对称拓扑上，
> 选核还会参考 `compute_energy()` 选能耗最低的落点（见
> `Documentation/scheduler/sched-energy.rst`），这里不展开。

### 3.10 远程唤醒：TTWU_QUEUE 与 IPI

当唤醒目标在**别的 CPU** 上时，直接去抢对端 rq 锁会造成锁弹跳。`TTWU_QUEUE` 特性
（`features.h:85`，非 RT 内核默认开）改为把唤醒请求**排到目标 CPU 的队列**，
再发一个调度 IPI 让目标 CPU 自己处理，显著降低 `rq->lock` 争用。

### 3.11 本章小结

| 机制 | 入口函数 | 位置 | 触发 |
| --- | --- | --- | --- |
| 周期均衡软中断 | `sched_balance_softirq` | `kernel/sched/fair.c:13333` | tick → SCHED_SOFTIRQ |
| 遍历调度域 | `sched_balance_domains` | `kernel/sched/fair.c:12590` | 软中断 |
| 单域均衡 | `sched_balance_rq` | `kernel/sched/fair.c:12101` | 上者调用 |
| 找最忙 group | `sched_balance_find_src_group` | `kernel/sched/fair.c:11645` | 上者调用 |
| 找最忙 rq | `sched_balance_find_src_rq` | `kernel/sched/fair.c:11783` | 上者调用 |
| 算不平衡 | `calculate_imbalance` | `kernel/sched/fair.c:11443` | 上者调用 |
| 摘/挂任务 | `detach_tasks` / `attach_tasks` | `kernel/sched/fair.c:9904` / `10043` | 上者调用 |
| active balance | `active_load_balance_cpu_stop` | `kernel/sched/fair.c:12431` | 单任务硬迁 |
| newidle balance | `sched_balance_newidle` | `kernel/sched/fair.c:5061` | CPU 将空闲 |
| 唤醒选核 | `select_task_rq_fair` | fair.c | 任务唤醒 |
| 找目标 CPU | `sched_balance_find_dst_cpu` | `kernel/sched/fair.c:7753` | 选核回退 |

> **EEVDF 与负载均衡的关系**：负载均衡主要按 **PELT 负载/利用率**（第 5 章）而非 vruntime
> 来搬任务——它关心的是"算力分布"，不是单核内的"谁先跑"。迁移时通过
> `PLACE_REL_DEADLINE`（2.8）保留任务的相对紧迫度，落到新 CPU 后由 `place_entity`
> 用缓存的 `vlag` 复原公平账。两套机制正交协作。

---

## 第 4 章 组调度与带宽控制

到目前为止，我们把 `sched_entity` 当作"一个任务"。但 EEVDF/CFS 真正的威力在于：
**一个 `sched_entity` 也可以是一整个任务组**。这让"先在用户/容器之间公平，再在
每个用户/容器内部的任务之间公平"成为可能——这就是 **组调度（group scheduling）**，
是 cgroup `cpu` 控制器的内核基础。

### 4.1 为什么需要组调度

设想：用户 A 起了 1 个任务，用户 B 起了 99 个任务。若只在任务级公平，B 会拿走 99% 的 CPU。
组调度让我们先把 CPU 在 A、B 两个**组**之间公平分（各 50%），再在组内部的任务间分。
于是 A 的那个任务独享 50%，B 的 99 个任务平分另外 50%（设计动机见
`Documentation/scheduler/sched-design-CFS.rst:211`）。

### 4.2 层级结构：task_group 与嵌套 cfs_rq

开启 `CONFIG_FAIR_GROUP_SCHED`（`kernel/sched/sched.h:482`）后，引入
`struct task_group`（`kernel/sched/sched.h:474`）：

```c
struct task_group {
	struct cgroup_subsys_state css;
#ifdef CONFIG_FAIR_GROUP_SCHED
	struct sched_entity	**se;       // ★ 每个 CPU 上代表本组的"组实体"
	struct cfs_rq		**cfs_rq;   // ★ 每个 CPU 上本组"拥有"的子队列
	unsigned long		shares;     // 组权重（cpu.weight / cpu.shares）
	atomic_long_t		load_avg ____cacheline_aligned;  // 组的聚合 PELT 负载
#endif
	/* ... rt_bandwidth / scx / cfs_bandwidth ... */
};
```

层级关系（每个 CPU 上各有一份）：

```text
   rq->cfs  (根 cfs_rq)
      │  红黑树里排着：
      ├── task X 的 se
      ├── task Y 的 se
      └── 组 G 的 se   ──┐  (se->my_q 指向 ↓)
                         ▼
                   G 的 cfs_rq  (G.cfs_rq[cpu])
                      │  红黑树里排着：
                      ├── task a 的 se   (se->cfs_rq 指回 ↑)
                      ├── task b 的 se
                      └── 子组 H 的 se ──┐
                                         ▼
                                   H 的 cfs_rq ...
```

两个关键指针（`include/linux/sched.h:603`）：
- **`se->my_q`**：若 `se` 是组实体，它指向"这个组自己拥有的 cfs_rq"（组内任务排在这里）。
  任务实体的 `my_q == NULL`。
- **`se->cfs_rq`**：`se` 被排队进的那个 cfs_rq（即它的父层队列）。
- **`se->parent`**、**`se->depth`**：组层级的父指针与深度。

辅助宏 `for_each_sched_entity(se)`（`kernel/sched/fair.c:314`）就是沿 `se->parent`
**自底向上遍历整条组层级链**——入队/出队/记账几乎所有操作都用它逐层处理。

### 4.3 入队/出队的递归：enqueue_task_fair

回顾第 2 章：`enqueue_entity` 处理"一个实体进一个 cfs_rq"。而组调度下，
一个任务入队意味着**它自己、它的父组、祖父组……只要还没在队，都要逐层入队**。
这就是 `enqueue_task_fair()`（公平类 `enqueue_task` hook）干的事：

```text
enqueue_task_fair(rq, p, flags):
    for_each_sched_entity(se):              // 从任务 se 向上走到根
        if (se->on_rq) break;              // 这一层（及以上）已在队，停
        cfs_rq = cfs_rq_of(se);
        enqueue_entity(cfs_rq, se, flags);  // 把本层实体放进父队列（第 2 章）
        // 更新 h_nr_queued / h_nr_runnable 等层级计数
```

> **直觉**：第一次有任务进入某组，组实体本身也得"进上一层的就绪队列"，
> 这样上一层的 `pick_eevdf` 才可能选到这个组。出队（`dequeue_task_fair`）对称：
> 一个组的最后一个任务离开时，组实体也要从上层出队。

选择时同样逐层（已在 2.13 给出）：`pick_next_task_fair` 在根选出组实体，
顺 `group_cfs_rq(se)`（即 `se->my_q`）下钻，在子队列再跑 `pick_eevdf`，直到选出任务实体。
**每一层都是独立的一次 EEVDF**——这正是"先组间公平、再组内公平"的实现。

### 4.4 组权重的动态分配：update_cfs_group

`cpu.weight`（cgroup v2）/ `cpu.shares`（v1）设定的是**整个组在所有 CPU 上的总权重**。
但任务是分散在各 CPU 上的，每个 CPU 上的"组实体"应该分到总权重的多少？
这由 `update_cfs_group()`（`kernel/sched/fair.c:4243`）动态计算：

- 组实体在某 CPU 上的有效权重 ≈ `tg->shares × (本 CPU 上组的负载占比)`。
- 即：组里活跃任务多的那个 CPU，该组在那个 CPU 上的组实体权重更大。
- 这保证"无论组内任务怎么在 CPU 间分布，整个组占用的总 CPU 份额都符合 shares 设定"。

`update_cfs_group` 在 enqueue/dequeue/tick 时被反复调用（见 2.9、2.11 的代码），
用 `reweight_entity()` 即时调整组实体权重并更新所在队列的总 `load`。

### 4.5 CFS 带宽控制（CFS Bandwidth / cpu.max）

`shares/weight` 只控制**相对**份额（按比例分）。但有时需要**绝对**上限：
"这个容器最多用 2 个 CPU 的算力，哪怕机器空着也不许超"。这就是
**CFS 带宽控制**（CFS bandwidth controller，`CONFIG_CFS_BANDWIDTH`），
对应 cgroup v2 的 `cpu.max`（`quota period` 两个值），文档见
`Documentation/scheduler/sched-bwc.rst`。

#### 4.5.1 数据结构 cfs_bandwidth

`struct cfs_bandwidth`（`kernel/sched/sched.h:447`）：

```c
struct cfs_bandwidth {
	raw_spinlock_t		lock;
	ktime_t			period;       // ★ 计费周期（cpu.max 第二个值，默认 100ms）
	u64			quota;        // ★ 每周期配额（cpu.max 第一个值）
	u64			runtime;      // ★ 本周期全局剩余可分配运行时间
	u64			burst;        // 允许的突发额度
	s64			hierarchical_quota;
	struct hrtimer		period_timer; // ★ 周期补充配额的高精度定时器
	struct hrtimer		slack_timer;  // 回收零散剩余配额
	struct list_head	throttled_cfs_rq;  // 被限流的 cfs_rq 链表
	/* 统计：nr_periods / nr_throttled / throttled_time ... */
};
```

每个 `cfs_rq` 则持有本地配额（`kernel/sched/sched.h:752`）：
`runtime_enabled`（是否启用）、`runtime_remaining`（本地剩余配额）、
`throttled`（是否已被限流）、`throttle_count`、`throttled_list`。

#### 4.5.2 配额消费与本地补充

任务运行时，`update_curr`（2.5）末尾调 `account_cfs_rq_runtime()`，
其实现 `__account_cfs_rq_runtime()`（`kernel/sched/fair.c:5957`）：

```c
static void __account_cfs_rq_runtime(struct cfs_rq *cfs_rq, u64 delta_exec)
{
	cfs_rq->runtime_remaining -= delta_exec;   // 扣减本地配额

	if (likely(cfs_rq->runtime_remaining > 0))
		return;
	if (cfs_rq->throttled)
		return;
	/* 本地配额耗尽，尝试从全局 cfs_b 再领一片；领不到就 resched，
	 * 让该层级在下一个调度点被限流 */
	if (!assign_cfs_rq_runtime(cfs_rq) && likely(cfs_rq->curr))
		resched_curr(rq_of(cfs_rq));
}
```

本地配额是**一片一片**从全局领的，每次领 `sched_cfs_bandwidth_slice_us`
（默认 5ms，`kernel/sched/fair.c:137`，可调）。`__assign_cfs_rq_runtime()`
（`kernel/sched/fair.c:5917`）从 `cfs_b->runtime` 切一片给 `cfs_rq->runtime_remaining`。
这种"批发-零售"设计减少了对全局 `cfs_b->lock` 的争用。

#### 4.5.3 限流 throttle

当本地配额耗尽且全局也没得领时，在 `put_prev_entity`（2.10）调用的
`check_cfs_rq_runtime()`（`kernel/sched/fair.c:6711`）触发限流：

```c
static bool check_cfs_rq_runtime(struct cfs_rq *cfs_rq)
{
	if (!cfs_bandwidth_used())
		return false;
	if (likely(!cfs_rq->runtime_enabled || cfs_rq->runtime_remaining > 0))
		return false;
	if (cfs_rq_throttled(cfs_rq))
		return true;
	return throttle_cfs_rq(cfs_rq);          // ★ 真正限流
}
```

`throttle_cfs_rq()`（`kernel/sched/fair.c:6239`）：

```c
static bool throttle_cfs_rq(struct cfs_rq *cfs_rq)
{
	struct cfs_bandwidth *cfs_b = tg_cfs_bandwidth(cfs_rq->tg);
	int dequeue = 1;

	raw_spin_lock(&cfs_b->lock);
	if (__assign_cfs_rq_runtime(cfs_b, cfs_rq, 1)) {  // 最后再抢 1ns，抢到就不限流
		dequeue = 0;
	} else {
		list_add_tail_rcu(&cfs_rq->throttled_list, &cfs_b->throttled_cfs_rq);
	}
	raw_spin_unlock(&cfs_b->lock);

	if (!dequeue)
		return false;

	/* 冻结整条层级的 runnable 平均，并把组实体从上层队列摘除 */
	walk_tg_tree_from(cfs_rq->tg, tg_throttle_down, tg_nop, (void *)rq);
	cfs_rq->throttled = 1;
	return true;
}
```

被限流后，该 `cfs_rq` 的任务**整体停跑**（组实体从上层队列移除，`pick_eevdf` 再也选不到它），
直到下个周期配额补充才解冻。注意限流是**层级冻结**：`tg_throttle_down` 沿子树
把整个组层级的 PELT 时钟也冻住，避免被限流期间负载被错误衰减。

#### 4.5.4 周期补充与解限流 unthrottle

`period_timer` 每个 `period` 触发 `sched_cfs_period_timer()`（`kernel/sched/fair.c:6739`），
调 `do_sched_cfs_period_timer()` → `__refill_cfs_bandwidth_runtime()`（`kernel/sched/fair.c:5893` 附近）
把 `cfs_b->runtime` 重置为 `quota`（+burst 上限），并 `distribute_cfs_runtime()`
给 `throttled_cfs_rq` 链表上的队列分配新配额、调 `unthrottle_cfs_rq()`（`kernel/sched/fair.c:6280`）解限流：

```c
void unthrottle_cfs_rq(struct cfs_rq *cfs_rq)
{
	...
	cfs_rq->throttled = 0;
	/* 累计被限流时长（统计/可观测用）*/
	cfs_b->throttled_time += rq_clock(rq) - cfs_rq->throttled_clock;
	list_del_rcu(&cfs_rq->throttled_list);

	/* 沿层级解冻、把组实体重新挂回上层队列 */
	walk_tg_tree_from(cfs_rq->tg, tg_nop, tg_unthrottle_up, (void *)rq);
	...
	if (rq->curr == rq->idle && rq->cfs.nr_queued)
		resched_curr(rq);          // 必要时唤醒空闲 CPU 来跑解限流的任务
}
```

`__refill_cfs_bandwidth_runtime()`（`kernel/sched/fair.c:5893`）的核心：

```c
	cfs_b->runtime += cfs_b->quota;
	runtime = cfs_b->runtime_snap - cfs_b->runtime;
	if (runtime > 0) {                       // 上周期没用完的算作 burst 统计
		cfs_b->burst_time += runtime;
		cfs_b->nr_burst++;
	}
	cfs_b->runtime = min(cfs_b->runtime, cfs_b->quota + cfs_b->burst);  // 封顶
	cfs_b->runtime_snap = cfs_b->runtime;
```

#### 4.5.5 配额回收 slack

任务出队时若本地还剩较多配额（`return_cfs_rq_runtime()`，在 `dequeue_entity` 末尾调用），
会把多余的还给全局 `cfs_b`，并由 `slack_timer` 异步回收，避免配额"沉淀"在
空闲队列里导致别处领不到。

### 4.6 SCHED_IDLE 组与 idle

组也可以被标记为 idle（`tg->idle`，`cgroup` 的 `cpu.idle`）。`cfs_rq_is_idle()` /
`se_is_idle()`（`kernel/sched/fair.c:467`）让 idle 组的实体在竞争中权重极低，
仅在没有正常任务时才跑——比 nice 19 还弱，但又不是真正的 idle 类，避免优先级反转死锁
（`Documentation/scheduler/sched-design-CFS.rst:139`）。

### 4.7 本章小结

| 概念 | 结构/函数 | 位置 |
| --- | --- | --- |
| 任务组 | `struct task_group` | `kernel/sched/sched.h:474` |
| 组层级遍历 | `for_each_sched_entity` | `kernel/sched/fair.c:314` |
| 递归入队 | `enqueue_task_fair` | fair.c（调 `enqueue_entity` 5523） |
| 组权重分配 | `update_cfs_group` | `kernel/sched/fair.c:4243` |
| 带宽结构 | `struct cfs_bandwidth` | `kernel/sched/sched.h:447` |
| 配额消费 | `__account_cfs_rq_runtime` | `kernel/sched/fair.c:5957` |
| 本地领配额 | `__assign_cfs_rq_runtime` | `kernel/sched/fair.c:5917` |
| 触发限流 | `check_cfs_rq_runtime` | `kernel/sched/fair.c:6711` |
| 限流 | `throttle_cfs_rq` | `kernel/sched/fair.c:6239` |
| 解限流 | `unthrottle_cfs_rq` | `kernel/sched/fair.c:6280` |
| 周期补充 | `__refill_cfs_bandwidth_runtime` | `kernel/sched/fair.c:5893` |
| 周期定时器 | `sched_cfs_period_timer` | `kernel/sched/fair.c:6739` |

用户态接口对应关系：

| cgroup v2 文件 | 内核字段 | 含义 |
| --- | --- | --- |
| `cpu.weight` | `tg->shares` | 相对份额（比例分配） |
| `cpu.max` = `quota period` | `cfs_b->quota` / `cfs_b->period` | 绝对上限（每 period 最多 quota） |
| `cpu.max.burst` | `cfs_b->burst` | 允许的突发额度 |
| `cpu.idle` | `tg->idle` | 标记为 idle 组 |
| `cpu.stat` | `nr_periods`/`nr_throttled`/`throttled_time` | 限流统计 |

---

## 第 5 章 PELT 与可观测 / 调优

前面多次提到"负载"和 `update_load_avg`。本章讲清楚负载到底**怎么量化**（PELT），
以及整套调度器有哪些**可观测、可调优**的旋钮。

### 5.1 为什么需要 PELT

vruntime 解决"谁先跑"，但**负载均衡**和**调频（cpufreq）**需要另一个问题的答案：
"这个任务/这个 CPU 到底有多忙？"——而且要的是**有历史记忆的平滑值**，不能只看瞬时。

**PELT（Per-Entity Load Tracking，每实体负载跟踪）**就是答案：它为每个 `sched_entity`
和每个 `cfs_rq` 维护一个**几何衰减的负载/利用率平均**，新近的活动权重高，久远的指数衰减。

### 5.2 数学模型：几何级数衰减

PELT 把时间切成约 1ms（1024µs）的段，第 N 段记为 p_N（p_0 是当前段）。
设 u_i 是任务在 p_i 段中处于 runnable 的比例，则历史负载表示为几何级数
（`kernel/sched/pelt.c:152` 注释）：

```text
  load = u_0 + u_1*y + u_2*y^2 + u_3*y^3 + ...
```

关键选择 **y^32 = 0.5**，即 **32ms 前的贡献衰减到一半**（`LOAD_AVG_PERIOD = 32`）。
这给了负载一个约 32ms 的"半衰期"记忆窗。当时间推进一段，只需把旧和乘以 y 再加新段——
O(1) 更新。`LOAD_AVG_MAX ≈ 47742`（geometric series 1024·Σy^n 的收敛值，约对应满载）。

### 5.3 衰减实现：decay_load

`decay_load()`（`kernel/sched/pelt.c:32`）算 `val * y^n`，用查表做到常数时间：

```c
static u64 decay_load(u64 val, u64 n)
{
	if (unlikely(n > LOAD_AVG_PERIOD * 63))
		return 0;                          // 衰减到可忽略，直接归零
	...
	if (unlikely(local_n >= LOAD_AVG_PERIOD)) {
		val >>= local_n / LOAD_AVG_PERIOD; // 每 32 段 → 右移 1 位（×0.5）
		local_n %= LOAD_AVG_PERIOD;
	}
	/* 余下 <32 段用查表 runnable_avg_yN_inv[] 做 y^(n%32) */
	val = mul_u64_u32_shr(val, runnable_avg_yN_inv[local_n], 32);
	return val;
}
```

巧思：`y^n = (y^32)^(n/32) · y^(n%32) = (1/2)^(n/32) · y^(n%32)`，
前半用移位、后半用 32 项查表，避免任何浮点或循环。

### 5.4 累加三段：accumulate_sum

跨段时把历史贡献分三部分累加（`kernel/sched/pelt.c:102`，注释里的 d1/d2/d3 图很直观）：

```text
          d1          d2           d3
          ^           ^            ^
        |<->|<----------------->|<--->|
... |---x---|------| ... |------|-----x (now)

  u' = (u + d1) y^p + 1024·Σ y^n + d3·y^0
```

- `d1`：上一个不完整段的剩余；
- `d2`：中间若干完整段（用 `__accumulate_pelt_segments` 的闭式求和）；
- `d3`：当前不完整段。

`accumulate_sum()` 据此更新 `load_sum`/`runnable_sum`/`util_sum`，
再由 `___update_load_avg()`（`kernel/sched/pelt.c:257`）除以 divider 得到对外的
`load_avg`/`runnable_avg`/`util_avg`。

### 5.5 三个量：load / runnable / util

`struct sched_avg`（`include/linux/sched.h:510`）：

```c
struct sched_avg {
	u64	last_update_time;
	u64	load_sum;        // 加权负载累加和
	u64	runnable_sum;    // 可运行累加和
	u32	util_sum;        // 利用率累加和
	u32	period_contrib;  // 当前段已过的部分
	unsigned long	load_avg;      // ★ 负载平均（含权重）
	unsigned long	runnable_avg;  // ★ 可运行平均（含排队等待）
	unsigned long	util_avg;      // ★ 利用率平均（只看"在 CPU 上跑"）
	unsigned int	util_est;      // ★ 利用率估计（UTIL_EST）
};
```

三者语义不同（`kernel/sched/pelt.c:270` 注释精确定义）：

| 量 | 含义 | 主要用途 |
| --- | --- | --- |
| `load_avg` | runnable 比例 × **权重**（nice 影响） | 负载均衡的 `migrate_load` |
| `runnable_avg` | runnable（含**排队等待**）比例 | 判断 CPU 是否过载（任务在排队） |
| `util_avg` | 实际**占用 CPU**（running）比例，0~1024 | 调频（schedutil）、misfit、选核 |

- 任务级：`__update_load_avg_se()`（`kernel/sched/pelt.c:307`）。
- 队列级：`__update_load_avg_cfs_rq()`（`kernel/sched/pelt.c:321`），是其上实体之和。
- 阻塞态：`__update_load_avg_blocked_se()`（`kernel/sched/pelt.c:296`）——
  任务睡了，负载继续衰减（"blocked load"），所以刚睡的任务仍短暂计入负载，避免均衡抖动。

### 5.6 util_est —— 利用率估计

纯 PELT 的 `util_avg` 对"间歇性突发"任务反应慢（衰减需要时间）。`UTIL_EST` 特性
（`features.h:125`）额外记住任务**出队时的 util 峰值**，下次入队直接用这个估计值，
让调频和选核**更快**响应突发负载，避免"任务一跑就卡、跑一会才升频"。

### 5.7 PELT 与调频：schedutil

`util_avg`/`util_est` 经 `cpufreq_update_util()` 喂给 **schedutil** governor
（`Documentation/scheduler/schedutil.rst`），它据此调整 CPU 频率——
"调度器知道每个任务多忙，所以它最适合决定该跑多快"。这是 PELT 在节能/性能上的直接价值。

`NONTASK_CAPACITY` 特性（`features.h:76`）则把"花在中断、RT、DL 上的时间"从 CPU 可用算力里扣除，
让 CFS 看到的 capacity 更真实。

### 5.8 调度特性开关：features.h

`kernel/sched/features.h` 是一组 `SCHED_FEAT(NAME, default)` 宏，可在运行时经
`/sys/kernel/debug/sched/features` 动态开关（写 `NAME` 开、`NO_NAME` 关）。
按主题归类（默认值见 `features.h`）：

**EEVDF 行为类：**

| 特性 | 默认 | 作用 |
| --- | --- | --- |
| `PLACE_LAG` | on | 入队复原睡前 lag（2.8） |
| `PLACE_DEADLINE_INITIAL` | on | 新任务给半个 slice 温和入场 |
| `PLACE_REL_DEADLINE` | on | 迁移保留相对 deadline |
| `RUN_TO_PARITY` | on | 抑制唤醒抢占直到 0-lag 点（2.7） |
| `PREEMPT_SHORT` | on | 短 slice 任务可取消 RUN_TO_PARITY |
| `DELAY_DEQUEUE` | on | 延迟出队烧负 lag（2.9.2） |
| `DELAY_ZERO` | on | 出队/唤醒时把 lag 削到 0 |

**唤醒/buddy 类：**

| 特性 | 默认 | 作用 |
| --- | --- | --- |
| `NEXT_BUDDY` | off | 偏向调度刚唤醒的任务（cache 局部性） |
| `PICK_BUDDY` | on | `pick_eevdf` 允许走 ->next 捷径 |
| `CACHE_HOT_BUDDY` | on | buddy 视为 cache 热，减少被迁走 |
| `WAKEUP_PREEMPTION` | on | 允许唤醒时抢占 |

**负载均衡/选核类：**

| 特性 | 默认 | 作用 |
| --- | --- | --- |
| `SIS_UTIL` | on | 按 LLC 利用率限制选空闲核的扫描 |
| `WA_IDLE`/`WA_WEIGHT`/`WA_BIAS` | on | wake-affine 调控 |
| `UTIL_EST` | on | 用利用率估计 |
| `TTWU_QUEUE` | on(非RT) | 远程唤醒排队 + IPI |
| `NI_RANDOM`/`NI_RATE` | on | newidle 均衡按成功率随机/限频 |
| `ATTACH_AGE_LOAD` | on | 迁移时考虑任务负载年龄 |

### 5.9 sysctl 调优接口（/proc/sys/kernel/）

| sysctl | 默认 | 定义位置 | 作用 |
| --- | --- | --- | --- |
| `sched_cfs_bandwidth_slice_us` | 5000 | `kernel/sched/fair.c:137` | 本地从全局领配额的片大小（4.5.2） |
| `sched_schedstats` | 0 | `kernel/sched/core.c:4633` | 开启调度统计（schedstats） |
| `sched_util_clamp_min` / `_max` | — | `kernel/sched/core.c:4644` | 全局 uclamp 范围 |
| `sched_util_clamp_min_rt_default` | — | `kernel/sched/core.c:4658` | RT 任务默认 uclamp_min |
| `numa_balancing` | — | `kernel/sched/core.c:4667` | 自动 NUMA 均衡开关 |

> 注意：经典 CFS 时代的 `sched_latency_ns`、`sched_min_granularity_ns`、
> `sched_wakeup_granularity_ns` 等已**不复存在**——EEVDF 用 `slice` 取代了这套 granularity 旋钮。
> 现在的核心旋钮是 `base_slice_ns`（见下）和每任务的 `sched_setattr(sched_runtime)`。

### 5.10 debugfs 调优 / 观测接口（/sys/kernel/debug/sched/）

定义见 `kernel/sched/debug.c`：

| 接口 | 位置 | 作用 |
| --- | --- | --- |
| `features` | `debug.c:600` | 运行时开关调度特性（5.8） |
| `base_slice_ns` | `debug.c:606` | ★ EEVDF 核心旋钮：默认 slice（700µs） |
| `migration_cost_ns` | `debug.c:612` | cache 热判定阈值（默认 500µs，3.6） |
| `nr_migrate` | `debug.c:613` | 单次均衡最多搬几个任务 |
| `latency_warn_ms` | `debug.c:608` | 延迟告警阈值（配合 `LATENCY_WARN`） |
| `verbose` / `preempt` | `debug.c:601` / `603` | 调度域调试详细度 / 动态抢占模式 |
| `debug` | `debug.c:629` | 转储调度器详细状态（每任务 vruntime/deadline/lag 等） |
| `fair_server/` | `debug.c:555` | fair_server（DL 兜底）调试 |

> **`/sys/kernel/debug/sched/debug` 是分析 EEVDF 的利器**：它逐 CPU、逐任务打印
> `vruntime`、`deadline`、`vlag`、`slice`、负载等，能直接观察到本文讲的所有量在运行时的真实值。

### 5.11 tracepoints —— 动态追踪

定义见 `include/trace/events/sched.h`，可用 `trace-cmd`/`perf sched`/`ftrace`/BPF 挂载：

| tracepoint | 用途 |
| --- | --- |
| `sched_switch` | 上下文切换（谁换下、谁换上、状态） |
| `sched_wakeup` / `sched_wakeup_new` | 任务唤醒 / 新任务唤醒 |
| `sched_migrate_task` | 任务跨 CPU 迁移（负载均衡观测） |
| `sched_stat_runtime` | 任务运行时间记账（来自 `update_se`） |
| `sched_process_fork` / `_exit` | fork / 退出 |
| `sched_pi_setprio` | 优先级继承（PI） |
| 内部 `sched_pelt_*` / `pelt_cfs_tp` | PELT 负载变化（`kernel/sched/pelt.c:300` 等） |

配套统计文档：`Documentation/scheduler/sched-stats.rst`（/proc/schedstat 字段含义）、
`Documentation/scheduler/sched-debug.rst`（debugfs 接口说明）。

### 5.12 本章小结

- **PELT** 用几何衰减（y^32=0.5，半衰期 ~32ms）把"任务/队列多忙"量化成
  `load_avg`/`runnable_avg`/`util_avg` 三个有记忆的平滑值，是**负载均衡**和**调频**的数据基础，
  与 EEVDF 的 vruntime/deadline **正交**（一个管"多忙/搬哪个"，一个管"先跑谁"）。
- 核心代码：`decay_load`（`pelt.c:32`）、`accumulate_sum`（`pelt.c:102`）、
  `___update_load_avg`（`pelt.c:257`）、`__update_load_avg_se/_cfs_rq`（`pelt.c:307`/`321`）。
- 调优三件套：`features`（行为开关）、`base_slice_ns`（默认时间片）、
  `sched_setattr(sched_runtime)`（每任务自定义 slice）。
- 观测三件套：`/sys/kernel/debug/sched/debug`、tracepoints、`/proc/schedstat`。

---

## 第 6 章 端到端实例串讲

前五章把零件逐个拆开了。本章把它们装回去，跟着**一个普通任务的一生**走一遍，
把所有概念串成一条时间线。

### 6.1 出生：fork

```text
父进程 fork()
  └─ sched_fork() / __sched_fork()        // 设置调度类、初始 nice/weight
       ├─ se->slice 继承父进程（custom_slice）或置 0 待入队时取默认
       └─ init_entity_runnable_average()  // PELT：新任务按满载初始化 (fair.c:1270)
  └─ sched_cgroup_fork()                   // 归入父进程的 task_group
       └─ task_fork_fair()                 // 本版本仅 set_task_max_allowed_capacity (fair.c:13714)
  └─ wake_up_new_task()
       └─ post_init_entity_util_avg()      // PELT util 外推 (fair.c:1315)
       └─ activate_task() → enqueue_task_fair(..., ENQUEUE_INITIAL | ENQUEUE_NEW)
            └─ for_each_sched_entity: enqueue_entity() 逐层入队 (fair.c:5523)
                 └─ place_entity(cfs_rq, se, flags)        // fair.c:5380
                      └─ PLACE_DEADLINE_INITIAL: vslice/=2，只给半个 slice
                         se->vruntime = avg_vruntime() - lag
                         se->deadline = se->vruntime + vslice/2
                 └─ __enqueue_entity() 插入红黑树（按 deadline 排序）
       └─ wakeup_preempt() → check_preempt_wakeup_fair()
            └─ 若新任务 deadline 比当前早且未被 RUN_TO_PARITY 挡住 → resched
```

要点：新任务以**当前平均 V 附近**落点（保证不会凭空获得巨大优势/劣势），
且 `PLACE_DEADLINE_INITIAL` 只给半个 slice，"温和入场"。注意本版本的
`task_fork_fair()`（`kernel/sched/fair.c:13714`）已被精简为只设置 `max_allowed_capacity`，
真正的初始定位（`place_entity` + `ENQUEUE_INITIAL`）发生在 `wake_up_new_task` 的入队路径里。

### 6.2 被选中：上 CPU

```text
时钟中断 / 主动让出 → 需要重新调度
schedule() → __schedule()                 // core.c:7017
  └─ pick_next_task(rq, prev, &rf)
       └─ pick_next_task_fair()            // fair.c:9234
            ├─ (组调度) 逐层 pick：
            │    cfs_rq = &rq->cfs
            │    se = pick_next_entity() → pick_eevdf()   // fair.c:1136 ★
            │       ├─ 单实体快路径？PICK_BUDDY 捷径？
            │       ├─ curr 还在保护期(protect_slice)？→ 留 curr (RUN_TO_PARITY)
            │       └─ 否则增广树堆搜索：eligible 集合里取 deadline 最早
            │    若 se 是组实体 → cfs_rq = se->my_q，继续下钻
            └─ set_next_entity(cfs_rq, se, first=true)    // fair.c:5729
                 ├─ __dequeue_entity(se)  // 从树摘出，存入 cfs_rq->curr
                 └─ set_protect_slice()   // 设 vprot 保护窗
  └─ context_switch(rq, prev, next)        // 真正切到我们的任务
```

### 6.3 运行中：被记账与可能被抢占

```text
我们的任务在 CPU 上跑……
每个 tick：
  sched_tick()
    └─ task_tick_fair() → entity_tick()    // fair.c:13687 / 5821
         └─ update_curr()                  // fair.c:1407 ★
              ├─ delta_exec = update_se()  // 这段真实运行时间
              ├─ vruntime += calc_delta_fair(delta_exec)   // 按权重推进
              ├─ resched = update_deadline()               // slice 满则推进 deadline
              ├─ account_cfs_rq_runtime()  // 带宽配额扣减 (第4章)
              └─ if (resched || !protect_slice) resched_curr_lazy()  // 该让位了

别的任务被唤醒：
  try_to_wake_up() → enqueue → check_preempt_wakeup_fair()
       └─ 它 deadline 更早 + (PREEMPT_SHORT 取消我们的保护) → 抢占我们
```

任务的 `vruntime` 一路按权重爬升，`deadline` 每跑满一个 slice 就向后推一次。
当它的 deadline 不再是 eligible 集合里最早的，或被更急的任务抢占，就轮到下 CPU。

### 6.4 让出：下 CPU 或睡眠

**情况 A：时间片用完被抢，但仍可运行**

```text
__schedule():
  └─ put_prev_task_fair() → put_prev_entity()   // fair.c:5798
       ├─ update_curr()       // 最后结算
       ├─ check_cfs_rq_runtime()  // 带宽：超额则 throttle (第4章)
       └─ __enqueue_entity(prev)  // 放回红黑树，带着更靠后的 deadline 排队
```

**情况 B：任务调用 read()/sleep() 等进入睡眠**

```text
__schedule() 检测到 prev 状态非 RUNNING:
  └─ deactivate_task() → dequeue_task_fair() → dequeue_entity()  // fair.c:5646
       ├─ update_curr()
       ├─ if (DELAY_DEQUEUE && lag<0): set_delayed(); return  // 延迟出队烧负 lag
       ├─ update_entity_lag()  // ★ 算 lag 缓存进 se->vlag（供醒来复原）
       ├─ PLACE_REL_DEADLINE: deadline 转相对形式
       └─ __dequeue_entity()   // 真正出树
```

关键：**出队时把 lag 算好缓存进 `se->vlag`**。这样无论睡多久，醒来时
`place_entity` 都能复原它的公平账——这就是 EEVDF "跨睡眠保留 lag" 的闭环。

### 6.5 醒来：唤醒与（可能的）跨 CPU 迁移

```text
数据就绪，任务被唤醒：
  try_to_wake_up()
    └─ select_task_rq_fair()               // fair.c:8829
         ├─ wake_affine(): 倾向唤醒者附近的 CPU（cache 局部性）
         └─ select_idle_sibling(): LLC 内找空闲核（SIS_UTIL 限制扫描）
       → 选定目标 CPU（可能与睡前不同 → 迁移）
    └─ enqueue_task_fair() → enqueue_entity()
         └─ place_entity()                 // fair.c:5380 ★
              ├─ PLACE_LAG: 用缓存的 se->vlag 复原 vruntime 落点
              │    （并按 (W+w_i)/W 放大 lag 抵消加入新实体对 V 的扰动）
              ├─ PLACE_REL_DEADLINE: 相对 deadline 加回 vruntime 复原
              └─ se->deadline = se->vruntime + vslice
    └─ check_preempt_wakeup_fair(): 决定是否立刻抢占目标 CPU 的当前任务
```

一圈下来，任务回到 6.2 的状态，循环往复，直到退出。

### 6.6 死亡：exit

```text
任务 exit():
  └─ dequeue_task_fair()        // 最后一次出队
  └─ task_dead_fair()           // 从 PELT 的 removed 累加里扣除其残余负载
       （blocked load 会随时间在 cfs_rq 上自然衰减干净）
```

### 6.7 一张图总览任务生命周期

```text
        fork                                                exit
         │                                                   │
         ▼                                                   ▼
   ┌──────────┐  enqueue   ┌──────────┐  pick_eevdf  ┌──────────┐
   │  新建    │──────────► │ 就绪(树中)│────────────►│ 运行(curr)│
   │place_ent │            │ 按deadline│   set_next   │update_curr│
   └──────────┘            │   排序    │              │ 推进vrt   │
                           └──────────┘              └──────────┘
                              ▲   ▲                     │     │
                   place_entity│   │put_prev     被抢/片满│     │睡眠
                   (复原lag)   │   └─────────────────────┘     │dequeue
                              │                                 │(缓存lag)
                           ┌──────────┐  wake + select_rq      ▼
                           │ 睡眠(出树)│◄──────────────────────┘
                           │ vlag缓存 │
                           └──────────┘
```

---

## 第 7 章 调度核心与高级机制源码走读

前六章建立了完整的概念框架。本章回到源码，把几个**贯穿全局但前面只点到为止**的
机制完整读一遍：调度主循环 `__schedule` 的真实代码、组调度下 `pick_next_task_fair`
的"最小化切换"技巧、改权重时的 `reweight_entity`、负载均衡的 `can_migrate_task` 与
层级负载 `h_load`，以及 proxy-exec、NUMA 均衡、core scheduling、sched_ext、hrtick 等
高级特性。

### 7.1 account_entity_enqueue / dequeue —— 队列权重记账

第 2 章的 `enqueue_entity`/`dequeue_entity` 末尾都调用了"记账"函数，维护队列总权重
和任务链表。`account_entity_enqueue()`（`kernel/sched/fair.c:3875`）：

```c
static void account_entity_enqueue(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	update_load_add(&cfs_rq->load, se->load.weight);   // 队列总权重 += se 权重
	if (entity_is_task(se)) {
		struct rq *rq = rq_of(cfs_rq);
		account_numa_enqueue(rq, task_of(se));         // NUMA 统计（7.7）
		list_add(&se->group_node, &rq->cfs_tasks);     // 挂入 rq->cfs_tasks 链表
	}
	cfs_rq->nr_queued++;                                   // 在队计数 +1
}

static void account_entity_dequeue(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	update_load_sub(&cfs_rq->load, se->load.weight);   // 对称：总权重 -= se 权重
	if (entity_is_task(se)) {
		account_numa_dequeue(rq_of(cfs_rq), task_of(se));
		list_del_init(&se->group_node);
	}
	cfs_rq->nr_queued--;
}
```

两个产物很关键：
- **`cfs_rq->load`（队列总权重）**：`avg_vruntime`/`vruntime_eligible` 里的 `Σ w_i` 就来自它，
  也是组调度 `update_cfs_group` 分配份额、负载均衡比较的依据。
- **`rq->cfs_tasks` 链表**：负载均衡的 `detach_tasks`（3.5）正是遍历这个链表来挑可迁移任务的。
  注意它挂的是**任务级**实体（`entity_is_task`），组实体不进此链表。

### 7.1b enqueue_task_fair / dequeue_task_fair —— 任务级入队/出队封装

第 2.9 讲的 `enqueue_entity` 是"一个实体进一个队列"。但调度核心调用的是**任务级**的
`enqueue_task_fair()`（`kernel/sched/fair.c:7197`）——它负责沿组层级**逐层**调用
`enqueue_entity`，并维护层级计数 `h_nr_queued`/`h_nr_runnable`/`h_nr_idle`：

```c
static void enqueue_task_fair(struct rq *rq, struct task_struct *p, int flags)
{
	struct sched_entity *se = &p->se;
	int task_new = !(flags & ENQUEUE_WAKEUP);
	...
	/* ① 先把任务估计利用率加进 cfs_rq，再后续触发调频（schedutil 用） */
	if (!p->se.sched_delayed || (flags & ENQUEUE_DELAYED))
		util_est_enqueue(&rq->cfs, p);

	if (flags & ENQUEUE_DELAYED) {       // ② 延迟态任务被唤醒 → 走复活路径
		requeue_delayed_entity(se);
		return;
	}

	if (p->in_iowait)                    // ③ iowait 任务显式触发调频（提频降延迟）
		cpufreq_update_util(rq, SCHED_CPUFREQ_IOWAIT);

	/* ④ 自底向上逐层入队，直到遇到已在队的祖先 */
	for_each_sched_entity(se) {
		if (se->on_rq) {
			if (se->sched_delayed)
				requeue_delayed_entity(se);   // 半路遇到延迟态祖先，复活它
			break;                            // 这层及以上已在队，停
		}
		cfs_rq = cfs_rq_of(se);
		if (slice) {                          // 组实体 slice = 子队列最小 slice
			se->slice = slice;
			se->custom_slice = 1;
		}
		enqueue_entity(cfs_rq, se, flags);    // ★ 第 2.9 的实体级入队
		slice = cfs_rq_min_slice(cfs_rq);
		/* 更新本层 h_nr_queued / h_nr_runnable / h_nr_idle ... */
	}
	...
}
```

几个细节：
- **iowait 提频**（第③点）：刚结束 I/O 等待的任务被唤醒时显式提频，是降低 I/O 密集型
  负载延迟的常见手段（"任务在等盘，盘好了赶紧用高频把它伺候完"）。
- **组实体的 slice**（第④点）：组实体的 slice 取其子队列的最小 slice
  （`cfs_rq_min_slice`），保证组能在"组内最急任务"要求的时间框架内被服务到。
- **`h_nr_*` 层级计数**：每层维护"我这棵子树里有多少在队/可运行/idle 任务"，
  供负载均衡、限流、nr_running 等快速查询。

`dequeue_task_fair()` 对称：逐层 `dequeue_entity`，处理 DELAY_DEQUEUE
（某层实体若 lag<0 且该层还有别的任务则停在延迟态），并对称维护 `h_nr_*`。

> **fair_server 提示**：`enqueue_task_fair` 在第一个任务入队时还会
> `dl_server_start(&rq->fair_server)`（`kernel/sched/fair.c:7286`）——见 7.2b。

### 7.2 reweight_entity —— 改 nice/权重时如何不破坏公平

当任务 `renice`、或组 `cpu.weight` 变化时，实体权重要改。但**权重一改，`vruntime` 的
推进速率、lag、deadline 全都受影响**——必须小心地"先卸账、改权重、再装账"，
否则公平性立刻被破坏。这由 `reweight_entity()`（`kernel/sched/fair.c:4068`）完成：

```c
static void reweight_entity(struct cfs_rq *cfs_rq, struct sched_entity *se,
			    unsigned long weight)
{
	bool curr = cfs_rq->curr == se;
	u64 avruntime = 0;

	if (se->on_rq) {
		update_curr(cfs_rq);                       // ① 先结清旧权重下的运行时间
		avruntime = avg_vruntime(cfs_rq);
		se->vlag = entity_lag(cfs_rq, se, avruntime);  // ② 用旧权重算好当前 lag
		se->deadline -= avruntime;                 // ③ deadline/vprot 转成相对量
		se->rel_deadline = 1;
		...
		cfs_rq->nr_queued--;
		if (!curr)
			__dequeue_entity(cfs_rq, se);      // ④ 暂时摘出树
		update_load_sub(&cfs_rq->load, se->load.weight);
	}
	dequeue_load_avg(cfs_rq, se);

	rescale_entity(se, weight, rel_vprot);             // ⑤ 按新旧权重比缩放 lag/deadline
	update_load_set(&se->load, weight);                // ⑥ 真正写入新权重
	... // 重算 PELT load_avg
	enqueue_load_avg(cfs_rq, se);

	if (se->on_rq) {
		se->deadline += avruntime;                 // ⑦ 相对量加回绝对基准，复原
		se->rel_deadline = 0;
		se->vruntime = avruntime - se->vlag;       // ⑧ 用保留的 lag 复原 vruntime 落点
		update_load_add(&cfs_rq->load, se->load.weight);
		if (!curr)
			__enqueue_entity(cfs_rq, se);      // ⑨ 用新权重重新入树
		cfs_rq->nr_queued++;
	}
}
```

这套"卸-改-装"的关键在于 **lag 守恒**：第②步用旧权重算好 lag 并缓存，
第⑧步用这个 lag 在新权重下复原 `vruntime = V - vlag`。于是任务在改权重前后**保持相同的
公平账（lag）**，只是后续推进速率随新权重改变。这与 `place_entity`（2.8）跨睡眠保留 lag
是同一种思想的不同应用场景。

> `reweight_task_fair()`（`kernel/sched/fair.c:4118`）是它对任务的封装，
> `renice`/`sched_setattr` 改 nice 时经此进入。组权重变化则经 `update_cfs_group` →
> `reweight_entity` 即时调整组实体权重。

### 7.2b fair_server —— 防止公平任务被实时任务饿死

回顾调度类优先级：`stop → dl → rt → fair → idle`。这意味着只要有 RT/DL 任务可跑，
**公平任务永远轮不到**——理论上 RT 任务可以把普通任务彻底饿死。早期靠
`RT_RUNTIME_SHARE`/`rt_bandwidth`（限制 RT 总占用，如默认每秒最多 950ms）兜底，
但较粗糙。现代内核引入了更优雅的 **fair_server**。

`rq->fair_server` 是一个 **DL（SCHED_DEADLINE）带宽服务器**
（`fair_server_init`，`kernel/sched/fair.c:9321`）：

```c
void fair_server_init(struct rq *rq)
{
	struct sched_dl_entity *dl_se = &rq->fair_server;
	...
	dl_server_init(dl_se, rq, fair_server_pick_task);
}
```

工作原理：
- fair_server 是一个挂在 **DL 调度类**里的"虚拟实体"，被分配一份 DL 带宽
  （如每周期保证若干 ms）。因为它在 DL 类里，**优先级高于 RT**。
- 当它的 DL 带宽到期需要运行时，它的 `pick_task` 回调 `fair_server_pick_task`
  （`kernel/sched/fair.c:9316`）会**去公平队列里挑一个任务来跑**。
- 于是：即便 RT 任务长期占用 CPU，fair_server 也能凭借 DL 带宽**周期性地抢到
  时间片，并把它让给公平任务**，保证公平任务获得一个有保证的最小份额，不被饿死。

记账上，`update_curr`（2.5）里的 `dl_server_update(&rq->fair_server, delta_exec)`
（`kernel/sched/fair.c:1441`）把公平任务消耗的时间也记到 fair_server 头上——
无论任务是"借 fair_server 的带宽跑"还是"正常跑"，都统一记账，使 fair_server 能
准确知道公平类已获得多少时间、是否还需要这个周期介入。

可经 `/sys/kernel/debug/sched/fair_server/`（`kernel/sched/debug.c:555`）观测/调节其带宽参数。

> **意义**：fair_server 用"DL 带宽服务器"这一统一机制取代了旧的 RT 限流 hack，
> 为公平任务提供**确定性的最小服务保证**，是近年调度器健壮性的重要改进。

### 7.3 __schedule —— 调度主循环完整走读

第 2.13 节给了 `__schedule` 的伪代码，这里读真实源码（`kernel/sched/core.c:7017`）。
它是整个调度器的"心脏"，被标记 `notrace`、对中断/抢占极其敏感。

```c
static void __sched notrace __schedule(int sched_mode)
{
	struct task_struct *prev, *next;
	bool preempt = sched_mode > SM_NONE;
	struct rq_flags rf;
	struct rq *rq;
	int cpu;

	cpu  = smp_processor_id();
	rq   = cpu_rq(cpu);
	prev = rq->curr;

	schedule_debug(prev, preempt);

	local_irq_disable();                 // ① 关本地中断
	rcu_note_context_switch(preempt);

	rq_lock(rq, &rf);                    // ② 上 rq 锁
	smp_mb__after_spinlock();            //    内存屏障，与唤醒侧配对（注释详述竞态）

	rq->clock_update_flags <<= 1;
	update_rq_clock(rq);                 // ③ 刷新 rq->clock / clock_task（记账基准）

	prev_state = READ_ONCE(prev->__state);
	if (sched_mode == SM_IDLE) {
		...                          // 进 idle 的快路径
	} else if (!preempt && prev_state) {
		/* ④ prev 不是被抢占、且状态非 RUNNING（要睡）→ 出队 */
		try_to_block_task(rq, prev, &prev_state, !task_is_blocked(prev));
		switch_count = &prev->nvcsw;
	}

pick_again:
	next = pick_next_task(rq, rq->donor, &rf);   // ⑤ ★ 选下一个（按类优先级）
	rq->next_class = next->sched_class;

	if (sched_proxy_exec()) {            // ⑥ proxy-exec：处理 donor/curr 分离（7.6）
		rq_set_donor(rq, next);
		if (unlikely(next->blocked_on)) {
			next = find_proxy_task(rq, next, &rf);
			if (!next) { ...; goto pick_again; }
		}
		...
	} else {
		rq_set_donor(rq, next);
	}

picked:
	clear_tsk_need_resched(prev);        // ⑦ 清 TIF_NEED_RESCHED
	clear_preempt_need_resched();

	is_switch = prev != next;
	if (likely(is_switch)) {
		...
		rq = context_switch(rq, prev, next, &rf);  // ⑧ ★ 真正切换地址空间+寄存器
	} else {
		rq_unlock_irq(rq, &rf);          // 选中的还是自己，免切换
	}
	// ⑨ balance_callback 在 context_switch 后、解锁时被处理
}
```

几个值得记住的点：
- **第②步 `smp_mb__after_spinlock()`** 配合唤醒侧的屏障，解决"设置任务状态"与
  "检测 signal_pending / 唤醒"之间的经典竞态（源码 `core.c:7047` 有长篇注释）。
  这是调度器正确性的微妙之处。
- **第⑤步 `pick_next_task(rq, rq->donor, ...)`** 传的是 `rq->donor` 而非 `rq->curr`——
  这是 proxy-exec 的体现：选择基于"调度上下文任务"。
- **第⑧步 `context_switch`** 才是真正的切换：切页表（`switch_mm`）、切内核栈和寄存器
  （`switch_to`）。切换后"返回"时已经在新任务的上下文里了。

`pick_next_task()`（核心层，`kernel/sched/core.c` 中）有快慢两条路：
- **快路径**：若 `prev` 是公平类且 `rq->nr_running == rq->cfs.h_nr_queued`（全是公平任务），
  直接 `pick_next_task_fair()`，跳过逐类遍历。
- **慢路径**：`for_each_class(class)` 从 `stop → dl → rt → fair → idle` 依次调
  `class->pick_next_task`，第一个返回非 NULL 的胜出。这保证了**调度类的严格优先级**。

### 7.4 pick_next_task_fair —— 组调度下的最小化切换

第 2.13 给了它的流程，这里读组调度部分的精妙实现（`kernel/sched/fair.c:9234`）：

```c
pick_next_task_fair(struct rq *rq, struct task_struct *prev, struct rq_flags *rf)
{
again:
	p = pick_task_fair(rq, rf);            // ① 逐层 pick_eevdf 选出任务 p
	if (!p) goto idle;
	se = &p->se;

#ifdef CONFIG_FAIR_GROUP_SCHED
	if (prev->sched_class != &fair_sched_class)
		goto simple;                   // prev 不是公平类，走简单路径

	if (prev != p) {
		struct sched_entity *pse = &prev->se;
		struct cfs_rq *cfs_rq;

		/* ② 只切换 prev 与 p 在组层级中"分叉"的那一段，
		 *    共同祖先以上的组实体不动——大幅减少 put/set 开销 */
		while (!(cfs_rq = is_same_group(se, pse))) {
			int se_depth  = se->depth;
			int pse_depth = pse->depth;
			if (se_depth <= pse_depth) {
				put_prev_entity(cfs_rq_of(pse), pse);   // 把 prev 这边逐层放回树
				pse = parent_entity(pse);
			}
			if (se_depth >= pse_depth) {
				set_next_entity(cfs_rq_of(se), se, true); // 把 p 这边逐层设为 curr
				se = parent_entity(se);
			}
		}
		put_prev_entity(cfs_rq, pse);          // ③ 在共同祖先处收尾
		set_next_entity(cfs_rq, se, true);
		__set_next_task_fair(rq, p, true);
	}
	return p;

simple:
#endif
	put_prev_set_next_task(rq, prev, p);
	return p;

idle:
	new_tasks = sched_balance_newidle(rq, rf);   // ④ 无任务可选 → newidle 均衡（3.8）
	if (new_tasks > 0) goto again;               //    拉到任务则重选
	...
}
```

**第②步是组调度的性能关键**：假设 prev 和 p 同属一个 cgroup（很常见，因为
`set_next_buddy` 倾向同组），那么它们在组层级里共享大段祖先，只有最底层不同。
这段 `while` 循环只 `put`/`set` 那段**分叉路径**，共同祖先以上的组实体完全不动，
把组调度的切换开销从 O(层级深度) 降到 O(分叉深度)。注释（`kernel/sched/fair.c:9253`）
明确点出了这个优化动机。

### 7.5 can_migrate_task —— 负载均衡的迁移闸门

第 3.6 概述了迁移约束，这里读真实代码（`kernel/sched/fair.c:9740`）。
它是 `detach_tasks` 每搬一个任务前的"安检"：

```c
int can_migrate_task(struct task_struct *p, struct lb_env *env)
{
	/*
	 * 不迁移：1) 延迟出队任务(除非按 load 迁) 2) 目标在被限流层级
	 *         3) 亲和性不允许目标 CPU 4) 正在运行 5) 在原 CPU cache 热
	 *         6) 阻塞在 mutex 上(proxy-exec)
	 */
	if (p->se.sched_delayed && env->migration_type != migrate_load)
		return 0;                                  // ①
	if (lb_throttled_hierarchy(p, env->dst_cpu))
		return 0;                                  // ②

	/* ③ 优先迁移 eligible 任务；不 eligible 的软限制，仅在均衡屡败时才迁 */
	if (!env->sd->nr_balance_failed &&
	    task_is_ineligible_on_dst_cpu(p, env->dst_cpu))
		return 0;

	if (kthread_is_per_cpu(p))   return 0;             // ④ per-cpu 内核线程不迁
	if (task_is_blocked(p))      return 0;

	if (!cpumask_test_cpu(env->dst_cpu, p->cpus_ptr)) { // ⑤ 亲和性不允许
		env->flags |= LBF_SOME_PINNED;
		/* 记下它能去本 group 的别的 CPU，供后续重试 */
		...
		return 0;
	}
	/* ⑥ 再检查 task_hot()（cache 热度，基于 migration_cost_ns）... */
}
```

注意 **EEVDF 感知的细节③**：负载均衡**优先迁移 eligible（lag≥0）的任务**，
对不 eligible 的任务做软限制——只有当本调度域的均衡已经**失败过若干次**
（`nr_balance_failed`）才允许迁移它们。这避免了"为了均衡硬把超额任务搬走"
反而扰动公平性。这是 EEVDF 语义渗透进负载均衡的一个具体例子。

### 7.6 h_load —— 层级负载传播

负载均衡按 `task_h_load(p)`（任务对**根** cfs_rq 的负载贡献）来搬运。
但组调度下，一个任务的 `load_avg` 是相对**它所在组队列**的，要换算到根队列
才能跨组比较。这就是 `h_load`（hierarchical load）：

```c
static unsigned long task_h_load(struct task_struct *p)   // fair.c:10207
{
	struct cfs_rq *cfs_rq = task_cfs_rq(p);
	update_cfs_rq_h_load(cfs_rq);                     // 先把 h_load 沿层级算到位
	return div64_ul(p->se.avg.load_avg * cfs_rq->h_load,
			cfs_rq_load_avg(cfs_rq) + 1);     // 任务负载 × 组在根的占比
}
```

公式直觉：`任务对根的贡献 = 任务在本组的 load_avg × (本组 cfs_rq 在根的 h_load 占比)`。
`update_cfs_rq_h_load()` 自底向上（用 `h_load_next` 链）把每层的 `h_load` 算出来：
`cfs_rq->h_load = 父组的 h_load × (本组 load_avg / 父组 load_avg)`
（注释 `kernel/sched/sched.h:726` 给了 `h_load = weight * f(tg)` 的定义）。

> 非组调度时（`#else`，`fair.c:10228`）`task_h_load` 直接返回 `se.avg.load_avg`——
> 没有层级，任务负载就是它对根的贡献。

`__update_blocked_fair()` / `sched_balance_update_blocked_averages()`
（`kernel/sched/fair.c:10234`）则在均衡前周期性衰减**所有队列**（含已空队列的 blocked load），
保证负载数据新鲜，并顺手 `cpufreq_update_util()` 触发调频。

### 7.7 wake_affine —— 唤醒亲和的两种判据

第 3.9 提到 wake-affine 倾向把任务放在唤醒者附近。`wake_affine()`
（`kernel/sched/fair.c:7670`）用两个判据先后尝试：

```c
static int wake_affine(struct sched_domain *sd, struct task_struct *p,
		       int this_cpu, int prev_cpu, int sync)
{
	int target = nr_cpumask_bits;

	if (sched_feat(WA_IDLE))                        // ① 优先：哪边有空闲核就去哪
		target = wake_affine_idle(this_cpu, prev_cpu, sync);

	if (sched_feat(WA_WEIGHT) && target == nr_cpumask_bits)  // ② 退而求其次：比负载
		target = wake_affine_weight(sd, p, this_cpu, prev_cpu, sync);

	if (target != this_cpu)
		return prev_cpu;                       // 不值得拉过来，留在原 CPU
	return target;                                 // 拉到唤醒者所在 CPU
}
```

- **`wake_affine_idle`（WA_IDLE）**：若唤醒者 CPU 或其 SMT sibling 空闲，
  且是 `sync` 唤醒（唤醒者马上要睡，如 pipe write 后等 read），就把被唤醒任务拉过来——
  接棒空出来的 CPU，cache 还热。
- **`wake_affine_weight`（WA_WEIGHT）**：比较"拉过来"vs"留原地"两种放置下，
  两个 CPU 的**加权负载**谁更均衡，选更均衡的。`WA_BIAS` 给一点偏置避免抖动。

之后再由 `select_idle_sibling()` 在选定的"方向"上找具体空闲核落脚。

### 7.8 proxy-exec：rq->curr 与 rq->donor 的分离

前面反复出现 `rq->donor`。**proxy execution** 解决的是**优先级反转**：
高优先级任务 A 阻塞在一把被低优先级任务 B 持有的 mutex 上。传统做法是优先级继承
（把 B 临时提到 A 的优先级）。proxy-exec 更彻底：**让 CPU 直接"替 A"去执行 B 的代码**，
B 跑在 A 的调度上下文（时间片、deadline）里，直到放锁。

在这套模型里：
- **`rq->donor`**：提供"调度上下文"的任务（被选中的、本该跑的 A）。
- **`rq->curr`**：真正在 CPU 上执行指令的任务（持锁的 B）。
- 普通情况 `curr == donor`。

这就是为什么 `update_curr` 注释（`kernel/sched/fair.c:1409`）强调
"`cfs_rq->curr` 对应 `rq->donor.se`，未必是真在跑的 `rq->curr.se`"；
`__schedule` 第⑤步用 `rq->donor` 去 `pick_next_task`；`update_se`（`kernel/sched/fair.c:1363`）
把时间记到真在跑的 `rq->curr` 头上、cgroup 时间记到 donor 头上。
初学时可忽略，但读源码时遇到 donor 要知道它的来历。

### 7.9 NUMA 均衡

在 NUMA 机器上，任务访问"远端"内存比"本地"慢得多。**自动 NUMA 均衡**
（`CONFIG_NUMA_BALANCING`）周期性给任务的内存页设"NUMA hinting fault"，
统计任务实际在哪个 node 上访问内存最多，进而：
- 把**任务迁到它内存所在的 node**（`task_numa_migrate`），或
- 把**内存迁到任务所在的 node**（page migration）。

入口在 `task_tick_fair` 里调的 `task_tick_numa()`（非 NUMA 配置下是空函数，
`kernel/sched/fair.c:3857`），核心是 `task_numa_fault()` / `task_numa_placement()`。
开关：`/proc/sys/kernel/numa_balancing`（`kernel/sched/core.c:4667`）。
相关 tracepoint：`sched_move_numa`、`sched_swap_numa`。这是吞吐密集型负载（数据库、HPC）
的重要调优维度，但与 EEVDF 的单核选择逻辑正交。

### 7.10 core scheduling

`CONFIG_SCHED_CORE` 为应对 L1TF/MDS 等**超线程侧信道**漏洞而生：它保证
**同一物理核的多个 SMT 兄弟上只跑"互信"的任务**（同一 `core_cookie`），
否则宁可让兄弟超线程空转。`cfs_rq` 里的 `forceidle_seq` / `zero_vruntime_fi`
（`kernel/sched/sched.h:690`）就是为此服务——在跨 SMT 比较 vruntime 时需要一个
统一基准。开启后 pick 逻辑会做"core-wide"的协调。这是安全特性，默认编译可选。

### 7.11 sched_ext（BPF 可扩展调度类）

`CONFIG_SCHED_CLASS_EXT`（`kernel/sched/sched.h:775` 的 `struct scx_rq`）引入了
**用 BPF 写调度策略**的能力（文档 `Documentation/scheduler/sched-ext.rst`）。
它是一个**独立调度类**（优先级介于 fair 之上、可抢占 fair 任务），不是改 EEVDF，
而是让用户态能加载自定义调度器做实验/特化负载优化。EEVDF 仍是默认 `SCHED_NORMAL` 实现，
sched_ext 是"旁路"。了解它的存在即可，本文不展开。

### 7.12 hrtick：高精度抢占

普通抢占检查发生在周期 tick（`task_tick_fair`），粒度受 `CONFIG_HZ` 限制
（如 1000Hz = 1ms）。当任务的 `slice` 比一个 tick 还短（如自定义 200µs），
光靠 tick 无法精确在 slice 耗尽时抢占。`HRTICK` 特性（`features.h:68`）用
**高精度定时器**按任务的实际 slice 精确排程一次抢占（`entity_tick` 里的
`#ifdef CONFIG_SCHED_HRTICK` 分支，`kernel/sched/fair.c:5835`）。
这对 EEVDF 的"小 slice → 低延迟"承诺很重要——否则自定义短 slice 会被 tick 粒度"抹平"。

### 7.13 本章小结

| 机制 | 函数/结构 | 位置 | 一句话 |
| --- | --- | --- | --- |
| 队列权重记账 | `account_entity_enqueue/dequeue` | `kernel/sched/fair.c:3875` | 维护 `cfs_rq->load` 与 `rq->cfs_tasks` |
| 任务级入队 | `enqueue_task_fair` | `kernel/sched/fair.c:7197` | 逐层入队 + h_nr 计数 + 调频 |
| 防 RT 饿死 | `fair_server` (DL 服务器) | `kernel/sched/fair.c:9321` | 给公平类保证最小份额 |
| 改权重保公平 | `reweight_entity` | `kernel/sched/fair.c:4068` | 卸账→改权重→用保留 lag 复原 |
| 调度主循环 | `__schedule` | `kernel/sched/core.c:7017` | 关中断→上锁→pick→context_switch |
| 最小化组切换 | `pick_next_task_fair` | `kernel/sched/fair.c:9234` | 只切换组层级的分叉段 |
| 迁移闸门 | `can_migrate_task` | `kernel/sched/fair.c:9740` | 亲和/cache热/eligible 多重检查 |
| 层级负载 | `task_h_load` | `kernel/sched/fair.c:10207` | 任务对根队列的负载贡献 |
| 唤醒亲和 | `wake_affine` | `kernel/sched/fair.c:7670` | WA_IDLE 优先、WA_WEIGHT 兜底 |
| proxy-exec | `rq->donor` vs `rq->curr` | `kernel/sched/sched.h:1131` | 替高优先级任务执行持锁者 |
| NUMA 均衡 | `task_tick_numa` | `kernel/sched/fair.c:3857` | 任务/内存就近 |
| core sched | `forceidle_seq` | `kernel/sched/sched.h:690` | SMT 只跑互信任务 |
| sched_ext | `struct scx_rq` | `kernel/sched/sched.h:775` | BPF 自定义调度类 |
| hrtick | `HRTICK` 特性 | `kernel/sched/features.h:68` | 按 slice 精确抢占 |

---

## 第 8 章 数值实例：手算几轮 EEVDF 调度

理论讲完，最好的巩固是**拿起笔手算一遍**。本章用具体数字走几轮调度，
把 vruntime、V、lag、eligible、deadline、pick 全部算出来，并标注每一步对应的内核函数。

> 约定：所有任务 nice=0（权重 `w=1024=NICE_0_LOAD`）。
> 由 `calc_delta_fair(delta, se) = delta * 1024 / w`，nice-0 任务的虚拟时间增量 **= 真实时间增量**，
> 所以下文 vruntime 直接用微秒（µs）记，省去换算。`base_slice = 700µs`。

### 8.1 场景设定

某 CPU 的 `cfs_rq` 上有三个就绪任务，某一时刻快照如下（都不是 curr，都在树里）：

| 任务 | nice | 权重 w | vruntime v | slice r | deadline = v + r/w |
| --- | --- | --- | --- | --- | --- |
| A | 0 | 1024 | 1000 | 700µs | 1000 + 700 = **1700** |
| B | 0 | 1024 | 1000 | 700µs | 1000 + 700 = **1700** |
| L | 0 | 1024 | 1000 | **200µs** | 1000 + 200 = **1200** |

A、B 是普通 CPU 任务（默认 slice 700µs）；**L 是延迟敏感任务**，
通过 `sched_setattr(sched_runtime=200µs)` 把 slice 设小（`custom_slice=1`，见
`__setparam_fair`，`kernel/sched/fair.c:5364`）。三者 vruntime 恰好相同（都跑了一样多）。

### 8.2 第 1 轮：pick_eevdf 选谁？

**Step 1 — 算加权平均 V**（`avg_vruntime`，`kernel/sched/fair.c:780`）：

权重相同，V 就是算术平均：

```text
V = (1024·1000 + 1024·1000 + 1024·1000) / (1024·3) = 1000
```

**Step 2 — 算各任务 lag 并判 eligible**（`entity_eligible` → `vruntime_eligible`，`fair.c:939`）：

```text
lag_i = V - v_i
lag_A = 1000 - 1000 = 0   → 0 ≥ 0 → eligible ✓
lag_B = 1000 - 1000 = 0   → eligible ✓
lag_L = 1000 - 1000 = 0   → eligible ✓
```

三个都 eligible（lag=0 是 eligible 的边界，仍算合格）。

**Step 3 — eligible 集合里取 deadline 最早者**（`pick_eevdf` 堆搜索，`fair.c:1136`）：

```text
eligible = {A:1700, B:1700, L:1200}
最早 deadline = L (1200)  →  ★ 选中 L
```

**结论**：三个任务 vruntime 完全相同（一样公平），但 **L 凭借更小的 slice 算出更早的
deadline，被优先调度**。这就是 EEVDF 给延迟敏感任务的红利——
在不破坏长期公平的前提下，让"只想跑一小会"的任务先上。

> 对照经典 CFS：三者 vruntime 相同，CFS 只能"随便挑一个最左的"，
> 无法表达 L 的低延迟诉求。

### 8.3 第 2 轮：L 跑完它的 slice 之后

`set_next_entity(L)`（`fair.c:5729`）把 L 移出树设为 curr，并 `set_protect_slice`。
L 上 CPU 跑了 **200µs**（它的 slice）。期间 `update_curr`（`fair.c:1407`）不断推进：

**Step 1 — vruntime 推进**：

```text
L.vruntime += calc_delta_fair(200, L) = 200
L.vruntime: 1000 → 1200
```

**Step 2 — update_deadline 触发**（`fair.c:1238`）：L.vruntime(1200) ≥ L.deadline(1200)，
slice 跑满，重算：

```text
L.deadline = 1200 + calc_delta_fair(200, L) = 1200 + 200 = 1400
update_deadline 返回 true → resched
```

L 被换下，`put_prev_entity(L)`（`fair.c:5798`）把它放回树。新快照：

| 任务 | vruntime | deadline |
| --- | --- | --- |
| A | 1000 | 1700 |
| B | 1000 | 1700 |
| L | **1200** | **1400** |

**Step 3 — 重新 pick**。先算新 V：

```text
V = (1000 + 1000 + 1200) / 3 = 1066.67
```

判 eligible：

```text
lag_A = 1066.67 - 1000 = +66.67   → eligible ✓
lag_B = 1066.67 - 1000 = +66.67   → eligible ✓
lag_L = 1066.67 - 1200 = -133.33  → 不 eligible ✗（L 刚超额消费）
```

**L 现在不 eligible 了**！它的 deadline 虽然最早（1400），但 lag<0 被挡在门外。
eligible 集合 = {A:1700, B:1700}，deadline 相同，取树最左 → **选中 A**。

**这正是 EEVDF 的精髓**：L 享受了低延迟（第 1 轮先跑），但**不能连续霸占** CPU——
跑完一个 slice 后立刻变得 ineligible，必须让 A、B 追上来。**低延迟与长期公平兼得**。

### 8.4 第 3 轮：A 跑完 700µs

`set_next_entity(A)`，A 跑满 slice 700µs。`update_curr`：

```text
A.vruntime: 1000 → 1000 + 700 = 1700
A.deadline: 1700 → 1700 + 700 = 2400  (update_deadline)
```

新快照：

| 任务 | vruntime | deadline |
| --- | --- | --- |
| A | **1700** | **2400** |
| B | 1000 | 1700 |
| L | 1200 | 1400 |

新 V：

```text
V = (1700 + 1000 + 1200) / 3 = 1300
```

eligible：

```text
lag_A = 1300 - 1700 = -400   → ✗（A 刚跑完，超额）
lag_B = 1300 - 1000 = +300   → ✓（B 一直没跑，欠最多）
lag_L = 1300 - 1200 = +100   → ✓（L 又重新 eligible 了）
```

eligible = {B:1700, L:1400}，最早 deadline → **选中 L**。

**观察**：经过一轮，L 又变回 eligible 并再次被优先选中。如果 L 周期性醒来跑很短，
它会**反复抢到最早 deadline**，始终获得低延迟响应；而它每次只跑 200µs，
长期占用的 CPU 份额并不会超过公平额度（因为每次跑完就 ineligible）。
A、B 这种大块 CPU 任务则在 L 不活跃的间隙充分运行。

### 8.5 lag 守恒自检

任意时刻 `Σ w_i·lag_i = 0`（2.1.1 的不变式）。以第 3 轮快照验证（w 相同，约去）：

```text
Σ lag_i = lag_A + lag_B + lag_L = -400 + 300 + 100 = 0  ✓
```

守恒成立——有人超额（A: -400）必有人欠额（B:+300, L:+100），总账平衡。
这是公平性的数学保证：EEVDF 永远不会"凭空创造或销毁" CPU 时间账。

### 8.6 加入权重：nice 的影响

换个场景看 nice（权重）如何改变虚拟时间速率。两个任务：

| 任务 | nice | 权重 w | 真实跑了 Δ=1000µs 后 vruntime 增量 = Δ·1024/w |
| --- | --- | --- | --- |
| H（高优先级） | -5 | 3121 | 1000·1024/3121 ≈ **328µs** |
| P（普通） | 0 | 1024 | 1000·1024/1024 = **1000µs** |

同样跑 1000µs 真实时间：
- H 的 vruntime 只涨 ~328µs（涨得**慢**）→ 更"赖"在 eligible 区 → 更容易被反复选中。
- P 的 vruntime 涨满 1000µs（涨得**快**）→ 很快超过 V 变 ineligible → 让位。

长期看，要让两者 vruntime 同步增长（都追着 V 跑），H 必须获得约 `3121/1024 ≈ 3.05` 倍于 P 的
真实 CPU 时间。这正是 nice -5 相对 nice 0 的权重比，**CPU 份额 ≈ 3:1**——
与第 1 章 `sched_prio_to_weight[]` 表（`kernel/sched/core.c:10569`）一致。

> deadline 也受权重影响：`deadline = v + r/w`。同样的 slice `r`，权重大的 H 的
> `r/w` 更小 → deadline 更早 → 更优先。所以高优先级任务**既跑得久（vruntime 慢）
> 又被优先选（deadline 早）**，双重受益。

### 8.7 本章小结

通过手算我们直观看到了 EEVDF 的两条核心规律：

1. **deadline 决定"谁先跑"**：小 slice / 高权重 → 早 deadline → 优先调度（低延迟）。
2. **eligible 决定"能跑多久"**：跑满 slice 后 lag 转负、变 ineligible，被迫让位（保公平）。

两者协同：**短而频繁的延迟敏感任务持续享受低延迟，却无法侵占长期公平份额**——
这是经典 CFS 单靠 vruntime 做不到的。所有计算都可在
`/sys/kernel/debug/sched/debug` 的输出里找到对应的真实数值（见附录 B）。

---

## 第 9 章 机制生命周期与配套子系统补遗

本章补齐几个"前面提到但没走完整生命周期"的机制，并介绍两个与公平调度紧密配合的
子系统（uclamp、调频），最后给出 CFS→EEVDF 的演进时间线和第二个数值实例。

### 9.1 DELAY_DEQUEUE 的完整生命周期

第 2.9.2 讲了"睡眠且 lag<0 的任务被延迟出队"，但没走完它的一生。
延迟出队任务（`sched_delayed=1`）有三种结局，构成一个完整状态机：

```text
        正常睡眠(lag<0)
              │ dequeue_entity: set_delayed()，留在树里，on_rq 仍=1
              ▼
      ┌──────────────────┐
      │  sched_delayed    │
      │  (留在树中竞争)    │
      └──────────────────┘
        │          │          │
   被pick选中   被重新唤醒   lag 自然回正
        │          │          │
        ▼          ▼          ▼
  真正出队     requeue复活   （下次pick时按正lag参与）
 (DEQUEUE_     (取消延迟,
  DELAYED)      重新place)
```

**结局一：被 pick 选中** → 其实它本意是睡，于是真正出队。
`pick_next_entity`（`kernel/sched/fair.c:5780`）发现 `se->sched_delayed`：

```c
	se = pick_eevdf(cfs_rq, protect);
	if (se->sched_delayed) {
		dequeue_entities(rq, se, DEQUEUE_SLEEP | DEQUEUE_DELAYED);
		return NULL;     // 本轮重新挑别人
	}
```

**结局二：任务被重新唤醒** → 它根本没真睡成，直接"复活"。
`requeue_delayed_entity()`（`kernel/sched/fair.c:7165`）：

```c
static void requeue_delayed_entity(struct sched_entity *se)
{
	struct cfs_rq *cfs_rq = cfs_rq_of(se);

	WARN_ON_ONCE(!se->sched_delayed);
	WARN_ON_ONCE(!se->on_rq);                 // 延迟态 on_rq 仍为 1

	if (update_entity_lag(cfs_rq, se)) {      // lag 被限幅修改过 → 需重新定位
		cfs_rq->nr_queued--;
		if (se != cfs_rq->curr)
			__dequeue_entity(cfs_rq, se);
		place_entity(cfs_rq, se, 0);      // 重新 place（DELAY_ZERO 时 lag 已削到 0）
		if (se != cfs_rq->curr)
			__enqueue_entity(cfs_rq, se);
		cfs_rq->nr_queued++;
	}
	update_load_avg(cfs_rq, se, 0);
	clear_delayed(se);                        // 清 sched_delayed
}
```

**结局三：lag 随别人运行自然回正** → 当 V 因别的任务推进而上移，
该任务 `lag = V - v_i` 回到 ≥ 0，它自然重新 eligible，下次 `pick_eevdf` 正常参与竞争。

`set_delayed()`（`kernel/sched/fair.c:5607`）打标记时还**递减 `h_nr_runnable`**：
延迟任务虽在树上，但不计入"可运行"计数（它逻辑上是要睡的），
保证负载/调频看到的 runnable 数准确。`clear_delayed()` 复活时对称地加回。

> **设计意义**：DELAY_DEQUEUE 把"睡眠"与"出队"解耦。一个超额（lag<0）的任务睡觉时
> 继续留在队里"虚拟欠债"，杜绝了经典做法下"睡一下→出队→醒来按平均重新入队→负 lag 清零"
> 的作弊路径。这是 EEVDF 公平性闭环的最后一块拼图。

### 9.2 CFS 带宽：周期定时器完整流程

第 4.5 讲了限流/解限流，这里把**周期定时器**这条主动线走完整。
`period_timer` 每个 `period`（默认 100ms）触发，最终进
`do_sched_cfs_period_timer()`（`kernel/sched/fair.c:6491`）：

```c
static int do_sched_cfs_period_timer(struct cfs_bandwidth *cfs_b, int overrun, ...)
{
	if (cfs_b->quota == RUNTIME_INF)
		goto out_deactivate;                  // 无限制，停掉定时器

	throttled = !list_empty(&cfs_b->throttled_cfs_rq);
	cfs_b->nr_periods += overrun;

	__refill_cfs_bandwidth_runtime(cfs_b);        // ① 补充全局 runtime = quota(+burst)

	if (cfs_b->idle && !throttled)
		goto out_deactivate;                  // 没人用也没人被限流 → 休眠定时器
	if (!throttled) {
		cfs_b->idle = 1;                      // 本周期没限流，标记可能空闲
		return 0;
	}

	cfs_b->nr_throttled += overrun;

	/* ② 把新补充的配额分给被限流的队列，逐个解限流 */
	while (throttled && cfs_b->runtime > 0) {
		raw_spin_unlock_irqrestore(&cfs_b->lock, flags);
		throttled = distribute_cfs_runtime(cfs_b);    // 见下
		raw_spin_lock_irqsave(&cfs_b->lock, flags);
	}
	...
}
```

`distribute_cfs_runtime()`（`kernel/sched/fair.c:6403`）遍历 `throttled_cfs_rq` 链表，
给每个被限流队列**补到 `runtime_remaining > 0`**，然后解限流：

```c
	list_for_each_entry_rcu(cfs_rq, &cfs_b->throttled_cfs_rq, throttled_list) {
		if (!remaining) { throttled = true; break; }  // 全局配额用完，留到下周期
		...
		runtime = -cfs_rq->runtime_remaining + 1;     // 补到刚好为正
		runtime = min(runtime, cfs_b->runtime);
		cfs_b->runtime -= runtime;
		cfs_rq->runtime_remaining += runtime;

		if (cfs_rq->runtime_remaining > 0) {
			if (cpu_of(rq) != this_cpu)
				unthrottle_cfs_rq_async(cfs_rq);  // 跨 CPU：发 CSD 异步解限流
			else
				list_add_tail(..., &local_unthrottle);  // 本 CPU：稍后本地解
		}
	}
```

完整时间线：

```text
period_timer 到期 (每 100ms)
  └─ sched_cfs_period_timer()                 fair.c:6739
       └─ do_sched_cfs_period_timer()         fair.c:6491
            ├─ __refill_cfs_bandwidth_runtime()  // runtime = quota
            └─ while(throttled && runtime>0):
                 distribute_cfs_runtime()      fair.c:6403
                   └─ 逐个被限流队列：补配额 → unthrottle_cfs_rq()  fair.c:6280
                        └─ walk_tg_tree(tg_unthrottle_up)  // 沿层级解冻
                        └─ 组实体重新挂回上层队列、必要时 resched
```

另有 `slack_timer` 走 `sched_cfs_slack_timer()`（`kernel/sched/fair.c:6729`）
异步回收零散剩余配额（4.5.5）。两个定时器一"补"一"收"，维持全局配额池的平衡。

### 9.3 yield：主动让出

任务调用 `sched_yield(2)` 时进 `yield_task_fair()`（`kernel/sched/fair.c:9347`）。
EEVDF 下 yield 的语义是"放弃当前的 eligibility 优势"：它通过
`cancel_protect_slice()`（解除 RUN_TO_PARITY 保护）+ 调整使当前任务的 deadline 不再最优，
从而让 `pick_eevdf` 倾向选别人。注意 yield 在 EEVDF 里**不会破坏公平账**——
它只是放弃"先跑"的权利，不改变长期应得份额。`yield_to()` 则用 `set_next_buddy()`
把目标任务设为 buddy，配合 `PICK_BUDDY`（2.7）让目标优先被选。

### 9.4 配套子系统一：uclamp（利用率钳制）

`CONFIG_UCLAMP_TASK` 让用户给任务设**利用率下限/上限**（`uclamp_min`/`uclamp_max`，
经 `sched_setattr` 的 `sched_util_min`/`sched_util_max`），文档见
`Documentation/scheduler/sched-util-clamp.rst`。它**不改 EEVDF 的选择逻辑**，
而是影响**调频和选核看到的"任务有多忙"**：

- **`uclamp_min`（boost）**：即使任务 PELT util 很低，也按至少 `uclamp_min` 对待 →
  让调频升频、选核选强核。用于"低 CPU 占用但要求响应快"的任务（如 UI 线程）。
- **`uclamp_max`（cap）**：即使任务 util 很高，也按至多 `uclamp_max` 对待 →
  抑制升频、可放小核。用于"占满 CPU 但不重要"的后台任务（省电）。

`util_fits_cpu()`（`kernel/sched/fair.c:5326` 附近，第 3 章选核用到）就综合
uclamp_min/max 判断任务"装不装得下"某 CPU。全局范围由 sysctl
`sched_util_clamp_min`/`_max`（`kernel/sched/core.c:4644`）限定。
uclamp 是移动/嵌入式场景能效调度的关键旋钮。

### 9.5 配套子系统二：schedutil 调频闭环

PELT 的 `util_avg`/`util_est`（第 5 章）经 `cpufreq_update_util()` 喂给
**schedutil** governor（`Documentation/scheduler/schedutil.rst`），形成闭环：

```text
任务运行 → update_curr/update_load_avg → util_avg 变化
   → cpufreq_update_util(rq)
   → schedutil: 目标频率 ∝ max(各任务 util，经 uclamp 钳制)
   → 调 CPU 频率
   → 频率变 → 同样 util 对应的"真实算力需求"变 → capacity 调整
   → 反馈回负载均衡的 capacity 计算
```

"调度器最懂每个任务多忙，所以由它驱动调频最准"——这是 schedutil 取代旧 ondemand/
conservative governor 的核心理由。`NONTASK_CAPACITY`（5.7）把中断/RT/DL 占用的算力扣除，
让 CFS 看到的可用 capacity 更真实。

### 9.6 CFS → EEVDF 演进时间线

| 版本 | 里程碑 |
| --- | --- |
| 2.6.23（2007） | CFS 合入，取代 O(1) 调度器；引入 vruntime + 红黑树 + 调度类框架 |
| 2.6.24+ | 组调度（CONFIG_FAIR_GROUP_SCHED）、nice 权重表完善 |
| 3.x | CFS 带宽控制（cpu.max throttle）、PELT 引入并逐步替代旧负载跟踪 |
| 4.x | PELT 完善（util_avg/runnable）、EAS 能效调度、schedutil |
| 5.x | uclamp、core scheduling（SMT 安全）、PELT 多项优化 |
| **6.6（2023/24）** | **EEVDF 取代经典 CFS 成为默认**：引入 lag/eligible/deadline、自定义 slice |
| 6.7+ | DELAY_DEQUEUE、RUN_TO_PARITY、PLACE_LAG 等 EEVDF 特性逐步稳定 |
| 6.12+ | sched_ext（BPF 可扩展调度类）合入 |
| 6.18（本仓库） | EEVDF 成熟态：增广树堆搜索、proxy-exec、fair_server 等 |

关键点：**EEVDF 不是推倒重来，而是在 CFS 的框架（vruntime、红黑树、调度类、PELT、组调度、
带宽控制）之上替换"选择策略"**。所以 CFS 时代积累的大量基础设施（第 3/4/5 章的内容）
被完整继承，只有第 2 章的"怎么选"被换成了 EEVDF。这也是符号名仍叫 cfs/fair 的根本原因。

### 9.7 第二个数值实例：DELAY_DEQUEUE 防作弊

用数字演示 9.1 的防作弊价值。两个 nice-0 任务（w=1024）：

初始快照，C 是个"想作弊"的任务（刚跑完，超额）：

| 任务 | vruntime | lag = V - v |
| --- | --- | --- |
| D | 1000 | — |
| C | 1400 | — |

`V = (1000 + 1400)/2 = 1200`。`lag_D = +200`（eligible），`lag_C = -200`（ineligible，超额）。

**没有 DELAY_DEQUEUE 的"作弊"路径（假想）：**
C 短暂睡眠 → 立即出队 → 醒来 `place_entity` 按当前 V 重新放置 →
`C.vruntime ≈ V = 1200`，`lag_C ≈ 0`。**C 用一次睡眠把 -200 的负 lag 抹成了 0**，
凭空"赖掉"了它超额消费的账，对老实排队的 D 不公平。

**有 DELAY_DEQUEUE 的真实路径：**
C 睡眠 → `dequeue_entity` 发现 `lag_C = -200 < 0` → `set_delayed()`，
**C 留在树里，on_rq 仍=1，vruntime 仍是 1400**。此后：
- 若 C 很快醒来 → `requeue_delayed_entity` 复活，lag 仍是负的（或 DELAY_ZERO 削到 0 但不会变正），
  **作弊未遂**。
- 若 C 继续"睡"→ 随着 D 运行推高 V，C 的负 lag 慢慢被"烧掉"回正，
  **相当于 C 仍在为它的超额消费"服刑"**，公平账得以保全。

`Σ lag = +200 + (-200) = 0` 守恒始终成立。这个例子直观展示了
DELAY_DEQUEUE 如何堵住"短睡眠重置负 lag"的公平漏洞。

### 9.8 本章小结

- **DELAY_DEQUEUE** 把睡眠与出队解耦，超额任务睡觉时继续"虚拟服刑"，堵住作弊漏洞
  （`set_delayed`/`requeue_delayed_entity`，`kernel/sched/fair.c:5607`/`7165`）。
- **带宽周期定时器**主动补充全局配额并 `distribute_cfs_runtime` 逐个解限流
  （`do_sched_cfs_period_timer`，`kernel/sched/fair.c:6491`）。
- **uclamp** 和 **schedutil** 不改 EEVDF 选择，但通过影响"任务多忙"驱动调频与选核，
  是能效调度的关键配套。
- **EEVDF 是 CFS 框架上的策略替换**，而非重写——这解释了全部的命名延续与基础设施继承。

---

## 附录 A 常见误区与排查

**误区 1：6.6+ 内核还在用经典 CFS。**
错。核心算法是 EEVDF（`pick_eevdf`，`kernel/sched/fair.c:1136`），只是符号名仍叫 cfs/fair。

**误区 2：调 `sched_min_granularity_ns` / `sched_latency_ns` 能调延迟。**
这些 sysctl 在 EEVDF 下**已被移除**。现在调 `/sys/kernel/debug/sched/base_slice_ns`，
或对延迟敏感任务用 `sched_setattr()` 设更小的 `sched_runtime`（自定义 slice）。

**误区 3：红黑树按 vruntime 排序。**
经典 CFS 是，EEVDF **按 deadline 排序**（`entity_before` 比的是 `deadline`，
`kernel/sched/fair.c:589`），vruntime 只作为增广字段用于剪枝。

**误区 4：当前运行任务在红黑树里。**
不在。`set_next_entity` 会 `__dequeue_entity` 把它摘出存到 `cfs_rq->curr`
（`kernel/sched/fair.c:5742`）。所以 `avg_vruntime`/`vruntime_eligible` 都要单独把 curr 贡献加回。

**误区 5：nice 值是线性的。**
不是。相邻 nice 差约 1.25 倍权重（`sched_prio_to_weight[]`，`kernel/sched/core.c:10569`），
每降 1 个 nice 约多拿 10% CPU。

**误区 6：cpu.shares/weight 能限制绝对 CPU 用量。**
不能，它只控制**相对比例**。要硬上限用 `cpu.max`（CFS bandwidth，`quota/period`，第 4 章）。

**排查建议：**
- 任务"该跑不跑"→ 看 `/sys/kernel/debug/sched/debug` 里它的 `vlag`（负 lag 说明超额了）、
  是否 `sched_delayed`、是否所在组被 throttle（`cpu.stat` 的 `nr_throttled`）。
- 延迟抖动 → 检查 `RUN_TO_PARITY`/`PREEMPT_SHORT`/`base_slice_ns`，用 `perf sched latency` 量化。
- 负载不均 → 看 `migrate` tracepoint、`/proc/schedstat`、各 CPU 的 `util_avg`。

---

## 附录 B 实测命令速查

> 以下命令用于在真实系统上观测本文讲的机制（需相应权限/内核配置）。

**查看与调节核心旋钮：**
```bash
# EEVDF 默认时间片（纳秒）
cat /sys/kernel/debug/sched/base_slice_ns

# 调度特性开关（开 NAME / 关 NO_NAME）
cat /sys/kernel/debug/sched/features
echo NO_RUN_TO_PARITY | sudo tee /sys/kernel/debug/sched/features   # 实验：关闭后观察延迟变化

# 迁移代价 / 单次迁移任务数
cat /sys/kernel/debug/sched/migration_cost_ns
cat /sys/kernel/debug/sched/nr_migrate
```

**逐任务观察 vruntime / deadline / lag：**
```bash
sudo cat /sys/kernel/debug/sched/debug | less   # 每 CPU、每任务的调度状态转储
```

**给延迟敏感任务设自定义 slice（EEVDF 新能力）：**
```c
/* C: 通过 sched_setattr(2) 请求更短时间片，换取更早 deadline */
struct sched_attr attr = {
	.size = sizeof(attr),
	.sched_policy = SCHED_NORMAL,
	.sched_runtime = 200000,   /* 200µs，比默认 700µs 短 → 更易被优先调度 */
};
syscall(SYS_sched_setattr, 0, &attr, 0);
```

**用 perf / ftrace 追踪调度事件：**
```bash
# 上下文切换、唤醒延迟统计
sudo perf sched record -- sleep 5
sudo perf sched latency
sudo perf sched timehist

# 用 ftrace 直接看 tracepoint
sudo trace-cmd record -e sched_switch -e sched_wakeup -e sched_migrate_task -- sleep 3
sudo trace-cmd report
```

**CFS 带宽（cgroup v2）限流观测：**
```bash
# 给某 cgroup 设上限：每 100ms 最多 50ms CPU（即 0.5 核）
echo "50000 100000" | sudo tee /sys/fs/cgroup/mygrp/cpu.max
# 观察限流统计
cat /sys/fs/cgroup/mygrp/cpu.stat   # nr_periods / nr_throttled / throttled_usec
```

**调度统计：**
```bash
# 开启 schedstats 后读取
echo 1 | sudo tee /proc/sys/kernel/sched_schedstats
cat /proc/schedstat
cat /proc/<pid>/schedstat        # 单任务：运行时间 / 等待时间 / 时间片数
```

---

## 参考资料

内核源码（本仓库）：
- `kernel/sched/fair.c` —— EEVDF/CFS 核心实现
- `kernel/sched/core.c` —— 调度主循环、权重表、sysctl
- `kernel/sched/sched.h` —— `cfs_rq`/`rq`/`sched_class`/`cfs_bandwidth`/`task_group`
- `kernel/sched/pelt.c` / `pelt.h` —— PELT 负载跟踪
- `kernel/sched/features.h` —— 特性开关
- `kernel/sched/debug.c` —— debugfs 接口
- `include/linux/sched.h` —— `sched_entity` / `sched_avg`
- `include/trace/events/sched.h` —— tracepoints

内核文档（`Documentation/scheduler/`）：
- `sched-eevdf.rst` —— EEVDF 设计（含 1995 年原始论文引用）
- `sched-design-CFS.rst` —— 经典 CFS 设计（对照）
- `sched-domains.rst` —— 调度域 / 负载均衡框架
- `sched-bwc.rst` —— CFS 带宽控制
- `sched-nice-design.rst` —— nice 值与权重设计
- `sched-stats.rst` / `sched-debug.rst` —— 统计与调试接口
- `schedutil.rst` / `sched-capacity.rst` / `sched-energy.rst` —— 调频与能效

> 本文档基于 Linux 6.18.5 源码撰写，所有 `文件:行号` 引用均以该版本为准。
> 内核演进较快，跨版本阅读时请以实际源码为准重新核对行号。

---

## 附录 C 术语表

按拼音/字母排序，给出每个术语的精确含义与对应代码。

- **base_slice（基础时间片）**：默认请求时间片，700µs（`sysctl_sched_base_slice`，
  `kernel/sched/fair.c:79`）。可经 `/sys/kernel/debug/sched/base_slice_ns` 调节。
  任务未自定义 slice 时即用它。

- **cfs_rq（公平运行队列）**：`struct cfs_rq`（`kernel/sched/sched.h:678`），
  承载一组就绪 `sched_entity` 的红黑树及其聚合统计。每 CPU 一个根 cfs_rq，
  每组每 CPU 一个组 cfs_rq。

- **curr（当前实体）**：`cfs_rq->curr`，正在该队列上"运行"的实体。**它不在红黑树里**
  （被 `set_next_entity` 摘出），所以 V/eligible 计算要单独补回它的贡献。

- **deadline / virtual deadline（虚拟截止时间，vd_i）**：`se->deadline`
  （`include/linux/sched.h:579`），`vd_i = ve_i + r_i/w_i`。红黑树的排序键。
  EEVDF 选择"eligible 集合里 deadline 最早者"。

- **DELAY_DEQUEUE（延迟出队）**：特性（`features.h:58`）。睡眠且 lag<0 的任务不立即出树，
  留下"烧掉"负 lag，防止靠短睡眠重置负账作弊。

- **eligible（合格）**：lag ≥ 0，即 `v_i ≤ V`，任务没有超额消费 CPU 时间，有资格被选中。
  判定见 `entity_eligible`（`kernel/sched/fair.c:939`）。

- **EEVDF**：Earliest Eligible Virtual Deadline First，最早合格虚拟截止时间优先。
  6.6 起取代经典 CFS 的核心算法（`pick_eevdf`，`kernel/sched/fair.c:1136`）。

- **lag（滞后，lag_i / vlag）**：`lag_i = S - s_i = w_i·(V - v_i)`，任务"应得-已得"CPU 时间之差。
  正=系统欠它（eligible），负=它超额（暂不 eligible）。近似值缓存在 `se->vlag`
  （`include/linux/sched.h:596`），出队时由 `update_entity_lag`（`fair.c:858`）算好。

- **NICE_0_LOAD**：nice 0 的基准权重 1024。所有权重换算的基准。

- **PELT（Per-Entity Load Tracking）**：每实体负载跟踪。几何衰减（y^32=0.5）的负载/利用率
  平均，是负载均衡与调频的数据基础。实现在 `kernel/sched/pelt.c`。

- **proxy-exec（代理执行）**：CPU 替高优先级任务（donor）执行其所阻塞 mutex 的持有者（curr）。
  `rq->donor` 提供调度上下文，`rq->curr` 是真在跑的任务。

- **RUN_TO_PARITY**：特性（`features.h:20`）。当前任务到达 0-lag 点前免受唤醒抢占，
  保证至少跑出一个有意义 quantum。`vprot`（`se->vprot`）是其保护边界。

- **sched_entity（调度实体）**：`struct sched_entity`（`include/linux/sched.h:575`）。
  既可是任务也可是任务组，是 EEVDF 计算的载体。

- **slice（请求时间片，r_i）**：`se->slice`（`include/linux/sched.h:599`）。任务这一轮想跑多久。
  默认 base_slice，可经 `sched_setattr(sched_runtime)` 自定义。小 slice → 早 deadline → 低延迟。

- **throttle（限流）**：CFS 带宽控制下，组用尽 quota 后被整体停跑
  （`throttle_cfs_rq`，`kernel/sched/fair.c:6239`），直到下个 period 补充配额才解限流。

- **vruntime（虚拟运行时间，v_i）**：`se->vruntime`（`include/linux/sched.h:594`）。
  按权重归一化的累计运行时间，`+= calc_delta_fair(delta_exec)`。CFS 与 EEVDF 共有。

- **V（加权平均虚拟时间，avg_vruntime）**：`V = Σ(w_i·v_i)/Σw_i`，eligible 判定的基准。
  用相对量 `sum_w_vruntime`/`sum_weight`/`zero_vruntime` 防溢出地计算
  （`avg_vruntime`，`kernel/sched/fair.c:780`）。

- **zero_vruntime（v0，相对基准点）**：`cfs_rq->zero_vruntime`（`kernel/sched/sched.h:687`）。
  V 计算的相对原点，随平均缓慢推进，保证 `v_i - v0` 维持小量级避免 u64 乘法溢出。

---

## 附录 D EEVDF 数学符号与公式速查

| 符号 | 含义 | 代码对应 |
| --- | --- | --- |
| `v_i` | 任务 i 的虚拟运行时间 | `se->vruntime` |
| `w_i` | 任务 i 的权重（nice 映射） | `se->load.weight` |
| `W` | 队列总权重 `Σ w_i` | `cfs_rq->load` / `sum_weight` |
| `V` | 加权平均虚拟时间 | `avg_vruntime(cfs_rq)` |
| `s_i` | 实际服务时间 | `se->sum_exec_runtime`（真实） |
| `S` | 理想服务时间 | （隐含） |
| `lag_i` | 滞后 = `S - s_i = w_i(V - v_i)` | `se->vlag`（虚拟形式 `V - v_i`） |
| `r_i` | 请求时间片 | `se->slice` |
| `ve_i` | eligible 起点虚拟时间 | ≈ `se->vruntime` |
| `vd_i` | 虚拟截止时间 | `se->deadline` |
| `v0` | 相对基准点 | `cfs_rq->zero_vruntime` |

**核心公式：**

```text
lag 守恒：       Σ w_i · lag_i = 0
虚拟 lag：       vl_i = V - v_i
加权平均：       V = Σ(w_i·v_i) / W = Σ((v_i-v0)·w_i)/W + v0
eligible：       lag_i ≥ 0  ⟺  v_i ≤ V
                 等价无除法形式：Σ(v_i-v0)·w_i ≥ (v_query-v0)·W
虚拟截止时间：   vd_i = ve_i + r_i/w_i
真实→虚拟换算：  Δv = Δ_real · NICE_0_LOAD / w_i   = calc_delta_fair(Δ_real, se)
入队保 lag：     v_i = V - vl_i（vl_i 需预放大 (W+w_i)/W 倍）
选择规则：       next = argmin{ vd_i : i ∈ eligible }
```

**PELT 公式：**

```text
几何级数负载：   load = u_0 + u_1·y + u_2·y² + ...,   y³² = 0.5
衰减：           decay_load(val, n) = val · y^n
                 = val · (1/2)^(n/32) · y^(n%32)
半衰期：         ~32ms（LOAD_AVG_PERIOD = 32）
满载常数：       LOAD_AVG_MAX ≈ 47742
```

---

## 附录 E 关键函数速查索引

按功能域归类，便于按图索源。位置均为 Linux 6.18.5。

**核心算法（kernel/sched/fair.c）：**

| 函数 | 行 | 作用 |
| --- | --- | --- |
| `calc_delta_fair` | 297 | 真实时间 → 虚拟时间（÷权重） |
| `__calc_delta` | 261 | 定点乘移位实现（防溢出） |
| `entity_key` | 614 | `v_i - v0` |
| `avg_vruntime` | 780 | 加权平均 V（防溢出） |
| `update_zero_vruntime` | 758 | 推进相对基准 v0 |
| `entity_lag` | 832 | 裸 lag（含限幅） |
| `update_entity_lag` | 858 | 算 lag 并缓存进 se->vlag |
| `vruntime_eligible` | 894 | eligible 判定（无除法） |
| `entity_eligible` | 939 | 对实体的 eligible 封装 |
| `entity_before` | 589 | 按 deadline 比较（树排序键） |
| `min_vruntime_update` | 1010 | 增广树聚合回调 |
| `__enqueue_entity` / `__dequeue_entity` | 1040 / 1049 | 红黑树插入/删除 |
| `set_protect_slice` / `protect_slice` | 1084 / 1106 | RUN_TO_PARITY 保护窗 |
| **`pick_eevdf`** | **1136** | **EEVDF 选择核心** |
| `update_deadline` | 1238 | slice 满则推进 deadline |
| `update_se` / `update_curr` | 1353 / 1407 | 记账：算 delta / 推进 vruntime |
| `place_entity` | 5380 | 入队定位（lag 复原 + 定 deadline） |
| `enqueue_entity` / `dequeue_entity` | 5523 / 5646 | 实体入队/出队 |
| `set_next_entity` / `put_prev_entity` | 5729 / 5798 | 上/下 CPU |
| `pick_next_entity` | 5780 | 调 pick_eevdf + 处理延迟出队 |
| `entity_tick` | 5821 | 周期 tick 内的记账与抢占 |

**调度类钩子与主流程：**

| 函数 | 位置 | 作用 |
| --- | --- | --- |
| `__schedule` | `core.c:7017` | 调度主循环 |
| `pick_next_task_fair` | `fair.c:9234` | 公平类选择（最小化组切换） |
| `pick_task_fair` | `fair.c:9197` | 只选实体无副作用 |
| `task_tick_fair` | `fair.c:13687` | 周期抢占入口 |
| `task_fork_fair` | `fair.c:13714` | fork hook（本版精简） |
| `update_curr_fair` | `fair.c:1455` | update_curr 封装 |
| `set_next_task_fair` | `fair.c:13890` | 设定 curr |
| `select_task_rq_fair` | `fair.c:8829` | 唤醒选核 |
| `reweight_entity` | `fair.c:4068` | 改权重保公平 |
| `DEFINE_SCHED_CLASS(fair)` | `fair.c:14207` | 公平类实例 |

**负载均衡：**

| 函数 | 位置 | 作用 |
| --- | --- | --- |
| `sched_balance_softirq` | `fair.c:13333` | 周期均衡软中断 |
| `sched_balance_domains` | `fair.c:12590` | 遍历调度域 |
| `sched_balance_rq` | `fair.c:12101` | 单域均衡 |
| `sched_balance_find_src_group` | `fair.c:11645` | 找最忙 group |
| `sched_balance_find_src_rq` | `fair.c:11783` | 找最忙 rq |
| `calculate_imbalance` | `fair.c:11443` | 算不平衡量与迁移类型 |
| `detach_tasks` / `attach_tasks` | `fair.c:9904` / `10043` | 摘/挂任务 |
| `can_migrate_task` | `fair.c:9740` | 迁移闸门 |
| `task_h_load` | `fair.c:10207` | 层级负载贡献 |
| `wake_affine` | `fair.c:7670` | 唤醒亲和判定 |
| `sched_balance_newidle` | `fair.c:5061` | CPU 将空闲时拉任务 |
| `active_load_balance_cpu_stop` | `fair.c:12431` | 硬迁正在跑的任务 |

**组调度与带宽（kernel/sched/fair.c）：**

| 函数 | 位置 | 作用 |
| --- | --- | --- |
| `update_cfs_group` | 4243 | 组权重动态分配 |
| `account_entity_enqueue/dequeue` | 3875 / 3888 | 队列权重记账 |
| `__account_cfs_rq_runtime` | 5957 | 配额消费 |
| `__assign_cfs_rq_runtime` | 5917 | 本地领配额 |
| `check_cfs_rq_runtime` | 6711 | 触发限流判断 |
| `throttle_cfs_rq` | 6239 | 限流 |
| `unthrottle_cfs_rq` | 6280 | 解限流 |
| `__refill_cfs_bandwidth_runtime` | 5893 | 周期补充配额 |
| `sched_cfs_period_timer` | 6739 | 周期定时器回调 |

**PELT（kernel/sched/pelt.c）：**

| 函数 | 位置 | 作用 |
| --- | --- | --- |
| `decay_load` | 32 | 几何衰减 val·y^n |
| `__accumulate_pelt_segments` | 58 | 三段求和的闭式部分 |
| `accumulate_sum` | 102 | 跨段累加 *_sum |
| `___update_load_sum` | 180 | 内部 sum 更新 |
| `___update_load_avg` | 257 | sum → avg |
| `__update_load_avg_se` | 307 | 实体级负载更新 |
| `__update_load_avg_cfs_rq` | 321 | 队列级负载更新 |
| `__update_load_avg_blocked_se` | 296 | 阻塞实体负载衰减 |

**数据结构：**

| 结构 | 位置 |
| --- | --- |
| `struct sched_entity` | `include/linux/sched.h:575` |
| `struct sched_avg` | `include/linux/sched.h:510` |
| `struct cfs_rq` | `kernel/sched/sched.h:678` |
| `struct rq` | `kernel/sched/sched.h:1131` |
| `struct cfs_bandwidth` | `kernel/sched/sched.h:447` |
| `struct task_group` | `kernel/sched/sched.h:474` |
| `enum group_type` | `kernel/sched/fair.c:9532` |
| `sched_prio_to_weight[]` | `kernel/sched/core.c:10569` |

---

## 附录 F 调度器输出字段解读

把本文的概念映射到真实系统输出，便于实战排查。

### F.1 /sys/kernel/debug/sched/debug

该文件由 `kernel/sched/debug.c` 生成，逐 CPU 打印 `cfs_rq` 与每个任务的调度状态。
关键字段（不同版本略有差异，以实际输出为准）：

**cfs_rq 级：**

| 字段 | 含义 | 本文对应 |
| --- | --- | --- |
| `.nr_running` / `.h_nr_queued` | 本队列/层级在队任务数 | `cfs_rq->nr_queued` / `h_nr_queued`（1.3） |
| `.load` | 队列总权重 `Σ w_i` | `cfs_rq->load`（7.1） |
| `.min_vruntime` | 队列基准虚拟时间 | 与 `zero_vruntime` 相关（1.3、2.2） |
| `.avg_vruntime` / `.avg_load` | 加权平均相关聚合 | `sum_w_vruntime`/`sum_weight`（2.2） |
| `.util_avg` / `.load_avg` / `.runnable_avg` | PELT 三量 | `cfs_rq->avg`（第 5 章） |
| `.throttled` / `.throttle_count` | 带宽限流状态 | `cfs_rq->throttled`（4.5） |

**任务级（每行一个任务）：**

| 字段 | 含义 | 本文对应 |
| --- | --- | --- |
| `tree-key` | 任务在红黑树的排序键（deadline 相关） | `se->deadline`（2.6） |
| `vruntime` | 虚拟运行时间 | `se->vruntime`（1.1、2.5） |
| `deadline` | 虚拟截止时间 | `se->deadline`（2.1.4） |
| `vlag` | 近似虚拟 lag | `se->vlag`（2.3）；**负值=该任务超额、暂不 eligible** |
| `slice` | 请求时间片 | `se->slice`（1.1、8.1） |
| `sum-exec` | 累计真实运行时间 | `se->sum_exec_runtime` |
| `switches` | 上下文切换次数 | nvcsw + nivcsw |

> **排查口诀**：任务"该跑不跑"先看它的 `vlag`——若是较大负值，说明它最近超额消费了 CPU，
> EEVDF 正让它"服刑"（ineligible），属正常公平行为；若所在组 `throttled=1`，
> 则是被 cpu.max 限流（看 `cpu.stat`）。

### F.2 /proc/<pid>/schedstat 与 /proc/schedstat

需先 `echo 1 > /proc/sys/kernel/sched_schedstats` 开启（字段含义见
`Documentation/scheduler/sched-stats.rst`）。

`/proc/<pid>/schedstat` 三个数：

```text
<在 CPU 上运行的总时间ns>  <在队列等待的总时间ns>  <时间片数(被调度上 CPU 次数)>
```

- 第二个数（等待时间）持续偏大 → 任务经常排队等不到 CPU（CPU 竞争激烈或被限流）。
- 第三个数（time slices）很大而运行时间不大 → 任务被频繁切换（可能上下文切换开销大）。

`/proc/schedstat` 则是每 CPU、每调度域的均衡统计（`lb_count`/`lb_balanced`/
`lb_failed` 等），用于诊断负载均衡是否健康。

### F.3 perf sched 输出

```text
perf sched latency  的关键列：
  Task            : 任务名
  Runtime         : 累计运行时间
  Switches        : 切换次数
  Avg delay       : 平均唤醒延迟（被唤醒到真正上 CPU 的间隔）★延迟敏感
  Max delay       : 最大唤醒延迟
```

`Avg/Max delay` 是评估"调度延迟"最直接的指标。若某延迟敏感任务 delay 偏大，
可尝试：减小它的 `sched_runtime`（slice）、检查 `RUN_TO_PARITY`/`PREEMPT_SHORT`、
确认它没被 RT 任务挤压（看 fair_server 是否生效）、检查 CPU 亲和与 newidle 均衡。

### F.4 cgroup cpu.stat（带宽限流）

```text
usage_usec     : 累计 CPU 用量
nr_periods     : 经历的计费周期数            ← cfs_b->nr_periods
nr_throttled   : 发生限流的周期数            ← cfs_b->nr_throttled
throttled_usec : 累计被限流时长              ← cfs_b->throttled_time
nr_bursts      : 突发次数                    ← cfs_b->nr_burst
burst_usec     : 累计突发时长
```

`nr_throttled` / `nr_periods` 比值高 → 该 cgroup 经常撞到 `cpu.max` 上限被限流，
若非预期可调大 quota 或排查是否有突发负载（考虑配置 `cpu.max.burst`）。





