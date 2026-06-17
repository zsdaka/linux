# 第 01 章 GPU 硬件工作原理

> 所属：第一篇 预备与基础　|　前置：第 00 章　|　代码基线：Linux `7.1.0-rc6`
>
> 上一章我们从软件视角鸟瞰了 DRM。但驱动归根结底是"操纵硬件的软件"——不理解 GPU 这块硅片**到底
> 怎么工作**，读再多驱动代码也是浮沙筑塔。本章把"GPU 作为一块 PCIe 设备"从外到内讲透：它如何挂上
> 总线、如何被 CPU 寻址（BAR/MMIO）、如何反过来读写系统内存（DMA）、如何通过中断回报事件；它内部
> 又是怎样的执行机器（命令流处理器、执行单元阵列、三类管线）；以及最容易被忽视、却最常致命的
> **CPU↔GPU 内存一致性与顺序**问题。这是读懂第五、六篇 Intel 硬件与 Xe 提交路径的硬地基。
>
> 本章以概念与硬件机制为主，但每个机制都尽量落到 Xe 的真实代码（`drivers/gpu/drm/xe/`）与 Intel
> 规格（Bspec）上，做到"讲得清、查得到、对得上"。

---

## 1.1 学习目标与前置依赖

### 学习目标

学完本章，你应当能够：

1. 说清 **GPU 作为 PCIe 设备**的全部对外接口：配置空间、BAR（基址寄存器）、MMIO、DMA 主控、中断。
2. 解释 CPU 如何通过 **MMIO** 读写 GPU 寄存器，GPU 如何通过 **DMA** 读写系统内存——两个方向的数据通路。
3. 描述 Intel GPU 的 **BAR 布局**：`GTTMMADR_BAR`（MMIO + GTT）与 `LMEM_BAR`（VRAM），以及 small/
   resizable BAR 的由来。
4. 讲清 **MSI-X 中断**如何从 GPU 传到 CPU，以及 Xe 的"主中断寄存器 → 分层 demux"模型。
5. 建立 GPU **内部执行模型**的心智图：命令流处理器（CS）、ring buffer、context/LRC、batch buffer。
6. 区分 GPU 的三类工作负载与硬件管线：**3D/渲染、media（编解码）、compute（GPGPU）**；理解
   **Xe-core / EU / SIMD** 的吞吐式并行哲学。
7. 说清 GPU 的 **内存层次**：寄存器、各级缓存（L1/L2/L3、LLC）、VRAM、系统内存、stolen；以及
   缓存属性 **LLC（一致）/ WC（写合并）/ UC（不缓存）** 的取舍。
8. **最重要**：理解 **CPU↔GPU 一致性与内存序**——为什么 MMIO 写是"投递（posted）"的、为什么写完
   doorbell 常要补一次读、为什么需要内存屏障；并能在 Xe 代码里指认这些处理。

### 前置依赖

- 第 00 章（图形栈全景、两条主链路、vkms、工具链）。
- 计算机组成基础：物理地址空间、缓存、中断的一般概念。
- 不要求懂 PCIe 协议细节——本章会把驱动需要的部分讲清楚。

> **为什么把硬件放这么靠前**：这是《深入理解 Linux 内核》与《计算机体系结构：量化研究方法》共同的
> 主张——**efficient kernels take advantage of hardware features**，不懂硬件就读不懂高效内核代码里
> 那些"看似多余"的屏障、posting read、缓存属性选择。本章正是为后面"看得懂为什么这么写"打底。

---

## 1.2 主题导引：GPU 是一台什么样的机器，以及为什么这样设计

### 1.2.1 CPU 与 GPU 的根本分工：延迟优化 vs 吞吐优化

理解 GPU 硬件，先要理解它**不是**什么——它不是"更快的 CPU"。CPU 与 GPU 是两种针对不同目标优化的机器：

| 维度 | CPU（延迟优化机器） | GPU（吞吐优化机器） |
|------|---------------------|---------------------|
| 优化目标 | 让**单个**任务尽快完成（低延迟） | 让**大量**任务总吞吐最大（高 throughput） |
| 核心数 | 少（几核~几十核），每核很强 | 极多的小核（成百上千 EU 通道） |
| 隐藏延迟手段 | 大缓存、乱序执行、深分支预测 | **超线程式的海量并发**：一个单元慢了就切到别的线程 |
| 控制逻辑 | 复杂（占芯片面积大头） | 简单（把面积让给计算单元） |
| 典型工作 | 串行、分支密集、低并行 | 数据并行、规则、海量同质 |

一句话：**CPU 用"聪明的少数"快速做完一件事；GPU 用"听话的多数"同时做完一万件事。** 这条哲学解释了
GPU 硬件的几乎一切设计：为什么有海量 EU、为什么用 SIMD、为什么靠"线程多到能盖住内存延迟"而不是
靠大缓存、为什么需要专门的命令流处理器把成批工作喂进去。

### 1.2.2 GPU 是一台"被远程驱动"的协处理器

关键认知：**CPU 不能像调用函数那样直接"调用"GPU**。GPU 是挂在 PCIe 总线另一端的独立处理器，CPU
对它的所有操作都归结为两类极原始的动作：

1. **写它的寄存器 / 内存**（通过 MMIO 或 DMA 让 GPU 看到的内存），告诉它"要做什么"；
2. **被它中断**，得知"做完了 / 出事了"。

中间没有"函数调用"。所谓"提交一帧绘制"，本质是：CPU（驱动 + Mesa）把一串 GPU 能懂的**命令**
（batch buffer）写进一块 GPU 能访问的内存，再通过写一个寄存器（doorbell/tail 指针）"戳"一下 GPU
说"队列里有活了"，然后等中断回报完成。**这条"写命令 + 戳一下 + 等中断"的范式，是理解整条渲染/提交
路径（第 16、19 章）的总纲。**

### 1.2.3 历史视角：集显与独显的硬件差异如何塑造驱动

Intel GPU 有两条硬件形态，深刻影响驱动设计：

- **集成显卡（iGPU）**：与 CPU 同封装，**共享系统内存**，且常能共享 CPU 的最后一级缓存（LLC），
  因此 CPU↔GPU 在某些缓冲上可以是**缓存一致**的。没有独立 VRAM，靠从系统内存"偷"一块
  （**stolen memory**）做固件/帧缓冲。
- **独立显卡（dGPU，如 DG2/Arc）**：板载独立 **VRAM（LMEM）**，通过 PCIe 与 CPU 相连。CPU 想访问
  VRAM 要通过一个 PCIe BAR 窗口，而这个窗口历史上**小于** VRAM 全容量（small BAR 问题），催生了
  **resizable BAR（rebar）**。数据要在 VRAM↔系统内存间迁移（这正是 TTM 第 11 章的用武之地）。

这条差异是后面很多设计的源头：为什么 Xe 用 TTM（要管 VRAM 迁移）、为什么有 `LMEM_BAR` 与 rebar 处理
（`xe_pci_rebar.c`）、为什么缓存一致性在集显与独显上表现不同。**记住"集显共享内存、独显独立 VRAM"
这条主线，本章后面与第 11、15、18 章都会回扣。**

---

## 1.3 原理与全景：GPU 作为 PCIe 设备的完整接口

### 1.3.1 一张"GPU 接到系统里"的硬件全景图

```
        CPU 封装                                   PCIe 总线              GPU(以独显为例)
┌──────────────────────────┐                 ┌──────────────┐   ┌────────────────────────────┐
│  CPU 核心们                │                 │              │   │  PCI 配置空间                │
│   └─ Load/Store ──────────┼── MMIO 写/读 ──►│   Root       │──►│   (Vendor=8086, Device, BAR) │
│                           │                 │   Complex    │   ├────────────────────────────┤
│  LLC (集显时与GPU共享)     │                 │   + IOMMU    │   │  BAR0 GTTMMADR: MMIO寄存器+GTT │
│                           │                 │              │   │  BAR2 LMEM:     VRAM 窗口      │
│  内存控制器 ──► 系统内存    │◄── DMA 读/写 ───┤              │◄──┤  DMA 引擎(GPU 主控读写系统内存)│
│                  (含stolen)│                 │              │   ├────────────────────────────┤
│  中断控制器(APIC)          │◄── MSI-X 写 ────┤              │◄──┤  中断: 写一个地址=发中断       │
└──────────────────────────┘                 └──────────────┘   ├────────────────────────────┤
                                                                 │  命令流处理器(CS) × 每引擎    │
                                                                 │  执行单元阵列(Xe-core/EU)     │
                                                                 │  各级缓存 / VRAM 控制器        │
                                                                 │  显示引擎 / 编解码引擎         │
                                                                 └────────────────────────────┘
```

图里有**四条独立的通路**，构成 CPU 与 GPU 的全部交互，请逐条记牢：

1. **MMIO（CPU→GPU 寄存器/GTT）**：CPU 用普通的 load/store 指令，落到映射进物理地址空间的 BAR 窗口，
   实际就是读写 GPU 的寄存器。这是 CPU"指挥"GPU 的主要手段。
2. **DMA（GPU→系统内存）**：GPU 作为总线主控（bus master），自己发起对系统内存的读写，去取命令、
   取纹理、写结果。CPU 不参与每次传输，只是事先把数据/命令放好。
3. **中断（GPU→CPU）**：GPU 完成工作或出错时，通过 MSI-X（本质是 GPU 向一个特定地址写一个值）触发
   CPU 的中断，驱动的中断处理函数被唤醒。
4. **配置空间（枚举与配置）**：开机时内核 PCI 子系统读 GPU 的配置空间，认出它（8086:xxxx）、读出
   它要多大的地址窗口（BAR），分配并使能。

> **这四条通路 = 驱动与硬件交互的全部词汇表。** 后续所有复杂流程（提交、显示、固件通信）都是这四个
> 原语的组合。把它们刻进脑子，再复杂的代码也能拆解。

### 1.3.2 PCI 配置空间：GPU 的"身份证 + 需求清单"

每个 PCIe 设备都有一段标准化的**配置空间**，内核开机枚举时读它来认识设备：

