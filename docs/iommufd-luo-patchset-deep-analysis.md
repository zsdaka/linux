# iommufd Live Update Patchset 深度分析

**系列**: `[PATCH v2 00/16] iommu: Add live update state preservation`  
**作者**: Samiullah Khawaja (Google), Pasha Tatashin, YiFei Zhu  
**日期**: 2026-04-27  
**规模**: +3016 行, 38 文件, 16 个补丁  
**状态**: 邮件列表评审中 (v2)  

---

## 一、整体目标：保住什么？

一台机器上一个 VM 通过 VFIO 直通了一个 PCIe 设备。kexec 换内核后，这个设备的 **DMA 翻译能力** 必须无中断延续。具体要保的东西：

| 要保的对象 | 对应内核结构 | 物理实体 |
|---|---|---|
| IOMMU 页表（GVA→HPA 映射） | `struct iommu_domain` + generic_pt | 多级页表物理页 |
| IOMMU 硬件上下文 | root table + context entry | VT-d DMAR 寄存器指向的内存 |
| 设备-domain 绑定关系 | `dev->iommu` attachment | context entry 里的 DID 字段 |
| PASID table（如有） | per-device PASID directory | 物理页 |

---

## 二、16 个补丁逐个拆解

### 分层架构图

```
┌─────────────────────────────────────────────────────────────────┐
│ Patch 12: iommufd UAPI (MARK_PRESERVE ioctl)                    │ ← 用户态入口
│ Patch 13-14: iommufd file handler (preserve/retrieve 回调)       │
│ Patch 15: vfio cdev 保 iommufd 状态                              │
│ Patch 16: selftest                                              │
├─────────────────────────────────────────────────────────────────┤
│ Patch 03: IOMMU domain preserve/unpreserve/restore (IOMMU core) │ ← 核心 API
│ Patch 04: device + IOMMU HW preserve/unpreserve                  │
│ Patch 08: VT-d restore IOMMU + reclaim DID                       │
│ Patch 09: IOMMU core restore + reattach domain                   │
│ Patch 10: VT-d preserve PASID table                              │
├─────────────────────────────────────────────────────────────────┤
│ Patch 02: IOMMU FLB handler (KHO 序列化骨架)                     │ ← 数据搬运层
│ Patch 05: iommu-pages preserve/unpreserve/restore                │
│ Patch 06: generic_pt (iommupt) preserve/restore 回调             │
├─────────────────────────────────────────────────────────────────┤
│ Patch 07: VT-d 驱动 preserve/unpreserve 实现                     │ ← 硬件驱动
│ Patch 11: get preserved state API (供 iommufd/VFIO 查询)         │
├─────────────────────────────────────────────────────────────────┤
│ Patch 01: LUO internal file API (kernel 内部 preserve/retrieve)  │ ← LUO 扩展
└─────────────────────────────────────────────────────────────────┘
```

---

### Patch 01: LUO 内核内部文件保留 API

**问题**: LUO 原有设计只让**用户态**通过 ioctl 来 preserve/retrieve fd。但 iommufd 需要在内核里**程序化地**保留/取回另一个子系统（比如 memfd）的 fd，而不依赖用户态交互。

**解法**: 新增两个内核 API：
```c
// 旧内核：给定一个 file，查找它在 session 中的 token
int liveupdate_get_token_outgoing(session, file, &token);

// 新内核：给定 token，从 session 中取回 struct file
int liveupdate_get_file_incoming(session, token, &filep);
```

**关键设计点**:
- `args->session` 被加入所有文件回调参数中 → 子系统的 preserve/retrieve 回调能拿到 session 句柄，进而操作同一 session 中其他文件。
- 这使得 vfio cdev 在 preserve 时能"看到" iommufd fd 的 token，反之亦然。

---

### Patch 02: IOMMU FLB — KHO 序列化骨架

**这是整个系列的"数据容器"设计。**

#### 内存布局

