# 第 03 章 DRM 设备模型与 ioctl

> 所属：第二篇 DRM 核心　|　前置：第 00、01、02 章　|　代码基线：Linux `7.1.0-rc6`
> 参考驱动：**Xe**（`drivers/gpu/drm/xe/`）——本书统一以 Xe 作为代码走读对象。
>
> 第一篇打好了硬件与内核设施的地基。从本章起，我们正式钻进 DRM 框架本身。一切的入口是这条链：
> **驱动注册设备 → 内核创建 `/dev/dri/*` 节点 → 用户态 open 得到一个上下文 → ioctl 把请求分发到处理
> 函数**。本章逐字段拆解四个根本结构（`drm_device`/`drm_driver`/`drm_minor`/`drm_file`），并沿 Xe 的
> 真实路径（`xe_pci_probe` → `xe_device_create` → `drm_dev_register`，以及 `drm_ioctl` 分发与鉴权）
> 把这条链走通。学完它，你就掌握了"用户态的一次 ioctl 是怎么精确落到某个驱动函数上的"——这是后面
> 所有章节（显示、内存、提交）的公共入口。

---

## 3.1 学习目标与前置依赖

### 学习目标

学完本章，你应当能够：

1. 逐字段说清 `drm_device`/`drm_driver`/`drm_minor`/`drm_file` 的职责与关系，并指出"厂商结构内嵌
   `drm_device`、用 `driver_priv` 挂私有数据"这两个贯穿全 DRM 的惯用法。
2. 沿 Xe 真实路径讲清**设备注册全流程**：`xe_pci_probe` → `xe_device_create`（`devm_drm_dev_alloc`）
   → `xe_device_probe` → `drm_dev_register`，并指出"用户态可见性分界"在哪一行。
3. 解释 **minor（设备节点）** 如何用 xarray 分配、`/dev/dri/cardN` 与 `renderD(128+N)` 的编号规则。
4. 解释 `driver_features` 如何决定创建哪些节点；理解 Xe 为何在静态 `drm_driver` 里只声明渲染侧能力、
   而显示能力由 `xe_display_driver_set_hooks` 运行时追加。
5. 讲清 **ioctl 分发**：`drm_ioctl` 如何按命令号查表（核心表 vs 驱动表）、拷贝参数、调
   `drm_ioctl_kernel`；以及 `drm_ioctl_permit` 的**四条鉴权规则**。
6. 说清 **master 与 render client** 的权限模型，并能从 `xe_ioctls` 全部带 `DRM_RENDER_ALLOW` 这一
   事实，推断 Xe 的渲染/计算 uAPI 在 render node 上即可用。
7. 走通一次 `DRM_IOCTL_XE_DEVICE_QUERY` 从用户态到 `xe_query_ioctl` 的完整链路。

### 前置依赖

- 第 00 章（节点、master/render 初识、`driver_features`）、第 01 章（PCI 探测、BAR）、
  第 02 章（设备/驱动模型、托管资源、kref、ioctl 的锁路径）。

> **关于参考驱动**：本书统一用 **Xe** 走读。Xe 是带完整渲染能力的真实驱动，运行需要 Intel GPU
> （或 QEMU GPU 直通）；没有硬件时，本章的代码走读与"源码研读型 Lab"同样能让你彻底理解机制——
> DRM 设备模型的**结构与流程**不依赖具体硬件是否在手。

---

## 3.2 主题导引：DRM 设备模型的由来与设计

### 3.2.1 DRM 本质是一个"字符设备子系统"

剥开所有图形概念，DRM 对用户态暴露的就是一组**字符设备**（`/dev/dri/*`）：你 `open` 它、对它
`ioctl`/`mmap`/`poll`。所有显示设置、缓冲分配、命令提交，都是通过对这些字符设备的 ioctl 完成的
（第 00 章 0.3 全栈图里"uAPI 分界"那一层）。所以 DRM 设备模型要解决的核心问题是：

- 一块 GPU 如何变成一个（或几个）`/dev/dri/*` 节点？——**设备注册 + minor 分配**。
- 一个进程 open 节点后，内核如何记住"这个进程的 GPU 上下文"（它分配了哪些缓冲、有没有 master 权限、
  有哪些待投递事件）？——**`drm_file` 每文件上下文**。
- 一次 ioctl 如何精确落到正确的处理函数、并做好权限检查？——**ioctl 分发表 + 鉴权**。

本章就是把这三件事讲透。

### 3.2.2 历史动因：为什么要分 primary/render、要有 master

第 00 章 0.2、0.2.8 已铺垫，这里从"设备模型"角度再钉一遍，因为它直接决定了数据结构：

- **master（主控）**：显示是独占资源——同一时刻只能有一个实体设置模式、点屏，否则多家抢屏会乱。
  于是 DRM 引入 **master** 概念：只有持 master 的 `drm_file` 能执行改显示的 ioctl。`drm_device.master`
  字段（第 00 章 0.4.1.1 见过）记录当前 master，受 `master_mutex` 保护。
- **render node（渲染节点）**：想用 GPU 算东西的进程不该被迫去碰显示控制与 master。于是 DRM 把能力
  拆成两个节点：primary（`cardN`，控显示，需 master）与 render（`renderD128+`，只渲染，无需特权）。
  这就是为什么一个 `drm_device` 可能有 `primary` 和 `render` 两个 `drm_minor`。

**Xe 是个绝佳例子**：它的渲染/计算 uAPI（`XE_GEM_CREATE`/`XE_VM_BIND`/`XE_EXEC`…）全部标了
`DRM_RENDER_ALLOW`（3.5.4），意味着这些操作在 render node 上即可做、无需 master——这正是"GPU 计算
应是低权限操作"理念的代码体现。显示相关能力则走 primary node + master。

### 3.2.3 一个 driver 结构，多个设备实例

`struct drm_driver` 是**每个驱动一份的静态常量**（vtable + 能力声明），而 `struct drm_device` 是
**每个设备实例一份**。一块机器上插两块同型号 GPU，就有两个 `drm_device`、共享同一个 `drm_driver`。
这解释了 `drm_device.driver_features`（每设备可再裁剪）与 `drm_driver.driver_features`（驱动默认）
并存的设计（3.4.2）。

---

## 3.3 原理与全景：从注册到 ioctl 的总链路

