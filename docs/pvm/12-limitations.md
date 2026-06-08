# PVM 已知限制、TODO 项与社区状态

## 1. 概述

PVM 是 RFC（Request for Comments）状态的 patch 系列，截至 2026 年 6 月尚未合并进 Linux 主线。代码已在阿里云和蚂蚁集团生产环境中大规模部署，但仍存在多项技术限制和未完成的工作，其中一些被 KVM 维护者认为是阻塞上游合并的关键问题。

本文汇总了所有已知限制，分为以下几类：
1. **硬性限制**：代码中明确检查并返回 `-EOPNOTSUPP` 的场景
2. **功能存根（Stub）**：代码存在但未实现或返回 0
3. **内核配置不兼容**：特定 `CONFIG_*` 选项无法与 PVM 共存
4. **架构约束**：设计层面的限制，不易修复
5. **安全问题**：speculation side-channel 保护不完整
6. **TODO 项**：源码注释中标记的待完成工作
7. **社区反馈与上游进展**：maintainer 的评审意见

---

## 2. 硬性限制（模块加载检查）

这些限制在 `hardware_cap_check()` 函数中以 `return -EOPNOTSUPP` 强制检查，PVM 模块在不满足条件时无法加载。

### 2.1 不支持宿主 KPTI（内核页表隔离）

**来源**：`arch/x86/kvm/pvm/pvm.c`，`hardware_cap_check()`:

```c
/*
 * switcher can't be used when KPTI. See the comments above
 * SWITCHER_SAVE_AND_SWITCH_TO_HOST_CR3
 */
if (boot_cpu_has(X86_FEATURE_PTI)) {
    pr_warn("Support for host KPTI is not included yet.\n");
    return -EOPNOTSUPP;
}
```

**技术原因**：

KPTI（Kernel Page Table Isolation，针对 Meltdown 的缓解）将内核地址空间分成两个 CR3：
- `kernel_cr3`：内核代码执行时使用，包含内核页表
- `user_cr3`：用户空间执行时使用，内核部分只有最小映射（trampoline）

Switcher 使用 `enter_cr3` / `host_cr3` / `smod_cr3` / `umod_cr3` 四个 CR3 进行切换。当宿主启用 KPTI 时，这四个 CR3 都需要成对存在（kernel 版本 + user 版本），使得 CR3 切换逻辑加倍复杂。

更具体地说，`switcher_return_from_guest` 中有一个宏 `SWITCHER_SAVE_AND_SWITCH_TO_HOST_CR3`，注释说明：当 KPTI 启用时，这里需要特殊处理，但目前代码尚未实现。

**影响**：  
几乎所有运行 Linux ≥ 4.15（2018年）的 x86-64 系统，如果 CPU 易受 Meltdown 攻击（通常是 2019 年以前的 Intel CPU），就会在内核启用 KPTI。这意味着 PVM **无法在大量老 Intel 服务器上运行**（比如 Haswell、Broadwell、Skylake 系列，这些都是数据中心主力机型）。

在阿里云等公有云环境，宿主机已通过其他方式（微码更新、硬件隔离）缓解 Meltdown，通常可以关闭 KPTI（`nopti` 内核参数），所以生产环境有对策。但对普通用户，KPTI 通常是默认开启的。

**状态**：标记为"Not included yet"——未来计划实现，但需要大量工程工作。

---

### 2.2 必须有 FSGSBASE

```c
if (!boot_cpu_has(X86_FEATURE_FSGSBASE)) {
    pr_warn("FSGSBASE is required per PVM specification.\n");
    return -EOPNOTSUPP;
}
```

PVM 规范强制要求 `FSGSBASE`（`RDFSBASE/WRFSBASE/RDGSBASE/WRGSBASE` 指令集），原因：
- Switcher 用 `SWAPGS` + `WRGSBASE` 在 smod↔umod 切换时管理 GS base
- 不用 WRMSR（需要 ring 0），而是用 WRGSBASE（ring 3 可访问，有 FSGSBASE 时）

FSGSBASE 在 Intel Ivy Bridge（2012）及更新 CPU 上可用，但需要内核启用（`CR4.FSGSBASE=1`）。Linux 5.9+ 默认启用。

---

### 2.3 必须有 RDTSCP

```c
if (!boot_cpu_has(X86_FEATURE_RDTSCP)) {
    pr_warn("RDTSCP is required to support for getcpu in guest vdso.\n");
    return -EOPNOTSUPP;
}
```

