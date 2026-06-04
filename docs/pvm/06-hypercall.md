# PVM Hypercall 接口完整分析

## 1. Hypercall 机制概述

PVM Guest 在 supervisor mode（smod）下通过 SYSCALL 指令触发 hypercall。调用约定：

```
输入：
  RAX = hypercall 号（PVM_HC_* 常量）
  RBX = 参数 0（a0）
  R10 = 参数 1（a1，注意：不是 RCX，因为 SYSCALL 破坏 RCX）
  RDX = 参数 2（a2）
  ...

输出：
  RAX = 返回值（若有）
  RCX、R11 被 SYSCALL/SYSRET 破坏（已由 pvm_hypercall 包装保存）
```

**R10 vs RCX 的约定**：SYSCALL 指令执行时将 RIP 保存到 RCX，破坏 RCX 的原有值。为了传递参数 1，PVM 规定用 R10 代替 RCX（Linux SYSCALL 也有同样约定）。超管处理时：

```c
// 超管处理 SYSCALL 类 hypercall 时，将 R10 映射回 RCX
static int handle_kvm_hypercall(struct kvm_vcpu *vcpu)
{
    int r;
    kvm_rcx_write(vcpu, kvm_r10_read(vcpu));   // r10 → rcx
    r = kvm_emulate_hypercall_noskip(vcpu);
    kvm_r10_write(vcpu, kvm_rcx_read(vcpu));   // rcx → r10（恢复）
    return r;
}
```

## 2. Hypercall 号定义

```c
// arch/x86/include/uapi/asm/pvm_para.h

#define PVM_HC_SPECIAL_BASE    0x17088200   // 专用范围起始
#define PVM_HC_SPECIAL_MAX_NR  256
#define PVM_HC_SPECIAL_MAX     (PVM_HC_SPECIAL_BASE + PVM_HC_SPECIAL_MAX_NR)

// PVM 专用 Hypercall
#define PVM_HC_LOAD_PGTBL      (PVM_HC_SPECIAL_BASE + 0)   // 0x17088200
#define PVM_HC_IRQ_WIN         (PVM_HC_SPECIAL_BASE + 1)   // 0x17088201
#define PVM_HC_IRQ_HALT        (PVM_HC_SPECIAL_BASE + 2)   // 0x17088202
#define PVM_HC_TLB_FLUSH       (PVM_HC_SPECIAL_BASE + 3)   // 0x17088203
#define PVM_HC_TLB_FLUSH_CURRENT (PVM_HC_SPECIAL_BASE + 4) // 0x17088204
#define PVM_HC_TLB_INVLPG      (PVM_HC_SPECIAL_BASE + 5)   // 0x17088205
#define PVM_HC_LOAD_GS         (PVM_HC_SPECIAL_BASE + 6)   // 0x17088206
#define PVM_HC_RDMSR           (PVM_HC_SPECIAL_BASE + 7)   // 0x17088207
#define PVM_HC_WRMSR           (PVM_HC_SPECIAL_BASE + 8)   // 0x17088208
#define PVM_HC_LOAD_TLS        (PVM_HC_SPECIAL_BASE + 9)   // 0x17088209
```

对于超出 `PVM_HC_SPECIAL_BASE ~ PVM_HC_SPECIAL_MAX` 范围的 hypercall，超管将其转发给 KVM 的标准 hypercall 处理（通过 `handle_kvm_hypercall`），支持 KVM 半虚拟化接口（如 KVM_HC_CLOCK_PAIRING、KVM_HC_SEND_IPI 等）。

## 3. 各 Hypercall 详解

### 3.1 PVM_HC_LOAD_PGTBL — 加载页表

```c
// 超管实现（arch/x86/kvm/pvm/pvm.c）
static int handle_hc_load_pagetables(struct kvm_vcpu *vcpu,
                                     unsigned long flags,  // RBX（a0）
                                     unsigned long pgd)    // R10（a1）
{
    unsigned long cr4 = vcpu->arch.cr4;

    // 处理 LA57（5 级分页）模式切换请求
    if (!(flags & PVM_LOAD_PGTBL_FLAGS_LA57))
        cr4 &= ~X86_CR4_LA57;
    else if (guest_cpuid_has(vcpu, X86_FEATURE_LA57))
        cr4 |= X86_CR4_LA57;

    if (cr4 != vcpu->arch.cr4) {
        vcpu->arch.cr4 = cr4;
        kvm_mmu_reset_context(vcpu);       // 重建整个 shadow MMU
        kvm_make_request(KVM_REQ_TLB_FLUSH_GUEST, vcpu);
    }

    // 加载 CR3
    if (flags & PVM_LOAD_PGTBL_FLAGS_TLB)
        kvm_set_cr3(vcpu, pgd);            // 同时刷新 TLB
    else
        kvm_set_cr3(vcpu, pgd | CR3_NOFLUSH);  // 不刷新（保留 PCID TLB）

    return 1;
}
```

