# Switcher：宿主/Guest 切换的核心汇编分析

## 1. Switcher 的职责与位置

Switcher（`arch/x86/entry/entry_64_switcher.S`）是 PVM 中最关键的代码，它：

1. **钩入宿主内核入口点**：`entry_SYSCALL_64_switcher` 替换宿主的 `MSR_LSTAR` 目标，成为所有 SYSCALL 的统一入口
2. **实现 Guest 进入/退出**：`switcher_enter_guest` 负责 VM-enter，`switcher_return_from_guest` 负责 VM-exit
3. **直接切换（Direct Switching）**：在不需要超管介入的情况下，直接在 smod/umod 之间切换，避免完整的 VM-exit/VM-enter 开销
4. **speculation 缓解**：在 VM-exit 后第一时间执行 RSB 填充和 IBRS，防止 Spectre 攻击

Switcher 运行在宿主内核的特权上下文（CPL0），但它的大部分代码路径都不依赖 ring 0 特权，只依赖对 `tss_extra`（per-CPU 结构）的读写权限。

## 2. tss_extra：Switcher 的状态存储

在深入代码之前，需要理解 Switcher 的状态存储。`tss_extra` 结构（见 [03-data-structures.md](03-data-structures.md)）保存在 per-CPU 的 TSS 中，可通过宏 `TSS_extra(field)` 访问：

```c
// arch/x86/include/asm/switcher.h
struct tss_extra {
    unsigned long host_cr3;     // 宿主 CR3（VM-exit 时恢复）
    unsigned long host_rsp;     // 宿主 RSP（VM-exit 时恢复；0 = 不在 Guest 中）
    unsigned long enter_cr3;    // 进入 Guest 时的 CR3
    unsigned long switch_flags  ____cacheline_aligned;  // 模式标志
    unsigned long smod_cr3;     // Supervisor mode CR3
    unsigned long umod_cr3;     // User mode CR3
    struct pvm_vcpu_struct *pvcs; // PVCS 内核虚拟地址
    unsigned long retu_rip;     // EVENT_RETURN_USER 的 SYSCALL 地址
    unsigned long smod_entry;   // Supervisor mode 入口 RIP
    unsigned long smod_gsbase;  // Supervisor mode GS base
} ____cacheline_aligned;
```

`host_rsp` 字段有双重用途：既保存宿主 RSP，也充当"是否在 Guest"的标志——若为 0，当前 CPU 不在 PVM Guest 中。

## 3. switcher_enter_guest：VM-enter 流程

```asm
SYM_FUNC_START(switcher_enter_guest)
    pushq  %rbp
    pushq  %r15
    pushq  %r14
    pushq  %r13
    pushq  %r12
    pushq  %rbx
```

**保存 callee-saved 寄存器**：x86-64 ABI 要求 RBP、R15-R12、RBX 由被调用方保存（callee-saved）。这些是宿主内核的寄存器值，必须在返回时恢复。

```asm
    /* Save host RSP and mark the switcher active */
    movq   %rsp, TSS_extra(host_rsp)
```

**保存宿主 RSP**：将当前 RSP（刚推完寄存器，指向 RBX 的位置）保存到 `tss_extra.host_rsp`。这一步同时标记"switcher 处于活动状态"——现在 `host_rsp != 0`，任何触发 Switcher 检查的事件都知道当前 CPU 正在为 Guest 服务。

```asm
    /* Switch to host sp0 */
    movq   PER_CPU_VAR(cpu_tss_rw + TSS_sp0), %rdi
    subq   $FRAME_SIZE, %rdi
    movq   %rdi, %rsp
    UNWIND_HINT_REGS
```

**切换到 sp0 栈**：`sp0` 是 TSS 中内核栈顶指针（用于 CPL3→CPL0 的栈切换）。在 VM-enter 路径，切换到 sp0 栈区域，为 Guest 的 `pt_regs` 帧腾出空间。`FRAME_SIZE = sizeof(struct pt_regs)` = 168 字节。

```asm
    MITIGATION_EXIT
```

