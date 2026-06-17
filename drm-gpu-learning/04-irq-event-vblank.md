# 第 04 章 中断、事件与 vblank 底层

> 所属：第二篇 DRM 核心　|　前置：第 00–03 章　|　代码基线：Linux `7.1.0-rc6`　参考驱动：**Xe**
>
> 第 03 章建立了 DRM 的**同步**入口——ioctl：用户发请求、内核处理、返回结果。但 GPU 与显示天生
> **异步**：vblank（帧边界）何时到、翻页（page flip）何时完成、命令何时执行完，都不是 ioctl 能"原地
> 等到"的。本章讲 DRM 的**异步事件通路**：硬件中断（第 01 章 MSI-X）如何变成内核里的 vblank 记账，
> 又如何打包成 `drm_event` 投递给用户态的 `poll`/`read`。这条通路直接挂在第 03 章的 `drm_file` 事件
> 队列上，并且是第 06 章"原子翻页"的底层支撑——理解它，才能理解"合成器如何精确地每帧翻页且不撕裂"。

---

## 4.1 学习目标与前置依赖

### 学习目标

学完本章，你应当能够：

1. 区分 DRM 的**同步通路（ioctl）**与**异步通路（事件）**，说清各自解决什么问题、为什么都需要。
2. 讲清 **vblank（垂直消隐）** 是什么、为什么是显示系统的"心跳"，以及它与 page flip 的关系。
3. 逐字段说清 `drm_vblank_crtc`（vblank 记账状态）与 `drm_pending_event`/`drm_event`（事件对象）。
4. 沿代码讲清**中断 → vblank 记账 → 事件投递**全链路：`drm_crtc_handle_vblank` → 更新
   count/timestamp（seqlock）→ `drm_crtc_send_vblank_event` → `drm_send_event`（入队 + 唤醒）→
   用户态 `drm_read`/`drm_poll`。
5. 解释 vblank 的**计数与回绕处理、高精度时间戳、seqlock 无锁读、refcount + 禁用迟滞**等机制。
6. 说清 Xe 的中断如何路由到显示子系统（`xe_display_irq_handler`）并最终触发 `drm_crtc_handle_vblank`。
7. 走通一次"带 `DRM_MODE_PAGE_FLIP_EVENT` 的翻页 → vblank → 完成事件投递"的端到端流程（为第 06 章铺垫）。

### 前置依赖

- 第 01 章（MSI-X 中断、`xelp_irq_handler` 分层 demux）、第 02 章（spinlock/seqlock、wait_queue、
  workqueue、kref）、第 03 章（`drm_file` 的事件队列字段、minor）。

> vblank/事件本质属于显示（KMS）范畴；Xe 的显示是与 i915 共享的 `display/` 子系统。本章聚焦**通用
> 的事件/vblank 底层机制**（在 `drm_vblank.c`/`drm_file.c`），Xe 作为"中断如何路由进来"的真实例子；
> 具体显示硬件如何产生 vblank 留到第 21 章。

---

## 4.2 主题导引：为什么需要异步事件

### 4.2.1 同步不够用：异步是显示/GPU 的本质

第 03 章的 ioctl 是"问—答"式同步：发一个请求、原地拿到结果。但很多事情你**无法原地等**：

- "**这一帧什么时候开始扫描？**"（vblank）——这是硬件按刷新率（如 60Hz，每 16.6ms）自己产生的节拍，
  不是你能"调用"出来的。
- "**我提交的翻页什么时候真正生效（显示在屏幕上）？**"（flip done）——必须等到下一个 vblank。
- "**GPU 把这批命令算完了吗？**"——异步完成（第 10 章 fence，第 19 章提交）。

如果用同步：要么**忙等**（busy-poll 寄存器，烧 CPU、不可扩展），要么**阻塞**（线程卡死等一个事件，
无法同时干别的）。现代合成器要同时管很多窗口/输出、还要响应输入，必须**非阻塞 + 事件驱动**：提交一个
请求并说"完成时给我发个事件"，然后回到主循环 `poll` 多个 fd，事件到了再处理。

### 4.2.2 历史演进：从阻塞 WAIT_VBLANK 到事件化

DRM 的 vblank 接口经历了演进（呼应第 00 章演进主题）：

- 早期：`DRM_IOCTL_WAIT_VBLANK` **阻塞**等到某个 vblank 序号——简单但占住调用线程。
- 现在：`WAIT_VBLANK` 也支持**事件模式**（`_DRM_VBLANK_EVENT`），以及 page flip 的
  `DRM_MODE_PAGE_FLIP_EVENT`/atomic 的 `DRM_MODE_ATOMIC_EVENT`——提交时声明"完成发事件"，内核在
  vblank 时把事件投到 `drm_file` 事件队列，用户态用 `poll`+`read` 取。

事件化让"一个线程驱动多个输出、多个 fd"成为可能——这是 Wayland 合成器、X 的现代显示循环的基础。

### 4.2.3 vblank：显示系统的心跳

**vblank（vertical blanking，垂直消隐）** 源自 CRT 时代：电子束扫完一帧、回到左上角准备下一帧的
那段"空白期"。今天虽无电子束，但显示控制器仍以固定节拍（刷新率）逐帧扫描 framebuffer，每帧边界
产生一次 vblank 中断。它的意义：

