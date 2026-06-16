# 第 00 章 课程导引与开发环境

> 所属：第一篇 预备与基础　|　前置：无（本书起点）　|　代码基线：Linux `7.1.0-rc6`
>
> 本章是整本书的"地图与工具箱"。读完它，你会对 DRM/GPU 驱动这片大陆有一张清晰的鸟瞰图，
> 知道每一块大陆（KMS 显示、GEM/TTM 内存、调度、Xe 提交、GuC 固件……）在哪里、彼此如何连接；
> 并且亲手搭好实验环境，用纯软件驱动 **vkms** 点亮第一块"虚拟屏幕"，为后续 23 章的源码精读打底。

---

## 0.1 学习目标与前置依赖

### 学习目标

学完本章，你应当能够回答/做到：

1. 用一张图说清 **Linux 图形栈** 从应用程序到 GPU 硬件的完整分层，指出 DRM 处在哪一层、上下各是谁。
2. 区分 DRM 的 **两条主链路**——"显示路径"（KMS）与"渲染/计算路径"（GEM/TTM→调度→提交）——并说出各自经过哪些子系统。
3. 解释 `/dev/dri/cardN`（primary node）与 `/dev/dri/renderD128`（render node）的区别与各自用途。
4. 说清 DRM 的 **历史演进**：为什么会从 UMS 走到 KMS、从 DRI1 走到 DRI3、从 legacy modeset 走到 atomic、从 i915 走到 Xe——理解"为什么"，而不仅是"是什么"。
5. 独立完成 **环境搭建**：配置并编译内核 DRM 模块、加载 `vkms`、用 `modetest`/`drm_info` 观察并点亮虚拟显示。
6. 掌握后续章节会反复使用的 **工具链**：IGT、`drm_info`、`modetest`、debugfs、ftrace、drgn。
7. 学会如何 **在线检索 Intel GPU 规格**（Bspec / PRM），为第五、六篇的硬件精读做准备。

### 前置依赖

- C 语言、Linux 内核基础（模块、`module_init`、字符设备的概念）。
- 能编译内核（或至少能编译外部模块）。本章会带你走一遍。
- 不需要 GPU 硬件——本章主线实验用纯软件的 vkms，在任意 Linux（含虚拟机）上都能跑。

> **学习建议**：本章信息量大但门槛低，目的是"建立全局观 + 跑通环境"。遇到暂时不懂的术语（如
> dma-buf、GEM、GuC）不必纠结，记下它在地图上的位置即可，后续每章都会展开。术语统一见
> [附录 B 术语表](./appendix-b-glossary.md)。

---

## 0.2 主题导引：DRM 为何存在，又如何演进至今

要理解一套庞大框架，最好的入口不是"它现在长什么样"，而是"它当初要解决什么问题，后来又被什么
新问题逼着改成今天这样"。DRM（Direct Rendering Manager，直接渲染管理器）的历史，本质上是
一部"如何让多个进程安全、高效地共享一块越来越强大的 GPU"的演化史。

### 0.2.1 史前时代：用户态直接捅硬件（UMS）

最早的 Linux 图形（XFree86 时代），X 服务器是用户态进程，它**直接** `mmap` 显卡寄存器、自己
设置显示模式（分辨率、刷新率）、自己管理显存。这套做法叫 **UMS（User Mode Setting，用户态显示设置）**。

它的问题是致命的：

- **不安全**：任何能访问 X 的进程都能写显卡寄存器，一个 bug 就能挂死整机或花屏。
- **无法共享**：内核对 GPU 一无所知，多个图形客户端之间没有仲裁者，3D 加速（DRI）只能让
  **单个**直接渲染客户端独占 GPU。
- **挂起恢复、VT 切换、fbcon 与 X 抢显卡**：因为没有内核统一管理，模式设置与电源管理一团乱。

于是社区逐渐把"显卡的主权"收归内核。DRM 就是这个"内核里的 GPU 主权机构"。

### 0.2.2 DRI 的三代：从"不安全直连"到"基于 dma-buf 的共享"

DRM 最初（1999 年左右）是为 **DRI（Direct Rendering Infrastructure，直接渲染基础设施）** 服务的，
让用户态的 3D 驱动（Mesa）能把命令直接送给 GPU，而不必绕道 X 协议。它经历了三代：

| 代 | 年代 | 关键特征 | 痛点/进步 |
|----|------|----------|-----------|
| **DRI1** | ~2000 | 用户态直接管理命令缓冲，内核只做最低限度仲裁 | 不安全、单客户端、与 UMS 同病 |
| **DRI2** | ~2008 | 引入 **GEM** 内存管理；内核统一管理 buffer，X 负责 buffer 分配与 present | 安全性提升，但 buffer 经过 X，跨进程共享靠 GEM flink（全局名，不安全） |
| **DRI3** | ~2013 | 用 **dma-buf** 文件描述符共享 buffer，配合 **render node** | 真正安全的跨进程零拷贝共享，客户端绕开 X 直接拿内核渲染节点 |

这条线索贯穿本书第四篇：**GEM**（第 09 章）、**dma-buf/dma-fence**（第 10 章）就是 DRI2/DRI3
共享模型的内核基石。

### 0.2.3 KMS：把"显示设置"搬进内核

2008 年前后，**KMS（Kernel Mode Setting，内核显示设置）** 诞生。核心思想：**显示模式的设置
（点亮哪个屏、什么分辨率、framebuffer 是哪块内存）必须由内核统一负责**，而不是用户态各自为政。

KMS 带来的直接好处：

- **开机即有原生分辨率的图形控制台**（fbcon 走 KMS）。
- **平滑的 VT 切换、挂起/恢复**，因为内核是唯一的模式设置者。
- 把显示硬件抽象成一组对象：**CRTC、plane、encoder、connector、framebuffer**（第 05 章）。

### 0.2.4 Atomic：从"一次改一个"到"一次改一整套，全有或全无"

早期 KMS 的接口是 **legacy（命令式）**：设置一个 mode、翻一个 plane、改一个属性，各是独立 ioctl。
问题是：复杂显示更新（比如同时改主平面 + overlay + 缩放 + 旋转）无法 **原子地** 提交——
中间状态可能被硬件扫描出去，导致撕裂或硬件根本不支持的中间组合。

2014–2016 年间引入 **Atomic Modesetting（原子显示）**（第 06 章）：用户态把"我想要的完整新状态"
一次性提交，内核先 **check**（这套组合硬件能不能做到？），能做到才 **commit**，做不到就整体失败、
保持原状。这就是数据库式的"全有或全无（all-or-nothing）"语义。今天所有现代驱动都以 atomic 为正路，
legacy 接口只是 atomic 之上的兼容垫片。

### 0.2.5 内存管理两条血脉：GEM 与 TTM

GPU 驱动绕不开"显存/缓冲怎么管"。历史上有两套：

- **TTM（Translation Table Maps）**：为带独立显存（VRAM）的离散显卡（radeon、nouveau）设计，
  能在 VRAM 与系统内存之间迁移、按 LRU 驱逐——重而全（第 11 章）。
- **GEM（Graphics Execution Manager）**：2008 年由 Intel（Keith Packard、Eric Anholt）为集显设计，
  轻量、以 shmem 页为后端、句柄化管理——简而美（第 09 章）。

今天二者并存且融合：GEM 是**面向用户态的统一对象抽象**（句柄、mmap、dma-buf 导出），底下可以挂
shmem、DMA、VRAM、或 **TTM** 作后端。现代离散 GPU 驱动（amdgpu、Xe）走的正是"GEM 对象 + TTM 后端"。

### 0.2.6 从 i915 到 Xe：现代重写的范式转变

Intel 的集成/独立显卡驱动 **i915** 历经十余年演进，积累了大量历史包袱（手写的 per-gen 页表、
relocation 式命令提交、execlists 与 GuC 双提交路径……）。为迎接独立显卡与现代编程模型，Intel
重写了全新的 **Xe** 驱动（约在 Linux 6.8 合入主线），其设计哲学是"**多用通用框架、少造轮子**"：

| 维度 | i915（传统） | Xe（现代，本书主线） |
|------|--------------|----------------------|
| 内存管理 | 手写 VMM + 部分 TTM | 全面 **TTM** + `drm_buddy` |
| 地址空间 | per-context PPGTT | **进程级 VM**，基于通用 `drm_gpuvm` |
| 绑定模型 | execbuffer + relocation/softpin | 显式 **VM-bind**（第 18 章） |
| 命令提交 | execlists 或 GuC（双路径） | **GuC-only**（第 19、20 章） |
| 调度 | 自有调度 | 通用 `drm_gpu_scheduler`（第 12 章） |
| SVM | 后补 | 一等公民 `drm_gpusvm`（第 18 章） |

> **本书取向**：以 **Xe 为主线**讲现代设计，以 **i915 作对照**讲经典机制。两者共享同一套 DRM 通用
> 框架与同一个 `display/` 显示子系统（第 21 章），所以前四篇打通通用框架后，第五、六篇会顺理成章。

**演进主题（贯穿全书，请记住这条暗线）**：`UMS→KMS`、`DRI1→DRI3`、`legacy→atomic`、
`relocation→VM-bind`、`i915→Xe`、`手写页表→drm_gpuvm`、`自有调度→drm_sched`。
每一次演进都是"通用化 + 安全化 + 显式化"。理解了动机，再看代码就不再是死记。

### 0.2.7 一张演进时间线（速查）

| 大约年代 | 里程碑 | 解决的核心问题 | 本书章节 |
|----------|--------|----------------|----------|
| 1999 | DRM/DRI 诞生 | 让用户态 3D 能直连 GPU | 全书背景 |
| ~2008 | **GEM** 内存管理 | 内核统一管理缓冲、句柄化 | 第 09 章 |
| ~2008 | **KMS** 内核显示设置 | 把显示主权收归内核 | 第 05 章 |
| ~2008 | **DRI2** | 安全性提升（buffer 经 X） | 背景 |
| ~2011 | **dma-buf** | 跨设备/驱动共享缓冲 | 第 10 章 |
| ~2013 | **render node + DRI3** | 安全的无特权渲染、零拷贝共享 | 第 03、10 章 |
| 2014–2016 | **Atomic Modesetting** | 显示更新的原子性（全有或全无） | 第 06 章 |
| ~2017+ | **drm_gpu_scheduler** | 通用 GPU 任务调度 | 第 12 章 |
| ~2021+ | **drm_gpuvm / VM-bind** | 通用 GPU 地址空间 + 显式绑定 | 第 12、18 章 |
| ~2024 | **Xe 驱动入主线** | 现代 Intel 驱动（多用通用框架） | 第六篇 |

把这张表与 0.7 的"代码重量分布"对照看：越晚出现、越"通用化"的能力（gpuvm、scheduler），正是
现代驱动（Xe）赖以瘦身的支柱。

### 0.2.8 为什么 render node 是安全模型的关键一跃

render node（0.6.1 会从节点角度再讲一遍）值得在"历史"里单独点名，因为它体现了 DRM 演进的核心价值观
——**最小权限**。在 render node 出现前，任何想用 GPU 算东西的进程都得碰 primary node，而 primary
node 牵连显示控制与 master 仲裁，等于"为了算一道题，先拿到了改屏幕的钥匙"，权限过大且需要 X 中转。

render node 把"渲染/计算"从"显示控制"中**彻底切开**：

- 一个容器里的 ML 任务、浏览器的 GPU 进程，只需 `open("/dev/dri/renderD128")`，**无需任何显示特权、
  无需经过 X/合成器**，就能提交计算。
- 它**做不到**改分辨率、点屏、抢 master——这些 ioctl 没有 `DRM_RENDER_ALLOW`（0.6.3 已见其代码形态）。

这条"按能力切分节点 + 按 ioctl 标志授权"的设计，是后面第六篇（Xe 渲染路径全在 render node 上跑）
和容器/云场景 GPU 共享的基础。记住这个价值观：**DRM 的每一次演进，都在问"这个能力到底需不需要
这么大的权限？"**

---

## 0.3 原理与全景：Linux 图形栈与两条主链路

### 0.3.1 全栈分层鸟瞰