**flags 定义**：
```c
#define PVM_LOAD_PGTBL_FLAGS_TLB   _BITUL(0)  // 刷新与当前 PCID 关联的 TLB
#define PVM_LOAD_PGTBL_FLAGS_LA57  _BITUL(1)  // 切换到 5 级分页
```

**pgd 参数**：Guest 物理地址，指向 Guest 顶层页目录（PGD）。超管通过 KVM MMU 建立 shadow paging 映射。

**Guest 调用时机**：
- 进程切换（`switch_mm`）：切换到新进程的 CR3
- `mmap`/`munmap` 等内存管理操作（通过 TLB flush hypercall 而非此 hypercall）
- 引导初期：第一次建立分页

**返回值**：无（返回时 RAX 未修改，但通过 `return 1` 告诉 KVM 继续运行 Guest）。

### 3.2 PVM_HC_IRQ_WIN — 中断窗口

```c
static int handle_hc_irq_window(struct kvm_vcpu *vcpu)
{
    // 处理待处理中断：kvm_cpu_has_interrupt() 检查是否有挂起的 vIRQ
    // 若有，通过 pvm_inject_event 交付
    // 清除 PVCS::event_flags.IP 位（中断待处理已处理）
    return 1;
}
```

**调用场景**：Guest 执行 `pvm_irq_enable` 时，若 `PVCS::event_flags.IP` 位置位（有待处理中断），Guest 立即调用此 hypercall：

```asm
// entry_64_pvm.S
pvm_irq_enable:
    orq    $X86_EFLAGS_IF, PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_flags)
    btq    $PVM_EVENT_FLAGS_IP_BIT, PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_flags)
    jc     .L_maybe_interrupt_pending  // IP=1，有待处理中断
    RET
.L_maybe_interrupt_pending:
    movq   $PVM_HC_IRQ_WIN, %rax
    jmp    pvm_hypercall
```

**与 `SWITCH_FLAGS_IRQ_WIN` 的关系**：当 `switch_flags & SWITCH_FLAGS_IRQ_WIN` 时，Switcher 不允许从 smod 直接返回 umod（因为超管请求了中断窗口）。IRQ_WIN hypercall 清除此标志。

### 3.3 PVM_HC_IRQ_HALT — 仿真 STI+HLT

```c
static int handle_hc_irq_halt(struct kvm_vcpu *vcpu)
{
    // 设置 RFLAGS.IF = 1（等效于 STI）
    kvm_set_rflags(vcpu, kvm_get_rflags(vcpu) | X86_EFLAGS_IF);
    // 仿真 HLT（挂起 vCPU，直到有中断到来）
    return kvm_emulate_halt_noskip(vcpu);
}
```

**调用场景**：Guest 内核的 idle 循环（`cpu_idle` → `hlt` → PVM 替换为此 hypercall）。`kvm_emulate_halt_noskip` 让 KVM 将 vCPU 置为"等待中断"状态，释放宿主 CPU 直到有中断到达。

### 3.4 PVM_HC_TLB_FLUSH — 全局 TLB 刷新

```c
static int handle_hc_flush_tlb_all(struct kvm_vcpu *vcpu)
{
    kvm_make_request(KVM_REQ_TLB_FLUSH_GUEST, vcpu);
    return 1;
}
```

`KVM_REQ_TLB_FLUSH_GUEST` 是 KVM 的 per-vCPU 请求位，在下次 VM-enter 前会触发完整的 TLB 刷新（通过写 CR3 不带 `CR3_NOFLUSH`，或在 VM-enter 时清除所有 PCID 的 TLB）。