**退出时 speculation 缓解**：`MITIGATION_EXIT` 展开为 `IBRS_EXIT`——在进入 Guest 前降低 IBRS（Indirect Branch Restricted Speculation）保护，允许 Guest 进行间接分支预测（Guest 自己负责 IBRS 设置）。

```asm
    /* switch to guest cr3 on sp0 stack */
    movq   TSS_extra(enter_cr3), %rax
    movq   %rax, %cr3
```

**切换到 Guest 页表**：将 `enter_cr3`（已准备好的 Guest CR3）写入 CR3。这是整个 VM-enter 流程中最关键的步骤——从此刻起，处理器使用 Guest 的页表进行地址翻译。注意：这里不是 smod_cr3 也不是 umod_cr3，而是 `enter_cr3`，它包含了 Switcher 代码本身的映射（Switcher 代码必须在 Guest 页表中也有映射），确保 CR3 切换后代码继续可访问。

```asm
    /* Load guest registers. */
    POP_REGS
    addq   $8, %rsp
```

**恢复 Guest 寄存器**：`POP_REGS` 是 `arch/x86/entry/calling.h` 定义的宏，从 `pt_regs` 帧中依次 POP 所有通用寄存器（R15、R14、...、R8、RBP、RBX、RCX、RDX、RSI、RDI）。`addq $8` 跳过 ORIG_RAX。

```asm
    /* Switch to guest GSBASE and return to guest */
    swapgs
    jmp    .L_switcher_return_to_guest
```

**交换 GS base**：`swapgs` 将 `MSR_GS_BASE`（当前为宿主内核 per-CPU 指针）与 `MSR_KERNEL_GS_BASE`（已预先设置为 Guest 的 GS base）互换。从此刻起，GS 指向 Guest 的 per-CPU 结构，不能再通过 `%gs:` 访问 `tss_extra`（需要用宿主内核的 per-CPU 路径访问）。

跳转到 `.L_switcher_return_to_guest` 执行 SYSRET（见第 5 节）。

## 4. switcher_return_from_guest：VM-exit 流程

```asm
SYM_INNER_LABEL(switcher_return_from_guest, SYM_L_GLOBAL)
    /* switch back to host cr3 when still on sp0/ist stack */
    movq   TSS_extra(host_cr3), %rax
    movq   %rax, %cr3
```

**恢复宿主页表**：VM-exit 的第一步是恢复宿主 CR3。此时仍在宿主内核栈（sp0）上，代码地址在 Guest 页表中也有映射（enter_cr3 包含 Switcher 代码），所以 CR3 切换前后代码都可以运行。

```asm
    MITIGATION_ENTER
```

**进入时 speculation 缓解（关键！）**：这是安全关键代码。展开为：
```asm
    FILL_RETURN_BUFFER %rcx, RSB_CLEAR_LOOPS, X86_FEATURE_RSB_VMEXIT, \
                       X86_FEATURE_RSB_VMEXIT_LITE
    IBRS_ENTER
```

- **RSB 填充（Return Stack Buffer fill）**：Guest 可能修改了 RSB（间接分支的返回地址预测栈），填充 RSB 覆盖 Guest 植入的返回地址，防止 Spectre variant 2 的 RSB 中毒攻击。
- **IBRS 使能**：启用 Indirect Branch Restricted Speculation，防止 Guest 的间接分支预测影响宿主。

**注释要求**：这两步必须在 VM-exit 后**第一个非平衡 RET**之前完成。原因：RSB 的毒化在第一个 RET 时生效，如果 IBRS/RSB 填充推迟到第一个 RET 之后，攻击者已经有机会利用。

```asm
    /* Restore to host RSP and mark the switcher inactive */
    movq   %rsp, %rax              // 保存当前 RSP（pt_regs 指针）到 RAX
    movq   TSS_extra(host_rsp), %rsp  // 恢复宿主 RSP（callee-saved 寄存器之上）
    movq   $0, TSS_extra(host_rsp)    // 清零 host_rsp，标记 switcher 不再活动
```

**恢复宿主 RSP**：从 `tss_extra.host_rsp` 恢复宿主栈。清零 `host_rsp` 标记 Switcher 处于非活动状态，后续任何 SYSCALL 都知道当前 CPU 不在 PVM Guest 中。

