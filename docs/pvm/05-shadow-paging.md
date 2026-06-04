# Shadow Paging：PVM 的内存虚拟化实现

## 1. 为什么需要 Shadow Paging

PVM 无法使用 EPT/NPT（这些是 VT-x/AMD-V 特性），因此必须用软件实现内存虚拟化。Shadow paging 的思路：

- **Guest 页表**：Guest 内核维护自己认为的页表（GVA→GPA 映射）
- **影子页表**：超管维护真实使用的页表（GVA→HPA 映射），这是 CR3 中实际加载的页表
- **同步**：当 Guest 修改页表时（通过 `PVM_HC_LOAD_PGTBL`），超管同步影子页表

PVM 的影子页表分三层：
1. `host_mmu_root_pgd`：模块级共享（所有 Guest VM 共用），包含宿主内核映射的影子
2. 每 vCPU 的影子页表：在 KVM MMU 框架内，跟踪 Guest 的 GVA→HPA 映射
3. smod_cr3 / umod_cr3：当前激活的影子页表（见第 4 节）

## 2. 宿主 MMU 初始化（host_mmu_init）

```c
// arch/x86/kvm/pvm/host_mmu.c

int __init host_mmu_init(void)
{
    u64 *host_pgd;

    // 1. 初始化 Guest 地址空间（分配排他 VA 范围）
    if (guest_address_space_init() < 0)
        return -ENOMEM;

    // 2. 获取宿主顶层页表（考虑 KPTI）
    if (!boot_cpu_has(X86_FEATURE_PTI))
        host_pgd = (void *)current->active_mm->pgd;
    else
        // KPTI 环境：使用 KPTI 用户侧页表（包含内核映射）
        host_pgd = (void *)kernel_to_user_pgdp(current->active_mm->pgd);

    // 3. 分配模块级影子根页表
    host_mmu_root_pgd = (void *)__get_free_page(GFP_KERNEL | __GFP_ZERO);
    if (!host_mmu_root_pgd) {
        host_mmu_destroy();
        return -ENOMEM;
    }

    if (pgtable_l5_enabled()) {
        // 5 级分页（LA57）
        host_mmu_la57_top_p4d = (void *)__get_free_page(GFP_KERNEL | __GFP_ZERO);
        // 克隆宿主 PML5（跳过 Guest PML5 范围）
        clone_host_mmu(host_mmu_root_pgd, host_pgd,
                       pml5_index_start, pml5_index_end);
        // 克隆顶层 P4D（跳过 Guest PML4 范围）
        clone_host_mmu(host_mmu_la57_top_p4d,
                       __va(host_pgd[511] & SPTE_BASE_ADDR_MASK),
                       pml4_index_start, pml4_index_end);
        // 将修改后的 P4D 插入根页表
        host_mmu_root_pgd[511] = (host_pgd[511] & ~SPTE_BASE_ADDR_MASK) |
                                   __pa(host_mmu_la57_top_p4d);
        host_mmu_root_pgd[511] &= ~(_PAGE_USER | SPTE_MMU_PRESENT_MASK);
    } else {
        // 4 级分页（LA48）
        clone_host_mmu(host_mmu_root_pgd, host_pgd,
                       pml4_index_start, pml4_index_end);
    }

    return 0;
}
```

### 2.1 clone_host_mmu 函数详解

```c
static __init void clone_host_mmu(u64 *spt, u64 *host, int index_start, int index_end)
{
    int i;

    // 只克隆上半部分（内核地址空间，PGD index 256-511）
    for (i = PTRS_PER_PGD/2; i < PTRS_PER_PGD; i++) {
        // 跳过 Guest 排他地址范围
        if (i >= index_start && i < index_end)
            continue;

        // 克隆宿主 PGD 项，但去掉以下位：
        // - _PAGE_USER：使宿主内核地址对 CPL3 不可访问
        // - SPTE_MMU_PRESENT_MASK：KVM shadow PTE 的 present 位
        spt[i] = host[i] & ~(_PAGE_USER | SPTE_MMU_PRESENT_MASK);
    }
}
```

**为什么去掉 `_PAGE_USER`？**：PVM Guest（CPL3）运行时，宿主内核地址空间不应该可访问。去掉 U/S 位后，只有 CPL0 才能访问这些映射。当 Switcher 需要访问 `tss_extra`（在宿主内核地址空间），它通过 swapgs 切回宿主 GS 后访问宿主 per-CPU 结构——但此时处理器是 CPL3，如何访问呢？

