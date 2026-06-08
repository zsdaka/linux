# PVM 技术概览：背景、动机与整体架构

## 1. 什么是 PVM

PVM（Pagetable-based Virtual Machine）是由蚂蚁集团（Ant Group）和阿里云联合研发的新型 x86 虚拟化框架，其核心思想是：**利用 x86 分页机制和内核入口点劫持，在完全软件层面实现虚拟机隔离，无需 VT-x/AMD-V 嵌套虚拟化硬件支持**。

PVM 以 73 个 patch 的 RFC 系列（主题为"KVM: x86/PVM: Introduce a new hypervisor"）于 2024 年 2 月 26 日由 Lai Jiangshan 提交至 LKML，并于 SOSP 2023 正式发表学术论文。在 RFC 提交之前，PVM 已在阿里云和蚂蚁集团内部生产环境运行，支撑每日数万个 Kata Containers 安全容器。

## 2. 问题背景：云 VM 内的安全容器

### 2.1 Kata Containers 的困境

安全容器（如 Kata Containers）通过将每个容器运行在独立 VM 中来实现强隔离。这在裸金属服务器上工作良好，但在公有云 VM 内部署时面临根本性挑战：

```
用户视角：
  公有云 VM（L1 Guest）
    └── Kata Containers（需要 L2 Guest VM）
          └── 容器工作负载

问题：L2 Guest VM 需要 Nested KVM（L2 VMX/SVM），
     但 99% 的云厂商在 L1 Guest 中禁用了 VMX/SVM 能力
```

云厂商禁用嵌套虚拟化的原因很充分：
- **安全**：嵌套虚拟化扩大了攻击面（VMCS 逃逸、shadow VMCS 漏洞）
- **性能**：两层 VM-exit 开销显著，TLB miss 代价翻倍
- **复杂性**：嵌套 VMX/SVM 代码路径极为复杂，维护成本高

### 2.2 现有替代方案及其缺陷

| 方案 | 原理 | 缺陷 |
|------|------|------|
| 嵌套 KVM | 硬件辅助嵌套虚拟化 | 云厂商普遍禁用 |
| QEMU/TCG | 纯软件模拟 CPU | 性能开销 10-100x，生产不可用 |
| gVisor | 系统调用拦截沙箱 | 非完整 VM 隔离，Linux ABI 覆盖不全 |
| Firecracker | 精简 KVM VM | 同样需要硬件虚拟化支持 |
| Xen PV | 半虚拟化，无需完整 VMX | 需要独立的 Xen 宿主，无法跑在 Linux KVM 宿主上 |

**PVM 的定位**：在 Linux KVM 宿主上（即公有云 VM 内），无需 VMX，以生产可接受的性能运行完整 Linux Guest。

### 2.3 PVM 如何解决问题

```
PVM 的目标环境：
  物理机
    └── L1 KVM（标准 VMX/SVM）
          └── 公有云 L1 Guest VM（无 VMX）
                └── PVM Hypervisor（在 L1 Guest 内核中，无需 VMX）
                      └── PVM Guest（Kata 安全容器）
                            └── 容器工作负载
```

PVM 以 KVM vendor module 的形式集成到 Linux 内核，作为 `kvm_x86_ops` 的一个实现，与 KVM VMX/SVM 模块并列。这意味着在支持 KVM 的宿主上，即使没有嵌套虚拟化能力，也可以启动 PVM 类型的 VM。

## 3. 核心技术创新概览

PVM 建立在四个相互配合的核心创新上：

### 3.1 Ring 压缩（CPL Compression）

**传统 KVM 的问题**：运行 Guest 需要 ring 0（CPL=0），而在已经是 Guest 的情况下再进入 ring 0 触发的是 VM-exit 而非真正的 ring 0 特权。

**PVM 的解决方案**：Guest 内核永远运行在硬件 CPL3，不需要 ring 0 特权。PVM 定义两个虚拟特权级：
- `smod`（supervisor mode）：对应 Guest 内核，硬件 CPL=3
- `umod`（user mode）：对应 Guest 用户态，硬件 CPL=3