下面这张图是你接下来几个月要反复回看的"总地图"。它把从应用到硅片的所有层画在一起：

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 应用层      游戏 / 浏览器 / 合成器(Compositor: Mutter, KWin, Weston) ...    │
├─────────────────────────────────────────────────────────────────────────┤
│ API 层      Vulkan / OpenGL / OpenCL / VA-API(视频)                         │
├─────────────────────────────────────────────────────────────────────────┤
│ 用户态驱动  Mesa: ANV(Vulkan/Intel) · IRIS(GL/Intel) · ...                 │
│            ↳ 把绘制调用编译成 GPU 命令(batch buffer)、管理缓冲             │
├─────────────────────────────────────────────────────────────────────────┤
│ 用户态库    libdrm  (封装 ioctl, 屏蔽各驱动 uAPI 细节)                       │
│            libdrm_intel / libdrm_amdgpu / ...                              │
├══════════════════════════════ 用户态 / 内核态分界 ═════════════════════════┤
│ uAPI       /dev/dri/cardN (primary)  ·  /dev/dri/renderD128 (render)        │
│            ioctl: DRM_IOCTL_MODE_*(显示) · DRM_IOCTL_XE_*(渲染) ...          │
├─────────────────────────────────────────────────────────────────────────┤
│ DRM 核心    设备/文件/ioctl 框架 · KMS · GEM · drm_gpuvm · 调度器 · vblank   │
│  (本书      drm_drv/drm_ioctl/drm_file · drm_atomic · drm_gem · scheduler/  │
│   主战场)   dma-buf/dma-fence/dma-resv · TTM                                │
├─────────────────────────────────────────────────────────────────────────┤
│ 厂商驱动    xe(本书主线) · i915(对照) · amdgpu · nouveau · vkms(纯软件) ...  │
│            ↳ 命令提交 · 页表 · 引擎 · 显示输出 · 固件(GuC/HuC)              │
├─────────────────────────────────────────────────────────────────────────┤
│ 内核基础    PCI · 中断(MSI-X) · dma-mapping · iommu · workqueue · ww-mutex   │
├══════════════════════════════ 软件 / 硬件分界 ════════════════════════════┤
│ 固件        GuC(调度微控制器) · HuC · GSC                                    │
├─────────────────────────────────────────────────────────────────────────┤
│ GPU 硬件    命令流处理器(CS) · 执行单元(Xe-core/EU) · 显存(VRAM)/GTT ·       │
│            显示引擎(pipe/transcoder/PHY) · 编解码引擎                       │
└─────────────────────────────────────────────────────────────────────────┘
```

把这张图按本书篇章染色，你会发现每一篇都对应图中一条横带或一个区块：

- 第一篇（00–02）：最底的"内核基础"与最上的"全栈定位"；GPU 硬件的工作原理（01）。
- 第二篇（03–04）：DRM 核心的"设备/文件/ioctl + 中断/事件"骨架。
- 第三篇（05–08）：KMS 显示子系统（图中"DRM 核心"的显示部分 + 显示输出）。
- 第四篇（09–12）：内存与同步（GEM/TTM/dma-buf/调度/gpuvm）。
- 第五篇（13–16）：GPU 硬件 + 厂商驱动的底层（寄存器/页表/命令）。
- 第六篇（17–20）：Xe 厂商驱动的核心（设备/内存/提交/固件）。
- 第七篇（21–23）：显示输出驱动（intel_display）+ 性能调试 + 用户态闭环。

### 0.3.2 两条主链路

DRM 同时服务两类截然不同的需求，它们在内核里走两条不同的路。**始终分清你在看哪条路**，是读懂
DRM 的第一要义。

**① 显示路径（Display path / KMS）——"把一块内存的内容送到屏幕上"**

```
合成器(Mutter/Weston)
  └─ ioctl DRM_IOCTL_MODE_ATOMIC  (提交"我要的完整显示状态")
       └─ drm_atomic 核心: check → commit            [第06章]
            └─ KMS 对象: plane→CRTC→encoder→connector [第05章]
                 └─ 厂商显示驱动 intel_display         [第21章]
                      └─ 配置 pipe/transcoder/PHY, 扫描 framebuffer
                           └─ vblank 中断回报"翻页完成" [第04章]
```

**② 渲染/计算路径（Render path）——"让 GPU 执行一段命令，算出新内容"**

```
Mesa(ANV/IRIS)
  └─ 分配缓冲 GEM_CREATE, 编译出 batch buffer        [第09章, 第16章]
  └─ 绑定地址 XE_VM_BIND (把缓冲映射进 GPU 地址空间)  [第18章]
  └─ 提交执行 XE_EXEC (送 batch 给某个 exec_queue)    [第19章]
       └─ drm_gpu_scheduler 排队/依赖/超时             [第12章]
       └─ GuC 固件实际在硬件上调度上下文               [第20章]
            └─ GPU 执行单元跑命令, 写结果
            └─ dma_fence 信号"完成", 唤醒等待者         [第10章]
