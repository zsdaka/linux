#!/usr/bin/env python3
"""Architecture diagrams and result charts for the mlx5 HWS model."""
import csv
import os
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

FONT = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
font_manager.fontManager.addfont(FONT)
plt.rcParams["font.family"] = "WenQuanYi Micro Hei"
plt.rcParams["axes.unicode_minus"] = False

NAVY = "#1B3A4B"
TEAL = "#1F7A8C"
SAND = "#F6E7C1"
MIST = "#E7F0F4"
CORAL = "#C4493A"
GREEN = "#2F6F4E"
ORANGE = "#D4890B"
WHITE = "#FFFFFF"
INK = "#14242E"

OUT = os.path.join(os.path.dirname(__file__), "..", "figures")
SIM = os.path.join(os.path.dirname(__file__), "out")


def box(ax, x, y, w, h, text, fc, fs=10, ec=NAVY, tc=INK, lw=1.2):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.012,rounding_size=0.08",
        facecolor=fc, edgecolor=ec, linewidth=lw, mutation_aspect=0.5,
    ))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, color=tc, linespacing=1.25)


def arrow(ax, x1, y1, x2, y2, color=NAVY):
    ax.add_patch(FancyArrowPatch(
        (x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=12,
        color=color, linewidth=1.3,
    ))


def new_ax(w, h):
    fig, ax = plt.subplots(figsize=(w, h), dpi=140)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    fig.patch.set_facecolor(WHITE)
    return fig, ax


def save(fig, name):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    fig.savefig(path, bbox_inches="tight", facecolor=WHITE)
    plt.close(fig)
    print(path)


def fig_objects():
    fig, ax = new_ax(13.2, 7.6)
    ax.set_title("HWS 对象关系（本仓库 steering/hws）", fontsize=16, color=NAVY, pad=8)
    box(ax, 3, 78, 94, 18, "", MIST, ec=TEAL, lw=1.6)
    ax.text(6, 92, "mlx5hws_context", fontsize=12, color=TEAL, fontweight="bold")
    box(ax, 6, 81, 28, 8, "STC pool\n动作模板的硬件动作", SAND, fs=9)
    box(ax, 36, 81, 28, 8, "send queue × Q\n每条队列一对 SQ + CQ", SAND, fs=9)
    box(ax, 66, 81, 28, 8, "definer / pattern cache", SAND, fs=9)

    box(ax, 3, 40, 94, 34, "", "#F8FBFC", ec=NAVY, lw=1.4)
    ax.text(6, 70, "mlx5hws_table    类型 FDB，带 level", fontsize=12, color=NAVY, fontweight="bold")
    box(ax, 6, 58, 28, 9, "flow table 对象\nft_id，默认 miss", MIST, fs=9)
    box(ax, 36, 58, 28, 9, "matcher 按 priority 串接\nmiss 进入下一张", MIST, fs=9)
    box(ax, 66, 58, 28, 9, "default miss\n交给管理 vport", MIST, fs=9)

    box(ax, 6, 43, 42, 12, "matcher\nmatch template = key\naction template = 动作种类", "#D9EDE8", fs=9)
    box(ax, 52, 43, 42, 12, "碰撞 matcher\n同一 priority，挂在主 matcher 之后\n行数 = 主表 / 32，深度 16", "#F8D7D2", fs=9)

    box(ax, 6, 8, 20, 26, "RTC RX\nlog_hash_size = 行\nlog_depth = 2\n每行 4 个 STE", "#D6E4F0", fs=9)
    box(ax, 28, 8, 20, 26, "RTC TX\nFDB 的镜像\nFS_FT_FDB_TX\n同一条规则的第二份 STE", "#D6E4F0", fs=9)
    box(ax, 50, 8, 20, 26, "STE 区间\n大小 = 2^(行对数+深度对数)\n8DW tag 32B\njumbo tag 44B", SAND, fs=9)
    box(ax, 72, 8, 22, 26, "STC\n改头、计数、丢弃\nvport、跳表、封装\n动作参数在 pat/arg 池", "#E7F6D9", fs=9)
    arrow(ax, 27, 49, 16, 34)
    arrow(ax, 27, 49, 38, 34)
    arrow(ax, 73, 43, 83, 34)
    save(fig, "01_objects.png")


def fig_domains():
    fig, ax = new_ax(14.4, 8.4)
    ax.set_title("三个以太网域并列。HWS 在本树里只打开 FDB", fontsize=16, color=NAVY, pad=8)
    cols = [
        (3, "#D6E4F0", "NIC_RX",
         "FS_FT_NIC_RX = 0\n根：steering->root_ns\n本树命令：固件 DMFS",
         "namespace\nBYPASS、RX MACSEC\nLAG、OFFLOADS\nETHTOOL、KERNEL\nLEFTOVERS、ANCHOR",
         "只建 RX 半边\n终结可以是 TIR\nTIR 再选 RQ"),
        (35, "#F6E7C1", "NIC_TX",
         "FS_FT_NIC_TX = 1\n根：egress_root_ns\n本树命令：固件 DMFS",
         "namespace\nEGRESS\nEGRESS_IPSEC\nEGRESS_MACSEC",
         "只建 TX 半边\n封装方向是 L2→隧道\n根由 SET_FLOW_TABLE_ROOT\ntable_type = NIC_TX 挂上"),
        (67, "#D9EDE8", "FDB",
         "FS_FT_FDB = 4\n根：fdb_root_ns\n本树可切 dmfs / smfs / hmfs",
         "namespace\nFDB 根、FDB_BYPASS\n以及运行时的 TC chain",
         "RX、TX 两套 STE\nHWS 时是 rtc_0 / rtc_1\n动作是 vport、丢弃、跳表"),
    ]
    for x, color, title, head, mid, tail in cols:
        box(ax, x, 18, 30, 70, "", color, ec=NAVY, lw=1.4)
        ax.text(x + 15, 82, title, ha="center", fontsize=13, color=NAVY, fontweight="bold")
        ax.text(x + 15, 68, head, ha="center", va="center", fontsize=8.5, color=INK)
        ax.text(x + 15, 48, mid, ha="center", va="center", fontsize=8.5, color=INK)
        ax.text(x + 15, 28, tail, ha="center", va="center", fontsize=8.5, color=INK)
    ax.text(50, 8,
            "smfs / hmfs 的模式切换只接受 FS_FT_FDB。NIC_RX 与 NIC_TX 的根留在固件流表上。",
            ha="center", fontsize=10, color=TEAL)
    save(fig, "21_domains.png")


def fig_legacy():
    fig, ax = new_ax(14.2, 8.0)
    ax.set_title("legacy：FDB 做 L2 转发，每个 function 用自己的 NIC 域", fontsize=15, color=NAVY, pad=6)
    box(ax, 3, 58, 30, 32, "FDB 域\n固件 DMFS，不切 hmfs\n\nchain 0，prio 1\nDMAC → vport\n组播位 → vport\nuplink 混杂 → vport\n\n打开 VEPA 才有 prio 0", "#D9EDE8", fs=8.5)
    box(ax, 35, 58, 30, 32, "vport ACL\n非管理 vport 才建\n\ningress：SMAC、CVLAN\n不合法就丢\n\negress：指定 VLAN 放行\n其余丢弃", SAND, fs=8.5)
    box(ax, 67, 58, 30, 32, "NIC_RX / NIC_TX\nPF 和每个 VF 各一套\n\nKERNEL 的 L2 和 TTC\n目的地是本 function 的 TIR\n\nVF 的表在客户机驱动里建", "#D6E4F0", fs=8.5)
    stages = [
        (3, "线侧"),
        (22, "FDB\n按 DMAC 选 vport"),
        (41, "VF 的 NIC_RX"),
        (60, "TIR"),
        (79, "RQ"),
    ]
    for x, t in stages:
        box(ax, x, 28, 16, 16, t, MIST, fs=8.5)
    for x in (19, 38, 57, 76):
        arrow(ax, x, 36, x + 3, 36)
    ax.text(50, 14, "VF 发出的包先过该 VF 的 NIC_TX，再回到这张 FDB，按目的 MAC 转到 uplink 或另一个 vport。",
            ha="center", fontsize=9, color=INK)
    ax.text(50, 6, "esw_legacy_enable() 建表。mlx5_flow_namespace_set_mode() 不会被调用。",
            ha="center", fontsize=9, color=TEAL)
    save(fig, "22_legacy.png")


def fig_switchdev():
    fig, ax = new_ax(14.2, 8.2)
    ax.set_title("switchdev：FDB 可切到 HWS，代表口从 NIC_RX 的 OFFLOADS 表收包", fontsize=14, color=NAVY, pad=6)
    box(ax, 3, 56, 30, 34, "FDB 域\n可切 dmfs / smfs / hmfs\n\n慢路径：source_sqn、reg_c0\nTC miss 表\nTC / FT chain 运行时加表\n动作为 vport、封装、跳表", "#D9EDE8", fs=8.5)
    box(ax, 35, 56, 30, 34, "NIC_RX 的 OFFLOADS\n只有 switchdev 建\n\nft_offloads：源 vport\n或 reg_c0 → 代表口\n表尾一条丢弃\nrestore：reg_c1 拷到 reg_b", SAND, fs=8.5)
    box(ax, 67, 56, 30, 34, "代表口的 NIC_RX / NIC_TX\n\nKERNEL 里仍有 TTC → TIR\n上行口另有一张空 root\nmiss 接到 ethtool / TTC\n\ningress ACL 写 reg_c0", "#D6E4F0", fs=8.5)
    stages = [
        (2, "线侧"),
        (18, "FDB 域"),
        (34, "vport"),
        (50, "OFFLOADS\n表"),
        (66, "代表口\nTTC"),
        (82, "TIR → RQ"),
    ]
    for x, t in stages:
        box(ax, x, 26, 14, 16, t, MIST, fs=8)
    for x in (16, 32, 48, 64, 80):
        arrow(ax, x, 34, x + 2, 34)
    ax.text(50, 12, "发送方向先过代表口的 NIC_TX，再进 FDB 的 TX 侧。慢路径和 TC miss 在 steering_init 里创建，TC chain 的表在运行时加上。",
            ha="center", fontsize=9, color=INK)
    ax.text(50, 5, "hmfs / smfs 只在这条路径上打开，而且只打开 FDB 域。",
            ha="center", fontsize=9, color=TEAL)
    save(fig, "23_switchdev.png")


def fig_rx():
    fig, ax = new_ax(13.4, 7.2)
    ax.set_title("RX：先过 FDB 域，再进该 function 的 NIC_RX 域，然后才到 TIR", fontsize=15, color=NAVY, pad=8)
    stages = [
        (2, "线侧 / 上行口", SAND),
        (18, "FDB 域\n可切到 HWS\n走 RTC RX", MIST),
        (34, "目的 vport", "#D6E4F0"),
        (50, "NIC_RX 域\n固件 DMFS", SAND),
        (66, "TIR", "#E7F6D9"),
        (82, "RQ → CQ → EQ", "#F8D7D2"),
    ]
    for x, t, c in stages:
        box(ax, x, 62, 14, 22, t, c, fs=9)
    for x in (16, 32, 48, 64, 80):
        arrow(ax, x, 73, x + 2, 73)
    ax.text(50, 54, "没有 FDB offload 时，线侧直接进入 NIC_RX 域。TIR 是 NIC_RX 的终结对象。",
            ha="center", fontsize=9, color=INK)
    box(ax, 6, 18, 88, 28, "", "#F8FBFC", ec=TEAL)
    ax.text(10, 40, "规则下发走另一套队列，不经过数据包 RQ", fontsize=11, color=TEAL, fontweight="bold")
    box(ax, 10, 22, 24, 14, "主机组 GTA WQE\nopcode 0x2c", SAND, fs=9)
    box(ax, 38, 22, 24, 14, "HWS SQ 门铃\n写入 RTC", MIST, fs=9)
    box(ax, 66, 22, 24, 14, "HWS CQ 轮询完成\nEQ 只做通知", "#E7F6D9", fs=9)
    arrow(ax, 34, 29, 38, 29)
    arrow(ax, 62, 29, 66, 29)
    save(fig, "02_rx_pipeline.png")


def fig_tx():
    fig, ax = new_ax(13.4, 6.6)
    ax.set_title("TX：SQ / TIS 之后先过 NIC_TX 域，再进 FDB 域的 TX 侧", fontsize=15, color=NAVY, pad=8)
    stages = [
        (2, "主机数据包\nSQ 门铃", SAND),
        (18, "TIS\n发送侧传输接口", "#F8D7D2"),
        (34, "NIC_TX 域\n固件 DMFS\nIPsec / MACsec", SAND),
        (50, "FDB 域 TX\n可切到 HWS\nRTC TX", MIST),
        (66, "STC\n改头 / 封装 / 计数", "#E7F6D9"),
        (82, "上行口发出\n或丢弃", "#D6E4F0"),
    ]
    for x, t, c in stages:
        box(ax, x, 48, 14, 28, t, c, fs=8.5)
    for x in (16, 32, 48, 64, 80):
        arrow(ax, x, 62, x + 2, 62)
    ax.text(50, 30,
            "没有 FDB offload 时，NIC_TX 之后直接出端口。FDB 规则默认写 RX、TX 两套 STE。",
            ha="center", fontsize=9, color=INK)
    ax.text(50, 16, "数据包 SQ 与 HWS 规则 SQ 是两套队列。前者搬报文，后者搬 STE。",
            ha="center", fontsize=11, color=TEAL)
    save(fig, "03_tx_pipeline.png")


def fig_fdb():
    fig, ax = new_ax(12.6, 7.0)
    ax.set_title("一条 FDB 规则的两套 RTC", fontsize=16, color=NAVY, pad=8)
    box(ax, 32, 78, 36, 14, "软件里的一条 rule\nmatch tag + action template", SAND, fs=11)
    arrow(ax, 40, 78, 22, 66)
    arrow(ax, 60, 78, 78, 66)
    box(ax, 6, 40, 36, 26, "rtc_0\nFS_FT_FDB_RX\n线侧进来的包\nSTE base = ste_0", "#D6E4F0", fs=11)
    box(ax, 58, 40, 36, 26, "rtc_1\nFS_FT_FDB_TX\nvport 出来的包\nSTE base = ste_1", "#D9EDE8", fs=11)
    box(ax, 6, 8, 36, 24, "主行满\nretry_rtc_0\n碰撞表 RX", "#F8D7D2", fs=11)
    box(ax, 58, 8, 36, 24, "主行满\nretry_rtc_1\n碰撞表 TX", "#F8D7D2", fs=11)
    arrow(ax, 24, 40, 24, 32)
    arrow(ax, 76, 40, 76, 32)
    save(fig, "04_fdb_dual_rtc.png")


def fig_insert():
    fig, ax = new_ax(13.2, 7.4)
    ax.set_title("插入：固定 1 次或 2 次 GTA WQE", fontsize=16, color=NAVY, pad=8)
    steps = [
        (4, 70, "1. definer 生成 tag\n普通 32 字节"),
        (28, 70, "2. 填写 GTA WQE\nctrl 48B + data 64B"),
        (52, 70, "3. 写入 HWS SQ\n敲 UAR 门铃"),
        (76, 70, "4. 硬件按哈希\n在主行 4 槽里找空位"),
    ]
    for x, y, t in steps:
        box(ax, x, y, 20, 18, t, MIST, fs=9)
    arrow(ax, 24, 79, 28, 79)
    arrow(ax, 48, 79, 52, 79)
    arrow(ax, 72, 79, 76, 79)
    box(ax, 8, 36, 36, 22, "主行有空 STE\n完成状态成功\n步数 = 1", "#E7F6D9", fs=11)
    box(ax, 56, 36, 36, 22, "主行已满，CQE 失败\n软件向碰撞 RTC 再发 1 条 WQE\n步数 = 2", "#F8D7D2", fs=11)
    arrow(ax, 30, 70, 26, 58)
    arrow(ax, 80, 70, 74, 58)
    box(ax, 18, 8, 64, 18, "碰撞行也满：规则失败。成功路径不会出现第 3 条 WQE。\n步数上界与表里已经有多少条规则无关。", SAND, fs=11)
    save(fig, "05_insert.png")


def fig_lookup():
    fig, ax = new_ax(13.2, 7.2)
    ax.set_title("查找：主行最多比 4 次，碰撞行最多再比 16 次", fontsize=16, color=NAVY, pad=8)
    box(ax, 4, 62, 18, 22, "报文进入\nflow table", SAND, fs=10)
    box(ax, 28, 62, 22, 22, "主 RTC\n哈希选 1 行\n最多 4 个 STE", "#D6E4F0", fs=10)
    box(ax, 56, 62, 18, 22, "tag 命中\n执行 STC", "#E7F6D9", fs=10)
    box(ax, 80, 62, 16, 22, "vport / 丢弃\n/ 下一张表", MIST, fs=10)
    arrow(ax, 22, 73, 28, 73)
    arrow(ax, 50, 73, 56, 73)
    arrow(ax, 74, 73, 80, 73)
    box(ax, 28, 28, 22, 22, "主行未命中\n跳到 end FT", SAND, fs=10)
    box(ax, 56, 28, 22, 22, "碰撞 RTC\n另一套哈希\n最多 16 个 STE", "#F8D7D2", fs=10)
    box(ax, 80, 28, 16, 22, "仍未命中\n下一 priority\n或 default miss", MIST, fs=9)
    arrow(ax, 39, 62, 39, 50)
    arrow(ax, 50, 39, 56, 39)
    arrow(ax, 78, 39, 80, 39)
    ax.text(50, 12, "碰撞 matcher 与主 matcher 优先级相同，插在主 matcher 后面，所以查找路径是主表然后副表。",
            ha="center", fontsize=10, color=INK)
    save(fig, "06_lookup.png")


def fig_geometry():
    fig, ax = new_ax(12.8, 6.8)
    ax.set_title("num_log = 24 时主表为什么是 2^24 行", fontsize=16, color=NAVY, pad=8)
    box(ax, 4, 28, 42, 52, "驱动几何\nnum_log = 24 的实际配法\n\n行数 2^24\n每行 4 个 STE\nSTE 总数 2^26\n\n2^24 条 key 时\n平均每行 1 条\n主表槽位利用率约 25%", "#D6E4F0", fs=12)
    box(ax, 54, 28, 42, 52, "紧凑几何\n对照实验，驱动不会这样配\n\n行数 2^22\n每行 4 个 STE\nSTE 总数 2^24\n\n同样 2^24 条 key\n平均每行 4 条\n哈希一偏，整行溢出", "#F8D7D2", fs=12)
    ax.text(50, 14, "深度加在行数之外：STE 区间 = 2^(sz_row_log + sz_col_log)。碰撞表只从自己的行对数里减 5。",
            ha="center", fontsize=10, color=INK)
    save(fig, "07_geometry.png")


def load_summary():
    path = os.path.join(SIM, "summary.csv")
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_hist():
    path = os.path.join(SIM, "hist.csv")
    rows = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows[(r["exp"], r["data"])].append(r)
    return rows


def fig_results(summary, hist):
    want = [
        ("A", "uniform", "A 均匀"),
        ("B", "uniform", "B 配满"),
        ("C", "uniform", "C 超出"),
        ("A", "ctu_resample", "A 真实采样"),
        ("Dtight", "uniform", "D 紧凑"),
    ]
    labels, over, theory, fail, tfail = [], [], [], [], []
    by = {(r["exp"], r["data"]): r for r in summary}
    tick = {
        "A 均匀": "1000万条\n2^24 行",
        "B 配满": "1677万条\n2^24 行",
        "C 超出": "2000万条\n2^24 行",
        "A 真实采样": "CTU 采样\n1000万条",
        "D 紧凑": "1677万条\n2^22 行",
    }
    for key in want:
        r = by[(key[0], key[1])]
        labels.append(tick[key[2]])
        over.append(float(r["over_frac"]) * 100)
        theory.append(float(r["theory_over"]) * 100)
        fail.append(float(r["fail_frac"]) * 100)
        tfail.append(float(r["theory_fail"]) * 100)

    fig, axes = plt.subplots(1, 2, figsize=(13.6, 5.8), dpi=140)
    x = list(range(len(labels) - 1))
    w = 0.36
    axes[0].bar([i - w / 2 for i in x], over[:-1], width=w, color=TEAL, label="仿真溢出")
    axes[0].bar([i + w / 2 for i in x], theory[:-1], width=w, color=ORANGE, label="Poisson 公式")
    axes[0].set_xticks(x, labels[:-1], fontsize=8.5)
    axes[0].set_ylabel("占该档插入条数的百分比")
    axes[0].set_ylim(0, 1.05)
    axes[0].set_xlabel("每一档的上行是插入条数，下行是主表行数\n这四档都是 2^24 行 × 4 个槽", fontsize=8.5)
    axes[0].set_title("主表溢出。2^22 行的 19.53% 放在右图")
    axes[0].legend(frameon=False, loc="upper left")
    for i, (a, b) in enumerate(zip(over[:-1], theory[:-1])):
        axes[0].text(i, max(a, b) + 0.04, f"{a:.3f}", ha="center", fontsize=8, color=NAVY)
    axes[1].bar([i - w / 2 for i in range(len(labels))], fail, width=w, color=CORAL, label="仿真失败")
    axes[1].bar([i + w / 2 for i in range(len(labels))], tfail, width=w, color=NAVY, label="Poisson 公式")
    axes[1].set_xticks(list(range(len(labels))), labels, fontsize=8)
    axes[1].set_ylabel("占该档插入条数的百分比")
    axes[1].set_xlabel("前四档主表是 2^24 行\n最后一档：同样 1677 万条，主表收成 2^22 行", fontsize=8.5)
    axes[1].set_title("插入失败率。前四档是 0，最后一档约 7.07%")
    axes[1].legend(frameon=False, loc="upper left")
    fig.tight_layout()
    save(fig, "08_overflow_fail.png")

    fig, ax = plt.subplots(figsize=(13.4, 5.6), dpi=140)
    names, s1, s2, fl = [], [], [], []
    order = [
        ("A", "uniform", "1000万\n均匀"),
        ("B", "uniform", "1677万\n2^24行"),
        ("C", "uniform", "2000万\n2^24行"),
        ("A", "uniform_alt", "1000万\n另一哈希"),
        ("A", "zipf_mixed", "1000万\n重尾全字段"),
        ("A", "ctu_resample", "1000万\nCTU采样"),
        ("TraceFull", "ctu", "160万\nCTU全量"),
        ("Ablation", "uniform", "1000万\n无副表"),
        ("Dtight", "uniform", "1677万\n2^22行"),
        ("A", "zipf_coarse", "1000万\n粗哈希"),
    ]
    for exp, data, name in order:
        r = by[(exp, data)]
        n = float(r["n"])
        names.append(name)
        s1.append(float(r["step1"]) / n * 100)
        s2.append(float(r["step2"]) / n * 100)
        fl.append(float(r["fail"]) / n * 100)
    import numpy as np
    idx = np.arange(len(names))
    ax.bar(idx, s1, color=GREEN, label="1 次 WQE")
    ax.bar(idx, s2, bottom=s1, color=ORANGE, label="2 次 WQE")
    bottom2 = [a + b for a, b in zip(s1, s2)]
    ax.bar(idx, fl, bottom=bottom2, color=CORAL, label="失败")
    ax.set_xticks(idx, names, fontsize=8)
    ax.set_xlabel("每一档是一次插入。上行是条数，下行是数据集或表的排法\n只有“2^22行”的主表是 2^22 行；“无副表”去掉了碰撞表。其余主表都是 2^24 行 × 4 槽，并且带碰撞表", fontsize=8.5, labelpad=10)
    ax.set_ylabel("占该档插入条数的百分比")
    ax.set_ylim(0, 112)
    ax.set_title("一次插入用了几次 WQE")
    ax.legend(frameon=False, loc="center left", bbox_to_anchor=(1.01, 0.55))
    for i, text in ((7, "失败 0.072%"), (8, "失败 7.07%"), (9, "失败 14.1%")):
        ax.text(i, 103, text, ha="center", va="bottom", fontsize=7.5, color=CORAL)
    fig.tight_layout()
    save(fig, "09_steps.png")

    fig, ax = plt.subplots(figsize=(9.0, 5.2), dpi=140)
    for exp, data, style, label in (
        ("A", "uniform", "-o", "均匀 A 仿真"),
        ("B", "uniform", "-s", "配满 B 仿真"),
    ):
        rows = hist[(exp, data)]
        ks, cs, ex = [], [], []
        total = sum(int(r["count"]) for r in rows)
        for r in rows:
            k = int(r["k"])
            if k > 12:
                continue
            ks.append(k)
            cs.append(int(r["count"]) / total)
            ex.append(float(r["poisson_expect"]) / total)
        ax.plot(ks, cs, style, color=TEAL if exp == "A" else NAVY, label=label)
        ax.plot(ks, ex, "--", color=ORANGE if exp == "A" else CORAL,
                label=("Poisson λ=0.596" if exp == "A" else "Poisson λ=1"))
    ax.set_xlabel("k = 落到同一主表行的 key 条数\nk=0 是空行。k>4 时只有 4 条留在主表，多出的进冲突表。图上画到 k=12", fontsize=8.5)
    ax.set_ylabel("这样的行占主表 2^24 行的比例")
    ax.set_title("均匀哈希：1000 万条（λ=0.596）和配满 1677 万条（λ=1）")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    save(fig, "10_hist_uniform.png")

    fig, ax = plt.subplots(figsize=(9.0, 5.2), dpi=140)
    rows = hist[("A", "zipf_coarse")]
    ks = [int(r["k"]) for r in rows if int(r["k"]) <= 20]
    cs = []
    total = sum(int(r["count"]) for r in rows)
    capped = int(rows[-1]["count"]) if rows else 0
    for r in rows:
        if int(r["k"]) <= 20:
            cs.append(int(r["count"]) / total)
    ax.bar(ks, cs, color=TEAL)
    ax.set_yscale("log")
    ax.set_xlabel("k = 落到同一主表行的 key 条数，只画 0 到 20\nk≥47 的行另有 6986 行，没有画在这张图上", fontsize=8.5)
    ax.set_ylabel("这样的行占主表 2^24 行的比例（对数）")
    ax.set_title("粗哈希、1000 万条：绝大多数行是空的")
    fig.tight_layout()
    save(fig, "11_hist_zipf.png")

    fig, ax = plt.subplots(figsize=(9.2, 5.4), dpi=140)
    metrics = ["主表利用率 %", "溢出 %", "2 步 WQE %", "失败 %"]
    b = by[("B", "uniform")]
    d = by[("Dtight", "uniform")]
    def vals(r):
        n = float(r["n"])
        return [
            float(r["u_main"]) * 100,
            float(r["over_frac"]) * 100,
            float(r["step2"]) / n * 100,
            float(r["fail_frac"]) * 100,
        ]
    import numpy as np
    idx = np.arange(len(metrics))
    ax.bar(idx - 0.18, vals(b), width=0.36, color=TEAL, label="驱动几何  2^24 行")
    ax.bar(idx + 0.18, vals(d), width=0.36, color=CORAL, label="紧凑几何  2^22 行")
    ax.set_xticks(idx, metrics)
    ax.set_ylabel("百分比")
    ax.set_xlabel("左柱是驱动几何（2^24 行），右柱是紧凑几何（2^22 行）\n利用率的分母是槽位数；溢出、2 步 WQE、失败的分母是 16,777,216 条 key", fontsize=8.5, labelpad=16)
    ax.set_title("同一批 1677 万条均匀 key，两种行数")
    ax.legend(frameon=False)
    fig.tight_layout()
    save(fig, "12_d_compare.png")

    fig, ax = plt.subplots(figsize=(9.2, 5.4), dpi=140)
    xs, us, fs = [], [], []
    for exp, data in (("A", "uniform"), ("B", "uniform"), ("C", "uniform"), ("Dtight", "uniform")):
        r = by[(exp, data)]
        xs.append(float(r["n"]) / float(r["rows"]))
        us.append(float(r["u_main"]) * 100)
        fs.append(float(r["fail_frac"]) * 100)
    ax.plot(xs[:3], us[:3], "-o", color=TEAL, label="驱动几何 主表利用率")
    ax.plot(xs[:3], fs[:3], "-o", color=GREEN, label="驱动几何 失败率")
    ax.scatter([xs[3]], [us[3]], s=60, color=CORAL, label="紧凑几何 利用率")
    ax.scatter([xs[3]], [fs[3]], s=60, color=ORANGE, label="紧凑几何 失败率")
    ax.set_xlabel("λ = 插入条数 / 主表行数\n0.596 = 1000万 / 2^24 行，1 = 配满，1.19 = 2000万 / 2^24 行，4 = 1677万 / 2^22 行", fontsize=8.5)
    ax.set_ylabel("百分比")
    ax.set_title("λ 变大时，主表利用率和插入失败率")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    save(fig, "13_capacity.png")
    _ = capped


def load_depth():
    path = os.path.join(SIM, "depth.csv")
    rows = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows[(r["exp"], r["data"], r["table"])].append(r)
    return rows


def fig_loads(summary, depth):
    import math
    import numpy as np

    order = [("L25", "25%"), ("L50", "50%"), ("L70", "70%"),
             ("L90", "90%"), ("L100", "100%")]
    by = {(r["exp"], r["data"]): r for r in summary}
    labels, over, theory, fail, tfail = [], [], [], [], []
    for exp, lab in order:
        r = by[(exp, "uniform")]
        labels.append(lab)
        over.append(float(r["over_frac"]) * 100)
        theory.append(float(r["theory_over"]) * 100)
        fail.append(float(r["fail_frac"]) * 100)
        tfail.append(float(r["theory_fail"]) * 100)

    load_ticks = ["25%\n419万条", "50%\n839万条", "70%\n1174万条",
                  "90%\n1510万条", "100%\n1678万条"]
    fig, axes = plt.subplots(1, 2, figsize=(12.6, 5.6), dpi=140)
    x = np.arange(len(labels))
    w = 0.36
    axes[0].bar(x - w / 2, over, width=w, color=TEAL, label="仿真")
    axes[0].bar(x + w / 2, theory, width=w, color=ORANGE, label="Poisson 公式")
    axes[0].set_xticks(x, load_ticks, fontsize=8)
    axes[0].set_xlabel("上行是负载 = 插入条数 / 2^24，下行是这一档的条数\n主表始终是 2^24 行 × 4 槽。这不是槽位被填满的比例", fontsize=8.5)
    axes[0].set_ylabel("占该档插入条数的百分比")
    axes[0].set_title("主表溢出率")
    axes[0].legend(frameon=False)
    for i, v in enumerate(over):
        axes[0].text(i, v + max(over) * 0.03, f"{v:.4f}", ha="center", fontsize=7, color=NAVY)
    axes[0].set_ylim(0, max(over + theory) * 1.45)
    axes[1].bar(x - w / 2, fail, width=w, color=CORAL, label="仿真，即插入失败率")
    axes[1].bar(x + w / 2, tfail, width=w, color=NAVY, label="Poisson 公式")
    axes[1].set_xticks(x, load_ticks, fontsize=8)
    axes[1].set_xlabel("刻度与左图相同\n100% 时平均每行 1 条，槽位大约只满 25%", fontsize=8.5)
    axes[1].set_ylabel("占该档插入条数的百分比")
    axes[1].set_ylim(0, 1)
    axes[1].set_title("冲突表溢出率，五档都是 0")
    for i, v in enumerate(fail):
        axes[1].text(i, 0.04, "0", ha="center", fontsize=9, color=CORAL)
    axes[1].legend(frameon=False, fontsize=8, loc="upper right")
    fig.tight_layout()
    save(fig, "14_load_overflow.png")

    colors = ["#8FB8C9", "#1F7A8C", "#E09F3E", "#C4493A", "#1B3A4B"]
    fig, ax = plt.subplots(figsize=(9.2, 5.4), dpi=140)
    depths = np.arange(0, 5)
    width = 0.15
    for i, ((exp, lab), color) in enumerate(zip(order, colors)):
        rows = {int(r["depth"]): float(r["fraction"])
                for r in depth[(exp, "uniform", "main")]}
        vals = [rows.get(d, 0.0) * 100 for d in depths]
        ax.bar(depths + (i - 2) * width, vals, width=width, color=color, label=lab)
    ax.set_xticks(depths, [str(d) for d in depths])
    ax.set_xlabel("横轴：这一行已经写下的 STE 个数\n0 = 空行，4 = 四个槽都满。同一数字上从左到右五根柱是 25% 到 100%", fontsize=8.5, labelpad=16)
    ax.set_ylabel("占主表 2^24 行的百分比")
    ax.set_title("驱动几何：主表每行深度")
    ax.legend(frameon=False, ncol=5, fontsize=8, title="插入条数 / 2^24")
    fig.tight_layout()
    save(fig, "15_main_row_depth.png")

    fig, ax = plt.subplots(figsize=(10.2, 5.4), dpi=140)
    for (exp, lab), color in zip(order, colors):
        rows = sorted(depth[(exp, "uniform", "collision")], key=lambda r: int(r["depth"]))
        xs, ys = [], []
        for r in rows:
            frac = float(r["fraction"])
            if frac <= 0:
                continue
            xs.append(int(r["depth"]))
            ys.append(frac * 100)
        ax.plot(xs, ys, "-o", color=color, label=lab, markersize=3.5)
    ax.set_yscale("log")
    ax.set_xlabel("横轴：冲突表这一行写下的 STE 个数，范围 0 到 16\n五条线仍是插入条数 / 2^24 的五档。纵轴是行数占比，用对数", fontsize=8.5, labelpad=16)
    ax.set_ylabel("占冲突表 2^19 行的百分比（对数）")
    ax.set_title("驱动几何：冲突表每行深度")
    ax.set_xticks(range(0, 17))
    ax.legend(frameon=False, ncol=5, fontsize=8, title="插入条数 / 2^24")
    fig.tight_layout()
    save(fig, "16_col_row_depth.png")
    _ = math


def fig_tight(summary, depth):
    import numpy as np

    order = [("D25", "25%"), ("D50", "50%"), ("D70", "70%"),
             ("D90", "90%"), ("D100", "100%")]
    by = {(r["exp"], r["data"]): r for r in summary}
    wide = [("L25", "25%"), ("L50", "50%"), ("L70", "70%"),
            ("L90", "90%"), ("L100", "100%")]
    labels = [lab for _, lab in order]
    over, theory, fail, tfail = [], [], [], []
    for exp, _lab in order:
        r = by[(exp, "uniform")]
        over.append(float(r["over_frac"]) * 100)
        theory.append(float(r["theory_over"]) * 100)
        fail.append(float(r["fail_frac"]) * 100)
        tfail.append(float(r["theory_fail"]) * 100)

    load_ticks = ["25%\n419万条", "50%\n839万条", "70%\n1174万条",
                  "90%\n1510万条", "100%\n1678万条"]
    fig, axes = plt.subplots(1, 2, figsize=(12.6, 5.6), dpi=140)
    x = np.arange(len(labels))
    w = 0.36
    axes[0].bar(x - w / 2, over, width=w, color=CORAL, label="仿真")
    axes[0].bar(x + w / 2, theory, width=w, color=ORANGE, label="Poisson 公式")
    axes[0].set_xticks(x, load_ticks, fontsize=8)
    axes[0].set_xlabel("上行仍是插入条数 / 2^24，下行是条数。表已收成 2^22 行\n所以 100% 时平均每行 4 条，不是 1 条", fontsize=8.5)
    axes[0].set_ylabel("占该档插入条数的百分比")
    axes[0].set_title("2^22 行主表溢出率")
    axes[0].legend(frameon=False, loc="upper left")
    ymax = max(over + theory)

    def bar_label(v):
        if v == 0:
            return "0"
        if v < 0.1:
            return f"{v:.3f}"
        return f"{v:.2f}"

    for i, v in enumerate(over):
        axes[0].text(i, v + ymax * 0.03, bar_label(v), ha="center", fontsize=7, color=NAVY)
    axes[0].set_ylim(0, ymax * 1.28)
    axes[1].bar(x - w / 2, fail, width=w, color=CORAL, label="仿真，即插入失败率")
    axes[1].bar(x + w / 2, tfail, width=w, color=NAVY, label="Poisson 公式")
    axes[1].set_xticks(x, load_ticks, fontsize=8)
    axes[1].set_xlabel("刻度与左图相同\n纵轴是冲突表也放不下的 key，占该档插入条数的百分比", fontsize=8.5)
    axes[1].set_ylabel("占插入条数的百分比")
    axes[1].set_title("2^22 行时的冲突表溢出率")
    fmax = max(fail + tfail + [1.0])
    for i, v in enumerate(fail):
        axes[1].text(i, v + fmax * 0.03, bar_label(v), ha="center", fontsize=7, color=NAVY)
    axes[1].set_ylim(0, fmax * 1.28)
    axes[1].legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    save(fig, "17_tight_overflow.png")

    fig, axes = plt.subplots(1, 2, figsize=(12.6, 5.4), dpi=140)
    wover, wfail = [], []
    for exp, _lab in wide:
        r = by[(exp, "uniform")]
        wover.append(float(r["over_frac"]) * 100)
        wfail.append(float(r["fail_frac"]) * 100)
    axes[0].plot(x, wover, "-o", color=TEAL, label="2^24 行")
    axes[0].plot(x, over, "-o", color=CORAL, label="2^22 行")
    axes[0].set_xticks(x, load_ticks, fontsize=8)
    axes[0].set_yscale("log")
    axes[0].set_xlabel("上行 = 插入条数 / 2^24，下行是条数\n青线和红线用的是同一批 key", fontsize=8.5, labelpad=12)
    axes[0].set_ylabel("主表溢出率（%）")
    axes[0].set_title("同一批 key 的主表溢出")
    axes[0].legend(frameon=False)
    axes[1].plot(x, wfail, "-o", color=TEAL, label="2^24 行")
    axes[1].plot(x, fail, "-o", color=CORAL, label="2^22 行")
    axes[1].set_xticks(x, load_ticks, fontsize=8)
    axes[1].set_xlabel("刻度与左图相同\n纵轴是插入失败率，等于该几何下冲突表的溢出率", fontsize=8.5, labelpad=12)
    axes[1].set_ylabel("冲突表溢出率，即失败率（%）")
    axes[1].set_title("同一批 key 的插入失败率")
    axes[1].legend(frameon=False)
    fig.tight_layout()
    save(fig, "18_geometry_compare.png")

    colors = ["#8FB8C9", "#1F7A8C", "#E09F3E", "#C4493A", "#1B3A4B"]
    fig, ax = plt.subplots(figsize=(9.2, 5.4), dpi=140)
    depths = np.arange(0, 5)
    width = 0.15
    for i, ((exp, lab), color) in enumerate(zip(order, colors)):
        rows = {int(r["depth"]): float(r["fraction"])
                for r in depth[(exp, "uniform", "main")]}
        vals = [rows.get(d, 0.0) * 100 for d in depths]
        ax.bar(depths + (i - 2) * width, vals, width=width, color=color, label=lab)
    ax.set_xticks(depths, [str(d) for d in depths])
    ax.set_xlabel("横轴：2^22 行主表里，这一行写下的 STE 个数\n0 = 空行，4 = 行满。同一数字上从左到右五根柱是 25% 到 100%", fontsize=8.5, labelpad=16)
    ax.set_ylabel("占主表 2^22 行的百分比")
    ax.set_title("2^22 行主表的每行深度")
    ax.legend(frameon=False, ncol=5, fontsize=8, title="插入条数 / 2^24")
    fig.tight_layout()
    save(fig, "19_tight_main_depth.png")

    fig, ax = plt.subplots(figsize=(10.2, 5.4), dpi=140)
    for (exp, lab), color in zip(order, colors):
        rows = sorted(depth[(exp, "uniform", "collision")], key=lambda r: int(r["depth"]))
        xs, ys = [], []
        for r in rows:
            frac = float(r["fraction"])
            if frac <= 0:
                continue
            xs.append(int(r["depth"]))
            ys.append(frac * 100)
        ax.plot(xs, ys, "-o", color=color, label=lab, markersize=3.5)
    ax.set_yscale("log")
    ax.set_xlabel("横轴：2^17 行冲突表里，这一行写下的 STE 个数，0 到 16\n深度 16 表示 16 个槽都满，再来的 key 就是插入失败", fontsize=8.5, labelpad=16)
    ax.set_ylabel("占冲突表 2^17 行的百分比（对数）")
    ax.set_title("2^22 行方案的冲突表每行深度")
    ax.set_xticks(range(0, 17))
    ax.legend(frameon=False, ncol=5, fontsize=8, title="插入条数 / 2^24")
    fig.tight_layout()
    save(fig, "20_tight_col_depth.png")


def main():
    summary = load_summary()
    hist = load_hist()
    depth = load_depth()
    fig_objects()
    fig_domains()
    fig_legacy()
    fig_switchdev()
    fig_rx()
    fig_tx()
    fig_fdb()
    fig_insert()
    fig_lookup()
    fig_geometry()
    fig_results(summary, hist)
    fig_loads(summary, depth)
    fig_tight(summary, depth)


if __name__ == "__main__":
    main()
