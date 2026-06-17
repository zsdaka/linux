# 第 03 章 课后练习题参考答案

> 对应正文：[`03-drm-core.md`](../03-drm-core.md) 第 3.13 节。先独立完成再核对。
> 每题给出**标准答案 / 关键点 / 常见错误 / 源码锚点**。参考驱动统一为 Xe。

---

## L1 概念辨析

### 3.13.1 四结构职责与份数

**标准答案**：`drm_driver`——能力声明 + vtable（**每驱动一份**静态常量）；`drm_device`——一个设备实例
的总枢纽（**每设备一份**，内嵌于 `xe_device`）；`drm_minor`——一个字符设备节点（每节点一份）；
`drm_file`——一次 open 的上下文（**每次 open 一份**，`driver_priv` 挂 `xe_file`）。
**锚点**：3.4、3.2.3。

### 3.13.2 master vs render

**标准答案**：master 持有者才能执行改显示的 ioctl（`DRM_MASTER`），同一时刻一个设备只一个 master，
对应 primary node（`cardN`）；render client 只能做渲染/计算（`DRM_RENDER_ALLOW`），无需 master，对应
render node（`renderD128+`）。**锚点**：3.2.2、3.5.7。

### 3.13.3 feature 与显示能力

**标准答案**：`driver_features` 决定建哪些节点/放行哪些能力。Xe 静态结构只列渲染侧（GEM/RENDER/
SYNCOBJ/GEM_GPUVA），显示能力由 `xe_display_driver_set_hooks(&driver)` 运行时追加 → 说明 feature
**不必是编译期常量**，可按平台/配置动态裁剪。**锚点**：3.4.2、`xe_device.c:444`。

### 3.13.4 cmd 编码与二级分发

**标准答案**：`cmd` 含方向(读/写/读写)、类型(`DRM_IOCTL_BASE`)、序号 `nr`、参数大小。`drm_ioctl` 用
`nr` 判断：落在 `[DRM_COMMAND_BASE,END)` → 驱动表 `dev->driver->ioctls`(xe_ioctls)；否则 → 核心表
`drm_ioctls[]`。**锚点**：3.5.6、3.6.2。

### 3.13.5 四规则 + render 禁显示

**标准答案**：① ROOT_ONLY 需 CAP_SYS_ADMIN；② AUTH 需已鉴权或 render 客户端；③ MASTER 需持 master；
④ 没标 DRM_RENDER_ALLOW 的 ioctl 拒绝 render 客户端。**规则 (4)（配合 (3)）** 直接实现"render node
不能控显示"。**锚点**：3.5.7、`drm_ioctl.c:599`。

### 3.13.6 driver_priv

**标准答案**：`driver_priv` 是驱动挂私有每文件状态的 void* 扩展点（Xe 挂 `xe_file`）。核心只认
`drm_file`（句柄表/事件/鉴权），不知 `xe_file`；驱动在 `.open` 分配并挂上、在 ioctl 处理函数取回。
体现"核心通用 + 驱动特化"分层。**锚点**：3.4.3、3.6.3。

---

## L2 数据结构与源码阅读

### 3.13.7 Xe driver 结构

**标准答案**：`driver_features = DRIVER_GEM|DRIVER_RENDER|DRIVER_SYNCOBJ|DRIVER_SYNCOBJ_TIMELINE|
DRIVER_GEM_GPUVA`；回调 `.open=xe_file_open`、`.postclose=xe_file_close`、`.gem_prime_import`、
`.dumb_create`、`.ioctls=xe_ioctls`、`.num_ioctls`、`.fops=&xe_driver_fops`。**MODESET/ATOMIC 等显示
能力不在静态结构里，由 `xe_display_driver_set_hooks` 运行时追加**。**锚点**：`xe_device.c:392`、3.4.2。

### 3.13.8 xe_file_open

**标准答案**：初始化 `vm.xa`（vm_id→xe_vm）与 `exec_queue.xa`（id→xe_exec_queue）两张 xarray，各配
mutex 保护查找/增删（进程上下文，非中断，故 mutex 而非 spinlock）；`kref_init(&xef->refcount)` 因
`xe_file` 可能被异步引用、需引用计数管生命周期；最后 `file->driver_priv = xef`。
**锚点**：`xe_device.c:82`、`xe_device_types.h:572`、第 02 章。

### 3.13.9 drm_ioctl 十步

