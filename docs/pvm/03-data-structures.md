# PVM 核心数据结构完整解析

## 1. 数据结构总览

PVM 的状态管理分三个层次：
1. **Hypervisor 层**（`struct vcpu_pvm`）：每 vCPU，保存在宿主内核内存
2. **Switcher 层**（`struct tss_extra`）：每 CPU，保存在 TSS（Task State Segment）末尾，热路径直接访问
3. **Guest-Hypervisor 共享层**（`struct pvm_vcpu_struct`，即 PVCS）：一页共享内存，映射在 Guest 物理地址空间中

## 2. struct vcpu_pvm — vCPU 完整状态

```c
// arch/x86/kvm/pvm/pvm.h

struct vcpu_pvm {
    // ═══════════════════════════════════════════════════════
    // KVM 基础状态（必须是第一个字段，以便 container_of 兼容）
    // ═══════════════════════════════════════════════════════
    struct kvm_vcpu vcpu;

    // ═══════════════════════════════════════════════════════
    // Guest 通用寄存器状态
    // ═══════════════════════════════════════════════════════
    unsigned long rflags;   // Guest RFLAGS（虚拟值，IF 映射到 PVCS）

    // ═══════════════════════════════════════════════════════
    // 模式控制标志（与 tss_extra.switch_flags 同步）
    // ═══════════════════════════════════════════════════════
    unsigned long switch_flags;
    // bit 0: SWITCH_FLAGS_SMOD     — 当前在 supervisor mode
    // bit 1: SWITCH_FLAGS_UMOD     — 当前在 user mode
    // bit 8: SWITCH_FLAGS_IRQ_WIN  — 等待中断窗口
    // bit 9: SWITCH_FLAGS_SINGLE_STEP — 单步调试激活
    // bit 10: SWITCH_FLAGS_PVCS_INVALID — PVCS 映射无效
    // bit 16+: Exit reason（从 Switcher 返回时填入）

    // ═══════════════════════════════════════════════════════
    // 宿主保存状态（VM-enter 前保存，VM-exit 后恢复）
    // ═══════════════════════════════════════════════════════
    u16 host_ds_sel;        // 宿主 DS 选择子
    u16 host_es_sel;        // 宿主 ES 选择子
    u64 host_debugctlmsr;   // 宿主 MSR_IA32_DEBUGCTLMSR

    // ═══════════════════════════════════════════════════════
    // VM-exit 原因数据
    // 联合体：根据 exit_vector 选择解释方式
    // ═══════════════════════════════════════════════════════
    union {
        unsigned long exit_extra;  // 通用 exit 附加数据
        unsigned long exit_cr2;    // #PF 时保存 CR2（故障地址）
        unsigned long exit_dr6;    // #DB 时保存 DR6（调试状态）
        struct ve_info exit_ve;    // #VE（Virtualization Exception）信息
    };
    u32 exit_vector;        // 触发 exit 的向量号（0-255 或 PVM_EXIT_REASONS_*）
    u32 exit_error_code;    // 异常错误码（#PF、#GP 等有效时）

    // ═══════════════════════════════════════════════════════
    // 硬件段选择子（实际加载到 CS/SS 的值）
    // ═══════════════════════════════════════════════════════
    u32 hw_cs;  // 实际 CS = __USER_CS (0x33)
    u32 hw_ss;  // 实际 SS = __USER_DS (0x2b)

    // ═══════════════════════════════════════════════════════
    // CPU 状态加载追踪
    // ═══════════════════════════════════════════════════════
    int loaded_cpu_state;   // CPU 状态是否已加载到硬件
    int int_shadow;         // 中断影子（INT/IRET 后一条指令的中断抑制）

    // ═══════════════════════════════════════════════════════
    // 特殊模式标志
    // ═══════════════════════════════════════════════════════
    bool non_pvm_mode;      // true：Guest 在 PVM 感知前（预引导仿真模式）
    bool nmi_mask;          // NMI 掩码状态
    unsigned long guest_dr7; // Guest 调试寄存器 DR7 虚拟值

    // ═══════════════════════════════════════════════════════
    // PVCS（pvm_vcpu_struct）的 GFN-to-PFN 缓存
    // 避免每次访问 PVCS 都做 GPA→HPA 转换
    // ═══════════════════════════════════════════════════════
    struct gfn_to_pfn_cache pvcs_gpc;

    // ═══════════════════════════════════════════════════════
    // TLB/ASID 管理
    // ═══════════════════════════════════════════════════════
    bool flush_hwtlb_current;  // 需要刷新当前 ASID 的 TLB
    u32  asid;                 // 当前分配的 ASID（1 到 PVM_ASID_MAX）
    u64  asid_generation;      // ASID 代号（用于 ASID 回收检测）

    // ═══════════════════════════════════════════════════════
    // MSR 影子（超管维护的 Guest MSR 虚拟值）
    // ═══════════════════════════════════════════════════════
    // 标准 SYSCALL/SYSRET 相关 MSR
    u64 msr_lstar;          // MSR_LSTAR：Guest 64-bit SYSCALL 入口
    u64 msr_syscall_mask;   // MSR_SYSCALL_MASK：SYSCALL 时 RFLAGS 清零掩码
    u64 msr_star;           // MSR_STAR：SYSCALL/SYSRET 段选择基准
    u64 msr_kernel_gs_base; // MSR_KERNEL_GS_BASE：SWAPGS 时的内核 GS base
    u64 msr_tsc_aux;        // MSR_TSC_AUX：RDTSCP 的辅助值（CPU ID）

    // ═══════════════════════════════════════════════════════
    // PVM 专用 MSR（Guest 通过 WRMSR/RDMSR hypercall 设置）
    // ═══════════════════════════════════════════════════════
    unsigned long msr_vcpu_struct;          // MSR_PVM_VCPU_STRUCT：PVCS 的 GPA
    unsigned long msr_event_entry;          // MSR_PVM_EVENT_ENTRY：事件处理入口 RIP
    unsigned long msr_retu_rip_plus2;       // MSR_PVM_RETU_RIP + 2：EVENT_RETURN 后的 RIP
    unsigned long msr_linear_address_range; // MSR_PVM_LINEAR_ADDRESS_RANGE：地址范围编码

    // ═══════════════════════════════════════════════════════
    // 从 msr_linear_address_range 解码的地址范围边界
    // ═══════════════════════════════════════════════════════
    u64 l4_range_start;  // PML4 索引范围起始（对应 VA 范围起始）
    u64 l4_range_end;    // PML4 索引范围结束
    u64 l5_range_start;  // PML5 索引范围起始（LA57 模式）
    u64 l5_range_end;    // PML5 索引范围结束

    // ═══════════════════════════════════════════════════════
    // 段寄存器虚拟状态（Guest 视图，非硬件实际值）
    // ═══════════════════════════════════════════════════════
    struct kvm_segment segments[NR_VCPU_SREG];
    // 包括：CS、DS、ES、FS、GS、SS、TR、LDTR

    // ═══════════════════════════════════════════════════════
    // 描述符表虚拟状态
    // ═══════════════════════════════════════════════════════
    struct desc_ptr idt_ptr;    // Guest 虚拟 IDTR（仅记录，不实际加载）
    struct desc_ptr gdt_ptr;    // Guest 虚拟 GDTR

    // ═══════════════════════════════════════════════════════
    // TLS 描述符缓存（通过 PVM_HC_LOAD_TLS 同步到宿主 GDT）
    // ═══════════════════════════════════════════════════════
    struct desc_struct tls_array[GDT_ENTRY_TLS_ENTRIES];
    // GDT_ENTRY_TLS_ENTRIES = 3（线程 0/1/2 的 TLS 段描述符）
};
```

