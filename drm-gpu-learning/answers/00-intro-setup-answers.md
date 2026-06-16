# 第 00 章 课后练习题参考答案

> 对应正文：[`00-intro-setup.md`](../00-intro-setup.md) 第 0.13 节。
> 使用建议：务必先独立完成再对照。每题给出**标准答案 / 关键点 / 常见错误 / 源码或规格锚点**。

---

## L1 概念辨析

### 0.13.1 UMS vs KMS

**标准答案**：UMS（User Mode Setting）由用户态进程（如 X 服务器）直接 `mmap` 显卡寄存器、自行设置
显示模式与管理显存；KMS（Kernel Mode Setting）把"显示模式设置"收归内核统一负责。

**两个具体好处**：① 开机即有原生分辨率的内核图形控制台（fbcon 走 KMS），VT 切换/挂起恢复平滑，
因为内核是唯一模式设置者，不会与用户态抢硬件；② 安全性提升——普通进程不再能直接写显卡寄存器，
避免一个 bug 挂死整机。

**关键点**：主权从用户态移到内核；"唯一仲裁者"消除竞争。
**常见错误**：把 KMS 等同于"整个 DRM"。KMS 只是 DRM 的显示部分（见 0.2.3、0.11 谬误 1）。
**锚点**：正文 0.2.1–0.2.3。

### 0.13.2 DRI 三代

**标准答案**：DRI1（~2000）用户态直管命令缓冲、不安全、单客户端；DRI2（~2008）引入 GEM，内核统一
管理 buffer，但跨进程共享靠不安全的 GEM flink 全局名，且 buffer 经 X 中转；DRI3（~2013）用
**dma-buf 文件描述符**共享 buffer，配合 render node，实现安全的跨进程零拷贝。dma-buf 是在
**DRI3** 这一代成为跨进程共享基础的。

**关键点**：演进主线是"安全化 + 去 X 中转化"。
**常见错误**：把 dma-buf 归到 DRI2。DRI2 用的是 GEM flink，不是 dma-buf。
**锚点**：0.2.2 表格。

### 0.13.3 GEM vs TTM

**标准答案**：TTM 为带独立显存（VRAM）的离散显卡设计，支持 VRAM↔系统内存迁移、LRU 驱逐，功能全
但重；GEM 由 Intel 为集显设计，以 shmem 页为后端、句柄化、轻量。现代离散 GPU 驱动用"GEM 对象 +
TTM 后端"，是因为 GEM 提供面向用户态的统一对象抽象（句柄、mmap、dma-buf 导出），而 TTM 提供
底层的 VRAM 放置/迁移/驱逐能力——各取所长。

**关键点**：GEM 是"面子"（用户态抽象），TTM 是"里子"（显存管理）。
**常见错误**：认为二者互斥、只能选一个。现代驱动是组合使用。
**锚点**：0.2.5；第 09、11 章。

### 0.13.4 演进暗线（≥4 对）

**标准答案**（任举四对并说明动机）：
- `UMS→KMS`：把显示主权收归内核，换取安全与统一仲裁。
- `legacy→atomic`：换取显示更新的原子性（全有或全无），避免中间态撕裂。
- `relocation→VM-bind`：换取显式、可预测的地址绑定，去掉提交时的重定位开销。
- `i915→Xe`：换取"多用通用框架"的现代架构，降低维护成本。
- （另可：`手写页表→drm_gpuvm`、`自有调度→drm_sched`、`DRI1→DRI3`。）

**关键点**：每对都是"通用化 / 安全化 / 显式化"。
**锚点**：0.2.6 末、0.2.7 时间线。

### 0.13.5 四个 feature flag

**标准答案**：`DRIVER_MODESET` → 支持 KMS、创建 primary node（cardN）；`DRIVER_ATOMIC` → 放行
`DRM_IOCTL_MODE_ATOMIC` 原子接口；`DRIVER_GEM` → 支持 GEM 内存对象与相关 ioctl；`DRIVER_RENDER` →
创建 render node（renderD128）、放行带 `DRM_RENDER_ALLOW` 的渲染类 ioctl。

**关键点**：features 直接决定"建哪些节点 + 放行哪些能力"，不是装饰。
**常见错误**：以为不声明 `DRIVER_RENDER` 也会有 renderD128。vkms 即反例。
**锚点**：0.5.4、0.6.1、0.6.3、0.11 谬误 3。

---

## L2 数据结构与源码阅读