```
[开机/插卡] PCI 枚举 → 匹配 xe_pci_driver.id_table(8086:xxxx)         [第01/02章]
     │
     ▼ 命中
xe_pci_probe(pdev, ent)                                    drivers/gpu/drm/xe/xe_pci.c:1054
     │  force_probe 检查、id_blocked 检查
     ▼
xe_device_create(pdev, ent)                               xe_device.c:438
     │  aperture_remove_conflicting_pci_devices()  (赶走 fbdev 等占用者)
     │  devm_drm_dev_alloc(&pdev->dev, &driver, struct xe_device, drm)  ← 分配并内嵌 drm_device
     │  ttm_device_init(...)  drmm_add_action_or_reset(xe_device_destroy)
     ▼
xe_device_probe(xe)                                       xe_device.c:844
     │  探测 tile/GT、映射 MMIO、初始化 GuC、内存、显示...(第17章详)
     ▼
drm_dev_register(&xe->drm, 0)                             xe_device.c:981
     │  为 DRIVER_RENDER → 分配 render minor → /dev/dri/renderD128
     │  (若 display 钩子追加了 MODESET) → primary minor → /dev/dri/cardN
     ▼  ★ 此刻起用户态可见
[用户态] open("/dev/dri/renderD128")
     │
     ▼
drm_open → drm_open_helper → drm_file_alloc → driver->open=xe_file_open  drm_file.c / xe_device.c:82
     │  分配 struct xe_file, 挂到 drm_file.driver_priv
     ▼
[用户态] ioctl(fd, DRM_IOCTL_XE_DEVICE_QUERY, &q)
     │
     ▼
drm_ioctl(filp, cmd, arg)                                drm_ioctl.c:821
     │  按命令号查表(核心表 / xe_ioctls 驱动表)
     │  拷入参数(copy_from_user)
     ▼
drm_ioctl_kernel → drm_ioctl_permit(鉴权) → ioctl->func   drm_ioctl.c:787/599
     │
     ▼
xe_query_ioctl(dev, data, file)                          xe 驱动处理函数
     │  填结果 → copy_to_user
     ▼  返回用户态
```

本章其余各节就是把这张图的每一段落到字段与代码。**这条链是全书所有 ioctl（显示、内存、提交）的
公共骨架**——后面只是 `func` 指向不同的处理函数而已。

---

## 3.4 关键数据结构

### 3.4.1 `struct drm_minor`：一个设备节点

`drm_minor` 代表一个字符设备节点（`include/drm/drm_file.h`）。一个 `drm_device` 可有最多三种 minor：

| 字段/概念 | 含义 |
|-----------|------|
| `type` | `DRM_MINOR_PRIMARY` / `DRM_MINOR_RENDER` / `DRM_MINOR_ACCEL` |
| `index` | 全局编号（决定 `cardN`/`renderD(128+N)` 的 N，见 3.6.1） |
| `dev` | 反指其所属的 `drm_device` |
| `kdev` | 对应的内核 `struct device`（sysfs/devnode） |
| `debugfs_*` | 该节点的 debugfs 目录（第 00 章 0.9.5 见过 `/sys/kernel/debug/dri/N/`） |

`drm_device` 用三个指针字段持有它们（第 00 章 0.4.1.1 字段表）：`primary`、`render`、`accel`。
**有没有某个 minor，取决于 `driver_features`**（3.4.2、3.6.1）。

### 3.4.2 `struct drm_driver`：能力声明 + vtable（以 Xe 为例）

`drm_driver` 是每驱动一份的静态常量。看 Xe 的定义（`drivers/gpu/drm/xe/xe_device.c:392`）：

```c
static struct drm_driver driver = {
    .driver_features =
        DRIVER_GEM |                    // 支持 GEM 内存对象
        DRIVER_RENDER |                 // 创建 render node, 放行 DRM_RENDER_ALLOW 的 ioctl
        DRIVER_SYNCOBJ |                // 支持 syncobj(显式同步, 第10章)
        DRIVER_SYNCOBJ_TIMELINE |       // 支持 timeline syncobj
        DRIVER_GEM_GPUVA,               // 支持 GPU 虚拟地址管理(drm_gpuvm, 第12/18章)
    .open       = xe_file_open,         // 每次 open 的回调(分配 xe_file)
    .postclose  = xe_file_close,        // 关闭回调(清理 xe_file)
    .gem_prime_import = xe_gem_prime_import,   // dma-buf 导入(第10章)
    .dumb_create = xe_bo_dumb_create,          // dumb buffer
    .ioctls     = xe_ioctls,                   // 驱动私有 ioctl 表(3.5.4)
    .num_ioctls = ARRAY_SIZE(xe_ioctls),
    .fops       = &xe_driver_fops,             // file_operations
    .name = DRIVER_NAME, .desc = DRIVER_DESC, .major = DRIVER_MAJOR,
};
```

**一个极重要的观察**：这份静态声明里**没有 `DRIVER_MODESET`/`DRIVER_ATOMIC`**——只有渲染侧能力！
那 Xe 的显示从哪来？答案在 `xe_device_create` 开头（`xe_device.c:444`）：

```c
xe_display_driver_set_hooks(&driver);   // 若编译/支持显示, 运行时给 driver 追加 MODESET/ATOMIC 能力与显示回调
```

也就是说：**Xe 把"渲染能力"写死在静态结构里，"显示能力"按是否支持显示在运行时追加**。这是一个很
有教学价值的设计——`driver_features` 不必全是编译期常量，可按平台/配置动态裁剪（这也呼应 3.2.3 与
`drm_device.driver_features` 的存在）。Xe 与 i915 共享同一套 `display/` 子系统（第 21 章），用这种
"钩子追加"的方式把显示能力插进来。

feature flag 速查（`include/drm/drm_drv.h:64` 起）：

| flag | 作用 |
|------|------|
| `DRIVER_GEM` | GEM 内存对象 |
| `DRIVER_MODESET` | KMS 显示 → 创建 primary node |
| `DRIVER_RENDER` | 渲染 → 创建 render node、放行 `DRM_RENDER_ALLOW` ioctl |
| `DRIVER_ATOMIC` | 原子显示接口 |
| `DRIVER_SYNCOBJ`/`_TIMELINE` | 同步对象（第 10 章） |
| `DRIVER_GEM_GPUVA` | GPU 虚拟地址管理（第 12/18 章） |

### 3.4.3 `struct drm_file`：每次 open 的上下文

每次 `open("/dev/dri/*")` 都会分配一个 `drm_file`（`include/drm/drm_file.h`，由 `drm_file_alloc`
创建，3.5.5）。关键字段：

| 字段 | 类型 | 含义 |
|------|------|------|
| `minor` | `struct drm_minor *` | 这次 open 的是哪个节点（反查 `dev`） |
| `authenticated` | `bool` | 是否已鉴权（DRM_AUTH 类 ioctl 要看它） |
| `is_master` / 关联 `drm_master` | — | 是否持 master（控显示） |
| `object_idr` | idr | 该文件的 **GEM 句柄表**（句柄↔对象，第 09 章） |
| `event_list` / 等待队列 | — | 待投递给用户态的事件（vblank/flip 完成，第 04 章） |
| **`driver_priv`** | `void *` | **驱动私有数据**——Xe 在这里挂 `struct xe_file` |

`driver_priv` 是关键扩展点：DRM 核心管通用部分，驱动把自己的每文件状态挂在 `driver_priv` 上。

### 3.4.4 `struct xe_file`：Xe 的每文件私有状态

Xe 的每文件私有数据（`drivers/gpu/drm/xe/xe_device_types.h:572`）：

```c
struct xe_file {
    struct xe_device *xe;          // 反指设备
    struct drm_file *drm;          // 反指基类 drm_file
    struct {                        // 该文件创建的 VM 集合
        struct xarray xa;           // vm_id → xe_vm
        struct mutex lock;          // 保护查找/增删(第02章: 进程上下文用 mutex)
    } vm;
    struct {                        // 该文件创建的提交队列集合
        struct xarray xa;           // exec_queue_id → xe_exec_queue
        struct mutex lock;
        ... pending_removal;
    } exec_queue;
    struct kref refcount;          // 引用计数(第02章 kref)
    char *process_name; pid_t pid; // 调试/统计用
    ...
};
```