- **Vendor ID / Device ID**：`8086:xxxx`。Intel 的 Vendor ID 是 `0x8086`（第 00 章 0.8.4 已见，驱动的
  PCI ID 表就是匹配这两个值）。
- **Class Code**：标明这是"显示控制器"（VGA/3D），决定内核归类。
- **BAR0..BAR5（Base Address Registers）**：设备向系统**申报**"我需要多大的地址窗口、是 MMIO 还是
  I/O、是 32 位还是 64 位"。内核据此在物理地址空间里分配窗口并写回 BAR。这是 MMIO 与 VRAM 访问的前提。
- **Capabilities 链表**：声明设备支持的扩展能力，例如 **MSI/MSI-X**（中断方式）、**Resizable BAR**、
  **ATS/PASID**（地址翻译服务 / 进程地址空间标识，与 SVM 相关，第 18 章）等。

### 1.3.3 Intel GPU 的 BAR 布局（来自真实代码）

Intel GPU 的 BAR 不是抽象概念，Xe 在 `drivers/gpu/drm/xe/regs/xe_bars.h` 里把它们编了号：

```c
// drivers/gpu/drm/xe/regs/xe_bars.h:8
#define GTTMMADR_BAR    0   /* MMIO + GTT */
#define LMEM_BAR        2   /* VRAM */
#define VF_LMEM_BAR     9   /* VF VRAM (SR-IOV 虚拟功能用, 第22章) */
```

- **BAR0 = `GTTMMADR_BAR`（MMIO + GTT）**：这块窗口同时映射了 **GPU 的 MMIO 寄存器** 和 **GGTT
  页表**（全局图形地址翻译表，第 15 章）。CPU 读写这块窗口 = 读写 GPU 寄存器 / 改 GGTT 表项。
  其大小由 `pci_resource_len(pdev, GTTMMADR_BAR)` 得到（见 1.5）。
- **BAR2 = `LMEM_BAR`（VRAM 窗口）**：仅独显有意义，是 CPU 访问板载 VRAM 的窗口。历史上它可能小于
  VRAM 全容量（small BAR），需 resizable BAR 扩大（1.7 量化、`xe_pci_rebar.c`）。
- **BAR9 = `VF_LMEM_BAR`**：SR-IOV 场景下虚拟功能（VF）的 VRAM BAR（第 22 章虚拟化）。

> 记忆要点：**"寄存器和 GTT 在 BAR0，VRAM 在 BAR2"**。后面读 `xe_mmio.c`（映射 BAR0）与
> `xe_vram.c`（映射 BAR2）时，这条布局就是地图。

### 1.3.4 配置空间布局与 MSI-X 中断机制（再细一层）

配置空间是一段 256 字节（PCIe 扩展到 4KB）的标准结构，内核 PCI 子系统据它枚举设备。与 GPU 驱动
相关的部分：

```
偏移 0x00  Vendor ID(0x8086) | Device ID        ← 身份(0.8.4 的 PCI ID 表匹配它)
偏移 0x04  Command | Status                      ← Command 里有 "Bus Master Enable" 位:
                                                    不置位, GPU 就不能发起 DMA!
偏移 0x10  BAR0 ... BAR5                          ← 申报地址窗口(1.3.3)
偏移 0x34  Capabilities Pointer ───┐
          ...                      ▼
          Capability List: MSI-X / Resizable BAR / PCIe / Power Mgmt / ATS / PASID ...
```

两点对驱动至关重要：

- **Bus Master Enable（Command 寄存器的 BME 位）**：GPU 要做 DMA（自己读写系统内存）必须先被使能
  bus mastering。`pci_enable_device` 之后驱动通常 `pci_set_master`。**忘了使能 → GPU 一做 DMA 就失败**，
  是新驱动的经典坑。这就是 1.3.1"DMA 通路"的前置开关。
- **Capability 链表**：从 0x34 指向的偏移开始，是一条单链表，每个节点声明一种能力。GPU 关心的有：
  **MSI-X**（中断）、**Resizable BAR**（1.7.2 的 rebar）、**ATS/PASID**（地址翻译服务/进程地址空间
  标识，SVM 用，第 18 章）。

**MSI-X 到底是什么**：传统 INTx 中断要一根共享物理中断线；**MSI/MSI-X（Message Signaled Interrupt）**
则把"中断"变成 **GPU 往一个特定内存地址写一个特定的值** —— 这次写被中断控制器（APIC）识别为"产生
某号中断"。好处：① 不占物理引脚、可有很多独立向量（MSI-X 表可有上千项，不同事件用不同向量）；
② 天然有序——中断作为一次内存写，排在该设备之前的 DMA 写之后，**保证"CPU 进中断时，GPU 之前写的
数据已到达"**（这点对"完成中断 + 完成数据"的配对极重要，呼应 1.6）。Xe 默认用 MSI/MSI-X（见 1.5.5）。

### 1.3.5 关键前置认知：GPU 通过"自己的页表"访问内存

一个极其重要、却常被初学者忽略的事实：**GPU 发起 DMA 时用的不是物理地址，而是它自己的图形虚拟地址**，
经 GPU 的页表（**GTT/GGTT** 与 **PPGTT**）翻译成物理地址（或 VRAM 地址）。这与 CPU 经 MMU 翻译虚拟
地址如出一辙，只是这套页表是 GPU 专用的（第 15 章精讲）。

为什么现在就要知道？因为它解释了三件本章相关的事：

1. **为什么 BAR0 里要放 GGTT**（1.3.3）：CPU 通过 BAR0 写 GGTT 表项，等于"为 GPU 建立地址映射"，
   之后 GPU 用那个图形地址就能访问对应内存。
2. **为什么命令/缓冲要先"绑定"再用**（VM-bind，第 18 章）：GPU 看不到没在它页表里映射过的内存。
   你给 GPU 的每个 batch buffer、纹理，都得先在 GPU 地址空间里有映射。
3. **隔离与保护**：每个进程的 GPU 上下文用各自的 PPGTT，互相看不到对方内存——这是 GPU 上的"进程
   地址空间隔离"（回扣第 00 章 0.2.8 的最小权限主题）。

记住这句话：**"CPU 经 MMU 用虚拟地址；GPU 经 GTT/PPGTT 用图形虚拟地址。两套页表，同一种思想。"**
第 15、18 章会把它彻底展开。

---

## 1.4 关键数据结构：内核如何抽象"寄存器访问"

CPU 对 GPU 的指挥主要靠读写寄存器，所以"寄存器访问"在 Xe 里有专门的数据结构与函数族。

### 1.4.1 `struct xe_mmio`：一块"已映射的寄存器窗口"

`xe_mmio` 代表"某个 tile 已经映射好的 MMIO 区域"，是寄存器读写的句柄（概念字段，定义见
`drivers/gpu/drm/xe/xe_device_types.h`，初始化见 `xe_mmio.c:122`）：

| 字段 | 类型 | 含义 | 不变量/备注 |
|------|------|------|-------------|
| `regs` | `void __iomem *` | 映射后的内核虚拟地址基址（指向 BAR0 窗口） | `__iomem` 标注："这是设备内存，必须用 readl/writel 访问，不能当普通内存解引用" |
| `regs_size` | `u32` | 该窗口大小（字节） | 访问偏移须 < 此值 |
| `tile` | `struct xe_tile *` | 这块 MMIO 属于哪个 tile | 多 tile 设备会切分 BAR（见 1.5） |
| `sriov_vf_gt` | `struct xe_gt *` | SR-IOV VF 场景下的重定向目标 | 非虚拟化时为空 |

`xe_mmio_init`（`xe_mmio.c:122`）只是把这几个字段填好：

```c
// drivers/gpu/drm/xe/xe_mmio.c:122
void xe_mmio_init(struct xe_mmio *mmio, struct xe_tile *tile, void __iomem *ptr, u32 size)
{
    xe_tile_assert(tile, size <= XE_REG_ADDR_MAX);
    mmio->regs = ptr;          // BAR 映射得到的虚拟地址
    mmio->regs_size = size;
    mmio->tile = tile;
}
```

### 1.4.2 `struct xe_reg`：一个"带元信息的寄存器地址"

Xe 不直接用裸偏移量访问寄存器，而是用 `struct xe_reg`（`regs/xe_reg_defs.h`）包装寄存器地址 + 元信息
（如是否需要 VF 重定向、是否在某 mask 范围）。这带来两个好处：

- **类型安全**：编译器能区分"寄存器地址"与"普通整数"，避免把数值误当寄存器偏移。
- **可携带元数据**：如 `reg.vf`（该寄存器在 VF 下是否直接访问，见 `xe_mmio_write32` 里的分支）。

寄存器本身用宏定义（`regs/` 目录下大量 `XE_REG(...)`），例如各引擎、各功能块的寄存器都有命名常量，
配合 `REG_BIT()`/`REG_GENMASK()` 描述位域（第 14 章详解这套宏体系）。

> 设计取舍（量化思维预告）：用结构体包装寄存器看似"多此一举"，但在一个有**上万个寄存器**、要适配
> **多代硬件 + 虚拟化**的驱动里，类型安全与元数据携带能消除大量低级 bug。这是"用一点运行时/认知成本
> 换可维护性"的典型权衡——与第 00 章 Xe"多用通用框架"哲学一脉相承。

---

## 1.5 核心代码走读：从 BAR 映射到一次寄存器读写

### 1.5.1 把 BAR0 映射进内核：`xe_mmio_probe_early` 路径

GPU 刚被 PCI 子系统认领后，驱动要做的第一件实事，就是把 BAR0（GTTMMADR）映射成内核可访问的虚拟
地址。核心几行（`drivers/gpu/drm/xe/xe_mmio.c:95` 附近）：

```c
// drivers/gpu/drm/xe/xe_mmio.c (节选, 第 95-100 行附近)
/*
 * Map the entire BAR.
 * The first 16MB of the BAR, belong to the root tile, and include:
 * registers (0-4MB), reserved space (4MB-8MB) and GGTT (8MB-16MB).
 */
xe->mmio.size = pci_resource_len(pdev, GTTMMADR_BAR);   // 问 PCI: 这个 BAR 多大?
xe->mmio.regs = pci_iomap(pdev, GTTMMADR_BAR, 0);       // 把它映射成 void __iomem *
```