### 2.1 vcpu_pvm 与 kvm_vcpu 的关系

```c
// 从 kvm_vcpu 获取 vcpu_pvm
static inline struct vcpu_pvm *to_pvm(struct kvm_vcpu *vcpu)
{
    return container_of(vcpu, struct vcpu_pvm, vcpu);
}

// 从 vcpu_pvm 获取 kvm_vcpu（直接取首字段）
struct kvm_vcpu *vcpu = &pvm->vcpu;
```

`vcpu_pvm` 的第一个字段必须是 `struct kvm_vcpu`，这样 `container_of` 才能正确工作（零偏移，无需减法）。

### 2.2 switch_flags 详细位定义

```c
// 共享常量（arch/x86/include/asm/switcher.h 和 arch/x86/kvm/pvm/pvm.h）

// Switcher 层标志（低 16 位）
#define SWITCH_FLAGS_SMOD            BIT(0)   // 在 supervisor mode
#define SWITCH_FLAGS_UMOD            BIT(1)   // 在 user mode
// bit 2-7: 保留
#define SWITCH_FLAGS_IRQ_WIN         BIT(8)   // IRQ 窗口等待（设置后 Switcher 在 irq 开时退出）
#define SWITCH_FLAGS_SINGLE_STEP     BIT(9)   // 单步执行激活
#define SWITCH_FLAGS_PVCS_INVALID    BIT(10)  // PVCS GPA 映射无效，需重新映射

// Switcher exit reason（高 16 位，从 entry_SYSCALL_64_switcher 传出）
#define PVM_EXIT_REASONS_SYSCALL     (1 << 16)  // Guest 用户态 SYSCALL（需要超管介入）
#define PVM_EXIT_REASONS_HYPERCALL   (2 << 16)  // Guest 内核 hypercall
#define PVM_EXIT_REASONS_ERETU       (3 << 16)  // EVENT_RETURN_USER（返回用户态）
#define PVM_EXIT_REASONS_INTERRUPT   (4 << 16)  // 中断（需要超管处理）
#define PVM_EXIT_REASONS_INT80       (5 << 16)  // 32-bit int 0x80 兼容调用

// Switcher 自己内部使用的 exit reasons（不通过 switch_flags，而是直接传给 pvm_vcpu_run）
#define SWITCH_EXIT_REASONS_SYSCALL         1024  // SYSCALL（从 Switcher 路由给超管）
#define SWITCH_EXIT_REASONS_FAILED_VMENTRY  1025  // VM-entry 失败
```

