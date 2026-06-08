# Ring 压缩（CPL Compression）：PVM 的核心抽象

## 1. 为什么需要 Ring 压缩

标准 x86-64 Linux 内核运行在 CPL0（ring 0），这带来了以下特权：
- 可执行 LGDT、LIDT、LLDT、LTR、CLTS 等特权指令
- 可读写 CR0/CR2/CR3/CR4 控制寄存器
- 可读写 MSR（WRMSR/RDMSR）
- IDT 中的中断门在 ring 0 无须 DPL 检查
- SS/CS 段选择子可以选 ring 0 描述符

在虚拟化场景中，Guest 内核尝试执行上述操作时：
- **VMX 方案**：硬件捕获这些操作并触发 VM-exit，由超管模拟
- **PVM 方案**：Guest 内核根本不在 ring 0，这些操作本身就不会被硬件允许，但 PVM 通过**让 Guest 内核知道自己在 PVM 中**，将这些操作替换为 hypercall 或合法的 CPL3 操作

Ring 压缩的关键洞察是：**现代 Linux 内核中，大量"需要 ring 0"的操作其实是可以被替换为等价 PV 操作的**——这正是 Xen PV 多年来证明的。PVM 将这个想法推进一步：不仅替换特权操作，还将 Guest 内核彻底运行在 ring 3。

## 2. smod 与 umod：两个虚拟特权级

PVM 定义了两个虚拟特权级，名称来源于 x86 的 supervisor/user 概念：

### 2.1 smod（Supervisor Mode）

- **含义**：Guest 内核执行态（类比 ring 0）
- **硬件实际状态**：CPL=3，CS = `__USER_CS`（0x33），SS = `__USER_DS`（0x2b）
- **页表**：使用 `smod_cr3`（super mode CR3）——包含 Guest 内核页和 Guest 用户页，但用 PKS 限制对 Guest 用户页的访问
- **GS base**：指向 Guest 内核的 `per_cpu` 结构（通过 `msr_kernel_gs_base` 维护）
- **进入方式**：从 umod 执行 SYSCALL（或发生事件），Switcher 将 CR3 切换到 `smod_cr3` 并跳转到 `smod_entry`
- **退出方式**：执行 EVENT_RETURN（在 `MSR_PVM_RETU_RIP` 记录的地址执行特殊 SYSCALL），Switcher 切换回 `umod_cr3`

### 2.2 umod（User Mode）

- **含义**：Guest 用户态执行（类比 ring 3）
- **硬件实际状态**：CPL=3，CS = `__USER_CS`，SS = `__USER_DS`（与 smod 相同！）
- **页表**：使用 `umod_cr3`（user mode CR3）——只包含 Guest 用户页，不包含 Guest 内核页（保护 Guest 内核不被用户态窥探）
- **GS base**：指向 Guest 用户态 TLS 区域
- **进入方式**：从 smod 执行 EVENT_RETURN（通过 `MSR_PVM_RETU_RIP` 地址的 SYSCALL）

### 2.3 smod 与 umod 的区分机制

由于两者硬件 CPL 相同，如何区分？答案是 `switch_flags`：

```c
// arch/x86/include/asm/switcher.h
#define SWITCH_FLAGS_SMOD    _BITULL(0)   // 当前 CPU 在 PVM supervisor mode
#define SWITCH_FLAGS_UMOD    _BITULL(1)   // 当前 CPU 在 PVM user mode
```

`switch_flags` 保存在 `tss_extra` 结构中（per-CPU），Switcher 汇编代码在每次模式切换时更新这个字段。当发生 SYSCALL 时，Switcher 检查这个字段：
- 若 `SWITCH_FLAGS_UMOD` 置位 → 这是 Guest 用户态的系统调用，路由到 smod
- 若 `SWITCH_FLAGS_SMOD` 置位 → 这是 Guest 内核的 hypercall（或 EVENT_RETURN）
- 若两者均未置位 → 正常宿主 SYSCALL，走宿主路径

## 3. CS 段选择子的处理

这是 Ring 压缩中最微妙的细节之一。

### 3.1 Guest 内核期望的 CS

标准 Linux 内核期望在 ring 0 运行，因此编译时假设 CS = `__KERNEL_CS`（0x10，RPL=0）。PVM Guest 内核（打了 `CONFIG_PVM_GUEST` patch 后）被修改为接受 CS = `__USER_CS`（0x33，RPL=3）。

