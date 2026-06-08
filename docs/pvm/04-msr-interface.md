# PVM MSR 接口：专用 MSR 与标准 MSR 影子

## 1. MSR 地址空间规划

PVM 在 KVM 保留的 MSR 范围内定义了自己的 MSR：

```c
// arch/x86/include/uapi/asm/pvm_para.h

/* PVM virtual MSRs: 0x4b564df0 - 0x4b564dff */
#define PVM_VIRTUAL_MSR_BASE    0x4b564df0
#define PVM_VIRTUAL_MSR_MAX_NR  15
#define PVM_VIRTUAL_MSR_MAX     (PVM_VIRTUAL_MSR_BASE + PVM_VIRTUAL_MSR_MAX_NR)
```

`0x4b564d` = ASCII "KVM"（0x4b=K，0x56=V，0x4d=M）。PVM 的 MSR 范围 `0x4b564df0-0x4b564dff` 在 KVM 保留范围（`0x4b564d00-0x4b564dff`）内，不与 KVM 标准 MSR 冲突。

当前实际使用的 PVM MSR（有两个已注释为 deprecated，RFC v2 将重新编号）：

```c
#define MSR_PVM_LINEAR_ADDRESS_RANGE  0x4b564df0  // 地址范围边界编码
#define MSR_PVM_VCPU_STRUCT           0x4b564df1  // PVCS GPA
// 0x4b564df2: deprecated (MSR_PVM_SWITCH_CR3)
// 0x4b564df3: deprecated (MSR_PVM_SUPERVISOR_RSP)
#define MSR_PVM_EVENT_ENTRY           0x4b564df4  // 事件处理入口 RIP
#define MSR_PVM_RETU_RIP              0x4b564df5  // EVENT_RETURN_USER 地址
// 0x4b564df6: reserved (MSR_PVM_RETS_RIP)
```

## 2. MSR_PVM_LINEAR_ADDRESS_RANGE（0x4b564df0）

### 2.1 作用

这个 MSR 定义了 PVM Guest 在宿主虚拟地址空间中的"排他地址范围"（exclusive address range）。宿主保证这段地址范围不被宿主自身使用，全部让给 PVM Guest。

地址范围通过 PML4/PML5 页目录索引来定义边界，因为这是最粗粒度的地址划分（PML4 每个条目覆盖 512GB，PML5 每个条目覆盖 256TB）。

### 2.2 编码格式（来自官方规范）

```
Bits 57-63:  全 1（7 bits）
Bits 48-56:  PML5_INDEX_END   （9 bits，bit 56 必须置 1，即最高位=1，范围 256-511）
Bits 41-47:  全 1（7 bits）
Bits 32-40:  PML5_INDEX_START （9 bits，bit 40 必须置 1，即最高位=1，范围 256-511）
Bits 25-31:  全 1（7 bits）
Bits 16-24:  PML4_INDEX_END   （9 bits，bit 24 必须置 1，即最高位=1，范围 256-511）
Bits 9-15:   全 1（7 bits）
Bits 0-8:    PML4_INDEX_START （9 bits，bit 8 必须置 1，即最高位=1，范围 256-511）
```

约束条件：
- `256 ≤ PML5_INDEX_START < PML5_INDEX_END < 511`
- `256 ≤ PML4_INDEX_START < PML4_INDEX_END < 511`
- 若 `underlying CR4.LA57` 未置位（4 级分页）：`PML5_INDEX_START = PML5_INDEX_END = 0x1FF`

**为什么索引必须 ≥ 256？**：x86-64 地址空间的上半部分（`0xFFFF000000000000` 以上）对应 PML4 索引 256-511（最高位为 1，符号扩展为负地址）。PVM 只在上半部分（内核地址空间）分配 Guest 区域，下半部分（用户态地址 `0x0000-0x7FFF...`）保持用于 Guest 用户态映射。

**为什么不包含 511？**：PML4[511] 是宿主内核的顶层页目录（`0xFFFFFF8000000000` 开始，宿主自己的内核映射）。PML5[511] 同理。保留 511 给宿主。

### 2.3 三个合法地址范围

规范定义了 PVM Guest VA 的三个合法来源：

```
1. 上半 L5 范围（LA57 模式）：
   [ (1UL << 48) * (0xFE00 | PML5_INDEX_START),
     (1UL << 48) * (0xFE00 | PML5_INDEX_END) )

2. 上半 L4 范围（4 级或 5 级分页的 PML4 范围）：
   [ (1UL << 39) * (0x1FFFE00 | PML4_INDEX_START),
     (1UL << 39) * (0x1FFFE00 | PML4_INDEX_END) )

3. 下半地址（用户态）：
   [ 0, (1UL << 47) )   （LA48）
   [ 0, (1UL << 56) )   （LA57）
```