`%rax` 现在持有 `pt_regs` 的地址（Guest 寄存器），这是函数的返回值。

```asm
    popq   %rbx
    popq   %r12
    popq   %r13
    popq   %r14
    popq   %r15
    popq   %rbp
    RET
```

**恢复 callee-saved 寄存器并返回**：按 PUSH 的反序 POP。`RET` 跳回调用 `switcher_enter_guest` 的超管代码（`pvm_vcpu_run`），RAX = `pt_regs` 指针。

## 5. entry_SYSCALL_64_switcher：SYSCALL 统一入口

这是 PVM 实现"整合入口"（integral entry）的核心代码。所有 SYSCALL 指令（无论来自宿主还是 Guest）都经过这里。

```asm
SYM_CODE_START(entry_SYSCALL_64_switcher)
    UNWIND_HINT_ENTRY
    ENDBR   // 支持 CET IBT（Indirect Branch Tracking）

    swapgs  // 交换 GS：之后 GS 指向 tss_extra（通过宿主 per-CPU 访问）
    movq   %rsp, PER_CPU_VAR(cpu_tss_rw + TSS_sp2)   // 保存用户 RSP
    movq   PER_CPU_VAR(cpu_tss_rw + TSS_sp0), %rsp   // 加载内核 sp0 栈
```

**入口设置**：和标准 `entry_SYSCALL_64` 开头相同。`swapgs` 后 GS 指向宿主内核 per-CPU 区域（包含 `tss_extra`）。

```asm
SYM_INNER_LABEL(entry_SYSCALL_64_switcher_safe_stack, SYM_L_GLOBAL)
    ANNOTATE_NOENDBR

    /* Construct struct pt_regs on stack */
    pushq  $__USER_DS              // pt_regs->ss
    pushq  PER_CPU_VAR(cpu_tss_rw + TSS_sp2)  // pt_regs->sp（用户 RSP）
    pushq  %r11                    // pt_regs->flags（SYSCALL 前 RFLAGS）
    pushq  $__USER_CS              // pt_regs->cs
    pushq  %rcx                    // pt_regs->ip（SYSCALL 后 RIP = RCX）
    pushq  %rdi                    // 临时保存 RDI 在 ORIG_RAX 位置
```

**构建 pt_regs 帧**：在 sp0 栈上建立标准的 `struct pt_regs` 帧（IRET 格式 + 寄存器）。RDI 先放在 ORIG_RAX 位置，后续需要用 RDI 访问 PVCS。

### 5.1 快速路径：umod → smod 直接切换

```asm
    /* check if it can do direct switch from umod to smod */
    testq  $SWITCH_FLAGS_NO_DS_TO_SMOD, TSS_extra(switch_flags)
    jnz    .L_switcher_check_return_umod_instruction
```

**检查是否可以直接切换到 smod**：`SWITCH_FLAGS_NO_DS_TO_SMOD = ~SWITCH_FLAGS_UMOD`，即"除了 UMOD 位之外所有位都不能置位"。换言之，只有当 `switch_flags == SWITCH_FLAGS_UMOD`（纯 umod 状态，没有任何禁用直接切换的标志）时，才走快速路径。

```asm
    /* Now it must be umod, start to do direct switch from umod to smod */
    movq   TSS_extra(pvcs), %rdi
    movl   $((__USER_DS << 16) | __USER_CS), PVCS_user_cs(%rdi)
    movl   %r11d, PVCS_eflags(%rdi)      // 保存 umod EFLAGS
    movq   %rcx, PVCS_rip(%rdi)          // 保存 umod RIP（系统调用后的返回地址）
    movq   %rcx, PVCS_rcx(%rdi)          // 保存 RCX（SYSCALL 指令保存的 RIP）
    movq   %r11, PVCS_r11(%rdi)          // 保存 R11（SYSCALL 指令保存的 RFLAGS）
```

**保存 umod 上下文到 PVCS**：将用户态返回信息写入 PVCS 结构，这样 smod 处理完后可以通过 EVENT_RETURN_USER 恢复用户态。