Guest 的 vDSO（`getcpu()`）使用 `RDTSCP` 读取 TSC_AUX 来获取 CPU ID。PVM 将 `TSC_AUX` 的 guest 值保存在 MSR 影子中，通过 RDTSCP 实现无 hypercall 的快速 CPU ID 读取。

RDTSCP 在绝大多数现代 CPU 上可用，影响面较小。

---

### 2.4 必须有 CMPXCHG16B

```c
if (!boot_cpu_has(X86_FEATURE_CX16)) {
    pr_warn("CMPXCHG16B is required for guest.\n");
    return -EOPNOTSUPP;
}
```

Guest 内核使用 CMPXCHG16B（`LOCK CMPXCHG16B`，128-bit 原子操作）。这是 x86-64 ABI 的事实标准，所有 64 位 CPU 均支持（AMD64 规范要求）。几乎不会成为实际障碍。

---

### 2.5 CPUID 故障支持（可配置）

```c
if (!boot_cpu_has(X86_FEATURE_CPUID_FAULT) && enable_cpuid_intercept) {
    pr_warn("Host doesn't support cpuid faulting.\n");
    return -EOPNOTSUPP;
}
```

PVM 使用 CPUID Fault（Intel 的 `MSR_MISC_FEATURES_ENABLES.CPUID_FAULT`）拦截用户态 CPUID。若宿主不支持此特性，则 `enable_cpuid_intercept` 必须关闭。这不是严格硬性限制，但禁用 CPUID 拦截会影响部分虚拟化正确性（guest 可能看到宿主真实 CPUID）。

---

## 3. 内核配置不兼容

这些是编译期或初始化期的配置冲突，在 `guest_address_space_init()` 中检查（`arch/x86/kvm/pvm/host_mmu.c`）：

### 3.1 CONFIG_KASAN_VMALLOC 不兼容

```c
if (IS_ENABLED(CONFIG_KASAN_VMALLOC)) {
    pr_warn("CONFIG_KASAN_VMALLOC is not compatible with PVM");
    return -1;
}
```

**原因**：PVM 需要在 vmalloc 区域分配连续地址空间作为 guest 地址范围：

```c
pvm_va_range = get_vm_area_align(DEFAULT_RANGE_L4_SIZE, PT_L4_SIZE,
                                  VM_ALLOC|VM_NO_GUARD);
```

`CONFIG_KASAN_VMALLOC` 会在 vmalloc 分配时自动插入 KASAN shadow 内存，改变了 vmalloc 区域的布局，导致 PVM 无法分配所需的连续对齐区域。

**影响**：开启 KASAN_VMALLOC 的内核（通常是调试/测试内核）无法使用 PVM。生产内核通常不开启 KASAN，影响有限，但阻碍了在 KASAN 内核上进行 PVM 的内核调试。

---

### 3.2 CONFIG_KASAN + 5 级分页不兼容

```c
if (pgtable_l5_enabled()) {
    if (IS_ENABLED(CONFIG_KASAN)) {
        pr_warn("CONFIG_KASAN is not compatible with PVM on 5-level paging mode");
        return -1;
    }
```

在 4 级分页（LA48）模式下，KASAN 与 PVM 共存没有问题（只有 KASAN_VMALLOC 有冲突）。但在 5 级分页（LA57）模式下，KASAN 需要的地址空间布局与 PVM 的 `PML5_INDEX` 范围保留冲突，故不兼容。

---

## 4. 功能存根（已有代码骨架但未实现）

### 4.1 嵌套虚拟化（Nested Virtualization）

```c
// arch/x86/kvm/pvm/pvm.c
struct kvm_x86_nested_ops pvm_nested_ops = {};
```

`pvm_nested_ops` 是一个**空结构体**，没有任何回调函数实现。

`pvm_check_intercept()` 的实现也只有一个警告：

```c
static int pvm_check_intercept(struct kvm_vcpu *vcpu,
                               struct x86_instruction_info *info,
                               enum x86_intercept_stage stage,
                               struct x86_exception *exception)
{
    /*
     * HF_GUEST_MASK is not used even nested pvm is supported. L0 pvm
     * might even be unaware the L1 pvm.
     */
    WARN_ON_ONCE(1);
    return X86EMUL_CONTINUE;
}
```