观察它如何复用第 02 章的设施：**xarray** 做"id → 对象"映射（VM、exec_queue 都按 id 管理，
对应 `XE_VM_CREATE` 返回 vm_id、`XE_EXEC` 用 exec_queue_id）；**mutex** 保护查找增删（进程上下文，
非中断）；**kref** 管 `xe_file` 自身生命周期。这就是第 02 章"DRM 重度复用内核设施"的活样本。

> 心智模型：`drm_file`（核心通用：句柄表、事件、鉴权）+ `driver_priv→xe_file`（驱动私有：VM、提交
> 队列）= 一个进程打开 Xe 设备后的完整上下文。第 18 章（VM）、19 章（exec）都从 `xe_file` 里取状态。

---

## 3.5 核心代码走读

### 3.5.1 注册入口 `xe_pci_probe`

PCI 匹配成功后（第 02 章设备/驱动模型），内核调 `xe_pci_probe`（`xe_pci.c:1054`）：

```c
static int xe_pci_probe(struct pci_dev *pdev, const struct pci_device_id *ent)
{
    const struct xe_device_desc *desc = (const void *)ent->driver_data;  // (1) 取平台描述符
    struct xe_device *xe;
    ...
    if (desc->require_force_probe && !id_forced(pdev->device)) {         // (2) 实验性硬件需显式 force_probe
        dev_info(&pdev->dev, "... not officially supported ... use xe.force_probe=...");
        return -ENODEV;
    }
    if (id_blocked(pdev->device)) ...                                     // (3) 被屏蔽的 id 不接
    ...
    xe = xe_device_create(pdev, ent);                                    // (4) 创建 drm/xe 设备
    ...
    err = xe_device_probe(xe);                                           // (5) 硬件初始化 + 注册(第17章)
    ...
}
```

- **(1)** `ent->driver_data` 就是第 00 章 0.8.4 那张 PCI ID 表里绑定的平台描述符（`xe_device_desc`），
  记录这代硬件的能力。这是"一份驱动适配多代"的关键。
- **(2)** `require_force_probe`：对尚未正式支持的实验性硬件，Xe 默认拒绝 probe，要用户显式
  `xe.force_probe=<devid>` 才接——避免在不成熟支持上误用。这是发行策略的体现。
- **(4)(5)** 真正干活的是 `xe_device_create`（分配设备）与 `xe_device_probe`（初始化 + 注册）。

### 3.5.2 设备分配 `xe_device_create`

```c
struct xe_device *xe_device_create(struct pci_dev *pdev, const struct pci_device_id *ent)
{
    struct xe_device *xe;
    xe_display_driver_set_hooks(&driver);                          // (1) 按需给 driver 追加显示能力(3.4.2)
    err = aperture_remove_conflicting_pci_devices(pdev, driver.name); // (2) 赶走占用显存孔径的 fbdev 等
    xe = devm_drm_dev_alloc(&pdev->dev, &driver, struct xe_device, drm); // (3) 分配并内嵌 drm_device
    err = ttm_device_init(&xe->ttm, ...);                          // (4) 初始化 TTM(第11章)
    xe_bo_dev_init(&xe->bo_device);
    err = drmm_add_action_or_reset(&xe->drm, xe_device_destroy, NULL); // (5) 登记销毁动作(第02章托管)
    ...
    xe->info.devid = pdev->device;                                 // 记录 device id/revision
    return xe;
}
```

- **(1)** 前述"运行时追加显示能力"。注意它改的是**静态全局 `driver`**——所以是按驱动一次性设定。
- **(2)** `aperture_remove_conflicting_pci_devices`：开机早期 fbdev/EFI framebuffer 可能占着显存孔径，
  真正的 GPU 驱动接管前要把它们赶走，避免冲突。
- **(3)** `devm_drm_dev_alloc(&pdev->dev, &driver, struct xe_device, drm)`：第 00 章 0.5.2 (C)、第 02 章
  反复见过的标准分配——分配 `struct xe_device`，初始化内嵌的 `drm`，绑定 `driver`，父设备是 `pdev`。
- **(4)(5)** TTM 初始化（Xe 用 TTM 管显存）、登记托管销毁动作。**此时设备还未对用户态可见**。

### 3.5.3 注册：`drm_dev_register` 与"可见性分界"

`xe_device_probe`（`xe_device.c:844`）做完所有硬件初始化（tile/GT/MMIO/GuC/内存/显示，第 17 章），
最后一步（`xe_device.c:981`）：

```c
err = drm_dev_register(&xe->drm, 0);   // ★ 用户态可见性分界
```

`drm_dev_register`（`drm_drv.c`）内部为每种该建的 minor 调 `drm_minor_register`：

- 因为有 `DRIVER_RENDER` → 建 render minor → 出现 `/dev/dri/renderD128`。
- 若显示钩子追加了 `DRIVER_MODESET` → 建 primary minor → 出现 `/dev/dri/cardN`。

**这一行成功返回的瞬间**：节点出现、udev 收到事件、用户态可以 open。和第 00 章 0.5.2 (H)、第 02 章
"先准备、后注册"完全一致——注册前一切必须就绪，注册后竞态窗口立刻打开。

### 3.5.4 ioctl 表：`xe_ioctls`

Xe 的驱动私有 ioctl 表（`xe_device.c:192`），注意**每一条都带 `DRM_RENDER_ALLOW`**：

```c
static const struct drm_ioctl_desc xe_ioctls[] = {
    DRM_IOCTL_DEF_DRV(XE_DEVICE_QUERY,        xe_query_ioctl,              DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF_DRV(XE_GEM_CREATE,          xe_gem_create_ioctl,         DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF_DRV(XE_GEM_MMAP_OFFSET,     xe_gem_mmap_offset_ioctl,    DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF_DRV(XE_VM_CREATE,           xe_vm_create_ioctl,          DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF_DRV(XE_VM_BIND,             xe_vm_bind_ioctl,            DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF_DRV(XE_EXEC,                xe_exec_ioctl,               DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF_DRV(XE_EXEC_QUEUE_CREATE,   xe_exec_queue_create_ioctl,  DRM_RENDER_ALLOW),
    DRM_IOCTL_DEF_DRV(XE_WAIT_USER_FENCE,     xe_wait_user_fence_ioctl,    DRM_RENDER_ALLOW),
    ... // OBSERVATION / MADVISE / VM_QUERY_MEM_RANGE_ATTRS ...
};
```

**从这张表能直接读出 Xe 的 uAPI 全貌与设计**：

- 全部 `DRM_RENDER_ALLOW` → Xe 的渲染/计算 uAPI 在 **render node** 上即可用、无需 master 特权
  （呼应 3.2.2、第 00 章 0.6.1）。这是"GPU 计算 = 低权限操作"的代码落点。