- **它是改显示内容的安全窗口**：在 vblank 期间切换 framebuffer（翻页），不会出现"上半屏旧帧、下半屏
  新帧"的撕裂（tearing）。
- **它是时间基准**：合成器据 vblank 节拍调度渲染、计算 presentation time。
- **它是"翻页完成"的判据**：你提交的翻页，在下一个 vblank 真正生效，内核此时发 flip-done 事件。

所以本章副标题可以是"显示系统如何打节拍、并把节拍告诉用户态"。

---

## 4.3 原理与全景：中断 → vblank 记账 → 事件投递

```
[显示硬件] 每帧边界产生 vblank 中断 (MSI-X, 第01章)
     │
     ▼
xelp_irq_handler → xe_display_irq_handler(xe, master_ctl)     [xe_irq.c / xe display]
     │  (分层 demux 到显示, 第01章 1.5.4)
     ▼
drm_crtc_handle_vblank(crtc)                                  [drm_vblank.c]
     │  ① drm_update_vblank_count(): 更新 count + timestamp (seqlock 写)
     │  ② 唤醒 vblank 等待者 (wait_queue)
     │  ③ 投递到期的 vblank/flip 事件:
     ▼
drm_crtc_send_vblank_event(crtc, e)                          [drm_vblank.c]
     │  填入当前 seq/timestamp
     ▼
drm_send_event(dev, e)                                       [drm_file.c]
     │  把 drm_pending_event 从 pending_event_list 移到 event_list
     │  wake_up_interruptible(&file->event_wait)
     ▼
[用户态] poll(fd) 返回可读 → read(fd) 取出 drm_event
     │  drm_poll / drm_read                                   [drm_file.c]
     ▼
合成器据事件(vblank 序号/时间戳, 或 flip 完成)继续下一帧
```

这条链有两个清晰的阶段：**(A) 记账**（`drm_crtc_handle_vblank` 更新 count/time，供任何人查询当前
vblank 状态）和 **(B) 投递**（把"某人预约的事件"在到期时送到其 `drm_file` 队列）。本章 4.5 沿这两段
走读代码。

---

## 4.4 关键数据结构

### 4.4.1 `struct drm_vblank_crtc`：每 CRTC 的 vblank 记账

每个 CRTC（扫描引擎）有一份 vblank 记账（`include/drm/drm_vblank.h`）。核心字段：

| 字段 | 类型 | 含义 | 不变量/锁 |
|------|------|------|-----------|
| `count` | `atomic64_t` | **软件 vblank 计数器**（单调递增） | **禁止直接读**，必须用 `drm_crtc_vblank_count()`（保证内存屏障，见 4.6.4） |
| `time` | `ktime_t` | 与 `count` 对应的 vblank 时间戳 | 与 count 一起被 `seqlock` 保护 |
| `seqlock` | `seqlock_t` | 保护 count + time 的**顺序锁** | 读者可无锁重试读，写者（中断里）独占（4.6.3） |
| `queue` | `wait_queue_head_t` | vblank 等待者队列（阻塞等 vblank 的线程睡这里） | 中断到达时 `wake_up` |
| `refcount` | `atomic_t` | 有多少用户/等待者需要 vblank 中断 | 归零才可关中断（配 `disable_timer` 迟滞，4.6.5） |
| `disable_timer` | `timer_list` | 延迟关 vblank 中断的迟滞定时器 | 避免频繁开关中断 |
| `last` | `u32` | 上次读到的硬件 vblank 寄存器值 | 用于回绕处理（4.6.1） |
| `max_vblank_count` | `u32` | 硬件 vblank 计数器的最大值（+1 即回绕） | 0 表示用高精度时间戳估算 |

**为什么 count 是 `atomic64_t` 还要 seqlock**：seqlock 保证"count 与 time 这一对"被一致地读到
（不会读到新 count 配旧 time）；atomic64 + 访问器额外提供**内存屏障语义**——见 4.6.4 这个微妙但重要
的保证。

### 4.4.2 `struct drm_pending_event` 与 `drm_event`：一个待投递事件

事件对象分两层（`include/drm/drm_file.h`、`include/uapi/drm/drm.h`）：

| 结构 | 角色 |
|------|------|
| `struct drm_event`（uAPI） | 投到用户态的**裸数据头**：`type` + `length`，后接具体事件体（如 vblank 事件含序号/时间戳） |
| `struct drm_pending_event`（内核） | 内核侧的"待投递事件"包装：含指向 `drm_event` 的指针、所属 `drm_file`、链表节点 `link`、可选 `completion` |
| `struct drm_pending_vblank_event` | vblank/flip 专用：内嵌 `drm_pending_event` + vblank 事件体 |

`drm_pending_event` 关键字段：

- `completion`：可选的内核内部完成量——`drm_send_event` 时 `complete()`，用于内核侧非阻塞操作的同步
  （如 atomic commit 自己想知道事件已发，第 06 章）。
- `link`：挂进 `drm_file` 的两个链表之一（见 4.4.3）。
- `file_priv`：这个事件该投给哪个 `drm_file`。