**调用场景**：
- `flush_tlb_mm`（整个地址空间失效，如 `execve`）
- `flush_tlb_all`（SMP 系统中某些内存屏障场景）

### 3.5 PVM_HC_TLB_FLUSH_CURRENT — 刷新当前 PCID

```c
static int handle_hc_flush_tlb_current(struct kvm_vcpu *vcpu)
{
    // 用当前 CR3 重写 CR3（不带 CR3_NOFLUSH），触发当前 PCID 的 TLB 刷新
    kvm_set_cr3(vcpu, vcpu->arch.cr3);
    return 1;
}
```

比 TLB_FLUSH 精确：只刷新当前 PCID 的 TLB，不影响其他进程的 TLB 缓存。

**调用场景**：`flush_tlb_range`（刷新某个 VA 范围内的 TLB）在不使用 INVLPG 的 bulk 场景下使用。

### 3.6 PVM_HC_TLB_INVLPG — 单页 TLB 失效

```c
static int handle_hc_invlpg(struct kvm_vcpu *vcpu, unsigned long addr)
{
    kvm_mmu_invlpg(vcpu, addr);
    return 1;
}
```

`kvm_mmu_invlpg` 触发 KVM shadow MMU 对单个 GVA 的失效，下次访问此地址时重新 page walk 建立映射。

**调用场景**：`flush_tlb_page`（单页失效，最精确的 TLB 管理）。在内存解映射（`munmap`、写保护）时按页调用。

### 3.7 PVM_HC_LOAD_GS — 加载 GS 段

```c
static int handle_hc_load_gs(struct kvm_vcpu *vcpu, unsigned short sel)
{
    struct vcpu_pvm *pvm = to_pvm(vcpu);
    unsigned long guest_kernel_gs_base;

    // 安全检查：RPL 必须为 3（用户态段）
    if (sel != 0 && (sel & 3) != 3)
        sel = 0;  // 无效选择子 → 使用 NULL

    preempt_disable();  // 防止迁移

    // 切换到 Guest 硬件状态
    if (!pvm->loaded_cpu_state)
        pvm_prepare_switch_to_guest(vcpu);
    else
        __save_gs_base(pvm);  // 保存当前 GS base

    // 加载 sel 到硬件 %gs（同时更新 MSR_KERNEL_GS_BASE）
    load_gs_index(sel);

    // 读取 load_gs_index 设置的 GS base
    rdmsrl(MSR_KERNEL_GS_BASE, guest_kernel_gs_base);

    // 恢复 Guest GS base（load_gs_index 修改了它）
    __load_gs_base(pvm);

    preempt_enable();

    // 返回：RAX = 新 GS base
    kvm_rax_write(vcpu, guest_kernel_gs_base);
    return 1;
}
```

**为什么需要此 hypercall**：Linux 线程切换时需要加载新的 GS 段（TLS 切换），而 LGDT/段加载需要在 ring 0 进行 GDT 访问验证（TLS 描述符在 GDT 中）。PVM Guest 无法直接执行 `mov %sel, %gs`（CPL3 下可以，但 TLS 描述符验证需要 ring 0 语义）。

**实现细节**：`load_gs_index` 是宿主内核的函数，在 CPL0 下执行真正的 `mov %sel, %gs`。执行后，`MSR_KERNEL_GS_BASE`（在 CPL3 swapgs 之前的值）包含了新 GS base——这个值通过 RAX 返回给 Guest，Guest 再将其存入 `MSR_KERNEL_GS_BASE`（通过 WRMSR hypercall）。

### 3.8 PVM_HC_RDMSR — 读取 MSR

```c
static int handle_hc_rdmsr(struct kvm_vcpu *vcpu, u32 index)
{
    u64 value = 0;
    kvm_get_msr(vcpu, index, &value);
    kvm_rax_write(vcpu, value);
    return 1;
}
```

**调用约定**：
- RBX（a0）= MSR index
- 返回 RAX = MSR value（64-bit）

`kvm_get_msr` 通过 KVM 的 MSR 仿真层，对标准 MSR 返回 Guest 视图的值（可能是影子值，而非硬件实际值）。

### 3.9 PVM_HC_WRMSR — 写入 MSR

```c
static int handle_hc_wrmsr(struct kvm_vcpu *vcpu, u32 index, u64 value)
{
    if (kvm_set_msr(vcpu, index, value))
        kvm_rax_write(vcpu, -EIO);  // 失败（如无效 MSR index）
    else
        kvm_rax_write(vcpu, 0);     // 成功
    return 1;
}
```

