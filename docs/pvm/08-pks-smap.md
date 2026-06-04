# PKS 替代 SMAP：PVM 的内存访问保护实现

## 1. 问题：为什么 SMAP 在 PVM 中失效

SMAP（Supervisor Mode Access Prevention）是 Intel Haswell 引入的安全特性，防止内核代码意外访问用户内存：

- **工作原理**：若 `CR4.SMAP=1`，当 CPL ≤ 2（supervisor mode）时访问用户页（U/S=1）会触发 #PF（除非 `RFLAGS.AC=1`）
- **PVM 的困境**：Guest 内核（smod）和 Guest 用户态（umod）都运行在 CPL3（硬件 user mode），所以 SMAP 根本区分不了 smod 和 umod——对 SMAP 来说，两者都是 CPL3，都是"用户模式"

因此，PVM 需要一个完全不同的机制来实现与 SMAP 等价的保护：防止 Guest 内核（smod）意外访问 Guest 用户内存（除非显式允许）。

## 2. PKS（Protection Keys for Supervisor）简介

PKS 是 Intel Tiger Lake（2020 年）引入的特性（`CPUID.07:ECX.PKS`）：

- 每个页表项的 bit 62:59 编码一个 protection key（0-15）
- `MSR_IA32_PKRS`（`0x6E1`）是 32-bit MSR，每个 key 占 2 位：
  - bit `2*key`：AD（Access Disable），若置位则禁止读写
  - bit `2*key+1`：WD（Write Disable），若置位则禁止写（允许读）
- PKS 对 supervisor 访问（CPL ≤ 2）生效，与 SMAP 类似但更细粒度

**注意 PKS 与 PKU 的区别**：
- PKU（Protection Keys for User）：对 CPL=3 的用户态访问生效，`MSR_IA32_PKRU`
- PKS（Protection Keys for Supervisor）：对 CPL ≤ 2 的 supervisor 访问生效，`MSR_IA32_PKRS`

在 PVM 中，由于 smod 是 CPL3，技术上两者都适用于 CPL3 访问。PVM 的设计需要精心选择使用哪个 key 机制。

## 3. PVM 的 PKS/PKU 策略

根据规范（`pvm-spec.rst`）和 PVM 源码，PVM 使用如下策略：

### 3.1 页面 Protection Key 分配

```
smod 页（Guest 内核）：使用 protection key K_KERNEL（比如 key 1）
umod 页（Guest 用户）：使用 protection key K_USER（比如 key 0 = 默认）
宿主内核页（影子映射）：不在 Guest 地址范围，无 _PAGE_USER，PKS 不适用
```

在 shadow paging 中，超管在建立 smod_cr3 / umod_cr3 时，设置页表项的 protection key 字段：
- Guest 内核页 → key K_KERNEL
- Guest 用户页 → key K_USER

### 3.2 smod 下的 PKRS 设置

在 smod（Guest 内核）执行期间，`MSR_IA32_PKRS` 设置为：
- key K_USER：AD=1（禁止访问 Guest 用户页）→ 等效于 SMAP

这样，smod 尝试访问 Guest 用户页会触发 #PF（PKS violation），防止内核意外读写用户内存。

### 3.3 smod 访问用户内存（`access_ok` 语义）

当 smod 需要合法访问用户内存（如 `copy_from_user`）时：
```c
// Guest 内核的 __uaccess_begin()
wrpkru(pkru_for_user_access);  // 临时允许 key K_USER 访问

// 执行用户内存访问
copy_from_user(kernel_buf, user_ptr, len);

// Guest 内核的 __uaccess_end()
wrpkru(pkru_default);  // 恢复限制
```

**注意**：WRPKRU 修改的是 PKRU（PKU，用户态 key 寄存器），而不是 PKRS。这里有一个微妙之处：PVM 在 CPL3 下运行，PKRU 对 CPL3 访问生效，PKRS 对 CPL ≤ 2 访问生效。由于 smod 是 CPL3，实际上 PKRU 保护 smod 访问用户页，而非 PKRS！

### 3.4 smod ↔ umod 切换时的 PKRU/PKRS 管理

```
进入 smod（从 umod SYSCALL）：
  Switcher 在 entry_SYSCALL_64_switcher 中：
  // 保存 umod PKRU 到 PVCS
  // 设置 smod PKRU（禁止访问用户页，等效 SMAP）
  rdpkru → PVCS::pkru  // 保存用户态 PKRU
  wrpkru $smod_pkru    // 设置内核态 PKRU（K_USER = AD）

返回 umod（EVENT_RETURN）：
  Switcher：
  // 从 PVCS 恢复用户态 PKRU
  wrpkru PVCS::pkru
```

等等，PKRU 是用户态寄存器（通过 RDPKRU/WRPKRU 在 CPL3 访问），而 PKRS 是 MSR（需要 WRMSR，CPL0 才能访问）。在 PVM 中，smod 是 CPL3，不能执行 WRMSR 修改 PKRS，所以 PKS（通过 PKRS）实际上由超管控制，而不是 Switcher 在 Guest CPL3 下修改。

因此实际的 PVM 实现是：
- 超管在 VM-enter 前设置 `MSR_IA32_PKRS`（通过 WRMSR，CPL0）
- smod 通过 RDPKRU/WRPKRU（CPL3 允许）管理 PKRU（PKU）

