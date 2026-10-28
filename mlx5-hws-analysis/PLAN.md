# mlx5 HWS 分析计划

日期：2026-10-28（Asia/Shanghai）

状态：分析已经完成。报告在 `mlx5-hws-analysis/mlx5-hws-model.docx`，仿真在 `mlx5-hws-analysis/sim/`。本目录不改驱动。

## 本版变更

验证程序的主实验改为千万级流表实插，不再用十万级样本外推。

- 每组数据集至少插入 10,000,000 条互异流。
- 表的行数、桶深按本仓库 `matcher.c` 对 `MLX5HWS_MATCHER_RESOURCE_MODE_RULE` 的算法，取能覆盖该规模的 `num_log`。
- 另两组仍在同一张表上插入 16,777,216 条和 20,000,000 条，用来看驱动配满点和配满之后。这三组都在千万级以上。
- 插入必须逐条写入槽位数组。占用直方图可以聚合，不能用小表蒙特卡洛代替这三次运行。
- 已确认：`num_log` 表示哈希行规模，主表 STE 约为规则数的 4 倍是刻意余量；报告与仿真增加 **主表 STE 利用率**，以及相对 **\(2^{22}\) 行** 几何的对照实验 D。
- 已更正：NIC_RX、NIC_TX、FDB 是三个并列的域。本树的 smfs/hmfs 只挂在 FDB 根上，NIC_RX 与 NIC_TX 留在固件流表。
- 已展开 legacy 与 switchdev：三个根都在，两边创建的流表和 FDB 转向模式不同。

## 目标

从 `drivers/net/ethernet/mellanox/mlx5/core/steering/hws/` 反推硬件流表：域、namespace、表、matcher 如何配合；流表如何存储、插入、查找；插入步数为什么有上界，从而在规则数到千万时单次插入代价仍是常数。给出 RX、TX 流水线，标出流表、TIR、TIS、RQ、SQ、CQ、EQ 各在哪一层。

为这套结构建立桶容量模型，用仿真在千万级规模上对照公式。仿真对照的是模型和驱动里的几何参数，不是 ConnectX 上的计时。本环境没有这块网卡。

## 源码范围

| 对象 | 文件 | 计划里要写清的点 |
| --- | --- | --- |
| Context、发送队列 | `context.c` `send.c` `send.h` | 每条队列一对 SQ/CQ；GTA WQE；doorbell；完成回收 |
| Table | `table.c` `table.h` | level、miss、FDB 的 RX/TX 资源 |
| Matcher、RTC、STE | `matcher.c` `matcher.h` `pool.c` `buddy.c` | 行、列、碰撞表、hash / index |
| Key | `definer.c` `definer.h` | tag 从哪些报文字段来；普通 STE 与 jumbo |
| Action | `action.c` `action.h` `pat_arg.c` | STC、action template、参数池 |
| Rule | `rule.c` `bwc.c` `bwc_complex.c` | 满桶重试、碰撞 RTC、resize |
| 与 fs 的衔接 | `fs_hws.c` `fs_hws_pools.c` | namespace、表链、落到 TIR / TIS / vport / drop |

本树公开表类型只有 `MLX5HWS_TABLE_TYPE_FDB`。FDB matcher 仍按 `MLX5HWS_MATCHER_SIZE_TYPE_RX` 和 `MLX5HWS_MATCHER_SIZE_TYPE_TX` 各建一套 RTC。RX、TX 数据路径用 `fs_core` 里的 TIR、TIS、RQ、SQ、CQ、EQ 补全，并标明哪些路径在 HWS 源码里有对象，哪些是同一套 PRM 流水线在 NIC 域上的对应物。

Namespace 指内核 `mlx5_flow_namespace`。一张 namespace 里的表数随 level 和 offload 规则变化。报告列出 `fs_hws.c` 实际创建的锚点表，以及每张表的 match template（key）和 action template（action），并分开写固定锚点和动态表。

## 插入上界要核对的代码事实

`matcher.h` 里的常数：

