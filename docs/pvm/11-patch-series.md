# PVM Patch 系列结构详析（73 个 Patch）

## 1. Patch 系列总览

RFC 系列由 Lai Jiangshan（来自 Ant Group）提交，邮件主题为：
```
[RFC PATCH 00/73] KVM: x86/PVM: Introduce a new hypervisor
```

提交时间：2024 年 2 月 26 日

Patch 按功能分为四大组：

| 组 | Patch 范围 | 内容摘要 |
|----|-----------|---------|
| A | 01–02 | ABI 文档和 UAPI 头文件 |
| B | 03–04 | 宿主侧 Switcher 汇编 |
| C | 05–49 | Hypervisor 核心实现 |
| D | 50–73 | Guest 侧 Linux 内核修改 |

## 2. 组 A：ABI 文档（Patch 01-02）

### Patch 01：PVM 规范文档
- 文件：`Documentation/virt/kvm/x86/pvm-spec.rst`（989 行）
- 内容：完整的 PVM ABI 规范，包括底层状态、PVM 模式、PVCS 结构、MSR 接口、Paging、事件交付、Hypercall 协议
- 这是整个 patch 系列的基础文档

### Patch 02：UAPI 头文件
- 文件：`arch/x86/include/uapi/asm/pvm_para.h`
- 内容：所有 Guest 可见的常量定义：
  - PVM_SYNTHETIC_CPUID 序列
  - MSR_PVM_* 地址（0x4b564df0-0x4b564df5）
  - PVM_HC_* hypercall 号（0x17088200-0x17088209）
  - PVM_EVENT_FLAGS_* 位定义
  - PVM_PVCS_EVENT_VECTOR_* 位定义
  - `struct pvm_vcpu_struct` 布局

## 3. 组 B：Switcher（Patch 03-04）

### Patch 03：Switcher 状态结构
- 文件：`arch/x86/include/asm/switcher.h`
- 内容：
  - `struct tss_extra` 定义
  - SWITCH_FLAGS_* 常量（SMOD、UMOD、NO_DS_CR3、MOD_TOGGLE 等）
  - SWITCH_EXIT_REASONS_* 常量
  - SWITCH_ENTER_EFLAGS_* 掩码
  - `switcher_enter_guest`、`entry_SYSCALL_64_switcher` 等外部符号声明

### Patch 04：Switcher 汇编实现
- 文件：`arch/x86/entry/entry_64_switcher.S`（约 270 行汇编）
- 内容：
  - `MITIGATION_EXIT`/`MITIGATION_ENTER` 宏（speculation 缓解）
  - `switcher_enter_guest`：完整 VM-enter 函数
  - `switcher_return_from_guest`：VM-exit 标签
  - `entry_SYSCALL_64_switcher`：SYSCALL 统一入口（直接切换路径）
  - `entry_SYSCALL_64_switcher_safe_stack`、`entry_SYSRETQ_switcher_unsafe_stack` 标签
  - `canonical_rcx` 宏（SYSRET 安全检查）
- 同时修改 `arch/x86/entry/entry_64.S`（链入 Switcher 入口）

## 4. 组 C：Hypervisor 核心（Patch 05-49）

### 子组 C1：KVM x86 框架修改（Patch 05-15）

这些 patch 修改 KVM 的公共 x86 代码，为 PVM vendor module 提供必要的钩子和基础设施：

| Patch | 修改文件 | 说明 |
|-------|---------|------|
| 05 | `arch/x86/kvm/x86.c` | 为 PVM 添加 KVM_X86_HV_SUPPORTED 能力 |
| 06 | `arch/x86/kvm/mmu/*.c` | Shadow MMU 适配：允许 guest-mode shadow walk |
| 07-10 | `arch/x86/kvm/emulate.c` | 指令仿真器扩展（non_pvm_mode 使用）|
| 11-12 | `arch/x86/include/asm/kvm_host.h` | 添加 PVM 需要的 `kvm_x86_ops` 钩子 |
| 13-14 | `arch/x86/kvm/cpuid.c` | PVM Guest CPUID 处理钩子 |
| 15 | `arch/x86/kvm/lapic.c` | 虚拟 APIC 适配（PVM 中断交付路径）|

### 子组 C2：host_mmu（Patch 16-20）

