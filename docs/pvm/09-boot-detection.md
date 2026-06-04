# PVM 引导探测、合成指令与 non_pvm_mode

## 1. 为什么 Guest 需要探测 PVM

标准 KVM Guest 通过 CPUID 自报（`KVM_CPUID_SIGNATURE`）检测超管类型。PVM Guest 也需要类似机制，但有一个关键困难：

**PVM Guest 的 CPUID 可能不被拦截**。在 PVM 中，Guest 以 CPL3 运行，CPUID 指令在 CPL3 下是**合法的无特权指令**，CPU 会直接执行它返回宿主 CPU 的 CPUID 结果，超管根本没有机会拦截（除非超管显式配置 CPUID 陷入，但这有性能开销）。

```c
// pvm.c 模块参数
static bool __read_mostly enable_cpuid_intercept = 0;
module_param_named(cpuid_intercept, enable_cpuid_intercept, bool, 0444);
```

默认 `enable_cpuid_intercept = false`（不拦截 CPUID），这意味着 Guest 执行 CPUID 直接返回宿主 CPU 的 CPUID，无法通过此机制识别 PVM 超管。

## 2. PVM 合成 CPUID（Synthetic CPUID）

### 2.1 设计原理

PVM 使用一个两条指令的合成序列来触发可拦截的 CPUID：

```c
// arch/x86/include/uapi/asm/pvm_para.h

/*
 * The CPUID instruction in PVM guest might not be trapped and emulated,
 * so PVM guest should use the following two instructions instead:
 * "invlpg 0xffffffffff4d5650; cpuid;"
 *
 * PVM_SYNTHETIC_CPUID is supposed to not trigger any trap in the real or any
 * paravirtual x86 kernel mode and is also guaranteed to trigger a trap in the
 * underlying hardware user mode for the hypervisor emulating it. The
 * hypervisor emulates both of the basic instructions, while the INVLPG is
 * often emulated as an NOP since 0xffffffffff4d5650 is normally out of the
 * allowed linear address ranges.
 */
#define PVM_SYNTHETIC_CPUID         0x0f,0x01,0x3c,0x25,0x50, \
                                    0x56,0x4d,0xff,0x0f,0xa2
#define PVM_SYNTHETIC_CPUID_ADDRESS 0xffffffffff4d5650
```

合成 CPUID 序列：
```asm
invlpg  0xffffffffff4d5650    // 5 字节：0f 01 3c 25 50 56 4d ff
cpuid                          // 2 字节：0f a2
```

### 2.2 为什么这个序列有效

**在正常 Linux 内核 ring 0 执行**（宿主或标准 KVM Guest）：
- `invlpg 0xffffffffff4d5650`：`0xffffffffff4d5650` 是一个内核地址（负数），在 ring 0 下 INVLPG 是合法的（即使地址未映射，INVLPG 也不产生 #PF）
- `cpuid`：正常执行，返回宿主 CPUID

**在 PVM Guest smod 执行（CPL3 底层）**：
- `invlpg 0xffffffffff4d5650`：这个地址（`0xffffffffff4d5650`）落在宿主内核地址空间，**超出 PVM 允许的 `MSR_PVM_LINEAR_ADDRESS_RANGE`**（Guest 地址范围是上半部分的一个子集，不包含顶层地址）
- CPL3 下 INVLPG 对非合法地址触发 **#GP（General Protection Fault）**（注意：不是 #PF，因为这是非规范地址的 INVLPG）
- 超管拦截 #GP，识别指令序列，仿真为合成 CPUID
- 然后继续执行 CPUID（由超管仿真返回 PVM 特征值）

**`0xffffffffff4d5650` 的选择**：
- `ff4d5650`（反向读为 `PVM`？实际上 `4d` = 'M'，`56` = 'V'，`4d` 重复... 实际是 `0xffffffffff4d5650` 看作标识地址）
- 这个地址在非规范范围（48-bit VA：有效范围 `0x0000...` 或 `0xffff8000...`，而 `0xffffffffff4d5650` 的 bit 47 是 1 但 bit 48-63 不全是 1，是非规范地址）
- 非规范地址的 INVLPG 在 CPL3 下保证触发 #GP，在 ring 0 下某些 CPU 可能忽略（NOP）