Guest 内核（supervisor mode）使用范围 2 的地址；Guest 用户态使用范围 3；范围 1 在 LA57 模式下用于大型部署。

### 2.4 宿主如何利用这个 MSR

超管在 `host_mmu.c` 中的 `guest_address_space_init()` 预先分配好地址范围，然后在 Guest 设置 `MSR_PVM_LINEAR_ADDRESS_RANGE` 时验证其值在预分配范围内：

```c
// arch/x86/kvm/pvm/pvm.c（MSR 写处理）
static int pvm_set_msr_pvm(struct kvm_vcpu *vcpu, u32 index, u64 data)
{
    struct vcpu_pvm *pvm = to_pvm(vcpu);

    switch (index) {
    case MSR_PVM_LINEAR_ADDRESS_RANGE:
        // 验证值在模块全局预分配范围内
        if (!is_valid_linear_address_range(data))
            return 1;   // #GP
        pvm->msr_linear_address_range = data;
        // 解码并缓存到 l4_range_start/end 等字段
        pvm_decode_linear_address_range(pvm, data);
        // 重新建立 shadow paging 上下文
        kvm_mmu_reset_context(vcpu);
        break;
    // ...
    }
}
```

**关键约束**：Guest 设置的范围必须是超管预分配范围的**子集**，不能扩大到宿主已使用的地址范围。

### 2.5 MSR 初始值

CPU 复位时，`MSR_PVM_LINEAR_ADDRESS_RANGE` 初始化为**最宽范围**（覆盖所有合法范围）。Guest 可以缩小范围，但不能扩大到超过初始值。

### 2.6 与 BREAKPOINT 的交互

规范明确指出：超出合法地址范围的断点（`DR0-DR3`）在硬件 `DR7` 中对应的使能位会被超管清除，即断点不会在越界地址上触发。

## 3. MSR_PVM_VCPU_STRUCT（0x4b564df1）

### 3.1 作用

这个 MSR 设置 PVCS（PVM vCPU Struct）页的 Guest 物理地址（GPA）。

PVCS 是 Guest-Hypervisor 共享的通信内存，所有需要低延迟 Guest→Hypervisor 或 Hypervisor→Guest 通知的数据都通过这个结构传递，避免 hypercall 的开销（hypercall 需要 SYSCALL 陷入超管，而 PVCS 读写是内存操作）。

### 3.2 超管端实现

```c
// 当 Guest 写 MSR_PVM_VCPU_STRUCT 时
static int pvm_set_msr_pvm(struct kvm_vcpu *vcpu, u32 index, u64 data)
{
    struct vcpu_pvm *pvm = to_pvm(vcpu);
    switch (index) {
    case MSR_PVM_VCPU_STRUCT:
        // 释放旧 GPC 映射
        kvm_gfn_to_pfn_cache_destroy(vcpu->kvm, &pvm->pvcs_gpc);
        pvm->msr_vcpu_struct = data;

        if (data == 0) {
            // PVCS 未设置，置 PVCS_INVALID 标志，禁止直接切换
            pvm->switch_flags |= SWITCH_FLAGS_PVCS_INVALID;
            break;
        }

        // 建立 GFN→PFN 缓存，将 GPA 映射到内核虚拟地址
        if (kvm_gfn_to_pfn_cache_init(vcpu->kvm, &pvm->pvcs_gpc, vcpu,
                                       KVM_HOST_USES_PFN, data,
                                       sizeof(struct pvm_vcpu_struct))) {
            // 映射失败
            pvm->switch_flags |= SWITCH_FLAGS_PVCS_INVALID;
            break;
        }

        // 映射成功，清除 PVCS_INVALID 标志，允许直接切换
        pvm->switch_flags &= ~SWITCH_FLAGS_PVCS_INVALID;
        // 更新 tss_extra.pvcs 指针（下次 VM-enter 时生效）
        break;
    }
}
```

`gfn_to_pfn_cache`（GPC）是 KVM 提供的机制，将 Guest 物理地址（GFN：Guest Frame Number）固定映射到宿主内核虚拟地址（KVA）。一旦建立，超管和 Switcher 都可以直接通过内核虚拟地址访问 PVCS，无需每次都做 GPA→HPA 翻译。

### 3.3 PVCS_INVALID 标志的含义

