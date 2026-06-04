# PVM 事件交付机制：PVCS 模型与 Guest 入口汇编

## 1. 为什么不能用 IDT

传统 Linux 内核通过 IDT（Interrupt Descriptor Table）接收中断和异常：
- IDT 中的中断门（Interrupt Gate）配置 DPL=0，即只有 CPL ≤ DPL 才能通过 INT 触发
- 中断发生时，硬件将 CS/RIP/RFLAGS 压栈（有时还有 Error Code），然后跳转到 IDT 中配置的处理函数
- 跳转时可以发生特权级切换（ring 3→ring 0），TSS 的 sp0 字段提供新 RSP

PVM Guest 的问题：
1. IDT 由宿主内核控制（宿主有自己的 IDTR），Guest 不能设置自己的 IDTR（LIDT 需要 CPL0）
2. 即使 Guest 想安装中断门，目标 RIP 在 Guest VA 空间，不在 ring 0
3. 发生中断时，x86 硬件会用宿主的 IDT，跳到宿主的中断处理函数，Guest 永远不知道

因此，PVM 需要一个完全软件化的事件交付机制，绕过 IDT。

## 2. PVCS 事件模型概述

PVM 的事件交付通过两个通信渠道：

1. **PVCS 共享内存**（`struct pvm_vcpu_struct`）：超管写入事件信息，Guest 读取
2. **MSR_PVM_EVENT_ENTRY**：定义 Guest 的事件处理入口点

整个流程：

```
事件发生（中断/异常）
    │
    ▼
超管（pvm_handle_exit 或 pvm_inject_event）
    │
    ├─► 写入 PVCS：
    │     event_vector = vector | (type_flags << 8)
    │     event_errcode = error_code（若有）
    │     cr2 = fault_addr（若是 #PF）
    │
    ├─► 检查 PVCS::event_flags.IF
    │     IF=1：中断使能，立即交付
    │       → 清除 IF，设置 IP
    │       → 设置 VM-enter 目标 RIP = smod_entry（事件处理入口）
    │     IF=0：中断禁止，推迟
    │       → 设置 IP（Interrupt Pending）
    │       → 保持事件在 event_vector 中
    │
    ▼
VM-enter → Guest 执行 pvm_user_event_entry 或 pvm_kernel_event_entry
    │
    ▼
Guest 事件处理函数（pvm_event）
    │
    ▼
EVENT_RETURN（pvm_restore_regs_and_return_to_usermode）
    │
    ▼
Switcher：从 PVCS 恢复上下文，切换 CR3，SYSRET 回原执行点
```

## 3. PVCS::event_flags 的精确语义

```c
// event_flags 是一个 64-bit 字段，只有 bit 8 和 bit 9 有效：

// bit 9: IF（Interrupt Flag）
//   = 1：Guest 内核允许接收可屏蔽中断（类似 x86 RFLAGS.IF）
//   = 0：Guest 内核禁止接收可屏蔽中断
//   仅在 supervisor mode 下有意义。用户态始终允许中断。

// bit 8: IP（Interrupt Pending）
//   = 1：有待处理的中断（在 IF=0 时超管无法立即交付，挂起到此位）
//   = 0：无待处理中断
//   Guest 在 IRQ_WIN hypercall 中，超管处理并清除此位。
```

**IF 的工作流程**：

```
正常状态（IF=1）：
  超管收到 IRQ → 写 event_vector/flags → 清除 IF → 设置 VM-enter 入口
  Guest 进入 pvm_kernel_event_entry 处理中断

IF=0 时（临界区）：
  超管收到 IRQ → 设置 IP 位（不改变 event_vector/IF）
  当 Guest 退出临界区：
    pvm_irq_enable() → 设置 IF=1 → 检查 IP 位
    若 IP=1 → 调用 PVM_HC_IRQ_WIN
    超管在 IRQ_WIN 中处理挂起的 IRQ，清除 IP
```

注意：IP 位可能在没有实际 IRQ 时被置位（超管的某些路径可能误置），Guest 对此无害——IRQ_WIN hypercall 时超管检查是否真的有 IRQ 待处理，若没有就直接返回。

## 4. PVCS::event_vector 详解