### 4.4.3 `drm_file` 的两个事件链表（接第 03 章）

第 03 章 `drm_file_alloc` 里 `INIT_LIST_HEAD` 了两个链表（`drm_file.c:155`）：

| 链表 | 含义 |
|------|------|
| `pending_event_list` | **已预约、尚未到期**的事件（如"等下一个 vblank 翻页完成"——预约了但 vblank 还没来） |
| `event_list` | **已到期、待用户读取**的事件（vblank 到了，事件从 pending 移到这里，等 `read`） |
| `event_wait`（wait_queue） | 用户态 `poll`/`read` 睡在这里，事件入 `event_list` 时被唤醒 |

事件的一生：预约 → 进 `pending_event_list` →（vblank 到，`drm_send_event`）→ 移到 `event_list` +
唤醒 → 用户 `read` 取走。这就是 4.3 全景图 (B) 段的数据结构落点。

---

## 4.5 核心代码走读

### 4.5.1 中断进显示：`xe_display_irq_handler`

第 01 章 1.5.4 的 `xelp_irq_handler` 在分层 demux 时调了 `xe_display_irq_handler(xe, master_ctl)`
（`drivers/gpu/drm/xe/display/xe_display.c:209`）。它进一步把显示中断细分（pipe/端口/vblank…），
对每个发生 vblank 的 pipe，最终调用通用的 `drm_crtc_handle_vblank()`。

> 关键认知：**Xe 不自己实现 vblank 记账与事件投递**——它只负责"把硬件 vblank 中断翻译出来、调通用的
> `drm_crtc_handle_vblank`"。记账与投递全在 DRM 核心（`drm_vblank.c`/`drm_file.c`）。这又是第 00 章
> 0.7"通用核心 + 驱动特化"的体现：驱动管硬件中断源，核心管 vblank 语义。

### 4.5.2 记账核心：`drm_crtc_handle_vblank` → `drm_update_vblank_count`

`drm_crtc_handle_vblank(crtc)`（`drm_vblank.c`，驱动在 vblank 中断里调它）做三件事：更新计数、唤醒
等待者、投递到期事件。计数更新的核心是 `drm_update_vblank_count`（`drm_vblank.c:295`）与
`store_vblank`（:191）：

```c
// drivers/gpu/drm/drm_vblank.c:191 (概念节选)
static void store_vblank(struct drm_device *dev, unsigned int pipe,
                         u32 vblank_count_inc, ktime_t t_vblank, u32 cur_vblank)
{
    struct drm_vblank_crtc *vblank = drm_vblank_crtc(dev, pipe);
    write_seqlock(&vblank->seqlock);                 // (1) seqlock 写端: 开始
    vblank->time = t_vblank;                          // (2) 更新时间戳
    atomic64_add(vblank_count_inc, &vblank->count);   // (3) 增加计数
    write_sequnlock(&vblank->seqlock);               // (4) seqlock 写端: 结束
}
```

`drm_update_vblank_count`（:295）负责算出"距上次过了几个 vblank"（处理硬件计数器**回绕**、用高精度
时间戳填补中断关闭期间漏掉的 vblank），再调 `store_vblank` 一次性更新 count+time。**用 seqlock 写、
让读者无锁读**，是因为 vblank 计数被极高频查询（每个等翻页的合成器都在查），而更新只在中断里发生——
典型的"读多写少且读要快"（第 02 章为何不全用普通锁）。

### 4.5.3 投递 vblank/flip 事件：`drm_crtc_send_vblank_event` → `drm_send_event`

`drm_crtc_handle_vblank` 在更新计数后，检查 `pending_event_list` 里有没有"预约到这一帧"的事件，到期
的就投递。投递两步：

```c
// 1) 填好事件体(当前 vblank 序号 + 时间戳), 见 drm_crtc_send_vblank_event (drm_vblank.c)
drm_crtc_send_vblank_event(crtc, e);   // 内部填 e 的 sequence/tv_sec/tv_usec, 再调 drm_send_event

// 2) drm_send_event (drm_file.c): 真正投递
void drm_send_event_helper(...)        // 概念:
{
    list_del(&e->pending_link);                       // 从 pending_event_list 摘除
    list_add_tail(&e->link, &e->file_priv->event_list); // 加入 event_list(可读队列)
    if (e->completion) complete_all(e->completion);    // 唤醒内核内部等待者(可选)
    wake_up_interruptible_poll(&e->file_priv->event_wait, EPOLLIN);  // 唤醒用户态 poll/read
}
```

这就是 4.4.3 "pending → event_list + 唤醒"的代码。一旦事件进了 `event_list` 并唤醒，用户态那边
阻塞在 `poll`/`read` 的合成器就醒来。

### 4.5.4 用户态取事件：`drm_read` 与 `drm_poll`

`drm_file.fops` 把 `.read`/`.poll` 指向 DRM 核心的 `drm_read`/`drm_poll`（`drm_file.c`，第 03 章
`DEFINE_DRM_GEM_FOPS` 等会带上）：

- **`drm_poll`**：检查 `event_list` 是否非空；空则把当前线程挂到 `event_wait` 等待队列、返回"暂不可
  读"。事件到达唤醒后，`poll` 返回 `EPOLLIN`。