当 `SWITCH_FLAGS_PVCS_INVALID` 置位时，`switch_flags` 不等于 `SWITCH_FLAGS_SMOD` 或 `SWITCH_FLAGS_UMOD`（因为 bit 10 也置位了），所以 Switcher 的"直接切换"检查失败，任何 SYSCALL 都返回超管。

这是一个安全机制：若 PVCS 未映射或映射失败，不允许直接切换，防止 Switcher 访问空指针 PVCS。

### 3.4 PVCS 的生命周期

```
1. Guest 分配一页物理内存
2. Guest 写 MSR_PVM_VCPU_STRUCT = GPA（通过 hypercall PVM_HC_WRMSR）
3. 超管建立 GPC 映射（GPA → KVA）
4. 超管更新 tss_extra.pvcs = KVA（在 VM-enter 前）
5. Switcher 通过 tss_extra.pvcs 直接读写 PVCS
6. Guest 通过 GS-relative 地址访问 PVCS（PER_CPU_VAR(pvm_vcpu_struct + offset)）
```

## 4. MSR_PVM_EVENT_ENTRY（0x4b564df4）

### 4.1 作用

这个 MSR 定义了 PVM Guest 的**向量事件处理入口**（类比 IDT 的功能）。

- `MSR_PVM_EVENT_ENTRY`：用户态（umod）向量事件的处理入口 RIP
- `MSR_PVM_EVENT_ENTRY + 512`：supervisor mode（smod）向量事件的处理入口 RIP

这两个入口地址必须在同一物理页内（由于 +512 的固定偏移），且用户态入口地址必须对齐到 64 字节边界（`pvm_user_event_entry` 的 `.align 64` 保证）。

### 4.2 两个入口的区别

```asm
// entry_64_pvm.S

// 用户态事件入口（MSR_PVM_EVENT_ENTRY 处）
    .align 64
SYM_CODE_START(pvm_user_event_entry)
    PUSH_IRET_FRAME_FROM_PVCS user=1   // 从 PVCS 构建用户态 IRET 帧
    PUSH_REGS_AND_HANDLE_EVENT handler=pvm_event
    ...

// supervisor mode 事件入口（MSR_PVM_EVENT_ENTRY + 512 处）
    .org pvm_user_event_entry+512, 0xcc  // 强制对齐到 +512 偏移
SYM_CODE_START(pvm_kernel_event_entry)
    PUSH_IRET_FRAME_FROM_PVCS user=0   // 从 PVCS 构建内核态 IRET 帧
    // 额外的 IRET fixup（处理 supervisor IRET 窗口期的递归事件）
    PUSH_REGS_AND_HANDLE_EVENT handler=pvm_event
    ...
```

两者的区别：
1. **栈选择**：用户态入口从 `pcpu_hot.X86_top_of_stack` 加载内核栈顶；supervisor 入口在当前 smod 栈上继续
2. **CS/SS 推入**：用户态使用 PVCS 中保存的 user_cs/user_ss；supervisor 使用 `__KERNEL_CS`/`__KERNEL_DS`
3. **IRET fixup**：supervisor 入口有特殊处理——在"supervisor IRET 使能窗口"（`iret_irq_enabled_start` 到 `iret_irq_enabled_end` 之间）发生事件时需要修正栈帧

### 4.3 超管如何使用这个 MSR

```c
// 准备进入 Guest 前，设置 tss_extra
static void pvm_prepare_switch_to_guest(struct kvm_vcpu *vcpu)
{
    struct vcpu_pvm *pvm = to_pvm(vcpu);
    struct tss_extra *tss_ex = this_cpu_tss_extra();

    // 设置 supervisor mode 入口 = event_entry + 512
    tss_ex->smod_entry = pvm->msr_event_entry + 512;

    // 设置 retu_rip（EVENT_RETURN_USER 的检测地址）
    tss_ex->retu_rip = pvm->msr_retu_rip_plus2;

    // 设置 smod gsbase
    tss_ex->smod_gsbase = pvm->msr_kernel_gs_base;

    // 设置 PVCS 指针
    tss_ex->pvcs = (struct pvm_vcpu_struct *)pvm->pvcs_gpc.khva;
    // ...
}
```

### 4.4 Guest 如何设置这个 MSR

Guest 内核在 PVM 初始化时：

```c
// Guest 侧（arch/x86/kernel/pvm.c 或类似文件）
wrmsrl(MSR_PVM_EVENT_ENTRY, (unsigned long)pvm_user_event_entry);
// 超管记录：msr_event_entry = &pvm_user_event_entry
// 超管计算：smod_entry = &pvm_user_event_entry + 512 = &pvm_kernel_event_entry
```

