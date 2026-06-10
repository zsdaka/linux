# PCI 桥枚举"两遍扫描 + read-back/allocate 分支"完整流程图

## 背景

PCI 子系统在 `pci_scan_child_bus_extend()` 中对同一层的所有桥遍历**两遍**：
- **Pass 0**：处理"已被配置好的桥"（BIOS/前一内核已写入 bus number）
- **Pass 1**：处理"需要重新配置的桥"（未配置 or 配置损坏）

两遍的目的是：先锁定所有已知的 bus 号范围，再给剩下的发新号，**避免新分配的号落到已有范围里形成重叠**。

---

## 全局调用结构

```
pci_scan_child_bus_extend(bus)
│
├── ① for devnr in 0..31:
│       pci_scan_slot(bus, devnr)    // 探测这一层的所有设备（读 config）
│
├── ② 统计桥数量 (hotplug_bridges / normal_bridges)
│
├── ③ 第一遍 (Pass 0)  ─── 处理"已配好"的桥
│     for_each_pci_bridge(dev, bus):
│         max = pci_scan_bridge_extend(bus, dev, max, 0, pass=0)
│
└── ④ 第二遍 (Pass 1)  ─── 处理"需重配"的桥
      for_each_pci_bridge(dev, bus):
          max = pci_scan_bridge_extend(bus, dev, max, buses, pass=1)
```

---

## `pci_scan_bridge_extend()` 内部分支流程图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│               pci_scan_bridge_extend(bus, dev, max, avail, pass)             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                ┌───────────────────────────────────────┐
                │ 读取桥寄存器 PCI_PRIMARY_BUS（32位）   │
                │   primary    = bits[7:0]              │
                │   secondary  = bits[15:8]             │
                │   subordinate = bits[23:16]           │
                └───────────────────────────────────────┘
                                    │
                                    ▼
                ┌───────────────────────────────────────┐
                │ 校验拓扑合理性                         │
                │ if primary≠parent_bus || sec≤parent   │
                │    || sec > sub:                      │
                │       broken = 1   // 标记损坏        │
                └───────────────────────────────────────┘
                                    │
                                    ▼
          ┌─────────────────────────────────────────────────────┐
          │ 判断分支条件：                                        │
          │   (secondary || subordinate)                         │
          │   && !pci_should_assign_new_buses(dev)               │
          │   && !broken                                         │
          │                                                      │
          │ 注: Live Update 下 pci_should_assign_new_buses()     │
          │     强制返回 false（因为 inherit_buses=true）          │
          └─────────────────────────────────────────────────────┘
                    │                              │
              条件为 TRUE                      条件为 FALSE
           （桥已配好 + 不重分配）           （未配/损坏/要求重分配）
                    │                              │
                    ▼                              ▼
    ┌──────────────────────────────┐   ┌──────────────────────────────┐
    │ ★ READ-BACK 路径 (Pass 0)    │   │ ★ ALLOCATE 路径 (Pass 1)     │
    │                              │   │                              │
    │ if (pass == 1): goto out     │   │ if (pass == 0):              │
    │    // Pass0 处理，Pass1跳过   │   │   暂时清空桥配置（防冲突）    │
    │                              │   │   goto out                   │
    │ 沿用旧 secondary/subordinate │   │   // Pass0 暂不处理           │
    │                              │   │                              │
    │ child = pci_add_new_bus(     │   │ ┌────────────────────────┐   │
    │           bus, dev, secondary│   │ │ [LIVE UPDATE 拦截]     │   │
    │         )                    │   │ │ if (inherit_buses):    │   │
    │                              │   │ │   "Cannot reconfig!"  │   │
    │ pci_bus_insert_busn_res(     │   │ │   goto out            │   │
    │     child, secondary, sub)   │   │ │   // 不枚举下游设备！  │   │
    │                              │   │ └────────────────────────┘   │
    │ // 递归扫描子总线：           │   │                              │
    │ cmax = pci_scan_child_bus(   │   │ next_busnr = max + 1         │
    │           child, sub-sec)    │   │  // 从全局最大号+1 发新号    │
    │                              │   │                              │
    │ max = max(max, subordinate)  │   │ child = pci_add_new_bus(     │
    │                              │   │           bus, dev, next_busnr│
    │ ✅ 完全不修改寄存器           │   │         )                    │
    │ ✅ bus number 原样复现        │   │                              │
    │                              │   │ pci_write_config_dword(      │
    └──────────────────────────────┘   │   dev, PCI_PRIMARY_BUS,      │
                                       │   new_primary|new_sec|new_sub│
                                       │ )  // 写入新 bus number      │
                                       │                              │
                                       │ // 递归扫描子总线：           │
                                       │ max = pci_scan_child_bus(    │
                                       │         child, avail)        │
                                       │                              │
                                       │ pci_write_config_byte(       │
                                       │   dev, SUBORDINATE, max)     │
                                       │ // 回填最终 subordinate      │
                                       │                              │
                                       │ ⚠️ 新 bus 号 = max+1         │
                                       │    可能和已保留设备撞号！      │
                                       └──────────────────────────────┘
