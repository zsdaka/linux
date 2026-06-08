# aarch64 嵌套虚拟化系统寄存器机制深度分析

> 基于 `zsdaka/linux` HEAD `8bc67e4db` (v7.1.0-rc4 era) 的 KVM/arm64 实现

## 目录

1. [背景与术语](#1-背景与术语)
2. [ARM 硬件 NV/NV2 能力](#2-arm-硬件-nvnv2-能力)
3. [KVM/arm64 嵌套虚拟化整体架构](#3-kvmarm64-嵌套虚拟化整体架构)
4. [系统寄存器分类](#4-系统寄存器分类)
5. [存储布局与核心数据结构](#5-存储布局与核心数据结构)
6. [NV trap 路由机制](#6-nv-trap-路由机制)
7. [VNCR 内存映射机制](#7-vncr-内存映射机制)
8. [上下文保存/恢复路径](#8-上下文保存恢复路径)
9. [每类系统寄存器 L2 访问情景分析](#9-每类系统寄存器-l2-访问情景分析)
10. [三层数据流综合交互图](#10-三层数据流综合交互图)
11. [总结](#11-总结)

---


## 1. 背景与术语

### 1.1 三层模型

```
+--------------------------------------------------------------+
| L2  Guest          (Guest of L1)                             |
|     vEL1/vEL0 from L1's perspective                          |
|     Runs at physical EL0/EL1 with shadow stage-2             |
+--------------------------------------------------------------+
| L1  Guest Hypervisor  (e.g. KVM-on-KVM, Hyper-V, ESXi guest) |
|     Thinks it runs at "vEL2"                                 |
|     Actually runs at physical EL1 with HCR_EL2.NV/NV2=1      |
+--------------------------------------------------------------+
| L0  Host Hypervisor (KVM)                                    |
|     Runs at physical EL2 with VHE (HCR_EL2.E2H=1)            |
|     Owns the shadow stage-2, programs VNCR_EL2               |
+--------------------------------------------------------------+
| Hardware (ARMv8.6+/ARMv9 with FEAT_NV2)                      |
+--------------------------------------------------------------+
```

### 1.2 关键术语

| 术语 | 含义 |
|---|---|
| **vEL2 / vEL1 / vEL0** | "虚拟 EL2/EL1/EL0"，L1 视角的异常等级 |
| **physical EL2/EL1/EL0** | 真实硬件异常等级 |
| **VHE** | Virtualization Host Extensions (ARMv8.1)，HCR_EL2.E2H=1 让宿主内核运行在 EL2 |
| **NV** | Nested Virtualization (ARMv8.4 FEAT_NV)，HCR_EL2.NV=1 把 EL1 的 EL2 寄存器访问 trap 到 EL2 |
| **NV2** | Enhanced NV (ARMv8.6 FEAT_NV2)，HCR_EL2.NV2=1 把部分 EL2 寄存器访问**重定向到内存**而非 trap |
| **VNCR** | Virtual Nested Control Register, `VNCR_EL2`，NV2 的"内存重定向基址寄存器" |
| **VNCR-backed register** | 在 NV2 下硬件会把读写自动重定向到 VNCR 页面对应偏移的 EL2 系统寄存器 |
| **\_EL12 alias** | VHE 引入的 MRS/MSR 编码，让 EL2 软件能直接访问"EL1 的 EL1 寄存器" |
| **Coarse-Grained Trap (CGT)** | HCR_EL2、MDCR_EL2 等寄存器中"粗粒度"的 trap 控制位（一个位控制一组寄存器） |
| **Fine-Grained Trap (FGT)** | ARMv8.6 引入的 HFGRTR/HFGWTR/HFGITR 等"细粒度" trap 寄存器（一个位控制一个寄存器） |

---


## 2. ARM 硬件 NV/NV2 能力

### 2.1 演进时间线

| 特性 | 引入版本 | 关键作用 |
|---|---|---|
| **VHE** (FEAT_VHE) | ARMv8.1 | EL2 与 EL1 寄存器集合大致对齐，宿主内核可直接跑在 EL2 |
| **FEAT_NV** | ARMv8.4 | EL1 访问 EL2 寄存器/执行 ERET 时 trap 到 EL2，让 L0 可模拟 |
| **FEAT_NV2** | ARMv8.6 | 大量 EL2 寄存器访问被硬件**重定向到内存**（VNCR 页），消除高频 trap |
| **FEAT_FGT** | ARMv8.6 | 细粒度 trap 控制（HFGRTR/HFGWTR/HFGITR/HDFGRTR/HDFGWTR/HAFGRTR） |
| **FEAT_E2H0** | ARMv9.5 | 允许实现使 HCR_EL2.E2H 为 RES1（仅支持 VHE） |
| **FEAT_FGT2** | ARMv9.x | FGT 第二代（HFGRTR2/HFGWTR2/HFGITR2/HDFGRTR2/HDFGWTR2） |

### 2.2 HCR_EL2 中的 NV 相关位

| 位 | 名称 | 作用 |
|---|---|---|
| `NV` | bit 42 | 当 EL=EL1 时，访问 EL2 系统寄存器、执行 ERET 等会 trap 到 EL2 |
| `NV1` | bit 43 | 配合 NV，影响某些寄存器（如 VBAR/ELR/SPSR）的 trap 路由（用于建模 nVHE 客户机） |
| `AT` | bit 44 | EL1 执行 `AT S1E*` 地址翻译指令时 trap 到 EL2 |
| `NV2` | bit 45 | 启用"内存重定向"模式：EL2 寄存器访问指向 VNCR_EL2 指定的页面而非 trap |
| `E2H` | bit 34 | VHE 主开关（KVM 始终为 1） |
| `TGE` | bit 27 | "Trap General Exceptions"，仅在宿主 EL0 执行时为 1 |

### 2.3 VNCR_EL2 寄存器

- `VNCR_EL2.BADDR` (bits [56:12])：VNCR 页面在 **EL2&0 翻译机制下的 VA**（不是 IPA！）。  
- 必须 4kB 对齐。
- 当 `HCR_EL2.NV2=1` 且当前 EL=EL1 时，对一组架构定义的 EL2 系统寄存器执行 MRS/MSR，硬件会：
  1. 将"寄存器编号"映射为页面内固定偏移（见 `arch/arm64/include/asm/vncr_mapping.h`）
  2. 通过 EL2&0 翻译机制（即 EL2 启用 E2H 时的 stage-1 + stage-2）访问 `VNCR_EL2.BADDR + offset` 处的 8 字节
  3. 完成后**不**产生异常

### 2.4 哪些寄存器是 VNCR-backed?

由 ARM 架构静态规定，内核 `arch/arm64/include/asm/vncr_mapping.h` 中是其完整列表，部分核心条目：

```c
#define VNCR_VTTBR_EL2          0x020
#define VNCR_VTCR_EL2           0x040
#define VNCR_VMPIDR_EL2         0x050
#define VNCR_HCR_EL2            0x078   /* HCR_EL2 自身也是 VNCR-backed! */
#define VNCR_HCRX_EL2           0x0A0
#define VNCR_VNCR_EL2           0x0B0   /* 自指（用于 L2 嵌套） */
#define VNCR_SCTLR_EL1          0x110
#define VNCR_TTBR0_EL1          0x200
#define VNCR_TTBR1_EL1          0x210
#define VNCR_VBAR_EL1           0x250
#define VNCR_HFGRTR_EL2         0x1B8   /* 细粒度 trap 寄存器 */
#define VNCR_ICH_LR0_EL2        0x400   /* GICv3 LR 寄存器组 */
```

> 注意：列表里既有 EL2 寄存器（如 HCR_EL2、VTTBR_EL2、HFG*_EL2），也有 EL1 寄存器（如 SCTLR_EL1、TTBR0_EL1）。
> 后者出现在 VNCR 页面的原因是 ARM 设计：**当 NV1=1（即客户机 hypervisor 是 nVHE 风格）时，L1 看到的"EL1 寄存器"其实是它的 vEL2 私有状态**，所以也走 VNCR 重定向。

---


## 3. KVM/arm64 嵌套虚拟化整体架构

### 3.1 异常等级映射

```
逻辑视角                    KVM 实际部署
─────────────              ────────────────────────────────
L2.vEL0 (用户态)     →     物理 EL0 (HCR_EL2.NV2=1)
L2.vEL1 (内核态)     →     物理 EL1 (HCR_EL2.NV2=1)
L1.vEL0 (用户态)     →     物理 EL0 (HCR_EL2.NV=1, NV2=1)
L1.vEL1 (内核态)     →     物理 EL1 (HCR_EL2.NV=1, NV2=1)
L1.vEL2 (Hypervisor) →     物理 EL1 (HCR_EL2.NV=1, NV2=1, AT=1, TTLB=1)
                            ※ KVM 让 L1 自以为在 EL2，实际仍在 EL1
L0  KVM (host hyp)   →     物理 EL2 (HCR_EL2.E2H=1, TGE=0/1)
```

### 3.2 KVM NV 启用方式

```bash
# 启动参数
kvm-arm.mode=nested
# 或
kvm-arm.mode=nv
```

每个 vCPU 通过 `KVM_ARM_VCPU_INIT` 的特性位开启：

```c
KVM_ARM_VCPU_HAS_EL2       /* 启用 NV，默认 VHE 风格 (E2H=1) */
KVM_ARM_VCPU_HAS_EL2_E2H0  /* 让 L1 看起来不支持 VHE (E2H=0)，需 FEAT_NV1 */
```

### 3.3 L0、L1、L2 的"角色"

| 层 | 物理 EL | 谁在干活 | 看到的 vEL2 是什么 |
|---|---|---|---|
| L0 | EL2 | 真正的 hypervisor，掌握 VTTBR_EL2 / VNCR_EL2 / HCR_EL2 | 自己就是 EL2 |
| L1 | EL1 | Linux/Hyper-V/Xen 等客户机 hypervisor | 假象：通过 NV/NV2 访问"vEL2 寄存器" |
| L2 | EL1/EL0 | 普通客户机内核/用户态 | L1 给它的虚拟硬件 |

### 3.4 KVM NV 的核心难题

1. **L1 想做 EL2 的事，但硬件不让它在 EL2** → 用 NV trap 把所有 EL2 寄存器访问转给 L0 模拟。
2. **trap 太频繁，性能崩溃** → NV2 把"读写性"操作改为内存读写（无 trap），仅在"控制类"配置改变时才 trap。
3. **L1 给 L2 配置的 stage-2 是 vS2，硬件只认一个 stage-2** → L0 维护 shadow stage-2（vS2 ⊗ S2 复合）。
4. **三层之间寄存器副本必须一致** → 复杂的"位置定位 + 上下文切换"机制（本文核心）。

---


## 4. 系统寄存器分类

KVM 把全部系统寄存器按"L2 访问时硬件如何处理 + 物理副本存放位置"分成 **6 大类**。
分类的根据来自三处：

1. `enum vcpu_sysreg` 中的 marker（`__SANITISED_REG_START__`、`__VNCR_START__`）—— 决定**存储位置**
2. `arch/arm64/kvm/sys_regs.c::locate_register()` 的分支 —— 决定**运行时定位**
3. `arch/arm64/kvm/emulate-nested.c::encoding_to_cgt[]` —— 决定**trap 行为**

### 4.1 六大类总览

| 类 | 名称 | 硬件路径 | 内核存储位置 | 典型寄存器 |
|---|---|---|---|---|
| **A** | 普通 EL1 寄存器（VNCR-backed） | NV2 内存重定向，无 trap | `vncr_array[]`，热路径上加载到物理 EL1 sysreg | `SCTLR_EL1` `TTBR0/1_EL1` `TCR_EL1` `MAIR_EL1` `VBAR_EL1` |
| **B** | EL2 控制寄存器（trap 类） | trap 到 L0 模拟 | `sys_regs[]`（非 VNCR 部分） | `VTTBR_EL2` `VTCR_EL2` `CPTR_EL2` `MDCR_EL2`(部分位) |
| **C** | EL2 寄存器（VNCR-backed） | NV2 内存重定向，无 trap | `vncr_array[]` | `HCR_EL2` `HCRX_EL2` `HFGRTR_EL2` 等 FGT 全家、`VPIDR/VMPIDR_EL2` |
| **D** | EL2 "shadow EL1" 寄存器 | trap → 由 KVM 写到物理 EL1 | `sys_regs[]`，运行时通过 `_EL12` 别名加载 | `SCTLR_EL2` `TTBR0/1_EL2` `TCR_EL2` `VBAR_EL2` `ESR_EL2` `MAIR_EL2` |
| **E** | ID/特性寄存器 | trap | `kvm_arch.id_regs[]`（VM 全局） | `MIDR_EL1` `ID_AA64*_EL1` `CTR_EL0` `REVIDR_EL1` |
| **F** | 子系统专属寄存器 | 由各自 trap 控制位（CPTR/MDCR/PMU）独立处理 | 子系统局部存储 | PMU/Debug/SVE/MTE 类（本文不展开） |

### 4.2 类别归属边界（来自 `enum vcpu_sysreg`）

```c
/* arch/arm64/include/asm/kvm_host.h */
enum vcpu_sysreg {
    __INVALID_SYSREG__,
    MPIDR_EL1, CLIDR_EL1, CSSELR_EL1, ...    /* 类 E：ID 类 / 普通固定值 */
    PMCR_EL0, PMSELR_EL0, ...                /* 类 F：PMU */
    APIAKEYLO_EL1, ...                       /* 类 F：PAuth */
    ACTLR_EL2, CPTR_EL2, HACR_EL2, ...       /* 类 B：EL2 控制 */
    SCTLR_EL2, ...                           /* 类 D：EL2 shadow EL1（特殊：还需 sanitisation） */
    MARKER(__SANITISED_REG_START__),         /* ↑ 之后的寄存器要走 RES0/RES1 mask */
    SCTLR_EL2, TCR2_EL2, MDCR_EL2, ...       /* 类 D + 需要特性裁剪 */
    MARKER(__VNCR_START__),                  /* ↑ 之后的寄存器存在 vncr_array[] 中 */
    VNCR(SCTLR_EL1), VNCR(TTBR0_EL1), ...    /* 类 A：VNCR-backed EL1 */
    VNCR(HCR_EL2), VNCR(HCRX_EL2), ...       /* 类 C：VNCR-backed EL2 */
    NR_SYS_REGS
};
```

> 注意一个细节：`SCTLR_EL2` 既出现在 marker 之前（用于非 NV、用 sys_regs[] 存）也出现在 marker 之后吗？
> 答：实际上 `SCTLR_EL2` 跨越 `__SANITISED_REG_START__`（即位于 marker **之后**），所以它会被 `kvm_vcpu_apply_reg_masks` 处理，但在 `__VNCR_START__` 之前 → 存在 `sys_regs[]` 而非 `vncr_array[]`。

---


## 5. 存储布局与核心数据结构

### 5.1 vCPU 上下文存储结构

```c
/* arch/arm64/include/asm/kvm_host.h */

struct kvm_cpu_context {
    struct user_pt_regs regs;          /* x0-x30, sp, pc, pstate */
    u64  spsr_abt, spsr_und, spsr_irq, spsr_fiq;
    struct user_fpsimd_state fp_regs;

    u64  sys_regs[NR_SYS_REGS];        /* (1) 主存储数组 */
    struct kvm_vcpu *__hyp_running_vcpu;

    u64 *vncr_array;                   /* (2) 4kB 对齐页面，VNCR-backed 寄存器存这里 */
};

struct kvm_vcpu_arch {
    struct kvm_cpu_context ctxt;       /* 主上下文 */
    void *sve_state;
    enum fp_type fp_type;
    unsigned int sve_max_vl;
    struct kvm_s2_mmu *hw_mmu;         /* 当前生效的 stage-2 MMU（shadow） */
    u64 hcr_el2, hcrx_el2, mdcr_el2;   /* L0 实际写到硬件的值（已合并 L1 视图） */
    struct { u64 r, w; } fgt[__NR_FGT_GROUP_IDS__];  /* 合并后的 FGT 值 */
    struct kvm_vcpu_fault_info fault;
    /* ... */
    struct vncr_tlb *vncr_tlb;         /* L1 自己的 VNCR 页面伪 TLB */
};

struct kvm_arch {
    struct kvm_s2_mmu mmu;
    u64 fgu[__NR_FGT_GROUP_IDS__];     /* 全 VM Fine-Grained UNDEF 掩码 */
    struct kvm_s2_mmu *nested_mmus;    /* 多个 shadow stage-2 MMU 池 */
    u64 id_regs[KVM_ARM_ID_REG_NUM];   /* VM 范围的 ID 寄存器值（类 E） */
    struct kvm_sysreg_masks *sysreg_masks;  /* RES0/RES1 掩码表 */
    atomic_t vncr_map_count;
};
```

### 5.2 三种存储位置

```
+--------------------------------------------------------------------+
| vcpu->arch.ctxt.sys_regs[NR_SYS_REGS]                              |
| ├── [MPIDR_EL1..]  非 VNCR、非 marker 区间                         |
| ├── [SCTLR_EL2..CNTHCTL_EL2]  __SANITISED_REG_START__ 之后         |
| └── [__VNCR_START__..]  这部分**不使用**这个数组!                  |
+--------------------------------------------------------------------+
| vcpu->arch.ctxt.vncr_array[NR_SYS_REGS - __VNCR_START__]           |
| ├── 4kB 对齐独立页面（专门为 VNCR 重定向准备）                     |
| ├── array[i] 对应 vcpu_sysreg = __VNCR_START__ + i                 |
| ├── 字节偏移 = i * 8 = VNCR_<reg> 宏值                             |
| └── 例如 array[(VNCR_HCR_EL2/8)] 即 array[15] 存 HCR_EL2           |
+--------------------------------------------------------------------+
| 物理 CPU 系统寄存器（仅当 SYSREGS_ON_CPU 标志置位）                |
| └── 通过 _EL12 别名直接访问，是"热数据"                             |
+--------------------------------------------------------------------+
```

### 5.3 `__vcpu_sys_reg(v, r)` 宏的统一访问路径

```c
/* arch/arm64/include/asm/kvm_host.h */
static inline u64 *___ctxt_sys_reg(const struct kvm_cpu_context *ctxt, int r)
{
#if !defined (__KVM_NVHE_HYPERVISOR__)
    if (unlikely(cpus_have_final_cap(ARM64_HAS_NESTED_VIRT) &&
                 r >= __VNCR_START__ && ctxt->vncr_array))
        return &ctxt->vncr_array[r - __VNCR_START__];
#endif
    return (u64 *)&ctxt->sys_regs[r];
}

#define __vcpu_sys_reg(v, r)                                       \
({                                                                  \
    const struct kvm_cpu_context *ctxt = &(v)->arch.ctxt;           \
    u64 __v = ctxt_sys_reg(ctxt, (r));                              \
    if (vcpu_has_nv((v)) && (r) >= __SANITISED_REG_START__)         \
        __v = kvm_vcpu_apply_reg_masks((v), (r), __v);              \
    __v;                                                            \
})
```

→ **关键点**：所有内核内部代码都通过 `__vcpu_sys_reg()` 访问寄存器，无须关心它实际在 `sys_regs[]` 还是 `vncr_array[]`。VNCR-backed 寄存器对内核代码是透明的。

### 5.4 `vcpu_read_sys_reg / vcpu_write_sys_reg` 的位置定位

`locate_register()` 结合 `is_hyp_ctxt(vcpu)` 给出 5 种位置（`SR_LOC_*` 标志位）：

```
SR_LOC_MEMORY   = 0          → 仅在内存中
SR_LOC_LOADED   = BIT(0)     → 在物理寄存器上
SR_LOC_MAPPED   = BIT(1)     → 在物理 EL1 sysreg 上（用别的寄存器编号）
SR_LOC_XLATED   = BIT(2)     → 需要值转换（E2H=0 时 EL2/EL1 字段不一致）
SR_LOC_SPECIAL  = BIT(3)     → 自定义路径（仅 CNTHCTL_EL2）
```

定位逻辑（简化）：

```
                       ┌──────────────────────────────┐
                       │ vcpu_get_flag(SYSREGS_ON_CPU)│
                       │   ── false → SR_LOC_MEMORY   │
                       └──────────────┬───────────────┘
                                      │ true
                                      ▼
                ┌─────────────────────────────────────┐
                │ 寄存器在 enum vcpu_sysreg 哪个区间？│
                └─┬─────┬──────────────────────────┬──┘
                  │     │                          │
       EL1 类     │     │ EL2 "shadow EL1" 类      │ 其他 EL2 控制类
    (TTBR0_EL1)   │     │ (SCTLR_EL2, VBAR_EL2)    │ (HCR_EL2)
                  ▼     ▼                          ▼
       hyp_ctxt? ── false→SR_LOC_LOADED  hyp_ctxt? ── false→SR_LOC_MEMORY
              true→SR_LOC_MEMORY              true→SR_LOC_LOADED|MAPPED[|XLATED]
```

---


## 6. NV trap 路由机制

### 6.1 顶层异常分发

```c
/* arch/arm64/kvm/handle_exit.c */
static exit_handle_fn arm_exit_handlers[] = {
    [ESR_ELx_EC_SYS64]      = kvm_handle_sys_reg,      /* MSR/MRS trap */
    [ESR_ELx_EC_DABT_LOW]   = kvm_handle_guest_abort,  /* L2 stage-2 fault */
    [ESR_ELx_EC_DABT_CUR]   = kvm_handle_vncr_abort,   /* L1 访问 VNCR 页失败 */
    [ESR_ELx_EC_ERET]       = kvm_handle_eret,         /* ERET 指令模拟 */
    [ESR_ELx_EC_HVC64]      = handle_hvc,
    [ESR_ELx_EC_FP_ASIMD]   = kvm_handle_fpasimd,
    [ESR_ELx_EC_SVE]        = handle_sve,
    [ESR_ELx_EC_PAC]        = kvm_handle_ptrauth,
    [ESR_ELx_EC_OTHER]      = handle_other,            /* LS64, ST64BV, TSB CSYNC 等 */
    /* ... */
};
```

### 6.2 `kvm_handle_sys_reg` → `triage_sysreg_trap`

L2 执行 `MSR/MRS` 指令并被硬件 trap 到 EL2 后：

```
kvm_handle_sys_reg(vcpu)
  │
  └─► triage_sysreg_trap(vcpu, &sr_idx)        [emulate-nested.c:2542]
        │
        ├─ tc = xarray_load(encoding)          /* CGT/FGT 静态表索引 */
        │
        ├─ 若 vm.fgu[group] & bit 命中
        │     → kvm_inject_undefined()         /* L0 强制 UNDEF */
        │
        ├─ 若 !vcpu_has_nv() → 跳到 local 分支（普通 KVM 模拟）
        │
        ├─ 若 is_hyp_ctxt(vcpu) && !host_el0
        │     → 跳到 local（L1 自己访问自己的，由 KVM 直接处理）
        │
        ├─ 检查 FGT bit:
        │     fgtreg = HFGRTR_EL2 / HFGWTR_EL2 / HFGITR_EL2 / ...
        │     val = __vcpu_sys_reg(vcpu, fgtreg)   /* L1 设的 FGT */
        │     若 (val & BIT(tc.bit)) → 跳到 inject
        │
        ├─ b = compute_trap_behaviour(vcpu, tc.cgt)
        │     遍历 CGT 组合，对每个 CGT_HCR_*：
        │       reg_val = __vcpu_sys_reg(vcpu, HCR_EL2 / MDCR_EL2 / ...)
        │       若控制位被 L1 置 1，累加 BEHAVE_FORWARD_RW/READ/WRITE
        │
        ├─ 若 b 与访问方向匹配 → 跳到 inject
        │
        ▼ local
        return false; /* 让 kvm_handle_sys_reg 走 sys_reg_descs 本地模拟 */

      inject:
        kvm_inject_nested_sync(vcpu, esr);       /* 把异常反射给 L1 vEL2 */
        return true;
```

### 6.3 CGT（Coarse-Grained Trap）控制位映射

`enum cgt_group_id` 定义了所有"粗粒度 trap"组：

```c
/* arch/arm64/kvm/emulate-nested.c */
enum cgt_group_id {
    CGT_HCR_TID1, CGT_HCR_TID2, CGT_HCR_TID3,    /* ID 寄存器读 trap */
    CGT_HCR_IMO, CGT_HCR_FMO,                    /* GIC 中断路由 trap */
    CGT_HCR_TIDCP,                               /* IMPDEF 寄存器 */
    CGT_HCR_TACR,                                /* ACTLR_EL1 */
    CGT_HCR_TSW, CGT_HCR_TPC, CGT_HCR_TPU,       /* 缓存维护 */
    CGT_HCR_TTLB, CGT_HCR_TTLBIS, CGT_HCR_TTLBOS,/* TLBI */
    CGT_HCR_TVM, CGT_HCR_TRVM,                   /* 虚拟内存控制写/读 */
    CGT_HCR_TDZ,                                 /* DC ZVA */
    CGT_HCR_TLOR,                                /* LORegions */
    CGT_HCR_NV, CGT_HCR_NV1_nNV2, CGT_HCR_AT,    /* NV 自身控制 */
    CGT_MDCR_TPM, CGT_MDCR_TDE, CGT_MDCR_TDA,    /* 调试/PMU */
    CGT_CPTR_TAM, CGT_CPTR_TCPAC,                /* CPACR/CPTR */
    /* 复合控制（多位组合）*/
    CGT_HCR_TVM_TRVM, CGT_HCR_TID2_TID4, ...
    /* 复杂条件（callback 评估）*/
    CGT_CNTHCTL_EL1PCTEN, ...
};

static const struct trap_bits coarse_trap_bits[] = {
    [CGT_HCR_TVM] = {
        .index     = HCR_EL2,
        .value     = HCR_TVM,
        .mask      = HCR_TVM,
        .behaviour = BEHAVE_FORWARD_WRITE,
    },
    [CGT_HCR_TID3] = {
        .index     = HCR_EL2,
        .value     = HCR_TID3,
        .mask      = HCR_TID3,
        .behaviour = BEHAVE_FORWARD_READ,
    },
    /* ... 几十个条目 ... */
};
```

### 6.4 寄存器 → CGT 组的映射（节选）

```c
static const struct encoding_to_trap_config encoding_to_cgt[] = {
    SR_TRAP(SYS_REVIDR_EL1,    CGT_HCR_TID1),
    SR_TRAP(SYS_CTR_EL0,       CGT_HCR_TID2),
    SR_RANGE_TRAP(SYS_ID_PFR0_EL1, sys_reg(3,0,0,7,7), CGT_HCR_TID3),
    SR_TRAP(SYS_SCTLR_EL1,     CGT_HCR_TVM_TRVM),
    SR_TRAP(SYS_TTBR0_EL1,     CGT_HCR_TVM_TRVM),
    SR_TRAP(SYS_TTBR1_EL1,     CGT_HCR_TVM_TRVM),
    SR_TRAP(SYS_TCR_EL1,       CGT_HCR_TVM_TRVM),
    SR_TRAP(SYS_MAIR_EL1,      CGT_HCR_TVM_TRVM),
    SR_TRAP(SYS_VBAR_EL1,      CGT_HCR_TVM_TRVM),
    SR_TRAP(SYS_ACTLR_EL1,     CGT_HCR_TACR),
    SR_TRAP(OP_TLBI_VMALLE1,   CGT_HCR_TTLB),
    SR_TRAP(OP_AT_S1E1R,       CGT_HCR_AT),
    SR_TRAP(SYS_ICC_SGI1R_EL1, CGT_HCR_IMO_FMO_ICH_HCR_TC),
    /* ... */
};
```

### 6.5 异常注入到 L1 vEL2

```c
/* arch/arm64/kvm/emulate-nested.c */
int kvm_inject_nested_sync(struct kvm_vcpu *vcpu, u64 esr_el2)
{
    return kvm_inject_nested(vcpu, esr_el2, except_type_sync);
}

/* 内部完成: */
//   - vcpu_write_sys_reg(vcpu, esr_el2, ESR_EL2)
//   - vcpu_write_sys_reg(vcpu, current_pc, ELR_EL2)
//   - vcpu_write_sys_reg(vcpu, current_pstate, SPSR_EL2)
//   - 设置 PENDING_EXCEPTION + EXCEPT_AA64_EL2_SYNC iflags
//   - vcpu->arch.ctxt.regs.pc = vEL2 vector(VBAR_EL2 + offset)
//   - vcpu->arch.ctxt.regs.pstate = EL2 mode
```

`__kvm_adjust_pc` 在下次进入 guest 前会真正应用这些状态，让 L1 在它的 vEL2 vector 处恢复执行。

---


## 7. VNCR 内存映射机制

KVM 的 NV2 实现里实际存在 **两个不同的 VNCR 页面**，理解它们的差别是理解整个机制的关键。

### 7.1 两个 VNCR 页面对照

```
┌────────────────────────────────────────────────────────────────────┐
│ 页面 P1: KVM 私有 VNCR 页面                                        │
│   分配方:  L0 KVM (kvm_vcpu_init_nested in nested.c:69)            │
│   位置:    vcpu->arch.ctxt.vncr_array (host kernel VA)             │
│   用途:    L1 自己以为它在写 vEL2 寄存器时，实际写到这里           │
│   何时使用:L1 (vEL2) 运行期间                                      │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│ 页面 P2: L1 客户机自己的 VNCR 页面                                 │
│   分配方:  L1 hypervisor 自己（在它的 vEL2 内存空间中分配一页）    │
│   位置:    L1 写到自己 vVNCR_EL2 的那个 EL2&0 VA                   │
│   用途:    当 L1 让 L2 启用 NV2 时，L2 的 vEL2 寄存器访问指向这里  │
│   KVM 视角:仅当 L2 运行时通过 fixmap 临时映射到 EL2 VA 空间        │
└────────────────────────────────────────────────────────────────────┘
```

### 7.2 P1 — KVM 的 VNCR 页面

```c
/* arch/arm64/kvm/nested.c:78 */
if (!vcpu->arch.ctxt.vncr_array)
    vcpu->arch.ctxt.vncr_array = (u64 *)__get_free_page(
        GFP_KERNEL_ACCOUNT | __GFP_ZERO);
```

布局严格遵循 ARM 架构规定（`vncr_mapping.h`）：

```
ctxt.vncr_array → ┌──────────────────────────┐ +0x000
                  │ (reserved)               │
                  │ VTTBR_EL2 @ 0x020        │
                  │ VTCR_EL2  @ 0x040        │
                  │ VMPIDR_EL2 @ 0x050       │
                  │ HCR_EL2   @ 0x078        │
                  │ HCRX_EL2  @ 0x0A0        │
                  │ VNCR_EL2  @ 0x0B0        │  ← 自指
                  │ SCTLR_EL1 @ 0x110        │
                  │ ...                      │
                  │ TTBR0_EL1 @ 0x200        │
                  │ TTBR1_EL1 @ 0x210        │
                  │ VBAR_EL1  @ 0x250        │
                  │ ...                      │
                  │ HFGRTR_EL2 @ 0x1B8       │
                  │ ICH_LR0_EL2 @ 0x400      │
                  │ ...                      │
                  └──────────────────────────┘ +0xFFF
```

### 7.3 P2 — L1 自己的 VNCR 页面

L1 hypervisor 在它的内存空间分配一页，把这个页面的 EL2&0 VA 写入它的（虚拟）VNCR_EL2 寄存器。

KVM 用 `vncr_tlb` 缓存这个映射的翻译结果（一个伪 TLB，每 vCPU 一项）：

```c
struct vncr_tlb {
    bool      valid;
    u64       gva;          /* L1 写入 vVNCR_EL2 的原始 VA */
    phys_addr_t hpa;        /* 走完 L1 stage-1 + L0 stage-2 后的 host PA */
    int       cpu;          /* 当前 fixmap 在哪个 CPU */
    struct s1_walk_info wi; /* 翻译信息: regime=TR_EL20, ASID, etc. */
    struct s1_walk_result wr;
};
```

### 7.4 fixmap 槽位

```c
/* arch/arm64/include/asm/fixmap.h */
enum fixed_addresses {
    /* ... */
    FIX_VNCR_END,
    FIX_VNCR = FIX_VNCR_END + NR_CPUS,   /* 每 CPU 一个槽位 */
    /* ... */
};

#define vncr_fixmap(cpu)  (FIX_VNCR_END + (cpu))
```

每个物理 CPU 都有一个 fixmap 槽位用于映射"当前正在该 CPU 上运行的 vCPU 的 P2 页面"。

### 7.5 硬件 VNCR_EL2 在两种情境下的取值

```c
/* arch/arm64/kvm/hyp/vhe/switch.c::__compute_hcr() — 每次进入 guest 前调用 */

if (is_hyp_ctxt(vcpu)) {
    /* 情境 1: 即将运行 L1 (vEL2) */
    hcr |= HCR_NV | HCR_NV2 | HCR_AT | HCR_TTLB;
    if (!vcpu_el2_e2h_is_set(vcpu))
        hcr |= HCR_NV1;

    /* 物理 VNCR_EL2 = P1 (KVM 自己的页面) */
    write_sysreg_s(vcpu->arch.ctxt.vncr_array, SYS_VNCR_EL2);

    host_data_set_flag(VCPU_IN_HYP_CONTEXT);
} else {
    /* 情境 2: 即将运行 L2 (vEL1) */
    guest_hcr = __vcpu_sys_reg(vcpu, HCR_EL2);   /* L1 给 L2 设的 vHCR */

    if (guest_hcr & HCR_NV) {
        /* L1 让 L2 也走 NV2 → 物理 VNCR_EL2 = P2 (L1 的页面) via fixmap */
        u64 va = __fix_to_virt(vncr_fixmap(smp_processor_id()));
        va |= __vcpu_sys_reg(vcpu, VNCR_EL2) & GENMASK(PAGE_SHIFT-1, 0);
        write_sysreg_s(va, SYS_VNCR_EL2);

        guest_hcr |= HCR_NV2;
    }

    host_data_clear_flag(VCPU_IN_HYP_CONTEXT);
}
BUG_ON(host_data_test_flag(VCPU_IN_HYP_CONTEXT) &&
       host_data_test_flag(L1_VNCR_MAPPED));    /* 互斥! */
```

### 7.6 L1 VNCR 页面的按需建立

```
┌─ L1 写 vVNCR_EL2 = 0xffff_8000_xxxx_x000 ─────────────────────────┐
│                                                                   │
│  (这是一次 NV trap：L1 在 EL1 执行 MSR VNCR_EL2, x0)              │
│  → kvm_handle_sys_reg → 走 sys_regs.c 本地模拟                    │
│  → vcpu_write_sys_reg(vcpu, val, VNCR_EL2)                        │
│    存到 vncr_array[VNCR_VNCR_EL2/8]                               │
│  → 不立即建立映射，等 L1 实际进入 L2 后再处理                     │
└───────────────────────────────────────────────────────────────────┘

┌─ L1 ERET 进入 L2 ─────────────────────────────────────────────────┐
│                                                                   │
│  L1 执行 ERET (来自 vEL2 → vEL1) 触发 NV trap                     │
│  → kvm_handle_eret → kvm_emulate_nested_eret                      │
│    切换 vcpu state 到"运行 L2"                                    │
│  → 下次进入：__compute_hcr 看到 !is_hyp_ctxt && (guest_hcr & NV)  │
│    尝试设置 VNCR_EL2 = fixmap_va，但 fixmap 还没填充              │
└───────────────────────────────────────────────────────────────────┘

┌─ L2 执行 MSR HCR_EL2, x0 ─────────────────────────────────────────┐
│                                                                   │
│  L2 在物理 EL1 + NV2=1 → 硬件试图访问 VNCR_EL2 + 0x078            │
│  fixmap 槽位是空的 → DABT_CUR 异常，ESR 含 ESR_ELx_VNCR=1         │
│  → kvm_handle_vncr_abort                                          │
│    → kvm_translate_vncr:                                          │
│        invalidate_vncr (清掉旧的)                                 │
│        va = read_vncr_el2(vcpu)  /* L1 设的 vVNCR_EL2 */          │
│        __kvm_translate_va(vcpu, &wi, &wr, va)                     │
│            /* 走 L1 stage-1 (TR_EL20 regime) → IPA */             │
│        gfn_to_memslot → __kvm_faultin_pfn → host PFN              │
│        vt->valid=true; vt->hpa=pfn<<12; vt->gva=va                │
│        kvm_make_request(KVM_REQ_MAP_L1_VNCR_EL2, vcpu)            │
│        return 0;                                                  │
│  → handle_exit 回到 vcpu 主循环                                   │
└───────────────────────────────────────────────────────────────────┘

┌─ 下次进入 L2 ─────────────────────────────────────────────────────┐
│                                                                   │
│  vcpu_run 检查请求 → kvm_check_request(KVM_REQ_MAP_L1_VNCR_EL2)   │
│  → kvm_map_l1_vncr:                                               │
│      __set_fixmap(vncr_fixmap(cpu), vt->hpa, prot)                │
│      host_data_set_flag(L1_VNCR_MAPPED)                           │
│      atomic_inc(&kvm->arch.vncr_map_count)                        │
│  → __compute_hcr 写物理 VNCR_EL2 = fixmap_va                      │
│  → ERET 进入 L2                                                   │
│  → L2 那条 MSR HCR_EL2 重新执行：硬件直接重定向到 P2 页面，无 trap│
└───────────────────────────────────────────────────────────────────┘
```

### 7.7 失效时机

```c
/* arch/arm64/kvm/nested.c */

/* 1. vcpu 离开 CPU */
void kvm_vcpu_put_hw_mmu(struct kvm_vcpu *vcpu)
{
    if (host_data_test_flag(L1_VNCR_MAPPED)) {
        clear_fixmap(vncr_fixmap(vcpu->arch.vncr_tlb->cpu));
        vcpu->arch.vncr_tlb->cpu = -1;
        host_data_clear_flag(L1_VNCR_MAPPED);
    }
}

/* 2. L1 修改 stage-1 (导致 IPA 变化) - MMU notifier */
static void kvm_invalidate_vncr_ipa(struct kvm *kvm, u64 start, u64 end)
{
    kvm_for_each_vcpu(i, vcpu, kvm) {
        struct vncr_tlb *vt = vcpu->arch.vncr_tlb;
        if (vt cached IPA in [start, end))
            invalidate_vncr(vt);
    }
}

/* 3. L1 执行 TLBI VA */
static void invalidate_vncr_va(struct kvm *kvm, scope) { ... }
```

---


## 8. 上下文保存/恢复路径

KVM/arm64 的系统寄存器切换分两层：

```
┌─────────────────────────────────────────────────────────────┐
│ 慢路径: vcpu_load / vcpu_put                                │
│   • 进程级别（vCPU 调度）                                   │
│   • 仅 EL1 / vEL2 状态相关的寄存器                          │
│   • 通过 _EL12 别名直接读写物理 sysreg                      │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ 快路径: __kvm_vcpu_run_vhe（每次进入 guest）                │
│   • 仅"common state" + "EL2 return state"                   │
│   • 主要是 PC/PSTATE/MDSCR_EL1/POR_EL0 + trap 控制寄存器    │
│   • 不重新加载所有 EL1 寄存器（昂贵）                       │
└─────────────────────────────────────────────────────────────┘
```

### 8.1 慢路径：`__vcpu_load_switch_sysregs`

```c
/* arch/arm64/kvm/hyp/vhe/sysreg-sr.c:212 */
void __vcpu_load_switch_sysregs(struct kvm_vcpu *vcpu)
{
    /* 1. 保存 host 用户态 (TPIDR_EL0/RO_EL0) */
    __sysreg_save_user_state(host_ctxt);

    /* 2. NV 特殊：因为 vEL2↔vEL1 转换可能没有 vcpu_put/load 介入 */
    if (vcpu_has_nv(vcpu))  dsb(nsh);

    /* 3. 加载 guest 用户态 + 32bit + MPAM */
    __sysreg32_restore_state(vcpu);
    __sysreg_restore_user_state(guest_ctxt);

    /* 4. 关键分叉：根据 is_hyp_ctxt 决定加载哪一套寄存器 */
    if (unlikely(is_hyp_ctxt(vcpu))) {
        __sysreg_restore_vel2_state(vcpu);   /* 加载 L1 的 vEL2 状态 */
    } else {
        u64 midr, mpidr;
        if (vcpu_has_nv(vcpu)) {
            midr  = ctxt_sys_reg(guest_ctxt, VPIDR_EL2);
            mpidr = ctxt_sys_reg(guest_ctxt, VMPIDR_EL2);
        } else {
            midr  = ctxt_midr_el1(guest_ctxt);
            mpidr = ctxt_sys_reg(guest_ctxt, MPIDR_EL1);
        }
        __sysreg_restore_el1_state(guest_ctxt, midr, mpidr);  /* 加载 vEL1 */
    }

    vcpu_set_flag(vcpu, SYSREGS_ON_CPU);
}
```

### 8.2 vEL2 状态加载详情

```c
/* arch/arm64/kvm/hyp/vhe/sysreg-sr.c:85 */
static void __sysreg_restore_vel2_state(struct kvm_vcpu *vcpu)
{
    write_sysreg(par_el1_val,    par_el1);
    write_sysreg(tpidr_el1_val,  tpidr_el1);
    write_sysreg(midr_val,       vpidr_el2);   /* L1 看到的 MIDR */
    write_sysreg(mpidr_val,      vmpidr_el2);  /* L1 看到的 MPIDR */

    /* vEL2 寄存器写到物理 EL1 sysregs（VHE 在 EL2 下访问 EL1 寄存器名 == 自己 EL1 状态） */
    write_sysreg_el1(MAIR_EL2_val,    SYS_MAIR);
    write_sysreg_el1(VBAR_EL2_val,    SYS_VBAR);
    write_sysreg_el1(CONTEXTIDR_EL2_val, SYS_CONTEXTIDR);

    if (vcpu_el2_e2h_is_set(vcpu)) {
        /* E2H=1: EL2 寄存器布局与 EL1 兼容，直接写 */
        write_sysreg_el1(SCTLR_EL2_val,  SYS_SCTLR);
        write_sysreg_el1(CPTR_EL2_val,   SYS_CPACR);
        write_sysreg_el1(TTBR0_EL2_val,  SYS_TTBR0);
        write_sysreg_el1(TTBR1_EL2_val,  SYS_TTBR1);
        write_sysreg_el1(TCR_EL2_val,    SYS_TCR);
        write_sysreg_el1(CNTHCTL_EL2_val,SYS_CNTKCTL);
    } else {
        /* E2H=0: EL2 寄存器布局不同，需要值转换 */
        write_sysreg_el1(translate_sctlr_el2_to_sctlr_el1(...), SYS_SCTLR);
        write_sysreg_el1(translate_cptr_el2_to_cpacr_el1(...), SYS_CPACR);
        write_sysreg_el1(translate_ttbr0_el2_to_ttbr0_el1(...), SYS_TTBR0);
        write_sysreg_el1(translate_tcr_el2_to_tcr_el1(...), SYS_TCR);
    }

    write_sysreg_el1(ESR_EL2_val,   SYS_ESR);
    write_sysreg_el1(FAR_EL2_val,   SYS_FAR);
    write_sysreg(SP_EL2_val,        sp_el1);
    write_sysreg_el1(ELR_EL2_val,   SYS_ELR);
    write_sysreg_el1(SPSR_EL2_val,  SYS_SPSR);
    /* ... TCR2/PIR/POR 等可选 ... */
}
```

### 8.3 快路径：`__kvm_vcpu_run_vhe`

```c
/* arch/arm64/kvm/hyp/vhe/switch.c:572 */
static int __kvm_vcpu_run_vhe(struct kvm_vcpu *vcpu)
{
    fpsimd_lazy_switch_to_guest(vcpu);
    sysreg_save_host_state_vhe(host_ctxt);    /* 保存少量 host: MDSCR_EL1, POR_EL0 */

    __activate_traps(vcpu);                    /* 程式化 HCR/MDCR/CPTR/FGT/VNCR_EL2/VTTBR */
    __kvm_adjust_pc(vcpu);                     /* 应用 pending exception */
    sysreg_restore_guest_state_vhe(guest_ctxt); /* 写 ELR_EL2=PC, SPSR_EL2=PSTATE */
    __debug_switch_to_guest(vcpu);

    do {
        exit_code = __guest_enter(vcpu);       /* ERET 到 guest */
    } while (fixup_guest_exit(vcpu, &exit_code));

    sysreg_save_guest_state_vhe(guest_ctxt);   /* 读回 ELR_EL2/SPSR_EL2 */
    __deactivate_traps(vcpu);
    sysreg_restore_host_state_vhe(host_ctxt);
    isb();
    fpsimd_lazy_switch_to_host(vcpu);
    return exit_code;
}
```

### 8.4 PSTATE 转换（vEL2 → 物理 EL1）

```c
/* arch/arm64/kvm/hyp/include/hyp/sysreg-sr.h::to_hw_pstate() */
static inline u64 to_hw_pstate(const struct kvm_cpu_context *ctxt)
{
    u64 mode = ctxt->regs.pstate & (PSR_MODE_MASK | PSR_MODE32_BIT);
    switch (mode) {
    case PSR_MODE_EL2t: mode = PSR_MODE_EL1t; break;
    case PSR_MODE_EL2h: mode = PSR_MODE_EL1h; break;
    }
    return (ctxt->regs.pstate & ~(PSR_MODE_MASK | PSR_MODE32_BIT)) | mode;
}
```

> **关键技巧**：vcpu 内存里 PSTATE.M 可能是 EL2h（L1 自以为在 EL2），但写到硬件 SPSR_EL2 之前必须改成 EL1h，因为 L1 实际跑在物理 EL1。

---


## 9. 每类系统寄存器 L2 访问情景分析

为每一类挑选一个典型寄存器，给出从 L2 执行 MSR/MRS 起的完整生命周期。

---

### 9.1 类 A 情景：L2 写 `TTBR0_EL1`（VNCR-backed 普通 EL1 寄存器）

**寄存器属性**:
- `enum vcpu_sysreg`: `__VNCR_START__ + (VNCR_TTBR0_EL1/8) = __VNCR_START__ + 64`
- VNCR-backed (offset 0x200)
- `encoding_to_cgt[]`: `CGT_HCR_TVM_TRVM` （L1 可以选择是否 trap）

**前提**：L1 在它的 vHCR_EL2 中没有设 TVM=1（不要求 trap L2 的 TTBR0_EL1 写）。

**完整生命周期**:

```
┌─[L2 (vEL1)]───────────────────────────────────────────────────────┐
│ msr ttbr0_el1, x0                                                  │
└──────────────────────┬─────────────────────────────────────────────┘
                       │
        当前硬件状态:
          物理 EL = EL1 (L2 实际跑在物理 EL1)
          HCR_EL2.NV2 = 1 (L0 强制设的)
          VNCR_EL2 = fixmap_va(L1 的 P2 页面)
                       │
                       ▼
┌─[硬件 (CPU MMU)]──────────────────────────────────────────────────┐
│ NV2 不重定向 EL1 寄存器（除非 NV1=1，这里 L1 是 VHE 风格 NV1=0）  │
│ → 这是个普通的 EL1 寄存器写, 没有 TVM trap                         │
│ → 直接写到物理 TTBR0_EL1                                           │
│ → 一切如常，无 trap，无 VM exit                                    │
└────────────────────────────────────────────────────────────────────┘
```

**等等！** 这里有个关键差异：当 L1 是 VHE 风格 (E2H=1) 且 NV1=0 时，L2 的 EL1 寄存器走**普通路径**，不进入 VNCR 重定向。VNCR_EL1 那部分在 vncr_mapping.h 中是为 NV1=1 (即 L1 是 nVHE 风格) 准备的。

**L0 视角**: 完全不感知此次访问。

**L1 视角**: 等到 L1 因为其它原因获得 CPU（比如 L2 时钟中断 trap 到 L1）后，它的代码里读 `vTTBR0_EL1`：

```
┌─[L1 (vEL2)]───────────────────────────────────────────────────────┐
│ mrs x0, ttbr0_el1                                                  │
└──────────────────────┬─────────────────────────────────────────────┘
                       │
        当前硬件状态: HCR_EL2.NV2=1, VNCR_EL2=ctxt.vncr_array
                       │
                       ▼
                  L1 物理上在 EL1，访问 TTBR0_EL1
                  这是 NV1=0 情况下的"自己的 EL1 寄存器"
                  → 读到的是上次 vcpu_load 时由 KVM 写入的值
                  → 该值是 L1 自己之前设的 vTTBR0_EL1（来自 sys_regs[]）
                  → 与 L2 设的 ttbr0_el1 是不同的 vCPU 视图
```

**vcpu_load 时如何切换**: 参见 `__sysreg_restore_el1_state`：

- 如果 `is_hyp_ctxt(vcpu)==true`（L1 在 vEL2）: 加载 L1 的 vEL1 状态到物理 EL1。  
- 如果 `is_hyp_ctxt(vcpu)==false`（L2 在 vEL1）: 加载 L2 的 vEL1 状态到物理 EL1。

```
存储位置时序图（A 类）：

  L1 vEL2 时             L2 vEL1 时
  ─────────              ──────────
  物理 TTBR0_EL1  ←      物理 TTBR0_EL1   ← 实际硬件
  = L1 自己设的           = L2 设的 (此时 L1 的状态已被换出到内存)

  vcpu->arch.ctxt.sys_regs[TTBR0_EL1]    ← L2 的 vEL1 备份（vcpu_put 时保存）
  vcpu->arch.ctxt.vncr_array[...]         ← (L1 自己的 vTTBR0_EL1, 当 NV1=1)
```

> 总结：A 类**完全无 trap**，是 NV2 性能优势的核心。

---

### 9.2 类 B 情景：L2 写 `VTTBR_EL2`（EL2 控制类，硬件资源）

**寄存器属性**:
- `enum vcpu_sysreg`: `VTTBR_EL2`（在 `__VNCR_START__` 之后，所以是 VNCR-backed）
- VNCR offset 0x020
- 但其值含义是"L1 给 L2 设的 stage-2 基址"，需要 KVM 同步到 shadow stage-2

**前提**：L2 是 L1 的客户机；L1 自己想访问它的 vVTTBR_EL2 来管理 L2 的 stage-2。

**关键**：这里 "L2 写 VTTBR_EL2" 这个说法本身就有问题——通常 **L1**（不是 L2）才会写 VTTBR_EL2。L2 没有理由写它（除非 L2 也是个 hypervisor，即三层嵌套）。

让我们改换情景为 **L1 写 vVTTBR_EL2** （它是从 L0 的视角等同"vEL1 上的某次 MSR"）：

```
┌─[L1 (vEL2)]───────────────────────────────────────────────────────┐
│ msr vttbr_el2, x0    /* L1 设 L2 的 stage-2 根 */                  │
└──────────────────────┬─────────────────────────────────────────────┘
                       │
        当前硬件状态:
          物理 EL = EL1
          HCR_EL2.{NV=1, NV2=1}
          VNCR_EL2 = ctxt.vncr_array (P1)
                       │
                       ▼
┌─[硬件]────────────────────────────────────────────────────────────┐
│ VTTBR_EL2 在 VNCR mapping 中 (offset 0x020)                       │
│ → NV2 重定向，写到 [VNCR_EL2 + 0x020]                             │
│ → 即写到 P1.vncr_array[(0x020/8)] = vncr_array[4]                 │
│ → 无 trap                                                          │
└────────────────────────────────────────────────────────────────────┘

后续: 当 L1 ERET 到 L2 时, KVM 才会用这个 vVTTBR_EL2 构建 shadow stage-2:

┌─[L1 ERET 到 L2]───────────────────────────────────────────────────┐
│ L1 执行 ERET → 触发 NV trap (HCR_EL2.NV=1 时 ERET trap to EL2)    │
│ → handle_exit → kvm_handle_eret → kvm_emulate_nested_eret         │
│ → 在 nested.c::get_s2_mmu_nested:                                 │
│     vttbr = __vcpu_sys_reg(vcpu, VTTBR_EL2);  /* 读 P1 */          │
│     找到/分配匹配 VMID 的 nested_mmus[i]                          │
│     vcpu->arch.hw_mmu = &nested_mmus[i];                          │
│ → 下次 entry: VTTBR_EL2(物理) = hw_mmu->vttbr (是 SHADOW 的!)     │
│   不是 L1 设的那个值                                              │
└────────────────────────────────────────────────────────────────────┘
```

**数据流图**:

```
                       L1 写              L1 ERET 到 L2          L2 实际运行
                  ───────────────       ─────────────────       ─────────────
ctxt.vncr_array   ✱ 收到值 X (NV2)       未变                    未变
(VTTBR_EL2 槽)

vcpu->arch.hw_mmu  未变                  → 切到 nested_mmus[i]   随之

物理 VTTBR_EL2     未变                  未变                    ← 设为 hw_mmu->vttbr
                                                                  (shadow，VMID 分配)
```

> 这里展现了 NV2 的精妙：**L1 想配置 L2 的 stage-2，写 VTTBR 不 trap**；但 KVM 在 ERET 时介入，把 L1 的"逻辑配置"翻译成"shadow 物理 VTTBR_EL2"。

---

### 9.3 类 C 情景：L1 写 `HCR_EL2`（最经典的 NV2 重定向）

**寄存器属性**:
- VNCR-backed, offset 0x078
- 含义：L1 用来配置 L2 的运行环境（包含 VM/IMO/FMO/TGE/TVM 等）

```
┌─[L1 (vEL2)]──────────────────────────────────────────────────────┐
│ msr hcr_el2, x0                                                   │
│   x0 = HCR_VM | HCR_RW | HCR_TVM | HCR_IMO | ...                  │
└──────────────────────┬────────────────────────────────────────────┘
                       │
                       ▼
┌─[硬件]───────────────────────────────────────────────────────────┐
│ HCR_EL2 是 VNCR-backed，硬件直接写到 vncr_array[VNCR_HCR_EL2/8]  │
│ NO TRAP. NO L0 INVOLVEMENT.                                       │
└───────────────────────────────────────────────────────────────────┘

数据流:
  L1 软件值 x0
       │
       ▼
  ctxt.vncr_array[VNCR_HCR_EL2/8] = x0    (硬件直接写)
       │
       │ (后续 L1 ERET 到 L2, KVM 处理)
       ▼
  __compute_hcr(vcpu) 在 hyp/vhe/switch.c:
    guest_hcr = __vcpu_sys_reg(vcpu, HCR_EL2);   /* 读回 x0 */
    若 (guest_hcr & HCR_NV)
      guest_hcr |= HCR_NV2;   /* 强制 */
    物理 HCR_EL2 = host_default | guest_hcr & ~NV_HCR_GUEST_EXCLUDE
       │
       ▼
  L2 运行时硬件 HCR_EL2 反映了 L1 的意图（经过 L0 过滤）
```

**为什么 L1 设的 TVM 位会让 L2 写 SCTLR_EL1 trap？**

```
L2 执行: msr sctlr_el1, x0
  ↓
硬件检查 HCR_EL2.TVM (此时由物理 HCR_EL2 决定)
  此时物理 HCR_EL2.TVM = (L1 设的) | (L0 自己要的) = 1 (因为 L1 设了)
  ↓
trap 到 EL2 (L0 KVM)
  ↓
kvm_handle_sys_reg → triage_sysreg_trap:
  tc.cgt = CGT_HCR_TVM_TRVM
  compute_trap_behaviour 读 __vcpu_sys_reg(vcpu, HCR_EL2):
    L1 的 vHCR.TVM = 1 → BEHAVE_FORWARD_WRITE
  ↓
  方向是 write → goto inject
  ↓
  kvm_inject_nested_sync(vcpu, esr) — 反射给 L1 的 vEL2 vector
  ↓
L1 的 trap handler 看到 ESR_EL2 描述这次 SCTLR_EL1 写, 决定如何处理
```

> 关键：HCR_EL2 是 L0 与 L1 共同的"权益寄存器"。L0 必须**合并**自己想要的位 + L1 想要的位，才能写到物理 HCR_EL2。这个合并在 `__compute_hcr` 中完成。

---

### 9.4 类 D 情景：L1 读 `VBAR_EL2`（EL2 "shadow EL1" 寄存器）

**寄存器属性**:
- 非 VNCR（在 `__VNCR_START__` 之前）
- 在 `locate_register()` 的 `MAPPED_EL2_SYSREG(VBAR_EL2, VBAR_EL1, NULL)` 列表中
- 当 `is_hyp_ctxt(vcpu)==true` 时，物理 VBAR_EL1 sysreg 持有 L1 的 vVBAR_EL2 值（VHE 巧思）

```
┌─[L1 (vEL2)]──────────────────────────────────────────────────────┐
│ mrs x0, vbar_el2                                                  │
└──────────────────────┬────────────────────────────────────────────┘
                       │
                       ▼
┌─[硬件]───────────────────────────────────────────────────────────┐
│ VBAR_EL2 不在 VNCR 列表中，且 NV=1 → trap 到 EL2                  │
└──────────────────────┬────────────────────────────────────────────┘
                       │ (DABT/SYS64 异常进入 L0 EL2)
                       ▼
┌─[L0 KVM (handle_exit)]──────────────────────────────────────────┐
│ kvm_handle_sys_reg(vcpu)                                          │
│   triage_sysreg_trap:                                             │
│     tc.val == 0 (VBAR_EL2 在 NV trap 表里没有特殊条件)?           │
│     - 实际查 encoding_to_cgt: 没有专门给 VBAR_EL2 的条目          │
│     - 所以 tc.val=0 → goto local                                  │
│     → 设 *sr_index = 对应 sys_reg_descs 索引                      │
│   返回 false → 走本地模拟                                         │
│                                                                   │
│   perform_access(vcpu, params, &sys_reg_descs[i]):                │
│     desc->access(vcpu, params, desc) =                            │
│       access_rw 或专用 handler:                                   │
│         params.regval = vcpu_read_sys_reg(vcpu, VBAR_EL2)         │
│                                                                   │
│   vcpu_read_sys_reg:                                              │
│     locate_register: SR_LOC_LOADED|SR_LOC_MAPPED                  │
│       map_reg = VBAR_EL1                                          │
│     read_sr_from_cpu(VBAR_EL1):                                   │
│       val = read_sysreg_s(SYS_VBAR_EL12)                          │
│         /* _EL12 别名让 L0 (EL2) 读到 L1 的 EL1 sysreg 值 */      │
│       /* 但 L1 此刻在 vEL2，物理 VBAR_EL1 持有 L1 的 vVBAR_EL2 */ │
│     return val                                                    │
│                                                                   │
│   vcpu_set_reg(vcpu, Rt, params.regval)  /* 写回 x0 */            │
│   kvm_incr_pc(vcpu)                                               │
└──────────────────────┬────────────────────────────────────────────┘
                       │
                       ▼
                  返回 L1, L1 看到自己之前设的 vVBAR_EL2
```

**为什么需要 trap？** 因为这个寄存器在 NV2 列表外。Marc Zyngier 选择不让某些 EL2 寄存器走 VNCR：硬件不支持，或者语义太复杂（涉及 PE 状态切换）。

**存储变迁**:

```
L1 vEL2 运行时:           L2 运行时:
─────────────────         ──────────────
物理 VBAR_EL1 持有        物理 VBAR_EL1 持有
L1 的 vVBAR_EL2           L2 的 vVBAR_EL1

vcpu->arch.ctxt.sys_regs[VBAR_EL2]:  (内存备份, vcpu_put 时同步)
vcpu->arch.ctxt.sys_regs[VBAR_EL1]:  (内存备份, 也是 vcpu_put 时同步)
```

> 这两个寄存器**复用同一个物理 sysreg**，只是在不同时刻代表不同含义。这就是 VHE 的"zero-cost" 切换思想 + KVM 的"在哪就是哪"扩展。

---

### 9.5 类 E 情景：L2 读 `ID_AA64MMFR2_EL1`（特性 ID 寄存器）

**寄存器属性**:
- 在 `enum vcpu_sysreg` 主体之外，存储位于 `kvm_arch.id_regs[]`（VM 全局，不是 per-vcpu）
- `encoding_to_cgt`: `SR_RANGE_TRAP(SYS_ID_PFR0_EL1, ..., CGT_HCR_TID3)` 把整个 ID 区段映射到 `CGT_HCR_TID3`
- L0 通过 sys_reg_descs 表项的 `.access = access_id_reg` 处理

```
┌─[L2 (vEL1)]──────────────────────────────────────────────────────┐
│ mrs x0, id_aa64mmfr2_el1                                          │
└──────────────────────┬────────────────────────────────────────────┘
                       │
                       ▼
┌─[硬件]───────────────────────────────────────────────────────────┐
│ HCR_EL2.TID3 由 L0 默认设为 1 (要 trap ID 寄存器读)              │
│ → trap 到 EL2                                                     │
└──────────────────────┬────────────────────────────────────────────┘
                       │
                       ▼
┌─[L0 KVM]─────────────────────────────────────────────────────────┐
│ kvm_handle_sys_reg → triage_sysreg_trap:                          │
│   tc = lookup ID_AA64MMFR2_EL1                                    │
│     tc.cgt = CGT_HCR_TID3                                         │
│   FGU 检查: 若 vm.fgu[HFGRTR_GROUP] 有相应位 → UNDEF             │
│   vcpu_has_nv && !is_hyp_ctxt:                                    │
│     compute_trap_behaviour(vcpu, CGT_HCR_TID3):                   │
│       index=HCR_EL2, value=HCR_TID3, mask=HCR_TID3                │
│       behaviour=BEHAVE_FORWARD_READ                               │
│       是否被 L1 设置:                                              │
│         val = __vcpu_sys_reg(vcpu, HCR_EL2)  /* 读 P1 */          │
│         若 (val & HCR_TID3) → return BEHAVE_FORWARD_READ          │
│         否则 return BEHAVE_HANDLE_LOCALLY                         │
│                                                                   │
│   情况 a: L1 没设 TID3 → goto local (本地模拟)                    │
│     perform_access → access_id_reg:                               │
│       params.regval = read_id_reg(vcpu, rd):                      │
│         val = vcpu->kvm->arch.id_regs[IDREG_IDX(rd)]              │
│         /* L0 在 VM 创建时已裁剪好 */                             │
│       /* 不让 NV 客户机看到 L0 不支持的特性 */                    │
│     vcpu_set_reg(vcpu, Rt, params.regval)                         │
│                                                                   │
│   情况 b: L1 设了 TID3 → goto inject                              │
│     kvm_inject_nested_sync(vcpu, esr)                             │
│     → 反射给 L1 的 vEL2，L1 自己决定返给 L2 什么值                │
└───────────────────────────────────────────────────────────────────┘
```

**ID 寄存器的特殊存储**:

```c
/* arch/arm64/include/asm/kvm_host.h */
struct kvm_arch {
    /* ...  */
    u64 id_regs[KVM_ARM_ID_REG_NUM];   /* 由 IDREG_IDX(reg) 索引 */
    u64 midr_el1;
    u64 revidr_el1;
    u64 aidr_el1;
    u64 ctr_el0;
};
```

→ ID 寄存器**全 VM 共享**一份（所有 vcpu 看到相同特性），不在 `vcpu->arch.ctxt.sys_regs[]` 里。

---

### 9.6 类 F 情景：L2 访问 `PMCR_EL0`（PMU 子系统）

**寄存器属性**:
- 在 `enum vcpu_sysreg` 中：`PMCR_EL0`（在 `__SANITISED_REG_START__` 之前 → 不走 mask）
- 存储于 `vcpu->arch.ctxt.sys_regs[PMCR_EL0]`
- trap 由 MDCR_EL2.TPM/TPMCR 控制（不是 HCR_EL2）
- KVM 用专门的 PMU 仿真模型（`pmu-emul.c`）

```
┌─[L2 (vEL1/EL0)]──────────────────────────────────────────────────┐
│ mrs x0, pmcr_el0                                                  │
└──────────────────────┬────────────────────────────────────────────┘
                       │
                       ▼
┌─[硬件]───────────────────────────────────────────────────────────┐
│ MDCR_EL2.TPM=1 (L0 设的) → trap to EL2                            │
└──────────────────────┬────────────────────────────────────────────┘
                       │
                       ▼
┌─[L0 KVM]─────────────────────────────────────────────────────────┐
│ kvm_handle_sys_reg → triage_sysreg_trap:                          │
│   tc.cgt = CGT_MDCR_TPM (或 CGT_MDCR_TPM_TPMCR 复合)              │
│   compute_trap_behaviour:                                         │
│     index=MDCR_EL2, value=MDCR_TPM, behaviour=FORWARD_RW          │
│     val = __vcpu_sys_reg(vcpu, MDCR_EL2)  /* L1 的 vMDCR_EL2 */   │
│     若 (val & MDCR_TPM) → forward                                 │
│                                                                   │
│   若 L1 未设: 本地模拟                                             │
│     perform_access → access_pmcr (sys_regs.c):                    │
│       读: 返回 vcpu_pmu->pmcr_el0 (有 RES0/RES1 处理)             │
│       写: kvm_pmu_handle_pmcr(vcpu, val) 走 PMU 仿真状态机        │
│   若 L1 设了 TPM: kvm_inject_nested_sync                          │
└───────────────────────────────────────────────────────────────────┘
```

PMU 子系统有自己复杂的事件、计数器、中断模拟（详见 `arch/arm64/kvm/pmu-emul.c`），不在本文重点。

---


## 10. 三层数据流综合交互图

### 10.1 全景图：三层共享与私有的存储位置

```
┌══════════════════════════════════════════════════════════════════════════════┐
║                      L0 KVM (host kernel @ EL2)                              ║
║  ┌────────────────────────────────────────────────────────────────────────┐  ║
║  │ struct kvm_arch (per-VM)                                                │  ║
║  │   id_regs[]            ← 类 E (ID 寄存器，VM 全局)                      │  ║
║  │   sysreg_masks         ← RES0/RES1 mask (kvm_init_nv_sysregs)          │  ║
║  │   nested_mmus[]        ← shadow stage-2 MMU 池                          │  ║
║  │   fgu[]                ← Fine-Grained UNDEF                            │  ║
║  └────────────────────────────────────────────────────────────────────────┘  ║
║  ┌────────────────────────────────────────────────────────────────────────┐  ║
║  │ struct kvm_vcpu_arch (per-vCPU)                                         │  ║
║  │   ctxt.sys_regs[NR_SYS_REGS]    ← 类 B、D（trap 类、非 VNCR）          │  ║
║  │   ctxt.vncr_array (4kB page) →┐ ← 类 A、C（VNCR-backed）               │  ║
║  │   hcr_el2 (cached merged)      │                                        │  ║
║  │   hw_mmu                        │                                        │  ║
║  │   vncr_tlb { gva, hpa, cpu, … } │ ← P2 翻译缓存（L1 自己的 VNCR 页）    │  ║
║  └─────────────────────────────────┼──────────────────────────────────────┘  ║
║                                     │                                         ║
║  ┌────────── kvm_cpu_context.vncr_array (P1) ────────────┐                   ║
║  │ +0x000  reserved                                      │                   ║
║  │ +0x020  VTTBR_EL2  (类 C)                            │                   ║
║  │ +0x078  HCR_EL2    (类 C)                            │                   ║
║  │ +0x110  SCTLR_EL1  (类 A，仅 NV1=1 时实际生效)       │                   ║
║  │ +0x200  TTBR0_EL1  (类 A)                            │                   ║
║  │ +0x250  VBAR_EL1   (类 A)                            │                   ║
║  │ +0x4xx  GICv3 LRs  (类 C, GIC 部分)                  │                   ║
║  └───────────────────────────────────────────────────────┘                   ║
║                                                                               ║
║  ┌────────── per-CPU fixmap slot vncr_fixmap(cpu) ────────┐                   ║
║  │ 当 L2 运行时 → 映射到 L1 的 P2 页面 (host PA)         │ ← __set_fixmap   ║
║  │ 当 L1 运行时 → 不使用                                  │                   ║
║  └────────────────────────────────────────────────────────┘                   ║
║                                                                               ║
║  ┌─── per-physical-CPU 系统寄存器 (SYSREGS_ON_CPU 时) ────┐                   ║
║  │ TTBR0_EL1, SCTLR_EL1, …  ← 持有当前运行层的 vEL1 状态  │                   ║
║  │ VBAR_EL1, ESR_EL1, …     ← 或 (L1 vEL2 时) 持有 vEL2  │                   ║
║  │ HCR_EL2, VNCR_EL2, VTTBR_EL2 ← 由 __activate_traps 写  │                   ║
║  └────────────────────────────────────────────────────────┘                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
                                  ▲
                                  │ KVM_RUN ioctl
                                  ▼
┌══════════════════════════════════════════════════════════════════════════════┐
║                        L1 Hypervisor (@ physical EL1)                        ║
║  L1 内核自以为在 EL2，使用：                                                 ║
║    - VNCR-backed vEL2 寄存器 → 走 NV2 重定向 → 写到 P1 (KVM 的 vncr_array)   ║
║    - 非-VNCR vEL2 寄存器     → trap 到 L0 → 由 sys_regs.c 模拟              ║
║    - vVNCR_EL2 = 某 EL2 VA (L1 自己的内存) → 触发 P2 fixmap 建立            ║
║                                                                               ║
║  L1 自己的内存 (位于 host 的某个 IPA 区段)                                   ║
║   ┌──── L1's VNCR page (P2) ────┐                                            ║
║   │ 给 L2 用的 vEL2 状态        │                                            ║
║   │ HCR (L2 看到的)             │                                            ║
║   │ TTBR (L2 看到的)            │                                            ║
║   └─────────────────────────────┘ ← L0 通过 fixmap 映射这个页               ║
╚══════════════════════════════════════════════════════════════════════════════╝
                                  ▲
                                  │ ERET (L1 → L2)
                                  ▼
┌══════════════════════════════════════════════════════════════════════════════┐
║                      L2 Guest OS (@ physical EL0/EL1)                        ║
║   L2 完全感觉不到嵌套，发起 MSR/MRS 访问寄存器                               ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 10.2 L2 写寄存器：四种命运对照（数据流箭头图）

#### 命运 ① — A 类 (TTBR0_EL1，无 trap)

```
[L2 vEL1]   msr ttbr0_el1, x0
                |
                ▼ (硬件直写物理 TTBR0_EL1)
[HW TTBR0_EL1] = x0           (无 trap，无 KVM 介入)
                |
                ▼ (vcpu_put 时)
[ctxt.sys_regs[TTBR0_EL1]] ← read_sysreg_s(SYS_TTBR0_EL12)
```

#### 命运 ② — C 类 (HCR_EL2，NV2 内存重定向)

```
[L1 vEL2]   msr hcr_el2, x0
                |
                ▼ (硬件 NV2 重定向到 VNCR_EL2 + 0x078)
[ctxt.vncr_array[(0x078/8)]] = x0     (无 trap)
                |
                ▼ (下次 vcpu run 时)
[__compute_hcr] reads ctxt.vncr_array → merges → physical HCR_EL2
```

#### 命运 ③ — B 类 (VTCR_EL2，trap 到 L0 模拟)

```
[L1 vEL2]   msr vtcr_el2, x0
                |
                ▼ (硬件 NV trap)
                ▼
[L0 EL2 handle_exit] kvm_handle_sys_reg
                |
                ▼ triage_sysreg_trap → goto local
                ▼
[L0]   perform_access → vcpu_write_sys_reg(vcpu, x0, VTCR_EL2)
                |
                ▼ apply RES0/RES1 mask
                ▼
[ctxt.sys_regs[VTCR_EL2]] = sanitised(x0)
                |
                ▼ (下次进入 guest，__activate_traps_vhe)
[HW VTCR_EL2]  = ctxt.sys_regs[VTCR_EL2] (经过 hw_mmu 计算)
```

#### 命运 ④ — D 类 (VBAR_EL2，trap → 写到 EL1 物理寄存器)

```
[L1 vEL2]   msr vbar_el2, x0
                |
                ▼ (硬件 NV trap)
                ▼
[L0 handle_exit] kvm_handle_sys_reg → triage → local
                ▼
[L0]   vcpu_write_sys_reg(vcpu, x0, VBAR_EL2)
                ▼ locate_register → SR_LOC_LOADED|SR_LOC_MAPPED
                |       map_reg = VBAR_EL1
                ▼
[HW VBAR_EL1] = x0     (写入物理 EL1 寄存器，因为 L1 还会继续在 vEL2)
                |
                ▼ 同时也回写内存
[ctxt.sys_regs[VBAR_EL2]] = x0
```

### 10.3 完整时序图：L1 修改自己 vHCR_EL2 → ERET 到 L2 → L2 触发 trap 反射

```
   L2 (vEL1)           L1 (vEL2 = 物理 EL1)        L0 KVM (物理 EL2)            硬件状态
  ──────────         ──────────────────────       ──────────────────       ──────────────────────
                                                                            HCR.NV=NV2=1
                                                                            VNCR_EL2=ctxt.vncr_array
                       msr hcr_el2, x0                                       (HCR.TVM bit set in x0)
                       (NV2 redirect)                                       
                       │                                                    
                       ▼                                                    
                       (无 trap)                                            ctxt.vncr_array[15] ← x0
                                                                            
                       ;; … L1 还做其他事 …                                 
                       
                       msr vbar_el2, x0_v                                   
                       (NV trap, 因为非-VNCR)        ↓ DABT/SYS64 to EL2     
                                                    handle_exit             
                                                    kvm_handle_sys_reg       
                                                    triage→local            
                                                    vcpu_write_sys_reg(VBAR_EL2)
                                                    SR_LOC_MAPPED → VBAR_EL1
                                                                            HW VBAR_EL1 ← x0_v
                                                                            ctxt.sys_regs[VBAR_EL2]←x0_v
                                                    eret 回 L1               
                                                    
                       eret  (从 vEL2→vEL1 ERET)                              
                       (NV ERET trap)               ↓                       
                                                    kvm_handle_eret         
                                                    is_hyp_ctxt → 是        
                                                    kvm_emulate_nested_eret 
                                                    清 vEL2 标志，置 vEL1 模式
                                                    切到 nested_mmu (shadow S2)
                                                    schedule out + vcpu_put 
                                                                            
                                                    保存 vEL2 物理 sysregs   
                                                    回到 ctxt.sys_regs[]    
                                                    SYSREGS_ON_CPU 清       
                       
                                                    ;; 进程调度上 vcpu_load 
                                                    is_hyp_ctxt(vcpu)→否    
                                                    __sysreg_restore_el1_state
                                                    把 L2 的 vEL1 写到 HW   HW EL1 sysregs ← L2 vEL1
                                                                            __compute_hcr:
                                                                            !is_hyp → guest_hcr.TVM=1
                                                                            HCR (HW) |= TVM
                                                                            VNCR_EL2 = fixmap_va
                                                    
                                                    sysreg_restore_guest_state
                                                    SYS_ELR=L2 PC, SYS_SPSR=L2 PSTATE
                                                    __guest_enter (eret to L2)
   ─ 实际进入 L2 ───────────────────────────────────────────────
   msr ttbr0_el1, x0
                                                                            HW HCR.TVM=1 → trap! 
                                                                            (this is a write-trap)
                                                    ↑ DABT/SYS64 to EL2     
                                                    handle_exit             
                                                    kvm_handle_sys_reg       
                                                    triage_sysreg_trap:     
                                                      tc.cgt=CGT_HCR_TVM_TRVM
                                                      compute_trap_behaviour:
                                                        read __vcpu_sys_reg(HCR_EL2) (P1)
                                                        L1 设了 TVM → forward write
                                                      goto inject           
                                                    kvm_inject_nested_sync(esr)
                                                                            ctxt.sys_regs[ESR_EL2]=esr
                                                                            ctxt.sys_regs[ELR_EL2]=L2.PC
                                                                            ctxt.sys_regs[SPSR_EL2]=L2.PSTATE
                                                                            iflags |= EXCEPT_AA64_EL2_SYNC
                                                                            
                                                    ;; 回到 vcpu_run loop;
                                                    ;; 也会触发 vcpu_put/load
                                                    ;; 因为 is_hyp_ctxt 状态变化
                                                    
                                                    next vcpu_load:        
                                                    is_hyp_ctxt(vcpu)→是    
                                                    __sysreg_restore_vel2_state
                                                    HW EL1 sysregs ← L1 vEL2 view
                                                                            VNCR_EL2 = ctxt.vncr_array
                                                    __kvm_adjust_pc applies
                                                    pending excep:
                                                      PC=VBAR_EL2 + sync_offset
                                                      PSTATE=EL2h
                                                    __guest_enter
   ────────────── 进入 L1 vEL2 vector ─────────────────────────────────
                       L1 trap handler 看到 ESR_EL2 = TVM trap of TTBR0_EL1
                       决定是注入 GP fault 给 L2、修改 shadow PT、还是其他
```

### 10.4 三个共享/私有数据的"对应"关系

```
                                    存储在 L0 (KVM)             存储在硬件
                                    ─────────────────           ─────────────
L1 看到的 vTTBR0_EL1 (类 A)         无 (透明)                    HW TTBR0_EL1 (NV2 不影响 EL1 reg)
L1 看到的 vHCR_EL2 (类 C)           ctxt.vncr_array[15]          经过合并 → HW HCR_EL2
L1 看到的 vVTCR_EL2 (类 C)          ctxt.vncr_array[8]           HW VTCR_EL2 (经 nested_mmu)
L1 看到的 vVBAR_EL2 (类 D)          ctxt.sys_regs[VBAR_EL2]      L1 跑时 → HW VBAR_EL1
L1 看到的 vCPTR_EL2 (类 D)          ctxt.sys_regs[CPTR_EL2]      L1 跑时 → HW CPACR_EL12
L1 给 L2 用的 vTTBR0_EL1            ctxt.sys_regs[TTBR0_EL1]     L2 跑时 → HW TTBR0_EL1

L2 看到的 TTBR0_EL1 (类 A)          ctxt.sys_regs[TTBR0_EL1]     L2 跑时 → HW TTBR0_EL1
L2 看到的 HCR_EL2 (无意义)          只在 L1 让 L2 也搞嵌套时有意义；指向 L1 自己的 P2 页
```

### 10.5 寄存器位置随时间变化（L1↔L2 切换）

```
Time:    t0         t1: vcpu_put     t2: vcpu_load    t3: enter L2     t4: L2 trap
────────────────────────────────────────────────────────────────────────────────────
  L1 跑                              L2 跑前           L2 跑           handle_exit

物理 TTBR0_EL1
  L1 vEL1 值          (保存)          L2 vEL1 值        L2 vEL1         L2 vEL1 (trap 时被读)
   ↓ save                                 ↓ restore
                ctxt.sys_regs[TTBR0_EL1]   ↑ 来自 L2 备份

物理 VBAR_EL1 (= vEL2 影子, L1 跑时)  
  L1 vEL2 值          (save_vel2)     L2 vEL1 VBAR_EL1  L2 vEL1         L2 vEL1
   ↓                  ↓
                ctxt.sys_regs[VBAR_EL2]   ↑ 由 __sysreg_restore_el1_state 写入

物理 HCR_EL2
  含 NV/NV2/AT/TTLB                  NV/NV2 但去掉 AT  同 t2           L0 KVM 自己用值
  + L1's vHCR (含 NV1)
                                 → __compute_hcr 重新计算

物理 VNCR_EL2
  ctxt.vncr_array (P1)               若 guest_hcr.NV=1 →fixmap_va (P2)  P2 (L1 的页)  P2

物理 VTTBR_EL2
  L1's S2 或 KVM canonical                             nested_mmu shadow  shadow
                                                       (L1 vS2 ⊗ L0 S2)
```

---


## 11. 总结

### 11.1 关键设计哲学

1. **VHE 是 NV 的基石**  
   KVM 选择"L1 跑在物理 EL1，但通过 NV/NV2 让它以为自己在 EL2"，正是因为 VHE 已经把 EL1 与 EL2 寄存器集合对齐。L0 在 EL2 通过 `_EL12` 别名"看穿" L1 的状态。

2. **NV2 把 trap 换成内存读写**  
   架构层面用 VNCR_EL2 + 固定偏移把 EL2 寄存器映射到物理内存。最高频的"L1 配置 vEL2 状态"操作不再 trap，性能从 NV1 的"几乎不可用"跃升到生产可行。

3. **两级存储 + 一个映射**  
   - `ctxt.sys_regs[]`：所有非-VNCR 寄存器
   - `ctxt.vncr_array`：VNCR-backed 寄存器（L1 自身视角的 vEL2）
   - `vcpu->arch.vncr_tlb` + 每 CPU fixmap：L2 视角的 vEL2（即 L1 给 L2 准备的 P2 页）

4. **trap 路由由"位置 + 上下文"决定**  
   `triage_sysreg_trap` 在 emulate-nested.c 中编码了完整的 ARM trap 决策表。

### 11.2 六大类对照速查

| 类 | 典型寄存器 | trap 行为 | KVM 内存位置 | 物理寄存器位置 |
|---|---|---|---|---|
| A | TTBR0_EL1, SCTLR_EL1 | 一般无 trap (除非 L1 设 TVM/TRVM) | vncr_array (NV1=1) 或 sys_regs[] | 物理 EL1 sysreg |
| B | VTCR_EL2, CPTR_EL2 | 总是 trap (L0 模拟) | sys_regs[] (sanitised) | KVM 自己用 |
| C | HCR_EL2, HCRX_EL2, FGT* | 无 trap (NV2 redirect) | vncr_array | 由 __compute_hcr 合并写入 |
| D | VBAR_EL2, ESR_EL2, MAIR_EL2 | 总是 trap (L0 模拟) | sys_regs[] | 写入物理 *_EL1 sysreg |
| E | ID_AA64*_EL1, MIDR_EL1 | trap (TID3/TID2/TID1) | kvm_arch.id_regs[] (VM 全局) | 不直接使用 |
| F | PMCR_EL0, ZCR_EL2, DBGBVR_EL1 | MDCR/CPTR 控制 | 各子系统局部 + sys_regs[] | 子系统专管 |

### 11.3 关键数据结构速查

```c
// arch/arm64/include/asm/kvm_host.h
struct kvm_cpu_context {
    u64 sys_regs[NR_SYS_REGS];   // 类 B/D/E/F 主存储
    u64 *vncr_array;              // 类 A/C 的存储页 (4kB)
    ...
};

struct kvm_vcpu_arch {
    struct kvm_cpu_context ctxt;
    struct kvm_s2_mmu *hw_mmu;    // 当前生效 stage-2 (NV 时是 shadow)
    u64 hcr_el2, hcrx_el2, mdcr_el2; // HW 实际写入值的缓存
    struct vncr_tlb *vncr_tlb;    // P2 页面伪 TLB
    ...
};

struct kvm_arch {
    u64 id_regs[KVM_ARM_ID_REG_NUM];   // 类 E
    struct kvm_sysreg_masks *sysreg_masks; // RES0/RES1
    struct kvm_s2_mmu *nested_mmus;     // shadow stage-2 池
    u64 fgu[__NR_FGT_GROUP_IDS__];      // FGU mask
    ...
};
```

### 11.4 关键函数速查

| 函数 | 文件 | 作用 |
|---|---|---|
| `__vcpu_sys_reg` | kvm_host.h | 通用读，自动选 sys_regs[] 或 vncr_array[] |
| `vcpu_read_sys_reg` / `vcpu_write_sys_reg` | sys_regs.c | 智能读/写：根据 SR_LOC 选择 HW 或内存 |
| `locate_register` | sys_regs.c | 决定寄存器当前在哪 |
| `kvm_handle_sys_reg` | sys_regs.c | EC=SYS64 trap 入口 |
| `triage_sysreg_trap` | emulate-nested.c | NV trap 路由决策 |
| `compute_trap_behaviour` | emulate-nested.c | 解析 CGT/FGT 控制位 |
| `kvm_inject_nested_sync` | emulate-nested.c | 把异常反射给 L1 vEL2 |
| `kvm_handle_vncr_abort` | nested.c | 处理 L1 P2 页面 fault |
| `kvm_translate_vncr` | nested.c | 走 L1 stage-1 翻译 vVNCR_EL2 |
| `kvm_map_l1_vncr` | nested.c | 把 P2 装入 fixmap |
| `kvm_init_nv_sysregs` | nested.c | 初始化 RES0/RES1 mask |
| `__compute_hcr` | hyp/vhe/switch.c | 每次 entry 重算 HCR_EL2 + 设 VNCR_EL2 |
| `__sysreg_restore_vel2_state` | hyp/vhe/sysreg-sr.c | 把 vEL2 状态加载到物理 EL1 |
| `__sysreg_save_vel2_state` | hyp/vhe/sysreg-sr.c | 反向操作 |
| `kvm_emulate_nested_eret` | emulate-nested.c | 模拟 ERET 状态切换 |

### 11.5 KVM/arm64 NV 与 KVM/x86 nested VMX 的设计哲学对比

| 方面 | aarch64 NV (本文) | x86 VMX nested |
|---|---|---|
| **L1 真正运行的 EL/Ring** | 物理 EL1 | 物理 ring 0 (但作为 vmx non-root 的 root) |
| **L1 的 hypervisor 状态在哪** | VNCR 页面 + sys_regs[] | shadow VMCS |
| **配置变更触发 trap** | 仅控制类（B/D），数据类（A/C）走内存 | 大部分写 VMCS 字段都 trap，VMCS shadowing 减轻 |
| **硬件辅助** | FEAT_NV / FEAT_NV2 / FEAT_FGT | VMCS shadowing / EPT-on-EPT |
| **stage-2 嵌套** | shadow stage-2（vS2 ⊗ S2 复合） | shadow EPT（vEPT ⊗ EPT 复合） |
| **trap 路由表** | encoding_to_cgt[] + encoding_to_fgt[] | nested_vmx_check_*/handle_*函数群 |
| **特性裁剪** | sysreg_masks (RES0/RES1) + fgu[] | nested_vmx_features 配置 |

### 11.6 当前 KVM 实现的限制（截至 v7.1.0-rc4 era）

- 仅支持 NV2，必须实现 FEAT_NV2 的硬件
- L1 必须使用 VHE 风格（HCR_EL2.E2H=1），nVHE 风格 (E2H=0) 通过 NV1 支持但要求 FEAT_HCR_NV1
- 三层及以上嵌套（L3+）不支持
- pKVM (protected KVM) 与 NV 不能同时启用

### 11.7 推荐进一步阅读

- `arch/arm64/kvm/nested.c` 中关于 `kvm_init_nested_s2_mmu` 的部分（shadow stage-2，本文未展开）
- `arch/arm64/kvm/emulate-nested.c::kvm_emulate_nested_eret`（NV ERET 完整模拟）
- `arch/arm64/kvm/at.c`（Address Translation 指令模拟）
- LWN 文章: ARMv8.3/8.4 Nested Virtualization 系列 (Marc Zyngier)
- ARM ARM (DDI 0487 Issue J 之后): D8.6 "Nested virtualization"

---

## 附：单条 MSR 指令在三层间的极简版数据流

```
[L2: msr X, val]
  │
  ├── X 是类 A 寄存器? ──→ HW 直写物理 EL1 sysreg ──→ done (no exit)
  │
  ├── X 是类 C 寄存器? ──→ HW NV2 重定向写 vncr_array ──→ done (no exit)
  │       (实际仅当 L1 是 vEL2 这个情景才有意义)
  │
  └── X 受 L1 vHCR/vMDCR/FGT 控制 trap ─→ exit ─→ L0 triage ─→
        ├── 命中本地模拟  ──→ vcpu_write_sys_reg ──→ memory + (HW if loaded)
        ├── 命中 forward ──→ kvm_inject_nested_sync(esr) ──→ vEL2 vector
        └── 未知/feat off ──→ kvm_inject_undefined ──→ L2 收 UNDEF
```

— 完 —