- **`drm_read`（`drm_file.c:540`）**：从 `event_list` 取出一个 `drm_event`，`copy_to_user` 给用户
  缓冲；无事件且非阻塞则返回 `-EAGAIN`，阻塞则睡在 `event_wait`。

于是合成器的主循环就是经典的：`poll([drm_fd, ...])` → 可读 → `read(drm_fd)` 得到一个 `drm_event`
（比如"vblank 序号 12345，时间戳 T，你的翻页完成了"）→ 据此渲染并提交下一帧。

### 4.5.5 把 IRQ 接进来：vblank 的开启与引用计数

vblank 中断不是一直开着的（开着费电）。谁需要 vblank（有等待者或有预约事件），就
`drm_crtc_vblank_get()` 增加 `refcount`、必要时打开硬件 vblank 中断；用完 `drm_crtc_vblank_put()`
减 refcount。refcount 归零后，**不立即关**，而是用 `disable_timer` 延迟一会儿再关（迟滞），避免
"频繁开关中断"的抖动（4.6.5）。`drm_crtc_vblank_on/off`（`drm_vblank.c`）在 CRTC 启用/禁用时整体
开关 vblank。

---

## 4.6 机制深入

### 4.6.1 vblank 计数与回绕（wraparound）

硬件 vblank 计数器位宽有限（如 32 位甚至更少），会**回绕**。`drm_update_vblank_count` 的职责之一就是
把"硬件寄存器读数"转换成"单调递增的软件 count"：

- 读当前硬件值 `cur_vblank`，与上次 `last` 比，算出增量 `diff`（处理 `max_vblank_count` 回绕）。
- 若 vblank 中断曾被关闭一段时间（4.6.5），硬件计数可能不可靠或已多次回绕，则用**高精度时间戳**
  估算这段时间过了多少个 vblank（据刷新周期）。
- 把 `diff` 累加到软件 `count`（`store_vblank`）。

`max_vblank_count == 0` 的驱动（硬件不暴露可靠 vblank 计数器）完全靠时间戳估算——注释明确说这"有
小的竞态与长期漂移，强烈建议硬件暴露 vblank 计数器"。这是"有硬件支持就用硬件、没有才软件兜底"的
典型工程权衡。

### 4.6.2 高精度时间戳：presentation time 的基础

合成器需要知道"这一帧将在何时显示"以做平滑动画/音视频同步。vblank 时间戳要尽量精确地对应"扫描开始
那一刻"。DRM 提供 `drm_crtc_vblank_count_and_time()` 同时拿到 count 与对应 time。驱动可提供
`get_vblank_timestamp` 回调做更精确的"扫描线位置 → 时刻"换算。这套时间戳是 `WAIT_VBLANK`、page flip
事件里返回给用户态的 `tv_sec/tv_usec`，也是 Wayland presentation-time 协议的内核基础。

### 4.6.3 seqlock：为什么不用普通锁/RCU

count+time 的读写选了 **seqlock**（第 02 章提过的锁家族成员），原因：

- **写极少（只在中断里）、读极多（每个查询者）**：适合读优化。
- **写者不能被读者阻塞**：中断里更新计数绝不能等读者——seqlock 写端不被读端阻塞。
- **读者可无锁重试**：读端读一个序号、读数据、再读序号；若序号变了（写在中途）就重读。无需加锁、
  无需等待。
- 相比 RCU：seqlock 更适合"读一小撮强一致的标量（count+time）",且写在中断上下文。RCU 更适合
  "无锁遍历/释放对象"（如 fence，第 10 章）。**选 seqlock vs RCU 是按数据形态定的**。

### 4.6.4 一个微妙的内存屏障保证

`drm_vblank_crtc.count` 的注释强调：**绝不要直接读 `count`，要用 `drm_crtc_vblank_count()`**。原因
不只是 seqlock 一致性，还有一个**屏障保证**：

> "Any writes done before calling `drm_crtc_handle_vblank()` will be visible to callers of
> `drm_crtc_vblank_count()` … iff the vblank count is the same or a later one."

即：驱动在调 `drm_crtc_handle_vblank` 之前对其他内存做的写，对"看到相同或更晚 vblank count"的读者
**保证可见**。这让"翻页在某个 vblank 生效"这类跨线程可见性有据可依（第 06 章 commit 依赖它）。这正是
第 01 章 1.6"内存序"主题在 vblank 上的体现——访问器封装了必要的屏障，绕过访问器直接读就破坏了这个
保证。**这是"为什么必须用访问器而不能直接读字段"的深层原因。**

### 4.6.5 vblank 中断的开关迟滞（hysteresis）

vblank 中断开着费电、关了又会漏计数。DRM 用"引用计数 + 迟滞定时器"平衡：

- 有需求（`drm_crtc_vblank_get`）→ refcount>0 → 开中断。
- 需求归零（`_put`）→ **不立即关**，启动 `disable_timer`（延迟 `offdelay_ms`）。
- 定时器到期前若又有需求 → 取消定时器、继续开着。
- 定时器到期仍无需求 → 关中断，并记录此刻状态以便下次开时用时间戳补算漏掉的 vblank（4.6.1）。