```

---

## Live Update 场景：强制全走 READ-BACK

```
┌──────────────────────────────────────────────────────────────────┐
│                    新内核启动 (kexec 之后)                         │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ pci_liveupdate_setup_device():                                    │
│   发现有 incoming 保留设备 → 对当前 bus 上所有 dev 设:             │
│       dev->liveupdate_inherit_buses = true                       │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ pci_should_assign_new_buses(dev):                                 │
│   if (dev->liveupdate_inherit_buses) return false;               │
│   // → 条件 (sec||sub) && !assign && !broken 为 TRUE             │
│   // → 所有桥全部走 READ-BACK 路径                                │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ pci_scan_bridge_extend():                                         │
│                                                                   │
│   所有桥 → 走 READ-BACK                                          │
│   ALLOCATE 路径无法进入（无桥能满足进入条件）                       │
│                                                                   │
│   结果:                                                           │
│   ✅ 每个桥的 secondary/subordinate 原样从寄存器读回               │
│   ✅ "max" 只随 read-back 的 subordinate 推进                     │
│   ✅ 没有任何一个 bus 号是"从池子里新发的"                          │
│   ✅ D/F 来自设备硬件探测 → BDF 三元组逐位复现                     │
└──────────────────────────────────────────────────────────────────┘
```

---

## 正常（非 Live Update）场景：两遍配合

```
                   pci_scan_child_bus_extend
                              │
           ┌──────────────────┴──────────────────┐
           │                                     │
    Pass 0: for_each_bridge             Pass 1: for_each_bridge
           │                                     │
           ▼                                     ▼
    ┌──────────────┐                    ┌──────────────┐
    │ 桥A (已配好) │                    │ 桥B (未配)   │
    │ sec=3 sub=5  │                    │ sec=0 sub=0  │
    └──────┬───────┘                    └──────┬───────┘
           │                                   │
    走 READ-BACK                         走 ALLOCATE
    child bus = 3                        next_busnr = max+1 = 6 ← ← ←
    max 推进到 5                         因为 Pass0 先执行，
           │                              max 已经包含了 3~5
           │                              所以新号从 6 开始
           ▼                              不会和桥A 重叠！
    递归扫描 bus 3~5 下设备                     │
                                               ▼
                                         分配 bus 6，递归扫描
```

**两遍的妙处：** Pass 0 先把所有"已知领地"记录到 max 里，Pass 1 从 `max+1` 起步，天然避开已知范围。

---

## 为什么 Live Update 必须全局继承（不能只冻结部分桥）

```
假设: 只冻结桥X（被保留设备所在），桥Y 允许重分配

        Root (bus 0)
        /          \
   桥X (sec=5)     桥Y (未配, Pass1 分配)
      |                |
   设备A (bus=5)      设备B
   [PRESERVED]

Pass 0: 桥X read-back → max 推到 5
Pass 1: 桥Y allocate → next = max+1 = 6 ← OK？

但如果桥Y 的下游有子桥:
   桥Y (sec=6, sub=8)
      └── 子桥Z allocate → next = 9

看起来安全? 考虑另一种拓扑:

        Root (bus 0)
        /          \
   桥X (sec=3,sub=5)   桥Y (sec=6,sub=6, 已配但 broken!)
      |                      |
   设备A (bus=5)            设备B

- Pass 0: 桥X read-back → max=5; 桥Y broken → 不处理
- Pass 1: 桥Y allocate → next = max+1 = 6? 但如果:
    旧内核的 max 不同步，或者 subordinate 被截断...
    → 一旦有任何边界 case，6 号可能和某条路径下的保留设备冲突

所以设计者选择: 全部继承，ALLOCATE 路径全程静默 → 从根本上杜绝风险
```

---

## 一句话总结

| 场景 | Pass 0 行为 | Pass 1 行为 | Bus 号来源 |
|------|------------|------------|-----------|
| 正常启动 | 已配桥 read-back | 未配桥 max+1 分配 | BIOS + 内核分配混合 |
| Live Update | **所有桥** read-back | **所有桥已在 Pass0 处理完，Pass1 无事可做** | 全部从旧内核寄存器继承 |
| Live Update 遇坏桥 | 标记 broken | **拒绝重配 + 拒绝枚举下游** | 无（安全放弃） |