完整的 `xe_mmio_probe_early`（`xe_mmio.c`，BAR 映射的总入口）：

```c
int xe_mmio_probe_early(struct xe_device *xe)
{
    struct xe_tile *root_tile = xe_device_get_root_tile(xe);
    struct pci_dev *pdev = to_pci_dev(xe->drm.dev);

    xe->mmio.size = pci_resource_len(pdev, GTTMMADR_BAR);   // (1) 问硬件: BAR0 多大
    xe->mmio.regs = pci_iomap(pdev, GTTMMADR_BAR, 0);       // (2) 映射整个 BAR0
    if (!xe->mmio.regs) {
        drm_err(&xe->drm, "failed to map registers\n");
        return -EIO;
    }
    /* Setup first tile; other tiles (if present) will be setup later. */
    xe_mmio_init(&root_tile->mmio, root_tile, xe->mmio.regs, SZ_4M);  // (3) root tile 拿前 4MB(寄存器区)
    return devm_add_action_or_reset(xe->drm.dev, mmio_fini, xe);      // (4) 注册自动 unmap
}
```

逐点能学到的硬件 + 内核事实：

- **(1)** `pci_resource_len(pdev, GTTMMADR_BAR)`：从配置空间（1.3.2）读出 BAR0 申报的窗口大小。
  **驱动不写死大小，而是问硬件**——不同代际/配置窗口大小不同。
- **(2)** `pci_iomap(pdev, GTTMMADR_BAR, 0)`：把整段物理 BAR 窗口映射进内核虚拟地址空间，得到
  `void __iomem *`。从此 CPU 读写这个指针（经 readl/writel）= 读写 GPU 寄存器/GGTT。
- **(3)** `xe_mmio_init(..., SZ_4M)`：root tile 的寄存器窗口取前 4MB——与下方多 tile 布局注释一致。
- **(4)** `devm_add_action_or_reset(dev, mmio_fini, xe)`：**托管式清理**。注册一个回调 `mmio_fini`
  （内部 `pci_iounmap`），设备销毁时自动 unmap，无需手写配对的释放。这是第 00 章 0.5.8、第 02 章
  "托管资源"思想在底层硬件初始化里的又一次体现。`ALLOW_ERROR_INJECTION(xe_mmio_probe_early, ERRNO)`
  还允许测试框架人为注入失败，验证错误路径——可靠性工程的细节。

**BAR0 的内部布局**（来自 `xe_mmio.c` 注释，印证 1.3.3）：前 4MB 是寄存器，4–8MB 保留，8–16MB 是
GGTT；每个 tile 占 16MB。多 tile 设备由 `mmio_multi_tile_setup` 把后续 tile 的窗口切出来：

```
.----------------------.  <- tile_count * 16MB
|   tile1 GTT + other  |
|----------------------|  <- 16MB + 4MB
|   tile1->mmio.regs   |  (tile1 寄存器)
|----------------------|  <- 16MB
|   tile0 GTT + other  |
|----------------------|  <- 4MB
|   tile0->mmio.regs   |  (tile0/root 寄存器)
'----------------------'  <- 0
```

每个 tile 拿到自己的 `xe_mmio`（`mmio_multi_tile_setup` 里 `xe_mmio_init(&tile->mmio, tile,
xe->mmio.regs + id * tile_mmio_size, SZ_4M)`）。"tile"是 Xe 的物理分区概念（第 13、17 章），多 tile
就是"一块卡上多个准独立的 GPU 分区"，各有各的寄存器/GTT 窗口。

### 1.5.2 一次寄存器写：`xe_mmio_write32`

```c
// drivers/gpu/drm/xe/xe_mmio.c:179
void xe_mmio_write32(struct xe_mmio *mmio, struct xe_reg reg, u32 val)
{
    u32 addr = xe_mmio_adjusted_addr(mmio, reg.addr);   // (1) 算出最终偏移(可能含 tile 调整)
    trace_xe_reg_rw(mmio, true, addr, val, sizeof(val)); // (2) tracepoint: 可被 ftrace 抓到(第00章)
    if (!reg.vf && IS_SRIOV_VF(mmio->tile->xe))          // (3) SR-IOV VF: 某些寄存器要重定向给 GuC
        xe_gt_sriov_vf_write32(...);
    else
        writel(val, mmio->regs + addr);                  // (4) 真正的硬件写: writel 到 BAR 映射地址
}
```

逐点：

- **(1)** `xe_mmio_adjusted_addr`：把逻辑寄存器地址调整成该 mmio 窗口内的实际偏移（多 tile/特殊映射时
  会有偏移）。
- **(2)** `trace_xe_reg_rw`：每次寄存器读写都打一个 tracepoint。这就是为什么你能用 ftrace（第 00 章
  0.11）**逐条看到驱动对硬件的每一次寄存器操作**——调试硬件交互的利器。
- **(3)** 虚拟化分支：VF（虚拟功能）下部分寄存器不能直接写硬件，要经 GuC 重定向（第 22 章）。
- **(4)** `writel(val, mmio->regs + addr)`：**这一行才是真正写硬件**。`writel` 是内核访问 MMIO 的标准
  原语，它保证用正确的方式（非缓存、正确字节序）把值送到设备。**注意：到此为止，这次写可能还没真正
  "落到"硬件寄存器**——这就是 1.6.1 要讲的"写投递（posting）"问题。

### 1.5.3 一次寄存器读：`xe_mmio_read32` 与"读会清空写队列"

```c
// drivers/gpu/drm/xe/xe_mmio.c:192
u32 xe_mmio_read32(struct xe_mmio *mmio, struct xe_reg reg)
{
    u32 addr = xe_mmio_adjusted_addr(mmio, reg.addr);
    u32 val;
    mmio_flush_pending_writes(mmio);                     // (1) 一个硬件 WA: 先冲刷挂起的写
    if (!reg.vf && IS_SRIOV_VF(mmio->tile->xe))
        val = xe_gt_sriov_vf_read32(...);
    else
        val = readl(mmio->regs + addr);                  // (2) 真正的硬件读
    trace_xe_reg_rw(mmio, false, addr, val, sizeof(val));
    return val;
}
```

特别看 **(1)** `mmio_flush_pending_writes`（`xe_mmio.c:131`）：

```c
static void mmio_flush_pending_writes(struct xe_mmio *mmio)
{
#define DUMMY_REG_OFFSET 0x130030
    int i;
    if (!XE_DEVICE_WA(mmio->tile->xe, 15015404425))   // 仅特定硬件需要此 WA
        return;
    for (i = 0; i < 4; i++)                             // 4 次哑写, 把挂起的写挤出去
        writel(0, mmio->regs + DUMMY_REG_OFFSET);
}
```

这是一个针对特定硬件勘误（workaround，编号 `15015404425`）的处理：在读之前先做 4 次"哑写"，确保
之前挂起的写已经真正落地。**这段代码本身就是 1.6"内存序与投递"问题的活教材**——硬件的写不是即时的，
驱动必须用额外手段保证顺序。读到这种"看似多余的哑写/读"，别急着以为是冗余，它们往往在解决你看不见的
硬件时序问题。

### 1.5.4 中断从硬件到驱动：`xelp_irq_handler` 的分层 demux

GPU 完成工作靠中断回报。看 Xe 的顶层中断处理（`drivers/gpu/drm/xe/xe_irq.c:412`）：

```c
// drivers/gpu/drm/xe/xe_irq.c:412 (节选)
static irqreturn_t xelp_irq_handler(int irq, void *arg)
{
    struct xe_device *xe = arg;
    u32 master_ctl, gu_misc_iir;
    ...
    master_ctl = xelp_intr_disable(xe);          // (1) 读主中断寄存器 GFX_MSTR_IRQ, 同时关总开关
    if (!master_ctl) {                            // (2) 主寄存器为 0 = 不是我的中断(共享 IRQ)
        ...
        return IRQ_NONE;
    }
    gt_irq_handler(tile, master_ctl, intr_dw, identity); // (3) 按 master_ctl 的位, 分发到各 GT/引擎
    xe_display_irq_handler(xe, master_ctl);              // (4) 分发到显示子系统
    gu_misc_iir = gu_misc_irq_ack(xe, master_ctl);       // (5) 杂项中断应答
    xelp_intr_enable(xe, false);                         // (6) 重新打开总开关
    ...
    return IRQ_HANDLED;
}
```

这段揭示了 GPU 中断的**分层 demux 模型**（第 04 章会从 DRM 事件角度再讲一遍）：

- **(1)** GPU 有一个**主中断寄存器** `GFX_MSTR_IRQ`（`xe_irq.c:101`）。读它一次性拿到"哪些大类有
  中断挂起"的位图（`master_ctl`），并顺手关掉总中断开关（避免处理期间重复进来）。
- **(2)** `master_ctl == 0` 表示"这次中断不是本设备引起的"——在共享中断线场景下返回 `IRQ_NONE`，
  让内核继续问别的设备。这是 Linux 共享 IRQ 的标准礼仪。
- **(3)(4)(5)** 按 `master_ctl` 的不同位，**逐层向下分发**：到各 GT 的引擎中断、到显示、到杂项。
  每一层再读自己的中断标识寄存器（IIR）确定具体来源、做应答（ack）。
- **(6)** 处理完重新使能总开关。

> 这就是 1.2.2"被它中断 → 得知做完了"的代码兑现。`master_ctl` 这套"主寄存器位图 → 分层细化"的
> 设计，是大型 GPU 管理几十种中断源的标准做法。第 04 章讲 DRM 事件、第 19/20 章讲提交完成与 GuC
> G2H 消息时，都会回到这里。

### 1.5.5 中断是怎么"装"上的：`xe_irq_install`

中断处理函数（1.5.4）要先注册到内核才会被调用。看 `xe_irq_install`（`xe_irq.c:806`）：