### 0.13.6 `vkms_driver` 字段 + `DEFINE_DRM_GEM_FOPS`

**标准答案**：`vkms_driver`（`vkms_drv.c:93`）声明：`driver_features`
（`DRIVER_MODESET|DRIVER_ATOMIC|DRIVER_GEM`）、`fops`（`&vkms_driver_fops`）、
`DRM_GEM_SHMEM_DRIVER_OPS` 与 `DRM_FBDEV_SHMEM_DRIVER_OPS` 两组宏展开的回调、`name`/`desc`/
`major`/`minor`。`DEFINE_DRM_GEM_FOPS(vkms_driver_fops)`（:63）生成标准 `file_operations`，把
`open`/`release`/`mmap`/`unlocked_ioctl`/`poll`/`read` 等指向 DRM 核心通用实现，因此 vkms 无需
手写任何文件操作。

**关键点**：宏 = "把通用 fops 一次性补齐"。
**锚点**：`vkms_drv.c:63,93`；0.5.4。

### 0.13.7 `vkms_create` 资源/可见性

**标准答案**（按序，会改状态或申请资源的调用）：`faux_device_create`（造载体设备）→
`devres_open_group`（开托管资源组）→ `devm_drm_dev_alloc`（分配并初始化内嵌 drm_device）→
`dma_coerce_mask_and_coherent`（设 DMA 掩码）→ `drm_vblank_init`（初始化 vblank）→
`vkms_modeset_init`（建 KMS 对象）→ `vkms_config_register_debugfs`（注册 debugfs）→
`drm_dev_register`（**用户态首次可见的分界**）→ `drm_client_setup`（拉起内核 client）。

**关键点**：`drm_dev_register` 成功返回那一刻起 `/dev/dri/cardN` 出现、可被 open。
**常见错误**：把 `devm_drm_dev_alloc` 当成"可见点"。它只是分配，未对外暴露。
**锚点**：`vkms_drv.c:160`；0.5.2、0.6。

### 0.13.8 三个回调归属

**标准答案**：`fb_create = drm_gem_fb_create`（从用户态句柄造 framebuffer，显示路径）；
`atomic_check = vkms_atomic_check`（提交前校验整套状态，显示路径）；
`atomic_commit = drm_atomic_helper_commit`（提交新状态，显示路径）。三者都属**显示路径**。

**关键点**：它们都挂在 `mode_config.funcs`，是 KMS（显示）的入口回调。
**锚点**：`vkms_drv.c:123`；0.5.3、0.3.2。

### 0.13.9 内嵌 ↔ 外层换算

**标准答案**：厂商把 `struct drm_*`（如 `drm_crtc_state`）内嵌进自己的结构（如
`vkms_crtc_state`），用 `container_of(ptr, 外层类型, 内嵌成员名)` 由内嵌成员指针反算外层结构指针
（如 `to_vkms_crtc_state`）。`devm_drm_dev_alloc(parent, driver, 外层类型, 内嵌成员名)` 同理：它
分配整个外层结构，并返回外层指针，内部据成员名定位 drm_device。

**关键点**：内嵌（非指针）是前提，`container_of` 是手段。
**常见错误**：把内嵌成员当指针。
**锚点**：0.4.1、0.5.2 (C)、0.11 陷阱 5。

### 0.13.10 ★ `DRM_XE` 的 select 组件

**标准答案**（任列五个并对应章节）：`DRM_TTM`（显存管理，第 11 章）、`DRM_BUDDY`（伙伴分配器，
第 11 章）、`DRM_KMS_HELPER`（KMS helper，第 05/06 章）、`DRM_DISPLAY_DP_HELPER`/`HDMI_HELPER`/
`DSI`（显示链路，第 07/21 章）、`DRM_DISPLAY_HDCP_HELPER`（内容保护，第 08 章）、`SYNC_FILE`
（显式同步，第 10 章）、`DRM_PANEL`/`DRM_TTM_HELPER`/`DRM_SUBALLOC_HELPER` 等。

**关键点**：Xe 大量 `select` 通用组件，正是"多用通用框架"的 Kconfig 实证（呼应 0.2.6、0.7）。
**锚点**：`drivers/gpu/drm/xe/Kconfig`；0.9.6。

---

## L3 机制分析

### 0.13.11 modprobe→ioctl 时序图

**标准答案**（ASCII，标注核心 vs 驱动）：

