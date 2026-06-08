# PVM 硬件寄存器完整映射表

本文汇总了 PVM Guest 运行期间所有重要的 x86 寄存器/状态的三个层次：
1. **底层硬件实际值**（CPU 实际看到的）
2. **PVM 虚拟值**（Guest 看到的，超管维护）
3. **超管维护机制**（如何保持一致）

来源：`Documentation/virt/kvm/x86/pvm-spec.rst` Underlying States 章节 + 源码分析。

## 1. 控制寄存器

| 寄存器 | 底层硬件实际值 | PVM 虚拟（Guest 视图）| 维护机制 |
|--------|------------|---------------------|---------|
| **CPL** | **始终为 3**（通过 CS 选择子 RPL）| 0（smod）或 3（umod）| 通过 smod_cr3/umod_cr3 隔离而非 CPL |
| **CR0** | PE=PG=WP=ET=NE=AM=MP=1；CD=NW=EM=TS=0 | 相同（完全透传）| 宿主内核控制，Guest 不能修改 CR0 |
| **CR2** | 宿主 #PF 的故障地址 | 通过 PVCS::cr2 虚拟 | 超管在注入 #PF 时写 PVCS::cr2；Guest 读 PVCS::cr2 |
| **CR3** | smod_cr3 或 umod_cr3（影子页表的物理地址）| Guest 自己的 PGD（GPA）| 通过 PVM_HC_LOAD_PGTBL hypercall，超管建立影子映射 |
| **CR4** | FSGSBASE=1, PCID=1（宿主内核要求）；VME=PVI=SMAP=0 | VME/PVI/SMAP=0；PAE/FSGSBASE=1；PCID=1（强制）；SMEP=EFER.NXE | 宿主内核控制；Guest 虚拟 CR4 由超管维护；LA57 通过 LOAD_PGTBL flags 切换 |

### 1.1 CR0 详细分析

```
底层 CR0：PE=PG=WP=ET=NE=AM=MP=1，CD=NW=EM=TS=0

PE（Protection Enable）=1：始终启用保护模式
PG（Paging）=1：始终启用分页
WP（Write Protect）=1：防止 ring 0 写只读页（但 PVM Guest 是 ring 3，此位无额外效果）
ET（Extension Type）=1：固定位
NE（Numeric Error）=1：x87 错误处理方式
AM（Alignment Mask）=1：允许对齐检查（但 RFLAGS.AC=0 时不触发）
MP（Monitor Coprocessor）=1：WAIT 指令检查 TS 位

EM（Emulation）=0：无 FPU 仿真
TS（Task Switched）=0：超管清除 TS，防止 Guest 因 FPU 不可用（#NM）而困惑
CD（Cache Disable）=0：缓存启用
NW（Not Write-through）=0：写回缓存
```

### 1.2 CR4 详细分析

```
PVM 规范中 Guest 视图：
  VME=PVI=0：虚拟 8086 中断不支持（PVM 不支持 VM 模式）
  SMAP=0：SMAP 对 CPL3 无意义（由 PKS/PKRU 替代）
  SMEP=EFER.NXE：Supervisor 不执行保护由 NX 位提供
  PAE=1：始终启用物理地址扩展（现代 64-bit 内核要求）
  FSGSBASE=1：允许 CPL3 通过 RDFSBASE/WRFSBASE 等访问 FS/GS 基址
  PCID=1：启用 PCID（即使底层 CR4.PCID 未启用，PVM 也虚拟为 1）
  UMIP/PKE/OSXSAVE/OSFXSR/OSXMMEXCPT：映射到宿主实际值
```

PCID 的特殊处理：Guest 认为 PCID=1（可以使用 PCID），但底层可能没有。PVM 在影子页表中通过 ASID 机制模拟 PCID 语义，Guest 可以使用 CR3_NOFLUSH 和 PCID 值，超管映射到底层的 ASID。

## 2. EFER（Extended Feature Enable Register）

| 字段 | 底层实际值 | PVM 虚拟值 | 说明 |
|------|----------|----------|------|
| SCE | 1 | 1 | SYSCALL/SYSRET 使能（宿主需要）|
| LME | 1 | 1 | Long Mode 使能 |
| LMA | 1 | 1 | Long Mode 激活 |
| NXE | 底层值 | 透传（底层相同）| NX 页保护使能 |