| Patch | 修改文件 | 说明 |
|-------|---------|------|
| 16 | `arch/x86/kvm/pvm/host_mmu.c`（新增）| guest_address_space_init、clone_host_mmu、host_mmu_init、host_mmu_destroy |
| 17-20 | host_mmu.c 完善 | ASID/PCID 管理、LA57 支持、smod/umod CR3 构建 |

### 子组 C3：PVM 核心模块（Patch 21-49）

这是 patch 系列最大的子组，实现 `arch/x86/kvm/pvm/pvm.c`（3257 行）：

| Patch 范围 | 内容 |
|-----------|------|
| 21-22 | 模块初始化：`pvm_hardware_setup`、`pvm_hardware_enable`（per-CPU 初始化）|
| 23-25 | vCPU 生命周期：`pvm_vcpu_create`、`pvm_vcpu_free`、`pvm_vcpu_reset` |
| 26-28 | 寄存器接口：`pvm_get_segment`、`pvm_set_segment`、`pvm_get_cpl`、`pvm_get_cs_db_l_bits` |
| 29-31 | VM-enter/exit：`pvm_vcpu_run`、`pvm_prepare_switch_to_guest`、`pvm_run_vcpu` |
| 32-35 | Exit 处理：`pvm_handle_exit`、`handle_exit_syscall`、`handle_exit_gp`、`handle_exit_pf` |
| 36-38 | Hypercall 处理：`handle_hc_load_pagetables`、`handle_hc_irq_*`、`handle_hc_tlb_*` |
| 39-40 | MSR 处理：`pvm_get_msr`、`pvm_set_msr`（含 PVM 专用 MSR）|
| 41-42 | 中断/异常注入：`pvm_inject_event`、`pvm_queue_exception`、`pvm_set_nmi` |
| 43-44 | CPUID 虚拟化：`pvm_get_cpuid_features`、`pvm_vcpu_after_set_cpuid` |
| 45-46 | non_pvm_mode：`pvm_vcpu_reset`（含仿真模式）、`pvm_handle_exit`（仿真分支）|
| 47-48 | 调试支持：单步、硬件断点 |
| 49 | Kconfig + Makefile：`CONFIG_KVM_PVM`（alpha 质量警告）|

### 子组 C4：头文件（Patch 贯穿 C 组）

- `arch/x86/kvm/pvm/pvm.h`：`struct vcpu_pvm`、`struct kvm_pvm`、宏定义
- `arch/x86/include/asm/cpufeatures.h`：添加两个 feature bit
- `arch/x86/include/asm/disabled-features.h`：DISABLE_KVM_PVM_GUEST 掩码

```c
// arch/x86/include/asm/cpufeatures.h
#define X86_FEATURE_KVM_PVM_GUEST   (8*32+23)  // word 8, bit 23
#define X86_FEATURE_PV_GUEST        (8*32+24)  // word 8, bit 24（Xen PV 和 PVM 共用）
```

## 5. 组 D：Guest 侧修改（Patch 50-73）

这些 patch 修改 Linux 内核，使其能够作为 PVM Guest 运行（需要 `CONFIG_PVM_GUEST`）。

### 子组 D1：内核重定位和 PIE 支持（Patch 50-55）

PVM Guest 内核必须是 PIE（Position Independent Executable），因为内核需要加载在 PVM 分配的地址范围内（PML4[256..287]），而不是标准的 `0xffffffff80000000`。

| Patch | 修改文件 | 说明 |
|-------|---------|------|
| 50 | `arch/x86/Kconfig` | 添加 `CONFIG_PVM_GUEST` 选项（依赖 `CONFIG_PARAVIRT` + `X86_64`）|
| 51 | `arch/x86/include/asm/pvm.h`（新增）| Guest 侧 PVM 声明（pvm_vcpu_struct per-CPU 变量等）|
| 52-53 | `arch/x86/kernel/head64.c` | 早期引导：切换到 PVM 地址空间 |
| 54-55 | Linker scripts（`arch/x86/kernel/vmlinux.lds.S`）| 确保 PVM 内核加载在合法地址范围 |

### 子组 D2：事件处理入口汇编（Patch 56-60）

