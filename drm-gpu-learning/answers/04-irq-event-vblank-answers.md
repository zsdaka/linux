# 第 04 章 课后练习题参考答案

> 对应正文：[`04-irq-event-vblank.md`](../04-irq-event-vblank.md) 第 4.13 节。先独立完成再核对。
> 参考驱动统一为 Xe。每题给出**标准答案 / 关键点 / 锚点**。

---

## L1 概念辨析

### 4.13.1 同步 vs 异步
**标准答案**：同步 ioctl——问答式，如 `XE_DEVICE_QUERY`（要个结果原地拿）；异步事件——等不可预知/未来
发生的事，如等 vblank/翻页完成。前者适合"立即可得的请求"，后者适合"未来才发生、且不该阻塞调用线程"
的通知。二者都必要：配置/查询用同步，时序/完成通知用异步。**锚点**：4.2.1。

### 4.13.2 vblank 意义
**标准答案**：来历——显示控制器逐帧扫描，每帧边界（电子束回扫的"垂直消隐期"遗留概念）一次中断。
三重意义：① 改显示内容的安全窗口（避免撕裂）；② 时间基准（调度/presentation time）；③ 翻页完成的
判据（翻页在 vblank 生效，此时发 flip-done）。**锚点**：4.2.3。

### 4.13.3 事件一生
**标准答案**：预约时进 `pending_event_list`（已预约未到期）→ 对应 vblank 到达、`drm_send_event` 把它
移到 `event_list`（已到期待读）并 `wake_up(event_wait)` → 用户 `poll` 返回可读、`read` 取走。
**锚点**：4.4.3、4.5.3。

### 4.13.4 为何 seqlock
**标准答案**：① 读多写少——大量查询者高频读 count/time；② 写在中断——写者绝不能被读者阻塞，seqlock
写端不被读端阻塞；③ 读强一致标量——count+time 这一小对要一致读到，seqlock 读端无锁重试即可，无需
RCU 那种对象级延迟释放。**锚点**：4.6.3。

### 4.13.5 开关迟滞两矛盾
**标准答案**：矛盾一——开着费电 vs 关了漏计数（refcount 控制有需求才开）；矛盾二——频繁开关产生抖动
（归零不立即关，迟滞定时器延迟关，期间有新需求就取消关闭）。**锚点**：4.6.5。

### 4.13.6 屏障保证
**标准答案**：`drm_crtc_handle_vblank` 保证：调它之前对其他内存的写，对"看到相同或更晚 vblank count"
的 `drm_crtc_vblank_count()` 读者可见。所以必须用访问器读 count（访问器封装了 seqlock + 屏障）；
直接读字段绕过屏障，破坏该可见性保证。**锚点**：4.6.4。

---

## L2 数据结构与源码阅读

### 4.13.7 drm_vblank_crtc 字段
**标准答案**：`count`(atomic64,软件 vblank 计数,必须经访问器)、`time`(对应时间戳)、`seqlock`(保护
count+time)、`queue`(vblank 等待者 wait_queue)、`refcount`(需求计数,归零可关中断)、`disable_timer`
(迟滞关中断)、`last`(上次硬件读数,回绕用)、`max_vblank_count`(硬件计数器上限,0=用时间戳估算)。
**锚点**：4.4.1、`drm_vblank.h`。

### 4.13.8 store_vblank
**标准答案**：`write_seqlock`/`write_sequnlock` 包住 time 与 count 更新，使二者作为一个一致单元被
读到；读者用 `read_seqbegin`/`read_seqretry` 无锁读，若期间序号变了则重读，从不阻塞中断里的写。
**锚点**：`drm_vblank.c:191`、4.5.2/4.6.3。

### 4.13.9 事件队列初始化
**标准答案**：`drm_file_alloc` 里 `INIT_LIST_HEAD(&file->pending_event_list)` 与 `&file->event_list`
（`drm_file.c:155`）把两条事件链表初始化为空，挂在每文件上下文上；事件按 `file_priv` 投到对应文件的
队列。体现第 03 章"每 open 一份上下文"。**锚点**：4.4.3、第 03 章 3.4.3。

### 4.13.10 drm_read
**标准答案**：从 `event_list` 取一个 `drm_event`，`copy_to_user` 到用户缓冲；列表空时：非阻塞返回
`-EAGAIN`，阻塞则睡在 `event_wait` 直到被 `drm_send_event` 唤醒。**锚点**：`drm_file.c:540`、4.5.4。

### 4.13.11 ★ 回绕与时间戳估算
**标准答案**：`drm_update_vblank_count` 读硬件 cur_vblank 与 last 比、按 max_vblank_count 算回绕得
diff；若中断曾关闭，硬件计数不可靠，则用高精度时间戳 / 刷新周期估算漏掉的 vblank 数。
`max_vblank_count==0`（无可靠硬件计数器）只能全靠时间戳估算，存在小竞态与长期漂移，故精度差、
注释建议硬件暴露计数器。**锚点**：4.6.1、`drm_vblank.c:295`。

---

## L3 机制分析

### 4.13.12 翻页事件链路
**标准答案**：见 4.9.2——`drmModePageFlip(...EVENT)`→ioctl(第03章)→分配 `drm_pending_vblank_event`+
`drm_crtc_vblank_get`(refcount++,第02章)+挂 pending_event_list；ioctl 非阻塞返回。下一 vblank：
`xe_display_irq_handler`(第01章中断)→`drm_crtc_handle_vblank`→`drm_update_vblank_count`(seqlock,
第02章)→`drm_crtc_send_vblank_event`→`drm_send_event`(pending→event_list+wake)→`drm_crtc_vblank_put`。
用户 poll/read。**评分**：标出第01(中断)/02(seqlock/wait/kref)/03(ioctl/drm_file)章设施。**锚点**：4.9.2。