## 3. 段寄存器

### 3.1 CS（代码段）

| 状态 | 底层值 | PVM 虚拟值 |
|------|-------|----------|
| smod（supervisor）| `__USER_CS` (0x33)，RPL=3 | 由 MSR_STAR 派生，RPL=0（如 `__KERNEL_CS` = 0x10）|
| umod（user）| `__USER_CS` (0x33) | `__USER_CS` (0x33)（透传）|
| non_pvm_mode | 仿真 | 由仿真器维护 |

CS 的"虚拟化"：当 QEMU 通过 `KVM_GET_SREGS` ioctl 读取 CS 时，超管的 `pvm_get_segment(VCPU_SREG_CS)` 返回 `vcpu_pvm->segments[VCPU_SREG_CS]`，其中存储了虚拟 CS（如 0x10）。实际硬件 CS 是 0x33。

### 3.2 SS（栈段）

| 状态 | 底层值 | PVM 虚拟值 |
|------|-------|----------|
| smod | `__USER_DS` (0x2b)，RPL=3 | 由 MSR_STAR 派生，RPL=0（如 `__KERNEL_DS` = 0x18）|
| umod | `__USER_DS` (0x2b) | `__USER_DS` (0x2b)（透传）|

### 3.3 DS/ES/FS/GS

| 段 | 底层值 | PVM 虚拟值 | 说明 |
|----|-------|----------|------|
| DS | NULL (0x00) 或 `__USER_DS` | 映射底层 | 64-bit 模式 DS 不使用基址 |
| ES | NULL (0x00) 或 `__USER_DS` | 映射底层 | 同 DS |
| FS | NULL (0x00) | 映射底层；FS.base 通过 WRFSBASE 设置 | 线程本地存储 |
| GS（smod）| NULL (0x00) | GS.base = Guest 内核 per-CPU 指针 | 通过 tss_extra.smod_gsbase 设置 |
| GS（umod）| NULL (0x00) | GS.base = 用户态 TLS 指针（PVCS::user_gsbase）| 通过 wrgsbase 设置 |

GS base 是 PVM 中最精心管理的状态：
```
Switcher 路径（entry_SYSCALL_64_switcher）：
  umod→smod：
    rdgsbase %rcx               // 读取 umod GS base
    movq %rcx, PVCS_user_gsbase // 保存到 PVCS
    wrgsbase smod_gsbase        // 写入 smod GS base（Guest 内核 per-CPU）
  smod→umod：
    movq PVCS_user_gsbase, %rcx
    wrgsbase %rcx               // 恢复 umod GS base

tss_extra.smod_gsbase = msr_kernel_gs_base（Guest 的 MSR_KERNEL_GS_BASE 值）
```

## 4. RFLAGS

| 位 | 底层硬件值 | PVM 虚拟值 | 维护机制 |
|----|----------|----------|---------|
| IF（bit 9）| **始终为 1** | PVCS::event_flags bit 9 | pvm_irq_enable/disable；超管在交付事件时检查虚拟 IF |
| TF（bit 8）| 超管控制 | Guest 期望值 | SWITCH_FLAGS_SINGLE_STEP 同步 |
| IOPL（bits 12-13）| 0 | 0 | Guest 不允许 I/O（CPL3，IOPL=0）|
| VM（bit 17）| 0 | 0 | V8086 不支持 |
| VIF/VIP（bits 19-20）| 0 | 0 | 虚拟中断标志，PVM 不使用 |
| FIXED（bit 1）| 1 | 1 | 始终为 1（x86 要求）|
| AC（bit 18）| 透传 | 透传 | 对齐检查（通常不设置）|
| 算术标志（CF/PF/AF/ZF/SF/OF/DF）| Guest 值 | 透传 | 由 CPU 直接执行，无需虚拟化 |
| RF（bit 16）| 控制调试 | 透传 | 单步调试使用 |

RFLAGS 在 VM-enter/exit 时的处理：