**架构原因**：PVM 嵌套虚拟化在理论上是可行的：L1 Guest 运行 PVM hypervisor，L1 PVM hypervisor 管理 L2 Guest（也是 PVM Guest）。L0 宿主的 Switcher 负责 L1↔L2 切换时的 CR3 管理。

但这需要：
1. Switcher 支持三层 CR3（L1 smod_cr3、L2 smod_cr3、L2 umod_cr3）
2. 正确处理 L1 hypercall 透传给 L0
3. PVCS 层次化管理

现有代码完全没有实现这一切，`pvm_nested_ops` 为空是明确的"不支持"信号。

---

### 4.2 PMU（性能监控单元）

PMU 支持用一整套 dummy 实现代替：

```c
//====== start of dummy pmu ===========
//TODO: split kvm-pmu-intel.ko & kvm-pmu-amd.ko from kvm-intel.ko & kvm-amd.ko.
static bool dummy_pmu_hw_event_available(struct kvm_pmc *pmc)
{
    return true;
}
static struct kvm_pmc *dummy_pmc_idx_to_pmc(struct kvm_pmu *pmu, int pmc_idx)
{
    return NULL;
}
static struct kvm_pmc *dummy_pmu_rdpmc_ecx_to_pmc(struct kvm_vcpu *vcpu,
    unsigned int idx, u32 *mask)
{
    return NULL;
}
// ... 所有函数均返回 NULL/0/false/无操作 ...
struct kvm_pmu_ops dummy_pmu_ops = {
    .hw_event_available = dummy_pmu_hw_event_available,
    .pmc_idx_to_pmc     = dummy_pmc_idx_to_pmc,
    // ...
};
//========== end of dummy pmu =============
```

**原因**：PMU 在 KVM 中通过 `kvm-intel.ko` 或 `kvm-amd.ko` 中的 `kvm_pmu_ops` 实现。PVM 是独立的 `kvm-pvm.ko`，需要自己的 PMU 后端。TODO 注释指出理想方案是将 PMU 实现分离到独立模块（`kvm-pmu-intel.ko` / `kvm-pmu-amd.ko`），但目前还未拆分。

**影响**：Guest 内核中 `perf` 工具无法工作，Intel PT（Processor Trace）、LBR 等硬件性能计数器均不可用。对生产 Kata Containers 工作负载影响较小，但对需要 profiling 的场景是缺失功能。

---

### 4.3 SMM（系统管理模式）

```c
#ifdef CONFIG_KVM_SMM
static int pvm_smi_allowed(struct kvm_vcpu *vcpu, bool for_injection)
{
    return 0;
}

static int pvm_enter_smm(struct kvm_vcpu *vcpu, union kvm_smram *smram)
{
    return 0;
}

static int pvm_leave_smm(struct kvm_vcpu *vcpu, const union kvm_smram *smram)
{
    return 0;
}
```

三个函数均只返回 0，没有任何实际 SMM 处理逻辑。

**原因**：SMM 需要进入 ring -2（或特殊的 SMM 地址空间），这与 PVM 的 CPL3-only 架构根本矛盾。Guest 运行在 CPL3，永远不会有真正的 SMM 机会。即使在生产 QEMU/KVM 中，SMM 的虚拟化也主要用于模拟 UEFI/BIOS，而 Kata Containers 场景的直接启动路径不需要 SMM。

**影响**：PVM guest 无法支持任何依赖 SMM 的功能（如 UEFI 安全启动的某些组件）。

---

## 5. 架构约束（设计层面限制）

这些不是 bug，而是 PVM 架构设计的必然结果。

### 5.1 smod 禁用指令：SYSRET、SYSEXIT、SWAPGS

在 smod（Guest 内核）中，以下指令被**禁止**：

- **SYSRET**：在 CPL3 执行 SYSRET（64-bit）会尝试跳回 CPL3 的"用户态"，但 SYSRET 的语义要求从 CPL0 执行（SYSCALL/SYSRET 配对机制）。在 CPL3 执行会产生 #GP 或行为不确定。PVM 使用 `ERETU`（通过 `MSR_PVM_RETU_RIP` 处的 SYSCALL）代替。
- **SYSEXIT**：同理，SYSEXIT 需要 CPL0。Guest 内核使用 hypercall 代替。
- **SWAPGS**：SWAPGS 在 CPL3 执行是一个特权操作，Switcher 在真正切换时调用硬件 SWAPGS。Guest 内核使用 `PVM_HC_LOAD_GS` hypercall 管理 GS base，而不是直接执行 SWAPGS。

