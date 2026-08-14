"""检验 1: event study of chip-structure factors around squeeze episodes.

Select pilot episodes (EVM-covered, severity-ranked) + OI-matched controls,
pull on-chain data (ETH: Blockscout full history; BSC: NodeReal windowed +
archive B0 balances), compute daily factors, Mann-Whitney U on the pre-event
window, BH-FDR, aligned trajectory plots, markdown-ready stats.

Run modes:
  python3 event_study.py pull     # data acquisition + factor computation only
  python3 event_study.py stats    # stats + plots from whatever factors exist
  python3 event_study.py all
"""
import glob
import gzip
import json
import os
import sys
import traceback

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

from common import DATA_DIR, OUT_DIR
from factors import compute_daily_factors
from onchain import bsc_windowed_pull, eth_full_pull

GATE_DIR = os.path.join(DATA_DIR, "gate")
FACT_DIR = os.path.join(DATA_DIR, "factors")
os.makedirs(FACT_DIR, exist_ok=True)

FACTORS = ["CR10", "CR50", "HHI", "dCR10_7d", "dCR10_30d", "FWA_30d",
           "WAR_14d", "OPR", "WCB_dist", "HODL_90d", "ExShare", "PoolShare"]
MAX_EVENTS = 12
MAX_EP_PER_TOKEN = 2
CTRL_PER_EVENT = 2
PRE_WIN = (7, 1)
TRAJ = range(-30, 8)
LOOKBACK = 135 * 86400   # covers HODL_90d at emit start T-45d
EMIT_BACK = 45 * 86400
EMIT_FWD = 8 * 86400


def load_gate(contract):
    with gzip.open(os.path.join(GATE_DIR, f"{contract}.csv.gz"), "rt") as f:
        return pd.read_csv(f)


MAX_CTRL_AGE_D = 450  # control perp listed within this window (age matching)


def build_units():
    eps = pd.read_csv(os.path.join(OUT_DIR, "episodes.csv"))
    ctrl = pd.read_csv(os.path.join(OUT_DIR, "controls.csv"))
    cov = pd.read_csv(os.path.join(OUT_DIR, "chip_coverage.csv"))
    cov_map = {r["currency"]: r for _, r in cov.iterrows()
               if r["evm_ok"] and r["chain"] in ("BSC", "ETH")}
    with open(os.path.join(GATE_DIR, "_meta.json")) as f:
        meta = json.load(f)
    import time as _time
    now = _time.time()

    def young(contract):
        ct = (meta.get(contract) or {}).get("create_time")
        return ct is not None and now - ct <= MAX_CTRL_AGE_D * 86400

    def ccy(c):
        return c.replace("_USDT", "")

    eps["severity"] = eps[["max_amp", "max_liq_frac"]].max(axis=1)
    eps = eps[eps["contract"].map(lambda c: ccy(c) in cov_map)]
    eps = (eps.sort_values("severity", ascending=False)
              .groupby("contract").head(MAX_EP_PER_TOKEN)
              .sort_values("severity", ascending=False))
    pilot = eps.head(MAX_EVENTS)

    units = []
    for _, e in pilot.iterrows():
        ec = ccy(e["contract"])
        echain = cov_map[ec]["chain"]
        units.append({"kind": "event", "contract": e["contract"],
                      "ccy": ec, "chain": echain,
                      "addr": cov_map[ec]["addr"], "T": int(e["t_first"])})
        cands = ctrl[(ctrl["event_contract"] == e["contract"]) &
                     (ctrl["event_t"] == e["t_first"])].sort_values("rank")
        same, other = [], []
        for _, c in cands.iterrows():
            cc = ccy(c["control_contract"])
            if cc not in cov_map or not young(c["control_contract"]):
                continue
            (same if cov_map[cc]["chain"] == echain else other).append(cc_row(c, cc, cov_map))
        chosen = (same + other)[:CTRL_PER_EVENT]
        for u in chosen:
            u["T"] = int(e["t_first"])
            units.append(u)
        if not chosen:
            print(f"WARN: no control for {e['contract']} @ {e['t_first']}")
    return units


def cc_row(c, cc, cov_map):
    return {"kind": "control", "contract": c["control_contract"], "ccy": cc,
            "chain": cov_map[cc]["chain"], "addr": cov_map[cc]["addr"]}