```c
// event_vector 是 16-bit 字段：

// bits 7:0：向量号（0-255）
//   对于非异步异常事件有效

// bits 15:8：事件类型标志：
#define PVM_PVCS_EVENT_VECTOR_STD_BIT  8
#define PVM_PVCS_EVENT_VECTOR_STD      _BITUL(8)  // 标准（非异步）事件正在交付
#define PVM_PVCS_EVENT_VECTOR_NMI_BIT  9
#define PVM_PVCS_EVENT_VECTOR_NMI      _BITUL(9)  // NMI 正在交付或挂起
#define PVM_PVCS_EVENT_VECTOR_MCE_BIT  10
#define PVM_PVCS_EVENT_VECTOR_MCE      _BITUL(10) // MCE 正在交付或挂起
```

**异步异常 vs 非异步异常**：

- **异步异常**（Async Exception）：NMI（向量 2）和 MCE（向量 18）。可以在任意时刻交付，即使 IF=0，即使在 PVCS 正在写入的窗口期也可以。
- **非异步异常**（Standard Event）：所有其他向量（中断、异常），受 IF 控制，在 PVCS 写入窗口期不能交付（否则 PVCS 结构会被覆盖）。

`PVM_PVCS_EVENT_VECTOR_STD` 位用作"PVCS 繁忙"锁：

```
规则：
  当 event_vector 的 STD 位置位时，PVCS 中的 user_cs、user_ss、
  user_gsbase、pkru、eflags、rip、rcx、r11 字段是"被使用中的"，
  不能被非异步事件覆盖。

  Guest 在返回用户态（ERETU）前必须置位 STD 位（通过
  POP_REGS_AND_LOAD_IRET_FRAME_INTO_PVCS 宏）。
  超管在 ERETU 处理中也检查 NMI/MCE 位。
```

具体流程：
```asm
// entry_64_pvm.S：准备返回用户态
POP_REGS_AND_LOAD_IRET_FRAME_INTO_PVCS:
    POP_REGS

    // 标记 PVCS 繁忙（STD 位置位），保护 ERETU 窗口
    btsw   $PVM_PVCS_EVENT_VECTOR_STD_BIT, PVCS_event_vector(%gs:PVCS_OFFSET)

    // 复制 IRET 帧到 PVCS（RIP、CS、RFLAGS、RSP、SS）
    movq   1*8(%rsp), %rcx        /* RIP */
    movq   %rcx, PVCS_rip(%gs:PVCS_OFFSET)
    // ... 其他字段
pvm_retu_rip:
    syscall                       // EVENT_RETURN_USER
```

## 5. entry_64_pvm.S 逐函数分析

### 5.1 PUSH_IRET_FRAME_FROM_PVCS 宏

在 Guest 事件处理函数入口，需要从 PVCS 中恢复被中断时的上下文，构建标准的 `pt_regs` 帧供 C 函数使用：

```asm
.macro PUSH_IRET_FRAME_FROM_PVCS user:req
    movq   %rsp, %r11   // 保存当前 RSP

    .if \user == 1
        // 用户态事件：切换到内核栈顶（从 pcpu_hot 获取）
        movq   PER_CPU_VAR(pcpu_hot + X86_top_of_stack), %rsp
        // 推入 SS（来自 PVCS::user_ss）
        movl   PER_CPU_VAR(pvm_vcpu_struct + PVCS_user_ss), %ecx
        andl   $0xFFFF, %ecx
        pushq  %rcx                   // pt_regs->ss
    .else
        // 内核（supervisor）事件：在当前栈上操作
        andq   $-16, %rsp             // 对齐到 16 字节
        subq   $REDZONE_SIZE, %rsp    // 预留红区（16 字节）
        pushq  $__KERNEL_DS           // pt_regs->ss（内核 DS）
    .endif

    pushq  %r11                       // pt_regs->sp（被中断时的 RSP）
    // 推入 EFLAGS
    movl   PER_CPU_VAR(pvm_vcpu_struct + PVCS_eflags), %ecx
    pushq  %rcx                       // pt_regs->flags

    .if \user == 1
        // 推入用户态 CS（来自 PVCS::user_cs）
        movl   PER_CPU_VAR(pvm_vcpu_struct + PVCS_user_cs), %ecx
        andl   $0xFFFF, %ecx
        pushq  %rcx                   // pt_regs->cs
    .else
        pushq  $__KERNEL_CS           // pt_regs->cs（内核 CS）
    .endif

    pushq  PER_CPU_VAR(pvm_vcpu_struct + PVCS_rip)  // pt_regs->ip

    // 设置 RCX 和 R11（PVM 事件交付规范）
    movq   PER_CPU_VAR(pvm_vcpu_struct + PVCS_rcx), %rcx
    movq   PER_CPU_VAR(pvm_vcpu_struct + PVCS_r11), %r11
.endm
```