```
                    KHO 物理内存（跨 kexec 保留）
                    
┌──────────────────────────────────────────────┐
│          struct iommu_flb_ser                 │  ← FLB 顶层，一个实例
│  - iommu_array_phys ──────────────┐          │
│  - iommu_domain_array_phys ───┐   │          │
│  - device_array_phys ──┐      │   │          │
└────────────────────────┼──────┼───┼──────────┘
                         │      │   │
     ┌───────────────────┘      │   └────────────────────────┐
     ▼                          ▼                            ▼
┌─────────────────┐      ┌─────────────────┐         ┌─────────────────┐
│ device_array[0] │      │ domain_array[0] │         │ iommu_array[0]  │
│ hdr.next → [1]  │      │ hdr.next → [1]  │         │ hdr.next → NULL │
│ hdr.nr_objects  │      │ hdr.nr_objects  │         │ hdr.nr_objects  │
│                 │      │                 │         │                 │
│ objects[]:      │      │ objects[]:      │         │ objects[]:      │
│  iommu_device  │      │  iommu_domain   │         │  iommu_hw_ser   │
│  _ser          │      │  _ser           │         │                 │
│  ...           │      │  ...            │         │  ...            │
└─────────────────┘      └─────────────────┘         └─────────────────┘
         │                       │
         ▼                       ▼
┌─────────────────┐      ┌─────────────────┐
│ device_array[1] │      │ domain_array[1] │   ... (链表，满了就追加新页)
│ hdr.next → NULL │      │ hdr.next → ...  │
└─────────────────┘      └─────────────────┘
```

**设计要点**:
- 每个 array 是**一个物理页**（PAGE_SIZE），链表组织
- `nr_objects` 计数当前页里有多少个对象
- 满了 → `kho_alloc_preserve(PAGE_SIZE)` 分配新页，前一个的 `next_array_phys` 指过去
- 所有指针都是**物理地址 u64**（跨 kexec 的铁律）
- `iommu_flb_obj` 是纯本内核的缓存结构（有 mutex、有当前 array 指针），不过 kexec

#### 四个 FLB 回调

| 回调 | 时机 | 做什么 |
|---|---|---|
| `preserve` | 第一个 iommufd fd 被 preserve 时 | 分配 `iommu_flb_ser` + 三个初始数组页（KHO 内存） |
| `unpreserve` | 所有 fd 被取消保留 | `kho_unpreserve_free()` 释放所有 KHO 页 |
| `retrieve` | 新内核首次访问 FLB 时 | `kho_restore_folio()` 恢复每个物理页，重建 `iommu_flb_obj` |
| `finish` | 所有 fd 完成 live update | `folio_put()` 归还物理页给新内核页帧分配器 |

**失败哲学（BUG_ON）**:
```c
BUG_ON(!kho_restore_folio(array_phys));
```
注释原话（改写）：如果恢复失败，保留的 domain 里映射的内存可能已被设备通过 DMA 破坏，无法确认内存完整性，BUG_ON 是最安全选择。

---

### Patch 03: IOMMU Domain Preserve/Unpreserve

**核心 API（被 iommufd 调用）**:

```c
int iommu_domain_preserve(struct iommu_domain *domain, 
                          struct iommu_domain_ser **ser);
void iommu_domain_unpreserve(struct iommu_domain *domain);
```

**流程**:
1. 从 FLB 拿到 `iommu_flb_obj`（通过 `liveupdate_flb_get_outgoing`）
2. 在 domain array 里分配一个 `iommu_domain_ser` 槽位
3. 调用**驱动回调** `domain->ops->preserve(domain, domain_ser)` → 驱动在这里填 `top_table_phys`（根页表物理地址）和 `top_level`
4. 把 `domain_ser` 指针存到 `domain->preserved_state`

**新增的 iommu_domain_ops**:
```c
struct iommu_domain_ops {
    ...
    int (*preserve)(struct iommu_domain *domain, struct iommu_domain_ser *ser);
    void (*unpreserve)(struct iommu_domain *domain, struct iommu_domain_ser *ser);
    int (*restore)(struct iommu_domain *domain, struct iommu_domain_ser *ser);
};
```

**巧妙之处 — 不需要独立引用计数**:
> 注释原话（改写）：正确性依赖 LUO session 的生命周期。所有资源（iommufd、vfio 设备）在同一个 session 中被保留。如果 session 被销毁，所有文件的 unpreserve 回调都会被调用，保证一致清理。

---

### Patch 04: Device + IOMMU HW Preservation

**这是把"设备绑定关系"和"IOMMU 硬件实例"存下来的补丁。**

两个新的 IOMMU ops（加到 `struct iommu_ops`）:
```c
int (*preserve_device)(struct device *dev, struct iommu_device_ser *device_ser);
void (*unpreserve_device)(struct device *dev, struct iommu_device_ser *device_ser);
int (*preserve)(struct iommu_device *iommu, struct iommu_hw_ser *iommu_ser);
void (*unpreserve)(struct iommu_device *iommu, struct iommu_hw_ser *iommu_ser);
```