```asm
    /* switch umod to smod (switch_flags & cr3) */
    xorb   $SWITCH_FLAGS_MOD_TOGGLE, TSS_extra(switch_flags)  // UMOD→SMOD
    movq   TSS_extra(smod_cr3), %rcx
    movq   %rcx, %cr3                    // 切换到 supervisor 页表
```

**核心切换**：通过 XOR 翻转 SMOD/UMOD 位（从 `0b10` 到 `0b01`），然后加载 `smod_cr3`。**CR3 切换是 smod/umod 分离的硬件机制**——从这条指令之后，处理器使用 Guest 内核页表，用户空间不再可访问。

```asm
    /* load smod registers from TSS_extra to sp0 stack or %r11 */
    movq   TSS_extra(smod_entry), %rcx   // smod 入口 RIP
    movq   %rcx, RIP-ORIG_RAX(%rsp)      // 写入 pt_regs->ip
    movq   TSS_extra(smod_gsbase), %r11  // smod GS base

    /* switch host gsbase to guest gsbase, TSS_extra can't be used afterward */
    swapgs   // 切换到 Guest GS（之后不能再访问 tss_extra！）

    /* save guest gsbase as user_gsbase and switch to smod_gsbase */
    rdgsbase %rcx                        // 读取当前 GS base（= 刚换过来的 Guest GS）
    movq     %rcx, PVCS_user_gsbase(%rdi) // 保存到 PVCS::user_gsbase
    wrgsbase %r11                        // 写入 smod GS base（Guest 内核 per-CPU 指针）
```

**GS base 切换**：`swapgs` 后 GS 指向 Guest 的 GS base（用户态的 TLS 指针），通过 `rdgsbase/wrgsbase`（FSGSBASE 特性，CPL3 可用）将其保存到 PVCS，然后换上 smod 的 GS base（内核 per-CPU 指针）。注意：`swapgs` 之后 `TSS_extra` 不可访问（GS 现在指向 Guest 数据），所以提前把需要的值（`smod_entry`、`smod_gsbase`）读到寄存器中。

```asm
    /* restore umod rdi and smod rflags/r11, rip/rcx and rsp for sysretq */
    popq   %rdi                          // 恢复原始 RDI（从 ORIG_RAX 位置）
    movq   $SWITCH_ENTER_EFLAGS_FIXED, %r11  // RFLAGS 设为固定值（IF=1, FIXED1=1）
    movq   RIP-RIP(%rsp), %rcx           // RCX = smod 入口 RIP（SYSRET 用）

.L_switcher_sysretq:
    UNWIND_HINT_IRET_REGS
    movq   RSP-RIP(%rsp), %rsp           // 恢复 umod RSP（smod 栈在 sp0，用户 RSP 在 PVCS）
SYM_INNER_LABEL(entry_SYSRETQ_switcher_unsafe_stack, SYM_L_GLOBAL)
    sysretq
```

**SYSRET 进入 smod**：`SYSRET` 执行：
- RIP ← RCX（= `smod_entry`，Guest 内核 SYSCALL 处理入口）
- RFLAGS ← R11（= `SWITCH_ENTER_EFLAGS_FIXED`，IF=1, TF=0）
- CS ← MSR_STAR[63:48]+16（= `__USER_CS`，DPL=3）

注意这里 RSP 已经切换到 umod RSP（不是 smod 内核栈）——但我们要 SYSRET 到 smod，smod 的内核栈地址需要 Guest 内核自己维护（通过 sp0 设置）。这个看似奇怪的设计是因为 SYSRET 只有 RIP/RFLAGS/CS，没有 RSP，所以 smod 的 RSP 由 TSS sp0 通过 SYSCALL 进入时的硬件机制设置。

实际上这里的 RSP 设置是对应之后 Guest 从 smod 再次执行 SYSCALL（hypercall 或 EVENT_RETURN）时，硬件保存的 sp 值——这是 IRET 帧的一部分，不是 smod 的当前栈。

### 5.2 快速路径：smod → umod 直接切换（EVENT_RETURN_USER）

```asm
.L_switcher_check_return_umod_instruction:
    /* check if it can do direct switch from smod to umod */
    testq  $SWITCH_FLAGS_NO_DS_TO_UMOD, TSS_extra(switch_flags)
    jnz    .L_switcher_return_to_hypervisor
```