**用户态 vs 内核态的区别**：
- 用户态事件（user=1）：切换到独立的内核栈（sp0），推入用户态 CS/SS（来自 PVCS）
- 内核态事件（user=0）：在当前 smod 栈上继续（不切换栈），使用 `__KERNEL_CS`/`__KERNEL_DS`

### 5.2 entry_SYSCALL_64_pvm — 用户态 SYSCALL 入口

```asm
SYM_CODE_START(entry_SYSCALL_64_pvm)
    UNWIND_HINT_ENTRY
    ENDBR

    // 1. 从 PVCS 构建 pt_regs（用户态格式）
    PUSH_IRET_FRAME_FROM_PVCS user=1

    // 2. 清除 STD 位（解锁 PVCS），同时检查有无异步事件
    btrw   $PVM_PVCS_EVENT_VECTOR_STD_BIT, PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_vector)
    cmpw   $0, PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_vector)
    jz     entry_SYSCALL_64_after_hwframe  // 无挂起异步事件 → 正常系统调用处理

.L_async_exception:
    // 3. 有挂起的异步事件（NMI/MCE）：先处理事件
    PUSH_REGS_AND_HANDLE_EVENT handler=pvm_event
    POP_REGS
    addq   $8, %rsp
    jmp    entry_SYSCALL_64_after_hwframe  // 处理完后继续系统调用
SYM_CODE_END(entry_SYSCALL_64_pvm)
```

**关键点**：`entry_SYSCALL_64_pvm` 是 PVM Guest 版的 SYSCALL 入口（替代标准的 `entry_SYSCALL_64`）。它的特殊之处：

1. 不执行 `swapgs`（Switcher 已经处理了 GS 切换）
2. 不执行 RFLAGS 处理（来自 PVCS）
3. 检查异步事件：Switcher 在 umod→smod 直接切换时，会将 SYSCALL 事件（vector）保留在 PVCS，以便 smod 进入后立即处理

### 5.3 pvm_user_event_entry — 用户态向量事件处理

```asm
    .align 64   // 必须对齐到 64 字节（MSR_PVM_EVENT_ENTRY 要求）
SYM_CODE_START(pvm_user_event_entry)
    UNWIND_HINT_ENTRY
    ENDBR

    // 1. 从 PVCS 构建 pt_regs（用户态格式）
    PUSH_IRET_FRAME_FROM_PVCS user=1

    // 2. 保存所有寄存器并调用 C 事件处理函数
    PUSH_REGS_AND_HANDLE_EVENT handler=pvm_event
```

`.macro PUSH_REGS_AND_HANDLE_EVENT handler:req` 展开：
```asm
    pushq  $-1                    // orig_ax = -1（非系统调用）
    PUSH_AND_CLEAR_REGS           // 保存并清零所有 GPR（防止信息泄露）
    // 参数传递给 C 函数 pvm_event：
    movw   PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_errcode), %dx  // 错误码
    xorl   %esi, %esi
    // 用 xchgw 原子性地读取并清零 event_vector（防止并发的异步事件覆盖）
    xchgw  %si, PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_vector)
    movq   %rsp, %rdi             // pt_regs 指针
    call   \handler               // 调用 pvm_event(pt_regs*, event_vector, errcode)
```

注意 `xchgw`（原子交换）：读取 `event_vector` 并同时清零，确保处理函数看到的是一致的 event_vector 值，即使此后 NMI/MCE 再来也不会干扰（因为字段已被清零）。