#### `iommu_preserve_device()` 的调用链（被 vfio cdev preserve 触发）:

```
vfio_cdev preserve
  └─► iommu_preserve_device(domain, dev, &preserved_state)
        ├── 校验：只允许 PCI 设备 + domain 已保留 + DMA owner 已 claim
        ├── 拿 FLB，加锁
        ├── alloc_iommu_device_ser()  ← 在 device array 里分配槽位
        ├── iommu_preserve_locked(iommu_dev, flb_obj)
        │     └── 如果这个 IOMMU HW 第一次被保留：
        │           alloc_iommu_hw_ser()
        │           iommu->ops->preserve(iommu, hw_ser)  ← VT-d: 保 root table
        │         否则：refcount++  ← 同一个 IOMMU 下多设备共享
        ├── 填 device_ser:
        │     .domain_phys = __pa(domain->preserved_state)  ← 指向已保留的 domain
        │     .iommu_phys = __pa(iommu->outgoing_preserved_state) ← 指向已保留的 IOMMU HW
        │     .devid = pci_dev_id(pdev)  ← BDF
        │     .pci_domain_nr = pci_domain_nr()
        └── iommu->ops->preserve_device(dev, device_ser)  ← VT-d: 保 context entry 中设备部分
```

**关键设计**: IOMMU HW 用**引用计数**。一个 IOMMU 下面可能挂了多个被保留设备，IOMMU 的全局态（root table）只保一次，refcount 跟踪。最后一个设备 unpreserve 时才真正释放。

---

### Patch 05: iommu-pages 保留/恢复

IOMMU 页表是用 `iommu_alloc_pages_node()` 分配的物理页。这个补丁加了：
```c
int iommu_preserve_pages(void *vaddr, int order);      // 标记 KHO 保留
void iommu_unpreserve_pages(void *vaddr, int order);   // 取消保留
int iommu_restore_pages(phys_addr_t phys, int order);  // 新内核恢复
```
底层调 `kho_preserve_folio()` / `kho_restore_folio()`。还更新了 memcg 统计，确保恢复后的页被正确记账。

---

### Patch 06: Generic Page Table (iommupt) Preserve/Restore

这是 iommu generic_pt 框架（用 C template 生成各种格式页表）的扩展。

```c
// 在 struct iommu_pt_ops 里新增：
int (*preserve)(struct pt_common *common, struct iommu_domain_ser *ser);
int (*restore)(struct pt_common *common, struct iommu_domain_ser *ser);
```

**preserve**: 遍历整棵页表树（用 `pt_descend` 递归），对每个中间/叶子页调 `iommu_preserve_pages()`。把根物理地址存入 `ser->top_table_phys`。

**restore**: 从 `ser->top_table_phys` 拿到根，递归遍历恢复每一页（`iommu_restore_pages`）。失败？忽略 `pt_descend` 的错误（注释：dead code，如果根能恢复子页也一定能恢复）。

---

### Patch 07: Intel VT-d 驱动 — preserve/unpreserve

`drivers/iommu/intel/liveupdate.c` (337 行)

实现上面定义的 4 个 ops：

| ops | VT-d 做什么 |
|---|---|
| `preserve(iommu, hw_ser)` | 把 MMIO base 地址作为 token 存入 `hw_ser->token`；遍历 root table 每个 root entry/context entry，对每一页调 `iommu_preserve_pages()` |
| `unpreserve(iommu, hw_ser)` | 逆操作，`iommu_unpreserve_pages()` 释放所有 root/context 页的 KHO 标记 |
| `preserve_device(dev, device_ser)` | 把 context entry 中该设备的 attachment_id (DID) 存入 `device_ser->domain_iommu_ser.attachment_id` |
| `unpreserve_device(dev, device_ser)` | 无特殊操作（VT-d 不需要） |

**token = MMIO base address**: 新内核恢复时靠这个把序列化的 IOMMU 状态匹配到正确的硬件单元。

---

### Patch 08: VT-d Restore — IOMMU State + Reclaim DIDs

新内核启动时：

