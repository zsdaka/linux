#!/usr/bin/env python3
"""Build the mlx5 HWS analysis Word report from the simulation CSV and PNGs."""
import csv
import os
from datetime import datetime

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIG = os.path.join(ROOT, "figures")
CSV = os.path.join(ROOT, "sim", "out", "summary.csv")
OUT = os.path.join(ROOT, "mlx5-hws-model.docx")
FONT = "微软雅黑"


def set_run(run, size=11, bold=False, color=None):
    run.font.size = Pt(size)
    run.bold = bold
    run.font.name = "Calibri"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    if color:
        run.font.color.rgb = RGBColor(*color)


def add_p(doc, text, size=11, bold=False, center=False, space=6):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space)
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.line_spacing = 1.15
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    set_run(run, size=size, bold=bold)
    return p


def add_h(doc, text, level):
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        set_run(run, size={1: 18, 2: 15, 3: 13}[level], bold=True, color=(27, 58, 75))
    return h


def add_fig(doc, name, caption):
    path = os.path.join(FIG, name)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run().add_picture(path, width=Inches(6.35))
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.space_after = Pt(12)
    run = cap.add_run(caption)
    set_run(run, size=9, color=(60, 70, 80))


def shade_header(cell):
    tc = cell._tePr if hasattr(cell, "_tePr") else cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = tcPr.makeelement(qn("w:shd"), {
        qn("w:fill"): "1B3A4B",
        qn("w:val"): "clear",
    })
    tcPr.append(shd)


def add_table(doc, headers, rows):
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = ""
        p = cell.paragraphs[0]
        run = p.add_run(h)
        set_run(run, size=9, bold=True, color=(255, 255, 255))
        shade_header(cell)
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            cell = table.rows[r + 1].cells[c]
            cell.text = ""
            run = cell.paragraphs[0].add_run(str(val))
            set_run(run, size=8)
    doc.add_paragraph()