## 3. struct tss_extra — Switcher 热路径状态

```c
// arch/x86/include/asm/switcher.h
// 放在 TSS 末尾，通过 GS-relative 访问（per-CPU）

struct tss_extra {
    // ═══════════════════════════════════════════════════════
    // CR3 管理（cache-line 对齐，3 个 CR3 值）
    // ═══════════════════════════════════════════════════════
    unsigned long host_cr3;
    // VM-exit 时加载此 CR3，恢复宿主内核页表
    // 由 pvm_prepare_switch_to_guest() 在每次 VM-enter 前设置
    // 等于宿主 per-CPU 的 cpu_tlbstate.loaded_mm->pgd 的物理地址

    unsigned long host_rsp;
    // 宿主 RSP 的保存位置（VM-enter 时保存，VM-exit 时恢复）
    // 当此值为 0 时，表示当前 CPU 不在 PVM 模式（正常宿主路径）
    // Switcher 用这个字段快速判断"是否在 Guest"：
    //   if (tss_extra.host_rsp == 0) → 不在 Guest

    unsigned long enter_cr3;
    // VM-enter 时加载的 CR3（= 当前 Guest 的 smod_cr3 或 umod_cr3，
    // 取决于 Guest 当前处于哪种模式）

    // ═══════════════════════════════════════════════════════
    // 模式控制标志（单独 cache line）
    // ═══════════════════════════════════════════════════════
    unsigned long switch_flags;    // ____cacheline_aligned
    // 与 vcpu_pvm::switch_flags 同步
    // Switcher 汇编直接通过此字段判断 smod/umod

    // ═══════════════════════════════════════════════════════
    // Guest 页表指针
    // ═══════════════════════════════════════════════════════
    unsigned long smod_cr3;
    // Supervisor mode 页表物理地址
    // 进入 smod 时加载（包含 Guest 内核映射 + 受 PKS 保护的用户映射）

    unsigned long umod_cr3;
    // User mode 页表物理地址
    // 进入 umod 时加载（只包含 Guest 用户映射）

    // ═══════════════════════════════════════════════════════
    // Guest-Hypervisor 共享状态页指针
    // ═══════════════════════════════════════════════════════
    struct pvm_vcpu_struct *pvcs;
    // PVCS 的内核虚拟地址（通过 gfn_to_pfn_cache 映射）
    // Switcher 汇编通过此指针访问 PVCS（event_flags、rip、rcx 等）

    // ═══════════════════════════════════════════════════════
    // 事件返回路由
    // ═══════════════════════════════════════════════════════
    unsigned long retu_rip;
    // EVENT_RETURN_USER 的 SYSCALL 指令地址
    // = msr_retu_rip_plus2 - 2（SYSCALL 指令长度为 2 字节）
    // 当 smod 执行 SYSCALL 且 RIP == retu_rip 时，这是 EVENT_RETURN

    // ═══════════════════════════════════════════════════════
    // smod 入口状态
    // ═══════════════════════════════════════════════════════
    unsigned long smod_entry;
    // supervisor mode 事件处理入口 RIP
    // = msr_event_entry + 512（用户态入口 + 512 = supervisor 入口）
    // 当从 umod SYSCALL 路由到 smod 时，SYSRET 跳转到此地址

    unsigned long smod_gsbase;
    // supervisor mode 的 GS base（Guest 内核 per-CPU 指针）
    // = msr_kernel_gs_base 的当前值
    // 进入 smod 时通过 WRGSBASE 加载

} ____cacheline_aligned;
```