- `MLX5HWS_MATCHER_ASSURED_MAIN_TBL_DEPTH = 2`，写入 RTC 的 `log_depth`。每行 STE 数是 \(2^2 = 4\)。
- `MLX5HWS_MATCHER_ASSURED_COL_TBL_DEPTH = 4`，碰撞表 `log_depth`。每行 STE 数是 \(2^4 = 16\)。
- `MLX5HWS_MATCHER_ASSURED_ROW_RATIO = 5`。碰撞表行数是主表行数的 \(2^{-5} = 1/32\)，注释里的 3% 即 \(\log_2(0.03) \approx 5\)。
- `MLX5HWS_MATCHER_ASSURED_RULES_TH = 10`。`num_log > 10` 才挂碰撞表。千万级必挂。

`MLX5HWS_MATCHER_RESOURCE_MODE_RULE` 下，`union mlx5hws_matcher_size` 的 `rule.num_log` 与 `table.sz_row_log` 共用第一个字节。主表 `log_hash_size = num_log`。`hws_matcher_rules_to_tbl_depth()` 把大表的 `log_depth` 设为 2。碰撞 matcher 在 `hws_matcher_create_col_matcher()` 里把行对数减 5，列对数设为 4。代码只从碰撞表的行对数里减，不从主表行对数里减 `log_depth`。

\(2^{24}\) 条 key、`num_log = 24` 时，主表是 \(2^{24}\) 行而不是 \(2^{22}\) 行。每行 4 个槽，STE 总数是 \(2^{24+2} = 2^{26}\)，等于 key 数的 4 倍。平均每行 1 条 key，行内另外 3 个槽是余量。碰撞表是 \(2^{19}\) 行、每行 16 个槽，行数为主表的 1/32。`hws_matcher_bind_mt()` 里 STE 区间是 `sz_row_log + sz_col_log`，BWC 判断能否再放大时用的也是 `size_log + ASSURED_MAIN_TBL_DEPTH`。这两处都把深度加进行数的对数，对应“槽位 = 行数 × 4”，对应“行数 = key 数”，不对应“行数 = key 数 / 4”。若把主表收成 \(2^{22}\) 行，驱动会把它理解成只承诺 \(2^{22}\) 条规则。

插入 WQE 是 `MLX5HWS_WQE_OPCODE_TBL_ACCESS`（0x2c），opmod 为 GTA STE。硬件按 hash 在固定深度的行里找空 STE。主行满时软件再向碰撞 RTC 发一条 WQE（`send.c` 的 retry 路径）。成功插入的硬件往返是 1 条或 2 条 WQE，与已经插入了多少条无关。

## 容量、内存与刻意余量

`num_log` 在 `RESOURCE_MODE_RULE` 下表示 **目标规则规模对应的哈希行数**（RTC `log_hash_size`），不是「总 STE 槽位数」。BWC 在 `num_of_rules * 100 / 70 >= 2^size_log` 时扩表，阈值对着 **行数** 而不是槽位数。

主表 STE 总数 \(M = 2^{r+2} = 4R\)。插入 \(n\) 条互异规则且均匀哈希时，期望占用槽位数为 \(n\)（每条占 1 槽），**主表 STE 利用率**：

\[
u_{\mathrm{main}}(n) = \frac{n}{M} = \frac{n}{4R}
\]

当 \(n = R = 2^r\)（BWC 意义上的「配满」）时，\(u_{\mathrm{main}} = 25\%\)：约 **75% 主表 STE 在满表时仍空**。这不是实现疏漏，而是驱动把 **行深 4** 当作同一哈希行上的冲突吸收带，让 \(\lambda = n/R \approx 1\) 时泊松方差大多落在主表内，溢出由碰撞表（\(R_c = R/32\)，行深 16）承接。