实际上，Switcher 在 swapgs 后立即通过 GS-relative 访问 `tss_extra`，而 GS-relative 寻址使用 GS base + 偏移，这是通过 WRGSBASE/RDGSBASE 设置的内存地址。访问 `tss_extra` 时，GS base 指向宿主 per-CPU 区域（在上半部分地址空间），这个地址的 _PAGE_USER 已经被去掉。

等等，这样不会 #PF 吗？这里需要更深入分析：实际上，`tss_extra` 通过 WRGSBASE 设置为 per-CPU TSS 区域的地址，而 TSS 在宿主 KPTI 用户侧页表中**是有 _PAGE_USER 位的**（允许 CPL3 在异常时切换栈）。

对于 PVM，`clone_host_mmu` 使用的是 KPTI 用户侧页表（`kernel_to_user_pgdp`），这个页表中 TSS 页本来就有 _PAGE_USER，所以 Switcher 可以访问。但 clone 时去掉 _PAGE_USER，那 TSS 就不可访问了？

实际上 `SPTE_MMU_PRESENT_MASK` 的作用是关键——去掉 present 位后，KVM shadow MMU 知道这些页表项是"无效"的，需要在 fault 时重新建立，通过正常的 shadow page walk 建立正确的映射（包含 Guest 物理地址到宿主物理地址的翻译）。

**VSYSCALL 页的特殊处理**：注释中提到"remove userbit from host mmu, which also disable VSYSCALL page"——vsyscall 页在宿主页表中有 _PAGE_USER，去掉后 Guest 访问 vsyscall 会 #PF。这是正确行为——PVM Guest 应该使用 vDSO 而不是 vsyscall。

## 3. Guest 地址空间初始化

```c
static int __init guest_address_space_init(void)
{
    // KASAN_VMALLOC 与 PVM 不兼容
    if (IS_ENABLED(CONFIG_KASAN_VMALLOC)) {
        pr_warn("CONFIG_KASAN_VMALLOC is not compatible with PVM");
        return -1;
    }

    if (pgtable_l5_enabled()) {
        // 5 级分页下，KASAN 也不兼容
        if (IS_ENABLED(CONFIG_KASAN)) {
            pr_warn("CONFIG_KASAN is not compatible with PVM on 5-level paging mode");
            return -1;
        }

        // 检查 PVM_GUEST_MAPPING_START（= -1UL << 47 = 0xFFFF800000000000）
        // 与 VADDR_END_L5 的关系（编译时断言）
        BUILD_BUG_ON(PVM_GUEST_MAPPING_START != VADDR_END_L5);

        // 设置 PML4 范围
        pml4_index_start = L4_PT_INDEX(PVM_GUEST_MAPPING_START);
        pml4_index_end = L4_PT_INDEX(RAW_CPU_ENTRY_AREA_BASE);

        // 尝试分配 32 * 256TB 的 PML5 级别 vmalloc 区域
        pvm_va_range = get_vm_area_align(DEFAULT_RANGE_L5_SIZE, PT_L5_SIZE,
                                         VM_ALLOC|VM_NO_GUARD);
        if (!pvm_va_range) {
            // 分配失败：设置 PML5 为最小范围（=当前 level 5 不可用）
            pml5_index_start = 0x1ff;
            pml5_index_end = 0x1ff;
        } else {
            // 从分配的 VA 计算 PML5 索引范围
            pml5_index_start = L5_PT_INDEX((u64)pvm_va_range->addr);
            pml5_index_end = L5_PT_INDEX((u64)pvm_va_range->addr + pvm_va_range->size);
        }
    } else {
        // 4 级分页：分配 32 * 512GB 的 vmalloc 区域
        pvm_va_range = get_vm_area_align(DEFAULT_RANGE_L4_SIZE, PT_L4_SIZE,
                                         VM_ALLOC|VM_NO_GUARD);
        if (!pvm_va_range)
            return -1;

        // 从分配的 VA 计算 PML4 索引范围
        pml4_index_start = L4_PT_INDEX((u64)pvm_va_range->addr);
        pml4_index_end = L4_PT_INDEX((u64)pvm_va_range->addr + pvm_va_range->size);
        // LA48 模式下 PML5 范围不使用
        pml5_index_start = 0x1ff;
        pml5_index_end = 0x1ff;
    }

    return 0;
}
```

### 3.1 分配参数解析

```c
#define PT_L4_SHIFT      39
#define PT_L4_SIZE       (1UL << 39)   // 512GB（每个 PML4 条目覆盖范围）
#define DEFAULT_RANGE_L4_SIZE  (32 * PT_L4_SIZE)  // 32 * 512GB = 16TB

#define PT_L5_SHIFT      48
#define PT_L5_SIZE       (1UL << 48)   // 256TB（每个 PML5 条目覆盖范围）
#define DEFAULT_RANGE_L5_SIZE  (32 * PT_L5_SIZE)  // 32 * 256TB = 8PB
```

