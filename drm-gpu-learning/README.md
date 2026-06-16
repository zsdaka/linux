# 深入 DRM 与 Intel GPU 驱动（以 Xe 为主）

> 一套以"教材 + 一学期课程"标准编写的中文深度教程。目标：学期末**真正掌握** Linux DRM
> 子系统与 GPU 驱动的原理与实现，能够**读懂、追踪、修改乃至开发**驱动代码。

本书以 **当前内核源码** 与 **Intel GPU 官方规格（Bspec / PRM）** 为唯一事实来源，
所有数据结构、调用链、寄存器/页表/命令语义都附 `file:line` 锚点或可访问的 spec 链接，
拒绝凭空想象与"网传二手知识"。

---

## 一、本书定位与特色

市面上讲 DRM/GPU 的中文材料，要么停在"概念科普"，要么是零散博客；本书要做的是
**像《深入理解 Linux 内核》那样把代码讲透，像《CSAPP》那样让你动手，像《计算机体系结构：
量化研究方法》那样用数据说话**。三条主线贯穿全书：

1. **代码即真相（UTLK 风格）**：核心数据结构给出**逐字段完整表格**（含 `file:line`、不变量、
   锁要求）；关键函数**逐行剖析**；每个机制都回答"**为什么这样设计**"，而不仅是"怎么做"。
2. **边学边练（CSAPP 风格）**：正文穿插 **随堂练习（Practice Problems）** 并随章给即时解答；
   课后另设 **分层习题（≥20 题）** 与 **turnkey 实验（自带骨架/校验/评分点）**，题答分离便于自测。
3. **量化驱动（H&P 风格）**：用真实 **延迟 / 带宽 / 占用 / 开销** 数字支撑论断与权衡；每章设
   **横向关联、综合实例、谬误与陷阱、历史演进** 四件套，把零散知识织成体系。

**贯穿全书的主题（每章按需回扣）**：
性能（latency / throughput）｜功耗（RC6 / RPS / power well）｜成本与复杂度（设计取舍）｜
可靠性（hang 检测、reset、恢复、devcoredump）｜保护与隔离（PPGTT / 进程级 VM、PXP、SR-IOV）｜
编程模型（uAPI 设计、隐式 vs 显式同步）｜演进趋势（legacy→atomic、i915→Xe、relocation→VM-bind）。

---

## 二、范围与取向

| 维度 | 选择 | 说明 |
|------|------|------|
| 硬件侧重 | **以 Xe 为主，i915 作对照** | 现代 Xe 驱动（`drm_gpuvm`、VM-bind、GuC-only 提交、SVM）是主线；i915 的 GTT/PPGTT、execbuffer、execlists 作为经典机制对照穿插 |
| 内容范围 | **显示(KMS) + 渲染/计算 全覆盖** | 两条主链路并进：显示路径（KMS→intel_display）与渲染路径（GEM/TTM→调度→VM-bind→exec→GuC） |
| 语言 | 中文 | 术语保留英文原词并给中文释义（见附录 B） |
| 事实来源 | 内核源码 + Intel Bspec/PRM | 拒绝二手转述；引用必附锚点/链接 |

---

## 三、代码基线与环境

- **内核版本**：`7.1.0-rc6`（`VERSION=7 PATCHLEVEL=1 SUBLEVEL=0 EXTRAVERSION=-rc6`）。
- **主要代码树**：
  - `drivers/gpu/drm/`：DRM 核心（`drm_*.c`）、`scheduler/`、`ttm/`、`display/`、参考驱动 `vkms/`、`tiny/`
  - `drivers/gpu/drm/xe/`：现代 Intel Xe 驱动（主线）
  - `drivers/gpu/drm/i915/`：传统 Intel i915 驱动（对照）
  - `drivers/dma-buf/`：dma-buf / dma-fence / dma-resv
  - `include/drm/`、`include/uapi/drm/`：内核内 API 与用户态 uAPI
- **官方文档**：`Documentation/gpu/*.rst`、`Documentation/driver-api/dma-buf.rst`。
- **实验环境**：QEMU（vkms / virtio-gpu / 软件 KMS）或带 Intel GPU 的真机；工具链见附录 A、D。

