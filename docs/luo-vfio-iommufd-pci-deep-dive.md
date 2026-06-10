# Live Update (LUO) 在 VFIO / iommufd / PCI 中的支持：深度分析

> 本文整理自对 Linux 内核 Live Update Orchestrator (LUO) 机制及其在 VFIO、iommufd、PCI
> 三个子系统中接入工作的逐层分析。基于本地内核树 **v7.1-rc6** 的代码，以及社区
> 2025–2026 年的相关补丁集（截至 2026-06 仍在邮件列表评审中）。
>
> 内容均经改写以符合引用许可要求。

---

## 目录

1. [整体结论与社区现状](#1-整体结论与社区现状)
2. [地基：LUO 提供的核心原语](#2-地基luo-提供的核心原语)
3. [三个补丁集的分层依赖关系](#3-三个补丁集的分层依赖关系)
4. [PCI v4：总线/硬件这层](#4-pci-v4总线硬件这层)
5. [VFIO v4：fd 这层](#5-vfio-v4fd-这层)
6. [iommufd v2：地址翻译这层（最重）](#6-iommufd-v2地址翻译这层最重)
7. [BDF 稳定性与 PCI 两遍枚举](#7-bdf-稳定性与-pci-两遍枚举)
8. [IOMMU HW 是什么 + preserve/restore 全貌](#8-iommu-hw-是什么--preserverestore-全貌)
9. [KHO：物理页如何跨 kexec 标记与保护](#9-khophysical-页如何跨-kexec-标记与保护)
10. [kexec 如何向新内核传递信息](#10-kexec-如何向新内核传递信息)
11. [保留页的维护：元数据页 vs memfd 数据页](#11-保留页的维护元数据页-vs-memfd-数据页)
12. [关键设计决策汇总](#12-关键设计决策汇总)
13. [参考来源](#13-参考来源)

---

## 1. 整体结论与社区现状

**Live Update** 是一种基于 kexec 的特殊重启：在把内核从一个版本换到另一个版本的同时，
保留选定资源的状态，并让指定硬件设备保持运行。目标场景是云上 hypervisor 的热升级，
让运行中的虚拟机感知不到宿主内核被替换。

### 本地树（v7.1-rc6）现状

| 组件 | 是否已在本地树 | 说明 |
|---|---|---|
| LUO 核心 + KHO | ✅ 已合入 | `kernel/liveupdate/`，`include/linux/liveupdate.h` |
| memfd 文件处理器 | ✅ 已合入 | `mm/memfd_luo.c`（唯一在树内的参考消费者） |
| VFIO LUO 集成 | ❌ 未合入 | `drivers/vfio` 下无 `liveupdate`/`luo` 引用 |
| iommufd LUO 集成 | ❌ 未合入 | `drivers/iommu/iommufd` 下无引用 |
| PCI LUO 集成 | ❌ 未合入 | `drivers/pci` 下无引用 |

### 社区状态（截至 2026-06）

| 子系列 | 管哪一层 | 状态 |
|---|---|---|
| LUO 核心 + memfd | 资源保留框架 | ✅ 已进主线 |
| PCI v4/v6 (David Matlack) | 总线/硬件拓扑 | 🔶 邮件列表评审中 |
| VFIO v4 (Vipin Sharma) | fd + 用户 ABI | 🔶 邮件列表评审中 |
| iommufd v2 (Samiullah Khawaja) | 地址翻译/页表 | 🔶 邮件列表评审中 |

**一句话总结**：核心机制已就绪，三个子系统的接入仍在路上，本地树尚无这三层代码。

---

## 2. 地基：LUO 提供的核心原语

三个子系统都建立在 LUO 提供的原语之上。LUO 核心**不懂任何设备**，只提供"把状态搬过 kexec"的通用机制。

### 2.1 KHO（Kexec HandOver）—— 物理层

最底层。解决"kexec 换内核时哪些物理页不能被踩、并能重新拿回"的问题。

```
kho_alloc_preserve(size)      // 分配并标记"这块内存过 kexec 保留"
kho_preserve_folio(folio)     // 标记某个 folio 保留
kho_preserve_vmalloc(...)     // 标记一段 vmalloc 区
kho_restore_folio(phys)       // 新内核里按物理地址拿回
```

**铁律**：所有跨 kexec 的"指针"都必须是物理地址（u64），因为虚拟地址映射在新内核里全变了。

### 2.2 原语一：File Handler（文件处理器）—— 按 fd 粒度保活

```c
struct liveupdate_file_handler {
    const struct liveupdate_file_ops *ops;
    const char compatible[];   // "memfd-v1" / "vfio-pci" / "iommufd" ...
};
```

`compatible` 字符串是新旧内核之间的握手暗号。它定义了一套严格的生命周期状态机：

```
旧内核（系统在线运行）                          ┊ 新内核（kexec 之后）
can_preserve()  ── 认领这个 fd 吗？            ┊
preserve()      ── 重活：把状态存进 KHO 内存    ┊
        ⋮ （此时业务还在跑）                    ┊
─────────── reboot() 系统调用进入 ───────────   ┊
freeze()        ── 最后定格，用户态已无法再改    ┊
                                  ══ kexec ══>  ┊ deserialize() ── 重建链表
                                               ┊ retrieve()     ── 按 token 重建 file
                                               ┊ finish()       ── 收尾交还所有权
```

要点：
- **preserve 是重活、freeze 是轻活**：preserve 在系统在线时做（耗时无妨），freeze 在 `reboot()` 里做（必须快）。
- **回滚对称**：preserve 失败 → unpreserve；freeze 失败 → 逐个 unfreeze 并取消活迁移，系统继续跑。
- **retrieve 可乱序、按 token 索引**。
- **失败哲学硬核**：反序列化中途失败当作内存泄漏让用户态重启；`luo_restore_fail` 直接 `panic()`，因为设备/内存半生不熟时继续启动可能泄漏租户私有数据。

### 2.3 原语二：FLB（File-Lifecycle-Bound）—— 多 fd 共享的全局对象

为 iommufd/pci 这种"多个 fd 共享同一份全局硬件态"场景设计，**引用计数管理**：

```c
struct liveupdate_flb {
    const struct liveupdate_flb_ops *ops;     // preserve/unpreserve/retrieve/finish
    const char compatible[];                   // "iommu-liveupdate-v1" / "pci core"
};
```

语义：
- **第一个**依赖它的 fd 被 preserve 时 → 触发 FLB 的 `preserve()`（count 0→1）。
- 后续 fd preserve 只 `count++`，不重复存。
- **最后一个** fd 被 unpreserve/finish 时 → 触发 `unpreserve()`/`finish()`（count→0）。
- 新内核里 `liveupdate_flb_get_incoming()` 首次访问时按需触发 `retrieve()`，之后缓存返回。
- `incoming`/`outgoing` 两套独立状态：旧内核走 outgoing，新内核走 incoming。

> **一句话**：File Handler 管"每个 fd 的私有态"，FLB 管"多个 fd 共享的全局态"，
> KHO 管"把这些态的物理内存搬过 kexec"，session 把一组 fd 打包成命名容器。

---

## 3. 三个补丁集的分层依赖关系

这不是三个并列的功能，而是一个**垂直依赖的栈**。一个直通设备要"带电"过 kexec，需要三层各管一摊：

```
        ┌─────────────────────────────────────────────┐
        │  用户态 VMM：通过 /dev/liveupdate 编排        │
        └─────────────────────────────────────────────┘
                              │
   ┌──────────────────────────────────────────────────────────┐
   │ VFIO v4 (Vipin Sharma)  ── "fd 这层"                       │
   │  保住 vfio cdev 这个 struct file 本身，挡住旁路打开          │
   └──────────────────────────────────────────────────────────┘
                              │ 依赖
   ┌──────────────────────────────────────────────────────────┐
   │ iommufd v2 (Samiullah Khawaja) ── "地址翻译这层"           │
   │  保住 IOMMU domain / 页表 / context entry / DMA 映射        │
   └──────────────────────────────────────────────────────────┘
                              │ 依赖
   ┌──────────────────────────────────────────────────────────┐
   │ PCI v4 (David Matlack)  ── "总线/硬件这层"                 │
   │  保住 PCI 设备身份、桥、bus number、ACS、不关 bus mastering │
   └──────────────────────────────────────────────────────────┘
                              │ 依赖
   ┌──────────────────────────────────────────────────────────┐
   │ LUO FLB refcount 补丁 (2 个小补丁)                         │
   └──────────────────────────────────────────────────────────┘
                              │ 依赖
   ┌──────────────────────────────────────────────────────────┐
   │ LUO 核心 + KHO   ── 已合入 v7.1                            │
   └──────────────────────────────────────────────────────────┘
```

合入只能自底向上：LUO 核心 → FLB refcount → PCI → VFIO → iommufd。

---

## 4. PCI v4：总线/硬件这层

**`[PATCH v4 00/11] PCI: liveupdate: PCI core support for Live Update`**，David Matlack。
新增 `drivers/pci/liveupdate.c`（~562 行）+ `include/linux/kho/abi/pci.h`。

### 核心矛盾

kexec 后新内核会**重新枚举 PCI 总线**。放任不管会导致：bus number 改变 → requester ID 变 →
IOMMU 翻译乱；IOMMU group 重排 → 隔离边界变；shutdown 关 BME → 正在进行的 DMA 被掐断。

### 11 个补丁归成 6 件事

1. **建 FLB handler**（`struct pci_ser`），通过 KHO 保 PCI 全局拓扑态。
2. **出/入双向追踪 API**：驱动登记 outgoing 待保留设备；新内核枚举时用 `liveupdate_flb_get_incoming()` 识别 incoming 保留设备。（这就是依赖 FLB refcount 的原因——probe 期间反复访问 incoming FLB 必须有引用计数防 UAF。）
3. **自动保留上游所有桥**：保留一个 endpoint，到 root 链路上所有桥跟着保留（引用计数）。
4. **继承拓扑事实**：secondary/subordinate bus number、ARI Forwarding Enable、ACS flags 全部从旧内核继承。
5. **只允许"不可变单例 IOMMU group"里的设备被保留**，且上游桥须开 ACS（保证 group 拓扑跨 kexec 不变）。
6. **改 PCI shutdown 路径**：对保留设备及其上游桥不清 bus mastering → DMA 不断。

### 演进与争议

- rfc(2025-10) → v1 → v2 → v3 → v4(2026-04) → v5 → v6(2026-06)。
- Kconfig `PCI_LIVEUPDATE` 标 **experimental**，且 `depends on !CARDBUS`。
- 未做（future work）：保留期间桥保持 D0、保留 BAR（P2P）、保留 SR-IOV VF。

---

## 5. VFIO v4：fd 这层

**`[PATCH v4 00/16] vfio/pci: Base Live Update support for VFIO`**，Vipin Sharma。
新增 `drivers/vfio/pci/vfio_pci_liveupdate.c`（349 行）。

### 关键认知：这一版只保 fd，不保设备状态

- 目前只有 vfio cdev 这个 fd 被保留，底层设备状态**不保**。
- 更"反直觉"的是：在 freeze 阶段（kexec 前一刻）VFIO 会主动 reset 设备、关 BME、恢复原始状态。
- 这是**阶段性妥协**：base 系列先把骨架（fd 过 kexec、用户 ABI、挡旁路）立起来，为新内核安全先 reset。真正"带电不重置"要等 PCI v4 + iommufd v2 叠上来。reset 这步是临时的。

### 用户态 ABI

- `VFIO_PCI_LIVEUPDATE` config，依赖 `VFIO_DEVICE_CDEV`（**只支持 cdev 模式，不支持 legacy group**），临时禁用 `VFIO_PCI_DMABUF`。
- cdev fd 通过 session preserve，kexec 后用同一 session + token 取回。
- **挡旁路**：kexec 后用 legacy group API 或直接 `open(/dev/vfio/devices/vfioX)` 拿保留设备会报错，必须走 `LIVEUPDATE_SESSION_RETRIEVE_FD`。

---

## 6. iommufd v2：地址翻译这层（最重）

**`[PATCH v2 00/16] iommu: Add live update state preservation`**，Samiullah Khawaja / Pasha Tatashin / YiFei Zhu。
**+3016 行，38 文件，16 补丁**，三者中最重。这是其工作的 **Phase 1**。

### 目标

保住 iommufd 管理的、挂在被保留 VFIO 设备上的 IOMMU domain。要搬三样：
1. IOMMU 页表（用 generic_pt 序列化）
2. IOMMU root table
3. 相关 context entry（以及 PASID table）

### 三个硬约束

1. HWPT **只有当只包含 file 型 DMA 映射时**才能保留（匿名内存搬不了）。
2. 这些映射用的 memfd 必须被 **`F_SEAL_SEAL`** 封印（保证序列化期间映射不变）。
3. 新内核里被恢复的设备会被主动 **claim DMA ownership** → 任何绑非 vfio 驱动/新 iommufd 的尝试都 `-EBUSY`。

### 16 个补丁分层

```
P12  iommufd UAPI (MARK_PRESERVE ioctl)           ← 用户态入口
P13-14 iommufd file handler (preserve/retrieve)
P15  vfio cdev 保 iommufd 状态
P16  selftest
─────────────────────────────────────────────
P03  IOMMU domain preserve/unpreserve/restore     ← 核心 API
P04  device + IOMMU HW preserve/unpreserve
P08  VT-d restore IOMMU + reclaim DID
P09  IOMMU core restore + reattach domain
P10  VT-d preserve PASID table
─────────────────────────────────────────────
P02  IOMMU FLB handler (KHO 序列化骨架)            ← 数据搬运层
P05  iommu-pages preserve/unpreserve/restore
P06  generic_pt (iommupt) preserve/restore
─────────────────────────────────────────────
P07  VT-d 驱动 preserve/unpreserve ops             ← 硬件驱动
P11  get preserved state API
─────────────────────────────────────────────
P01  LUO internal file API                         ← LUO 扩展
```

### 关键补丁要点

- **P01**：新增 `liveupdate_get_token_outgoing()` / `liveupdate_get_file_incoming()`，让内核子系统能程序化地跨子系统查找 fd；所有 file 回调参数加 `session` 字段。
- **P02 IOMMU FLB**：定义三数组链表（IOMMU HW / Domain / Device），每节点一页，链表组织，指针全是物理地址。四回调 preserve/unpreserve/retrieve/finish。失败时 `BUG_ON`。
- **P03 domain preserve**：`iommu_domain_preserve()` 调驱动 `ops->preserve` 填 `top_table_phys`；不需要独立 refcount，靠 LUO session 生命周期保证一致清理。
- **P04 device + HW**：`iommu_preserve_device()` 触发设备/IOMMU HW 保留；IOMMU HW 用引用计数（一个 DMAR 单元管几十设备，root table 只保一次）。
- **P12 MARK_PRESERVE ioctl**：`IOMMU_HWPT_LIVEUPDATE_MARK_PRESERVE`，用户给 `hwpt_token`，XArray 打标记，token 须唯一（否则 `-EADDRINUSE`），**不可 unmark**。

### Phase 1 vs Phase 2

| Phase 1（本系列） | Phase 2（未来） |
|---|---|
| preserve 路径完整 | iommufd restore（用户态 reclaim） |
| 新内核早启动 restore + claim | 重建 iommufd 内核对象 |
| 阻止新绑定（`-EBUSY`） | 解除阻止，交还用户态 |
| 只做 Intel VT-d | 扩展到 AMD-Vi / ARM SMMUv3 |

---

## 7. BDF 稳定性与 PCI 两遍枚举

### 是否需要保留所有设备的 BDF？

**不需要。** 必须分清两个概念：

| 概念 | 范围 | 含义 |
|---|---|---|
| A. "被保留"(preserve) | 被直通端点 + 它到 root 的上游桥链 | 状态序列化进 KHO |
| B. "BDF 不漂移"(inherit bus numbers) | **整棵树的所有桥** | 继承旧内核 bus number，≠ 被 preserve |

真正被 preserve 的只有"端点 + 其祖先桥"这一条到根的路径（`pci_liveupdate_preserve_path()` 递归保到 root，桥用引用计数）。与该链路无关的设备根本不进 `pci_ser`，新内核里照常重新枚举。

### BDF 三字段的来源

| 字段 | 谁决定 | 软件能改吗 |
|---|---|---|
| **B**us | 上游桥的 secondary bus 寄存器 | ✅ 唯一软件分配项 |
| **D**evice | 物理拓扑/插槽位置 | ❌ 硬件固定 |
| **F**unction | 设备自己实现的功能号 | ❌ 硬件固定 |

→ D/F 是**探测**出来的不是分配的，不可能错发。唯一可能撞号的只有 Bus number。

### 两遍扫描 + read-back/allocate 分支

`pci_scan_child_bus_extend()` 对每层桥遍历两遍：
- **Pass 0**：处理"已配好"的桥 → read-back（沿用旧 secondary/subordinate，不分配）。
- **Pass 1**：处理"需重配"的桥 → allocate（`next_busnr = max + 1`，写新号）。

目的：Pass 0 先锁定所有已知领地（推高 max），Pass 1 从 `max+1` 起步，天然避开。

`pci_scan_bridge_extend()` 核心分支：

```c
if ((secondary || subordinate) && !pci_should_assign_new_buses(dev) && !broken) {
    // READ-BACK：沿用旧号，pass==1 时 goto out
    child = pci_add_new_bus(bus, dev, secondary);
    pci_bus_insert_busn_res(child, secondary, subordinate);
} else {
    // ALLOCATE：pass==0 时清空配置 goto out；pass==1 时
    next_busnr = max + 1;
    pci_write_config_dword(dev, PCI_PRIMARY_BUS, ...);  // 写新号
}
```

### Live Update 下的强制继承

```c
static bool pci_should_assign_new_buses(struct pci_dev *dev)
{
    if (dev->liveupdate_inherit_buses)
        return false;          // 永远走 read-back，连 assign-busses 都覆盖
    return pcibios_assign_all_busses();
}
```

效果：所有桥全走 read-back，ALLOCATE 路径全程不执行 → 没有任何"发号"动作 →
被保留设备的 BDF 不是"被避让"而是"压根没有重新分配这一步"。遇坏桥则直接拒绝重配并不枚举下游。

### 为什么必须全局继承（不能只冻结子树）

Bus number 在一个 domain 内是扁平全局命名空间。若只冻结被保留子树而让无关桥走 ALLOCATE，
`max+1` 可能算出与被保留设备 bus 重叠的窗口 → 撞号 → IOMMU 翻译失效。
所以必须让整个 domain 所有桥都 read-back。

> kexec 不 reset 桥，旧值还在寄存器里 → 新内核照抄 → bus 号天然不变 → BDF 天然不变。

---

## 8. IOMMU HW 是什么 + preserve/restore 全貌

### IOMMU HW 是什么

物理存在的 IOMMU 硬件翻译单元。Intel VT-d 下每个对应一个 `struct intel_iommu`，
系统里通常有多个（每组根端口一个）。关键字段：

```c
struct intel_iommu {
    void __iomem *reg;             // MMIO 寄存器虚拟地址
    u64 reg_phys;                  // MMIO 物理地址 ← 用作 "token"
    struct root_entry *root_entry; // root table 内核虚拟地址
    struct ida domain_ida;         // domain ID 分配器
    ...
};
```

### 三个数组分别保了什么

| 数组 | 类比 | 实际内容 |
|---|---|---|
| IOMMU HW array | 收费站本身 | root table / context table（每 BDF 的翻译配置） |
| Domain array | 收费规则手册 | IOMMU 页表（多级，GPA→HPA） |
| Device array | 车牌登记表 | 设备 BDF + 绑定 domain + 绑定 IOMMU + DID |

### 硬件翻译链

```
设备 DMA (requester=BDF, addr=GPA)
  → DMAR_RTADDR_REG → root table
  → root[bus] → context table
  → context[devfn]: .lo=页表根PA, .hi=DID
  → 多级页表查 GPA → HPA → 访问物理内存
```

### Preserve 保了什么（精确字段）

- **iommu_hw_ser**：`token = reg_phys`；root table + 所有 context table 物理页标 KHO 保留。**不保**：MMIO 寄存器值、invalidation queue、中断配置。
- **iommu_domain_ser**：`top_table_phys`（页表根 PA）、`top_level`、`vasz`；页表树所有**中间节点**物理页标保留（**不保**叶子指向的数据页，那是 memfd_luo 的事）。
- **iommu_device_ser**：`devid`(BDF)、`pci_domain_nr`、`attachment_id`(DID)、`domain_phys`/`iommu_phys`（物理地址交叉引用）。

### Restore 两阶段

**阶段 A：早启动（PCI 枚举前）**
```
liveupdate_flb_get_incoming() → FLB retrieve() → kho_restore_folio 恢复所有数组页
遍历 iommu_hw_array：token(reg_phys) 匹配本内核 intel_iommu
  恢复 root table（不用重写 DMAR_RTADDR：kexec 没 reset 硬件，寄存器还指旧表）
  扫描 context entry 找出在用 DID → IDA 预占
  clear_unpreserved_context_entries()：清掉非保留设备的 context entry（安全）
```

**阶段 B：设备 probe（PCI 枚举后）**
```
iommu_probe_device() 检测 incoming device_ser
  → iommu_restore_domain(): generic_pt restore 恢复页表树 → 重建 struct iommu_domain
  → iommu_attach_device(): context entry 已对，只更新内核结构
  → iommu_group_claim_dma_owner(): 阻止非 VFIO 驱动/新 iommufd 绑定
```

### 核心哲学

> **物理页原地不动（KHO 保证），内核结构体全部重建（new alloc），然后把新结构体的指针"嫁接"到旧物理页上。**

| 对象 | kexec 后处理 | 复用/重建 |
|---|---|---|
| IOMMU 寄存器 (DMAR_RTADDR) | 不变（没 reset） | 原样复用 |
| root/context/页表 物理页 | `kho_restore_folio()` 认领 | 物理页复用 |
| `struct intel_iommu` / `iommu_domain` | new alloc + 指向旧物理页 | 重建结构体 |
| DID | 从 context entry 扫出 → IDA 预占 | 重建 IDA 状态 |
| Invalidation Queue / 中断 | 重新分配 | 全新创建 |

---

## 9. KHO：物理页如何跨 kexec 标记与保护

### 标记入口

所有要跨 kexec 的物理页，最终都走 `kho_preserve_folio()` → 写入同一棵 **radix tree**：

```c
kho_preserve_folio(folio):
  pfn = folio_pfn(folio); order = folio_order(folio);
  kho_radix_add_page(&kho_out.radix_tree, pfn, order)
    key = kho_radix_encode_key(PFN_PHYS(pfn), order)  // order+地址混合编码
    沿 radix tree 走到叶子 bitmap → __set_bit(idx)
```

radix tree 本身也用物理地址链接（`node->table[idx] = virt_to_phys(new_node)`），因为它也要跨 kexec。

### radix tree 结构（6 级）

- 中间节点 `kho_radix_node`：512 个 `u64`（物理地址），指向下一级。
- 叶子 `kho_radix_leaf`：32768 位 bitmap，每个 set 位代表一个被保留页。
- key 编码：高位是"order bit"（位置代表 order），低位是 `pa >> (PAGE_SHIFT+order)`。
- 一个叶子 bitmap 可覆盖 128MB（order-0）的物理地址范围。

### 三道保护防线

| 层次 | 保护什么 | 机制 | 时效 |
|---|---|---|---|
| Layer 1 | 保留页不被新内核 image 覆盖 | kexec 加载时查 radix tree 避开 | kexec 到跳转前 |
| Layer 2 | 新内核早期 memblock 分配不踩保留页 | `memblock_set_kho_scratch_only()`（只从 scratch CMA 区分配） | 极早期到 buddy 初始化 |
| Layer 3 | buddy 后保留页不进 free list | `memblock_reserve()` + `mark_noinit` | buddy 初始化后永久 |

### 新内核认领

```c
kho_restore_folio(phys):
  page = pfn_to_online_page(PHYS_PFN(phys))
  WARN_ON(info.magic != KHO_PAGE_MAGIC)   // 校验魔数 "KHOP"
  page->private = 0                        // 清标记（防二次 restore/UAF）
  kho_init_folio(page, info.order)         // 设 refcount=1
  adjust_managed_page_count()
```

`page->private` 里存 `{magic=0x4b484f50 "KHOP", order}`：校验 + 一次性使用 + 携带 order 信息。

**完整保护链**：`radix tree bit` → `kexec 跳过` → `memblock_reserve` → `不进 buddy` → `restore 认领`。

---

## 10. kexec 如何向新内核传递信息

### 传递机制（x86）

复用 boot protocol 的 **`setup_data` 链表**。旧内核 `setup_kho()` 在 boot_params 里追加一个
`SETUP_KEXEC_KHO` 节点，携带一个 32 字节 `struct kho_data`：

```c
struct kho_data {
    __u64 fdt_addr;      // KHO 顶层 FDT 物理地址
    __u64 fdt_size;      // = PAGE_SIZE
    __u64 scratch_addr;  // scratch 区物理地址
    __u64 scratch_size;
};
```

新内核 `parse_setup_data()` → `add_kho()` → `kho_populate(fdt, scratch)` 开始恢复。

> kho_data 只有 32 字节，只携带物理地址。真正的数据全在那些地址指向的内存里。

### 传递的 FDT 层级（含真实数字举例）

```
KHO 顶层 FDT (0x1_FF00_0000, 4KB)
  compatible = "kho-v3"
  preserved-memory-map = 0x1_FE00_0000   → radix tree root
  子节点 LUO:
    preserved-data = 0x1_FD00_0000        → LUO 子 FDT
  子节点 kexec-metadata:
    preserved-data = 0x1_FC00_0000        → kho_kexec_metadata

LUO FDT (0x1_FD00_0000)
  compatible = "luo-v1"; liveupdate-number = 3
  luo-session: luo-session-header = 0x1_FB00_0000
  luo-flb:     luo-flb-header = 0x1_FA00_0000

session header (0x1_FB00_0000)
  luo_session_header_ser { count=1 }
  luo_session_ser[0] { name="vm-session-001", file_set={files=0x1_F900_0000, count=3} }

file 数组 (0x1_F900_0000)
  [0] { compatible="memfd-v1",  data=0x1_F800_0000, token=100 }
  [1] { compatible="iommufd",   data=0x1_F700_0000, token=200 }
  [2] { compatible="vfio-pci",  data=0x1_F600_0000, token=300 }

flb header (0x1_FA00_0000)
  luo_flb_header_ser { pgcnt=1, count=2 }
  luo_flb_ser[0] { name="pci core",            data=..., count=1 }
  luo_flb_ser[1] { name="iommu-liveupdate-v1", data=..., count=2 }

kexec-metadata (0x1_FC00_0000)
  { version=1, previous_release="7.1.0-rc6", kexec_count=3 }
```

### Scratch 区域的作用

新内核 boot 极早期（buddy 未初始化）memblock 需要分配内存。`memblock_set_kho_scratch_only()`
强制所有早期分配只从 scratch CMA 区取，保证不踩任何保留页。

---

## 11. 保留页的维护：元数据页 vs memfd 数据页

**底层标记机制完全相同**（都走 `kho_preserve_folio()` → radix tree），但**上层管理完全不同**。

| 维度 | 元数据页（序列化结构） | 数据页（memfd guest 内存） |
|---|---|---|
| 来源 | `kho_alloc_preserve(size)` 新分配 | 已存在的 shmem folio |
| 数量级 | 几页到几十页 | 几十万到几百万页（2GB=50万页） |
| 分配时机 | preserve 时才分配 | VM 运行时早已分配 |
| 内容 | 序列化结构（物理地址指针） | VM 实际内存内容 |
| 索引方式 | FDT + 结构体内嵌物理地址链表 | folios_ser 数组(pfn+index+flags) |
| 标记方式 | `kho_alloc_preserve` 内部自动 | 逐 folio 循环 `kho_preserve_folio` |
| 额外保护 | 无需（内核独占） | `memfd_pin_folios`（防 reclaim/CMA 迁移） |
| 恢复方式 | `phys_to_virt` 直接当 struct 用 | 逐个 restore + 插入 page cache/LRU/memcg |
| 生命终结 | `kho_unpreserve_free` | 随 memfd 正常使用/释放 |

### 元数据页

```c
void *kho_alloc_preserve(size_t size) {
    folio = folio_alloc(GFP_KERNEL | __GFP_ZERO, get_order(size));
    kho_preserve_folio(folio);     // 分配+清零+标记 一步完成
    return folio_address(folio);
}
```
使用方：LUO FDT、file/session/flb header 数组、memfd 的小元数据 struct、IOMMU FLB、kexec-metadata。

### memfd 数据页

```c
memfd_luo_preserve_folios(file, ...):
  memfd_pin_folios(...)               // ① pin（防 reclaim/迁移，填补空洞）
  for each folio:
      kho_preserve_folio(folio)       // ② 标记（同底层调用）
      folio_mark_dirty(folio)         // 强制标脏防 reclaim
      pfolio->{pfn,index,flags} = ... // ③ 记录元数据
  kho_preserve_vmalloc(folios_ser)    // ④ 数组本身也保留
```

恢复（memfd 独有的"重新入册"）：
```c
memfd_luo_retrieve_folios():
  for each: folio = kho_restore_folio(phys)
            mem_cgroup_charge(folio)
            shmem_add_to_page_cache(folio, mapping, index)  // 插入 page cache！
            folio_mark_dirty/uptodate; folio_add_lru()
```

### 为什么 memfd 不用 `kho_alloc_preserve`

1. 数据已在那里（VM 内存正在用），不能新分配再拷贝（慢且费内存）。
2. 物理地址不能变（IOMMU 页表指向这些物理页）。
3. folio 可能是大页（THP order-9），`kho_alloc_preserve` 做不了"保留别人已有的页"。

> radix tree **不区分**两类页——它只知道"这个 PFN 被保留了"，不知道是谁、为什么。

---

## 12. 关键设计决策汇总

| 决策 | 理由 |
|---|---|
| 只支持 file-based + SEAL 的映射 | 匿名内存新内核里已回收，页表会指向垃圾 |
| restore 失败 → BUG_ON / panic | 设备可能正 DMA，页表不完整 = 跨租户泄漏风险 |
| IOMMU HW 用 refcount / domain 不用 | IOMMU HW 被多设备共享；domain 1:1 由 session 管 |
| 显式 MARK ioctl + 无 unmark | 避免 TOCTOU，简化实现 |
| PCI 全局继承所有桥 bus number | bus number 是全局命名空间，部分冻结会撞号 |
| VFIO base 版仍 reset 设备 | 阶段性妥协，骨架先行，带电连续性靠三层凑齐 |
| 物理页复用 + 结构体重建 | 新内核完全自主、零拷贝、硬件翻译零中断 |
| token = MMIO base (reg_phys) | 全局唯一、硬件固定、新旧内核一致 |
| Phase 1/2 切分 | 降低单系列评审压力，先跑通 preserve+boot restore |

---

## 13. 参考来源

代码（本地树 v7.1-rc6）：
- `kernel/liveupdate/`（luo_core/file/flb/session.c, kexec_handover.c）
- `include/linux/liveupdate.h`, `include/linux/kho/abi/*.h`
- `mm/memfd_luo.c`
- `drivers/pci/probe.c`
- `drivers/iommu/intel/iommu.h`

上游补丁集（内容经改写以符合引用许可）：
- [PCI v4 cover](https://lore.kernel.org/all/20260423212316.3431746-1-dmatlack@google.com/)
- [PCI v4 05/11 Inherit bus numbers](https://lore.kernel.org/all/20260423212316.3431746-6-dmatlack@google.com/)
- [PCI v4 06/11 Auto-preserve upstream bridges](https://lore.kernel.org/all/20260423212316.3431746-7-dmatlack@google.com/)
- [VFIO v4 cover](https://lore.kernel.org/all/20260511234802.2280368-1-vipinsh@google.com/)
- [iommufd v2 cover](https://lore.kernel.org/all/20260427175633.1978233-1-skhawaja@google.com/)
- [iommufd v2 P01–P04, P12](https://lore.kernel.org/all/20260427175633.1978233-1-skhawaja@google.com/)

相关分析文档（同目录 `docs/`）：
- `docs/iommufd-luo-patchset-deep-analysis.md`
- `docs/pci-bus-enum-two-pass-flowchart.md`