`get_vm_area_align(size, align, flags)` 在 vmalloc 区域分配连续的虚拟地址空间（不分配物理内存！）。`VM_NO_GUARD` 禁用保护页（Guard page）。

分配 16TB / 8PB 只是**保留虚拟地址范围**，不消耗物理内存。这个范围用于：后续每个 PVM Guest 的内核地址空间映射到此范围内的子范围。

### 3.2 为什么 KASAN_VMALLOC 不兼容

`CONFIG_KASAN_VMALLOC` 让 KASAN 在 vmalloc 分配时跟踪内存使用，为所有 vmalloc VA 范围添加 shadow 映射。PVM 分配的 16TB vmalloc 范围也会被 KASAN 插桩，导致：
1. 影子内存开销巨大（16TB * 1/8 = 2TB KASAN shadow）
2. KASAN 在初始化时扫描所有 vmalloc 映射，PVM 的范围让初始化时间极长
3. KASAN 的 shadow 映射与 PVM 的 clone_host_mmu 逻辑冲突

## 4. ASID/PCID 管理

PVM 利用 x86 的 PCID（Process-Context Identifiers）特性来减少 TLB 刷新开销。

### 4.1 PCID 到 ASID 的映射

```c
// arch/x86/kvm/pvm/pvm.h

#define PVM_ASID_SHIFT           3
#define NUM_PVM_GUEST_PCID_INDEX (1U << PVM_ASID_SHIFT)  // = 8
#define PVM_GUEST_PTI_PCID_BIT   11
#define PVM_GUEST_PTI_PCID_MASK  (1U << PVM_GUEST_PTI_PCID_BIT)  // = 0x800

#define PVM_GUEST_PCID_INDEX_MASK  (NUM_PVM_GUEST_PCID_INDEX - 1)  // = 0x7
#define PVM_GUEST_PCID_MASK        (PVM_GUEST_PCID_INDEX_MASK | PVM_GUEST_PTI_PCID_MASK)
                                   // = 0x80F（PCID 低 4 位 + bit 11）

#define PVM_ASID_MIN  1
#define PVM_ASID_MAX  (((1U << PVM_GUEST_PTI_PCID_BIT) - 1) / NUM_PVM_GUEST_PCID_INDEX)
                      // = (0x7FF) / 8 = 255
```

**设计原理**：

硬件 PCID 是 12 位（0-4095）。PVM 将这 12 位分配如下：

```
Bit 11     (PVM_GUEST_PTI_PCID_BIT): PTI 奇偶位
Bits 3-10  (8 位): ASID 值（1-255）
Bits 0-2   (3 位，PVM_ASID_SHIFT): Guest 内部 PCID 索引（0-7）

硬件 PCID = (asid << 3) | guest_pcid_index
           + (pti_bit << 11) 用于 KPTI 时的内核侧/用户侧区分
```

每个 PVM Guest 有 256 个可能的 ASID（1-255），每个 ASID 有 8 个子 PCID 槽（供 Guest 内核自己使用，比如多个 mm 的并发 CR3）。

**ASID 0 保留**：ASID=0 对应 PCID=0，是宿主内核使用的。

**PTI bit**：当宿主启用 KPTI 时，bit 11 用于区分"内核侧页表"（bit 11=0）和"用户侧页表"（bit 11=1）。PVM 同样需要维护这个约定。

### 4.2 ASID 生命周期

```c
// 全局 ASID 生成号（用于 ASID 回收）
static u64 pvm_asid_generation;

// 每 vCPU ASID 状态
struct vcpu_pvm {
    u32 asid;            // 当前 ASID（0 = 未分配）
    u64 asid_generation; // 上次分配时的全局生成号
    bool flush_hwtlb_current; // 需要刷新当前 ASID 的 TLB
};
```

ASID 分配策略（类似 KVM 的 vpid 机制）：
1. 每个 vCPU 在 VM-enter 时检查 `asid_generation == pvm_asid_generation`
2. 若一致：ASID 有效，直接使用
3. 若不一致（全局 generation 已更新）：ASID 可能已被其他 vCPU 使用，需要重新分配或刷新
4. 若 ASID 耗尽（所有 1-255 都在使用）：递增全局 `pvm_asid_generation`，触发所有 vCPU 重新分配（相当于 TLB 全局刷新）