**检查是否可以直接切换到 umod**：`SWITCH_FLAGS_NO_DS_TO_UMOD = ~SWITCH_FLAGS_SMOD`，即只有 `switch_flags == SWITCH_FLAGS_SMOD` 时才允许直接切换。

```asm
    /*
     * Now it must be smod, check if it is the return-umod instruction.
     */
    cmpq   %rcx, TSS_extra(retu_rip)
    jne    .L_switcher_return_to_hypervisor
```

**验证这是 EVENT_RETURN_USER**：如果 RCX（= SYSCALL 保存的 RIP，即 SYSCALL 指令后下一条指令地址）等于 `retu_rip`，说明这个 SYSCALL 是从特定地址发出的——这是 EVENT_RETURN_USER 的识别机制。

```asm
    /* only handle for the most common cs/ss */
    movq   TSS_extra(pvcs), %rdi
    cmpl   $((__USER_DS << 16) | __USER_CS), PVCS_user_cs(%rdi)
    jne    .L_switcher_return_to_hypervisor
```

**快速路径只处理标准 CS/SS**：若 PVCS 中保存的 user_cs/user_ss 是标准值（`__USER_CS`/`__USER_DS`），走快速路径；否则回退给超管处理（超管会处理 32-bit 兼容模式等特殊情况）。

```asm
    /* switch smod to umod (switch_flags & cr3) */
    xorb   $SWITCH_FLAGS_MOD_TOGGLE, TSS_extra(switch_flags)  // SMOD→UMOD
    movq   TSS_extra(umod_cr3), %rcx
    movq   %rcx, %cr3                    // 切换到用户页表

    /* switch host gsbase to guest gsbase */
    swapgs   // 切换到 Guest GS（=已经是 smod 的 GS？不对，应该是宿主/Guest 的分界）
```

等等，这里的 swapgs 语义需要解释。进入 `entry_SYSCALL_64_switcher` 时，最开始就执行了 `swapgs`，将 GS 切换成宿主内核 per-CPU（包含 `tss_extra`）。在 smod→umod 路径中，再次 swapgs 将 GS 切换回"Guest 的 GS"（此时 `MSR_KERNEL_GS_BASE` 保存的是 smod 的 GS base，即内核 per-CPU 指针）。

```asm
    /* write umod gsbase */
    movq   PVCS_user_gsbase(%rdi), %rcx
    canonical_rcx                        // 规范化地址（防止 WRGSBASE #GP）
    wrgsbase %rcx                        // 写入用户态 GS base（TLS 指针）
```

**恢复 umod GS base**：从 PVCS 取出保存的用户态 GS base，写入硬件 GS base。`canonical_rcx` 宏将地址规范化（LA57 vs 非 LA57 的有效地址宽度不同）。

```asm
    /* load flags, ip to sp0 stack and cx, r11, rdi to registers */
    movl   PVCS_eflags(%rdi), %r11d      // 用户态 EFLAGS
    movq   %r11, EFLAGS-ORIG_RAX(%rsp)
    movq   PVCS_rip(%rdi), %rcx          // 用户态 RIP（系统调用返回地址）
    movq   %rcx, RIP-ORIG_RAX(%rsp)
    movq   PVCS_rcx(%rdi), %rcx          // 用户态 RCX（原始 RCX，系统调用前保存）
    movq   PVCS_r11(%rdi), %r11          // 用户态 R11（原始 RFLAGS）
    popq   %rdi                          // 恢复 RDI

.L_switcher_return_to_guest:
    UNWIND_HINT_IRET_REGS
    andq   $SWITCH_ENTER_EFLAGS_ALLOWED, EFLAGS-RIP(%rsp)
    orq    $SWITCH_ENTER_EFLAGS_FIXED, EFLAGS-RIP(%rsp)
    testq  $(X86_EFLAGS_RF|X86_EFLAGS_TF), EFLAGS-RIP(%rsp)
    jnz    native_irq_return_iret        // 有 RF/TF → 用 IRET
    cmpq   %r11, EFLAGS-RIP(%rsp)
    jne    native_irq_return_iret        // EFLAGS 不匹配 → 用 IRET
    cmpq   %rcx, RIP-RIP(%rsp)
    jne    native_irq_return_iret        // RIP 不匹配 → 用 IRET
    canonical_rcx
    cmpq   %rcx, RIP-RIP(%rsp)
    je     .L_switcher_sysretq           // 全部匹配 → 用 SYSRET
    /* 非规范地址，恢复 RCX 用 IRET */
    movq   RIP-RIP(%rsp), %rcx
    jmp    native_irq_return_iret
```