```c
// VM-enter 时 RFLAGS 的清理（SWITCH_ENTER_EFLAGS_ALLOWED）
// 允许的位：FIXED、IF、TF、RF、AC、OF、DF、SF、ZF、AF、PF、CF、ID、NT
// 不允许的位：IOPL、VM、VIF、VIP
// 强制置位：FIXED、IF

// 实际上 SYSRET 时 R11 被设为 SWITCH_ENTER_EFLAGS_FIXED（只有 IF=1, FIXED=1）
// 这意味着进入 Guest 时，RFLAGS 几乎为零（除了 IF 和 FIXED）
// Guest 必须自己恢复算术标志（从 pt_regs）
```

## 5. 描述符表寄存器

| 寄存器 | 底层实际值 | PVM 虚拟值 | 维护机制 |
|--------|----------|----------|---------|
| **GDTR** | 宿主 GDT（宿主内核控制）| 虚拟 GDT（只读，由超管维护）| Guest LGDT → 记录虚拟值；实际 GDT = 宿主 GDT + Guest TLS（通过 PVM_HC_LOAD_TLS）|
| **IDTR** | 宿主 IDT（宿主内核控制）| 虚拟 IDT（无效，事件由 PVCS 机制处理）| Guest LIDT → 记录虚拟值（仅供调试用）|
| **TR**（Task Register）| 宿主 TSS（含 tss_extra）| 虚拟 TR（无效，不使用宿主 TSS 切换栈机制）| 不使用 TSS 的 IST/sp0 切换（用 PVCS 机制替代）|
| **LDTR**（Local Descriptor Table）| NULL | NULL | 不支持 LDT |

### 5.1 GDT 的特殊情况

PVM 规范说明虚拟 GDT 包含以下条目：

```
1. 仿真的 supervisor mode CS/SS 描述符
   （虚拟 __KERNEL_CS, __KERNEL_DS，DPL=0）

2. 底层 GDT 中 DPL=3 的条目（用户态段描述符）：
   - __USER32_CS（兼容 32-bit 代码段）
   - __USER_CS（64-bit 用户代码段）
   - __USER_DS（用户数据段）
   - 宿主 CPUNODE 描述符（包含 segment limit = CPU node ID）
   - 通过 PVM_HC_LOAD_TLS 修改的 TLS 描述符

3. Guest 不能添加任何 DPL < 3 的描述符
   （会被超管拒绝）
```

## 6. MSR 寄存器

| MSR | 底层硬件值 | PVM 虚拟值 | 维护机制 |
|-----|----------|----------|---------|
| MSR_LSTAR | `entry_SYSCALL_64_switcher`（Switcher 入口）| Guest 的 SYSCALL 处理函数（vcpu_pvm::msr_lstar）| Guest WRMSR 通过 PVM_HC_WRMSR hypercall 记录；实际不改变底层 LSTAR |
| MSR_STAR | 宿主内核的段选择基准 | Guest 的 MSR_STAR（vcpu_pvm::msr_star）| 同上；验证 Guest 的 CS/SS 派生值与宿主一致 |
| MSR_SYSCALL_MASK | 宿主值 | 忽略（PVM 不使用）| SYSCALL 时 RFLAGS 由 SWITCH_ENTER_EFLAGS_FIXED 设置 |
| MSR_GS_BASE | smod：Guest 内核 per-CPU 指针；umod：Guest TLS | 透传当前 GS base | Switcher 通过 rdgsbase/wrgsbase 管理 |
| MSR_KERNEL_GS_BASE | smod：不确定（被 swapgs 影响）| vcpu_pvm::msr_kernel_gs_base（Guest 的内核 GS base）| swapgs 时切换；超管在 VM-enter 前写底层 MSR_KERNEL_GS_BASE |
| MSR_FS_BASE | 线程 FS base | 透传（直接 WRFSBASE 可访问）| FSGSBASE 特性允许 CPL3 直接操作 |
| MSR_TSC_AUX | Guest CPU ID（by 超管设置）| 透传 | 超管 VM-enter 前写 MSR_TSC_AUX |
| MSR_IA32_PKRS | 超管设置的值（smod 的 PKRS）| 无（Guest 不能直接访问，需 hypercall）| 超管在 VM-enter 前通过 WRMSR（CPL0）设置 |
| MSR_PVM_* | N/A（PVM 专用，宿主内核不用）| Guest 读写通过 PVM_HC_RDMSR/WRMSR | vcpu_pvm::msr_vcpu_struct/event_entry 等字段 |
| MSR_IA32_DEBUGCTLMSR | 宿主值（VM-enter 前保存 / VM-exit 后恢复）| Guest 调试控制值 | vcpu_pvm::host_debugctlmsr 保存宿主值 |