```

两条路在 **缓冲（buffer）** 处交汇：渲染路径算出的内容，最终成为显示路径要扫描的 framebuffer；
而这块缓冲跨进程/跨设备共享，靠的是 **dma-buf**（第 10 章）。这就是为什么本书把内存与同步
（第四篇）放在显示与渲染的中间——它是两条路的公共地基。

**用"数据结构在两条路上流动"的视角再看一遍**（现在不必记，混个脸熟，后续章节会逐一坐实）：

- 显示路径上流动的主要是 **状态对象**：用户态构造 `drm_atomic_state`，里面装着各个
  `drm_plane_state`/`drm_crtc_state`/`drm_connector_state`；它们引用 `drm_framebuffer`，后者又引用
  底层的 GEM 对象。核心校验这套状态、提交到硬件、用 `drm_crtc_commit` 跟踪三个里程碑。
- 渲染路径上流动的主要是 **缓冲与栅栏**：`drm_gem_object`（缓冲）被 `XE_VM_BIND` 映射进 `xe_vm`
  地址空间，`XE_EXEC` 把批缓冲送进 `xe_exec_queue`，经 `drm_gpu_scheduler` 排队、GuC 实际调度，
  完成时 `dma_fence` 发信号。
- 两条路靠 `dma_buf` + `dma_resv`（缓冲上的栅栏集合）实现"渲染完成后显示才扫描"的隐式同步。

一句话记忆：**显示路径搬的是"状态"，渲染路径搬的是"缓冲 + 栅栏"，二者在缓冲上握手。**

### 0.3.3 一个最小但完整的样本：vkms

为了在没有任何 GPU 硬件的机器上也能学习 KMS，内核自带一个 **纯软件 KMS 驱动 vkms**
（Virtual Kernel Modesetting）。它实现了完整的 KMS 对象与 atomic 流程，但"扫描输出"是用 CPU
在内存里合成像素（甚至能算 CRC 供测试比对），不依赖任何显示硬件。

vkms 是本书前三篇的"实验靶场"：它足够小（整个驱动约 **8000 行**，见 0.7 量化分析），又走的是
和真实驱动一模一样的 DRM 核心路径。本章的主线 Lab 就是把它跑起来。

### 0.3.4 DRM 核心文件地图（你将反复回访的"街区图"）

`drivers/gpu/drm/` 顶层的 `drm_*.c`（约 8.3 万行，见 0.7）就是"通用核心"。初学最怕"不知道东西在哪"，
这里给一张按职责分组的文件地图。**不要求现在读懂任何一个**，只要建立"我要找 X，该去哪个文件"的索引。

| 职责分组 | 关键文件 | 大致内容 | 精读章节 |
|----------|----------|----------|----------|
| 设备/驱动/文件 | `drm_drv.c` · `drm_file.c` · `drm_ioctl.c` · `drm_auth.c` | 设备注册、节点、文件上下文、ioctl 分发、鉴权 | 第 03 章 |
| 事件/vblank | `drm_vblank.c` · `drm_vblank_work.c` | vblank 记账、事件投递 | 第 04 章 |
| KMS 对象 | `drm_crtc.c` · `drm_plane.c` · `drm_encoder.c` · `drm_connector.c` · `drm_framebuffer.c` · `drm_mode_config.c` | 显示管线对象与注册 | 第 05 章 |
| 原子显示 | `drm_atomic.c` · `drm_atomic_helper.c` · `drm_atomic_uapi.c` · `drm_atomic_state_helper.c` · `drm_modeset_lock.c` | 原子状态机、check/commit、ww-mutex 加锁 | 第 06 章 |
| 显示链路 | `drm_bridge.c` · `drm_panel.c` · `drm_edid.c` · `drm_probe_helper.c` · `drm_simple_kms_helper.c` | encoder/bridge/panel、探测、EDID | 第 07 章 |
| 格式/色彩 | `drm_fourcc`(头) · `drm_blend.c` · `drm_color_mgmt.c` · `drm_colorop.c` | 像素格式、混合、色彩管线 | 第 08 章 |
| GEM/内存 | `drm_gem.c` · `drm_gem_shmem_helper.c` · `drm_gem_dma_helper.c` · `drm_prime.c` · `drm_gem_framebuffer_helper.c` · `drm_mm.c` · `drm_buddy.c` | GEM 对象、shmem/dma 后端、PRIME、分配器 | 第 09、11 章 |
| 同步 | `drm_syncobj.c` · `drm_exec.c`（+ `drivers/dma-buf/`） | syncobj、多对象加锁（dma-buf/fence 在隔壁目录） | 第 10、12 章 |
| 调度/地址空间 | `scheduler/sched_main.c` · `scheduler/sched_entity.c` · `drm_gpuvm.c` | GPU 调度器、GPU 虚拟地址管理 | 第 12 章 |
| 显存管理 | `ttm/ttm_bo.c` · `ttm/ttm_resource.c` · `ttm/ttm_pool.c` | TTM 显存（VRAM）管理 | 第 11 章 |
| 客户端/控制台 | `drm_client.c` · `drm_client_modeset.c` · `drm_fbdev_shmem.c` | 内核内置 client、fbdev 仿真 | 第 03、05 章 |
| 参考驱动 | `vkms/` · `tiny/`（如 `tiny/bochs.c`） | 纯软件/最小驱动，学习首选 | 第 03、05 章 |

> 用法示范：日后你想知道"atomic commit 到底怎么把状态推到硬件"，看这张表就知道去 `drm_atomic_helper.c`
> （第 06 章）；想知道"句柄怎么变成内存对象"，去 `drm_gem.c`（第 09 章）。这张表 + 0.10 的章节索引，
> 构成你全书的"导航双件套"。

---

## 0.4 关键数据结构（鸟瞰版）

本节只做"全书核心数据结构的全家福"，让你在脑中先有名字与位置；每个结构的逐字段精读分散在后续章节。
所有路径基于内核 `7.1.0-rc6`。

### 0.4.1 四个最根本的对象

| 结构 | 定义位置 | 一句话职责 | 精读章节 |
|------|----------|------------|----------|
| `struct drm_device` | `include/drm/drm_device.h` | 一个 DRM 设备的总枢纽，挂着所有 KMS 对象、内存管理器、文件列表 | 第 03 章 |
| `struct drm_driver` | `include/drm/drm_drv.h` | 厂商驱动的"虚函数表 + 能力声明"（`DRIVER_GEM`/`MODESET`/`ATOMIC` 等） | 第 03 章 |
| `struct drm_file` | `include/drm/drm_file.h` | 每个打开 `/dev/dri/*` 的进程上下文（句柄表、鉴权、事件队列） | 第 03 章 |
| `struct drm_minor` | `include/drm/drm_file.h` | 一个设备节点（primary/render/accel），对应 `/dev/dri/cardN` 等 | 第 03 章 |

它们的关系（高层）：

```
        struct drm_driver  (一个驱动一份, 静态 const)
              ▲  .driver_features / .fops / 各类回调
              │
   struct drm_device  ── mode_config ──► CRTC/plane/encoder/connector  [第05章]
        │   │   └── 内存管理器(GEM/TTM)                                 [第09/11章]
        │   └── minors[]: primary(cardN) / render(renderD128)          [第03章]
        │
        └── 打开后每进程一份 ──► struct drm_file (句柄表 idr, 事件队列)  [第03章]
```

### 0.4.1.1 先尝一口"字段表"：`struct drm_device` 节选

本书每章讲数据结构都会给"逐字段表"。这里以全书最根本的 `struct drm_device`
（`include/drm/drm_device.h:75`）开胃，挑出与本章已出现概念直接相关的字段（完整逐字段拆解见第 03 章）：

| 字段 | 类型 | 含义 | 关联本章/锁 |
|------|------|------|-------------|
| `ref` | `struct kref` | 设备引用计数；归零才真正释放 | 第 02 章引用计数 |
| `dev` | `struct device *` | 承载它的总线设备（vkms 是 faux_dev，真 GPU 是 pci_dev） | 0.5.2 (A) |
| `managed` | 匿名 struct（`resources`/`final_kfree`/`lock`） | **托管资源链表**：`devm_*`/`drmm_*` 申请的资源挂这里，销毁时统一回收 | 0.5.2 (B)(C)、0.5.5 |
| `driver` | `const struct drm_driver *` | 指向该设备的驱动 vtable（vkms 即 `&vkms_driver`） | 0.5.4 |
| `primary` | `struct drm_minor *` | primary 节点（`/dev/dri/cardN`） | 0.6.1 |
| `render` | `struct drm_minor *` | render 节点（`/dev/dri/renderD128`），无 `DRIVER_RENDER` 则为空 | 0.6.1 |
| `accel` | `struct drm_minor *` | 计算加速节点（accel 子系统，本书少涉及） | — |
| `registered` | `bool` | 是否已 `drm_dev_register`——"可见性分界"的真身 | 0.5.2 (H)、0.11 陷阱 6 |
| `master` | `struct drm_master *` | 当前显示主控；受 `master_mutex` 保护 | 0.6.1 |
| `driver_features` | `u32` | **每设备**可再裁剪的能力位（可比 `drm_driver` 更严） | 0.5.4 |
| `filelist` / `filelist_mutex` | 链表 + 锁 | 所有打开本设备的用户态 `drm_file` | 0.6 |
| `open_count` | `atomic_t` | 当前打开计数 | 0.6 |

> 注意 `dev_private` 字段（文档明确标"deprecated"）：老驱动用它存私有数据指针；**新驱动一律改用
> "内嵌 drm_device + `devm_drm_dev_alloc`"**（正是 vkms 的写法，0.5.2 (C)）。看到这条 deprecated
> 注释，你就明白了 0.4.1"内嵌惯用法"背后的历史动因——又一个"读注释知演进"的例子。

读这张表时请体会本书"字段表"的信息密度：**类型 + 含义 + 不变量/锁 + 与代码现场的关联**。后续每个
核心结构都会这样拆，这是 UTLK 式精读的标准动作。

### 0.4.2 全书数据结构地图（按篇）

| 篇 | 代表性结构 | 关键词 |
|----|------------|--------|
| 三 显示 | `drm_crtc` / `drm_plane` / `drm_connector` / `drm_atomic_state` / `drm_crtc_commit` | KMS 对象、原子状态机 |
| 四 内存 | `drm_gem_object` / `dma_buf` / `dma_fence` / `dma_resv` / `ttm_buffer_object` / `drm_gpu_scheduler` / `drm_gpuvm` | 缓冲、同步、调度、地址空间 |
| 五 硬件 | `intel_uncore` / `xe_mmio` / GGTT/PPGTT 页表项 / MI 命令 | 寄存器、页表、命令 |
| 六 Xe | `xe_device` / `xe_gt` / `xe_bo` / `xe_vm` / `xe_vma` / `xe_exec_queue` / `xe_lrc` / `intel_guc` | 设备、内存、提交、固件 |

> 现在不必记住这些名字，只需知道"它们都挂在 `drm_device` 这棵大树上，分属两条主链路"。后续每章
> 都会给出对应结构的**逐字段完整表格 + 不变量 + 锁要求**（UTLK 风格）。本章先给一个最小示例，
> 让你感受这种表格长什么样——以 vkms 自己的设备结构为例：

`struct vkms_device`（`drivers/gpu/drm/vkms/vkms_drv.h`，节选概念）：

| 字段 | 类型 | 含义 | 备注 |
|------|------|------|------|
| `drm` | `struct drm_device` | **内嵌**的 DRM 设备（注意是内嵌，不是指针） | `devm_drm_dev_alloc(..., struct vkms_device, drm)` 用 `drm` 成员定位外层 |
| `faux_dev` | `struct faux_device *` | 承载它的 faux 总线设备（vkms 无真实 PCI 设备） | 见 0.5 代码走读 |
| `config` | `struct vkms_config *` | 该实例的配置（是否启用 cursor/overlay/writeback 等） | 由模块参数或 configfs 决定 |

这里要点出一个**贯穿所有 DRM 驱动的惯用法**：厂商设备结构（如 `vkms_device`、`xe_device`）总是把
`struct drm_device` 作为**内嵌成员**，再用 `container_of`/`devm_drm_dev_alloc` 的最后两个参数
在二者间换算。记住这一点，你读任何驱动的第一行都不会迷路。

---

## 0.5 核心代码走读：vkms 是如何"出生"的

理论说够了。我们来逐行读 vkms 的诞生过程——这是你本书读到的第一段真实内核代码，也是后面所有
驱动 `probe` 流程的"最小公倍数"。文件：`drivers/gpu/drm/vkms/vkms_drv.c`。

### 0.5.1 模块入口 `vkms_init`

```c
// drivers/gpu/drm/vkms/vkms_drv.c:223
static int __init vkms_init(void)
{
    int ret;
    struct vkms_config *config;

    ret = vkms_configfs_register();        // (1) 注册 configfs 接口, 允许运行时创建多个 vkms 设备
    if (ret)
        return ret;

    if (!create_default_dev)               // (2) 模块参数: 是否自动建一个默认设备
        return 0;

    config = vkms_config_default_create(enable_cursor, enable_writeback,
                                        enable_overlay, enable_plane_pipeline);
                                           // (3) 依据模块参数生成默认配置
    if (IS_ERR(config))
        return PTR_ERR(config);

    ret = vkms_create(config);             // (4) 真正创建并注册 DRM 设备
    if (ret) {
        vkms_config_destroy(config);
        return ret;
    }

    default_config = config;
    return 0;
}
module_init(vkms_init);                     // drivers/gpu/drm/vkms/vkms_drv.c:281
```

逐点看：

- **(1)** `vkms_configfs_register()`：vkms 支持通过 configfs（`/sys/kernel/config/vkms/`）在运行时
  动态创建/配置多个虚拟显示设备。这是较新的能力，本章实验用默认设备即可，configfs 留到 Lab 进阶。
- **(2)** `create_default_dev` 是模块参数（`module_param_named`，见文件第 59–61 行），默认 `true`。
  这解释了"为什么 `modprobe vkms` 后就直接有了一个 cardN"。
- **(3)** 四个布尔（cursor/writeback/overlay/plane_pipeline）对应四个模块参数（第 43–57 行），
  控制这个虚拟显示暴露哪些 plane 与 connector 能力。
- **(4)** 重头戏在 `vkms_create()`。

### 0.5.2 设备创建 `vkms_create`

```c
// drivers/gpu/drm/vkms/vkms_drv.c:160
int vkms_create(struct vkms_config *config)
{
    int ret;
    struct faux_device *fdev;
    struct vkms_device *vkms_device;
    const char *dev_name;

    dev_name = vkms_config_get_device_name(config);
    fdev = faux_device_create(dev_name, NULL, NULL);   // (A) 造一个 "faux" 总线设备做载体
    if (!fdev)
        return -ENODEV;

    if (!devres_open_group(&fdev->dev, NULL, GFP_KERNEL)) { // (B) 开一个 devres 资源组, 便于一把回收
        ret = -ENOMEM;
        goto out_unregister;
    }

    vkms_device = devm_drm_dev_alloc(&fdev->dev, &vkms_driver,
                                     struct vkms_device, drm); // (C) 分配并初始化 drm_device(内嵌)
    if (IS_ERR(vkms_device)) {
        ret = PTR_ERR(vkms_device);
        goto out_devres;
    }
    vkms_device->faux_dev = fdev;
    vkms_device->config = config;
    config->dev = vkms_device;

    ret = dma_coerce_mask_and_coherent(vkms_device->drm.dev,
                                       DMA_BIT_MASK(64));        // (D) 声明 64 位 DMA 能力
    if (ret) { DRM_ERROR("Could not initialize DMA support\n"); goto out_devres; }

    ret = drm_vblank_init(&vkms_device->drm,
                          vkms_config_get_num_crtcs(config));    // (E) 初始化 vblank 子系统
    if (ret) { DRM_ERROR("Failed to vblank\n"); goto out_devres; }

    ret = vkms_modeset_init(vkms_device);                        // (F) 搭建 KMS 对象(CRTC/plane/...)
    if (ret) goto out_devres;

    vkms_config_register_debugfs(vkms_device);                   // (G) 注册 debugfs 节点

    ret = drm_dev_register(&vkms_device->drm, 0);                // (H) 正式注册! 此刻 /dev/dri/cardN 出现
    if (ret) goto out_devres;

    drm_client_setup(&vkms_device->drm, NULL);                  // (I) 拉起内核内置 client(如 fbdev 仿真)
    return 0;
    /* ... 错误回滚 ... */
}
```

这八个步骤几乎是**所有** DRM 驱动 probe 的模板，逐点对照后续真实驱动（Xe 的 `xe_device_probe`，
第 17 章）你会发现骨架惊人地一致：

- **(A) 载体设备**：每个 `drm_device` 都要挂在一个父 `struct device` 上。真实 GPU 挂在 PCI 设备上；
  vkms 没有硬件，于是用内核新引入的 **faux 总线**（`linux/device/faux.h`）造一个"假"设备做父亲。
  这是 vkms 在新内核里的写法（早期用 platform device）。
- **(B) devres 资源组**：`devres_open_group` 把后续 `devm_*` 申请的资源圈成一组，出错或卸载时
  `devres_release_group` 一次性回收。这是内核"托管资源（managed resources）"思想（第 02 章详解）。
- **(C) `devm_drm_dev_alloc`**：这是 DRM 设备分配的标准函数。它分配 `struct vkms_device`，初始化其中
  **内嵌**的 `struct drm_device drm` 成员，并绑定 `vkms_driver`。最后两个参数 `struct vkms_device, drm`
  正是 0.4.1 强调的"外层结构类型 + 内嵌 drm 成员名"。返回的是外层结构指针。
- **(D) DMA mask**：声明设备能寻址 64 位物理地址，影响后续缓冲分配。
- **(E) `drm_vblank_init`**：初始化垂直消隐（vblank）记账（第 04 章）。即使是软件 vkms，也要"假装"
  有 vblank 来驱动翻页完成事件——见 `vkms_atomic_commit_tail` 里的 `drm_atomic_helper_fake_vblank`。
- **(F) `vkms_modeset_init`**：搭建 KMS 对象树，见下一小节。
- **(G) debugfs**：注册调试节点（如 CRC 输出），后续调试常用。
- **(H) `drm_dev_register`**：**临界点**。在此之前设备只是"准备中"，用户态看不到；这一行成功返回后，
  `/dev/dri/cardN` 节点出现、udev 收到事件、用户态可以 `open()` 了。**记住这个分界**：注册前做准备，
  注册后才对外可见。
- **(I) `drm_client_setup`**：拉起内核内置客户端（比如把 fbdev 控制台仿真接到这个 KMS 设备上），
  于是 `dmesg` 里能看到 fbcon 绑定、虚拟控制台可用。

### 0.5.3 KMS 对象搭建 `vkms_modeset_init`

```c
// drivers/gpu/drm/vkms/vkms_drv.c:133
static int vkms_modeset_init(struct vkms_device *vkmsdev)
{
    struct drm_device *dev = &vkmsdev->drm;
    int ret;

    ret = drmm_mode_config_init(dev);          // (1) 初始化 mode_config(KMS 子系统的根)
    if (ret) return ret;

    dev->mode_config.funcs = &vkms_mode_funcs; // (2) 挂上 fb_create / atomic_check / atomic_commit
    dev->mode_config.min_width  = XRES_MIN;    // (3) 声明支持的分辨率范围
    dev->mode_config.min_height = YRES_MIN;
    dev->mode_config.max_width  = XRES_MAX;
    dev->mode_config.max_height = YRES_MAX;
    dev->mode_config.cursor_width  = 512;
    dev->mode_config.cursor_height = 512;
    dev->mode_config.preferred_depth = 0;      // 0 = 默认 XRGB8888
    dev->mode_config.helper_private = &vkms_mode_config_helpers; // (4) atomic_commit_tail 回调

    return vkms_output_init(vkmsdev);          // (5) 创建具体的 CRTC/plane/encoder/connector
}
```

- **(2)** `vkms_mode_funcs`（文件第 123 行）给出三个关键回调：`fb_create = drm_gem_fb_create`
  （如何从用户态句柄造一个 framebuffer）、`atomic_check = vkms_atomic_check`、
  `atomic_commit = drm_atomic_helper_commit`。这三者是第 05、06 章的主角。
- **(4)** `helper_private` 指向 `vkms_mode_config_helpers`，其中 `atomic_commit_tail = vkms_atomic_commit_tail`
  （文件第 65 行）——这就是"提交到硬件"的尾段逻辑。对 vkms 而言"硬件"是 CPU 合成，所以它调用
  `drm_atomic_helper_commit_planes` 后用 `flush_work(&vkms_state->composer_work)` 等软件合成完成。
  这段我们会在第 06 章逐行拆。
- **(5)** `vkms_output_init` 才真正 `drm_crtc_init_with_planes` / `drm_connector_init` 等——第 05 章主菜。

### 0.5.4 驱动声明 `vkms_driver`

```c
// drivers/gpu/drm/vkms/vkms_drv.c:93
static const struct drm_driver vkms_driver = {
    .driver_features = DRIVER_MODESET | DRIVER_ATOMIC | DRIVER_GEM, // 声明能力
    .fops            = &vkms_driver_fops,                            // 文件操作(由宏生成)
    DRM_GEM_SHMEM_DRIVER_OPS,        // 一组 GEM(shmem 后端)回调
    DRM_FBDEV_SHMEM_DRIVER_OPS,      // 一组 fbdev 仿真回调
    .name = "vkms", .desc = "Virtual Kernel Mode Setting",
    .major = 1, .minor = 0,
};
```

`driver_features` 这三个 flag 是"驱动的自我介绍"：

- `DRIVER_MODESET`：我支持 KMS 显示设置 → 会创建 primary node `cardN`。
- `DRIVER_ATOMIC`：我支持原子接口 → 用户态可用 `DRM_IOCTL_MODE_ATOMIC`。
- `DRIVER_GEM`：我支持 GEM 内存对象。

> 对照预告：Xe 的 `drm_driver` 还会带 `DRIVER_RENDER`（创建 render node `renderD128`）、
> `DRIVER_SYNCOBJ`、`DRIVER_GEM_GPUVA` 等更多 flag——能力越多，flag 越多。第 03、17 章会对照。

`DEFINE_DRM_GEM_FOPS(vkms_driver_fops)`（文件第 63 行）这个宏生成了标准的 `file_operations`
（`open`/`release`/`mmap`/`unlocked_ioctl` 等都指向 DRM 核心的通用实现）。这解释了"为什么 vkms
没写一行 `open`/`ioctl` 代码，却能响应用户态 ioctl"——因为 DRM 核心代劳了（第 03 章）。

### 0.5.5 真正"造对象"的地方 `vkms_output_init`

`vkms_modeset_init` 最后调用的 `vkms_output_init`（`drivers/gpu/drm/vkms/vkms_output.c:9`）才是把
抽象的 KMS 对象一个个建出来、并把它们"接线"的地方。它的骨架（按配置遍历创建）非常具有代表性：

```c
// drivers/gpu/drm/vkms/vkms_output.c:9 (节选)
int vkms_output_init(struct vkms_device *vkmsdev)
{
    struct drm_device *dev = &vkmsdev->drm;
    ...
    /* (1) 先建所有 plane */
    vkms_config_for_each_plane(vkmsdev->config, plane_cfg) {
        plane_cfg->plane = vkms_plane_init(vkmsdev, plane_cfg);   // -> drm_universal_plane_init
        ...
    }
    /* (2) 再建 CRTC, 并把 primary/cursor plane 绑给它 */
    vkms_config_for_each_crtc(vkmsdev->config, crtc_cfg) {
        primary = vkms_config_crtc_primary_plane(...);
        cursor  = vkms_config_crtc_cursor_plane(...);
        crtc_cfg->crtc = vkms_crtc_init(dev, &primary->plane->base,
                                        cursor ? &cursor->plane->base : NULL);
        if (vkms_config_crtc_get_writeback(crtc_cfg))            // (2b) 可选 writeback 连接器
            vkms_enable_writeback_connector(vkmsdev, crtc_cfg->crtc);
    }
    /* (3) 给每个 plane 设置 possible_crtcs 位掩码(它能配合哪些 CRTC) */
    vkms_config_for_each_plane(...) {
        ... plane->base.possible_crtcs |= drm_crtc_mask(&possible_crtc->crtc->crtc);
    }
    /* (4) 建 encoder, 设置 possible_crtcs / possible_clones */
    vkms_config_for_each_encoder(...) {
        drmm_encoder_init(dev, encoder, NULL, DRM_MODE_ENCODER_VIRTUAL, NULL);
        ...
    }
    /* (5) 建 connector, 并把它 attach 到可用的 encoder */
    vkms_config_for_each_connector(...) {
        connector_cfg->connector = vkms_connector_init(vkmsdev);
        drm_connector_attach_encoder(&connector->base, possible_encoder->encoder);
    }
    /* (6) 复位整套 mode 配置到一致初始态 */
    drm_mode_config_reset(dev);
    return 0;
}
```

这里有几条**所有 KMS 驱动通用的规律**，值得现在就刻进脑子（第 05 章会逐一展开）：

- **创建顺序有讲究**：先 plane（因为 CRTC 初始化要把 primary/cursor plane 传进去），再 CRTC，
  再 encoder，再 connector。对象间是有依赖的。
- **"谁能配谁"用位掩码表达**：`plane->possible_crtcs`、`encoder->possible_crtcs`、
  `encoder->possible_clones`、`connector` ↔ `encoder` 的 attach，共同描述了这块显示硬件的"布线约束"。
  原子 check 阶段（第 06 章）会用这些掩码判断用户请求的组合是否合法。
- **`drmm_*` 系列分配**：如 `drmm_encoder_init`、`drmm_kzalloc`——这些是"绑定到 drm_device 生命周期"的
  托管分配，设备销毁时自动释放，不必手写 free。这是第 02 章"托管资源"的又一处实证。
- **`drm_mode_config_reset`**：把所有 KMS 对象的软件状态复位到一个自洽的初始值，是注册前的收尾。

> `vkms_output_init` 用 `vkms_config_for_each_*` 遍历"配置"来决定建多少 plane/CRTC/connector——这正是
> 0.5.7 要讲的"configfs 可配置多实例"能力的落点。真实驱动（如 intel_display）则是按**硬件探测结果**
> 来建对象，但"建对象 + 设布线掩码 + reset"的骨架完全一致。

### 0.5.6 提交尾段 `vkms_atomic_commit_tail`：软件如何"扫描输出"

显示路径的最后一步是"把新状态推到硬件并等它生效"。对真实硬件，这意味着写寄存器、等 vblank；
对 vkms，"硬件"是 CPU 合成。看它的实现（`vkms_drv.c:65`）：

```c
static void vkms_atomic_commit_tail(struct drm_atomic_state *old_state)
{
    struct drm_device *dev = old_state->dev;
    ...
    drm_atomic_helper_commit_modeset_disables(dev, old_state);   // (1) 关掉要关的 CRTC/encoder
    drm_atomic_helper_commit_planes(dev, old_state, 0);          // (2) 提交各 plane 的新内容
    drm_atomic_helper_commit_modeset_enables(dev, old_state);    // (3) 打开要开的 CRTC/encoder
    drm_atomic_helper_fake_vblank(old_state);                    // (4) 没有真硬件, 伪造一次 vblank
    drm_atomic_helper_commit_hw_done(old_state);                 // (5) 宣告"硬件已接受新状态"
    drm_atomic_helper_wait_for_flip_done(dev, old_state);        // (6) 等"翻页完成"
    for_each_old_crtc_in_state(old_state, crtc, old_crtc_state, i) {
        struct vkms_crtc_state *vkms_state = to_vkms_crtc_state(old_crtc_state);
        flush_work(&vkms_state->composer_work);                  // (7) 等 CPU 合成线程干完活
    }
    drm_atomic_helper_cleanup_planes(dev, old_state);            // (8) 清理旧 plane 资源
}
```

- 第 (1)(2)(3) 步是 atomic helper 提供的**标准三段式**：先 disable、再提交 plane、再 enable。
  几乎每个驱动的 commit_tail 都长这样，差别只在中间"提交 plane"对硬件意味着什么。
- 第 (4) `fake_vblank` 回答了 0.13.12 的伏笔：vkms 没有真 vblank 中断源，但显示路径的"翻页完成"
  语义依赖 vblank，于是它**伪造**一次，让上层流程闭合。
- 第 (5)(6) 是 hw_done/flip_done 两个里程碑（第 06 章会精讲这套 `drm_crtc_commit` 三阶段）。
- 第 (7) `flush_work(composer_work)`：vkms 把"把各 plane 合成成最终一帧、并算 CRC"放在一个 work
  里异步做（`vkms_composer.c`），这里等它完成。这就是 0.9.5 看到的 CRC 的来源。

**把 0.5.5 与 0.5.6 连起来看**：`vkms_output_init` 建好了对象与布线（静态结构），
`vkms_atomic_commit_tail` 则是运行时把"用户想要的新状态"落到这些对象上（动态行为）。静态结构 +
动态提交，正是 KMS 的一体两面，也是第 05（结构）与第 06（提交）两章的分工。

### 0.5.7 模块参数与 configfs：同一驱动如何"多实例、可配置"

回到 `vkms_drv.c` 顶部，你会看到一组 `module_param_named`（第 43–61 行）：

```c
static bool enable_cursor = true;
module_param_named(enable_cursor, enable_cursor, bool, 0444);
MODULE_PARM_DESC(enable_cursor, "Enable/Disable cursor support");
// 同理: enable_writeback / enable_overlay / enable_plane_pipeline / create_default_dev
```

- `module_param_named(用户可见名, 变量, 类型, 权限)`：让你在 `modprobe vkms enable_overlay=1` 时
  从用户态改变驱动行为。权限 `0444` 表示在 `/sys/module/vkms/parameters/` 下只读可见。
- 这些参数最终喂给 `vkms_config_default_create(...)`（0.5.1 的 (3) 步），决定"默认设备"建几个
  plane、要不要 cursor/overlay/writeback。

而更强的能力是 **configfs**：`vkms_init` 第一步 `vkms_configfs_register()`（0.5.1 (1)）注册了
`/sys/kernel/config/vkms/` 接口，允许你在运行时 **mkdir 创建任意多个、各自配置不同的 vkms 设备**，
而不只用一个默认实例（`create_default_dev=0` 时甚至不建默认设备，全靠 configfs）。这解释了为什么
`vkms_create` 接收一个 `struct vkms_config *config` 参数——它被设计成"按配置造设备"，天然支持多实例。

> 这点对学习极有价值：你可以用 configfs 造一个"4 个 plane + writeback"的 vkms、再造一个"最小单 plane"
> 的 vkms，对比它们在 `drm_info`/`modetest` 下的差异，从而把"配置 → 对象 → 用户态可见能力"这条因果
> 链摸透。具体玩法见 0.9.9 与课后题 0.13.18。

### 0.5.8 反向看一遍：卸载与错误回滚

读懂一个驱动，"怎么活"要配合"怎么死"。看 `vkms_destroy`（`vkms_drv.c:251`）与 `vkms_create` 的
错误回滚标签，能巩固"申请顺序 = 释放逆序"的内核铁律：

```c
void vkms_destroy(struct vkms_config *config)
{
    struct faux_device *fdev = config->dev->faux_dev;
    drm_dev_unregister(&config->dev->drm);          // (1) 先从用户态摘除: /dev/dri/cardN 消失
    drm_atomic_helper_shutdown(&config->dev->drm);  // (2) 关闭所有 CRTC/输出, 把显示停下
    devres_release_group(&fdev->dev, NULL);         // (3) 释放当初 devres_open_group 圈起的全部托管资源
    faux_device_destroy(fdev);                       // (4) 销毁载体设备
    config->dev = NULL;
}
```

两条要点：

- **顺序与创建严格相反**：创建是 `faux_device_create → devres_open_group → devm_drm_dev_alloc →
  ... → drm_dev_register`；销毁就 `drm_dev_unregister → ... → devres_release_group →
  faux_device_destroy`。`drm_dev_unregister` 必须排在最前——**先让用户态再也碰不到它，再拆内部**，
  否则正在 open 的进程会踩到半拆的设备（与 0.11 陷阱 6 同源）。
- **`devm_*`/`drmm_*` 的回报在此兑现**：注意 `vkms_create` 里那么多 `devm_drm_dev_alloc`、
  `drmm_mode_config_init`、`drmm_encoder_init` 都**没有**配对的手写 free——它们全靠
  `devres_release_group`（或设备引用归零）一把回收。这就是托管资源"少写错少漏写"的价值（第 02 章）。

再看 `vkms_create` 的错误处理用了 `goto out_devres` / `out_unregister` 阶梯式回滚：哪一步失败，就
跳到对应标签、只回滚已成功申请的部分。这种"阶梯 goto"是内核错误处理的标准范式，全书驱动代码里
随处可见，第 17 章 Xe 的 `probe` 也是同一套路，只是层数更多。

---

## 0.6 机制深入：一个设备节点是怎么"接住"用户态请求的

把上面的"出生"串成一条因果链，你就理解了 DRM 最基本的工作机制：

```
modprobe vkms
  → vkms_init → vkms_create → drm_dev_register(&drm, 0)
       → DRM 核心为该 device 分配 minor, 创建字符设备节点:
            primary  → /dev/dri/card0      (因为声明了 DRIVER_MODESET)
            render   → /dev/dri/renderD128 (仅当声明了 DRIVER_RENDER; vkms 默认不创建)
  → 用户态 open("/dev/dri/card0")
       → DRM 核心 drm_open() 分配 struct drm_file (该进程的上下文)
  → 用户态 ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &req)
       → DRM 核心 drm_ioctl() 查 ioctl 表, 鉴权, 拷入参数
            → 转给 atomic 核心 / 驱动回调