### 2.3 超管的合成 CPUID 处理

```c
// arch/x86/kvm/pvm/pvm.c
static int handle_synthetic_instruction_pvm_cpuid(struct kvm_vcpu *vcpu)
{
    // 向 Guest 报告 PVM 特征（通过修改 vCPU 的 RAX/RBX/RCX/RDX）
    // 类似 KVM 的 kvm_emulate_cpuid()

    u32 eax = kvm_rax_read(vcpu);   // CPUID leaf
    u32 ecx = kvm_rcx_read(vcpu);   // CPUID sub-leaf

    // 处理 PVM 专用 CPUID leaf
    switch (eax) {
    case KVM_CPUID_SIGNATURE:       // 0x40000000
        // 返回 "KVMPVMKVMPVM" 签名（类似 "KVMKVMKVM\0" 但是 PVM 版）
        kvm_rax_write(vcpu, KVM_CPUID_SIGNATURE + 2);  // 最大叶子
        kvm_rbx_write(vcpu, 0x4d564b50);  // "KVMP"
        kvm_rcx_write(vcpu, 0x4d564b56);  // "VMKV"
        kvm_rdx_write(vcpu, 0x0000004d);  // "M\0"
        break;
    case KVM_CPUID_FEATURES:        // 0x40000001
        // 返回 PVM 能力位
        // X86_FEATURE_KVM_PVM_GUEST = bit 23（word 8）
        kvm_rax_write(vcpu, /* PVM 特性掩码 */);
        break;
    }

    kvm_rip_write(vcpu, kvm_rip_read(vcpu) + 2);  // 跳过 CPUID 指令
    return 1;
}
```

**两条指令的分离处理**：
1. `invlpg 0xffffffffff4d5650` → 触发 #GP → 超管识别 → 标记"下一条 CPUID 是合成的"
2. `cpuid` → 超管检查标记 → 返回 PVM 特征值

（实际实现可能是一次性识别 `invlpg + cpuid` 的 10 字节序列）

## 3. Guest 引导探测序列

Guest 内核（打了 `CONFIG_PVM_GUEST` patch 的 Linux）在早期引导时检测 PVM 环境：

### 3.1 第一阶段：基本 CPL 检测

```c
// arch/x86/kernel/pvm.c（Guest 侧代码）
static bool pvm_detect(void)
{
    // 步骤 1：检查进入内核时的 EFLAGS.IF
    // 在 PVM 中，RFLAGS.IF 始终为 1（由超管保证）
    if (!(native_save_fl() & X86_EFLAGS_IF))
        return false;  // IF=0 → 不在 PVM

    // 步骤 2：检查 CS 的 RPL
    // 在 PVM 中，CS = __USER_CS（RPL=3）
    u16 cs;
    asm("movw %%cs, %0" : "=r"(cs));
    if ((cs & 3) != 3)
        return false;  // CPL 不是 3 → 不在 PVM

    // 步骤 3：执行合成 CPUID
    u32 eax, ebx, ecx, edx;
    asm volatile(
        ".byte 0x0f,0x01,0x3c,0x25,0x50,0x56,0x4d,0xff\n\t"  // invlpg addr
        "cpuid\n\t"
        : "=a"(eax), "=b"(ebx), "=c"(ecx), "=d"(edx)
        : "0"(KVM_CPUID_SIGNATURE)
    );

    // 步骤 4：验证 KVM PVM 签名
    if (ebx != 0x4d564b50 || ecx != 0x4d564b56 || edx != 0x0000004d)
        return false;  // 签名不匹配 → 不是 PVM

    return true;
}
```

### 3.2 第二阶段：PVM MSR 初始化

一旦确认在 PVM 中运行：