def pull_and_factor(units, chain_filter=None):
    # per-token union window
    tok = {}
    for u in units:
        if chain_filter and u["chain"] != chain_filter:
            continue
        k = (u["chain"], u["addr"])
        lo, hi = u["T"] - LOOKBACK, u["T"] + EMIT_FWD
        if k in tok:
            tok[k] = (min(tok[k][0], lo), max(tok[k][1], hi), tok[k][2])
        else:
            tok[k] = (lo, hi, u["contract"])
    # ETH first (fast), then BSC ordered by window size
    order = sorted(tok.items(), key=lambda kv: (kv[0][0] != "ETH",
                                                kv[1][1] - kv[1][0]))
    keys = {}
    for (chain, addr), (lo, hi, contract) in order:
        fpath = os.path.join(FACT_DIR, f"{chain}_{addr}.csv")
        if os.path.exists(fpath):
            df = pd.read_csv(fpath)
            if not df.empty and df["day"].min() <= lo + LOOKBACK - EMIT_BACK + 86400 \
               and df["day"].max() >= hi - 86400:
                keys[(chain, addr)] = fpath
                print(f"cached factors {chain} {contract}", flush=True)
                continue
        print(f"pulling {chain} {contract} {addr}", flush=True)
        try:
            if chain == "ETH":
                key = eth_full_pull(addr)
            else:
                key = bsc_windowed_pull(addr, lo, hi)
            emit_from = lo + LOOKBACK - EMIT_BACK   # = min(T)-45d
            f = compute_daily_factors(key, chain, addr, load_gate(contract),
                                      emit_from, hi)
            f.to_csv(fpath, index=False)
            keys[(chain, addr)] = fpath
            print(f"  factors saved: {len(f)} days", flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            with open(os.path.join(OUT_DIR, "pull_failures.log"), "a") as fh:
                fh.write(f"{chain} {addr} {contract}\n{traceback.format_exc()}\n")
    return keys


def assemble_panel(units):
    import time as _time
    now = _time.time()
    rows = []
    for u in units:
        fpath = os.path.join(FACT_DIR, f"{u['chain']}_{u['addr']}.csv")
        if not os.path.exists(fpath):
            continue
        f = pd.read_csv(fpath)
        if f.empty:
            continue
        f["off"] = ((f["day"] - u["T"]) // 86400).astype(int)
        sel = f[(f["off"] >= min(TRAJ)) & (f["off"] <= max(TRAJ)) & (f["day"] <= now)]
        for _, r in sel.iterrows():
            rec = {"kind": u["kind"], "contract": u["contract"],
                   "chain": u["chain"], "T": u["T"], "off": int(r["off"])}
            for fac in FACTORS:
                rec[fac] = r.get(fac)
            rows.append(rec)
    return pd.DataFrame(rows)


def run_stats(panel):
    pre = panel[(panel["off"] >= -PRE_WIN[0]) & (panel["off"] <= -PRE_WIN[1])]
    unit_mean = pre.groupby(["kind", "contract", "T"])[FACTORS].mean().reset_index()
    unit_mean.to_csv(os.path.join(OUT_DIR, "unit_prewindow_means.csv"), index=False)
    stats = []
    for fac in FACTORS:
        a = unit_mean.loc[unit_mean["kind"] == "event", fac].dropna()
        b = unit_mean.loc[unit_mean["kind"] == "control", fac].dropna()
        row = {"factor": fac, "n_event": len(a), "n_control": len(b),
               "event_median": a.median() if len(a) else np.nan,
               "control_median": b.median() if len(b) else np.nan,
               "p": np.nan, "rank_biserial": np.nan}
        if len(a) >= 4 and len(b) >= 4:
            u, p = mannwhitneyu(a, b, alternative="two-sided")
            row["p"] = p
            row["rank_biserial"] = 1 - 2 * u / (len(a) * len(b))
        stats.append(row)
    st = pd.DataFrame(stats)
    valid = st["p"].notna()
    m = int(valid.sum())
    st["q_bh"] = np.nan
    if m:
        ranked = st.loc[valid].sort_values("p").reset_index()
        qs = (ranked["p"] * m / (np.arange(m) + 1)).to_numpy()
        qs = np.minimum.accumulate(qs[::-1])[::-1]
        for i, idx in enumerate(ranked["index"]):
            st.loc[idx, "q_bh"] = min(qs[i], 1.0)
    st = st.sort_values("p")
    st.to_csv(os.path.join(OUT_DIR, "event_study_stats.csv"), index=False)
    print(st.to_string(index=False))
    return st


def make_plots(panel):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plot_facs = [f for f in FACTORS if panel[f].notna().sum() > 40]
    ncol = 3
    nrow = max(1, int(np.ceil(len(plot_facs) / ncol)))
    fig, axes = plt.subplots(nrow, ncol, figsize=(15, 3.2 * nrow), squeeze=False)
    for i, fac in enumerate(plot_facs):
        ax = axes[i // ncol][i % ncol]
        for kind, color in [("event", "crimson"), ("control", "steelblue")]:
            sub = panel[panel["kind"] == kind]
            g = sub.groupby("off")[fac].agg(
                med="median",
                q1=lambda x: x.quantile(0.25),
                q3=lambda x: x.quantile(0.75))
            ax.plot(g.index, g["med"], color=color, label=kind)
            ax.fill_between(g.index, g["q1"], g["q3"], color=color, alpha=0.15)
        ax.axvline(0, color="k", lw=0.8, ls="--")
        ax.set_title(fac, fontsize=10)
        ax.legend(fontsize=7)
    for j in range(len(plot_facs), nrow * ncol):
        axes[j // ncol][j % ncol].axis("off")
    fig.suptitle("Chip-structure factors around squeeze episodes (event-aligned days)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "event_trajectories.png"), dpi=110)
    print("plots saved")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    units = build_units()
    with open(os.path.join(OUT_DIR, "pilot_units.json"), "w") as f:
        json.dump(units, f, indent=1)
    n_ev = sum(1 for u in units if u["kind"] == "event")
    print(f"units: {len(units)} ({n_ev} events, {len(units)-n_ev} controls)",
          flush=True)
    if mode in ("pull", "all"):
        pull_and_factor(units, chain_filter=(sys.argv[2] if len(sys.argv) > 2 else None))
    if mode in ("stats", "all"):
        panel = assemble_panel(units)
        panel.to_csv(os.path.join(OUT_DIR, "event_panel.csv"), index=False)
        got = panel.groupby("kind")["contract"].nunique().to_dict() if len(panel) else {}
        print(f"panel units with data: {got}")
        if len(panel):
            run_stats(panel)
            make_plots(panel)


if __name__ == "__main__":
    main()