- 表项就是后面六篇要逐个攻克的主角：`XE_VM_CREATE`/`XE_VM_BIND`（第 18 章）、`XE_EXEC`/
  `XE_EXEC_QUEUE_CREATE`（第 19 章）、`XE_WAIT_USER_FENCE`（第 10/19 章）、`XE_DEVICE_QUERY`（本章 3.9）。
- `DRM_IOCTL_DEF_DRV` 宏与第 00 章 0.6.3 的核心表 `DRM_IOCTL_DEF` 同形，只是号段不同（驱动 ioctl 在
  `DRM_COMMAND_BASE..DRM_COMMAND_END`，见 3.6.2）。

### 3.5.5 文件打开：`drm_open` → `xe_file_open`

用户态 `open("/dev/dri/renderD128")` 进内核走 `drm_open`（`drm_file.c:369`）→ `drm_open_helper`
（:316）→ `drm_file_alloc`（:132，分配并初始化通用 `drm_file`）→ 调 `driver->open`，即
`xe_file_open`（`xe_device.c:82`）：

```c
static int xe_file_open(struct drm_device *dev, struct drm_file *file)
{
    struct xe_device *xe = to_xe_device(dev);
    struct xe_file *xef = kzalloc_obj(*xef);          // (1) 分配 Xe 每文件私有
    ...
    xef->drm = file; xef->xe = xe;
    mutex_init(&xef->vm.lock);  xa_init_flags(&xef->vm.xa, XA_FLAGS_ALLOC1);          // (2) VM 表
    mutex_init(&xef->exec_queue.lock); xa_init_flags(&xef->exec_queue.xa, ...);       // (3) 提交队列表
    file->driver_priv = xef;                          // (4) 挂到 drm_file.driver_priv
    kref_init(&xef->refcount);                        // (5) 引用计数置 1
    ... process_name/pid (调试)
    return 0;
}
```

逐点：

- **(1)** 分配 `struct xe_file`（3.4.4）——这次 open 的 Xe 私有上下文。
- **(2)(3)** 初始化两张 xarray（VM、exec_queue）及其保护 mutex——此后该进程创建的 VM/队列都登记在
  这里，按 id 索引。
- **(4)** `file->driver_priv = xef`：把私有上下文挂到核心 `drm_file` 上。**后续任何 ioctl 处理函数
  都能通过 `file->driver_priv` 取回 `xe_file`**（如 `xe_vm_create_ioctl` 把新 VM 存进 `xef->vm.xa`）。
- **(5)** `kref_init`：`xe_file` 自身用 kref 管生命周期（可能被异步引用）。

对称地，关闭时 `drm_release`（`drm_file.c:427`）会调 `driver->postclose = xe_file_close`，清理这些
VM/队列、放 `xe_file` 引用。

### 3.5.6 ioctl 分发核心：`drm_ioctl`

这是全 DRM 最关键的分发函数（`drm_ioctl.c:821`）。精读其骨架：

```c
long drm_ioctl(struct file *filp, unsigned int cmd, unsigned long arg)
{
    struct drm_file *file_priv = filp->private_data;     // 取本次 open 的上下文
    struct drm_device *dev = file_priv->minor->dev;
    const struct drm_ioctl_desc *ioctl = NULL;
    unsigned int nr = DRM_IOCTL_NR(cmd);                 // (1) 从 cmd 解出命令号
    char stack_kdata[128];                               // (2) 小参数用栈缓冲
    char *kdata = NULL;
    ...
    if (drm_dev_is_unplugged(dev)) return -ENODEV;       // (3) 设备已拔出
    if (DRM_IOCTL_TYPE(cmd) != DRM_IOCTL_BASE) return -ENOTTY;

    is_driver_ioctl = nr >= DRM_COMMAND_BASE && nr < DRM_COMMAND_END;
    if (is_driver_ioctl) {                               // (4) 驱动 ioctl: 查 xe_ioctls
        unsigned int index = nr - DRM_COMMAND_BASE;
        if (index >= dev->driver->num_ioctls) goto err_i1;
        index = array_index_nospec(index, dev->driver->num_ioctls);  // 防 Spectre 越界推测
        ioctl = &dev->driver->ioctls[index];
    } else {                                             // (5) 核心 ioctl: 查 drm_ioctls[]
        if (nr >= DRM_CORE_IOCTL_COUNT) goto err_i1;
        nr = array_index_nospec(nr, DRM_CORE_IOCTL_COUNT);
        ioctl = &drm_ioctls[nr];
    }
    ...
    func = ioctl->func;                                  // (6) 取处理函数(信任内核表, 不信用户态)
    if (!func) { retcode = -EINVAL; goto err_i1; }

    if (ksize <= sizeof(stack_kdata)) kdata = stack_kdata;  // (7) 参数缓冲: 小用栈, 大用 kmalloc
    else { kdata = kmalloc(ksize, GFP_KERNEL); ... }

    if (copy_from_user(kdata, (void __user *)arg, in_size)) { retcode = -EFAULT; goto err_i1; } // (8) 拷入
    if (ksize > in_size) memset(kdata + in_size, 0, ksize - in_size);  // 清零未填部分

    retcode = drm_ioctl_kernel(filp, func, kdata, ioctl->flags);       // (9) 鉴权 + 调处理函数
    if (copy_to_user((void __user *)arg, kdata, out_size)) retcode = -EFAULT; // (10) 拷出
    ...
}
```

逐点（这十步是所有 ioctl 的公共流程）：

- **(1)** `DRM_IOCTL_NR(cmd)`：ioctl 命令号编码里含序号、方向、参数大小；这里解出序号 `nr`（3.6.2）。
- **(2)(7)** 性能优化：≤128 字节的参数用**栈上缓冲** `stack_kdata`，避免每次 ioctl 都 kmalloc（3.7）。
- **(3)** 设备已热拔则 `-ENODEV`——配合第 02 章 SRCU 的 `drm_dev_enter/exit` 保护拔出竞态。
- **(4)(5)** **分两张表**：命令号在 `[DRM_COMMAND_BASE, DRM_COMMAND_END)` 区间是**驱动 ioctl**，查
  `dev->driver->ioctls`（即 Xe 的 `xe_ioctls`）；否则是**核心 ioctl**，查 `drm_ioctls[]`。
  `array_index_nospec` 是 Spectre 缓解（防止越界推测执行泄漏）。
- **(6)** `func = ioctl->func`：**用内核表里的函数，不信用户态传的任何东西**（注释 "Do not trust
  userspace"）。这是安全要点。
- **(8)(10)** `copy_from_user`/`copy_to_user`：内核与用户态的唯一数据搬运方式；只拷声明的 in/out 大小。
- **(9)** `drm_ioctl_kernel` 做鉴权后真正调 `func`——见 3.5.7。

### 3.5.7 鉴权：`drm_ioctl_permit` 的四条规则

`drm_ioctl_kernel`（`drm_ioctl.c:787`）在调处理函数前先 `drm_ioctl_permit`（:599）做权限检查：