这避免了"每帧开关一次中断"的高频抖动——典型的"用一点延迟换掉大量开关开销"的量化权衡（4.7）。

---

## 4.7 量化分析

| 量 | 量级 | 说明 |
|----|------|------|
| vblank 节拍 | 60Hz=16.6ms / 120Hz=8.3ms / 144Hz≈6.9ms | 刷新率决定，是显示循环的根本时间预算 |
| 事件投递延迟 | 中断到 `wake_up` 到用户 `read` 返回：微秒级 | 远小于一帧，故合成器能在 vblank 后及时渲染下一帧 |
| seqlock 读一次 count+time | 纳秒级（无锁，偶尔重试） | 故可被每个查询者高频调用而不成瓶颈（4.6.3） |
| vblank 中断频率 | = 刷新率 × 活跃 CRTC 数 | 多屏高刷时中断频繁，故有迟滞关中断省电（4.6.5） |
| 禁用迟滞 `offdelay_ms` | 毫秒级 | 太短=频繁开关抖动；太长=空闲时多耗电。折中值 |

**可操作结论**：

1. **一帧的时间预算由 vblank 节拍决定**（60Hz=16.6ms）。合成器必须在收到 vblank 事件后、下一个
   vblank 前完成"渲染 + 提交"，否则掉帧。这是第 06 章 atomic 非阻塞 commit 设计的根本约束。
2. **事件化 + seqlock 让"一个线程伺候多输出高刷"可行**：读 vblank 状态近零成本、事件投递微秒级，
   合成器主循环 `poll` 多 fd 不会被同步开销拖垮。这是 4.2 "为什么要异步事件"的量化背书。

---

## 4.8 硬件视角：Xe 的 vblank 从哪来

- vblank 中断源是**显示控制器的 pipe/transcoder**（第 21 章）：它逐帧扫描 framebuffer，每到帧边界
  产生中断。Xe 的中断 demux（第 01 章 1.5.4）把它路由到 `xe_display_irq_handler`（4.5.1），后者按
  pipe 调 `drm_crtc_handle_vblank`。
- vblank 计数器寄存器（若硬件提供）由驱动的 `get_vblank_counter` 回调读出，喂给
  `drm_update_vblank_count` 做精确计数（4.6.1）；时间戳精度由 `get_vblank_timestamp` 提升（4.6.2）。
- Xe/i915 共享的 display 子系统实现这些回调，对照 Bspec 的显示 pipe/中断章节（第 21 章会查具体寄存器
  与 Bspec 编号）。

> 所以本章的"通用机制"（记账/投递/seqlock/迟滞）对所有 KMS 驱动一致；"硬件如何产生 vblank、计数器
> 在哪个寄存器"才是 Xe/显示特定的，留待第 21 章。

---

## 4.9 综合实例：一次非阻塞翻页的事件流（放在一起看，预接第 06 章）

把本章机制串进一次真实的"带完成事件的翻页"。这是合成器每帧都在做的事，也是第 06 章 atomic 的入口。

### 4.9.1 用户态视角

```c
// 合成器主循环(简化)
struct pollfd pfd = { .fd = drm_fd, .events = POLLIN };
for (;;) {
    render_next_frame();                           // 渲染下一帧到一个 framebuffer
    // 提交翻页, 并要求"完成时发事件"
    drmModePageFlip(drm_fd, crtc, fb_id,
                    DRM_MODE_PAGE_FLIP_EVENT, user_data);   // 或 atomic + DRM_MODE_ATOMIC_EVENT(第06章)
    poll(&pfd, 1, -1);                              // 阻塞等事件(期间可被多个 fd 唤醒)
    char buf[1024];
    read(drm_fd, buf, sizeof(buf));                // 读出 drm_event
    // buf 里是 struct drm_event_vblank: 含 sequence(vblank 序号) + tv_sec/tv_usec(时间戳) + user_data
    // → 表示"翻页已在该 vblank 生效", 据时间戳计划下一帧
}
```

### 4.9.2 内核侧链路（对照 4.3 全景）

```
drmModePageFlip(..., DRM_MODE_PAGE_FLIP_EVENT, user_data)
  └─ ioctl → drm_ioctl(第03章) → page_flip/atomic 处理
       ├─ 分配 drm_pending_vblank_event(含 user_data)
       ├─ drm_crtc_vblank_get()  → refcount++ → 确保 vblank 中断开着(4.5.5)
       └─ 把事件挂到 file->pending_event_list, 预约"下一个 vblank 投递"
  ... 提交翻页到硬件, ioctl 立即返回(非阻塞) ...

[下一个 vblank 中断]
  xe_display_irq_handler → drm_crtc_handle_vblank(crtc)        [4.5.1/4.5.2]
       ├─ drm_update_vblank_count: count++/time(seqlock 写)
       ├─ 翻页已生效 → drm_crtc_send_vblank_event(crtc, e)     [4.5.3]
       │     填 e.sequence=当前count, e.tv_*=time
       └─ drm_send_event: pending_event_list → event_list + wake_up(event_wait)
  ... drm_crtc_vblank_put() → refcount-- (4.5.5/4.6.5) ...

[用户态] poll 返回 POLLIN → read(drm_fd) 取出该 drm_event_vblank
```