### 3.5 PVCS::pkru 字段的用途

```c
// PVCS 中的 pkru 字段
u32 pkru;
// 在 umod → smod 切换时，保存 umod 的 PKRU 值
// 在 smod → umod 切换时，从此恢复 PKRU

// Switcher（entry_SYSCALL_64_switcher）：进入 smod 时
rdpkru                          // 读取当前 PKRU（= umod 的 PKRU）
movl   %eax, PVCS_pkru(%rdi)    // 保存到 PVCS
movl   $smod_pkru_value, %eax   // smod 的 PKRU（K_USER = AD）
wrpkru                          // 设置

// 返回 umod 时
movl   PVCS_pkru(%rdi), %eax    // 从 PVCS 读取 umod PKRU
wrpkru                          // 恢复用户态 PKRU
```

## 4. 规范中的 MSR_IA32_PKRS

来自 `pvm-spec.rst`：

```
MSR_IA32_PKRS
^^^^^^^^^^^^^

See "Protection Keys".
```

规范中 Protection Keys 章节说明：
- PVM 实现应正确处理 MSR_IA32_PKRS（超管可以设置，Guest 不能直接设置，因为需要 CPL0）
- 超管在 VM-enter 时设置 PKRS，在 VM-exit 时恢复
- Guest 内核通过 PKRU（CPL3 可写）实现 SMAP 等价保护

## 5. CR4.SMAP 在 PVM 中的状态

```
// pvm-spec.rst 的底层状态表
CR4: VME=PVI=0, PAE=FSGSBASE=1. Others are implementation-defined.
```

特别说明 `CR4.SMAP` 是 `implementation-defined`（实现定义）。实际上，在 PVM 中：
- 宿主内核的 CR4.SMAP 可以为 1（宿主内核可以启用 SMAP 保护自己）
- Guest 看到的虚拟 CR4.SMAP = 0（PVM 虚拟 CR4 禁用 SMAP，因为 Guest 是 CPL3，SMAP 对 CPL3 无意义）

```
// pvm-spec.rst 虚拟 CR4 状态
CR4: VME/PVI/SMAP=0; PAE/FSGSBASE=1; SMEP = underlying EFER.NXE
```

注意 `SMEP`（Supervisor Mode Execution Prevention）也是 0——同样因为 smod 是 CPL3，无法区分 supervisor 和 user 执行，SMEP 没有意义。

## 6. 与 SMEP 的类比

SMEP 防止内核执行用户页面代码（类似 NX 位保护）。在 PVM 中：
- 硬件 SMEP 对 CPL3 不生效
- PVM 通过 `NX` 位（Not Executable）在 shadow page 中为内核不应执行的页设置 NX，实现等价保护
- Guest 内核内存有 NX=0（可执行），Guest 用户内存有 NX=1（不可执行）——这由 shadow paging 在建立映射时控制

## 7. PKS 支持的硬件要求

PVM 中 PKS 替代 SMAP 需要 CPU 支持 PKS 特性（CPUID.07:ECX bit 31）：
- Intel Tiger Lake（Ice Lake 服务器）及更新：支持
- AMD：目前不支持 PKS
- 老 Intel（Haswell-Broadwell-Skylake-Icelake）：不支持

这意味着 PVM 在不支持 PKS 的 CPU 上，内核→用户内存的隔离不完整（类似 Spectre/Meltdown 的 SMAP 绕过风险，但对正常路径安全性无影响）。超管可以在不支持 PKS 的 CPU 上仍然运行 PVM，只是缺少这个保护层。

## 8. PVM 与 Xen PV 的 SMAP 处理对比

| 方面 | Xen PV | PVM |
|------|--------|-----|
| Guest 内核特权 | CPL1（或 ring 3 in ring 1）| CPL3 |
| SMAP 可用性 | 不适用（CPL1 不是 supervisor）| 不适用（CPL3）|
| 替代机制 | Xen 通过只读页表防止写，读保护通过页表 | PKS（PKRU，hardware）|
| 用户/内核分隔 | Guest 内核 vs 用户地址空间 | smod_cr3 vs umod_cr3 |
| 跨域访问控制 | Xen hypercall 或 grant table | pvm 通过 PKS key 控制 |

## 9. 安全性分析

PKS 替代 SMAP 的安全强度：

**优点**：
- 硬件强制（CPU 执行 PKS check，不依赖软件）
- 细粒度（每个页有独立的 key，可以有多个保护级别）
- 切换 smod/umod 时自动管理（Switcher 负责 PKRU 保存恢复）

**局限**：
- Spectre/Meltdown 类攻击可能通过投机执行绕过 PKS（规范明确 TODO：Meltdown 缓解不完整）
- 只在支持 PKS 的硬件上有效
- PKRS 由超管控制，Guest 无法单独配置（需要通过 WRMSR hypercall，但超管可以拒绝）
- `wrpkru` 是非特权指令，Guest 可以随时调用——但这是预期的：Guest 在 `access_ok` 时合法调用

**关键安全保证**：正常代码路径下，smod 不会意外访问 umod 内存。攻击者若能控制 smod 的 PKRU，则已经有代码执行权限，保护已无意义。