这个修改影响了内核中所有检查 CS 段值的地方，包括：
- `dump_regs`（打印 CS 时显示虚拟值而非硬件值）
- 段描述符缓存（GDT 中的 CS 描述符）
- IRET 相关代码

### 3.2 MSR_STAR 中的 CS 布局

在标准 Linux 中，`MSR_STAR`（SYSCALL/SYSRET 段选择基准）的高 32 位布局：

```
bits 63:48  SYSRET 段选择基准：用于 SYSRET 时加载 CS（+16 = __USER_CS）和 SS（+8 = __USER_DS）
bits 47:32  SYSCALL 段选择基准：用于 SYSCALL 时加载 CS（这个值本身 = __KERNEL_CS）和 SS（+8）
```

在 PVM 中，Guest 的 `MSR_STAR` 被超管拦截和仿真。当 Guest 写 `MSR_STAR` 时，超管记录 Guest 期望的值，但实际硬件 `MSR_STAR` 由宿主内核控制（因为 SYSCALL 入口必须到 Switcher，而不是 Guest）。

### 3.3 Supervisor mode 下禁止 32-bit 兼容模式

在 smod 下，PVM 不允许 Guest 切换到 32-bit 兼容模式（IA-32e compatibility submode）。原因：
- 32-bit 模式下的段特权检查机制不同，PVM 的 ring 压缩假设不成立
- 大量 smod/umod 切换代码依赖 64-bit SYSCALL/SYSRET 语义
- `non_pvm_mode` 会处理引导期间的 32-bit 阶段

若 Guest 在 smod 尝试执行 32-bit 兼容模式代码（通过 `lcall`/`ljmp` 切换 CS），PVM 将其视为 #GP。

## 4. 地址空间的 smod/umod 分隔

### 4.1 smod_cr3 的构成

```
smod_cr3 指向的页表：

PGD[0..127]       用户地址空间（Guest 用户内存）
  → 页表项设置 PKS protection key = PVM_PKS_USER_KEY
  → PKRS 寄存器在 smod 下设置该 key 为"禁止写，禁止读"（可选配置）
  → 等效于 SMAP 效果：smod 不能意外读写用户内存

PGD[256..287]     Guest 内核地址空间（PVM 排他区域）
  → 页表项设置 PKS protection key = PVM_PKS_KERNEL_KEY
  → PKRS 寄存器在 smod 下允许该 key 读写

PGD[288..511]     宿主内核地址空间（clone 自宿主页表，去掉 _PAGE_USER 位）
  → Guest 内核不可访问（无用户态 U/S 位），只有超管自身 CPL0 可访问
  → 等效于宿主内核对 Guest 不可见
```

### 4.2 umod_cr3 的构成

```
umod_cr3 指向的页表：

PGD[0..127]       用户地址空间（与 smod_cr3 相同的映射）
  → 页表项设置 PKS protection key = PVM_PKS_USER_KEY
  → PKRS 寄存器在 umod 下允许该 key 读写（用户可访问自己内存）

PGD[256..287]     不包含 Guest 内核映射（空 PGD 项）
  → umod 无法访问 Guest 内核页（内核代码、数据、栈）
  → 等效于内核地址空间隔离

PGD[288..511]     不包含宿主内核地址空间
```

这意味着：
- Guest 用户态（umod）**完全看不到** Guest 内核空间
- Guest 内核（smod）可以访问 Guest 用户空间，但 PKS 提供了类似 SMAP 的保护（`pvm_access_ok` 等函数需要临时允许访问）

### 4.3 地址空间切换时序

```
umod 状态（用户态正在运行系统调用）：
  SYSCALL 指令执行
    ↓
  硬件：RIP → MSR_LSTAR（= Switcher 入口）
         RSP → MSR_SP0（= 内核栈顶）
         RFLAGS.IF → 0（中断关闭）
    ↓
  Switcher（entry_SYSCALL_64_switcher）：
    1. swapgs → 切换到 tss_extra GS
    2. 读取 switch_flags，确认 UMOD 位
    3. 将 RIP、RSP 等保存到 PVCS 结构
    4. CR3 → smod_cr3（切换到 Guest 内核页表）
    5. 加载 smod_gsbase 到 GS
    6. swapgs → 切换到 Guest 内核 GS
    7. SYSRET → smod_entry（Guest 内核 SYSCALL 处理入口）
```