| Patch | 修改文件 | 说明 |
|-------|---------|------|
| 56 | `arch/x86/entry/entry_64_pvm.S`（新增）| 所有 Guest 入口汇编（约 350 行）|
| 57 | `arch/x86/entry/calling.h` | PUSH_IRET_FRAME_FROM_PVCS 等宏的依赖 |
| 58-60 | `arch/x86/kernel/asm-offsets.c` | PVCS 字段偏移量定义（用于汇编）|

### 子组 D3：Paravirt Ops 适配（Patch 61-68）

| Patch | 修改文件 | 说明 |
|-------|---------|------|
| 61 | `arch/x86/kernel/pvm.c`（新增）| PVM Guest 主初始化（pvm_detect、pvm_init）|
| 62 | `arch/x86/include/asm/paravirt.h` | 添加 PVM 专用的 pv_ops 钩子 |
| 63 | `arch/x86/kernel/paravirt.c` | PVM paravirt ops 注册 |
| 64-65 | `arch/x86/include/asm/pvm_ops.h`（新增）| pvm_irq_disable/enable、pvm_save_fl 声明 |
| 66-67 | `arch/x86/mm/tlb.c` | flush_tlb_* 替换为 PVM TLB hypercall |
| 68 | `arch/x86/include/asm/mmu_context.h` | CR3 切换替换为 PVM_HC_LOAD_PGTBL |

### 子组 D4：特权操作替换（Patch 69-73）

| Patch | 修改文件 | 说明 |
|-------|---------|------|
| 69 | `arch/x86/include/asm/uaccess.h` | `__uaccess_begin`/`__uaccess_end` 使用 PKRU 替代 STAC/CLAC |
| 70 | `arch/x86/include/asm/processor.h` | CPU 特性检查的 PVM 虚拟化 |
| 71 | `arch/x86/kernel/traps.c` | 异常处理：读 PVCS::cr2 和 event_vector |
| 72 | `arch/x86/kernel/smp.c` | IPI 通过 KVM hypercall（PVM_HC_*）|
| 73 | `arch/x86/include/asm/smp.h` | 最终 cleanup + CONFIG_PVM_GUEST 封装 |

## 6. 关键文件统计

| 文件 | 行数 | 说明 |
|------|------|------|
| `arch/x86/kvm/pvm/pvm.c` | 3257 | Hypervisor 核心（新增）|
| `Documentation/virt/kvm/x86/pvm-spec.rst` | 989 | ABI 规范（新增）|
| `arch/x86/entry/entry_64_switcher.S` | ~270 | Switcher 汇编（新增）|
| `arch/x86/entry/entry_64_pvm.S` | ~350 | Guest 入口汇编（新增）|
| `arch/x86/kvm/pvm/host_mmu.c` | ~250 | Shadow MMU（新增）|
| `arch/x86/kvm/pvm/pvm.h` | ~200 | 头文件（新增）|
| `arch/x86/include/uapi/asm/pvm_para.h` | ~150 | UAPI（新增）|
| `arch/x86/include/asm/switcher.h` | ~100 | Switcher 头文件（新增）|

新增代码总计：约 6000 行（不含 Documentation）
修改代码：约 500-1000 行（分散在 KVM、entry 等）

## 7. 模块初始化流程

### 7.1 模块加载（`modprobe kvm_pvm`）

```c
// arch/x86/kvm/pvm/pvm.c
static int __init pvm_init(void)
{
    int r;

    // 1. 检测 CPU 特性
    if (!boot_cpu_has(X86_FEATURE_FSGSBASE)) {
        pr_err("PVM requires FSGSBASE\n");
        return -EOPNOTSUPP;
    }
    if (!boot_cpu_has(X86_FEATURE_RDTSCP)) {
        pr_err("PVM requires RDTSCP\n");
        return -EOPNOTSUPP;
    }
    // KPTI 不兼容
    if (boot_cpu_has(X86_FEATURE_PTI)) {
        pr_warn("PVM does not support KPTI\n");
        // （继续加载，但 KPTI 功能受限）
    }

    // 2. 初始化宿主 MMU（分配 Guest 地址空间）
    r = host_mmu_init();
    if (r) {
        pr_err("PVM: host_mmu_init failed\n");
        return r;
    }

    // 3. 注册 KVM vendor module
    r = kvm_init(&pvm_init_ops, sizeof(struct vcpu_pvm), THIS_MODULE);
    if (r)
        goto out;

    return 0;
out:
    host_mmu_destroy();
    return r;
}
module_init(pvm_init);
```