1. VT-d 驱动通过 `liveupdate_flb_get_incoming()` 拿到 IOMMU FLB
2. 遍历 `iommu_hw_array`，按 token（MMIO base）匹配到本内核的 `struct intel_iommu`
3. 恢复 root table 指针（让硬件继续使用旧页表）
4. 遍历 root/context entry，找到所有在用的 DID
5. **在 IDA 分配器中"预占"这些 DID** → 保证新内核不会把这些 DID 分给别的 domain

**`clear_unpreserved_context_entries()`**: 遍历所有 context entry，把**不属于保留设备的条目清零** → 保证旧内核遗留的非保留设备不会继续有翻译能力（安全）。

---

### Patch 09: IOMMU Core — Restore + Reattach Domain

在设备 probe 时：

```
iommu_probe_device()
  └─► 检查 dev 是否有 preserved state (incoming)
        └─► iommu_restore_domain(dev, domain_ser)
              ├── domain->ops->restore(domain, ser)  ← 重建内核 struct
              ├── iommu_attach_device(domain, dev)   ← 重新 attach
              └── iommu_group_claim_dma_owner(group)  ← claim ownership!
```

**claim_dma_owner** 的效果：
- 阻止任何非 VFIO 驱动绑定到这个设备
- 阻止新的 iommufd 绑定
- 保证设备"属于旧 session，直到用户态来 retrieve"

---

### Patch 10: VT-d Preserve PASID Table

除了 root/context table，直通设备如果用了 PASID（Scalable Mode），还得保 PASID directory：
```c
intel_preserve_pasid_table(dev, device_ser):
  for each active PASID entry in device's PASID table:
      iommu_preserve_pages(entry_page, order)
```

注意 v2 changelog："cleanup the full PASID directory instead of only 4K" — 之前只保了一页，现在保完整目录。

---

### Patch 11: Get Preserved State API

提供查询接口让 iommufd/VFIO 在 preserve 回调中获取已保留的 domain/device state：
```c
void *dev_iommu_preserved_state(struct device *dev);
```

---

### Patch 12: iommufd UAPI — MARK_PRESERVE ioctl ⭐

**这是用户态的"入口"。**

新 ioctl:
```c
struct iommu_hwpt_liveupdate_mark_preserve {
    __u32 size;
    __u32 hwpt_id;       // 要标记的 HWPT 对象 ID
    __u64 hwpt_token;    // 用户给的唯一标识（恢复时用来匹配）
};
#define IOMMU_HWPT_LIVEUPDATE_MARK_PRESERVE ...
```

**实现逻辑**:
1. 通过 `hwpt_id` 拿到 `iommufd_hwpt_paging` 对象
2. 加 `liveupdate_mutex`
3. 在 XArray 中扫描是否有重复 token → 有则返回 `-EADDRINUSE`
4. 给 HWPT 打上 `IOMMUFD_OBJ_LIVEUPDATE_MARK` XArray 标记
5. 存 token 到 `hwpt_paging->liveupdate_token`

**约束（UAPI 文档写死的）**:
- HWPT 一旦标记，**无法取消标记**（没有 unmark）
- 只有 **file-based 映射**（memfd）的 HWPT 才能被保留；如果里面有匿名内存映射，preserve 阶段会失败
- token 在同一个 iommufd 内必须唯一

---

### Patch 13-14: iommufd File Handler

iommufd 注册一个 LUO file handler（`compatible = "iommufd"`）。

**preserve 回调**:
```
iommufd_preserve(args):
  遍历 XArray 中所有带 LIVEUPDATE_MARK 标记的 HWPT:
    检查所有映射是否都是 file-based (check at IOPT level)
    iommu_domain_preserve(hwpt->domain, &domain_ser)
      └── 触发 generic_pt preserve + VT-d preserve
```

**iommufd FLB 注册**:
```c
iommu_liveupdate_register_flb(&iommufd_handler);
// → IOMMU core FLB 挂到 iommufd file handler 上
// → 第一个 iommufd fd preserve 时，IOMMU FLB.preserve() 被自动触发
```

---

### Patch 15: vfio/pci Preserve iommufd State

当 vfio cdev 被 preserve 时，它需要把**自己和 iommufd 的绑定关系**也存下来。

```
vfio_pci_preserve(args):
  找到 bound iommufd fd
  liveupdate_get_token_outgoing(session, iommufd_file, &iommufd_token)
    // 拿到 iommufd 在同一 session 中的 token
  iommu_preserve_device(domain, dev, &preserved_state)
    // 保设备 + IOMMU HW（触发 Patch 04 的逻辑）
  // 把 iommufd_token + preserved_state 存到 vfio serialized data
```