## 5. MSR_PVM_RETU_RIP（0x4b564df5）

### 5.1 作用

`MSR_PVM_RETU_RIP` 定义了 EVENT_RETURN_USER（从 smod 返回 umod）的 SYSCALL 指令地址。

这是 PVM 中最精妙的设计之一：EVENT_RETURN_USER 不是一个单独的合成指令，而是在特定地址（`MSR_PVM_RETU_RIP`）执行的 SYSCALL 指令。Switcher 通过检查 SYSCALL 的 RCX（返回地址 = `retu_rip + 2`，因为 SYSCALL 指令长 2 字节）来识别这是 EVENT_RETURN。

### 5.2 为什么用 SYSCALL 而非新指令

- 不需要 CPU 微码支持，纯软件实现
- x86 CPL3 下 SYSCALL 是合法指令（不触发 #UD）
- Switcher 拦截 SYSCALL 后，通过 RIP 匹配来区分"这是 EVENT_RETURN"还是"这是普通 hypercall"

```c
// 超管记录 msr_retu_rip_plus2（注意 +2）
pvm->msr_retu_rip_plus2 = data + 2;  // data = MSR_PVM_RETU_RIP 的写入值

// Switcher 检查：
// 当 smod 执行 SYSCALL 时，RCX = SYSCALL 之后的下一条指令地址
//                        = MSR_PVM_RETU_RIP + 2（SYSCALL 是 2 字节指令）
// 超管存储的是 +2 值，所以检查时直接 cmpq %rcx, retu_rip 即可
```

### 5.3 Guest 的 EVENT_RETURN_USER 实现

```asm
// entry_64_pvm.S
SYM_INNER_LABEL(pvm_restore_regs_and_return_to_usermode, SYM_L_GLOBAL)
    STACKLEAK_ERASE
    POP_REGS_AND_LOAD_IRET_FRAME_INTO_PVCS   // 将返回状态写入 PVCS
SYM_INNER_LABEL(pvm_retu_rip, SYM_L_GLOBAL)  // 这就是 MSR_PVM_RETU_RIP 的地址
    ANNOTATE_NOENDBR
    syscall                                   // Switcher 识别并处理
```

`pvm_retu_rip` 标签就是 `MSR_PVM_RETU_RIP` 应该设置的值。Guest 在初始化时：

```c
wrmsrl(MSR_PVM_RETU_RIP, (unsigned long)pvm_retu_rip);
```

## 6. 标准 MSR 的影子机制

除了 PVM 专用 MSR，超管还必须处理 Guest 对标准 x86 MSR 的读写请求（这些 MSR 在硬件层面由宿主内核控制）。

### 6.1 通过 hypercall 处理

Guest 通过 `PVM_HC_RDMSR`/`PVM_HC_WRMSR` hypercall 读写 MSR：

```c
// 超管 MSR 写处理
static int handle_hc_wrmsr(struct kvm_vcpu *vcpu, u32 index, u64 value)
{
    if (kvm_set_msr(vcpu, index, value))
        kvm_rax_write(vcpu, -EIO);   // 失败：RAX = -EIO
    else
        kvm_rax_write(vcpu, 0);      // 成功：RAX = 0
    return 1;
}

// 超管 MSR 读处理
static int handle_hc_rdmsr(struct kvm_vcpu *vcpu, u32 index)
{
    u64 value = 0;
    kvm_get_msr(vcpu, index, &value);
    kvm_rax_write(vcpu, value);       // 返回值在 RAX
    return 1;
}
```

`kvm_set_msr`/`kvm_get_msr` 是 KVM 核心提供的 MSR 仿真接口，超管调用它们模拟 Guest 对标准 MSR 的访问。

### 6.2 SYSCALL 相关 MSR 的特殊处理

`MSR_STAR`、`MSR_LSTAR`、`MSR_SYSCALL_MASK` 在 PVM 中需要特殊处理：

**MSR_STAR（段选择基准）**

```c
// Guest 写 MSR_STAR 时
case MSR_STAR:
    // 验证 Guest 期望的 CS/SS 与宿主一致（必须使用宿主的 __USER_CS/__USER_DS）
    if ((data >> 48) != user_cs32_by_msr(data))
        return 1;  // #GP
    // 验证 kernel CS RPL=0
    if (kernel_cs_by_msr(data) & 3)
        return 1;
    pvm->msr_star = data;
    // 宿主硬件 MSR_STAR 不变（由宿主内核控制，Switcher 需要它路由 SYSCALL）
    break;
```