```c
int xe_irq_install(struct xe_device *xe)
{
    struct pci_dev *pdev = to_pci_dev(xe->drm.dev);
    unsigned int irq_flags = PCI_IRQ_MSI;
    int nvec = 1;
    ...
    xe_irq_reset(xe);                                  // (1) 先把硬件中断状态清干净

    if (xe_device_has_msix(xe)) {                       // (2) 选 MSI-X 还是 MSI
        nvec = xe->irq.msix.nvec;
        irq_flags = PCI_IRQ_MSIX;
    }
    err = pci_alloc_irq_vectors(pdev, nvec, nvec, irq_flags); // (3) 向内核申请中断向量
    ...
    err = xe_device_has_msix(xe) ? xe_irq_msix_request_irqs(xe)
                                 : xe_irq_msi_request_irqs(xe); // (4) 注册处理函数
    ...
}

// MSI 路径(单向量), xe_irq.c:752
static int xe_irq_msi_request_irqs(struct xe_device *xe)
{
    ...
    irq = pci_irq_vector(pdev, 0);                      // 取第 0 号向量的 Linux IRQ 号
    err = request_irq(irq, irq_handler, IRQF_SHARED, DRIVER_NAME, xe); // 绑定处理函数
    ...
}
```

逐点：

- **(1)** `xe_irq_reset`：注册前先复位硬件中断状态，避免"装上就来一堆历史中断"。
- **(2)** `xe_device_has_msix` 决定用 **MSI-X**（多向量，不同事件用不同向量）还是 **MSI**（单向量，
  所有事件挤一个，靠 1.5.4 的主寄存器自己 demux）。新硬件倾向 MSI-X。
- **(3)** `pci_alloc_irq_vectors(pdev, min, max, flags)`：向内核 PCI/IRQ 子系统申请中断向量。这一步
  之后，GPU 的 MSI/MSI-X 才真正与某个 CPU 中断号挂钩。
- **(4)** `request_irq(irq, handler, IRQF_SHARED, ...)`：把处理函数（MSI 单向量时就是 `irq_handler`
  → 最终 `xelp_irq_handler`）注册到那个 IRQ 号。`IRQF_SHARED` 表示这条中断线可能被多个设备共享——
  这正是 1.5.4 里 `master_ctl==0 → IRQ_NONE` 礼仪存在的原因：共享时每个设备都要先判断"是不是我"。

> 把 1.5.4（处理）与 1.5.5（安装）连起来：`xe_irq_install` 把 `xelp_irq_handler` 绑到 GPU 的 MSI/MSI-X
> 向量；之后每当 GPU 写中断消息（1.3.4），CPU 就跳进 `xelp_irq_handler` 做分层 demux。这是 GPU→CPU
> 通路的完整闭环。第 04 章会接着讲这些中断如何变成 DRM 事件投递给用户态。

### 1.5.6 一个会反复绊倒你的硬件机制：forcewake（电源/时钟域）预览

现代 GPU 为省电，会把内部各功能块（render、media、各 GT 域……）的时钟在空闲时**门控（gate）**掉。
但被门控的块，其寄存器读出来是**垃圾值（常为 0 或 0xFFFFFFFF）**，写进去也丢失。于是 Intel 引入
**forcewake**：访问某域的寄存器前，驱动必须先"叫醒"（force-wake）该域，访问完再释放，让硬件继续
省电。Xe 有专门的 `xe_force_wake`（`xe_force_wake.c`）按域管理。

为什么现在预览它：你在第 14 章及各处会看到大量这样的代码：

```
xe_force_wake_get(fw, DOMAIN);     // 叫醒目标域
... xe_mmio_read32/write32(...);   // 这时寄存器才是有效的
xe_force_wake_put(fw, DOMAIN);     // 释放, 让硬件回去省电
```

**初学者最常见的硬件 bug 之一**：没拿 forcewake 就读寄存器，读到 0/全 1，再基于垃圾值做判断，行为
诡异且难复现。记住这条规律：**"读到的寄存器值离谱（0 或 0xFFFFFFFF）→ 先怀疑没拿 forcewake 或域
没上电。"** forcewake 的完整机制（域划分、引用计数、超时）留到第 14 章；这里先在硬件层面理解
"寄存器不是随时可读的，要先唤醒"。这也是 1.7.1 说"MMIO 读贵"的另一面——有时不止贵，还要先唤醒域。

---

## 1.6 机制深入：CPU↔GPU 的一致性与内存序（最易错的地方）

这一节是本章的灵魂。**绝大多数"偶发、难复现"的 GPU 驱动 bug，根子都在 CPU 与 GPU 对内存/寄存器的
可见性与顺序理解错误。** 把这节吃透，你就比很多人更懂"为什么代码要这么写"。

### 1.6.1 MMIO 写是"投递（posted）"的：写了不等于到了

CPU 执行一条 `writel(val, addr)` 写 MMIO，**并不保证这条写在指令返回时就已经到达 GPU 寄存器**。
在现代系统里，MMIO 写通常是**posted（投递式）**的：CPU 把写交给总线/Root Complex 就继续往下执行，
写在路上异步完成。好处是 CPU 不必每次写都停下来等几百纳秒的总线往返；代价是**你无法仅凭"写指令
已返回"断定 GPU 已经看到这个值**。

由此产生几条铁律：

1. **要确保前面的 MMIO 写已经生效，做一次该设备的 MMIO 读**。读是**非 posted**的（CPU 必须等数据
   回来），而总线保证"读会把它前面的写挤到设备"。这就是著名的 **"posting read"**（投递后补一次读）。
   1.5.3 里那个"读之前先哑写、某些路径写完 doorbell 再读一下"都是这个道理的变体。
2. **写顺序不一定 = 程序顺序**。对不同地址的多次写，到达设备的顺序可能被重排；需要顺序时用屏障或
   posting read 强制。

### 1.6.2 内存屏障：wmb / rmb / mb 与 dma 屏障

当 CPU 既写"普通内存"（GPU 将通过 DMA 读的命令）又写"MMIO doorbell"（通知 GPU）时，顺序至关重要：
**必须保证 GPU 在被通知后，能看到完整的命令，而不是写了一半的命令**。典型模式：

```
1. CPU 把命令写进 ring buffer(普通内存, GPU 将 DMA 读取)
2. wmb()  // 写屏障: 保证上面的命令写, 先于下面的 tail 更新对 GPU 可见
3. CPU 更新 ring tail 指针 / 敲 doorbell(MMIO 写)
4. (必要时) posting read, 确保 doorbell 真的送达
```

- **`wmb()`（write memory barrier）**：保证屏障前的写先于屏障后的写"对外可见"。上例第 2 步确保
  GPU 不会"先看到新 tail、却还没看到对应命令"。
- **`rmb()` / `mb()`**：读屏障 / 全屏障，用于反方向（CPU 读 GPU 通过 DMA 写回的状态时）。
- **`dma_wmb()`/`dma_rmb()`**：专门针对"与 DMA 设备共享的内存"的轻量屏障，比全 `mb()` 开销小。

> 为什么这事在 GPU 驱动里格外突出？因为 GPU 是**异步、自主取数**的 DMA 主控：你写好的命令什么时候被
> GPU 读，不由你控制。一旦"通知"越过了"数据"，GPU 就会执行到残缺命令 → 花屏 / hang / 随机崩。
> 第 16 章（命令/ring）、第 19/20 章（提交/GuC doorbell）会反复用到这套顺序规则。

### 1.6.3 缓存属性：LLC（一致）/ WC（写合并）/ UC（不缓存）

CPU 访问一块内存时，这块内存的**缓存属性**决定了 CPU 与 GPU 之间的可见性行为。GPU 驱动会为不同用途的
缓冲精心选择属性：

| 属性 | 全称 | 行为 | 典型用途 |
|------|------|------|----------|
| **UC** | Uncached | 完全不缓存，每次访问直达内存/设备；慢但强一致 | MMIO 寄存器、需要立刻可见的少量数据 |
| **WC** | Write-Combining | 写被合并成大块批量刷出；CPU 写很快，但**乱序、需屏障**，读很慢 | CPU 大批量写、GPU 读的缓冲（如往 VRAM 窗口灌数据、写命令） |
| **WB/LLC** | Write-Back / 经最后一级缓存 | 正常可缓存；集显上若 GPU 也接 LLC，则可达成 CPU↔GPU **缓存一致** | 集显上 CPU、GPU 都频繁读写且需一致的缓冲 |

关键洞察：

- **WC 是 GPU 驱动的常客**：CPU 往 GPU 要读的缓冲里灌数据（命令、顶点、纹理）时，WC 让 CPU 侧写吞吐
  很高（合并成突发），但因为它**弱序**，灌完必须 `wmb()`/sfence 再通知 GPU——又回到 1.6.2 的屏障。
- **集显能"一致"，独显通常不能**：集显 GPU 可挂在 LLC 上，于是某些缓冲 CPU 改了 GPU 直接能看到、
  无需显式刷缓存（呼应 1.2.3）。独显的 VRAM 在 PCIe 另一端，CPU 缓存与 GPU 之间没有硬件一致性，
  必须靠显式管理（刷缓存 / 选 WC/UC / DMA 同步）。
- **选错属性的后果**：把"GPU 要读的缓冲"放成普通 WB 而不刷缓存 → GPU 读到旧值；把寄存器当普通内存
  缓存 → 完全错乱。所以驱动里到处能看到对缓存属性的显式指定（第 11、15、18 章会看到 PTE 里的缓存位、
  TTM 的 caching 设置）。

### 1.6.4 把三者串起来：一次"安全提交"的内存视角

综合 1.6.1–1.6.3，一次把命令交给 GPU 的"内存正确性"全景：