```

### 0.6.1 primary node 与 render node 的分工

| 节点 | 路径 | 触发条件 | 能做什么 | 谁在用 |
|------|------|----------|----------|--------|
| **primary** | `/dev/dri/cardN` | 驱动声明 `DRIVER_MODESET` | 显示设置（KMS）+（历史上）渲染；需要 DRM master 才能改显示 | 合成器、X、内核 fbcon |
| **render** | `/dev/dri/renderD(128+N)` | 驱动声明 `DRIVER_RENDER` | 只做渲染/计算提交，**不能**改显示，无需 master | Mesa、计算/AI 负载、容器内进程 |

为什么要分两个节点？这是 **DRI3 + render node**（约 2013，Linux 3.12）安全模型的核心：

- 想用 GPU **算东西**的进程（比如浏览器的 GPU 进程、容器里的 ML 任务），只需要 render node。
  render node **不涉及显示控制**，因此可以放心地给非特权进程、不需要 X/合成器中转。
- 想 **控制显示**（点屏、改分辨率、翻页）的，才需要 primary node，并且要竞争到 **DRM master**
  身份（同一时刻只能有一个 master 操纵显示，避免多家抢屏）。

> vkms 默认只创建 primary node（它是显示驱动，不暴露渲染）。真实的 Xe/i915 同时创建 primary 和
> render。这条区别会在第 03 章（鉴权与 master）和第六篇（render 路径）反复用到。

### 0.6.2 为什么 vkms 不写 ioctl 也能工作

因为 **DRM 核心把"通用动作"全做了**：设备节点管理、文件打开/关闭、ioctl 分发与鉴权、GEM 句柄
管理、atomic 接口解析……驱动只需要"声明能力 + 填回调 + 提供硬件相关的那一小段"。这种"核心通用 +
驱动特化"的分层，是 DRM 能同时托起 70 多个厂商驱动（见 0.7）的根本原因，也是本书前四篇值得重投入
的原因：**通用框架学一次，所有驱动都受用**。

### 0.6.3 ioctl 分发表长什么样（先睹为快）

`drm_ioctl()` 收到请求后，靠一张**静态分发表**找到处理函数与权限要求。表项由宏定义
（`drivers/gpu/drm/drm_ioctl.c:623`）：

```c
#define DRM_IOCTL_DEF(ioctl, _func, _flags) \
    [DRM_IOCTL_NR(ioctl)] = { .cmd = ioctl, .func = _func, .flags = _flags, ... }