当 smod 执行上述指令时，硬件产生 `#UD`（Undefined Opcode），被 hypervisor 的 `handle_ud()` 捕获并处理（通常是模拟或注入 #UD 给 guest）。

**影响**：Guest Linux 内核必须被 patch 成 PVM guest（使用 `pv_ops`/`paravirt_ops` 替换这些指令），纯标准 Linux 内核无法直接作为 PVM guest 运行。

---

### 5.2 Double Fault 始终提升为 Triple Fault

```c
case DF_VECTOR:
    // DF is handled when exiting and can't reach here.
    pr_warn_once("host bug, can't reach here");
    break;
```

以及多处：

```c
kvm_make_request(KVM_REQ_TRIPLE_FAULT, vcpu);
```

**原因**：传统 x86 系统中，Double Fault（#DF，vector 8）发生时 CPU 切换到 DF 任务/IST 栈，执行 DF 处理程序，处理程序可以记录状态后 IRET 回来（若情况不严重）。

在 PVM 中，由于没有真正的 ring 0，没有 IDT，没有 TSS 的 IST（这些都在宿主内核控制下），Guest 内核无法独立处理 Double Fault。当检测到 DF 时，超管直接触发 Triple Fault（`KVM_REQ_TRIPLE_FAULT`），导致 vCPU 被终止/重置。

实际上，从 Guest 内核的角度，Double Fault 等于 VM 崩溃，无法从中恢复。这与 KVM/VMX 的行为不同——VMX 下 guest 可以有 DF handler。

**影响**：Guest 内核的 DF 处理代码（如 `do_double_fault()`）实际上永远不会被 PVM 调用，内核崩溃时无法获得 DF 的诊断信息。

---

### 5.3 32-bit 兼容模式在 smod 中禁用

规范明确：smod 下**不支持 32-bit 兼容段**（即 CS 不能是 `__USER32_CS`）。原因：

1. `SYSCALL` 指令在 32-bit 兼容模式（CS = `__USER32_CS`）下，处理路径与 64-bit 不同（使用 IA32 SYSCALL，`MSR_STAR` 的 CS bits 不同）
2. Switcher 中用 `sysretq`（64-bit）返回 smod，若 CS 是 32-bit，`sysretq` 会产生 #GP

**影响**：Guest 内核代码必须是纯 64-bit。Guest 用户态（umod）可以运行 32-bit 程序（通过 `int 0x80` 或 32-bit SYSCALL），因为 umod 不受 smod 的这个限制。

---

### 5.4 LDT（Local Descriptor Table）不支持

LDT 是 x86 遗留特性，需要 `LLDT` 指令（ring 0 特权）加载。Guest 运行在 CPL3，无法执行 `LLDT`，超管不提供对应 hypercall。

实际上几乎所有现代 Linux 应用都不用 LDT（Windows Wine 等老式兼容层可能用到），影响面极小。

---

### 5.5 XSAVES 不暴露给 Guest

```c
// pvm_set_cpu_caps() 中：
kvm_cpu_cap_clear(X86_FEATURE_XSAVES);  // 不暴露 XSAVES
```

`XSAVES`（Saves Processor Extended States Supervisor）需要 ring 0 访问 XSTATE 的 supervisor 组件（如 CET shadow stack state）。Guest 在 CPL3 无法使用 XSAVES，只能用 `XSAVE`/`XSAVEOPT`（用户态 XSTATE）。

**影响**：Guest 无法使用 Intel Control Flow Enforcement Technology（CET）的 shadow stack（kernel shadow stack），因为 CET shadow stack 依赖 XSAVES。

---

### 5.6 LA57（5 级分页）支持不完整

```c
/*
 * Unlike VMX/SVM which can switches paging mode atomically, PVM
 * implements guest LA57 through host LA57 shadow paging.
 *
 * If the allocation of the reserved range fails, disable support for
 * 5-level paging support.
 */
if (!pgtable_l5_enabled() || pml5_index_start == 0x1ff)
    kvm_cpu_cap_clear(X86_FEATURE_LA57);
```

PVM 的 5 级分页实现依赖宿主本身运行在 LA57 模式下（`pgtable_l5_enabled()`）。若宿主运行 4 级分页，Guest 就不能使用 5 级分页。与此对比，VMX/SVM 可以在 4 级分页宿主上运行 5 级分页 Guest（通过 EPT/NPT 的 5 级扩展）。

