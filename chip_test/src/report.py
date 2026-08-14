"""Generate out/event_study_report.md from stats + panel outputs."""
import datetime
import json
import os

import pandas as pd

from common import OUT_DIR

PASS_P = 0.05
PASS_RB = 0.30


def dt(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.UTC).strftime("%Y-%m-%d %H:%M")


def main():
    st = pd.read_csv(os.path.join(OUT_DIR, "event_study_stats.csv"))
    panel = pd.read_csv(os.path.join(OUT_DIR, "event_panel.csv"))
    with open(os.path.join(OUT_DIR, "pilot_units.json")) as f:
        units = json.load(f)
    ev_units = [u for u in units if u["kind"] == "event"]
    got = set(zip(panel["kind"], panel["contract"], panel["T"]))
    n_ev = len({(c, t) for k, c, t in got if k == "event"})
    n_ct = len({(c, t) for k, c, t in got if k == "control"})

    st["sig"] = (st["p"] < PASS_P) & (st["rank_biserial"].abs() >= PASS_RB)
    winners = st[st["sig"]]

    lines = []
    lines.append("# 检验 1：筹码结构因子事件研究报告\n")
    lines.append(f"- 生成时间: {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"- 事件样本: {n_ev} 个 episode（计划试点 {len(ev_units)}），"
                 f"对照样本: {n_ct} 个（OI 匹配 + 上市年龄 ≤450 天 + 同链优先）")
    lines.append("- 事件定义: Gate USDT 永续 1h 面板上，(振幅≥25% 且收涨) 或 (单小时空爆≥前时 OI 的 3%)，"
                 "且前时 OI≥$100 万；48h 合并为 episode，取首个触发小时为 T")
    lines.append("- 检验量: 事件前窗 [T−7d, T−1d] 因子日频均值，事件组 vs 对照组 "
                 "Mann-Whitney U（双侧），效应量为 rank-biserial，BH-FDR 校正")
    lines.append("- 数据: ETH=Blockscout 全史转账；BSC=NodeReal 窗口扫描 [T−135d, T+8d]"
                 " + 窗口起点归档余额快照（方法对事件/对照对称）\n")

    lines.append("## 结论\n")
    if len(winners):
        lines.append(f"**通过第一道闸门**：{len(winners)} 个因子满足 p<{PASS_P} 且 "
                     f"|rank-biserial|≥{PASS_RB}：")
        for _, r in winners.iterrows():
            direction = "高于" if r["event_median"] > r["control_median"] else "低于"
            lines.append(f"- **{r['factor']}**: p={r['p']:.4f}, q(BH)={r['q_bh']:.4f}, "
                         f"效应量={r['rank_biserial']:+.2f}，事件组中位数 {direction} 对照组"
                         f"（{r['event_median']:.4g} vs {r['control_median']:.4g}）")
        lines.append("")
    else:
        lines.append("**未通过第一道闸门**：没有因子同时满足显著性与效应量门槛。"
                     "按方案，筹码结构维度对本场景缺乏领先信息，应终止后续工程投入。\n")

    lines.append("## 全部因子统计\n")
    lines.append("| 因子 | n(事件) | n(对照) | 事件中位数 | 对照中位数 | p | q(BH) | 效应量 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in st.iterrows():
        lines.append(f"| {r['factor']} | {r['n_event']} | {r['n_control']} | "
                     f"{r['event_median']:.4g} | {r['control_median']:.4g} | "
                     f"{r['p']:.4f} | {r['q_bh']:.4f} | {r['rank_biserial']:+.2f} |"
                     if pd.notna(r["p"]) else
                     f"| {r['factor']} | {r['n_event']} | {r['n_control']} | "
                     f"{r['event_median']:.4g} | {r['control_median']:.4g} | — | — | — |")
    lines.append("")

    lines.append("## 事件清单\n")
    lines.append("| 合约 | 链 | 事件时刻 (UTC) | 数据 |")
    lines.append("|---|---|---|---|")
    for u in ev_units:
        has = ("event", u["contract"], u["T"]) in got
        lines.append(f"| {u['contract']} | {u['chain']} | {dt(u['T'])} | "
                     f"{'✓' if has else '缺失'} |")
    lines.append("")
    lines.append("![trajectories](event_trajectories.png)\n")
    lines.append("## 边界与偏差说明\n")
    lines.append("- BSC 窗口法看不到窗口前 135 天起就完全沉默的持有人，CR/HODL 水平被低估；"
                 "该偏差对事件组与对照组对称，组间对比仍有效。ETH 全史无此偏差，可对照检验。")
    lines.append("- 交易所地址识别用『窗口内交易参与次数 ≥500』启发式 + GeckoTerminal 池子排除，"
                 "无人工标签校验；漏标会抬高 CR 类因子读数（对两组同向）。")
    lines.append("- 成本类因子（OPR/WCB_dist）只对 Gate 期货价格窗口内（约 180 天）的转账定价，"
                 "更早筹码计为无成本数据。")
    lines.append("- 事件集由标签规则独立重建，与既有 A/D 回测的 episode 高度重合（TUT/BICO/STO/"
                 "RAVE/SIREN/LAB 等），但时间戳可能相差数小时。")

    with open(os.path.join(OUT_DIR, "event_study_report.md"), "w") as f:
        f.write("\n".join(lines))
    print("report written")


if __name__ == "__main__":
    main()