**这条链把本书前几章全用上了**：第 01 章中断 → 第 03 章 ioctl 入口与 `drm_file` → 第 02 章 seqlock/
wait_queue/kref → 本章 vblank 记账与事件投递。它也是第 06 章原子翻页的底层——atomic commit 用
`DRM_MODE_ATOMIC_EVENT` 走的就是同一套事件机制，只是提交的是"一整套状态"而非单个翻页。

### 4.9.3 观察手段

- `drm.debug` 的 VBL 位（第 00 章 0.11，`0x20`）会打印 vblank 相关日志。
- tracepoint：`/sys/kernel/tracing/events/drm/` 下有 `drm_vblank_event`/`drm_vblank_event_queued`/
  `drm_vblank_event_delivered` 等（第 00 章 0.11.ftrace），可逐个看"预约→投递→读取"。
- `strace -e poll,read,ioctl` 一个合成器，能看到 `poll`/`read` 与翻页 ioctl 的交替——4.9.1 主循环的
  真实样子。

---

## 4.10 横向关联

| 本章内容 | 关联章节 |
|----------|----------|
| `drm_file` 事件队列、poll/read | 第 03 章（`drm_file` 字段、fops） |
| 中断 demux → `xe_display_irq_handler` | 第 01 章（MSI-X、`xelp_irq_handler`） |
| seqlock/wait_queue/kref/timer | 第 02 章（内核设施） |
| vblank 作为翻页"心跳"、`DRM_MODE_ATOMIC_EVENT` | 第 06 章（原子翻页、commit 三阶段） |
| vblank 硬件源、pipe/transcoder、计数/时间戳寄存器 | 第 21 章（intel_display） |
| `drm_vblank_work`（vblank 对齐的延迟工作） | 第 06/21 章（在 vblank 时刻做寄存器更新等） |
| presentation timestamp | 第 23 章（用户态 present 协议） |

**跨切面主题**：性能（事件化 + seqlock 支撑高刷多屏）；功耗（vblank 中断迟滞关断）；可靠性（计数回绕
/漏算的兜底）；编程模型（非阻塞 + poll 事件循环）；内存序（vblank count 的屏障保证）。

---

## 4.11 谬误与陷阱 + 调试

### 谬误与陷阱

- **谬误 1：「等 vblank/翻页完成就该忙等寄存器或阻塞线程」。**
  现代做法是事件化 + `poll`：提交时要求发事件，主循环 poll 多 fd。忙等烧 CPU、阻塞使单线程无法
  伺候多输出（4.2.1）。

- **谬误 2：「直接读 `drm_vblank_crtc.count` 就行」。**
  绝不。必须用 `drm_crtc_vblank_count()`——它保证 seqlock 一致性**和**内存屏障语义（4.6.4）。直接读
  会读到撕裂的 count/time，或破坏"vblank 前的写对后续可见"的保证。

- **谬误 3：「vblank 时间戳就是中断处理函数运行的时刻」。**
  不是。时间戳要尽量对应"扫描开始那一刻"，可能由 `get_vblank_timestamp` 据扫描线位置精确换算，而非
  中断进入时刻（4.6.2）。

- **谬误 4：「vblank 中断一直开着」。**
  默认按需开关，refcount 归零后经迟滞定时器关闭以省电（4.6.5）。所以"没人等就没有 vblank 中断"。

- **陷阱 5：`drm_crtc_vblank_get` 与 `_put` 不配对。**
  漏 `_put` → refcount 泄漏 → vblank 中断永不关、费电；多 `_put` → 提前关、漏事件。与 kref 同理
  （第 02 章借用 vs 拥有）。

- **陷阱 6：在中断里做重活。**
  `drm_crtc_handle_vblank` 在中断上下文，只能做记账 + 唤醒 + 投递（轻）。要做寄存器序列等重活，用
  `drm_vblank_work`（对齐到 vblank 的延迟工作，第 06 章）或 workqueue（第 02 章）。

### 调试小抄

| 想知道 | 手段 |
|--------|------|
| vblank 是否在产生、序号/时间戳 | `drm.debug` VBL 位（0x20）；tracepoint `drm/drm_vblank_event*` |
| 翻页事件是否预约/投递/读取 | tracepoint `drm_vblank_event_queued`/`_delivered` |
| 合成器的事件循环 | `strace -e poll,read,ioctl <compositor>` |
| vblank 中断计数 | `/proc/interrupts`（显示中断随刷新率累加，第 01 章 1.9.3） |
| vblank refcount/开关 | `drm.debug` + debugfs；查 `drm_crtc_vblank_get/put` 调用配对 |

---

## 4.12 随堂练习（Practice Problems）

**练习 4.1** 为什么"等下一个 vblank"不能用第 03 章那种同步 ioctl 原地等到？事件化解决了什么？

**练习 4.2** vblank 为什么被称为显示系统的"心跳"？它和 page flip（翻页）有什么关系？

**练习 4.3** 一个 vblank 事件从产生到被用户态读到，依次经过哪几个函数/数据结构（中断→记账→投递→读）？

**练习 4.4** 为什么 `drm_vblank_crtc.count` 必须用 `drm_crtc_vblank_count()` 访问而不能直接读？（两个
理由）