```c
static int drm_ioctl_permit(u32 flags, struct drm_file *file_priv)
{
    if (unlikely((flags & DRM_ROOT_ONLY) && !capable(CAP_SYS_ADMIN)))   // (1) ROOT_ONLY 要 CAP_SYS_ADMIN
        return -EACCES;
    if (unlikely((flags & DRM_AUTH) && !drm_is_render_client(file_priv) // (2) AUTH 要已鉴权或 render 客户端
                 && !file_priv->authenticated))
        return -EACCES;
    if (unlikely((flags & DRM_MASTER) && !drm_is_current_master(file_priv))) // (3) MASTER 要持 master
        return -EACCES;
    if (unlikely(!(flags & DRM_RENDER_ALLOW) && drm_is_render_client(file_priv))) // (4) 没标 RENDER_ALLOW 的, render 客户端禁用
        return -EACCES;
    return 0;
}
```

这四条规则把第 00 章 0.6.1、3.2.2 的权限模型钉死在代码里：

- **(3)** 改显示的 ioctl 标 `DRM_MASTER`，只有持 master 的 `drm_file` 能过——这就是"同时只能一家控屏"。
- **(4)** 关键的一条：**只要这个 ioctl 没标 `DRM_RENDER_ALLOW`，render node 上的客户端就被拒**。
  反过来，Xe 的 `xe_ioctls` 全标了 `DRM_RENDER_ALLOW`（3.5.4），所以它们在 render node 全部可用。
  **(4) 与 `xe_ioctls` 的标志，正是"render node 只能渲染、不能控显示"的一体两面。**

> 把 3.5.6 与 3.5.7 连起来：`drm_ioctl` 查表 + 拷参数，`drm_ioctl_permit` 按 `ioctl->flags` 鉴权，
> 通过才调 `func`。一次 `DRM_IOCTL_XE_EXEC` 之所以能在 render node 上被普通进程调用，正因为它在
> `xe_ioctls` 里标了 `DRM_RENDER_ALLOW`、且过了 (4) 的检查。

---

## 3.6 机制深入

### 3.6.1 minor 的 xarray 分配与 `/dev/dri` 命名规则

`/dev/dri/card0`、`renderD128` 里的数字不是随意的。看 `drm_minor_alloc`（`drm_drv.c:143`）与编号
上限宏（`drm_drv.c:137`）：

```c
#define DRM_MINOR_LIMIT(t) ({ \
    ... XA_LIMIT(64 * _t, 64 * _t + 63); })       // 按类型分段: 每类占 64 个号

r = xa_alloc(drm_minor_get_xa(type), &minor->index, NULL, DRM_MINOR_LIMIT(type), GFP_KERNEL);
```

机制：

- minor 用 **xarray** 分配全局唯一的 `index`，按 `type` 分段。传统上 primary 段从 0 起（`card0`、
  `card1`…），render 段从 128 起（`renderD128`、`renderD129`…）。这就是"第一个渲染节点是 128"的由来。
- 分配登记为托管动作（`drmm_add_action_or_reset(dev, drm_minor_alloc_release, minor)`，`drm_drv.c:163`）
  ——设备销毁时自动回收（第 02 章托管资源）。

所以：**有几个 minor、各是什么号，由 `driver_features`（建哪些类型）+ xarray 分配（具体号）共同决定**。
Xe（`DRIVER_RENDER` + 显示钩子的 `DRIVER_MODESET`）→ 同时有 `cardN` 与 `renderD(128+N)`。

### 3.6.2 ioctl 命令号编码与"两张表"分发

ioctl 的 `cmd` 是一个编码整数，含：方向（读/写/读写）、类型（`DRM_IOCTL_BASE`，区分是不是 DRM 的）、
序号 `nr`、参数大小。`drm_ioctl` 据此：

1. `DRM_IOCTL_TYPE(cmd) != DRM_IOCTL_BASE` → 不是 DRM ioctl，`-ENOTTY`。
2. `nr` 落在 `[DRM_COMMAND_BASE, DRM_COMMAND_END)` → **驱动 ioctl**，查 `dev->driver->ioctls`
   （Xe 的 `xe_ioctls`），下标 = `nr - DRM_COMMAND_BASE`。
3. 否则 → **核心 ioctl**，查 `drm_ioctls[]`（第 00 章 0.6.3）。

这套"核心表 + 驱动表"的二级分发，让核心通用 ioctl（VERSION/GET_CAP/PRIME 等）与驱动私有 ioctl
（`XE_*`）共用同一个入口 `drm_ioctl`，又各自独立扩展。命令号、参数结构都定义在 uAPI 头
（`include/uapi/drm/drm.h` 与 `include/uapi/drm/xe_drm.h`，第 19 章会精读 Xe uAPI）。

### 3.6.3 `driver_priv`：核心与驱动的解耦点

`drm_file.driver_priv` 是 DRM "核心通用 + 驱动特化"分层的缩影：

- **核心**只认 `drm_file`（句柄表、事件、鉴权），完全不知道 `xe_file` 长什么样。
- **驱动**在 `.open` 里分配私有结构挂上 `driver_priv`，在自己的 ioctl 处理函数里取回。

这种"基类 + void* 私有"的模式在内核里无处不在。它让核心保持稳定、驱动自由扩展——也是 DRM 能同时
托起 70+ 驱动的结构基础（第 00 章 0.7）。

---

## 3.7 量化分析：ioctl 路径的成本

| 成本点 | 量级/做法 | 说明 |
|--------|-----------|------|
| 一次 ioctl 的系统调用 + 分发 | 微秒级以下 | 进/出内核 + 查表 + 两次 copy_user |
| 参数拷贝 `copy_from/to_user` | 与参数大小成正比 | 大参数（如 `XE_EXEC` 的数组）更贵 |
| 参数缓冲分配 | **≤128B 用栈 `stack_kdata`，否则 kmalloc** | 高频小 ioctl 走栈，省掉每次 kmalloc/kfree（3.5.6 (7)） |
| 鉴权 `drm_ioctl_permit` | 几个分支判断 | 极廉价，但每个 ioctl 都过 |
| 防 Spectre `array_index_nospec` | 极小 | 安全 vs 微小性能的权衡 |

**可操作结论**：

1. **高频路径要让参数 ≤128 字节走栈**：`stack_kdata[128]` 这个看似随意的大小，是"绝大多数 ioctl 参数
   都不大、走栈避免 kmalloc"的量化权衡。设计 uAPI 结构时，把热路径参数控制在这个量级有实际收益。
2. **ioctl 不是零成本**：每次都有两次 copy_user + 查表 + 鉴权。极高频操作（如每帧大量小提交）应尽量
   合并（一次 ioctl 提交一批），而非每个动作一次 ioctl——这正是 `XE_EXEC` 用数组批量提交的动机
   （第 19 章）。

---

## 3.8 硬件视角：Xe 的节点与 force_probe

- **Xe 同时创建 render 与 primary 节点**（前提：`DRIVER_RENDER` + 显示钩子的 `DRIVER_MODESET`）。
  在有 Intel GPU 的机器上 `ls /dev/dri/` 会看到 `card0` 与 `renderD128`（编号视系统而定）。
- **运行需要真实 Intel GPU**（或 QEMU GPU 直通）。这与第 00 章用 vkms 在纯软件环境学 KMS 不同——
  Xe 是真硬件驱动。没有硬件时，本章以**源码研读**为主（结构与流程不依赖硬件在手）。