### 4.3 TLB 刷新策略

PVM 提供四种 TLB 刷新粒度：

| Hypercall | KVM 实现 | 适用场景 |
|-----------|----------|---------|
| `PVM_HC_TLB_FLUSH` | `kvm_make_request(KVM_REQ_TLB_FLUSH_GUEST, vcpu)` | 全局 TLB 刷新（如进程退出）|
| `PVM_HC_TLB_FLUSH_CURRENT` | `kvm_set_cr3(vcpu, vcpu->arch.cr3)` | 刷新当前 PCID（如映射更改）|
| `PVM_HC_TLB_INVLPG` | `kvm_mmu_invlpg(vcpu, addr)` | 单页失效（如 `munmap` 单页）|
| `PVM_HC_LOAD_PGTBL with TLB flag` | `kvm_set_cr3(vcpu, pgd)` | 加载新 CR3 并刷新 |

**`kvm_set_cr3(vcpu, pgd | CR3_NOFLUSH)` vs `kvm_set_cr3(vcpu, pgd)`**：
- `CR3_NOFLUSH`（bit 63）：写 CR3 时不刷新 TLB（保留当前 PCID 的 TLB）
- 不带 `CR3_NOFLUSH`：写 CR3 时刷新当前 PCID 的所有 TLB 项

这对应 `PVM_LOAD_PGTBL_FLAGS_TLB` 标志：Guest 调用 `PVM_HC_LOAD_PGTBL` 时可以选择是否同时刷新 TLB。

## 5. CR3 的三种形态

PVM 中有三个不同的 CR3 值：

```
enter_cr3（tss_extra.enter_cr3）：
  Switcher 在 VM-enter 时加载的 CR3。
  = smod_cr3 或 umod_cr3，取决于 Guest 当前模式。
  但要注意：enter_cr3 必须包含 Switcher 代码的映射（PCID=0 时），
  因为 CR3 切换那一刻处理器在执行 Switcher 代码。

smod_cr3（tss_extra.smod_cr3）：
  Supervisor mode（Guest 内核）的完整影子页表。
  包含：
  - Guest 内核虚拟地址（范围 2：PML4 index 内）→ Guest 内核物理页
  - Guest 用户虚拟地址（范围 3：下半部分）→ Guest 用户物理页（PKS 限制访问）
  - 宿主内核地址（上半部分，非 Guest 范围）→ 宿主物理页（无 _PAGE_USER）

umod_cr3（tss_extra.umod_cr3）：
  User mode（Guest 用户态）的影子页表。
  包含：
  - Guest 用户虚拟地址 → Guest 用户物理页
  - 不包含 Guest 内核虚拟地址（Guest 内核不在此 CR3 中可见）
  - 包含必要的宿主映射（Switcher、PVCS 等）
```

### 5.1 smod_cr3 的构建

smod_cr3 基于 `host_mmu_root_pgd`（共享宿主映射）+ Guest 内核 PGD 项：

```c
// 构建 smod_cr3 的伪代码（实际通过 KVM MMU shadow walk 实现）
u64 smod_pgd[512];

// 上半部分：使用 clone_host_mmu 的结果（宿主内核映射，无 _PAGE_USER）
memcpy(&smod_pgd[256], &host_mmu_root_pgd[256], 256 * sizeof(u64));

// Guest 内核地址范围：指向 Guest 内核的物理页
for (i = pml4_index_start; i < pml4_index_end; i++) {
    smod_pgd[i] = guest_kernel_pmd[i];  // Guest 内核 PGD 项（已翻译为 HPA）
}

// 下半部分：用户地址空间（Guest 用户内存）
for (i = 0; i < 256; i++) {
    smod_pgd[i] = guest_user_pmd[i];  // 带 PKS protection key 的用户页
}
```

### 5.2 umod_cr3 的构建

umod_cr3 只包含用户地址空间和必要的宿主映射（用于 Switcher 工作）：

```c
// 构建 umod_cr3 的伪代码
u64 umod_pgd[512];

// 下半部分：用户地址空间
for (i = 0; i < 256; i++) {
    umod_pgd[i] = guest_user_pmd[i];  // Guest 用户内存
}

// 上半部分：不包含 Guest 内核地址，但包含 Switcher 代码
// Switcher 代码在 .entry.text 节，有特殊映射保证在 umod_cr3 中也可见
// 宿主 per-CPU（tss_extra）也需要可访问（通过 KPTI 用户侧映射）

// Guest 内核地址范围：全部清零（umod 不能访问 Guest 内核）
for (i = pml4_index_start; i < pml4_index_end; i++) {
    umod_pgd[i] = 0;  // Guest 内核不可访问
}
```