新内核 retrieve 时反向：
```
vfio_pci_retrieve(args):
  liveupdate_get_file_incoming(session, iommufd_token, &iommufd_file)
    // 从 session 中取回 iommufd 的 struct file
  // 重建 vfio cdev ↔ iommufd 绑定
```

---

### Patch 16: Selftest

`tools/testing/selftests/iommu/iommufd_liveupdate_kexec_test.c`

两阶段测试：
- **Stage 1** (kexec 前): open cdev → 绑 iommufd → alloc HWPT → MARK_PRESERVE → preserve 到 session → 触发 kexec
- **Stage 2** (kexec 后): 打开 session → 验证设备不能被新 iommufd 绑定 → 验证 `-EBUSY`

---

## 三、完整数据流图

```
                    旧内核（在线运行）
                    ═══════════════
                    
用户态 VMM ─────────────────────────────────────────────────────────
  │                                                                  
  │ ① IOMMU_HWPT_LIVEUPDATE_MARK_PRESERVE(hwpt_id, token)           
  │    → iommufd: 在 XArray 打标记                                   
  │                                                                  
  │ ② LUO PRESERVE(iommufd_fd)                                      
  │    → LUO core: 调 iommufd file handler .preserve()               
  │         ├── 首次触发 IOMMU FLB .preserve()                       
  │         │     → alloc iommu_flb_ser + 三个数组页 (KHO)            
  │         └── 遍历 marked HWPTs:                                   
  │               └── iommu_domain_preserve()                        
  │                     ├── alloc domain_ser (in domain array)       
  │                     └── VT-d domain ops->preserve()              
  │                           └── generic_pt: 遍历页表树             
  │                                 对每一页 iommu_preserve_pages()  
  │                                 ser->top_table_phys = root PA    
  │                                                                  
  │ ③ LUO PRESERVE(vfio_cdev_fd)                                    
  │    → LUO core: 调 vfio file handler .preserve()                  
  │         └── iommu_preserve_device(domain, dev)                   
  │               ├── alloc device_ser (in device array)             
  │               ├── iommu_preserve_locked(iommu_dev)               
  │               │     ├── first time: alloc hw_ser + VT-d preserve │
  │               │     │   root table → iommu_preserve_pages()      
  │               │     └── else: hw_ser->refcount++                 
  │               ├── fill device_ser:                               
  │               │     .devid = BDF                                 
  │               │     .domain_phys = PA of domain_ser              
  │               │     .iommu_phys = PA of hw_ser                   
  │               └── VT-d preserve_device(): store DID              
  │                                                                  
───────── reboot() → freeze ────────────────────────────────────────
  │                                                                  
  │ 所有保留的 KHO 物理页不被踩                                       
  │ PCI: 不关 BME, 设备继续 DMA                                      
  │                                                                  
═══════════════════ kexec ══════════════════════════════════════════

                    新内核（早启动）
                    ═══════════════
                    
  │ ④ VT-d 驱动初始化:                                               
  │    liveupdate_flb_get_incoming(&iommu_flb)                       
  │      → 触发 IOMMU FLB .retrieve()                                
  │           kho_restore_folio() 恢复 flb_ser + 所有 array 页       
  │    遍历 iommu_hw_array:                                          
  │      token (MMIO base) 匹配 → 恢复 root table                   
  │      预占所有在用 DID                                             
  │      清除非保留设备的 context entry                               
  │                                                                  
  │ ⑤ IOMMU probe_device (PCI 枚举后):                               
  │    检测 incoming device_ser                                       
  │      → iommu_restore_domain():                                   
  │           generic_pt restore (恢复页表树所有页)                   
  │           重建 struct iommu_domain                                
  │           attach 到设备                                           
  │      → iommu_group_claim_dma_owner()                             
  │           → 阻止非 VFIO 驱动 / 新 iommufd 绑定                   
  │                                                                  
─────────── 用户态就绪 ─────────────────────────────────────────────
  │                                                                  
  │ ⑥ VMM 打开 /dev/liveupdate, retrieve session                    
  │    LUO RETRIEVE(token) → 取回 iommufd fd                         
  │    LUO RETRIEVE(token) → 取回 vfio cdev fd                       
  │    → 设备恢复完毕，VM 继续运行                                    
```

---

## 四、关键设计决策与权衡

### 1. 为什么要 MARK_PRESERVE 而不是自动保留所有 HWPT？