```c
static void pvm_init(void)
{
    // 1. 查询 PVM MSR 地址范围
    u64 linear_range;
    rdmsrl(MSR_PVM_LINEAR_ADDRESS_RANGE, linear_range);

    // 2. 分配并注册 PVCS 页
    struct pvm_vcpu_struct *pvcs = alloc_page(GFP_KERNEL);
    wrmsrl(MSR_PVM_VCPU_STRUCT, __pa(pvcs));  // 通过 PVM_HC_WRMSR hypercall

    // 3. 注册事件处理入口
    wrmsrl(MSR_PVM_EVENT_ENTRY, (u64)pvm_user_event_entry);  // 通过 PVM_HC_WRMSR

    // 4. 注册 EVENT_RETURN_USER 地址
    wrmsrl(MSR_PVM_RETU_RIP, (u64)pvm_retu_rip);  // 通过 PVM_HC_WRMSR

    // 5. 设置 paravirt ops
    pv_ops.mmu.flush_tlb_user = pvm_flush_tlb_user;
    pv_ops.mmu.flush_tlb_kernel = pvm_flush_tlb_kernel;
    pv_ops.irq.save_fl = pvm_save_fl;
    pv_ops.irq.irq_disable = pvm_irq_disable;
    pv_ops.irq.irq_enable = pvm_irq_enable;
    pv_ops.irq.halt = pvm_halt;
    // ... 其他 pv_ops
}
```

### 3.3 KVM PVM Guest CPU 特性位

```c
// arch/x86/include/asm/cpufeatures.h
#define X86_FEATURE_KVM_PVM_GUEST   (8*32+23)  /* PVM Guest KVM 功能 */
#define X86_FEATURE_PV_GUEST        (8*32+24)  /* PV Guest（Xen PV 和 PVM 共用）*/
```

超管通过 CPUID leaf `0x40000001` 向 Guest 报告这些特性，Guest 通过 `check_cpuid_features()` 检查并设置对应的 `X86_FEATURE_*` 位。

## 4. non_pvm_mode：预引导仿真

### 4.1 什么是 non_pvm_mode

在 Guest 完成 PVM 初始化之前，Guest 可能处于以下状态：
1. **实模式（Real Mode）**：BIOS/UEFI 固件引导阶段
2. **32-bit 保护模式**：引导加载器（GRUB 等）阶段
3. **未分页模式**：内核 setup 代码（`arch/x86/boot/`）阶段
4. **64-bit 内核模式，但分页未启用 PVM 机制**：内核早期初始化

在这些阶段，PVM 的 CPL3 + shadow paging 机制无法工作，超管必须用纯软件仿真处理所有指令。

```c
// arch/x86/kvm/pvm/pvm.h
struct vcpu_pvm {
    bool non_pvm_mode;  // true：处于预引导仿真模式
    // ...
};
```

### 4.2 non_pvm_mode 的进入和退出

**进入 non_pvm_mode**：vCPU 复位时。

```c
static void pvm_vcpu_reset(struct kvm_vcpu *vcpu, bool init_event)
{
    struct vcpu_pvm *pvm = to_pvm(vcpu);

    // vCPU 复位到类 x86 标准状态
    // CS:RIP = 0xF000:FFF0（标准复位向量）
    // CR0 = 0（没有分页、没有保护模式）

    pvm->non_pvm_mode = true;  // 进入仿真模式
    // ...
}
```

**退出 non_pvm_mode**（过渡到 PVM 模式）：

```c
// 超管检测 Guest 何时进入 PVM 模式
// 条件：Guest 设置了 CR4.FSGSBASE，CR0.PG，EFER.LME，
//        且 MSR_PVM_VCPU_STRUCT 已配置，且 CS 是 __USER_CS

static bool should_exit_non_pvm_mode(struct vcpu_pvm *pvm)
{
    // 检查 Guest 是否已满足 PVM 模式的所有前提
    return pvm->vcpu.arch.cr4 & X86_CR4_FSGSBASE &&
           pvm->vcpu.arch.cr0 & X86_CR0_PG &&
           pvm->vcpu.arch.efer & EFER_LME &&
           pvm->msr_vcpu_struct != 0;
}
```

实际上退出 non_pvm_mode 不是一个明确的时刻，而是当超管拦截到 Guest 试图进入 PVM 模式时（比如 WRMSR MSR_PVM_VCPU_STRUCT），超管清除 `non_pvm_mode` 标志，从那时起使用 PVM 机制。

### 4.3 non_pvm_mode 的性能