```
[用户] modprobe vkms
   └─[vkms]   vkms_init → vkms_create
        └─[vkms] devm_drm_dev_alloc / vkms_modeset_init   (准备阶段)
        └─[核心] drm_dev_register → 分配 minor、建 /dev/dri/card0
[用户] open("/dev/dri/card0")
   └─[核心] drm_open → 分配 struct drm_file (该进程上下文)
[用户] ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &req)
   └─[核心] drm_ioctl → 查 drm_ioctls[] → 权限检查 → 拷入参数
        └─[核心] atomic 核心 drm_mode_atomic_ioctl
             └─[核心] atomic_check → [vkms] vkms_atomic_check
             └─[核心] atomic_commit(drm_atomic_helper_commit)
                  └─[vkms] vkms_atomic_commit_tail (CPU 合成)
```

**关键点**：准备在 register 前；分发/权限在核心；check/commit_tail 才进 vkms。
**锚点**：0.5、0.6、0.6.3。

### 0.13.12 fake_vblank 的意义

**标准答案**：显示路径的"翻页完成（flip_done）"语义建立在 vblank（帧边界）之上：上层要等"新帧已开始
扫描"才认为翻页完成、才回收旧帧、发事件。vkms 没有真实显示硬件、没有真 vblank 中断源，于是用
`drm_vblank_init` 建立 vblank 记账、用 `drm_atomic_helper_fake_vblank` 在提交尾段**伪造**一次
vblank，使 flip_done 流程能正常闭合。

**关键点**：vblank 是显示流程的"心跳"，软件驱动也得有（哪怕是假的）。
**锚点**：`vkms_drv.c:78,195`；0.5.6；第 04、06 章。

### 0.13.13 两进程抢屏 + master

**标准答案**：DRM 用 master 机制仲裁显示控制——同一时刻一个设备只能有一个 DRM master，只有持
master 的 `drm_file` 能执行改显示的 ioctl（受 `DRM_MASTER` 标志保护）。两个进程都想控屏时，先到者
持有 master，后到者执行显示 ioctl 会被拒（或需对方 drop master）。render node 不涉及显示控制，其
ioctl 带 `DRM_RENDER_ALLOW`、不要求 master，因此多个渲染进程可并存、互不影响。

**关键点**：master 仲裁"控显示"，render node 绕开它做"纯计算"。
**锚点**：0.6.1、0.6.3、0.2.8；第 03 章。

---

## L4 设计与对比

### 0.13.14 i915 vs Xe 五维对比

**标准答案**：

| 维度 | i915 | Xe |
|------|------|----|
| 内存管理 | 手写 VMM + 部分 TTM | 全面 TTM + drm_buddy |
| 地址空间 | per-context PPGTT | 进程级 VM（drm_gpuvm） |
| 绑定模型 | execbuffer + relocation/softpin | 显式 VM-bind |
| 命令提交 | execlists 或 GuC | GuC-only |
| 调度 | 自有调度 | drm_gpu_scheduler |

**利弊论述**：通用框架的**利**——代码量小（13.7 万 vs 41.8 万行）、bug 修在通用层全体受益、新人
易上手；**弊**——通用层为兼容多家可能引入抽象开销或不够贴合某硬件的特化优化，极端性能场景可能不如
手写。总体现代趋势认为可维护性收益 > 少量抽象成本。

**关键点**：用 0.7 的量化数据支撑"可维护性"论断。
**锚点**：0.2.6、0.7。

### 0.13.15 为何"通用核心 + 厂商驱动"

**标准答案**：DRM 用 ~8.3 万行通用核心支撑了 70+ 驱动、数百万行驱动代码（i915 单驱动就 41.8 万行）。
若每家各写完整栈，则设备节点管理、ioctl 分发、GEM、atomic 等会被重复实现 70 遍，维护与一致性灾难。
通用核心把这些"机制"抽出来，厂商只填"策略 + 硬件相关"，杠杆率极高。

**关键点**：量化杠杆——少量核心撬动海量驱动。
**锚点**：0.7 三条结论。

### 0.13.16 为何保留 legacy

**标准答案**：海量既有用户态（老版本 X、各类工具）仍用 legacy 接口，直接删除会破坏向后兼容（内核
"不破坏用户态"原则）。把 legacy 实现成 atomic 之上的**兼容垫片**，既保留旧 ABI，又避免维护两套独立
代码路径——新功能只在 atomic 演进，legacy 自动受益且不增维护负担。

**关键点**：向后兼容 + 单一真实实现（垫片）。
**锚点**：0.2.4。

---

## L5 动手编程 / 调试