## 7. 调试寄存器

| 寄存器 | 底层值 | PVM 虚拟值 | 维护机制 |
|--------|-------|----------|---------|
| DR0-DR3 | 由超管设置，超出合法 VA 范围的地址对应 DR7 使能位被清零 | Guest 设置的断点地址 | 超管验证地址在合法范围内，否则清除对应 DR7 使能位 |
| DR6 | 0xFFFF0FF0（复位值），#DB 触发后更新 | 透传（通过 exit_dr6 传递给 Guest）| VM-exit 时超管读取底层 DR6 保存到 vcpu_pvm::exit_dr6 |
| DR7 | 超管控制，`DR7_GD=0`（禁止调试控制寄存器保护）| Guest 值（经过超管验证）| vcpu_pvm::guest_dr7 保存 Guest 期望值 |

DR7_GD=0：禁用 GD（General Detect）位，防止 Guest 尝试检测底层调试寄存器访问模式。

## 8. 性能监控 MSR（PMU）

PVM 当前 PMU 支持是 **stub**（仅打印警告，返回假数据）。Guest 执行的 PERF 相关操作（RDPMC、PMU MSR 读写）被拦截但不实际执行：

```c
// pvm.c（PMU stub）
static int pvm_pmu_get_msr(struct kvm_vcpu *vcpu, struct msr_data *msr_info)
{
    pr_warn_once("PVM: PMU not supported\n");
    return 1;  // 返回 0（成功，数据为 0）
}
```

这是 RFC v1 的已知限制，PMU 完整支持将在后续版本实现。

## 9. x87 FPU / SSE / AVX 状态

| 状态 | 处理方式 |
|------|---------|
| x87 FPU 状态 | 通过 XSAVE/XRSTOR（或 FXSAVE/FXRSTOR）在 VM-enter/exit 时保存恢复（与 KVM VMX 相同机制）|
| SSE 状态（XMM 寄存器）| 同上 |
| AVX 状态（YMM/ZMM）| 通过 XSAVE 扩展状态保存（若 XSAVES 支持）|
| CR0.TS（Task Switched）| 超管始终清除 CR0.TS（TS=0），防止 Guest FPU 操作触发 #NM |

注意：PVM Guest 不支持 `XSAVES`/`XRSTORS` 指令（需要 ring 0），所以 Guest 只能使用 `XSAVE`/`XRSTOR`（用户可访问的变体）。超管仍使用 XSAVES 保存/恢复完整 XSAVE 状态（在宿主 CPL0 下），但不向 Guest 暴露 XSAVES 能力。

## 10. 完整状态转储（VM-exit 时）

当 Guest 触发超管处理的 VM-exit 时，超管保存以下状态到 `vcpu_pvm`：

```c
// pvm_vcpu_run 的返回路径（switcher_return_from_guest 后）
struct pt_regs *guest_regs = switcher_enter_guest();  // 返回 Guest pt_regs

// 1. 通用寄存器：从 pt_regs 读取
//    RAX, RBX, RCX, RDX, RSI, RDI, RSP, RBP, R8-R15
kvm_rax_write(vcpu, guest_regs->ax);
// ... 等等

// 2. 段寄存器：通过 pvm_save_host 保存/恢复
// （大部分段值从 vcpu_pvm->segments[] 中维护）

// 3. 控制寄存器：通过 shadow 值维护

// 4. PVCS：内存中直接访问（pvm_get_pvcs()）
```

超管不需要像 VMX 一样从 VMCS 读取所有寄存器——Guest 寄存器在 pt_regs 帧中（由 Switcher 保存），控制寄存器通过影子值维护，段寄存器通过 vcpu_pvm->segments[] 维护。这比 VMX 的 VMCS 读写机制更轻量。