两者都是 CPL3，特权分离通过**独立的 CR3 页表**实现：supervisor 页表中，Guest 内核页标记为只有 supervisor 可访问（通过 PKS），用户页标记为用户可访问；user 页表则相反。

这与传统的"ring 压缩"（ring 1/2 技术）不同，PVM 完全压缩到单个 ring 3，并依赖 x86 PKS（Protection Keys for Supervisor）而非段权限来实现 smod/umod 内存隔离。

### 3.2 整合入口（Integral Entry）

**传统 PV 虚拟化的问题**：Xen PV、lguest 等方案使用"从属入口"（subordinate entry）——宿主内核的事件（IRQ、系统调用）会先到达宿主，再被转发给 Guest。这带来额外的上下文切换开销，且需要 Xen 有独立的宿主软件栈。

**PVM 的设计**：Switcher 模块直接集成（hook）到 Linux 内核的入口点（`entry_64.S`）。具体地，`entry_SYSCALL_64` 和硬件中断入口在执行宿主路径之前，先检查 `tss_extra.switch_flags` 标志位，判断当前 CPU 是否正在执行 PVM Guest。

```
传统 KVM 事件流：
  硬件事件 → VM-exit → KVM 处理 → VM-entry → Guest

传统 PV 方案事件流：
  硬件事件 → Xen 宿主内核 → 转发给 Guest

PVM 事件流：
  硬件事件 → entry_64.S（Switcher 钩子）→ 
    if (in_pvm_guest) → 直接传递给 Guest 或 VM-exit
    else              → 正常宿主处理（零开销）
```

关键点：**宿主内核正常运行时，Switcher 路径添加的开销几乎为零**——仅一次内存检查（`tss_extra.switch_flags`），这个字段是 cache-line 对齐的，通常在 L1d 中，分支预测也会命中"不在 Guest"的路径。

### 3.3 排他地址空间 + 软件 Shadow Paging

**为何不用 EPT/NPT**：EPT/NPT 是 VT-x/AMD-V 特性，在没有嵌套虚拟化时不可用。

**PVM 的内存虚拟化**：
1. 在 Linux 宿主的 vmalloc 区域为每个 PVM Guest 预留一段连续的虚拟地址空间（通过 `MSR_PVM_LINEAR_ADDRESS_RANGE` 编码边界）
2. 在这段地址范围内，Guest 拥有"排他所有权"——宿主内核保证不在此范围内建立自己的映射
3. PVM 维护软件影子页表（shadow page table），跟踪 Guest 物理地址到宿主物理地址的映射
4. Guest 的页表修改通过 hypercall（`PVM_HC_LOAD_PGTBL`）通知超管

```
地址空间布局示意（4 级分页）：

0xFFFF800000000000           宿主 Linux 内核空间开始
    ...
    [PML4 index 256-287]     PVM Guest 排他区域（32 × 512GB）
    ...
0xFFFFFFFFFFFFFFFF

Guest 的所有内存映射都在 PML4[256..287] 范围内
宿主内核在此范围内不建立任何映射（通过 clone_host_mmu 跳过）
```

### 3.4 PKS 替代 SMAP 实现 smod/umod 隔离

**问题**：由于 Guest 内核（smod）和用户态（umod）都运行在 CPL3，x86 的 SMAP（Supervisor Mode Access Prevention）无法区分两者——SMAP 依赖 CPL，而两者 CPL 相同。

**PVM 的解决方案**：利用 Intel PKS（Protection Keys for Supervisor），这是 Intel Ice Lake 引入的特性：
- 页表项的 bit 59-62 编码 protection key（0-15）
- `MSR_IA32_PKRS`（Protection Key Rights for User pages）控制访问权限
- PVM 将 smod 页和 umod 页分配不同的 protection key
- 切换 smod/umod 时，更新 `MSR_IA32_PKRS` 来限制/允许访问