static const struct drm_ioctl_desc drm_ioctls[] = {
    DRM_IOCTL_DEF(DRM_IOCTL_VERSION,     drm_version,         DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF(DRM_IOCTL_GET_CAP,     drm_getcap,          DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF(DRM_IOCTL_SET_MASTER,  drm_setmaster_ioctl, 0),
    DRM_IOCTL_DEF(DRM_IOCTL_AUTH_MAGIC,  drm_authmagic,       DRM_MASTER),
    DRM_IOCTL_DEF(DRM_IOCTL_GEM_CLOSE,   drm_gem_close_ioctl, DRM_RENDER_ALLOW),
    ... // 共上百条
};
```

每条三要素：**命令号 + 处理函数 + 权限标志**。权限标志正是 0.6.1 那套安全模型的代码落点
（检查逻辑在 `drm_ioctl.c:606` 附近）：

| 标志 | 含义 | 例 |
|------|------|----|
| `0`（无） | 任何打开了设备的进程都能调 | `DRM_IOCTL_GET_CAP`（部分） |
| `DRM_AUTH` | 需经过鉴权（authenticated）的客户端 | 老式 GEM flink |
| `DRM_MASTER` | 必须是当前 DRM master（控显示者） | `DRM_IOCTL_AUTH_MAGIC` |
| `DRM_ROOT_ONLY` | 仅 root | 一些危险/遗留操作 |
| `DRM_RENDER_ALLOW` | **允许在 render node 上调用** | `DRM_IOCTL_GEM_CLOSE`、各 `*_EXEC` |

关键洞察：**一个 ioctl 能不能在 render node 上用，取决于它有没有 `DRM_RENDER_ALLOW`**。显示类
ioctl 普遍没有这个标志，所以你在 `renderD128` 上 `SET_MASTER`/改 mode 会被拒——这从代码层面坐实了
0.6.1"render node 不能控显示"的论断。第 03 章会逐行读这段检查逻辑（`drm_ioctl.c:600+`）。

> 厂商驱动（如 Xe）会在自己的 `drm_driver.ioctls[]` 里追加 `DRM_IOCTL_XE_*` 表项，与核心表合并。
> 也就是说"渲染路径"的 `XE_VM_BIND`/`XE_EXEC` 等也是同一套分发 + 权限机制（带 `DRM_RENDER_ALLOW`），
> 只是处理函数指向 Xe 驱动。第 17、19 章会看到 Xe 的 ioctl 表。

---

## 0.7 量化分析：这片大陆到底有多大

学习一套框架前，先对它的"体量"有个量化认识，能帮你合理分配精力。以下数字基于本书基线
`7.1.0-rc6`，用 `wc -l` 统计（统计口径：`*.c` 与 `*.h`，含注释与空行；仅供量级参考）：

| 范围 | 代码行数 | 说明 |
|------|----------|------|
| DRM 核心顶层（`drivers/gpu/drm/*.c`） | ~83,000 | drm_drv/ioctl/atomic/gem/... 的"通用框架"，本书前四篇主战场 |
| 整个 `drivers/gpu/drm/` 子系统 | ~8,000,000 | 含全部厂商驱动与**大量自动生成的 AMD 寄存器头**，量级最大 |
| `drivers/gpu/drm/i915/`（传统 Intel） | ~418,000 | 单个驱动就堪比一个中型子系统 |
| `drivers/gpu/drm/xe/`（现代 Intel，本书主线） | ~137,000 | 比 i915 精简，但仍是巨型驱动 |
| `drivers/gpu/drm/vkms/`（纯软件，本书靶场） | ~8,000 | 麻雀虽小五脏俱全，前三篇精读它 |
| `drivers/gpu/drm/` 下驱动目录数 | ~71 | DRM 是 Linux 中厂商驱动最多的子系统之一 |

**从这些数字能读出三件事**（量化思维示例，本书各章都会这样用数据说话）：

1. **通用框架的杠杆率极高**：8.3 万行的核心，支撑了 70+ 个驱动、数百万行驱动代码。**投资前四篇
   = 投资所有驱动的可读性**。这就是本书把通用框架放最前、讲最透的依据。
2. **不可能"读完所有代码"**：i915 单驱动 41.8 万行，靠通读不现实。正确策略是**沿调用链按需深入**
   （本书每章示范怎么追一条链），而非地毯式扫读。vkms 的 8000 行则可以、也应该通读。
3. **Xe 比 i915 精简约 1/3 体量却功能更现代**：这正是 0.2.6"多用通用框架、少造轮子"的量化体现——
   Xe 把页表、调度、地址空间等都交给 DRM 通用层（`drm_gpuvm`/`drm_sched`/TTM），自己只留硬件相关部分。

**通用核心内部的"重量分布"**（顶层 `drm_*.c` 中最大的若干文件，`wc -l`，基线 `7.1.0-rc6`）：

| 文件 | 行数 | 是什么 | 精读章节 |
|------|------|--------|----------|
| `drm_edid.c` | ~7,665 | EDID 解析（显示器自报家门的数据格式，极其琐碎） | 第 07 章 |
| `drm_atomic_helper.c` | ~4,104 | 原子提交的"标准实现"，驱动大量复用 | 第 06 章 |
| `drm_connector.c` | ~3,661 | 连接器对象与属性、热插拔 | 第 05、07 章 |
| `drm_gpuvm.c` | ~3,244 | GPU 虚拟地址管理（Xe VM-bind 的通用地基） | 第 12 章 |
| `drm_vblank.c` | ~2,320 | vblank 记账与事件 | 第 04 章 |
| `drm_atomic.c` | ~2,143 | 原子状态机核心 | 第 06 章 |
| `drm_plane.c` | ~1,879 | plane 对象 | 第 05 章 |
| `drm_gem.c` | ~1,764 | GEM 对象生命周期 | 第 09 章 |
| `drm_atomic_uapi.c` | ~1,741 | atomic ioctl 的用户态翻译层 | 第 06 章 |
| `drm_syncobj.c` | ~1,719 | 同步对象（Vulkan fence 容器） | 第 10 章 |
| `drm_drv.c` | ~1,283 | 设备注册（你已在概念上读过它的作用） | 第 03 章 |

**从重量分布能读出**：

- **原子显示（atomic.c + helper + uapi ≈ 8000 行）是核心里最重的一坨**，这也是本书给第 06 章
  分配 2400–2900 行篇幅、单列一章的原因——它是显示路径的"心脏"。
- **`drm_edid.c` 近 7700 行只为解析一个数据格式**，说明"和真实显示器打交道"的琐碎程度远超想象；
  本书第 07 章会讲清它的结构，但不会逐行——这正是"沿主干读、枝节按需查"的取舍示范。
- **`drm_gpuvm.c` 这种较新的通用框架（3200+ 行）体量已不小**，说明现代 DRM 正把越来越多"原本各驱动
  自己写"的能力上提为通用层——与 0.2.6 的 Xe 哲学一脉相承。

> 这些行数会随版本变化，但"哪几块最重、为什么重"长期稳定。带着重量分布去学，你就知道哪些章节
> 值得慢读（atomic、gem、gpuvm），哪些可以"懂结构 + 会查"（edid）。

**一次基础操作的量级感**（后续章节会给精确测量方法，见[附录 D](./appendix-d-methodology.md)）：

| 操作 | 量级 | 备注 |
|------|------|------|
| 一次 ioctl 进出内核（系统调用 + 分发） | ~微秒级（µs） | `drm_ioctl` 的拷贝与查表开销，第 03 章测 |
| 一次 atomic 翻页到屏幕可见 | 受刷新率限制（60Hz≈16.6ms） | 必须等 vblank，第 06 章测 |
| 一次 GPU 命令提交到完成 | 从微秒到毫秒，取决于负载 | 依赖调度与硬件，第 19 章测 |

> 这些只是"数量级直觉"。本书坚持 H&P 的量化精神：凡涉及性能论断，都会在对应章节给出**可复现的
> 测量方法与真实数字**，而不是拍脑袋。本章先建立"DRM 很大、但有杠杆点"的整体判断。

---

## 0.8 硬件视角：Intel GPU 家族与如何查规格

本书第五、六篇要深入 Intel 硬件，届时一切论断都以 Intel 官方规格为准。这里先教你"怎么查"，
并建立对 Intel GPU 家族的粗略坐标。

### 0.8.1 Intel GPU 代际坐标（粗略）

| 时期/架构 | 代表产品 | 驱动 | 备注 |
|-----------|----------|------|------|
| Gen8–Gen11 | Broadwell…Ice Lake 集显 | i915 | 经典 execlists 时代 |
| Gen12 / Xe-LP | Tiger Lake、DG1 | i915（也是 Xe 起点） | Xe 架构开端 |
| Xe-HPG | Alchemist（Arc A 系列）、DG2 | i915 与 xe 均支持 | 独立显卡 |
| Xe-HPC | Ponte Vecchio | xe | 数据中心计算 |
| Xe2-LPG / 后续 | Lunar Lake、Battlemage 等 | **xe**（主推） | 本书主线代际 |

> `DRM_XE` 在 Kconfig 中的菜单名正是 **"Intel Xe2 Graphics"**（见 0.9 配置），点明它面向 Xe 及之后代际。
> i915 仍维护着大量较老硬件；新硬件优先走 xe。"i915 与 xe 在 DG2/Gen12 上的重叠"是历史过渡的结果。

### 0.8.2 如何在线检索 Intel 规格（Bspec / PRM）

Intel 公开两类官方文档，本书引用硬件细节时都会指向它们：

- **PRM（Programmer's Reference Manual，程序员参考手册）**：按代际发布的成卷 PDF/网页，覆盖命令格式、
  寄存器、显示、内存等。历史上发布在 01.org，现整合到 Intel 官网"Intel Graphics Documentation"。
  搜索关键词示例：`Intel Graphics PRM <代际名>`（如 `Intel Graphics PRM Tiger Lake Volume Command Reference`）。
- **Bspec（Behavior Spec）**：Intel 内部更细的行为规格，部分以编号条目对外可见；社区与驱动注释里
  常以 `Bspec: <编号>` 形式引用。代码里直接 `grep` 就能看到大量锚点：

```
$ git grep -n "Bspec" drivers/gpu/drm/xe | head
```

**检索方法论（本书统一做法）**：

1. 先在内核源码里 `git grep "Bspec"` 或读驱动注释，拿到 Bspec 编号或 PRM 章节名。
2. 再去 Intel Graphics Documentation 站点按编号/代际/卷名定位原文。
3. 在教程里同时给出**源码锚点**（`file:line`）与**规格来源**（Bspec 编号或 PRM 卷名 + 链接），
   让你能双向验证。详细的文档入口清单见[附录 C](./appendix-c-refs.md)。

> 注意：具体 URL 会随 Intel 站点改版变动，所以本书优先给"可搜索的稳定标识"（PRM 卷名、Bspec 编号），
> URL 作为辅助。这也是为什么硬件章节我们强调"**查得到、验得了**"而非死链。

### 0.8.3 用 lspci 认出你的 GPU（真机）

如果你有 Intel GPU 真机，最快的"硬件视角"入口：

```
$ lspci -nn | grep -iE 'vga|display|3d'
00:02.0 VGA compatible controller [0300]: Intel Corporation Device [8086:xxxx] (rev ..)
```

- `8086` 是 Intel 的 PCI Vendor ID（记住它，第 01、17 章讲 PCI 探测会反复见到）。
- `[8086:xxxx]` 中的 `xxxx` 是 Device ID，驱动正是靠它在 PCI ID 表里匹配并决定走 i915 还是 xe、
  按哪个代际初始化（第 13、17 章）。

### 0.8.4 驱动是怎么"认领"硬件的：PCI ID 表（Xe 实例）

8086:xxxx 不是抽象概念，它在代码里是一张实打实的匹配表。看 Xe（`drivers/gpu/drm/xe/xe_pci.c`）：

```c
// drivers/gpu/drm/xe/xe_pci.c:501 (节选) —— 每一行 = "一组 Device ID → 一个平台描述符"
static const struct pci_device_id pciidlist[] = {
    INTEL_TGL_IDS(INTEL_VGA_DEVICE, &tgl_desc),    // Tiger Lake  -> tgl_desc
    INTEL_RKL_IDS(INTEL_VGA_DEVICE, &rkl_desc),    // Rocket Lake
    INTEL_ADLP_IDS(INTEL_VGA_DEVICE, &adl_p_desc), // Alder Lake-P
    ...                                             // 一直到最新代际
};

static struct pci_driver xe_pci_driver = {
    .name     = DRIVER_NAME,
    .id_table = pciidlist,           // (xe_pci.c:1329) 内核按这张表匹配设备
    .probe    = xe_pci_probe,        // (xe_pci.c:1054) 匹配成功 -> 调用它
    ...
};
// xe_pci.c:1358: pci_register_driver(&xe_pci_driver);
```

机制链条（第 17 章精读，这里先建立印象）：

1. `INTEL_VGA_DEVICE(_id, _info)`（定义于 `include/drm/intel/pciids.h:34`）展开成
   `{ vendor=0x8086, device=_id, ..., driver_data=_info }`——这里就藏着那个 `8086`。
2. `INTEL_TGL_IDS(...)` 等宏一次性铺开"某代际的全部 Device ID"，每个都绑同一个平台描述符
   （如 `tgl_desc`），描述符里记着这代有几个引擎、什么显示能力、页表怎么走……
3. 内核 PCI 子系统拿真实硬件的 `8086:xxxx` 去 `id_table` 里查；命中就回调 `xe_pci_probe(pdev, ent)`，
   `ent->driver_data` 就是那个平台描述符。**这就是"一份驱动代码适配多代硬件"的根本机制**。

> 对照预告：i915 有自己的 `pciidlist`，覆盖更老的代际；某些过渡硬件（如 DG2）两边都列。"同一块卡
> 可能被 i915 或 xe 认领"的现象，根源就在两张 PCI ID 表的覆盖范围（与内核/发行版的驱动选择策略）。

### 0.8.5 Bspec 锚点：代码里到处是"规格出处"

Intel 驱动的注释里大量直接标注 Bspec 编号，这是你"从代码反查规格"的金线。实测本基线下：

```
$ git grep -iE "bspec" drivers/gpu/drm/xe   | wc -l     # ≈ 19 处
$ git grep -iE "bspec" drivers/gpu/drm/i915 | wc -l     # ≈ 150 处
$ git grep -nE "Bspec: ?[0-9]+" drivers/gpu/drm/i915 | head
drivers/gpu/drm/i915/display/intel_fbc.c:1652:  * Bspec: 50422 ...
drivers/gpu/drm/i915/display/intel_dp.c:2201:   * ... (Bspec:49259).
```

- i915 的 Bspec 锚点（~150）远多于 xe（~19），符合"i915 更老更全、display 细节更密"的现实
  （显示子系统是共享的，但 i915 树里沉淀的硬件注释更多）。
- **用法**：读到一段你不懂的寄存器/位操作，先在附近找 `Bspec: NNNNN`，再按 0.8.2 去 Intel
  Graphics Documentation 查这个编号。第 14（寄存器）、15（页表）、16（命令）、21（显示）章会
  反复演示这种"源码锚点 ↔ Bspec 编号"的双向验证。

---

## 0.9 综合实例：从零点亮一块虚拟屏幕（放在一起看）

现在把本章所有概念串成一次完整、可复现的端到端操作。目标：**在一台没有 GPU 的 Linux（虚拟机也行）
上，加载 vkms，点亮一块虚拟显示，并观察整条链路的产物**。这同时是本章主线 Lab 的"标准答案演示"。

### 0.9.1 准备内核模块

vkms 的配置项（`drivers/gpu/drm/vkms/Kconfig`）：

```
config DRM_VKMS
    tristate "Virtual KMS (EXPERIMENTAL)"
    depends on DRM && MMU
    select DRM_CLIENT_SELECTION
    select DRM_KMS_HELPER
    select DRM_GEM_SHMEM_HELPER
    select CRC32
    select CONFIGFS_FS
```

两种方式获得 `vkms.ko`：

**(a) 用发行版自带内核**（最省事）：多数发行版已把 vkms 编成模块，直接：

```
$ sudo modprobe vkms
```

**(b) 自己编译**（学习推荐，能改源码做实验）：在内核源码树里

```
$ make menuconfig         # 路径: Device Drivers → Graphics support → Virtual KMS (DRM_VKMS) 选 M
# 或直接在 .config 里:
$ scripts/config -m DRM_VKMS
$ make modules SUBDIRS=drivers/gpu/drm/vkms     # 老写法
# 新内核用:
$ make drivers/gpu/drm/vkms/vkms.ko             # 或 make M=drivers/gpu/drm/vkms modules
$ sudo insmod drivers/gpu/drm/vkms/vkms.ko
```

> 编译整棵内核当然也行，但学习阶段单独编 vkms 模块迭代更快。第 02 章会讲内核/外部模块构建的细节。

### 0.9.2 加载并观察

```
$ sudo modprobe vkms
$ dmesg | tail
... [drm] Initialized vkms 1.0.0 ... on minor 0     # 对应 DRIVER_MAJOR/MINOR=1/0(见 0.5.4)
$ ls -l /dev/dri/
crw-rw----+ 1 root video 226,   0 ... card0          # primary node 出现了!(0.6 的 drm_dev_register 之功)
```

注意：vkms 默认**只有** `card0`，没有 `renderD128`——印证 0.6.1：它没声明 `DRIVER_RENDER`。

### 0.9.3 用 drm_info 体检

`drm_info`（独立工具，多数发行版有同名包）一把打印设备的全部 KMS 对象与能力：

```
$ drm_info /dev/dri/card0
Node: /dev/dri/card0
Driver: vkms (Virtual Kernel Mode Setting) version 1.0.0
Driver features: DRIVER_MODESET DRIVER_ATOMIC DRIVER_GEM   # 正是 0.5.4 声明的三个 flag!
...
Connectors:
  Connector 0  (Virtual)  status: connected   modes: 1024x768 ...
CRTCs / Planes: ...
```

把这份输出和 0.5 的源码对照，你会获得强烈的"代码→现象"对应感：你在 `vkms_driver` 里看到的
`driver_features`，原封不动出现在用户态体检结果里。

### 0.9.4 用 modetest 真正点亮

`modetest`（来自 libdrm 的测试程序）能枚举对象并实际驱动一次显示：

```
$ modetest -M vkms                 # 枚举该驱动的 connector/encoder/crtc/plane/mode
$ sudo modetest -M vkms -s <connector_id>@<crtc_id>:1024x768   # 在指定连接器上设置模式并显示测试图案
```

执行 `-s` 时，`modetest` 在内部走的正是 0.3.2 的**显示路径**：构造 framebuffer → 发
`DRM_IOCTL_MODE_ATOMIC`（或 legacy setcrtc）→ DRM atomic 核心 check/commit → vkms 的
`vkms_atomic_commit_tail` 用 CPU 合成像素 → `fake_vblank` 报告翻页完成。整条链路你已在 0.5 读过源码。

### 0.9.5 看见"扫描输出"的产物：CRC 与 writeback

vkms 的精妙在于：它能把"扫描出去的那一帧"算出 **CRC** 校验值，供测试比对（这是 IGT 测 KMS 正确性的
基础）。在 debugfs 里：

```
$ ls /sys/kernel/debug/dri/0/
... crc/ ...
$ cat /sys/kernel/debug/dri/0/crtc-0/crc/control   # CRC 源
```

这把"虚拟扫描输出"变成了可观测、可断言的数据——后续第 06 章讲 atomic 正确性、第 21 章对照真实
显示时，都会回到这个能力。至此，你已经：声明能力 → 注册设备 → 出现节点 → 用户态体检 → 设置模式 →
合成输出 → 校验结果，**完整走通了显示路径的最小闭环**。

### 0.9.6 一个可重复的开发内核：编译与配置片段

学习驱动免不了要改内核源码、加打印、跑实验，所以你需要一个**自己能编译、能快速迭代**的内核，而不是
只用发行版内核。这里给一套最小可行流程（细节与外部模块构建见第 02 章）。

**(1) 拿到与本书基线一致的源码**：本书锚点基于 `7.1.0-rc6`。`git checkout` 对应 tag，或直接用你手上的
树（函数名稳定，行号可能略有出入）。

**(2) 一个面向"学 DRM"的配置片段**。在 `.config` 里至少打开（用 `scripts/config` 批量设置）：

```
# 基础 DRM 与参考驱动
scripts/config -e DRM -m DRM_VKMS -m DRM_XE -m DRM_I915
# KMS helper / GEM / TTM / buddy（被上面 select，确认开启）
scripts/config -e DRM_KMS_HELPER -e DRM_GEM_SHMEM_HELPER -e DRM_TTM -e DRM_BUDDY
# 调试设施：本书反复用到
scripts/config -e DEBUG_FS              # debugfs：/sys/kernel/debug/dri/
scripts/config -e DYNAMIC_DEBUG         # 动态打开 drm 各处 debug 打印
scripts/config -e FTRACE -e FUNCTION_TRACER -e FUNCTION_GRAPH_TRACER
# 驱动开发期强烈建议（抓 bug 利器，性能代价大，仅开发用）
scripts/config -e PROVE_LOCKING         # lockdep：自动检测锁序错误（第 02、06 章关键）
scripts/config -e DEBUG_KMEMLEAK        # 内存泄漏检测（可选）
scripts/config -e KASAN                 # 越界/use-after-free 检测（可选，重）
scripts/config -e KUNIT -m DRM_VKMS_KUNIT_TEST   # vkms 自带的 KUnit 单元测试
```

> **为什么强调 `PROVE_LOCKING`（lockdep）**：DRM 充斥着复杂锁（ww-mutex、各种 spinlock，第 02、06 章），
> 锁序错误是驱动最常见、最难查的 bug。lockdep 能在错误**第一次发生**时就在 dmesg 里报出完整环路。
> 本书很多调试小节都假设你开了它。

**(3) 编译**：

```
$ make -j$(nproc)                 # 整树（首次较慢）
# 或只编你关心的模块, 迭代快:
$ make -j$(nproc) M=drivers/gpu/drm/vkms modules
```

**(4) KUnit 快速自测**（不需要起整机）：

```
$ ./tools/testing/kunit/kunit.py run --kunitconfig=drivers/gpu/drm/vkms
```

它会在用户态模拟环境里跑 vkms 的单元测试——这是验证"你改的东西没把基本逻辑改坏"的最快手段。

### 0.9.7 用 QEMU 起一个安全的实验机

直接在自己的主力机上 `insmod` 实验性改动有风险（可能挂死、花屏）。推荐在 **QEMU 虚拟机**里实验，
崩了重启即可。两种典型玩法：

**(a) 纯 vkms（学前三篇，最简单，零图形依赖）**：QEMU 里跑你编的内核，启动后 `modprobe vkms` 即可，
不需要任何虚拟显卡。适合 KMS/原子显示/GEM 的全部前三篇实验。

```
$ qemu-system-x86_64 -kernel arch/x86/boot/bzImage -nographic \
    -append "console=ttyS0" -initrd <你的 initramfs> -m 2G
# 进系统后:
# modprobe vkms ; ls /dev/dri ; drm_info /dev/dri/card0
```

**(b) virtio-gpu（想体验"有渲染节点"的驱动）**：给 QEMU 加 `-device virtio-gpu-pci`，guest 内核加载
`virtio_gpu` 驱动，会同时出现 `card0` 与 `renderD128`——可用来对照 vkms（只有 primary）与一个
"完整能力"驱动的节点差异（呼应 0.6.1）。

> 真正的 Intel Xe/i915 实验需要**真硬件**或 GPU 直通（VFIO passthrough），成本较高。本书的策略是：
> **前四篇用 vkms/virtio-gpu 在 QEMU 里把通用框架学透**；第五、六篇的 Xe 代码以"源码精读 + Bspec 对照"
> 为主，辅以有 Intel GPU 时的真机验证。没有 Intel 硬件也能学完本书的绝大部分——这正是我们重代码、
> 重规格的原因。

### 0.9.8 构建 IGT：DRM 的"官方测试与示例宝库"

**IGT（igt-gpu-tools）** 是 DRM 社区的测试套件，也是学习者的金矿：成百上千个 subtests 演示了每个
uAPI 的正确用法，本书的"动手编程题"很多会让你读/写 IGT 用例。

```
$ git clone https://gitlab.freedesktop.org/drm/igt-gpu-tools.git
$ cd igt-gpu-tools && meson build && ninja -C build
# 跑一个 vkms 相关的测试(示例):
$ sudo ./build/tests/kms_atomic --run-subtest ...     # 具体 subtest 名用 --list-subtests 查
```

IGT 里和本书强相关的几组：`kms_atomic`/`kms_plane`/`kms_writeback`（显示，第 05–08 章）、
`kms_vblank`（第 04 章）、各驱动的 `xe_*`/`gem_*`（渲染，第六篇）。**学某个 uAPI 时，先去 IGT 找
对应 subtest 读它怎么调，是最高效的"用法字典"。**

### 0.9.9 进阶玩法：用 configfs 造多个不同配置的 vkms

呼应 0.5.7，用 configfs 在运行时造设备，直观感受"配置 → 能力"的因果：

```
$ sudo modprobe vkms create_default_dev=0       # 不建默认设备, 全靠 configfs
$ sudo mount -t configfs none /sys/kernel/config 2>/dev/null
$ sudo mkdir /sys/kernel/config/vkms/mydev      # 造一个新 vkms 实例
# (在 mydev/ 下按接口配置 plane/crtc/connector/writeback ...)
$ ls /dev/dri/                                   # 观察新出现的 cardN
$ drm_info /dev/dri/cardN                        # 对比它与默认实例的能力差异
```

把"加了 overlay 的实例"和"最小实例"用 `drm_info` 并排对比，你会清楚看到 `vkms_output_init`
（0.5.5）里那些 `vkms_config_for_each_*` 循环的产物如何反映到用户态。

### 0.9.10 用户态那一侧：libdrm 与 Mesa（先建立印象）

虽然本书主体在内核，但要理解 uAPI 的"另一端"，需要对用户态栈有印象（第 23 章会闭环）：

- **libdrm**（`libdrm.so`）：对 `ioctl(/dev/dri/*)` 的薄封装，提供 `drmModeGetResources`、
  `drmModeAtomicCommit` 等函数。`modetest`、`drm_info` 都建立在它之上。读它的源码是理解每个 ioctl
  参数结构的捷径。
- **Mesa**：开源用户态 GPU 驱动集合。Intel 相关：**ANV**（Vulkan）、**IRIS**（OpenGL，Gen8+）。它们
  负责把 Vulkan/GL 调用编译成 GPU 命令（batch buffer，第 16 章），再通过 libdrm/ioctl 提交给内核
  Xe/i915（第 19 章）。
- **kmscube**：一个最小的"用 KMS + GBM + GL 画一个旋转立方体"的示例程序，是端到端串起显示路径与
  渲染路径的经典 demo，适合第 23 章 capstone 前热身。

> 现在你不必装齐这些。记住这条链：**应用 → Vulkan/GL → Mesa(ANV/IRIS) → libdrm → ioctl →
> 内核 Xe/i915**。本章 0.3.1 的全栈图就是它的可视化。第 23 章我们会真正打通这条闭环。

---

## 0.10 横向关联：本章是通往全书的索引

本章几乎触及了后续每一章，这里把"线头"理清，方便你日后回查：

| 本章提到的点 | 在哪一章展开 |
|--------------|--------------|
| `drm_device`/`drm_driver`/`drm_file`/`drm_minor`、ioctl 分发、master/render node | 第 03 章 DRM 设备模型与 ioctl |
| `drm_vblank_init`、`fake_vblank`、翻页完成事件 | 第 04 章 中断/事件/vblank |
| `vkms_modeset_init`、`drm_crtc_init_with_planes`、KMS 对象 | 第 05 章 KMS 对象模型 |
| `atomic_check`/`atomic_commit`/`atomic_commit_tail`、check→commit | 第 06 章 原子显示 |
| `drm_gem_fb_create`、`DRM_GEM_SHMEM_DRIVER_OPS`、句柄与 mmap | 第 09 章 GEM |
| dma-buf 跨进程共享（两条主链路的交汇点） | 第 10 章 dma-buf/fence |
| TTM 与离散显存（Xe 的内存后端） | 第 11 章 TTM |
| `drm_gpu_scheduler`、`drm_gpuvm`（Xe 的通用地基） | 第 12 章 调度器/GPUVM |
| 8086 Vendor ID、PCI 探测、代际匹配 | 第 13、17 章 |
| Bspec/PRM 检索、寄存器/页表/命令 | 第 14、15、16 章 |
| Xe 的 `probe`/VM-bind/exec/GuC | 第 17–20 章 |
| Mesa/libdrm 如何消费 uAPI（用户态闭环） | 第 23 章 |

**跨切面主题在本章的体现**（呼应[README](./README.md)的贯穿主题）：

- **性能**：0.7 用代码体量与操作量级建立直觉。
- **保护与隔离**：0.6.1 的 primary/render 分离、master 仲裁。
- **编程模型**：0.3.2 两条 uAPI 主链路、legacy vs atomic。
- **演进趋势**：0.2 整节就是一部演进史。

---

## 0.11 谬误与陷阱 + 调试

> 本节专治"想当然"。每条都是初学者真实踩过的坑，以及如何用工具自证。

### 谬误与陷阱

- **谬误 1：「DRM 就是显示驱动」。**
  错。DRM 同时管显示（KMS）**和**渲染/计算（render）。很多人只看 KMS 就以为懂了 DRM，结果一碰
  Mesa/Xe 的提交路径就晕。本书坚持"两条主链路"并讲，正是为了破这个误解。

- **谬误 2：「`/dev/dri/card0` 和 `renderD128` 是同一个设备的两个名字，随便用哪个都行」。**
  不对。它们是**同一物理设备的两个能力不同的节点**：card0 能控显示但要 master，renderD128 只能渲染
  但无需特权。用错节点会得到 `-EACCES`/`-EOPNOTSUPP`。（第 03 章细讲。）

- **谬误 3：「driver_features 只是个标签」。**
  恰恰相反，它**直接决定**创建哪些设备节点、放行哪些 ioctl。vkms 不声明 `DRIVER_RENDER`，所以根本
  没有 renderD128；声明了 `DRIVER_ATOMIC`，用户态才能用 atomic ioctl。

- **谬误 4：「vkms 是玩具，学它没用」。**
  vkms 走的是和真实驱动**完全相同**的 DRM 核心路径（drm_dev_register、atomic helper、GEM shmem……）。
  它只是把"硬件扫描输出"换成 CPU 合成。前三篇用它，能在零硬件依赖下把 KMS 学到底。

- **陷阱 5：内嵌 vs 指针。**
  `struct drm_device` 在厂商结构里是**内嵌成员**（`struct vkms_device { struct drm_device drm; ... }`），
  不是指针。误当指针解引用会直接崩。换算要用 `devm_drm_dev_alloc(..., 外层类型, 内嵌成员名)` 或
  `container_of`。

- **陷阱 6：`drm_dev_register` 之前别让用户态可见。**
  注册前 KMS 对象、内存管理器必须都备好；一旦 `drm_dev_register` 返回，节点立刻可被 open，竞态窗口
  就开了。这条"先准备、后注册"的顺序对所有驱动都成立（第 17 章 Xe 同理）。

### 环境调试小抄

| 现象 | 排查手段 |
|------|----------|
| `modprobe vkms` 失败 | `dmesg | tail`；确认 `CONFIG_DRM`/`CONFIG_DRM_VKMS` 已编入；依赖 `MMU`/`CONFIGFS_FS` |
| 没出现 `/dev/dri/card0` | 确认模块已加载 `lsmod | grep vkms`；看 dmesg 有无 `drm_dev_register` 失败 |
| 想看 DRM 详细日志 | 启用 drm.debug：`echo 0x1e > /sys/module/drm/parameters/debug`（按位掩码，谨慎，刷屏） |
| 想跟踪函数路径 | ftrace：`trace-cmd record -p function_graph -l 'drm_*' modetest -M vkms -s ...` |
| 想看活的内核数据结构 | drgn：`drgn` 脚本读 `struct drm_device`（[附录 D](./appendix-d-methodology.md)） |
| 想看设备的 debugfs | `/sys/kernel/debug/dri/<minor>/`（CRC、状态、对象列表等） |

### `drm.debug` 掩码：让内核"自报家门"

DRM 的日志由一个**按位掩码** `drm.debug` 控制（启动参数 `drm.debug=0x1e` 或运行时写
`/sys/module/drm/parameters/debug`）。常用位（与 `include/drm/drm_print.h` 的 `DRM_UT_*` 对应）：

| 位 | 名称 | 打开后能看到 |
|----|------|--------------|
| `0x01` | CORE | 设备注册、核心流程 |
| `0x02` | DRIVER | 驱动自身的 debug 打印 |
| `0x04` | KMS | 显示设置相关 |
| `0x08` | PRIME | dma-buf 导入导出 |
| `0x10` | ATOMIC | 原子 check/commit 细节（第 06 章超有用） |
| `0x20` | VBL | vblank |
| `0x40` | STATE | 原子状态对象 |

常用组合 `0x1e`（= KMS|ATOMIC|DRIVER|...）适合调显示问题。**注意刷屏与性能**：调完务必关掉
（写 `0`）。这套掩码全书显示章节会反复用到。

### DRM 的 ftrace tracepoint

除了函数图跟踪，DRM 还埋了一批**静态 tracepoint**（`/sys/kernel/tracing/events/` 下），比裸函数图
更稳定、信息更结构化。开发常用：

```
$ ls /sys/kernel/tracing/events/ | grep -iE 'drm|gpu|dma_fence'
drm        dma_fence    gpu_scheduler    ...
$ echo 1 > /sys/kernel/tracing/events/dma_fence/enable      # 跟踪所有 fence 信号/等待(第10章)
$ echo 1 > /sys/kernel/tracing/events/drm/drm_vblank_event/enable   # 跟踪 vblank 事件(第04章)
$ cat /sys/kernel/tracing/trace
```

`dma_fence`（第 10 章）、`gpu_scheduler`（第 12 章）、`drm_vblank_*`（第 04 章）这几组是后续追踪
同步与提交链路的主力。可视化可配合 `perfetto`/`gpuvis`（[附录 D](./appendix-d-methodology.md)）。

### 用 drgn 看"活着的"内核对象

`drgn` 是一个可编程的内核调试器，能直接读取运行内核的数据结构——非常适合验证你对结构关系的理解。
例如确认 vkms 设备的 driver 名：

```python
# drgn 交互式
>>> from drgn.helpers.linux import list_for_each_entry
>>> # 找到某个 drm_minor / drm_device, 读它的 driver->name
>>> # (具体脚本见附录 D；这里只示意"代码里的 struct 能在活内核里直接读出来")
```

本书很多"数据结构"小节，都可以用 drgn 把书上的字段表和**真实运行值**对上号——这是检验"读懂了没"
的硬办法。系统用法见[附录 D](./appendix-d-methodology.md)。

这些工具会在全书反复出现。第 02 章给出 ftrace/drgn/lockdep 的系统用法，[附录 D](./appendix-d-methodology.md)
给出可复现的测量规范。

---

## 0.12 随堂练习（Practice Problems）

> 边读边做，做完看本节末"即时解答"。这些题只测本章核心概念，不需要写代码。

**练习 0.1** 不查资料，凭 0.3 的全栈图，按从上到下顺序写出这几层：Mesa、合成器、libdrm、GPU 硬件、
DRM 核心、Vulkan、厂商驱动。

**练习 0.2** 一个浏览器的 GPU 进程只想用 GPU 做合成渲染、不想控制屏幕。它应该打开
`/dev/dri/card0` 还是 `/dev/dri/renderD128`？为什么？

**练习 0.3** vkms 的 `vkms_driver.driver_features` 是 `DRIVER_MODESET | DRIVER_ATOMIC | DRIVER_GEM`。
据此判断：加载 vkms 后会不会出现 `/dev/dri/renderD128`？

**练习 0.4** 在 `vkms_create()` 中，`drm_dev_register()` 这一行执行**之前**和**之后**，对"用户态能否
`open` 到这个设备"分别意味着什么？

**练习 0.5** `struct vkms_device` 里的 `drm` 字段是 `struct drm_device`（内嵌）还是
`struct drm_device *`（指针）？这对 `devm_drm_dev_alloc(&fdev->dev, &vkms_driver, struct vkms_device, drm)`
最后两个参数的写法有什么影响？

**练习 0.6** 用 0.2 的演进史回答：为什么现代驱动几乎都用 atomic 而不用 legacy modeset 接口？atomic
解决了 legacy 的什么根本缺陷？

**练习 0.7（量化）** 已知 i915 约 41.8 万行、xe 约 13.7 万行、vkms 约 0.8 万行。如果你每天能精读
约 500 行驱动代码，"通读 vkms"和"通读 i915"分别需要多久？这组对比支持本书"沿调用链按需深入"的策略吗？

---

### 随堂练习即时解答

**解 0.1** 从上到下：合成器 → Vulkan → Mesa → libdrm →（用户/内核分界）→ DRM 核心 → 厂商驱动 →
GPU 硬件。对照 0.3.1 的图核验。关键是记住"libdrm 在用户态、DRM 核心在内核态，二者隔着 ioctl 分界"。

**解 0.2** 应打开 `renderD128`（render node）。因为它只做渲染/计算、不涉及显示控制，无需 DRM master
特权，适合非特权/容器进程；而 `card0` 用于显示控制且需竞争 master（见 0.6.1）。

**解 0.3** 不会。出现 `renderD128` 的前提是驱动声明 `DRIVER_RENDER`，而 vkms 的 features 里没有它，
只有 `MODESET/ATOMIC/GEM`，故只创建 primary node `card0`（见 0.5.4、0.6.1）。

**解 0.4** 之前：设备处于"准备中"，KMS 对象/内存管理器正在搭建，用户态**看不到**节点、无法 open。
之后（成功返回）：`/dev/dri/cardN` 出现、udev 收到事件，用户态**可以** open 并发 ioctl。这条分界要求
"先把一切准备好、再注册"（见 0.5.2 (H)、0.6、0.11 陷阱 6）。

**解 0.5** 是**内嵌**的 `struct drm_device drm;`，不是指针。正因为内嵌，`devm_drm_dev_alloc` 才能用
"外层类型 `struct vkms_device` + 内嵌成员名 `drm`"来分配整个外层结构并定位其中的 drm 子对象
（见 0.4.1、0.5.2 (C)、0.11 陷阱 5）。

**解 0.6** legacy 接口一次只能改一个对象/属性，无法把"一组相互依赖的显示更新"原子提交，可能出现
硬件不支持的中间组合或撕裂；且无法在提交前统一校验整套组合是否可行。atomic 用"check（整套能否做到）
→ commit（全有或全无）"解决了这两点（见 0.2.4）。

**解 0.7** vkms：8000/500 ≈ 16 天可通读；i915：418000/500 ≈ 836 天（两年多）显然不现实。这正支持
本书策略：vkms 可、也应通读；i915/xe 这类巨型驱动只能**沿调用链按需深入**，靠的是前四篇打下的通用
框架理解（见 0.7）。

---

## 0.13 课后练习题

> 仅题目。答案见 [`answers/00-intro-setup-answers.md`](./answers/00-intro-setup-answers.md)。
> 标注 ★ 的为需要动手或综合的题。建议全部独立完成后再对答案。

### L1 概念辨析

**0.13.1** 用自己的话解释 UMS 与 KMS 的区别，并说明把"模式设置"搬进内核带来的两个具体好处。

**0.13.2** DRI1/DRI2/DRI3 三代各自的标志性进步是什么？dma-buf 是在哪一代成为跨进程共享基础的？

**0.13.3** 解释 GEM 与 TTM 的设计出发点差异；为什么现代离散 GPU 驱动会"用 GEM 对象 + TTM 后端"？

**0.13.4** 列出本章出现的"演进暗线"中至少四对（形如 `A→B`），并各用一句话说明驱动这次演进的动机。

**0.13.5** `DRIVER_MODESET`、`DRIVER_ATOMIC`、`DRIVER_GEM`、`DRIVER_RENDER` 四个 feature flag 各自
对"创建什么节点 / 放行什么能力"意味着什么？

### L2 数据结构与源码阅读

**0.13.6** 阅读 `drivers/gpu/drm/vkms/vkms_drv.c:93` 的 `vkms_driver`，列出它声明的所有字段，并说明
`DEFINE_DRM_GEM_FOPS(vkms_driver_fops)`（同文件:63）为它省去了哪些本该手写的 `file_operations`。

**0.13.7** 阅读 `vkms_create`（`vkms_drv.c:160`），按出现顺序列出从 `faux_device_create` 到
`drm_dev_register` 之间所有"会改变系统可见状态或申请资源"的调用，并指出哪一行是"用户态首次可见"的分界。

**0.13.8** 阅读 `vkms_modeset_init`（`vkms_drv.c:133`）与 `vkms_mode_funcs`（:123），指出
`fb_create`、`atomic_check`、`atomic_commit` 三个回调分别指向谁，并说明它们各属于哪条主链路。

**0.13.9** 在内核树执行 `git grep -n "container_of" drivers/gpu/drm/vkms`（或阅读
`to_vkms_crtc_state`），解释厂商结构如何在"内嵌的 drm 子对象"与"外层结构"之间换算。

**0.13.10 ★** 阅读 `drivers/gpu/drm/xe/Kconfig` 的 `config DRM_XE` 段，列出它 `select` 的、与本书后续
篇章相关的至少五个组件（如 `DRM_TTM`、`DRM_BUDDY`、`DRM_KMS_HELPER`、各 display helper 等），并各
对应到本书哪一章会讲。

### L3 机制分析

**0.13.11** 画出从 `modprobe vkms` 到用户态 `ioctl(fd, DRM_IOCTL_MODE_ATOMIC, ...)` 被驱动回调接住的
完整调用/事件序列（ASCII 时序图），标注哪些步骤在内核核心、哪些在 vkms 驱动。

**0.13.12** 解释 vkms 为什么需要 `drm_vblank_init` 与 `drm_atomic_helper_fake_vblank`——一个**没有真实
显示硬件**的驱动为何还要"假装有 vblank"？这与显示路径的"翻页完成"语义有何关系？

**0.13.13** 结合 0.6.1，描述一个具体场景：两个进程同时想控制同一块屏幕会发生什么？DRM master 机制
如何仲裁？render node 为什么不受这个限制？

### L4 设计与对比

**0.13.14** 用一张表对比 i915 与 Xe 在"内存管理 / 地址空间 / 绑定模型 / 命令提交 / 调度"五个维度的
选择，并论述 Xe"多用通用框架"在**可维护性**与**性能**上的潜在利弊。

**0.13.15** 为什么 DRM 选择"通用核心 + 厂商驱动"的分层，而不是让每个厂商各写一套完整栈？用 0.7 的
量化数据支撑你的论证。

**0.13.16** legacy 与 atomic 两套显示 uAPI 并存至今。从"向后兼容"与"维护成本"角度，讨论内核为何
保留 legacy 而把它实现成 atomic 之上的兼容垫片，而不是彻底删除。

### L5 动手编程 / 调试

**0.13.17 ★** 完成本章主线 Lab：编译/加载 vkms，用 `drm_info` 打印能力，用 `modetest -M vkms -s ...`
点亮一块虚拟显示。把 `drm_info` 输出里的 `Driver features` 与源码 `vkms_driver.driver_features` 对照，
确认完全一致，并截图/粘贴关键输出。

**0.13.18 ★** 用模块参数改变 vkms 行为：`modprobe vkms enable_overlay=1`（或卸载后重载），用 `modetest`
观察 plane 数量变化。结合 `vkms_drv.c:51` 的 `enable_overlay` 模块参数解释现象。

**0.13.19 ★** 打开 drm 调试日志（`echo 0x1e > /sys/module/drm/parameters/debug`），重做一次 modetest
设置模式，从 `dmesg` 中找出 atomic check 与 commit 的日志行，**初步**指认它们对应 0.3.2 显示路径的
哪几步（不要求看懂细节，只要求建立"日志↔链路"的对应）。

**0.13.20 ★** 用 ftrace（`trace-cmd` 或 `/sys/kernel/tracing`）记录一次 `modetest -M vkms -s ...` 期间
所有 `drm_atomic_*` 与 `vkms_*` 函数的调用，导出调用图，找出 `vkms_atomic_commit_tail` 的位置，
并对照 0.5.3 解释它在整条提交里的角色。

### 综合大题（贯通本篇/全书导引）

**0.13.21 ★（综合）** 画一张"本书学习地图"：以 0.3 的两条主链路为骨架，把第 03–23 章每一章**贴到
链路的对应位置**上（哪一章负责链路的哪一段）。这张图将作为你后续每学完一章就回填一次的"进度看板"。

**0.13.22 ★（综合）** 选择你最终最关心的一个目标之一：①只读懂显示路径；②只读懂渲染/计算路径；
③两者都要。基于 0.8 的依赖图与 0.9 的一学期节奏，为自己定制一条章节阅读顺序与时间预算，并说明
取舍理由。

**0.13.23 ★（综合·硬件视角）** 在内核树执行 `git grep -n "Bspec" drivers/gpu/drm/xe | head`，任取一条
带 Bspec 编号的注释，记录它所在的 `file:line` 与编号；再用 0.8.2 的方法去 Intel Graphics Documentation
尝试定位该条目主题（找不到精确原文也没关系，记录你的检索路径）。这是为第五、六篇"查得到、验得了"
做的热身。

---

## 0.14 本章小结

- **DRM 是内核里的 GPU 主权机构**，同时管两条主链路：**显示（KMS）** 与 **渲染/计算（render）**。
  分清你在看哪条路，是读懂一切的前提。
- **历史即动机**：UMS→KMS、DRI1→DRI3、legacy→atomic、relocation→VM-bind、i915→Xe，每一步都在做
  "通用化 + 安全化 + 显式化"。理解动机，代码就不再是死记。
- **通用核心 + 厂商驱动** 的分层带来极高杠杆：8.3 万行核心托起 70+ 驱动、数百万行代码。**前四篇
  打通通用框架，是对所有驱动可读性的投资**。
- **vkms** 是零硬件依赖、走真实 DRM 路径的最佳靶场。你已逐行读过它的"出生"（`vkms_init` →
  `vkms_create` → `drm_dev_register`），并亲手点亮了一块虚拟屏幕。
- **节点即能力**：`driver_features` 决定创建 primary/render 哪些节点、放行哪些 ioctl；primary 控显示
  需 master，render 只渲染无需特权。
- **工具箱已就位**：`drm_info`/`modetest`/debugfs/ftrace/drgn，以及"如何查 Intel Bspec/PRM"。后续章节
  会反复使用它们。

### 知识点清单（自检：能否不看书复述）

- [ ] 图形栈分层、DRM 的上下游、两条主链路各经过哪些子系统。
- [ ] primary node vs render node 的能力与触发条件（`DRIVER_MODESET`/`DRIVER_RENDER`）。
- [ ] DRM 的关键演进及其动机（至少四对 `A→B`）。
- [ ] `drm_device`/`drm_driver`/`drm_file`/`drm_minor` 各自职责与"内嵌 drm"惯用法。
- [ ] vkms `probe` 八步模板，特别是 `drm_dev_register` 的"可见性分界"。
- [ ] 如何加载 vkms、用 drm_info/modetest 体检与点亮、用 debugfs/ftrace 观察。
- [ ] 如何检索 Intel Bspec/PRM 并与源码锚点双向验证。

### 延伸阅读

- 内核文档：`Documentation/gpu/introduction.rst`（图形栈与 DRM 概念总览）、
  `Documentation/gpu/drm-internals.rst`（驱动初始化与核心服务）、
  `Documentation/gpu/vkms.rst`（vkms 专章）。
- 源码：`drivers/gpu/drm/vkms/`（建议本周内通读，约 8000 行）；
  `drivers/gpu/drm/drm_drv.c`（设备注册核心，第 03 章精读）。
- 规格检索入口与三本方法论经典的对照阅读建议见 [附录 C](./appendix-c-refs.md)。
- 测量与调试工具的系统用法见 [附录 D](./appendix-d-methodology.md)。

> **下一章预告**：第 01 章《GPU 硬件工作原理》。在深入任何驱动代码前，我们先把"GPU 作为一块 PCIe
> 设备到底怎么工作"讲透——配置空间、BAR、MSI-X 中断、命令流处理器、执行单元、显存层次与 CPU↔GPU
> 一致性。这是读懂第五、六篇 Intel 硬件与 Xe 提交路径的硬地基。