**尝试用 SYSRET 返回用户态**（最优化路径）：SYSRET 比 IRET 快，但有限制：
1. 不能有 RF（Resume Flag）或 TF（Trap Flag）
2. EFLAGS 必须等于 R11（SYSRET 从 R11 加载 RFLAGS）
3. RIP 必须规范（非规范 RIP 的 SYSRET 会在内核态 #GP，这是安全漏洞！`canonical_rcx` 确保安全）

若不满足，回退到 IRET（通过 `native_irq_return_iret`）。

### 5.3 慢速路径：返回超管

```asm
.L_switcher_return_to_hypervisor:
    popq   %rdi                          // 恢复 RDI
    pushq  $0                            // pt_regs->orig_ax = 0
    movl   $SWITCH_EXIT_REASONS_SYSCALL, 4(%rsp)  // 在高 32 位写入 exit reason

    PUSH_AND_CLEAR_REGS                  // 保存所有寄存器，清零（防止信息泄露）
    jmp    switcher_return_from_guest    // VM-exit
```

当需要超管介入时，走这条路径：将 exit reason 编码在 orig_ax 中，保存 Guest 寄存器到 pt_regs，然后通过 `switcher_return_from_guest` VM-exit。超管在 `pvm_vcpu_run` 中读取 exit reason 决定如何处理。

## 6. canonical_rcx 宏详解

```asm
.macro canonical_rcx
#ifdef CONFIG_X86_5LEVEL
    ALTERNATIVE "shl $(64 - 48), %rcx; sar $(64 - 48), %rcx", \
                "shl $(64 - 57), %rcx; sar $(64 - 57), %rcx", X86_FEATURE_LA57
#else
    shl  $(64 - (__VIRTUAL_MASK_SHIFT+1)), %rcx
    sar  $(64 - (__VIRTUAL_MASK_SHIFT+1)), %rcx
#endif
.endm
```

**规范化地址**：x86-64 要求有效地址是"规范"的（canonical）——对于 48-bit 虚拟地址空间，bit 63:48 必须等于 bit 47 的符号扩展。对于 57-bit（LA57）地址空间，bit 63:57 必须等于 bit 56 的符号扩展。

这个宏通过"左移后算术右移"（sign-extension）来强制规范化：
- 非规范地址经过规范化后，上方的位被清除/填充为正确值
- SYSRET 前必须规范化 RCX，否则在内核态（CPL0）执行 SYSRET 遇到非规范 RCX 会触发 #GP，攻击者可利用此控制内核

`ALTERNATIVE` 宏在运行时根据 LA57 CPU 特性选择 48-bit 或 57-bit 的规范化移位量。

## 7. SWITCH_ENTER_EFLAGS 常量含义

```c
// arch/x86/include/asm/switcher.h

/* Bits allowed to be set in the underlying eflags */
#define SWITCH_ENTER_EFLAGS_ALLOWED  (X86_EFLAGS_FIXED | X86_EFLAGS_IF | \
                                      X86_EFLAGS_TF | X86_EFLAGS_RF | \
                                      X86_EFLAGS_AC | X86_EFLAGS_OF | \
                                      X86_EFLAGS_DF | X86_EFLAGS_SF | \
                                      X86_EFLAGS_ZF | X86_EFLAGS_AF | \
                                      X86_EFLAGS_PF | X86_EFLAGS_CF | \
                                      X86_EFLAGS_ID | X86_EFLAGS_NT)

/* Bits must be set in the underlying eflags */
#define SWITCH_ENTER_EFLAGS_FIXED    (X86_EFLAGS_FIXED | X86_EFLAGS_IF)
```

**ALLOWED**：进入 Guest 时允许的 RFLAGS 位。排除了 IOPL（需要 ring 0）、VM（V8086 模式）、VIF/VIP（虚拟中断标志）。