### 7.2 per-CPU 初始化（`pvm_hardware_enable`）

在每个 CPU 上运行（通过 `on_each_cpu`）：

```c
static int pvm_hardware_enable(void)
{
    struct tss_extra *tss_ex = this_cpu_tss_extra();

    // 初始化 tss_extra
    memset(tss_ex, 0, sizeof(*tss_ex));

    // 保存当前宿主 CR3（作为默认 host_cr3）
    tss_ex->host_cr3 = __read_cr3();

    // 设置初始 switch_flags（SMOD | PVCS_INVALID）
    tss_ex->switch_flags = SWITCH_FLAGS_INIT;

    // 记录 host IDT 地址（用于非 PVM 模式的异常向量）
    store_idt((struct desc_ptr *)&host_idt_base);

    return 0;
}
```

### 7.3 vCPU 创建（`pvm_vcpu_create`）

```c
static int pvm_vcpu_create(struct kvm_vcpu *vcpu)
{
    struct vcpu_pvm *pvm = to_pvm(vcpu);

    // 初始化 switch_flags（SMOD | PVCS_INVALID）
    pvm->switch_flags = SWITCH_FLAGS_INIT;

    // 初始化 ASID（无效）
    pvm->asid = 0;
    pvm->asid_generation = PVM_ASID_GEN_RESERVED;

    // 初始化段寄存器为复位值
    pvm_vcpu_reset(vcpu, false);

    return 0;
}
```

## 8. 硬件要求汇总

### 8.1 必须支持

| 特性 | CPUID | 原因 |
|------|-------|------|
| FSGSBASE | CPUID.07:EBX bit 0 | Switcher 使用 RDGSBASE/WRGSBASE（CPL3 可用）|
| RDTSCP | CPUID.80000001:EDX bit 27 | 支持 RDTSCP 指令（TSC_AUX）|
| CMPXCHG16B | CPUID.01:ECX bit 13 | 原子 128-bit 比较交换 |
| 64-bit Long Mode | — | PVM 只支持 64-bit |
| SSE2 | CPUID.01:EDX bit 26 | 基本向量运算 |

### 8.2 推荐支持

| 特性 | 原因 |
|------|------|
| PKS（Protection Keys Supervisor）| SMAP 替代（无此特性则无内核/用户内存隔离保护）|
| PCID | TLB 优化（无此特性则每次 CR3 切换完全刷新 TLB）|
| XSAVE | 浮点/SIMD 状态保存 |
| AVX2/AVX-512 | 性能（超管和 Guest 均受益）|

### 8.3 不兼容

| 特性/配置 | 原因 |
|----------|------|
| 宿主 KPTI（`X86_FEATURE_PTI`）| Switcher 的 tss_extra 访问路径与 KPTI 冲突（TODO）|
| `CONFIG_KASAN_VMALLOC` | vmalloc shadow 与 PVM 地址空间分配冲突 |
| 在 5 级分页下 `CONFIG_KASAN` | LA57 + KASAN 地址空间冲突 |
| AMD CPU（无 PKS）| PKS 保护缺失（功能可运行但安全性降级）|
| Intel Tiger Lake 以前的 CPU（无 PKS）| 同上 |

## 9. Kconfig 选项

```kconfig
# arch/x86/kvm/Kconfig
config KVM_PVM
    tristate "PVM (Pagetable-based Virtual Machine) support"
    depends on X86_64 && KVM
    select KVM_GENERIC_HARDWARE_ENABLING
    help
      This is the new hypervisor based on page table and
      switcher technique.

      The hypervisor is in alpha quality, meaning you should
      only use it for testing and development purpose.

      If unsure, say N.

# arch/x86/Kconfig
config PVM_GUEST
    bool "PVM Guest support"
    depends on X86_64 && PARAVIRT && RELOCATABLE
    help
      Support running as a PVM (Pagetable-based Virtual Machine) guest.
      Requires a PVM hypervisor.
```

`CONFIG_PVM_GUEST` 依赖 `CONFIG_RELOCATABLE`——这对应 PIE 内核要求（内核必须能够被重定位到非默认地址）。