这样，即使 CPL 相同，通过 PKS 依然能强制 Guest 内核无法直接访问 Guest 用户内存（类似 SMAP 的语义）。

## 4. PVM 与其他虚拟化方案的深度对比

### 4.1 与标准 KVM（VMX）的对比

| 维度 | KVM VMX | PVM |
|------|---------|-----|
| 硬件要求 | 必须有 Intel VT-x / AMD-V | 仅需普通 x86-64，FSGSBASE、RDTSCP |
| Guest CPL | ring 0（CPL=0）| ring 3（CPL=3）|
| 内存虚拟化 | EPT（硬件辅助二级页表）| 软件影子页表 |
| 特权指令陷入 | 硬件 VM-exit | 通过 #GP、#PF 等异常软件陷入 |
| 中断虚拟化 | APIC virtualization、posted interrupt | 软件事件队列（PVCS event_flags）|
| 嵌套支持 | 支持（KVM on KVM）| 不支持（设计目标不同）|
| IOMMU 支持 | 完整 | 无 |
| 适用场景 | 通用 VM | 无嵌套硬件时的 PV 安全容器 |
| 启动时间 | 较快 | 较快（相比 QEMU/TCG）|
| 稳定状态性能 | 接近原生 | 约为 VMX 的 75-90%（SOSP 2023 数据）|

### 4.2 与 Xen PV 的对比

| 维度 | Xen PV | PVM |
|------|--------|-----|
| 入口设计 | 从属入口（subordinate entry）| 整合入口（integral entry）|
| 宿主依赖 | 需要 Xen Hypervisor | 在标准 Linux KVM 宿主上运行 |
| Guest 运行 CPL | ring 0（但被 Xen 截获）| ring 3 |
| 宿主内核修改 | 无（Xen 在 ring 0，Linux 在 ring 1）| 需要修改宿主 entry_64.S（Switcher）|
| 内存虚拟化 | PV 页表（Guest 直接管理宿主物理地址）| Shadow paging（超管维护影子页表）|
| 云 VM 可用性 | 需要 Xen 宿主（公有云一般是 KVM）| 在 KVM 宿主 VM 内直接使用 |
| 事件/中断 | Xen 事件通道（event channel）| PVCS event_flags 轮询/推送 |
| 向上游合并 | 已合并 | 尚在 RFC 阶段 |

**整合入口 vs 从属入口的性能影响**：Xen PV 的从属入口意味着每次硬件中断都需要 Xen 介入、存入事件队列、再通知 Guest；而 PVM 的整合入口中，硬件中断发生时 Switcher 代码直接判断是否在 PVM Guest 内，若在则直接将中断路由到 Guest 的中断处理，避免了 Xen 的迂回路径。

### 4.3 与 KVM 软件模拟（no-VMX）的对比

KVM 本身在没有 VMX 时无法工作（`kvm_intel.ko` 加载时强制检查 CPUID.1:ECX.VMX）。PVM 填补了这个空白，作为 KVM 框架内的第三条 vendor 路径（VMX、SVM、PVM）。

### 4.4 与 lguest 的对比

lguest 是 Rusty Russell 编写的轻量级 Linux in Linux PV 虚拟化（已于 Linux 4.14 移除），其思路与 PVM 最接近：Guest 在 ring 1 运行，通过段描述符陷入。PVM 的区别在于：
- lguest 使用 ring 1（现代 64-bit x86 已不支持 ring 1 有效分离）；PVM 用 ring 3 + PKS
- lguest 无法与现代 Linux（PIE 内核、KPTI 等）配合；PVM 专为现代内核设计
- lguest 没有独立的 shadow paging；PVM 有完整的影子页表管理

## 5. 整体架构图