| 视角 | `num_log=24`、\(n=2^{24}\) | 反事实 \(2^{22}\) 行 × 4 槽、仍插 \(2^{24}\) 条 key |
| --- | --- | --- |
| 主表 STE | \(2^{26}\) | \(2^{24}\)（槽位总数 = key 数） |
| 每行期望负载 \(\lambda\) | 1 | 4（顶满行深 4） |
| 设计意图 | 低主表溢出、少 2 步 WQE | 槽位更「满」但主表几乎必溢出 |
| 与驱动关系 | `num_log=24` 的实际行为 | 等价于把 `num_log` 设为 22 却插入 \(2^{24}\) 条，超出 BWC 承诺 |

固件在 `hws_matcher_bind_mt()` 时按 `sz_row_log + sz_col_log` 预留 STE 区间；空槽同样占 NIC 流表预算。报告里单独一节说明：**存储效率 vs 插入成功率 vs 有界 WQE 步数** 的权衡，并给出 \(u_{\mathrm{main}}\)、碰撞表占用率、失败率、2 步插入占比四条曲线。

## 流水线（报告中要画清的层次）

RX：端口进入 → namespace 上的 flow table 链 → matcher 的 match RTC（hash 选行、行内比较 tag）→ 命中则执行 STC → 终结动作为 TIR → RQ 收包 → CQ 完成 → EQ 通知主机。

TX：主机敲 SQ doorbell → TIS 取发送描述符 → egress 侧 flow table / matcher → STC → 从 vport 或 wire 发出。FDB 的同一条规则在 RX RTC 与 TX RTC 各有一份 STE，`table type == FDB` 时 `hws_matcher_create_rtc()` 会建第二套。

流表处理在 RQ/SQ 数据面之前的 steering 级。RQ、SQ 是数据包队列；HWS 自己的规则下发用另一对专用 SQ/CQ，上面跑的是 GTA WQE，不是数据包。EQ 在完成通知层：数据包 CQ 和规则下发 CQ 都可以挂到 EQ，规则快路径以轮询 CQ 为主。

## 数学模型

记主表行数 \(R = 2^{r}\)，每行容量 \(d = 2^{2} = 4\)。碰撞表行数 \(R_c = 2^{r-5}\)，每行容量 \(d_c = 2^{4} = 16\)。插入 \(n\) 条且行哈希均匀时，每行球数 \(X \sim \mathrm{Poisson}(\lambda)\)，\(\lambda = n / R\)。\(R\) 在千万级实验里是 \(2^{24}\)，Poisson 近似足够。

主表放不下的比例：

\[
f(n) = \frac{\mathbb{E}[\max(X - d, 0)]}{\lambda}
\]

这些规则再哈希进碰撞表，\(\lambda_c = n f(n) / R_c\)，\(Y \sim \mathrm{Poisson}(\lambda_c)\)。失败比例：

\[
p_{\mathrm{fail}}(n) = f(n) \cdot \frac{\mathbb{E}[\max(Y - d_c, 0)]}{\lambda_c}
\]

（\(\lambda_c = 0\) 时失败比例为 0。）

单次成功插入的 GTA 步数 \(S \in \{1, 2\}\)：主行未满为 1，否则为 2。查找时主行比较次数不超过 4，落到碰撞行时再不超过 16。步数上界不随 \(n\) 增长。千万条每秒靠的是这个上界，加上多条发送队列把固定代价的 WQE 流水打满，而不是靠插入成本随表长下降。

报告里的吞吐式子只作为数量级核对：\(Q\) 条发送队列，每条队列的 WQE 提交与 CQ 回收速率，再乘 \(1 - f(n)\) 的单 WQE 比例和 \(f(n)\) 的双 WQE 比例。不把“千万规则/秒”写成与负载无关的实测数字。

## 验证程序

程序放在 `mlx5-hws-analysis/sim/`，用 C 实现。Python 只负责把仿真输出的 CSV 画成 PNG。

### 表几何

三组实验共用驱动会为“至少一千万条”配出的那张表。\(10^{7}\) 大于 \(2^{23} = 8{,}388{,}608\)，所以 `num_log = 24`。