**练习 4.5** vblank 中断为什么不一直开着？refcount 归零后为什么不立即关，而要走迟滞定时器？

**练习 4.6** `drm_file` 的 `pending_event_list` 与 `event_list` 各装什么？一个翻页完成事件何时从前者
移到后者？

**练习 4.7（量化）** 60Hz 刷新下，合成器收到 vblank 事件后到下一个 vblank 有多少时间预算？这对它
"渲染 + 提交下一帧"的耗时有什么约束？

### 随堂练习即时解答

**解 4.1** vblank 由硬件按刷新率自己产生、不是调用能"求"出来的；同步 ioctl 原地等会阻塞调用线程，
使其无法同时伺候其他输出/fd。事件化让"提交+要求发事件+poll 多 fd"成为可能，单线程驱动多输出
（4.2.1）。

**解 4.2** 显示控制器按固定节拍逐帧扫描，每帧边界一次 vblank，是改显示内容的安全窗口、时间基准、
"翻页完成"的判据。翻页在 vblank 时生效、内核此时发 flip-done 事件（4.2.3）。

**解 4.3** 显示中断→`xe_display_irq_handler`→`drm_crtc_handle_vblank`→`drm_update_vblank_count`/
`store_vblank`（seqlock 更新 count+time）→`drm_crtc_send_vblank_event`→`drm_send_event`（pending→
event_list + wake event_wait）→用户 `drm_poll`/`drm_read`（4.3、4.5）。

**解 4.4** ① seqlock 一致性：保证 count 与 time 这一对被一致读到；② 内存屏障保证：vblank 前的写对
"看到相同/更晚 count"的读者可见。直接读破坏二者（4.6.3/4.6.4）。

**解 4.5** 开着费电、且高刷多屏中断频繁。归零不立即关是迟滞（hysteresis）：避免"每帧开关一次"的高频
抖动；定时器到期前若又有需求就继续开着（4.6.5）。

**解 4.6** `pending_event_list` 装已预约未到期的事件；`event_list` 装已到期待读的事件。翻页完成事件
在对应 vblank 到达、`drm_send_event` 时从前者移到后者并唤醒（4.4.3、4.5.3）。

**解 4.7** 16.6ms（1/60s）。合成器必须在这个预算内完成渲染下一帧 + 提交翻页，否则错过下一个 vblank
→ 掉帧。这正是 atomic 非阻塞 commit 要快速返回、把重活异步化的根本约束（4.7、第 06 章）。

---

## 4.13 课后练习题

> 仅题目。答案见 [`answers/04-irq-event-vblank-answers.md`](./answers/04-irq-event-vblank-answers.md)。★ 为动手/综合题。

### L1 概念辨析

**4.13.1** 对比 DRM 的同步通路（ioctl）与异步通路（事件），各举一个典型用例，说明为何二者都必要。

**4.13.2** 解释 vblank 的物理来历与三重意义（安全窗口/时间基准/完成判据）。

**4.13.3** 描述事件的一生：从预约、到期、投递、到用户读取，分别在哪个链表/队列上。

**4.13.4** 为什么 vblank 计数用 seqlock 而不用普通 mutex 或 RCU？从"读多写少 + 写在中断 + 读强一致
标量"三点论证。

**4.13.5** 解释 vblank 中断的"refcount + 迟滞定时器"开关策略要解决的两个矛盾（省电 vs 漏计数、
抖动）。

**4.13.6** `drm_crtc_handle_vblank` 提供的"内存屏障保证"是什么？它为什么要求必须用访问器读 count？

### L2 数据结构与源码阅读

**4.13.7** 阅读 `struct drm_vblank_crtc`（`drm_vblank.h`），列出 count/time/seqlock/queue/refcount/
disable_timer/last/max_vblank_count 各自作用与保护方式。

**4.13.8** 阅读 `store_vblank`（`drm_vblank.c:191`），说明它为何用 `write_seqlock`/`write_sequnlock`
包住 time 与 count 的更新，以及读者侧如何无锁读。

**4.13.9** 阅读 `drm_file_alloc`（`drm_file.c:155`）里对 `pending_event_list`/`event_list` 的初始化，
结合第 03 章说明事件队列如何挂在每文件上下文上。

**4.13.10** 阅读 `drm_read`（`drm_file.c:540`）骨架，说明它如何从 `event_list` 取事件、`copy_to_user`、
以及阻塞/非阻塞行为。

**4.13.11 ★** 阅读 `drm_update_vblank_count`（`drm_vblank.c:295`）处理回绕与"中断关闭期间用时间戳
估算"的逻辑，解释 `max_vblank_count==0` 时为何精度较差。

### L3 机制分析

**4.13.12** 画出一次"带 `DRM_MODE_PAGE_FLIP_EVENT` 的翻页"的完整内核链路（预约 → vblank 中断 →
记账 → 投递 → 用户读），标注用到的第 01/02/03 章设施。

**4.13.13** 解释为什么 `drm_crtc_handle_vblank` 必须保持轻量（只记账/唤醒/投递），要做寄存器序列等
重活应改用什么（`drm_vblank_work`/workqueue），以及原因（中断上下文）。