```
┌──────────────────────────────────────────────────────────────────┐
│  Host Linux Kernel（运行在 L1 或裸金属）                          │
│                                                                  │
│  ┌────────────────────┐   ┌─────────────────────────────────┐   │
│  │  正常宿主内核路径    │   │  PVM Hypervisor（kvm/pvm/）      │   │
│  │  entry_64.S        │   │                                 │   │
│  │  ↕ (Switcher hook) │   │  vcpu_pvm  host_mmu  pvm.c     │   │
│  │  entry_64_switcher │   │                                 │   │
│  └────────────────────┘   └─────────────────────────────────┘   │
│            │                             │                       │
│            └──────────────┬──────────────┘                       │
│                           │                                      │
│                    tss_extra（per-CPU）                           │
│                    switch_flags / smod_cr3 / umod_cr3            │
│                    pvcs / retu_rip / smod_entry                  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                             │ VM-enter/exit
┌──────────────────────────────────────────────────────────────────┐
│  PVM Guest（运行在 CPL3）                                         │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Supervisor Mode（smod，Guest 内核，硬件 CPL=3）           │   │
│  │  entry_64_pvm.S   pvm_hypercall   pvm_irq_enable/disable  │   │
│  │  使用 smod_cr3（含 Guest 内核映射 + 受限用户页访问）         │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  User Mode（umod，Guest 用户态，硬件 CPL=3）               │   │
│  │  使用 umod_cr3（含用户内存映射 + 不可访问内核页）            │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  pvm_vcpu_struct (PVCS)：Guest/Hypervisor 共享状态页              │
│  event_flags / cr2 / event_vector / pkru / rip / rcx / r11      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

## 6. 关键设计权衡

### 6.1 软件影子页表的代价

Shadow paging 的主要开销是**页表同步**：每当 Guest 修改页表，超管需要更新影子页表。PVM 通过以下方式缓解：
- 使用 PCIDs（通过 `PVM_ASID_SHIFT=3` 映射），避免每次 CR3 切换都刷新 TLB
- `PVM_HC_TLB_FLUSH_CURRENT` 只刷新当前 ASID 的 TLB，不影响其他 ASID
- `PVM_HC_TLB_INVLPG` 实现细粒度单页失效

### 6.2 无 KPTI 的 Meltdown 风险

PVM 当前未实现 KPTI（内核页表隔离），原因是：
1. Guest 内核（smod）和用户态（umod）已经使用不同的 CR3（smod_cr3 vs umod_cr3）
2. 但 smod_cr3 中可能包含用户态映射（PKS 保护访问，但映射存在）
3. Meltdown 通过推测执行可能绕过 PKS
4. 这是 KVM 维护者（Sean Christopherson）在 RFC review 中指出的主要问题

### 6.3 non_pvm_mode 预引导仿真

Guest 在 BIOS/引导阶段可能以 32-bit 实模式或保护模式运行，而 PVM 的 CPL3 机制需要分页模式。为此，PVM 有一个 `non_pvm_mode` 状态，在 Guest 启用分页且自报为 PVM Guest 之前，用纯软件模拟（类似 QEMU/KVM 的 emulator）处理所有指令，代价是性能极差，但此阶段持续时间极短（bootloader 的前几百毫秒）。

### 6.4 PIE 内核要求

PVM Guest 必须使用 PIE（Position Independent Executable）编译的内核（`CONFIG_RANDOMIZE_BASE` + `CONFIG_PVM_GUEST`），因为：
- Guest 内核加载地址需要在 PVM 分配的地址范围内（PML4[256..287]）
- 传统 Linux 内核链接到 `0xffffffff80000000`，落在宿主 Linux 内核地址范围，与 PVM 排他地址空间冲突

## 7. 性能数据（SOSP 2023 论文）

论文在 Intel Xeon 上对 PVM 与 KVM VMX、KVM TCG 进行了对比：

| 测试 | KVM VMX | PVM | TCG |
|------|---------|-----|-----|
| UnixBench CPU（分）| 100% 基准 | ~92% | ~8% |
| 内存带宽 | 100% | ~85% | ~5% |
| 系统调用延迟（ns）| ~200 | ~350 | ~5000 |
| fork+exec（ops/s）| 100% | ~80% | ~2% |
| nginx QPS | 100% | ~88% | ~3% |

**结论**：PVM 稳定状态性能约为 VMX KVM 的 75-90%，显著优于 TCG。在不需要嵌套虚拟化硬件的场景下（公有云 VM 内），PVM 是唯一具有生产可用性能的选择。

## 8. PVM 在 KVM 框架中的位置

PVM 作为 KVM 的 vendor module 实现了 `kvm_x86_ops` 接口：

```c
// arch/x86/kvm/pvm/pvm.c
static struct kvm_x86_ops pvm_x86_ops __initdata = {
    .name                    = "kvm_pvm",
    .hardware_enable         = pvm_hardware_enable,
    .hardware_disable        = pvm_hardware_disable,
    .vcpu_create             = pvm_vcpu_create,
    .vcpu_run                = pvm_vcpu_run,
    .handle_exit             = pvm_handle_exit,
    .get_cpl                 = pvm_get_cpl,
    .set_segment             = pvm_set_segment,
    .get_segment             = pvm_get_segment,
    // ... 共约 100 个操作
};
```

KVM 框架通过 `kvm_x86_ops` 调度，对 QEMU 等用户态 VMM 完全透明——QEMU 用同样的 `/dev/kvm` ioctl 接口（`KVM_RUN`、`KVM_SET_REGS` 等）控制 PVM vCPU，无需任何修改。

## 9. 部署架构

实际的 Kata Containers on PVM 部署：

```
阿里云公有云 ECS 实例（L1 KVM Guest，无 VMX）
  ├── containerd（容器运行时）
  │     └── kata-runtime（Kata Containers shim）
  │           └── QEMU → /dev/kvm（PVM 模式）
  │                 └── PVM Guest（Linux 内核 + CONFIG_PVM_GUEST）
  │                       └── 容器工作负载（安全隔离）
  └── 普通 Docker 容器（无 VM 隔离）