### 4.13.13 中断里保持轻量
**标准答案**：`drm_crtc_handle_vblank` 在中断上下文，不能睡眠/做长序列。只做记账(更新count)、唤醒
(wait_queue)、投递(移链表+wake)。要做对齐 vblank 的寄存器序列等重活用 `drm_vblank_work`(在 vblank
时刻于可睡眠上下文执行)或 workqueue。**锚点**：4.11 陷阱6、第02章。

### 4.13.14 seqlock 读端
**标准答案**：读端 `read_seqbegin` 取序号(偶数表示无写进行中)→读 count/time→`read_seqretry` 比较
序号，若变了(写在中途,序号变奇/递增)则重读。读端不加锁、不阻塞写端，写端(中断)永远能立即更新；
代价是读端偶尔重试。**锚点**：4.6.3。

---

## L4 设计与对比

### 4.13.15 事件化 vs 阻塞 WAIT_VBLANK
**标准答案**：阻塞 WAIT_VBLANK 占住一个线程等一个 vblank，多输出要多线程、且无法在等待时干别的；
事件化让单线程 `poll` 多个 drm_fd/输入 fd，事件到了再处理，CPU 占用低、天然支持多输出与高刷。
**锚点**：4.2.1/4.2.2、4.7。

### 4.13.16 驱动只翻译中断
**标准答案**：驱动只需把硬件 vblank 中断翻译成 `drm_crtc_handle_vblank` 调用，记账/投递/seqlock/迟滞
全在核心。于是 70+ 驱动复用同一套 vblank 语义，只各自实现"硬件中断源 + 计数/时间戳寄存器读取"。
与第03章二级分发同理，杠杆率高。**锚点**：4.5.1、第00章0.7。

### 4.13.17 seqlock vs RCU
**标准答案**：seqlock 适合"读多写少 + 读一小撮强一致标量(count+time) + 写在中断"——vblank 用它；
RCU 适合"读极多 + 无锁遍历/延迟释放对象"——fence 用它(第10章)。选型按数据形态：标量一致快照→seqlock，
对象生命周期+无锁遍历→RCU。**锚点**：4.6.3、第02章2.6.2。

---

## L5 动手 / 调试

### 4.13.18 ★ vblank tracepoint
**要点**：enable `drm/drm_vblank_event_queued` 与 `_delivered`，翻页程序运行时应看到成对(预约→投递)；
对照 4.5.3。无硬件描述预期序列。**锚点**：4.9.3。

### 4.13.19 ★ strace 合成器
**要点**：`strace -e poll,read,ioctl` 见"翻页 ioctl→poll→read(drm_event)"循环，与 4.9.1 主循环对应。
**锚点**：4.9.1/4.9.3。

### 4.13.20 ★ /proc/interrupts
**要点**：有内容翻页/高刷时显示中断计数快速增长，空闲时增长慢/停(迟滞关断)，印证 4.6.5 按需开关。
**锚点**：4.6.5、第01章1.9.3。

### 4.13.21 ★ 事件丢失调试
**要点**：检查 ① `drm_crtc_vblank_get/put` 是否配对(refcount 泄漏致中断异常)；② 事件是否进
pending_event_list(预约成功否)；③ tracepoint 是否 `_delivered`(投递成功否)；④ 用户是否及时 poll/read。
**锚点**：4.11 陷阱5、4.9.3。

---

## 综合大题

### 4.13.22 ★ 一帧循环
**参考**：用户态——渲染→`drmModePageFlip/atomic(EVENT)`→`poll`→`read` 事件→据时间戳渲染下一帧。
内核态——ioctl(第03章 drm_ioctl) 预约事件+vblank_get(第02章 kref/refcount)；下一 vblank 中断(第01章
MSI-X)→`drm_crtc_handle_vblank`→seqlock 更新 count/time(第02章)→`drm_send_event`(wait_queue 唤醒)。
**评分**：四章串通、用户/内核两侧都讲。**锚点**：4.9、4.10。

### 4.13.23 ★ 量化
**参考**：① 144Hz→每帧≈6.94ms 预算；② 渲染 4ms+提交 0.5ms=4.5ms<6.94ms，稳定不掉帧；③ 渲染涨到
8ms>6.94ms→错过下一个 vblank→掉帧(实际帧率降到≤72Hz)。说明：合成器须据 vblank 节拍预算调度，事件
时间戳帮它判断是否将掉帧、提前降载。**锚点**：4.7。

### 4.13.24 ★ ATOMIC_EVENT 复用
**参考**：`DRM_MODE_ATOMIC_EVENT` 与 page flip 事件**复用同一套**：都分配 `drm_pending_vblank_event`、
挂 pending_event_list、vblank 到达经 `drm_send_event` 投递、用户 poll/read 读 `drm_event_vblank`。
异同：page flip 是单 CRTC 单缓冲切换发一个事件；atomic 提交一整套状态(可能多 CRTC/plane)，每个
请求事件的 CRTC 在其完成 vblank 发事件。底层投递路径相同。**锚点**：4.9、4.10、第06章预告。

---

> 自评：L1–L2 全对；L3 能画翻页事件链路、解释 seqlock 读端与中断轻量原则；L4 能论证事件化/分工/选型；
> L5 能用 tracepoint/strace/proc 观察。本章 4.6（seqlock/屏障/回绕/迟滞）是难点，与第06章原子翻页强
> 相关，务必吃透。