此外，前述 KASAN + LA57 不兼容问题也进一步限制了 LA57 的可用性。

**影响**：目前主流 Linux 服务器内核都运行 4 级分页（LA48），LA57 尚未普及，短期影响有限。

---

### 5.7 LAM（Linear Address Masking）不支持

```c
/* PVM hypervisor hasn't implemented LAM so far */
kvm_cpu_cap_clear(X86_FEATURE_LAM);
```

LAM 是 Intel 最新的 CPU 特性，允许在线性地址中嵌入元数据（类似 ARM 的 TBI）。PVM 尚未实现对 Guest 的 LAM 虚拟化。

---

### 5.8 BUS_LOCK_DETECT 不支持

```c
/* Don't expose MSR_IA32_DEBUGCTLMSR related features. */
kvm_cpu_cap_clear(X86_FEATURE_BUS_LOCK_DETECT);
```

Bus Lock Detection（检测跨缓存行的总线锁定操作）需要 `MSR_IA32_DEBUGCTLMSR` 中的控制位。PVM 不暴露 DEBUGCTLMSR 相关特性。

---

### 5.9 LBR（Last Branch Records）不支持

`MSR_IA32_DEBUGCTLMSR` 相关特性（包括 LBR）均不暴露。这是 PMU stub 问题的延伸——没有完整的 PMU 支持，LBR 也无从实现。

---

### 5.10 SENTER/SEXIT（SGX）限制

SGX（Software Guard Extensions）的 SENTER/SEXIT 需要 ring 0，Guest 在 CPL3 无法直接执行。PVM 未专门为此提供 hypercall，故 Guest 中 SGX enclave 的 supervisor 相关操作不可用。

---

## 6. 推测执行侧信道（Speculation Side-Channel）问题

这是 KVM 维护者（Sean Christopherson、Paolo Bonzini）最集中的技术反馈，也是阻碍主线合并的**最主要障碍**之一。

### 6.1 核心矛盾：CPL3 无法与内核隔离

在传统 KVM（VMX/SVM）中：
- Guest 与 Host 的隔离由硬件 VM Entry/Exit 保证
- 推测执行（Spectre/Meltdown）缓解由 CPU 微码和软件共同处理（IBRS、IBPB、STIBP、RSB 填充等）
- KVM 在 VM-Exit 后立即启用 IBRS，在 VM-Entry 前关闭 IBRS

在 PVM 中：
- Guest 运行在 CPL3，与宿主内核共享同一硬件特权级
- **没有 VM-Entry/VM-Exit 的硬件隔离屏障**
- smod→宿主内核的"切换"是通过一个 SYSCALL 指令完成的（Switcher 拦截）
- 这意味着：推测执行时，Guest CPL3 代码可以沿着宿主内核的预测路径投机执行

### 6.2 现有缓解措施

**RSB 填充**（`FILL_RETURN_BUFFER`）：

```asm
// entry_64_switcher.S - MITIGATION_ENTER macro
FILL_RETURN_BUFFER %rcx, RSB_CLEAR_LOOPS, X86_FEATURE_RSB_VMEXIT, \
                   X86_FEATURE_RSB_VMEXIT_LITE
IBRS_ENTER
```

当 Guest→Host 切换时（`switcher_return_from_guest`），Switcher 执行：
1. `FILL_RETURN_BUFFER`：用有效的返回地址填充 RSB，防止 Guest 注入毒化的 RSB 条目后被宿主使用
2. `IBRS_ENTER`：启用 IBRS（Indirect Branch Restricted Speculation），防止 Guest 代码影响宿主的间接分支预测

当 Host→Guest 切换时（`switcher_enter_guest`，`MITIGATION_EXIT`）：

```asm
MITIGATION_EXIT  // = IBRS_EXIT
```

关闭 IBRS。

### 6.3 缺失的缓解措施

**问题 1：IBPB（Indirect Branch Predictor Barrier）缺失**

IBPB 刷新间接分支预测器历史，防止 Guest 利用宿主遗留的分支预测历史进行攻击（L1TF/Spectre v2 类攻击）。

在 VMX/SVM 中，VM-Exit 后 KVM 会在适当时机执行 IBPB（`kvm_x86_call(prepare_guest_switch)` → 处理 `SPEC_CTRL_EXIT_TO_GUEST`）。PVM 的 Switcher 没有等价的 IBPB 调用。