- 并非所有 HWPT 都能保留（匿名内存映射的不行）
- 用户态可能有多个 HWPT 但只想保一部分
- 显式标记避免意外保留、提供清晰的错误报告时机
- token 让恢复时能明确匹配

### 2. 为什么只支持 file-based 映射 + memfd SEAL？

- file-based 映射的物理页通过 **memfd_luo** 机制已经被保留
- 匿名内存映射的物理页新内核里已被回收 → 页表指向垃圾
- SEAL 保证 preserve 后到 kexec 之间，映射不会被修改 → 序列化的页表保持有效

### 3. 为什么 IOMMU HW 用引用计数而 domain 不用？

- 一个 IOMMU HW 实例被多个设备共享（一个 Intel DMAR unit 管几十个设备）→ 必须 refcount
- domain 是 per-HWPT 的，一对一关系，不需要额外 refcount
- domain 的生命周期由 LUO session 统一管理

### 4. 为什么 restore 失败直接 BUG_ON / panic？

- 设备可能正在对 domain 里映射的内存做 DMA
- 如果恢复失败（页表不完整），设备可能读到错误数据或写到别人的内存
- 这是**多租户安全问题**：宁可 panic 也不能让一个 VM 的 DMA 越界
- 和 LUO 核心的 `luo_restore_fail = panic` 哲学一致

### 5. Phase 1 vs Phase 2 的切分点

| Phase 1 (本系列) | Phase 2 (未来) |
|---|---|
| preserve 路径完整 | iommufd restore (用户态 reclaim) |
| 新内核早启动 restore + claim | 重建 iommufd 内核对象 |
| 阻止新绑定 (`-EBUSY`) | 解除阻止，交还给用户态 |
| VT-d 只实现 | 扩展到 AMD-Vi, ARM SMMUv3 |

---

## 五、与其他系列的接口关系

```
本系列 Patch 01 ──扩展──► LUO Core (已合入)
                           提供 get_token_outgoing / get_file_incoming

本系列 Patch 04/15 ──调用──► PCI v4 系列
                              pci_liveupdate_preserve(dev)

本系列 Patch 12-14 ──注册──► iommufd file handler ──挂载──► IOMMU FLB

本系列 Patch 15 ──嵌入──► VFIO v4 系列
                           vfio_pci_liveupdate.c 里调 iommufd preserve_device

本系列 Patch 05-06 ──使用──► iommu-pages + generic_pt (已在树)
                              只加了 preserve/restore 回调
```

---

## 六、社区评审焦点（从 v1→v2 changelog 提炼）

1. **TOCTOU 问题**: v1 里 HWPT mark + preserve 之间有竞争；v2 用 `liveupdate_mutex` + XArray 原子标记解决
2. **IDA/DID 恢复**: v1 没检查 IOMMU 关联；v2 在 `_restore_used_domain_id` 里校验
3. **PASID 清理**: v1 只清 4K；v2 清整个 PASID directory
4. **UAF 防护**: `iommu_hw_ser` 标 `restored=true` 后不能再次恢复
5. **映射检查**: v1 在 HWPT 层检查；v2 在 IOPT 层检查（更准确）
6. **UAPI 对齐**: struct 加 padding 保证 64-bit 对齐
7. **paging_domain_compatible()**: 恢复的 domain 跳过兼容性检查（特征已被裁剪到只有保留态）

---

## 七、一句话总结

这个系列做的事情等价于：**把一棵"活着的" IOMMU 页表树（连同所有中间节点物理页）和设备绑定关系像化石一样凝固在物理内存中，让新内核启动时像考古一样把它完整挖出来、立即让硬件继续用，中间不留任何翻译真空期。**

来源（内容已改写以符合引用许可）：
- [iommufd v2 cover letter](https://lore.kernel.org/all/20260427175633.1978233-1-skhawaja@google.com/)
- [Patch 01](https://lore.kernel.org/all/20260427175633.1978233-2-skhawaja@google.com/)
- [Patch 02](https://lore.kernel.org/all/20260427175633.1978233-3-skhawaja@google.com/)
- [Patch 03](https://lore.kernel.org/all/20260427175633.1978233-4-skhawaja@google.com/)
- [Patch 04](https://lore.kernel.org/all/20260427175633.1978233-5-skhawaja@google.com/)
- [Patch 12](https://lore.kernel.org/all/20260427175633.1978233-13-skhawaja@google.com/)