注意第 4 步（CR3 切换）是整个 Ring 压缩机制的核心动作——通过换页表，而非换 CPL，实现了特权级分隔。

## 5. 特权指令的 PV 替换

Guest 内核（打了 PVM Guest patch 的 Linux）必须替换所有 CPL0 特权指令。以下是主要替换关系：

### 5.1 CR3 写操作（页表切换）

```c
// 原始内核代码
write_cr3(new_pgd);

// PVM Guest 替换
pvm_load_pgtbl(new_pgd, flags);  // 展开为 SYSCALL（hypercall PVM_HC_LOAD_PGTBL）
```

实现：
```asm
// entry_64_pvm.S
pvm_hypercall:
    push %r11
    push %rcx
    mov %rcx, %r10
    syscall
    pop %rcx
    pop %r11
    ret
```

注意 R10/R11 的保存：SYSCALL 指令会破坏 RCX（保存 RIP）和 R11（保存 RFLAGS），PVM hypercall 包装在 SYSCALL 前保存这两个寄存器，并将 RCX 复制到 R10 传递给超管。

### 5.2 WRMSR/RDMSR

```c
// 原始内核代码
wrmsrl(MSR_LSTAR, handler_addr);

// PVM Guest 替换
pvm_wrmsr(MSR_LSTAR, handler_addr);  // hypercall PVM_HC_WRMSR
```

PVM 超管处理 WRMSR hypercall 时，将值记录到 `vcpu_pvm` 的 MSR 影子字段（如 `msr_lstar`）。在实际进入 Guest 前，超管设置好宿主硬件 MSR，使 Guest 的 SYSCALL 路由正确。

某些 MSR 被允许 Guest 直接读写（如 `MSR_FS_BASE`——因为 FSGSBASE 特性允许 CPL3 访问），其余 MSR 必须通过 hypercall。

### 5.3 CR0/CR4 读写

```c
// 原始
cr4_set_bits(X86_CR4_FSGSBASE);

// PVM Guest 替换（通过 paravirt ops hook）
// pvm_set_cr4() → 记录虚拟 CR4 值，不真正写 CR4
// （CR4 由宿主内核控制，Guest 的 CR4 是虚拟值）
```

PVM Guest 维护一个虚拟 CR4 值，每次 Guest 尝试读 CR4 时返回这个虚拟值。实际写 CR4 是禁止的（会触发 #GP），超管在必要时（如 PCID 管理）自行控制 CR4。

### 5.4 HLT 指令

```c
// 原始（等待中断）
asm("hlt");

// PVM Guest 替换（paravirt ops）
pvm_safe_halt();  // STI + hypercall PVM_HC_IRQ_HLT
```

`PVM_HC_IRQ_HLT` 告诉超管："我现在处于 idle，等待中断"。超管收到这个 hypercall 后，将 vCPU 挂起（调用 KVM 的 `kvm_vcpu_halt()`），直到有 IRQ 需要交付。

### 5.5 CLI/STI（中断禁用/使能）

这是 PVM 最精妙的设计之一。

```c
// 原始
local_irq_disable();  // CLI

// PVM Guest 替换（paravirt ops）
pvm_irq_disable();    // 清除 PVCS::event_flags 的 IF 位（bit 9）
```

```asm
// entry_64_pvm.S
pvm_irq_disable:
    btrq $9, PVCS_OFFSET_EVENT_FLAGS(%gs:PVM_VCPU_STRUCT_OFFSET)
    ret

pvm_irq_enable:
    btsq $9, PVCS_OFFSET_EVENT_FLAGS(%gs:PVM_VCPU_STRUCT_OFFSET)
    jc pvm_irq_enable_check  // 若 IP（interrupt pending）位已置位
    ret

pvm_irq_enable_check:
    // 有待处理中断，通知超管
    call pvm_hypercall_irq_win  // PVM_HC_IRQ_WIN
    ret
```

关键设计：**RFLAGS.IF 在 Guest 内核（smod）中始终为 1**（从宿主角度看），中断始终是开的。但 Guest 通过 `PVCS::event_flags` 的 IF 位（bit 9）控制是否接受中断。当 Guest 清除 IF 时，并不真正关中断，只是告诉超管"先不要交付中断"。