### 3.1 tss_extra 在内存中的位置

```c
// arch/x86/include/asm/processor.h
struct tss_struct {
    struct x86_hw_tss  x86_tss;       // 硬件 TSS（104 字节）
    unsigned long      io_bitmap[...]; // I/O 位图
    // ... 其他字段 ...
    struct tss_extra   extra;          // PVM Switcher 状态（在末尾）
} ____cacheline_aligned;

// per-CPU 访问
#define this_cpu_tss_extra() \
    (&per_cpu(cpu_tss_rw, smp_processor_id()).extra)
```

为什么放在 TSS 末尾？因为 TSS 本身是 per-CPU 的，且 GS base 在内核模式下指向 per-CPU 数据区域。通过 GS-relative 寻址可以直接访问 `tss_extra`，无需额外指针。

### 3.2 cache-line 布局分析

```
偏移 0:    host_cr3    (8 bytes)
偏移 8:    host_rsp    (8 bytes)
偏移 16:   enter_cr3   (8 bytes)
偏移 24:   (padding)   (8 bytes)   — 凑满第一个 cache line (32 bytes)

偏移 32:   switch_flags (8 bytes)  — ____cacheline_aligned，第二个 cache line
偏移 40:   smod_cr3    (8 bytes)
偏移 48:   umod_cr3    (8 bytes)
偏移 56:   pvcs        (8 bytes)

偏移 64:   retu_rip    (8 bytes)   — 第三个 cache line
偏移 72:   smod_entry  (8 bytes)
偏移 80:   smod_gsbase (8 bytes)
```

`switch_flags` 对齐到 cache line 边界是关键：Switcher 汇编中，**第一条指令**就读取 `switch_flags` 判断是否需要切换模式。如果这个字段在宿主 syscall 路径被频繁访问（每次 syscall 都会命中 Switcher 检查），cache line 对齐确保读取命中 L1d，分支预测也能优化"不在 Guest"路径。

## 4. struct pvm_vcpu_struct (PVCS) — Guest-Hypervisor 共享状态

PVCS 是 PVM 最独特的设计：一个由超管分配、映射到 Guest 物理地址空间的共享内存页，用于超高速的 Guest-Hypervisor 数据交换。