源码中明确：

```c
/* Don't expose MSR_IA32_SPEC_CTRL to guest */
kvm_cpu_cap_clear(X86_FEATURE_SPEC_CTRL);
kvm_cpu_cap_clear(X86_FEATURE_AMD_STIBP);
kvm_cpu_cap_clear(X86_FEATURE_AMD_IBRS);
kvm_cpu_cap_clear(X86_FEATURE_AMD_SSBD);
```

Guest 看不到 `MSR_IA32_SPEC_CTRL`，因此 Guest 内核无法独立管理推测控制位。这些 MSR 完全由宿主超管控制（宿主直接写 SPEC_CTRL MSR）。

问题在于：宿主超管何时/是否刷新分支预测器，取决于宿主内核的 IBPB 策略，而不是每次 smod↔宿主切换时执行。在高频切换（每次 syscall 都是一次 Switcher 拦截尝试）的场景下，若不每次都刷新 IBPB，理论上存在分支预测器污染风险。

**问题 2：Meltdown 缓解（KPTI）未实现**

正如第 2.1 节所述，宿主 KPTI 导致 PVM 无法加载。这意味着 PVM 只能在禁用 KPTI 的宿主上运行，而禁用 KPTI 意味着宿主内核地址空间在用户态（CPL3）TLB 中可见。

对于 PVM 而言，Guest 代码就是"宿主用户态"（同为 CPL3），理论上在推测执行窗口内可以访问宿主内核地址的映射（通过 Meltdown 类技术）。

**问题 3：L1TF（Foreshadow）缓解不完整**

L1TF 利用 EPT 违规的推测窗口读取物理内存。在 PVM 中没有 EPT，但 shadow paging 的 `SPTE_MMU_PRESENT_MASK` 处理也需要相应的缓解措施。PVM 代码中没有明确的 L1TF 缓解路径。

**问题 4：Direct SYSCALL 路径的 Spectre v2 风险**

当 Guest umod 执行 SYSCALL 时，Switcher 的 `entry_SYSCALL_64_switcher` 接管。宿主内核的 SYSCALL 入口点（`entry_SYSCALL_64`）通过 `ALTERNATIVE_2` 宏处理 Spectre 缓解（`SPEC_CTRL_ENTRY_FROM_USER`）。Switcher 替换了这个入口路径，需要确保其 Spectre 缓解同样完整。

### 6.4 社区讨论摘要

Sean Christopherson（Intel，KVM 核心维护者）在 LKML 评审中指出：

> "PVM 在 VM-entry/exit 时的推测执行缓解远不如 KVM VMX。具体来说，没有正确的 IBPB 处理，guest→host 切换时的分支预测器状态没有被正确隔离。"

Paolo Bonzini（Red Hat，KVM 维护者）补充：

> "PVM 的推测缓解与 KVM 的安全模型不一致。在 KVM 中，guest 和 host 被 VM 边界严格隔离，任何 side-channel 缓解都在这个边界上实施。PVM 绕过了这个边界，必须提供等价的保护。"

PVM 开发者（Lai Jiangshan）承认这些问题：

> "推测缓解确实是待完成项，我们在生产环境通过宿主层面的缓解（CPU 微码、硬件隔离）来管理这些风险。内核 patch 系列中的推测缓解是下一步的工作。"

---

## 7. 源码中的 TODO 项汇总

以下是从 `pvm.c`、`host_mmu.c`、`entry_64_switcher.S` 等文件中收集的所有 `TODO` 注释，反映了开发者已知但未完成的工作：

### 7.1 TSC 处理（pvm.c line ~817-824）

```c
// TODO: add proper ABI and make guest use host TSC
```

出现两次，涉及 TSC（Time Stamp Counter）的 Guest/Host 同步。目前 PVM 的 TSC 处理通过 MSR 影子实现，但缺少：
- TSC scaling/offsetting（TSC 频率虚拟化）
- 正式的 ABI（Guest 如何稳定地读取 Host TSC 基准）

在嵌套虚拟化场景（L1 VM 内跑 PVM），TSC 同步更复杂。

---

### 7.2 PMU 模块分离（pvm.c line ~2935）

```c
//TODO: split kvm-pmu-intel.ko & kvm-pmu-amd.ko from kvm-intel.ko & kvm-amd.ko.
```