```

PVM 对 Kata Containers 完全透明：kata-runtime 只需将 QEMU machine type 设为 PVM 兼容类型，其余 VM 管理（内存、设备、vCPU）接口不变。

## 10. 源码位置总览

```
arch/x86/kvm/pvm/           PVM Hypervisor 核心
  pvm.c                      主模块（3257 行）
  pvm.h                      数据结构与常量
  host_mmu.c                 Shadow 页表管理

arch/x86/entry/
  entry_64_switcher.S        宿主侧 Switcher（~270 行汇编）
  entry_64_pvm.S             Guest 侧 PVM 入口汇编

arch/x86/include/asm/
  switcher.h                 tss_extra 结构与 switch 常量
  pvm.h                      PVM ABI 常量（MSR、hypercall 号等）

Documentation/virt/kvm/x86/
  pvm-spec.rst               PVM ABI 规范（989 行）
```

详细各模块技术实现见各子文档：
- [01-ring-compression.md](01-ring-compression.md) — Ring 压缩详解
- [02-switcher.md](02-switcher.md) — Switcher 汇编逐行分析
- [03-data-structures.md](03-data-structures.md) — 核心数据结构
- [04-msr-interface.md](04-msr-interface.md) — MSR 接口
- [05-shadow-paging.md](05-shadow-paging.md) — Shadow 页表管理
- [06-hypercall.md](06-hypercall.md) — Hypercall 接口
- [07-event-delivery.md](07-event-delivery.md) — 事件交付机制
- [08-pks-smap.md](08-pks-smap.md) — PKS 替代 SMAP
- [09-boot-detection.md](09-boot-detection.md) — 引导探测
- [10-hardware-state.md](10-hardware-state.md) — 硬件寄存器映射
- [11-patch-series.md](11-patch-series.md) — Patch 系列结构
- [12-limitations.md](12-limitations.md) — 限制与社区进展