**标准答案**：决定查哪张表——步 (4)/(5)（按 `nr` 是否在驱动号段）；拷入参数——步 (8) `copy_from_user`；
鉴权+调处理——步 (9) `drm_ioctl_kernel`→`drm_ioctl_permit`→`func`；拷出结果——步 (10) `copy_to_user`。
**锚点**：3.5.6、`drm_ioctl.c:821`。

### 3.13.10 drm_ioctl_permit 四 if

**标准答案**：if1 拦非 root 调 ROOT_ONLY；if2 拦未鉴权且非 render 调 AUTH；if3 拦非 master 调 MASTER；
if4 拦 render 客户端调没标 RENDER_ALLOW 的 ioctl。`xe_ioctls` 全带 RENDER_ALLOW → 全部能过 if4 →
Xe 渲染 uAPI 在 render node 可用。**锚点**：3.5.7。

### 3.13.11 ★ 128 由来

**标准答案**：`DRM_MINOR_LIMIT(t)` 用 `XA_LIMIT(64*type, 64*type+63)` 给每类 minor 分段；render 段
起始使 render 编号从 128 起。`drm_minor_alloc` 用 `xa_alloc` 分配 index，并
`drmm_add_action_or_reset(dev, drm_minor_alloc_release, minor)` 登记托管释放（设备销毁自动回收）。
**锚点**：`drm_drv.c:137/143/163`、3.6.1。

---

## L3 机制分析

### 3.13.12 注册→ioctl 时序图

**标准答案**：`xe_pci_probe`→`xe_device_create`(devm_drm_dev_alloc+显示钩子)→`xe_device_probe`(硬件
初始化)→`drm_dev_register`【★可见性分界，renderD128/cardN 出现】→用户 `open`→`drm_open`→
`xe_file_open`(建 xe_file)→用户 `ioctl(DEVICE_QUERY)`→`drm_ioctl`(查 xe_ioctls)→`drm_ioctl_kernel`→
`drm_ioctl_permit`(过 RENDER_ALLOW)→`xe_query_ioctl`。**锚点**：3.3、3.9.2。

### 3.13.13 array_index_nospec + 不信用户态

**标准答案**：`array_index_nospec` 防 Spectre——即使越界检查通过，也防 CPU 推测执行用越界下标访问
数组泄漏数据。"用内核表的 func"——`func=ioctl->func` 取自内核静态表，绝不用用户态传的函数/大小，
防止用户态指定任意内核函数或错误大小造成越权/越界。**锚点**：3.5.6 (6)、3.11 谬误 3。

### 3.13.14 热拔竞态

**标准答案**：ioctl 执行中设备可能被拔出，处理函数若继续访问已释放的设备资源 → UAF。
`drm_ioctl` 入口 `drm_dev_is_unplugged` 先挡掉已拔出的；长流程用 `drm_dev_enter/exit`（SRCU）标记
"正在使用设备"，拔出方等所有使用者退出才真正释放（第 02 章 SRCU）。**锚点**：3.5.6 (3)、3.11 陷阱 6。

---

## L4 设计与对比

### 3.13.15 二级分发 vs 各写入口

**标准答案**：二级表（核心表 + 驱动表共用 `drm_ioctl` 入口）让核心通用 ioctl 与鉴权/拷贝/防护逻辑
只实现一次，70+ 驱动复用；驱动只提供自己的表项。若各写入口，则鉴权/拷贝/Spectre 防护等要重复实现
70 遍，易不一致、易出安全漏洞。杠杆率高（第 00 章 0.7）。**锚点**：3.6.2、3.6.3。

### 3.13.16 基类 + 私有指针

**标准答案**：内嵌 `drm_device` + `driver_priv→xe_file` 让核心结构稳定、驱动自由扩展私有字段，互不
污染；编译期解耦（核心不需知 xe_file 定义）。相比"一个大结构塞所有字段"，避免核心被各驱动字段撑爆、
便于核心独立演进。**锚点**：3.4.3/3.4.4、3.6.3。

### 3.13.17 显示能力运行时追加

**标准答案**：i915 与 xe 共享同一套 `display/` 子系统；用"钩子运行时追加显示能力/回调"让两个驱动
都能插入共享显示代码，而无需把显示能力写死进各自静态结构、也便于按是否支持显示裁剪。代价：能力
不再全是编译期可见常量，读代码要知道有运行时追加这一步（易踩谬误 1）。**锚点**：3.4.2、3.11 谬误 1。

---

## L5 动手 / 调试

### 3.13.18 ★ uAPI→章节表