```
[CPU] 在 WC 缓冲里写好 batch buffer 命令          (写吞吐高, 但弱序)
[CPU] wmb()/sfence                                 (确保命令写全部刷出且有序)
[CPU] 更新 ring tail (MMIO 写, posted)             (通知 GPU "有新活")
[CPU] posting read (读一下该设备寄存器)            (确保 tail 更新真的送达)
        │
        ▼ (GPU 异步地)
[GPU] DMA 读取 ring 里的命令 → 命令流处理器解析 → 执行单元跑
[GPU] 完成 → 写一个完成标记到内存(DMA) + 触发 MSI-X 中断
        │
        ▼
[CPU] 中断处理 xelp_irq_handler → 读 IIR 确定来源 → 唤醒等待者
[CPU] (读 GPU 写回的状态前) rmb()/dma_rmb()        (确保读到的是新值)
```

这张图是第 16、19、20 章"提交路径"的内存正确性骨架。**现在记住它的形状；后面每次看到驱动里
"莫名其妙"的屏障或 posting read，回来对照本图，就懂了。**

### 1.6.5 IOMMU 与 DMA 地址翻译：GPU 看到的"系统内存地址"也是翻译过的

1.3.5 说 GPU 经自己的 GTT 访问内存；而在 CPU 侧，当 GPU 对**系统内存**做 DMA 时，还可能经过
**IOMMU**（输入输出内存管理单元）再翻译一层。于是驱动必须用 DMA API 拿到"设备能用的地址"
（dma_addr_t），而不能直接把 CPU 物理地址给 GPU。Xe 在为 BO 准备系统内存页时就这么做
（`xe_bo.c:395`）：

```c
// drivers/gpu/drm/xe/xe_bo.c (节选)
ret = sg_alloc_table_from_pages_segment(&xe_tt->sgt, tt->pages, ...);  // (1) 把零散页组织成 scatter-gather 表
...
ret = dma_map_sgtable(xe->drm.dev, xe_tt->sg, DMA_BIDIRECTIONAL, ...); // (2) 经 DMA API/IOMMU 映射, 得到设备地址
```

要点：

- **(1) scatter-gather（散列表，sg_table）**：系统内存里一个大缓冲在物理上往往是**许多不连续的页**
  （4KB 一块）。`sg_table` 把这些零散页串成一张表，描述"这块逻辑缓冲 = 物理页 A + 页 B + …"。
- **(2) `dma_map_sgtable(dev, sg, dir, ...)`**：对每段调用 DMA API，**经 IOMMU（若启用）建立映射**，
  返回 GPU 实际能用的 `dma_addr_t`。方向 `DMA_BIDIRECTIONAL` 表示 GPU 既读又写。
- **为什么不能跳过**：① 有 IOMMU 时，CPU 物理地址 ≠ 设备地址，必须翻译；② DMA API 还负责必要的
  缓存同步（在非一致架构上）；③ 安全——IOMMU 能把 GPU 的 DMA 限制在被授权的页内，防止越界
  （与 SVM/PASID 第 18 章相关）。
- **`dma_set_mask`（`xe_device.c:636`）**：驱动先声明"我能寻址多少位 DMA 地址"（如 64 位），DMA API
  据此决定是否需要 bounce buffer（swiotlb）等。掩码设太小会导致高地址内存需中转，损失性能。

一句话：**GPU 访问系统内存有两层可能的翻译——CPU 侧的 IOMMU + GPU 侧的 GTT/PPGTT；驱动用 sg_table
+ DMA API 拿到正确的设备地址，再把它填进 GPU 页表。** 第 11（TTM 页）、15（GTT）、18（VM-bind）章
会把这条链补全。

### 1.6.6 GPU 也有缓存：一致性域与"该刷谁"