- **`force_probe`（3.5.1 (2)）**：实验性硬件需 `xe.force_probe=<devid>` 才被 Xe 接管。这也是
  "同一块卡 i915 还是 xe 认领"的人为可控点（配合 `i915.force_probe='!<devid>'` 把它从 i915 排除）。
- `drm_info /dev/dri/renderD128` 会打印 `Driver: xe ... Driver features: DRIVER_GEM DRIVER_RENDER
  DRIVER_SYNCOBJ ...`——与 3.4.2 的 `driver_features` 一一对应（第 00 章 0.9.3 在 vkms 上做过同样
  的"代码↔现象"对照，这里换成 Xe）。

---

## 3.9 综合实例：一次 `DRM_IOCTL_XE_DEVICE_QUERY` 的全链路（放在一起看）

把本章所有环节在一次最简单、最适合入门的 Xe ioctl 上走通。`XE_DEVICE_QUERY` 用于查询设备拓扑/能力
（引擎、内存区域、GT 配置等），是 Mesa 初始化时第一批调用之一，且是只读、`DRM_RENDER_ALLOW`，最适合
作为"第一个 Xe ioctl"。

### 3.9.1 用户态视角

```c
int fd = open("/dev/dri/renderD128", O_RDWR);     // 打开 render node → drm_open → xe_file_open
struct drm_xe_device_query q = {
    .query = DRM_XE_DEVICE_QUERY_ENGINES,         // 想查"引擎信息"
    .size = 0, .data = 0,                          // 第一次调: 先问"要多大缓冲"
};
ioctl(fd, DRM_IOCTL_XE_DEVICE_QUERY, &q);          // 内核回填 q.size = 需要的字节数
void *buf = malloc(q.size);
q.data = (uintptr_t)buf;
ioctl(fd, DRM_IOCTL_XE_DEVICE_QUERY, &q);          // 第二次: 真正拿数据
// buf 里就是引擎拓扑(RCS/CCS/BCS/... 各几个), 对应第01章 1.8.3 的六类引擎
```

这套"先问大小、再取数据"的两段式是可变长 uAPI 的常见模式（避免一次性分配过大）。

### 3.9.2 内核侧链路（对照 3.3 全景）

```
ioctl(fd, DRM_IOCTL_XE_DEVICE_QUERY, &q)
  └─ drm_ioctl(filp, cmd, arg)                         [drm_ioctl.c:821]
       ├─ nr = DRM_IOCTL_NR(cmd); 落在驱动号段
       ├─ ioctl = &xe_ioctls[XE_DEVICE_QUERY 下标]      [xe_device.c:192]
       ├─ copy_from_user(kdata=&q, arg, in_size)        (q 很小, 走 stack_kdata)
       └─ drm_ioctl_kernel(filp, func, kdata, flags)    [drm_ioctl.c:787]
            ├─ drm_ioctl_permit(DRM_RENDER_ALLOW, file) [drm_ioctl.c:599]
            │    → render 客户端 + 有 RENDER_ALLOW → 通过(规则 4)
            └─ func == xe_query_ioctl(dev, kdata, file) [xe 驱动]
                 ├─ file->driver_priv → xe_file (3.5.5)
                 ├─ 据 q.query 填引擎/内存/GT 信息
                 └─ 回写 q(size 或 data)
       └─ copy_to_user(arg, kdata, out_size)            (把 q 写回用户态)
```

**这条链就是 3.3 全景图的实例化**。把它和你在第 00/01 章用过的工具结合：
- `strace -e ioctl your_program` 能看到这两次 `DRM_IOCTL_XE_DEVICE_QUERY` 调用与返回；
- ftrace function_graph（第 02 章 2.9）能看到 `drm_ioctl → drm_ioctl_kernel → xe_query_ioctl` 的内核
  调用层级；
- `drm.debug` 的 core 位（第 00 章 0.11）会打印 `drm_ioctl` 那行 `comm=... auth=... <ioctl name>` 日志
  （来自 `drm_ioctl.c` 的 `drm_dbg_core`）。

### 3.9.3 这就是所有 ioctl 的模板

`XE_DEVICE_QUERY` 只是最简单的一个；`XE_VM_BIND`、`XE_EXEC` 走的是**完全相同的 `drm_ioctl` 分发 +
鉴权框架**，区别只在 `func` 指向更复杂的处理函数、参数结构更大（可能走 kmalloc 而非栈）。掌握了本节
这条链，你就掌握了进入第六篇（Xe 内存/提交）的钥匙。

---

## 3.10 横向关联

| 本章内容 | 在哪深用/延续 |
|----------|----------------|
| `xe_pci_probe`/`xe_device_create`/`xe_device_probe` 全流程 | 第 17 章 Xe 设备初始化（probe 内部细节） |
| `drm_file`/`xe_file`、`driver_priv` | 第 18 章（`xef->vm.xa`）、第 19 章（`xef->exec_queue.xa`） |
| ioctl 分发 + 鉴权框架 | 全书所有 ioctl（显示第 06 章、内存第 09 章、提交第 19 章） |
| `xe_ioctls` 表项 | XE_VM_*（第 18 章）、XE_EXEC*（第 19 章）、XE_WAIT_USER_FENCE（第 10 章） |
| master/render、`DRM_RENDER_ALLOW` | 第 22 章（虚拟化下的权限）、容器 GPU 共享 |
| minor/事件队列 | 第 04 章（事件投递）、第 05 章（KMS 在 primary node） |

**跨切面主题**：保护与隔离（master/render 权限、`array_index_nospec`、不信用户态）；编程模型（统一
ioctl 入口 + 二级分发表）；性能（栈缓冲优化、批量 ioctl）；可靠性（热拔检查、托管 minor）。

---

## 3.11 谬误与陷阱 + 调试

### 谬误与陷阱

- **谬误 1：「Xe 的 `drm_driver` 没声明 MODESET，所以 Xe 不能显示」。**
  错。Xe 把显示能力通过 `xe_display_driver_set_hooks(&driver)`（`xe_device.c:444`）在运行时追加，
  静态结构里只列渲染侧能力（3.4.2）。`driver_features` 不必全是编译期常量。

- **谬误 2：「在 render node 上也能设置显示模式」。**
  不能。改显示的 ioctl 标 `DRM_MASTER`，render 客户端过不了 `drm_ioctl_permit` 规则 (3)；而没标
  `DRM_RENDER_ALLOW` 的 ioctl 还会被规则 (4) 直接拒。Xe 渲染 ioctl 全标了 `DRM_RENDER_ALLOW` 才得以
  在 render node 用（3.5.7）。

- **谬误 3：「ioctl 处理函数可以信任用户态传进来的大小/指针」。**
  绝不。`drm_ioctl` 用**内核 ioctl 表里**的大小与 `func`（"Do not trust userspace"），只 copy 声明
  的字节数。处理函数里对用户态指针/长度也要再校验（3.5.6 (6)(8)）。

- **谬误 4：「`renderD128` 的 128 是随便定的」。**
  不是。minor 按 type 分段 xarray 分配，render 段从 128 起（3.6.1）。