> 注：内核代码持续演进，本书锚点以上述基线为准。若你的树版本不同，函数名/行号可能漂移，
> 但**数据结构关系与设计思想**长期稳定——这正是本书着力讲透的部分。

---

## 四、全书结构（7 篇 24 章 + 4 附录）

> 编排逻辑：先打通 **DRM 通用框架**（篇一~篇四），再深入 **Intel 硬件与 Xe 实现**（篇五、篇六），
> 最后是 **Intel 显示驱动与进阶实战**（篇七）。i915 作为对照穿插于篇五、篇六。

### 第一篇 预备与基础
| 章 | 标题 | 文件 |
|----|------|------|
| 00 | 课程导引与开发环境 | `00-intro-setup.md` |
| 01 | GPU 硬件工作原理（PCIe / 中断 / 执行模型 / 内存层次） | `01-gpu-hardware.md` |
| 02 | DRM 依赖的内核基础设施（ww-mutex / kref / RCU / dma-mapping / iommu） | `02-kernel-infra.md` |

### 第二篇 DRM 核心
| 章 | 标题 | 文件 |
|----|------|------|
| 03 | DRM 设备模型与 ioctl | `03-drm-core.md` |
| 04 | 中断、事件与 vblank 底层 | `04-irq-event-vblank.md` |

### 第三篇 显示子系统（KMS）
| 章 | 标题 | 文件 |
|----|------|------|
| 05 | KMS 对象模型（CRTC / plane / encoder / connector / framebuffer） | `05-kms-objects.md` |
| 06 | 原子显示（Atomic Modesetting） | `06-atomic-kms.md` |
| 07 | 显示链路：encoder / bridge / panel | `07-display-chain.md` |
| 08 | 格式 / tiling / modifier / color / HDCP | `08-fb-format-color.md` |

### 第四篇 内存与同步
| 章 | 标题 | 文件 |
|----|------|------|
| 09 | GEM 内存管理基础 | `09-gem.md` |
| 10 | dma-buf / dma-fence / dma-resv / syncobj | `10-dma-buf-fence.md` |
| 11 | TTM 显存管理 | `11-ttm.md` |
| 12 | GPU 调度器、GPUVM 与 drm_exec | `12-scheduler-gpuvm.md` |

### 第五篇 Intel 硬件
| 章 | 标题 | 文件 |
|----|------|------|
| 13 | Intel GPU 架构与代际（tile / GT / engine / Xe-core / EU） | `13-intel-arch.md` |
| 14 | 寄存器访问、forcewake 与电源（RC6 / RPS） | `14-mmio-power.md` |
| 15 | 地址翻译：GGTT / PPGTT / 多级页表 | `15-address-translation.md` |
| 16 | GPU 命令与指令集（MI / 3D / compute / media、batch / ring） | `16-gpu-commands.md` |

### 第六篇 Xe 驱动实现
| 章 | 标题 | 文件 |
|----|------|------|
| 17 | Xe 设备模型与初始化 | `17-xe-device-init.md` |
| 18 | Xe 内存：BO / VM / VM-bind / SVM | `18-xe-memory-vm.md` |
| 19 | Xe 命令提交：exec_queue / LRC | `19-xe-exec.md` |
| 20 | GuC / HuC 固件深入（CTB / 调度 / PM / reset） | `20-guc-firmware.md` |

### 第七篇 Intel 显示与进阶
| 章 | 标题 | 文件 |
|----|------|------|
| 21 | intel_display 驱动实现 | `21-intel-display.md` |
| 22 | 性能、调试、健壮性与虚拟化 | `22-perf-debug.md` |
| 23 | 用户态闭环与综合实战（Capstone） | `23-userspace-capstone.md` |

### 附录
| 附录 | 标题 | 文件 |
|------|------|------|
| A | 实验手册 + 签名实验 | `appendix-a-labs.md` |
| B | 术语表与缩写 | `appendix-b-glossary.md` |
| C | Spec / 文档索引（含三本经典对照阅读建议） | `appendix-c-refs.md` |
| D | 测量与基准方法学（H&P 量化精神） | `appendix-d-methodology.md` |

---

## 五、每章统一模板（14 部分）

每章正文 `NN-slug.md` 固定包含以下 14 部分，融合 UTLK / CSAPP / H&P 教学法：