> L5 为动手题，以下给"预期结果 + 评分要点"，具体输出因环境而异。

### 0.13.17 ★ 主线 Lab

**预期结果**：`modprobe vkms` 成功；`drm_info /dev/dri/card0` 显示
`Driver features: DRIVER_MODESET DRIVER_ATOMIC DRIVER_GEM`，与 `vkms_drv.c:94` 完全一致；
`modetest -M vkms -s ...` 后无报错、虚拟显示被点亮。
**评分要点**：① 能定位 card0；② 能指出 drm_info 的 features 与源码同源；③ modetest 成功设置模式。
**常见错误**：在没有 `DRIVER_RENDER` 的 vkms 上找 renderD128。
**锚点**：0.9.2–0.9.4。

### 0.13.18 ★ 模块参数改行为

**预期结果**：`modprobe vkms enable_overlay=1` 后，`modetest -M vkms` 列出的 plane 中出现
overlay 类型 plane（数量增加）。
**评分要点**：能把现象关联到 `vkms_drv.c:51` 的 `enable_overlay` 与 `vkms_output_init`
（0.5.5）的 `vkms_config_for_each_plane` 循环。
**锚点**：0.5.7、0.9.9。

### 0.13.19 ★ drm.debug 与日志

**预期结果**：`echo 0x1e > /sys/module/drm/parameters/debug` 后重做 modetest，dmesg 出现
`[drm:...] atomic_check`/`commit` 相关行。
**评分要点**：能把日志行**初步**对应到 0.3.2 显示路径的 check/commit 步骤；用完记得关掉调试位。
**锚点**：0.11 `drm.debug` 掩码表。

### 0.13.20 ★ ftrace 调用图

**预期结果**：function_graph 跟踪中可见 `drm_atomic_helper_commit` → ... →
`vkms_atomic_commit_tail` → `drm_atomic_helper_commit_planes` 等调用层级。
**评分要点**：能在调用图里定位 `vkms_atomic_commit_tail`，并对照 0.5.6 解释其在提交链中的角色
（disable→planes→enable→fake_vblank→hw_done→wait flip_done→flush composer）。
**锚点**：0.5.6、0.11 ftrace 小节。

---

## 综合大题

### 0.13.21 ★ 学习地图

**参考框架**：以 0.3.2 两条主链路为骨架，章节贴位示意：
- 显示路径：03（设备/ioctl 入口）→04（vblank/事件）→05（KMS 对象）→06（原子提交）→07（链路）→
  08（格式/色彩）→21（intel_display 硬件）。
- 渲染路径：03（入口）→09（GEM）→10（dma-buf/fence）→11（TTM）→12（调度/gpuvm）→
  13–16（Intel 硬件/命令）→17（Xe 设备）→18（VM-bind）→19（exec）→20（GuC）。
- 交汇：10（dma-buf）连接两路；23（用户态闭环）贯通全程。
**评分要点**：每章都能找到链路归属；能指出 10/23 是"连接点"。

### 0.13.22 ★ 定制阅读顺序

**参考**：
- 只读显示：00–02 →（03–04）→ 05–08 → 21；可跳过 09–20 的渲染细节（但 09/10 缓冲基础建议看）。
- 只读渲染：00–02 → 03 → 09–12 → 13–20 → 23；显示章（05–08、21）可略。
- 全要：按 0.9 的 16 周节奏顺序推进。
**评分要点**：顺序符合 0.8 依赖图（如 18/19 依赖 12/15/16）；时间预算合理；取舍有据。

### 0.13.23 ★ Bspec 检索热身

**参考**：例如 `drivers/gpu/drm/xe/xe_hw_engine.c:428` 注释 `Bspec: 72161`；记录该 `file:line` 与
编号，再去 Intel Graphics Documentation 按编号检索其主题（涉及硬件引擎相关）。i915 树里 Bspec
锚点更多（~150 处），如 `intel_fbc.c` 的 `Bspec: 50422`。
**评分要点**：能用 `git grep -i bspec` 定位、记录 file:line + 编号、给出检索路径（找不到精确原文
也接受，重在掌握"源码锚点→规格编号→官方文档"的方法）。
**锚点**：0.8.2、0.8.5。

---

> 完成情况自评：L1–L2 应全对（概念与源码）；L3 能独立画时序图/讲机制；L4 能有理有据地论述权衡；
> L5 能真正跑起来并把现象关联回源码。若某层吃力，回到正文对应小节重读后再做一遍。