- **陷阱 5：忘了 `file->driver_priv` 可能为这个 minor 类型不适用。**
  取 `driver_priv` 前要确保是本驱动的文件、`.open` 确实分配了它。跨驱动/内核内部 client 时要小心。

- **陷阱 6：在 ioctl 处理函数里持锁睡眠却没考虑设备热拔。**
  设备可能在处理中被拔出；长流程要配合 `drm_dev_enter/exit`（SRCU，第 02 章）保护，否则 UAF。

### 调试小抄

| 想知道 | 手段 |
|--------|------|
| 用户态调了哪些 ioctl | `strace -e ioctl <prog>`（看 `DRM_IOCTL_XE_*`） |
| ioctl 的内核调用层级 | ftrace function_graph `-l 'drm_ioctl*' -l 'xe_*'`（第 02 章 2.9） |
| ioctl 分发/鉴权日志 | `drm.debug` core 位，看 `drm_ioctl` 的 `auth=.. <name>` 行 |
| 设备有哪些节点/能力 | `drm_info /dev/dri/renderD128`、`ls /dev/dri/` |
| 为何返回 -EACCES | 对照 `drm_ioctl_permit` 四规则：是不是 render 调了非 RENDER_ALLOW？是不是要 master？ |
| 注册失败 | dmesg 看 `xe_device_probe`/`drm_dev_register` 的错误 |

---

## 3.12 随堂练习（Practice Problems）

**练习 3.1** 一次 `open("/dev/dri/renderD128")` 在内核里依次经过哪几个函数，最终调到 Xe 的哪个回调？
那个回调分配了什么、挂在哪里？

**练习 3.2** Xe 的静态 `drm_driver.driver_features` 里没有 `DRIVER_MODESET`，为什么 Xe 仍能显示？

**练习 3.3** `drm_ioctl` 如何判断一个命令该查核心表还是 Xe 的 `xe_ioctls`？

**练习 3.4** `xe_ioctls` 里每条都带 `DRM_RENDER_ALLOW`，这对"能不能在 render node 上调用它们"意味
着什么？依据是 `drm_ioctl_permit` 的哪条规则？

**练习 3.5** `drm_ioctl` 里 `stack_kdata[128]` 起什么作用？什么情况下会改用 kmalloc？

**练习 3.6** `drm_dev_register` 这一行执行前后，对"用户态能否 open 到 Xe 设备"分别意味着什么？

**练习 3.7（量化）** 为什么 `XE_EXEC` 设计成"一次 ioctl 提交一批 batch"而不是"一个 batch 一次 ioctl"？
用 3.7 的 ioctl 成本分析支撑。

### 随堂练习即时解答

**解 3.1** `drm_open`（`drm_file.c:369`）→ `drm_open_helper`（:316）→ `drm_file_alloc`（:132，建通用
`drm_file`）→ `driver->open` 即 `xe_file_open`（`xe_device.c:82`）。它分配 `struct xe_file`、初始化
VM/exec_queue 两张 xarray、`kref_init`，并挂到 `file->driver_priv`（3.5.5）。

**解 3.2** 因为 `xe_device_create` 开头调 `xe_display_driver_set_hooks(&driver)`（`xe_device.c:444`），
在运行时给静态 `driver` 追加 `DRIVER_MODESET`/`DRIVER_ATOMIC` 与显示回调。静态结构只列渲染侧能力
（3.4.2、3.11 谬误 1）。

**解 3.3** 看命令号 `nr = DRM_IOCTL_NR(cmd)`：落在 `[DRM_COMMAND_BASE, DRM_COMMAND_END)` 是驱动
ioctl → 查 `dev->driver->ioctls`（`xe_ioctls`）；否则是核心 ioctl → 查 `drm_ioctls[]`（3.5.6 (4)(5)、
3.6.2）。

**解 3.4** 意味着它们在 render node 上即可被普通进程调用、无需 master。依据是 `drm_ioctl_permit` 规则
(4)：没标 `DRM_RENDER_ALLOW` 的 ioctl 会拒绝 render 客户端；标了就放行（3.5.7）。

**解 3.5** `stack_kdata[128]` 是栈上参数缓冲：参数 ≤128 字节直接用它，省掉 kmalloc/kfree（高频小
ioctl 的优化）；参数 >128 字节才 kmalloc（3.5.6 (7)、3.7）。

**解 3.6** 之前：设备初始化中、用户态看不到节点、无法 open；之后（成功返回）：render/primary 节点
出现、udev 收到事件、用户态可 open 并发 ioctl。这是"先准备、后注册"的可见性分界（3.5.3、3.6.1）。

**解 3.7** 每次 ioctl 都有系统调用进出 + 两次 copy_user + 查表 + 鉴权的固定成本（3.7）。一个 batch
一次 ioctl 会把这些固定成本乘以 batch 数；一次提交一批则把固定成本摊薄到整批上，高频提交时差异显著。
故 `XE_EXEC` 用数组批量（第 19 章）。

---

## 3.13 课后练习题

> 仅题目。答案见 [`answers/03-drm-core-answers.md`](./answers/03-drm-core-answers.md)。★ 为动手/综合题。

### L1 概念辨析

**3.13.1** 用一句话分别概括 `drm_device`/`drm_driver`/`drm_minor`/`drm_file` 的职责，并说明哪个是
"每驱动一份"、哪个是"每设备一份"、哪个是"每次 open 一份"。

**3.13.2** 解释 master 与 render client 的权限差异，以及它如何映射到 primary/render 两类节点。

**3.13.3** `driver_features` 决定创建哪些节点。Xe 的静态结构只列渲染侧能力，显示能力如何补上？这说明
feature 必须是编译期常量吗？

**3.13.4** 描述 ioctl 命令号 `cmd` 编码里至少三类信息，以及 `drm_ioctl` 如何用它做"核心表/驱动表"
二级分发。

**3.13.5** 复述 `drm_ioctl_permit` 的四条规则，并指出哪条直接实现了"render node 不能控显示"。

**3.13.6** 解释 `drm_file.driver_priv` 的作用，以及它体现的"核心通用 + 驱动特化"分层。

### L2 数据结构与源码阅读

**3.13.7** 阅读 `xe_device.c:392` 的 `static struct drm_driver driver`，列出它声明的 `driver_features`
与各回调（open/postclose/ioctls/fops 等），并指出哪些能力是显示钩子运行时追加的。

**3.13.8** 阅读 `xe_file_open`（`xe_device.c:82`）与 `struct xe_file`（`xe_device_types.h:572`），说明
它初始化了哪两张 xarray、各存什么、为何用 mutex 保护、为何 `kref_init`。

**3.13.9** 阅读 `drm_ioctl`（`drm_ioctl.c:821`）十步骨架，指出哪一步"决定查哪张表"、哪一步"拷入
参数"、哪一步"鉴权 + 调处理函数"、哪一步"拷出结果"。

**3.13.10** 阅读 `drm_ioctl_permit`（`drm_ioctl.c:599`），逐条解释四个 `if` 各拦什么；结合 `xe_ioctls`
全带 `DRM_RENDER_ALLOW`，推断 Xe 渲染 uAPI 在 render node 的可用性。