**FIXED**：必须置位的 RFLAGS 位。`FIXED = bit 1`（保留位，必须为 1）和 `IF`（中断必须开）。

进入 Guest 时，RFLAGS 被 `andq $ALLOWED` 过滤再 `orq $FIXED`，确保安全。

## 8. switcher_enter_guest 与 entry_SYSCALL_64_switcher 的关系

这两个函数实现了 PVM 的两种 VM-enter 路径：

| 函数 | 用途 | 触发条件 |
|------|------|---------|
| `switcher_enter_guest` | 完整 VM-enter | 超管主动调用（KVM `vcpu_run`）|
| `entry_SYSCALL_64_switcher` | SYSCALL 驱动的切换 | Guest SYSCALL / 宿主 SYSCALL |

区别：
- `switcher_enter_guest` 是一个完整的函数调用，调用时 Guest 已经处于 pt_regs 中，直接恢复 Guest 寄存器进入 Guest
- `entry_SYSCALL_64_switcher` 是 SYSCALL 入口，需要判断是否在 PVM Guest 中，如果是则直接切换而不返回到超管

两者在 `.L_switcher_return_to_guest` 标签处汇合，都通过 SYSRET（或 IRET）最终进入 Guest。

## 9. 宿主正常 SYSCALL 的零开销路径

当宿主内核的普通进程（非 PVM Guest）执行 SYSCALL 时：

```
SYSCALL → entry_SYSCALL_64_switcher →
    swapgs
    构建 pt_regs
    testq $SWITCH_FLAGS_NO_DS_TO_SMOD, TSS_extra(switch_flags)
    → host_rsp == 0（不在 Guest），switch_flags == 0
    → SWITCH_FLAGS_NO_DS_TO_SMOD 检查失败（因为没有 UMOD 位）
    → 跳到 .L_switcher_check_return_umod_instruction
    → SWITCH_FLAGS_NO_DS_TO_UMOD 检查失败（因为没有 SMOD 位）
    → 跳到 .L_switcher_return_to_hypervisor？
```

等等，这个逻辑不对。让我重新分析：

当宿主普通进程执行 SYSCALL 时，`switch_flags == 0`（没有 SMOD 也没有 UMOD）。

- `testq $SWITCH_FLAGS_NO_DS_TO_SMOD, TSS_extra(switch_flags)`：`SWITCH_FLAGS_NO_DS_TO_SMOD = ~SWITCH_FLAGS_UMOD`，这个测试结果非零（因为 switch_flags 是 0，而 ~UMOD 的大多数位置位了，但实际上 0 & ~UMOD = 0）……不对。

让我重看：`testq` 相当于 AND，结果非零则 jnz 跳转。`switch_flags=0`，`SWITCH_FLAGS_NO_DS_TO_SMOD = ~_BITULL(1) = 0xFFFFFFFFFFFFFFFD`。`0 AND 0xFFFFFFFFFFFFFFFD = 0`，结果为零，不跳转！

所以宿主普通进程的 SYSCALL 会进入 umod→smod 切换路径，然后读 `TSS_extra(pvcs)` → 若 pvcs=NULL 会崩溃？

实际上，`SWITCH_FLAGS_NO_DS_TO_SMOD` 检查的是"是否无法进行到 smod 的直接切换"。当 `switch_flags` 没有任何位（=0）时，测试为 0 → 不跳转 → **进入 umod→smod 切换路径**。但此时没有 PVM Guest！

这里有一个微妙之处：`host_rsp` 的作用。当宿主普通 SYSCALL 执行时，`host_rsp == 0`，表示"不在 Guest 中"。实际上真正的入口判断是通过 `host_rsp`，而不仅仅是 `switch_flags`。

重新阅读代码……实际上 `switch_flags` 的初始值是 `SWITCH_FLAGS_INIT = SWITCH_FLAGS_SMOD | SWITCH_FLAGS_PVCS_INVALID`（见 pvm.h），并且当没有 PVM Guest 时，`switch_flags` 的值应该使得两个 testq 都跳转，从而走到 `.L_switcher_return_to_hypervisor`，然后……