1. **学习目标与前置依赖**
2. **主题导引与设计动机 / 历史演进**（讲"为什么"——UTLK + H&P）
3. **原理与全景**（ASCII 架构 / 时序图）
4. **关键数据结构**（逐字段完整表格 + `file:line` + 不变量 / 锁——UTLK）
5. **核心代码走读**（逐函数 + 关键函数逐行注释——UTLK）
6. **算法 / 机制深入**（worked example）
7. **量化分析**（真实数字、权衡、基准方法——H&P）
8. **硬件视角**（Bspec / PRM 对照表 + 来源链接）
9. **综合实例：放在一起看**（端到端案例——H&P "Putting It All Together"）
10. **横向关联问题**（与其他子系统的交叉——H&P "Cross-Cutting Issues"）
11. **谬误与陷阱 + 调试**（误解 / 锁序 / 竞态 / 真实 bug 定位——H&P "Fallacies & Pitfalls"）
12. **随堂练习（Practice Problems）**（穿插正文，章末给即时解答——CSAPP）
13. **课后练习题**（仅题目，答案在 `answers/`——CSAPP Homework）
14. **本章小结 + 知识点清单 + 延伸阅读**

---

## 六、练习与实验体系

### 6.1 两层练习（对标 CSAPP）
- **随堂练习（Practice Problems）**：穿插正文、即学即测，**解答随章给出**。
- **课后练习题（Homework）**：每章 **≥20 题**，分五个难度层，每层 ≥3 题，外加 **2–3 道综合大题**：

| 层级 | 类型 | 考查 |
|------|------|------|
| L1 | 概念辨析 | 术语、机制、设计动机 |
| L2 | 数据结构与源码阅读 | 给定 `file:line`，解读字段 / 函数行为与不变量 |
| L3 | 机制分析 | 追踪调用链、解释锁序 / 竞态 / fence 传播、画时序图 |
| L4 | 设计与对比 | i915 vs Xe、TTM vs GEM、隐式 vs 显式同步等权衡 |
| L5 | 动手编程 / 调试 | 改写 / 扩展 vkms / tiny 或 Xe 路径、写 IGT 用例、故障诊断 |

**题答分离**：课后题的题目在正文第 13 节，答案在 `answers/NN-slug-answers.md`，
每题含 **标准答案 / 关键点逐条 / 常见错误 / 源码或 Bspec 锚点**，便于自测后核对。

### 6.2 签名实验（对标 CSAPP Data/Bomb/Malloc Lab）
跨章的 turnkey 实验，自带 **骨架代码 + 校验脚本 + 构建说明 + 分步指引 + 评分点**（详见附录 A）：

- **vkms 扩展 Lab**：给纯软件 KMS 驱动加一个特性（plane / property）并用 IGT 验证。
- **Batch 拆弹 Lab**：给一段 batch buffer 二进制 dump，逐条解码 MI / 3D 命令。
- **Fence 竞态 Lab**：给含同步 bug 的场景，用 trace 定位并修复。
- **VM-bind Lab**：用 Xe uAPI 完成 bind + exec，追踪页表更新与 fence。
- **性能优化 Lab**：测量并优化一段提交 / 拷贝路径，用 perf / OA / ftrace 给出前后数字。

---

## 七、目录结构

```
drm-gpu-learning/
├── README.md                      # 本文件：课程总览
├── 00-intro-setup.md              # 正文（含题目，不含答案）
├── 01-gpu-hardware.md
├── ……
├── 23-userspace-capstone.md
├── appendix-a-labs.md             # 附录
├── appendix-b-glossary.md
├── appendix-c-refs.md
├── appendix-d-methodology.md
└── answers/                       # 答案目录（题答分离）
    ├── 00-intro-setup-answers.md
    ├── 01-gpu-hardware-answers.md
    └── ……
```

> **如何使用答案**：先独立完成正文第 12 节随堂练习（解答随章）与第 13 节课后题，
> 再翻 `answers/` 对照。建议把课后题当成"读完一章能否过关"的硬指标。

---

## 八、推荐阅读顺序与前置依赖

本书章节有明确依赖关系，**强烈建议顺序阅读**。依赖图（→ 表示"需先读"）：