```asm
SYM_INNER_LABEL(pvm_restore_regs_and_return_to_usermode, SYM_L_GLOBAL)
    STACKLEAK_ERASE               // 安全：清除内核栈上的敏感数据
    POP_REGS_AND_LOAD_IRET_FRAME_INTO_PVCS  // 恢复寄存器并写回 PVCS
SYM_INNER_LABEL(pvm_retu_rip, SYM_L_GLOBAL)
    ANNOTATE_NOENDBR
    syscall                       // EVENT_RETURN_USER（Switcher 识别并处理）
SYM_CODE_END(pvm_user_event_entry)
```

### 5.4 pvm_kernel_event_entry — Supervisor Mode 事件处理

Supervisor mode 事件处理比用户态复杂，因为需要处理"IRET 窗口期"的递归事件：

```
问题：Guest 内核在返回用户态的最后几条指令（标记为 iret_irq_enabled_start 到
      iret_irq_enabled_end）中，重新开启了中断（IF=1），但还未完成 ERETU。
      如果此时有中断来临，PVCS 中的 user_cs/user_ss/rip 等字段正在被写入，
      可能处于不一致状态。
```

Supervisor mode 事件入口的 IRET fixup：

```asm
SYM_CODE_START(pvm_kernel_event_entry)
    UNWIND_HINT_ENTRY
    ENDBR

    PUSH_IRET_FRAME_FROM_PVCS user=0   // 构建 smod pt_regs

    // 检查被中断的 RIP 是否在 IRET 窗口期内
    leaq   .L_pvm_supervisor_iret_irq_enabled_start(%rip), %rcx
    cmpq   RIP-RIP(%rsp), %rcx
    ja     .L_pvm_supervisor_iret_irq_fixed  // RIP < start → 不在窗口，无需 fixup

    leaq   .L_pvm_supervisor_iret_irq_enabled_end(%rip), %rcx
    cmpq   RIP-RIP(%rsp), %rcx
    jbe    .L_pvm_supervisor_iret_irq_fixed  // RIP >= end → 不在窗口，无需 fixup

    // 在窗口期内！需要修复
    // 被中断时的栈帧布局（从当前 RSP 往上）：
    //   Previous IRET Frame（用于 iret 到用户态的 CS/RIP/RFLAGS/RSP/SS）
    //   RAX                   +40
    //   RCX                   +32
    //   R11                   +24
    //   HW ring3 IRET Frame（SYSCALL 产生的）
    //   padding（8 bytes）
    //   RED_ZONE（16 bytes）
    //   Current IRET Frame（指向此处的 RSP）
    movq   RSP-RIP(%rsp), %rcx    // 获取被中断时的 RSP
    movq   (16+40)(%rcx), %rax    // Previous IRET Frame 的 RAX
    movq   ( 8+40)(%rcx), %r11    // 获取 RCX 位置的值（被中断时的 RCX）
    movq   %r11, PER_CPU_VAR(pvm_vcpu_struct + PVCS_rcx)
    movq   ( 0+40)(%rcx), %r11    // Previous IRET Frame 的 R11
    leaq   (24+40)(%rcx), %rsp    // 跳过损坏的帧，回到正常 smod 栈
.L_pvm_supervisor_iret_irq_fixed:
    movq   PER_CPU_VAR(pvm_vcpu_struct + PVCS_rcx), %rcx

    PUSH_REGS_AND_HANDLE_EVENT handler=pvm_event
```

**IRET fixup 的必要性**：

在 `iret_irq_enabled_start ~ iret_irq_enabled_end` 区间内，Guest 内核正在执行如下序列：

```asm
// 重新开启中断（IF=1）
orq   $X86_EFLAGS_IF, PVCS_event_flags

// 检查 IP 位
btq   $IP_BIT, PVCS_event_flags
jc    .L_pvm_supervisor_iret_irq_pending

// 正常 IRET 路径
.L_pvm_supervisor_iret_irq_enabled_start:
    btq   $IP_BIT, PVCS_event_flags   // 再次检查
    jc    .L_pvm_supervisor_iret_irq_pending
.L_pvm_supervisor_iret:
    movq  8*7(%rsp), %rax
    iretq
.L_pvm_supervisor_iret_irq_pending:
    // 处理挂起 IRQ
    movq  $PVM_HC_IRQ_WIN, %rax
    syscall
    movq  8*5(%rsp), %r11
    movq  8*6(%rsp), %rcx
    jmp   .L_pvm_supervisor_iret
.L_pvm_supervisor_iret_irq_enabled_end:
```