| 实验 | 插入条数 \(n\) | 主表 | 碰撞表 | \(\lambda = n/2^{24}\) |
| --- | --- | --- | --- | --- |
| A 千万级设计点 | 10,000,000 | \(2^{24}\) 行 × 4 STE | \(2^{19}\) 行 × 16 STE | 0.596 |
| B 驱动按 `num_log` 配满 | 16,777,216 | 同上 | 同上 | 1 |
| C 超过配给 | 20,000,000 | 同上 | 同上 | 1.192 |

主表槽位按 `uint64` 指纹预分配：\(2^{24} \times 4 \times 8 = 512\,\mathrm{MiB}\)。碰撞表：\(2^{19} \times 16 \times 8 = 64\,\mathrm{MiB}\)。空槽用 0。指纹为 0 的规则改写为一个非 0 常数，避免和空槽冲突。

### 对照实验 D（\(2^{22}\) 行 vs 驱动 \(2^{24}\) 行）

在同一均匀 5 元组、同一插入条数 **\(n = 16{,}777{,}216 = 2^{24}\)** 下，跑两套几何（均带碰撞表，碰撞表行对数仍比主表少 5）：

| 配置 | 主表 | 碰撞表 | \(\lambda = n/R\) | 说明 |
| --- | --- | --- | --- | --- |
| **D-mlx**（驱动） | \(2^{24}\) 行 × 4 | \(2^{19}\) 行 × 16 | 1 | 与实验 B 相同，单独汇总利用率与 2 步占比 |
| **D-tight**（反事实） | \(2^{22}\) 行 × 4 | \(2^{17}\) 行 × 16 | 4 | 槽位总数 \(2^{24}\)，与 key 数相等；**不是** `num_log=24` 的驱动配置 |

D 的配满点只跑均匀集、\(n = 2^{24}\) 一次。另外用和负载扫描相同的五档 key 数（\(n/2^{24}\) 为 25%、50%、70%、90%、100%）再跑一遍 \(2^{22}\) 行几何，实验名 D25–D100，用来对照同一批 key 在两种行数下的溢出、失败和每行深度。输出对比表：

- 主表 STE 利用率 \(u_{\mathrm{main}}\)、碰撞表 STE 利用率 \(u_{\mathrm{col}}\)
- 主表溢出条数、失败条数、2 步插入占比
- 平均主行占用（插入结束后非空行的平均深度）

预期：**D-tight** 的 \(u_{\mathrm{main}}\) 接近 100%，但溢出与 2 步 WQE 显著高于 **D-mlx**；用于量化「多预留 4× 主表行 STE」换来的插入路径差异。Word 与 PNG 各增加一张 D 对比图（柱状或并列曲线）。

### 数据集

每个数据集都跑 A、B、C，也就是每种分布都有一次不少于一千万条的实插。

1. 均匀随机。互异 5 元组（源/目的 IPv4、L4 协议、源/目的端口），行号由 tag 的 64 位哈希取低 24 位。B、C 在同一生成器上继续取到对应条数。
2. 重尾流。唯一流的条数仍是 A/B/C。源 IP 按 Zipf，指数取数据中心里常见的 1.1 到 1.3 这一档（实现时固定一个值并写进报告）；目的端口在少量周知端口和均匀高端口之间混合。用来看行占用偏离 Poisson 之后，步数上界是否还在，失败率偏离多少。
3. 公开报文。能下载则抽取 5 元组。迹内唯一流不足 \(10^{7}\) 时，报告单独给出迹内全量结果，再用该迹的经验分布采样补到 \(10^{7}\) 跑 A。补齐部分和迹内部分分列，不把补齐样本叫作原始迹。

哈希函数在设备内部完成，驱动只提交 tag。仿真使用一个固定的 64 位哈希，并在均匀集上再换一种哈希复跑 A，确认结论不绑在某一个混合函数上。行号均匀性是模型假设，重尾集专门打破它。

### 必须打出的数

对每一次运行：