超管在交付中断之前检查 `PVCS::event_flags & IF`：
- 若 IF=1 → 立即交付（设置 PVCS::event_vector，清除 IF，设置 IP）
- 若 IF=0 → 设置 PVCS::event_flags 的 IP（interrupt pending，bit 8），等待 Guest 重新使能 IF

当 Guest 执行 `pvm_irq_enable` 发现 IP=1 时，发出 `PVM_HC_IRQ_WIN` hypercall，超管随即处理挂起的中断。

### 5.6 SWAPGS

SWAPGS 是 CPL0 特权指令（在 CPL3 执行会引发 #GP），用于切换 GS base（内核 per-CPU 指针与用户态 TLS 指针互换）。

PVM 的替换方案：

```c
// 进入内核（smod 入口）时：
// Switcher 已经通过 tss_extra.smod_gsbase 加载了正确的内核 GS
// Guest 内核不再需要 SWAPGS

// 返回用户态（umod 出口）时：
// Switcher 通过 PVCS::user_gsbase 加载用户 GS base
// Guest 内核同样不再需要 SWAPGS
```

GS base 的维护由 Switcher 全权负责，Guest 内核永远看到的都是正确的 GS（内核 per-CPU 或用户 TLS，取决于当前模式），无需自己执行 SWAPGS。

## 6. 段寄存器的虚拟化

### 6.1 硬件实际段值

在 PVM Guest 运行期间，段寄存器的实际硬件值：

```
CS:  0x33 (__USER_CS, DPL=3, 64-bit code segment)
SS:  0x2b (__USER_DS, DPL=3, data segment)
DS:  0x00（不使用，64-bit 模式下 DS/ES/GS/FS 基本不用，除 GS）
ES:  0x00
FS:  0x00（FS.base 通过 FSGSBASE 指令直接设置）
GS:  0x00（GS.base 通过 tss_extra 的 swapgs 机制维护）
```

### 6.2 Guest 的虚拟段视图

Guest 看到的段：

```
CS（smod）：虚拟值 = __KERNEL_CS (0x10) 或其他 ring 0 描述符
            实际值 = __USER_CS (0x33)
CS（umod）：虚拟值 = 用户态 CS，与宿主 __USER_CS 相同
            实际值 = __USER_CS (0x33)
SS：        与标准 Linux 相同
```

超管在 `pvm_get_segment(VCPU_SREG_CS)` 时返回虚拟 CS 值（`vcpu_pvm->segments[VCPU_SREG_CS]`），在 QEMU 读取寄存器时能看到正确的 ring 0 CS。

### 6.3 段描述符表（GDT/LDT）

- **GDT**：宿主控制。Guest 内核通过 `PVM_HC_LOAD_TLS` 请求超管在宿主 GDT 中设置 TLS 描述符（`GDT_ENTRY_TLS_MIN` 至 `GDT_ENTRY_TLS_MAX`）。
- **LDT**：Guest 请求加载 LDT 时，PVM 超管拒绝（返回 #GP），因为 LDT 需要 LLDT 特权指令。PVM 通过 GDT 模拟满足大多数场景。
- **IDT**：Guest 不使用真正的 IDT（需要 ring 0 才能安装中断门），改用 PVCS 事件机制。Guest 的 LIDT 调用被超管拦截，只记录虚拟 IDT 指针（用于 backtrace 等调试场景）。

## 7. RFLAGS 的虚拟化

### 7.1 真实 RFLAGS vs 虚拟 RFLAGS

| RFLAGS 位 | 真实值（硬件）| Guest 虚拟视图 | 说明 |
|-----------|-------------|--------------|------|
| IF (bit 9) | 始终为 1 | PVCS::event_flags bit 9 | 中断使能由 PVCS 虚拟 |
| TF (bit 8) | 0 或受超管控制 | Guest 设置值 | 单步调试 |
| IOPL (bits 12-13) | 0（CPL3 不允许 I/O）| 仿真为 0 | Guest 不允许直接 I/O |
| VM (bit 17) | 0 | 0 | V8086 模式不支持 |
| 其余算术标志 | 实际值 | 相同 | CF/PF/AF/ZF/SF/OF 等正常 |

### 7.2 POPF/PUSHF 的处理