在这个区间内，栈上有"Previous IRET Frame"（准备 iretq 回用户态的数据）、RAX/RCX/R11 等临时值。如果中断在此时发生，正常的事件处理会构建一个不完整的 pt_regs，需要 fixup 逻辑恢复正确的状态。

### 5.5 pvm_early_kernel_event_entry — 早期引导事件

```asm
SYM_CODE_START_NOALIGN(pvm_early_kernel_event_entry)
    UNWIND_HINT_ENTRY
    ENDBR

    incl   early_recursion_flag(%rip)   // 递归计数 +1
    PUSH_IRET_FRAME_FROM_PVCS user=0
    PUSH_REGS_AND_HANDLE_EVENT handler=pvm_early_event
    decl   early_recursion_flag(%rip)   // 递归计数 -1
    jmp    .L_pvm_supervisor_restore_regs_and_return
SYM_CODE_END(pvm_early_kernel_event_entry)
```

在 Guest 内核早期（MSR_PVM_EVENT_ENTRY 可能指向此入口），使用 `pvm_early_event` 处理函数（更简单的处理，避免依赖尚未初始化的子系统），同时有 `early_recursion_flag` 防止早期递归异常。

### 5.6 pvm_irq_disable / pvm_irq_enable — 中断控制

这两个函数在 `.noinstr.text` 节（不可插桩），实现虚拟 CLI/STI：

```asm
SYM_FUNC_START(pvm_irq_disable)
    // 原子清除 IF 位（BTR = Bit Test and Reset）
    btrq   $X86_EFLAGS_IF_BIT, PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_flags)
    RET
SYM_FUNC_END(pvm_irq_disable)

SYM_FUNC_START(pvm_irq_enable)
    // 原子设置 IF 位（OR）
    orq    $X86_EFLAGS_IF, PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_flags)
    // 检查 IP 位
    btq    $PVM_EVENT_FLAGS_IP_BIT, PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_flags)
    jc     .L_maybe_interrupt_pending
    RET
.L_maybe_interrupt_pending:
    movq   $PVM_HC_IRQ_WIN, %rax
    jmp    pvm_hypercall   // 通知超管有待处理中断
SYM_FUNC_END(pvm_irq_enable)
```

**pvm_save_fl — 读取中断标志**：

```asm
SYM_FUNC_START(pvm_save_fl)
    movq   PER_CPU_VAR(pvm_vcpu_struct + PVCS_event_flags), %rax
    RET
SYM_FUNC_END(pvm_save_fl)
```

返回整个 `event_flags`，调用者提取 bit 9（IF）来判断中断是否使能。

## 6. 事件交付的完整时序图

### 6.1 外部中断（IRQ）交付到 smod

```
时刻 T1：硬件 IRQ 到达宿主 CPU
  → 宿主内核处理 IRQ（正常路径）
  → KVM 检测到有 vIRQ 需要交付（通过 kvm_cpu_has_interrupt）

时刻 T2：超管决定注入 IRQ 给 Guest
  pvm_inject_event(vcpu, vector, errcode, inject_type):
    pvcs = pvm_get_pvcs(pvm)
    pvcs->event_vector = vector | (STD << 8)
    if (errcode_valid):
        pvcs->event_errcode = errcode
    
    flags = pvcs->event_flags
    if (flags & IF) {
        // IF=1，可以立即交付
        pvcs->event_flags &= ~IF  // 关闭中断（类似 RFLAGS.IF=0）
        pvcs->event_flags |= IP   // 标记待处理
        // 设置 VM-enter 目标：下次进入 Guest 跳到事件入口
        tss_ex->enter_cr3 = smod_cr3  // 确保进 smod
    } else {
        // IF=0，推迟
        pvcs->event_flags |= IP   // 标记 IP，Guest 检查到后会 IRQ_WIN
    }

时刻 T3：VM-enter（如果 IF=1）
  Switcher 设置 RIP = smod_entry（= MSR_PVM_EVENT_ENTRY + 512）
  切换 CR3 = smod_cr3
  SYSRET → pvm_kernel_event_entry

时刻 T4：pvm_kernel_event_entry 执行
  1. PUSH_IRET_FRAME_FROM_PVCS user=0
     （构建被中断时的 pt_regs，包括被打断的 RIP/RFLAGS）
  2. PUSH_REGS_AND_HANDLE_EVENT handler=pvm_event
     → 读取 event_vector（xchgw 原子读清）
     → 调用 pvm_event(pt_regs, event_vector, errcode)

时刻 T5：pvm_event 处理（Guest C 代码）
  → 分发到对应的向量处理函数（如 do_IRQ、schedule）

时刻 T6：EVENT_RETURN（若返回用户态）
  pvm_restore_regs_and_return_to_usermode:
    1. POP_REGS（恢复通用寄存器）
    2. 设置 PVCS STD 位（保护 ERETU 窗口）
    3. 将 IRET 帧数据写入 PVCS（RIP、CS、RFLAGS、RSP、SS）
    pvm_retu_rip: syscall   → Switcher 识别为 EVENT_RETURN_USER

时刻 T7：Switcher 处理 EVENT_RETURN
  验证 RCX == retu_rip
  switch_flags SMOD→UMOD
  切换 CR3 = umod_cr3
  从 PVCS 恢复用户态上下文（RIP、RFLAGS）
  SYSRET → 用户态
```