**调用约定**：
- RBX（a0）= MSR index
- R10（a1）= value 低 32 位（整个 64 位值由 R10:RDX 组成？或纯 R10？）

实际上根据 x86-64 WRMSR 约定，value = R10（因为 64-bit 的参数在一个 64-bit 寄存器中）。

`kvm_set_msr` 通过 KVM MSR 仿真层设置 Guest MSR。对于 PVM 专用 MSR，会触发 `pvm_set_msr_pvm`；对于标准 MSR，会更新 `vcpu->arch.*` 中的影子值。

### 3.10 PVM_HC_LOAD_TLS — 加载 TLS 描述符

```c
static int handle_hc_load_tls(struct kvm_vcpu *vcpu,
                               unsigned long tls_desc_0,  // RBX（a0）
                               unsigned long tls_desc_1,  // R10（a1）
                               unsigned long tls_desc_2)  // RDX（a2）
{
    struct vcpu_pvm *pvm = to_pvm(vcpu);
    unsigned long *tls_array = (unsigned long *)&pvm->tls_array[0];
    int i;

    // 保存 3 个 TLS 描述符到 vcpu_pvm 缓存
    tls_array[0] = tls_desc_0;
    tls_array[1] = tls_desc_1;
    tls_array[2] = tls_desc_2;

    // 验证并规范化每个描述符
    for (i = 0; i < GDT_ENTRY_TLS_ENTRIES; i++) {
        if (!tls_desc_okay(&pvm->tls_array[i])) {
            pvm->tls_array[i] = (struct desc_struct){0};  // 无效 → 清零
            continue;
        }
        // 规范化（同 fill_ldt() 的逻辑）：
        pvm->tls_array[i].type |= 1;    // 已访问位
        pvm->tls_array[i].s = 1;        // 非系统段
        pvm->tls_array[i].dpl = 0x3;    // DPL=3（用户级）
        pvm->tls_array[i].l = 0;        // 非 64-bit（TLS 是 32-bit 段）
    }

    preempt_disable();
    if (pvm->loaded_cpu_state)
        host_gdt_set_tls(pvm);  // 立即写入宿主 GDT
    preempt_enable();

    return 1;
}
```

**TLS 验证**（`tls_desc_okay`）：
```c
static bool tls_desc_okay(struct desc_struct *desc)
{
    if (!desc->p)       return false;  // 段必须 present
    if (desc->type & 8) return false;  // 必须是 data 段（非 code）
    if (!desc->d)       return false;  // 必须是 32-bit 段（D=1）
    return true;
}
```

**GDT 写入**（`host_gdt_set_tls`）：

```c
static void host_gdt_set_tls(struct vcpu_pvm *pvm)
{
    // 将 3 个 TLS 描述符写入宿主 GDT 的 TLS 槽
    // GDT_ENTRY_TLS_MIN 到 GDT_ENTRY_TLS_MIN+2
    native_set_tls_desc(pvm->tls_array, pvm->tls_array + GDT_ENTRY_TLS_ENTRIES);
}
```

**调用场景**：`arch_set_tls_desc`（线程创建/切换时设置 TLS 描述符）。Linux glibc 的线程库（pthread）在每个线程创建时调用 `arch_prctl(ARCH_SET_GS, tls_ptr)` 或 `set_thread_area()`，最终触发此 hypercall。

## 4. KVM 兼容 Hypercall 透传

PVM hypercall 号超出 PVM_HC_SPECIAL 范围时，超管将其透传给 KVM 标准 hypercall 处理：

```c
// handle_exit_syscall 中的 dispatch
switch (kvm_rax_read(vcpu)) {
    case PVM_HC_LOAD_PGTBL ... PVM_HC_LOAD_TLS:
        // PVM 专用 hypercall
        return handle_specific_hc(vcpu, ...);
    default:
        // 转发给 KVM 标准 hypercall
        return handle_kvm_hypercall(vcpu);
}
```