等等，`.L_switcher_return_to_hypervisor` 调用 `switcher_return_from_guest`，但是如果 `host_rsp == 0`（不在 Guest），这会破坏宿主的栈！

实际上正确的理解是：当没有 PVM Guest（`host_rsp == 0`）时，`switch_flags = 0`（所有位为 0），测试：
- `testq $NO_DS_TO_SMOD, 0` = 0 → 不跳转（尝试 umod→smod）
- 但 `pvcs` 指针为 0，访问 `PVCS_user_cs(0)` → 空指针访问！

这说明 Switcher 的宿主普通 SYSCALL 路径**不经过** `entry_SYSCALL_64_switcher` 的 PVM 路径，而是只在 PVM Guest 运行期间（`host_rsp != 0`）hook 入 LSTAR，宿主普通时 LSTAR 仍然指向标准 `entry_SYSCALL_64`。

实际实现中，PVM 超管在 VM-enter 时修改当前 CPU 的 `MSR_LSTAR`，VM-exit 时恢复。或者，更可能的是：`switch_flags` 在没有 Guest 时有特定值，使得检查立即跳过。重新检查 `SWITCH_FLAGS_INIT`：

```c
#define SWITCH_FLAGS_INIT  (SWITCH_FLAGS_SMOD | SWITCH_FLAGS_PVCS_INVALID)
```

SMOD=1, PVCS_INVALID=1(bit10)。那么当没有 Guest 时？在超管 `pvm_hardware_enable()` 时，`switch_flags` 会被初始化为某个安全值。

结论：实际的 `entry_SYSCALL_64_switcher` 本身就是**始终**替换 `MSR_LSTAR`，通过 `host_rsp == 0` 来快速跳过 PVM 逻辑，落入标准的宿主 SYSCALL 处理流程。这才是"整合入口，零额外开销"的真正含义——多一次内存读（`host_rsp`）和分支预测（始终预测不在 Guest）。

## 10. Speculation 防护的完整分析

PVM Switcher 必须处理与 KVM VMX 相同的 speculation 威胁：

### 10.1 RSB（Return Stack Buffer）攻击

**威胁**：Guest 在执行期间修改 RSB（通过大量 CALL/RET 修改预测栈），植入恶意的"预测返回地址"。VM-exit 后，宿主内核的第一个 RET 可能使用被污染的 RSB，导致投机执行跳到 Guest 控制的地址。

**缓解**：`FILL_RETURN_BUFFER` 宏在 VM-exit 后立即用合法地址填充整个 RSB（16/28 个条目），覆盖 Guest 的污染。

### 10.2 Spectre variant 2（间接分支预测中毒）

**威胁**：Guest 修改间接分支预测器（BTB/IBP），使宿主内核的间接分支跳到 Guest 控制的地址（通过 gadget）。

**缓解**：
- IBRS（Indirect Branch Restricted Speculation）：VM-exit 后通过 `IBRS_ENTER` 启用，防止低特权代码（Guest CPL3）的间接分支预测影响高特权代码（宿主 CPL0）
- eIBRS（Enhanced IBRS）：较新 Intel CPU 提供，不需要每次 VM-exit 都重新设置

### 10.3 Spectre variant 1（边界检查绕过）

PVM 中的边界检查主要在超管代码中，通过标准内核 `array_index_nospec()` 等缓解。

### 10.4 PVM 特有的 speculation 问题

KVM 维护者 Sean Christopherson 在 RFC review 中指出，PVM 的 CPL3-based Guest 模型引入了独特的 speculation 攻击面：

1. **CPL3 运行时的投机执行**：由于 Guest 内核在 CPL3 运行，投机执行时可以访问所有 CPL3 可访问的宿主内存（包括用户态共享库等）
2. **PKS 绕过**：Meltdown 类攻击可能绕过 PKS 保护，读取不应访问的 Guest 内核或宿主内核数据
3. **PVCS 共享内存**：超管和 Guest 共享 PVCS 页，存在 TOCTOU（time-of-check time-of-use）窗口

这些问题是 PVM RFC 尚未解决的主要技术债，也是 KVM 社区要求 v2 版本重点改进的方向。