### 6.2 页面错误（#PF）交付

#PF 是最复杂的异常，因为它携带故障地址（CR2）：

```
时刻 T1：Guest 访问未映射页
  → Shadow page walk 失败
  → KVM shadow MMU：kvm_mmu_page_fault()
  → 超管尝试填充影子页表（fix-up）
  → 若 GPA 无效（Guest 内核 bug）：注入 #PF 给 Guest

时刻 T2：超管注入 #PF
  pvcs->event_vector = PF_VECTOR | (STD << 8)
  pvcs->event_errcode = error_code（W/R/I/S 位）
  pvcs->cr2 = fault_address   // 关键！CR2 通过 PVCS 传递

时刻 T3-T5：同普通中断
  Guest #PF 处理函数从 PVCS 读取 CR2：
  // 在 entry_64_pvm.S 中，pvm_event 调用：
  read_pvcs_cr2() → PER_CPU_VAR(pvm_vcpu_struct + PVCS_cr2)
```

## 7. NMI 和 MCE 的特殊处理

NMI（Non-Maskable Interrupt）和 MCE（Machine Check Exception）是"异步异常"，它们：
1. 不受 IF 控制
2. 可以在任意时刻（包括 PVCS 写入窗口）交付
3. 可以与正在处理的标准事件同时存在

PVM 通过 `PVM_PVCS_EVENT_VECTOR_NMI` / `MCE` 位来处理：
- 这些位独立于 STD 位
- Guest 处理任何事件时，都应该检查 NMI/MCE 位
- `pvm_event` 在退出前检查并处理 NMI/MCE

```c
// pvm_event 的伪代码
void pvm_event(struct pt_regs *regs, u16 event_vector, u16 errcode)
{
    u8 vector = event_vector & 0xFF;
    u8 type = event_vector >> 8;

    // 首先处理异步异常
    if (type & PVM_PVCS_EVENT_VECTOR_NMI)
        handle_nmi(regs);
    if (type & PVM_PVCS_EVENT_VECTOR_MCE)
        handle_mce(regs);

    // 然后处理标准事件
    if (type & PVM_PVCS_EVENT_VECTOR_STD)
        handle_vector(regs, vector, errcode);
}
```

## 8. PVCS 访问的并发安全

PVCS 被两个实体并发访问：
- **超管**：在 VM-exit 后写入事件信息，在 VM-enter 前读取返回状态
- **Guest**：在 smod 中读取事件信息，在 ERETU 前写入返回状态

并发安全策略：
1. **event_flags 的 IF/IP 位**：使用 BTR/BTS（原子位操作）
2. **event_vector**：使用 XCHGW（原子 16-bit 交换）读取并清零
3. **cr2/errcode/user_cs 等字段**：超管仅在 Guest 不运行时（VM-exit 状态）或 IF=0 时写入，不与 Guest 并发
4. **STD 位**：作为"PVCS 繁忙"标志，ERETU 窗口期内防止超管再次写入这些字段

这种无锁设计依赖 x86 的 TSO（Total Store Order）内存模型和原子指令，避免了所有需要锁的场景。