**MSR_LSTAR（64-bit SYSCALL 入口）**

```c
// Guest 写 MSR_LSTAR 时
case MSR_LSTAR:
    pvm->msr_lstar = data;
    // 宿主硬件 MSR_LSTAR 指向 entry_SYSCALL_64_switcher，不改变
    // Guest 的 msr_lstar 值用于在 Switcher 中路由 Guest SYSCALL 到 Guest 内核处理函数
    // 等等，这里有点复杂——Guest 的 SYSCALL 处理函数是 Guest 内核的 sys_call_table
    // 路由是通过 smod_entry（= MSR_PVM_EVENT_ENTRY + 512）实现的，不是 msr_lstar
    // msr_lstar 主要用于 non_pvm_mode 的仿真
    break;
```

**MSR_SYSCALL_MASK（SYSCALL 时清零的 RFLAGS 位）**

PVM 规范说明 `MSR_SYSCALL_MASK` 被忽略——SYSCALL 进入 smod 时，RFLAGS 被设为 `SWITCH_ENTER_EFLAGS_FIXED`（固定值），不使用 `MSR_SYSCALL_MASK` 机制。

**MSR_KERNEL_GS_BASE（SWAPGS 目标）**

```c
// Guest 写 MSR_KERNEL_GS_BASE 时
case MSR_KERNEL_GS_BASE:
    pvm->msr_kernel_gs_base = data;
    // 同时更新 tss_extra.smod_gsbase（下次 VM-enter 时生效）
    if (pvm->loaded_cpu_state)
        wrmsrl(MSR_KERNEL_GS_BASE, data);
    break;
```

### 6.3 TSC 相关 MSR

规范说明 TSC 映射到底层 TSC（不稳定，受宿主调度影响），推荐使用 KVM 时钟（kvmclock）作为稳定时间源。

```c
// MSR_TSC_AUX（RDTSCP 的 CPU ID）
case MSR_TSC_AUX:
    pvm->msr_tsc_aux = data;
    if (pvm->loaded_cpu_state)
        wrmsrl(MSR_TSC_AUX, data);
    break;
```

### 6.4 Feature Control MSR

```c
// MSR_IA32_FEAT_CTL（Feature Control）
// 在 PVM 中，VMX 能力被禁用（Guest 不能使用 VMX）
pvm->msr_ia32_feature_control =
    FEAT_CTL_LOCKED;  // 仅锁定位，不允许 VMX

pvm->msr_ia32_feature_control_valid_bits =
    FEAT_CTL_LOCKED;  // 只有 LOCKED 位是有效的
```

这防止了 PVM Guest 尝试使用 VMX（若 Guest 尝试进行嵌套虚拟化）。

## 7. MSR 访问路径总结

Guest 访问 MSR 的完整路径：

```
1. Guest 执行 WRMSR（超特权指令）
   ↓
2. PVM Guest 没有 ring 0，不能直接 WRMSR
   ↓
3. Guest PV 代码调用 pvm_wrmsr(index, value)：
   - 若是允许直接访问的 MSR（如 MSR_FS_BASE）：WRMSR（FSGSBASE 允许 CPL3 访问）
   - 否则：PVM_HC_WRMSR hypercall
   ↓
4. Switcher 将 SYSCALL 路由到超管
   ↓
5. 超管 handle_hc_wrmsr()：
   - PVM 专用 MSR → pvm_set_msr_pvm()
   - 标准 MSR → kvm_set_msr()
   - 更新 vcpu_pvm 中的 MSR 影子字段
   - 更新 tss_extra（如果需要 Switcher 使用此 MSR）
```

允许 Guest CPL3 直接访问（无需 hypercall）的 MSR：
- `MSR_FS_BASE`：通过 FSGSBASE（`WRFSBASE`/`RDFSBASE`）
- `MSR_GS_BASE`：通过 FSGSBASE（`WRGSBASE`/`RDGSBASE`）
- `MSR_KERNEL_GS_BASE`：在 smod 中，通过 SWAPGS 语义（但 PVM Guest 不用 SWAPGS，而是通过 tss_extra 机制）
- `MSR_TSC`：通过 RDTSC/RDTSCP（CPL3 允许，若 CR4.TSD=0）

需要 hypercall 的 MSR：所有其他 MSR，包括 `MSR_LSTAR`、`MSR_STAR`、`MSR_EFER`、`MSR_CR3`（通过 `PVM_HC_LOAD_PGTBL` 而非 WRMSR）等。