def load():
    with open(CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    return {(r["exp"], r["data"]): r for r in rows}


def pct(x):
    return f"{float(x) * 100:.4f}%"


def rel(emp, theory):
    theory = float(theory)
    emp = float(emp)
    if theory == 0:
        return "公式为 0"
    return f"{(emp - theory) / theory * 100:+.2f}%"


def main():
    by = load()
    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = Cm(1.8)
    sec.bottom_margin = Cm(1.8)
    sec.left_margin = Cm(1.8)
    sec.right_margin = Cm(1.8)
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)

    add_p(doc, "mlx5 硬件流表（HWS）结构、流水线与插入上界", size=22, bold=True, center=True, space=4)
    add_p(doc, "从本仓库 drivers/net/ethernet/mellanox/mlx5/core/steering/hws 反推", size=12, center=True, space=2)
    add_p(doc, "2026-10-28  Asia/Shanghai", size=11, center=True, space=12)
    add_p(doc, "本文对照的是驱动里的表几何和一份按该几何写成的用户态模型。环境里没有 ConnectX，文中的秒数是模型程序自己的运行时间，不是网卡插入速率。")

    add_h(doc, "1  结论", 1)
    add_p(doc, "HWS 把一张逻辑流表拆成按优先级串起来的 matcher。每个 matcher 有一套匹配模板（key）和若干动作模板（action）。FDB 表会为同一条规则建两套 RTC：RX 服务线侧进入的包，TX 服务 vport 发出的包。大表在主 RTC 后面再挂一张碰撞 RTC，优先级与主 matcher 相同。")
    add_p(doc, "存储单位是 STE。RESOURCE_MODE_RULE 下，调用者给出的 num_log 是哈希行数的对数。num_log = 24 时主表是 2^24 行、每行 4 个 STE，一共 2^26 个槽。碰撞表是 2^19 行、每行 16 个槽。2^24 条 key 均匀放入时，主表大约四分之三的槽是空的。这些空槽用来吸收同一行上的哈希起伏，使溢出比例停在千分之几。")
    add_p(doc, "插入由专用发送队列上的 GTA WQE 完成，opcode 为 0x2c。硬件只在哈希选中的那一行里找空 STE。行满时软件再向碰撞 RTC 发一条 WQE。成功路径的 WQE 数只有 1 或 2，与表中已有规则数无关。千万条每秒这个量级要靠设备把这种定长 WQE 在多条队列上打满；规则变多不会把单次插入变成随表长增长的搜索。")
    a = by[("A", "uniform")]
    b = by[("B", "uniform")]
    d = by[("Dtight", "uniform")]
    add_p(doc, f"均匀哈希、1,000 万条、驱动几何：溢出 {pct(a['over_frac'])}，公式 {pct(a['theory_over'])}，相对误差 {rel(a['over_frac'], a['theory_over'])}，失败 0。配满 2^24 条时溢出 {pct(b['over_frac'])}，公式 {pct(b['theory_over'])}，相对误差 {rel(b['over_frac'], b['theory_over'])}，失败仍为 0。把主表收成 2^22 行后再放入同样的 2^24 条 key，失败率 {pct(d['fail_frac'])}，公式 {pct(d['theory_fail'])}。模型与驱动几何在均匀负载上对得上；把行数按“槽位总数等于 key 数”收紧之后，失败率会离开设计点。")

    add_h(doc, "2  源码地图", 1)
    add_p(doc, "分析只读本仓库，不改驱动。HWS 入口在 steering/hws。与 fs_core 的衔接在 fs_hws.c：只有 table_type 为 FS_FT_FDB 的 namespace 会走 mlx5hws_table_create，其它 namespace 仍走固件流表命令。")
    add_table(doc,
              ["对象", "文件", "在硬件侧对应什么"],
              [
                  ["context", "context.c", "每设备一个上下文：STC 池、definer 缓存、发送队列"],
                  ["table", "table.c", "一张 FDB flow table，带 level 和默认 miss"],
                  ["matcher", "matcher.c", "一张 RTC 哈希表，按 priority 串进表"],
                  ["definer", "definer.c", "从 match criteria 选出要写入 STE tag 的字段"],
                  ["action / STC", "action.c pat_arg.c", "改头、计数、vport、跳表等动作"],
                  ["rule", "rule.c", "一条规则：tag、动作、rtc_0 / rtc_1"],
                  ["send", "send.c", "GTA WQE、门铃、CQ 完成；满行则重试碰撞 RTC"],
                  ["bwc", "bwc.c", "按规则数扩行，70% 水位触发 rehash"],
                  ["fs 衔接", "fs_hws.c", "flow group 变成 bwc matcher，目的地变成 action"],
              ])

    add_h(doc, "3  域、namespace、表、key 与 action", 1)
    add_h(doc, "3.1  域", 2)
    add_p(doc, "本树公开的 HWS 表类型只有 MLX5HWS_TABLE_TYPE_FDB。创建表时若 type 大于 FDB 会直接拒绝。FDB 在固件侧仍拆成两个资源类型：FS_FT_FDB_RX 与 FS_FT_FDB_TX。hws_matcher_create_rtc() 在表类型为 FDB 时创建第二套 RTC，STE 区间分别记在 ste_0_base 与 ste_1_base。")
    add_p(doc, "context 初始化还要求 eswitch manager、WQE 更新、8DW STE、按哈希和按偏移两种 RTC 更新模式，以及 SELECT definer。不满足这些能力时，上下文不会置上 HWS_SUPPORT。")
    add_h(doc, "3.2  namespace 里有几张表", 2)
    add_p(doc, "mlx5_flow_namespace_type 列出了 BYPASS、KERNEL、FDB、EGRESS、RDMA、IPsec、MACsec 等二十余个 namespace。HWS 创建路径只接受 FS_FT_FDB。因此在这棵树里，HWS 流表全部挂在 FDB namespace 上，其它 namespace 没有 HWS matcher。")
    add_p(doc, "FDB namespace 内部的表数随 offload 变化，不是一个写死的整数。fs.h 里的优先级梯子是：FDB_BYPASS_PATH、FDB_CRYPTO_INGRESS、FDB_TC_OFFLOAD、FDB_FT_OFFLOAD、FDB_TC_MISS、FDB_BR_OFFLOAD、FDB_SLOW_PATH、FDB_CRYPTO_EGRESS、FDB_PER_VPORT。esw_create_offloads_fdb_tables() 至少创建两张表：慢路径表（优先级 FDB_SLOW_PATH）和 TC miss 表（优先级 FDB_TC_MISS）。TC chain 在运行时继续按 level 加表。每张 mlx5_flow_table 对应一张 mlx5hws_table。")
    add_p(doc, "一张 HWS 表里的 matcher 数等于这张表上的 flow group 数，再加每个大 matcher 的一张碰撞表。碰撞 matcher 使用与主 matcher 相同的 priority，创建时插到主 matcher 后面，查找顺序是先主 RTC、再碰撞 RTC。")
    add_h(doc, "3.3  每张表的 key 和 action", 2)
    add_p(doc, "key 不是全 FDB 共用的一张五元组。fs_hws 把每个 flow group 的 match_criteria 交给 bwc matcher，definer 再把置位字段收成 STE tag。普通 tag 32 字节，jumbo tag 44 字节。不同 group 的 key 不同。慢路径表上驱动写死的几张 group 是：")
    add_table(doc,
              ["flow group", "key", "action"],
              [
                  ["send-to-vport", "source_sqn，必要时再加源 eswitch 的 vhca id", "转到对应 vport"],
                  ["meta send-to-vport", "metadata_reg_c_0 的 vport 元数据，以及 reg_c_1 的隧道位", "转到 vport"],
                  ["miss", "外层 DMAC 的组播位（dmac[0] = 0x01）", "单播和组播 miss 都转到管理 vport"],
              ])
    add_p(doc, "业务规则（TC、桥、封装）的 key 由当时的 flow spec 决定，常见字段来自 definer 能选的外层/内层地址、端口、VLAN、隧道和寄存器。action 来自动作模板，类型包括丢弃、跳表、计数、tag、改头、vport、压弹 VLAN、封装与解封装、插入或删除头、meter、采样和目的地数组。终结到 vport 的动作在 fs_hws.c 里通过 mlx5hws_action_create_dest_vport 创建。")
    add_p(doc, "TIR 出现在 fs 的通用目的地结构里（mlx5_flow_destination.tir_num），供 NIC RX 的终结表使用。HWS 动作枚举里没有 TIR。FDB 命中之后的典型终结是 vport 或丢弃；报文到达代表口之后，才进入 NIC RX，由 TIR 选择 RQ。")

    add_h(doc, "4  存储、插入、查找", 1)
    add_fig(doc, "01_objects.png", "图 1  context、table、matcher、RTC、STE、STC 的关系")
    add_h(doc, "4.1  怎么存", 2)
    add_p(doc, "RTC 的 log_hash_size 来自 sz_row_log，log_depth 来自 sz_col_log。RESOURCE_MODE_RULE 且按哈希插入时，这两个字段共用 union 的第一个字节：调用者写入 rule.num_log，驱动只把第二个字节写成深度。大表（num_log > 10）的深度固定为 MLX5HWS_MATCHER_ASSURED_MAIN_TBL_DEPTH，也就是 2，每行 4 个 STE。STE 池的对数大小是行对数加深对数。")
    add_p(doc, "碰撞表在 hws_matcher_create_col_matcher() 里生成。行对数等于 num_log 减 MLX5HWS_MATCHER_ASSURED_ROW_RATIO（5），深度对数为 4，每行 16 个 STE。注释里的 3% 来自 log2(0.03) ≈ 5：碰撞行数约为主表行数的 1/32。")
    add_fig(doc, "07_geometry.png", "图 2  2^24 条 key 的两种行数。左边是驱动，右边是槽位数等于 key 数的排法")
    add_h(doc, "4.2  怎么插入，时间上界从哪来", 2)
    add_fig(doc, "05_insert.png", "图 3  插入路径。满行只再发一条碰撞 WQE")
    add_p(doc, "rule.c 把主 RTC 填进 rtc_0 / rtc_1，把碰撞 RTC 填进 retry_rtc_0 / retry_rtc_1。flow_source 为 WIRE 或 VPORT 时可以跳过另一方向。send.c 在 CQE 报失败且 retry_id 非 0 时，把同一条规则再投递到碰撞 RTC。碰撞 RTC 再失败，规则状态变为失败，不再发第三条 WQE。")
    add_p(doc, "因此一次成功插入的设备操作是 1 次或 2 次定长 WQE（控制段 48 字节，数据段 64 字节）。比较次数被行深卡住：主行 4，碰撞行 16。这个上界不随 n 增长。BWC 在规则数达到 2^size_log 的 70% 时把行数加一档，扩的是行，不是把深度吃满。热路径按队列加锁，队列之间可以并行提交。")
    add_h(doc, "4.3  怎么查找", 2)
    add_fig(doc, "06_lookup.png", "图 4  报文查找。碰撞表在同一优先级上接在主表后面")
    add_p(doc, "hws_matcher_connect() 按 priority 把 matcher 串起来。前一张的 end FT 指向下一张的 RTC；最后一张指向 miss 表。碰撞 matcher 与主 matcher 优先级相等，插在后面，所以主 RTC 未命中会进入碰撞 RTC。命中后执行 STE 里指向的 STC。整条 matcher 链都未命中时，FDB 默认 miss 把报文转到 eswitch manager vport。")

    add_h(doc, "5  流水线，以及和 TIR、TIS、RQ、SQ、CQ、EQ 的层次", 1)
    add_fig(doc, "02_rx_pipeline.png", "图 5  RX。HWS 流表在 FDB，TIR 和 RQ 在随后的 NIC RX")
    add_fig(doc, "03_tx_pipeline.png", "图 6  TX。数据包先到 SQ 和 TIS，再进 FDB 的 TX RTC")
    add_fig(doc, "04_fdb_dual_rtc.png", "图 7  一条 FDB 规则写入 RX、TX 两套 STE，各自带碰撞 RTC")
    add_p(doc, "从报文方向看，层次是：端口或 vport，然后是 namespace 上的 flow table 链，然后是 matcher 的 RTC，然后是 STC。FDB 的 STC 把报文送到 vport 或丢掉。代表口上的 NIC RX 再用 TIR 选定 RQ。RQ 完成写进 CQ，CQ 的通知到达 EQ。")
    add_p(doc, "TIS 是发送侧的传输接口。主机把数据包 WQE 放进 SQ 并敲门铃之后，报文带着 TIS 进入 TX 方向，FDB 用 TX RTC 做哈希。")
    add_p(doc, "规则下发不走数据包 RQ/SQ。context 按队列数打开一组 HWS SQ/CQ，上面只跑 GTA WQE。完成由驱动轮询这条 CQ 取回；EQ 处于通知层，插入快路径不依赖 EQ 中断。所以图里有两条队列：一条搬包，一条搬 STE。")

    add_h(doc, "6  数学模型", 1)
    add_p(doc, "记主表行数 R = 2^r，每行容量 d = 4。碰撞表行数 Rc = R / 32，每行容量 dc = 16。插入 n 条互异规则，行哈希均匀时，每行到达数 X 服从 Poisson(λ)，λ = n / R。R 在实验里是 2^24 或 2^22，泊松近似足够。")
    add_p(doc, "主表放不下的比例 f(n) = E[max(X − d, 0)] / λ。这些规则再经独立哈希进入碰撞表，λc = n · f(n) / Rc，Y ~ Poisson(λc)。失败比例 p_fail(n) = f(n) · E[max(Y − dc, 0)] / λc。λc 为 0 时失败比例为 0。")
    add_p(doc, "主表槽位利用率 u_main = 主表实际占用槽数 / (4R)。设计点 n = R 时，若溢出可以忽略，u_main 约为 25%。成功插入的 GTA 步数 S 属于 {1, 2}：主行未满为 1，否则为 2。查找比较次数不超过 d，落到碰撞行时不超过 d + dc。")
    add_p(doc, "单队列规则速率可以写成 R_wqe / (1 + f(n))，其中 R_wqe 是该队列每秒能完成的 GTA WQE 数，Q 条队列近似乘 Q。f(n) 在驱动几何的配满点约为 0.44%，第二步带来的额外 WQE 不到百分之一。本文没有测量 R_wqe。上界成立时，表从空涨到 2^24 条不会把单次插入从常数次 WQE 变成线性扫描。")

    add_h(doc, "7  千万级验证", 1)
    add_p(doc, "程序是 mlx5-hws-analysis/sim/hws_sim.c。主表和碰撞表都按槽位数组逐条写入 64 位指纹。空槽为 0。插入之后按同一哈希把每条 key 再查一遍：成功的必须命中，失败的必须查不到，比较次数不得超过 4 + 16。全部实验的 lookup_bad 都是 0。")
    add_p(doc, "驱动几何固定为 num_log = 24：主表 2^24 行 × 4，碰撞表 2^19 行 × 16。三组规模是 10,000,000、16,777,216 和 20,000,000。D-tight 把行对数改成 22，碰撞行对数为 17，插入条数仍是 16,777,216。消融实验去掉碰撞表。")
    add_h(doc, "7.1  数据集", 2)
    add_p(doc, "均匀集：每条规则一个互异序号，经 64 位混合后取低位作为行号。另用第二套混合常数复跑 1,000 万条，用来看结论是否粘在某一个混合函数上。")
    add_p(doc, "重尾有两套。zipf_mixed：源组服从 Zipf，指数 1.2，组数 2^20，再与唯一序号一起混合后取行。这代表五元组字段都进了哈希，重尾被打散。zipf_coarse：行号只由 Zipf 源组决定，碰撞行仍用唯一序号的独立哈希。这代表 matcher 的 key 很短，或者哈希没有把热点字段打散。")
    add_p(doc, "真实数据用 CTU-13 的 capture20110810.binetflow（捷克理工大学 MCFP 公开的 botnet 流记录，2015-01-30 发布的文件）。原始 2,824,636 行，去掉非 IPv4 之后得到 1,602,033 个互异五元组指纹。全量作为 TraceFull。不足 1,000 万条，因此再按该文件里源地址、目的地址、协议、源端口、目的端口的经验频率独立采样，生成 1,000 万个互异组合，记为 ctu_resample。采样组合是经验分布的扩展，逐条列出的部分才是原始流。")
    add_h(doc, "7.2  结果", 2)
    add_fig(doc, "08_overflow_fail.png", "图 8  溢出率与失败率。左图把 D 拿开，否则 19% 会把千分之几的柱子压没")
    add_fig(doc, "09_steps.png", "图 9  每条规则的 WQE 步数。绿色是 1 步，橙色是 2 步，红色是失败")
    add_fig(doc, "10_hist_uniform.png", "图 10  均匀哈希的行到达直方图与 Poisson")
    add_fig(doc, "12_d_compare.png", "图 11  同一批 2^24 条均匀 key，驱动行数与 2^22 行的对照")
    add_fig(doc, "13_capacity.png", "图 12  利用率、失败率随 λ 的变化。紧凑几何的点单独标出")
    add_fig(doc, "11_hist_zipf.png", "图 13  粗哈希重尾下，大多数行是空的")

    headers = ["实验", "n", "溢出", "公式", "相对误差", "失败", "失败公式", "u_main", "最大比较"]
    body = []
    order = [
        ("A", "uniform", "A 均匀"),
        ("B", "uniform", "B 配满 2^24"),
        ("C", "uniform", "C 2,000 万"),
        ("A", "uniform_alt", "A 另一哈希"),
        ("A", "zipf_mixed", "A 重尾全字段哈希"),
        ("A", "ctu_resample", "A CTU 采样 1,000 万"),
        ("TraceFull", "ctu", "CTU 全量 1,602,033"),
        ("Ablation", "uniform", "消融，无碰撞表"),
        ("Dtight", "uniform", "D-tight 2^22 行"),
        ("A", "zipf_coarse", "A 粗哈希重尾"),
        ("B", "zipf_coarse", "B 粗哈希重尾"),
        ("C", "zipf_coarse", "C 粗哈希重尾"),
    ]
    for exp, data, name in order:
        r = by[(exp, data)]
        body.append([
            name,
            f"{int(r['n']):,}",
            pct(r["over_frac"]),
            pct(r["theory_over"]),
            rel(r["over_frac"], r["theory_over"]),
            pct(r["fail_frac"]),
            pct(r["theory_fail"]),
            pct(r["u_main"]),
            r["lookup_max_cmp"],
        ])
    add_table(doc, headers, body)
    add_p(doc, "表 1  全部实插。u_main 是主表已占用槽除以主表槽位总数。公式失败率为 0 的格子，数值小于 10^-8，打印成 0.0000%。CTU 全量的期望溢出条数约 1 条，仿真为 1 条。", size=9)

    add_p(doc, f"均匀 A 的溢出相对误差 {rel(a['over_frac'], a['theory_over'])}，落在计划要求的 5% 以内。配满点和 2,000 万条同样贴住公式，失败条数都是 0。第二套哈希的溢出是 {pct(by[('A','uniform_alt')]['over_frac'])}，与第一套同一量级。CTU 采样的 1,000 万条溢出 {pct(by[('A','ctu_resample')]['over_frac'])}，相对误差 {rel(by[('A','ctu_resample')]['over_frac'], by[('A','ctu_resample')]['theory_over'])}。真实五元组经过与均匀实验相同的混合之后，行占用回到 Poisson。")
    ab = by[("Ablation", "uniform")]
    add_p(doc, f"消融实验的失败条数 {int(ab['fail']):,}，等于均匀 A 的溢出条数 {int(a['overflow']):,}。碰撞表在驱动几何下承担的是这几千条尾部规则的成功率。步数上界则来自两级深度都有限：即使粗哈希把 1,000 万条里的 {pct(by[('A','zipf_coarse')]['fail_frac'])} 打成失败，成功的那些仍然只走了 1 步或 2 步，查找最大比较次数是 20，也就是 4 + 16。")
    add_p(doc, f"D-tight 的主表利用率 {pct(d['u_main'])}，溢出 {pct(d['over_frac'])}（公式 {pct(d['theory_over'])}），失败 {pct(d['fail_frac'])}（公式 {pct(d['theory_fail'])}）。槽位总数等于 key 数时，哈希方差仍让约 19.5% 的 key 无法留在自己的行里，其中约 7.1% 连深度 16 的碰撞行也放不下。驱动几何在同一批 key 上的主表利用率是 {pct(b['u_main'])}，失败数是 0。多出来的行把 λ 从 4 降到 1。")
    z = by[("A", "zipf_coarse")]
    zm = by[("A", "zipf_mixed")]
    add_p(doc, f"重尾全字段哈希的溢出 {pct(zm['over_frac'])}，与均匀 A 一致，失败为 0。粗哈希下 1,000 万条的主表利用率只有 {pct(z['u_main'])}，因为绝大多数行是空的，热点行立刻溢出；碰撞表利用率 {pct(z['u_col'])}，失败 {int(z['fail']):,} 条。配满和 2,000 万条时碰撞表利用率接近 100%，失败比例升到 {pct(by[('B','zipf_coarse')]['fail_frac'])} 和 {pct(by[('C','zipf_coarse')]['fail_frac'])}。")

    add_h(doc, "8  机制在什么范围内站得住", 1)
    add_p(doc, "均匀哈希、以及能把五元组打散的哈希，在 num_log = 24 的表上把 2,000 万条放完仍然失败 0 条。第二步 WQE 的比例从 1,000 万条时的 0.07% 升到配满时的 0.44%，再升到 2,000 万条时的 0.78%。插入代价保持为一次或两次定长设备操作。这就是“表已经有千万条，再插一条仍然是常数时间”的硬件原因：哈希只选一行，行深是常数，满了还有一张行深同样有常数上界的副表。")
    add_p(doc, "主表 75% 左右的空槽是这个常数上界的代价。BWC 还在 70% 行水位就扩行，所以线上常用水位比 λ = 1 更松，STE 相对规则数的余量比配满点更大。")
    add_p(doc, "上界先失效的方式有两种。一是碰撞行也被占满，插入从“最多两次 WQE”变成失败，粗哈希重尾和 D-tight 都走到了这里。二是 key 本身很短，许多互异规则落在同一行，副表接不住热点。步数上界对成功的插入仍然成立，先丢的是成功率。")
    add_p(doc, "模型没有覆盖的部分：设备内部哈希多项式的具体系数、GTA 引擎每秒能退休多少 WQE、以及 STC 动作链本身的延迟。那些决定 R_wqe 的具体数值。本文证明的是驱动选的行数和深度，在均匀与真实五元组哈希下，把溢出和失败控制在公式给出的位置，并且把单次插入限制在两条 WQE 以内。")

    add_h(doc, "9  复现", 1)
    add_p(doc, "在 mlx5-hws-analysis/sim 下执行 make，然后 ./hws_sim out。均匀、重尾、D-tight 和消融不需要外部文件。CTU 原始文件与采样文件通过第 3、第 4 个参数传入。画图使用 draw_figures.py，本文由 build_docx.py 生成。")
    add_p(doc, "仿真输出在 sim/out/summary.csv 与 hist.csv。各次运行的墙钟时间在 0.5 秒到 8 秒之间，包含插入和全量查找，只说明这个用户态模型在单线程下的速度。")

    doc.save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