PMU 实现目前耦合在 `kvm-intel.ko`/`kvm-amd.ko` 中，PVM 独立的 `kvm-pvm.ko` 无法直接复用。计划将 PMU 拆分为独立模块，但工作量较大（涉及 KVM 架构重构）。

---

### 7.3 TDX 宿主支持（pvm.c line ~2523）

```c
// TODO: pvm host is TDX guest.
// tdx_get_ve_info(&pvm->host_ve);
```

场景：PVM 宿主本身运行在 Intel TDX（Trusted Domain Extensions）虚拟机中。当发生 `#VE`（Virtualization Exception，TDX 特有）时，PVM 需要调用 `tdx_get_ve_info()` 处理。目前这段代码被注释掉，`#VE` 被忽略。

**影响**：PVM 无法在 TDX 保护虚机中运行（PVM-in-TDX）。

---

### 7.4 SEV 宿主支持（pvm.c line ~2528）

```c
// TODO: pvm host is SEV guest.
```

类似 TDX，当 PVM 宿主运行在 AMD SEV（Secure Encrypted Virtualization）环境中，`#VC`（VMM Communication Exception，SEV-SNP 特有）的处理需要特殊逻辑，目前未实现。

---

### 7.5 Machine Check 处理分离（pvm.c line ~2208）

```c
// TODO: split kvm_machine_check() to avoid irq-enabled or
// schedule code (thread dead) in pvm_handle_exit_irqoff().
```

`kvm_machine_check()` 中包含可能开启中断或调度的代码，但 PVM 的 `pvm_handle_exit_irqoff()` 需要在关中断上下文（`irqoff`）中处理 MCE。目前通过 `irqoff` 约束绕过，但从正确性角度需要将 `kvm_machine_check()` 中的这部分代码分离出来。

---

### 7.6 #VC（SEV-SNP 异常）第二阶段处理（pvm.c line ~2204）

```c
// TODO: handle the second part for #VC.
goto unknown_exit_reason;
```

`#VC` 异常处理需要两阶段：第一阶段在关中断的 `irqoff` 上下文，第二阶段在普通上下文。目前只有第一阶段的框架（被注释掉），第二阶段完全未实现，直接跳转到 `unknown_exit_reason`。

---

## 8. 功能限制汇总表

| 限制 | 严重程度 | 状态 |
|------|---------|------|
| 宿主 KPTI 不支持 | **关键** | TODO，计划实现 |
| 推测侧信道缓解不完整 | **关键** | 主要上游阻塞点 |
| 嵌套虚拟化 | 中等 | Stub，未实现 |
| PMU | 低（容器场景）| Dummy，TODO |
| SMM | 低 | Stub，返回 0 |
| KPTI Guest | 中等 | 未规划 |
| LA57 限制 | 低（目前罕见）| 部分支持 |
| KASAN_VMALLOC | 低（调试场景）| 不兼容 |
| SYSRET/SYSEXIT/SWAPGS | 设计约束 | 永久限制 |
| Double Fault → Triple Fault | 设计约束 | 永久限制 |
| TSC ABI | 低 | TODO |
| TDX 宿主 | 低 | TODO |
| SEV 宿主 | 低 | TODO |
| LDT | 低（遗留） | 永久限制 |
| XSAVES | 低 | 永久限制 |
| LAM | 低（新特性）| TODO |

---

## 9. 社区状态与上游进展

### 9.1 RFC 提交历史