支持的 KVM 标准 hypercall（通过 `kvm_emulate_hypercall_noskip` 实现）：
- `KVM_HC_VAPIC_POLL_IRQ`：半虚拟化 APIC 轮询
- `KVM_HC_KICK_CPU`：唤醒另一个 vCPU
- `KVM_HC_SEND_IPI`：发送 IPI（半虚拟化 SMP）
- `KVM_HC_SCHED_YIELD`：让出 vCPU
- `KVM_HC_CLOCK_PAIRING`：时钟配对（精确时间同步）

## 5. Hypercall 的 pvm_hypercall 包装

Guest 侧的 hypercall 包装函数（在 `.noinstr.text` 节，不可插桩）：

```asm
// arch/x86/entry/entry_64_pvm.S
SYM_FUNC_START(pvm_hypercall)
    push   %r11    // 保存 R11（SYSCALL 指令会破坏 R11）
    push   %r10    // 保存 R10（可能是参数）
    movq   %rcx, %r10    // 将 RCX 移到 R10（参数 1 位置）
    UNWIND_HINT_SAVE
    syscall         // 触发 Switcher，进入超管
    UNWIND_HINT_RESTORE
    movq   %r10, %rcx    // 恢复 RCX（从 R10 恢复参数 1）
    popq   %r10
    popq   %r11
    RET
SYM_FUNC_END(pvm_hypercall)
```

**UNWIND_HINT_SAVE/RESTORE**：为了使内核 unwinder（DWARF/ORC）能正确追踪 syscall 前后的栈状态。

**`.noinstr.text` 节**：这个节中的代码不会被 ftrace/kprobes 插桩，避免递归：如果 hypercall 被 trace，trace 函数自己可能触发 hypercall，造成无限递归。

## 6. Hypercall 参数传递约定总结

```
RAX = hypercall 号
RBX = a0（参数 0）
R10 = a1（参数 1，因 SYSCALL 破坏 RCX，用 R10 代替）
RDX = a2（参数 2）
RSI = a3（参数 3，若有）
RDI = a4（参数 4，若有）

超管读取参数：
  a0 = kvm_rbx_read(vcpu)
  a1 = kvm_r10_read(vcpu)
  a2 = kvm_rdx_read(vcpu)

返回值：
  RAX = 返回值（通过 kvm_rax_write(vcpu, value) 设置）
```

这与 Linux 的 SYSCALL 约定基本一致（Linux 用 RDI/RSI/RDX/R10/R8/R9，PVM 用 RBX/R10/RDX/RSI/RDI 略有差异），主要区别是第一个参数用 RBX 而非 RDI（RDI 在 Switcher 入口被临时用于访问 PVCS）。

## 7. Hypercall 性能分析

### 7.1 直接切换路径（最快）

对于频繁执行的操作（系统调用、EVENT_RETURN），PVM 使用 Switcher 的直接切换机制，完全绕过超管处理：

```
umod SYSCALL → Switcher (≈ 50-100 cycles) → smod 入口
smod ERETU   → Switcher (≈ 50-100 cycles) → umod
```

无超管介入，无完整 VM-exit/VM-enter。

### 7.2 Hypercall 路径（较慢）

PVM 专用 hypercall 需要完整的 Switcher VM-exit + 超管处理 + VM-enter：

```
smod SYSCALL → Switcher VM-exit (≈ 200-500 cycles)
→ 超管处理 hypercall (≈ 100-2000 cycles，取决于操作)
→ VM-enter (≈ 100-300 cycles)
```

### 7.3 各 Hypercall 开销估计

| Hypercall | 开销估计 | 主要原因 |
|-----------|---------|---------|
| TLB_INVLPG | 低（≈ 500 cycles）| KVM mmu_invlpg 相对简单 |
| IRQ_WIN | 低（≈ 300 cycles）| 仅检查并交付中断 |
| LOAD_GS | 中（≈ 1000 cycles）| preempt_disable + load_gs_index |
| LOAD_PGTBL | 中到高（≈ 500-5000 cycles）| kvm_set_cr3 + 可能的 shadow walk |
| LOAD_TLS | 中（≈ 800 cycles）| GDT 修改 |
| RDMSR/WRMSR | 低到中（≈ 500-2000 cycles）| KVM MSR 仿真 |
| IRQ_HALT | 高（≈ vCPU 挂起时间）| 等待中断 |

频繁路径（`sched_clock`、TLS 访问）已通过 vDSO 或 per-CPU 直接访问优化，避免 hypercall。