- 逐条插入，记录该条是 1 步、2 步还是失败。
- 断言：凡成功插入，步数只有 1 或 2。出现 3 步即判定模型实现错误。
- 主表溢出比例，对照 \(f(n)\)。
- **主表 STE 利用率** \(u_{\mathrm{main}} = n_{\mathrm{occupied\_main}} / (R \cdot d)\)；实验 B/C 报告插入结束时的实际占用（含行内空槽）。
- **碰撞表 STE 利用率** \(u_{\mathrm{col}}\)（同上，仅统计碰撞 RTC 槽位）。
- 失败比例，对照 \(p_{\mathrm{fail}}(n)\)。
- 行占用直方图对照 \(\mathrm{Poisson}(\lambda)\)，至少给出 0 到 8 的桶。
- 插入结束后，对已插入键做一遍查找（A 为全量 \(10^{7}\)）。查找比较次数不得超过该规则所在行的深度。
- 消融：同一千万级均匀集只开主表、不挂碰撞表，失败条数应等于主表溢出条数。用来说明碰撞表承担的是成功率，步数上界来自两级深度都有限。

均匀集 A 的接受标准：溢出比例与 \(f(10^{7})\) 的相对误差在 5% 以内，失败条数为 0 或与 \(p_{\mathrm{fail}}\) 同一数量级且绝对条数极少。步数断言必须全部成立。重尾集不要求贴住 Poisson，要求步数断言仍然成立，并定量写出失败率相对均匀集的变化。

墙钟时间只描述仿真程序本身，不写成网卡插入速率。

## 报告中的 PNG

1. Context、Table、Matcher、RTC、STE、STC、Definer 的对象关系。
2. RX 流水线，标出 namespace 表链、matcher、TIR、RQ、CQ、EQ。
3. TX 流水线，标出 SQ、TIS、egress matcher。
4. FDB 双 RTC：一条规则的 RX STE 与 TX STE。
5. 插入时序：组 WQE、门铃、主 RTC、满行再写碰撞 RTC、CQ 回收。
6. 查找时序：hash 选行、行内比较、STC 或 miss。
7. 千万级结果：A/B/C 的溢出率、失败率与公式曲线；步数只有 1 和 2 的堆叠条；均匀与重尾的占用直方图。
8. **D 对照**：\(2^{24}\) 行 vs \(2^{22}\) 行的 \(u_{\mathrm{main}}\)、溢出率、2 步占比并列图。
9. **容量权衡示意**：同一 \(n\) 下主表 STE 总数、利用率、失败率的三轴或分面图（说明「空槽多」与「插入更稳」的对应关系）。

图内文字用可读的中文或直接的英文术语（RTC、STE、TIR），避免缩写墙。

## Word 结构

1. 结论。对象关系、步数上界、千万级仿真和公式是否一致。
2. 源码地图。
3. 域、namespace、表、matcher、key、action。表的数量按 `fs_hws.c` 的实际锚点写，不写一个虚构的固定张数。
4. 存储、插入、查找，以及步数上界；**容量、主表 STE 余量、碰撞表角色**（含 \(u_{\mathrm{main}}=25\%\) 在配满点的含义）。
5. RX、TX、FDB 流水线，和 TIR、TIS、RQ、SQ、CQ、EQ 的层次。
6. 数学模型。
7. 千万级实验：几何参数、三种数据集、A/B/C、消融、偏差；**实验 D**（\(2^{22}\) 行对照）。
8. 这套上界在什么负载下还能保住成功率，哈希严重偏斜或碰撞表被占满时哪一条保证先失效；**何时「省 STE」会先于「保插入」成为瓶颈**。

文件名：`mlx5-hws-analysis/mlx5-hws-model.docx`。PNG 放在 `mlx5-hws-analysis/figures/`。

## 交付

- 本计划：`mlx5-hws-analysis/PLAN.md`
- 确认后：仿真源码、CSV、PNG、Word
- 不修改 `drivers/net/ethernet/mellanox/mlx5/`

## 仍按此执行的约定

- 源码以本仓库内核 HWS 加它衔接的 fs 流水线为准。
- Namespace 按动态表链写。
- 仿真是用户态模型，用来核对公式和驱动几何，不是网卡基准。
- 验证规模以本节的千万级三次实插为准。