| 时间 | 事件 |
|------|------|
| 2023 年 10 月 | SOSP 2023 学术论文发表（[链接](https://ranger.uta.edu/~jrao/papers/sosp23.pdf)）|
| 2024 年 2 月 26 日 | LKML RFC 00/73 提交（Lai Jiangshan，蚂蚁集团）|
| 2024 年 3~4 月 | 多轮 LKML 讨论，KVM 维护者提出 speculation 问题 |
| 2024 年 11 月 | Linux Plumbers Conference 2024，PVM 专题演讲（[幻灯片](https://lpc.events/event/18/contributions/1766/attachments/1498/3306/LPC-PVM.pdf)）|
| 2025 年 | 开发者持续迭代，解决部分 API 问题 |
| 2026 年 6 月 | RFC 状态，尚未合并主线 |

### 9.2 KVM 维护者关注点

**主要技术问题（Sean Christopherson）**：
1. speculation side-channel 缓解不完整（最核心问题）
2. Switcher 与宿主 `entry_64.S` 的耦合太深，维护成本高
3. `tss_extra` 结构嵌入 TSS 的设计与未来 TSS 演进可能冲突
4. shadow paging 在宿主压力下的行为（内存压力、kswapd 驱逐等）

**主要技术问题（Paolo Bonzini）**：
1. PVM 安全模型与 KVM 现有模型的兼容性
2. Guest 内核需要专门 patch 这一点提高了使用门槛

**主要 API 问题（kvm-devel 列表）**：
1. `MSR_PVM_LINEAR_ADDRESS_RANGE` 的编码方式过于复杂
2. PVM 合成 CPUID（`invlpg + cpuid`）使用了奇怪的机制，与 KVM 标准 CPUID 虚拟化不一致
3. `struct pvm_vcpu_struct`（PVCS）的 ABI 稳定性：一旦进入主线，需要承诺向后兼容

### 9.3 生产部署状态

尽管上游 RFC 状态，PVM 已在生产环境大规模使用：

- **阿里云**：用于 Elastic Container Instance（ECI，弹性容器实例），在普通云 VM 中提供 Kata Containers 隔离，每日服务数万个安全容器
- **蚂蚁集团**：支付宝和金融核心系统的安全容器基础设施
- **OpenAnolis**（阿里 Linux 发行版）：已将 PVM patch 合并，作为企业支持特性

### 9.4 与 Xen PV / lguest 的对比意义

PVM 的上游路径比历史上的 Xen PV 更艰难，原因：
1. Xen PV 有独立的 hypervisor（Xen）作为支撑，不需要完全依赖 KVM
2. PVM 作为 KVM vendor 模块需要满足 KVM 更严格的安全标准
3. speculation side-channel 在 2024 年比 Xen PV 进入内核（2006 年）时要复杂得多

另一方面，PVM 相比 Xen PV 有重要优势：
- 无需修改宿主 Bootloader/BIOS
- 与标准 KVM 工具链（libvirt、QEMU）兼容
- 性能数据优异（SOSP 2023 论文报告接近原生性能）

### 9.5 未来路线图

根据开发者在 LPC 2024 的演讲和邮件列表讨论，优先解决的项目：
1. 实现宿主 KPTI 支持（最高优先级，解锁大量机器）
2. 完善 speculation 缓解（满足上游要求）
3. 模块化 PMU 支持
4. 完善 TSC 虚拟化 ABI
5. 更简洁的 PVCS ABI

---

## 10. 与 KVM VMX/SVM 的安全模型对比

理解限制的背景，需要与传统 KVM 对比：

| 安全属性 | KVM VMX/SVM | PVM |
|---------|-------------|-----|
| 硬件隔离边界 | VM-Entry/VM-Exit（硬件原语）| SYSCALL 拦截（软件）|
| Guest→Host 推测隔离 | IBPB + IBRS（VM-Exit 触发）| RSB 填充 + IBRS（Switcher 触发）|
| KPTI 支持 | 宿主完整支持 | 当前不支持 |
| Meltdown 缓解 | KPTI 保护宿主地址 | 宿主必须禁用 KPTI |
| L1TF | EPT 级别缓解 | Shadow paging 缓解（不完整）|
| Spectre v2 | IBPB + retpoline + eIBRS | 部分：IBRS_ENTER，无 IBPB |
| MDS（微架构缓冲区）| MD_CLEAR（VERW）| 未明确处理 |
| STIBP（超线程隔离）| 可配置 | Guest 不可见，宿主级控制 |

**结论**：PVM 的推测缓解相对于 VMX/SVM 确实存在差距，这是当前最重要的技术债务。开发团队在阿里云通过宿主层面的硬件/微码缓解弥补了这个差距，但对于通用 Linux 内核来说，需要在内核层面提供完整的保护。

---

## 11. 评估：主线合并的障碍

综合以上分析，PVM 进入主线的主要障碍：

**必须解决（硬阻塞）**：
1. **KPTI 支持**：没有这个，PVM 无法在大多数生产服务器上运行
2. **完整的 speculation 缓解**：KVM 维护者明确要求

**应该解决（软阻塞）**：
3. **PMU 支持**：对于生产工作负载中的 perf profiling 有需求
4. **更简洁的 PVCS ABI**：ABI 一旦进主线就要维护，需要仔细设计

**可以暂后**：
5. 嵌套虚拟化
6. TDX/SEV 宿主支持
7. TSC ABI 精细化