## 6. Guest CR3 加载（PVM_HC_LOAD_PGTBL 实现）

```c
static int handle_hc_load_pagetables(struct kvm_vcpu *vcpu, unsigned long flags,
                                     unsigned long pgd)
{
    unsigned long cr4 = vcpu->arch.cr4;

    // 处理 LA57 模式切换
    if (!(flags & PVM_LOAD_PGTBL_FLAGS_LA57))
        cr4 &= ~X86_CR4_LA57;
    else if (guest_cpuid_has(vcpu, X86_FEATURE_LA57))
        cr4 |= X86_CR4_LA57;

    if (cr4 != vcpu->arch.cr4) {
        vcpu->arch.cr4 = cr4;
        kvm_mmu_reset_context(vcpu);  // 重建整个 shadow paging 上下文
        kvm_make_request(KVM_REQ_TLB_FLUSH_GUEST, vcpu);
    }

    // 加载 CR3（pgd = Guest 物理地址）
    if (flags & PVM_LOAD_PGTBL_FLAGS_TLB)
        kvm_set_cr3(vcpu, pgd);           // 刷新 TLB
    else
        kvm_set_cr3(vcpu, pgd | CR3_NOFLUSH);  // 不刷新 TLB（PCID 保持）

    return 1;
}
```

`kvm_set_cr3` 触发 KVM MMU 的 shadow page table walk，建立或更新 Guest GPA→HPA 的映射。

## 7. Shadow Paging 的性能影响

### 7.1 开销来源

1. **TLB miss 放大**：软件 shadow paging 的 TLB miss 需要 shadow page walk，比 EPT/NPT 的两级硬件 page walk 慢 2-4x
2. **CR3 切换开销**：smod/umod 切换时必须写 CR3，触发 TLB 失效（通过 PCID 部分缓解）
3. **影子页表同步**：每次 Guest CR3 切换（进程切换）需要 `kvm_set_cr3` 和可能的影子页表重建
4. **内存开销**：每个 Guest 需要维护完整的影子页表（对多进程 Guest 影响显著）

### 7.2 优化措施

| 优化 | 实现方式 | 效果 |
|------|---------|------|
| PCID | `PVM_ASID_SHIFT=3`，每 ASID 8 个 PCID 子槽 | 进程切换时避免全局 TLB 刷新 |
| `CR3_NOFLUSH` | `PVM_HC_LOAD_PGTBL` 无 TLB flag 时使用 | 保留 TLB 条目 |
| `INVLPG` hypercall | `PVM_HC_TLB_INVLPG` 精确失效单页 | 减少 TLB 开销 |
| 影子页表复用 | `host_mmu_root_pgd` 所有 Guest 共享 | 减少内核映射影子页表开销 |

### 7.3 与 EPT 的性能对比

SOSP 2023 论文数据：
- 内存密集型负载（大量 TLB miss）：PVM 约为 VMX+EPT 的 70-80%
- CPU 密集型（少 TLB miss）：PVM 约为 VMX+EPT 的 90-95%
- 系统调用密集型（无 EPT miss）：PVM 约为 VMX+EPT 的 75-85%（smod/umod 切换开销）

在公有云场景中，EPT 根本不可用（因为没有嵌套 VMX），PVM 是唯一选择，所以与 EPT 的比较只是学术意义。

## 8. 已知的 Shadow Paging 限制

### 8.1 KPTI 未实现

Guest 内核的 KPTI（使用两个 CR3 隔离内核/用户的 Meltdown 缓解）尚未实现。原因：
- PVM 已经有 smod_cr3/umod_cr3 两个 CR3，再加 KPTI 的内核侧/用户侧是四个 CR3
- KVM shadow MMU 需要支持为每个 Guest 进程维护四个影子页表，复杂度剧增

### 8.2 非规范地址的 DR 断点被禁用

超出 `MSR_PVM_LINEAR_ADDRESS_RANGE` 范围的断点（DR0-DR3 中设置的地址）在 `underlying DR7` 中对应位被清零，即这些断点不生效。这是为了防止 Guest 通过断点探测宿主地址空间。

### 8.3 LA57 支持不完整

LA57（5 级分页，57-bit 地址空间）模式支持标记为不完整。主要问题：
- `pml5_index_start = pml5_index_end` 时意味着 PML5 范围不可用，Guest 只能使用 PML4 范围
- LA57 下的地址空间规划与 KASAN 的 vmalloc shadow 冲突（见 `guest_address_space_init` 中的 KASAN 检查）