**3.13.11 ★** 阅读 `drm_minor_alloc`（`drm_drv.c:143`）与 `DRM_MINOR_LIMIT` 宏（:137），解释
`renderD128` 的 128 从何而来，以及 minor 分配如何登记为托管动作（第 02 章）。

### L3 机制分析

**3.13.12** 画出从 `xe_pci_probe` 到 `/dev/dri/renderD128` 出现、再到用户态首个 ioctl 被 `xe_query_ioctl`
接住的完整调用/事件序列（ASCII），标注"可见性分界"在哪。

**3.13.13** 解释 `array_index_nospec` 在 `drm_ioctl` 里的作用（Spectre 缓解），以及"用内核表的 func、
不信用户态"为什么是安全要点。

**3.13.14** 描述设备热拔（unplug）下，正在执行的 ioctl 可能踩到的竞态，以及 `drm_dev_is_unplugged`/
`drm_dev_enter/exit`（SRCU，第 02 章）如何防护。

### L4 设计与对比

**3.13.15** "核心 ioctl 表 + 驱动 ioctl 表"二级分发 vs "每驱动各写一个 ioctl 入口"，论述前者在可维护
性与一致性上的优势（联系第 00 章 0.7 杠杆率）。

**3.13.16** Xe 用"内嵌 `drm_device` + `driver_priv→xe_file`"组织每设备/每文件状态。论述这种"基类 +
私有指针"模式相比"把所有字段塞进一个大结构"的好处。

**3.13.17** Xe 把显示能力做成"运行时钩子追加"而非编译期写死，论述这种设计对"i915/xe 共享 display
子系统"的意义与代价。

### L5 动手 / 调试

**3.13.18 ★（源码研读）** 在内核树通读 `xe_ioctls`（`xe_device.c:192`）全表，按本书篇章给每个 `XE_*`
ioctl 标注"将在第几章详解"，形成一张"Xe uAPI → 章节"对照表。

**3.13.19 ★** 若有 Intel GPU：`ls /dev/dri/` 确认 card 与 renderD 节点；`drm_info /dev/dri/renderD128`
对照 3.4.2 的 `driver_features`；`strace -e ioctl` 跑一个用到 GPU 的程序，找出 `DRM_IOCTL_XE_DEVICE_QUERY`
等调用。无硬件：用 ftrace 概念 + 源码完成等价分析。

**3.13.20 ★** 写一个最小程序：`open("/dev/dri/renderD128")` → 两段式调用 `DRM_IOCTL_XE_DEVICE_QUERY`
（先问 size 再取 data）查询引擎信息，打印各类引擎数量，对照第 01 章 1.8.3 的六类引擎。（无硬件则
基于 `xe_drm.h` 写出代码并解释预期，不要求运行。）

**3.13.21 ★（调试）** 设想现象：某进程在 render node 上调用一个显示相关 ioctl 得到 `-EACCES`。用
`drm_ioctl_permit` 四规则分析最可能是哪条拦的，以及正确做法（该用 primary node + 取 master）。

### 综合大题

**3.13.22 ★（综合）** 以 `DRM_IOCTL_XE_DEVICE_QUERY` 为例，完整叙述一次 ioctl 从用户态 `ioctl()` 到
`xe_query_ioctl` 返回的全链路：经过的每个核心函数、查表、拷贝、鉴权、`driver_priv` 取用。贯通 3.5/3.6/3.9。

**3.13.23 ★（综合）** 把第 00–03 章串起来：从 `modprobe`/PCI 探测，到设备注册出现节点，到 open 建立
`drm_file`+`xe_file`，到一次 ioctl 被分发鉴权处理。画一张跨章的"设备/进程生命周期"总图，标注每段
用到了第 01/02 章的哪些设施（托管资源、kref、xarray、SRCU）。

**3.13.24 ★（综合·设计）** 假设要给 Xe 加一个新的只读查询 ioctl（如查询某统计）。基于本章知识，列出
你需要改动的所有点：uAPI 头定义命令号与参数结构、`xe_ioctls` 加表项（选权限标志）、写处理函数
（从 `driver_priv` 取 `xe_file`、校验用户参数、copy_to_user）。说明每步为什么。

---

## 3.14 本章小结

- DRM 对用户态本质是**字符设备子系统**：注册设备 → 创建 `/dev/dri/*` 节点 → open 得上下文 → ioctl
  分发到处理函数。这条链是全书所有操作的公共入口。
- **四个根本结构**：`drm_driver`（每驱动一份，能力 + vtable）、`drm_device`（每设备一份，内嵌于
  `xe_device`）、`drm_minor`（每节点一份，xarray 分配编号）、`drm_file`（每次 open 一份，`driver_priv`
  挂 `xe_file`）。
- **Xe 注册全流程**：`xe_pci_probe` → `xe_device_create`（`devm_drm_dev_alloc` + 显示钩子追加能力）
  → `xe_device_probe` → `drm_dev_register`（可见性分界）。
- **ioctl 分发**：`drm_ioctl` 按命令号查"核心表/驱动表"、拷参数、调 `drm_ioctl_kernel`；
  `drm_ioctl_permit` 四规则鉴权。Xe 的 `xe_ioctls` 全带 `DRM_RENDER_ALLOW` → 渲染 uAPI 在 render node
  可用、无需 master。
- **设施复用**：minor/托管释放、`xe_file` 的 xarray+mutex+kref、热拔的 SRCU——全是第 02 章设施的落地。
- 一次 `DRM_IOCTL_XE_DEVICE_QUERY` 走通了整条链；`XE_VM_BIND`/`XE_EXEC` 用的是同一框架，只是 `func`
  与参数更复杂（第六篇）。

### 知识点清单（自检）

- [ ] 四个根本结构的职责、份数、内嵌/`driver_priv` 惯用法。
- [ ] Xe 注册全流程与"可见性分界"；显示能力的运行时追加。
- [ ] minor 的 xarray 分段分配与 `cardN`/`renderD(128+N)` 编号。
- [ ] `drm_ioctl` 十步、二级分发表、命令号编码。
- [ ] `drm_ioctl_permit` 四规则与 master/render 权限模型。
- [ ] 一次 `XE_DEVICE_QUERY` 的全链路。

### 延伸阅读

- 源码：`drivers/gpu/drm/xe/xe_device.c`（driver/open/ioctls）、`xe_pci.c`（probe）、
  `xe_device_types.h`（`xe_file`）；`drivers/gpu/drm/drm_drv.c`（注册/minor）、`drm_file.c`（open）、
  `drm_ioctl.c`（分发/鉴权）。
- uAPI：`include/uapi/drm/drm.h`（核心 ioctl）、`include/uapi/drm/xe_drm.h`（Xe ioctl，第 19 章精读）。
- 文档：`Documentation/gpu/drm-internals.rst`（驱动初始化）、`Documentation/gpu/drm-uapi.rst`。

> **下一章预告**：第 04 章《中断、事件与 vblank 底层》。本章建立了"ioctl 同步请求"的入口；第 04 章
> 讲 DRM 的**异步事件**通路——GPU/显示的中断如何变成 `drm_event` 投递给用户态的 poll/read，以及
> vblank 记账。它与本章的 `drm_file`（事件队列）直接相连，并为第 06 章原子翻页铺路。