```c
// arch/x86/include/asm/pvm.h（或 Documentation/virt/kvm/x86/pvm-spec.rst 的 ABI 定义）

struct pvm_vcpu_struct {
    // ═══════════════════════════════════════════════════════
    // 偏移 0: 事件控制标志
    // ═══════════════════════════════════════════════════════
    u64 event_flags;
    // bit 9  (IF): 中断使能标志
    //   - Guest 通过 pvm_irq_enable/disable 设置此位（无需 hypercall）
    //   - 超管在交付中断前检查此位
    //   - 等效于 x86 RFLAGS.IF，但 RFLAGS.IF 硬件值始终为 1
    //
    // bit 8  (IP): 中断待处理标志（Interrupt Pending）
    //   - 超管在 IF=0 时有中断需交付，置此位
    //   - Guest 在 pvm_irq_enable 时检查此位，若置位则发 PVM_HC_IRQ_WIN
    //
    // bit 16-23: 事件类型（超管写入，Guest 读取）
    //   - 0x00: 无事件
    //   - 0x01: 硬件中断（IRQ）
    //   - 0x02: 软件中断（INT n）
    //   - 0x03: 异常（带或不带错误码）
    //   - 0x04: NMI
    //
    // 其余位：保留

    // ═══════════════════════════════════════════════════════
    // 偏移 8: 缺页故障地址
    // ═══════════════════════════════════════════════════════
    u64 cr2;
    // 当事件为 #PF（向量 14）时，超管在此保存故障线性地址
    // Guest 内核在 #PF 处理函数中读取此值（等同于读 CR2）

    // ═══════════════════════════════════════════════════════
    // 偏移 16-63: 保留（未来扩展）
    // ═══════════════════════════════════════════════════════
    u64 reserved0[6];

    // ═══════════════════════════════════════════════════════
    // 偏移 64: 用户态段寄存器（从 umod 进入 smod 时保存）
    // ═══════════════════════════════════════════════════════
    u16 user_cs;     // 用户态 CS（通常 = __USER_CS = 0x33）
    u16 user_ss;     // 用户态 SS（通常 = __USER_DS = 0x2b）

    // ═══════════════════════════════════════════════════════
    // 偏移 68: 事件向量信息（超管写入，Guest 读取）
    // ═══════════════════════════════════════════════════════
    u16 event_errcode;
    // 异常错误码（仅对 #PF、#GP、#SS、#TS、#NP、#AC 有意义）
    // 其余情况为 0

    u16 event_vector;
    // bits 7:0   — 向量号（0-255）
    // bits 15:8  — 事件类型标志：
    //   bit 8: TRAPFLAG   — 陷阱类型（vs 故障类型）
    //   bit 9: ERRCODE    — event_errcode 字段有效
    //   bit 10: NMI       — 这是 NMI
    //   bit 11: SOFT      — 软件产生（INT n、ICEBP）
    //   bit 12: PEND_DBG  — 待处理调试异常

    // ═══════════════════════════════════════════════════════
    // 偏移 72: 用户态 GS base（从 umod 进入 smod 时保存）
    // ═══════════════════════════════════════════════════════
    u64 user_gsbase;
    // 用户态 GS.base（通常是 TLS 段基址）
    // Guest 内核通过此字段读写用户态 GS（代替 SWAPGS + RDGSBASE）

    // ═══════════════════════════════════════════════════════
    // 偏移 80: 用户态 EFLAGS（从 umod 进入 smod 时保存）
    // ═══════════════════════════════════════════════════════
    u32 eflags;
    // 用户态 RFLAGS 的低 32 位（不含 IF，IF 在 event_flags 中）

    // ═══════════════════════════════════════════════════════
    // 偏移 84: 用户态 PKRU 寄存器值
    // ═══════════════════════════════════════════════════════
    u32 pkru;
    // 从 umod 进入 smod 时，Switcher 将硬件 PKRU 寄存器值保存到此处
    // 从 smod 返回 umod 时，Switcher 从此处恢复 PKRU
    // Guest 内核通过 pvm_pkru_save/restore 管理此字段

    // ═══════════════════════════════════════════════════════
    // 偏移 88: 用户态 RIP（EVENT_RETURN 时 Guest 用户态的返回地址）
    // ═══════════════════════════════════════════════════════
    u64 rip;
    // 路径 1：EVENT_RETURN_USER — smod 通过此字段告诉 Switcher 要 SYSRET 到的地址
    // 路径 2：事件交付 — 超管将被打断的 smod RIP 保存到此处，Guest 事件处理完后恢复

    // ═══════════════════════════════════════════════════════
    // 偏移 96: SYSCALL/SYSRET 使用的 RCX/R11
    // ═══════════════════════════════════════════════════════
    u64 rcx;
    // SYSCALL 时 RCX = 返回地址（下一条指令 RIP），Switcher 保存到此
    // SYSRET 时从此恢复 RCX（即用户态 RIP）

    u64 r11;
    // SYSCALL 时 R11 = RFLAGS，Switcher 保存到此
    // SYSRET 时从此恢复 R11（即用户态 RFLAGS 快照）

    // ═══════════════════════════════════════════════════════
    // 偏移 112-127: 保留
    // ═══════════════════════════════════════════════════════
    u64 reserved1[2];
};
// 总大小：128 字节（2 个 cache line）
```