**4.13.14** seqlock 读端的"读序号—读数据—再读序号、不一致则重读"如何保证读到一致的 count+time 且
不阻塞中断里的写端？

### L4 设计与对比

**4.13.15** 事件化（poll/read）vs 阻塞 `WAIT_VBLANK`：从"单线程多输出、高刷新率、CPU 占用"论述事件化
的优势。

**4.13.16** "驱动只负责把硬件 vblank 中断翻译成 `drm_crtc_handle_vblank`，记账/投递全在核心"——论述
这种分工对支持多驱动的价值（联系第 00 章 0.7、第 03 章二级分发）。

**4.13.17** seqlock vs RCU：结合本章 vblank count 与第 02/10 章 fence，论述"按数据形态选同步原语"。

### L5 动手 / 调试

**4.13.18 ★** 启用 `drm` vblank tracepoint（`/sys/kernel/tracing/events/drm/`），（有显示硬件时）跑一个
会翻页的程序，观察 `drm_vblank_event_queued`/`_delivered` 的成对出现，对照 4.5.3 解释。无硬件则基于
源码描述预期序列。

**4.13.19 ★** `strace -e poll,read,ioctl` 一个合成器（或 `kmscube`），找出"翻页 ioctl → poll → read
事件"的循环，与 4.9.1 主循环对照。

**4.13.20 ★** 用 `/proc/interrupts` 观察显示中断计数随"是否有内容在翻页/刷新率"的变化，结合 4.6.5
解释 vblank 中断按需开关。

**4.13.21 ★（调试）** 设想现象：某合成器偶发"翻页事件丢失/卡住"。列出你会检查的点：`drm_crtc_vblank_
get/put` 是否配对（refcount）、事件是否被预约进 pending_event_list、tracepoint 是否显示 delivered。

### 综合大题

**4.13.22 ★（综合）** 完整叙述"合成器渲染—翻页—等事件—再渲染"一帧循环在用户态与内核态分别发生
什么，把第 01（中断）、02（seqlock/wait_queue）、03（ioctl/drm_file）、04（vblank/事件）四章串起来。

**4.13.23 ★（综合·量化）** 给定 144Hz 刷新、合成器渲染一帧需 4ms、提交开销 0.5ms。分析：① 每帧时间
预算多少？② 该合成器能否稳定不掉帧？③ 若渲染涨到 8ms 会怎样？由此说明事件时间戳/vblank 节拍对调度
的指导意义。

**4.13.24 ★（综合·设计）** 解释 atomic commit 的 `DRM_MODE_ATOMIC_EVENT`（第 06 章）如何复用本章的
事件机制：相比单个 page flip 事件，它在"提交一整套状态 + 完成时发一个事件"上有何异同？预测它在
`pending_event_list`/`drm_send_event` 上走的是不是同一套路。

---

## 4.14 本章小结

- DRM 有**两条通路**：同步 ioctl（第 03 章）与**异步事件**（本章）。显示/GPU 本质异步，事件化 + poll
  是高刷多屏的唯一可行模型。
- **vblank 是显示心跳**：硬件每帧边界产生中断，既是改内容的安全窗口、又是时间基准、又是翻页完成判据。
- **两阶段**：记账（`drm_crtc_handle_vblank`→`drm_update_vblank_count`→`store_vblank`，seqlock 更新
  count+time）与投递（`drm_crtc_send_vblank_event`→`drm_send_event`，pending→event_list + 唤醒）。
- **关键机制**：seqlock 无锁读 + 屏障保证（必须用访问器）、计数回绕/时间戳兜底、refcount + 迟滞
  开关中断、高精度时间戳。
- **Xe 的角色**：只把硬件 vblank 中断翻译成 `drm_crtc_handle_vblank`，记账/投递全在 DRM 核心——
  又一次"通用核心 + 驱动特化"。
- 一次"带事件的翻页"把第 01–04 章全串起来，且是第 06 章原子翻页的底层。

### 知识点清单（自检）

- [ ] 同步 vs 异步通路；事件化解决的问题。
- [ ] vblank 的来历与三重意义；与 page flip 的关系。
- [ ] `drm_vblank_crtc`/`drm_pending_event`/两个事件链表的字段与作用。
- [ ] 中断→记账→投递→读 的完整链路与对应函数。
- [ ] seqlock 选型、屏障保证、回绕处理、refcount 迟滞。
- [ ] 一次非阻塞翻页的端到端事件流。

### 延伸阅读

- 源码：`drivers/gpu/drm/drm_vblank.c`（记账/投递）、`drm_file.c`（事件队列/read/poll）、
  `include/drm/drm_vblank.h`/`drm_vblank_work.h`；Xe 侧 `drivers/gpu/drm/xe/display/xe_display.c`
  （中断路由）。
- 文档：`Documentation/gpu/drm-kms.rst`（vblank 与事件章节）、`Documentation/gpu/drm-uapi.rst`
  （事件 uAPI）。

> **下一章预告**：进入第三篇（显示子系统）。第 05 章《KMS 对象模型》——CRTC/plane/encoder/connector/
> framebuffer 与 property 系统。本章的 vblank/事件将作为"翻页完成"的底层，在第 06 章原子提交里被真正
> 用起来。