不只 CPU 有缓存，GPU 内部也有多级缓存（L1/L2/**L3**，新代际还有 **L4**）。于是"一致性"不是一个开关，
而是**多个域之间**的关系。Xe 代码里的真实注释把这点讲得很透（`xe_device.c:1159` `xe_device_td_flush`）：

> "Display engine has direct access to memory and is **never coherent with L3/L4** caches (or CPU
> caches), however KMD is responsible for **specifically flushing transient L3 GPU cache entries
> prior to the flip sequence** to ensure scanout can happen … without seeing corruption."

以及（同函数注释）："SA Media is **not coherent with L3** … For copy/blt the HW internally forces
uncached behaviour …"。从这些注释能提炼出 GPU 内部一致性的几条真实规律：

- **显示引擎不与 GPU 的 L3/L4、也不与 CPU 缓存一致**：所以翻页（flip）前，驱动必须**主动刷** L3 里
  那些"瞬态"条目（`xe_device_td_flush`），否则扫描输出会看到脏数据（花屏）。这就是第 21 章显示翻页
  里"看似多余的 cache flush"的来历。
- **不同引擎与 L3 的一致性不同**：render/compute 与 media 之间要协同时，需在提交末尾用 `PIPE_CONTROL`
  刷 L3（保证 render 的产物 media 能读到）；copy/blt 引擎硬件强制 uncached，反而不用刷。
- **独显系统内存默认 WB 且与 GPU 一致**（`xe_bo.c:497` 注释："DGFX … GPU system memory accesses
  are always coherent with the CPU"），但 **PPGTT 页表本身在某些代际是非一致的，需 CPU:WC 映射**
  （`xe_bo.c:514`）；**scanout 永远与 CPU 缓存非一致**（`xe_bo.c:3375`）。

> 这解释了为什么本书后面（第 11、15、21 章）到处是"针对某用途选某缓存属性 + 在某时刻刷某级缓存"的
> 代码——**一致性是按"域 × 用途 × 代际"精细管理的，不是全局一刀切**。现在你只需建立"GPU 内部也有
> 缓存、不同部件一致性不同、驱动负责在关键点显式刷缓存"的认知。

---

## 1.7 量化分析：带宽、延迟与那些"看不见的成本"

H&P 的精神：凡论性能，给数字。以下是理解 GPU 数据通路必备的量级直觉（具体数值随平台变化，量级稳定；
测量方法见[附录 D](./appendix-d-methodology.md)）。

### 1.7.1 各通路的带宽/延迟量级

| 通路 | 带宽量级 | 延迟量级 | 含义 |
|------|----------|----------|------|
| 单次 MMIO 寄存器读 | — | ~数百纳秒（µs 量级以下） | 一次 PCIe 往返；**posting read 不是免费的** |
| 单次 MMIO 寄存器写（posted） | 高（可流水） | 写本身"立即返回" | 但"生效"要等投递完成（1.6.1） |
| PCIe（独显 ↔ 系统内存） | 数 GB/s ~ 数十 GB/s（按代/lane） | 微秒级 | 独显跨 PCIe 搬数据的上限 |
| 独显 VRAM 本地带宽 | 数百 GB/s ~ TB/s | 纳秒级 | 远高于 PCIe → "数据尽量留 VRAM" |
| 系统内存（集显共享） | 数十 GB/s | 纳秒~百纳秒 | 集显 GPU 与 CPU 抢同一内存带宽 |

**能读出的工程结论**：

1. **"少过 PCIe"是独显性能的第一原则**：VRAM 本地带宽可比 PCIe 高一个数量级，所以热数据要驻留 VRAM
   （TTM 的迁移/驻留管理，第 11 章），尽量避免反复跨 PCIe 搬运。
2. **MMIO 读很贵，能不读就不读**：每次 posting read 都是一次 PCIe 往返（数百纳秒）。高频路径
   （如提交）会尽量减少 MMIO 读、用内存里的状态 + 屏障替代——这解释了为什么现代提交走 doorbell +
   内存状态而非频繁轮询寄存器（第 19、20 章 GuC）。
3. **集显与独显的优化方向相反**：集显省的是"内存带宽争用 + 拷贝"（数据本就在系统内存，零拷贝是关键）；
   独显省的是"PCIe 往返 + VRAM 容量驱逐"。

### 1.7.2 small BAR 与 resizable BAR：一个量化驱动的设计

独显 VRAM 可能有 8/16GB，但传统 PCIe BAR 窗口只有 **256MB**——CPU 一次只能"看到" VRAM 的一小片
（small BAR）。这迫使驱动在"CPU 可见区"里腾挪，带来复杂度与性能损失。**Resizable BAR（rebar）**
让 BAR 窗口扩大到覆盖全部 VRAM，CPU 能直接寻址整块 VRAM。Xe 在探测早期就尝试 resize：

```
// drivers/gpu/drm/xe/xe_pci.c:1103
xe_pci_rebar_resize(xe);    // 尝试把 LMEM_BAR 调大到覆盖全部 VRAM
```

- 没有 rebar：CPU 可见 VRAM ≤ 256MB，往 VRAM 上传大纹理要分片经"可见窗口"中转 → 多一次拷贝、更慢。
- 有 rebar：CPU 可直接映射整块 VRAM（配合 WC 属性），上传路径更短。

这是"一个硬件能力（rebar）直接改变软件数据路径与性能"的典型案例，也是量化思维的好素材：
**BAR 窗口大小这个看似枯燥的数字，直接决定了上传带宽与代码复杂度。** 细节见第 11、17 章。

### 1.7.3 寄存器规模的量级

Intel GPU 有**上万个** MMIO 寄存器（覆盖各引擎、显示、功耗、固件接口等）。这解释了：

- 为什么 Xe 用 `struct xe_reg` + `REG_BIT`/`REG_GENMASK` 一整套宏体系来管理（1.4.2、第 14 章）——
  靠裸偏移量根本管不过来。
- 为什么寄存器访问要有 tracepoint（1.5.2）——上万寄存器、几十种交互，没有 trace 无法调试。

---

## 1.8 硬件视角：GPU 内部执行机器与 Intel 的具体形态

前面讲的是"GPU 对外的四条通路"。本节钻进 GPU 内部，看它接到命令后**怎么算**——这是第 13、16 章的
铺垫，这里建立心智图。

### 1.8.1 命令流处理器（Command Streamer, CS）与 ring buffer

每个 GPU 引擎前端都有一个**命令流处理器（CS）**：它从一段**环形缓冲区（ring buffer）**里顺序读取
命令并执行/分派。ring 有两个指针：

- **tail**：软件（驱动）写到哪了——驱动加完命令就推进 tail（MMIO 写或内存 + doorbell）。
- **head**：硬件读到哪了——CS 消费命令时推进 head。

```
   ring buffer (一段 GPU 可见内存)
   ┌───────────────────────────────────────────────┐
   │ cmd cmd cmd cmd | (空) ............ | cmd cmd   │  (环形)
   └───────▲──────────▲─────────────────────────────┘
          head        tail
        (GPU读到此)  (CPU写到此)
   head==tail: 空(没活); CPU 推 tail 添活; GPU 推 head 消费
```

命令分两级：ring 里通常放的是"跳转到一个 **batch buffer**"的命令（`MI_BATCH_BUFFER_START`），真正的
大段绘制/计算命令在 batch buffer 里（由 Mesa 生成）。**ring → batch → (可能二级 batch)** 这套结构是
第 16 章的主菜，这里先记住"CS 从 ring 顺序取命令、命令里会跳到 batch"。

### 1.8.2 context 与 LRC：让引擎能在任务间切换

GPU 要在多个进程的任务间切换，就需要**保存/恢复每个任务的硬件状态**。这份状态叫 **LRC（Logical Ring
Context，逻辑环上下文）**——一块 GPU 内存，存着该上下文的寄存器镜像、ring 指针等。切换上下文 = 让引擎
加载另一份 LRC。现代 Intel（Xe）由 **GuC 固件**调度这些上下文（第 19、20 章），CPU 只管把任务排进
队列。这里先建立"每个任务有自己的 context/LRC，引擎靠切换 LRC 来复用"的概念。

### 1.8.2.1 怎么"戳" GPU：tail 更新、doorbell 与提交模型预览

1.2.2 说提交 = "写命令 + 戳一下 + 等中断"。这里把"戳一下"讲清楚，它有两种历史形态：

- **直接写 ring tail（execlists/传统）**：CPU 通过 MMIO 写引擎的 tail 寄存器，告诉 CS"命令到这了"。
  CS 立即（或被调度后）开始消费。i915 的 execlists 大体如此（第 19 章对照）。
- **doorbell（GuC 提交，Xe 主线）**：CPU 把工作描述写进一块与 **GuC 固件**约定的内存（队列/工作项），
  然后写一个 **doorbell** 通知 GuC，由 **GuC 固件**决定把哪个 context 放到哪个引擎上跑。CPU 不再
  直接操作引擎 tail，调度交给固件（第 19、20 章）。

为什么演进到 doorbell + 固件调度？回扣 1.7.1 的量化：**MMIO 读/写贵、CPU 直接调度引擎要频繁碰寄存器**。
让 GuC 固件在 GPU 侧调度，CPU 侧大多只需写内存 + 敲一次 doorbell，**大幅减少 CPU↔GPU 的 MMIO 往返**，
并支持更灵活的优先级/抢占。代价是多了一层固件、调试更间接（第 20 章会讲 GuC 的 CTB 通信与调试）。

**无论哪种方式，1.6.4 的内存正确性都成立**：写命令 → 写屏障 → 敲 doorbell/写 tail → (必要的 posting
read)。doorbell 只是"戳"的载体变了，"先让数据可见、再通知"的铁律不变。

### 1.8.3 引擎分类：Xe 代码里的六类

GPU 内有多个专用引擎，各司其职。Xe 在 `xe_hw_engine_types.h:15` 把引擎分类编号：

```c
// drivers/gpu/drm/xe/xe_hw_engine_types.h:15
enum xe_engine_class {
    XE_ENGINE_CLASS_RENDER        = 0,  // 3D 渲染 + 通用(RCS)
    XE_ENGINE_CLASS_VIDEO_DECODE  = 1,  // 视频解码(VCS/VDBOX)
    XE_ENGINE_CLASS_VIDEO_ENHANCE = 2,  // 视频后处理(VECS/VEBOX)
    XE_ENGINE_CLASS_COPY          = 3,  // 拷贝/blitter(BCS)
    XE_ENGINE_CLASS_OTHER         = 4,  // 其他(如 GSC)
    XE_ENGINE_CLASS_COMPUTE       = 5,  // 计算(CCS)
};
```

| 类 | 俗称 | 干什么 |
|----|------|--------|
| RENDER (RCS) | 渲染引擎 | 3D 图形管线 + 部分通用计算 |
| COMPUTE (CCS) | 计算引擎 | 专用 GPGPU/计算（较新代际独立出来） |
| COPY (BCS) | blitter | 高速内存拷贝/填充（如缓冲搬运、清屏） |
| VIDEO_DECODE (VCS) | 解码引擎 | 硬件视频解码 |
| VIDEO_ENHANCE (VECS) | 增强引擎 | 视频后处理/去噪等 |
| OTHER | — | GSC 等管理用途 |

多引擎可**并行**工作（解码的同时渲染、blitter 搬数据），这正是吞吐机器的体现。每个引擎都有自己的
CS + ring（1.8.1）。第 13 章会把它们落到具体硬件拓扑（tile/GT 下挂多少个各类引擎）。

### 1.8.4 计算阵列：Xe-core 与 EU 的吞吐式并行

引擎前端取命令，真正"算"的是后端的**执行单元阵列**。Intel 的层次（Xe 架构术语）：

- **EU（Execution Unit，执行单元）**：最小的执行体，内部是 **SIMD**（单指令多数据）——一条指令同时算
  多个数据通道（如 SIMD8/16/32 表示一次处理 8/16/32 个数据元素）。
- **Xe-core**（早期称 subslice）：一组 EU + 本地共享内存（SLM）+ 采样器等的集合。
- 更上层：多个 Xe-core 组成更大的块，直至整个 GPU（第 13 章 tile/GT 拓扑）。

**为什么这样设计**（回扣 1.2.1 吞吐哲学）：图形/计算负载是**海量同质数据并行**（给一百万个像素做
同样的着色），用"很多简单 EU + 每个 EU 用 SIMD 一次算一批 + 线程多到能盖住内存延迟"远比"少数强核"
高效。GPU 不靠大缓存和乱序去降低单任务延迟，而靠**超额并发**：某个线程等内存时，EU 立刻切到别的
就绪线程，硬件利用率因此维持很高。

### 1.8.5 三类工作负载与三条管线

GPU 硬件为三类负载提供（部分共享、部分专用的）管线：

- **3D / 渲染管线**：顶点 → 光栅化 → 像素着色 → 输出合并。命令是 `3DSTATE_*` / `3DPRIMITIVE`（第 16 章）。
- **Compute / GPGPU 管线**：无图形固定功能，直接派发计算线程网格（`COMPUTE_WALKER`/`GPGPU_WALKER`）。
  OpenCL/Level Zero/CUDA 类负载走这里。
- **Media 管线**：固定功能的视频编解码/处理，走 VCS/VECS 引擎。

三者很多时候共用同一批 EU 计算资源，但前端固定功能不同。理解"同一堆 EU、不同前端管线"有助于看懂
为什么渲染、计算、视频能在一块 GPU 上并存调度。

### 1.8.6 GPU 的内存层次全景（把散落各处的概念收口）

前面在不同小节零散提到 VRAM、系统内存、stolen、L3/LLC。这里用一张表把 GPU 的内存层次收口，作为
第 11、15 章的索引：

| 层级 | 在哪 | 速度/容量 | 谁访问 | 一致性 | 相关代码/章节 |
|------|------|-----------|--------|--------|----------------|
| 寄存器 | GPU 内 | 最快/极小 | CS、固定功能 | — | `xe_mmio.c`、第 14 章 |
| L1 / SLM | Xe-core 内 | 很快/小 | EU、线程组共享 | 线程组内 | 第 13 章 |
| L2 / L3（/L4） | GPU 内 | 快/中 | 各引擎 | 域间不一定一致（1.6.6） | `xe_device_td_flush`、第 15/21 章 |
| LLC | CPU 封装（集显） | 快/中 | 集显 GPU 与 CPU 共享 | 可达 CPU↔GPU 一致 | 1.2.3、1.6.3 |
| VRAM / LMEM | 独显板载 | 高带宽/大（GB 级） | GPU 本地，CPU 经 BAR2 窗口 | 与 CPU 缓存非一致 | `xe_vram.c`、TTM 第 11 章 |
| 系统内存 | 主存 | 中/最大 | CPU 直接；GPU 经 DMA+GTT | 独显 sysmem 默认 WB 且一致(`xe_bo.c:497`) | 1.6.5、第 09/11 章 |
| stolen memory | 系统内存里被 BIOS/驱动预留的一块 | = 系统内存 | GPU（固件、帧缓冲等） | — | `xe_ttm_stolen_mgr.c` |

关于 **stolen memory**：它是开机时由固件/BIOS 从系统内存"偷"出来预留给 GPU 的一段（集显尤为重要，
因为没有独立 VRAM），用于 framebuffer、固件等早期/特殊用途。Xe 用 `xe_ttm_stolen_mgr`
（`xe_ttm_stolen_mgr.c:29` `struct xe_ttm_stolen_mgr`，字段 `stolen_base` 记录其物理基址）把它纳入
TTM 管理。独显上 stolen 则取自 VRAM 起始处（见该文件对 `LMEM_BAR` 的判断）。

**数据放哪里 = 性能与正确性的核心决策**：热数据放 VRAM（带宽高，1.7.1）、CPU 要频繁改的放系统内存
或集显 LLC、扫描输出缓冲要注意非一致需刷缓存（1.6.6）。这套"放置 + 迁移 + 缓存属性"的决策正是 TTM
（第 11 章）与 PTE/PAT（第 15 章）的核心职责。本章先建立"层次 + 取舍"的全景。

### 1.8.7 如何对照 Bspec 验证

本章的硬件论断都可在 Intel Bspec 找到出处。示范（第 00 章 0.8 方法）：

```
$ git grep -nE "Bspec: ?[0-9]+" drivers/gpu/drm/xe/xe_hw_engine.c
drivers/gpu/drm/xe/xe_hw_engine.c:428:   * Bspec: 72161
```

读到 `xe_hw_engine.c` 里某段引擎初始化不懂时，就近找 `Bspec: 72161` 这样的锚点，再去 Intel Graphics
Documentation 按编号检索该条目（涉及引擎相关行为）。第 13、14、16 章会大量使用这种"源码锚点 ↔ Bspec
编号"的双向验证。

---

## 1.9 综合实例：跟踪一次寄存器写与一次中断（放在一起看）

把本章的"四条通路 + 内存序"在一个可观测的实例里走一遍。目标：在有 Intel GPU 的机器上（或借助
tracepoint 概念理解），观察"CPU 写寄存器"和"GPU 发中断"两件事。

### 1.9.1 看 GPU 在系统里的样子（配置空间 → BAR）

```
$ lspci -nn -v -s 00:02.0      # 假设 GPU 在 00:02.0
00:02.0 VGA compatible controller [0300]: Intel Corporation Device [8086:xxxx]
    ...
    Memory at xxxxxxxx (64-bit, non-prefetchable) [size=16M]    # ← 这就是 GTTMMADR_BAR(BAR0): MMIO+GTT
    Memory at xxxxxxxx (64-bit, prefetchable)     [size=256M]   # ← LMEM_BAR(BAR2): VRAM 窗口(可能 small BAR)
    Capabilities: [..] MSI-X: Enable+ Count=...                 # ← 中断方式: MSI-X
    Capabilities: [..] Resizable BAR ...                        # ← 支持 rebar
    Kernel driver in use: xe
```

把这份输出和本章对应：`[8086:xxxx]`=配置空间身份（1.3.2）；两块 Memory=BAR0/BAR2（1.3.3）；
`MSI-X`=中断方式（1.5.4）；`Resizable BAR`=rebar（1.7.2）；`driver in use: xe`=被 Xe 认领（0.8.4）。
**一条 `lspci` 把本章前半的硬件接口全印证了。**

### 1.9.2 抓"每一次寄存器读写"（tracepoint）

1.5.2/1.5.3 里每次 `xe_mmio_read32/write32` 都打 `trace_xe_reg_rw`。于是：

```
$ echo 1 > /sys/kernel/tracing/events/xe/xe_reg_rw/enable    # (事件名以实际为准, 用 ls 查 events/xe/)
$ cat /sys/kernel/tracing/trace
   ... xe_reg_rw: read=0 addr=0x... val=0x... len=4    # 一次寄存器写
   ... xe_reg_rw: read=1 addr=0x... val=0x... len=4    # 一次寄存器读(可能是 posting read)
```

你能**逐条看到驱动对硬件的每一次寄存器操作**——这就是 1.5 代码里那个 tracepoint 的价值。配合
`addr` 去 Bspec 查这是哪个寄存器，就把"软件动作 ↔ 硬件寄存器"完全对上了。

### 1.9.3 观察中断计数

```
$ grep -i -E 'xe|i915' /proc/interrupts     # 看 GPU 的中断计数随负载增长
  126:  ...  PCI-MSIX-...  xe          # 跑点 GPU 负载, 这个计数会涨
```

中断计数随 GPU 负载增长，印证 1.5.4 的 `xelp_irq_handler` 在被反复调用——每次 GPU 完成一批工作就发
一次 MSI-X，CPU 进中断处理、分层 demux、唤醒等待者。

> 把 1.9.1（接口）、1.9.2（CPU→GPU 寄存器）、1.9.3（GPU→CPU 中断）连起来，你就**亲眼**看到了本章
> "四条通路"中的三条（配置空间、MMIO、中断）在真实系统里运转。第四条 DMA 不易直接观测，但你已在
> 1.6 理解了它的内存正确性要求。

---

## 1.10 横向关联：本章是后续硬件章的地基

| 本章概念 | 在哪展开 |
|----------|----------|
| 寄存器访问、`REG_BIT`/`REG_GENMASK`、forcewake | 第 14 章 寄存器/电源 |
| GTT/GGTT（在 BAR0 里）、PPGTT、PTE 缓存位 | 第 15 章 地址翻译 |
| CS/ring/batch、MI 命令、3D/compute/media 命令 | 第 16 章 GPU 命令与指令集 |
| tile/GT/引擎拓扑、Xe-core/EU 代际差异 | 第 13 章 Intel 架构 |
| PCI 探测、BAR 映射、rebar 在 probe 里的位置 | 第 17 章 Xe 设备初始化 |
| VRAM/LMEM_BAR、stolen、TTM 迁移、缓存属性 → PTE | 第 11、15、18 章 |
| MSI-X → DRM 事件 → vblank/提交完成 | 第 04、19、20 章 |
| ATS/PASID、SVM、CPU↔GPU 共享地址 | 第 18 章 |

**跨切面主题在本章的体现**：

- **性能**：1.7 全节（带宽/延迟/rebar 的量化）。
- **可靠性**：1.6 的内存序——错了就是偶发 hang，是最难查的可靠性问题。
- **保护与隔离**：context/LRC（任务间状态隔离）、SR-IOV VF 寄存器重定向（1.5.2 (3)）。
- **演进趋势**：small BAR→rebar、execlists→GuC 调度（1.8.2）、RCS 兼做计算→独立 CCS（1.8.3）。

---

## 1.11 谬误与陷阱 + 调试

### 谬误与陷阱

- **谬误 1：「GPU 像协处理器指令一样被 CPU 调用」。**
  没有"调用"。CPU 只能写它的寄存器/内存、被它中断（1.2.2）。一切高层操作都是这两个原语的组合。

- **谬误 2：「`writel` 返回了，GPU 就看到这个值了」。**
  MMIO 写是 posted 的（1.6.1），返回 ≠ 到达。要确保到达，做一次 posting read。这是 GPU 驱动最常见的
  一类时序 bug 来源。

- **谬误 3：「往命令缓冲写完，直接敲 doorbell 就行」。**
  必须在写命令与敲 doorbell 之间加写屏障（1.6.2），否则 GPU 可能"先看到新 tail、后看到命令"，执行到
  残缺命令 → 随机 hang/花屏。

- **谬误 4：「缓存属性无所谓，反正都是内存」。**
  大错。UC/WC/WB(LLC) 决定可见性与性能（1.6.3）。把 GPU 要读的缓冲放成 WB 又不刷缓存 → GPU 读旧值；
  把 WC 缓冲当强序用 → 数据乱序。

- **谬误 5：「`void __iomem *` 是普通指针，可以直接 `*p` 解引用」。**
  不行。`__iomem` 内存必须用 `readl/writel/memcpy_toio` 等访问；直接解引用是 bug（且 sparse 会报警）。

- **陷阱 6：把"集显能一致"的经验用到独显上。**
  集显挂 LLC 可能 CPU↔GPU 一致，独显 VRAM 在 PCIe 另一端通常**不**一致（1.2.3、1.6.3）。跨形态移植
  代码时若不重新审视一致性假设，会踩雷。

- **陷阱 7：small BAR 下假设"CPU 能看到整块 VRAM"。**
  没 rebar 时 CPU 可见 VRAM 可能仅 256MB（1.7.2），直接按全 VRAM 地址 CPU 访问会越界。

### 调试小抄

| 想知道 | 手段 |
|--------|------|
| GPU 的 BAR/中断方式/驱动 | `lspci -nn -v -s <bdf>`（1.9.1） |
| 驱动对硬件的每次寄存器读写 | tracepoint `xe/xe_reg_rw`（1.9.2），`addr` 配 Bspec 查 |
| 中断是否在发、发多少 | `/proc/interrupts` 看 xe/i915 行计数（1.9.3） |
| 怀疑内存序/可见性 bug | 复查 doorbell 前后是否有 `wmb()`/posting read；用 lockdep/KASAN 辅助 |
| 某寄存器位含义 | 源码 `REG_BIT`/`REG_GENMASK` 定义 + 就近 `Bspec: NNNNN` 查（1.8.6、第14章） |
| 一段引擎/中断代码的硬件依据 | `git grep -nE "Bspec: ?[0-9]+" <file>` |

---

## 1.12 随堂练习（Practice Problems）

**练习 1.1** 用一句话分别概括 CPU 与 GPU 的优化目标，并据此解释"为什么 GPU 用海量简单 EU 而不是
少数强核"。

**练习 1.2** CPU 要"指挥" GPU、GPU 要"回报" CPU，分别用哪条通路？（从 MMIO/DMA/中断/配置空间 里选）

**练习 1.3** Intel GPU 的 BAR0（`GTTMMADR_BAR`）里同时放了哪两类东西？VRAM 在哪个 BAR？

**练习 1.4** `writel(val, mmio->regs + addr)` 返回后，能否断定 GPU 寄存器已经是 `val`？要确保的话怎么做？

**练习 1.5** 把命令写进 ring buffer 之后、推进 tail 之前，为什么要 `wmb()`？不加会出什么问题？

**练习 1.6** WC（写合并）缓冲适合什么访问模式？用它装"CPU 写、GPU 读"的命令时，灌完为什么还要屏障？

**练习 1.7（量化）** 已知独显 VRAM 本地带宽可比 PCIe 高约一个数量级。据此解释"为什么热数据要驻留
VRAM、尽量少跨 PCIe 搬运"，并说一个这条原则在驱动里的体现（哪个子系统负责）。

### 随堂练习即时解答

**解 1.1** CPU 优化单任务低延迟，GPU 优化海量任务高吞吐。图形/计算是海量同质数据并行，用"很多简单
EU + SIMD + 超额并发盖延迟"比"少数强核"在单位面积/功耗下吞吐高得多（1.2.1、1.8.4）。

**解 1.2** CPU 指挥 GPU：主要靠 **MMIO**（写寄存器/doorbell），数据靠 GPU 的 **DMA** 去取；GPU 回报
CPU：靠**中断（MSI-X）**。配置空间用于开机枚举配置（1.3.1）。

**解 1.3** BAR0 放 **MMIO 寄存器 + GGTT**；VRAM 在 **BAR2（LMEM_BAR）**（1.3.3、`regs/xe_bars.h:8`）。

**解 1.4** 不能。MMIO 写是 posted 的，返回 ≠ 到达（1.6.1）。要确保，做一次该设备的 MMIO 读
（posting read），读会把前面的写挤到设备。

**解 1.5** `wmb()` 保证命令写先于 tail 写对 GPU 可见；不加则 GPU 可能先看到新 tail、却还没看到对应
命令，执行到残缺命令 → 随机 hang/花屏（1.6.2）。

**解 1.6** WC 适合"CPU 大批量顺序写、GPU 读"的模式（写被合并成突发，吞吐高）。但 WC 弱序，灌完命令
必须 `wmb()`/sfence 把写刷出并定序，再通知 GPU，否则 GPU 读到乱序/不全的命令（1.6.3、1.6.2）。

**解 1.7** 因为 VRAM 本地带宽远高于 PCIe，数据留在 VRAM 访问快得多，跨 PCIe 搬运是瓶颈。体现：TTM
负责把热数据迁移/驻留到 VRAM、内存压力下才驱逐（第 11 章），尽量减少跨 PCIe 往返（1.7.1）。

---

## 1.13 课后练习题

> 仅题目。答案见 [`answers/01-gpu-hardware-answers.md`](./answers/01-gpu-hardware-answers.md)。★ 为动手/综合题。

### L1 概念辨析

**1.13.1** 列出 CPU↔GPU 交互的"四条通路"，各用一句话说明方向与用途。

**1.13.2** 解释 posted write 与 non-posted read 的区别，以及"posting read"为什么能保证前面的写已送达。

**1.13.3** 用自己的话区分 UC / WC / WB(LLC) 三种缓存属性的行为与典型用途。

**1.13.4** 集显与独显在"CPU↔GPU 内存一致性"上的根本差异是什么？根源是什么（硬件结构）？

**1.13.5** 解释 GPU"吞吐优化"哲学如何体现在 EU/SIMD/超额并发上；它和 CPU 用大缓存+乱序降延迟有何不同。

**1.13.6** ring buffer 的 head 与 tail 各由谁推进？`head==tail` 表示什么？

### L2 数据结构与源码阅读

**1.13.7** 阅读 `xe_mmio_init`（`xe_mmio.c:122`）与 `struct xe_mmio` 字段，说明 `regs`（`void __iomem *`）
为什么必须用 `readl/writel` 访问而非直接解引用。

**1.13.8** 阅读 `xe_mmio_write32`（`xe_mmio.c:179`）四个步骤，指出哪一行是"真正写硬件"，并解释
`trace_xe_reg_rw` 的调试价值。

**1.13.9** 阅读 `xe_mmio_read32`（`xe_mmio.c:192`）与 `mmio_flush_pending_writes`（:131），解释那 4 次
"哑写"在解决什么问题，并把它和 1.6 的内存序联系起来。

**1.13.10** 阅读 `regs/xe_bars.h`，写出 `GTTMMADR_BAR`/`LMEM_BAR`/`VF_LMEM_BAR` 的编号与用途；再到
`xe_mmio.c:100` 与 `xe_vram.c:49` 看它们分别被谁 `pci_iomap`/`pci_resource_*`。

**1.13.11 ★** 阅读 `xe_hw_engine_types.h:15` 的 `enum xe_engine_class`，列出六类引擎及其俗称/职责；
再 `git grep` 找出 Xe 里至少一处按 `engine->class` 分支处理的代码，说明为什么要分类。

### L3 机制分析

**1.13.12** 画出"CPU 安全地把一批命令交给 GPU 并等完成"的内存视角全序列（命令写→屏障→tail/doorbell
→posting read→GPU DMA 执行→完成中断→CPU 读状态），并标注每一步防的是什么错误。

**1.13.13** 阅读 `xelp_irq_handler`（`xe_irq.c:412`），描述"主中断寄存器 `GFX_MSTR_IRQ` → 分层 demux"
的工作方式；解释 `master_ctl == 0` 时返回 `IRQ_NONE` 的意义（提示：共享中断线）。

**1.13.14** 解释为什么"高频提交路径要尽量少做 MMIO 读"，用 1.7.1 的延迟量级支撑，并联系第 19/20 章
的 doorbell + 内存状态设计。

### L4 设计与对比

**1.13.15** 对比集显与独显的优化方向（分别要省什么），并各给一条驱动层面的具体策略。

**1.13.16** small BAR vs resizable BAR：说明 small BAR 给上传路径带来的额外成本，rebar 如何消除它；
用"硬件能力改变软件数据路径"的视角总结。

**1.13.17** Xe 为什么把寄存器包装成 `struct xe_reg` 并配 `REG_BIT`/`REG_GENMASK` 宏，而不用裸偏移？
从"上万寄存器 + 多代 + 虚拟化"角度论述其权衡。

### L5 动手 / 调试

**1.13.18 ★** 在有 Intel GPU 的机器上跑 `lspci -nn -v -s <bdf>`，把输出里的 Vendor/Device、两块
Memory(BAR)、MSI-X、Resizable BAR、driver in use 一一对应到本章 1.3/1.5/1.7 的概念。无 Intel 硬件
时，对任意 PCIe 设备做同样练习并指出哪些字段 GPU 特有。

**1.13.19 ★** 启用 `xe/xe_reg_rw` tracepoint（`/sys/kernel/tracing/events/xe/`，名称以实际为准），
跑一点 GPU 负载，截取若干条记录，区分哪些是写、哪些可能是 posting read。

**1.13.20 ★** 观察 `/proc/interrupts` 中 xe/i915 行，在空闲与跑负载（如 `glxgears`/计算任务）两种状态
下各采样一次，解释计数变化与 `xelp_irq_handler` 被调用频率的关系。

**1.13.21 ★（调试）** 设想一个 bug：某驱动在写命令后忘了 `wmb()` 就敲 doorbell，偶发 GPU hang。描述
你会用什么手段（trace/复查代码/对照本章 1.6）定位它，以及修复方案。

### 综合大题

**1.13.22 ★（综合）** 用本章的"四条通路 + 内存序"框架，完整叙述"Mesa 提交一次绘制到 GPU 画完"在
**硬件层面**发生了什么（不涉及具体 ioctl，只讲硬件交互）。这道题贯通 1.2–1.8，并预接第 16/19 章。

**1.13.23 ★（综合·量化）** 给定假想数字：PCIe 带宽 16 GB/s、VRAM 带宽 400 GB/s、单次 MMIO 读 400ns。
分析：① 上传 1GB 纹理走 PCIe 与在 VRAM 内拷贝各约需多久？② 一个每帧做 1000 次 MMIO 读轮询的设计，
仅轮询就花多少时间，对 60fps（每帧 16.6ms）预算占比多少？由此论证"少过 PCIe、少 MMIO 读"两条原则。

**1.13.24 ★（综合·硬件视角）** 用 `git grep -nE "Bspec: ?[0-9]+" drivers/gpu/drm/xe` 找一条引擎或
中断相关的 Bspec 锚点，记录 `file:line` 与编号，并尝试用第 00 章 0.8.2 的方法去 Intel 文档定位其主题。

---

## 1.14 本章小结

- **GPU 是挂在 PCIe 上的吞吐优化协处理器**，CPU 对它的全部操作 = 写寄存器/内存（MMIO/DMA）+ 被中断。
  没有"函数调用"，只有"写命令 + 戳一下 + 等中断"。
- **四条通路**：配置空间（枚举）、MMIO（CPU→GPU 寄存器，BAR0=GTTMMADR）、DMA（GPU→系统内存）、
  中断（GPU→CPU，MSI-X）。VRAM 在 BAR2（LMEM_BAR），独显才有意义。
- **内存序是灵魂**：MMIO 写是 posted 的（写≠到达，需 posting read）；命令写与 doorbell 之间需写屏障；
  缓存属性 UC/WC/WB(LLC) 决定可见性与性能。绝大多数偶发 hang 根在这里。
- **内部执行机器**：命令流处理器（CS）从 ring 顺序取命令、跳进 batch；context/LRC 支持任务切换；
  六类引擎（RCS/CCS/BCS/VCS/VECS/OTHER）可并行；EU + SIMD + 超额并发实现吞吐。
- **量化直觉**：VRAM 带宽 ≫ PCIe ≫（少过 PCIe）；MMIO 读是数百纳秒的 PCIe 往返（少读）；
  small BAR→rebar 直接改变上传路径。
- 一切论断都能落到 Xe 真实代码（`xe_mmio.c`/`xe_irq.c`/`xe_hw_engine_types.h`/`regs/xe_bars.h`）与
  Bspec 锚点，做到可查可验。

### 知识点清单（自检）

- [ ] 四条通路的方向、用途，以及 BAR0/BAR2 各放什么。
- [ ] posted write / posting read / wmb 的作用与触发场景。
- [ ] UC/WC/WB(LLC) 的行为与典型用途；集显 vs 独显一致性差异。
- [ ] CS/ring(head/tail)/batch、context/LRC、六类引擎、EU/SIMD 的心智图。
- [ ] 带宽/延迟量级与"少过 PCIe、少 MMIO 读"两条原则；small/resizable BAR。
- [ ] 能在 `xe_mmio.c`/`xe_irq.c` 指认寄存器读写与中断分层 demux。

### 延伸阅读

- 源码：`drivers/gpu/drm/xe/xe_mmio.c`（寄存器访问）、`xe_irq.c`（中断）、`regs/xe_bars.h`（BAR）、
  `xe_hw_engine_types.h`（引擎类）、`xe_vram.c`/`xe_pci_rebar.c`（VRAM/rebar）。
- 规格：Intel Graphics PRM/Bspec 中关于 GPU 架构、引擎、内存的卷（检索方法见第 00 章 0.8.2、附录 C）。
- 进阶：内核 `Documentation/core-api/dma-api.rst`（DMA 与一致性）、`Documentation/memory-barriers.txt`
  （内存屏障的权威解释，强烈建议结合 1.6 通读）。

> **下一章预告**：第 02 章《DRM 依赖的内核基础设施》。有了硬件地基，我们再补齐读 DRM 代码必备的内核
> 设施——设备/驱动模型、ww-mutex、kref/RCU、dma-mapping/iommu、workqueue/中断子系统。它们是连接
> "硬件"（本章）与"DRM 框架"（第三章起）的胶水。