### 4.1 PVCS 的访问方式

**超管侧（pvm.c）**：通过 `gfn_to_pfn_cache` 机制获取 PVCS 的内核虚拟地址：

```c
// 初始化 PVCS 缓存
static int pvm_map_pvcs(struct vcpu_pvm *pvm)
{
    return kvm_gfn_to_pfn_cache_init(pvm->vcpu.kvm,
                                      &pvm->pvcs_gpc,
                                      &pvm->vcpu,
                                      KVM_HOST_USES_PFN,
                                      pvm->msr_vcpu_struct,    // GPA
                                      sizeof(struct pvm_vcpu_struct));
}

// 访问 PVCS（超管侧）
static struct pvm_vcpu_struct *pvm_get_pvcs(struct vcpu_pvm *pvm)
{
    if (kvm_map_gfn(&pvm->pvcs_gpc, KVM_HOST_USES_PFN))
        return NULL;
    return (struct pvm_vcpu_struct *)pvm->pvcs_gpc.khva;
}
```

**Switcher 侧（汇编）**：通过 `tss_extra.pvcs` 指针直接访问（GS-relative）：

```asm
// 读取 event_flags（GS 指向 tss_extra）
movq    TSS_EXTRA_PVCS(%gs:TSS_EXTRA_OFFSET), %rax   // 加载 pvcs 指针
movq    PVCS_OFFSET_EVENT_FLAGS(%rax), %rbx           // 读取 event_flags
```

**Guest 侧（entry_64_pvm.S）**：Guest 知道 PVCS 的虚拟地址（通过 `MSR_PVM_VCPU_STRUCT` 设置，存在 GS 中的固定偏移处）：

```asm
// Guest 内核（entry_64_pvm.S）
pvm_irq_disable:
    btrq    $9, PVM_VCPU_STRUCT_OFFSET(%gs:0)  // 清除 PVCS event_flags.IF
    ret
```

### 4.2 PVCS 中的事件交付流程

超管向 Guest 交付中断的完整流程：

```
1. 超管调用 pvm_inject_event(pvm, vector, error_code, type)

2. 获取 PVCS 写权限：
   pvcs = pvm_get_pvcs(pvm)

3. 填写事件信息：
   pvcs->event_vector = vector | (type << 8)
   pvcs->event_errcode = error_code  （若 type & ERRCODE）
   pvcs->cr2 = exit_cr2              （若 vector == 14，#PF）

4. 检查 IF：
   if (pvcs->event_flags & IF_BIT) {
       // IF=1，立即交付
       pvcs->event_flags &= ~IF_BIT;  // 关闭中断（防止嵌套）
       pvcs->event_flags |= IP_BIT;   // 标记有待处理事件
       // 在下次 VM-enter 时，Switcher 将 RIP 切换到 Guest 事件处理入口
   } else {
       // IF=0，推迟交付
       pvcs->event_flags |= IP_BIT;   // 标记待处理，但不切换 RIP
   }

5. 设置 tss_extra 的事件字段（用于 VM-enter 时 Switcher 识别需要交付）
```

## 5. struct kvm_segment — 段寄存器虚拟状态

每个 vCPU 维护所有段寄存器的虚拟值：

