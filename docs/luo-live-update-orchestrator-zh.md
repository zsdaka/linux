# Linux Live Update Orchestrator（LUO）机制深度分析

> 分析对象：Linux 6.18.5 内核源码树（本仓库），模块路径 `kernel/liveupdate/`、`mm/memfd_luo.c`
> 等。LUO（Live Update Orchestrator，"实时更新编排器"）是建立在 KHO（Kexec HandOver，
> "kexec 交接"）机制之上、用于在 **kexec 重启过程中保留用户态关键资源状态** 的全新内核子系统。
> 所有代码引用均采用 `文件:行号` 形式（基于本仓库当前 HEAD），可在仓库中直接跳转核对。
>
> 本文不仅讲"LUO 是什么、怎么实现"，更着力讲清楚"为什么要这样设计、权衡了什么、
> 如果要在此基础上支持 VFIO/QEMU 热升级应该怎么做"。希望读完后，你能：
>
> 1. 看懂 `kernel/liveupdate/` 下每一个函数在状态机中的位置和职责；
> 2. 理解 LUO 与它的地基 KHO 之间的依赖关系，以及 FDT 序列化 ABI 的设计哲学；
> 3. 通过 `tools/testing/selftests/liveupdate/` 的范例，建立"用户态如何驱动 LUO"的直觉；
> 4. 评估在 VFIO 子系统中落地 LUO 支持需要解决哪些问题，以及 QEMU 如何借此实现热升级。

---

## 目录

- [第 0 章 导读：LUO 是什么，为什么是现在](#第-0-章-导读luo-是什么为什么是现在)
- [第 1 章 LUO 解决的问题：动机与设计目标](#第-1-章-luo-解决的问题动机与设计目标)
- [第 2 章 地基：KHO（Kexec HandOver）机制详解](#第-2-章-地基khokexec-handover机制详解)
- [第 3 章 LUO 整体架构总览](#第-3-章-luo-整体架构总览)
- [第 4 章 用户态 ioctl 接口完整走读](#第-4-章-用户态-ioctl-接口完整走读)
- [第 5 章 Session 子系统源码精读](#第-5-章-session-子系统源码精读)
- [第 6 章 File 保存生命周期源码精读](#第-6-章-file-保存生命周期源码精读)
- [第 7 章 FLB 全局共享对象源码精读](#第-7-章-flb-全局共享对象源码精读)
- [第 8 章 序列化 ABI 与内存布局精解](#第-8-章-序列化-abi-与内存布局精解)
- [第 9 章 memfd 跨 session 保存恢复完整精读](#第-9-章-memfd-跨-session-保存恢复完整精读)
- [第 10 章 端到端实例串讲：selftest 逐行解读](#第-10-章-端到端实例串讲selftest-逐行解读)
- [第 11 章 错误处理与异常路径全景梳理](#第-11-章-错误处理与异常路径全景梳理)
- [第 12 章 VFIO 对 LUO 的支持现状与差距分析](#第-12-章-vfio-对-luo-的支持现状与差距分析)
- [第 13 章 VFIO 集成 LUO 的设计草案](#第-13-章-vfio-集成-luo-的设计草案)
- [第 14 章 QEMU 利用 LUO 实现热升级的完整方案](#第-14-章-qemu-利用-luo-实现热升级的完整方案)
- [附录 A 常见问题与设计取舍 FAQ](#附录-a-常见问题与设计取舍-faq)
- [附录 B 术语表](#附录-b-术语表)
- [附录 C 关键函数与数据结构索引表](#附录-c-关键函数与数据结构索引表)
- [附录 D 参考资料](#附录-d-参考资料)

---

## 第 0 章 导读：LUO 是什么，为什么是现在

### 0.1 一句话定义

`kernel/liveupdate/luo_core.c:8-39` 的模块级文档注释给出了最权威的定义：

```text
Live Update is a specialized, kexec-based reboot process that allows a
running kernel to be updated from one version to another while preserving
the state of selected resources and keeping designated hardware devices
operational. For these devices, DMA activity may continue throughout the
kernel transition.
```

翻译过来：**Live Update 是一种基于 kexec 的特殊重启流程，它能在升级内核版本的同时，
保留被选中资源的状态，并让指定的硬件设备在整个内核切换过程中持续工作（DMA 活动不中断）**。

LUO（Live Update Orchestrator）就是承载和编排这一流程的内核子系统——它本身不直接保存
任何具体资源的状态（那是各个子系统、各个文件类型 handler 的职责），而是提供：

- 一套**状态机**和**生命周期回调框架**，让"何时该保存、何时该冻结、何时该恢复、
  何时该清理"这件事有统一、可预测的时序；
- 一套**用户态 ioctl 接口**（`/dev/liveupdate`），让用户态代理（比如 QEMU、或者更上层的
  虚拟化管理栈）能够声明"这些文件描述符需要在升级后继续使用"；
- 一套**序列化框架**，建立在 KHO 之上，把"哪些 session、哪些文件、用哪个 handler、
  私有数据是什么"这些元数据可靠地传递到新内核。

### 0.2 LUO 在系统软件版图中的位置

为了避免把 LUO 和邻近的几个机制搞混，先画一张定位图：

```text
                     "我要在不丢失运行状态的前提下升级/迁移系统"
                                      │
        ┌─────────────────┬──────────┴───────────┬───────────────────┐
        │                 │                      │                   │
   虚拟机热迁移        CRIU (用户态checkpoint)   传统 kexec reboot     LUO + KHO
   (live migration)    /restore                 (冷重启,清空一切)     (本文主角)
        │                 │                      │                   │
   迁移整个 VM 的     冻结整个进程树状态到      仅替换内核镜像,        替换内核镜像的同时
   内存+设备状态      磁盘/内存,在新进程        所有用户态状态/        保留指定资源
   到另一台物理机     里恢复执行                设备状态全部丢失       (内存、文件、设备)
        │                 │                      │                   │
   依赖网络/共享存储   依赖文件系统/磁盘,        无状态保留能力        本机完成,
   开销大,耗时长(秒~分钟) 对设备直通(VFIO)支持差  但速度最快           接近"热重启"的速度
```

LUO 想要解决的问题恰好落在传统 kexec reboot 与虚拟机热迁移之间的空白地带：

- 比起**虚拟机热迁移**：LUO 不需要第二台物理机、不需要把几十 GB 的内存通过网络搬运，
  停机时间被压缩到"kexec 本身的耗时"（通常是秒级，而非分钟级）；
- 比起**CRIU**：LUO 不依赖把状态序列化到磁盘再反序列化（这对于带 VFIO 直通设备、
  巨页内存的虚拟机几乎不可行），而是直接通过物理内存"原地"交给下一个内核；
- 比起**传统 kexec reboot**：LUO 让"刷新整个内核"这件事变得可以被业务接受——
  因为虚拟机的内存、直通设备、网络连接等关键状态可以被保留下来。

### 0.3 典型使用场景

`luo_core.c:17-30` 中给出了官方设想的两类场景：

1. **云 hypervisor 场景**（驱动这项工作的主要动机）：宿主机内核需要打补丁/升级，
   但宿主机上跑着大量客户的虚拟机。传统做法要么"实时迁移"全部 VM 走，要么"冷重启"
   导致所有 VM 宕机。LUO 让宿主机能够"原地"完成内核升级，VM 的内存、vCPU 状态、
   直通设备可以经过 kexec 之后继续运行（这正是第 13、14 章要展开的目标）。

2. **非虚拟化场景**：例如跑着几十 GB 缓存数据的 memcached。服务把缓存数据放进一个
   `memfd`，通过 LUO 保存，kexec 重启后立刻取回——避免了"重启=缓存全部清空，
   下游数据库被打爆"的连锁反应。

> LUO 的设计目标是 **workload-agnostic**（与具体工作负载无关）：无论是虚拟机、
> 容器、数据库还是网络服务，只要它依赖的内核资源类型注册了 LUO handler，
> 就能借助这套框架实现"内核完整升级 + 关键状态保留"。

### 0.4 本文的分析路线

```text
第 0 章  定位 LUO（你正在读）
   │
第 1 章  动机与设计目标 —— 为什么传统方案不够用,LUO 想成为什么
   │
第 2 章  地基:KHO —— LUO 完全建立在 KHO 之上,不懂 KHO 就看不懂 LUO 的每一行代码
   │
第 3 章  整体架构总览 —— Session/File Handler/FLB 三层模型,模块地图,启动时序
   │
第 4 章  用户态接口 —— /dev/liveupdate 的 ioctl 完整走读
   │
第 5~7 章 核心子系统源码精读 —— Session、File、FLB 三大模块逐函数走读(本文主体)
   │
第 8 章  序列化 ABI —— FDT 树结构、内存布局、版本兼容策略
   │
第 9 章  memfd 范例 —— 唯一已落地的 file handler,逐函数精读"保存到恢复"全过程
   │
第 10 章 端到端范例 —— selftest 逐行解读,把上面几章串成一条完整的时间线
   │
第 11 章 错误处理 —— 汇总所有失败分支与"宁可泄漏不做不安全 undo"的设计哲学
   │
第 12~14 章 VFIO 与 QEMU —— 现状差距分析、设计草案、热升级完整方案(应用与展望)
```

---

## 第 1 章 LUO 解决的问题：动机与设计目标

### 1.1 内核升级的根本性矛盻

内核升级一直是运维领域最让人头疼的操作之一，原因在于它和"保持业务连续性"天然冲突：

```text
                        内核升级的两难
        ┌─────────────────────┬─────────────────────┐
        │                     │                     │
   方案 A:重启升级          方案 B:不升级           方案 C:实时迁移走
        │                     │                     │
   业务全部中断           安全漏洞、性能问题、     需要冗余资源,
   (秒级到分钟级)         新硬件支持缺失长期        网络/存储开销,
                          得不到解决                耗时且有失败风险
```

在云计算/虚拟化场景下，这个矛盾被进一步放大：宿主机内核需要频繁更新（安全补丁、
性能优化、新硬件驱动），但宿主机上可能同时运行着成百上千个客户的虚拟机。任何一种
传统方案都会带来巨大的运维代价：

- **重启升级**：所有虚拟机瞬间下线，对 SLA 是灾难性的；
- **不升级**：技术债务持续累积，安全风险持续暴露；
- **实时迁移走**：需要为"腾笼换鸟"预留大量冗余容量，迁移期间网络带宽被大量占用，
  迁移耗时与虚拟机内存大小成正比（数十 GB 内存的虚拟机可能需要几分钟），
  且迁移过程本身可能因为网络抖动、目标机资源不足等原因失败。

### 1.2 kexec 本身解决了"快"，但丢失了"状态"

`kexec` 系统调用允许 Linux 在不经过 BIOS/UEFI 固件重新初始化硬件的情况下，
直接从一个内核镜像跳转到另一个内核镜像，因此重启速度可以做到非常快（秒级）。
但传统 kexec 的代价是**所有运行时状态全部丢失**——内存被清空、设备被复位、
文件描述符全部失效。这正是 LUO 试图打破的最后一道墙：

> **如果 kexec 的速度可以保留，但又能选择性地保留关键状态，那么内核升级就能
> 接近"热重启"的体验。**

这正是 LUO 的核心价值主张。`luo_core.c:11-15`：

```text
Live Update is a specialized, kexec-based reboot process that allows a
running kernel to be updated from one version to another while preserving
the state of selected resources and keeping designated hardware devices
operational. For these devices, DMA activity may continue throughout the
kernel transition.
```

注意这里的措辞——"DMA activity may continue throughout the kernel transition"，
这意味着 LUO 的终极目标不只是"保留内存内容"，而是要做到**直通设备在 kexec 期间
不被复位、DMA 不中断**。这是一个相当激进的目标（我们会在第 12、13 章详细讨论
它对 VFIO 子系统提出的挑战）。

### 1.3 设计目标与非目标

通读 `luo_core.c`、`luo_session.c`、`luo_file.c`、`luo_flb.c` 的文档注释，
可以总结出 LUO 的设计目标（Goals）与非目标（Non-Goals）：

**设计目标**：

| 目标 | 体现 |
| --- | --- |
| **选择性保存**：只保存用户态明确声明要保留的资源，而非"全量快照" | 用户态通过 ioctl 逐个 `PRESERVE_FD`，而不是自动扫描整个进程 |
| **可扩展的 handler 模型**：核心不关心具体文件类型如何序列化 | `liveupdate_file_handler`/`liveupdate_file_ops` 回调接口，参见 `liveupdate.h:73-113` |
| **共享状态去重**：多个文件可能依赖同一份全局状态（如 IOMMU 页表），只保存一次 | FLB（File-Lifecycle-Bound）机制，见第 7 章 |
| **原子性与可回滚**：升级中途失败要能回到运行状态，而不是把系统拖入"半成品"状态 | freeze/unfreeze、preserve/unpreserve 成对设计，见第 6、11 章 |
| **可验证的 ABI**：序列化数据要有版本号和兼容性检查机制，防止新旧内核互相误读 | `compatible` 字符串 + 版本号递增策略，见第 8 章 |
| **单一控制入口**：避免多个用户态代理互相打架 | `/dev/liveupdate` 单例设备模型，见第 3、4 章 |

**非目标**（即 LUO 不试图解决的问题）：

- **透明的进程迁移**：LUO 不像 CRIU 那样试图完整保存/恢复一个进程的全部状态
  （寄存器、打开的所有 fd、信号处理器等）。它只保存**用户态显式声明**的资源。
- **跨主机迁移**：LUO 的所有操作都在本机完成（同一台物理机的 kexec 重启），
  不涉及网络传输。
- **自动化决策**："要不要升级、什么时候升级、保存哪些资源" 完全由用户态代理决定，
  LUO 只负责执行编排，不做决策。

### 1.4 为什么需要一个"编排器"，而不是让各子系统各自为战

一个自然的问题是：既然 KHO 已经提供了"跨 kexec 保存内存"的能力，为什么还需要 LUO
这一层？直接让 memfd、VFIO 等子系统各自调用 KHO API 不就行了？

答案在于**编排的复杂性**——当多个子系统、多个文件、多个共享全局对象都需要在同一次
kexec 中协同保存/恢复时，会涌现出大量需要统一处理的问题：

1. **时序问题**：谁先保存、谁后保存？保存失败时谁负责回滚谁？
   `luo_file.c` 中的 preserve→freeze→retrieve→finish 状态机（第 6 章）正是为
   此而生。
2. **依赖管理**：多个 VFIO 设备文件可能共享同一个 IOMMU 状态，应该保存一次还是
   多次？谁先保存、谁后释放？这正是 FLB 机制（第 7 章）要解决的引用计数问题。
3. **命名与查找**：用户态如何在新内核里找回"我之前保存的那个资源"？
   Session + Token 的两级命名体系（第 5、6 章）解决了这个问题。
4. **版本兼容**：万一保存时是内核 A，恢复时是内核 B（版本不同），怎么知道
   序列化格式是否兼容？`compatible` 字符串机制（第 8 章）解决了这个问题。
5. **统一的用户态入口**：如果每个子系统都开放自己的 ioctl 接口来声明"保存我"，
   用户态代理需要逐个适配；统一到 `/dev/liveupdate` 之后，用户态只需要学习
   一套接口语义（第 3、4 章）。

LUO 的核心价值正是把这些"编排级"的复杂性集中到一处实现、一处测试、一处文档化，
让具体的资源类型 handler（memfd、未来的 VFIO 等）只需要专注于"如何序列化/
反序列化我自己的状态"这一件事。

---

## 第 2 章 地基：KHO（Kexec HandOver）机制详解

理解 LUO 之前，必须先理解它的地基——KHO。`luo_core.c:37-38` 直接点明了这层依赖：

```text
LUO uses Kexec Handover to transfer memory state from the current kernel to
the next kernel. For more details see Documentation/core-api/kho/index.rst.
```

事实上，翻开 `kernel/liveupdate/` 目录会发现一个有趣的事实：KHO 的实现代码
`kexec_handover.c`（1774 行）就直接放在 `kernel/liveupdate/` 目录下，与 LUO
的源文件相邻——这并非偶然，而是因为 **KHO 当前最主要、最复杂的消费者就是 LUO**。
不理解 KHO 的内存保存/FDT 组织方式，就无法理解 LUO 为什么把元数据这样组织、
为什么要用 `kho_alloc_preserve`/`kho_preserve_folio` 这些 API、为什么需要
"scratch 内存"这个概念。

### 2.1 KHO 要解决的问题

`Documentation/core-api/kho/index.rst:12-17`：

```text
Kexec HandOver (KHO) is a mechanism that allows Linux to preserve memory
regions, which could contain serialized system states, across kexec.

KHO uses flattened device tree (FDT) to pass information about the
preserved state from pre-exec kernel to post-kexec kernel and scratch
memory regions to ensure integrity of the preserved memory.
```

也就是说 KHO 提供了两个基础能力：

1. **跨 kexec 保存物理内存**：告诉新内核"这块物理内存的内容不要动，新内核启动后
   把它认领过去"；
2. **跨 kexec 传递元数据**：通过一棵 FDT（Flattened Device Tree，与设备树固件接口
   使用同一种二进制格式）告诉新内核"这些被保存的内存里装的是什么、怎么解释它们"。

### 2.2 FDT：传递元数据的"信封"

KHO FDT 是整个机制的核心枢纽。`index.rst:21-36`：

```text
Every KHO kexec carries a KHO specific flattened device tree (FDT) blob that
describes the preserved state. The FDT includes properties describing
preserved memory regions and nodes that hold subsystem specific state.

The preserved memory regions contain either serialized subsystem states, or
in-memory data that shall not be touched across kexec. After KHO,
subsystems can retrieve and restore the preserved state from KHO FDT.

Subsystems participating in KHO can define their own format for state
serialization and preservation.

KHO FDT and structures defined by the subsystems form an ABI between
pre-kexec and post-kexec kernels. This ABI is defined by header files in
include/linux/kho/abi directory.
```

直觉上可以把 KHO FDT 想象成一份"行李清单"：旧内核在重启之前，把"我保留了哪些
内存区域、每个区域里装的是什么格式的数据、由哪个子系统负责解释"这些信息，
按照树形结构写进一棵 FDT，再把这棵树自身的物理地址也通过某种方式传给新内核。
新内核启动后，第一件事就是读取这份"行李清单"，按图索骥地找到每一块被保留的内存，
调用对应子系统的恢复逻辑。

LUO 正是这样一个"在 KHO 根 FDT 下挂一棵子树"的子系统——它的子树名为 `"LUO"`
（`LUO_FDT_KHO_ENTRY_NAME`，见 `include/linux/kho/abi/luo.h:116`），下面又分出
`luo-session`、`luo-flb` 等子节点。第 8 章会展开讲解这棵树的具体结构。

### 2.3 Scratch 内存：保证"干净的着陆点"

一个容易被忽视，但对理解 KHO 整体设计极其关键的概念是 **scratch 内存**。
`index.rst:48-65`：

```text
To boot into kexec, we need to have a physically contiguous memory range
that contains no handed over memory. Kexec then places the target kernel
and initrd into that region. The new kernel exclusively uses this region
for memory allocations before during boot up to the initialization of the
page allocator.

We guarantee that we always have such regions through the scratch regions:
On first boot KHO allocates several physically contiguous memory regions.
... By default, size of the scratch region is calculated based on amount
of memory allocated during boot. ... The scratch regions are declared as
CMA when page allocator is initialized so that their memory can be used
during system lifetime. CMA gives us the guarantee that no handover pages
land in that region, because handover pages must be at a static physical
memory location and CMA enforces that only movable pages can be located
inside.
```

这段话回答了一个看起来很基础、但很容易被忽略的问题：**新内核刚启动时，
连页分配器都还没初始化，怎么知道哪些物理内存"已经被占用、不能碰"？**

KHO 的答案是反过来想——**预留一块"绝对干净"的区域**，新内核启动早期所有的内存分配
（包括把自己的镜像和 initrd 放到哪里）都只在这块区域里发生，从而保证不会踩到
任何被保留下来的内存。这块区域就是 scratch region。

更巧妙的是，KHO 利用了 **CMA（Contiguous Memory Allocator）的一个副作用**：
CMA 区域只允许放置"可迁移"（movable）页面，而被 KHO 保留下来准备移交给新内核的页面
天然是"不可迁移、必须固定在原物理地址"的（否则物理地址就对不上了），因此 CMA 的约束
天然保证了 scratch 区域里不会混入任何被保留页面——这是一个用现有机制（CMA）的限制
来达成新需求（隔离 scratch 区域）的漂亮设计。

这个机制还支持"递归 KHO"：新内核启动后会复用同一块 scratch 区域（而非重新划分），
从而可以无限次地连续执行 kexec + KHO（每次升级再升级再升级），不需要每次都重新
计算 scratch 区域大小。

> **对理解 LUO 的意义**：当你看到 LUO 中大量调用 `kho_preserve_folio`、
> `kho_alloc_preserve` 时要意识到——这些调用背后隐含着"这块内存绝对不会落在
> scratch 区域里"的保证（`kho_preserve_folio` 内部会用
> `kho_scratch_overlap()` 检查并拒绝，见 `kexec_handover.c:824`），
> 这正是 scratch 机制在底层默默发挥作用。

### 2.4 KHO 公开 API 速览：LUO 真正用到的那些函数

KHO 暴露了一组"保存/反保存/恢复"的对称 API 族。下表汇总了 LUO（及其下属
file handler，如 memfd）所使用的核心函数，并标注它们各自的语义角色：

| API | 角色 | 源码位置 | 语义 |
| --- | --- | --- | --- |
| `kho_is_enabled()` | 查询 | `kexec_handover.c:60-64` | KHO 总开关是否打开 |
| `kho_preserve_folio(folio)` | 保存 | `kexec_handover.c:818-829` | 把一个 folio（一组连续物理页）登记到 KHO 的基数树中，告诉它"别动这块内存" |
| `kho_unpreserve_folio(folio)` | 撤销保存 | `kexec_handover.c:839-847` | 撤销上面的登记（kexec 之前反悔了） |
| `kho_preserve_pages(page, nr)` | 保存 | `kexec_handover.c:890-921` | 按 0 阶（单页）粒度保存一段连续物理页（内部会自动按 NUMA 边界切分为多个 order） |
| `kho_alloc_preserve(size)` | 分配 + 保存 | `kexec_handover.c:1211-1235` | "二合一"便捷函数：分配一块清零的物理连续内存并立即登记保存，返回内核虚拟地址 |
| `kho_unpreserve_free(mem)` | 撤销 + 释放 | `kexec_handover.c:1245-1256` | `kho_alloc_preserve` 的逆操作：撤销保存并把内存还给伙伴系统 |
| `kho_restore_folio(phys)` | 恢复 | `kexec_handover.c:419-431` | 在新内核中，根据物理地址重新构造出对应的 `struct folio *`（"认领"这块内存） |
| `kho_restore_free(mem)` | 恢复 + 释放 | `kexec_handover.c:1270-1281` | "二合一"便捷函数：在新内核中认领一块由 `kho_alloc_preserve` 分配的内存，并立刻把它释放回伙伴系统（典型用法：恢复出元数据、读完即释放） |
| `kho_preserve_vmalloc(ptr, &kv)` | 保存 | `kexec_handover.c:1024-1075` | 保存一段 `vmalloc()` 分配出来的、虚拟地址连续但物理地址不连续的内存区域 |
| `kho_unpreserve_vmalloc(&kv)` | 撤销保存 | `kexec_handover.c:1084-1097` | 上面的逆操作 |
| `kho_restore_vmalloc(&kv)` | 恢复 | `kexec_handover.c:1109-1195` | 在新内核中重建出一段等价的 vmalloc 区域并填充原内容 |
| `kho_add_subtree(name, blob, size)` | 注册子树 | `kexec_handover.c:730-774` | 在 KHO 根 FDT 下挂一个子节点，记录某个序列化 blob 的物理地址和大小 |
| `kho_retrieve_subtree(name, &phys, &size)` | 查询子树 | `kexec_handover.c:1330-1364` | 在新内核中按名字找回上面注册的子树信息 |

下面对其中几个关键函数做更深入的逐句解读。

#### 2.4.1 `kho_preserve_folio` —— 最基础的"别动这块内存"声明

```c
// kernel/liveupdate/kexec_handover.c:818-829
int kho_preserve_folio(struct folio *folio)
{
	struct kho_radix_tree *tree = &kho_out.radix_tree;
	const unsigned long pfn = folio_pfn(folio);
	const unsigned int order = folio_order(folio);

	if (WARN_ON(kho_scratch_overlap(pfn << PAGE_SHIFT, PAGE_SIZE << order)))
		return -EINVAL;

	return kho_radix_add_page(tree, pfn, order);
}
```

这个函数的实现极其简洁——它本质上只是把 `(pfn, order)` 这一对信息塞进一棵全局的
基数树（radix tree）。但简洁的背后有两个值得注意的细节：

1. **`kho_scratch_overlap` 检查**：在登记之前先校验这块内存有没有落在 scratch 区域里。
   如果落在里面，说明调用者传入了一块"本应该是临时区域"的内存，这是一个编程错误
   （会触发 `WARN_ON`），因为 scratch 区域的内容在 kexec 后会被新内核任意覆写，
   "保存"一块即将被覆写的内存毫无意义。
2. **以 folio（而非单个 page）为粒度**：folio 天然携带阶数（order）信息，
   一次调用就能登记一段 `2^order` 个连续页，这与现代内存管理用 folio 取代
   裸 page 的趋势一致，也减少了基数树的节点数量（登记粒度越大，树越浅）。

#### 2.4.2 `kho_alloc_preserve` / `kho_unpreserve_free` / `kho_restore_free` —— 元数据传递的标准三件套

LUO 中绝大多数"序列化元数据"的传递都遵循同一种模式——分配一块物理连续、清零的内存，
把结构化数据写进去，然后通过 FDT 告诉新内核它的物理地址。`kho_alloc_preserve`
正是为这种模式量身定制的"二合一"便捷函数：

```c
// kernel/liveupdate/kexec_handover.c:1211-1235
void *kho_alloc_preserve(size_t size)
{
	struct folio *folio;
	int order, ret;

	if (!size)
		return ERR_PTR(-EINVAL);

	order = get_order(size);
	if (order > MAX_PAGE_ORDER)
		return ERR_PTR(-E2BIG);

	folio = folio_alloc(GFP_KERNEL | __GFP_ZERO, order);
	if (!folio)
		return ERR_PTR(-ENOMEM);

	ret = kho_preserve_folio(folio);
	if (ret) {
		folio_put(folio);
		return ERR_PTR(ret);
	}

	return folio_address(folio);
}
```

注意它的实现路径：`folio_alloc(..., __GFP_ZERO)` → `kho_preserve_folio()`。
这意味着调用者拿到的内存**已经是清零的、且已经登记到 KHO**——一步到位，
不需要分两步调用、也不用担心"分配成功但忘记登记"导致的不一致状态。这正是
LUO 中创建各种 `_ser`（序列化）结构体的标准开局动作，例如：

- `luo_session_setup_outgoing()` 用它分配 `luo_session_header_ser` 数组
  （`luo_session.c:447`）；
- `luo_alloc_files_mem()` 用它分配 `luo_file_ser` 数组（`luo_file.c:188`）；
- `luo_flb_setup_outgoing()` 用它分配 `luo_flb_header_ser` 数组（`luo_flb.c:561`）；
- `luo_fdt_setup()` 用它分配 LUO 自己的 FDT 树本身（`luo_core.c:163`）；
- `memfd_luo_preserve()` 用它分配 `memfd_luo_ser`（`mm/memfd_luo.c:270`）。

而它的逆操作有两个版本，分别对应"中途反悔"和"在新内核里读完就扔"两种场景：

```c
// kernel/liveupdate/kexec_handover.c:1245-1256（旧内核中途取消）
void kho_unpreserve_free(void *mem)
{
	struct folio *folio;
	if (!mem)
		return;
	folio = virt_to_folio(mem);
	kho_unpreserve_folio(folio);
	folio_put(folio);
}

// kernel/liveupdate/kexec_handover.c:1270-1281（新内核中"认领并释放"）
void kho_restore_free(void *mem)
{
	struct folio *folio;
	if (!mem)
		return;
	folio = kho_restore_folio(__pa(mem));
	if (!WARN_ON(!folio))
		folio_put(folio);
}
```

二者的区别本质上是"在哪个内核里调用、内存当前处于什么状态"：

- `kho_unpreserve_free`：还在**旧内核**里，内存仍然由旧内核的伙伴系统管理，
  只是被 KHO 标记为"将要保留"。撤销保存就是把标记抹掉，再正常释放。
- `kho_restore_free`：已经到了**新内核**，内存的物理地址有效但新内核的伙伴系统
  还不认识它（它属于 KHO 的"已保存但未认领"状态）。`kho_restore_folio` 的作用
  就是把它"认领"过来、变成新内核伙伴系统认识的正常 folio，然后立刻释放掉——
  典型场景就是"我只是想读一下里面记录的元数据，读完这块内存就没用了"。

这正是 `luo_session_deserialize()` 用 `kho_restore_free(sh->header_ser)`
（`luo_session.c:574`）和 `luo_file_finish()` 用 `kho_restore_free(file_set->files)`
（`luo_file.c:748`）的场景——元数据一旦被读取并转换成内核内部的链表/对象表示，
原始的序列化内存块就可以释放掉了。

#### 2.4.3 `kho_preserve_vmalloc` / `kho_restore_vmalloc` —— 跨越 vmalloc 的"虚拟地址连续、物理地址不连续"鸿沟

并非所有需要保存的数据都是物理连续的。比如 memfd 需要保存一个长度可变、
可能非常大的 folio 描述符数组（见第 9 章），用 `vmalloc()` 分配显然更合适。
但 `vmalloc()` 分配出的内存在虚拟地址上连续，物理地址却很可能是**离散的多段**——
这就需要专门的保存/恢复逻辑。

KHO 用一个"链表 of 物理地址数组"的结构来描述这种内存区域：

```c
// include/linux/kho/abi/kexec_handover.h（节选自 abi.rst 引用的文档结构）
struct kho_vmalloc_chunk {
	struct kho_vmalloc_hdr hdr;     /* 含指向下一个 chunk 的(可序列化)指针 */
	u64 phys[KHO_VMALLOC_SIZE];     /* 一页能装下的物理地址数组 */
};

struct kho_vmalloc {
	DECLARE_KHOSER_PTR(first, struct kho_vmalloc_chunk *);
	unsigned int total_pages;
	unsigned short flags;
	unsigned short order;
};
```

整个描述结构本质上是一条**单页大小的链表**——每个 `kho_vmalloc_chunk` 恰好
占一页（`static_assert(sizeof(struct kho_vmalloc_chunk) == PAGE_SIZE)`），
里面除了链表指针外，剩下的空间塞满物理地址；如果一页装不下整段 vmalloc 区域
对应的物理页地址，就再分配一个 chunk 串起来。`DECLARE_KHOSER_PTR` 宏
（`kexec_handover.h:121-125`）把指针包装成一个 union，既可以当成内核虚拟地址
使用（旧内核），又可以序列化为物理地址传给新内核（新内核据此 `phys_to_virt`
还原指针）——这是一种典型的"自描述指针"技巧，专门用来解决"指针在不同内核间
传递时虚拟地址会失效，但物理地址恒定"的问题。

`kho_preserve_vmalloc()` 的实现路径是：

```c
// kernel/liveupdate/kexec_handover.c:1024-1075（节选）
int kho_preserve_vmalloc(void *ptr, struct kho_vmalloc *preservation)
{
	struct vm_struct *vm = find_vm_area(ptr);
	...
	chunk = new_vmalloc_chunk(NULL);
	KHOSER_STORE_PTR(preservation->first, chunk);

	nr_contig_pages = (1 << order);
	for (int i = 0; i < vm->nr_pages; i += nr_contig_pages) {
		phys_addr_t phys = page_to_phys(vm->pages[i]);
		err = kho_preserve_pages(vm->pages[i], nr_contig_pages);
		...
		chunk->phys[idx++] = phys;
		if (idx == ARRAY_SIZE(chunk->phys)) {
			chunk = new_vmalloc_chunk(chunk);  /* 满了就接龙 */
			idx = 0;
		}
	}
	preservation->total_pages = vm->nr_pages;
	preservation->flags = flags;
	preservation->order = order;
	return 0;
}
```

可以看到它做了三件事：①找到 vmalloc 区域背后的 `vm_struct`（拿到页面数组）；
②按"连续物理页段"为单位调用 `kho_preserve_pages` 逐段登记（因为 vmalloc 内部
可能本身就用了高阶页面）；③把每一段的起始物理地址记录进 chunk 链表。
`kho_restore_vmalloc()` 则反向操作：按 chunk 链表读出物理地址列表 →
`kho_restore_pages` 逐段认领 → 用 `__get_vm_area_node` + `vmap_pages_range`
在新内核里重新建立一段 vmalloc 映射，让数据"原地"可见——不需要发生任何拷贝。

> **对理解 LUO 的意义**：第 9 章会看到，memfd 用它来传递一个长度可能高达
> 数十万项的 `struct memfd_luo_folio_ser` 数组（每个文件页对应一项）。
> 这个数组的大小取决于文件大小，无法用 `kho_alloc_preserve` 这种"固定大小、
> 物理连续分配"的方式优雅处理，`kho_preserve_vmalloc` 正是为这种"大小可变、
> 体量可能很大"的场景而设计的。

### 2.5 KHO 的两段式生命周期：outgoing 与 incoming

通读 KHO 与 LUO 的代码会发现一个反复出现的模式——几乎每个数据结构、每个全局状态
都被分成 **outgoing**（即将传给下一个内核）和 **incoming**（从上一个内核接收到）
两份。这不是偶然的代码风格，而是对"kexec 是单向、不可逆的状态切换"这一本质的
精确建模：

```text
   旧内核(kernel A)的视角                    新内核(kernel B)的视角
  ┌─────────────────────┐                  ┌─────────────────────┐
  │   outgoing 数据       │ ===kexec===>   │   incoming 数据       │
  │ (我要交给下一个内核的)  │   物理内存搬运    │ (上一个内核交给我的)   │
  └─────────────────────┘   (KHO/FDT)      └─────────────────────┘
            │                                        │
   luo_session_serialize()                luo_session_deserialize()
   luo_flb_serialize()                    luo_flb_setup_incoming()
   luo_*_setup_outgoing()                          ...
```

**关键洞察**：在重启之后，新内核既是"刚接收完上一代数据的接收方"，又同时是
"将要给下下一代内核准备数据的发送方"——因此一个运行中的内核会**同时持有**
incoming 和 outgoing 两份状态。`luo_session_global`（`luo_session.c:104-118`）、
`luo_flb_global`（`luo_flb.c:66-75`）都明确地用 `incoming`/`outgoing` 两个字段
分别管理这两份状态，这正是为了支持"无限次连续 live update"——每次 kexec 之后，
旧的 incoming 数据被消费完毕、转化为新内核运行时的活跃状态，而与此同时新内核
又在悄悄准备着自己的 outgoing 数据，为下一次 live update 做准备。

理解了这一点，再去看第 5、7 章中 `luo_session_global`/`luo_flb_global`
结构体的字段命名，就会豁然开朗。

---

## 第 3 章 LUO 整体架构总览

本章把视角从"地基"（KHO）抬高到 LUO 自身，给出一张可以指导后续阅读的整体地图：
LUO 的三层模型如何分工、源码文件如何映射到这三层、内核启动时各个初始化函数的
调用顺序，以及贯穿全文的核心交互入口 `/dev/liveupdate` 的设备模型设计。

### 3.1 三层模型：Session / File Handler / FLB

读 LUO 代码最容易迷失的地方在于：它同时存在三种"看似相似又互不相同"的核心抽象——
**Session（会话）**、**File Handler（文件处理器）**、**FLB（File-Lifecycle-Bound
object，文件生命周期绑定对象）**。下表先把它们的职责边界划清楚：

| 维度 | Session（会话） | File Handler（文件处理器） | FLB（文件生命周期绑定对象） |
|---|---|---|---|
| 代表什么 | 用户态的一个"保存任务分组"，类似于一个目录/命名空间 | 某一类文件（如 memfd、未来的 vfio 设备 fd）的序列化/反序列化回调集合 | 与某些文件类型相关、但**不属于任何单个文件**的全局共享内核状态（如 IOMMU 页表根、KVM 的 VM 拓扑） |
| 由谁创建 | 用户态通过 `ioctl(LIVEUPDATE_IOCTL_CREATE_SESSION)` 主动创建 | 内核子系统（如 `mm/memfd_luo.c`）通过 `liveupdate_register_file_handler()` 在模块初始化时注册 | 内核子系统通过 `liveupdate_register_flb()` 注册 |
| 数量级 | 用户态可创建多个，上限 `LUO_SESSION_MAX`（约 744 个，见 `luo_session.c:53`） | 由源码静态注册，数量很少（目前只有一个：memfd） | 由源码静态注册，数量很少 |
| 生命周期触发者 | 用户态显式调用 `preserve_fd`/`retrieve_fd`/`finish` 驱动 | 被 Session 在保存/恢复某个 fd 时间接调用（通过 `compatible` 字符串匹配出对应 handler） | 被"该类型的第一个文件被保存"间接触发保存，被"该类型的最后一个文件 finish"间接触发清理（引用计数模型，见第 7 章） |
| 对应源文件 | `kernel/liveupdate/luo_session.c` | `kernel/liveupdate/luo_file.c`（管理框架）+ 各子系统自己的 `*_luo.c`（如 `mm/memfd_luo.c`，提供具体回调实现） | `kernel/liveupdate/luo_flb.c`（管理框架）+ 各子系统自己注册的 FLB 实例（当前内核树中尚无落地实例，是预留扩展点） |
| 一句话比喻 | "文件夹" —— 用户态划分保存任务的容器 | "文件类型插件" —— 告诉内核"这一类文件该怎么序列化/反序列化" | "全局共享设施" —— 多个文件共同依赖、需要被恰好保存一次/恢复一次的后台基础设施 |

三者的关系可以用下面的依赖图概括：

```text
                         用户态 (userspace)
                               │
                               │ ioctl(/dev/liveupdate)
                               ▼
                    ┌─────────────────────┐
                    │   luo_session (N个)   │  ← 用户可见、可命名、可创建/检索
                    │  "我的保存任务分组"     │
                    └──────────┬──────────┘
                               │ 内部持有
                               ▼
                    ┌─────────────────────┐
                    │  luo_file_set (每个   │
                    │  session 一个，内含    │  ← 该 session 下被 preserve 的文件列表
                    │  最多 LUO_FILE_MAX 个 │
                    │  luo_file 节点)       │
                    └──────────┬──────────┘
                               │ 每个 luo_file 通过 compatible
                               │ 字符串匹配到对应的 handler
                               ▼
                    ┌─────────────────────┐
                    │ liveupdate_file_     │  ← 内核静态注册，定义"如何序列化
                    │ handler (memfd 等)   │     /反序列化这一类文件"
                    └──────────┬──────────┘
                               │ handler 在注册时声明自己
                               │ 依赖哪些 FLB（flb_list）
                               ▼
                    ┌─────────────────────┐
                    │   liveupdate_flb     │  ← 内核静态注册，全局共享状态，
                    │  (IOMMU/KVM 等，预留) │     引用计数式生命周期管理
                    └─────────────────────┘
```

需要特别强调三点，它们是初学者最容易搞混的地方：

1. **Session 是"分组容器"，不是"被保存的对象"**。真正携带数据的是 Session 内部
   `luo_file_set` 链表上的 `luo_file` 节点；Session 本身序列化后只保存一个名字
   字符串和文件列表的元数据（见 `struct luo_session_ser`，`include/linux/kho/abi/luo.h:171-174`）。
   也正因如此，第 5 章会看到"空 session"（没有任何文件）也是合法且能正确跨 kexec
   存活的——这正是 `luo_multi_session.c` 选择把 `SESSION_EMPTY_1`/`SESSION_EMPTY_2`
   作为测试用例的原因（验证容器本身的序列化路径，与其内容物解耦）。

2. **File Handler 是"类型描述符"，不是"文件实例"**。一个 handler（比如 memfd 的）
   在内核启动时只注册一次（`mm/memfd_luo.c` 的 `late_initcall(memfd_luo_init)`），
   之后无论用户创建多少个 session、preserve 多少个 memfd，都复用同一个 handler 的
   回调集合。`compatible` 字符串（如 `"memfd-v2"`）就是连接"序列化时落盘的元数据"
   与"恢复时应该调用哪个 handler"的纽带——这也是为什么序列化结构体 `luo_file_ser`
   （`include/linux/kho/abi/luo.h:66-70`）的第一个字段就是 `compatible[48]`。

3. **FLB 解决的是"N 个文件共享 1 份全局状态"的问题，是 LUO 框架里"看起来最不起眼，
   实际上对 VFIO/KVM 这类复杂子系统最关键"的设计**。设想一下：如果有 8 个 VFIO
   设备 fd 同时被 preserve，它们背后的 IOMMU 页表根、中断重映射表显然只应该被
   保存一次、恢复一次，而不是 8 次——FLB 用引用计数把这种"懒创建、共享、引用计数式
   销毁"的模式标准化了。第 7 章会看到 `luo_flb_file_preserve_one()` 中那个
   `count == 0` 的判断正是这个机制的核心。当前内核树里 FLB 还没有任何落地的
   注册者（`liveupdate_register_flb()` 目前没有调用方），但框架已经为 VFIO/KVM/
   IOMMU 等"重量级、跨文件共享状态"的子系统预留好了入口——这是第 13 章设计草案的
   立足点。

### 3.2 模块文件地图

下表把 LUO 相关的源码文件按照"层次"归类，建立一份可在后续章节中按图索骥的地图：

| 文件路径 | 行数 | 所属层次 | 一句话职责 |
|---|---|---|---|
| `kernel/liveupdate/Kconfig` | 92 | 配置 | 定义 `KEXEC_HANDOVER`/`LIVEUPDATE`/`LIVEUPDATE_MEMFD` 等 Kconfig 选项及其依赖关系 |
| `kernel/liveupdate/kexec_handover.c` | 1774 | 地基（KHO） | FDT 树管理、scratch 内存、`kho_preserve_*`/`kho_restore_*` API 族的实现（详见第 2 章） |
| `kernel/liveupdate/kexec_handover_internal.h` | - | 地基（KHO） | KHO 内部头文件，被 `luo_core.c` `#include` 用于访问 KHO 内部细节 |
| `kernel/liveupdate/luo_core.c` | 450 | 总控 + ioctl 入口 | 模块初始化时序、`/dev/liveupdate` 设备与 ioctl 分发、`liveupdate_reboot()` 钩子 |
| `kernel/liveupdate/luo_internal.h` | 118 | 总控（内部头） | `luo_session`/`luo_file_set`/`luo_ucmd` 等内部结构体定义、`luo_restore_fail()` 宏 |
| `kernel/liveupdate/luo_session.c` | 615 | Session 层 | Session 的创建/检索/序列化/反序列化/释放，详见第 5 章 |
| `kernel/liveupdate/luo_file.c` | 927 | File Handler 层（框架） | `luo_file`/`luo_file_set` 管理、preserve/freeze/retrieve/finish 状态机驱动，详见第 6 章 |
| `kernel/liveupdate/luo_flb.c` | 667 | FLB 层（框架） | FLB 注册、引用计数、incoming/outgoing 双状态管理，详见第 7 章 |
| `include/linux/liveupdate.h` | 290 | 跨层公共头 | 对外 API 声明：`liveupdate_file_ops`/`liveupdate_flb_ops`/`liveupdate_register_*` 等 |
| `include/uapi/linux/liveupdate.h` | 217 | 用户态 ABI | ioctl 命令号、`struct liveupdate_ioctl_*`/`liveupdate_session_*` 用户态结构体 |
| `include/linux/kho/abi/luo.h` | 248 | 序列化 ABI | LUO 自身的 FDT 序列化结构体：`luo_session_ser`/`luo_file_ser`/`luo_flb_ser` 等，详见第 8 章 |
| `include/linux/kho/abi/memfd.h` | 94 | 序列化 ABI（memfd 专属） | memfd 的序列化结构体 `memfd_luo_ser`/`memfd_luo_folio_ser`，详见第 8、9 章 |
| `mm/memfd_luo.c` | 623 | File Handler 层（具体实现） | memfd 类型的 `liveupdate_file_ops` 完整实现——目前内核树中**唯一**落地的 file handler，详见第 9 章 |
| `Documentation/core-api/kho/index.rst` | 89 | 文档 | KHO 机制概览（FDT、scratch 内存的设计说明） |
| `Documentation/core-api/liveupdate.rst` | 72 | 文档 | LUO 内核态 API 的 kernel-doc 索引页 |
| `Documentation/userspace-api/liveupdate.rst` | 20 | 文档 | LUO 用户态 ioctl ABI 的 kernel-doc 索引页 |
| `tools/testing/selftests/liveupdate/*` | 共 ~955 | 测试/范例 | 端到端 kselftest，是理解整个调用链最直观的"活文档"，详见第 10 章 |

可以看到一个清晰的分层关系：**地基层（KHO）→ 总控层（luo_core）→ 三个并列子系统层
（session/file/flb）→ 具体实现层（memfd_luo 等）**。这种分层在目录结构上也得到了
体现——`kernel/liveupdate/` 目录下，`kexec_handover.c` 与 `luo_*.c` 共享同一个
编译单元目录，恰好印证了 DOC 注释里"LUO uses Kexec Handover to transfer memory
state"（`luo_core.c:37`）这句话——它们在概念上是"上下楼"的关系，在物理布局上则
干脆是"邻居"。

### 3.3 初始化与启动时序：early_initcall 与 late_initcall 的精巧编排

LUO 的初始化分散在四个 `__init` 函数里，分别挂在 `early_initcall` 和
`late_initcall` 两个不同阶段——这个先后顺序绝非随意，而是处理"鸡生蛋蛋生鸡"
问题的关键设计。完整的启动时序如下图：

```text
内核启动阶段                  函数                         做的事情               为什么是这个时机
─────────────────────────────────────────────────────────────────────────────────
                    ┌─ early_param("liveupdate", ...)
  解析命令行参数      │   luo_core.c:81
                    └─ 设置 luo_global.enabled 初值       必须在所有 initcall 之前完成，
                                                          因为后续所有判断都依赖它

  ══════════════ early_initcall 阶段 ══════════════

                    liveupdate_early_init()               1. 调用 luo_early_startup()
                    luo_core.c:141 ────────┐              2. 失败则 panic（luo_restore_fail）
                                           │
                                           ▼
                    luo_early_startup()                   1. 检查 kho_is_enabled()
                    luo_core.c:83                         2. kho_retrieve_subtree() 取出
                                                             上一个内核传来的 LUO FDT 子树
                                                          3. 校验 compatible 字符串
                                                          4. luo_session_setup_incoming()
                                                          5. luo_flb_setup_incoming()
                                                                                  ↑
                                            必须尽早执行：一旦 KHO 把内存交还给伙伴分配器，
                                            被保存的物理页就可能被错误地复用；早期阶段
                                            "认领"这些页面是数据完整性的第一道防线，
                                            这与 KHO 自身的 early_initcall 时机要求一致

  ══════════════ late_initcall 阶段 ══════════════

                    luo_late_startup()                    1. 检查 liveupdate_enabled()
                    luo_core.c:200 ────────┐              2. 调用 luo_fdt_setup()
                                           │
                                           ▼
                    luo_fdt_setup()                       1. kho_alloc_preserve(LUO_FDT_SIZE)
                    luo_core.c:157                           创建*输出*用的 FDT 内存
                                                          2. fdt_create + fdt_property 系列
                                                             写入 compatible/liveupdate_num
                                                          3. luo_session_setup_outgoing()
                                                          4. luo_flb_setup_outgoing()
                                                          5. kho_add_subtree() 注册到 KHO
                                                                                  ↑
                                            延迟到 late_initcall：输出树只有在用户态开始
                                            使用 /dev/liveupdate 之后才有意义；过早创建
                                            只会浪费 scratch 内存、增加早期启动期间
                                            FDT 损坏的窗口（注释见 luo_core.c:196-199）

                    liveupdate_ioctl_init()               misc_register(&luo_dev.miscdev)
                    luo_core.c:442                        创建 /dev/liveupdate 设备节点
                                                                                  ↑
                                            必须在输出树就绪（luo_late_startup）之后再
                                            开放用户态入口，否则用户态可能在内核还没准备好
                                            接收 preserve 请求时就发起 ioctl
```

这张时序图揭示了一个重要的设计哲学——**"先认领，再创建，最后开放"**：

- **`early_initcall` 阶段**只做一件事：把上一个内核移交过来的数据"认领"回来
  （incoming 路径）。这一步必须越早越好，因为伙伴分配器（buddy allocator）一旦
  完全初始化，被 KHO 标记为"已保留"的物理页可能被提前回收路径误判为可用页——
  与 `kho_restore_folio()` 在 `kernel/liveupdate/kexec_handover.c` 中所处的早期
  调用链遥相呼应。

- **`late_initcall` 阶段**才创建"将要交给下一个内核"的数据结构（outgoing 路径）
  以及面向用户态的入口（`/dev/liveupdate`）。这是因为：(a) outgoing 树在系统真正
  发生 live update 之前都用不上，没有必要在系统启动早期就分配并占用宝贵的 scratch
  内存；(b) 用户态发起任何 `ioctl` 之前，必须确保 `luo_global.fdt_out` 已经就绪——
  否则 `luo_session_create()`（见第 5 章）在内部调用 `kho_alloc_preserve` 往 outgoing
  树里插入节点时就会因为树不存在而失败。

- **错误处理上的不对称**同样值得注意：`liveupdate_early_init()` 一旦失败就直接
  `luo_restore_fail()`（即 `panic()`，见 `luo_internal.h`），而 `luo_late_startup()`
  失败时只是把 `luo_global.enabled` 置为 `false` 并返回错误码，系统仍可继续启动。
  这背后的逻辑是：**incoming 路径承载着"上一个内核留下的、用户已经认为安全落地的
  数据"，一旦反序列化失败就意味着潜在的数据损坏或丢失，宁可让系统停在已知状态
  （panic）也不能让它带着"半失败"的状态继续运行；而 outgoing 路径只是"准备迎接
  未来可能发生的 live update"，失败了大不了这一代内核不支持 live update，并不会
  威胁到已经在跑的业务**。这与第 11 章要展开的"宁可泄漏不做不安全 undo"哲学
  其实是同一种价值取舍的不同表现形式。

### 3.4 `/dev/liveupdate` 单例设备模型

LUO 选择了一种克制而严谨的用户态接口形态——**单例字符设备 + ioctl**，而不是
`sysfs`/`procfs`/`netlink` 等更"现代"但也更难做访问控制的方式。其设计可以归纳为
三个关键点：

**(1) 全局唯一、独占访问。** `struct luo_device_state` 只有一个静态实例 `luo_dev`
（`luo_core.c:433-440`），其中 `atomic_t in_use` 字段在 `luo_open()` 中通过
`atomic_cmpxchg(&ldev->in_use, 0, 1)` 实现"试图把 0 改成 1，如果原值不是 0 就
失败"的经典独占锁模式：

```c
static int luo_open(struct inode *inodep, struct file *filep)
{
        struct luo_device_state *ldev = container_of(filep->private_data,
                                                      struct luo_device_state,
                                                      miscdev);

        if (atomic_cmpxchg(&ldev->in_use, 0, 1))
                return -EBUSY;

        /* Always return -EIO to user if deserialization fail */
        if (luo_session_deserialize()) {
                atomic_set(&ldev->in_use, 0);
                return -EIO;
        }

        return 0;
}
```
（`luo_core.c:337-353`）

这样设计省掉了在内核态维护"多个管理者之间如何协调"的全部复杂性——文档注释里
说得很直白：

> "To ensure that the state machine is controlled by a single entity, access
> to this device is exclusive... This singleton model simplifies state
> management by preventing conflicting commands from multiple userspace agents."
> （`luo_core.c:264-269`）

这其实是把"分布式协调问题"直接化简为"不存在"——内核不需要解决"两个 live update
管理 agent 同时操作怎么办"的难题，因为从设计上就不允许这种情况发生。这是一种
"用约束换简单"的经典系统设计取舍。

**(2) `open()` 即触发反序列化——把"昂贵的一次性操作"和"明确的用户态意图"绑定。**
注意 `luo_open()` 里在抢到锁之后立刻调用了 `luo_session_deserialize()`
（第 5 章详细分析）。这意味着：所有上一个内核遗留下来的 session 数据，要等到
**用户态第一次打开 `/dev/liveupdate`** 时才会被实际反序列化重建。这而不是在
`luo_early_startup()` 里就做完全部反序列化，原因有二：

  - 反序列化是一个相对重量级的操作（要重建 `luo_session`/`luo_file_set`/
    `luo_file` 等一整套对象图），把它推迟到"确实有用户态在等待结果"的时刻，
    避免了在系统可能根本不需要 live update 数据的场景下浪费早期启动时间；
  - 更重要的是，它建立了一个清晰的"语义契约"：**用户态打开设备，就表示它已经
    准备好接管 live update 状态机**。如果反序列化失败，`luo_open()` 会立即把
    `in_use` 复位并返回 `-EIO`（注释 `/* Always return -EIO to user if
    deserialization fail */`，`luo_core.c:346`），用户态据此可以明确判断
    "数据有问题，需要走应急预案"，而不是在某个无关的 ioctl 调用时才意外地
    遇到一个语焉不详的错误码。

**(3) `release()` 只做一件事——释放独占锁。** 对比 `luo_open()` 的"重"，
`luo_release()`（`luo_core.c:355-363`）极其"轻"：仅仅 `atomic_set(&ldev->in_use, 0)`。
这意味着关闭设备**不会**触发任何会话的自动回收——会话的生命周期完全独立于设备
fd 的生命周期，由 `luo_session_release()`（其自身的 fd 被 close 时触发，详见
第 5 章）单独管理。这种解耦是必要的：用户态完全可能"打开 `/dev/liveupdate`，
创建几个 session，然后关闭 `/dev/liveupdate`，但仍然持有 session 的 fd 继续操作"
——这正是 selftest 里 `daemonize_and_wait()`（`luo_test_utils.c:173-203`）所做的事：
关闭 `luo_fd` 之后，子进程仍然持有各个 session fd 穿越 kexec。

**(4) ioctl 分发表：用静态数组 + 编译期检查取代 `switch-case`。** `luo_ioctl()`
没有采用常见的 `switch (cmd) { case XXX: ... }` 写法，而是构建了一张
`luo_ioctl_ops[]` 静态表（`luo_core.c:387-392`），通过 `IOCTL_OP` 宏
（`luo_core.c:377-385`）生成每一项：

```c
#define IOCTL_OP(_ioctl, _fn, _struct, _last)                                  \
        [_IOC_NR(_ioctl) - LIVEUPDATE_CMD_BASE] = {                            \
                .size = sizeof(_struct) +                                      \
                        BUILD_BUG_ON_ZERO(sizeof(union ucmd_buffer) <          \
                                          sizeof(_struct)),                    \
                .min_size = offsetofend(_struct, _last),                       \
                .ioctl_num = _ioctl,                                           \
                .execute = _fn,                                                \
        }
```

这个宏一次性完成了三件事，每一件都对应着一个潜在的 bug 来源：

  - `.size = sizeof(_struct) + BUILD_BUG_ON_ZERO(...)`：用
    `BUILD_BUG_ON_ZERO` 在**编译期**断言 `union ucmd_buffer`（栈上的命令缓冲区，
    `luo_core.c:365-368`）足够容纳这个命令的最大结构体。如果以后有人往
    `liveupdate_ioctl_*` 添加新字段却忘了同步更新 `union ucmd_buffer`，
    编译会直接失败，而不是留下一个栈缓冲区溢出的运行时炸弹。
  - `.min_size = offsetofend(_struct, _last)`：自动计算"这个命令结构体里，
    用户态老版本至少必须传多少字节"——即从结构体开头到 `_last` 字段结束的偏移量。
    这是下面要讲的 `copy_struct_from_user` 扩展性方案里"向后兼容"的关键一环
    （第 4 章详述）。
  - `[_IOC_NR(_ioctl) - LIVEUPDATE_CMD_BASE] = { ... }`：用**指派初始化器**
    （designated initializer）按 ioctl 号直接定位数组下标，这样表项的声明顺序
    与 ioctl 号的数值顺序解耦，同时还天然支持"该 ioctl 号对应的表项不存在"的
    防御性检查——见下面 `luo_ioctl()` 里 `op->ioctl_num != cmd` 那一行。

`luo_ioctl()` 本身的分发逻辑则把"提取用户传入大小 → 校验 → 拷贝 → 执行"四步
拆解得干净利落：

```c
static long luo_ioctl(struct file *filep, unsigned int cmd, unsigned long arg)
{
        const struct luo_ioctl_op *op;
        struct luo_ucmd ucmd = {};
        union ucmd_buffer buf;
        unsigned int nr;
        int err;

        nr = _IOC_NR(cmd);
        if (nr - LIVEUPDATE_CMD_BASE >= ARRAY_SIZE(luo_ioctl_ops))
                return -EINVAL;

        ucmd.ubuffer = (void __user *)arg;
        err = get_user(ucmd.user_size, (u32 __user *)ucmd.ubuffer);
        if (err)
                return err;

        op = &luo_ioctl_ops[nr - LIVEUPDATE_CMD_BASE];
        if (op->ioctl_num != cmd)
                return -ENOIOCTLCMD;
        if (ucmd.user_size < op->min_size)
                return -EINVAL;

        ucmd.cmd = &buf;
        err = copy_struct_from_user(ucmd.cmd, op->size, ucmd.ubuffer,
                                    ucmd.user_size);
        if (err)
                return err;

        return op->execute(&ucmd);
}
```
（`luo_core.c:394-424`）

这里有两个容易被忽略但很关键的细节：

  - **先用 `get_user()` 单独读取 `user_size` 字段**（每个 `liveupdate_ioctl_*`
    结构体的第一个字段必须是 `__u32 size`，参见第 4 章对 ABI 设计规范的分析），
    再决定要拷贝多少字节——这是 `copy_struct_from_user` 模式的标准前奏，目的是
    支持新旧内核与新旧用户态程序的任意组合都能正确互操作（细节见第 4 章 4.3 节）。
  - **`if (op->ioctl_num != cmd) return -ENOIOCTLCMD;`** 是一个"双重校验"：
    数组下标 `nr - LIVEUPDATE_CMD_BASE` 只能保证落在数组范围内，但不能保证这个
    下标对应的表项确实是为这个 `cmd` 准备的（数组里可能存在因为 `IOCTL_OP`
    没有覆盖到某些下标而留下的全零"空洞"）。补上这一行之后，即便用户态传入一个
    "数值上落在范围内、但语义上从未定义"的 ioctl 号，也会得到标准的
    `-ENOIOCTLCMD`（"不支持的 ioctl 命令"）而不是把全零的表项当成有效操作来执行
    ——后者会导致 `op->execute` 是 `NULL` 从而内核态空指针解引用。这是一处典型的
    "防御性编程胜过侥幸"的范例。

### 3.5 `liveupdate_reboot()`：与 kexec 主流程的唯一连接点

LUO 的"出口"——也就是它何时真正把数据"封箱打包"——并不是由 LUO 自己的某个
ioctl 触发的，而是深深嵌入在内核重启系统调用的主路径中。通过搜索调用方可以发现，
`liveupdate_reboot()`（`luo_core.c:227-241`）的唯一调用点位于
`kernel_kexec()`（`kernel/kexec_core.c:1149` 附近），紧跟在 `kexec_trylock()`
成功获取重启锁、并确认 `kexec_image` 存在之后，早于 `CONFIG_KEXEC_JUMP` 的
特殊处理逻辑：

```text
kernel_kexec()                                    // kernel/kexec_core.c:1138
  │
  ├─ kexec_trylock()                              // 取得独占的"我要重启了"的锁
  │     失败 → 直接返回 -EBUSY（已有人在重启）
  │
  ├─ 检查 kexec_image 是否存在
  │     不存在 → goto unlock
  │
  ├─ liveupdate_reboot()  ◄───────────────────────  LUO 在这里被调用！
  │     │                                            （kernel/kexec_core.c:1149）
  │     ├─ luo_session_serialize()                 把所有 outgoing session 冻结、
  │     │                                          序列化进 FDT（详见第 5 章）
  │     │
  │     └─ luo_flb_serialize()                     把所有 outgoing FLB 私有状态
  │                                                序列化进 FDT（详见第 7 章）
  │     失败 → kernel_kexec() 直接返回错误，
  │            重启流程被中止，系统继续以旧内核运行
  │
  ├─ (CONFIG_KEXEC_JUMP 相关处理)
  │
  └─ machine_kexec()                              真正切换到新内核的汇编级跳转
```

这个调用位置的选择本身就传递了一个重要信息：**LUO 不是一个"旁路特性"，而是
kexec 重启流程里的一个正式阶段**。它与 `pm_prepare_console()`、设备驱动的
`shutdown()` 等传统重启钩子处于同一个"不可逆窗口"之前——一旦
`liveupdate_reboot()` 返回成功，序列化数据就已经被写入由 KHO 管理、
对新内核可见的物理内存区域；一旦 `machine_kexec()` 被调用，旧内核就再也没有
回头路。`liveupdate_reboot()` 返回非零值会让整个 `kernel_kexec()` 系统调用
失败并返回给用户态，**新内核根本不会被加载**——这是一种"全有或全无"
（all-or-nothing）的设计，避免了"kexec 已经发生但 LUO 数据没序列化好"这种
最危险的中间状态。

也正因为 `liveupdate_reboot()` 处于"终点站"位置，它内部调用
`luo_session_serialize()` 和 `luo_flb_serialize()` 的**顺序**就有了讲究：
session 先于 FLB 序列化。这是因为序列化 session 的过程会逐个调用
`ops->freeze()` 回调（比如 `memfd_luo_freeze()`），而某些类型的文件在
`freeze` 时可能需要读取/触碰由 FLB 管理的全局共享状态（例如，一个 VFIO 设备在
冻结前可能需要确认其所属的 IOMMU 域已经处于一致状态）；让 FLB 的输出状态在
"所有依赖它的文件都已冻结完毕"之后再统一序列化，可以保证 FLB 看到的是一个
"终态快照"而不是"冻结过程中的中间态"。下一节（第 5、7 章）会更细致地展开这个
顺序依赖。

---

## 第 4 章 用户态 ioctl 接口完整走读

第 3 章已经介绍了 `/dev/liveupdate` 这个"总入口"，但用户态与 LUO 打交道实际上
要经过**两级设备**：先打开 `/dev/liveupdate` 创建/检索出一个 **session fd**，
再对 session fd 发起第二级 ioctl 来管理具体文件的保存与恢复。本章把这两级 ioctl
逐个结构体、逐个字段地过一遍，并总结其背后统一的扩展性设计。

### 4.1 两级设备模型全景图

```text
                          ┌───────────────────────┐
                          │    /dev/liveupdate     │   "总控台"
                          │  (luo_fops, 单例独占)   │   luo_core.c:426-431
                          └───────────┬───────────┘
                                      │
                  ┌───────────────────┴───────────────────┐
                  │                                       │
       ioctl(LIVEUPDATE_IOCTL_              ioctl(LIVEUPDATE_IOCTL_
        CREATE_SESSION)                       RETRIEVE_SESSION)
       luo_ioctl_create_session()            luo_ioctl_retrieve_session()
       (luo_core.c:277-305)                  (luo_core.c:307-335)
                  │                                       │
                  ▼                                       ▼
        新建一个匿名 inode 文件                  在 incoming 列表里按名字查找
        (luo_session_create)                    并标记 retrieved=true
        加入 outgoing 列表                       (luo_session_retrieve)
                  │                                       │
                  └───────────────────┬───────────────────┘
                                      ▼
                          ┌───────────────────────┐
                          │   session fd           │   "分组操作柄"
                          │ (luo_session_fops,     │   luo_session.c:360-364
                          │  匿名 inode 文件)       │   可同时存在多个
                          └───────────┬───────────┘
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            │                         │                         │
   ioctl(LIVEUPDATE_SESSION_  ioctl(LIVEUPDATE_SESSION_  ioctl(LIVEUPDATE_
       PRESERVE_FD)              RETRIEVE_FD)             SESSION_FINISH)
   luo_session_preserve_fd()  luo_session_retrieve_fd()  luo_session_finish()
   (luo_session.c:230-246)    (luo_session.c:248-278)    (luo_session.c:280-290)
            │                         │                         │
            ▼                         ▼                         ▼
     luo_preserve_file()       luo_retrieve_file()       luo_session_finish_one()
     (第 6 章详解)              (第 6 章详解)              (第 6 章详解)
```

这种"先拿到分组句柄，再对分组句柄做操作"的两级模型，与 `openat(dirfd, ...)`、
`io_uring` 的"先建 ring、再在 ring 上提交 SQE"等设计异曲同工——好处是**把"鉴权
与生命周期管理"和"具体业务操作"彻底解耦**：内核只需要在第一级（创建/检索 session）
上做严格校验和单例化管理，第二级 ioctl 则可以自由地多个 session fd 并存、并发
操作（只要各自的 `session->mutex` 不冲突）。第 5 章会看到 `luo_session_getfile()`
正是通过 `anon_inode_getfile()` 把每个 `luo_session` 包装成一个独立的匿名 inode
文件，从而获得了"独立 fd 生命周期 + 独立 file_operations"的能力。

### 4.2 第一级 ioctl：会话的创建与检索

#### 4.2.1 `LIVEUPDATE_IOCTL_CREATE_SESSION`

```c
struct liveupdate_ioctl_create_session {
        __u32           size;
        __s32           fd;
        __u8            name[LIVEUPDATE_SESSION_NAME_LENGTH];
};
```
（`include/uapi/linux/liveupdate.h:77-81`，`LIVEUPDATE_SESSION_NAME_LENGTH` = 64）

| 字段 | 方向 | 含义 |
|---|---|---|
| `size` | 输入 | `sizeof(struct liveupdate_ioctl_create_session)`，用于 `copy_struct_from_user` 的扩展性协议（见 4.3 节） |
| `fd` | 输出 | 内核返回的新 session fd；内核侧实现先用 `get_unused_fd_flags(O_CLOEXEC)` 占好一个 fd 号再回填 |
| `name` | 输入 | 长度上限 64 字节（含结尾 `'\0'`）的会话名字符串，是后续跨 kexec 检索该会话的唯一标识 |

实现 `luo_ioctl_create_session()`（`luo_core.c:277-305`）的关键步骤：

```c
static int luo_ioctl_create_session(struct luo_ucmd *ucmd)
{
        struct liveupdate_ioctl_create_session *argp = ucmd->cmd;
        struct file *file;
        int err;

        argp->fd = get_unused_fd_flags(O_CLOEXEC);
        if (argp->fd < 0)
                return argp->fd;

        err = luo_session_create(argp->name, &file);
        if (err)
                goto err_put_fd;

        err = luo_ucmd_respond(ucmd, sizeof(*argp));
        if (err)
                goto err_put_file;

        fd_install(argp->fd, file);

        return 0;
        ...
}
```

这里"先占座、后入座"的顺序（`get_unused_fd_flags` → 真正创建 → `fd_install`）
是 Linux fd 分配的标准范式：`get_unused_fd_flags` 只是在进程的 fd 表里预留一个
编号而不暴露给用户态，真正的 `struct file *` 创建好之后才用 `fd_install()`
"挂接"上去。这样可以保证**任何中间步骤失败时，fd 表中都不会出现一个指向无效
对象的"半成品"fd**——`err_put_file`/`err_put_fd` 两条回滚路径分别对应"文件已创建
但响应用户失败"和"文件创建失败"两种场景，体现了细粒度的错误路径设计。

#### 4.2.2 `LIVEUPDATE_IOCTL_RETRIEVE_SESSION`

```c
struct liveupdate_ioctl_retrieve_session {
        __u32           size;
        __s32           fd;
        __u8            name[LIVEUPDATE_SESSION_NAME_LENGTH];
};
```
（`include/uapi/linux/liveupdate.h:112-116`）

字段含义与 `create_session` 完全相同（`size`/`fd`/`name`），但**语义截然不同**：
这里的 `name` 是用来在**上一个内核**遗留下来的、已经反序列化好的 incoming 会话
列表中按名字查找。文档注释明确指出：

> "If a preserved session with a matching name is found, the kernel
> instantiates it and returns a new file descriptor... If no session with the
> given name is found, the ioctl will fail with -ENOENT."
> （`include/uapi/linux/liveupdate.h:99-107`）

这是 LUO 命名设计哲学的核心体现——**用户态用一个事先约定好的字符串作为"提货单号"**，
不需要、也不可能依赖跨 kexec 不稳定的 PID、fd 数值、内存地址等运行时身份信息。
这串名字必须在 `create` 和 `retrieve` 两侧的用户态代码里保持完全一致——
`luo_kexec_simple.c` 里的 `TEST_SESSION_NAME "test-session"` 就是一个典型范例：
两个阶段（`run_stage_1`/`run_stage_2`）各自独立运行（甚至可能是不同的进程），
唯一的纽带就是这个编译期常量字符串。

注意文档里也强调了一个容易被忽视的约束：

> "This ioctl can only be called on the main /dev/liveupdate device when the
> system is in the LIVEUPDATE_STATE_UPDATED state."
> （`include/uapi/linux/liveupdate.h:109-110`）

实际上目前内核实现并没有显式的 `LIVEUPDATE_STATE_UPDATED` 状态机字段——这条注释
反映的是**设计意图**：检索操作只有在"系统确实经历过一次 live update、的确存在
incoming 数据"时才有意义。当前实现把这个语义隐含在了
`luo_session_retrieve()`（详见第 5 章）对 incoming 列表的查找结果里：找不到就是
`-ENOENT`，这与"系统从未经历过 live update（FDT 子树不存在）"和"系统经历过
live update 但这个名字的会话不存在"两种情况是统一处理的——从用户态视角看，
这两种情况下"我想要的东西不在"这个事实是一致的，没有必要用不同错误码区分。

### 4.3 第二级 ioctl：会话内的文件操作

第二级 ioctl 的命令号定义在另一个独立的枚举里（注意基址不同）：

```c
enum {
        LIVEUPDATE_CMD_SESSION_BASE = 0x40,
        LIVEUPDATE_CMD_SESSION_PRESERVE_FD = LIVEUPDATE_CMD_SESSION_BASE,
        LIVEUPDATE_CMD_SESSION_RETRIEVE_FD = 0x41,
        LIVEUPDATE_CMD_SESSION_FINISH = 0x42,
};
```
（`include/uapi/linux/liveupdate.h:57-62`）

`LIVEUPDATE_CMD_BASE = 0x00` 与 `LIVEUPDATE_CMD_SESSION_BASE = 0x40` 之间预留了
0x01～0x3F 共 63 个命令号的"空当"——这是给 `/dev/liveupdate` 总控台未来新增命令
（比如查询全局统计信息、设置全局选项）预留的扩展空间，避免将来不得不把
session 级命令号"挪位"。这种"预先把数值空间分段、为不同子系统的未来扩展买保险"
的做法，在 Linux uAPI 设计里相当常见（类比 `ioctl-number.rst` 里各子系统认领的
数值区间）。

#### 4.3.1 `LIVEUPDATE_SESSION_PRESERVE_FD`

```c
struct liveupdate_session_preserve_fd {
        __u32           size;
        __s32           fd;
        __aligned_u64   token;
};
```
（`include/uapi/linux/liveupdate.h:147-151`）

| 字段 | 方向 | 含义 |
|---|---|---|
| `size` | 输入 | 结构体大小（扩展性协议） |
| `fd` | 输入 | 待保存的用户态文件描述符（如 memfd、未来的 kvm/iommufd/VFIO fd） |
| `token` | 输入 | 一个**用户态自定义**的 64 位"提货凭证"，必须在同一 session 内唯一 |

`token` 是 LUO 命名体系的第二层——**会话名字定位到一组文件，token 在组内定位
到具体某一个文件**。这是一个"先按命名空间分组，再在组内按编号索引"的两级寻址
体系，与文件系统的"路径 + inode 号"或者数据库的"表名 + 主键"思路一致。`token`
被设计为完全不透明（"opaque"，见 `include/uapi/linux/liveupdate.h:127`），
内核不解释它的含义，只用于 xarray 查找——这给了用户态极大的自由度：可以是数组
下标、哈希值、设备总线地址，甚至是编码了类型信息的复合值。

文档注释里有一处特别值得玩味的措辞——它把保存过程描述成"两阶段"：

> "the kernel marks the FD internally and *initiates the process* of preparing
> its state for saving. The actual snapshotting of the state typically occurs
> during the subsequent %LIVEUPDATE_IOCTL_PREPARE execution phase..."
> （`include/uapi/linux/liveupdate.h:134-137`）

这里提到的 `%LIVEUPDATE_IOCTL_PREPARE` 在当前 UAPI 头文件中**并不存在**——这是
一处文档与实现"超前/滞后"不同步的痕迹（注释可能撰写于一个更早的设计版本，那时
`preserve` 与"快照"是分离的两步）。从已落地的代码看，`preserve` 已经把"校验
能否保存"和"实际启动保存流程"合并成了一步（见 `luo_preserve_file()`，第 6 章），
真正的"快照终态化"被推迟到了 `freeze` 阶段（即 `liveupdate_reboot()` 触发的
序列化时刻）。理解这一点能帮助读者在阅读 UAPI 注释时对"文档与代码可能存在版本差"
保持警惕——这在快速演进的内核子系统里并不罕见。

#### 4.3.2 `LIVEUPDATE_SESSION_RETRIEVE_FD`

```c
struct liveupdate_session_retrieve_fd {
        __u32           size;
        __s32           fd;
        __aligned_u64   token;
};
```
（`include/uapi/linux/liveupdate.h:175-179`）

字段在内存布局上与 `preserve_fd` **完全相同**，但 `fd` 的方向反转——这次是
**输出**：用户态填入 `token`（必须与 `preserve` 时使用的值完全一致），内核
负责重建出对应的内核对象并通过 `fd` 字段把新文件描述符交还给用户态。

这种"一组字段、两种方向"的结构体设计，从 ABI 稳定性角度看是一种"能省则省"的
优化：preserve 与 retrieve 在概念上是对称的逆操作（"存"与"取"），让它们共享
同一种线路格式（wire format）既减少了重复定义，也降低了未来同步修改两个结构体时
"改了一个忘了另一个"的风险。`luo_session_retrieve_fd()` 里同样遵循
"先占座、后入座"的 fd 安装范式（`luo_session.c:255-275`）。

#### 4.3.3 `LIVEUPDATE_SESSION_FINISH`

```c
struct liveupdate_session_finish {
        __u32           size;
        __u32           reserved;
};
```
（`include/uapi/linux/liveupdate.h:208-211`）

这是三个会话级命令里**唯一没有携带任何业务数据**的——它纯粹是一个"信号"：
"用户态已经把这个会话里所有需要的资源都 `retrieve` 完毕，可以释放内核侧持有的
保存数据了"。`reserved` 字段被显式标注为"Must be zero"，是为未来扩展预留的占位符
——内核可以在将来通过检查它是否为非零来安全地引入新行为，而不破坏老程序
（这是 `copy_struct_from_user` "尾部必须为零"校验天然支持的演进路径，见下节）。

文档注释里还揭示了一个重要的容错设计：

> "If this operation fails, the resources remain preserved in memory.
> Userspace may attempt to call finish again."
> （`include/uapi/linux/liveupdate.h:202-203`）

也就是说 `finish` 是**幂等且可重试**的——内核不会因为一次失败的 `finish`
调用就破坏已保存的数据或进入不一致状态。这与第 6 章要分析的
`luo_file_finish()` 采用的"先全体检查 `can_finish`，再统一提交"两阶段提交
模式相互印证：检查阶段失败不会产生任何副作用，自然可以安全重试。

### 4.4 `copy_struct_from_user`：一套贯穿所有 ioctl 的扩展性协议

读完上面几个结构体会发现一个共同点：**它们的第一个字段永远是 `__u32 size`**。
这不是巧合，而是 `include/uapi/linux/liveupdate.h:17-41` 那段 DOC 注释里描述的
统一 ABI 协议的基石：

> "Each ioctl is passed in a structure pointer as the argument providing the
> size of the structure in the first u32. The kernel checks that any structure
> space beyond what it understands is 0. This allows userspace to use the
> backward compatible portion while consistently using the newer, larger,
> structures."

这段话描述的正是内核里被称为 `copy_struct_from_user()` 的标准设施，第 3 章已经
展示过它在分发函数里的位置。这里把它的工作原理用一张图说清楚——设想内核版本
比用户态新（内核认识的结构体比用户态传入的更大）：

```text
   用户态（旧）传入的结构体                    内核（新）认识的结构体
  ┌──────────────────────┐                ┌──────────────────────────────┐
  │ size = 16             │                │ size                          │
  │ fd                    │   拷贝 16 字节  │ fd                            │
  │ token (前半部分)       │ ──────────────▶│ token                         │
  └──────────────────────┘                │ new_field_added_later (置 0)   │
       user_size = 16                     └──────────────────────────────┘
                                               ksize = sizeof(新结构体)
```

再设想反过来——用户态（新）传入的结构体比内核（旧）认识的更大：

```text
   用户态（新）传入的结构体                    内核（旧）认识的结构体
  ┌──────────────────────┐                ┌──────────────────────┐
  │ size = 24             │   只拷贝前      │ size                  │
  │ fd                    │   16 字节      │ fd                    │
  │ token                 │ ──────────────▶│ token                 │
  │ extra_field (必须是0) │   并检查        └──────────────────────┘
  └──────────────────────┘   后 8 字节
       user_size = 24        是否全 0
                             非零 → -E2BIG
```

`copy_struct_from_user(dst, ksize, src, usize)` 的算法可以归纳为三条规则：

1. 拷贝 `min(ksize, usize)` 字节到内核缓冲区；
2. 若 `ksize > usize`（内核更新），把 `dst` 中超出 `usize` 的"多出来的尾部"清零；
3. 若 `usize > ksize`（用户态更新），检查 `src` 中超出 `ksize` 的尾部是否全为零；
   只要发现任何非零字节，就返回 `-E2BIG`。

这把 ABI 的演进规则简化为一句口诀——**"新认识的字段在缺省时必须等价于全零，旧
内核遇到不认识的非零字段必须报错而不是默默忽略"**。这正是 4.3.3 节里
`liveupdate_session_finish.reserved` 字段"Must be zero"约束存在的根本原因：
它现在看起来"什么都不做"，但已经在 ABI 协议的层面上被纳入了"零值保留位"的
统一框架，将来内核可以安全地赋予它新的含义。

`luo_ucmd`（`luo_internal.h:14-18`）则是把这套协议从"原始指针操作"包装成
更易用的内部表示：

```c
struct luo_ucmd {
        void __user *ubuffer;   /* 用户态缓冲区指针，原始来源 */
        u32 user_size;          /* 用户态实际传入的大小（从 size 字段读出） */
        void *cmd;              /* 内核态缓冲区指针，已完成 copy_struct_from_user */
};
```

而 `luo_ucmd_respond()`（`luo_internal.h:20-32`）则负责"回程"——把内核处理结果
写回用户态时，同样取 `min(user_size, kernel_cmd_size)`，绝不会向用户缓冲区写出
超出其声明大小的数据：

```c
static inline int luo_ucmd_respond(struct luo_ucmd *ucmd,
                                   size_t kernel_cmd_size)
{
        if (copy_to_user(ucmd->ubuffer, ucmd->cmd,
                         min_t(size_t, ucmd->user_size, kernel_cmd_size))) {
                return -EFAULT;
        }
        return 0;
}
```

至此，"读大小 → 校验 → 拷入 → 执行 → 拷出"这条贯穿所有 ioctl 的标准流水线
就完整闭环了——这是一套**写一次、到处复用**的基础设施：`luo_core.c` 与
`luo_session.c` 各自独立定义了几乎一模一样的 `union ucmd_buffer`/
`struct luo_ioctl_op`/`IOCTL_OP`/分发函数（对比 `luo_core.c:365-424` 与
`luo_session.c:292-358`），这种"看似重复"实际上是因为两级设备的命令集合、
命令号基址（`LIVEUPDATE_CMD_BASE` vs. `LIVEUPDATE_CMD_SESSION_BASE`）和分发表
项类型（`execute(ucmd)` vs. `execute(session, ucmd)`）均不同，提炼公共框架的
收益小于增加的间接层复杂度——这是一种"局部重复好过过度抽象"的务实选择，
与本仓库写作规范里"三行类似代码好过一个过早的抽象"的原则不谋而合。

### 4.5 错误码语义表

`include/uapi/linux/liveupdate.h:26-40` 那段 DOC 注释为整个 ioctl 接口定义了
一套统一的错误码语义——这是确保用户态能够编写出**可靠的错误处理代码**而不必
靠"猜"的关键基础设施。下表整理自该注释并补充了在本仓库代码中能找到对应实现
位置的条目：

| 错误码 | 语义 | 典型触发位置 |
|---|---|---|
| `ENOTTY` | ioctl 命令号本身完全不被支持 | `luo_ioctl()`/`luo_session_ioctl()` 中下标越界或 `op->ioctl_num != cmd` 时返回（注：当前实现实际返回的是更细分的 `-EINVAL`/`-ENOIOCTLCMD`，DOC 注释描述的是通用 ioctl 设计规范的理想行为） |
| `E2BIG` | 命令号被支持，但用户传入的结构体里，内核不认识的那部分尾巴非零 | `copy_struct_from_user()` 内部检测到尾部非零时返回 |
| `EOPNOTSUPP` | 命令号和结构体都认识，但某个已知字段的取值内核不支持 | 例如 `memfd_luo_can_preserve()` 判定某个 fd 的属性组合不受支持时（第 9 章详解） |
| `EINVAL` | 一切都被理解，但字段取值本身不正确 | 比如 `ucmd.user_size < op->min_size`（`luo_core.c:415`）、会话名长度不合法等 |
| `ENOENT` | 提供的 token/会话名找不到对应资源 | `luo_session_retrieve()` 在 incoming 列表中查无此名（`-ENOENT`）、`luo_retrieve_file()` 在 file_set 中查无此 token |
| `ENOMEM` | 内存不足 | `luo_session_alloc()`/`kzalloc` 等分配失败路径 |
| `EOVERFLOW` | 数学运算溢出 | 例如 `mm/memfd_luo.c` 中折算 `nr_folios` 时对 `UINT_MAX` 的检查（第 9 章详解） |
| `EBUSY` | 设备已被独占 | `luo_open()` 中 `atomic_cmpxchg` 失败时返回（`luo_core.c:344`） |
| `EIO` | 反序列化失败，系统处于不可信状态 | `luo_open()` 在 `luo_session_deserialize()` 失败时返回（`luo_core.c:349`） |

把这张表与具体源码位置对照阅读，会发现 LUO 的错误码使用总体上**严格遵循了**
DOC 注释里定义的契约——这对于一个刚刚进入主线不久的新子系统来说并不容易：
错误码语义的一致性往往是用户态库（比如未来可能出现的 `libluo`）能否被可靠地
编写出来的先决条件。

---

## 第 5 章 Session 子系统源码精读

本章对 `kernel/liveupdate/luo_session.c`（615 行）做逐函数级的走读。这是
理解"用户态分组容器如何在内核里落地、如何跨越 kexec 存活"的核心章节——
读完本章，第 6、7 章里反复出现的"outgoing/incoming"、"序列化/反序列化"等
术语就会有一个具体可触摸的范例打底。

### 5.1 核心数据结构

```c
/* 16 4K pages, give space for 744 sessions */
#define LUO_SESSION_PGCNT       16ul
#define LUO_SESSION_MAX         (((LUO_SESSION_PGCNT << PAGE_SHIFT) -  \
                sizeof(struct luo_session_header_ser)) /               \
                sizeof(struct luo_session_ser))
```
（`luo_session.c:72-76`）

这两行宏定义体现了 LUO 序列化内存布局的一个通用范式——**"先划定一块固定大小
的物理内存，再用编译期算术从中反推出可容纳的元素上限"**。`LUO_SESSION_MAX`
不是一个写死的魔数，而是 `(总字节数 - 头部大小) / 单个元素大小` 的整数除法
结果（约 744）。这样做的好处是：当 `struct luo_session_ser` 因为新增字段而
变大时，`LUO_SESSION_MAX` 会自动按比例缩小，不需要开发者手动同步——**用
"结构关系"代替"魔数常量"，让编译器帮你保持一致性**。这一范式在 `luo_file.c`
的 `LUO_FILE_MAX` 和 `luo_flb.c` 的 `LUO_FLB_MAX` 中重复出现，是贯穿三个子
系统的统一设计语言。

```c
struct luo_session_header {
        long count;
        struct list_head list;
        struct rw_semaphore rwsem;
        struct luo_session_header_ser *header_ser;
        struct luo_session_ser *ser;
        bool active;
};

struct luo_session_global {
        struct luo_session_header incoming;
        struct luo_session_header outgoing;
};
```
（`luo_session.c:90-107`）

`luo_session_header` 是支撑 outgoing/incoming 双状态模式的"工作台"结构体，
逐字段看：

| 字段 | 作用 | 设计要点 |
|---|---|---|
| `count` | 当前链表上的会话数量 | 与 `header_ser->count` 是"运行时镜像"与"序列化落盘值"的关系——前者随时变化，后者只在 `serialize()` 时刻被赋值一次 |
| `list` | `struct luo_session` 的链表头 | outgoing 链表按**用户创建顺序**排列（`list_add_tail`），incoming 链表按**反序列化遍历顺序**排列——两者隐含的顺序语义不同，但代码复用了同一套 `list_head` 机制 |
| `rwsem` | 保护链表与计数字段的读写信号量 | 用读写锁而非互斥锁，是因为"按名字检索"（`luo_session_retrieve`）这种只读遍历远比"插入/删除"频繁，读写锁允许检索操作并发执行 |
| `header_ser` | 指向序列化区域的"头部"（记录会话总数） | outgoing 侧由 `kho_alloc_preserve` 分配，incoming 侧通过 FDT 属性里记录的物理地址 `phys_to_virt` 而来 |
| `ser` | 指向序列化数组本体（`struct luo_session_ser[]`） | 通过指针算术 `(void *)(header_ser + 1)` 得出——即"头部结构体后面紧跟着数组"，这正是 8.1 节将详细展开的 FDT 内存布局约定 |
| `active` | 标记这一侧（incoming/outgoing）是否已正确初始化 | incoming 侧的 `active == false` 表示"系统是第一次启动，没有上一个内核留下任何 LUO 数据"——这是判定"冷启动 vs. live-update 后启动"的旗标 |

`luo_session_global` 静态实例在编译期通过 `LIST_HEAD_INIT`/`__RWSEM_INITIALIZER`
完成链表头与读写锁的初始化（`luo_session.c:109-118`），这是内核里"全局单例 +
静态初始化"的标准写法，避免了运行时再去调用 `INIT_LIST_HEAD`/`init_rwsem`
带来的"忘记初始化就被使用"的隐患。

```c
struct luo_session {
        char name[LIVEUPDATE_SESSION_NAME_LENGTH];
        struct luo_session_ser *ser;
        struct list_head list;
        bool retrieved;
        struct luo_file_set file_set;
        struct mutex mutex;
};
```
（`luo_internal.h:71-78`）

`luo_session` 是一个会话在内核态运行时的"活的"表示，与它对应的"静态快照"
是 `struct luo_session_ser`（第 8 章详解其内存布局）。值得注意它额外携带的
`mutex` ——这是一把**会话粒度**的细粒度锁，与全局的 `rwsem` 形成"两级锁"
体系：`rwsem` 保护"哪些会话存在"这一全局事实，`mutex` 保护"某个具体会话内部
的状态"（文件集合、`retrieved` 标记等）。这样多个用户态线程可以同时操作
不同的 session 而不互相阻塞，只有在并发操作**同一个** session 时才会序列化
——是教科书式的"细化锁粒度以提升并发度"实践。

### 5.2 `luo_session_alloc`/`luo_session_free`：构造与析构的对称设计

```c
static struct luo_session *luo_session_alloc(const char *name)
{
        struct luo_session *session = kzalloc_obj(*session);

        if (!session)
                return ERR_PTR(-ENOMEM);

        strscpy(session->name, name, sizeof(session->name));
        INIT_LIST_HEAD(&session->file_set.files_list);
        luo_file_set_init(&session->file_set);
        INIT_LIST_HEAD(&session->list);
        mutex_init(&session->mutex);

        return session;
}

static void luo_session_free(struct luo_session *session)
{
        luo_file_set_destroy(&session->file_set);
        mutex_destroy(&session->mutex);
        kfree(session);
}
```
（`luo_session.c:120-141`）

两个函数互为镜像，按"后构造的先析构"的顺序对称排布——`alloc` 里
`file_set` 相关初始化在 `mutex_init` 之前，`free` 里 `file_set_destroy`
也在 `mutex_destroy` 之前。这种"严格对称"不是代码洁癖，而是为了让代码审阅者
可以"扫一眼就确信资源管理是配对的"——尤其在 `luo_file_set_init`/`destroy`
内部还分别调用 `kho_alloc_preserve`/`kho_unpreserve_free`（详见第 6 章）这种
"分配即预订给 KHO"的特殊语义时，构造/析构路径的严格对称就显得尤为重要：
任何一处疏漏都可能导致 KHO 侧的预订记录与实际内存对象"账实不符"。

另外注意 `strscpy(session->name, name, sizeof(session->name))`——使用
`strscpy` 而非 `strncpy`/`strlcpy`，是因为 `strscpy` 保证目标缓冲区**总是**
以 `'\0'` 结尾且不会读取源字符串越界，是内核里公认的"更安全的字符串拷贝"
首选（`strncpy` 在源串恰好等长时不会自动追加结尾符，是历史上无数缓冲区相关
bug 的根源）。

### 5.3 `luo_session_insert`/`remove`：命名冲突检测与容量限制

```c
static int luo_session_insert(struct luo_session_header *sh,
                              struct luo_session *session)
{
        struct luo_session *it;

        guard(rwsem_write)(&sh->rwsem);

        /*
         * For outgoing we should make sure there is room in serialization array
         * for new session.
         */
        if (sh == &luo_session_global.outgoing) {
                if (sh->count == LUO_SESSION_MAX)
                        return -ENOMEM;
        }

        /*
         * For small number of sessions this loop won't hurt performance
         * but if we ever start using a lot of sessions, this might
         * become a bottle neck during deserialization time, as it would
         * cause O(n*n) complexity.
         */
        list_for_each_entry(it, &sh->list, list) {
                if (!strncmp(it->name, session->name, sizeof(it->name)))
                        return -EEXIST;
        }
        list_add_tail(&session->list, &sh->list);
        sh->count++;

        return 0;
}
```
（`luo_session.c:143-173`）

这个函数浓缩了三个值得细品的设计决策：

1. **容量检查只在 `outgoing` 侧进行**。原因很直白：`outgoing.ser` 数组的大小
   是在 `luo_session_setup_outgoing()` 里按照 `LUO_SESSION_MAX` 一次性分配好的
   固定大小物理内存（见 5.6 节），新建会话最终是要写进这个数组的某个槽位，
   所以必须有上限检查。而 `incoming` 侧的会话数量是"既成事实"——它是从上一个
   内核**已经写好**的序列化数组里读出来的（`sh->header_ser->count`），不存在
   "新增导致超限"的问题，因此不需要也不应该在插入时检查容量。

2. **命名冲突检测是 O(n) 线性扫描，且代码作者主动写下了"这可能成为瓶颈"的
   自我批评式注释**。这是一处颇有"工程诚实"美德的代码——在会话数量级是"几个
   到几十个"的真实场景下，线性扫描带来的开销完全可以忽略；但作者没有藏着掖着
   "这是个潜在性能隐患"的事实，而是直接写进注释里，方便未来如果场景变化
   （比如某个用户真的创建了几百个会话），后来者能一眼定位到需要优化的地方。
   这是"不要为了假想的未来过度设计，但要为后来者留下诚实的路标"的优秀范例。

3. **`guard(rwsem_write)` 用 RAII 风格的清理守卫宏**消除了手工
   `down_write`/`up_write` 配对可能遗漏的风险——函数中任何一条 `return`
   语句执行时，C 编译器生成的析构调用都会自动释放写锁。这是 Linux 6.x 引入
   `linux/cleanup.h` 之后在新代码里被大量采用的现代写法，与 C++ 的
   `std::lock_guard`、Rust 的 `Drop` 在思想上一脉相承。

`luo_session_remove()`（`luo_session.c:175-181`）是 `insert` 的逆操作，
同样持写锁、`list_del` + `count--`，对称且简洁，不再赘述。

### 5.4 `luo_session_create`/`retrieve`：用户态可见的两个生命周期入口

```c
int luo_session_create(const char *name, struct file **filep)
{
        struct luo_session *session;
        int err;

        session = luo_session_alloc(name);
        if (IS_ERR(session))
                return PTR_ERR(session);

        err = luo_session_insert(&luo_session_global.outgoing, session);
        if (err)
                goto err_free;

        scoped_guard(mutex, &session->mutex)
                err = luo_session_getfile(session, filep);
        if (err)
                goto err_remove;

        return 0;

err_remove:
        luo_session_remove(&luo_session_global.outgoing, session);
err_free:
        luo_session_free(session);

        return err;
}
```
（`luo_session.c:383-409`）

调用顺序是"先分配对象 → 插入全局列表（建立可见性）→ 包装成 file（建立用户态
句柄）"，三个 `err_*` 标签按**逆序回滚**：`err_remove` 撤销 `insert`，
`err_free` 撤销 `alloc`。这个"建立顺序与回滚顺序互为镜像"的模式会在整个
LUO 代码库里反复出现（`luo_preserve_file`、`luo_ioctl_create_session` 等），
是 C 语言里用 `goto` 实现资源安全管理的经典范式——只要严格遵守这个对称性，
review 时只需要核对"每一步建立操作是否都有对应的逆操作标签"即可，而不需要
追踪复杂的控制流图。

```c
int luo_session_retrieve(const char *name, struct file **filep)
{
        struct luo_session_header *sh = &luo_session_global.incoming;
        struct luo_session *session = NULL;
        struct luo_session *it;
        int err;

        scoped_guard(rwsem_read, &sh->rwsem) {
                list_for_each_entry(it, &sh->list, list) {
                        if (!strncmp(it->name, name, sizeof(it->name))) {
                                session = it;
                                break;
                        }
                }
        }

        if (!session)
                return -ENOENT;

        guard(mutex)(&session->mutex);
        if (session->retrieved)
                return -EINVAL;

        err = luo_session_getfile(session, filep);
        if (!err)
                session->retrieved = true;

        return err;
}
```
（`luo_session.c:411-439`）

这里有一个微妙但重要的并发设计：**查找阶段持读锁，状态变更阶段持会话自身的
互斥锁，两把锁不交叠持有**（先 `scoped_guard(rwsem_read, ...)` 块结束、
释放读锁，再 `guard(mutex)(&session->mutex)`）。这是为了避免锁的"嵌套升级"
风险——如果在持有全局读锁的同时去抢会话的互斁锁，当另一个路径以相反顺序
（先锁会话、再试图获取全局写锁，例如 `luo_session_release` 中
`luo_session_remove` 需要写锁）获取这两把锁时，就会构成经典的 ABBA 死锁。
LUO 通过"用完一把锁就释放，再去拿下一把"的纪律性写法，从根本上避免了这种
死锁模式——这是并发编程里"缩短临界区、避免锁的嵌套"黄金法则的具体实践。

另外 `session->retrieved` 标记的"一次性"语义在这里体现得很清楚——第二次对
同一个会话调用 `retrieve` 会被 `-EINVAL` 拒绝。这与第 4 章 `finish` 的"可
重复调用"形成对照：`retrieve` 是"取得所有权"的单向转移操作（拿到了就不能
再拿一次），而 `finish` 是"声明完成"的幂等信号——两者在语义设计上的差异
精确反映了它们各自在状态机中的角色。

### 5.5 `luo_session_getfile`/`release`：把内核对象包装成用户态 fd

```c
static int luo_session_getfile(struct luo_session *session, struct file **filep)
{
        char name_buf[128];
        struct file *file;

        lockdep_assert_held(&session->mutex);
        snprintf(name_buf, sizeof(name_buf), "[luo_session] %s", session->name);
        file = anon_inode_getfile(name_buf, &luo_session_fops, session, O_RDWR);
        if (IS_ERR(file))
                return PTR_ERR(file);

        *filep = file;

        return 0;
}
```
（`luo_session.c:367-381`）

`anon_inode_getfile()` 是内核里"我需要一个 `struct file *`，但它背后并不
对应任何真实文件系统路径"场景下的标准工具——`eventfd`、`epoll`、`timerfd`、
`io_uring` 等都采用同样的手法。给匿名 inode 文件起一个 `[luo_session] <名字>`
形式的名字（会出现在 `/proc/<pid>/fd/<n>` 的符号链接目标里），是一个非常
体贴调试者的细节——运维人员通过 `ls -l /proc/<pid>/fd` 就能立刻看出某个 fd
是哪个 LUO 会话，而不必去翻源码或者用 `crash`/`drgn` 之类工具深挖。
`lockdep_assert_held()` 则是一处运行时锁状态断言，明确告知调用者"这个函数
要求在持有 `session->mutex` 的前提下调用"——`lockdep` 在调试构建下会在违反
此约束时立即报警，比"在注释里写一句话希望大家看到"可靠得多。

`luo_session_release()`（`luo_session.c:203-228`）是 fd 被关闭时的回调，
它需要根据会话的"血统"（incoming 还是 outgoing）选择截然不同的清理路径：

```c
static int luo_session_release(struct inode *inodep, struct file *filep)
{
        struct luo_session *session = filep->private_data;
        struct luo_session_header *sh;

        /* If retrieved is set, it means this session is from incoming list */
        if (session->retrieved) {
                int err = luo_session_finish_one(session);

                if (err) {
                        pr_warn("Unable to finish session [%s] on release\n",
                                session->name);
                        return err;
                }
                sh = &luo_session_global.incoming;
        } else {
                scoped_guard(mutex, &session->mutex)
                        luo_file_unpreserve_files(&session->file_set);
                sh = &luo_session_global.outgoing;
        }

        luo_session_remove(sh, session);
        luo_session_free(session);

        return 0;
}
```

这段代码用 `session->retrieved` 这一个布尔值，巧妙区分出了两种完全不同的
"会话来历"和与之匹配的清理语义：

```text
                    luo_session_release() 的两条岔路
                              │
              ┌───────────────┴────────────────┐
              │                                 │
   retrieved == true                  retrieved == false
   "我是从上一个内核继承来                "我是这一代内核里
    并已经被用户态接手的会话"             用户主动创建、尚未
              │                          经历 kexec 的会话"
              ▼                                 ▼
   调用 luo_session_finish_one()        调用 luo_file_unpreserve_files()
   ——通知每个文件 handler              ——撤销每个文件的"已标记保存"
   "恢复阶段结束，可以释放                状态，把它们从 luo_preserved_files
   保存期间占用的内核资源了"              xarray 中移除（详见第 6 章）
              │                                 │
              ▼                                 ▼
        归还给 incoming 列表              归还给 outgoing 列表
   （表示这个名字的会话已经走完           （用户中途放弃了这个会话，
    完整的恢复流程，画上句号）            它再也不会被序列化进下一次
                                          kexec 的 outgoing 数据里）
```

这正是"一个运行中的内核同时是接收方与发送方"这一主题在 fd 生命周期管理上的
具体投影——同一份代码（`luo_session_release`）必须能够正确处理"我是刚结束
我的旅程的旧数据"和"我是刚刚开始旅程、却中途被放弃的新数据"两种截然相反
的情形。如果没有 `retrieved` 这个简单的布尔标记作区分依据，这两条路径很可能
会被误用、产生资源泄漏或者双重释放。

### 5.6 `luo_session_setup_outgoing`/`incoming`：FDT 节点的对称构建与解析

这两个函数是第 3 章时序图里 `luo_early_startup`/`luo_fdt_setup` 调用链的
具体实现，分别负责"在 outgoing FDT 里凿出一个 session 子节点并分配序列化
内存"和"在 incoming FDT 里找到对应子节点并定位到上一个内核留下的序列化内存"。
两者的代码结构几乎是镜像对称的：

```c
int __init luo_session_setup_outgoing(void *fdt_out)
{
        struct luo_session_header_ser *header_ser;
        u64 header_ser_pa;
        int err;

        header_ser = kho_alloc_preserve(LUO_SESSION_PGCNT << PAGE_SHIFT);
        if (IS_ERR(header_ser))
                return PTR_ERR(header_ser);
        header_ser_pa = virt_to_phys(header_ser);

        err = fdt_begin_node(fdt_out, LUO_FDT_SESSION_NODE_NAME);
        err |= fdt_property_string(fdt_out, "compatible",
                                   LUO_FDT_SESSION_COMPATIBLE);
        err |= fdt_property(fdt_out, LUO_FDT_SESSION_HEADER, &header_ser_pa,
                            sizeof(header_ser_pa));
        err |= fdt_end_node(fdt_out);

        if (err)
                goto err_unpreserve;

        luo_session_global.outgoing.header_ser = header_ser;
        luo_session_global.outgoing.ser = (void *)(header_ser + 1);
        luo_session_global.outgoing.active = true;

        return 0;

err_unpreserve:
        kho_unpreserve_free(header_ser);
        return err;
}
```
（`luo_session.c:441-471`）

这个函数清楚地展示了 LUO 序列化元数据"两段式"传递协议：**FDT 节点本身只
携带一个 64 位物理地址属性（`LUO_FDT_SESSION_HEADER`），真正的大块数据
（头部 + 数组）放在通过 `kho_alloc_preserve` 单独分配的、按页对齐的物理内存
区域里**。这正是第 2 章总结的"标准三件套"模式（`kho_alloc_preserve` /
`kho_unpreserve_free` / `kho_restore_free`）在 LUO 自身元数据管理上的直接
应用——FDT 这棵"目录树"只负责存放"指路牌"（地址指针），真正的"货物"
（可能高达 64KB 的会话数组）存放在目录树之外的独立内存块中。这种分离带来
两个好处：(a) FDT 本身可以保持精巧紧凑，便于 `libfdt` 高效解析；(b) 大块
数据的内存可以用更适合大块连续分配的接口（`kho_alloc_preserve` 内部走的是
页分配器路径）单独管理，不必受 FDT 格式本身对齐和编码方式的限制。

`(void *)(header_ser + 1)` 这一行指针算术是 C 语言"结构体指针 +1"在这里
的巧妙复用——它精确地指向 `header_ser` 这块内存里、紧跟在
`struct luo_session_header_ser` 之后的第一个字节，也就是 `luo_session_ser`
数组的起始地址。这要求 `kho_alloc_preserve(LUO_SESSION_PGCNT << PAGE_SHIFT)`
分配出来的内存按照"头部紧跟数组"的方式连续布局——这正是 `LUO_SESSION_MAX`
宏定义里 `(总字节 - sizeof(头部)) / sizeof(数组元素)` 这个算式所依赖的
内存假设，三处代码（宏定义、`setup_outgoing` 里的指针算术、第 8 章要画的
内存布局图）共同构成了一份"事实上的 ABI 契约"，但有意思的是它**没有**用
显式的复合结构体（如 `struct { header; ser[N]; }`）来表达——这是因为
`LUO_SESSION_MAX` 在编译期是一个动态算式而非静态数组长度，C 语言的
"柔性数组成员"在这种"长度由另一个表达式决定"的场景下并不适用，所以作者
选择了"分配一块大内存 + 指针算术切分"的更原始但更灵活的方式。

`luo_session_setup_incoming()`（`luo_session.c:473-511`）则做相反的事情——
**解析**而非**构建**：用 `fdt_subnode_offset` 找到子节点、
`fdt_node_check_compatible` 校验版本兼容性、`fdt_getprop` 取出物理地址属性、
`phys_to_virt` 转换成可访问的虚拟地址。注意它对"找不到 session 子节点"
和"compatible 不匹配"这两种情况都直接返回 `-EINVAL` 并打印 `pr_err`——
这是 incoming 路径"零容忍"哲学的体现：任何解析异常都意味着上一个内核传来的
数据不可信，必须尽早（在 `early_initcall` 阶段）以最响亮的方式报错，为
3.3 节里 `liveupdate_early_init` 的 `panic` 路径提供准确的诊断信息。

### 5.7 `luo_session_serialize`：冻结全部会话并写入序列化数组

```c
int luo_session_serialize(void)
{
        struct luo_session_header *sh = &luo_session_global.outgoing;
        struct luo_session *session;
        int i = 0;
        int err;

        guard(rwsem_write)(&sh->rwsem);
        list_for_each_entry(session, &sh->list, list) {
                err = luo_session_freeze_one(session, &sh->ser[i]);
                if (err)
                        goto err_undo;

                strscpy(sh->ser[i].name, session->name,
                        sizeof(sh->ser[i].name));
                i++;
        }
        sh->header_ser->count = sh->count;

        return 0;

err_undo:
        list_for_each_entry_continue_reverse(session, &sh->list, list) {
                i--;
                luo_session_unfreeze_one(session, &sh->ser[i]);
                memset(sh->ser[i].name, 0, sizeof(sh->ser[i].name));
        }

        return err;
}
```
（`luo_session.c:584-613`）

这是 `liveupdate_reboot()` 调用链最终落地的核心函数，也是本章里"错误处理
设计哲学"展现得最完整的一处。请注意它与 `luo_session_deserialize()`
（下一节）形成了鲜明对比——**序列化路径精心设计了完整的回滚（undo）逻辑，
而反序列化路径却"放任失败、拒绝回滚"**。这种不对称绝非疏忽，而是经过深思
熟虑的取舍，原因藏在两个阶段所处的"危险等级"里：

- **序列化发生在 kexec 真正切换之前**——此刻旧内核仍然完整运行，所有的内核
  对象、硬件状态都处于已知、一致、可逆的状态。如果在序列化第 5 个会话时失败，
  把前 4 个已经"冻结"的会话解冻回正常运行状态是完全可行、代价可控的操作——
  `luo_session_unfreeze_one` 调用的 `luo_file_unfreeze`（第 6 章详解）能够
  把文件从"冻结态"带回"正常运行态"。**此时此刻，"回到从前"仍然是一个有意义
  的选项**，因此 LUO 选择不遗余力地实现它，确保一次序列化失败不会导致系统
  进入半死不活的状态——`liveupdate_reboot()` 失败后，系统还能继续以旧内核
  正常提供服务。

- 而 `luo_session_deserialize()` 发生在**新内核刚启动、用户态刚打开
  `/dev/liveupdate` 之时**——此刻旧内核早已不复存在，唯一的"已知状态"就是
  "新内核刚刚启动、尚未承接任何业务"。如果反序列化在处理第 5 个会话时失败，
  此刻已经重建出的前 4 个会话所对应的设备、内存、文件描述符可能处于任何
  无法预知的中间状态——**已经不存在"回到从前"这个选项**，因为"从前"
  （旧内核的运行状态）已经随着 kexec 一起灰飞烟灭了。试图去为这种"前所未有"
  的中间状态实现一套安全的清理逻辑，不仅工程代价极高，而且很可能因为遗漏了
  某个边界情况而引入新的、更隐蔽的 bug。

错误处理代码本身的实现细节也值得品味：`err_undo` 标签下使用
`list_for_each_entry_continue_reverse`，从刚刚失败的那个会话**之前**的
位置开始反向遍历——这要求 `i` 这个数组下标计数器与链表遍历位置精确同步
（每次成功冻结后才 `i++`，失败时 `i` 恰好停在"最后一个成功项的下一个位置"，
回滚循环里先 `i--` 再使用），这种"数组下标与链表位置协同维护"的写法虽然
不复杂，但任何一处 `i++`/`i--` 的位置错位都会导致回滚操作作用在错误的
序列化槽位上——这是阅读这段代码时需要格外留意、也是代码审阅时需要重点
核对的细节。

`sh->header_ser->count = sh->count;` 这一行只在**所有**会话都冻结成功后
才执行——这保证了序列化数组中 `count` 字段的"全有或全无"原子性：要么它
反映的是完整、一致的会话集合，要么（如果中途失败）它保持着上一次成功序列化
时的旧值（理论上第一次执行时是 0，因为 `kho_alloc_preserve` 分配的内存默认
被清零）。这样，即便发生了极端情况——序列化失败但 `liveupdate_reboot()`
的返回值检查存在 bug、kexec 仍然继续了——新内核看到的也只会是"会话数为 0"
或者"上一次成功序列化的旧快照"，而不会是一个数组内容与计数不匹配的"半成品"
状态。这是"宁可看到旧的一致状态，也不要看到新的不一致状态"原则的体现，
与数据库事务设计中的原子性（Atomicity）保证如出一辙。

### 5.8 `luo_session_deserialize`：一次性、带缓存的反序列化与"宁可泄漏"哲学

```c
int luo_session_deserialize(void)
{
        struct luo_session_header *sh = &luo_session_global.incoming;
        static bool is_deserialized;
        static int saved_err;
        int err;

        /* If has been deserialized, always return the same error code */
        if (is_deserialized)
                return saved_err;

        is_deserialized = true;
        if (!sh->active)
                return 0;
        ...
}
```
（`luo_session.c:513-526` 节选）

开篇这几行看似平淡，实则布下了两处精巧的状态管理机关：

1. **`static bool is_deserialized` + `static int saved_err`**：用函数静态
   变量实现"只执行一次，此后永远返回相同结果"的幂等缓存。回想第 3 章——
   `luo_session_deserialize()` 是从 `luo_open()` 里调用的，而
   `/dev/liveupdate` 在被关闭后是允许重新打开的（独占锁只是一个 `in_use`
   标记，不限制"先关后开"）。如果没有这个缓存，每次重新打开设备都会重新跑
   一遍完整的反序列化流程——轻则浪费 CPU，重则因为"第一次部分失败已经修改
   了某些全局状态"而在第二次调用时产生完全不同（且更难调试）的错误。
   用两个 `static` 变量把"是否已经跑过"和"跑出来的结果是什么"都钉死，
   一举解决了这两个问题。

2. **`if (!sh->active) return 0;`**：这是判定"冷启动"的关键分支——如果
   `luo_session_setup_incoming()` 在早期启动时没有找到来自上一个内核的
   session 节点（`sh->active` 保持初始值 `false`），说明这是系统的第一次
   启动（或者上一次重启不是 live update 而是普通重启），那么自然没有任何
   incoming 数据需要反序列化，直接返回成功。这一行让 `luo_open()` 在
   "冷启动后第一次打开 `/dev/liveupdate`" 与 "live update 后第一次打开
   `/dev/liveupdate`" 这两种场景下都能得到正确、一致的行为，调用方完全不
   需要关心区分这两种场景——这正是把"判断是否经历过 live update"的复杂度
   封装在 LUO 内部、不泄漏给上层调用者的良好封装实践。

进入循环体之前，代码作者用一段长达 14 行的注释（`luo_session.c:528-541`）
把整章反复提及的设计哲学讲得清清楚楚，原文摘录如下，逐句对照解读：

```c
        /*
         * Note on error handling:
         *
         * If deserialization fails (e.g., allocation failure or corrupt data),
         * we intentionally skip cleanup of sessions that were already restored.
         *
         * A partial failure leaves the preserved state inconsistent.
         * Implementing a safe "undo" to unwind complex dependencies (sessions,
         * files, hardware state) is error-prone and provides little value, as
         * the system is effectively in a broken state.
         *
         * We treat these resources as leaked. The expected recovery path is for
         * userspace to detect the failure and trigger a reboot, which will
         * reliably reset devices and reclaim memory.
         */
```

| 注释原句 | 解读 |
|---|---|
| "we **intentionally** skip cleanup" | 强调"跳过清理"是**有意为之**的设计决策，不是疏漏——这是写给未来可能想要"修复"这处"看似缺失的错误处理"的开发者的预防针 |
| "A partial failure leaves the preserved state **inconsistent**" | 点明根本原因：一旦中途失败，剩下未处理的序列化数据所描述的内核对象图已经与已重建出的部分脱节，没有任何"局部正确"的状态可言 |
| "Implementing a safe undo... is **error-prone** and provides **little value**" | 关键的成本-收益判断：构造一个能正确处理"会话→文件→硬件状态"多层依赖关系的安全回滚机制，本身的复杂度和出错概率，要高于它能带来的收益（因为系统已经处于"破碎"状态，回滚了又能怎样？） |
| "We **treat these resources as leaked**" | 直接给出了结论性的设计立场——接受"资源泄漏"这个看起来"难看"的结果 |
| "The expected recovery path is for **userspace** to detect the failure and trigger a **reboot**" | 把"恢复"这个职责显式地转交给用户态——通过一次新的、干净的（非 live-update）重启，依靠硬件复位和内存全清空这种"重锤"来一次性解决所有不确定状态 |

这段注释的价值，远超过它所描述的那几行代码本身——它实际上是在向读者**论证
一个反直觉的工程决策**：通常我们认为"完善的错误处理 = 全面的资源回收"，
但这里的论证指出，**当系统已经处于"无法定义什么是正确状态"的破碎境地时，
执着于"清理"反而可能带来比"不清理"更大的风险**（比如：在清理过程中触碰
一个已经处于异常状态的硬件寄存器，导致系统直接死锁或者损坏硬件）。"依赖外部、
更可靠的机制（重启）来兜底，而不是在系统内部强行实现一个不可能完美的
自愈逻辑"——这其实是分布式系统设计里"let it crash"哲学（Erlang/OTP 的
监督树思想）在单机内核语境下的回响。第 11 章会把这个哲学放到全局视角，
和 `luo_restore_fail`（即 `panic`）、文件层的 `luo_file_deserialize`
错误处理路径放在一起做系统性梳理。

`saved_err` 缓存与"跳过清理"两者结合，还产生了一个微妙但重要的协同效应：
即便第一次反序列化在处理第 5 个会话时失败，`is_deserialized` 也已经被设为
`true`、`saved_err` 记录了错误码——此后任何线程重新调用
`luo_session_deserialize()`（哪怕是并发地通过 `open()` 竞争）都会立即拿到
同一个缓存的错误码而不会"重新尝试、再次踩坑"。这种"一旦判死刑就不再上诉"
的处理方式，恰恰是对"系统已经处于不可信状态"这一现实的清醒承认——重新尝试
没有任何意义，只会拖慢失败反馈的速度。

最后，反序列化成功的收尾动作同样意味深长：

```c
        kho_restore_free(sh->header_ser);
        sh->header_ser = NULL;
        sh->ser = NULL;
```
（`luo_session.c:574-576`）

`kho_restore_free` 是第 2 章介绍过的"标准三件套"中专门用于**回收 incoming
侧临时元数据内存**的接口——一旦序列化数组里的所有信息都已经被读出、转化为
鲜活的 `luo_session`/`luo_file` 对象图，承载这些"原始数据"的物理内存就
失去了存在的价值，可以交还给伙伴分配器供后续正常使用。把指针置为 `NULL`
则是一种防御性编程——确保任何后续的误用（例如不小心再次调用某个本应只在
启动早期使用的函数）会立即触发空指针解引用而不是悄悄地读取已经失效（且可能
已被复用）的内存。

至此，我们已经把一个会话从"用户态发起创建"到"冻结进入序列化数组"再到
"在新内核里被重建出来"的完整生命周期串了一遍。第 6 章将深入它内部最复杂的
部分——`luo_file_set` 与单个 `luo_file` 的保存、冻结、检索、完成全流程。

---

## 第 6 章 File 保存生命周期源码精读

如果说 Session 是 LUO 面向用户态的"骨架"，那么 `kernel/liveupdate/luo_file.c`
（927 行）就是 LUO 真正的"心脏"——它定义了"一个文件如何被保存、冻结、
跨越 kexec、被恢复、最终被释放"这条贯穿全书的核心状态机，也是第 9 章
`mm/memfd_luo.c` 所有回调函数赖以运作的框架。本章逐函数走读这条生命线。

### 6.1 回调模型：`liveupdate_file_ops` 与 `liveupdate_file_handler`

在深入 `luo_file.c` 内部之前，必须先理解它的"扩展点"长什么样——内核子系统
通过实现一组回调函数、注册一个 `compatible` 字符串，就能把"如何保存/恢复
我这一类文件"的知识注入到 LUO 框架中：

```c
struct liveupdate_file_ops {
        bool (*can_preserve)(struct liveupdate_file_handler *handler,
                             struct file *file);
        int (*preserve)(struct liveupdate_file_op_args *args);
        void (*unpreserve)(struct liveupdate_file_op_args *args);
        int (*freeze)(struct liveupdate_file_op_args *args);
        void (*unfreeze)(struct liveupdate_file_op_args *args);
        int (*retrieve)(struct liveupdate_file_op_args *args);
        bool (*can_finish)(struct liveupdate_file_op_args *args);
        void (*finish)(struct liveupdate_file_op_args *args);
        unsigned long (*get_id)(struct file *file);
        struct module *owner;
};
```
（`include/linux/liveupdate.h:73-85`）

下表是这套回调矩阵的"职责说明书"，按"必需/可选"和"调用时机"两个维度梳理
（数据来自 `include/linux/liveupdate.h:55-68` 与 `luo_file.c:25-42` 两处
DOC 注释的交叉印证）：

| 回调 | 必需? | 调用时机 | 一句话职责 | 可能的失败后果 |
|---|---|---|---|---|
| `can_preserve` | **必需** | `preserve_fd` 时，遍历 handler 列表逐个尝试 | 轻量级匹配检查："这个 fd 是不是我能处理的类型？" | 返回 `false` 即跳过，继续尝试下一个 handler |
| `preserve` | **必需** | `preserve_fd` 时，匹配成功后立即调用 | 重量级状态保存："把这个文件的状态记下来，给我一个 `serialized_data` 句柄" | 失败则整个 `luo_preserve_file` 回滚（见 6.4 节） |
| `unpreserve` | **必需** | (a) 用户主动放弃（关闭 session fd）；(b) `preserve` 链路中途失败回滚 | 撤销 `preserve` 分配的一切资源 | 此回调本身不允许失败（返回 `void`）——这是"撤销动作不能再失败"原则的体现 |
| `freeze` | 可选 | `liveupdate_reboot()` 触发的序列化阶段，**已进入 reboot 系统调用、用户态无法再修改文件** | 最后一次"定格快照"机会，可更新 `serialized_data` | 失败则触发 `unfreeze` 回滚链（见 6.6 节），整个 kexec 被取消 |
| `unfreeze` | 可选 | `freeze` 失败需要回滚，或 LUO 在 `freeze` 全部成功后因其他原因（如 `luo_session_serialize` 中其它会话失败）需要整体撤销时 | 撤销 `freeze` 的效果，把文件带回"正常运行"状态 | 同 `unpreserve`，不允许失败 |
| `retrieve` | **必需** | 新内核中用户态调用 `LIVEUPDATE_SESSION_RETRIEVE_FD` 时，**懒加载**、按需触发 | 依据 `serialized_data` 重建出一个全新的 `struct file *` | 失败码被缓存进 `retrieve_status`（见 6.7 节的幂等设计） |
| `can_finish` | 可选 | `finish` 真正执行前的"预检"阶段，对 file_set 中所有文件统一调用一遍 | 回答"我现在可以被收尾吗？"（例如还有未完成的恢复前置条件） | 返回 `false` 则整个 `finish` 操作中止，不会产生任何副作用——可安全重试 |
| `finish` | **必需** | 在 `can_finish` 全部通过之后，真正执行收尾清理 | 释放保存期间占用的资源、放弃对底层对象的引用 | 此回调本身不允许失败（返回 `void`）——"两阶段提交"中真正提交的步骤不能半途而废 |
| `get_id` | 可选 | `luo_preserve_file`/`luo_retrieve_file` 内部需要往全局 xarray 里登记/查找文件时 | 返回一个能代表该文件的唯一标识；缺省退化为用 `(unsigned long)file` 指针值 | 不存在失败，是一个纯函数 |
| `owner` | **必需** | 整个文件处于"被保存"状态期间 | 模块引用计数锚点（`try_module_get`/`module_put`） | 防止处理某类文件期间，对应内核模块被卸载导致回调函数指针悬空 |

观察这张表，会发现一条贯穿性的设计纪律：**"做事"的回调（`preserve`/`freeze`/
`retrieve`）允许失败并返回错误码，而"撤销/收尾"的回调（`unpreserve`/
`unfreeze`/`finish`）一律返回 `void`，不允许失败**。这不是疏忽，而是一种
刻意的契约：如果"撤销"本身还可能失败，那么错误处理逻辑就需要"撤销的撤销"，
无穷递归下去永无宁日。LUO 把"撤销动作必须总是成功"作为一条公理写进了类型
系统（用返回值类型 `void` 固化下来），逼迫所有 handler 的实现者必须把
"撤销路径"设计成无条件成功的——这通常意味着把容易失败的操作（内存分配、
I/O）挪到"做事"阶段提前完成或预留好资源，"撤销"阶段只做无条件能成功的简单
释放动作。这是接口设计"用类型系统强制正确性"的优秀范例。

`liveupdate_file_op_args` 则是所有回调共享的"参数包"：

```c
struct liveupdate_file_op_args {
        struct liveupdate_file_handler *handler;
        int retrieve_status;
        struct file *file;
        u64 serialized_data;
        void *private_data;
};
```
（`include/linux/liveupdate.h:45-51`）

把多个回调的参数统一收纳进一个结构体，而不是让每个回调都有自己专属的参数
列表，换来的是 `luo_file.c` 里所有调用点都可以用几乎相同的"填参数→调用→
回写结果"模板代码（参见 6.4-6.8 节里反复出现的 `args.handler = ...; args.file
= ...;` 套路）——这是一种"接口一致性优先于参数精简"的取舍：牺牲了一点点
"每个回调只看到它真正需要的字段"的纯粹性，换来了调用方代码的高度统一和
可复制粘贴性，大大降低了在框架内新增一个回调时的心智负担。

### 6.2 `struct luo_file`：单个被保存文件的运行时表示

```c
struct luo_file {
        struct liveupdate_file_handler *fh;
        struct file *file;
        u64 serialized_data;
        void *private_data;
        int retrieve_status;
        struct mutex mutex;
        struct list_head list;
        u64 token;
};
```
（`luo_file.c:166-175`）

逐字段对照其在生命周期不同阶段的"取值演变"，能帮助建立起对这个结构体
"动态画像"的理解：

```text
              preserve 阶段       freeze 阶段        deserialize 阶段     retrieve 阶段成功后
fh            指向匹配的 handler   不变               重新按 compatible    不变
                                                      字符串查到的 handler
file          fget() 来的旧文件   不变               NULL（尚未恢复）     新 fget 出来的文件
serialized_data preserve() 给出的  freeze() 可能       从 file_ser[i].data  retrieve() 可能更新
                初始句柄           更新这个句柄        恢复出来            （理论上之后不再变）
private_data  preserve() 分配     不变               NULL（运行时态，    retrieve() 重新分配
                                                      不跨 kexec 保存）
retrieve_status  0（未涉及恢复）   0                  0                   1（成功）或 <0（失败）
token         用户传入的值        不变               从 file_ser[i].token  不变
                                                      恢复出来
```

这张表揭示了一个重要事实——`luo_file` 实际上承担着**两种截然不同的"角色"**：
在旧内核里，它是"我正在跟踪一个活跃文件，并记录如何描述它"的句柄；在新内核里
（反序列化刚完成、尚未 `retrieve` 时），它退化为一个"只有元数据、`file` 字段
是 `NULL` 的占位符"，只有当用户态主动 `retrieve` 时才会"实体化"成一个真正
持有 `struct file *` 引用的活跃对象。`private_data` 字段的命运尤其能说明
问题——文档注释专门强调它"used to hold runtime state **that is not
preserved**"（`luo_file.c:137-138`）：它在旧内核 `preserve()` 时被分配，
在 `unpreserve`/`unfreeze` 时被释放，**永远不会被序列化进 `luo_file_ser`**
——它纯粹是"这一代内核运行期间的辅助记录"，新内核里的同名字段会被
`retrieve()` 重新分配出一份全新的。这是"哪些状态需要跨 kexec 存活、哪些
状态只是本代运行时的辅助簿记"这一核心设计判断在数据结构层面的具体投影。

### 6.3 惰性内存分配：`luo_alloc_files_mem`/`luo_free_files_mem`

```c
/* 2 4K pages, give space for 128 files per file_set */
#define LUO_FILE_PGCNT          2ul
#define LUO_FILE_MAX                                                    \
        ((LUO_FILE_PGCNT << PAGE_SHIFT) / sizeof(struct luo_file_ser))

static int luo_alloc_files_mem(struct luo_file_set *file_set)
{
        size_t size;
        void *mem;

        if (file_set->files)
                return 0;

        WARN_ON_ONCE(file_set->count);

        size = LUO_FILE_PGCNT << PAGE_SHIFT;
        mem = kho_alloc_preserve(size);
        if (IS_ERR(mem))
                return PTR_ERR(mem);

        file_set->files = mem;

        return 0;
}

static void luo_free_files_mem(struct luo_file_set *file_set)
{
        /* If file_set has files, no need to free preservation memory */
        if (file_set->count)
                return;

        if (!file_set->files)
                return;

        kho_unpreserve_free(file_set->files);
        file_set->files = NULL;
}
```
（`luo_file.c:121-208`）

这一对函数看似平凡，实则体现了一种"按需分配、闲时归还"的资源管理策略——
回想第 3.1 节强调的"空 session 也是合法的"：如果一个 session 从未保存过
任何文件，它就完全不需要为 `luo_file_ser` 数组预留物理内存，`file_set->files`
始终是 `NULL`。只有当用户态第一次调用 `preserve_fd`、真正需要落盘元数据时，
才通过 `kho_alloc_preserve` 一次性分配整个 `LUO_FILE_PGCNT`（2 页 = 8KB，
容纳 `LUO_FILE_MAX`，约 128 个 `luo_file_ser`）大小的物理内存——这与 5.1
节看到的 `LUO_SESSION_MAX` 推导算式是同一种"从固定内存反推容量上限"的
范式在更小粒度上的复现。

`luo_alloc_files_mem` 内部 `if (file_set->files) return 0;` 这行短路检查
让它具有"幂等"的特性——可以被反复安全调用而不会重复分配（`luo_preserve_file`
在每次保存新文件时都会调用它）。`WARN_ON_ONCE(file_set->count)` 则是一处
"不应该发生但万一发生了请告诉我"式的运行时断言：理论上 `count > 0` 必然
意味着 `files` 已经非空（因为没有内存就没法记录第一个文件），如果这个不变式
被打破，说明代码的某个角落出现了逻辑错误——用 `WARN_ON_ONCE` 而不是 `BUG_ON`
是因为这是一个"值得警觉但不需要让整个系统宕机"级别的异常。

`luo_free_files_mem` 与之呼应——只有当 `count` 归零（所有文件都已经被
`unpreserve` 撤销，回到"空 session"状态）时才真正释放内存。这意味着这块
内存的生命周期与"file_set 是否非空"严格绑定，而不是与"file_set 对象本身"
绑定——`luo_session_release()` 中调用 `luo_file_unpreserve_files()`
（撤销所有文件后，内部最后一步正是 `luo_free_files_mem`）展现了这个回收
路径的真实触发点。

### 6.4 `luo_preserve_file`：保存流程的完整走读与七层回滚链

这是整个 LUO 框架里"最复杂的单个函数"之一，因为它需要协调五种不同的资源
（fd 引用、模块引用、xarray 登记、FLB 引用计数、内存分配），并保证任意一步
失败都能精确地撤销之前所有已完成的步骤。完整代码如下（已在前文小节引用过
其声明，这里给出完整实现）：

```c
int luo_preserve_file(struct luo_file_set *file_set, u64 token, int fd)
{
        struct liveupdate_file_op_args args = {0};
        struct liveupdate_file_handler *fh;
        struct luo_file *luo_file;
        struct file *file;
        int err;

        if (luo_token_is_used(file_set, token))
                return -EEXIST;

        if (file_set->count == LUO_FILE_MAX)
                return -ENOSPC;

        file = fget(fd);
        if (!file)
                return -EBADF;

        err = luo_alloc_files_mem(file_set);
        if (err)
                goto  err_fput;

        err = -ENOENT;
        down_read(&luo_register_rwlock);
        list_private_for_each_entry(fh, &luo_file_handler_list, list) {
                if (fh->ops->can_preserve(fh, file)) {
                        if (try_module_get(fh->ops->owner))
                                err = 0;
                        break;
                }
        }
        up_read(&luo_register_rwlock);

        /* err is still -ENOENT if no handler was found */
        if (err)
                goto err_free_files_mem;

        err = xa_insert(&luo_preserved_files, luo_get_id(fh, file),
                        file, GFP_KERNEL);
        if (err)
                goto err_module_put;

        err = luo_flb_file_preserve(fh);
        if (err)
                goto err_erase_xa;

        luo_file = kzalloc_obj(*luo_file);
        if (!luo_file) {
                err = -ENOMEM;
                goto err_flb_unpreserve;
        }

        luo_file->file = file;
        luo_file->fh = fh;
        luo_file->token = token;
        mutex_init(&luo_file->mutex);

        args.handler = fh;
        args.file = file;
        err = fh->ops->preserve(&args);
        if (err)
                goto err_kfree;

        luo_file->serialized_data = args.serialized_data;
        luo_file->private_data = args.private_data;
        list_add_tail(&luo_file->list, &file_set->files_list);
        file_set->count++;

        return 0;

err_kfree:
        kfree(luo_file);
err_flb_unpreserve:
        luo_flb_file_unpreserve(fh);
err_erase_xa:
        xa_erase(&luo_preserved_files, luo_get_id(fh, file));
err_module_put:
        module_put(fh->ops->owner);
err_free_files_mem:
        luo_free_files_mem(file_set);
err_fput:
        fput(file);

        return err;
}
```
（`luo_file.c:268-352`）

把"建立资源的正向序列"与"对应的回滚标签序列"并排画出来，就能看清这个函数
最值得学习的地方——它是 Linux 内核里 `goto` 错误处理范式的教科书级范例：

```text
   正向建立顺序（每步成功才能继续）          失败时的回滚顺序（goto 标签，逆序展开）
  ──────────────────────────────────       ──────────────────────────────────
   ① fget(fd)                                err_fput:        → fput(file)
                                                                    ▲
   ② luo_alloc_files_mem()                  err_free_files_mem:   │  逆序对应
                                              → luo_free_files_mem()│  （后建立的
                                                                    │   先撤销）
   ③ 遍历 handler 列表，                     err_module_put:       │
     can_preserve() 匹配，                    → module_put(owner) │
     try_module_get()                                              │
                                                                   │
   ④ xa_insert(luo_preserved_files)          err_erase_xa:        │
                                              → xa_erase(...)     │
                                                                   │
   ⑤ luo_flb_file_preserve(fh)               err_flb_unpreserve: │
     （引用计数 +1，可能触发                   → luo_flb_file_     │
      该 FLB 的首次 preserve）                  unpreserve(fh)    │
                                                                   │
   ⑥ kzalloc luo_file                        err_kfree:           │
                                              → kfree(luo_file)   │
                                                                   │
   ⑦ fh->ops->preserve(&args)                (本步失败直接走 ⑥    │
     （调用 handler 的重量级保存逻辑）          的回滚标签，因为     │
                                               luo_file 已分配但    │
                                               尚未真正"生效"）     ▼
   ✓ 全部成功：把 luo_file 加入链表，count++
```

每一条 `goto` 标签都精确地"回退到上一步成功之前的状态"——这要求开发者在
编写时极度严谨地核对"我刚刚成功做了什么，如果接下来失败，应该撤销到哪一步"。
几个细节特别值得标注：

- **`err = -ENOENT;` 的预置初值**（`luo_file.c:290`）：在遍历 handler 列表
  之前先把 `err` 设为 `-ENOENT`，如果遍历完整个列表都没有 `can_preserve()`
  返回 `true` 的 handler，`err` 就会保持这个"找不到兼容处理器"的初值——这是
  一种"用变量的初值表达默认结论，循环体只在找到匹配时才覆盖它"的简洁写法，
  避免了用一个额外的 `bool found` 变量。

- **`try_module_get` 与 `can_preserve` 的耦合时机**（`luo_file.c:293-297`）：
  只有在 `can_preserve()` 已经确认"这个 handler 能处理这个文件"之后，才去
  尝试增加模块引用计数——如果颠倒顺序（先加引用计数、再检查能否处理），
  会在"不匹配"的高频路径上产生不必要的原子操作开销。但这也意味着如果
  `try_module_get` 失败（模块正在被卸载的竞态窗口），`err` 仍然保持 `-ENOENT`
  ——从用户态视角看，"handler 存在但模块正在卸载"和"handler 根本不存在"
  被等价地报告为"找不到兼容的处理器"，这是一处刻意的简化：没有必要为这个
  极小概率的竞态窗口设计专门的错误码，模块卸载本身就是一个需要管理员介入
  的罕见操作。

- **`luo_flb_file_preserve(fh)` 出现在 `xa_insert` 之后、`kzalloc luo_file`
  之前**（`luo_file.c:310`）：这个顺序背后是"先确保这个文件不会和别处重复
  保存（xarray 去重），再触发可能昂贵的 FLB 首次保存逻辑"的考量——如果
  顺序反过来，一个本应被 `-EBUSY`（重复保存）拒绝的请求，却可能先触发了
  FLB 的 `preserve()` 回调（比如真的去保存一份 IOMMU 状态），造成不必要的
  工作量和需要撤销的副作用。"先做便宜的检查，再做昂贵的操作"是性能与正确性
  并重的常见排序原则。

- **`fh->ops->preserve(&args)` 是序列中的最后一步、也是唯一一个"调用外部
  代码"的步骤**（`luo_file.c:327`）：它被特意安排在所有内部簿记
  （token 检查、容量检查、xarray 登记、FLB 引用、`luo_file` 分配）都已就绪
  之后才执行——这样万一外部 handler 的 `preserve()` 实现存在 bug 或者
  耗时极长，LUO 自身的内部状态已经处于"万事俱备，只欠东风"的稳定中间态，
  之后的回滚（`err_kfree`）只需要拆除框架自己搭建的脚手架，不涉及任何
  对外部子系统状态的复杂查询。

值得一提的是 `luo_file.c:235-238` 注释里强调的所有权语义——"Upon entry,
it takes a reference to the 'struct file' via fget()... This reference is
held until the file is either unpreserved or successfully finished in the
next kernel"。这意味着**从 `preserve_fd` 调用成功的那一刻起，LUO 就成为了
这个文件的"共同所有者"**——即便用户态完全关闭了它自己持有的那个 fd，文件
对象也不会被销毁，因为 LUO 通过 `fget()` 拿到的引用计数仍然存在。这正是
"被保存的资源在 kexec 之前必须保持存活"这一根本需求在引用计数层面的实现
——`fput()` 只会在 `unpreserve`（放弃保存）或 `finish`（在新内核里收尾）
时才被调用。

### 6.5 `luo_file_unpreserve_files`：会话被放弃时的统一清理出口

```c
void luo_file_unpreserve_files(struct luo_file_set *file_set)
{
        struct luo_file *luo_file;

        while (!list_empty(&file_set->files_list)) {
                struct liveupdate_file_op_args args = {0};

                luo_file = list_last_entry(&file_set->files_list,
                                           struct luo_file, list);

                args.handler = luo_file->fh;
                args.file = luo_file->file;
                args.serialized_data = luo_file->serialized_data;
                args.private_data = luo_file->private_data;
                luo_file->fh->ops->unpreserve(&args);
                luo_flb_file_unpreserve(luo_file->fh);
                module_put(luo_file->fh->ops->owner);

                xa_erase(&luo_preserved_files,
                         luo_get_id(luo_file->fh, luo_file->file));
                list_del(&luo_file->list);
                file_set->count--;

                fput(luo_file->file);
                mutex_destroy(&luo_file->mutex);
                kfree(luo_file);
        }

        luo_free_files_mem(file_set);
}
```
（`luo_file.c:372-401`）

把这个函数与 6.4 节 `luo_preserve_file` 成功路径上建立资源的顺序对照，
会发现它执行的正是**完全逆序**的拆除动作——`unpreserve → flb_unpreserve →
module_put → xa_erase → list_del/count-- → fput → mutex_destroy → kfree`，
对应 `preserve → flb_preserve → module_get（隐含在 try_module_get 中）→
xa_insert → list_add/count++ → fget（隐含）→ mutex_init → kzalloc`
的反向操作。这种"批量清理函数严格复现单个对象建立时的逆序"的写法，再次
印证了 5.2 节总结的"构造与析构对称"哲学在更大粒度（整个集合的清理）上的
延续。

选择 `list_last_entry`（**从链表尾部**取出元素）而不是从头部，也并非随意——
`files_list` 是按"保存时间的先后顺序"用 `list_add_tail` 构建的 FIFO 队列
（见 `luo_file.c:46`/`333` 的注释与代码），从尾部撤销，相当于"后保存的
先撤销"——这是一种"后进先出"（LIFO）式的逆序清理。这个顺序选择背后隐含的
假设是：**后保存的资源更可能依赖于先保存的资源**（类似于 C++ 析构函数按
构造的逆序执行）。虽然在当前 memfd 这个简单 handler 的场景下，多个文件之间
互不依赖，这个顺序选择看不出明显差异；但当 VFIO/IOMMU 这类存在复杂依赖链
（设备依赖于 IOMMU 域，IOMMU 域依赖于地址空间）的 handler 接入之后，
"后建立的先拆除"就会成为避免"依赖倒置"式错误的关键保障——这是一处
"为尚未到来的复杂场景预先选对顺序"的前瞻性设计。

### 6.6 冻结与解冻：`freeze`/`unfreeze` 状态机及其回滚链

冻结相关的四个函数构成了一套精巧的"原子操作 + 回滚"组合，本节把它们放在
一起对照阅读：

```c
static int luo_file_freeze_one(struct luo_file_set *file_set,
                               struct luo_file *luo_file)
{
        int err = 0;

        guard(mutex)(&luo_file->mutex);

        if (luo_file->fh->ops->freeze) {
                struct liveupdate_file_op_args args = {0};
                ...
                err = luo_file->fh->ops->freeze(&args);
                if (!err)
                        luo_file->serialized_data = args.serialized_data;
        }

        return err;
}
```
（`luo_file.c:403-424` 节选）

```c
static void __luo_file_unfreeze(struct luo_file_set *file_set,
                                struct luo_file *failed_entry)
{
        struct list_head *files_list = &file_set->files_list;
        struct luo_file *luo_file;

        list_for_each_entry(luo_file, files_list, list) {
                if (luo_file == failed_entry)
                        break;

                luo_file_unfreeze_one(file_set, luo_file);
        }

        memset(file_set->files, 0, LUO_FILE_PGCNT << PAGE_SHIFT);
}
```
（`luo_file.c:443-457`）

`__luo_file_unfreeze` 这个"双下划线前缀"的辅助函数是整套冻结/解冻机制的
枢纽，它通过一个精妙的参数设计**同时服务于两种调用场景**：

| 调用方 | 传入的 `failed_entry` | 行为 |
|---|---|---|
| `luo_file_freeze()` 失败时（`err_unfreeze`） | 指向"刚刚冻结失败的那个 `luo_file`" | 从链表头开始遍历，**遇到失败的那个就停下**——只解冻"已经成功冻结"的那些（FIFO 顺序中位于失败点之前的所有项） |
| `luo_file_unfreeze()`（外部因素导致整体撤销，例如另一个 session 冻结失败） | `NULL` | 因为没有任何 `luo_file` 指针会等于 `NULL`，循环会遍历**整个**链表——解冻所有已经成功冻结的文件 |

`if (luo_file == failed_entry) break;` 这一行是整个技巧的核心——用"比较
是否等于一个哨兵指针值"来统一表达"提前终止"和"完整遍历"两种语义，避免了
写两份几乎相同的循环代码。这是 C 语言里"用特殊指针值（包括 `NULL`）作为
控制流哨兵"的经典手法，与字符串以 `'\0'` 结尾、链表以 `NULL` 终止是同一
设计语言的不同应用。

末尾的 `memset(file_set->files, 0, LUO_FILE_PGCNT << PAGE_SHIFT);` 同样
意味深长——它把整块序列化缓冲区清零，**不管刚才已经写入了多少个
`luo_file_ser` 条目**。这是因为一旦决定放弃这次冻结，序列化数组里此前写入
的部分数据就成了"垃圾"——与其费力气去判断"应该清除前 N 个还是后 M 个"，
不如把整块清零，确保它呈现出"从未被使用过"的初始状态，让下一次（可能是
重试的）`luo_file_freeze` 调用可以从一个干净的起点重新开始。这是"宁可
多做一点确定无误的工作，也不要冒险做'恰到好处但容易出错'的精细操作"
原则的又一次体现。

```c
int luo_file_freeze(struct luo_file_set *file_set,
                    struct luo_file_set_ser *file_set_ser)
{
        struct luo_file_ser *file_ser = file_set->files;
        struct luo_file *luo_file;
        int err;
        int i;

        if (!file_set->count)
                return 0;

        if (WARN_ON(!file_ser))
                return -EINVAL;

        i = 0;
        list_for_each_entry(luo_file, &file_set->files_list, list) {
                err = luo_file_freeze_one(file_set, luo_file);
                if (err < 0) {
                        pr_warn("Freeze failed for token[%#0llx] handler[%s] err[%pe]\n",
                                luo_file->token, luo_file->fh->compatible,
                                ERR_PTR(err));
                        goto err_unfreeze;
                }

                strscpy(file_ser[i].compatible, luo_file->fh->compatible,
                        sizeof(file_ser[i].compatible));
                file_ser[i].data = luo_file->serialized_data;
                file_ser[i].token = luo_file->token;
                i++;
        }

        file_set_ser->count = file_set->count;
        if (file_set->files)
                file_set_ser->files = virt_to_phys(file_set->files);

        return 0;

err_unfreeze:
        __luo_file_unfreeze(file_set, luo_file);

        return err;
}
```
（`luo_file.c:492-533`）

这个函数把"调用 handler 的 `freeze` 回调"和"把结果序列化进物理内存数组"
两件事**揉进同一次遍历**完成——`luo_file_freeze_one()` 成功返回后，立即
执行 `strscpy`/赋值三连击，把 `compatible`/`data`/`token` 写入
`file_ser[i]`。这种"边做边记"的写法效率很高（避免了二次遍历），但也对
错误处理提出了更高要求——一旦中途失败，**已经写入的 `file_ser[]` 条目
也是"半成品"**，这正是上面 `__luo_file_unfreeze` 末尾要把整块缓冲区
`memset` 清零的原因：它不仅要解冻 handler 状态，还要清理掉这些已经写脏的
序列化数据残留。

`file_set_ser->count = file_set->count;` 与 `file_set_ser->files =
virt_to_phys(file_set->files);` 这两行只在循环**完全成功**之后才执行
——这是 5.7 节里 `sh->header_ser->count = sh->count;` 同款"全有或全无"
原子性保证模式在文件层级的复现：外部观察者（FDT 解析逻辑、新内核的
反序列化代码）要么看到一个完整、自洽的 `luo_file_set_ser`（`count` 与
`files` 物理地址同时有效），要么看到它保持着初始的全零状态——不存在
"`count` 非零但 `files` 为零"或反之的中间态。

### 6.7 `luo_retrieve_file`：懒加载与三态缓存的幂等设计

```c
int luo_retrieve_file(struct luo_file_set *file_set, u64 token,
                      struct file **filep)
{
        struct liveupdate_file_op_args args = {0};
        struct luo_file *luo_file;
        bool found = false;
        int err;

        if (list_empty(&file_set->files_list))
                return -ENOENT;

        list_for_each_entry(luo_file, &file_set->files_list, list) {
                if (luo_file->token == token) {
                        found = true;
                        break;
                }
        }

        if (!found)
                return -ENOENT;

        guard(mutex)(&luo_file->mutex);
        if (luo_file->retrieve_status < 0) {
                /* Retrieve was attempted and it failed. Return the error code. */
                return luo_file->retrieve_status;
        }

        if (luo_file->retrieve_status > 0) {
                /*
                 * Someone is asking for this file again, so get a reference
                 * for them.
                 */
                get_file(luo_file->file);
                *filep = luo_file->file;
                return 0;
        }

        args.handler = luo_file->fh;
        args.serialized_data = luo_file->serialized_data;
        err = luo_file->fh->ops->retrieve(&args);
        if (err) {
                /* Keep the error code for later use. */
                luo_file->retrieve_status = err;
                return err;
        }

        luo_file->file = args.file;
        /* Get reference so we can keep this file in LUO until finish */
        get_file(luo_file->file);

        WARN_ON(xa_insert(&luo_preserved_files,
                          luo_get_id(luo_file->fh, luo_file->file),
                          luo_file->file, GFP_KERNEL));

        *filep = luo_file->file;
        luo_file->retrieve_status = 1;

        return 0;
}
```
（`luo_file.c:586-644`）

这个函数最值得欣赏的设计是把 `retrieve_status` 这一个 `int` 字段，
变成了一台精确的"三态状态机"——这是一种"用一个数值的符号位与零值划分出
三种语义状态"的经典节省手法（类似于 POSIX 系统调用里"返回值 < 0 表示
错误码、>= 0 表示有效结果"的统一约定）：

```text
                         retrieve_status 三态图

         ┌──────────────┐
         │  status == 0  │   "尚未尝试恢复"
         │  (初始态，    │   ──┬── 用户第一次调用 retrieve_fd(token)
         │  反序列化刚   │     │
         │  完成时的值)  │     ▼
         └──────────────┘   调用 ops->retrieve()，分两种结果：
                │                  │
        ┌───────┴────────┐         │
        │ 失败 (err < 0) │         │ 成功 (err == 0)
        ▼                          ▼
  ┌──────────────┐         ┌──────────────────┐
  │ status = err  │         │ status = 1        │
  │  (负数，固化   │         │ (正数，固化       │
  │   错误结果)   │         │  成功结果)        │
  └──────┬───────┘         └─────────┬────────┘
         │                            │
         │ 此后任何 retrieve 调用      │ 此后任何 retrieve 调用
         │ 都直接返回这个              │ 都执行 get_file() 之后
         │ 缓存的错误码，               │ 直接返回同一个 luo_file->file
         │ *不会*重新调用              │ （引用计数 +1，相当于"再借一次"）
         │ ops->retrieve()             │
         ▼                            ▼
    "死结"——一旦失败就            "活结"——可以被无限次
    永久失败，不会自动重试         安全地重复 retrieve，
    （这与 5.8 节                  每次都返回同一个底层
    luo_session_deserialize        对象的新引用
    的"saved_err"缓存如出一辙）
```

这一设计直接服务于 DOC 注释里强调的关键能力——"**The operation is
idempotent**"和"**Files can be retrieved in ANY order**"
（`luo_file.c:572-578`）。"任意顺序恢复"意味着用户态完全可以先恢复 token
为 `0x3003` 的文件，再恢复 `0x1001`——内核不对恢复顺序做任何强制要求；
"幂等"则意味着用户态即便因为某种原因（程序重启、多个线程并发尝试）对
同一个 token 调用了多次 `retrieve_fd`，也总能得到一致、正确的结果，不会
因为"重复执行了本应只执行一次的重量级操作"而产生资源泄漏或状态错乱。

`get_file(luo_file->file)` 在两个分支里各出现一次（缓存命中分支与首次
恢复成功分支），都精确对应着"调用方将获得一个独立的引用计数"——这与
`fd_install` 把 `struct file *` 安装到调用者 fd 表中的语义吻合：每一次
`retrieve_fd` 成功，调用者 fd 表里就多一个独立的、可以独立 `close` 的 fd，
而它们底层共享同一个 `struct file`。`xa_insert` 把恢复出来的文件重新登记
进全局 `luo_preserved_files` xarray——这是为了在新内核里继续维护"防止
重复保存"的不变式（如果用户态决定把这个刚刚恢复出来的文件**再次**
preserve 进另一个 outgoing session，`luo_preserve_file` 里的 `xa_insert`
检查依然能正确发现它已经被 LUO 跟踪）。`WARN_ON` 包裹这次插入，是因为
按照不变式它**理应**总是成功——如果插入失败（意味着这个 ID 已经存在），
说明出现了逻辑错误（比如同一个底层对象被两个不同的 token 同时跟踪），
值得用 `WARN_ON` 大声告警。

### 6.8 `luo_file_finish`：两阶段提交式的批量收尾

```c
static int luo_file_can_finish_one(struct luo_file_set *file_set,
                                   struct luo_file *luo_file)
{
        bool can_finish = true;

        guard(mutex)(&luo_file->mutex);

        if (luo_file->fh->ops->can_finish) {
                struct liveupdate_file_op_args args = {0};
                ...
                args.retrieve_status = luo_file->retrieve_status;
                can_finish = luo_file->fh->ops->can_finish(&args);
        }

        return can_finish ? 0 : -EBUSY;
}
```
（`luo_file.c:646-664` 节选）

```c
int luo_file_finish(struct luo_file_set *file_set)
{
        struct list_head *files_list = &file_set->files_list;
        struct luo_file *luo_file;
        int err;

        if (!file_set->count)
                return 0;

        list_for_each_entry(luo_file, files_list, list) {
                err = luo_file_can_finish_one(file_set, luo_file);
                if (err)
                        return err;
        }

        while (!list_empty(&file_set->files_list)) {
                luo_file = list_last_entry(&file_set->files_list,
                                           struct luo_file, list);

                luo_file_finish_one(file_set, luo_file);

                if (luo_file->file) {
                        xa_erase(&luo_preserved_files,
                                 luo_get_id(luo_file->fh, luo_file->file));
                        fput(luo_file->file);
                }
                list_del(&luo_file->list);
                file_set->count--;
                mutex_destroy(&luo_file->mutex);
                kfree(luo_file);
        }

        if (file_set->files) {
                kho_restore_free(file_set->files);
                file_set->files = NULL;
        }

        return 0;
}
```
（`luo_file.c:715-753`）

这是整章里最值得单独拎出来讲的设计模式——**"先全体检查、再统一提交"的
两阶段提交（Two-Phase Commit）**：

```text
   阶段一：检查（CHECK）——只读，不产生任何副作用，可以安全地中途失败退出
  ┌────────────────────────────────────────────────────────────────┐
  │  for each luo_file in files_list:                                │
  │      if !can_finish(luo_file):                                   │
  │          return -EBUSY   ← 任何一个文件没准备好，整个操作立即中止   │
  │                            此时还*没有*修改任何状态、释放任何资源   │
  └────────────────────────────────────────────────────────────────┘
                                  │
                          全部通过检查
                                  ▼
   阶段二：提交（COMMIT）——写操作，逐个执行 finish 并立即释放资源，
                            一旦开始就不再回头
  ┌────────────────────────────────────────────────────────────────┐
  │  while !list_empty(files_list):                                  │
  │      luo_file = 取链表尾部元素（LIFO，后保存的先收尾）              │
  │      finish_one(luo_file)   → 调用 handler.finish()（void，      │
  │                                不允许失败）                       │
  │      xa_erase + fput + list_del + count-- + kfree                │
  └────────────────────────────────────────────────────────────────┘
```

这个模式精确地解决了文档注释里描述的那个棘手问题——"**如果在收尾过程
进行到一半时失败了怎么办？**"。答案是：**让它在能够失败的地方（检查阶段）
提前失败，而把不允许失败的操作（`finish` 回调本身被定义为 `void`，见 6.1
节的回调矩阵）都集中在"已经确定能够成功"的提交阶段**。这样就规避了
"提交一半时失败导致部分文件已清理、部分还保留"的最危险局面——`finish`
失败的唯一途径是"检查阶段就被拒绝"，而检查阶段不产生任何副作用，因此
失败之后的状态与失败之前完全一致，UAPI 文档里"finish 可以安全重试"
（4.3.3 节引用过的注释）正是建立在这个不变式之上。

`luo_file_finish_one` 里调用顺序也值得留意——`finish()` 回调先于
`luo_flb_file_finish()` 和 `module_put()`：

```c
static void luo_file_finish_one(struct luo_file_set *file_set,
                                struct luo_file *luo_file)
{
        ...
        luo_file->fh->ops->finish(&args);
        luo_flb_file_finish(luo_file->fh);
        module_put(luo_file->fh->ops->owner);
}
```

这是因为 `finish()` 回调内部很可能需要访问由 FLB 管理的共享对象（例如
一个 VFIO 设备在最终释放前可能需要查询它所属 IOMMU 域的状态），所以
"通知 FLB 这是最后一个依赖者、可以触发其 `finish` 清理逻辑"必须排在
具体文件的 `finish()` 之后；而 `module_put` 排在最后，是为了保证在
整个收尾过程中——包括调用 `fh->ops->finish` 和 `luo_flb_file_finish`
期间——handler 所属的内核模块都不会被卸载，回调函数指针始终有效。

### 6.9 `luo_file_deserialize`：在新内核里凭空重建文件元数据列表

```c
int luo_file_deserialize(struct luo_file_set *file_set,
                         struct luo_file_set_ser *file_set_ser)
{
        struct luo_file_ser *file_ser;
        u64 i;

        if (!file_set_ser->files) {
                WARN_ON(file_set_ser->count);
                return 0;
        }

        file_set->count = file_set_ser->count;
        file_set->files = phys_to_virt(file_set_ser->files);

        /* Note on error handling: ...（与 5.8 节完全相同的"宁可泄漏"哲学注释）... */

        file_ser = file_set->files;
        for (i = 0; i < file_set->count; i++) {
                struct liveupdate_file_handler *fh;
                bool handler_found = false;
                struct luo_file *luo_file;

                down_read(&luo_register_rwlock);
                list_private_for_each_entry(fh, &luo_file_handler_list, list) {
                        if (!strcmp(fh->compatible, file_ser[i].compatible)) {
                                if (try_module_get(fh->ops->owner))
                                        handler_found = true;
                                break;
                        }
                }
                up_read(&luo_register_rwlock);

                if (!handler_found) {
                        pr_warn("No registered handler for compatible '%.*s'\n", ...);
                        return -ENOENT;
                }

                luo_file = kzalloc_obj(*luo_file);
                if (!luo_file) {
                        module_put(fh->ops->owner);
                        return -ENOMEM;
                }

                luo_file->fh = fh;
                luo_file->file = NULL;
                luo_file->serialized_data = file_ser[i].data;
                luo_file->token = file_ser[i].token;
                mutex_init(&luo_file->mutex);
                list_add_tail(&luo_file->list, &file_set->files_list);
        }

        return 0;
}
```
（`luo_file.c:780-847`，节选并省略了与 5.8 节完全相同的"宁可泄漏"哲学注释）

这个函数与 6.4 节的 `luo_preserve_file` 形成了一组绝佳的"正反对照"——
两者都需要"按 compatible/匹配条件找到 handler、增加模块引用、分配
`luo_file`"，但前者是在"旧内核里把活跃文件转换成元数据"，后者是在
"新内核里把元数据转换回（暂时还是空壳的）`luo_file` 占位符"：

| 对比维度 | `luo_preserve_file`（旧内核，preserve 时） | `luo_file_deserialize`（新内核，反序列化时） |
|---|---|---|
| handler 匹配方式 | 调用 `can_preserve(fh, file)` 做运行时类型探测 | 直接用 `strcmp` 比较 `compatible` 字符串——因为序列化数据里已经记录了"上一代内核认为它是什么类型" |
| `file` 字段初值 | 指向 `fget()` 拿到的真实文件 | `NULL`——真实文件要等用户态调用 `retrieve_fd` 时才会被懒加载创建出来 |
| `serialized_data` 来源 | 由 `ops->preserve()` 回调生成（运行时产物） | 直接从 `file_ser[i].data` 读出（上一代内核 `freeze()` 阶段固化下来的"出厂设置"） |
| `private_data` | 由 `ops->preserve()` 分配并赋值 | 保持 `NULL`（不跨 kexec 传递，等待 `retrieve()` 重新分配） |
| token 来源 | 用户态通过 ioctl 显式传入 | 从 `file_ser[i].token` 读出（实质上是"原样传回"） |
| 失败时的处理 | 完整的逐步回滚（七层 `goto` 链） | **刻意不做任何清理**——直接返回错误码（"宁可泄漏"哲学，与第 5.8 节完全相同的设计取舍） |

最后一行对比是本节的点睛之笔——同样是"分配资源失败"，`luo_preserve_file`
不遗余力地实现了精确到每一步的回滚，而 `luo_file_deserialize` 却"放任自流"。
这绝不是代码质量的双重标准，而恰恰是 5.8 节论证过的那套哲学在文件层面的
忠实复现：`luo_preserve_file` 运行在旧内核里，此刻"回到没有这个文件之前"
是一个清晰、安全、有意义的状态；而 `luo_file_deserialize` 运行在新内核
启动早期，此刻除了"继续往前走、让用户态尽快发现问题并触发干净的重启"之外，
没有任何"退路"可言。两个函数像一对镜像，分别站在"可逆"与"不可逆"两侧，
用完全不同的姿态回应同一个问题——"失败了怎么办"。

### 6.10 `liveupdate_register_file_handler`：扩展点的注册协议

```c
int liveupdate_register_file_handler(struct liveupdate_file_handler *fh)
{
        struct liveupdate_file_handler *fh_iter;
        int err;

        if (!liveupdate_enabled())
                return -EOPNOTSUPP;

        /* Sanity check that all required callbacks are set */
        if (!fh->ops->preserve || !fh->ops->unpreserve || !fh->ops->retrieve ||
            !fh->ops->finish || !fh->ops->can_preserve) {
                return -EINVAL;
        }

        down_write(&luo_register_rwlock);
        /* Check for duplicate compatible strings */
        list_private_for_each_entry(fh_iter, &luo_file_handler_list, list) {
                if (!strcmp(fh_iter->compatible, fh->compatible)) {
                        pr_err("File handler registration failed: Compatible string '%s' already registered.\n", ...);
                        err = -EEXIST;
                        goto err_unlock;
                }
        }

        INIT_LIST_HEAD(&ACCESS_PRIVATE(fh, flb_list));
        INIT_LIST_HEAD(&ACCESS_PRIVATE(fh, list));
        list_add_tail(&ACCESS_PRIVATE(fh, list), &luo_file_handler_list);
        up_write(&luo_register_rwlock);

        liveupdate_test_register(fh);

        return 0;
        ...
}
```
（`luo_file.c:872-909`）

三层校验依次展开，构成了一套"层层设防"的输入验证链：

1. **`liveupdate_enabled()`**——最外层的开关：如果整个 LUO 子系统因为
   `liveupdate=0` 命令行参数或 KHO 不可用而被禁用，那么注册请求直接被
   拒绝（`-EOPNOTSUPP`）。这避免了在一个"已知不会被使用"的框架里维护
   一份 handler 列表所带来的无谓开销和复杂度。

2. **回调完整性检查**（`luo_file.c:881-884`）——在**编译期类型系统**
   之外，再加一层**运行时契约检查**：`preserve`/`unpreserve`/`retrieve`/
   `finish`/`can_preserve` 这五个被标记为"必需"的回调（对照 6.1 节回调
   矩阵表）必须全部非空。这弥补了 C 语言里"函数指针字段允许为 `NULL`"
   这一类型系统的天然缺陷——如果不做这层检查，一个粗心的 handler 实现者
   可能注册了一个缺少 `finish` 回调的 handler，等到真正调用时才在
   `luo_file_finish_one` 里触发空指针解引用而 panic。把这类错误的发现
   时机从"运行时（可能是几个月后系统真正经历 live update 时）"前移到
   "注册时（模块加载阶段）"，是显著提升系统健壮性的"提前失败"
   （fail fast）实践。

3. **`compatible` 字符串唯一性检查**（`luo_file.c:888-895`）——确保
   "compatible 字符串 → handler" 这个映射在全局范围内是单射的。这是
   整个 ABI 设计能够正常工作的根本前提——如果两个 handler 注册了相同的
   `compatible` 字符串，`luo_file_deserialize` 在按字符串查找 handler 时
   就会产生歧义，可能把一类文件的序列化数据错误地交给另一类 handler 处理
   ——这是一类极其隐蔽、可能在恢复阶段才暴露、且后果可能是数据损坏的
   "类型混淆"漏洞，在注册时就把它扼杀在摇篮里是最经济的防御位置。

`ACCESS_PRIVATE(fh, flb_list)`/`ACCESS_PRIVATE(fh, list)` 这一对宏调用
体现了 6.1 节里 `struct liveupdate_file_handler` 定义中 `/* private: */`
注释划出的"公开/私有"边界——`list`（链入全局 handler 列表）和 `flb_list`
（记录该 handler 依赖哪些 FLB）是只应由 LUO 核心代码触碰的内部簿记字段，
通过 `__private` 标注和 `ACCESS_PRIVATE` 宏强制要求"任何访问都必须显式
声明你知道自己在碰一个私有字段"——这是内核里用宏机制在 C 语言中模拟
"访问控制"的一种轻量级手段，比单纯的注释约定多了一层编译期可检查的强制力。

最后，`liveupdate_test_register(fh)` 是一处面向 `CONFIG_LIVEUPDATE_TEST`
配置的钩子（在 `luo_internal.h:111-115` 中可以看到它在未开启该配置时
退化为空操作）——这暗示了内核里存在一套专门用于"在不需要真正 kexec 的
情况下"对 LUO 框架本身做单元测试的基础设施，是测试驱动开发理念在内核
子系统设计阶段就被纳入考量的体现。

至此，我们已经把"一个文件如何穿越 preserve→freeze→deserialize→retrieve→
finish 这条完整生命线"的全部细节摸了一遍。下面这张总览图把本章涉及的
全部状态迁移、可能的回滚路径汇总在一张图上，作为本章的收束：

```text
                       ╔═══════════════════════════════╗
                       ║   旧内核（生产运行中）           ║
                       ╚═══════════════════════════════╝

  [不存在] ──preserve_fd──▶ [PRESERVED]  ──放弃(关闭session fd)──▶ [不存在]
                │                │            luo_file_unpreserve_files()
                │ luo_preserve_  │            (调用 unpreserve 回调)
                │ file() 失败     │
                ▼                │
            [不存在]             │ liveupdate_reboot()
       (七层回滚链               │ → luo_session_serialize()
        精确复原)                │   → luo_file_freeze()
                                 ▼
                          [FROZEN] ──freeze 失败──▶ [PRESERVED]
                                │                  __luo_file_unfreeze()
                                │                  (调用 unfreeze 回调，
                                │                   kexec 被取消)
                                │
                       ════════ kexec 发生 ═══════
                                │
                       ╔═══════════════════════════════╗
                       ║   新内核（刚启动）               ║
                       ╚═══════════════════════════════╝
                                │
                                ▼ luo_file_deserialize()
                      [DESERIALIZED]（占位符，file == NULL）
                                │
                                │ retrieve_fd(token)
                                ▼
                ┌───────────────┴────────────────┐
                │                                 │
       ops->retrieve() 成功                ops->retrieve() 失败
                ▼                                 ▼
        [RETRIEVED]                      [RETRIEVE_FAILED]
   retrieve_status = 1                retrieve_status = err < 0
   (可重复 get_file()，                 (此后任何 retrieve 调用
    幂等地返回同一对象)                   都直接返回缓存的错误码)
                │                                 │
                └────────────┬────────────────────┘
                             │ session_finish
                             ▼
                  luo_file_finish()
                  (两阶段提交：can_finish 全检查通过
                   → finish_one 逐个收尾)
                             │
                             ▼
                         [不存在]
              (fput/xa_erase/list_del/kfree，
               LUO 放弃对该文件的全部所有权)
```

---

## 第 7 章 FLB 全局共享对象源码精读

> 本章源码：`kernel/liveupdate/luo_flb.c`（666 行）、
> `include/linux/liveupdate.h`（FLB 相关结构体定义）、
> `include/linux/kho/abi/luo.h`（FLB 序列化 ABI）

### 7.1 为什么需要 FLB：从"每个文件独立保存"到"全局共享一份"

回顾第 6 章：`luo_file` 体系解决的是"**单个文件对象**如何在 kexec 之后复活"的问题——
每个 `struct file` 都有自己独立的 `data`/`obj`/`token`，preserve 与 retrieve 都是
"各管各的"。但现实中的子系统并不总是这样组织的。

设想 VFIO（这正是 LUO 文档反复提到的目标用例）：一台云主机上可能同时打开了
几十个 `/dev/vfio/devices/vfioX` 文件描述符，分别对应几十个直通设备。但这些
设备背后**共享着同一份 IOMMU 页表、同一个 IOMMU 域、同一份中断重映射状态**——
这份状态只属于"VFIO 子系统"这个整体，不属于任何一个具体的设备文件。如果照搬
`luo_file` 的模型，会出现两个尴尬的问题：

1. **重复保存**：50 个 VFIO 文件每个都尝试 `kho_preserve` 一遍 IOMMU 页表，
   不仅浪费内存，多份序列化数据之间还可能出现不一致（这台设备保存时页表是状态 A，
   那台设备保存时已经变成状态 B）。
2. **生命周期错配**：IOMMU 域必须"**在第一个依赖它的设备文件被保存之前**建立好，
   在**最后一个依赖它的设备文件 finish 之后**才能销毁"——这是一个典型的
   **引用计数**问题，而不是某个文件自己的私有状态问题。

`struct liveupdate_flb`（File-Lifecycle-Bound，文件生命周期绑定的全局对象）就是为了
解决这一类"被多个可保存文件共享、生命周期由这些文件的存亡边界决定"的全局状态而设计的。
源码开头的 DOC 注释精确地概括了这个模型（`luo_flb.c:8-37`）：

```c
/**
 * DOC: LUO File Lifecycle Bound Global Data
 *
 * File-Lifecycle-Bound (FLB) objects provide a mechanism for managing global
 * state that is shared across multiple live-updatable files. The lifecycle of
 * this shared state is tied to the preservation of the files that depend on it.
 *
 * An FLB represents a global resource, such as the IOMMU core state, that is
 * required by multiple file descriptors (e.g., all VFIO fds).
 *
 * The preservation of the FLB's state is triggered when the *first* file
 * depending on it is preserved. The cleanup of this state (unpreserve or
 * finish) is triggered when the *last* file depending on it is unpreserved or
 * finished.
 *
 * Handler Dependency: A file handler declares its dependency on one or more
 * FLBs by registering them via liveupdate_register_flb().
 *
 * Callback Model: Each FLB is defined by a set of operations
 * (&struct liveupdate_flb_ops) that LUO invokes at key points:
 *
 *     - .preserve(): Called for the first file. Saves global state.
 *     - .unpreserve(): Called for the last file (if aborted pre-reboot).
 *     - .retrieve(): Called on-demand in the new kernel to restore the state.
 *     - .finish(): Called for the last file in the new kernel for cleanup.
 *
 * This reference-counted approach ensures that shared state is saved exactly
 * once and restored exactly once, regardless of how many files depend on it,
 * and that its lifecycle is correctly managed across the kexec transition.
 */
```

用一句话提炼这段注释的核心设计思想：**FLB 把"何时保存/何时销毁全局状态"这个决策，
从"子系统自己判断"下放给"LUO 通过引用计数自动判断"**。子系统只需要回答
"如何保存/如何恢复/如何销毁"（即实现四个回调），"什么时候做"完全由 LUO 根据依赖它的
文件数量自动触发——这正是 `luo_file` 的 preserve/freeze/retrieve/finish 状态机
在"全局对象"维度上的投影。

把 FLB 放进第 3 章的三层模型图里，它的位置是这样的：

```
              ┌─────────────────────────────┐
              │   liveupdate_file_handler   │   "VFIO 设备文件"类型描述符
              │   (compatible="vfio-v1")    │
              └──────────────┬──────────────┘
                             │ flb_list（私有链表，1 对多）
                ┌────────────┼────────────┐
                ▼            ▼            ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │   FLB    │  │   FLB    │  │   FLB    │   "IOMMU 域" / "中断重映射表"
        │ (iommu)  │  │ (irq)    │  │ (kvm)    │   / "KVM vIOMMU 状态" ...
        └──────────┘  └──────────┘  └──────────┘
                ▲ 多个文件实例共享同一份 FLB 状态（引用计数 count）
        ┌───────┴────────┬────────────────┐
   vfio fd #1        vfio fd #2       vfio fd #3
  (luo_file 实例)   (luo_file 实例)   (luo_file 实例)
```

可以看到，FLB 与文件之间是 **N : M** 的依赖关系：一个文件 handler 可以依赖多个 FLB
（`flb_list` 是一个链表），同一个 FLB 也可以被多个文件 handler 依赖（FLB 自身维护
全局链表 `luo_flb_global.list`）。这个"多对多 + 引用计数"的拓扑，决定了本章接下来
要解读的几乎所有数据结构和函数都是围绕"计数何时归零、归零时该做什么"展开的。

### 7.2 核心数据结构全景

FLB 子系统的数据结构可以分为两组：一组描述"**FLB 定义本身**"（子系统静态定义、
注册到 LUO 的对象），另一组描述"**LUO 内部如何组织/索引这些 FLB**"（运行时链表、
引用计数、序列化缓冲区）。我们先看全局视图，再逐个深入。

#### 7.2.1 子系统可见的部分：`liveupdate_flb` / `liveupdate_flb_ops` / `liveupdate_flb_op_args`

这三个结构体定义在 `include/linux/liveupdate.h:115-223`，是子系统编写者唯一需要
关心的"对外 API 表面"：

```c
struct liveupdate_flb_op_args {
        struct liveupdate_flb *flb;
        u64 data;
        void *obj;
};

struct liveupdate_flb_ops {
        int (*preserve)(struct liveupdate_flb_op_args *argp);
        void (*unpreserve)(struct liveupdate_flb_op_args *argp);
        int (*retrieve)(struct liveupdate_flb_op_args *argp);
        void (*finish)(struct liveupdate_flb_op_args *argp);
        struct module *owner;
};

struct liveupdate_flb {
        const struct liveupdate_flb_ops *ops;
        const char compatible[LIVEUPDATE_FLB_COMPAT_LENGTH];
        /* private: */
        struct luo_flb_private __private private;
};
```

这组定义与第 6 章 `liveupdate_file_handler`/`liveupdate_file_ops`/`liveupdate_file_op_args`
的设计如出一辙——都是"**公开只读的元信息（ops、compatible） + 一个 `__private` 修饰的内部状态**"
的组合，再次印证了 LUO 在多个子系统中坚持同一套设计语言：

| 字段 | 含义 | 设计要点 |
|---|---|---|
| `ops` | 四个回调函数指针 + `owner` 模块指针 | 与 `liveupdate_file_ops` 一样，是子系统实现"如何做"的唯一接口 |
| `compatible` | `LIVEUPDATE_FLB_COMPAT_LENGTH`(48) 字节的兼容性字符串 | 与 `liveupdate_file_handler.compatible` 同源设计：跨内核版本匹配 + ABI 演进的锚点 |
| `private` | `struct luo_flb_private`，`__private` 修饰 | 编译期强制外部代码只能通过 `ACCESS_PRIVATE()` 访问，详见 6.2 节对 `__private`/`ACCESS_PRIVATE` 的解释 |

`liveupdate_flb_op_args` 则是四个回调统一的参数/返回值载体——这同样是"少量通用字段
应付多种回调场景"的设计：

* `flb`：回调可以反查自己所属的 `liveupdate_flb`（例如取 `compatible` 打印日志）；
* `data`：一个 `u64` **不透明句柄**。`preserve()` 返回它，序列化时原样写入 FDT，
  `retrieve()` 时原样传回——子系统可以把它当成一个指针、一个偏移量，甚至直接编码状态，
  LUO 完全不关心其语义，只负责"原样传递"；
* `obj`：一个**运行时活对象指针**，由 `preserve()`/`retrieve()` 产生，后续
  `unpreserve()`/`finish()` 通过它定位需要清理的对象。它**不会**被序列化（因为它是
  本次启动中的内存地址，重启后毫无意义），只在同一次内核生命周期内有效。

把 `data` 和 `obj` 放在一起看，会发现这正是"跨 kexec 持久化的标识 vs.
当前内核中的活引用"这一对偶概念在 FLB 维度上的体现——与 `luo_file.token`（持久标识）
和 `luo_file.filp`（活引用）的关系几乎一一对应。

#### 7.2.2 LUO 内部的部分：`luo_flb_private` / `luo_flb_private_state`

`luo_flb_private` 是隐藏在 `liveupdate_flb.private` 后面的"真身"，它把
"outgoing（发往新内核）"和"incoming（从旧内核接收）"两套独立状态显式地分开存放
（`include/linux/liveupdate.h:170-189`）：

```c
struct luo_flb_private_state {
        long count;
        u64 data;
        void *obj;
        struct mutex lock;
        bool finished;
        bool retrieved;
};

struct luo_flb_private {
        struct list_head list;
        struct luo_flb_private_state outgoing;
        struct luo_flb_private_state incoming;
        int users;
        bool initialized;
};
```

这又是贯穿全书的 **outgoing/incoming 双状态模式**（2.5 节、5.1 节都已经见过）的
又一次出现，但 FLB 这里把它做得更"对称、更纯粹"——`outgoing` 和 `incoming` 共用
**完全相同的内部结构** `luo_flb_private_state`，仅靠字段语义区分用途：

| 字段 | `outgoing`（旧内核中的角色） | `incoming`（新内核中的角色） |
|---|---|---|
| `count` | 当前已 preserve、尚未 unpreserve 的依赖文件数 —— **正向计数，从 0 累加** | 当前还需要 finish 的依赖文件数 —— **反向计数，从序列化值递减到 0** |
| `data` | `ops->preserve()` 返回的不透明句柄，即将写入 FDT | 从 FDT 中读到的、传给 `ops->retrieve()` 的句柄 |
| `obj` | `ops->preserve()` 创建的活对象，供 `unpreserve()` 清理 | `ops->retrieve()` 重建的活对象，供消费者使用、`finish()` 清理 |
| `lock` | 保护 outgoing 三元组并发修改的互斥锁 | 保护 incoming 三元组并发修改/懒加载触发的互斥锁 |
| `finished` | （不使用） | 是否已经调用过 `ops->finish()`——一次性标记，防止重复 finish |
| `retrieved` | （不使用） | 是否已经调用过 `ops->retrieve()`——懒加载缓存标记，配合 `luo_flb_get_incoming()` |

这张表清楚地揭示了一个极简但精巧的设计决策：**outgoing 端是"从 0 向上累加再清零"
的计数，incoming 端是"从已知总数向下递减到 0"的计数**——前者驱动 `preserve`/`unpreserve`，
后者驱动 `retrieve`（懒加载）/`finish`。两套语义不同的状态机被塞进同一个结构体模板里，
这既减少了重复定义，又通过统一的字段名让阅读者可以"用同一套心智模型理解两侧"。

`luo_flb_private` 本身的另外两个字段：

* `list`：把这个 FLB 链入 **全局** FLB 链表 `luo_flb_global.list`（一个 FLB 在全局
  只出现一次，无论被多少个文件 handler 依赖）；
* `users`：有多少个 `liveupdate_file_handler` 通过 `liveupdate_register_flb()`
  声明了对这个 FLB 的依赖——它与 `outgoing.count`/`incoming.count` 是两个完全不同维度
  的计数（前者是"被多少种文件类型依赖"，后者是"当前有多少个具体文件实例存活"），
  稍后在 7.7 节会看到二者如何配合完成"全局唯一注册"。
* `initialized`：懒加载初始化完成标记，配合 7.3 节的双重检查锁定模式。

#### 7.2.3 LUO 私有的运行时容器：`luo_flb_global` / `luo_flb_header` / `luo_flb_link`

这三个结构体定义在 `luo_flb.c` 内部（不对外暴露），是 LUO 组织和索引所有已注册 FLB
的"账本"：

```c
struct luo_flb_header {
        struct luo_flb_header_ser *header_ser;
        struct luo_flb_ser *ser;
        bool active;
};

struct luo_flb_global {
        struct luo_flb_header incoming;
        struct luo_flb_header outgoing;
        struct list_head list;
        long count;
};

static struct luo_flb_global luo_flb_global = {
        .list = LIST_HEAD_INIT(luo_flb_global.list),
};

struct luo_flb_link {
        struct liveupdate_flb *flb;
        struct list_head list;
};
```

* `luo_flb_global`：**进程级单例**（`static` 全局变量），维护：
  - `list` + `count`：全局已注册 FLB 链表及其数量上限计数（`LUO_FLB_MAX`）；
  - `incoming`/`outgoing` 两个 `luo_flb_header`：分别指向"从旧内核继承来的序列化
    缓冲区"和"准备写给新内核的序列化缓冲区"——**注意这与 `luo_flb_private_state`
    里的 outgoing/incoming 是两个不同层次的概念**：`luo_flb_global` 里的 outgoing/incoming
    描述的是"FDT 序列化缓冲区"本身（所有 FLB 共享同一块缓冲区），而
    `luo_flb_private_state` 描述的是"单个 FLB 的引用计数状态"。
* `luo_flb_header`：序列化缓冲区的运行时句柄——`header_ser` 指向缓冲区头部
  （记录页数、条目数），`ser` 指向紧随其后的条目数组首地址，`active` 表示这块缓冲区
  当前是否有效（incoming 侧只有在成功从 FDT 中找到 FLB 节点后才会被置位）。
* `luo_flb_link`：**胶水节点**——把一个全局 `liveupdate_flb` 链入某个具体
  `liveupdate_file_handler` 的私有依赖链表 `flb_list` 中。源码注释精炼地概括了它的
  作用（`luo_flb.c:77-86`）：

  ```c
  /*
   * struct luo_flb_link - Links an FLB definition to a file handler's internal
   * list of dependencies.
   * @flb:  A pointer to the registered &struct liveupdate_flb definition.
   * @list: The list_head for linking.
   */
  ```

  之所以需要这一层间接的 `luo_flb_link`，而不是直接把 `liveupdate_flb` 塞进
  `flb_list`，是因为**同一个 `liveupdate_flb` 对象可能同时出现在多个文件 handler
  的依赖链表里**——`liveupdate_flb` 自身只有一个 `private.list` 字段用于链入全局链表，
  无法同时挂在 N 个 handler 的私有链表上；而 `luo_flb_link` 是为每一对
  `(handler, flb)` 关系动态分配的"关系节点"，可以同时存在多份。

这三层容器与上面两组结构体之间的关系，可以用下图概括（以一个假设场景为例：
`vfio-v1` 与 `kvm-v1` 两个文件 handler 都依赖同一个 `iommu-flb-v1`）：

```
                         luo_flb_global（进程级单例）
                ┌───────────────────────────────────────┐
                │ list ──────────────┐                  │
                │ count = 1          │                  │
                │ incoming/outgoing  │                  │
                │ (FDT 序列化缓冲区)  │                  │
                └────────────────────┼──────────────────┘
                                     │
                                     ▼
                       liveupdate_flb "iommu-flb-v1"
                ┌───────────────────────────────────────┐
                │ ops = {preserve, unpreserve, ...}     │
                │ compatible = "iommu-flb-v1"           │
                │ private (luo_flb_private):            │
                │   list ─────────────(链入全局 list)    │
                │   outgoing = {count=2, data, obj, ..} │
                │   incoming = {count=2, data, obj, ..} │
                │   users = 2   ◄── 被 2 个 handler 依赖  │
                └───────────────────────────────────────┘
                       ▲                         ▲
           luo_flb_link│             luo_flb_link│
                       │                         │
       ┌───────────────┴──────┐     ┌────────────┴──────────┐
       │ liveupdate_file_     │     │ liveupdate_file_      │
       │  handler "vfio-v1"   │     │  handler "kvm-v1"     │
       │ flb_list ──► [link]──┘     │ flb_list ──► [link]───┘
       └──────────────────────┘     └───────────────────────┘
```

这张图中最值得玩味的一点是：`outgoing.count`/`incoming.count` 统计的是
"**当前存活的、依赖这个 FLB 的具体文件实例数**"（例如打开了 2 个 `vfio` fd），
而 `users` 统计的是"**注册了对这个 FLB 的依赖关系的文件 handler 种类数**"
（`vfio-v1` 和 `kvm-v1` 两种）。前者随着运行时文件的打开/关闭动态变化，
后者在模块加载/卸载时才会变化——这是两个生命周期尺度完全不同的计数器，
千万不能混淆（笔者在第一遍读这段代码时就曾经把二者搞混，导致误判
`liveupdate_register_flb` 中 `if (!private->users)` 分支的触发条件）。

### 7.3 懒加载初始化：`luo_flb_get_private` 的双重检查锁定

`liveupdate_flb` 是子系统**静态定义**的全局变量（类似 `static struct liveupdate_flb
my_flb = { .ops = &my_flb_ops, .compatible = "my-flb-v1" }`），其内部的
`luo_flb_private` 在编译期只是一段全零内存——`mutex`、`list_head` 等字段都需要运行时
初始化。但 LUO 又不希望强迫每个子系统在注册之前手动调用一个"初始化"函数（这会增加
API 复杂度、也容易被遗漏）。`luo_flb_get_private()` 用一个**双重检查锁定**
（double-checked locking）模式优雅地解决了这个"首次访问时惰性初始化"的问题
（`luo_flb.c:88-107`）：

```c
/* luo_flb_get_private - Access private field, and if needed initialize it. */
static struct luo_flb_private *luo_flb_get_private(struct liveupdate_flb *flb)
{
        struct luo_flb_private *private = &ACCESS_PRIVATE(flb, private);
        static DEFINE_SPINLOCK(luo_flb_init_lock);

        if (smp_load_acquire(&private->initialized))
                return private;

        guard(spinlock)(&luo_flb_init_lock);
        if (!private->initialized) {
                mutex_init(&private->incoming.lock);
                mutex_init(&private->outgoing.lock);
                INIT_LIST_HEAD(&private->list);
                private->users = 0;
                smp_store_release(&private->initialized, true);
        }

        return private;
}
```

逐行拆解这个函数中的每一个内存序细节：

1. **快速路径**：`smp_load_acquire(&private->initialized)`——绝大多数调用都会走到
   这一行就返回。这里特意用 `smp_load_acquire` 而不是普通的 `READ_ONCE`，是为了
   建立一个 **acquire-release 配对**：一旦看到 `initialized == true`，就能保证
   "看到"该写入之前的所有写操作（即 `mutex_init`/`INIT_LIST_HEAD`/`private->users = 0`）
   也已经对当前 CPU 可见。这是无锁读路径下保证"看到标志位为真就等于看到完整初始化结果"
   的标准写法。
2. **慢速路径**：用一个**全局静态自旋锁** `luo_flb_init_lock` 串行化初始化过程。
   注意这把锁是 `static DEFINE_SPINLOCK`——**所有** `liveupdate_flb` 实例的首次初始化
   都共用同一把锁。这看似会成为并发瓶颈，但实际上完全不是问题：初始化只在系统启动、
   模块加载阶段发生寥寥数次，临界区极短（仅仅是给几个字段赋初值），用一把全局锁换取
   "不需要在 `liveupdate_flb` 里专门放一个初始化用的锁"的简洁性，是一笔划算的买卖。
3. **进入临界区后二次检查** `if (!private->initialized)`——这正是"双重检查"中的
   第二次检查：避免在排队等锁的过程中，前一个线程已经完成了初始化，自己却又重新做
   一遍（`mutex_init` 在已初始化的 mutex 上重复调用是未定义行为）。
4. **写入完成后** `smp_store_release(&private->initialized, true)`——与第 1 步的
   `smp_load_acquire` 配对，保证所有初始化写入在 `initialized = true` 对其他 CPU
   可见之前已经全部完成（release 语义阻止编译器/CPU 把 `initialized = true` 重排到
   前面）。

这个函数在整个 `luo_flb.c` 中几乎是所有对外函数（`preserve_one`/`retrieve_one`/
`register_flb`/`get_incoming` 等）的**第一行代码**，是一种"在访问前自助完成初始化"
的设计哲学——它让 `liveupdate_flb` 的使用方完全不需要关心"我是否已经初始化过"，
所有调用路径都天然是安全的。这与 C++ 中 "construct on first use" 的惯用法、以及
Rust 中 `OnceLock`/`Lazy` 的语义高度相似，只不过在 C 内核代码里需要手写内存序原语
来达成同样的正确性保证。

### 7.4 outgoing 侧：引用计数式 preserve / unpreserve

理解了基础设施之后，我们正式进入 FLB 的核心逻辑——引用计数驱动的
preserve/unpreserve 状态机。先看 `luo_flb_file_preserve_one()`
（`luo_flb.c:109-134`）：

```c
static int luo_flb_file_preserve_one(struct liveupdate_flb *flb)
{
        struct luo_flb_private *private = luo_flb_get_private(flb);

        scoped_guard(mutex, &private->outgoing.lock) {
                if (!private->outgoing.count) {
                        struct liveupdate_flb_op_args args = {0};
                        int err;

                        if (!try_module_get(flb->ops->owner))
                                return -ENODEV;

                        args.flb = flb;
                        err = flb->ops->preserve(&args);
                        if (err) {
                                module_put(flb->ops->owner);
                                return err;
                        }
                        private->outgoing.data = args.data;
                        private->outgoing.obj = args.obj;
                }
                private->outgoing.count++;
        }

        return 0;
}
```

这段代码用最经济的篇幅实现了一个经典的"**首次创建、引用计数管理生命周期**"模式：

* 整个函数体被包在 `scoped_guard(mutex, &private->outgoing.lock)` 之中——
  `scoped_guard` 是 `linux/cleanup.h` 提供的语法糖，等价于"加锁 → 执行代码块
  → 自动解锁"，无论从代码块的哪条路径退出（包括 `return`）锁都会被正确释放。
  这与第 5、6 章见过的 `guard()`/`scoped_guard()` 用法完全一致——LUO 全文统一使用
  这套基于 `__cleanup__` 属性的资源管理惯用法，几乎看不到手写 `mutex_lock`/`mutex_unlock`
  配对的代码，大幅降低了忘记解锁、异常路径泄漏锁的风险。
* `if (!private->outgoing.count)`——**只有当计数为 0 时**（即"我是第一个依赖者"）
  才会触发真正的保存动作：`try_module_get` 固定住实现该 FLB 的模块（防止在对象存活
  期间模块被卸载）、调用 `ops->preserve()`、把返回的 `data`/`obj` 缓存进
  `private->outgoing`。
* 无论是否触发了真正的保存，函数末尾都会执行 `private->outgoing.count++`——
  这一行在锁的保护下对**每一个**依赖文件都执行一次，因此 `count` 精确地反映了
  "当前有多少个已 preserve、尚未 unpreserve 的依赖文件"。

`luo_flb_file_unpreserve_one()`（`luo_flb.c:136-157`）则是它的镜像操作：

```c
static void luo_flb_file_unpreserve_one(struct liveupdate_flb *flb)
{
        struct luo_flb_private *private = luo_flb_get_private(flb);

        scoped_guard(mutex, &private->outgoing.lock) {
                private->outgoing.count--;
                if (!private->outgoing.count) {
                        struct liveupdate_flb_op_args args = {0};

                        args.flb = flb;
                        args.data = private->outgoing.data;
                        args.obj = private->outgoing.obj;

                        if (flb->ops->unpreserve)
                                flb->ops->unpreserve(&args);

                        private->outgoing.data = 0;
                        private->outgoing.obj = NULL;
                        module_put(flb->ops->owner);
                }
        }
}
```

注意这里的顺序与 `preserve_one` 正好相反：**先递减计数，再检查是否归零**——
这保证了"最后一个依赖者离开时才真正销毁"的语义。当 `count` 减到 0 时：

* 把缓存的 `data`/`obj` 重新打包进 `args`，回传给 `ops->unpreserve()`
  （注意这是一个 `void` 返回值的回调——清理动作被认为是不应失败的"尽力而为"操作，
  这与第 6 章 `unpreserve_files`/`__luo_file_unfreeze` 中"清理路径不允许失败"的设计
  哲学完全一致）；
* `flb->ops->unpreserve` 这个判空检查值得注意——`unpreserve` 回调是**可选的**
  （某些 FLB 可能根本不需要清理动作，比如它保存的只是一份只读快照，没有需要释放的
  运行时资源）；
* 清空缓存字段、`module_put` 释放模块引用——与 `preserve_one` 中的 `try_module_get`
  形成完整闭环。

把这两个函数放在一起，可以画出 outgoing 侧完整的引用计数状态机：

```
                        outgoing.count 状态机（旧内核侧）

   [count = 0]                                          [count = 0]
  (FLB 未激活)                                          (FLB 已清理)
       │                                                      ▲
       │ 第 1 个文件 preserve                    最后 1 个文件 unpreserve │
       │ try_module_get + ops->preserve()                ops->unpreserve()│
       │ 缓存 data/obj                          清空 data/obj + module_put│
       ▼                                                      │
  [count = 1] ───┐                                    ┌─── [count = 1]
       │ 第 2..N 个   │  count++ / count--             │  最后退到 1
       │ 文件 preserve│ （仅计数变化，不触发回调）       │  之前的递减
       ▼              │                                │
  [count = N] ◄───────┘                                └──► [count = N-1]
       └──────────────────────── 持续累加/递减 ─────────────────┘
```

这张状态机图揭示了 FLB 模型最重要的不变式：**`ops->preserve()`/`ops->unpreserve()`
在整个旧内核生命周期中各自最多被调用一次**——无论有多少文件依赖这个 FLB，
全局状态只会被真正保存一次、清理一次。这正是 7.1 节提出的"重复保存"问题的解法。

#### 7.4.1 preserve 失败时的回滚：`luo_flb_file_preserve`

单个 FLB 的 preserve/unpreserve 已经清楚，那么"一个文件 handler 依赖多个 FLB"的
场景如何处理？答案在对外暴露的胶水函数 `luo_flb_file_preserve()` 中
（`luo_flb.c:254-276`）：

```c
int luo_flb_file_preserve(struct liveupdate_file_handler *fh)
{
        struct list_head *flb_list = &ACCESS_PRIVATE(fh, flb_list);
        struct luo_flb_link *iter;
        int err = 0;

        down_read(&luo_register_rwlock);
        list_for_each_entry(iter, flb_list, list) {
                err = luo_flb_file_preserve_one(iter->flb);
                if (err)
                        goto exit_err;
        }
        up_read(&luo_register_rwlock);

        return 0;

exit_err:
        list_for_each_entry_continue_reverse(iter, flb_list, list)
                luo_flb_file_unpreserve_one(iter->flb);
        up_read(&luo_register_rwlock);

        return err;
}
```

这是第 6 章已经见过的"**七层回滚链**"思想在 FLB 维度上的缩影版：

* 正向遍历 `flb_list`，对每一个依赖的 FLB 调用 `luo_flb_file_preserve_one()`；
* 一旦某个 FLB 的 preserve 失败（`err != 0`），立刻 `goto exit_err`；
* 在 `exit_err` 中用 `list_for_each_entry_continue_reverse(iter, ...)`——
  **从当前失败的节点开始，沿链表反向回退**，对**已经成功 preserve 过的**那些 FLB
  逐一调用 `unpreserve_one()` 撤销。注意 `continue_reverse` 是从 `iter`
  **当前的位置继续**反向遍历，而失败的那个 `iter` 本身**不会**被回滚
  （因为它根本没有成功 preserve，回滚它只会导致计数错误或对未初始化的状态调用
  `unpreserve`）。这种"以当前失败点为基准向前/向后扫描"的写法，是处理列表式资源
  回滚时最容易出错也最容易写对的地方——LUO 在这里给出了一个教科书级别的范例。

`down_read`/`up_read` 锁住的是 `luo_register_rwlock`（全局注册读写锁，
在 `luo_internal.h` 中定义），它保护的是"**注册关系本身**"——即 `flb_list`
链表结构在遍历期间不会被并发的 `register_flb`/`unregister_flb` 修改。这把锁与
`outgoing.lock`/`incoming.lock` 处于不同的保护层次：前者保护"谁依赖谁"的拓扑关系，
后者保护"某个具体 FLB 的引用计数状态"。二者职责分离、互不干扰，这也是为什么
`luo_flb_file_preserve_one` 内部可以安全地在持有 `luo_register_rwlock` 读锁的同时
再去申请 `outgoing.lock`（不存在 AA 死锁，也不会与 `unregister_flb` 持有的写锁
产生循环等待——`unregister_flb` 不会反过来申请 `outgoing.lock`）。

`luo_flb_file_unpreserve()`（`luo_flb.c:290-298`）则简单得多——它没有"失败回滚"的
负担（`unpreserve_one` 是 `void` 返回值，不会失败），只需要按**反向**顺序遍历整个
链表逐一撤销：

```c
void luo_flb_file_unpreserve(struct liveupdate_file_handler *fh)
{
        struct list_head *flb_list = &ACCESS_PRIVATE(fh, flb_list);
        struct luo_flb_link *iter;

        guard(rwsem_read)(&luo_register_rwlock);
        list_for_each_entry_reverse(iter, flb_list, list)
                luo_flb_file_unpreserve_one(iter->flb);
}
```

这里选择**反向**遍历（`list_for_each_entry_reverse`）而不是正向，体现了与第 6 章
`luo_file_unpreserve_files()`"构造的反向就是析构的顺序"完全相同的设计直觉——
如果 FLB A 依赖 FLB B（这种依赖关系通过注册顺序隐式表达：先注册的在链表前面），
那么撤销时应该先撤销"后加入、可能依赖前面对象"的 B，再撤销 A，避免悬空引用。

### 7.5 incoming 侧：懒加载 retrieve 与 finish 的协作之舞

如果说 outgoing 侧是"从 0 累加到 N，归零时清理"的简单计数器，那么 incoming 侧的设计
要精妙得多——因为它要解决一个 outgoing 侧不存在的问题：**"什么时候真正调用
`ops->retrieve()`？"**

#### 7.5.1 为什么 retrieve 需要懒加载

直觉上，新内核启动时似乎应该"一鼓作气"把所有从旧内核继承来的 FLB 全部 retrieve
一遍。但这样做至少有两个问题：

1. **顺序/依赖问题**：FLB 之间可能存在隐式依赖（例如 IOMMU FLB 的重建可能需要先
   重建中断重映射 FLB），如果在 `late_initcall` 阶段不分青红皂白地一次性 retrieve
   所有 FLB，很容易撞上"B 还没初始化，A 却已经尝试引用 B"的先有鸡还是先有蛋问题。
2. **浪费**：如果某个 FLB 在新内核中实际上一个依赖它的文件都没有被 retrieve
   过（比如用户根本没有调用 `retrieve_fd` 取回那个 VFIO 设备），那么完全没有必要
   去重建它背后的全局状态——这是一种纯粹的资源浪费。

LUO 选择的方案是：**把 retrieve 推迟到"第一次真正需要它"的那一刻**，可能的触发点
有两个——要么是消费者主动调用 `liveupdate_flb_get_incoming()` 查询对象（详见 7.6 节），
要么是计数即将归零、必须在 `finish` 之前补做一次 retrieve（因为 `finish` 回调需要
拿到 `obj` 才能正确清理）。这正是 `luo_flb_retrieve_one()`（`luo_flb.c:159-206`）
和 `luo_flb_file_finish_one()`（`luo_flb.c:208-237`）联手实现的逻辑：

```c
static int luo_flb_retrieve_one(struct liveupdate_flb *flb)
{
        struct luo_flb_private *private = luo_flb_get_private(flb);
        struct luo_flb_header *fh = &luo_flb_global.incoming;
        struct liveupdate_flb_op_args args = {0};
        bool found = false;
        int err;

        guard(mutex)(&private->incoming.lock);

        if (private->incoming.finished)
                return -ENODATA;

        if (private->incoming.retrieved)
                return 0;

        if (!fh->active)
                return -ENODATA;

        for (int i = 0; i < fh->header_ser->count; i++) {
                if (!strcmp(fh->ser[i].name, flb->compatible)) {
                        private->incoming.data = fh->ser[i].data;
                        private->incoming.count = fh->ser[i].count;
                        found = true;
                        break;
                }
        }

        if (!found)
                return -ENOENT;

        if (!try_module_get(flb->ops->owner))
                return -ENODEV;

        args.flb = flb;
        args.data = private->incoming.data;

        err = flb->ops->retrieve(&args);
        if (err) {
                module_put(flb->ops->owner);
                return err;
        }

        private->incoming.obj = args.obj;
        private->incoming.retrieved = true;

        return 0;
}
```

这个函数的控制流是一连串"提前退出"式的状态检查，逐项过一遍：

1. **`finished` 检查**——如果这个 FLB 已经被 `finish` 过（生命周期已经结束），
   后续任何 retrieve 请求都应当被拒绝（`-ENODATA`）。这是防止"finish 之后还有迟到的
   消费者尝试访问已销毁对象"的安全网。
2. **`retrieved` 检查**——这是**幂等缓存**：一旦成功 retrieve 过一次，
   后续调用直接返回成功（`return 0`），不会重复执行 `ops->retrieve()`。这与第 6 章
   `luo_retrieve_file()` 的"三态缓存"（`retrieve_status` 字段）思路完全一致——
   只不过 FLB 这里用了两个独立的 bool（`finished`/`retrieved`）而不是一个三态整数，
   原因在于 FLB 的 incoming 状态机比单文件多一个"已结束"终态，用单个整数编码会让
   语义变得隐晦，拆成两个 bool 反而更直观。
3. **`fh->active` 检查**——`luo_flb_global.incoming` 这个全局序列化缓冲区句柄，
   只有在 `luo_flb_setup_incoming()` 成功从 FDT 中找到 `luo-flb` 节点之后才会被置位
   （详见 7.8 节）。如果整个系统压根不存在 incoming FLB 数据（例如这是系统的第一次
   正常启动，而非 kexec 之后的重生），这里会直接返回 `-ENODATA`。
4. **线性查找 compatible 字符串**——遍历 `fh->ser[]` 数组，用 `strcmp` 逐项比较
   `name` 与 `flb->compatible`。找到匹配项后，把序列化时记录的 `data`（不透明句柄）
   和 `count`（引用计数）原样复制进 `private->incoming`。这里的线性查找在 FLB
   数量上限 `LUO_FLB_MAX` 通常只有几十的量级下完全不是性能问题，简单直接远胜于
   引入哈希表的复杂度。
5. **`try_module_get` + `ops->retrieve()`**——与 outgoing 侧 `preserve_one`
   对称的模块引用计数保护，调用真正的恢复回调，把结果对象缓存进 `private->incoming.obj`
   并置位 `retrieved = true`（这一刻起，幂等缓存正式生效）。

整个函数被包在 `guard(mutex)(&private->incoming.lock)` 之下——这把锁同时保护着
"懒加载触发的互斥"（防止两个并发的消费者同时触发 `ops->retrieve()` 导致重复调用）
和"状态字段的可见性"。`luo_flb_get_incoming()` 在调用这个函数之前不持有锁
（见 7.6 节），完全依赖这里面的互斥锁加上 `retrieved` 标记位来保证"`ops->retrieve()`
精确执行一次"。

接下来看 `luo_flb_file_finish_one()`，它展示了"懒加载与最终清理如何相遇"
（`luo_flb.c:208-237`）：

```c
static void luo_flb_file_finish_one(struct liveupdate_flb *flb)
{
        struct luo_flb_private *private = luo_flb_get_private(flb);
        u64 count;

        scoped_guard(mutex, &private->incoming.lock)
                count = --private->incoming.count;

        if (!count) {
                struct liveupdate_flb_op_args args = {0};

                if (!private->incoming.retrieved) {
                        int err = luo_flb_retrieve_one(flb);

                        if (WARN_ON(err))
                                return;
                }

                scoped_guard(mutex, &private->incoming.lock) {
                        args.flb = flb;
                        args.obj = private->incoming.obj;
                        flb->ops->finish(&args);

                        private->incoming.data = 0;
                        private->incoming.obj = NULL;
                        private->incoming.finished = true;
                        module_put(flb->ops->owner);
                }
        }
}
```

这段代码里藏着一个非常容易被忽略、但对正确性至关重要的设计点——**"finish 之前必须
确保已经 retrieve 过"**。试想一种场景：系统重启后，依赖某 FLB 的 3 个文件全部被
`session_finish()` 释放，但消费者从未主动调用过 `liveupdate_flb_get_incoming()`
（也就是说 `ops->retrieve()` 从未被触发过、`private->incoming.obj` 仍是 `NULL`）。
此时如果直接调用 `ops->finish()`，传入的 `args.obj` 会是 `NULL`——子系统的
`finish` 回调本来期望"清理一个我之前 retrieve 出来的活对象"，结果却收到一个空指针，
这是一个逻辑错误。

`luo_flb_file_finish_one` 通过 `if (!private->incoming.retrieved)` 这一行**显式
"补做"** 一次 `luo_flb_retrieve_one(flb)` 来规避这个问题——确保无论消费者是否主动
查询过，FLB 在被 finish 之前**一定**经历过一次完整的 retrieve，`finish` 回调收到的
`obj` 永远是有效的活对象。`WARN_ON(err)` 这里选择了"打警告但不中断"的策略——
如果补做的 retrieve 也失败了（理论上几乎不可能，因为序列化数据必然存在，否则
计数不可能非零），打印警告并直接返回，**不**调用 `ops->finish()`。这种"宁可不清理
也不在错误状态上继续往下走"的克制，与第 6 章总结的"不做不安全 undo"哲学一脉相承——
此处虽然不是不可恢复的"deserialize 失败"场景，但同样体现了"遇到不应该发生的情况时，
最安全的做法是停下来报警，而不是继续猜测着往前走"的内核编程信条。

把 `luo_flb_retrieve_one` 与 `luo_flb_file_finish_one` 放在一起，可以画出 incoming
侧完整的状态机（与 outgoing 侧的"从 0 累加"形成镜像对比）：

```
            incoming 侧状态机（新内核侧，count 从序列化值开始递减）

  [count = N]  (从 fh->ser[i].count 反序列化而来，retrieved = false)
       │
       │ 消费者调用 liveupdate_flb_get_incoming()
       │   └─► luo_flb_retrieve_one() 命中 compatible 字符串
       │       try_module_get + ops->retrieve() → retrieved = true
       │       (此后任意次数调用都直接返回缓存的 obj，懒加载只发生一次)
       ▼
  [count = N, retrieved = true, obj 已就绪]
       │
       │ 每个依赖文件 finish 时 luo_flb_file_finish_one()
       │   count--（在 incoming.lock 保护下原子完成）
       ▼
  [count = N-1] ──► ... ──► [count = 1] ──► [count = 0]
                                                │
                          若此前从未 retrieved： │
                          补做 luo_flb_retrieve_one()（确保 obj 非空）
                                                │
                                                ▼
                              ops->finish(obj) + module_put
                              清空 data/obj，finished = true
                                                │
                                                ▼
                                         [终态：已结束]
                                  (任何后续 retrieve 请求返回 -ENODATA)
```

这张图与 7.4 节末尾 outgoing 侧的状态机图放在一起对照，会发现一个有趣的"时间箭头"
对称性：**outgoing 侧的故事是"从无到有、从有到无"（计数从 0 涨到 N 再缩回 0），
incoming 侧的故事则是"既定事实的消费过程"（计数从已知的 N 单调递减到 0）**——
这恰好对应着"旧内核里这份资源被创建和销毁的过程" vs. "新内核里这份资源被继承和
消费殆尽的过程"，是 LUO 整个 outgoing/incoming 二元论在 FLB 子系统里最纯粹的体现。

### 7.6 对外访问接口：`liveupdate_flb_get_incoming` / `get_outgoing`

子系统拿到 `obj` 指针的两个正式入口是 `liveupdate_flb_get_incoming()` 和
`liveupdate_flb_get_outgoing()`（`luo_flb.c:508-553`）：

```c
int liveupdate_flb_get_incoming(struct liveupdate_flb *flb, void **objp)
{
        struct luo_flb_private *private = luo_flb_get_private(flb);

        if (!liveupdate_enabled())
                return -EOPNOTSUPP;

        if (!private->incoming.obj) {
                int err = luo_flb_retrieve_one(flb);

                if (err)
                        return err;
        }

        guard(mutex)(&private->incoming.lock);
        *objp = private->incoming.obj;

        return 0;
}

int liveupdate_flb_get_outgoing(struct liveupdate_flb *flb, void **objp)
{
        struct luo_flb_private *private = luo_flb_get_private(flb);

        if (!liveupdate_enabled())
                return -EOPNOTSUPP;

        guard(mutex)(&private->outgoing.lock);
        *objp = private->outgoing.obj;

        return 0;
}
```

两相对比，差异恰好印证了 7.5 节分析的"非对称性"：

* **`get_incoming`**：先做一个无锁的快速检查 `if (!private->incoming.obj)`——
  如果对象尚未就绪，调用 `luo_flb_retrieve_one()` **触发懒加载**（其内部自带互斥锁，
  保证并发调用时只有一个线程真正执行 `ops->retrieve()`）。这正是整个 FLB
  子系统中"懒加载 retrieve"的**唯一对外触发点**之一（另一个隐藏的触发点是
  7.5 节看到的、`finish` 路径里的"补做"逻辑）。
* **`get_outgoing`**：没有任何懒加载逻辑，直接加锁读取 `outgoing.obj`——因为
  outgoing 侧的对象在"第一个文件 preserve 时"就已经被 `ops->preserve()`
  创建好了（7.4 节），消费者调用 `get_outgoing` 时这个对象**必然已经存在**，
  不需要、也不应该有"懒创建"的逻辑（如果对象还不存在就去主动创建它，会打乱
  "由第一个 preserve 的文件触发创建"这一约定的语义边界）。

这一组接口的函数注释里专门列出了详细的错误码语义表（`luo_flb.c:503-507`），
这与第 4 章总结的 ioctl 错误码表遥相呼应——**LUO 的几乎每一层接口都坚持把"每个
错误码代表什么场景"写进 kernel-doc 注释**，这是阅读这个子系统源码时一个非常
友好的细节：

| 错误码 | 含义 |
|---|---|
| `-EOPNOTSUPP` | live update 功能未启用或未编译进内核 |
| `-ENODATA` | 不存在 incoming FLB 数据（例如非 kexec 启动场景） |
| `-ENOENT` | 在 incoming 序列化数据中找不到与 `compatible` 匹配的条目 |
| `-ENODEV` | 该 FLB 所属的模块正在卸载中（`try_module_get` 失败） |

### 7.7 注册与注销：`liveupdate_register_flb` / `unregister_flb`

至此我们已经看清了"单个 FLB 如何工作"，现在来看它如何被**接入**到某个具体的文件
handler。`liveupdate_register_flb()`（`luo_flb.c:399-462`）是子系统模块初始化时
调用的入口，承担着"建立依赖关系 + 维护全局唯一性"两项职责：

```c
int liveupdate_register_flb(struct liveupdate_file_handler *fh,
                            struct liveupdate_flb *flb)
{
        struct luo_flb_private *private = luo_flb_get_private(flb);
        struct list_head *flb_list = &ACCESS_PRIVATE(fh, flb_list);
        struct luo_flb_link *link __free(kfree) = NULL;
        struct liveupdate_flb *gflb;
        struct luo_flb_link *iter;

        if (!liveupdate_enabled())
                return -EOPNOTSUPP;

        if (WARN_ON(!flb->ops->preserve || !flb->ops->unpreserve ||
                    !flb->ops->retrieve || !flb->ops->finish)) {
                return -EINVAL;
        }

        /*
         * File handler must already be registered, as it initializes the
         * flb_list
         */
        if (WARN_ON(list_empty(&ACCESS_PRIVATE(fh, list))))
                return -EINVAL;

        link = kzalloc_obj(*link);
        if (!link)
                return -ENOMEM;

        guard(rwsem_write)(&luo_register_rwlock);

        /* Check that this FLB is not already linked to this file handler */
        list_for_each_entry(iter, flb_list, list) {
                if (iter->flb == flb)
                        return -EEXIST;
        }

        /*
         * If this FLB is not linked to global list it's the first time the FLB
         * is registered
         */
        if (!private->users) {
                if (WARN_ON(!list_empty(&private->list)))
                        return -EINVAL;

                if (luo_flb_global.count == LUO_FLB_MAX)
                        return -ENOSPC;

                /* Check that compatible string is unique in global list */
                list_private_for_each_entry(gflb, &luo_flb_global.list, private.list) {
                        if (!strcmp(gflb->compatible, flb->compatible))
                                return -EEXIST;
                }

                list_add_tail(&private->list, &luo_flb_global.list);
                luo_flb_global.count++;
        }

        /* Finally, link the FLB to the file handler */
        private->users++;
        link->flb = flb;
        list_add_tail(&no_free_ptr(link)->list, flb_list);

        return 0;
}
```

这个函数值得拆开成几条逐一品味：

**1）`__free(kfree)` / `no_free_ptr` 自动清理属性。**

```c
struct luo_flb_link *link __free(kfree) = NULL;
...
list_add_tail(&no_free_ptr(link)->list, flb_list);
```

`__free(kfree)` 是 `linux/cleanup.h` 提供的变量级清理属性——它告诉编译器：
"当 `link` 这个变量离开作用域时，自动对它调用 `kfree()`"（前提是它非 NULL）。
这意味着函数中**任何一条提前 `return`**（`-EOPNOTSUPP`/`-EINVAL`/`-ENOMEM`/`-EEXIST`/
`-ENOSPC` 等等多达 6 处错误退出路径）都不需要手写 `kfree(link)`——编译器生成的
析构代码会自动完成。只有在函数最终成功、`link` 被真正挂入链表的那一刻，才通过
`no_free_ptr(link)` **取走所有权**——这个宏的语义是"返回指针的当前值，同时把变量
本身置为 `NULL`"，从而让自动清理逻辑在作用域结束时看到一个 `NULL`，不会误删已经
转移出去的对象。

这一对组合拳（`__free` + `no_free_ptr`）是现代 Linux 内核中用来消除"分配后忘记
在某条错误路径上释放"这一类经典 bug 的利器，本质上是把 C++ 里 `unique_ptr`/RAII
的思想用编译器属性 + 宏搬进了 C 语言。比起手写一长串 `goto err_free_link`，
这种写法让"正常路径"和"资源生命周期"完全解耦，读者不再需要在脑子里模拟每一条
错误分支上资源是否被正确释放——这正是本章开头提到的"统一设计语言"在内存管理
惯用法层面的体现（第 6 章的 `luo_alloc_files_mem`、`luo_session.c` 的部分函数
也使用了同样的模式，只是这次是首次专门展开讲解）。

**2）四个回调的完整性检查**——`WARN_ON(!flb->ops->preserve || !flb->ops->unpreserve ||
!flb->ops->retrieve || !flb->ops->finish)`。

注意这里**四个**回调全部要求非空，包括 `unpreserve`——这看起来与 7.4 节
`luo_flb_file_unpreserve_one` 中 `if (flb->ops->unpreserve)` 的判空检查相矛盾？
仔细品味会发现并不矛盾：`liveupdate_register_flb` 这里的检查针对的是
**FLB 定义本身**的完整性（防止子系统遗漏实现某个回调），是一种"开发期防御"；
而 `luo_flb_file_unpreserve_one` 里的判空检查，则是为了与历史遗留下来的、
或者刻意设计为"无需清理"的 FLB 兼容（保留这层判空，使将来某些 FLB 实现可以
合法地把 `unpreserve` 设为 `NULL` 而不触发这里的强制校验）——这其实是该函数里
一个细微但值得指出的"防御性冗余"：当前的强制校验逻辑下，`unpreserve == NULL`
根本不可能通过注册检查，因此判空分支理论上是死代码。但保留它的好处是给未来
"放宽校验、允许 `unpreserve` 可选"的演进留了余地，且不会带来任何运行期开销——
这是一种"宁可多写一行防御代码，也不要把假设刻死在调用点上"的工程克制。

**3）"全局首次注册" vs. "每次依赖关系建立"的双层判定**——`if (!private->users)`。

这是整个函数最精妙的设计点。`liveupdate_register_flb` 需要同时维护两件事：

* 全局唯一性：同一个 `compatible` 字符串、同一个 `liveupdate_flb` 对象，
  在全局只能存在一份（写入 `luo_flb_global.list`、计入 `luo_flb_global.count`）；
* 依赖关系：每一对 `(file_handler, flb)` 都需要一个独立的 `luo_flb_link`
  节点来表达"这个 handler 依赖这个 FLB"。

`private->users == 0` 精确地表达了"**这是这个 FLB 对象第一次被任何 handler 注册**"——
此时才需要：检查容量上限 `LUO_FLB_MAX`、检查 `compatible` 字符串在全局范围内唯一
（`list_private_for_each_entry` 遍历全局链表逐一 `strcmp`）、把 `private->list`
挂进全局链表。如果 `users > 0`（说明已经有其他 handler 注册过这个 FLB 了），
就直接跳过这一整段全局检查/挂载逻辑。

`list_private_for_each_entry` 是一个值得注意的辅助宏（来自 `linux/list_private.h`，
在源码 `#include` 列表中可以看到 `<linux/list_private.h>`）——它能够遍历一个
通过 `__private` 修饰的 `list_head` 字段组织起来的链表（即 `private.list`），
而不需要绕过编译期的私有性检查。这与第 6 章解读 `ACCESS_PRIVATE` 时强调的
"`__private` 是编译期强制、不是运行期强制"的论断互为佐证——内核为这种"私有字段
组成的链表"专门提供了配套的遍历宏，使得"私有"与"可遍历"两个看似冲突的需求
得以共存。

`!list_empty(&private->list)` 这条 `WARN_ON` 检查同样值得玩味——它断言"如果
`users == 0`，那么 `private->list` 必然是空链表节点"。这是一条**不变式断言**：
`users` 与 `private->list` 是否挂入全局链表理应永远保持一致，如果出现
"`users == 0` 但链表非空"的状况，说明发生了某种状态不一致的 bug（例如
`unregister_one` 中 `users--` 与 `list_del_init` 没有原子地一起完成）——
提前用 `WARN_ON` 把这种"理论上不可能"的状态暴露出来，是一种典型的内核防御性
编程范式：用断言固化设计者对不变式的假设，一旦假设被打破，第一时间报警而不是
让错误悄悄传播到更深的调用栈。

**4）最终步骤：`private->users++` + 创建链接节点**。

无论是否触发了"全局首次注册"分支，函数最后都会执行：递增 `users`、
把 `flb` 指针填入新分配的 `link`、用 `no_free_ptr(link)` 把节点所有权转移给
`flb_list`。这一步是每次注册都会发生的"个体级"操作——对应着"这个 handler
依赖关系链表多了一个节点"这一事实。

整个函数的逻辑可以归纳为下面这张决策图：

```
            liveupdate_register_flb(fh, flb)
                        │
              ┌─────────┴─────────┐
              │ 基础校验（enabled/ │
              │ ops 完整性/handler │
              │ 已注册/分配 link） │
              └─────────┬─────────┘
                        │ 加 luo_register_rwlock 写锁
                        ▼
        ┌───────────────────────────────┐
        │ 该 (fh, flb) 关系是否已存在？  │── 是 ──► -EEXIST
        └───────────────┬───────────────┘
                        │ 否
                        ▼
        ┌───────────────────────────────┐
        │  private->users == 0 ?        │
        │ （这是这个 FLB 全局首次注册）  │
        └───────┬───────────────┬───────┘
                │ 是            │ 否
                ▼               │
   全局唯一性 + 容量检查         │
   通过后 list_add 进全局链表    │
   luo_flb_global.count++       │
                │               │
                └───────┬───────┘
                        ▼
        users++ ; 创建 luo_flb_link
        挂入 fh 的私有 flb_list
                        │
                        ▼
                      成功
```

#### 7.7.1 注销路径：`luo_flb_unregister_one` / `unregister_all` / `liveupdate_unregister_flb`

注销路径是注册路径的镜像（`luo_flb.c:321-375`、`:479-488`）：

```c
static void luo_flb_unregister_one(struct liveupdate_file_handler *fh,
                                   struct liveupdate_flb *flb)
{
        struct luo_flb_private *private = luo_flb_get_private(flb);
        struct list_head *flb_list = &ACCESS_PRIVATE(fh, flb_list);
        struct luo_flb_link *iter;
        bool found = false;

        /* Find and remove the link from the file handler's list */
        list_for_each_entry(iter, flb_list, list) {
                if (iter->flb == flb) {
                        list_del(&iter->list);
                        kfree(iter);
                        found = true;
                        break;
                }
        }

        if (!found) {
                pr_warn("Failed to unregister FLB '%s': not found in file handler '%s'\n",
                        flb->compatible, fh->compatible);
                return;
        }

        private->users--;

        /*
         * If this is the last file-handler with which we are registred, remove
         * from the global list.
         */
        if (!private->users) {
                list_del_init(&private->list);
                luo_flb_global.count--;
        }
}
```

逻辑严格对称于注册：找到对应的 `luo_flb_link` 节点、`list_del` + `kfree`
释放它，`users--`，如果递减后归零则从全局链表中摘除（`list_del_init` 而不是
`list_del`——使用 `_init` 版本是为了让 `private->list` 重新回到"空链表节点"状态，
这样下一次该 FLB 被重新注册时，7.7 节中 `WARN_ON(!list_empty(&private->list))`
这条不变式断言才不会被误触发）。

`luo_flb_unregister_all()` 是一个简单的批量包装——遍历某个 handler 的整个
`flb_list`，逐一调用 `unregister_one`：

```c
void luo_flb_unregister_all(struct liveupdate_file_handler *fh)
{
        struct list_head *flb_list = &ACCESS_PRIVATE(fh, flb_list);
        struct luo_flb_link *iter, *tmp;

        if (!liveupdate_enabled())
                return;

        lockdep_assert_held_write(&luo_register_rwlock);
        list_for_each_entry_safe(iter, tmp, flb_list, list)
                luo_flb_unregister_one(fh, iter->flb);
}
```

它使用 `list_for_each_entry_safe`（而不是普通的 `list_for_each_entry`）——因为
循环体内部的 `unregister_one` 会执行 `list_del` 把当前节点从链表中摘除并释放，
若不使用"safe"版本（提前缓存下一个节点指针 `tmp`），遍历指针在节点被释放后就会
变成悬空指针，是经典的"边遍历边删除"陷阱。`lockdep_assert_held_write` 则是一条
仅在 `CONFIG_LOCKDEP` 开启时生效的运行期断言——它要求调用者必须已经持有
`luo_register_rwlock` 的**写锁**，这是因为 `unregister_all` 通常在
`liveupdate_unregister_file_handler()` 内部被调用（该函数已经持有写锁，详见
6.10 节），用 `lockdep_assert_held_write` 把"调用者必须持锁"这一隐含契约
显式地写进代码，能在开发/调试阶段第一时间捕获"忘记持锁就调用"的编程错误。

最外层的 `liveupdate_unregister_flb()` 则是单个依赖关系的撤销入口——它自己负责
持有写锁，然后委托给 `luo_flb_unregister_one`：

```c
void liveupdate_unregister_flb(struct liveupdate_file_handler *fh,
                               struct liveupdate_flb *flb)
{
        if (!liveupdate_enabled())
                return;

        guard(rwsem_write)(&luo_register_rwlock);

        luo_flb_unregister_one(fh, flb);
}
```

这里又出现了一个值得留意的"分层加锁"细节：`liveupdate_unregister_flb`（公开 API，
独立加锁）与 `luo_flb_unregister_all`（内部辅助函数，要求调用者已持锁）共享同一个
`luo_flb_unregister_one` 实现，却采取了完全不同的加锁责任划分——前者"自己加锁"，
后者"要求调用者已加锁"。这种差异不是随意的，而是分别匹配了它们各自的调用场景：
`liveupdate_unregister_flb` 是子系统在模块卸载时**单独**调用的公开接口（自然需要
自己负责加锁），而 `luo_flb_unregister_all` 是 LUO 核心在"批量销毁某个 file handler
的全部 FLB 依赖"这一更大操作的**子步骤**（此时锁已经在更外层被持有，重复加锁反而
会造成 ABBA 死锁或递归死锁）。理解这种"看似相似却分工不同"的函数对，是读懂
LUO 整个加锁体系的关键。

### 7.8 序列化与反序列化：FLB 的 FDT 节点

最后一块拼图是 FLB 状态如何穿越 kexec——这部分代码与第 5 章 `luo_session_setup_outgoing/incoming`
几乎是"一个模子刻出来的"，因为它们共用同一套 KHO FDT 基础设施。先看 ABI 层
（`include/linux/kho/abi/luo.h:193-240`）：

```c
#define LIVEUPDATE_FLB_COMPAT_LENGTH    48

#define LUO_FDT_FLB_NODE_NAME   "luo-flb"
#define LUO_FDT_FLB_COMPATIBLE  "luo-flb-v1"
#define LUO_FDT_FLB_HEADER      "luo-flb-header"

struct luo_flb_header_ser {
        u64 pgcnt;
        u64 count;
} __packed;

struct luo_flb_ser {
        char name[LIVEUPDATE_FLB_COMPAT_LENGTH];
        u64 data;
        u64 count;
} __packed;
```

画出这块序列化内存区域的布局图（与第 8 章即将详细展开的"内存布局精解"的写法一致，
这里先给出 FLB 专属的版本）：

```
   物理页帧（LUO_FLB_PGCNT = 1 页 = 4096 字节，由 kho_alloc_preserve 分配）

   偏移 0x000  ┌─────────────────────────────────────┐
              │   struct luo_flb_header_ser           │
              │     u64 pgcnt   (本块占用的总页数)     │   16 字节
              │     u64 count   (luo_flb_ser 条目数)   │
   偏移 0x010  ├─────────────────────────────────────┤
              │   struct luo_flb_ser  ser[0]           │
              │     char name[48]  (compatible 字符串) │
              │     u64   data     (preserve() 句柄)   │   64 字节/条目
              │     u64   count    (引用计数)           │
              ├─────────────────────────────────────┤
              │   struct luo_flb_ser  ser[1]           │
              │           ...                          │
              ├─────────────────────────────────────┤
              │           ...   ser[LUO_FLB_MAX-1]     │
   偏移 0xFFF  └─────────────────────────────────────┘

   LUO_FLB_MAX = ((1 << PAGE_SHIFT) - sizeof(luo_flb_header_ser))
                  / sizeof(luo_flb_ser)
              = (4096 - 16) / 64 = 63   （整页布局，宏定义见 luo_flb.c:56-58）
```

可以看到，`LUO_FLB_PGCNT`/`LUO_FLB_MAX` 这一对宏定义（`luo_flb.c:56-58`）
精确地反算出"一页内存最多能塞下多少个 FLB 条目"——这与第 5 章 `LUO_SESSION_MAX`
的计算方式（`luo_session.c` 中的同名宏）使用了完全相同的"按页大小反推容量上限"
的设计技巧：与其在运行时动态扩容（增加复杂度、引入额外的内存分配失败处理路径），
不如固定一页、把容量上限直接焊死在编译期常量里——这对于"FLB 数量在系统中通常
只有个位数到几十个"这一先验认知而言，是一个非常务实的权衡。

#### 7.8.1 `luo_flb_setup_outgoing`：构建 FDT 节点 + 分配序列化缓冲区

```c
int __init luo_flb_setup_outgoing(void *fdt_out)
{
        struct luo_flb_header_ser *header_ser;
        u64 header_ser_pa;
        int err;

        header_ser = kho_alloc_preserve(LUO_FLB_PGCNT << PAGE_SHIFT);
        if (IS_ERR(header_ser))
                return PTR_ERR(header_ser);

        header_ser_pa = virt_to_phys(header_ser);

        err = fdt_begin_node(fdt_out, LUO_FDT_FLB_NODE_NAME);
        err |= fdt_property_string(fdt_out, "compatible",
                                   LUO_FDT_FLB_COMPATIBLE);
        err |= fdt_property(fdt_out, LUO_FDT_FLB_HEADER, &header_ser_pa,
                            sizeof(header_ser_pa));
        err |= fdt_end_node(fdt_out);

        if (err)
                goto err_unpreserve;

        header_ser->pgcnt = LUO_FLB_PGCNT;
        luo_flb_global.outgoing.header_ser = header_ser;
        luo_flb_global.outgoing.ser = (void *)(header_ser + 1);
        luo_flb_global.outgoing.active = true;

        return 0;

err_unpreserve:
        kho_unpreserve_free(header_ser);

        return err;
}
```

走读这个函数的关键动作序列：

1. `kho_alloc_preserve(LUO_FLB_PGCNT << PAGE_SHIFT)`——一次性向 KHO 申请并登记
   一整页"将穿越 kexec 存活下来"的物理内存，返回它的内核虚拟地址；
2. `virt_to_phys()` 把它转换成物理地址——因为 FDT 中只能记录物理地址（新内核
   启动时还没有建立旧内核的虚拟地址映射，只有物理地址在 KHO 的"早期内存"阶段
   才有意义）；
3. 用三个 `fdt_*` 调用建立一个名为 `"luo-flb"` 的 FDT 子节点，写入
   `compatible = "luo-flb-v1"` 属性和指向序列化缓冲区物理地址的 `"luo-flb-header"`
   属性——这与第 5 章 `luo_session_setup_outgoing` 中"节点名 + compatible +
   数据指针"的三件套写法完全一致，是 LUO 在 FDT 组织上坚持的统一惯例；
4. **错误聚合**：`err |= fdt_property_string(...)` 这种用按位或累积多个
   `fdt_*` 调用返回值的写法（同样在第 5 章见过），利用了 `fdt_*` 系列函数
   "失败时返回负数、成功时返回 0"的约定——只要中间有一次失败，最终的 `err`
   就一定非零，不需要在每一步后面都写 `if (err) goto`，是一种"延迟检查、
   一次性判定"的简洁写法，代价是无法精确知道是哪一步失败的（但对于 FDT 构建
   这种"要么全部成功、要么全部失败重启"的场景，这点信息损失完全可以接受）；
5. 失败时 `kho_unpreserve_free(header_ser)`——撤销 KHO 登记并释放内存，
   是"分配与撤销成对出现"的范例；
6. 成功后把 `header_ser`、`(header_ser + 1)`（利用指针算术跳过头部、直接得到
   条目数组首地址——与第 5 章 `luo_session_setup_outgoing` 中同样的指针算术
   手法）保存进全局变量 `luo_flb_global.outgoing`，并置位 `active = true`——
   这个标志位正是 7.5 节 `luo_flb_retrieve_one` 中 `if (!fh->active)` 检查
   所依赖的"数据是否就绪"的开关。

#### 7.8.2 `luo_flb_setup_incoming`：从 FDT 中找回缓冲区

```c
int __init luo_flb_setup_incoming(void *fdt_in)
{
        struct luo_flb_header_ser *header_ser;
        int err, header_size, offset;
        const void *ptr;
        u64 header_ser_pa;

        offset = fdt_subnode_offset(fdt_in, 0, LUO_FDT_FLB_NODE_NAME);
        if (offset < 0) {
                pr_err("Unable to get FLB node [%s]\n", LUO_FDT_FLB_NODE_NAME);

                return -ENOENT;
        }

        err = fdt_node_check_compatible(fdt_in, offset,
                                        LUO_FDT_FLB_COMPATIBLE);
        if (err) {
                pr_err("FLB node is incompatible with '%s' [%d]\n",
                       LUO_FDT_FLB_COMPATIBLE, err);

                return -EINVAL;
        }

        header_size = 0;
        ptr = fdt_getprop(fdt_in, offset, LUO_FDT_FLB_HEADER, &header_size);
        if (!ptr || header_size != sizeof(u64)) {
                pr_err("Unable to get FLB header property '%s' [%d]\n",
                       LUO_FDT_FLB_HEADER, header_size);

                return -EINVAL;
        }

        header_ser_pa = get_unaligned((u64 *)ptr);
        header_ser = phys_to_virt(header_ser_pa);

        luo_flb_global.incoming.header_ser = header_ser;
        luo_flb_global.incoming.ser = (void *)(header_ser + 1);
        luo_flb_global.incoming.active = true;

        return 0;
}
```

这是 outgoing 路径的逆操作，遵循"**逐层验证、任一环节失败立刻返回明确错误**"的
保守策略：

1. `fdt_subnode_offset` 找到 `"luo-flb"` 子节点——找不到说明旧内核根本没有
   FLB 数据需要传递（可能是一次"冷启动"而非 kexec 唤醒），返回 `-ENOENT`；
2. `fdt_node_check_compatible` 校验 `compatible` 字符串——这是
   **跨内核版本 ABI 兼容性检查**的关键一环：如果新内核的 `LUO_FDT_FLB_COMPATIBLE`
   与旧内核序列化时写入的字符串不匹配（说明序列化结构发生了不兼容的变更），
   立刻返回 `-EINVAL` 并放弃解析——这正是第 5/8 章反复强调的"compatible string +
   版本号递增"ABI 演进策略在 FLB 子系统里的具体落地；
3. `fdt_getprop` 读取 `"luo-flb-header"` 属性，并验证其长度**精确等于**
   `sizeof(u64)`——这个长度校验进一步确保了"即使 compatible 字符串偶然匹配，
   属性本身的二进制布局也必须严丝合缝"，是双重保险；
4. `get_unaligned((u64 *)ptr)`——FDT 属性数据在内存中的对齐方式不被保证
   （它来自一段被打包的二进制 blob，可能出现在任意字节偏移上），使用
   `get_unaligned` 安全地读取一个可能未对齐的 64 位整数，避免在某些严格要求对齐
   访问的体系结构（如部分 ARM 配置）上触发对齐异常；
5. `phys_to_virt` 把物理地址换算回虚拟地址（此时新内核的直接映射已经建立），
   填充 `luo_flb_global.incoming` 三元组、置位 `active = true`。

可以看到，`setup_outgoing`/`setup_incoming` 这一对函数与第 5 章 `luo_session`
对应函数之间存在着近乎一一对应的代码结构相似性——这并非偶然重复，而是因为它们
本质上在解决同一个子问题（"如何把一段不透明的元数据通过 FDT 安全地从旧内核传递
到新内核"），LUO 选择让所有子模块都遵循同一套 FDT 节点组织规范，使得开发者在
读懂一处代码后，几乎可以"免学习成本"地读懂其余各处——这是优秀内核子系统在
内部一致性上的典范。

#### 7.8.3 `luo_flb_serialize`：只序列化"活跃"的 FLB

```c
void luo_flb_serialize(void)
{
        struct luo_flb_header *fh = &luo_flb_global.outgoing;
        struct liveupdate_flb *gflb;
        int i = 0;

        guard(rwsem_read)(&luo_register_rwlock);
        list_private_for_each_entry(gflb, &luo_flb_global.list, private.list) {
                struct luo_flb_private *private = luo_flb_get_private(gflb);

                if (private->outgoing.count > 0) {
                        strscpy(fh->ser[i].name, gflb->compatible,
                                sizeof(fh->ser[i].name));
                        fh->ser[i].data = private->outgoing.data;
                        fh->ser[i].count = private->outgoing.count;
                        i++;
                }
        }

        fh->header_ser->count = i;
}
```

这个函数在 `liveupdate_reboot()` 流程中、`kho_finalize()` 之前被调用（与
`luo_session_serialize`/`luo_file_serialize` 处于同一调用阶段——详见第 3 章
初始化/关闭时序图），它的核心逻辑只有一个判断：**`if (private->outgoing.count > 0)`**——

只有那些"在本次旧内核生命周期中，至少被一个文件 preserve 过"的 FLB，才会被
写入序列化缓冲区。这个看似简单的过滤条件，恰恰是整个 FLB 设计"按需保存"理念
的最终体现：

* 如果某个子系统**注册**了一个 FLB（比如 VFIO 模块加载时调用了
  `liveupdate_register_flb`），但这次关机重启时**一个 VFIO 设备都没有被打开/preserve**，
  那么 `outgoing.count` 始终是 0，这个 FLB 根本不会出现在序列化数据中——
  新内核里这个 FLB 的 `incoming.active` 对应的查找会直接落空（`-ENOENT`），
  不会有任何多余的内存分配或恢复动作。
* 这与 5.7 节 `luo_session_serialize`"只序列化非空 session"的过滤逻辑、
  以及第 6 章 `luo_file_serialize`"只序列化曾经 preserve 成功的文件"的过滤逻辑，
  共同构成了贯穿 LUO 全局的一条设计公理：**"序列化只为真正存在的东西买单"**——
  这既节省了宝贵的、必须穿越 kexec 存活下来的物理内存（这部分内存在新内核启动
  之前是不能被释放或复用的"黄金不动产"），也让新内核的恢复路径可以用最简单的
  "找不到就跳过"逻辑natural 地处理"那些根本不存在的可选依赖"。

`strscpy` 而不是 `strcpy`/`strncpy`——这是现代内核坚持的字符串拷贝惯例，
保证目标缓冲区始终以 `NUL` 结尾且不会发生缓冲区溢出，即使源字符串长度恰好等于
或超过 `LIVEUPDATE_FLB_COMPAT_LENGTH`（48 字节）也不会破坏后续的 `data`/`count`
字段——这也是为什么 `compatible` 字符串的长度限制必须被严格遵守（详见后面第 8 章
关于 ABI 字符串字段定长设计的讨论）。

### 7.9 小结：FLB 是 LUO 三层模型里"承上启下"的中间层

回顾整章内容，FLB 子系统虽然代码量不大（666 行），却是整个 LUO 架构里
"概念密度"最高的模块之一——它必须同时妥善处理：

1. **N : M 的依赖拓扑**（一个 handler 可依赖多个 FLB，一个 FLB 可被多个 handler
   依赖）——通过 `luo_flb_link` 中间节点 + 全局/局部双链表解决；
2. **outgoing/incoming 两套独立但结构对称的引用计数状态机**——通过
   `luo_flb_private_state` 模板复用 + "从 0 累加 / 从 N 递减"两种相反的计数策略解决；
3. **何时真正调用四个回调**这一时机判定问题——通过"计数归零触发"（outgoing 侧）
   和"懒加载 + finish 前补做"（incoming 侧）两套互补机制解决；
4. **跨 kexec 的元数据传递**——复用与 session 子系统完全一致的 FDT 节点组织规范，
   只序列化"非空"对象。

把它放回第 3 章给出的三层模型坐标系中看，FLB 处在 **"File Handler"与"全局共享资源"
之间的桥梁位置**——它让"文件级"的 preserve/retrieve/finish 事件能够被聚合、计数、
转化为"全局级"对象的创建与销毁时机。没有 FLB，类似 IOMMU 域这样的全局资源要么
必须被笨拙地塞进某一个"代表性文件"的私有状态里（导致生命周期与具体文件的存亡
强耦合，外加前述的"重复保存"问题），要么子系统必须自己手搓一套引用计数逻辑
（重复造轮子、且容易在边界条件上出错）。FLB 用一套通用、经过严格测试的引用计数
框架，把这件事从"每个子系统各自为政"统一成"声明依赖、实现回调、其余交给 LUO"——
这正是一个优秀基础设施层应有的样子：**把困难且容易出错的部分（生命周期与并发管理）
留给框架，把简单且各不相同的部分（如何保存、如何恢复）留给调用者**。

---

## 第 8 章 序列化 ABI 与内存布局精解

> 本章源码：`include/linux/kho/abi/luo.h`（247 行）、`include/linux/kho/abi/memfd.h`（93 行）、
> `include/linux/kho/abi/kexec_handover.h`（`kho_vmalloc` 相关定义）

前几章在解读 `luo_session.c`/`luo_file.c`/`luo_flb.c` 时，已经多次接触到
"`__packed` 序列化结构体 + FDT 节点"这一组合——本章把它们集中起来，从
"按字节计算的内存布局"这个更微观的视角重新审视一遍，并系统化地总结 LUO 的
ABI 演进策略。这是理解"LUO 数据如何在物理内存中真实存在"的最后一块拼图，
也是为第 9 章剖析 `mm/memfd_luo.c` 做铺垫——memfd 的序列化结构体
`struct memfd_luo_ser` 正是本章 ABI 体系里最复杂的一个实例。

### 8.1 自顶向下：一棵 FDT 究竟长什么样

`include/linux/kho/abi/luo.h` 开头的大段 DOC 注释（`luo.h:8-103`）用一段
**Devicetree 语法范例**，一次性勾勒出了整个 LUO 状态在 FDT 中的组织形态
（`luo.h:30-45`）：

```
/ {
    compatible = "luo-v1";
    liveupdate-number = <...>;

    luo-session {
        compatible = "luo-session-v1";
        luo-session-header = <phys_addr_of_session_header_ser>;
    };

    luo-flb {
        compatible = "luo-flb-v1";
        luo-flb-header = <phys_addr_of_flb_header_ser>;
    };
};
```

这棵树异常"扁平"——根节点下只有两个子节点 `luo-session`、`luo-flb`，
且每个子节点自身也只携带两个属性（`compatible` 字符串 + 一个指向某个序列化数据块
"头部"的物理地址 `u64`）。这种刻意的扁平化设计回答了一个很自然的问题：
**为什么不把所有的 session、所有的 file、所有的 FLB 都直接表示成 FDT 节点的层级结构，
而是只在 FDT 里放两个"指针属性"，把真正的数据丢到旁边的裸内存块里？**

答案藏在 FDT 这种格式本身的取舍里——FDT（Flattened Device Tree）被设计成
**易于在内核引导早期、尚未建立完整内存管理设施时被快速解析**的紧凑二进制格式
（`libfdt` 库的 API 集中在"按节点名/属性名查找"上，而不擅长"遍历成千上万个
同构条目并做结构化解码"）。如果把可能多达数百个 session、数千个文件的元数据
都塞进 FDT 节点/属性里，会带来三个问题：

1. **FDT 自身大小暴涨**：FDT 必须整体作为一段连续内存被 `kho_add_subtree`
   登记和传递（参见第 2 章 `LUO_FDT_SIZE = PAGE_SIZE` 的限制——整个 LUO 的 FDT
   被设计为只占**一页**），节点和属性数量越多，FDT header、字符串表、结构块就
   越膨胀，很容易突破这个预算；
2. **解析效率低**：`libfdt` 对深层嵌套结构的遍历是 O(n) 的线性扫描，
   节点数量一旦达到成百上千，启动阶段的解析开销会变得不可忽视；
3. **类型系统弱**：FDT 属性本质上是无类型的字节数组，想要表达
   "一个定长结构体数组"这种规整的二进制布局，远不如直接在裸内存上摆放
   `__packed` 结构体数组来得自然和高效。

于是 LUO 选择了一种**"FDT 做索引、裸内存做仓库"**的混合架构：FDT 本身只
保留**固定数量**的顶层节点（`luo-session`、`luo-flb`，未来可能还有更多顶层类别），
每个节点只携带一个指向"裸内存数据块物理地址"的属性；而真正数量可变、需要
高效随机访问的数组数据（session 列表、file 列表、FLB 列表、memfd 的 folio
列表……），全部交给 `kho_alloc_preserve()`/`kho_preserve_vmalloc()` 分配的
**独立物理页块**来承载，用简单的 `__packed` 结构体数组直接摆放，新内核里
通过指针算术（`header + 1`）和数组下标直接访问，完全不需要 FDT 解析开销。

这正是第 5、7 两章里反复出现的"**头部 + 紧随其后的条目数组**"内存布局模式
（`luo_session_header_ser` + `luo_session_ser[]`、`luo_flb_header_ser` +
`luo_flb_ser[]`）背后的根本原因——它不是某个函数的局部技巧，而是整个 LUO
ABI 架构在"FDT 索引层"与"裸内存数据层"之间划分职责的必然结果。整体关系可以
画成下面这张"森林图"：

```
                    KHO Entry "LUO"（一棵 FDT，≤ 1 页）
   ┌────────────────────────────────────────────────────────────┐
   │  / (compatible="luo-v1", liveupdate-number=<N>)             │
   │   ├── luo-session (compatible="luo-session-v2")             │
   │   │     └─ luo-session-header = <PA_1>  ───────────────┐    │
   │   └── luo-flb (compatible="luo-flb-v1")                │    │
   │         └─ luo-flb-header = <PA_2>  ───────────────┐   │    │
   └────────────────────────────────────────────────────│───│────┘
                                                         │   │
              "仓库"：FDT 之外的独立裸内存数据块            │   │
   ┌─────────────────────────────────────────────────────┘   │
   │                                                          │
   ▼  PA_1                                                   ▼  PA_2
┌──────────────────────────────┐            ┌──────────────────────────────┐
│ luo_session_header_ser        │            │ luo_flb_header_ser            │
│   count = M                   │            │   pgcnt / count = K           │
├──────────────────────────────┤            ├──────────────────────────────┤
│ luo_session_ser[0]            │            │ luo_flb_ser[0]                │
│   name[64] / file_set_ser ────┼─┐          │   name[48] / data / count     │
│ luo_session_ser[1]            │ │          │ luo_flb_ser[1]                │
│   ...                         │ │          │   ...                         │
│ luo_session_ser[M-1]          │ │          │ luo_flb_ser[K-1]              │
└──────────────────────────────┘ │          └──────────────────────────────┘
                                  │
              file_set_ser.files │ （又一层间接：指向另一块独立内存）
                                  ▼
                  ┌──────────────────────────────┐
                  │ luo_file_ser[0]               │
                  │   compatible[48] / data /     │
                  │   token                       │
                  │ luo_file_ser[1]               │
                  │   ...                         │
                  │ luo_file_ser[count-1]         │
                  └──────────────────────────────┘
```

注意图中 `luo_session_ser.file_set_ser` 这一处**第三层间接**——session 的
序列化条目本身并不直接内嵌文件数组，而是通过 `struct luo_file_set_ser`
（仅含 `files` 物理地址 + `count` 计数两个字段）再跳转到一块独立分配的内存。
这正是 5.6/5.7 节讨论过的"分离分配"策略：每个 session 的文件数量差异巨大
（有的 session 是空的，有的可能装着上百个 fd），如果把文件数组**内嵌**进
`luo_session_ser` 定长结构体，要么按最大可能数量预留空间（极大浪费），
要么变成变长结构体（破坏数组寻址的 O(1) 特性）。用一层指针间接，让
"session 元数据数组"和"每个 session 的文件元数据数组"成为两类可以独立伸缩、
独立分配的内存块，是处理"一对多、且'多'的数量高度可变"关系时的标准范式——
数据库设计中"主表 + 外键指向明细表"的思路与此异曲同工。

### 8.2 逐字段拆解：`__packed` 结构体的字节级内存布局

接下来我们把 ABI 头文件中定义的每一个 `__packed` 结构体摊开，画出精确到
字节偏移的内存布局表。**`__packed` 属性**是这里最关键的修饰符——它告诉编译器
**禁止为了对齐而插入任何填充字节（padding）**，结构体的内存布局严格按照成员
声明顺序、逐字节紧密排列。这是 ABI 结构体的生命线：如果不加 `__packed`，
编译器可能因为目标架构的对齐要求在成员之间插入数量不确定的 padding，
导致同一份数据在不同编译配置（甚至不同编译器版本）下产生不同的内存布局——
而 LUO 的核心约束恰恰是"旧内核写的数据，必须能被新内核**按位精确**重新解释"，
任何隐式 padding 都会让这个约束变成一场赌博。

#### 8.2.1 `struct luo_file_ser`（`luo.h:130-134`）—— 单个文件的序列化条目

```c
struct luo_file_ser {
        char compatible[LIVEUPDATE_HNDL_COMPAT_LENGTH];  /* = 48 */
        u64 data;
        u64 token;
} __packed;
```

```
   偏移   字段                    大小    说明
  ┌──────┬───────────────────────┬──────┬─────────────────────────────┐
  │ 0x00 │ compatible[48]         │ 48 B │ 文件 handler 的兼容性字符串   │
  │      │                        │      │ （查找新内核中对应 handler 的 │
  │      │                        │      │  唯一依据，例如"memfd-v2"）   │
  ├──────┼───────────────────────┼──────┼─────────────────────────────┤
  │ 0x30 │ data                   │  8 B │ ops->preserve() 返回的不透明  │
  │      │                        │      │ u64 句柄（如 folio 数组的     │
  │      │                        │      │ KHO vmalloc 描述符物理地址）  │
  ├──────┼───────────────────────┼──────┼─────────────────────────────┤
  │ 0x38 │ token                  │  8 B │ 用户态在 preserve_fd 时提供的 │
  │      │                        │      │ 标识符，用于 retrieve 时按图  │
  │      │                        │      │ 索骥找回这个文件               │
  └──────┴───────────────────────┴──────┴─────────────────────────────┘
   总大小：64 字节（48 + 8 + 8，因 __packed 无任何 padding，
            且恰好是 2 的整数次幂，便于数组按页对齐计算容量）
```

这个 64 字节的"整齐"大小并非偶然——`compatible` 字段的长度
`LIVEUPDATE_HNDL_COMPAT_LENGTH = 48`（`luo.h:120`）刻意选择了一个
"凑出整数倍总长度"的数值：`48 + 8 + 8 = 64`。后面会看到 `luo_session_ser`
（`64 + 16 = 80`）、`luo_flb_ser`（`48 + 8 + 8 = 64`）也都遵循类似的
"凑整字节数"惯例——这并非强制要求（`__packed` 数组并不需要每个元素大小是
2 的幂），但能让人在心算"N 个条目占多少字节、一页能放多少条目"时更直观，
也更容易让数组在 cache line（通常 64 字节）边界上对齐，减少跨缓存行访问——
这是一种"细节处的工程品味"，体现了 ABI 设计者对底层硬件特性的体贴。

#### 8.2.2 `struct luo_file_set_ser`（`luo.h:145-148`）—— 文件集合的间接层

```c
struct luo_file_set_ser {
        u64 files;
        u64 count;
} __packed;
```

```
   偏移   字段        大小    说明
  ┌──────┬───────────┬──────┬───────────────────────────────────┐
  │ 0x00 │ files      │  8 B │ luo_file_ser 数组的物理起始地址      │
  ├──────┼───────────┼──────┼───────────────────────────────────┤
  │ 0x08 │ count      │  8 B │ 数组中的条目数量                     │
  └──────┴───────────┴──────┴───────────────────────────────────┘
   总大小：16 字节 —— 一个最简化的"指针 + 长度"二元组（fat pointer）
```

这是整个 ABI 体系里最朴素的一个结构体——本质上就是 C 语言里
`{ ptr, len }` 这种"胖指针"模式在跨进程/跨内核序列化场景下的物理地址版本。
之所以专门为它定义一个命名结构体而不是直接把 `u64 files; u64 count;`
内嵌进 `luo_session_ser`，是为了让"指向文件数组"这件事成为一个**可复用、
可独立命名**的概念——从注释可以看到，它的定位是"代表一个文件集合"，
这种语义上的独立封装，使得未来如果出现"不属于任何 session、却也需要表示
一组文件"的场景（比如某种全局文件池），可以直接复用这个类型而不需要重新发明。

#### 8.2.3 `struct luo_session_header_ser` + `struct luo_session_ser`（`luo.h:170-190`）

```c
struct luo_session_header_ser {
        u64 count;
} __packed;

struct luo_session_ser {
        char name[LIVEUPDATE_SESSION_NAME_LENGTH];  /* = 64 */
        struct luo_file_set_ser file_set_ser;
} __packed;
```

```
  luo_session_header_ser                总大小：8 字节
  ┌──────┬───────────┬──────┬─────────────────────────────────┐
  │ 0x00 │ count      │  8 B │ 紧随其后的 luo_session_ser 条目数  │
  └──────┴───────────┴──────┴─────────────────────────────────┘

  luo_session_ser                       总大小：80 字节 (64 + 16)
  ┌──────┬───────────────────────┬──────┬──────────────────────────┐
  │ 0x00 │ name[64]               │ 64 B │ session 名称（用户态字符串） │
  ├──────┼───────────────────────┼──────┼──────────────────────────┤
  │ 0x40 │ file_set_ser.files     │  8 B │ 嵌入的 luo_file_set_ser:   │
  │ 0x48 │ file_set_ser.count     │  8 B │ 指向文件数组 + 文件数量      │
  └──────┴───────────────────────┴──────┴──────────────────────────┘
```

这里有一个值得注意的"组合而非指针"的设计选择：`luo_session_ser` 把
`luo_file_set_ser`**直接内嵌**（按值包含）而不是用指针引用。结合 8.2.2
节的分析——这意味着"session 名称"与"指向文件数组的胖指针"被打包在**同一个
80 字节的定长记录**里，新内核遍历 `luo_session_ser[]` 数组时，每读到一条
就能立即同时拿到 session 名字和文件数组位置，不需要再做一次额外的指针跳转
和地址转换。这是"内嵌小型定长结构体 vs. 链接大型变长数据"两种策略的
精准取舍：`luo_file_set_ser` 本身只有 16 字节、大小固定，内嵌它几乎不增加
任何存储成本；而真正"大小可变、可能很大"的 `luo_file_ser[]` 数组，
则通过 `files` 物理地址做独立分配——内嵌"小而定长"的部分、外链"大而变长"
的部分，是布局设计中平衡"访问局部性"与"空间利用率"的常见手法。

`name[64]`——`LIVEUPDATE_SESSION_NAME_LENGTH = 64`（`include/uapi/linux/liveupdate.h:47`）
与第 4 章解读 `liveupdate_ioctl_create_session.name` 字段时见到的长度限制完全一致——
这正是"用户态 ioctl 接口里的名字长度"与"内核序列化 ABI 里的名字字段长度"
**刻意保持一致**的体现：用户通过 `LIVEUPDATE_IOCTL_CREATE_SESSION` 传入的名字，
原样拷贝进 `luo_session` 内部状态，再原样序列化进 `luo_session_ser.name`——
全链路只有一种长度限制、一处定义来源（`#define`），从根本上杜绝了"用户态接口
允许 64 字节，内核序列化却只能装下 32 字节"这种隐藏的截断 bug。

#### 8.2.4 `struct luo_flb_header_ser` + `struct luo_flb_ser`（`luo.h:214-240`）

这一对结构体已经在 7.8 节详细画过布局图，这里不再重复，仅强调一处与
`luo_session_header_ser` 的微妙差异：`luo_flb_header_ser` 比 `luo_session_header_ser`
**多一个 `pgcnt` 字段**：

```c
struct luo_flb_header_ser {
        u64 pgcnt;   /* 整个数据块占用的总页数 */
        u64 count;   /* 紧随其后的条目数量 */
} __packed;
```

为什么 session 的 header 不需要记录页数，FLB 的却需要？答案在于二者的内存
分配策略不同：

* **session 的文件数组**：由 `luo_session_setup_outgoing` 按"实际需要的字节数
  向上取整到页"动态分配（具体做法可回顾第 5 章对 `kho_alloc_preserve` 调用处的
  分析），新内核侧不需要"释放回收"这块内存的责任——它会在 `luo_session_finish`
  时通过 `kho_restore_free`/`kho_unpreserve_free` 之类的接口按 `count`
  反推大小后释放；
* **FLB 数组**：固定分配 `LUO_FLB_PGCNT`（恒为 1 页），看似 `pgcnt` 字段是多余的
  （永远是常量 1）。但保留这个字段，是一种面向未来的 ABI 弹性设计——如果未来
  FLB 数量上限需要扩展到超过一页（即修改 `LUO_FLB_PGCNT` 常量），新内核仍然能够
  通过读取 `header_ser->pgcnt` 而不是依赖编译期常量来正确计算这块内存的总大小，
  从而正确地完成回收。这是一个"宁可现在多存 8 字节冗余信息，也不要把未来的
  扩展性焊死在编译期常量上"的远见型设计——本质上与 ABI 演进策略中"留出预留字段"
  的思想是同一类工程直觉的不同表现形式。

### 8.3 跨越裸内存边界：`struct kho_vmalloc` 与"可伸缩大数组"的传递机制

ABI 体系里还有一类比"定长结构体数组"更复杂的数据——大小可能从几十到几十万
不等的、需要跨 kexec 传递的**大数组**，典型代表就是第 9 章要详细剖析的
memfd 的 folio 元数据数组（一个几十 GB 的大内存文件可能对应数百万个 folio）。
这类数据天然地不适合用"提前一次性分配一整块连续物理内存"的方式处理——
连续的大块物理内存在系统运行一段时间后往往很难凑出来（内存碎片化）。

KHO 框架为此专门设计了 `struct kho_vmalloc` 描述符（`include/linux/kho/abi/kexec_handover.h:173-178`）：

```c
struct kho_vmalloc {
        DECLARE_KHOSER_PTR(first, struct kho_vmalloc_chunk *);
        unsigned int total_pages;
        unsigned short flags;
        unsigned short order;
};
```

它的核心思想是"**用一条物理页链表模拟一段虚拟连续的大内存区域**"——这正是
`vmalloc()` 本身的实现思路（用一段连续的虚拟地址，背后映射到一组不连续的
物理页）在跨 kexec 序列化场景下的复刻。`kho_vmalloc` 描述符本身只有
16 字节（一个 `DECLARE_KHOSER_PTR` 联合体 8 字节 + `total_pages` 4 字节 +
`flags`/`order` 各 2 字节），通过 `first` 指向一条 `kho_vmalloc_chunk` 链表：

```c
struct kho_vmalloc_hdr {
        DECLARE_KHOSER_PTR(next, struct kho_vmalloc_chunk *);
};

#define KHO_VMALLOC_SIZE \
        ((PAGE_SIZE - sizeof(struct kho_vmalloc_hdr)) / sizeof(u64))

struct kho_vmalloc_chunk {
        struct kho_vmalloc_hdr hdr;
        u64 phys[KHO_VMALLOC_SIZE];
};

static_assert(sizeof(struct kho_vmalloc_chunk) == PAGE_SIZE);
```

画成图就是一条"恰好一页大小的链表节点"串起来的链：

```
   kho_vmalloc                 kho_vmalloc_chunk #0（恰好 1 页 = 4096 字节）
  ┌──────────────┐            ┌─────────────────────────────────────┐
  │ first ───────┼───────────►│ hdr.next ───┐    （指向链表下一个节点） │
  │ total_pages  │            │ phys[0]      │  物理页帧地址数组         │
  │ flags        │            │ phys[1]      │  （每项 8 字节，          │
  │ order        │            │ ...          │   共 (4096-8)/8 = 511 项）│
  └──────────────┘            │ phys[510]    │                          │
                              └──────┬───────┘
                                     │
                                     ▼
                       kho_vmalloc_chunk #1
                      ┌─────────────────────────────────────┐
                      │ hdr.next ───┐  → ... → 最后一个节点   │
                      │ phys[0..510] │  继续记录物理页帧地址    │
                      └──────────────┘
```

`DECLARE_KHOSER_PTR`（`kexec_handover.h:120-125`）是这套机制里另一个值得细看的
小工具——一个"自描述的可序列化指针"联合体：

```c
#define DECLARE_KHOSER_PTR(name, type)  \
        union {                        \
                u64 phys;              \
                type ptr;              \
        } name
```

它让同一段内存**在写入时被当作虚拟指针操作（`ptr`）、在序列化/反序列化时
被当作物理地址操作（`phys`）**——配套的 `KHOSER_STORE_PTR`/`KHOSER_LOAD_PTR`
宏负责在两种解释之间做 `virt_to_phys`/`phys_to_virt` 转换。这本质上与
本章反复出现的"在结构体里存物理地址、使用时再转换成虚拟地址"模式是同一件事，
只不过 `DECLARE_KHOSER_PTR` 把"存什么、转换成什么"通过联合体的类型系统
显式地表达了出来——指针字段不再是裸的 `u64`，而是一个"自带类型提示"的
受限工具，减少了开发者手写转换代码时类型搞错的概率（`KHOSER_STORE_PTR`
内部还用 `typecheck()` 做了编译期类型校验）。

把这一切串起来看，`memfd_luo_ser`（详见第 9 章）里的 `folios` 字段之所以
被声明成 `struct kho_vmalloc` 而不是简单的 `u64 phys + u64 count`，正是
因为 folio 元数据数组的大小可能远远超过一页——必须借助这套"分段链表"机制，
把一个逻辑上连续的大数组拆分到许多物理页中分别保存、再在新内核里通过
`kho_restore_vmalloc()` 重新拼接成一段连续的虚拟地址空间。这是 ABI
设计中"为不同量级的数据选择不同载体"思想的极致体现——小型定长元数据用
`__packed` 结构体数组直接摆放，大型可变数组则借助 `kho_vmalloc` 这种
更复杂、但能突破单页限制的机制。

### 8.4 ABI 版本兼容策略：compatible 字符串 + 版本号递增

本章开头引用的 DOC 注释中有这样一段措辞严厉的"契约声明"（`luo.h:16-24`）：

```
This interface is a contract. Any modification to the FDT structure, node
properties, compatible strings, or the layout of the `__packed` serialization
structures defined here constitutes a breaking change. Such changes require
incrementing the version number in the relevant `_COMPATIBLE` string to
prevent a new kernel from misinterpreting data from an old kernel.

Changes are allowed provided the compatibility version is incremented;
however, backward/forward compatibility is only guaranteed for kernels
supporting the same ABI version.
```

把它翻译成工程语言，这段声明确立了 LUO ABI 演进的三条铁律：

1. **任何**改变内存布局的修改（增删字段、改变字段顺序、改变字段类型/长度，
   甚至包括 FDT 节点名/属性名的改变）都被视为**破坏性变更（breaking change）**；
2. 破坏性变更**必须**伴随相应 `_COMPATIBLE` 字符串中版本号的递增
   （例如 `"luo-session-v1"` → `"luo-session-v2"`——这正是当前代码库里
   `LUO_FDT_SESSION_COMPATIBLE` 已经走到 v2 的真实例证，`luo.h:156`）；
3. **新旧内核之间的兼容性，仅在 ABI 版本号完全相同时才被保证**——
   这是一种"显式声明、拒绝猜测"的策略：LUO 不去尝试做"新内核兼容旧版本数据"
   这种复杂的多版本共存逻辑，而是用 `fdt_node_check_compatible()` 在
   反序列化的第一道关卡就严格比对字符串，**任何不匹配都直接判定为不兼容、
   拒绝解析**（回顾 7.8.2 节 `luo_flb_setup_incoming` 中的
   `fdt_node_check_compatible` 调用，以及第 5 章 session 反序列化流程中的
   同类检查）。

这种策略初看似乎"简单粗暴"——为什么不像很多用户态 RPC 协议（Protobuf、
Thrift）那样设计一套精致的向前/向后兼容字段演进规则（可选字段、保留字段号、
默认值等）？答案要回到 LUO 所处的具体场景：**它解决的是"同一台机器上，
旧内核到新内核"这一单跳、可控的转换**，而不是"分布式系统里无数个版本各异的
节点互相通信"这种长期共存、必须容忍多版本混杂的场景。在 LUO 的世界里：

* 旧内核与新内核的 ABI 版本完全由**同一次系统升级操作**中加载的两个内核
  镜像决定——理论上运维侧（无论是手动 kexec 还是自动化的热升级流水线）
  天然地知道两者的版本搭配，可以在升级前就校验"目标内核是否支持源内核序列化
  出来的 ABI 版本"；
* 一旦版本不匹配，**最安全的应对方式就是直接拒绝**——因为这意味着旧内核
  序列化出的数据，新内核根本不可能正确解读其内存布局，强行解析只会读出
  随机的垃圾数据、引发更难调试的内存损坏或崩溃。**早失败、明确失败**远胜于
  "看似成功但悄悄读错数据"。

这与本书在第 6 章总结的"宁可泄漏不做不安全 undo"哲学，本质上是同一种
工程价值观的不同切面：**在面对"无法确定正确性"的局面时，LUO 的选择始终是
"停下来、报错、把恢复的决定权交还给运维方"，而不是凭着不完整的信息继续往前冲**。
对一个负责"跨内核版本传递关键状态"的基础设施而言，这种保守策略恰恰是
它能够被信任、被用于生产环境的前提。

把"compatible 字符串"理解为一种**自描述的版本标签**，整个 LUO ABI 体系
就可以归纳成一张表：

| 层级 | compatible 字符串（当前版本） | 对应序列化结构体 | 不兼容时的处理 |
|---|---|---|---|
| LUO 顶层 | `LUO_FDT_COMPATIBLE = "luo-v1"` | （根节点本身，无独立 `_ser` 结构体） | 整个 LUO FDT 解析失败，放弃恢复全部状态 |
| Session 节点 | `LUO_FDT_SESSION_COMPATIBLE = "luo-session-v2"` | `luo_session_header_ser`/`luo_session_ser`/`luo_file_set_ser` | session 子系统反序列化失败，记录错误日志（"宁可泄漏"哲学，详见第 11 章） |
| FLB 节点 | `LUO_FDT_FLB_COMPATIBLE = "luo-flb-v1"` | `luo_flb_header_ser`/`luo_flb_ser` | `luo_flb_setup_incoming` 直接返回错误，全局 incoming FLB 数据不可用 |
| 文件 handler | 各子系统自定义（如 `MEMFD_LUO_FH_COMPATIBLE = "memfd-v2"`） | 各子系统自定义（如 `memfd_luo_ser`） | `luo_file_deserialize` 中按 `compatible` 字符串查找 handler 失败，单个文件恢复失败 |
| FLB 对象 | 各子系统自定义（如 `LIVEUPDATE_TEST_FLB_COMPATIBLE`） | 各子系统自定义 | `luo_flb_retrieve_one` 中线性查找失败，返回 `-ENOENT` |

这张表格的最后一列其实揭示了一个精心设计的"**故障域隔离（fault isolation）**"
梯度：**版本不匹配造成的影响范围，与该层级在整个体系中的位置成正比**——
顶层 `compatible` 不匹配意味着整个 LUO 状态全部作废（最大故障域）；
Session/FLB 节点级别不匹配只影响该子系统；而最底层的"单个文件 handler"
或"单个 FLB 对象"的 `compatible` 不匹配，造成的影响被严格限制在**那一个
具体对象**身上——其余所有数据的恢复完全不受影响。这种"自顶向下逐级缩小
爆炸半径"的分层结构，使得"哪怕只升级了一个驱动模块、引入了不兼容的 ABI
变更"这种局部性的版本错配，也不会拖累整个系统的热升级流程——这是 LUO
之所以要把"全局唯一的 compatible"拆解成"逐层独立的 compatible"的根本动因，
也是阅读这份 ABI 文件时最值得提炼出的架构智慧。

### 8.5 小结：ABI 是 LUO"可被信任"的基石

本章看似只是在"数格子"——逐字节地画出每个结构体的内存布局——但这恰恰是
理解 LUO 为什么能够安全可靠地完成"跨内核版本传递状态"这一高风险操作的关键。
归纳起来，LUO 的序列化 ABI 设计始终围绕着三个互相支撑的支柱展开：

1. **`__packed` + 定长字段 = 确定性布局**：消除编译器引入的不确定性，
   保证"旧内核写的字节、新内核按位读出来还是同样的语义"；
2. **"头部 + 数组"+ "小型内嵌、大型外链"= 高效访问与灵活伸缩的折衷**：
   在"访问局部性"和"空间利用率/可伸缩性"之间，针对不同量级的数据做出
   不同的、恰到好处的取舍（小型定长信息内嵌、大型可变数据通过指针/`kho_vmalloc`
   外链）；
3. **逐层独立的 compatible 字符串 + 严格拒绝不匹配 = 故障域隔离**：
   把"version 不匹配"这种无法挽回的错误尽量限制在最小的影响范围内，
   用"显式拒绝"代替"模糊兼容"，把恢复的主动权留给可以做出更明智判断的
   运维方。

下一章我们将看到，这套抽象的 ABI 体系在 `mm/memfd_luo.c` 这一具体的
"内存文件保存与恢复"实现中，是如何被填充进实际的、运行时的代码逻辑——
folio 如何被 pin 住、序列化数组如何被填充、`kho_vmalloc` 如何被用于
传递可能多达数百万项的 folio 元数据，以及 page cache 如何在新内核里
被原样重建出来。

---

## 第 9 章 memfd 跨 session 保存恢复完整精读

> 本章源码：`mm/memfd_luo.c`（622 行），这是 LUO 框架在主线内核中**唯一**已落地的
> 文件保存类型实现，也是本文档前几章反复以"memfd 范例"讲解抽象框架时所指向的
> 真正源头。读完本章，第 6 章关于 `liveupdate_file_ops` 回调矩阵的讨论将从
> "这些回调大概要做什么"落实为"它们到底做了什么、为什么这么做"。

### 9.1 memfd 选择保存哪些属性：一份"非透明保存"的清单

`memfd_luo.c` 开头有一段近 60 行的 kernel-doc 注释（`memfd_luo.c:11-69`），
用近乎用户手册的笔调说明了"这次 live update 之后，你的 memfd 会变成什么样"。
这段说明本身就是理解整个实现之前必须读懂的"契约文档"——它直白地宣告了一个
关键设计立场（`memfd_luo.c:21-23`）：

```
The preservation is not intended to be transparent. Only select properties of
the file are preserved. All others are reset to default. The preserved
properties are described below.
```

**"保存不追求透明"**——这短短一句话其实划定了整个实现的工作量边界。
一个 `struct file`/`struct inode` 上挂着的属性多达数十个（权限位、时间戳、
锁状态、通知监听者、安全标签、cgroup 记账信息……），如果试图让 memfd
在 kexec 之后"看起来好像什么都没发生过"，工作量会呈指数级增长，且大量属性
本身就与"这是同一台机器上的同一个内核运行实例"这一假设绑定，根本无法
在跨内核重启的场景下保持原样（比如内核内部的锁对象地址、与某个已经不存在
的进程相关联的通知队列）。LUO 选择的是一种更现实、更可维护的策略：
**只保存"用户态真正关心、且能够被准确无误重建"的那一小撮核心属性**，
其余的一律明确告知用户"会被重置为默认值"，把选择权交还给用户态——
如果某个属性对应用至关重要，应用需要自己在 retrieve 之后显式地重新设置它
（文档里给出的范例正是 `FD_CLOEXEC` 标志，需要用户在恢复后自行调用 `fcntl()`
重新设置，`memfd_luo.c:65-68`）。

把整段文档提炼成一张"保存 vs. 不保存"对照表：

| 属性 | 是否保存 | 备注 |
|---|---|---|
| 文件内容（数据本身） | **保存** | 核心目标；空洞（hole）会在保存时被实际分配并填零 |
| 文件大小 `i_size` | **保存** | 精确恢复，包括稀疏区域 |
| 文件位置 `f_pos` | **保存** | 让应用可以从断点处继续读写 |
| 文件状态标志 | **保存（隐式）** | memfd 总是以 `O_RDWR \| O_LARGEFILE` 重新打开，恰好与原始状态一致 |
| Seals（密封标记） | **保存** | 仅限 `MEMFD_LUO_ALL_SEALS` 范围内的已知 seal 类型；遇到未知 seal 直接 `-EOPNOTSUPP` 拒绝保存 |
| `FD_CLOEXEC` | **不保存** | 必须在 retrieve 后用 `fcntl()` 显式重设 |
| 其他一切未提及的属性 | **不保存（重置为默认）** | 包括但不限于：权限、时间戳、通知、安全标签等 |

注意文档里还专门标注了两条"实现尚不成熟"的警示（`memfd_luo.c:25-31`）：
LUO API 本身还没有稳定下来，因此 memfd 保存的属性集合"也尚未稳定，可能发生
不兼容变更"；以及"目前不支持 `MFD_HUGETLB` 创建的 memfd"——这与第 6 章见过的
`memfd_luo_can_preserve()` 判定逻辑（9.2 节会展开）相呼应：HugeTLB 页面的
folio 管理路径与普通 shmem 页面差异巨大（不经过 page cache、分配粒度固定为
巨页），要把它们也纳入这套基于 `shmem_add_to_page_cache`/`memfd_pin_folios`
的通用实现需要专门的适配工作，当前版本选择直接拒绝，把这块留给未来扩展。

### 9.2 准入门槛：`memfd_luo_can_preserve` 与 `memfd_luo_get_id`

回顾第 6 章 6.1 节的回调矩阵表，`can_preserve`/`get_id` 是 `liveupdate_file_ops`
中两个看似不起眼、却处于"流程最前端"的钩子——它们决定"这个文件到底有没有资格
进入 LUO 的保存流程"以及"如何唯一标识它以防止重复保存"。memfd 对这两个回调
的实现极其简洁（`memfd_luo.c:580-591`）：

```c
static bool memfd_luo_can_preserve(struct liveupdate_file_handler *handler,
                                   struct file *file)
{
        struct inode *inode = file_inode(file);

        return shmem_file(file) && !inode->i_nlink;
}

static unsigned long memfd_luo_get_id(struct file *file)
{
        return (unsigned long)file_inode(file);
}
```

`memfd_luo_can_preserve` 的判定条件由两部分组成，分别守护着两条不同的边界：

1. **`shmem_file(file)`**——确认这确实是一个由 `shmem`（tmpfs 的内核内部实现）
   支持的文件。`memfd_create()` 创建出来的文件本质上就是匿名 `shmem` 文件，
   这条检查排除了"压根不是 memfd"的常规文件描述符（如果用户尝试把一个普通磁盘
   文件的 fd 传给 memfd 的 LUO 处理器，这里会直接拒绝）；另外正如 9.1 节提到的，
   `shmem_file()` 的判定天然就排除了 HugeTLB 文件（HugeTLB 走的是完全不同的
   `hugetlbfs` 路径），不需要额外的判断逻辑就自动满足了"暂不支持 HugeTLB"
   这条限制——这是一个"用一个已有的、语义恰好匹配的判断条件，顺带满足额外约束"
   的优雅复用范例。
2. **`!inode->i_nlink`**——确认这个 inode **没有任何硬链接**指向常规文件系统
   路径上的目录项。`memfd_create()` 创建出的 inode 在 `O_TMPFILE` 式的匿名状态下
   `i_nlink` 始终为 0；但用户态可以通过 `/proc/self/fd/N` 这条路径用
   `linkat()` 把一个 memfd "链接"进真实的文件系统命名空间，使其拥有一个
   真实的路径名。一旦发生这种情况，这个 inode 就不再是一个"纯粹活在内存里、
   生命周期完全由 fd 引用计数决定"的匿名对象，而是与底层文件系统产生了真实的
   关联——LUO 显然不可能、也不应该尝试去保存"一个挂在真实文件系统目录树上的
   inode"，那是完全不同的一套问题（涉及到磁盘 I/O、文件系统一致性、与
   其他不属于 LUO 管辖范围的进程共享等等）。这条检查精确地把"纯内存匿名对象"
   与"已经落地到文件系统的对象"区分开，确保 LUO 只接管它真正能够、也应该
   负责的那一类资源。

`memfd_luo_get_id` 则直接把 `inode` 指针的数值转换成一个 `unsigned long`
作为唯一标识。回顾第 6 章 6.2 节关于 `luo_file.id` 字段的讨论——LUO 用这个 ID
在 `luo_preserved_files` xarray 中检测重复保存（同一个底层 `inode` 即使被
打开成多个 `fd`，也只能被保存一次，否则会产生数据不一致甚至双重 pin/双重
preserve 的灾难）。把 `inode` 指针地址直接当成 ID 是一种"利用内核对象地址
天然唯一"的轻量级方案——只要保证比较的双方都在同一个内核生命周期内（preserve
阶段必然如此），这个 ID 就具有可靠的唯一性，完全不需要额外维护一张分配表。

### 9.3 `memfd_luo_preserve`：保存流程的总指挥

理解了准入条件，我们正式进入保存流程的核心。`memfd_luo_preserve()`
（`memfd_luo.c:258-322`）扮演着"总指挥"的角色——它负责加锁、分配序列化结构、
校验属性、然后委托 `memfd_luo_preserve_folios()` 完成最繁重的 folio 级别工作：

```c
static int memfd_luo_preserve(struct liveupdate_file_op_args *args)
{
        struct inode *inode = file_inode(args->file);
        struct memfd_luo_folio_ser *folios_ser;
        struct memfd_luo_ser *ser;
        u64 nr_folios, inode_size;
        int err = 0, seals;

        inode_lock(inode);
        shmem_freeze(inode, true);

        /* Allocate the main serialization structure in preserved memory */
        ser = kho_alloc_preserve(sizeof(*ser));
        if (IS_ERR(ser)) {
                err = PTR_ERR(ser);
                goto err_unlock;
        }

        seals = memfd_get_seals(args->file);
        if (seals < 0) {
                err = seals;
                goto err_free_ser;
        }

        /* Make sure the file only has the seals supported by this version. */
        if (seals & ~MEMFD_LUO_ALL_SEALS) {
                err = -EOPNOTSUPP;
                goto err_free_ser;
        }

        ser->pos = args->file->f_pos;
        inode_size = i_size_read(inode);

        /*
         * memfd_pin_folios() caps at UINT_MAX folios; refuse larger
         * files to avoid silently preserving only a prefix.
         */
        if (DIV_ROUND_UP_ULL(inode_size, PAGE_SIZE) > UINT_MAX) {
                err = -EFBIG;
                goto err_free_ser;
        }

        ser->size = inode_size;
        ser->seals = seals;

        err = memfd_luo_preserve_folios(args->file, &ser->folios,
                                        &folios_ser, &nr_folios);
        if (err)
                goto err_free_ser;

        ser->nr_folios = nr_folios;
        inode_unlock(inode);

        args->private_data = folios_ser;
        args->serialized_data = virt_to_phys(ser);

        return 0;

err_free_ser:
        kho_unpreserve_free(ser);
err_unlock:
        shmem_freeze(inode, false);
        inode_unlock(inode);
        return err;
}
```

逐段拆解这个函数的设计逻辑：

#### 9.3.1 加锁与冻结：`inode_lock` + `shmem_freeze`

```c
inode_lock(inode);
shmem_freeze(inode, true);
```

这两行是整个保存操作的"安全闸门"。`inode_lock` 是标准的 inode 互斥锁，
排除并发的 `write`/`truncate`/`fallocate` 等修改文件内容/大小的操作——
这本身并不新鲜。真正值得注意的是 `shmem_freeze(inode, true)`：它把这个
shmem inode 标记为"冻结"状态，阻止后续对它的进一步修改尝试（即便是在
`inode_lock` 被释放之后）。

为什么 `inode_lock` 还不够，需要再加一层 `shmem_freeze`？答案在于
LUO 文件保存生命周期的"持续性"——回顾第 6 章 6.1 节状态机，一个文件从
`preserve` 成功到真正 `freeze`（也就是不可逆地"冻结"准备 kexec）之间，
**可能间隔相当长的时间**（用户可以先 preserve 多个文件，再统一调用
session finish 触发 `liveupdate_reboot`）。在这段窗口期里，`inode_lock`
早已被释放（`memfd_luo_preserve` 函数末尾就执行了 `inode_unlock`），
如果不引入额外的持久化标记，文件内容完全可能在 preserve 之后、真正
reboot 之前被用户态进程继续修改——而那些修改不会被 `kho_preserve_folio`
重新捕获（folio 早已在 preserve 阶段被 pin 住并记录了当时的 PFN）。

`shmem_freeze` 正是为了堵住这个"preserve 之后仍可写"的窗口期漏洞——
它让 shmem 层在更细粒度上拒绝后续的写入尝试，确保从 `preserve` 那一刻起，
"已经被保存的内容"和"用户态此后还能看到/修改的内容"严格保持一致。这是一种
跨越函数调用边界的、"提前为未来某个阶段的不变式做铺垫"的设计考量——
读者在第一次看到 `memfd_luo_preserve` 里调用 `shmem_freeze` 时，很容易
误以为它只是 `inode_lock` 的某种冗余加强，但深入思考其生命周期跨度后，
才能意识到它解决的是一个完全不同维度的问题（"长期持有的不变式" vs.
"短期的并发互斥"）。

#### 9.3.2 主序列化结构分配：`kho_alloc_preserve(sizeof(*ser))`

```c
ser = kho_alloc_preserve(sizeof(*ser));
```

这一行把 `struct memfd_luo_ser`（回顾 8.2 节及下面 9.4 节即将细看的结构定义）
直接分配在"会穿越 kexec 存活下来"的物理内存中，并立即返回其内核虚拟地址。
这与第 6 章 `luo_preserve_file()` 通过 `args->serialized_data = virt_to_phys(ser)`
把序列化结构的物理地址回传给 LUO 核心的机制完全对应——`memfd_luo_ser` 正是
那个"不透明的 `data` 句柄"在 memfd 这个具体子系统里所代表的真实类型。

#### 9.3.3 Seals 校验：`memfd_get_seals` + 已知 seal 集合检查

```c
seals = memfd_get_seals(args->file);
if (seals < 0) {
        err = seals;
        goto err_free_ser;
}

if (seals & ~MEMFD_LUO_ALL_SEALS) {
        err = -EOPNOTSUPP;
        goto err_free_ser;
}
```

`MEMFD_LUO_ALL_SEALS`（`include/linux/kho/abi/memfd.h:63-68`）是一个按位或
拼出来的"本版本已知的 seal 类型集合"：

```c
#define MEMFD_LUO_ALL_SEALS (F_SEAL_SEAL | \
                             F_SEAL_SHRINK | \
                             F_SEAL_GROW | \
                             F_SEAL_WRITE | \
                             F_SEAL_FUTURE_WRITE | \
                             F_SEAL_EXEC)
```

`seals & ~MEMFD_LUO_ALL_SEALS` 这个表达式的含义是"取出当前文件 seal 位图中
**不在已知集合范围内**的那些位"——如果结果非零，说明这个 memfd 携带着
当前 LUO 版本不认识的 seal 类型（可能是未来内核引入的新 seal，而旧内核序列化
版本尚未感知）。此时直接返回 `-EOPNOTSUPP`，**拒绝保存**这个文件。

这个检查再一次印证了第 8 章总结的"故障域隔离 + 严格拒绝不匹配"哲学——
但这次发生在比"compatible 字符串比对"更细的粒度上：**同一个 ABI 版本
（`memfd-v2`）内部，针对具体属性值的合法范围也要做严格校验**。`seals`
是一个 uAPI 稳定的位图（注释明确写道"The seals are uABI so it is safe to
directly use them in the uAPI"，`memfd_luo.c` 注释中的措辞），但"uAPI 稳定"
不代表"LUO 当前版本认识所有可能的取值"——未来内核可能引入新的 seal 类型，
如果不做这层检查，旧内核会把一个自己不完全理解的位图原样序列化过去，新内核
（如果版本恰好相同、不会触发 compatible 检查）却可能因为遇到未知的 seal
组合而产生与用户预期不符的行为。**主动在源头拒绝那些"自己看不懂、但格式上
合法"的输入**，比"盲目转发、指望接收方能处理"更稳妥——这是防御性编程在
ABI 边界处的具体实践。

#### 9.3.4 文件大小上限检查：`memfd_pin_folios()` 的 `UINT_MAX` 约束

```c
if (DIV_ROUND_UP_ULL(inode_size, PAGE_SIZE) > UINT_MAX) {
        err = -EFBIG;
        goto err_free_ser;
}
```

注释直白地解释了这条检查的来由（`memfd_luo.c:291-294`）：

```
/*
 * memfd_pin_folios() caps at UINT_MAX folios; refuse larger
 * files to avoid silently preserving only a prefix.
 */
```

`memfd_pin_folios()`（稍后会在 9.4 节看到它的调用）这个辅助函数本身的接口
设计中，传入/传出的 folio 数量用 `unsigned int` 表示，因此存在一个
`UINT_MAX` 的硬上限。如果一个 memfd 的大小换算成页数后超过这个上限
（`(2^32 - 1) * 4096` 字节，约 16 PB——在当前的硬件条件下属于天文数字，
但 ABI 设计必须考虑边界情况而不是"实践中不会遇到"），如果不显式检查，
`memfd_pin_folios` 会**静默地只 pin 住前 `UINT_MAX` 个 folio**，
导致整个保存操作"看起来成功了"，但实际上只保存了文件的一个前缀片段——
这是一种极其隐蔽、用户几乎不可能察觉的数据损坏（恢复后文件大小字段显示
正常，但读取尾部数据时会得到全零或错误内容）。

`DIV_ROUND_UP_ULL` 计算"向上取整的页数"，与上限比较后，**提前**用明确的
`-EFBIG`（File Too Big）错误码拒绝整个保存请求——这是一种"宁可在入口处
报错，也不要在内部产生静默截断"的设计选择，与第 8 章总结的"早失败、明确
失败"原则一脉相承。这种检查也提醒读者：**调用一个底层接口时，不仅要关注
它"成功时返回什么"，更要关注它"在边界条件下是否存在静默降级行为"**——
`memfd_pin_folios` 的 `UINT_MAX` 截断行为，正是这样一个容易被忽视、
却足以引发严重数据正确性问题的细节。

#### 9.3.5 委托与收尾：`memfd_luo_preserve_folios` 调用与三层 goto 回滚链

主函数最后把"真正繁重"的 folio 处理工作委托给 `memfd_luo_preserve_folios()`
（9.4 节详细展开），自己只负责把返回的 `nr_folios` 写入 `ser`、解锁、
把 `private_data`（folio 元数据数组的虚拟地址，供 `unpreserve` 路径使用）
和 `serialized_data`（`ser` 的物理地址）回填进 `args`。

如果中途任何一步失败，错误处理走的是一条"短而精确"的两层 `goto` 链：

```
   err_free_ser:                    err_unlock:
       kho_unpreserve_free(ser)  ──►   shmem_freeze(inode, false)
                                       inode_unlock(inode)
```

这条链严格遵循"构造的反向就是析构的顺序"原则：`ser` 在 `kho_alloc_preserve`
之后才存在，因此只有走到分配成功之后的失败路径才需要 `err_free_ser`；
而 `inode_lock`/`shmem_freeze` 是函数最早建立的两个不变式，因此
**所有**失败路径最终都必须经过 `err_unlock` 来撤销它们——`err_free_ser`
标签正好"掉落"进 `err_unlock` 标签的代码块，这种 **goto 标签链式掉落（fallthrough）**
的写法，用最少的代码行数表达了"按构造的逆序逐层回滚"的语义，是 C 语言里
处理多层资源获取/释放配对时极为常见且被广泛认可的惯用法（与第 6 章
`luo_preserve_file` 的七层回滚链是同一种思想在不同规模下的应用）。

### 9.4 `memfd_luo_preserve_folios`：folio pin / preserve / 标记的核心逻辑

这是整个 memfd 保存路径里代码量最大、思考密度最高的函数——它要把一个
逻辑上连续的文件内容，转换成一组物理页（folio）的集合，把每一个都"钉"
在内存里、登记进 KHO 的存活清单、并准确记录下"将来如何重新认出它"所需的
全部元数据。函数签名（`memfd_luo.c:87-90`）：

```c
static int memfd_luo_preserve_folios(struct file *file,
                                     struct kho_vmalloc *kho_vmalloc,
                                     struct memfd_luo_folio_ser **out_folios_ser,
                                     u64 *nr_foliosp)
```

四个参数里，`kho_vmalloc` 是输出参数（指向调用方 `ser->folios` 字段，
由本函数填充——回顾 8.3 节，这正是那个用来传递"可能极大的数组"的
`struct kho_vmalloc` 描述符），`out_folios_ser`/`nr_foliosp` 则把
本函数构建出的 folio 元数据数组虚拟地址和数量回传给调用方，供后续
`private_data`/`ser->nr_folios` 赋值使用。

#### 9.4.1 空文件的快速路径

```c
size = i_size_read(inode);
if (!size) {
        *nr_foliosp = 0;
        *out_folios_ser = NULL;
        return 0;
}
```

一个大小为零的 memfd（典型场景：刚创建、还没写入任何内容）根本不需要
经历后面那一整套 pin/preserve/序列化流程——直接把 `nr_folios` 设为 0、
把数组指针设为 `NULL` 后立即返回成功。这不仅是性能优化，也是正确性需要——
如果走到后面的 `memfd_pin_folios(file, 0, size - 1, ...)`，`size - 1`
在 `size == 0` 时会因为无符号整数下溢变成一个巨大的正数，引发难以预料的
错误行为。提前对"零大小"这一边界情形做特判，干净利落地避开了这个潜在陷阱。

#### 9.4.2 容量预估与数组分配

```c
max_folios = PAGE_ALIGN(size) / PAGE_SIZE;
folios = kvmalloc_objs(*folios, max_folios);
```

`max_folios` 是按"每个 folio 恰好占一页"这一**最坏情况**估算出的折上折——
如果文件中存在更大粒度的"高阶 folio"（high-order folio，一个 folio 可能
跨越多个连续物理页，由 `shmem` 在特定条件下自动使用以提升效率），实际
folio 数量会比这个估计值更小。注释明确写出了这一点（`memfd_luo.c:112-115`）：

```
/*
 * Guess the number of folios based on inode size. Real number might end
 * up being smaller if there are higher order folios.
 */
```

这里选择"先用最坏情况估算上限、分配一个足够大的临时数组，再用实际
pin 到的数量回填"的策略，而不是先精确遍历一遍计算出准确数量再分配——
这是一种经典的"空间换简单性"取舍：临时数组 `folios`（`struct folio *`
指针数组）只是过程中的脚手架，函数末尾会被 `kvfree` 释放，多分配出来的
那部分空间只是短暂存在，不会带来长期的内存浪费；而"先扫描计数、再分配、
再扫描填充"的两遍式写法虽然能精确分配，却引入了"两次遍历必须看到完全一致
的文件状态"这一额外的并发约束（万一两次扫描之间文件发生了变化怎么办？），
反而让代码更脆弱。`kvmalloc_objs` 这个分配宏本身的命名也暗示了设计取向——
它优先尝试 `kmalloc`（物理连续，小块时更快），在分配较大内存块时自动回退到
`vmalloc`（虚拟连续），兼顾了"小文件时的效率"和"大文件时的可行性"。

#### 9.4.3 `memfd_pin_folios`：一次调用解决三个问题

```c
nr_pinned = memfd_pin_folios(file, 0, size - 1, folios, max_folios, &offset);
```

这一行调用是整个函数里"性价比"最高的一步——源码注释列举了它一举三得的效果
（`memfd_luo.c:121-132`）：

```
/*
 * Pin the folios so they don't move around behind our back. This also
 * ensures none of the folios are in CMA -- which ensures they don't
 * fall in KHO scratch memory. It also moves swapped out folios back to
 * memory.
 *
 * A side effect of doing this is that it allocates a folio for all
 * indices in the file. This might waste memory on sparse memfds. If
 * that is really a problem in the future, we can have a
 * memfd_pin_folios() variant that does not allocate a page on empty
 * slots.
 */
```

把这段注释拆解成三条独立的设计动机：

1. **Pin 住，防止"背着我们移动"**——folio 在正常运行的内核里并非静止不变：
   内存规整（compaction）、页面迁移（migration）等机制都可能把一个 folio
   的物理位置悄悄换掉。一旦 `kho_preserve_folio` 记录下了某个 PFN，
   这个 PFN 就必须在 reboot 之前**始终**指向同一份数据——`pin` 操作正是
   为了冻结这层映射关系，防止内核的内存管理子系统在我们不知情的情况下
   "重新摆放家具"。
2. **顺带规避 CMA / KHO scratch 区域冲突**——CMA（Contiguous Memory Allocator）
   分配出的内存通常需要保持"可被规整、可被迁移"的特性，以便随时腾出大块
   连续内存供有特殊需求的驱动（比如某些需要 DMA 连续缓冲区的设备）使用；
   而 KHO 的 "scratch" 内存区域则是预留给新内核启动早期使用的"周转空间"
   （详见第 2 章对 KHO scratch 内存的介绍）。这两类内存都与"长期不变的、
   穿越 kexec 的固定物理位置"这一需求相冲突。`memfd_pin_folios` 内部的
   实现保证了被 pin 住的页面不会落在这些特殊区域中——这是一个"调用一个
   通用接口、顺带获得了一系列本来需要自己手写大量代码才能保证的额外属性"
   的典型范例，提醒读者在阅读内核代码时，不能只看函数名直译的字面含义，
   还要去深挖它在更深层次上提供了哪些隐含保证。
3. **唤回换出的页面**——如果文件的某些部分因为内存压力被换出到 swap
   设备，`memfd_pin_folios` 会触发它们被重新读入物理内存。这是显而易见
   的必要条件——被换出的内容不在物理内存里，自然无法通过 `kho_preserve_folio`
   "原地保存"，必须先把它们都"请"回内存。

注释的最后一段还坦诚地指出了这个调用的一个**副作用**：它会为文件中
**所有**索引位置分配 folio——即便文件存在"空洞"（hole，例如通过
`lseek` 越过末尾再写入产生的稀疏区域），也会被强制分配真实页面并填零。
对于高度稀疏的大文件，这意味着保存操作本身可能消耗远超"实际有效数据量"
的内存。注释甚至给出了未来的优化方向——"如果这真的成为问题，可以引入一个
不在空槽位分配页面的 `memfd_pin_folios` 变体"。这种"诚实地把已知的性能
代价写进注释里、并指出可能的改进方向"的做法，是内核代码里值得称道的
工程文档习惯——它告诉后来者"这不是疏忽，而是权衡之下的有意选择，
这里有改进空间但暂时不是优先级"。

#### 9.4.4 逐 folio 处理循环：preserve、加锁、标记 dirty/uptodate

这是整个函数里最值得逐句精读的一段（`memfd_luo.c:148-202`）：

```c
for (i = 0; i < nr_folios; i++) {
        struct memfd_luo_folio_ser *pfolio = &folios_ser[i];
        struct folio *folio = folios[i];

        err = kho_preserve_folio(folio);
        if (err)
                goto err_unpreserve;

        folio_lock(folio);

        folio_mark_dirty(folio);

        if (!folio_test_uptodate(folio)) {
                folio_zero_range(folio, 0, folio_size(folio));
                flush_dcache_folio(folio);
                folio_mark_uptodate(folio);
        }

        folio_unlock(folio);

        pfolio->pfn = folio_pfn(folio);
        pfolio->flags = MEMFD_LUO_FOLIO_DIRTY | MEMFD_LUO_FOLIO_UPTODATE;
        pfolio->index = folio->index;
}
```

**第一步 `kho_preserve_folio(folio)`**——把这个 folio 正式登记进 KHO 的
"将穿越 kexec 存活"清单（这是第 2 章介绍过的 KHO 基础 API 之一）。
一旦失败，立即 `goto err_unpreserve`——回滚路径的设计放在 9.4.5 节细看。

**第二、三步 `folio_lock` + 无条件 `folio_mark_dirty`**——这是整个函数里
最反直觉、也最能体现作者深入思考的一处设计。源码用了相当长的一段注释
（`memfd_luo.c:158-176`）专门解释"为什么要无条件地把每一个 folio 都
标记成 dirty"，我们逐句拆解这段注释蕴含的推理链条：

1. **"dirty" 与 "clean" 的语义差异**：一个 dirty 的 folio 是"曾经被写入过、
   携带着用户数据"的 folio；一个 clean 的 folio 则相反——它"不携带用户数据"，
   因此在内存压力下可以被页面回收机制（page reclaim）自由丢弃（反正丢了
   也不会损失任何信息，下次访问时重新从磁盘/源头加载即可，对 shmem 而言
   则是重新分配一个全零页面）。
2. **为什么不能在 `preserve()` 时就"如实记录"当前的 dirty 状态**：
   注释指出"在 prepare（即 preserve）时机保存这个标志位是行不通的，
   因为它后续可能会变化"——在 `preserve` 成功之后、真正 `freeze`/重启
   之前，文件仍可能被继续写入（这正是 9.3.1 节讨论 `shmem_freeze`
   时埋下的伏笔——`shmem_freeze` 阻止的是"修改文件结构"层面的操作，
   而不是阻止单个已经存在的 folio 从 clean 变为 dirty）。
3. **为什么也不能推迟到 `freeze()` 阶段才记录**：注释进一步解释——
   "dirty 标志位通常在 unmap（取消页面映射）时才会被同步，而在 freeze
   阶段，这个文件可能仍然存在着映射"。也就是说，即便我们等到 freeze
   阶段，一个被 mmap 映射、用户态正在通过页表直接写入的 folio，其
   "硬件 dirty 位"也未必已经被同步进 `folio` 的软件状态里——我们看到的
   可能是一个"实际上已经脏了，但软件层面尚未感知"的过期快照。
4. **"先 clean 后 dirty"会导致的灾难性后果**：注释用一个具体的反例把
   这个问题钉死——"假设一个 folio 在 preserve 时是 clean 的，但之后变脏了。
   `pfolio` 的 flags 会把它标记成 clean。retrieve 之后，下一个内核可能在
   内存压力下尝试回收这个 folio，从而**丢失用户数据**"。这是整段推理的
   落脚点：一旦把"clean"这个错误信息编码进了序列化数据，新内核就会认为
   "这个 folio 即便丢了也无所谓"，进而在内存紧张时主动把它释放掉——
   而这块内存里其实装着用户辛辛苦苦写入、本应该被 LUO 完整保存下来的数据。
5. **最终决策——"宁可错杀，不可放过"**：与其冒着"误判 clean、丢失数据"的
   风险去精确计算每个 folio 的真实 dirty 状态（这在当前的内核基础设施下
   几乎不可能做到100%精确），不如**无条件地把所有 folio 都标记为 dirty**。
   注释直言不讳地指出这个选择的代价——"这样做的代价是，live update 之后，
   原本 clean 的 folio 会变得不可回收"。

这是一段教科书级别的"在不确定性面前如何选择安全边界"的工程推理——
当面临"过度保守 vs. 过度乐观"的两难时，**永远选择"过度保守"那一侧**，
因为它造成的后果是"轻微的资源浪费"（一些原本可以被回收的页面暂时不能被回收，
等到用户真正修改或删除文件时这些页面自然会被释放），而"过度乐观"一侧造成的
后果却是**不可逆的数据丢失**——这两种代价根本不在同一个量级上，选择哪一个
是显而易见的。这种推理模式与第 6 章总结的"宁可泄漏不做不安全 undo"哲学、
与第 11 章即将系统化梳理的错误处理决策树，本质上都在传递同一个核心信条：
**当无法确定"安全"的标准操作时，选择"造成的损失最小、且总是可以事后弥补"
的那一个选项**。

**第四步——处理"未初始化"（not uptodate）的 folio**：

```c
if (!folio_test_uptodate(folio)) {
        folio_zero_range(folio, 0, folio_size(folio));
        flush_dcache_folio(folio);
        folio_mark_uptodate(folio);
}
```

一个"not uptodate"的 folio 是通过 `fallocate()` 之类的接口被预分配、
但从未被实际写入过的页面——它的物理内容是未定义的（可能是上一个使用者
留下的残余数据）。注释解释了为什么这里也选择"无条件清零并标记为 uptodate"
（`memfd_luo.c:179-189`）：

```
/*
 * If the folio is not uptodate, it was fallocated but never
 * used. Saving this flag at prepare() doesn't work since it
 * might change later when someone uses the folio.
 *
 * Since we have taken the performance penalty of allocating,
 * zeroing, and pinning all the folios in the holes, take a bit
 * more and zero all non-uptodate folios too.
 *
 * NOTE: For someone looking to improve preserve performance,
 * this is a good place to look.
 */
```

这与上面 dirty 标志位的推理如出一辙——"在 preserve 时机记录这个标志位
同样行不通，因为它之后可能会变化（用户随时可能开始使用这个 folio）"。
但这里的处理方式略有不同：既然 `memfd_pin_folios` 已经为所有空洞分配
了真实页面（9.4.3 节提到的副作用），与其冒着"标记不一致"的风险，
不如顺势"再多走一步"——直接把这些未初始化的 folio 主动清零并标记为
uptodate。`folio_zero_range` 把整个 folio 清零，`flush_dcache_folio`
确保清零操作对所有可能存在的缓存别名（cache alias）可见（这在某些
体系结构上是必需的——CPU 数据缓存可能存在多重映射导致的不一致），
`folio_mark_uptodate` 最终把状态标记为"已初始化、内容有效"。

注释末尾那句"NOTE: For someone looking to improve preserve performance,
this is a good place to look"，是内核代码中一种珍贵的"路标式"注释——
它直接告诉未来的优化者"这里有一块明确的、已知的性能洼地，如果你想
做性能优化，从这里下手"。这种坦诚地暴露已知局限、并指引未来工作方向的
写法，体现了内核开发文化里"代码是写给后来人看的"这一根本信条。

**最后三行——填充序列化条目**：

```c
pfolio->pfn = folio_pfn(folio);
pfolio->flags = MEMFD_LUO_FOLIO_DIRTY | MEMFD_LUO_FOLIO_UPTODATE;
pfolio->index = folio->index;
```

至此，循环体把每一个 folio 转换成一条恰好 16 字节的 `memfd_luo_folio_ser`
记录——`pfn`（52 位的页帧号，用于在新内核里通过 `kho_restore_folio`
按物理地址精确找回这个 folio）、`flags`（恒定地置上 `DIRTY | UPTODATE`
两个标志位，呼应上面两段推理的最终决策）、`index`（这个 folio 在文件中
的页偏移，用于在新内核里把它精确地放回 page cache 中正确的位置）。

回顾 8.2 节看到的 `struct memfd_luo_folio_ser` 定义：

```c
struct memfd_luo_folio_ser {
        u64 pfn:52;
        u64 flags:12;
        u64 index;
} __packed;
```

`pfn` 与 `flags` 被打包进同一个 64 位字段的两个位域（`:52` 和 `:12`）——
这是一个针对存储密度精心计算过的设计：现代 64 位体系结构上，物理地址空间
一般不超过 52 位（即 4 PB），`pfn`（物理地址右移 `PAGE_SHIFT` 位后的
页帧号）用 52 位完全够用；剩下的 12 位用来存放 `flags`，恰好可以容纳
`MEMFD_LUO_FOLIO_DIRTY`（bit 0）、`MEMFD_LUO_FOLIO_UPTODATE`（bit 1）
以及未来可能增加的至多 10 个新标志位。把两个逻辑上独立、但取值范围都
"远小于一个完整 64 位字段"的属性压缩进同一个字段，既节省了 8 个字节的
存储空间（一个 `memfd_luo_folio_ser` 数组在大文件场景下可能有数百万项，
节省的总量相当可观），又通过位域语法让代码层面的访问保持自然直观——
这是 ABI 设计中"压缩存储密度而不牺牲可读性"的一个精巧范例。

#### 9.4.5 序列化数组的传递与多层错误回滚

```c
err = kho_preserve_vmalloc(folios_ser, kho_vmalloc);
if (err)
        goto err_unpreserve;

kvfree(folios);
*nr_foliosp = nr_folios;
*out_folios_ser = folios_ser;

/*
 * Note: folios_ser is purposely not freed here. It is preserved
 * memory (via KHO). In the 'unpreserve' path, we use the vmap pointer
 * that is passed via private_data.
 */
return 0;

err_unpreserve:
        for (i = i - 1; i >= 0; i--)
                kho_unpreserve_folio(folios[i]);
        vfree(folios_ser);
err_unpin:
        unpin_folios(folios, nr_folios);
err_free_folios:
        kvfree(folios);

return err;
```

`kho_preserve_vmalloc(folios_ser, kho_vmalloc)`——这正是 8.3 节详细
解读过的 `struct kho_vmalloc` 机制的实际调用现场：把刚刚用 `vcalloc`
分配出来的、可能横跨数百个物理页的 `folios_ser` 大数组，通过这个接口
登记进 KHO 的"分段链表"传递机制中，`kho_vmalloc`（即 `ser->folios`）
被填充上描述这个数组物理布局的全部信息。

成功路径末尾的注释（`memfd_luo.c:212-216`）特别提醒读者一个容易让人
迷惑的细节——"`folios_ser` 故意没有在这里被释放：它是通过 KHO 保存下来
的内存。在 `unpreserve` 路径中，我们使用通过 `private_data` 传递的
vmap 指针"。这句话解释了为什么 `memfd_luo_preserve` 最后要把
`folios_ser` 通过 `args->private_data` 回传——`folios_ser` 数组的内存
此刻已经"易主"给 KHO（它将穿越 kexec 存活），普通的 `vfree`/`kfree`
不再适用；但同时它在**当前内核生命周期内依然是一段有效的虚拟地址**——
如果保存之后用户又决定撤销（调用 `unpreserve_fd`），LUO 仍然需要
通过这个虚拟地址指针回到这块内存、调用 `kho_unpreserve_vmalloc`
解除 KHO 登记、再 `vfree` 真正释放它。`private_data` 正是为了承载
"这段内存的活引用"而存在——这与 `data`/`obj` 在 FLB 体系里扮演的
"持久句柄 vs. 活对象指针"二元角色（详见 7.2.1 节）属于同一种设计模式
在不同子系统里的复现。

而错误回滚路径，则是又一条精确镜像构造顺序的"层叠 goto"链：

```
   构造顺序：                         回滚顺序（err_* 标签从下往上掉落）：
   ① kvmalloc folios 数组    ────►   err_free_folios: kvfree(folios)
   ② memfd_pin_folios 钉住    ────►   err_unpin: unpin_folios(folios, nr_folios)
   ③ vcalloc folios_ser 数组  ┐
   ④ 循环中 kho_preserve_folio├──►   err_unpreserve:
      （可能在第 i 个失败）   │           for (i = i-1; i >= 0; i--)
                              │               kho_unpreserve_folio(folios[i]);
                              └──►        vfree(folios_ser);
```

这里有一个非常值得注意的细节——`for (i = i - 1; i >= 0; i--)`：
循环变量 `i` **复用**了主循环中"当前正在处理、却失败了"的那个下标。
`i - 1` 精确地跳过了"刚刚失败、根本没有成功 preserve"的那个 folio，
只回滚 `[0, i-1]` 这个**已经成功**的区间——这与第 7 章
`luo_flb_file_preserve` 中 `list_for_each_entry_continue_reverse`
"以失败点为基准、不回滚失败本身"的处理方式遵循着完全相同的逻辑原则，
是贯穿全书的"精确回滚已完成的工作、不画蛇添足地处理未完成的工作"
这一原则的又一例证。

`unpin_folios`/`kvfree` 与对应的"获取"调用（`memfd_pin_folios`/
`kvmalloc_objs`）形成镜像配对——整条回滚链严丝合缝地覆盖了函数体内
建立的全部四层资源（数组内存、folio pin 状态、序列化数组内存、
KHO preserve 登记），无一遗漏、无一重复。

### 9.5 `memfd_luo_freeze`：为什么只更新 `pos`

相比 `preserve` 的庞大复杂，`memfd_luo_freeze()`（`memfd_luo.c:324-340`）
显得异常精简：

```c
static int memfd_luo_freeze(struct liveupdate_file_op_args *args)
{
        struct memfd_luo_ser *ser;

        if (WARN_ON_ONCE(!args->serialized_data))
                return -EINVAL;

        ser = phys_to_virt(args->serialized_data);

        /*
         * The pos might have changed since prepare. Everything else stays the
         * same.
         */
        ser->pos = args->file->f_pos;

        return 0;
}
```

这个函数只做了一件事——重新读取 `f_pos` 并覆盖写入 `ser->pos`。
联系第 6 章对"freeze 阶段语义"的讨论——`freeze` 是"preserve 成功之后、
真正 kexec 重启之前的最后一次确认机会"，此时系统即将进入不可逆的转换流程，
是"补充那些在 preserve 时机尚未稳定、但到了此刻已经不会再变化"的属性的
最佳时机。

那么为什么**只**重新捕获 `pos`，而不是其他属性（比如再次检查 seals、
再次扫描文件大小是否变化）？这背后有一条隐含的逻辑链：

* `pos`（文件读写位置）是一个**会随着用户态正常的 `read`/`write`/`lseek`
  调用频繁变化、却完全不影响文件内容/结构**的轻量属性——它本质上只是
  "下一次 I/O 操作从哪里开始"这个游标，重新读取它的代价极小（一次内存读取），
  收益却很明确（让恢复后的应用能从更接近"真实最后位置"的地方继续）；
* 而 `seals`、`size`、`folios` 这些属性，要么在 `shmem_freeze(inode, true)`
  生效之后已经被锁定、不可能再变化（详见 9.3.1 节对 `shmem_freeze`
  作用范围的分析），要么本身就是"重新计算代价高昂"（如重新扫描整个文件
  内容并重新 pin folio）——既然 `preserve` 阶段已经把它们妥善地固定下来，
  在 `freeze` 阶段重新折腾一遍纯属浪费。

这正是"在合适的阶段做合适的事"这一设计哲学的具体体现——`preserve`
负责"建立起跨越漫长窗口期的、稳定不变的核心保证"（folio 内容、大小、
seals 全部冻结），`freeze` 则只负责"临门一脚式地补充那一小撮在窗口期内
仍然自然变化、且重新捕获代价极低的轻量属性"。二者分工明确，互不重叠，
合在一起才构成一份"既准确又高效"的完整快照。

### 9.6 `memfd_luo_unpreserve`：preserve 之后反悔的撤销路径

```c
static void memfd_luo_unpreserve(struct liveupdate_file_op_args *args)
{
        struct inode *inode = file_inode(args->file);
        struct memfd_luo_ser *ser;

        if (WARN_ON_ONCE(!args->serialized_data))
                return;

        inode_lock(inode);
        shmem_freeze(inode, false);

        ser = phys_to_virt(args->serialized_data);

        memfd_luo_unpreserve_folios(&ser->folios, args->private_data,
                                    ser->nr_folios);

        kho_unpreserve_free(ser);
        inode_unlock(inode);
}
```

这个函数的结构是 `memfd_luo_preserve` 的精确镜像——`inode_lock` +
`shmem_freeze(inode, false)`（**解除**冻结，恢复正常的可写状态）建立
临界区，然后依次撤销 `preserve` 建立的两层资源：先撤销 folio 级别的
保存（`memfd_luo_unpreserve_folios`），再撤销主序列化结构本身
（`kho_unpreserve_free(ser)`）——这同样是"逆序撤销"原则的体现：
`folios_ser` 数组是在 `ser` 分配**之后**创建的（`ser->folios` 字段
是在 `memfd_luo_preserve_folios` 内部填充的），所以撤销时必须先处理
`folios_ser`，再处理 `ser` 本身——否则会出现"先释放了 `ser`，再试图
通过已经失效的 `ser->folios` 字段去定位 `folios_ser`"的悬空引用错误。

`memfd_luo_unpreserve_folios()`（`memfd_luo.c:231-256`）则是 folio
级别的具体撤销逻辑：

```c
static void memfd_luo_unpreserve_folios(struct kho_vmalloc *kho_vmalloc,
                                        struct memfd_luo_folio_ser *folios_ser,
                                        u64 nr_folios)
{
        long i;

        if (!nr_folios)
                return;

        kho_unpreserve_vmalloc(kho_vmalloc);

        for (i = 0; i < nr_folios; i++) {
                const struct memfd_luo_folio_ser *pfolio = &folios_ser[i];
                struct folio *folio;

                if (!pfolio->pfn)
                        continue;

                folio = pfn_folio(pfolio->pfn);

                kho_unpreserve_folio(folio);
                unpin_folio(folio);
        }

        vfree(folios_ser);
}
```

注意它接收的 `folios_ser` 参数来自调用方传入的 `args->private_data`——
正是 9.4.5 节强调的"`private_data` 携带活虚拟地址指针"这一设计在这里
真正派上用场的地方。函数体的三个动作精确对应 `preserve_folios`
建立的三层状态，按"分配顺序的相反顺序"逐一撤销：

1. `kho_unpreserve_vmalloc(kho_vmalloc)`——撤销整个 `folios_ser` 数组的
   KHO vmalloc 登记（对应 `memfd_luo_preserve_folios` 里的
   `kho_preserve_vmalloc` 调用）；
2. 遍历每个 folio，先 `kho_unpreserve_folio` 撤销单个 folio 的 KHO
   登记，再 `unpin_folio` 解除 pin（对应循环中的 `kho_preserve_folio`
   + `memfd_pin_folios`，注意这里的撤销顺序也与建立顺序"内层先建立、
   内层先撤销"保持一致——`kho_preserve_folio` 是在 pin 之后才调用的，
   所以撤销时先 unpreserve 再 unpin）；
3. `vfree(folios_ser)`——最终释放整个数组本身的内存（对应
   `vcalloc` 分配）。

`if (!pfolio->pfn) continue;` 这一行跳过条目——结合 9.4.1 节"空文件
快速路径"以及 `pfolio` 数组用 `vcalloc`（清零分配）创建的事实，一个
`pfn == 0` 的条目代表"这个槽位从未被真正填充过有效数据"（`pfn` 字段
为 0 在实践中几乎不可能是一个真实的物理页帧号，因为页帧号 0 通常对应
系统保留的最低端内存）。这是一种"用一个永远不会出现的合法值表示
'空/无效'语义"的常见技巧（与 C 语言里用 `NULL` 表示"无指针"是同一类
约定），让代码不需要额外的"是否有效"标志位就能区分"已处理"与"未处理"
的条目。

### 9.7 `memfd_luo_retrieve` 与 `memfd_luo_retrieve_folios`：在新内核里重建 page cache

`retrieve` 路径是整个保存/恢复链条里"反向工程难度最高"的一环——它需要
把一组孤立的、只记录着 PFN 和索引的 folio，重新组装成一个具有完整
page cache 结构、可以被正常 `read`/`write`/`mmap` 的活文件。先看
总体调度函数 `memfd_luo_retrieve`（`memfd_luo.c:518-578`）：

```c
static int memfd_luo_retrieve(struct liveupdate_file_op_args *args)
{
        struct memfd_luo_folio_ser *folios_ser;
        struct memfd_luo_ser *ser;
        struct file *file;
        int err;

        ser = phys_to_virt(args->serialized_data);
        if (!ser)
                return -EINVAL;

        if (ser->seals & ~MEMFD_LUO_ALL_SEALS) {
                err = -EOPNOTSUPP;
                goto free_ser;
        }

        file = memfd_alloc_file("", MFD_ALLOW_SEALING);
        if (IS_ERR(file)) {
                pr_err("failed to setup file: %pe\n", file);
                err = PTR_ERR(file);
                goto free_ser;
        }

        err = memfd_add_seals(file, ser->seals);
        if (err) {
                pr_err("failed to add seals: %pe\n", ERR_PTR(err));
                goto put_file;
        }

        vfs_setpos(file, ser->pos, MAX_LFS_FILESIZE);
        i_size_write(file_inode(file), ser->size);

        if (ser->nr_folios) {
                folios_ser = kho_restore_vmalloc(&ser->folios);
                if (!folios_ser) {
                        err = -EINVAL;
                        goto put_file;
                }

                err = memfd_luo_retrieve_folios(file, folios_ser, ser->nr_folios);
                vfree(folios_ser);
                if (err)
                        goto put_file;
        }

        args->file = file;
        kho_restore_free(ser);

        return 0;

put_file:
        fput(file);
free_ser:
        kho_restore_free(ser);
        return err;
}
```

这是一个"先搭骨架、再填血肉"的两阶段流程，逐段拆解：

**阶段一：重建一个全新的"空"memfd**——`memfd_alloc_file("", MFD_ALLOW_SEALING)`
创建一个崭新的、匿名的 memfd 文件对象（注意传入的名字是空字符串
`""`——回顾 9.1 节的属性表，文件名本来就不在"被保存"的属性集合里，
新的 memfd 干脆以匿名形式创建）。`memfd_add_seals(file, ser->seals)`
重新应用从旧内核继承来的 seal 集合——这一步必须在文件内容/大小被设定
**之前**完成，因为某些 seal（如 `F_SEAL_SHRINK`/`F_SEAL_GROW`）一旦生效，
会立刻限制后续对文件大小的修改操作；如果顺序颠倒，重建大小这一步本身
就可能因为提前生效的 seal 而失败。`vfs_setpos`/`i_size_write` 则分别
重新设置文件的读写位置游标和逻辑大小——这是 9.1 节所列"被保存的属性"
中"文件位置"和"文件大小"两项在新内核里的具体落地。

**阶段二：用 `kho_restore_vmalloc` 取回大数组、委托 `retrieve_folios`
重建 page cache**——这正是 8.3 节详细解读过的 `kho_vmalloc` 机制在
"反向"过程中的应用：`kho_restore_vmalloc(&ser->folios)` 把那条横跨
多个物理页的"分段链表"重新拼接还原成一段连续的虚拟地址空间，得到
`folios_ser` 数组的虚拟地址，随后把真正的"逐 folio 重建"工作委托给
`memfd_luo_retrieve_folios`（详见下文）。

整个函数的错误处理同样遵循"构造逆序"的两层 `goto` 结构（`put_file` →
`free_ser`）——`file` 在 `memfd_alloc_file` 之后才存在，因此只有
走到那一步之后的失败需要 `fput(file)`；而 `ser` 本身的物理内存
（来自旧内核保存、本次内核刚刚 "认领"）无论成功与否，最终都要通过
`kho_restore_free(ser)` 归还给伙伴系统——这与 `memfd_luo_finish`
（9.8 节）中处理 `ser` 生命周期的方式形成呼应：**一旦 `retrieve`
被调用过（无论成功还是失败），`ser` 这块内存的归宿就已经确定**——
这正是 `args->retrieve_status` 三态缓存（回顾第 6 章 6.7 节）背后
"成功路径与失败路径都需要被纳入统一考量"这一设计动机的具体例证。

`memfd_luo_retrieve_folios()`（`memfd_luo.c:417-516`）则是真正
"重新组装 page cache"的核心逻辑：

```c
static int memfd_luo_retrieve_folios(struct file *file,
                                     struct memfd_luo_folio_ser *folios_ser,
                                     u64 nr_folios)
{
        struct inode *inode = file_inode(file);
        struct address_space *mapping = inode->i_mapping;
        struct folio *folio;
        long npages, nr_added_pages = 0;
        int err = -EIO;
        long i;

        for (i = 0; i < nr_folios; i++) {
                const struct memfd_luo_folio_ser *pfolio = &folios_ser[i];
                phys_addr_t phys;
                u64 index;
                int flags;

                if (!pfolio->pfn)
                        continue;

                phys = PFN_PHYS(pfolio->pfn);
                folio = kho_restore_folio(phys);
                if (!folio) {
                        pr_err("Unable to restore folio at physical address: %llx\n", phys);
                        err = -EIO;
                        goto put_folios;
                }
                index = pfolio->index;
                flags = pfolio->flags;

                /* Set up the folio for insertion. */
                __folio_set_locked(folio);
                __folio_set_swapbacked(folio);

                err = mem_cgroup_charge(folio, NULL, mapping_gfp_mask(mapping));
                if (err) {
                        pr_err("shmem: failed to charge folio index %ld: %d\n", i, err);
                        goto unlock_folio;
                }

                err = shmem_add_to_page_cache(folio, mapping, index, NULL,
                                              mapping_gfp_mask(mapping));
                if (err) {
                        pr_err("shmem: failed to add to page cache folio index %ld: %d\n", i, err);
                        goto unlock_folio;
                }

                if (flags & MEMFD_LUO_FOLIO_UPTODATE)
                        folio_mark_uptodate(folio);
                if (flags & MEMFD_LUO_FOLIO_DIRTY)
                        folio_mark_dirty(folio);

                npages = folio_nr_pages(folio);
                err = shmem_inode_acct_blocks(inode, npages);
                if (err) {
                        pr_err("shmem: failed to account folio index %ld(%ld pages): %d\n",
                               i, npages, err);
                        goto remove_from_cache;
                }

                nr_added_pages += npages;
                folio_add_lru(folio);
                folio_unlock(folio);
                folio_put(folio);
        }

        shmem_recalc_inode(inode, nr_added_pages, 0);

        return 0;
        ...
}
```

逐步还原它"重新登记一个 folio 进 page cache"的完整动作序列，对照
内核中 shmem 自身在正常运行时填充 page cache 的步骤（这正是它得以
正确工作的关键——它必须严格模拟 shmem 在"正常路径"下建立这些状态的
全部步骤，否则恢复出来的文件会在某些边角场景下出现行为异常）：

1. **`kho_restore_folio(phys)`**——这是与 `kho_preserve_folio` 严格
   对称的取回接口：根据物理地址重新获得这个 folio 的内核结构体表示
   （此时它的物理内容与旧内核保存时刻完全一致，因为整个 kexec 过程
   中这块物理内存从未被覆写）；
2. **`__folio_set_locked` + `__folio_set_swapbacked`**——手动设置两个
   关键状态位：锁定（准备进行后续的修改性操作，模拟一个"刚分配、即将
   被填充"的 folio 的标准前置状态）、"由 swap 支持"（这是 shmem/tmpfs
   文件 folio 的标志性属性——告诉内存管理子系统"这个 folio 如果需要
   被回收，应该走 swap 路径而不是直接丢弃，因为它携带着用户数据"，
   这与 9.4.4 节强制标记 dirty 的用意一脉相承：都是在确保"被恢复出来的
   数据，未来不会被内存管理子系统误判为可丢弃的临时数据"）；
3. **`mem_cgroup_charge`**——把这个 folio 的内存使用量记入相应的
   cgroup 内存控制组账本。这一步揭示了一个微妙但重要的事实：**folio
   本身的物理存在跨越了 kexec，但它所"归属"的 cgroup 记账关系却完全
   是新内核里全新建立的**——旧内核里的 cgroup 树、进程归属关系在新
   内核启动后已经不复存在（这正是 9.1 节"哪些不会被保存"中暗含的
   深层含义——不仅是文件本身的某些属性不被保存，文件与系统其余部分
   的关联关系也大多需要在新内核里重新建立）；
4. **`shmem_add_to_page_cache`**——这是整个流程里最核心的一步：
   把 folio 正式插入这个 inode 的 `address_space`（即 page cache），
   并记录它在文件中的逻辑页偏移 `index`。从这一刻起，这个 folio
   才真正成为"文件的一部分"——后续的 `read`/`mmap` 等操作能够通过
   page cache 查找机制找到它；
5. **重新应用 `flags`**——`folio_mark_uptodate`/`folio_mark_dirty`
   按照序列化时记录的标志位重新打上对应的状态标记（回顾 9.4.4 节，
   保存时这两个标志位被无条件置位，因此恢复时这里实际上总是会执行
   两个分支，但代码仍然按照"读取标志位、按位判断"的通用方式实现——
   这是一种面向未来的弹性写法：即便当前版本总是无条件设置这两个标志，
   `retrieve` 端的实现并不对这一点做任何假设，而是忠实地按照序列化
   数据中的实际取值来重建状态。如果未来 `preserve` 端的逻辑变得更精细
   （比如真的实现了"精确记录 dirty 状态"的优化），`retrieve` 端的代码
   不需要做任何修改就能正确处理）；
6. **`shmem_inode_acct_blocks`**——把这个 folio 占用的页面数计入
   inode 级别的块使用统计（shmem 文件系统的配额/统计机制，与
   `mem_cgroup_charge` 是两个不同维度的记账——前者是 cgroup 资源控制
   视角，后者是文件系统自身的容量统计视角）；
7. **`folio_add_lru` + `folio_unlock` + `folio_put`**——把 folio 加入
   LRU 链表（使其重新进入正常的内存回收候选队列管理范围）、解锁、
   释放从 `kho_restore_folio` 获得的引用计数（page cache 此刻已经
   持有了自己的引用，调用者的引用可以安全释放）。

最后，`shmem_recalc_inode(inode, nr_added_pages, 0)` 统一更新 inode
级别的页面计数缓存——这是 shmem 内部用于快速查询"这个文件实际占用了
多少页面"的统计机制，必须在所有 folio 都处理完毕后做一次性更新，
而不是在循环内部逐次更新（减少不必要的锁竞争和缓存行抖动）。

**错误处理的三级跳脱链**：函数末尾的 `remove_from_cache`/`unlock_folio`/
`put_folios` 三个标签，精确对应着循环体内三个不同阶段可能失败时
"已经达成的状态"：

```
   失败点                           需要撤销的状态（按"已建立"的逆序）
   ──────────────────────────────────────────────────────────────
   shmem_inode_acct_blocks 失败  →  remove_from_cache: 已加入 page cache，
                                                        需要 filemap_remove_folio
                                     unlock_folio:      已加锁，需要 unlock
                                     put_folios:        已持有引用，需要 put
   ──────────────────────────────────────────────────────────────
   shmem_add_to_page_cache 失败  →  unlock_folio + put_folios
                                     （尚未加入 cache，不需要 remove）
   ──────────────────────────────────────────────────────────────
   mem_cgroup_charge 失败        →  unlock_folio + put_folios
                                     （同上，加锁但未入 cache）
   ──────────────────────────────────────────────────────────────
   kho_restore_folio 失败        →  put_folios
                                     （folio 都还没拿到，直接进入收尾）
```

这种"多个标签层叠掉落、按 `goto` 跳入点精确对应不同的已完成状态"的写法，
是 C 语言错误处理中表达"部分完成、需要差异化回滚"语义的标准范式——
它比为每一种失败情形单独写一段完整的清理代码（导致大量重复）、或者
用一个无差别的"全部回滚"函数（导致对尚未建立的状态做出错误的撤销操作，
比如对一个根本没有加入 page cache 的 folio 调用 `filemap_remove_folio`）
都更精确、更安全。

`put_folios` 标签下还有一段专门的收尾逻辑，负责处理"循环中途失败时，
那些**尚未被处理到**的剩余 folio"：

```c
put_folios:
        /*
         * Note: don't free the folios already added to the file. They will be
         * freed when the file is freed. Free the ones not added yet here.
         */
        for (long j = i + 1; j < nr_folios; j++) {
                ...
                folio = kho_restore_folio(phys);
                if (folio)
                        folio_put(folio);
        }
```

这段逻辑里的注释一针见血地点出了"为什么从 `i + 1` 开始、而不是
`i`"——**已经成功添加进 page cache 的那些 folio（包括当前失败的这一个，
取决于失败发生在哪个子步骤——但代码已经在前面的 `unlock_folio`/
`remove_from_cache` 分支里妥善处理了它），它们的引用计数生命周期
此刻已经与"文件本身"绑定**——当这个新创建的 `file` 最终被 `fput`
释放时，page cache 的销毁路径会自动 `folio_put` 它们，调用者不需要、
也不应该重复释放（重复释放会导致引用计数下溢，触发严重的内存损坏）。
而 **从 `i + 1` 到 `nr_folios - 1` 这一段"还没来得及处理"的 folio**，
它们仅仅被 `kho_restore_folio` 临时取得了一份引用（还没有进入 page
cache、不会被文件销毁路径自动处理），必须在这里手动 `folio_put`
释放掉，否则就会成为"既不在 page cache 里、又没有任何人持有它们的
最终引用"的孤儿对象——永久泄漏。

这种"明确划分'已托付给某个更大对象自动管理的资源' vs. '仍然由我自己
负责释放的资源'"的边界意识，是阅读和编写涉及复杂所有权转移的内核代码时
最容易出错、也最考验功力的地方——这段代码用一句精炼的注释，把这条
本来需要反复推敲才能想清楚的边界，清晰地刻画了出来。

### 9.8 `memfd_luo_finish` 与 `memfd_luo_discard_folios`：当 retrieve 从未发生时的善后

最后一块拼图，是文件生命周期的终点——`memfd_luo_finish()`
（`memfd_luo.c:387-415`）：

```c
static void memfd_luo_finish(struct liveupdate_file_op_args *args)
{
        struct memfd_luo_folio_ser *folios_ser;
        struct memfd_luo_ser *ser;

        /*
         * If retrieve was successful, nothing to do. If it failed, retrieve()
         * already cleaned up everything it could. So nothing to do there
         * either. Only need to clean up when retrieve was not called.
         */
        if (args->retrieve_status)
                return;

        ser = phys_to_virt(args->serialized_data);
        if (!ser)
                return;

        if (ser->nr_folios) {
                folios_ser = kho_restore_vmalloc(&ser->folios);
                if (!folios_ser)
                        goto out;

                memfd_luo_discard_folios(folios_ser, ser->nr_folios);
                vfree(folios_ser);
        }

out:
        kho_restore_free(ser);
}
```

开篇的注释直接揭示了这个函数存在的全部理由——`if (args->retrieve_status)
return;`：

```
/*
 * If retrieve was successful, nothing to do. If it failed, retrieve()
 * already cleaned up everything it could. So nothing to do there
 * either. Only need to clean up when retrieve was not called.
 */
```

回顾第 6 章 6.7 节关于 `retrieve_status` 三态缓存的讨论——它有三种取值：
**0（从未尝试过 retrieve）**、**正值（retrieve 成功，缓存着 fd）**、
**负值（retrieve 失败，缓存着错误码）**。`memfd_luo_finish` 这里的
判断条件 `if (args->retrieve_status)` 同时覆盖了"成功"和"失败"两种
**非零**情形——这恰好对应着上面注释里说的"两种已经处理过的情形不需要
再做任何事"：

* **`retrieve_status > 0`（成功）**：这意味着 `memfd_luo_retrieve`
  已经成功跑完全程，新的 `file` 对象已经建立、page cache 已经重建好、
  `args->file` 已经被设置——此时 `ser` 本身的物理内存也已经在
  `memfd_luo_retrieve` 的成功路径末尾被 `kho_restore_free(ser)`
  释放掉了（回顾 9.7 节代码），整个生命周期已经圆满完结，没有任何
  额外的清理需要在 `finish` 阶段进行。
* **`retrieve_status < 0`（失败）**：`memfd_luo_retrieve` 在它自己
  的错误路径里已经做了完整的清理——无论失败发生在哪个子步骤，
  最终都会经过 `put_file`/`free_ser` 标签，把已分配的 `file`、
  `ser` 全部释放干净（回顾 9.7 节的两层 `goto` 链）。"它已经尽其所能
  地清理过了"——这正是注释里那句"retrieve() already cleaned up
  everything it could"的真实含义。

那么剩下的、真正需要 `finish` 出手处理的，只有**第三种情形**——
`retrieve_status == 0`，**`retrieve` 从未被调用过**。这对应着一种
颇为微妙的真实场景：用户在新内核里创建/检索了 session、甚至可能
枚举了其中的文件 token，但**从未实际调用 `retrieve_fd` 取回这个具体的
文件**，就直接调用了 `session_finish`。第 6 章 6.7 节已经分析过，
LUO 允许这种"跳过 retrieve、直接 finish"的操作模式——此时旧内核
保存下来的那些物理页（folio）、序列化结构 `ser`、`folios_ser` 数组，
全都还原封不动地"挂"在 KHO 的存活清单中，从未被认领、从未进入
任何 page cache——它们正是一笔需要被妥善偿还的"遗产债务"。

`memfd_luo_finish` 正是专门为了偿还这笔债务而存在——它的逻辑结构
与 `memfd_luo_retrieve` 形成微妙的对称："retrieve 是把这些 folio
**纳入**一个新建的活文件中"，而当 retrieve 从未发生时，`finish`
要做的是把它们**逐一释放**，但**不需要**真正建立任何 page cache
结构（因为根本不存在一个对应的活文件）。这正是 `memfd_luo_discard_folios()`
（`memfd_luo.c:362-385`）所体现的"轻量级丢弃"语义：

```c
static void memfd_luo_discard_folios(const struct memfd_luo_folio_ser *folios_ser,
                                     u64 nr_folios)
{
        u64 i;

        for (i = 0; i < nr_folios; i++) {
                const struct memfd_luo_folio_ser *pfolio = &folios_ser[i];
                struct folio *folio;
                phys_addr_t phys;

                if (!pfolio->pfn)
                        continue;

                phys = PFN_PHYS(pfolio->pfn);
                folio = kho_restore_folio(phys);
                if (!folio) {
                        pr_warn_ratelimited("Unable to restore folio at physical address: %llx\n",
                                            phys);
                        continue;
                }

                folio_put(folio);
        }
}
```

对比 `memfd_luo_retrieve_folios`（9.7 节）那一长串"加锁、charge、
加入 page cache、记账、加入 LRU"的繁复步骤，`discard_folios` 只剩下
最朴素的核心——`kho_restore_folio` 取回 folio、`folio_put` 释放引用。
这组对比鲜明地说明了 `retrieve` 与 `finish`（在"未曾 retrieve"这条
分支下）之间的本质区别：**前者要把这些物理页面"扶上正轨"、重新成为
一个活跃文件系统对象不可分割的一部分；后者只需要确认"这些内存不再
被任何人需要，可以安全地交还给伙伴系统"**。

`pr_warn_ratelimited` 而不是 `pr_err`——这里的日志级别选择也值得
玩味：在"丢弃一个本来就要被丢弃的对象"这条路径上，即便遇到
"`kho_restore_folio` 找不到对应物理页"这种异常情况（理论上不应该
发生，但防御性地处理），也只是警告级别且限速打印，而不是当作
严重错误处理并中断流程——因为此刻我们正处于"清理收尾"阶段，
即便某个 folio 状态异常，也不应该让它阻塞整个 `finish` 流程的完成
（`finish` 回调本身是 `void` 返回值，无法向上层报告错误——这也再次
印证了第 6 章总结的"清理路径不允许失败"设计公理）。

### 9.9 四条错误/清理路径的语义对比表

读完 `preserve`/`freeze`/`unpreserve`/`finish`/`retrieve` 的全部实现后，
不妨把它们在**清理责任边界**上的差异系统地汇总成一张表——这张表回答的
核心问题是："当事情没有按照'preserve → freeze → kexec → retrieve →
finish' 这条理想路径发展时，到底是谁负责把 `ser`/`folios_ser`/
folio pin 状态/KHO 登记 这四类资源清理干净？"

| 路径 | 触发场景 | 负责清理的函数 | 清理范围 |
|---|---|---|---|
| **preserve 失败** | `kho_alloc_preserve`/seals 校验/`preserve_folios` 内部任一步骤失败 | `memfd_luo_preserve` 自身的 `err_free_ser`/`err_unlock` 链 + `memfd_luo_preserve_folios` 自身的三层 `err_*` 链 | 已分配的 `ser`、已 pin/preserve 的 folio 子集、`folios_ser` 数组、inode 锁与 freeze 标记——**全部**在本次调用内原地回滚，函数返回时仿佛什么都没发生过 |
| **unpreserve（用户主动撤销 / session 异常关闭前回滚）** | preserve 成功之后、真正 reboot 之前，用户调用 `unpreserve_fd` 或 LUO 核心因其他文件失败而触发回滚 | `memfd_luo_unpreserve` + `memfd_luo_unpreserve_folios` | 解除全部 folio 的 KHO 登记与 pin 状态、`vfree(folios_ser)`、`kho_unpreserve_free(ser)`、解除 `shmem_freeze` 恢复可写——把文件**完全恢复到 preserve 之前的状态**，仿佛从未被保存过 |
| **retrieve 成功 / 失败** | 新内核里用户调用 `retrieve_fd` | `memfd_luo_retrieve` 自身的 `put_file`/`free_ser` 链（失败时）；成功时在函数末尾自行 `kho_restore_free(ser)` | 无论成功还是失败，`ser` 物理内存**总是**在 `retrieve` 内部被处理掉；失败时还会回滚已建立的部分 page cache 状态、释放新建的 `file` |
| **finish（retrieve 从未被调用）** | 用户跳过 `retrieve_fd`、直接 `session_finish` | `memfd_luo_finish` + `memfd_luo_discard_folios` | 以"轻量丢弃"的方式释放 `folios_ser` 数组对应的全部 folio 引用、`vfree(folios_ser)`、`kho_restore_free(ser)`——不建立任何 page cache 结构 |

这张表格最精炼地传达了一个事实：**LUO/memfd 这套体系里，每一类资源
（`ser` 物理内存、`folios_ser` 数组、folio 的 pin/KHO 登记状态、inode
锁与冻结标记）在其完整生命周期中的任意一个时间点上，都恰好存在唯一一个
"当前阶段负责人"**——不会出现"两个函数都以为对方会清理、结果谁都没清理"
的真空地带，也不会出现"两个函数都尝试清理、导致重复释放"的踩踏现场。
这种"职责边界处处分明、永不重叠也永不缺失"的设计纪律，正是 `mm/memfd_luo.c`
这 622 行代码能够承载"跨内核版本传递用户关键数据"这一高风险职责、
却依然保持清晰可读、易于审计的根本原因。

### 9.10 小结：memfd 是整个 LUO 抽象的"试金石"

回顾全章，`mm/memfd_luo.c` 之所以是理解 LUO 框架的最佳"练手案例"，
正是因为它几乎触碰到了第 2-8 章介绍过的**每一个**抽象层面：

* 它严格遵循 `liveupdate_file_ops` 的回调矩阵（第 6 章），用具体实现
  填满了 `preserve`/`freeze`/`unpreserve`/`retrieve`/`finish`/
  `can_preserve`/`get_id` 这一整套接口；
* 它把 `args->data`/`serialized_data`/`private_data` 这组"不透明
  句柄三元组"（第 6 章）落实为 `memfd_luo_ser` 物理地址、
  `folios_ser` 虚拟地址等具体的内存对象；
* 它的核心序列化结构 `memfd_luo_ser`/`memfd_luo_folio_ser` 严格遵守
  第 8 章总结的 `__packed` + 位域压缩 + `kho_vmalloc` 大数组传递
  等 ABI 设计规范；
* 它大量调用第 2 章介绍的 KHO 基础 API（`kho_preserve_folio`/
  `kho_preserve_vmalloc`/`kho_alloc_preserve`/`kho_restore_*`），
  是 KHO 这套底层机制在真实子系统中落地的活教材；
* 它在面对"无法精确判断的不确定性"（dirty/uptodate 标志位的真实状态）
  时所作出的保守抉择，与第 6 章总结的"宁可泄漏不做不安全 undo"哲学
  遥相呼应，进一步印证了这是 LUO 全局层面的设计共识，而非某个模块的
  孤立选择；
* 它的四条错误/清理路径之间泾渭分明的责任边界，也是对第 6 章"两阶段
  提交"、"七层回滚链"等设计模式在真实、复杂业务逻辑中如何落地的
  最佳验证。

正因如此，下一章我们将以 `mm/memfd_luo.c` 作为"标准答案"，逐行串讲
selftest 用例（`tools/testing/selftests/liveupdate/`）——看看用户态
程序如何驱动这一整套精密的内核机制，完成一次完整的、跨越 kexec 重启
的端到端验证。

---

## 第 10 章 端到端实例串讲——selftest 逐行解读

> 本章源码：`tools/testing/selftests/liveupdate/`
> （`liveupdate.c`、`luo_test_utils.{c,h}`、`luo_kexec_simple.c`、`luo_multi_session.c`、
> `do_kexec.sh`、`Makefile`、`config`，共 7 个文件）

前面九章我们一直在"自顶向下"地阅读内核源码——从抽象模型到具体回调，
从数据结构到字节布局。本章换一个方向："**自底向上**"，从用户态一行一行
真实的 ioctl 调用出发，回过头去印证前面章节里画过的每一张状态机图、
每一条调用链——这是检验"我们是否真正读懂了"这套机制的最佳方式：
如果一张时序图能够精确预测 selftest 里每一行代码的行为，那就说明
我们对这套机制的理解是扎实的。

LUO 的 selftest 套件由两类性质迥异的测试构成，分别验证系统的两个不同维度：

| 测试类型 | 代表文件 | 验证目标 | 是否需要真实 kexec |
|---|---|---|---|
| **单内核功能性测试** | `liveupdate.c` | ioctl 接口的正确性、错误码语义、并发/重复保存检测等"静态"行为 | 否——在同一个内核里运行完毕 |
| **跨 kexec 端到端测试** | `luo_kexec_simple.c`、`luo_multi_session.c` | 数据能否真正穿越一次真实的 kexec 重启并被完整恢复——这是 LUO 存在的全部意义 | 是——必须配合 `do_kexec.sh` 真实重启机器 |

这个二分恰好对应着第 4 章总结的"两级设备模型"——前者主要在
"`/dev/liveupdate` 全局设备 + session fd"这一层打转，后者则贯穿了
本文档第 0 章就已经强调的、LUO 真正的核心使命：**让状态在 `kexec_exec()`
这道生死之坎上完整存活**。

### 10.1 测试基础设施总览

先看一下整个目录的"地图"（结合 `Makefile`/`config`/`.gitignore`，此处不再
逐行贴出这些构建脚本，仅总结其关键约定）：

* `Makefile` 把 `liveupdate`、`luo_kexec_simple`、`luo_multi_session`
  三个可执行文件分别从 `liveupdate.c`、`luo_kexec_simple.c + luo_test_utils.c`、
  `luo_multi_session.c + luo_test_utils.c` 构建出来——后两者**共享**
  `luo_test_utils.c` 这个工具库，这正是本章接下来要重点解读的"跨 kexec
  测试公共基础设施"；
* `config` 文件列出了运行这些测试所需要开启的内核配置项（如
  `CONFIG_LIVEUPDATE`、`CONFIG_KEXEC`、`CONFIG_MEMFD_CREATE` 等），
  这是 kselftest 框架的标准约定——告诉测试运行环境"在执行我之前，
  请确保被测内核已经打开了这些选项"；
* `do_kexec.sh` 是一个极简的 shell 脚本封装——它的全部职责就是用
  `kexec -l` 加载新内核镜像、再用 `kexec -e` 真正触发重启。
  这个脚本的存在揭示了一个朴素却关键的事实：**LUO 的端到端验证
  天然需要操作系统级别的特权操作（重启机器）**，无法像绝大多数单元
  测试那样在一个隔离的进程沙箱里几毫秒内跑完——这从根本上决定了
  跨 kexec 测试用例必须采用一种"能够跨越真实重启、记住自己上次跑到
  哪一步"的特殊结构，这正是 `luo_test_utils.c` 存在的全部理由
  （详见 10.4 节）。

### 10.2 `liveupdate.c`：单内核功能性测试逐用例解读

`liveupdate.c` 使用了 kselftest 的 `FIXTURE`/`TEST_F` 测试框架——
这是一套与 GoogleTest 神似的 C 语言测试夹具系统：`FIXTURE` 定义了
每个测试用例共享的状态（这里是 `fd1`/`fd2` 两个文件描述符），
`FIXTURE_SETUP`/`FIXTURE_TEARDOWN` 分别在每个用例运行前后自动调用，
`TEST_F` 把测试逻辑与夹具绑定在一起——这种结构保证了即便某个用例
中途因为断言失败而提前退出，`fd1`/`fd2` 也总能在 `FIXTURE_TEARDOWN`
里被正确关闭，不会泄漏文件描述符污染后续用例。

它一共定义了 9 个测试用例，恰好分别针对前面章节里讨论过的某个具体
设计点——我们逐一对照：

#### 10.2.1 `basic_open_close` / `exclusive_open` —— 验证单例设备模型

```c
TEST_F(liveupdate_device, basic_open_close)
{
        self->fd1 = open(LIVEUPDATE_DEV, O_RDWR);
        if (self->fd1 < 0 && errno == ENOENT)
                SKIP(return, "%s does not exist.", LIVEUPDATE_DEV);

        ASSERT_GE(self->fd1, 0);
        ASSERT_EQ(close(self->fd1), 0);
        self->fd1 = -1;
}

TEST_F(liveupdate_device, exclusive_open)
{
        self->fd1 = open(LIVEUPDATE_DEV, O_RDWR);
        ...
        ASSERT_GE(self->fd1, 0);
        self->fd2 = open(LIVEUPDATE_DEV, O_RDWR);
        EXPECT_LT(self->fd2, 0);
        EXPECT_EQ(errno, EBUSY);
}
```

这两个用例直接对应第 3 章 3.4 节解读过的 `/dev/liveupdate` 单例设备
模型——内核用一个 `atomic_cmpxchg` 实现"全系统范围内只允许一个进程
持有该设备的打开实例"这一约束。`exclusive_open` 用例正是这条约束
最直白的验证：第一次 `open` 必须成功，第二次 `open`（无论是否同一
进程）必须返回 `-1` 且 `errno == EBUSY`。这里隐含着一条重要的设计
逻辑——**为什么 LUO 要把"独占访问"焊死在设备打开层面**？因为
session、文件保存等所有后续操作都建立在"当前只有一个协调者在驱动
整个 live-update 流程"这一假设之上；如果允许多个进程同时打开设备
并发地创建 session、preserve 文件，整个状态机的正确性论证将变得
极其复杂（甚至不可能成立）。"在最外层的入口处就把并发可能性焊死"，
是一种"用最简单的手段消除最复杂的潜在 bug 来源"的设计智慧。

`if (self->fd1 < 0 && errno == ENOENT) SKIP(...)`——这个模式在全部
9 个用例中反复出现，体现了 kselftest 编写中一条重要的惯例：
**优雅地处理"被测功能在当前内核中根本不存在"的情形**——`ENOENT`
表示 `/dev/liveupdate` 节点不存在（可能是因为 `CONFIG_LIVEUPDATE`
未开启、或 LUO 模块未加载），此时整个测试套件应当被**跳过**而不是
**失败**——"功能未启用"和"功能启用了但工作不正常"是两种性质完全
不同的结果，理应映射到测试框架里两种不同的结果状态（`SKIP` vs.
`FAIL`），这能帮助 CI 系统和开发者快速分辨"这是个真正的 bug"还是
"这只是测试环境配置不全"。

#### 10.2.2 `create_duplicate_session` / `create_distinct_sessions` —— 验证命名冲突检测

```c
TEST_F(liveupdate_device, create_duplicate_session)
{
        ...
        session_fd1 = create_session(self->fd1, "duplicate-session-test");
        ASSERT_GE(session_fd1, 0);

        session_fd2 = create_session(self->fd1, "duplicate-session-test");
        EXPECT_LT(session_fd2, 0);
        EXPECT_EQ(-session_fd2, EEXIST);
        ...
}
```

这正是第 5 章 5.3 节讨论过的 `luo_session_insert()` 命名冲突检测
逻辑在用户态的直接体现——同名 session 的第二次创建必须返回
`-EEXIST`。`create_distinct_sessions` 则是它的镜像验证："只要名字不同，
创建多少个 session 都应该顺利成功"——两个用例合在一起，完整覆盖了
"唯一性约束"这条不变式的正反两面。

#### 10.2.3 `preserve_memfd` —— 验证最基本的"保存后内容不受影响"

```c
TEST_F(liveupdate_device, preserve_memfd)
{
        const char *test_str = "hello liveupdate";
        char read_buf[64] = {};
        ...
        mem_fd = memfd_create("test-memfd", 0);
        ASSERT_GE(mem_fd, 0);

        ASSERT_EQ(write(mem_fd, test_str, strlen(test_str)), strlen(test_str));
        ASSERT_EQ(preserve_fd(session_fd, mem_fd, 0x1234), 0);
        ASSERT_EQ(close(session_fd), 0);

        ASSERT_EQ(lseek(mem_fd, 0, SEEK_SET), 0);
        ASSERT_EQ(read(mem_fd, read_buf, sizeof(read_buf)), strlen(test_str));
        ASSERT_STREQ(read_buf, test_str);
        ASSERT_EQ(close(mem_fd), 0);
}
```

这个用例的精妙之处在于它验证的是一个**极易被忽视、却至关重要**的
不变式——"调用 `preserve_fd` 这个动作本身，绝不能影响这个 fd 在
**当前内核**里的正常可用性"。试想：如果 `memfd_luo_preserve` 的实现
方式是"把数据从 page cache 中搬走、单独存放在 KHO 区域里"，那么
preserve 之后，用户态进程通过原来的 `mem_fd` 读取数据就会得到错误
结果——这不仅会让这个测试失败，更会让"先保存、再继续正常使用、
最后才真正重启"这一 LUO 设计的核心使用范式（回顾第 1 章动机分析中
"云 hypervisor 在准备热升级期间仍需正常服务客户机"的场景）变得
完全不可能。

回顾第 9 章对 `memfd_luo_preserve_folios` 的解读——它选择 **"原地
pin 住 folio、在旁边记录元数据快照"**而不是"搬移数据"，正是为了
保证这条不变式：preserve 之后，原来的 `mem_fd` 仍然指向同一批
page cache 中的 folio，读写行为与 preserve 之前完全一致——这个
selftest 用例，正是对这一设计取舍最直接的回归验证。`close(session_fd)`
这一步同样值得注意——回顾第 5 章 5.5 节关于 `luo_session_release`
"`retrieved` 标记驱动两条岔路"的讨论：在**当前**内核里关闭一个尚未
被标记为 `retrieved` 的 session fd，并**不会**撤销其中的保存（它会
被正常移交进 outgoing 链表，等待真正的 reboot）——这正是用户态
"先准备好所有要保存的状态，再决定何时真正触发 reboot"这一工作流
的体现。

#### 10.2.4 `preserve_multiple_memfds` / `preserve_complex_scenario` —— 验证多对象隔离性

这两个用例本质上是 `preserve_memfd` 在"数量"和"拓扑复杂度"两个维度上
的扩展：前者验证"同一个 session 里保存多个不同 token 的文件，数据
不会互相串扰"；后者构造了一个更接近真实场景的拓扑——两个 session、
四个 memfd（其中两个为空、两个有数据），交叉验证"无论文件是否为空、
属于哪个 session，保存动作互不干扰、各自独立"。

`preserve_complex_scenario` 里特意混入了空 memfd（`mem_fd_empty1`/
`mem_fd_empty2`，创建后从未写入），并在末尾验证 `read(...) == 0`——
这恰好对应第 9 章 9.4.1 节讨论过的"空文件快速路径"
（`memfd_luo_preserve_folios` 中 `if (!size) { ... return 0; }`）。
selftest 特意覆盖这条边界分支，说明测试设计者非常清楚"边界情形
（空输入）往往是 bug 的高发区"这一普遍规律——这是编写高质量测试
用例时值得借鉴的思维方式：不仅要测试"主路径work"，更要主动构造
那些"容易被实现者忽略"的边界形态。

#### 10.2.5 `preserve_unsupported_fd` —— 验证 handler 匹配失败的错误路径

```c
TEST_F(liveupdate_device, preserve_unsupported_fd)
{
        ...
        unsupported_fd = open("/dev/null", O_RDWR);
        ASSERT_GE(unsupported_fd, 0);

        ret = preserve_fd(session_fd, unsupported_fd, 0xDEAD);
        EXPECT_EQ(ret, -ENOENT);
        ...
}
```

这个用例直接对照第 6 章 6.4 节 `luo_preserve_file()` 的"七层回滚链"
最前端的逻辑——遍历已注册的 `liveupdate_file_handler` 链表，通过
逐一调用 `can_preserve()` 寻找一个愿意接纳这个文件的 handler；
如果遍历完毕仍未找到（`/dev/null` 是一个字符设备文件，没有任何
已注册的 handler 会认领它），返回 `-ENOENT`。回顾第 4 章 4.5 节的
错误码语义表——`-ENOENT` 在这里的含义是"没有找到与此文件匹配的
保存处理器"，selftest 的这个用例把这条错误码语义钉死成了一条
可自动验证的回归测试，确保未来任何对 handler 匹配逻辑的重构都不会
意外地改变这个对外可观测的错误码。

特意选择 `/dev/null` 作为"不支持类型"的代表，注释里解释得很清楚——
它是"字符设备这一类不被 orchestrator 支持的文件类型的代表"。这种
"选择一个具有代表性、且系统上必然存在、行为稳定可预期"的对象作为
测试素材的做法，也是编写可移植、可重复测试用例时的常见考量
（不依赖于任何特定的硬件配置或可选模块）。

#### 10.2.6 `prevent_double_preservation` —— 验证 xarray 防重复机制

```c
TEST_F(liveupdate_device, prevent_double_preservation)
{
        ...
        /* First preservation should succeed */
        ASSERT_EQ(preserve_fd(session_fd1, mem_fd, 0x1111), 0);

        /* Second preservation in a different session should fail with EBUSY */
        ret = preserve_fd(session_fd2, mem_fd, 0x2222);
        EXPECT_EQ(ret, -EBUSY);

        /* Second preservation in the same session (different token) should fail with EBUSY */
        ret = preserve_fd(session_fd1, mem_fd, 0x3333);
        EXPECT_EQ(ret, -EBUSY);
        ...
}
```

这是整个 `liveupdate.c` 测试套件里**覆盖面最精巧**的一个用例——
它用三行断言，同时验证了第 6 章 6.2 节讨论过的 `luo_preserved_files`
全局 xarray 防重复机制的**两种**触发场景：

1. **跨 session 重复**——同一个底层 inode（`mem_fd`）已经在
   `session_fd1` 中被保存，再尝试保存进完全不同的 `session_fd2`，
   必须被拒绝；
2. **同 session 内重复（即便 token 不同）**——即便是在**同一个**
   session 内，用一个**不同的** token（`0x3333` vs. 最初的 `0x1111`）
   再次尝试保存同一个 `mem_fd`，依然必须被拒绝。

第二种场景尤其值得品味——它排除了一种可能的"想当然"误解："是不是
只要 token 不同，就可以把同一个文件保存多份？"`-EBUSY` 的回答是
斩钉截铁的"不行"：**LUO 防重复检测的判定依据是"底层文件对象的唯一
标识"（即 `memfd_luo_get_id` 返回的 `inode` 地址），而不是用户提供的
`token`**——`token` 只是"找回时如何称呼它"的标签，不能、也不应该
被用来绕过"同一份物理资源只能被纳入保存计划一次"这一更底层的约束
（回顾 9.1 节讨论过的——重复保存同一份 folio 集合会引发数据不一致
甚至双重 pin 的灾难性后果）。这正是"测试不仅要验证'功能按预期工作'，
还要验证'功能在那些容易被误用的边界条件下，按照设计者的意图、
而不是用户的一厢情愿来拒绝请求'"这一更高阶测试设计理念的体现。

### 10.3 跨 kexec 测试的核心难题：如何让一个测试程序"记住"自己上次执行到哪一步

读完 `liveupdate.c`，我们已经验证了"接口在单个内核生命周期内行为正确"。
但 LUO 真正的价值主张——"数据能穿越 kexec 存活"——却完全无法在
单进程、单内核生命周期的测试模型里被验证。`kexec_exec()` 一旦执行，
当前所有的进程、内存、文件描述符**全部**化为乌有，测试程序自身也
随之消失——我们如何让"重启前做了什么"和"重启后看到了什么"这两个
分散在两次完全独立的进程生命周期里的观察结果，被关联、比对起来？

`luo_test_utils.c`/`luo_test_utils.h` 给出的答案是一套精巧的
**"双阶段、自我识别"**测试框架——核心想法是：**让测试程序自己
判断"我现在是在重启之前运行，还是在重启之后运行"，并通过 LUO
本身提供的保存机制，把"接下来该做什么"这条指令也保存下来、随
kexec 一起传递过去**。这是一种近乎哲学意味的自指设计——
**用被测系统自身的核心能力，来搭建测试这个系统所需要的脚手架**。

#### 10.3.1 阶段判定：`luo_test()` 的双重验证逻辑

整个框架的总入口是 `luo_test()`（`luo_test_utils.c:230-266`）：

```c
int luo_test(int argc, char *argv[],
             const char *state_session_name,
             luo_test_stage1_fn stage1,
             luo_test_stage2_fn stage2)
{
        int target_stage = parse_stage_args(argc, argv);
        int luo_fd = luo_open_device();
        int state_session_fd;
        int detected_stage;

        if (luo_fd < 0) {
                ksft_exit_skip("Failed to open %s. Is the luo module loaded?\n",
                               LUO_DEVICE);
        }

        state_session_fd = luo_retrieve_session(luo_fd, state_session_name);
        if (state_session_fd == -ENOENT)
                detected_stage = 1;
        else if (state_session_fd >= 0)
                detected_stage = 2;
        else
                fail_exit("Failed to check for state session");

        if (target_stage != detected_stage) {
                ksft_exit_fail_msg("Stage mismatch Requested --stage %d, but system is in stage %d.\n"
                                   "(State session %s: %s)\n",
                                   target_stage, detected_stage, state_session_name,
                                   (detected_stage == 2) ? "EXISTS" : "MISSING");
        }

        if (target_stage == 1)
                stage1(luo_fd);
        else
                stage2(luo_fd, state_session_fd);

        return 0;
}
```

这个函数最值得品味的设计，是它采用了**双重验证**——既检查命令行
参数 `--stage`（外部驱动脚本/操作者明确告诉测试程序"现在该跑第几
阶段了"），又通过查询一个事先约定好名字的"状态 session"是否存在
（`luo_retrieve_session(luo_fd, state_session_name)`）来"用系统本身
的状态自证"当前究竟处于哪个阶段。两者必须**一致**——`if (target_stage
!= detected_stage)`，否则立即失败退出。

为什么需要这种"既听指挥、又自我核实"的双保险？因为这两种判定方式
各自都存在单独使用时的盲区：

* **仅依赖命令行参数**：如果测试脚本本身存在 bug（比如在错误的时机
  调用了 `--stage 2`，而实际上 kexec 还没有发生），测试程序会"信以为真"
  地跳过准备阶段、直接尝试验证一个根本不存在的 session——产生的错误
  信息会高度误导人（"session 不存在"看起来像是 LUO 的 bug，实际上是
  测试驱动逻辑的 bug）；
* **仅依赖系统状态自检**：如果某次运行因为意外（例如内核 crash、
  KHO 数据损坏）导致 session 没有被正确保存/恢复，测试程序会把这次
  本应判定为"阶段 2 但数据缺失"的失败，误判成"我们仍处于阶段 1"，
  从而错误地重新执行一遍 `stage1`——这同样会把"数据丢失"这个真正的
  bug 掩盖在一团更混乱的错误现象之下。

把两者结合起来、要求严格一致，能够在第一时间精确地区分"测试驱动逻辑
出错"与"被测系统本身出错"这两类性质完全不同的失败，给出
`ksft_exit_fail_msg` 中那条信息量极高的诊断消息（同时报告
"期望阶段"、"探测到的阶段"、"状态 session 是否存在"）——这是测试
基础设施代码里"投资于诊断信息质量"的典范，能够大幅缩短未来排查
偶发性失败所需要的时间。

`parse_stage_args()`（`luo_test_utils.c:205-228`）本身只是一个标准
的 `getopt_long` 封装，解析 `--stage`/`-s` 参数并校验其取值只能是
`1` 或 `2`——这里不再展开。

#### 10.3.2 状态接力的核心机制：`create_state_file` / `restore_and_read_stage`

判定"现在是第几阶段"只是第一步——更关键的问题是："**测试程序在阶段 1
运行完毕、即将触发 kexec 时，如何把'接下来该跑阶段 2'这条指令本身
传递到重启后的世界里？**"`luo_test_utils.c` 给出的答案极其优雅——
**复用 LUO 自身的"创建 session + preserve memfd"机制，把'下一阶段
编号'这个整数，编码进一个 memfd 的内容里，作为一个专门的"状态 session"
保存下来**：

```c
void create_state_file(int luo_fd, const char *session_name, int token,
                       int next_stage)
{
        char buf[32];
        int state_session_fd;

        state_session_fd = luo_create_session(luo_fd, session_name);
        if (state_session_fd < 0)
                fail_exit("luo_create_session for state tracking");

        snprintf(buf, sizeof(buf), "%d", next_stage);
        if (create_and_preserve_memfd(state_session_fd, token, buf) < 0)
                fail_exit("create_and_preserve_memfd for state tracking");

        /*
         * DO NOT close session FD, otherwise it is going to be unpreserved
         */
}
```

这个函数把"下一阶段编号"格式化成十进制字符串（例如 `"2"`），写入
一个新建的 memfd，再用我们已经在第 6、9 两章反复剖析过的标准
preserve 流程把它保存进一个名为 `state_session_name`（如
`"kexec_simple_state"`）的专属 session——这个 session 与测试用例
本身要验证的"业务 session"（如 `"test-session"`）完全独立，
专门用于"自我通信"。

末尾那条**绝不能被忽视**的注释——`/* DO NOT CLOSE SESSION FD,
OTHERWISE IT IS GOING TO BE UNPRESERVED */`——精确地指向了第 5 章
5.5 节讨论过的 `luo_session_release` 状态机分叉点：在**当前**内核
（即将重启的旧内核）里，如果在 `retrieved` 标记被设置之前关闭一个
session fd，会触发"判定为本地 session、按用户意图取消整个保存计划"
的那条分支（这是为了支持"用户中途反悔，关闭 fd 即代表放弃保存"
这一符合直觉的语义）。如果测试代码在这里手滑加上一行 `close()`，
辛辛苦苦编码进 memfd 里的"下一阶段编号"会在还没来得及随 kexec 传递
出去之前，就被 `luo_session_release` 判定为"用户主动放弃"而撤销
掉——整个状态接力机制将无声无息地失效，留下一个"重启后系统死活
检测不到状态 session、于是判定自己仍处于阶段 1、于是无限循环重跑
阶段 1"的诡异死循环。**这条注释，正是用文字的形式，把一条极易
被忽视、却足以让整个测试框架失效的隐含契约钉死在了代码里**——
这也提醒我们，在阅读和编写依赖于"对象生命周期边界处隐含语义"
的代码时，留下清晰、醒目的警示注释有多么重要。

镜像的另一半——`restore_and_read_stage()`（`luo_test_utils.c:156-171`）
则是在重启之后，从这个状态 session 中把编码好的整数取回来：

```c
void restore_and_read_stage(int state_session_fd, int token, int *stage)
{
        char buf[32] = {0};
        int mfd;

        mfd = restore_and_verify_memfd(state_session_fd, token, NULL);
        if (mfd < 0)
                fail_exit("failed to restore state memfd");

        if (read(mfd, buf, sizeof(buf) - 1) < 0)
                fail_exit("failed to read state mfd");

        *stage = atoi(buf);

        close(mfd);
}
```

注意这里调用 `restore_and_verify_memfd` 时把 `expected_data` 参数
传入了 `NULL`——这是一处微妙却体现了"工具函数应有的克制"的细节：
状态文件本身的内容是动态生成的（`"1"` 还是 `"2"`，取决于上一阶段
要求下一阶段是几），调用方此刻并不知道、也不应该预设它"应该"是
什么——校验内容是否符合预期，是 `restore_and_read_stage` 调用方
（即 `run_stage_2` 函数）自己的职责（它会把读到的 `stage` 值与
自己期望的常量 `2` 进行比较）。`restore_and_verify_memfd` 因而被
设计成一个"既能严格校验已知期望值、又能在不知道期望值时单纯地
完成恢复动作"的双用途工具——这是一种小巧却值得学习的 API 设计
技巧：用一个可选参数（这里是把 `NULL` 当作"不校验"的哨兵值），
让同一个函数服务于两种略有差异的调用场景，而不必拆分成两个几乎
重复的函数。

#### 10.3.3 让进程"活过"`exit(EXIT_SUCCESS)`：`daemonize_and_wait`

还有最后一个棘手的问题——回顾第 5 章关于 session 的"`retrieved`
标记如何驱动 release 分叉"的讨论：一个本地（未被标记为
`retrieved`）的 session，只有在它的 fd 在**测试进程退出时被自动
关闭**、抑或显式调用 `close` 时，才会触发"按用户意图处理"的分支
逻辑——而无论走哪条分支，这都要求**进程本身在 kexec 真正发生之前
必须持续存活**（一旦进程退出，所有 fd 自动关闭，那些尚未决定"是否
真的要保存"的 session 状态就可能被提前敲定甚至撤销）。

但是 selftest 框架的标准运行方式（`kselftest_harness`/直接执行
可执行文件）期望测试程序"运行、断言、退出"——它不会知道"在我退出
之后，还需要有一个后台守护进程继续攥着这些 fd，直到几分钟后操作员
手动触发 kexec"这种特殊需求。`daemonize_and_wait()`
（`luo_test_utils.c:173-203`）正是为了弥合这个矛盾而生——它实现了
一套标准的 **Unix daemon 化双重 fork 技巧**的简化版：

```c
void daemonize_and_wait(void)
{
        pid_t pid;

        ksft_print_msg("[STAGE 1] Forking persistent child to hold sessions...\n");

        pid = fork();
        if (pid < 0)
                fail_exit("fork failed");

        if (pid > 0) {
                ksft_print_msg("[STAGE 1] Child PID: %d. Resources are pinned.\n", pid);
                ksft_print_msg("[STAGE 1] You may now perform kexec reboot.\n");
                exit(EXIT_SUCCESS);
        }

        /* Detach from terminal so closing the window doesn't kill us */
        if (setsid() < 0)
                fail_exit("setsid failed");

        close(STDIN_FILENO);
        close(STDOUT_FILENO);
        close(STDERR_FILENO);

        /* Change dir to root to avoid locking filesystems */
        if (chdir("/") < 0)
                exit(EXIT_FAILURE);

        while (1)
                sleep(60);
}
```

逐句拆解这套"金蝉脱壳"的手法：

1. **`fork()`**——分裂出一个子进程。父进程立即打印提示信息并
   `exit(EXIT_SUCCESS)`——这是"对外宣告：我（作为 kselftest 框架
   认识的那个测试程序）已经成功完成了阶段 1 的全部准备工作，
   测试框架可以认为这一步'通过'了"；
2. **子进程 `setsid()`**——脱离原来的会话和控制终端，成为新会话的
   首进程。这一步是真正让它"活下去"的关键——如果不这样做，
   操作员关闭终端窗口（很可能在准备 kexec 重启的过程中会发生）
   时，内核会向整个会话发送 `SIGHUP`，把这个进程也一并杀死；
3. **关闭标准输入输出错误流、`chdir("/")`**——这是标准 daemon 化
   的收尾动作：不再依赖任何可能被卸载的终端设备，也不占用任何
   可能需要被卸载的文件系统挂载点（如果子进程的当前工作目录停留
   在某个文件系统上，会阻止该文件系统被正常卸载）；
4. **`while (1) sleep(60)`**——进入一个几乎不消耗任何 CPU 资源的
   无限循环，让进程长久地存活下去，持续攥着所有打开的 fd
   （包括 LUO 设备、各个 session、各个 memfd）——这正是"保持
   `retrieved == false` 的本地 session 持续存活，等待用户决定
   何时触发 kexec"这一需求所要求的全部条件。

`ksft_print_msg` 打印出的两条提示——"Resources are pinned"
（资源已被钉住）、"You may now perform kexec reboot"（你现在可以
执行 kexec 重启了）——清晰地标志着"阶段 1 的全部准备工作已经就绪，
接下来是操作员的回合"这一交接时刻。把这一整套机制串起来看，
"双重 fork + 脱离终端 + 持久睡眠"其实正是大量真实世界守护进程
（如 `sshd`、各种系统服务）启动自身的标准范式——selftest 在这里
"借用"了这套成熟的系统编程技巧，来解决"一个本应短暂运行的测试程序，
如何变身为一个长期持有内核资源的守护进程"这一独特需求。

### 10.4 `luo_kexec_simple.c`：最简单的端到端时序完整走读

有了上述基础设施，`luo_kexec_simple.c`（90 行）本身的逻辑就显得
非常清爽——它把全部精力集中在"验证一个 session、一个 memfd 能否
完整地穿越 kexec"这一个核心场景上。我们把它的两个阶段串成一张完整
的端到端时序图，并标注每一步对应内核源码中的哪个函数（这是本节
最重要的产出——它是把本文档前 9 章讨论过的全部内核机制，与一次
真实的端到端调用序列严丝合缝地对应起来的"全景沙盘"）：

```
═══════════════════════ 阶段 1（旧内核，run_stage_1） ═══════════════════════

 用户态调用                                    内核侧对应实现
 ──────────────────────────────────────────────────────────────────────
 luo_open_device()
   open("/dev/liveupdate")           ──►  luo_open() [luo_core.c]
                                            atomic_cmpxchg 独占检查（3.4 节）

 create_state_file(luo_fd,
   "kexec_simple_state", 999, 2)
   ├─ luo_create_session()
   │    ioctl(CREATE_SESSION)          ──► luo_session_create_ioctl()
   │                                        → luo_session_alloc/insert（5.2/5.3 节）
   │                                        → anon_inode_getfile（5.4 节）
   └─ create_and_preserve_memfd()
        ├─ memfd_create()                  （glibc 包装的 memfd_create 系统调用）
        ├─ ftruncate / mmap / write         （准备 1 页数据 "2"）
        └─ ioctl(SESSION_PRESERVE_FD)   ──► luo_preserve_file_ioctl()
                                             → luo_preserve_file()（6.4 节七层链）
                                               ├─ liveupdate_get_file_handler()
                                               │    遍历 handler，调 can_preserve()
                                               │    → memfd_luo_can_preserve（9.2 节）
                                               ├─ luo_flb_file_preserve()（7.4 节，
                                               │    本例 memfd 无 FLB 依赖，空操作）
                                               └─ ops->preserve()
                                                    → memfd_luo_preserve()（9.3 节）
                                                      → memfd_luo_preserve_folios
                                                        （9.4 节：pin+kho_preserve_folio
                                                         +标记 dirty/uptodate）

 luo_create_session(luo_fd, "test-session")
   ioctl(CREATE_SESSION)               ──►  同上

 create_and_preserve_memfd(session_fd,
   0x1A, "hello kexec world")
   ioctl(SESSION_PRESERVE_FD)          ──►  同上（保存第二个 memfd）

 close(luo_fd)
 daemonize_and_wait()                       fork + setsid，子进程持续持有
                                             session_fd / state_session_fd /
                                             两个 memfd —— 保持它们 "活跃"
                                            （此刻两个 session 仍是
                                              "本地"状态，retrieved=false）

  ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ 操作员手动执行 do_kexec.sh ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  liveupdate_reboot() 在 kernel_kexec() 之前被调用（3.5 节）：
    luo_session_serialize() —— 把两个 session 写入 FDT/裸内存（5.7 节）
    luo_file_serialize()    —— 把已 freeze 的文件写入 FDT/裸内存（6.x 节）
    luo_flb_serialize()     —— 序列化非空 FLB（7.8.3 节，本例无 FLB）
    kho_finalize() → kexec_exec() —— 物理内存原样穿越重启
  ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

═══════════════════════ 阶段 2（新内核，run_stage_2） ═══════════════════════

 用户态调用                                    内核侧对应实现
 ──────────────────────────────────────────────────────────────────────
 (luo_test 内部)
 luo_retrieve_session(luo_fd,
   "kexec_simple_state")
   ioctl(RETRIEVE_SESSION)             ──►  luo_session_retrieve_ioctl()
                                             → 在 incoming 链表中按 name 查找
                                               （5.4 节）→ retrieved = true

 restore_and_read_stage(...)
   restore_and_verify_memfd()
     ioctl(SESSION_RETRIEVE_FD)        ──►  luo_retrieve_file_ioctl()
                                             → luo_retrieve_file()（6.7 节
                                               三态缓存）
                                               → ops->retrieve()
                                                 → memfd_luo_retrieve（9.7 节：
                                                   重建 file + page cache）
   atoi(buf) == 2  ✓ 阶段标记正确

 luo_retrieve_session(luo_fd, "test-session")
   ioctl(RETRIEVE_SESSION)             ──►  同上

 restore_and_verify_memfd(session_fd,
   0x1A, "hello kexec world")
   ioctl(SESSION_RETRIEVE_FD)          ──►  同上（取回业务 memfd 并核对内容）

 luo_session_finish(session_fd)
   ioctl(SESSION_FINISH)               ──►  luo_session_finish_ioctl()
                                             → luo_file_finish()（6.8 节
                                               两阶段提交）
                                               → ops->finish()
                                                 → memfd_luo_finish（9.8 节）
                                             → luo_flb_file_finish()（7.5 节）
                                             → 释放 session 自身

 luo_session_finish(state_session_fd)        同上（收尾状态 session）
```

这张图把全文档的核心机制——从 ioctl 接口、session 子系统、文件
生命周期状态机、FLB 引用计数，到 memfd 的 folio 保存恢复细节——
**串成了一条单一、连贯、可以被实际运行验证的执行路径**。如果说
前面九章是在"拆解一台精密机器的每一个零件"，这张图就是"看着这台
机器作为一个整体真正运转起来"——这正是阅读复杂系统源码时
"自顶向下分解、再自底向上验证"这一完整闭环里不可或缺的最后一步。

代码本身的两个阶段函数 `run_stage_1`/`run_stage_2`（`luo_kexec_simple.c:22-83`）
读起来几乎像是上面这张图的逐字翻译——这正是测试代码应有的样子：
**测试代码读起来应该像是"系统应该如何被使用"的范例文档**，而不是
一堆晦涩的断言堆砌。一个新加入项目的开发者，通过阅读 `run_stage_1`/
`run_stage_2` 这 60 行代码，几乎可以不看任何其他文档就掌握 LUO
最基本的使用范式——这是测试代码所能达到的最高境界之一。

### 10.5 `luo_multi_session.c`：拓扑复杂度的扩展验证

`luo_multi_session.c`（163 行）在 `luo_kexec_simple.c` 建立的基础
模式之上，把验证范围扩展到了一个更接近真实场景的拓扑——4 个
session（2 个空、2 个非空）、3 个 memfd（分布在不同 session 中，
其中一个 session 装了 2 个）。它的 `run_stage_1`/`run_stage_2`
（`luo_multi_session.c:31-156`）在结构上与 `luo_kexec_simple.c`
完全同构，只是把"创建/保存/取回/校验/finish"这一组动作在更多对象
上重复执行。我们不再逐行重复时序图（结构与 10.4 节完全一致），
而是聚焦于这个用例**新增的验证维度**——它们恰好对应着前面章节里
强调过的几个重要不变式：

#### 10.5.1 空 session 的完整生命周期

```c
s_empty1_fd = luo_create_session(luo_fd, SESSION_EMPTY_1);
...
s_empty2_fd = luo_create_session(luo_fd, SESSION_EMPTY_2);
```

这两个 session 自始至终没有任何文件被保存进去。回顾第 5 章 5.7 节
`luo_session_serialize()` 的过滤逻辑——"只序列化非空 session"
（与第 7 章 7.8.3 节"只序列化非空 FLB"是同一条设计公理的不同
应用）。这个用例确保了"空 session 即便不被序列化进 FDT 数据块，
依然能够在新内核里被正确地 `retrieve` 找到、并能够被正常
`finish`"——这要求新内核侧的反序列化逻辑必须能够正确处理
"某个曾经存在的 session，其名字虽然出现在某处元数据中，但它的
文件列表是空的"这一情形，而不是想当然地认为"被反序列化出来的
session 必然带有非空的文件列表"。

#### 10.5.2 一个 session 中的多文件保存与精确取回

```c
s_files2_fd = luo_create_session(luo_fd, SESSION_FILES_2);
...
create_and_preserve_memfd(s_files2_fd, MFD2_TOKEN, MFD2_DATA);
create_and_preserve_memfd(s_files2_fd, MFD3_TOKEN, MFD3_DATA);
...
/* 阶段 2 */
mfd2 = restore_and_verify_memfd(s_files2_fd, MFD2_TOKEN, MFD2_DATA);
mfd3 = restore_and_verify_memfd(s_files2_fd, MFD3_TOKEN, MFD3_DATA);
```

这一组调用验证的是第 6 章 6.7 节讨论过的、"按 token 在
`luo_file_set` 中精确查找"这一核心机制——必须确保
"用 `MFD2_TOKEN` 取回的文件，其内容精确对应 `MFD2_DATA`，
而不会与同一个 session 里 `MFD3_TOKEN` 对应的 `MFD3_DATA`
相互混淆"。`restore_and_verify_memfd` 内部对内容做 `strcmp`
比对（回顾 `luo_test_utils.c:109`），这是确保"按图索骥"机制
不会因为索引错位、数组下标计算失误等 bug 而发生数据串扰的
直接证据。

#### 10.5.3 全量 session finish 的收尾验证

```c
if (luo_session_finish(s_empty1_fd) < 0) ...
if (luo_session_finish(s_empty2_fd) < 0) ...
if (luo_session_finish(s_files1_fd) < 0) ...
if (luo_session_finish(s_files2_fd) < 0) ...
if (luo_session_finish(state_session_fd) < 0) ...
```

测试用例特意在末尾把全部 5 个 session（4 个业务 session + 1 个
状态 session）逐一 finish——这验证了第 6 章 6.8 节
`luo_file_finish()` 的"两阶段提交"模式在**多个独立 session 并存**
的场景下依然正确：每个 session 的 finish 操作必须是相互独立的
（一个 session 的 finish 失败不应该影响其他 session），且全部
完成之后，系统应当干净利落地回归到"无任何残留 LUO 状态"的初始局面——
这是对整个 LUO 状态机"最终收敛性"的端到端验证：无论中间过程多么
复杂、涉及多少个相互独立的对象，最终都必须能够走到一个干净、
确定、无歧义的终态。

### 10.6 从 selftest 反观全局：一张"用户态调用 → 内核函数"完整映射表

读完两个端到端用例，不妨把全篇文档涉及到的"用户态可观测动作"与
"内核侧实现函数"之间的映射关系，汇总成一张速查表——它既是本章的
总结，也可以被当作前 9 章内容的索引导航：

| 用户态动作（selftest 中的调用） | 触发的 ioctl | 核心内核函数 | 详见章节 |
|---|---|---|---|
| `open("/dev/liveupdate")` | （设备打开） | `luo_open()` + `atomic_cmpxchg` 独占检查 | 3.4 |
| `luo_create_session()` | `LIVEUPDATE_IOCTL_CREATE_SESSION` | `luo_session_create_ioctl → luo_session_alloc/insert` | 5.2/5.3/5.4 |
| `luo_retrieve_session()` | `LIVEUPDATE_IOCTL_RETRIEVE_SESSION` | `luo_session_retrieve_ioctl`（incoming 链表查找+`retrieved=true`） | 5.4/5.5 |
| `create_and_preserve_memfd()` | `LIVEUPDATE_SESSION_PRESERVE_FD` | `luo_preserve_file_ioctl → luo_preserve_file`（七层链）→ `memfd_luo_preserve` | 6.4 / 9.3-9.4 |
| `restore_and_verify_memfd()` | `LIVEUPDATE_SESSION_RETRIEVE_FD` | `luo_retrieve_file_ioctl → luo_retrieve_file`（三态缓存）→ `memfd_luo_retrieve` | 6.7 / 9.7 |
| `luo_session_finish()` | `LIVEUPDATE_SESSION_FINISH` | `luo_session_finish_ioctl → luo_file_finish`（两阶段提交）→ `memfd_luo_finish` + `luo_flb_file_finish` | 6.8 / 7.5 / 9.8 |
| `daemonize_and_wait()` 之后 / 关闭 luo_fd | （进程退出 / fd 关闭） | `luo_release` / `luo_session_release`（`retrieved` 驱动的两条岔路） | 5.5 |
| `do_kexec.sh`（操作员触发） | （非 ioctl，shell 命令） | `liveupdate_reboot()`：`luo_session_serialize` + `luo_file_serialize` + `luo_flb_serialize` → `kho_finalize` → `kernel_kexec` | 3.5 / 5.7 / 6.x / 7.8 |

这张表格把"用户能看到/能操作的东西"和"内核里真正执行的代码"
两个世界用一根根线连接起来——这正是阅读、理解任何一个有完善用户
接口的内核子系统时最有效的认知框架："**先弄清楚外部世界能做什么、
会观察到什么，再逐步深入到能够解释'为什么会观察到这个结果'的
内部实现**"。selftest 代码正是这两个世界之间最自然、最贴近真实
使用场景的一座桥梁——它既是验证代码正确性的工具，也是学习一个
复杂子系统最高效的"活文档"。

### 10.7 小结

本章我们换了一个观察角度——不再钻研"内核怎么实现"，而是追问
"用户怎么使用、使用之后系统应该呈现出什么样的可观测行为"。
通过逐行解读 `liveupdate.c` 的 9 个功能性用例和
`luo_kexec_simple.c`/`luo_multi_session.c` 这两个端到端用例，
我们看到了：

* **测试代码即设计意图的具象化**——每一个 `ASSERT_EQ`/`EXPECT_EQ`
  断言，背后都对应着前面章节里讨论过的某一条具体的设计决策或
  不变式（独占访问、命名唯一性、防重复保存、按 token 精确索引、
  状态机终态收敛……）；
* **跨 kexec 测试是一个独特的工程挑战**——它要求测试框架本身
  具备"自我识别阶段"、"状态接力"、"金蝉脱壳式持久化"等一系列
  特殊能力，而 `luo_test_utils.c` 给出的解法——**复用被测系统
  自身的核心机制来搭建测试脚手架**——本身就是一种值得学习的
  巧思；
* **selftest 是连接抽象设计与具体行为的最佳桥梁**——它把前面
  9 章里画过的所有状态机图、调用链图，编织成了一条真实、连贯、
  可以被实际跑通的执行路径，是检验"我们是否真正读懂了"的
  试金石。

下一章，我们将把视角从"正常路径如何工作"切换到"出现问题时系统
如何应对"——系统性地梳理 LUO 全局的错误处理与异常恢复设计哲学，
把散落在第 5、6、7、9 各章中的局部观察，汇聚成一幅完整的全景图。

---

## 第 11 章：错误处理与异常路径全景梳理

> 本章的视角与前面十章都不同——前面十章问的是"系统在正常路径上
> 如何工作"，本章问的是"当某一步偏离了正常路径，系统选择如何
> 应对"。这个问题之所以值得用单独一章来回答，是因为 LUO 的错误
> 处理并不是"在每个函数里见招拆招"的零散补丁集合，而是一套**贯穿
> 始终、自洽统一的分类决策体系**——同一段简短的注释文字，会一字
> 不差地出现在 `luo_session_deserialize()`（`kernel/liveupdate/luo_session.c:535-543`）
> 和 `luo_file_deserialize()`（`kernel/liveupdate/luo_file.c:801-809`）两个
> 相距数百行、分属不同子系统的函数里——这种"重复"不是疏忽，而是
> 作者在向读者宣告："这不是一个局部的权衡，这是一条贯穿全局的
> 设计公理。"
>
> 我们将看到，LUO 把可能出现的失败场景划分成了三种性质迥异的
> 范式，并为每一种范式匹配了截然不同的应对策略：
>
> 1. **可回滚的"冻结期"失败**——系统仍在旧内核中运行，资源完整，
>    "回到出发点"在技术上可行也在经济上划算，于是 LUO 不惜代价
>    地实现了精确的镜像回滚链；
> 2. **两阶段提交式的"完成期"失败**——操作涉及多个独立的
>    handler，"全有或全无"的原子性不能靠运气，于是 LUO 把
>    "决策"和"执行"显式拆成两个阶段，先全员表态，达成一致后
>    再批量落地；
> 3. **"不可逆"的反序列化失败**——系统已经身处新内核，部分状态
>    已经物理性地揉进了正在运行的内核数据结构里，"回滚"本身就是
>    一个伪命题，于是 LUO 干脆放弃了"安全 undo"的幻想，转而采用
>    "宁可泄漏、不做不安全的回退，把皮球踢给可靠的 reboot"这一
>    更诚实也更稳妥的策略。
>
> 这三种范式不是随意选择的结果，而是**对"失败发生时系统所处的
> 物理状态"的精确建模**——什么仍然完好、什么已经无法挽回、什么
> 牵涉到外部不可控的硬件/模块行为，决定了"回滚"这件事在每个阶段
> 究竟是"举手之劳"还是"以小博大"还是"与虎谋皮"。读完本章，你
> 会发现这恰恰是贯穿本文档第 5（Session）、第 6（File）、第 7
> （FLB）、第 9（memfd）各章局部观察的那条隐藏主线。

### 11.1 总览：三种错误处理范式与系统所处的物理阶段

在深入代码细节之前，我们先把"错误可能发生在哪个阶段"这件事
讲清楚，因为 LUO 选择哪种应对范式，本质上是由**失败发生时系统
所处的物理阶段**决定的，而不是由"这段代码看起来应该怎么写"的
直觉决定的。

```
                 旧内核运行中                    │            新内核启动后
  ─────────────────────────────────────────────┼──────────────────────────────────
                                                │
   用户态 ioctl                                  │
   preserve_fd / create_session                  │
        │                                        │
        ▼                                        │
   ┌─────────────────┐                           │
   │ 范式 0：前置校验  │ ← 失败 = 直接拒绝该次     │
   │ (can_preserve/   │   ioctl，对系统无副作用    │
   │  容量/重名检查)   │   （第 5、6 章已详述）     │
   └────────┬────────┘                           │
            │ 成功，资源进入"待保存"状态           │
            ▼                                    │
       reboot(LINUX_REBOOT_CMD_KEXEC)            │
            │                                    │
            ▼                                    │
   ┌─────────────────┐                           │
   │ 范式 1：冻结期    │ ← 系统仍在旧内核里完整     │
   │ 可回滚失败        │   运行，"回到出发点"代价   │
   │ (freeze 失败)    │   小、收益大 → 镜像回滚链   │
   └────────┬────────┘                           │
            │ 全部冻结成功                         │
            ▼                                    │
      kho_finalize + kexec 跳转 ═══════════════════╬══► 旧内核状态终结
                                                │         新内核启动
                                                │              │
                                                │              ▼
                                                │   ┌─────────────────┐
                                                │   │ 范式 3：反序列化  │
                                                │   │ 失败             │ ← 部分状态已经
                                                │   │ "宁可泄漏，不做   │   揉进运行中的
                                                │   │  不安全 undo"    │   内核数据结构，
                                                │   └────────┬────────┘   回滚是伪命题
                                                │            │ 成功的部分继续可用
                                                │            ▼
                                                │   用户态 retrieve_fd / finish
                                                │            │
                                                │            ▼
                                                │   ┌─────────────────┐
                                                │   │ 范式 2：完成期    │ ← 多 handler 涉及，
                                                │   │ 两阶段提交式      │   需要"全体一致
                                                │   │ (can_finish→     │   同意"才能保证
                                                │   │  finish 拆分)    │   原子性
                                                │   └─────────────────┘
```

这张图揭示了一个朴素却深刻的事实：**错误处理策略本质上是对"撤销
成本"的会计核算**。

* 在范式 0（前置校验）阶段，操作还没有产生任何持久副作用——
  `xa_insert` 还没调用、`luo_flb_file_preserve` 还没增加引用计数、
  `kzalloc_obj` 分配的 `struct luo_file` 还没挂到链表上——所以"拒绝"
  这个动作的成本几乎是零，直接返回错误码即可。这正是第 5、6 章
  里讨论过的 `luo_file_preserve_fd()` 那条"七层回滚链"的起点：
  越早失败，需要回滚的层数越少，这是一种主动的、设计层面的
  "提前止损"。

* 在范式 1（冻结期）阶段，撤销成本依然很低——旧内核完整运行，
  所有数据结构都还在内存里、所有设备都还能正常响应——但这一次
  "回滚"不再是"放弃一次 ioctl 请求"那么轻量，而是"让已经声明
  '准备好迎接重启'的多个独立子系统 collectively 改变主意"。这就
  需要一条**精确到字节级别的镜像回滚链**：谁先冻结的，谁后撤销；
  撤销到哪个点为止，由一个"哨兵"参数精确标记。

* 在范式 2（完成期）阶段，操作本身的"原子性"诉求和"涉及多个
  互不知情的 handler"这一现实产生了张力——某个 handler 可能因为
  自己内部还有未完成的工作而暂时无法 `finish`，但在它表态之前，
  其它 handler 已经把自己的资源释放掉了怎么办？LUO 的解法是把
  "做决定"和"执行"显式拆成两轮循环：第一轮只问"你准备好了吗"，
  全员说"是"之后，第二轮才真正动手清理。这正是数据库领域里
  "两阶段提交"思想在内核子系统里的朴素复刻。

* 在范式 3（反序列化）阶段，"撤销"已经从"代价略高的工程问题"
  彻底升级为"在哲学上不成立的伪命题"——旧内核已经不复存在，
  新内核里部分 session、部分文件已经被插入到全局链表、注册到
  xarray、与已经 `try_module_get` 的模块产生了纠缠，要把这一切
  干净地"退回"到"好像什么都没发生过"的状态，唯一可靠的办法就是
  ……重新走一遍 `liveupdate_reboot()`，而那恰恰需要回到旧内核——
  矛盾。于是 LUO 选择了一条更诚实的路：不装作能够安全回滚，而是
  把系统标记为"已损坏"，把恢复的责任交给一个**真正可靠**的兜底
  机制——重启。

接下来三节，我们逐一深入这三种范式的具体实现。

### 11.2 范式一：冻结期可回滚失败——freeze/unfreeze 镜像链

冻结阶段的核心矛盾是：**`luo_file_freeze()` 需要让 `file_set` 里
的每一个文件依次进入"已冻结"状态，但任何一个 handler 的
`.freeze()` 回调都可能失败**（比如设备当前正在执行一个无法被
中断的 DMA 传输，handler 判断"现在不是冻结的好时机"而返回
`-EBUSY`）。一旦发生这种情况，已经成功冻结的那些文件绝不能被
留在"冻结"状态——因为 `reboot()` 系统调用即将因为这个错误而
失败返回，旧内核将继续运行，所有已冻结的设备都需要"解冻"才能
继续正常服务。

#### 11.2.1 三个层次的函数：单点操作、批量编排、镜像回滚

`kernel/liveupdate/luo_file.c` 围绕"冻结"这一件事，精心设计了
三个不同粒度的函数，它们之间的关系正是理解这条回滚链的钥匙：

```c
/* 单点操作：冻结/解冻"一个" luo_file */
static int luo_file_freeze_one(struct luo_file_set *file_set,
                               struct luo_file *luo_file);                /* :403 */
static void luo_file_unfreeze_one(struct luo_file_set *file_set,
                                  struct luo_file *luo_file);              /* :426 */

/* 镜像回滚的核心：可以"冻结到某一点"，也可以"全部解冻" */
static void __luo_file_unfreeze(struct luo_file_set *file_set,
                                struct luo_file *failed_entry);            /* :443 */

/* 批量编排：对外暴露的两个入口 */
int luo_file_freeze(struct luo_file_set *file_set,
                    struct luo_file_set_ser *file_set_ser);                /* :492 */
void luo_file_unfreeze(struct luo_file_set *file_set,
                       struct luo_file_set_ser *file_set_ser);             /* :551 */
```

`luo_file_freeze_one()`/`luo_file_unfreeze_one()`（`luo_file.c:403-424`、
`:426-441`）是镜像对称的最小操作单元——前者在持有 `luo_file->mutex`
的前提下调用 handler 的 `.freeze()` 回调并在成功时回写
`serialized_data`；后者用完全相同的方式调用 `.unfreeze()`，但不
关心返回值（解冻被假定不会失败——这本身也是一种设计上的简化：
如果连"恢复到运行状态"都可能失败，那么整个回滚体系将无从谈起）。

```c
static int luo_file_freeze_one(struct luo_file_set *file_set,
                               struct luo_file *luo_file)
{
        int err = 0;

        guard(mutex)(&luo_file->mutex);

        if (luo_file->fh->ops->freeze) {
                struct liveupdate_file_op_args args = {0};

                args.handler = luo_file->fh;
                args.file = luo_file->file;
                args.serialized_data = luo_file->serialized_data;
                args.private_data = luo_file->private_data;

                err = luo_file->fh->ops->freeze(&args);
                if (!err)
                        luo_file->serialized_data = args.serialized_data;
        }

        return err;
}
```

注意这里一个容易被忽略的细节：`.freeze` 回调是**可选**的
（`if (luo_file->fh->ops->freeze)`）——并不是每一种文件类型都
需要在冻结阶段做额外的工作（例如对 memfd 而言，第 9 章讲过它
唯一需要在 `memfd_luo_freeze()` 中重新捕获的只有 `f_pos`，详见
`mm/memfd_luo.c` 中的 `memfd_luo_freeze`）。这种"按需注册回调、
未注册则跳过"的模式我们在第 6 章讨论 `liveupdate_file_ops` 回调
矩阵时已经见过，此处不再展开，但值得注意它与错误处理的关系：
**可选回调天然地缩小了"可能失败的范围"**——没有提供 `.freeze`
的 handler 根本不可能在这一步失败，这本身就是一种"减少出错面"
的设计取舍。

#### 11.2.2 `__luo_file_unfreeze`：用一个"哨兵"参数统一两种回滚需求

最有意思的是 `__luo_file_unfreeze()`（`luo_file.c:443-457`）这个
辅助函数。它需要同时服务于两种看起来不同、实则同构的场景：

* **场景 A（部分回滚）**：`luo_file_freeze()` 在冻结到第 *i* 个
  文件时失败，需要把第 *0* 到 *i-1* 个已经成功冻结的文件解冻；
* **场景 B（全部回滚）**：上层调用方（`luo_session_serialize()`）
  发现"这个 session 冻结成功了，但是同一批次里的另一个 session
  冻结失败了"，因此需要把这个本已成功的 session 整个撤销，包括
  它内部的全部文件。

这两种场景的唯一区别，就是"解冻应该在哪个文件停止"。LUO 没有
为它们写两份几乎相同的循环代码，而是引入了一个"哨兵"参数
`failed_entry`：

```c
static void __luo_file_unfreeze(struct luo_file_set *file_set,
                                struct luo_file *failed_entry)
{
        struct list_head *files_list = &file_set->files_list;
        struct luo_file *luo_file;

        list_for_each_entry(luo_file, files_list, list) {
                if (luo_file == failed_entry)
                        break;

                luo_file_unfreeze_one(file_set, luo_file);
        }

        memset(file_set->files, 0, LUO_FILE_PGCNT << PAGE_SHIFT);
}
```

* 当 `failed_entry` 指向"冻结失败的那个文件"时，循环在到达它
  之前就 `break`，于是只解冻了"它之前的那些已冻结文件"——这正是
  场景 A 所需要的精确截断；
* 当 `failed_entry == NULL` 时，循环条件 `luo_file == failed_entry`
  永远不成立（链表中不可能存在 `NULL` 节点），于是循环遍历到底，
  解冻**全部**文件——这正是场景 B 所需要的全量回滚。

`NULL` 在这里被用作"匹配不到任何真实节点的通配哨兵"，是 C 语言
里一个简洁却容易被忽视的技巧：**用同一个比较表达式同时表达
"在某个特定点截断"和"永不截断（即遍历到底）"两种语义**，从而
让一个函数体同时服务两种调用意图，避免了重复代码，也避免了
"两份几乎相同但又不完全相同的代码总有一天会发生分裂"这一长期
维护风险。

最后那行 `memset(file_set->files, 0, LUO_FILE_PGCNT << PAGE_SHIFT)`
也值得注意——它把"本应交给下一个内核"的序列化缓冲区清零。这是
一种防御性的"清场"动作：既然这次冻结被放弃了，缓冲区里那些
**部分写入**的、不完整的 `struct luo_file_ser` 条目就绝不能被
误用（比如被后续的重试流程当作"已经序列化好的数据"处理），
清零是消除这种隐患最简单也最彻底的方式。

#### 11.2.3 `luo_file_freeze`：goto 单出口与"哨兵 = 失败节点"的精确对应

`luo_file_freeze()`（`luo_file.c:492-534`）把上述两个辅助函数
组装成一条完整的、`goto` 单出口风格的回滚链：

```c
int luo_file_freeze(struct luo_file_set *file_set,
                    struct luo_file_set_ser *file_set_ser)
{
        struct luo_file_ser *file_ser = file_set->files;
        struct luo_file *luo_file;
        int err;
        int i;
        ...
        i = 0;
        list_for_each_entry(luo_file, &file_set->files_list, list) {
                err = luo_file_freeze_one(file_set, luo_file);
                if (err < 0) {
                        pr_warn("Freeze failed for token[%#0llx] handler[%s] err[%pe]\n",
                                luo_file->token, luo_file->fh->compatible,
                                ERR_PTR(err));
                        goto err_unfreeze;
                }

                strscpy(file_ser[i].compatible, luo_file->fh->compatible,
                        sizeof(file_ser[i].compatible));
                file_ser[i].data = luo_file->serialized_data;
                file_ser[i].token = luo_file->token;
                i++;
        }
        ...
        return 0;

err_unfreeze:
        __luo_file_unfreeze(file_set, luo_file);

        return err;
}
```

这里有一个非常精巧的细节：`goto err_unfreeze` 跳转之后，
`list_for_each_entry` 循环变量 `luo_file` **仍然指向当前正在
处理、且刚刚冻结失败的那个节点**——C 语言的 `for`/`while` 循环
变量在 `goto` 跳出循环体后依然保持其最后的值，这是一个语言层面
的基本事实，但在这里被有意识地利用了起来：**循环变量本身就是
现成的"失败哨兵"**，不需要额外声明一个变量来记录"失败发生在
哪里"。`__luo_file_unfreeze(file_set, luo_file)` 一行代码，就把
"从链表头开始解冻，直到遇见刚刚失败的这个节点为止"表达得既精确
又简洁——这正是上一节"哨兵参数"设计在调用点上结出的果实。

同时注意 `pr_warn()` 这一行——它把"哪个 token、哪个 handler、
什么错误码"完整记录到内核日志。这不是装饰性的调试信息，而是
LUO 错误处理哲学里"失败诚实化"的一部分：用户态的运维工具不一定
能拿到底层 handler 返回的精确错误码（`reboot()` 系统调用的返回值
往往会被进一步归并），但内核日志会留下一份**未经裁剪的第一手
记录**，这对事后定位"到底是哪个设备拒绝了冻结"至关重要。

最后，`luo_file_unfreeze()`（`luo_file.c:551-558`）作为公开的
"全量解冻"入口，本质上就是 `__luo_file_unfreeze(file_set, NULL)`
的薄包装——这再次印证了上一节"`NULL` 哨兵 = 全量回滚"这一设计
意图并非巧合，而是从一开始就被规划好的"一鱼两吃"。

#### 11.2.4 上溯一层：`luo_session_serialize` 的镜像式 `list_for_each_entry_continue_reverse`

`luo_file_freeze`/`luo_file_unfreeze` 解决的是"一个 file_set 内部
多个文件之间"的回滚问题。但 LUO 的 outgoing 链表上往往同时存在
**多个 session**，如果第 3 个 session 冻结失败，前 2 个已经冻结
成功的 session 该怎么办？答案在 `luo_session_serialize()`
（`luo_session.c:584-611`）里，它复刻了与 `luo_file_freeze` 完全
同构的"先尝试、失败则镜像回滚"模式，但这一次回滚的对象是
"session"这个更大的单元：

```c
int luo_session_serialize(void)
{
        struct luo_session_header *sh = &luo_session_global.outgoing;
        struct luo_session *session;
        int i = 0;
        int err;

        guard(rwsem_write)(&sh->rwsem);
        list_for_each_entry(session, &sh->list, list) {
                err = luo_session_freeze_one(session, &sh->ser[i]);
                if (err)
                        goto err_undo;

                strscpy(sh->ser[i].name, session->name,
                        sizeof(sh->ser[i].name));
                i++;
        }
        sh->header_ser->count = sh->count;

        return 0;

err_undo:
        list_for_each_entry_continue_reverse(session, &sh->list, list) {
                i--;
                luo_session_unfreeze_one(session, &sh->ser[i]);
                memset(sh->ser[i].name, 0, sizeof(sh->ser[i].name));
        }

        return err;
}
```

这段代码里有一个值得反复品味的设计选择：失败发生时，**当前
正在处理、刚刚冻结失败的那个 session 本身并不需要被解冻**——
因为 `luo_session_freeze_one()` 内部调用的 `luo_file_freeze()`
在自己失败时已经通过 `__luo_file_unfreeze` 做完了自我清理（见
11.2.3 节）。所以 `luo_session_serialize` 只需要处理"在它之前
已经成功冻结的那些 session"，而 `list_for_each_entry_continue_reverse`
宏恰好提供了这种语义——**从当前迭代位置开始、反向遍历到链表头**，
不多不少，正好覆盖"已成功冻结、需要撤销"的那个集合，把"当前
失败节点"自然地排除在外。

这种"上一层只负责回滚比自己更早成功的兄弟节点，下一层对自己
内部的失败已经自洽地清理干净"的分层职责划分，正是第 6 章里
"七层回滚链"思想在更高抽象层级上的自然延伸：**每一层只对自己
负责的资源边界负责，绝不越界代劳，也绝不依赖上层来擦屁股**。
这样一来，无论失败发生在哪一层、哪一个节点，整条回滚链条总能
首尾衔接、不重不漏。

最后两行 `memset(sh->ser[i].name, ...)` 与 `i--` 的配合，则与
`__luo_file_unfreeze` 末尾的 `memset` 异曲同工——把"本不该存在"
的部分序列化痕迹清理干净，确保即便这次 `liveupdate_reboot()`
失败后用户态选择重试，旧的、不完整的中间状态也不会污染下一次
尝试。

#### 11.2.5 顶层中止：`liveupdate_reboot` 的"快速失败"哲学

最顶层的协调者 `liveupdate_reboot()`（`kernel/liveupdate/luo_core.c:227-242`）
把这条回滚链的最后一环锁死：

```c
int liveupdate_reboot(void)
{
        int err;

        if (!liveupdate_enabled())
                return 0;

        err = luo_session_serialize();
        if (err)
                return err;

        luo_flb_serialize();

        return 0;
}
```

这个函数的注释直白地写道："If any callback fails, this function
aborts KHO, undoes the freeze() callbacks, and returns an error."
——代码本身则把这句话落实成了最朴素的形式：`luo_session_serialize()`
一旦失败就立即 `return err`，**根本不会继续调用 `luo_flb_serialize()`**。
这种"任何一环失败，立刻停止后续动作"的顺序依赖关系，恰恰是
冻结阶段回滚链能够保持"镜像对称"的前提——如果 session 序列化
已经失败、相关文件已经被解冻，FLB 的引用计数状态机（第 7 章）
绝不能继续往前推进，否则就会出现"文件已经回到运行状态，但 FLB
却以为它进入了 outgoing 状态"这种逻辑错位。

至此，"冻结期可回滚失败"这条主线从最底层的单个 handler 回调，
一路向上贯穿 file_set、session、再到顶层的 `liveupdate_reboot`，
形成了一条层层嵌套、互不越界、首尾自洽的镜像回滚链——这正是
"系统仍然完好、撤销代价低"这一物理现实，在代码结构上投下的
精确倒影。

### 11.3 范式二：完成期的两阶段提交——`can_finish`/`finish` 的拆分

把视角切换到新内核启动之后。当用户态决定"我已经把需要的文件都
取回来了，可以释放剩余的预留状态了"，它会调用 `LIVEUPDATE_SESSION_FINISH`
ioctl，最终落到 `luo_file_finish()`（`luo_file.c:715-752`）。这里
面临的核心矛盾与冻结阶段截然不同：**这一步操作本身被文档明确
要求是"原子的"**（"This operation is atomic. If any handler's
.can_finish() op fails, the entire function aborts immediately and
returns an error."——`luo_file.c:733-734`），但它涉及的是**多个
互不知情、各自管理着自己内部状态的 handler**，没有任何一个
handler 天生知道"其它 handler 是否也准备好了"。

#### 11.3.1 矛盾的根源：`finish` 本身不可逆，不能"先做后悔"

设想一种朴素的实现：直接遍历 `files_list`，对每个文件依次调用
`.finish()`。如果遍历到第 5 个文件时，它的 handler 说"我现在
还有一个未完成的异步 DMA，不能现在 `finish`，请稍后重试"，那么
前 4 个已经 `finish` 掉的文件该怎么办？

`finish` 回调的语义（详见第 6 章对 `liveupdate_file_ops` 回调
矩阵的讨论）通常意味着"彻底释放预留期间占用的资源、归还设备的
正常控制权、丢弃序列化数据"——这是一类**不可逆**的操作。一旦
"撤销 finish"被提上日程，我们又会掉进与范式三同样的陷阱：
"unfinish"在工程上几乎不可实现，在语义上也根本不成立。

LUO 的解法是釜底抽薪——**让"询问是否可以 finish"和"真正执行
finish"成为两个完全独立、互不交织的阶段**，只有在第一阶段
**全员一致同意**之后，第二阶段才会开始动手，从而把"操作本身
不可逆"这一事实，转化成"只要不动手，就还有回旋余地"这一更友好
的局面。

#### 11.3.2 第一阶段：`can_finish` —— 只问意见，不做改动

```c
static int luo_file_can_finish_one(struct luo_file_set *file_set,
                                   struct luo_file *luo_file)
{
        bool can_finish = true;

        guard(mutex)(&luo_file->mutex);

        if (luo_file->fh->ops->can_finish) {
                struct liveupdate_file_op_args args = {0};

                args.handler = luo_file->fh;
                args.file = luo_file->file;
                args.serialized_data = luo_file->serialized_data;
                args.retrieve_status = luo_file->retrieve_status;
                can_finish = luo_file->fh->ops->can_finish(&args);
        }

        return can_finish ? 0 : -EBUSY;
}
```

几个细节值得留意：

* `.can_finish` 回调同样是**可选**的——如果 handler 没有提供，
  默认值 `can_finish = true` 意味着"我没有特殊顾虑，随时可以
  finish"。这与上一节 `.freeze` 回调的"可选 = 缩小出错面"如出
  一辙：能不实现的回调就不强制实现，既减轻了 handler 作者的
  负担，也减少了潜在的失败点；
* 回调函数的签名是 `bool (*can_finish)(...)`——一个**纯粹的
  布尔判断**，没有任何修改状态的副作用语义。这种"类型即文档"的
  设计，从函数签名层面就明确昭示了"这一步只能看，不能动"，
  迫使 handler 作者也只能在这个边界内实现自己的逻辑；
* 失败时统一映射为 `-EBUSY`——这个错误码本身就在传达"现在不
  方便，请稍后重试"的语义，与 `EAGAIN`/`EBUSY` 在 POSIX 世界里
  约定俗成的含义相吻合，让用户态可以据此实现退避重试逻辑，而
  不必去猜测"这是一个永久性故障还是暂时性的"。

`luo_file_finish()` 的第一阶段就是把这个"只问不动"的判断，套用
到 `files_list` 上的**每一个**文件：

```c
        list_for_each_entry(luo_file, files_list, list) {
                err = luo_file_can_finish_one(file_set, luo_file);
                if (err)
                        return err;
        }
```

只要有任何一个 handler 投了反对票，函数立刻原样返回——**此刻
没有任何一个 handler 的状态被实际改动过**，所以"返回错误"本身
就是完整、干净、无副作用的回滚。这正是两阶段设计的精髓：**把
"判断会不会失败"完全移到"会产生不可逆副作用"的动作之前**，
让"失败"这件事重新回到"零成本可撤销"的范式 0 领域。

#### 11.3.3 第二阶段：`finish` —— 全员同意之后，放心地批量执行

只有在上面的循环顺利跑完、确认"所有 handler 都说可以"之后，
`luo_file_finish()` 才会进入第二个循环，真正开始不可逆的清理：

```c
        while (!list_empty(&file_set->files_list)) {
                luo_file = list_last_entry(&file_set->files_list,
                                           struct luo_file, list);

                luo_file_finish_one(file_set, luo_file);

                if (luo_file->file) {
                        xa_erase(&luo_preserved_files,
                                 luo_get_id(luo_file->fh, luo_file->file));
                        fput(luo_file->file);
                }
                list_del(&luo_file->list);
                file_set->count--;
                mutex_destroy(&luo_file->mutex);
                kfree(luo_file);
        }
```

`luo_file_finish_one()`（`luo_file.c:666-682`）则负责具体执行
"调用 `.finish()` 回调 → 通知 FLB 引用计数减一
（`luo_flb_file_finish`，呼应第 7 章 7.5 节状态机）→ `module_put`
释放模块引用"这一组动作，三者按照"先内层、后外层"的顺序排列，
与 `luo_file_preserve_fd` 当初"由外而内"建立这些关联时的顺序
正好相反——这是另一个我们在第 6、7 章反复见过的"建立顺序与
拆除顺序互为镜像"的范例，也再次印证了 LUO 整体代码风格中那种
"成对设计、首尾呼应"的一致美学。

注意这里用 `list_last_entry` + `while (!list_empty)` 而非
`list_for_each_entry_safe` 做遍历——这是因为循环体内部会把
当前节点从链表里摘除并释放，传统的"保存 next 指针"式安全遍历
在这里反而显得啰嗦；直接"每次都取链表尾部的节点、处理完就删除"
是一种更直接、更不容易出 bug 的写法（链表永远在收缩，不存在
"提前保存的 next 指针因为节点被释放而变成悬空指针"的风险）。
至于"为什么从尾部而非头部摘除"——回忆第 6 章讨论过的"FIFO 序
建立、LIFO 序拆除"的对称设计原则，这里再次得到了印证：`finish`
作为生命周期的终点，理应以与 `preserve`（建立时的 FIFO 顺序）
相反的次序进行，让"最后加入的最先离场"，这样可以最大程度地
保证"依赖关系总是先被满足、再被解除"。

#### 11.3.4 与 session 层的衔接：`luo_session_release` 中 finish 失败的"善后"

`luo_session_finish_one()`（`luo_session.c:183-187`）只是对
`luo_file_finish()` 的一层薄包装，但它的调用方
`luo_session_release()`（`luo_session.c:203-227`）展示了一个
有趣的边界情况——**当用户态进程异常退出、内核需要在 `release`
回调里自动清理一个"已检索 (incoming)"的 session 时**，如果这个
自动 `finish` 失败了怎么办？

```c
        if (session->retrieved) {
                int err = luo_session_finish_one(session);

                if (err) {
                        pr_warn("Unable to finish session [%s] on release\n",
                                session->name);
                        return err;
                }
                sh = &luo_session_global.incoming;
        } else {
                ...
        }
```

这里选择了"记录警告日志 + 把错误码透传给 `close()` 系统调用的
返回值"——既不强行把 session 从全局链表上摘除（那会造成"链表
里少了一个仍然持有未完成资源的 session"这种状态错位），也不
试图悄悄重试或忽略错误。`close()` 返回非零值在 POSIX 语义里
本身就比较罕见，会引起细心的调用方注意；再结合内核日志里留下
的 `pr_warn`，运维人员就有线索去诊断"为什么这个 session 没能
正常收尾"。这与上一节"宁可让用户态据此重试，也不要在内核里
悄悄吞掉错误"的克制态度一脉相承。

### 11.4 范式三：反序列化失败——"宁可泄漏，不做不安全 undo"

现在我们来到错误处理光谱的另一端——新内核刚刚启动，LUO 正在把
旧内核通过 KHO 移交过来的、压缩在 FDT 与裸内存块里的序列化数据
（第 8 章详述的"索引 + 仓库"结构）重新展开成活的内核对象。这一
步一旦失败，会面临一个比前两种范式都更棘手的局面。

#### 11.4.1 为什么这是一个"不可逆"的失败

请把自己代入 `luo_session_deserialize()`（`luo_session.c:513-583`）
的执行现场想象一下：当遍历到第 *i* 个序列化的 session 条目时，
`luo_session_alloc()` 因为内存不足而失败。此刻，系统中已经存在
**0 到 i-1 个**完全鲜活的 `struct luo_session`——它们已经被
`luo_session_insert()` 插入了全局链表、与之关联的 `struct luo_file`
（通过 `luo_file_deserialize()`）已经持有着对某些内核模块的
`try_module_get()` 引用、某些 `compatible` 字符串匹配到的 file
handler 已经记录了这些待恢复文件的 token 与私有数据句柄……

要把这一切"安全地退回去"，需要做到：

* 释放每一个已分配的 `struct luo_session`/`struct luo_file`；
* 对每一个已经 `try_module_get()` 的 handler 模块执行精确次数的
  `module_put()`；
* 把已经插入链表/xarray 的条目逐一摘除；
* 同时还要考虑：这些"待恢复"的资源里，有些可能**根本没有对应的
  活跃硬件状态**（因为它们尚未被 `retrieve`），但另一些（尤其是
  未来如果 VFIO 真的接入了 LUO，详见第 12、13 章的展望）则可能
  已经在新内核里**重新关联上了具体的物理设备**——这意味着"回滚"
  不再是单纯的内存操作，而可能牵涉到复杂的硬件复位时序。

这就是注释里那句话——"Implementing a safe 'undo' to unwind
complex dependencies (sessions, files, hardware state) is
error-prone and provides little value, as the system is
effectively in a broken state."（`luo_session.c:535-538`）——
想要表达的核心：**回滚代码本身的复杂度，可能会超过它试图修复的
那个错误的复杂度**，而且"系统已经处于损坏状态"这一前提意味着
即便回滚代码逻辑完美无缺，它所操作的环境本身也已经不再可信。
在这种情形下投入精力打磨一条"安全 undo"路径，边际收益趋近于零，
边际风险（"回滚代码本身引入新的 bug，把局面搞得更糟"）却不容
小觑。

#### 11.4.2 决策：把"恢复"的责任交给"更可靠的机制"——重启

注释接着给出了 LUO 的应对之道：

> "We treat these resources as leaked. The expected recovery path
> is for userspace to detect the failure and trigger a reboot,
> which will reliably reset devices and reclaim memory."
> （`luo_session.c:539-542`）

这句话里藏着一个非常值得玩味的视角转换：**当"在当前上下文中
安全恢复"这件事的可靠性低于"重启"时，最理性的选择就是主动放弃
前者，转而依靠后者**。重启之所以"更可靠"，恰恰是因为它不依赖
于"当前已经损坏的状态机能够正确判断自己损坏到了什么程度"——
它是一种从外部、从更高的可靠性层级强加的"全量复位"，不需要
对"具体哪里坏了"做任何精确诊断,就能把一切恢复到已知的、
干净的初始状态。这与分布式系统里"看门狗超时后直接重启整个
进程，而不是试图诊断并修复某个具体的死锁"是完全相通的工程
智慧——**有时候，最稳妥的恢复手段不是更聪明的代码，而是更
彻底的重置**。

而"泄漏"在这个语境下，也从一个听起来刺耳的负面词汇，变成了
一个经过深思熟虑、符合成本效益的**主动选择**——既然系统即将
被重启（内存会被整体回收、设备会被硬件级复位），那么"现在
立即释放这几个对象"和"等到重启时让它们随着整个地址空间一起
消失"在结果上没有任何区别，却在实现复杂度和潜在风险上有着
天壤之别。选择后者，是对工程资源的精打细算。

#### 11.4.3 `is_deserialized`/`saved_err`：把"一次性失败"固化为"永久性事实"

`luo_session_deserialize()` 的开头有这样一段不寻常的代码：

```c
int luo_session_deserialize(void)
{
        struct luo_session_header *sh = &luo_session_global.incoming;
        static bool is_deserialized;
        static int saved_err;
        int err;

        /* If has been deserialized, always return the same error code */
        if (is_deserialized)
                return saved_err;

        is_deserialized = true;
        ...
save_err:
        saved_err = err;
        return err;
}
```

两个 `static` 局部变量构成了一个简单却意味深长的"一次性闸门 +
错误码缓存"机制：**无论这个函数被调用多少次，它的"判定结果"
只在第一次真正生效时被计算一次，此后所有调用都会原样复现那个
结果**——无论是"成功"还是"失败"。

为什么需要这样一个机制？因为反序列化必然会在系统启动的早期
被多个路径间接触发（例如不同的 `/dev/liveupdate` 操作可能都
需要先确保 incoming 状态已经就绪），如果每次调用都重新执行一遍
"遍历、分配、插入"的逻辑：

* 对于"已经成功"的情形，重复执行将会产生**重复的 session/file
  对象**，造成命名冲突、双重计数等一系列连锁错误；
* 对于"已经失败"的情形，重复执行不仅没有任何意义（我们已经
  论证过，这种失败在哲学上不可恢复），还会进一步加剧对那个
  "已经处于损坏状态"系统的扰动——也许第二次尝试会在与第一次
  完全不同的位置失败，从而让 `pr_warn` 日志变得自相矛盾、令人
  困惑，给本就棘手的事后诊断雪上加霜。

`is_deserialized`/`saved_err` 这一对状态变量，本质上是在用最
朴素的手段实现一种"幂等性"——**把"第一次尝试的结果"作为这个
子系统此后永久的、不可更改的既定事实**。这与第 7 章讨论过的
`luo_flb_get_private()` 双重检查锁定模式（`smp_load_acquire`/
`smp_store_release` + `is_initialized`）在精神上是相通的——
都是"用一个标志位把'只应该发生一次的初始化/判定动作'锁定住"，
只不过这里因为 `luo_session_deserialize()` 在系统生命周期里
只会在早期被串行调用，所以不需要 SMP 内存屏障的复杂度，一对
普通的 `static` 变量就足够了。这是"用恰到好处的工具解决恰到
好处的问题，不过度设计"的又一例证。

`luo_file_deserialize()`（`luo_file.c:780-849`）虽然没有重复
这套"一次性闸门"机制（它由 `luo_session_deserialize()` 在持有
`session->mutex` 的情况下逐个 session 调用，天然不存在并发
重入的问题），但它的错误处理注释（`luo_file.c:801-809`）与
`luo_session_deserialize()` 一字不差——这正是本章开篇就指出的
那个细节：**两段相距数百行、服务于不同抽象层级的代码，选择
逐字复刻同一段说明**，这本身就是一种强烈的设计声明：作者希望
读者在阅读到第二处时能够立刻"认出"它,从而确信这不是孤立的
权宜之计，而是一条贯穿 session 与 file 两个层级、被反复确认过的
统一原则。

#### 11.4.4 `luo_file_deserialize` 内部："找不到 handler"与"内存不足"的不同对待

值得一提的是，`luo_file_deserialize()` 内部对不同性质的失败，
依然保持了细致的区分（即便它们最终都会触发同样的"停止并向上
传播错误"的整体策略）：

```c
                if (!handler_found) {
                        pr_warn("No registered handler for compatible '%.*s'\n",
                                (int)sizeof(file_ser[i].compatible),
                                file_ser[i].compatible);
                        return -ENOENT;
                }

                luo_file = kzalloc_obj(*luo_file);
                if (!luo_file) {
                        module_put(fh->ops->owner);
                        return -ENOMEM;
                }
```

* "找不到匹配的 compatible handler"返回 `-ENOENT`——这通常
  意味着新内核里**根本没有加载**对应的模块（也许是新内核的
  配置与旧内核不一致），这是一种**配置层面**的不兼容，与第 8
  章讨论过的"compatible 字符串 + 版本号"ABI 兼容策略直接相关；
* "`kzalloc_obj` 内存分配失败"返回 `-ENOMEM`——这是一种**资源
  层面**的偶发性短缺。

两者虽然最终都导致同一个外层函数提前返回、同一套"宁可泄漏"
策略接管局面，但区分这两种错误码依然是有价值的——它们会被
`pr_warn`/`pr_err` 写入不同的日志语句，为事后定位问题根因
（"是版本不兼容，还是单纯内存压力过大"）保留了关键线索。这
再次呼应了 11.2.3 节里强调过的"失败诚实化"——**即便选择不去
回滚，也绝不放弃如实记录"发生了什么"这一基本责任**。

还有一个不应被忽视的细节：在 `kzalloc_obj` 失败的分支里，代码
显式调用了 `module_put(fh->ops->owner)`——尽管整体哲学是"后续
不再清理"，但**在失败点本身、影响范围尚未扩散之前，做力所能及
的局部清理**仍然是值得的。这与"放弃整体回滚"并不矛盾：放弃的
是那种需要追踪"下游已经产生了多少层次的关联"的复杂回滚，而不是
"举手之劳就能避免的、影响范围明确的单步泄漏"。这是一种克制而
精准的中间立场——**不因为"反正最终都要重启"而彻底放弃所有
自律**，体现了 LUO 代码整体上"严谨但不偏执"的风格。

### 11.5 决策树：错误处理范式的统一选择逻辑

把前三节的讨论汇总成一张决策树，可以清楚地看到 LUO 在面对任意
一处潜在失败时，内心遵循的其实是同一套朴素的提问顺序：

```
                          发生了一个错误
                                │
                                ▼
              ┌─────────────────────────────────────┐
              │ Q1：此刻是否已经产生了"难以撤销的     │
              │     持久副作用"（数据结构已链接、     │
              │     模块引用已建立、设备已被接管）？  │
              └──────────────┬──────────────────────┘
                    │否       │是
                    ▼         ▼
        ┌────────────────┐  ┌─────────────────────────────────────┐
        │ 范式 0          │  │ Q2：系统当前所处的内核镜像，是否     │
        │ 直接拒绝/返回    │  │     还能让"撤销动作"安全可靠地执行？ │
        │ 错误码即可       │  │     （旧内核完整运行 vs. 新内核刚    │
        │（第 5、6 章的    │  │      启动、状态已部分迁移）          │
        │  早期校验）      │  └──────────────┬──────────────────────┘
        └────────────────┘         是│             │否
                                     ▼             ▼
                  ┌──────────────────────┐   ┌────────────────────────┐
                  │ 范式 1                │   │ 范式 3                  │
                  │ 构建精确的镜像回滚链   │   │ "宁可泄漏，不做不安全    │
                  │ （哨兵参数 + goto     │   │  undo"——固化错误码、    │
                  │  单出口 + 反向遍历）   │   │ 把恢复责任交给 reboot   │
                  │ 第 11.2 节            │   │ 第 11.4 节              │
                  └──────────────────────┘   └────────────────────────┘
                                │
                                ▼
              ┌──────────────────────────────────────┐
              │ 额外追问 Q3：这个操作是否涉及多个     │
              │  互不知情的独立 handler，且操作本身   │
              │  一旦开始执行就不可逆？                │
              └──────────────┬───────────────────────┘
                    │否      │是
                    ▼        ▼
           （范式 1 已足够） ┌─────────────────────────┐
                            │ 范式 2                   │
                            │ 拆分为"全员表态"与        │
                            │ "批量执行"两阶段          │
                            │ 第 11.3 节                │
                            └─────────────────────────┘
```

这张决策树最值得记住的一点是：**它的每一个分支判据，问的都不是
"这段代码应该怎么写才优雅"，而是"此刻这个系统的物理现实允许我
做什么"**。范式选择不是代码风格的偏好，而是对客观约束的诚实
回应——这正是贯穿本文档始终（尤其是第 6 章"两阶段提交"、第 7
章"引用计数状态机"、第 9 章"四条错误路径对比表"）的那种"机制
设计服务于物理现实，而非服务于代码的形式美感"的核心方法论在
错误处理这一专门领域里的完整呈现。

### 11.6 跨章交叉引用：把局部观察汇聚成全景图

读到这里，你也许已经意识到——本章其实没有引入任何"全新"的代码
逻辑，它做的事情，是把第 5、6、7、9 章中已经各自独立讨论过的
错误处理细节，重新放回同一张坐标系里，让它们的共性显现出来。
下表按照"范式"重新归类、汇总这些散落各处的局部观察：

| 范式 | 涉及章节与具体机制 | 共同特征 |
|---|---|---|
| **范式 0：前置拒绝** | 第 5 章 `luo_session_create` 的命名冲突检查（`-EEXIST`）/容量限制（`LUO_SESSION_MAX`）；第 6 章 `luo_file_preserve_fd` 的"七层回滚链"前几层（`luo_token_is_used`/`LUO_FILE_MAX`/`fget` 失败）；第 9 章 `memfd_luo_can_preserve` 的属性预检查 | 失败点位于"产生持久副作用之前"，回滚成本为零，直接返回错误码 |
| **范式 1：镜像回滚链** | 第 6 章 `luo_file_preserve_fd` 后几层回滚（`err_kfree`/`err_flb_unpreserve`/`err_erase_xa` 等）；第 7 章 `luo_flb_file_preserve`/`unpreserve` 的引用计数回退；第 9 章 `memfd_luo_preserve_folios` 的"`i = i - 1` 精确回滚"技术；本章 11.2 节 `__luo_file_unfreeze`/`luo_session_serialize` | 失败发生时旧内核仍然完整运行，使用"哨兵 + 精确回滚到失败点"的对称结构，撤销成本可控 |
| **范式 2：两阶段提交** | 第 6 章 `liveupdate_file_ops` 回调矩阵中 `can_finish`/`finish` 的职责划分；第 9 章"四条错误路径对比表"中 finish 阶段的处理；本章 11.3 节 `luo_file_can_finish_one`/`luo_file_finish_one` 的拆分 | 操作本身不可逆 + 涉及多个互不知情的参与者，需要"先达成一致，再批量执行"来保证原子性 |
| **范式 3：宁可泄漏** | 第 5 章 `luo_session_deserialize` 注释；第 6 章 `luo_file_deserialize` 注释（与前者逐字相同）；本章 11.4 节对二者的逐段精读 | 失败发生于"系统已迁移到新内核、部分状态已不可逆地融入运行时"之后，回滚本身在哲学上不成立，转而依赖外部更可靠的复位机制 |

这张表格本身，就是对"什么时候该不惜代价地回滚、什么时候该
果断放弃回滚"这一问题最简洁的回答——**一切都取决于"失败发生
时，系统距离'回到出发点'有多远"**。距离为零（范式 0），代价
为零；距离很近且路径完整可逆（范式 1），值得精雕细琢；距离
跨越了一次不可逆的状态迁移（范式 3），再精巧的回滚代码也只是
在一个已经无法被信任的地基上砌墙。

### 11.7 小结

本章我们暂时放下"系统如何把事情做对"这一主线，转而审视
"系统如何应对事情没有按计划进行"——而这恰恰是检验一个内核
子系统是否真正成熟的试金石。我们看到：

* LUO 的错误处理远非见招拆招的零散补丁，而是建立在对**"失败
  发生时系统所处物理阶段"的精确建模**之上的、自洽统一的三种
  范式——可回滚的冻结期失败、两阶段提交式的完成期失败、不可逆
  的反序列化失败；
* **范式一**（11.2 节）展示了"哨兵参数 + `goto` 单出口 +
  `list_for_each_entry_continue_reverse` 反向遍历"如何在
  `luo_file_freeze`/`luo_session_serialize` 两个层级上构建出
  镜像对称、首尾自洽、不重不漏的精确回滚链，把"系统仍然完好"
  这一物理优势转化为代码上的从容与严谨；
* **范式二**（11.3 节）展示了 `can_finish`/`finish` 的显式拆分
  如何把"多方参与的不可逆操作"转化为"先以零成本的方式达成
  共识，再以满怀信心的姿态批量执行"，是数据库"两阶段提交"
  思想在内核子系统里质朴而有效的复刻；
* **范式三**（11.4 节）展示了 LUO 如何以罕见的工程诚实承认
  "有些失败就是无法被安全撤销"，并主动选择"把恢复的责任交给
  更可靠的外部机制（重启）"——这不是放弃治疗，而是经过精确成本
  核算之后做出的、真正对系统稳健性负责的选择；同时 `is_deserialized`/
  `saved_err` 这对朴素的静态变量，展示了如何用最小的复杂度把
  "一次性判定"固化为"永久性事实"。
* 最后，11.6 节的交叉引用表证明了这三种范式并非本章凭空总结
  出来的抽象概念，而是从第 5、6、7、9 章里一次次具体的代码
  细节中**自然涌现**出来的统一规律——这种"先有大量具体实例、
  后有归纳出的通用法则"的认知顺序，本身也是我们阅读和理解任何
  复杂系统时最值得信赖的路径。

读完本章，希望你已经能够脱口而出地回答这样一个问题："如果
LUO 子系统的某一处突然失败了，它会怎么办？"——而你的答案，
不会是"要看具体在哪里失败"，而会是一句更有力的话："它会先
判断自己此刻还能不能回头，然后选择诚实面对、精确回滚，或是
果断止损这三条路中最契合当下处境的那一条。"

到这里，本文档对 LUO 自身机制的解构——从背景动机、底层基石
KHO，到三层架构、ioctl 接口、Session/File/FLB 三大子系统、
序列化 ABI、memfd 范例实现，再到端到端测试与本章的错误处理
全景——已经形成了一个完整的闭环。接下来，我们将把目光投向
LUO 之外的世界：第 12 章会盘点 VFIO 子系统当前与 LUO 的集成
现状（结论是：尚未集成），并深入分析"为什么 VFIO 比 memfd
难得多"；第 13 章会基于本文档已经建立起来的全部理解，给出
一份 VFIO 集成 LUO 的设计草案；第 14 章则会进一步展望 QEMU
如何借助这一整套机制实现虚拟机的热升级。

---

## 第 12 章：VFIO 对 LUO 的支持现状与差距分析

> 本章要回答的问题很直接：**今天的 Linux 内核里，VFIO 子系统
> 对 LUO 的支持到了哪一步？** 但更有价值的问题紧随其后：
> **为什么到现在还没有？这件事究竟难在哪里？** 第一个问题的
> 答案只需要一次 `grep` 就能给出；第二个问题的答案，则需要把
> 本文档第 2-11 章建立起来的关于 LUO 全部设计原则——"冻结-
> 序列化-恢复"模型、"保存不是透明的"哲学、引用计数状态机、
> ABI 版本化策略——逐一拿来检验"它们在面对一台真实运行的物理
> 设备时是否依然成立"。这种检验过程本身，恰恰是对前面十一章
> 内容最好的复习与升华。

### 12.1 结论先行：今天没有任何 VFIO-LUO 集成代码

直接给出结论：截至本文档写作时，`drivers/vfio/` 目录下**不存在
任何**对 `liveupdate`/`luo`/`kho` 相关接口的引用，`include/linux/vfio.h`
中也没有出现 `liveupdate_register_file_handler`、
`liveupdate_register_flb` 或任何与 LUO ABI（`include/linux/kho/abi/`）
相关的符号。可以用最朴素的方式验证这个结论——在整个仓库范围内
搜索：

```
$ grep -rln "liveupdate\|live_update\|LUO\|kho_\|KHO" drivers/vfio/ include/linux/vfio.h
（无任何匹配结果）
```

再翻一遍 `git log` 中所有涉及 `liveupdate` 关键字的提交记录，
能看到的全部是 LUO 核心框架自身的维护性提交（修复返回值、传播
反序列化失败、补充文档等），**没有一条与 VFIO 相关**。

这意味着：

* 没有任何 VFIO 设备的 `struct file` 能够通过
  `LIVEUPDATE_SESSION_PRESERVE_FD` ioctl 被预留——如果你现在就去
  尝试，得到的将是第 10 章里测试过的那个标准结果：`-ENOENT`
  （"找不到能够处理这个文件类型的 LUO file handler"，正如
  `tools/testing/selftests/liveupdate/liveupdate.c` 里
  `preserve_unsupported_fd` 用例对 `/dev/null` 所验证的那样，
  详见第 10 章 10.2 节）；
* `kernel/liveupdate/luo_flb.c` 里登记的全局 FLB 链表中，不会
  存在任何与 IOMMU、中断重映射、KVM vCPU 状态相关的注册项（第
  7 章讨论的那个全局共享对象框架，目前完全是"有框架、无住户"
  的状态——至少对 VFIO 相关的资源而言是如此）；
* 用户如果尝试通过 LUO + kexec 的方式对一台正在运行 VFIO 直通
  设备的虚拟机宿主机执行热升级，**今天得到的行为将与"完全没有
  LUO"几乎一样**——VFIO 设备会在 kexec 跳转的过程中被强制复位、
  其上运行的 DMA 会被中断、与之关联的虚拟机会因为设备消失而
  立刻崩溃或挂起。

### 12.2 唯一的"官方承诺"：DOC 注释里的一句话

那么，"VFIO 终将支持 LUO"这件事，是这篇文档作者凭空猜测的吗？
并不是——它确确实实写在内核源码里，只不过目前还只是**一句
郑重其事的声明，而非一行可以运行的代码**。回到 `luo_core.c`
顶部那段 `DOC: Live Update Orchestrator (LUO)` 的总览注释（第 1
章已经引用过它的开篇部分，这里我们聚焦于此前未曾展开的关键
段落）：

```c
/*
 * DOC: Live Update Orchestrator (LUO)
 *
 * Live Update is a specialized, kexec-based reboot process that allows a
 * running kernel to be updated from one version to another while preserving
 * the state of selected resources and keeping designated hardware devices
 * operational. For these devices, DMA activity may continue throughout the
 * kernel transition.
 *
 * While the primary use case driving this work is supporting live updates of
 * the Linux kernel when it is used as a hypervisor in cloud environments, the
 * LUO framework itself is designed to be workload-agnostic. ...
 *
 * The core of LUO is a mechanism that tracks the progress of a live update,
 * along with a callback API that allows other kernel subsystems to participate
 * in the process. Example subsystems that can hook into LUO include: kvm,
 * iommu, interrupts, vfio, participating filesystems, and memory management.
 *
 * LUO uses Kexec Handover to transfer memory state from the current kernel to
 * the next kernel. ...
 */
```
（`kernel/liveupdate/luo_core.c:9-39`，重点摘录第 13-15 行与
第 34-35 行）

这段注释里有两句话特别关键，值得我们逐字拆解：

1. **"...while preserving the state of selected resources and
   keeping designated hardware devices operational. For these
   devices, DMA activity may continue throughout the kernel
   transition."**（`luo_core.c:13-15`）

   这句话定义了 LUO 对"硬件设备热升级"这件事的**终极目标**——
   不只是"保存设备状态、之后能恢复"，而是**"设备在整个内核
   切换过程中持续保持运行，DMA 不中断"**。这是一个远比"冻结-
   序列化-恢复"模型更严苛的承诺——它意味着对某些设备而言，
   "冻结"这一步本身可能根本不应该发生。我们将在 12.3 节看到，
   这恰恰是 VFIO 集成在难度上与 memfd 产生本质分野的源头。

2. **"Example subsystems that can hook into LUO include: kvm,
   iommu, interrupts, vfio, participating filesystems, and
   memory management."**（`luo_core.c:34-35`）

   这是目前能找到的、内核源码中**唯一**明确点名"vfio"作为 LUO
   目标接入方的地方。请注意它的措辞——"Example subsystems
   **that can** hook into"，用的是"能够"（潜在可能性）而非
   "已经"（既成事实）；它与 `iommu`、`interrupts`、`kvm` 被
   并列在同一句话里，进一步印证了我们在 12.3 节将要论证的
   观点：**VFIO 设备的热升级从来都不是一个孤立的"注册一个
   file handler"就能解决的问题，而是一整条横跨多个子系统的
   依赖链**。

把这两条线索放在一起看，结论已经很清楚：**VFIO 支持是 LUO
设计之初就明确瞄准的目标场景之一**（甚至可以说是"主要驱动
力"——"the primary use case driving this work is supporting
live updates of the Linux kernel when it is used as a
hypervisor in cloud environments"，`luo_core.c:17-18`，而
hypervisor 场景里最关键的硬件资源正是通过 VFIO 直通给虚拟机
的设备），**但目前它仍然停留在"设计意图"阶段，距离"具体实现"
还有相当长的一段路要走**。这并不是 LUO 这个框架本身的缺陷——
恰恰相反，正如我们在 12.1 节看到的，LUO 框架已经为"file
handler"和"FLB"预留好了完整、稳健、经过 memfd 验证过的接入
机制（第 6、7 章详述）；缺失的是 VFIO 一侧——**为这样一台
"在升级过程中必须持续运转的设备"建立一套状态保存与恢复
方案**，这件事的难度，远远超出了"实现几个回调函数"的范畴。

### 12.3 为什么 VFIO 比 memfd 难得多：一张本质对比表

要理解这件事究竟难在哪里，最好的办法就是把"目前唯一已经落地
的范例——memfd"和"目标但尚未落地的范例——VFIO 设备"放在
同一张表里逐项对比。这张表的每一行，对应的都是本文档第 6、9
章里已经详细讨论过的 LUO 核心机制——通过重新审视这些机制在
"VFIO 设备"这一假想场景下是否还能成立，我们就能精确地定位出
"难"到底难在何处。

| 维度 | memfd（已落地，详见第 9 章） | VFIO 直通设备（尚未落地，本节分析） |
|---|---|---|
| **本质** | 纯内存对象——一组 page cache 中的 folio + 元数据 | 一个**活跃运行**的物理/虚拟硬件实体——有寄存器、有中断、有 DMA、有自己的内部状态机 |
| **"冻结"是否可行/廉价** | 可行且廉价——`memfd_luo_preserve` 在持有 `inode_lock` 的情况下把文件标记为"已保存"，本质上是让它的内容在保存期间静止下来（详见第 9 章 9.3 节） | **代价高昂、甚至可能不可行**——根据 `luo_core.c:14-15` 的设计目标，目标恰恰是让设备"在整个内核切换过程中持续运转、DMA 不中断"。强行冻结意味着违背设计初衷；不冻结又意味着设备状态在"冻结快照"形成之后仍在持续变化 |
| **状态的可序列化性** | 完全可序列化——`folio` 的内容就是数据本身，`pfn`/`flags`/`index` 就是其全部的元数据（详见第 8 章 `struct memfd_luo_folio_ser` 的字节级布局） | 状态分布在多个层面，且许多层面**不是简单的内存快照可以表达的**：PCI 配置空间、设备私有 MMIO 寄存器堆、IOMMU 页表与映射关系、中断路由表、正在传输中的 DMA 描述符环等，它们彼此关联、互相依赖，且很多直接由硬件而非软件维护 |
| **跨重启的"恢复"路径** | 在新内核中重新分配 page cache、用保存的 `pfn` 重建 folio、重新插入 page cache（详见第 9 章 9.7 节 `memfd_luo_retrieve_folios`） | 设备本体（如果选择不复位）**根本不需要"恢复"**——它本来就还在运行；但与之关联的软件侧状态（IOMMU 映射、中断路由、`struct vfio_device`/`struct kvm` 数据结构）需要在新内核里被重新构建并与依然运行的硬件**精确对齐**，任何错位都可能导致 DMA 写坏内存或设备失去响应 |
| **依赖的子系统数量** | 几乎是自包含的——只依赖 page cache/shmem 和 KHO 的 `kho_preserve_folio`/`kho_preserve_vmalloc`（详见第 9 章 9.4 节） | 形成一条长长的依赖链——按照 `luo_core.c:34-35` 的列举：`vfio` 本身只是链条的一环，还需要 `iommu`（管理 DMA 映射）、`interrupts`（管理中断路由与亲和性）、`kvm`（管理虚拟机与直通设备的关联）协同动作，**任何一环没有就绪，整条链就无法闭环** |
| **failure 的影响半径** | 失败影响的是"这一个 memfd 里的数据是否完整"，本质上是一个内存正确性问题（详见第 9 章 9.9 节四条错误路径对比表） | 失败可能意味着"一台正在生产环境中运行、承载着真实业务的虚拟机失去其直通设备"，影响半径直接扩大到上层 hypervisor 与租户业务，恢复成本和风险量级完全不在一个数量级上 |
| **现有可借鉴的内核机制** | 无需借鉴——shmem/page cache 本身的实现已经覆盖了大部分细节，LUO 只需要在其上补一层"序列化/反序列化" | 已经存在一套成熟、广泛部署的实时迁移框架——`vfio_migration_ops` + `VFIO_DEVICE_STATE_*` 状态机（详见 12.4 节），它已经解决了"如何把一个活跃设备的状态打包成数据流"这个核心难题，**理论上可以被复用，但其设计前提与 LUO 的应用场景存在微妙却关键的错位**（详见 12.5 节） |

读完这张表，"为什么 VFIO 比 memfd 难得多"这个问题的答案已经
跃然纸上：**memfd 的"保存"本质上是对一份静态数据的快照与
重建，而 VFIO 设备的"保存"本质上是要在'让设备完全停下来'与
'让设备完全不受打扰地继续运行'这两个互相矛盾的目标之间，
找到一条精确、可靠、可验证的中间道路**——这条道路要求软件
状态与硬件实际行为在一次内核切换的前后保持位级精度的一致，
其难度早已超出了"实现一个 LUO file handler"的范畴，而进入了
"重新设计一整条设备生命周期管理基础设施"的领域。

### 12.4 现有基石：VFIO 实时迁移框架（`vfio_migration_ops`/`VFIO_DEVICE_STATE_*`）速览

幸运的是，VFIO 子系统并非要从零开始解决"如何描述一台活跃设备
的可迁移状态"这个问题——它早已为支持**虚拟机实时迁移
（live migration）**而构建了一整套成熟的状态机和回调接口。
理解这套既有机制，是理解"VFIO 集成 LUO 还差什么"的必要前提，
因为第 13 章的设计草案在很大程度上正是建立在"能在多大程度上
复用它"这一问题的答案之上的。

#### 12.4.1 `struct vfio_migration_ops`：三个回调，一套数据流协议

这套机制的核心接口定义在 `include/linux/vfio.h:224-233`：

```c
struct vfio_migration_ops {
        struct file *(*migration_set_state)(
                struct vfio_device *device,
                enum vfio_device_mig_state new_state);
        int (*migration_get_state)(struct vfio_device *device,
                                   enum vfio_device_mig_state *curr_state);
        int (*migration_get_data_size)(struct vfio_device *device,
                                       unsigned long *stop_copy_length);
};
```

它的设计哲学和 LUO 的 `liveupdate_file_ops`（第 6 章）有着
异曲同工之妙——都是"把复杂的生命周期管理，收敛成几个清晰
划分职责的回调函数"：

* **`migration_set_state`** 是这套接口里真正的主角——它请求
  设备从当前状态迁移到一个新状态，并且**返回一个 `struct file *`**。
  这个返回的文件描述符不是普通意义上的"文件"，而是一条**数据
  传输管道**——根据状态机的方向，用户态既可以从这个 fd 里
  `read()` 出设备状态数据流（迁出方向），也可以向这个 fd 里
  `write()` 注入之前保存的数据流（迁入方向）。这种"用文件
  描述符抽象一条数据流"的设计，与 LUO 整个体系"一切皆围绕
  `struct file`"的哲学（见第 4、6 章）形成了一种隔空呼应——
  尽管它们解决的是完全不同层面的问题；
* **`migration_get_state`** 用于查询设备当前所处的迁移状态——
  在复杂的组合状态转换路径中，用户态需要确认"设备真的已经
  到达了我期望的状态"；
* **`migration_get_data_size`** 用于在进入 `STOP_COPY` 阶段前
  让用户态预估"完成这次停机拷贝大概需要传输多少数据"，从而
  规划停机时间预算——这对实时迁移场景里"尽量缩短业务不可用
  窗口"的核心诉求至关重要。

#### 12.4.2 `VFIO_DEVICE_STATE_*`：一台设备的"全生命周期地图"

与这三个回调配套的，是一个用 `enum vfio_device_mig_state`
（`include/uapi/linux/vfio.h:1238-1248`）定义的状态空间：

```c
enum vfio_device_mig_state {
        VFIO_DEVICE_STATE_ERROR = 0,
        VFIO_DEVICE_STATE_STOP = 1,
        VFIO_DEVICE_STATE_RUNNING = 2,
        VFIO_DEVICE_STATE_STOP_COPY = 3,
        VFIO_DEVICE_STATE_RESUMING = 4,
        VFIO_DEVICE_STATE_RUNNING_P2P = 5,
        VFIO_DEVICE_STATE_PRE_COPY = 6,
        VFIO_DEVICE_STATE_PRE_COPY_P2P = 7,
        VFIO_DEVICE_STATE_NR,
};
```

这八个状态构成了一张精心设计的有限状态机（FSM）。结合内核
头文件中那段长达 80 余行的状态机说明文档
（`include/uapi/linux/vfio.h:1115-1245`，本节摘录其要点），
我们可以画出这张图：

```
                         ┌──────────────────────────────────────────┐
                         │                                          │
                         ▼                                          │
   RUNNING ────────► RUNNING_P2P ────────► PRE_COPY_P2P ───────► PRE_COPY
      │  (正常运行)    │ (P2P DMA 静止,    │ (P2P 静止 +         │ (允许迁出方
      │                │  其余照常)        │  预拷贝进行中)       │  开始预拷贝,
      │                │                   │                     │  设备仍运行)
      │                ▼                   ▼                     │
      │              STOP ◄────────────────┴─────────────────────┘
      │            (设备完全静止,
      │             不接受任何 DMA)
      │                │  │
      │      ┌─────────┘  └──────────┐
      │      ▼                       ▼
      │  STOP_COPY               RESUMING
      │ (停机拷贝:               (迁入方接收数据流,
      │  迁出方读出最终           设备处于"正在恢复"
      │  状态数据流)              的中间态)
      │      │                       │
      │      └──────────┬────────────┘
      │                 ▼
      │             （迁移完成 / 失败）
      │                 │
      └─────────────────┴──────► 任意状态转换失败 ──► ERROR
                                  （只能通过 VFIO_DEVICE_RESET
                                   恢复到 RUNNING）
```

这张状态机图里有几个设计细节，特别值得我们用"已经理解了 LUO
设计哲学"的眼光重新审视一遍——你会发现它们彼此之间存在着
奇妙的"似曾相识感"：

* **P2P（peer-to-peer）静止状态的设计**（`RUNNING_P2P`/
  `PRE_COPY_P2P`）——文档里这样描述它的目的："The optional
  peer to peer (P2P) quiescent state is intended to be a
  quiescent state for the device for the purposes of managing
  multiple devices within a user context where peer-to-peer
  DMA between devices may be active."（`include/uapi/linux/vfio.h:1206-1208`）。
  这与第 9 章讨论 `memfd_luo_preserve` 时反复强调的"先让对象
  静止下来，再对其状态进行快照"的核心思想完全一致——只不过
  memfd 处理的是单个文件内部的并发写入者，而这里处理的是
  **多个设备之间通过 DMA 互相影响的复杂耦合关系**，需要一个
  专门的中间状态来"冻结跨设备的交互，但暂不冻结设备自身"；

* **"任意状态都可能滑落到 ERROR，只能靠复位恢复"的兜底设计**
  （"any -> ERROR ... To recover from ERROR VFIO_DEVICE_RESET
  must be used to return the device_state back to RUNNING."，
  `include/uapi/linux/vfio.h:1196-1201`）——这与第 11 章
  讨论的"宁可泄漏不做不安全 undo、把恢复责任交给更可靠的外部
  机制"在精神内核上完全相通：当一个状态转换中途失败、设备
  内部状态已经无法用软件准确判断时，**与其试图精确诊断并修复，
  不如诉诸一个更底层、更可靠、更"暴力"的复位动作**，把系统
  带回一个已知的、干净的起点；

* **"最短路径 + 不允许把保存类状态作为中间过渡点"的组合转换
  规则**（"Select the shortest path. The path cannot have
  saving group states as interior arcs, only starting/end
  states."，`include/uapi/linux/vfio.h:1218-1220`）——这条
  规则背后的考量，与第 7 章讨论 FLB 引用计数状态机时反复强调
  的"状态转换路径必须是确定性的、不依赖运行时偶然性的"如出
  一辙：复杂系统的状态机设计,最怕的就是"同一个起点到终点之间
  存在多条路径，导致系统行为难以预测、难以测试穷尽"，而这条
  规则正是为了消除这种不确定性而设的"导航算法约束"。

这些相通之处并非巧合——它们都是"如何在复杂、有状态、可能
随时失败的系统中设计可靠的生命周期管理"这一更普遍工程问题的
不同侧面解答。看到这些相通之处，也让我们对"VFIO 与 LUO
能否兼容"这件事多了一份审慎的乐观：**两套机制的设计哲学是
相通的，分歧的根源不在"理念"，而在"前提假设"**——这正是
下一节要深入讨论的话题。

### 12.5 真正的鸿沟：实时迁移与 LUO 之间存在的"前提假设错位"

现在到了本章最关键的分析部分。既然 VFIO 已经有了一套成熟、
广泛验证过的"序列化活跃设备状态"的机制（`vfio_migration_ops`），
那为什么不能简单地"复用"它来实现 VFIO 设备的 LUO 集成呢？
答案藏在两套机制各自的**前提假设**里——而这些前提假设上的
差异，恰恰映照出本文档第 1 章就已经埋下的伏笔："LUO 与传统
虚拟机热迁移、CRIU 等方案的关系定位"。

#### 12.5.1 "目的地"完全不同：另一台机器 vs. 同一台机器的下一个内核

`vfio_migration_ops` 的整套设计,服务的场景是**虚拟机的实时
迁移**——把一台正在运行的 VM（连带它直通的设备状态）从物理
机 A 迁移到物理机 B。这个场景里：

* "源端"和"目的端"是**两个独立运行的内核实例**，它们各自
  拥有自己完整的硬件资源、各自独立地管理着自己的 IOMMU、
  中断控制器、PCI 拓扑；
* 数据传输通过 `migration_set_state` 返回的 `struct file *`
  这条**网络/管道式的数据流**完成，源端和目的端之间不共享
  任何内存——这是一个彻头彻尾的"序列化 → 网络传输 →
  反序列化"过程；
* 迁移完成后，**源端的设备会被释放、复位，目的端的设备会
  被重新初始化**——两端的硬件实体本质上是两个独立的个体，
  只是恰好承载着同一份逻辑状态。

而 LUO 服务的场景是**同一台物理机上、同一组物理硬件设备，
经历一次内核版本切换**——这意味着：

* "源端"（旧内核）和"目的端"（新内核）操作的是**同一组物理
  硬件**——同一颗 CPU、同一组 PCI 设备、同一个 IOMMU 实例；
* 这恰恰为 LUO 打开了一扇 `vfio_migration_ops` 从未设想过的
  大门——**既然硬件设备本身没有变化，为什么不能让它在整个
  切换过程中"原地不动、持续运行"，而不是被迫经历一次"打包-
  传输-重建"的折腾**？这正是 `luo_core.c:14-15` 里那句
  "DMA activity may continue throughout the kernel transition"
  真正想要表达的雄心——它瞄准的不是"如何把设备状态完整地
  迁移到别处"，而是"如何让设备压根不需要被打扰"；
* 但这扇门的另一侧也布满荆棘：要做到"设备原地不动地撑过一次
  内核切换"，新内核里管理这台设备的全部软件基础设施——
  IOMMU 页表、中断路由表、`struct vfio_device`、与之关联的
  `struct kvm`——都必须在**设备完全不被打扰**的前提下被重新
  建立起来,并与设备此刻的真实物理状态严丝合缝地对齐。这是一类
  `vfio_migration_ops` 从未需要面对的全新约束：它的整个设计都
  建立在"设备最终会被复位、重新初始化"这一假设之上,而 LUO
  恰恰想打破这个假设。

#### 12.5.2 状态机里"STOP/STOP_COPY/RESUMING"在 LUO 语境下的尴尬

把 `VFIO_DEVICE_STATE_*` 状态机拿到 LUO 的场景下重新审视，
会发现它的多数状态都隐含着一个 LUO 想要避免的前提——"设备
需要先停下来"：

* `STOP`/`STOP_COPY` 状态意味着"设备完全静止,不接受任何 DMA"——
  这正是 LUO 试图避免的"冻结"动作。如果 VFIO 集成 LUO 的
  方案不得不依赖这两个状态,那么"DMA 活动贯穿整个内核切换
  过程"这一目标就根本无从谈起；
* `RESUMING` 状态描述的是"设备正在从一个数据流中恢复状态"——
  这隐含着"设备此刻是一张白板,正在被动地接受外部灌入的数据"。
  但 LUO 场景里,新内核启动时设备**根本不是一张白板**——它
  从旧内核的运行状态直接延续过来,带着自己此刻真实的、动态的
  内部状态(可能还有正在传输中的 DMA),没有人能够、也没有
  必要把这些状态"重新灌入"给它。

这并不是说 `VFIO_DEVICE_STATE_*` 状态机"设计得不好"——恰恰
相反,它针对自己的目标场景(实时迁移)设计得极其精巧、考虑
极其周全(我们在 12.4.2 节已经领略过它的精妙之处)。问题在于,
**LUO 想要解决的是一个"目的地不同"的问题**——它不需要"如何
把状态搬运到别处",而需要"如何让状态原地保持、同时让管理它的
软件基础设施完成一次精确的'交接班'"。这是两个本质不同的问题,
即便它们表面上都叫"迁移/保存设备状态"。

#### 12.5.3 一个更精确的类比：USB 设备在 `kexec` 过程中的"幸存"

为了更直观地理解 LUO 对 VFIO 设备的诉求,不妨打一个类比——
回想一下普通系统执行 `kexec` 重启时,USB 控制器、网卡、磁盘
控制器等设备会发生什么:它们会先被各自的驱动 `.shutdown()`/
`.remove()`,进入一个"安静"的状态,然后整个系统跳转到新内核,
新内核重新探测、初始化这些设备,一切从零开始。这个过程对
绝大多数设备而言代价是可以接受的——重新初始化一块网卡只需要
几百毫秒。

但是,设想一台云 hypervisor 宿主机上,通过 VFIO 直通给客户
虚拟机的一块高性能 GPU 或者智能网卡(SmartNIC)——它内部可能
正在处理着客户的实时 AI 推理请求或者承载着数万 TCP 连接的
状态。让它经历一次"复位-重新初始化"的过程,代价就远不只是
"几百毫秒的延迟"那么简单了——客户虚拟机里运行的应用会感知到
设备消失、连接中断、计算任务失败,造成的影响可能是数十秒甚至
数分钟的服务不可用,以及随之而来的客户投诉、SLA 违约。这正是
`luo_core.c` 开篇那句"the primary use case driving this work
is supporting live updates of the Linux kernel when it is used
as a hypervisor in cloud environments"(`luo_core.c:17-18`)
背后真实的商业与技术驱动力——**LUO 想要做到的,是让这类设备
"安然无恙地撑过"内核切换这一整个过程,如同它从未被打扰过一样**。

而 `vfio_migration_ops` 框架,恰恰是为"设备终将经历一次完整
的复位与重建"这一前提服务的——它把"复位重建的代价"妥善地
封装、转移到了"目的端"(另一台物理机)上,但从未试图消除这个
代价本身。LUO 想要消除的,正是这个代价——这才是二者之间真正
的、根本性的鸿沟。

### 12.6 小结：差距清单与下一章的展望

把本章的分析浓缩成一份"待解决问题清单",作为通向第 13 章设计
草案的桥梁:

| 序号 | 差距/挑战 | 现状 | 第 13 章将探讨的方向 |
|---|---|---|---|
| 1 | 没有任何 VFIO file handler 注册到 LUO | `drivers/vfio/` 中无 `liveupdate_register_file_handler` 调用(12.1 节) | 给出注册 file handler 的伪代码框架与 compatible 字符串/回调职责划分 |
| 2 | "设备持续运行、DMA 不中断"与"冻结-序列化-恢复"模型的根本冲突 | `VFIO_DEVICE_STATE_STOP`/`STOP_COPY` 隐含"设备必须先停"的前提,与 `luo_core.c:14-15` 的目标相悖(12.5.2 节) | 探讨"近乎不冻结"的 freeze/unfreeze 回调可能的实现思路,以及"软件状态交接"与"硬件持续运行"解耦的设计空间 |
| 3 | IOMMU/中断/KVM 构成一条必须协同的依赖链 | 目前只有 LUO 框架,没有任何一环就绪(`luo_core.c:34-35` 仅仅"点名"了它们,见 12.2 节) | 画出 FLB 依赖链图,讨论 IOMMU 页表/中断路由作为 FLB 候选对象的设计草案 |
| 4 | 现有 `vfio_migration_ops` 框架的前提假设(目的地是另一台机器、设备终将复位重建)与 LUO 场景错位 | 12.5 节已详述这一"鸿沟"的本质 | 给出与 `vfio_migration_ops`/`VFIO_DEVICE_STATE_*` 的逐项对比表,探讨二者是否有可能通过新增状态/新增能力协商机制实现某种程度的统一 |
| 5 | 缺乏可供参考的"持续运行设备跨 kexec 保存恢复"先例 | memfd 是目前唯一落地的范例,但它属于"纯内存对象、可冻结"这一相对简单的类别(见 12.3 节对比表) | 借助本文档第 6-9 章对 memfd 实现细节的全部理解,类比推演 VFIO handler 各回调的可能实现轮廓,同时坦诚指出仍然存在的未知与挑战 |

这份清单也再次印证了本章开篇提出的论断——VFIO 集成 LUO 的
真正难度,并不在于"file handler 这一层接口设计得是否友好"
(LUO 在这方面已经做得足够出色,memfd 的成功落地就是明证),
而在于**它触及了操作系统中一个长期存在、从未被真正攻克过的
难题:如何让一台正在运行的物理设备,在管理它的软件基础设施
经历一次彻底重建的同时,自身却感受不到任何中断**。这个难题
的解法,注定不会是某一个子系统单方面的努力可以达成的——它
需要 VFIO、IOMMU、中断子系统、KVM,乃至硬件厂商提供的设备
固件能力,共同向同一个目标迈进。

带着这份清醒的认知,我们在下一章将尝试做一件"知其不可而为之"
的事情——基于本文档已经建立起来的对 LUO 全部机制的理解,
**画出一份 VFIO 集成 LUO 的设计草案**。这份草案不会是、也不
可能是"现成的、可以直接合并进内核的实现方案"——它更像是
一张"如果让我们来设计,可能会从哪里入手"的路线图,其价值
不在于"预测未来的代码长什么样",而在于通过"亲手画一遍设计图"
这一过程,把本文档前十二章学到的全部知识、原则、模式重新
应用一遍,从而真正检验——我们是否已经把 LUO 这套机制读懂、
读透了。

---

## 第 13 章：VFIO 集成 LUO 的设计草案

> 声明在先：本章接下来呈现的所有伪代码、表格、依赖关系图,
> **都不是内核里已经存在的实现,而是基于本文档第 1-12 章对
> LUO 全部机制的理解,推演出的一份"如果让我们来设计,可能会
> 是什么样子"的草案**。它的目的不是预测未来的真实代码,而是
> 通过"亲手设计一遍"这个过程,把前面学到的全部知识重新应用
> 一次——这是检验理解程度最有效的方式,就如同学完一门数学
> 理论后,最好的巩固方式从来不是"再读一遍证明",而是"自己
> 动手解一道新题"。
>
> 我们将从一个意想不到的地方开始——内核源码里其实已经埋下
> 了一个非常有趣的"彩蛋",它会成为本章叙事的绝佳起点。

### 13.1 起点：源码里早已写好的暗示——`"vfiofd-v1"`

在第 6 章讨论 `struct liveupdate_file_handler` 的 `compatible`
字段时,我们曾经引用过它的文档注释,但当时的重点放在"这个
字符串如何与 `struct file` 关联、如何参与匹配查找"上。现在
让我们重新回到这段注释,逐字重读一遍它给出的**示例**:

```c
/**
 * struct liveupdate_file_handler - Represents a handler for a live-updatable file type.
 * @ops:                Callback functions
 * @compatible:         The compatibility string (e.g., "memfd-v1", "vfiofd-v1")
 *                      that uniquely identifies the file type this handler
 *                      supports. ...
 */
```
（`include/linux/liveupdate.h:88-91`，重点见第 90 行）

请留意这个示例——它给出了**两个** compatible 字符串作为范例:
`"memfd-v1"` 和 `"vfiofd-v1"`。前者我们再熟悉不过——它正是
`mm/memfd_luo.c` 里真正注册、真正落地、被第 9 章逐函数精读过的
那个 handler 所使用的字符串(尽管实际版本号已经迭代到了
`MEMFD_LUO_FH_COMPATIBLE = "memfd-v2"`,见 `include/linux/kho/abi/memfd.h:91`,
但 `liveupdate.h` 里这条更早些的文档注释依然保留着 `"memfd-v1"`
这一历史印记,这本身也从侧面印证了第 8 章讨论过的"compatible
字符串 + 版本号递增"演进策略确实在被使用)。

而后者——`"vfiofd-v1"`——**在整个代码仓库中,只出现在这一行
文档注释里**。它没有对应任何已经注册的 `liveupdate_file_handler`
实例,没有任何 `MODULE_LICENSE`、没有任何 `.ops` 表、没有任何
实现代码。它就像一张"预留车位"的标牌——清清楚楚地告诉每一位
读到这里的内核开发者:**这个名字已经被"内定"了,将来 VFIO 的
LUO file handler,大概率会以它(或者它的某个迭代版本)的
身份登场**。

这个发现对本章的意义,不亚于考古学家在遗址里挖出一张写有
"此处计划修建神庙"的石碑——它不能告诉我们神庙最终会是什么
模样,但它确确实实地告诉我们:**设计者从一开始就在为这件事
预留位置、铺设地基**。我们接下来的设计草案,就以这个名字为
起点和锚点。

### 13.2 注册 file handler 的伪代码框架

让我们模仿 `mm/memfd_luo.c` 里 `memfd_luo_init()` 注册自身
file handler 的方式(详见第 9 章对该函数的精读),为一个
假想中的 `vfio_luo` 模块画出它的注册框架:

```c
/* drivers/vfio/vfio_luo.c (假想中的文件,本节为推演性设计草案) */

#define VFIO_LUO_FH_COMPATIBLE  "vfiofd-v1"

static const struct liveupdate_file_ops vfio_luo_file_ops = {
        .can_preserve = vfio_luo_can_preserve,
        .preserve     = vfio_luo_preserve,
        .unpreserve   = vfio_luo_unpreserve,
        .freeze       = vfio_luo_freeze,
        .unfreeze     = vfio_luo_unfreeze,
        .retrieve     = vfio_luo_retrieve,
        .can_finish   = vfio_luo_can_finish,
        .finish       = vfio_luo_finish,
        .get_id       = vfio_luo_get_id,
        .owner        = THIS_MODULE,
};

static struct liveupdate_file_handler vfio_luo_handler = {
        .ops        = &vfio_luo_file_ops,
        .compatible = VFIO_LUO_FH_COMPATIBLE,
};

static int __init vfio_luo_init(void)
{
        return liveupdate_register_file_handler(&vfio_luo_handler);
}
```

这个框架本身平平无奇——它严格遵循第 6 章总结过的"回调矩阵"
模式,与 `mm/memfd_luo.c` 的注册代码几乎一模一样。**这恰恰是
LUO 框架设计成功的体现**:无论被保存的对象是一段内存还是
一台设备,接入 LUO 这件事本身的"仪式"是统一的、廉价的、
可预测的。真正的难度,全部压缩进了 `vfio_luo_file_ops` 表里
那八个回调函数的**具体实现**之中——这正是接下来几节要逐一
推演的内容。

下表给出每个回调在"VFIO 设备"语境下的职责定位,并与 memfd
对应实现(第 9 章已详述)做横向对比,作为后续展开讨论的索引:

| 回调 | memfd 实现要点(第 9 章) | VFIO 语境下的职责设想(本章 13.3 节将展开) | 难度评估 |
|---|---|---|---|
| `can_preserve` | 检查文件是否为 shmem/memfd 类型(`memfd_luo_can_preserve`) | 检查 `struct file` 是否对应一个已绑定到 `vfio_device` 的设备 fd | 低——纯粹的类型判定,模式与 memfd 几乎一致 |
| `preserve` | 锁定 inode、调用 `kho_alloc_preserve` 分配序列化结构、保存 seals/size/folios(9.3、9.4 节) | 记录设备此刻的运行状态快照所必需的元数据(PCI BDF、IOMMU 映射表的引用、中断路由表的引用、与之关联的 `kvm`/`iommufd` 上下文标识),通过 KHO 序列化结构传递给新内核 | 高——不是"快照数据本身",而是"指向活跃状态的引用关系网" |
| `unpreserve` | 镜像回滚 `preserve` 的全部分配(9.6 节) | 镜像回滚 `preserve` 阶段记录的全部引用关系、释放任何中间分配 | 中——遵循 LUO 既有的镜像回滚模式(第 11 章范式一),但涉及的资源种类更复杂多样 |
| `freeze` | 重新捕获 `f_pos`(9.5 节,"近乎不动") | **关键分歧点**——理想情况下"近乎不冻结",只做最后一刻的状态核对与一致性快照;但某些寄存器/DMA 描述符状态可能必须在此刻"定格" | 极高——这正是第 12 章揭示的核心矛盾的正面战场 |
| `unfreeze` | 调用 handler 的 `.unfreeze` 回滚 freeze 动作(第 11 章 11.2 节) | 撤销 freeze 阶段做的任何"定格"动作,让设备退回完全运行状态 | 中——只要 freeze 设计得当(改动越少,回滚越简单),unfreeze 应当是轻量的 |
| `retrieve` | 在新内核里重建 page cache、返回新 `struct file`(9.7 节) | 在新内核里重新构建 `struct vfio_device`、重新关联 IOMMU/中断/`kvm` 上下文,使其精确指向**仍在运行的同一台物理设备**,返回新的设备 fd | 极高——这是"重建管理结构、对齐物理现实"而非"重建数据本身" |
| `can_finish`/`finish` | 三态 retrieve_status 缓存判定 + 折叠式清理(9.8 节、第 11 章 11.3 节) | 判定"设备是否已经被新内核的 VFIO/IOMMU/中断/KVM 各层完全接管、可以安全释放 LUO 侧的引用",再执行最终的引用释放 | 中高——两阶段提交模式可以直接复用,难点在于"判定条件"本身需要横跨多个子系统达成一致 |
| `get_id` | 返回 inode 号(`memfd_luo_get_id`) | 返回设备的某个稳定标识(如 PCI BDF 编码、`vfio_device` 的内部索引) | 低——只要存在一个跨重启保持稳定的标识符即可 |

读这张表时,请特别留意一个规律:**凡是"操作不可变的元数据/
引用关系"的回调(`can_preserve`/`get_id`/`unpreserve`/
`unfreeze`)难度都相对可控,而凡是涉及"与活跃硬件状态精确
对齐"的回调(`preserve`/`freeze`/`retrieve`)难度都呈跳跃式
上升**——这与第 12 章 12.3 节那张对比表得出的结论遥相呼应:
**真正的鸿沟不在于"LUO 接口是否好用",而在于"如何在一个
持续变化的物理现实面前,做出精确、可靠、可验证的状态描述
与重建"**。

### 13.3 各回调的设计草图与关键权衡

接下来,我们逐一深入这张表里标记为"高难度"的几个回调,尝试
画出更具体的设计草图——不是为了给出"标准答案"(这件事本身
还没有标准答案),而是为了让"难"这件事变得**具体、可触摸、
可讨论**,而不是停留在一句空泛的"这很难"上。

#### 13.3.1 `preserve`:保存"引用关系"而非"数据本身"

memfd 的 `preserve`(第 9 章 9.3、9.4 节)做的事情,本质上是
"把数据从一种存在形式(运行中的 page cache)转换成另一种存在
形式(可被 KHO 跨内核传递的序列化结构)"。

但 VFIO 设备的 `preserve` 不能这样做——设备的"数据"(它内部
寄存器的值、它正在传输的 DMA 描述符、它的电源管理状态)**不
应该、也不可能被"复制一份带到下一个内核"**——它们必须原地
保留在硬件里。`vfio_luo_preserve` 真正需要保存的,是一组**指向
活跃状态的稳定引用**——伪代码大致会是这样的形态:

```c
static int vfio_luo_preserve(struct liveupdate_file_op_args *args)
{
        struct vfio_device *vdev = vfio_device_from_file(args->file);
        struct vfio_luo_ser *ser;       /* 假想中的 KHO ABI 序列化结构 */
        int err;

        /* 与 memfd_luo_preserve 类似:先确认设备目前处于一个
         * "可以安全声明进入热升级流程"的状态——但注意,这里
         * 的"安全"未必意味着"静止",更可能意味着"当前没有正在
         * 进行中的、不可被打断的状态转换(例如正在响应另一个
         * VFIO_DEVICE_FEATURE_MIGRATION 请求)"。
         */
        err = vfio_device_luo_precheck(vdev);
        if (err)
                return err;

        ser = kho_alloc_preserve(sizeof(*ser));
        if (IS_ERR(ser))
                return PTR_ERR(ser);

        /* 记录的不是数据快照,而是"如何在新内核里找到并接管
         * 同一台设备"所需的全部坐标信息。 */
        ser->pci_bdf          = vfio_device_pci_bdf(vdev);
        ser->iommu_fault_data = vfio_device_iommu_token(vdev);
        ser->irq_routing_ref  = vfio_device_irq_token(vdev);
        ser->kvm_assoc_ref    = vfio_device_kvm_token(vdev);
        /* ...等等:任何"在新内核里重建管理结构所需的坐标"。 */

        args->serialized_data = virt_to_phys(ser);
        args->private_data = vdev; /* 持有对活跃设备对象的引用 */

        return 0;
}
```

这段伪代码里最重要的设计决策,藏在那句注释里——**"记录的不是
数据快照,而是坐标信息"**。这是一种与 memfd 截然不同的"保存"
范式:memfd 保存的是"内容的复制品",VFIO 设备保存的则更像是
一张**"藏宝图"**——它不包含宝藏本身(宝藏——也就是设备的
活跃状态——会原地不动),只包含"如何在新世界里精确地找到这
件宝藏,并重新拥有对它的支配权"所需的全部坐标。

这个范式转变,直接决定了 `struct vfio_luo_ser` 这样一个假想
ABI 结构(若有朝一日真正落地,大概率会出现在
`include/linux/kho/abi/vfio.h` 中,与第 8 章讨论过的 `memfd.h`
并列)的设计重心——它需要回答的核心问题不是"如何字节精确地
描述设备的内部状态"(那是 `vfio_migration_ops` 已经解决得很好
的问题),而是"**新内核里的软件基础设施,需要哪些坐标信息,
才能在不打扰设备的前提下,重新认领它、接管它**"。

#### 13.3.2 `freeze`/`unfreeze`:在"完全不动"与"必须定格"之间寻找最小交集

这是整张表里难度评估最高的一对回调,原因正如第 12 章 12.5.2
节分析的那样——"冻结"这个动作本身,与 LUO 对 VFIO 设备的
终极目标存在张力。

但"近乎不冻结"不等于"完全不需要 freeze 回调"。让我们换一个
角度想:即便设备本身持续运行、DMA 不中断,**总有那么"一瞬间"
——旧内核即将让出 CPU、新内核尚未接管——在这一瞬间,某些
"软件侧的簿记信息"(而非"设备硬件状态")必须被最后定格一次,
否则新内核接手时手里的坐标信息就会失之毫厘、谬以千里**。
`freeze` 回调真正的使命,可能恰恰是为这"最后一瞬"服务的:

```c
static int vfio_luo_freeze(struct liveupdate_file_op_args *args)
{
        struct vfio_device *vdev = args->private_data;
        struct vfio_luo_ser *ser = phys_to_virt(args->serialized_data);

        /* 这里不冻结设备本身——设备依然在运行、DMA 依然在
         * 流动。我们只是给"此刻这一瞬间"的软件簿记信息拍一张
         * 快照,确保 preserve 阶段记录的坐标信息在交接的最后
         * 一刻仍然准确。
         *
         * 设想的候选项可能包括:
         *  - 当前 IOMMU 映射表的版本号/世代标记
         *  - 当前中断亲和性配置的快照
         *  - 任何"在 preserve 之后、reboot 之前"可能发生变化、
         *    且变化后会让新内核里的接管逻辑产生歧义的簿记字段
         */
        ser->iommu_generation = vfio_device_iommu_generation(vdev);
        ser->irq_affinity_snapshot = vfio_device_irq_affinity_snapshot(vdev);

        return 0;
}

static void vfio_luo_unfreeze(struct liveupdate_file_op_args *args)
{
        /* 因为 freeze 阶段没有对设备做任何"会改变其运行状态"
         * 的动作——只是记录了一份簿记快照——所以 unfreeze
         * 几乎不需要做任何事情:简单地丢弃这份快照即可。
         * 这正是 freeze 阶段"克制"所换来的回报:回滚自然轻量。
         */
}
```

这个设计草图里隐藏着一条重要的设计原则,值得明确点出:
**`freeze` 阶段对设备做的改动越少,`unfreeze` 阶段需要撤销
的东西就越少,整条回滚链(第 11 章范式一)就越简单、越可靠**。
这不是巧合,而是一种值得主动追求的设计目标——**一个理想的
`freeze` 实现,应该让它的逆操作 `unfreeze` 趋于"什么都不用做"**。
对 memfd 而言(只重新捕获 `f_pos`,详见第 9 章 9.5 节),这个
目标已经基本达成;对 VFIO 设备而言,这应当成为指导
`freeze`/`unfreeze` 实现的核心准则——**能不动,就不动;必须
记录的,只记录"此刻"这一瞬间的、转瞬即逝的簿记信息,而不要
触碰任何属于设备本身、本应继续流动的状态**。

#### 13.3.3 `retrieve`:"重新认领"而非"重新创建"

memfd 的 `retrieve`(第 9 章 9.7 节)需要"无中生有"——在
全新的、空白的页缓存里,根据保存下来的 `pfn` 重新把每一个
folio 插入回正确的位置,重建出一个看起来与保存前一模一样的
文件。

VFIO 设备的 `retrieve` 面临的局面则恰恰相反——**它面对的不是
一片空白,而是一台"已经在那里、一直在运行"的设备**。它的
任务不是"重新创造"出这台设备,而是"**重新认领**"它——在新
内核里,凭借 `preserve` 阶段记录的那张"藏宝图"(坐标信息),
重新搭建起 `struct vfio_device`/`struct iommufd_device`/
`struct kvm` 之间的关联关系,使得新内核的软件视角与设备此刻
真实的物理状态严丝合缝地对齐:

```c
static int vfio_luo_retrieve(struct liveupdate_file_op_args *args)
{
        struct vfio_luo_ser *ser = phys_to_virt(args->serialized_data);
        struct vfio_device *vdev;
        struct file *new_file;
        int err;

        /* 第一步:依据保存的 PCI BDF,在新内核里重新定位到
         * 那台"从未停止运行"的物理设备,而不是去初始化一台
         * "新"设备。 */
        vdev = vfio_device_find_by_bdf(ser->pci_bdf);
        if (!vdev)
                return -ENODEV;

        /* 第二步:重新搭建 IOMMU/中断/KVM 关联——这一步必须
         * 以"设备当前真实状态"为准绳,而不是以"保存时的状态"
         * 为准绳,因为 DMA 活动从未中断,设备状态在此过程中
         * 仍在持续演进。这正是"重新认领"与"重新创建"的本质
         * 区别:后者只需要面对一份静态快照,前者却要在重建的
         * 同时,与一个仍在运动的目标精确对齐。*/
        err = vfio_device_luo_reattach(vdev, ser);
        if (err)
                return err;

        new_file = vfio_device_alloc_luo_file(vdev);
        if (IS_ERR(new_file))
                return PTR_ERR(new_file);

        args->file = new_file;
        return 0;
}
```

注释里那句"与一个仍在运动的目标精确对齐"道出了整个设计草图
里最尖锐的难点——这不是一个可以靠"更仔细地编写代码"就能
解决的工程问题,而是一个**根本性的同步问题**:在 `preserve`
记录坐标、`retrieve` 重新认领这两个时刻之间,设备的真实状态
可能已经发生了变化(中断被重新路由、DMA 映射被刷新、电源
状态发生迁移)。`retrieve` 阶段的实现,必须有能力判断"哪些
坐标信息依然有效、哪些需要基于设备的当前真实状态重新推导"——
这是一类传统意义上"先保存后恢复"模型从未需要面对的挑战,也是
为什么本文档反复强调"VFIO 与 memfd 不在同一个难度量级"的
根本原因。

### 13.4 IOMMU FLB 设计草案:为什么 IOMMU 映射应该是"全局共享对象"

第 12 章 12.2 节引用的 `luo_core.c:34-35` 把 `iommu` 与 `vfio`
并列点名——这绝非随意之举。让我们用第 7 章建立起来的 FLB
(File-Lifecycle-Bound)框架去审视一下"IOMMU 页表映射"这个
对象,会发现它与 FLB 机制的设计初衷高度契合。

回忆一下第 7 章 7.1 节论证 FLB 存在必要性时举的例子——"多个
被保存的文件可能共享同一份全局状态,如果让每个文件各自独立
地保存一份,不仅浪费空间,在恢复阶段还可能引发严重的不一致"。
IOMMU 页表映射正是这样一个典型场景:

* **共享性**:同一个 IOMMU 域(`iommu_domain`)/页表,很可能
  被**多个** VFIO 直通设备共同使用(例如,一台虚拟机同时直通
  了一块网卡和一块加速卡,二者通常共享同一个 IOAS/IOMMU 域);
* **生命周期与"使用它的设备"绑定,而非与某一个设备绑定**:
  只要还有任何一个设备依赖这份映射关系,它就不能被释放或
  重建;只有当**最后一个**依赖它的设备完成 `retrieve`/`finish`
  之后,它才真正可以被回收。

这与第 7 章 7.4、7.5 节描绘的 FLB 引用计数状态机——"第一个
文件触发 preserve(outgoing)/懒加载触发 retrieve(incoming),
最后一个文件触发 unpreserve/finish"——可以说是**完美的同构
映射**。让我们模仿 `luo_flb_ops` 的形态,画出一份 IOMMU FLB
的设计草案:

```c
/* drivers/iommu/iommu_luo.c (假想中的文件) */

#define IOMMU_LUO_FLB_COMPATIBLE  "iommu-domain-v1"

struct iommu_luo_flb_state {
        struct iommu_domain *domain;
        /* IOMMU 页表/映射关系的 KHO 序列化坐标 */
        struct kho_vmalloc mappings;
        u64 nr_mappings;
};

static int iommu_luo_flb_preserve(struct liveupdate_flb_op_args *argp)
{
        /* 在"第一个"依赖此 IOMMU 域的 VFIO 设备触发 preserve
         * 时被调用一次(详见第 7 章 7.4 节状态机:0 -> 1 触发)。
         * 职责:把整个 IOMMU 域的页表映射关系序列化、通过
         * kho_preserve_vmalloc 这样的机制(第 8 章 8.3 节)
         * 交给下一个内核。 */
        ...
}

static int iommu_luo_flb_retrieve(struct liveupdate_flb_op_args *argp)
{
        /* 懒加载语义(第 7 章 7.5 节):只有当第一个设备真正
         * 尝试 retrieve 时,才反序列化、重建 iommu_domain 及
         * 其映射关系——避免在用户态明确表示"需要它"之前,
         * 就预先做这件代价高昂的工作。 */
        ...
}

static const struct liveupdate_flb_ops iommu_luo_flb_ops = {
        .preserve   = iommu_luo_flb_preserve,
        .unpreserve = iommu_luo_flb_unpreserve,
        .retrieve   = iommu_luo_flb_retrieve,
        .finish     = iommu_luo_flb_finish,
};

static struct liveupdate_flb iommu_luo_flb = {
        .ops        = &iommu_luo_flb_ops,
        .compatible = IOMMU_LUO_FLB_COMPATIBLE,
};

/* 由 vfio_luo_handler 通过 liveupdate_register_flb() 声明依赖
 * (详见第 7 章 7.7 节对 flb_list 的讨论)。 */
```

这份草案里最值得强调的一点是:**它不是凭空发明出来的新模式,
而是把第 7 章已经在 memfd 场景下被反复验证过的"引用计数 +
outgoing/incoming 双状态 + 懒加载"FLB 框架,原封不动地套用到
了一个全新的、更复杂的对象(IOMMU 域)上**。这恰恰证明了
LUO 框架本身的可扩展性设计是成功的——它把"如何安全地共享
全局状态"这个困难问题,在框架层面一次性解决了,使得后来者
(无论是 IOMMU、中断子系统,还是其它任何需要被多个文件共享
的全局对象)都可以直接复用这套现成的脚手架,而不必各自重新
发明。

### 13.5 依赖链全景图:vfio file handler 与多个 FLB 之间的协作关系

把上面讨论过的各个角色拼接起来,我们可以画出一张更完整的
依赖关系全景图——它展示了"一个 VFIO 直通设备被保存"这件事,
实际上会牵动多少个子系统协同动作:

```
                    用户态: preserve_fd(vfio_device_fd, token)
                                    │
                                    ▼
                      ┌─────────────────────────────┐
                      │  vfio_luo_handler            │  compatible = "vfiofd-v1"
                      │ (struct liveupdate_file_     │  (13.1、13.2 节)
                      │  handler,本章设计草案核心)    │
                      └──────────────┬──────────────┘
                                     │ 通过 flb_list 声明依赖
                                     │ (第 7 章 7.7 节)
              ┌──────────────────────┼──────────────────────┬─────────────────────┐
              ▼                      ▼                      ▼                     ▼
   ┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐ ┌──────────────────┐
   │ IOMMU FLB           │ │ 中断路由 FLB        │ │ KVM 关联 FLB        │ │ (未来可能出现的    │
   │ "iommu-domain-v1"   │ │ "irq-routing-v1"    │ │ "kvm-vfio-assoc-v1" │ │  其它共享状态 FLB)│
   │ (13.4 节设计草案)    │ │ (设想)              │ │ (设想)              │ │                  │
   │                     │ │                     │ │                     │ │                  │
   │ 管理:页表映射、       │ │ 管理:中断路由表、     │ │ 管理:vfio_device   │ │                  │
   │ DMA 域归属关系        │ │ MSI/MSI-X 配置       │ │ 与 kvm 实例的       │ │                  │
   │                     │ │                     │ │ 绑定关系            │ │                  │
   └─────────────────────┘ └─────────────────────┘ └─────────────────────┘ └──────────────────┘
              ▲                      ▲                      ▲                     ▲
              │                      │                      │                     │
              └──────────────────────┴──────────────────────┴─────────────────────┘
                       多个 VFIO 设备(以及它们各自对应的
                       luo_file 实例)可能共享同一份 FLB 状态
                          (第 7 章 7.1 节"共享性"论证)
```

这张图最重要的启示在于:**一个 VFIO 设备的 `preserve`,远不只是
"一个 file handler 调用一次回调"那么简单——它实际上是在向
LUO 声明一整张"依赖关系网"**。`luo_flb_file_preserve()`(第 7
章 7.4 节)那个看似简单的"遍历 `handler->flb_list`、对每个
依赖项调用 `liveupdate_flb_get_outgoing`/递增引用计数"的循环,
在 VFIO 场景下将真正发挥它的威力——它需要确保:无论这台设备
依赖多少个全局共享对象、这些对象又被多少其它设备共同依赖,
整个系统在 reboot 前后都能保持引用关系的精确一致、不多不少。

这也是为什么第 12 章会断言"VFIO 集成不是一个孤立的子系统能够
单独完成的"——上图中的每一个 FLB 角色(IOMMU、中断、KVM),
都对应着一个独立维护、独立演进、有着自己复杂内部逻辑的内核
子系统。`vfio_luo_handler` 要做的,与其说是"实现自己的保存
恢复逻辑",不如说更像是"**牵头组织一场跨子系统的协同演练**"——
而 LUO 的 FLB 框架,恰恰为这场演练提供了一套统一的、经过验证
的"集合点"协议。

### 13.6 与现有 `vfio_migration_ops`/`VFIO_DEVICE_STATE_*` 的逐项对比

延续第 12 章 12.4-12.5 节的分析,我们把"假想中的 LUO 集成
方案"与"现有的实时迁移框架"做一次更细致的逐项对比,这有助于
厘清"哪些部分有可能复用、哪些部分注定需要全新设计":

| 维度 | `vfio_migration_ops`(现有,服务于实时迁移) | LUO file handler 设计草案(本章推演,服务于热升级) |
|---|---|---|
| **核心数据载体** | `migration_set_state` 返回的 `struct file *` ——一条用于 `read`/`write` 的数据流管道 | KHO 序列化结构(`struct vfio_luo_ser`)——一份通过物理内存直接移交的"坐标元数据",而非数据流 |
| **状态机起点/终点** | `RUNNING` → ... → `STOP_COPY`/`RESUMING` → `RUNNING`(两端各自独立运行) | 理想状态下:`RUNNING` → (近乎无感知的"freeze 一瞬") → `RUNNING`(同一台设备,从未真正离开 `RUNNING`) |
| **谁负责"重建"设备** | 目的端的迁移驱动从数据流里完整重建设备状态(`RESUMING` 状态) | "重建"的对象是**软件管理结构**而非设备本身——设备始终在原地运行,`retrieve` 只负责把管理结构与它重新对齐 |
| **DMA 是否中断** | 通常需要经历 `STOP`/`STOP_COPY` 这样的静止期,DMA 会短暂中断 | 设计目标是"贯穿整个切换过程不中断"(`luo_core.c:14-15`) |
| **失败兜底机制** | `VFIO_DEVICE_RESET` 把设备拉回 `RUNNING`(12.4.2 节) | 与第 11 章范式三相通:"宁可泄漏,把恢复责任交给可靠的外部机制(reboot)";同时设备复位作为"万不得已"的最后手段依然适用 |
| **能否直接复用** | —— | 三个回调中的 `migration_get_state`/`migration_get_data_size` 所依赖的"设备状态自描述能力"具有复用价值;但 `migration_set_state` 隐含的"目的地是另一台机器"前提与 LUO 场景错位,难以直接复用其状态转换逻辑 |
| **能力协商机制** | `VFIO_DEVICE_FEATURE_MIGRATION` —— 用户态据此查询设备是否支持迁移、支持哪些可选状态 | 设想中需要一个相应的`VFIO_DEVICE_FEATURE_LIVE_UPDATE`(或类似)能力位——用户态需要能够查询"这台设备是否支持、以及在多大程度上支持'近乎无感知'的热升级模式",并据此决定是否将其纳入 LUO 保存范围 |

这张表格最后一行格外值得展开——**能力协商机制的缺失,可能
是横亘在"理论设计"与"现实落地"之间最现实的一道门槛**。
不同厂商、不同代际的硬件设备,对"在 DMA 不中断的前提下被
重新认领"这件事的支持程度必然参差不齐——有些新一代设备的
固件可能天然具备这种能力,有些老旧设备则可能完全不具备。
LUO + VFIO 集成方案要走向现实,大概率需要一套类似于
`VFIO_DEVICE_FEATURE_MIGRATION` 的能力发现与协商机制,让
用户态的编排程序(例如下一章将要讨论的 QEMU/libvirt)能够
**对每一台具体的设备做出"它能否、以及该如何参与这次热升级"
的精确判断**,而不是采取"一刀切"的策略。

### 13.7 关键技术挑战清单与可能的应对思路

把本章的设计推演收束成一份"挑战与应对思路"清单,作为本章
的总结,也为第 14 章讨论 QEMU 的落地方案预留下关键的"待确认
事项"接口:

| 挑战 | 具体表现 | 可能的应对思路(推演性质,非定论) |
|---|---|---|
| **设备持续运行下的状态一致性** | `preserve`(记录坐标)与 `retrieve`(重新认领)之间,设备真实状态可能已发生改变(13.3.3 节) | 借鉴 IOMMU/中断子系统已有的"世代号(generation)/版本戳"机制,让 `retrieve` 能够判断哪些坐标依然有效、哪些需要基于实时状态重新推导,而不是盲目信任保存时的快照 |
| **DMA 不中断的硬约束** | "近乎不冻结"对 `freeze`/`unfreeze` 的设计提出了远超 memfd 的克制要求(13.3.2 节) | 把 `freeze` 的职责严格限定在"为转瞬即逝的软件簿记信息拍快照",绝不触碰任何属于设备本身、仍需持续流动的状态——让 `unfreeze` 趋于"无事可做" |
| **复位语义与"原地复活"语义的并存** | 现有 `vfio_migration_ops` 框架建立在"设备终将复位重建"的假设之上(第 12 章 12.5.2 节),而 LUO 想打破这一假设 | 不必非此即彼——可以为设备设计"两条腿走路"的能力声明:不支持"原地复活"模式的旧设备,退化为传统的"复位-重新初始化"路径(性能代价高但逻辑简单);支持的新设备,走 LUO 的"近乎无感知"路径 |
| **能力协商缺位** | 不同硬件对"参与无中断热升级"的支持程度参差不齐(13.6 节) | 引入类似 `VFIO_DEVICE_FEATURE_MIGRATION` 的能力发现接口,让用户态可以对每台设备做出精确判断,支持"部分设备走 LUO 路径、部分设备走传统路径"的混合编排策略 |
| **跨子系统协同的复杂度** | 一个 VFIO 设备的保存,牵动 IOMMU/中断/KVM 等多个独立演进的子系统(13.5 节依赖链图) | 充分发挥 LUO 既有的 FLB 框架(第 7 章)作为"统一集合点"的作用——每个子系统只需要按照已经过 memfd 验证的协议接入,不需要彼此之间发明私有的协调机制 |
| **失败影响半径过大** | 失败可能直接导致生产环境虚拟机失去直通设备(第 12 章 12.5.3 节类比讨论) | 延续第 11 章确立的错误处理范式——能在"旧内核仍然完整运行"阶段精确回滚的(范式一),就不惜代价做到精确回滚;一旦跨过了不可逆的边界,坦然采用"宁可泄漏、诉诸可靠的复位机制"这一更诚实的策略,而不要试图在一个已不可信的环境里强行"自愈" |

### 13.8 小结

本章我们做了一件"知其不可而为之、却又收获满满"的事情——
在明知道 VFIO 集成 LUO 至今尚未落地的前提下,依然尝试动手
画出一份完整的设计草案。回顾整个推演过程,几个收获值得在
这里再次强调:

* 我们在 `include/linux/liveupdate.h:90` 这行不起眼的文档
  注释里,发现了一个意味深长的"伏笔"——`"vfiofd-v1"` 这个
  从未被使用过的 compatible 字符串,如同一张写在地基里的
  "预留车位"标牌,昭示着设计者从一开始就为这一天留好了
  位置;
* 我们尝试为 `vfio_luo_file_ops` 的八个回调逐一画出设计
  草图,过程中越发清晰地认识到:**真正的难度并非来自"如何
  填写这张回调表",而是来自"如何在'设备完全静止'与'设备
  完全不受打扰'这两个极端之间,找到一条可以被精确描述、
  可靠验证的中间路径"**——这正是 `preserve` 需要保存
  "坐标"而非"数据"、`freeze` 需要追求"近乎无为"、`retrieve`
  需要"重新认领"而非"重新创造"这三个核心设计决策共同
  指向的同一个本质;
* 我们把第 7 章已经在 memfd 场景下被验证、被信赖的 FLB
  框架,原封不动地套用到了一个全新的、复杂得多的对象——
  IOMMU 域——之上,证明了 LUO 框架本身的可扩展性设计是
  成功的、稳健的、值得信赖的;它把"如何安全共享全局状态"
  这一困难问题,在框架层面一次性、一劳永逸地解决了;
* 最后,我们诚实地把现有 `vfio_migration_ops` 框架与设计
  草案做了逐项对比,既看到了二者在设计哲学上的相通之处
  (P2P 静止状态、ERROR 兜底机制、确定性状态转换路径——
  这些都与 LUO 的设计原则遥相呼应),也直面了二者在"前提
  假设"上的根本错位——这种"既相通又错位"的微妙关系,
  恰恰是创新性工作最真实的写照:**全新的方案从来不是凭空
  出现的,而是站在已有成熟机制的肩膀上,精确地识别出"哪里
  可以借力、哪里必须另辟蹊径"**。

带着这份对"VFIO 一侧还差什么"的清醒认知,下一章我们将把
视野进一步扩大——从"内核如何支持"切换到"上层 hypervisor
如何使用"。我们将探讨一个更贴近现实落地场景的问题:假如
VFIO 集成 LUO 真的实现了,**QEMU 应该如何驾驭这一整套机制,
为云环境中的虚拟机用户提供一种全新的、近乎无感知的宿主机
热升级体验**?

---

## 第 14 章:QEMU 利用 LUO 实现热升级的完整方案

> 本章是整篇文档"自底向上"叙事的最后一站——我们已经从最底层
> 的 KHO 内存移交机制(第 2 章),逐步走到了 LUO 的核心架构
> (第 3-8 章)、具体范例实现(第 9 章)、端到端测试(第 10 章)、
> 错误处理哲学(第 11 章),又在第 12、13 章里探讨了"如何把
> 这一切扩展到 VFIO 设备"。现在,让我们把视角抬升到最顶层——
> **一个真实的、运行着客户虚拟机的云 hypervisor 场景中,QEMU
> 这样的虚拟机监控器(VMM),要如何驾驭这一整套机制,为虚拟机
> 用户提供一种全新的、近乎无感知的宿主机内核热升级体验**?
>
> 与第 13 章相同的声明依然适用:由于 VFIO 对 LUO 的支持尚未
> 落地(第 12 章已详述),本章描绘的"QEMU 热升级方案"同样是
> 一份**基于本文档已建立的全部理解推演出的展望性设计**,而非
> 对现有代码的解读。它的价值在于——**把前面十三章学到的全部
> 知识,串联成一条服务于真实业务场景的完整主线**,让读者在
> 合上这篇文档时,脑海里留下的不是一堆孤立的技术细节,而是
> 一幅"这一切究竟是为了解决什么问题"的完整图景。

### 14.1 起点:重新定义"热升级"——它到底"热"在哪里

在深入流程设计之前,有必要先精确定义本章讨论的"热升级"
(hot upgrade)究竟意味着什么,因为这个词在不同语境下可能
指代完全不同的事情:

* 它**不是**"虚拟机实时迁移"(live migration)——后者是把
  一台虚拟机从物理机 A 搬到物理机 B,物理机 A 上的内核可以
  继续保持旧版本不变;
* 它**不是**"虚拟机内部操作系统的热补丁"(kernel live
  patching)——那解决的是客户虚拟机内部的问题,与宿主机内核
  毫无关系;
* 它指的是:**让宿主机(host)的 Linux 内核完成一次完整的
  版本升级(例如从 6.x 升级到 6.y,或打上一个安全补丁后
  重新编译内核),而宿主机上正在运行的客户虚拟机——包括它们
  通过 VFIO 直通使用的物理设备——在整个过程中持续运行,
  几乎感知不到任何中断**。

这正是 `luo_core.c` 开篇 DOC 注释里那句话的真正含义——
"the primary use case driving this work is supporting live
updates of the Linux kernel when it is used as a hypervisor in
cloud environments"(`luo_core.c:17-18`)。"热"这个字,修饰的
是**宿主机内核的版本切换过程**,而"几乎无感知"则是**对运行
在其上的虚拟机**的承诺。

理解了这一点,我们就能立刻明白这件事为什么如此有价值——在
今天的云计算行业里,宿主机内核的版本升级(无论是为了修复
安全漏洞、获得新特性,还是仅仅为了与上游保持同步)始终是
一件令运维团队头疼的事情:它要么需要把虚拟机实时迁移到其它
宿主机(代价高昂、需要预留冗余容量、且并非所有虚拟机都能
被安全迁移——例如那些直通了特殊硬件的虚拟机),要么需要
让虚拟机经历一次完整的关机/重启(对客户业务造成实实在在的
中断)。LUO 描绘的图景,提供了第三条路——**让宿主机自己
"金蝉脱壳",而虚拟机浑然不觉**。

### 14.2 端到端流程时序图:从触发到完成的完整旅程

让我们试着画出一幅完整的端到端时序图——它把第 5、6、9、10
章里讨论过的具体 LUO 操作序列,放回到一个真实的、由 QEMU 与
宿主机管理面共同驱动的业务场景中:

```
 时间线 ───────────────────────────────────────────────────────────────────►

 阶段 0:准备                  阶段 1:声明保存             阶段 2:冻结与切换       阶段 3:恢复与收尾
 (旧内核中)                    (旧内核中)                  (旧→新内核)            (新内核中)
┌──────────────────┐    ┌────────────────────┐   ┌──────────────────┐   ┌─────────────────────┐
│                  │    │                    │   │                  │   │                     │
│ 宿主机管理面      │    │ QEMU(每台 VM 一份  │   │ liveupdate_reboot│   │ QEMU 重新启动       │
│ (libvirt/        │    │  独立 LUO session) │   │ + kexec 跳转     │   │ (作为同一个进程的   │
│  orchestrator)   │    │                    │   │ (第 11 章)       │   │  延续,详见 14.5 节) │
│  检测到新内核     │    │  1. open           │   │                  │   │                     │
│  镜像就绪,        │    │     /dev/liveupdate│   │ 1. 用户态触发    │   │  1. open            │
│  开始编排流程     │    │  2. CREATE_SESSION │   │    reboot(       │   │     /dev/liveupdate │
│                  │    │     (会话名 = VM   │   │    KEXEC)        │   │  2. RETRIEVE_SESSION│
│  对每台 VM 并行   │    │     的稳定 ID)     │   │                  │   │     (按名字找回     │
│  发出"准备好      │    │  3. 对每个需要      │   │ 2. 内核侧:       │   │     之前的会话)     │
│  迁移"信号        │    │     保存的资源:     │   │   luo_session_   │   │                     │
│                  │    │     PRESERVE_FD    │   │   serialize →    │   │  3. 对每个之前      │
│ (第 5 章          │    │     - guest 内存    │   │   luo_file_freeze│   │     preserve 的     │
│  luo_session_    │    │       (memfd, 第 9  │   │   (第 11 章      │   │     token,调用      │
│  create 流程)     │    │       章已详述)     │   │   11.2 节)       │   │     RETRIEVE_FD     │
│                  │    │     - VFIO 设备 fd  │   │                  │   │     (按 token 精确   │
│                  │    │       (第 13 章设计  │   │ 3. kho_finalize  │   │     恢复,第 6、10   │
│                  │    │       草案)         │   │   (第 2 章)      │   │     章已详述)       │
│                  │    │                    │   │                  │   │                     │
│                  │    │  此刻 QEMU 进程     │   │ 4. kexec 跳转    │   │  4. 重新驱动 VM     │
│                  │    │  仍在正常服务,      │   │   (新内核启动)   │   │     的 vCPU,        │
│                  │    │  虚拟机毫无感知      │   │                  │   │     恢复全速运行     │
└──────────────────┘    └────────────────────┘   └──────────────────┘   └─────────────────────┘
                                  │                                               ▲
                                  │      整个虚拟机及其 VFIO 直通设备             │
                                  └───────────────────────────────────────────────┘
                                       全程持续运行,DMA 不曾中断
                                       (第 12 章 12.5.1 节"原地不动"理念)
```

这张图最值得细细品味的地方,是**它与传统方案在"虚拟机生命
周期"这条时间线上画出的轨迹截然不同**——在实时迁移方案里,
虚拟机的运行轨迹会出现一段"黑盒切换期"(数据通过网络传输、
目的端重新初始化);在传统重启方案里,轨迹会出现一段彻底的
"空白期"(虚拟机完全停止);而在 LUO 描绘的图景里,**虚拟机
运行的轨迹是一条连续不断的实线**——唯一发生根本性变化的,是
"管理这条实线背后那一整套软件基础设施"所运行的内核版本。

### 14.3 ioctl 调用序列伪代码:QEMU 与 `/dev/liveupdate` 的对话

把上面的流程图转译成更具体的、QEMU 进程视角下的伪代码序列——
这本质上是把第 4 章详述的 ioctl 接口、第 6 章的 `preserve_fd`
流程、第 10 章 `luo_kexec_simple.c` 的端到端范例(已经手把手
教过我们如何与这套接口对话),组合成一套服务于真实虚拟机
管理场景的调用模式:

```c
/* QEMU 进程内,假想中的"准备热升级"处理逻辑(展望性伪代码) */

int qemu_prepare_for_host_live_update(struct qemu_vm_state *vm)
{
        struct liveupdate_ioctl_create_session sess_args = {};
        int luo_fd, session_fd;
        int err;

        /* 第 1 步:打开 LUO 设备——遵循第 3 章讨论过的"单例
         * 设备模型",通常由宿主机的某个特权代理进程统一持有,
         * QEMU 可能通过受控的 fd 传递机制获得访问权限。 */
        luo_fd = open("/dev/liveupdate", O_RDWR);
        if (luo_fd < 0)
                return -errno;

        /* 第 2 步:为这台虚拟机创建一个独立的 session——
         * 会话名采用虚拟机的稳定标识(例如 UUID),这样新内核
         * 启动后,QEMU 能够通过 LIVEUPDATE_IOCTL_RETRIEVE_SESSION
         * (第 5 章 5.x 节)精确地找回属于"这一台"虚拟机的
         * 全部状态,即使宿主机上同时运行着成百上千台虚拟机。 */
        sess_args.size = sizeof(sess_args);
        strscpy((char *)sess_args.name, vm->uuid_str, sizeof(sess_args.name));
        err = ioctl(luo_fd, LIVEUPDATE_IOCTL_CREATE_SESSION, &sess_args);
        if (err)
                goto out_close_luo;
        session_fd = sess_args.fd;

        /* 第 3 步:逐一保存这台虚拟机依赖的全部资源。
         *
         * 资源 A——客户内存(guest RAM):通常以 memfd 的形式
         * 存在(无论是通过 memfd_create 还是通过类似
         * memfd-backed hugetlbfs 的机制),直接复用第 9 章
         * 详细解读过的 memfd LUO 路径,这部分在今天的内核里
         * 已经是完全成熟、可以直接使用的能力。 */
        for_each_guest_memory_region(vm, region) {
                preserve_fd(session_fd, region->memfd,
                            qemu_make_token(QEMU_TOKEN_GUEST_RAM, region->slot));
        }

        /* 资源 B——VFIO 直通设备:依赖第 13 章描绘的、目前
         * 尚未落地的 vfio_luo_handler。token 编码了"这是哪台
         * 虚拟机的哪一个直通设备",确保新内核里能够按图索骥、
         * 精确恢复每一个设备与虚拟机之间的绑定关系
         * (第 6 章 6.x 节"按 token 精确索引"机制)。 */
        for_each_passthrough_device(vm, vdev) {
                preserve_fd(session_fd, vdev->fd,
                            qemu_make_token(QEMU_TOKEN_VFIO_DEV, vdev->index));
        }

        /* 资源 C——其它需要延续的运行时状态:vCPU 寄存器快照、
         * 中断控制器状态、设备模型(device model)的内部状态等。
         * 这些数据量通常不大,既可以编码进某个 memfd 里通过
         * 既有路径保存,也可能催生出全新的、专门面向"虚拟机
         * 运行时状态"的 LUO file handler。 */
        preserve_fd(session_fd, vm->runtime_state_memfd,
                    qemu_make_token(QEMU_TOKEN_RUNTIME_STATE, 0));

        /* 第 4 步:关闭 session fd——注意第 10 章 10.3 节强调
         * 过的关键点:这里 *不能* 简单地一关了之。关闭一个
         * "outgoing"会话会触发 luo_session_release 路径
         * (第 11 章 11.3.4 节),其语义是"放弃保存"。QEMU
         * 需要让这个 fd 保持存活,直到 reboot() 真正发生——
         * 这通常意味着把它的生命周期与宿主机的"协调代理进程"
         * 绑定,而不是与 QEMU 进程本身的某个局部作用域绑定。 */
        vm->luo_session_fd = session_fd; /* 保留,等待 reboot */

        return 0;

out_close_luo:
        close(luo_fd);
        return err;
}

/* 新内核启动后,QEMU(作为同一个逻辑虚拟机的延续而重新启动)
 * 执行的"恢复"逻辑(展望性伪代码) */
int qemu_resume_after_host_live_update(struct qemu_vm_state *vm)
{
        struct liveupdate_ioctl_create_session sess_args = {};
        int luo_fd, session_fd, err;

        luo_fd = open("/dev/liveupdate", O_RDWR);

        /* 用与之前完全相同的名字(虚拟机 UUID)调用
         * RETRIEVE_SESSION——第 5 章已经讲过,LUO 会在内部
         * 完成"按名字在 incoming 链表中查找、首次访问时触发
         * 反序列化"的全部工作(第 11 章 11.4 节"反序列化"
         * 流程正是在此刻被惰性触发)。 */
        strscpy((char *)sess_args.name, vm->uuid_str, sizeof(sess_args.name));
        ioctl(luo_fd, LIVEUPDATE_IOCTL_RETRIEVE_SESSION, &sess_args);
        session_fd = sess_args.fd;

        /* 按照与 preserve 时完全相同的 token 编码方案,逐一
         * 取回每一份资源——guest RAM、VFIO 设备、运行时状态。
         * 第 10 章 10.5 节 luo_multi_session.c 范例已经展示过
         * 这种"按 token 精确恢复多个文件"的完整模式。 */
        for_each_preserved_token(vm, token) {
                int fd = retrieve_fd(session_fd, token);
                qemu_reattach_resource(vm, token, fd);
        }

        /* 全部资源恢复完毕、虚拟机模型重新拼装完整后,
         * 调用 SESSION_FINISH——这是第 11 章 11.3 节详述的
         * "两阶段提交"的发起点:can_finish 先在所有资源上
         * 达成一致("我已经被正确接管,可以释放 LUO 侧的引用
         * 了"),finish 再批量执行最终的清理与归还。 */
        ioctl(session_fd, LIVEUPDATE_SESSION_FINISH, &(struct liveupdate_session_finish){
                .size = sizeof(struct liveupdate_session_finish),
        });

        close(session_fd);
        close(luo_fd);

        /* 至此,虚拟机已经在新内核的管理之下恢复全速运行——
         * 而对于运行在它内部的客户操作系统与应用而言,
         * 这一切几乎不曾发生过。 */
        qemu_resume_vcpus(vm);
        return 0;
}
```

这份伪代码序列,本质上是把本文档第 4-6、9-10 章里逐一精读
过的 ioctl 接口与调用模式——`CREATE_SESSION`/`PRESERVE_FD`/
`RETRIEVE_SESSION`/`RETRIEVE_FD`/`SESSION_FINISH`,以及
"token 精确索引"、"session 命名约定"、"不能过早关闭 session
fd"这些贯穿始终的设计要点——全部重新组织进了一个**服务于
真实虚拟机管理场景**的连贯故事里。如果说第 10 章的 selftest
走读教会了我们"这套接口长什么样、怎么用",那么这一节想要
传达的是"**它们被设计出来,究竟是为了在怎样的真实场景里,
拼出一幅怎样的图景**"。

### 14.4 与传统方案的对比:停机时间、依赖、状态完整性

把 LUO 驱动的热升级方案,与业界现有的两类主流方案——虚拟机
实时迁移(live migration)与基于 CRIU 的进程级检查点恢复——
放在同一张表里横向对比,可以更清晰地看出它的独特定位:

| 维度 | 虚拟机实时迁移(live migration) | CRIU(用户态进程检查点/恢复) | LUO 驱动的宿主机热升级(本章方案) |
|---|---|---|---|
| **停机时间** | 通常为毫秒级到秒级"黑盒切换期"(取决于内存脏页收敛速度);理论上可以做到亚秒级,但伴随大量预拷贝流量 | 取决于进程状态大小与磁盘/网络 I/O 速度,通常是秒级到分钟级 | 理论目标是"几乎无感知"——虚拟机及其直通设备全程运行,只有管理它们的内核版本发生切换(第 14.1、14.2 节) |
| **是否需要冗余容量** | 需要——必须有"目的端"物理机随时待命,这部分容量在大规模部署中是不小的成本开销 | 不一定,但若涉及跨机器恢复同样需要 | **不需要**——升级发生在同一台物理机上,这是相对实时迁移最直接的成本优势 |
| **网络依赖** | 强依赖——状态数据需要通过网络在两台机器间传输,带宽与时延直接影响切换速度与成功率 | 视场景而定 | **无网络依赖**——一切都在本机完成,通过 KHO 在物理内存中直接移交(第 2 章) |
| **对特殊硬件(如 VFIO 直通设备)的支持** | 历史上是公认的难题——直通设备的状态难以通过传统迁移路径完整保存与恢复,很多生产环境直接将"使用了直通设备"作为"不可迁移"的标签 | 同样困难——CRIU 主要面向用户态进程状态,对内核管理的硬件设备状态无能为力 | 这正是 LUO 试图正面攻克的核心场景(第 12、13 章);如果成功,将直接补上实时迁移方案最大的一块短板 |
| **状态完整性保障** | 依赖精心设计的"预拷贝-停机拷贝"协议,理论上可以做到完整,但实现复杂、边界情况众多 | 依赖用户态状态的可观测性与可重建性,对内核态资源(文件描述符指向的复杂对象、设备状态)支持有限 | 由内核内置机制(KHO + LUO 框架)直接提供"按位精确"的保存与恢复保障(第 2、8、9 章已详述其严谨性) |
| **适用边界** | 适用于"虚拟机需要换一台物理机运行"的场景(故障转移、负载均衡、物理机维护) | 适用于无状态或弱状态依赖的用户态服务 | 适用于"物理机本身需要升级,但虚拟机想留在原地"的场景——与实时迁移形成互补而非替代关系 |

这张表格最后一行想要表达的观点尤其值得强调:**LUO 驱动的
热升级方案,并不是要"取代"实时迁移或 CRIU——它们解决的是
不同维度的问题,适用于不同的场景组合**。一个成熟的云计算
基础设施,完全可以(也应该)同时拥有这几种能力,根据具体
场景按需选用:

* 物理机硬件需要下线维护(磁盘故障、内存故障)→ 实时迁移;
* 物理机内核需要升级,虚拟机希望原地不动地"穿越"这次升级 →
  LUO 驱动的热升级(本章方案);
* 用户态服务需要在容器编排场景下做快速的检查点/恢复 → CRIU。

三者并行不悖、各擅胜场——这正是"workload-agnostic"(第 1
章已经强调过 LUO 设计哲学中的这一关键词,见 `luo_core.c:19`)
所追求的真正图景:**不是用一种方案统治所有场景,而是为每一类
场景,提供一种"恰如其分"的解法**。

### 14.5 落地这一方案,还需要哪些上下游配合

从"内核已经具备 LUO 框架"到"用户能够享受到无感知的宿主机
热升级体验",中间还隔着一条相当长的协作链条。让我们沿着
"从内核到用户"的方向,逐层盘点还需要哪些环节就位:

#### 14.5.1 内核侧:VFIO/IOMMU/中断/KVM 的 LUO 接入(第 12、13 章已详述)

这是整条链条中最基础、也最具挑战性的一环——第 12 章已经
给出了详尽的差距分析,第 13 章给出了一份设计草案及其面临的
关键技术挑战清单。在它真正落地之前,**任何依赖 VFIO 直通
设备的虚拟机,都无法纳入 LUO 热升级的覆盖范围**——这是当前
最大的阻塞点。

#### 14.5.2 QEMU 侧:成为"可被无缝重启的进程"

QEMU 自身需要具备一种特殊的能力——**当宿主机内核完成 kexec
切换、重新启动后,以"延续者"而非"新建者"的姿态重新加入到
虚拟机的运行中**。这要求 QEMU:

* 能够通过本章 14.3 节描绘的 ioctl 序列与 LUO 交互,精确地
  保存与恢复自身管理的全部资源(guest RAM、设备模型状态、
  vCPU 寄存器等);
* 具备一套"自我识别"机制——这与第 10 章 10.3 节深入分析过
  的 `luo_test_utils.c` 那个"自我识别阶段"测试基础设施在
  精神上是相通的:新启动的 QEMU 进程需要知道"我是不是
  应该接管一台已存在的虚拟机,以及具体是哪一台";
* 能够正确处理"vCPU 此刻应当处于什么状态"这一棘手问题——
  在传统的虚拟机启动流程中,vCPU 从"复位状态"开始;而在
  这里,vCPU 应当从"宿主机内核切换前的最后一刻"无缝衔接
  下去,这对 QEMU 的状态管理基础设施提出了与实时迁移目的端
  类似、但时间窗口要求更为苛刻的要求。

#### 14.5.3 libvirt/管理面:编排"舰队级"的升级流程

在真实的云环境里,一台物理机上往往同时运行着数十甚至上百台
虚拟机,而一次内核升级往往需要覆盖一整个机房、一整个可用区
的成百上千台物理机。这就要求管理面(libvirt 及更上层的云
编排系统)具备:

* **批量编排能力**——按照既定的策略(例如"先小流量灰度
  验证,再逐步扩大范围")依次触发每台物理机的热升级流程,
  并能够在出现异常时及时暂停、回滚整体进度;
* **能力探测与降级策略**——结合第 13 章 13.6 节讨论过的
  "能力协商机制"展望,管理面需要能够查询"这台物理机上的
  哪些虚拟机、哪些直通设备真正具备参与 LUO 热升级的条件",
  对不具备条件的虚拟机自动退化到传统方案(实时迁移或者
  计划性的重启窗口);
* **可观测性与审计**——记录每一次热升级尝试的完整过程、
  耗时、成功与否,这与第 11 章反复强调的"失败诚实化"
  ——`pr_warn`/错误码精确传递——理念一脉相承:运维团队需要
  在出现问题时,能够清晰地回答"这次升级究竟在哪一步、因为
  什么原因偏离了预期"。

#### 14.5.4 一张全景式的协作分工表

把以上讨论汇总成一张表,呈现"从内核到用户"这条链条上每一环
各自的职责边界:

| 层级 | 核心职责 | 当前就绪程度 |
|---|---|---|
| **内核 - LUO 框架本身** | 提供 session/file/FLB 三层模型、ioctl 接口、KHO 序列化能力 | **已就绪**——本文档第 2-11 章详述的全部机制均已落地并经过 selftest 验证 |
| **内核 - memfd handler** | 支持 guest RAM(以 memfd 形式存在)的保存恢复 | **已就绪**——第 9 章详述的 `mm/memfd_luo.c` 是当前唯一已落地的 file handler 范例 |
| **内核 - VFIO/IOMMU/中断/KVM 集成** | 支持直通设备在"近乎无感知"前提下的状态保存恢复 | **尚未就绪**——这是第 12、13 章重点分析的关键阻塞点 |
| **QEMU** | 成为"可被无缝重启延续的进程",驱动与 `/dev/liveupdate` 的交互 | **尚未就绪**——需要新增"自我识别"、"资源精确保存恢复"、"vCPU 无缝衔接"等一整套能力(本节 14.5.2) |
| **libvirt/管理面** | 编排批量升级流程、能力探测与降级、可观测性 | **尚未就绪**——这是一项纯粹面向运维场景的工程任务,技术复杂度相对可控,但需要大量与现实部署环境磨合的实践积累 |

这张表格诚实地呈现了现状:**LUO 框架本身——本文档着墨最多
的部分——已经是这条链条中最成熟、最稳固的一环**;而真正
通往"用户能够享受到的产品级能力"的道路,还需要 VFIO 子系统、
QEMU 社区、libvirt/云编排生态共同向前迈出关键的几步。这恰恰
印证了第 12 章末尾的论断——**这个难题的解法,注定不会是
某一个子系统单方面的努力可以达成的**。

### 14.6 小结:从"理解一个子系统"到"看见一整幅图景"

本章我们把视野从"内核如何实现"彻底转向"用户最终将如何
受益",试图回答一个最朴素也最根本的问题——**这一切机制,
最终究竟是为了让谁、在什么场景下、获得怎样的体验**?我们
看到:

* "热升级"这件事的"热",指的是宿主机内核完成版本切换的
  过程,而"无感知",承诺给的是运行在其上、依赖直通硬件的
  虚拟机及其内部的客户业务(14.1 节);
* 14.2、14.3 节描绘的端到端流程图与 ioctl 调用序列,把
  本文档第 4-6、9-11 章里分散讨论过的全部技术细节——session
  生命周期、token 精确索引、两阶段提交、惰性反序列化——
  重新编织进了一个连贯的、服务于真实虚拟机管理场景的故事里;
* 14.4 节的对比分析告诉我们,LUO 驱动的热升级不是要"取代"
  现有方案,而是要"补齐"现有方案在"物理机自身需要升级"
  这一独特场景下的短板,与实时迁移、CRIU 形成互补共存的
  生态;
* 14.5 节的全景式分工表则诚实地揭示了现状——**从"内核框架
  就绪"到"用户体验落地",中间还有相当长的一段路**,需要
  VFIO、QEMU、libvirt 等多个生态共同迈进,而本文档详细解读
  过的 LUO 框架本身,正是这条漫长道路最坚实的第一块基石。

至此,本文档的内容主线已经画上句点——我们从"LUO 解决什么
问题"出发(第 1 章),沿着"它建立在怎样的地基之上"(第 2
章 KHO)、"它自身是怎样的三层架构"(第 3 章)、"用户如何
与它对话"(第 4 章)、"它的三大子系统各自如何运作"(第
5-7 章)、"它的数据如何被精确序列化"(第 8 章)、"一个
真实范例如何把这一切串联起来"(第 9 章)、"端到端的使用
范式与测试是什么样子"(第 10 章)、"它如何应对意外"(第
11 章)的顺序,完整地走过了 LUO 这个子系统的内部世界;又
通过第 12、13、14 三章,把目光投向了它尚未抵达、却已经
明确瞄准的远方——VFIO 设备的支持,以及建立在其上的、能够
真正改变云计算行业格局的虚拟机热升级体验。

接下来的附录部分,将以更易于检索、更适合"按图索骥"的形式,
把本文档中出现过的常见问题、术语、关键函数与数据结构、
参考资料重新归纳整理,作为这份长文档的"快速查阅手册"。

---

## 附录 A:常见问题与设计取舍 FAQ

本附录把贯穿全文、反复出现的一些"为什么这样设计"问题汇总在
一起,以一问一答的形式呈现,方便读者快速回顾关键的设计取舍
逻辑,也方便在向他人讲解 LUO 时直接引用。

**Q1:LUO 和普通的 `kexec` 有什么区别?直接 `kexec` 不行吗?**

普通的 `kexec` 是一次"冷"的内核切换——旧内核的全部状态(
打开的文件、网络连接、运行中的虚拟机、直通设备的映射关系)
在跳转瞬间全部消失,新内核从一片空白开始重新构建一切。LUO
要解决的恰恰是这个问题——它在 `kexec` 跳转之前,有秩序地把
"哪些资源需要被保留、它们的状态是什么"序列化下来,通过 KHO
（第 2 章)把这些信息原封不动地"渡"到新内核,使新内核可以
在极短的时间内"认领"回这些资源,而不必从零开始。可以说,
**`kexec` 提供了"切换"的能力,LUO 则在这个切换过程中架起了
一座"记忆的桥梁"**。

**Q2:为什么 LUO 选择"用户态主导编排、内核侧只提供原语"
这种架构,而不是把整个热升级流程都封装在内核里?**

这是第 3、4 章反复强调的设计取向——LUO 刻意把自己定位成一套
"机制"而非"策略"。判断"哪些资源值得保存"、"按什么顺序保存"、
"保存失败时该如何应对业务"这些问题,本质上属于**应用层面的
策略决策**,不同的工作负载(虚拟机管理程序、内存数据库、
容器运行时)有着完全不同的答案。把这些决策固化在内核里,
既会让内核变得臃肿、僵化,也会限制 LUO 的适用范围。让用户态
拿着一组精炼、稳定、可组合的 ioctl 原语自行编排,正是
"workload-agnostic"(`luo_core.c:19`)理念的具体落地。

**Q3:`outgoing`/`incoming` 这种"双状态"设计是不是有点啰嗦?
直接用一个状态字段表示"当前处于保存还是恢复阶段"不行吗?**

不行——因为同一个内核生命周期里,这两种状态可能**同时存在**。
设想一个连续多次执行热升级的场景:第二次升级发生时,系统里
同时存在"刚从上一次升级中恢复回来、尚待 finish 的 incoming
状态"和"准备迈向下一次升级、正在被构建的 outgoing 状态"——
二者的生命周期有重叠,绝非简单的"非此即彼"。第 5、7 章里
反复出现的 `outgoing`/`incoming` 双链表设计,正是为了精确地
为这种共存关系建模,而不是人为地把它们捏合成一个会引发歧义
的单一状态。

**Q4:为什么 `struct luo_file_set`、FDT 序列化结构里,大量
采用"固定大小索引数组 + 指向裸内存块的指针"这种"索引 +
仓库"的混合结构,而不直接把所有数据都塞进 FDT 树里?**

这是第 8 章 8.1 节深入讨论过的核心权衡——FDT(Flattened
Device Tree)被设计用来描述**结构相对固定、层级清晰**的树形
信息,如果让它直接承载数量可能高达成百上千、大小动态变化的
条目数组,会让它的查找效率与内存开销都急剧恶化。"索引 + 仓库"
模式让 FDT 只承载"有多少条、它们在哪里"这类元信息,真正的
"重头戏"——大批量、变长的数据数组——则交给专门为此设计的、
更高效的裸内存块和 `kho_vmalloc` 机制(第 8 章 8.3 节)来
承载。这是"用合适的工具做合适的事"这一朴素工程智慧的体现。

**Q5:`liveupdate_flb_get_incoming`/`get_outgoing` 为什么要
设计成"懒加载"(lazy)?直接在系统启动时把所有 FLB 状态都
准备好,不是更省心吗?**

恰恰相反——"更省心"往往意味着"更浪费"。第 7 章 7.5 节论证
过,很多 FLB 所代表的全局共享状态(例如某个复杂的 IOMMU 域
映射表)的反序列化成本可能相当高昂,而**并不是每一次系统
启动后,这些状态都一定会被用到**(用户可能根本没有保存任何
依赖它的文件)。懒加载用"第一次真正访问时才执行"的策略,把
这部分成本精确地分摊到"真正需要它的那一刻",对所有不需要它
的场景则做到了零开销——这是一种对资源极为节制、对"不要在
不确定是否需要时预先买单"原则身体力行的设计。

**Q6:为什么 LUO 在反序列化失败时选择"宁可泄漏",而不是
想办法实现一个安全的"撤销"?这听起来像是在逃避问题。**

第 11 章 11.4 节用了大量篇幅论证这一点,这里再强调一次它的
核心逻辑:**"撤销"在这个场景下不是"做得好不好"的问题,而是
"能不能做"的问题**。此刻系统已经跨越了一次不可逆的内核切换,
部分状态已经物理性地融入了正在运行的内核数据结构与硬件,真正
"安全"的撤销需要的是一台"时光机",而不是更精巧的代码。在
这个前提下,选择"诚实地承认做不到、把恢复的责任交给一个
真正可靠的外部机制(重启)",远比"假装能够安全撤销、结果在
一个已经不可信的环境里引入新的混乱"更负责任、更稳健。这不是
逃避,而是经过精确成本核算之后的、更成熟的工程判断。

**Q7:VFIO 集成 LUO 既然这么难,为什么不干脆放弃这个目标,
只服务于 memfd 这类"简单"的对象就好了?**

因为 VFIO 设备(尤其是云 hypervisor 场景下的直通设备)恰恰
是"宿主机内核热升级"这件事里最棘手、也最关键的那一块拼图——
正如第 12 章 12.5.3 节的类比所揭示的,正是这些设备的存在,
让"虚拟机能否被无感知地保留下来"成为悬在云计算行业头顶多年
的难题。如果 LUO 只能覆盖 memfd 这类"自带可冻结性"的简单
对象,它固然依然有价值(例如直接服务于内存数据库这样的场景,
正如 `luo_core.c:22-25` 举的 memcached 例子),但距离"成为
云 hypervisor 热升级的基础设施"这一更宏大的目标,还差着
最后,也是最难啃的一块骨头。LUO 选择直面这块骨头,而不是
绕开它,这本身就是一种值得敬佩的工程姿态。

## 附录 B:术语表

按照首次出现的逻辑顺序(而非字母顺序)排列,便于读者按照
本文档的阅读路径查阅:

| 术语 | 含义 |
|---|---|
| **kexec** | Linux 内核提供的"内核内重启"机制——跳过 BIOS/bootloader,直接从一个运行中的内核切换到另一个内核镜像,显著缩短重启耗时 |
| **LUO(Live Update Orchestrator)** | 本文档的主角——构建在 KHO 之上的内核子系统,负责在 `kexec` 切换前后,有秩序地保存与恢复用户指定的资源状态(第 1 章) |
| **KHO(Kexec HandOver)** | LUO 赖以生存的底层基石——一套允许内核把指定的内存区域"原封不动"地移交给下一个内核的机制,通过 FDT 组织元数据(第 2 章) |
| **FDT(Flattened Device Tree)** | 一种树形的、用于描述硬件/元数据信息的二进制格式,KHO 用它来组织跨内核移交的元信息索引(第 2、8 章) |
| **Session(会话)** | LUO 中代表"一组相关的、需要一起管理的被保存资源"的容器抽象,以名字作为唯一标识(第 5 章) |
| **File Handler(文件处理器)** | 为某一类特定的 `struct file`(如 memfd、未来的 vfio 设备 fd)实现 LUO 保存/恢复回调的注册单元,通过 `compatible` 字符串与文件类型匹配(第 6 章) |
| **FLB(File-Lifecycle-Bound objects)** | "文件生命周期绑定的全局共享对象"——为多个被保存文件可能共同依赖的全局状态(如 IOMMU 域)提供统一的引用计数式生命周期管理(第 7 章) |
| **token** | 用户态在 `preserve_fd`/`retrieve_fd` 时指定的一个 64 位标识符,用于在恢复阶段精确地"按图索骥"找回之前保存的特定资源(第 6、10 章) |
| **outgoing / incoming** | LUO 中无处不在的"双状态"设计模式——`outgoing` 表示"准备移交给下一个内核"的状态,`incoming` 表示"从上一个内核接收而来"的状态,二者的生命周期可能重叠共存(第 5、7 章) |
| **freeze / unfreeze** | 文件保存生命周期中的"冻结/解冻"阶段——在 `kexec` 跳转前的最后一刻,给资源状态"拍一张精确的快照",并在需要中止升级时能够镜像式地撤销这一动作(第 6、11 章) |
| **finish** | 文件恢复生命周期的终点——在新内核中确认资源已被正确接管后,释放 LUO 侧持有的全部引用,将控制权完全交还给系统(第 6、11 章) |
| **memfd** | 一种基于匿名内存的文件类型(`memfd_create(2)`),是目前唯一已经落地的 LUO file handler 范例对象(第 9 章) |
| **folio** | 现代 Linux 内存管理中用于表示一组连续物理页面的核心抽象,是 memfd 内容在内存中的实际载体(第 9 章) |
| **seal(密封)** | memfd 的一种安全特性——通过 `fcntl(F_ADD_SEALS)` 限制对文件的某些操作(如缩小、增长、写入),LUO 在保存/恢复时需要精确地保留这些密封状态(第 9 章 9.1、9.3 节) |
| **`__packed` 结构体** | C 语言中通过编译器属性强制取消内存对齐填充的结构体,LUO 的全部跨内核序列化 ABI 结构都采用这种方式以保证字节级精确的布局(第 8 章) |
| **compatible 字符串 + 版本号** | LUO ABI 演进策略的核心——每个 file handler/FLB 都有一个唯一的兼容性标识字符串,版本号的递增意味着布局发生了不兼容的变化,新旧内核据此精确判断是否可以安全互操作(第 8 章 8.4 节) |
| **`kho_vmalloc` / `DECLARE_KHOSER_PTR`** | KHO 提供的、用于在跨内核传递大块变长数据(如 memfd 的 folio 元数据数组)时使用的"页块链表 + 自描述指针联合体"机制(第 8 章 8.3 节) |
| **两阶段提交(can_xxx / xxx 拆分)** | LUO 错误处理中的核心范式之一——把"判断是否可以执行某个不可逆操作"和"真正执行该操作"显式拆分成两个独立阶段,确保多方参与下的原子性(第 6、11 章) |
| **"宁可泄漏,不做不安全 undo"** | LUO 在面对反序列化失败这类"不可逆"错误时采用的设计哲学——主动放弃在已不可信的环境中实施复杂回滚,转而依赖更可靠的外部机制(重启)来恢复系统(第 11 章 11.4 节) |
| **FLB 引用计数状态机** | FLB 框架的核心机制——通过对依赖某全局对象的文件数量计数,在"0→1"时触发 preserve/懒加载 retrieve、在"N→0"时触发 unpreserve/finish(第 7 章) |
| **selftest("自我识别阶段"/"状态接力")** | `tools/testing/selftests/liveupdate/` 中跨 `kexec` 测试基础设施所采用的特殊技巧——测试程序通过命令行参数与 LUO 自身的保存机制相结合,实现跨重启的"自我识别"与状态传递(第 10 章 10.3 节) |
| **`vfio_migration_ops` / `VFIO_DEVICE_STATE_*`** | VFIO 子系统现有的、服务于虚拟机实时迁移的设备状态管理框架,与 LUO 在设计哲学上有相通之处,但前提假设(目的地是另一台机器)与 LUO 场景存在错位(第 12 章) |
| **FLB 依赖链** | 本文档第 13 章设计草案中提出的概念——一个 file handler(如假想中的 `vfio_luo_handler`)可能同时依赖多个 FLB(如 IOMMU、中断路由、KVM 关联),共同构成一张需要协同维护的依赖关系网(第 13 章 13.5 节) |

## 附录 C:关键函数与数据结构索引表

为方便按图索骥地回查源码,本附录把全文引用过的关键函数与
数据结构,按照所在文件分类汇总。**请注意**:本文档写作时所
对应的代码状态下,下列行号是准确的;但内核代码会持续演进,
若发现行号与当前代码略有出入,请以 `compatible` 字符串、
函数名等结构性信息为准重新定位。

### C.1 `kernel/liveupdate/luo_core.c`(核心调度与 ioctl 入口,第 3、4 章)

| 符号 | 行号 | 角色 |
|---|---|---|
| `DOC: Live Update Orchestrator (LUO)` | :9-39 | 全局总览注释,定义 LUO 的目标、应用场景与可接入子系统列表 |
| `luo_early_startup` | :83 | `early_initcall`,完成最早期的初始化 |
| `liveupdate_early_init` | :141 | 解析命令行参数、初始化 incoming 状态 |
| `luo_fdt_setup` | :157 | 构建 outgoing FDT 的根节点结构 |
| `luo_late_startup` | :200 | `late_initcall`,在用户态可以访问 `/dev/liveupdate` 之前完成最终准备 |
| `liveupdate_reboot` | :227-242 | 顶层协调入口——`reboot()` 系统调用触发的总指挥,串联 session/FLB 序列化(第 11 章 11.2.5 节) |
| `liveupdate_enabled` | :251 | 查询 LUO 功能是否通过命令行参数启用 |
| `luo_ioctl_create_session` | :277 | `LIVEUPDATE_IOCTL_CREATE_SESSION` 的处理函数 |
| `luo_ioctl_retrieve_session` | :307 | `LIVEUPDATE_IOCTL_RETRIEVE_SESSION` 的处理函数 |
| `luo_open`/`luo_release` | :337/:355 | `/dev/liveupdate` 的独占打开/关闭逻辑(第 3 章单例设备模型) |

### C.2 `kernel/liveupdate/luo_session.c`(Session 子系统,第 5 章)

| 符号 | 行号 | 角色 |
|---|---|---|
| `luo_session_free`/`alloc`/`insert`/`remove` | :136/(见上文)/:143/:175 | session 对象的基本生命周期管理 |
| `luo_session_release` | :203-227 | session fd 关闭时的收尾逻辑,区分 outgoing/incoming 两条路径(第 11 章 11.3.4 节) |
| `luo_session_preserve_fd`/`retrieve_fd` | :230/:248 | session 级 ioctl 处理,转发到 `luo_file_*` 实现 |
| `luo_session_finish` | :280 | `LIVEUPDATE_SESSION_FINISH` 的处理入口 |
| `luo_session_create`/`retrieve` | :383/:411 | 创建/检索 session 的核心逻辑,内部调用 `luo_session_getfile` |
| `luo_session_setup_outgoing`/`setup_incoming` | :441/:473 | FDT 中 session 子树的搭建(对应第 8 章序列化结构) |
| `luo_session_deserialize` | :513-583 | 新内核中反序列化 incoming session 列表,内含"宁可泄漏"哲学的经典注释(第 11 章 11.4.1、11.4.3 节) |
| `luo_session_serialize` | :584-611 | 旧内核中序列化 outgoing session 列表,内含 `list_for_each_entry_continue_reverse` 镜像回滚链(第 11 章 11.2.4 节) |

### C.3 `kernel/liveupdate/luo_file.c`(File 保存生命周期,第 6 章)

| 符号 | 行号 | 角色 |
|---|---|---|
| `luo_alloc_files_mem`/`free_files_mem` | :177/:197 | 序列化缓冲区(`LUO_FILE_PGCNT` 页)的分配与释放 |
| `luo_preserve_file` | :268-340 | `PRESERVE_FD` 的核心实现——"七层回滚链"的所在地(第 6 章) |
| `luo_file_unpreserve_files` | :372 | 镜像回滚 `preserve_file` 建立的全部关联 |
| `luo_file_freeze_one`/`unfreeze_one`/`__luo_file_unfreeze` | :403/:426/:443 | 单点冻结/解冻操作与"哨兵参数"统一的镜像回滚辅助函数(第 11 章 11.2.1、11.2.2 节) |
| `luo_file_freeze`/`unfreeze` | :492-534/:551-558 | 批量编排冻结/解冻,`goto err_unfreeze` 单出口回滚链(第 11 章 11.2.3 节) |
| `luo_retrieve_file` | :586 | `RETRIEVE_FD` 的核心实现——按 token 精确恢复 |
| `luo_file_can_finish_one`/`finish_one` | :646/:666 | 两阶段提交的"询问"与"执行"两个阶段的单点实现(第 11 章 11.3.2、11.3.3 节) |
| `luo_file_finish` | :715-752 | 两阶段提交的批量编排入口,文档明确声明"原子性"要求 |
| `luo_file_deserialize` | :780-849 | 新内核中反序列化文件元数据列表,与 `luo_session_deserialize` 共享同一段错误处理哲学注释(第 11 章 11.4.3、11.4.4 节) |
| `liveupdate_register_file_handler`/`unregister_file_handler` | :872/:918 | file handler 的注册/注销入口(第 6 章) |

### C.4 `kernel/liveupdate/luo_flb.c`(FLB 全局共享对象,第 7 章)

| 符号 | 行号 | 角色 |
|---|---|---|
| `luo_flb_file_preserve_one`/`unpreserve_one` | :109/:136 | outgoing 侧引用计数 0→1/N→0 触发的单点逻辑(第 7 章 7.4 节状态机) |
| `luo_flb_retrieve_one`/`file_finish_one` | :159/:208 | incoming 侧懒加载触发与最终 finish 的单点逻辑(第 7 章 7.5 节状态机) |
| `luo_flb_file_preserve`/`unpreserve`/`finish` | :254/:290/:311 | 对外暴露给 `luo_file.c` 的批量编排接口 |
| `liveupdate_register_flb`/`unregister_flb` | :399/:479 | FLB 的注册/注销入口,内含 `__free(kfree)`/`no_free_ptr` 资源管理范例(第 7 章 7.7 节) |
| `liveupdate_flb_get_incoming`/`get_outgoing` | :508/:542 | 双重检查锁定式的懒加载访问入口(第 7 章 7.3、7.6 节) |
| `luo_flb_setup_outgoing`/`setup_incoming`/`serialize` | :555/:590/:646 | FDT 中 FLB 子树的搭建与序列化(第 7 章 7.8 节) |

### C.5 `mm/memfd_luo.c`(memfd 范例实现,第 9 章)

| 符号 | 行号 | 角色 |
|---|---|---|
| `memfd_luo_preserve_folios`/`unpreserve_folios` | :87/:231 | folio 数组的核心保存/回滚逻辑——pin/dirty 标记/`kho_preserve_vmalloc` 的精确回滚链(第 9 章 9.4、9.6 节) |
| `memfd_luo_preserve`/`freeze`/`unpreserve` | :258/:324/:342 | 三段保存生命周期的单文件实现 |
| `memfd_luo_discard_folios`/`finish` | :362/:387 | 处理"从未被 retrieve"场景的折叠式清理逻辑(第 9 章 9.8、9.9 节) |
| `memfd_luo_retrieve_folios`/`retrieve` | :417/:518 | page cache 重建的核心逻辑(第 9 章 9.7 节) |
| `memfd_luo_can_preserve`/`get_id` | :580/:588 | 类型判定与稳定标识符获取 |
| `memfd_luo_file_ops`/`memfd_luo_init` | :593/:609 | 回调表定义与模块注册入口 |

### C.6 ABI 与接口定义(第 4、6、7、8 章)

| 符号/文件 | 角色 |
|---|---|
| `include/linux/liveupdate.h` | 内核态公开 API——`liveupdate_file_op_args`/`liveupdate_file_ops`/`liveupdate_file_handler`/`liveupdate_flb_op_args`/`liveupdate_flb_ops`/`liveupdate_flb`,以及那行埋藏着"vfiofd-v1"伏笔的文档注释(`:90`,第 13 章 13.1 节) |
| `include/uapi/linux/liveupdate.h` | 用户态 ioctl 接口定义(第 4 章) |
| `include/linux/kho/abi/luo.h` | LUO 序列化 ABI——`luo_file_ser`/`luo_file_set_ser`/`luo_session_ser`/`luo_flb_ser` 等结构体的字节级布局定义(第 8 章 8.2 节) |
| `include/linux/kho/abi/memfd.h` | memfd 的序列化 ABI——`memfd_luo_folio_ser`/`memfd_luo_ser`/`MEMFD_LUO_ALL_SEALS`/`MEMFD_LUO_FH_COMPATIBLE`(第 8、9 章) |
| `include/linux/kho/abi/kexec_handover.h` | KHO 的核心传输机制定义——`struct kho_vmalloc`/`DECLARE_KHOSER_PTR`/`kho_vmalloc_chunk`(第 8 章 8.3 节) |
| `kernel/liveupdate/luo_internal.h` | 内部数据结构——`struct luo_ucmd`/`luo_file_set`/`luo_session`(第 5、6 章) |

### C.7 VFIO 相关参考定义(第 12、13 章)

| 符号/文件 | 行号 | 角色 |
|---|---|---|
| `struct vfio_migration_ops` | `include/linux/vfio.h:224-233` | 现有的设备实时迁移回调接口——`migration_set_state`/`get_state`/`get_data_size` |
| `enum vfio_device_mig_state` | `include/uapi/linux/vfio.h:1238-1248` | VFIO 设备迁移状态机的八个状态定义 |
| `VFIO_DEVICE_STATE_*` 状态机说明文档 | `include/uapi/linux/vfio.h:1115-1245` | 详细描述状态转换规则、P2P 静止状态、ERROR 兜底机制的长篇 DOC 注释(第 12 章 12.4.2 节重点摘录) |

## 附录 D:参考资料

### D.1 内核源码(本文档分析的主要对象)

* `kernel/liveupdate/luo_core.c`、`luo_session.c`、`luo_file.c`、
  `luo_flb.c`、`luo_internal.h` —— LUO 核心实现
* `include/linux/liveupdate.h`、`include/uapi/linux/liveupdate.h` ——
  LUO 内核态/用户态接口定义
* `include/linux/kho/abi/luo.h`、`include/linux/kho/abi/memfd.h`、
  `include/linux/kho/abi/kexec_handover.h` —— LUO/memfd/KHO 序列化 ABI 定义
* `mm/memfd_luo.c` —— memfd 的 LUO file handler 范例实现
* `kernel/liveupdate/Kconfig` —— LUO 子系统的配置选项
* `include/linux/vfio.h`、`include/uapi/linux/vfio.h` —— VFIO 子系统接口定义(用于第 12、13 章对比分析)

### D.2 内核官方文档

* `Documentation/core-api/liveupdate.rst` —— LUO 核心 API 文档(基于 kernel-doc 自动生成)
* `Documentation/userspace-api/liveupdate.rst` —— LUO 用户态 ABI 文档
* `Documentation/core-api/kho/index.rst` —— KHO(Kexec HandOver)机制文档

### D.3 selftest 测试代码(第 10 章端到端范例的来源)

* `tools/testing/selftests/liveupdate/liveupdate.c` —— 9 个功能性测试用例(设备访问、会话管理、资源保存)
* `tools/testing/selftests/liveupdate/luo_kexec_simple.c` —— 单 session 端到端跨 `kexec` 范例
* `tools/testing/selftests/liveupdate/luo_multi_session.c` —— 多 session、多文件场景的端到端范例
* `tools/testing/selftests/liveupdate/luo_test_utils.c`、`luo_test_utils.h` —— 跨 `kexec` 测试基础设施("自我识别阶段"/"状态接力"/双重 fork 守护进程化)
* `tools/testing/selftests/liveupdate/do_kexec.sh` —— 触发测试用 `kexec` 的辅助脚本
* `tools/testing/selftests/liveupdate/Makefile`、`config`、`.gitignore` —— 测试套件的构建与运行环境配置

### D.4 本文档定位与姊妹篇

本文档是仓库 `docs/` 目录下的一篇分析性长文档,与同目录下的
`docs/pvm/`(分章节目录形式)以及 `analysis/cfs-eevdf-scheduler-zh.md`
(单文件、按"第 N 章"组织、大量 `文件:行号` 引用的长文档形式)
属于同一性质——均为面向学习与深入理解目的撰写的源码分析笔记,
而非内核官方文档(`Documentation/`)的一部分。如果你觉得本文档
的写作风格似曾相识,这并非巧合——它是有意识地向 `analysis/`
目录下已有分析文档的体例与深度看齐的结果。

---

*(全文完。本文档共十四章正文 + 四篇附录,完整覆盖了 LUO 子系统
从底层基石、核心架构、三大子系统、序列化 ABI、范例实现、端到端
测试、错误处理哲学,到 VFIO 集成现状与展望、QEMU 落地方案的
全景式分析。希望它能成为你理解这一精巧子系统的一把可靠钥匙。)*