```
00 ──> 01 ──> 02 ──┬─> 03 ─> 04 ─> 05 ─> 06 ─> 07 ─> 08      (显示主线)
                   │
                   └─> 09 ─> 10 ─> 11 ─> 12                   (内存与同步主线)
                                          │
13 ─> 14 ─> 15 ─> 16 <────────────────────┘                  (Intel 硬件，依赖 12)
                  │
                  └─> 17 ─> 18 ─> 19 ─> 20                    (Xe 实现)
                                          │
06,08,12,20 ─────────────────────────────┴─> 21 ─> 22 ─> 23  (显示驱动与综合实战)
```

- **零基础 / 时间紧**：篇一 → 篇二 → 篇三（显示）或篇四（渲染）择一深入，再回头补另一条。
- **已懂 DRM 框架、想直攻 Intel**：快速过篇一~篇四，重点篇五 → 篇六。
- **只关心显示**：篇一、篇二、篇三、篇七的 Ch21；**只关心渲染/计算**：篇一、篇四、篇五、篇六。

---

## 九、一学期学习节奏建议（约 16 周）

| 周次 | 内容 | 里程碑（Checkpoint） |
|------|------|----------------------|
| 1–2 | 篇一（00–02） | 能讲清 GPU 作为 PCIe 设备如何被驱动接管、两条主链路全景 |
| 3 | 篇二（03–04） | 能独立追踪一次 ioctl 与一次 vblank 事件 |
| 4–6 | 篇三（05–08） | 能读懂并扩展 vkms 的 atomic 显示路径 |
| 7–9 | 篇四（09–12） | 能讲清一个 BO 从分配、共享、驻留到被 GPU 引用并同步的全过程 |
| 10–11 | 篇五（13–16） | 能对照 Bspec 解释地址翻译与一段 batch 命令 |
| 12–14 | 篇六（17–20） | 能完整追踪 Xe 的 bind + exec 并解释 GuC 调度 |
| 15–16 | 篇七（21–23）+ Capstone | 完成两个 capstone 全链路追踪与一个 IGT 用例 |

每篇末有 **Checkpoint 自测题**，过关再进入下一篇。

---

## 十、排版与约定

- **代码锚点**：形如 `drivers/gpu/drm/xe/xe_vm.c:1234`，可在内核树中直接定位（基线见第三节）。
- **结构体字段表**：列出 `字段名 | 类型 | 含义 | 不变量/锁`，对照源码定义处。
- **ASCII 图**：架构图、状态机、时序图一律用等宽 ASCII，便于在终端 / 纯文本中阅读。
- **硬件断言**：凡涉及寄存器位、页表项、命令格式的论断，均附 Intel Bspec/PRM 链接（见附录 C）。
- **术语**：首次出现给"英文（中文）"，统一收录于附录 B。
- **i915 对照**：以 `〔i915 对照〕` 小节标注，便于只看 Xe 主线的读者跳过。

---

## 十一、写作与交付方式（逐章评审制）

本书采用**逐章评审制**增量产出：每完成一章（正文 + 对应答案），即提交评审；
**有问题打回重写，通过后才写下一章**。每章独立 commit，便于按章 review 与回退。
篇幅上每章正文 **≥ 2000 行**（靠完整字段表、逐行走读、量化数据、≥20 题等真材料支撑，不注水）。

---

## 十二、参考资料（详见附录 C）

- **内核官方文档**：`Documentation/gpu/`（introduction / drm-internals / drm-kms / drm-mm / drm-uapi …）。
- **Intel 规格**：Intel Graphics **Bspec**、各代 **PRM（Programmer's Reference Manual）**（01.org / Intel 官网）。
- **三本方法论经典**（本书教学法所本）：
  - 《深入理解 Linux 内核》Bovet & Cesati — 数据结构 / 逐行代码 / 硬件相关取向。
  - 《深入理解计算机系统（CSAPP）》Bryant & O'Hallaron — 随堂练习 + turnkey 实验。
  - 《计算机体系结构：量化研究方法》Hennessy & Patterson — 量化分析 + 每章四件套。
- **社区**：dri-devel 邮件列表、freedesktop.org、Mesa 文档、IGT 文档。

---

> 准备好了吗？从 [`00-intro-setup.md`](./00-intro-setup.md) 开始，搭好环境、点亮第一块（虚拟）屏幕。