```c
// include/uapi/linux/kvm.h
struct kvm_segment {
    __u64 base;         // 段基址
    __u32 limit;        // 段限制
    __u16 selector;     // 段选择子（虚拟值，如 CS 虚拟为 0x10）
    __u8  type;         // 段类型（code/data/system）
    __u8  present;      // 段有效位
    __u8  dpl;          // 描述符特权级（虚拟，CS 的虚拟 DPL=0）
    __u8  db;           // 默认操作数大小（D bit）
    __u8  s;            // 系统段 vs 非系统段
    __u8  l;            // 64-bit 代码段标志
    __u8  g;            // 粒度（Granularity）
    __u8  avl;          // 保留给软件
    __u8  unusable;     // 段不可用标志
    __u8  padding;
};
```

PVM 在 `pvm_get_segment()`/`pvm_set_segment()` 中维护这些虚拟值，对 QEMU 的 `KVM_GET_SREGS`/`KVM_SET_SREGS` ioctl 透明。

## 6. 完整的数据流关系图

```
                    ┌─────────────────────────┐
                    │  QEMU（用户态 VMM）      │
                    │  KVM_GET_REGS            │
                    │  KVM_RUN                 │
                    └──────────┬──────────────┘
                               │ ioctl
                    ┌──────────▼──────────────┐
                    │  KVM 核心（kvm.ko）      │
                    │  kvm_vcpu_run()          │
                    │  kvm_x86_ops.vcpu_run()  │
                    └──────────┬──────────────┘
                               │ 调用
              ┌────────────────▼────────────────────────┐
              │  vcpu_pvm（hypervisor 层状态）           │
              │  MSR 影子 / 段虚拟值 / PVCS GPC         │
              │  switch_flags / asid / exit_vector       │
              └─────┬──────────────────────┬────────────┘
          VM-enter  │                      │ VM-exit
              ┌─────▼──────────────────────▼────────────┐
              │  tss_extra（per-CPU，Switcher 层）       │
              │  host_cr3  enter_cr3  switch_flags       │
              │  smod_cr3  umod_cr3  pvcs  retu_rip      │
              └──────────────────┬──────────────────────┘
                                 │ 汇编直接访问
              ┌──────────────────▼──────────────────────┐
              │  pvm_vcpu_struct / PVCS（共享内存页）    │
              │  event_flags  cr2  event_vector           │
              │  user_gsbase  pkru  rip  rcx  r11         │
              │                                           │
              │  ← Guest 读写（CPL3，GS-relative）       │
              │  ← Switcher 读写（汇编，GS-relative）    │
              │  ← Hypervisor 读写（pvm_get_pvcs()）     │
              └───────────────────────────────────────────┘
```

## 7. 内存访问模式分析

### 7.1 热路径数据（最频繁访问）

1. **`tss_extra.switch_flags`**：每次 SYSCALL 都读（宿主也好，Guest 也好），必须 cache line 对齐
2. **`tss_extra.smod_cr3` / `tss_extra.umod_cr3`**：每次 smod/umod 切换都写 CR3
3. **`PVCS.event_flags`**：Guest 每次 `pvm_irq_enable/disable` 都访问

### 7.2 冷路径数据（低频访问）

1. `vcpu_pvm.segments[]`：仅在 QEMU ioctl 时访问
2. `vcpu_pvm.gdt_ptr` / `idt_ptr`：仅在 Guest 执行 LGDT/LIDT 时
3. `vcpu_pvm.tls_array[]`：仅在线程创建/销毁时

### 7.3 PVCS 双向写的竞争

PVCS 被 Guest（CPL3，运行在 Guest vCPU）和 Hypervisor（超管，运行在宿主）同时访问，潜在竞争：

- **`event_flags`**：Guest 写 IF 位，超管写 IP 位——原子 bit 操作（BTR/BTS）保证原子性
- **`event_vector` / `cr2`**：超管写，Guest 读——超管在 Guest 处于 smod 时写（此时 IF=0，不会有新的异步访问），或在 VM-exit 后写（Guest 不在运行）
- **`rip` / `rcx` / `r11`**：Switcher 在转换时写（原子操作），Guest 在事件返回时读

这种设计避免了所有需要锁的情况，依赖 x86 的指令顺序语义和 BTR/BTS 原子性。