**参考**：XE_DEVICE_QUERY→第03/17章；XE_GEM_CREATE/GEM_MMAP_OFFSET→第09/18章；XE_VM_CREATE/
VM_DESTROY/VM_BIND/MADVISE/VM_QUERY_MEM_RANGE_ATTRS→第18章；XE_EXEC/EXEC_QUEUE_*→第19章；
XE_WAIT_USER_FENCE→第10/19章；XE_OBSERVATION→第22章。**锚点**：`xe_device.c:192`、3.5.4。

### 3.13.19 ★ 节点/strace

**要点**：`ls /dev/dri/` 见 card0+renderD128；`drm_info /dev/dri/renderD128` 的 Driver features 与
3.4.2 一致（GEM/RENDER/SYNCOBJ…，若有显示再加 MODESET/ATOMIC）；`strace -e ioctl` 见
`DRM_IOCTL_XE_DEVICE_QUERY` 等。无硬件用源码 + ftrace 概念替代。**锚点**：3.8、3.9。

### 3.13.20 ★ DEVICE_QUERY 程序

**要点**：`open` render node→构造 `drm_xe_device_query{.query=DRM_XE_DEVICE_QUERY_ENGINES,.size=0}`→
第一次 ioctl 得 size→malloc→设 `.data`→第二次 ioctl 取数据→解析引擎数组。两段式是可变长 uAPI 常见
模式。无硬件则基于 `xe_drm.h` 写出并解释预期。**锚点**：3.9.1、`xe_drm.h`。

### 3.13.21 ★ -EACCES 分析

**标准答案**：render node 上调显示 ioctl 得 -EACCES，最可能是 `drm_ioctl_permit` 规则 (4)（该 ioctl
没标 DRM_RENDER_ALLOW，拒绝 render 客户端）或规则 (3)（要 master 而 render 无 master）。正确做法：
改用 primary node（cardN）、取得 master 后再调显示 ioctl。**锚点**：3.5.7、3.11 谬误 2。

---

## 综合大题

### 3.13.22 ★ DEVICE_QUERY 全链路

**参考**：见 3.9.2 链路——`ioctl()`→`drm_ioctl`（解 nr、命中 xe_ioctls[DEVICE_QUERY]、copy_from_user
到 stack_kdata）→`drm_ioctl_kernel`→`drm_ioctl_permit`（RENDER_ALLOW 过规则4）→`xe_query_ioctl`
（`file->driver_priv`→`xe_file`、按 query 填引擎/内存/GT、回写）→`copy_to_user`→返回。**评分**：查表/
拷贝/鉴权/driver_priv/回写齐全。**锚点**：3.5/3.9。

### 3.13.23 ★ 跨章生命周期总图

**参考**：modprobe/PCI 探测(第01/02章设备模型)→`xe_pci_probe`→`xe_device_create`(devm_drm_dev_alloc
托管=第02章)→`xe_device_probe`(BAR 映射/中断=第01章)→`drm_dev_register`(minor xarray=第03章)→
open→`drm_file`+`xe_file`(xarray/kref=第02章)→ioctl(分发/鉴权,热拔 SRCU=第02章)。**评分**：每段标
出复用的第01/02章设施。**锚点**：3.10、第00–03章。

### 3.13.24 ★ 加一个查询 ioctl

**参考**：① 在 `include/uapi/drm/xe_drm.h` 定义新命令号（`DRM_IOCTL_XE_*`）与参数结构（含可变长则
用 size/data 两段式）；② 在 `xe_ioctls` 加 `DRM_IOCTL_DEF_DRV(XE_XXX, xe_xxx_ioctl, DRM_RENDER_ALLOW)`
（只读查询选 RENDER_ALLOW，让 render node 可用、无需 master）；③ 写 `xe_xxx_ioctl(dev, data, file)`：
`file->driver_priv`→`xe_file`、校验用户参数（大小/范围）、填数据、（可变长走两段式）。每步理由：uAPI
要稳定 ABI；权限标志决定可用节点；处理函数不能信用户态、要从 driver_priv 取上下文。**锚点**：3.5/3.6、
3.13.18。

---

> 自评：L1–L2 应全对；L3 能独立画注册→ioctl 时序、讲 Spectre/热拔防护；L4 能论证二级分发/基类私有/
> 显示钩子的设计；L5 能读懂 xe_ioctls 全表、（有硬件则）实际 strace/写 DEVICE_QUERY。本章是后续所有
> ioctl 的入口框架，务必把 3.5（注册+分发+鉴权）与 3.9（全链路）吃透。
