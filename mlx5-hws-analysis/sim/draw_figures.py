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


def fig_rx():
    fig, ax = new_ax(13.4, 7.2)
    ax.set_title("RX 数据面：流表在 TIR / RQ 之前", fontsize=16, color=NAVY, pad=8)
    stages = [
        (2, "线侧 / 上行口", SAND),
        (18, "FDB 根表\nnamespace FDB", MIST),
        (34, "matcher 链\nRTC RX 哈希选行", "#D6E4F0"),
        (50, "STC 动作\nvport / 丢弃 / 跳表", "#E7F6D9"),
        (66, "代表口进入\nNIC RX 域", SAND),
        (82, "TIR → RQ\nCQ → EQ", "#F8D7D2"),
    ]
    for x, t, c in stages:
        box(ax, x, 62, 14, 22, t, c, fs=9)
    for x in (16, 32, 48, 64, 80):
        arrow(ax, x, 73, x + 2, 73)
    ax.text(50, 54, "本仓库 HWS 只创建 FS_FT_FDB。TIR 属于 NIC RX 的终结对象，不在 HWS 动作枚举里。",
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
    ax.set_title("TX 数据面：SQ / TIS 在流表之前，FDB 用 TX RTC", fontsize=16, color=NAVY, pad=8)
    stages = [
        (2, "主机数据包\nSQ 门铃", SAND),
        (18, "TIS\n发送侧传输接口", "#F8D7D2"),
        (34, "vport 进入 FDB\nflow source = VPORT", MIST),
        (50, "RTC TX\n哈希选行，比 tag", "#D6E4F0"),
        (66, "STC\n改头 / 封装 / 计数", "#E7F6D9"),
        (82, "上行口发出\n或丢弃", SAND),
    ]
    for x, t, c in stages:
        box(ax, x, 48, 14, 28, t, c, fs=9)
    for x in (16, 32, 48, 64, 80):
        arrow(ax, x, 62, x + 2, 62)
    ax.text(50, 30,
            "optimize_flow_src = WIRE 时跳过 TX RTC；= VPORT 时跳过 RX RTC。默认 ANY，一条规则写两套 STE。",
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
    box(ax, 4, 28, 42, 52, "驱动实际几何\n\n行数 2^24\n每行 4 个 STE\nSTE 总数 2^26\n\n2^24 条 key 时\n平均每行 1 条\n主表槽位利用率 25%", "#D6E4F0", fs=12)
    box(ax, 54, 28, 42, 52, "槽位刚好等于 key 的排法\n\n行数 2^22\n每行 4 个 STE\nSTE 总数 2^24\n\n平均每行 4 条\n哈希一偏，整行溢出\n这是实验 D-tight", "#F8D7D2", fs=12)
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
    for key in want:
        r = by[(key[0], key[1])]
        labels.append(key[2])
        over.append(float(r["over_frac"]) * 100)
        theory.append(float(r["theory_over"]) * 100)
        fail.append(float(r["fail_frac"]) * 100)
        tfail.append(float(r["theory_fail"]) * 100)

    fig, axes = plt.subplots(1, 2, figsize=(12.2, 4.8), dpi=140)
    x = list(range(len(labels) - 1))
    w = 0.36
    axes[0].bar([i - w / 2 for i in x], over[:-1], width=w, color=TEAL, label="仿真溢出")
    axes[0].bar([i + w / 2 for i in x], theory[:-1], width=w, color=ORANGE, label="Poisson 公式")
    axes[0].set_xticks(x, labels[:-1], fontsize=9)
    axes[0].set_ylabel("占插入条数的百分比")
    axes[0].set_ylim(0, 1.05)
    axes[0].set_title("主表溢出，驱动几何（D 紧凑是 19.53%）")
    axes[0].legend(frameon=False, loc="upper left")
    for i, (a, b) in enumerate(zip(over[:-1], theory[:-1])):
        axes[0].text(i, max(a, b) + 0.04, f"{a:.3f}", ha="center", fontsize=8, color=NAVY)
    axes[1].bar([i - w / 2 for i in range(len(labels))], fail, width=w, color=CORAL, label="仿真失败")
    axes[1].bar([i + w / 2 for i in range(len(labels))], tfail, width=w, color=NAVY, label="Poisson 公式")
    axes[1].set_xticks(list(range(len(labels))), labels, fontsize=9)
    axes[1].set_ylabel("占插入条数的百分比")
    axes[1].set_title("两级都满才失败；驱动几何上失败数为 0")
    axes[1].legend(frameon=False)
    fig.tight_layout()
    save(fig, "08_overflow_fail.png")

    fig, ax = plt.subplots(figsize=(11.5, 4.6), dpi=140)
    names, s1, s2, fl = [], [], [], []
    order = [
        ("A", "uniform", "A 均匀"),
        ("B", "uniform", "B 配满"),
        ("C", "uniform", "C 超出"),
        ("A", "uniform_alt", "A 另一哈希"),
        ("A", "zipf_mixed", "A 重尾但全字段哈希"),
        ("A", "ctu_resample", "A CTU 采样"),
        ("TraceFull", "ctu", "CTU 全量"),
        ("Ablation", "uniform", "消融 无副表"),
        ("Dtight", "uniform", "D 紧凑"),
        ("A", "zipf_coarse", "A 粗哈希"),
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
    ax.set_xticks(idx, names, rotation=25, ha="right", fontsize=8)
    ax.set_ylabel("占插入条数的百分比")
    ax.set_ylim(0, 100)
    ax.set_title("成功插入只有 1 步或 2 步")
    ax.legend(frameon=False, ncol=3)
    fig.tight_layout()
    save(fig, "09_steps.png")

    fig, ax = plt.subplots(figsize=(8.2, 4.6), dpi=140)
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
    ax.set_xlabel("一行上的到达条数（含溢出，不截断在 4）")
    ax.set_ylabel("行的比例")
    ax.set_title("均匀哈希下的行占用与 Poisson")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    save(fig, "10_hist_uniform.png")

    fig, ax = plt.subplots(figsize=(8.2, 4.6), dpi=140)
    rows = hist[("A", "zipf_coarse")]
    ks = [int(r["k"]) for r in rows if int(r["k"]) <= 20]
    cs = []
    total = sum(int(r["count"]) for r in rows)
    capped = int(rows[-1]["count"]) if rows else 0
    for r in rows:
        if int(r["k"]) <= 20:
            cs.append(int(r["count"]) / total)
    ax.bar(ks, cs, color=TEAL)
    ax.set_xlabel("一行上的到达条数（47 表示 47 及以上）" if False else "一行上的到达条数，最后一个桶是截断")
    ax.set_ylabel("行的比例")
    ax.set_title("粗哈希重尾：大量行是空的，少数行极深")
    fig.tight_layout()
    save(fig, "11_hist_zipf.png")

    fig, ax = plt.subplots(figsize=(8.4, 4.8), dpi=140)
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
    ax.bar(idx - 0.18, vals(b), width=0.36, color=TEAL, label="D-mlx  2^24 行")
    ax.bar(idx + 0.18, vals(d), width=0.36, color=CORAL, label="D-tight  2^22 行")
    ax.set_xticks(idx, metrics)
    ax.set_title("同一 2^24 条均匀 key：多留的行换来少溢出")
    ax.legend(frameon=False)
    fig.tight_layout()
    save(fig, "12_d_compare.png")

    fig, ax = plt.subplots(figsize=(8.2, 4.6), dpi=140)
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
    ax.set_xlabel("λ = 插入条数 / 主表行数")
    ax.set_ylabel("百分比")
    ax.set_title("空槽是行内容量，紧凑几何把失败率拉到约 7%")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    save(fig, "13_capacity.png")
    _ = capped


def main():
    summary = load_summary()
    hist = load_hist()
    fig_objects()
    fig_rx()
    fig_tx()
    fig_fdb()
    fig_insert()
    fig_lookup()
    fig_geometry()
    fig_results(summary, hist)


if __name__ == "__main__":
    main()