Guest 执行 POPF（从栈弹出到 RFLAGS）时，若尝试修改 IF 位，在 CPL3 下硬件只修改 EFLAGS.IF 同级权限的位（CPL3 可以修改 IF）。但在 PVM 中，IF 的真实值无意义（始终为 1），POPF 后的 IF 被超管忽略；Guest 通过 `pvm_irq_enable/disable` 管理虚拟 IF。

若 Guest 用 POPF 关闭 TF（单步标志），超管需要同步更新 `switch_flags` 中的 `SWITCH_FLAGS_SINGLE_STEP` 位。

## 8. 64-bit 特有挑战

### 8.1 SYSCALL 指令的语义

在 64-bit 模式下，`SYSCALL` 指令：
1. RCX ← RIP + 2（返回地址）
2. R11 ← RFLAGS
3. CS ← MSR_STAR[47:32]（kernel CS 基准）
4. SS ← MSR_STAR[47:32] + 8
5. RIP ← MSR_LSTAR（64-bit mode SYSCALL target）
6. RFLAGS &= ~MSR_FMASK

在 PVM 中，`MSR_LSTAR` 指向 `entry_SYSCALL_64_switcher`（Switcher 入口），不是 Guest 的 SYSCALL 处理函数。Switcher 检查 `switch_flags` 决定路由：
- 若 `SWITCH_FLAGS_UMOD`：Guest 用户态 SYSCALL → 路由到 Guest 内核 SYSCALL 处理
- 若 `SWITCH_FLAGS_SMOD` + RIP == `retu_rip`：Guest 内核 EVENT_RETURN → 返回用户态
- 若 `SWITCH_FLAGS_SMOD`：Guest 内核 hypercall → 路由到 PVM hypercall 处理
- 若 neither：正常宿主 SYSCALL → 走宿主 `entry_SYSCALL_64` 后续逻辑

### 8.2 SYSRET 的使用

SYSRET 恢复 RIP ← RCX，RFLAGS ← R11，并将 CS/SS 切换回用户态。Switcher 在以下两个方向都使用 SYSRET：

**umod → smod**（相当于 VM-enter）：
```asm
mov smod_cr3, %rax
mov %rax, %cr3                      // 切换到 supervisor 页表
mov smod_gsbase, %rax
xor %edx, %edx
mov $MSR_GS_BASE, %ecx
wrmsrl                               // 加载 Guest 内核 GS base
swapgs                               // 激活新 GS base
mov pvcs->rip, %rcx                  // 恢复 RIP
mov pvcs->rflags, %r11               // 恢复 RFLAGS
sysretq                              // 跳回 smod_entry，CPL 仍为 3
```

**smod → umod**（相当于 VM-exit 到用户态）：
```asm
mov umod_cr3, %rax
mov %rax, %cr3                      // 切换到用户页表
mov pvcs->user_gsbase, %rax
xor %edx, %edx
mov $MSR_GS_BASE, %ecx
wrmsrl
swapgs
mov pvcs->rcx, %rcx                 // 用户态 RIP（SYSCALL 时保存的 RCX）
mov pvcs->r11, %r11                 // 用户态 RFLAGS
sysretq                              // 返回用户态
```

注意两个方向都是 SYSRET，CPL 始终为 3——换页表（CR3）是唯一区分 smod/umod 的硬件机制。

## 9. KPTI 与 Ring 压缩的交互

KPTI（Kernel Page-Table Isolation，Meltdown 缓解）需要内核/用户使用不同 CR3。PVM 已经在 smod/umod 之间切换 CR3，但 KPTI 的 CR3 切换是在 smod 内部（进入/离开 smod 时的额外翻转），与 PVM 的 smod/umod CR3 切换不同。

当前 PVM 实现**不支持 Guest KPTI**（`TODO: KPTI`），主要难点是：
1. PVM 的 CR3 管理已经很复杂（smod_cr3、umod_cr3、enter_cr3 三个值）
2. KPTI 在每个 smod 进入/退出时额外翻转一次 CR3（shadow pgtbl），需要在现有机制之上再加一层
3. 影子页表需要为每个 Guest CR3 维护两个影子（KPTI 内核侧和用户侧）

由于 PKS 替代 SMAP 已经提供了 smod 访问 umod 内存的保护，Guest KPTI 的缺失主要是 Meltdown 缓解问题（投机执行绕过 PKS 读取用户数据），而非正常路径安全问题。