non_pvm_mode 本质上是 **QEMU/KVM 的 CPU 软件仿真**——每条指令都需要超管解码并模拟。这非常慢（约为原生速度的 0.1-1%），但 non_pvm_mode 只在：
- BIOS/UEFI 运行期间（通常 0.5-2 秒）
- 引导加载器运行期间（<1 秒）

之后就永久进入 PVM 模式，这点开销是可以接受的。

### 4.4 non_pvm_mode 下的特权指令处理

在 non_pvm_mode，超管需要仿真所有 x86 特权指令：

```c
// non_pvm_mode 下的指令仿真（通过 KVM 的 x86_emulate_insn）
static int pvm_handle_exit(struct kvm_vcpu *vcpu, fastpath_t exit_fastpath)
{
    struct vcpu_pvm *pvm = to_pvm(vcpu);

    if (pvm->non_pvm_mode) {
        // 使用 KVM 通用指令仿真器处理所有退出
        return kvm_emulate_instruction(vcpu, 0);
    }

    // PVM 模式的正常 exit 处理
    switch (pvm->exit_vector) {
    case GP_VECTOR:
        return handle_exit_gp(vcpu);
    case PF_VECTOR:
        return handle_exit_pf(vcpu);
    // ...
    }
}
```

## 5. CPUID 拦截模式（可选）

虽然默认不拦截 CPUID（性能优先），超管提供了可选的 CPUID 拦截模式：

```c
static bool __read_mostly enable_cpuid_intercept = 0;
module_param_named(cpuid_intercept, enable_cpuid_intercept, bool, 0444);
```

启用后（`modprobe kvm_pvm cpuid_intercept=1`）：
- 每次 Guest CPUID 都触发 VM-exit
- 超管通过 `pvm_handle_exit_cpuid` 返回虚拟 CPUID 值
- 可以向 Guest 隐藏某些宿主 CPU 特性（如 VMX），防止 Guest 尝试嵌套虚拟化
- 性能开销：每次 `cpuid` 约 1-5 微秒（VM-exit 开销）

```c
static int pvm_handle_exit_cpuid(struct kvm_vcpu *vcpu)
{
    if (!enable_cpuid_intercept)
        return 0;   // 不拦截，让 CPU 直接执行

    // 仿真 CPUID
    kvm_emulate_cpuid(vcpu);
    return 1;
}
```

## 6. Guest CPU 特性禁用列表

即使在 PVM 模式下，某些 CPU 特性必须对 Guest 禁用，因为它们与 PVM 的 CPL3 模型不兼容：

```c
// arch/x86/include/asm/disabled-features.h（需要修改）
// 或通过 pvm_cpuid_override() 在运行时禁用

// 禁用的 Guest CPU 特性：
static void pvm_cpuid_override(struct kvm_vcpu *vcpu,
                                struct kvm_cpuid_entry2 *entry)
{
    if (entry->function == 7 && entry->index == 0) {
        // SMAP：Guest 在 CPL3 运行，SMAP 对其无意义（会误报保护）
        entry->ecx &= ~(F(SMAP));

        // SMEP：同上
        entry->ecx &= ~(F(SMEP));
    }

    if (entry->function == 0x80000001) {
        // XSAVES：需要 ring 0，PVM Guest 不支持
        // （xsaves 指令在 CPL3 下 #GP）
    }

    if (entry->function == 7 && entry->index == 0) {
        // 投机控制特性（需要 WRMSR，PVM 通过 hypercall 处理）
        // LA57：不完整支持
        entry->ecx &= ~(F(LA57));
    }

    // LDT（通过 LLDT 加载）：PVM 不支持
    // （LLDT 是特权指令，PVM 拒绝此 hypercall）
}
```

完整的禁用列表（来自 pvm.c 的 `pvm_vcpu_after_set_cpuid`）：
- `X86_FEATURE_XSAVES`（XSAVES/XRSTORS 指令，需要 ring 0）
- `X86_FEATURE_SMAP`（在 CPL3 下无意义）
- `X86_FEATURE_SMEP`（在 CPL3 下无意义）
- `X86_FEATURE_LA57`（5 级分页，不完整支持）
- 投机执行控制 MSR 相关特性（通过 hypercall 仿真）
- LDT 相关（不支持 LLDT）
