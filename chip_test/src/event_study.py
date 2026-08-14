"""检验 1: event study of chip-structure factors around squeeze episodes.

Pipeline: select pilot episodes (EVM-covered) + OI-matched controls,
pull transfers, compute daily factors, Mann-Whitney U on pre-event window,
BH-FDR, aligned trajectory plots, markdown report.
"""
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
from onchain import pull_token

GATE_DIR = os.path.join(DATA_DIR, "gate")
FACT_DIR = os.path.join(DATA_DIR, "factors")
os.makedirs(FACT_DIR, exist_ok=True)

FACTORS = ["CR10", "CR50", "HHI", "dCR10_7d", "dCR10_30d", "FWA_30d",
           "WAR_14d", "OPR", "WCB_dist", "HODL_90d", "ExShare", "PoolShare"]
MAX_EVENTS = 12
MAX_EP_PER_TOKEN = 2
CTRL_PER_EVENT = 3
PRE_WIN = (7, 1)          # mean over days [T-7d, T-1d]
TRAJ = range(-30, 8)


def load_gate(contract):
    with gzip.open(os.path.join(GATE_DIR, f"{contract}.csv.gz"), "rt") as f:
        return pd.read_csv(f)


def get_factors(chain, addr, contract, emit_from, emit_to):
    key = f"{chain}_{addr.lower()}"
    path = os.path.join(FACT_DIR, key + ".csv")
    if os.path.exists(path):
        df = pd.read_csv(path)
        if df.empty or (df["day"].min() <= emit_from + 86400 and df["day"].max() >= emit_to - 86400):
            return df
    pull_token(chain, addr)
    df = compute_daily_factors(chain, addr, load_gate(contract), emit_from, emit_to)
    df.to_csv(path, index=False)
    return df


def main():
    eps = pd.read_csv(os.path.join(OUT_DIR, "episodes.csv"))
    ctrl = pd.read_csv(os.path.join(OUT_DIR, "controls.csv"))
    cov = pd.read_csv(os.path.join(OUT_DIR, "chip_coverage.csv"))
    cov_map = {r["currency"]: r for _, r in cov.iterrows() if r["evm_ok"]}

    def ccy(contract):
        return contract.replace("_USDT", "")

    eps["severity"] = eps[["max_amp", "max_liq_frac"]].max(axis=1)
    eps = eps[eps["contract"].map(lambda c: ccy(c) in cov_map)]
    eps = (eps.sort_values("severity", ascending=False)
              .groupby("contract").head(MAX_EP_PER_TOKEN)
              .sort_values("severity", ascending=False))
    pilot = eps.head(MAX_EVENTS).copy()
    print(f"pilot episodes: {len(pilot)} on tokens: {sorted(pilot['contract'].unique())}",
          flush=True)

    # assemble sample units
    units = []   # (kind, contract, chain, addr, T)
    for _, e in pilot.iterrows():
        units.append(("event", e["contract"], e["t_first"]))
        cands = ctrl[(ctrl["event_contract"] == e["contract"]) &
                     (ctrl["event_t"] == e["t_first"])].sort_values("rank")
        n = 0
        for _, c in cands.iterrows():
            if ccy(c["control_contract"]) not in cov_map:
                continue
            units.append(("control", c["control_contract"], e["t_first"]))
            n += 1
            if n >= CTRL_PER_EVENT:
                break
        if n == 0:
            print(f"  WARN no EVM control for {e['contract']} @ {e['t_first']}")

    # compute factors per unit
    rows = []
    fail = []
    for kind, contract, T in units:
        info = cov_map[ccy(contract)]
        chain, addr = info["chain"], info["addr"]
        try:
            f = get_factors(chain, addr, contract,
                            int(T) - 45 * 86400, int(T) + 8 * 86400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            fail.append((kind, contract, str(e)[:150]))
            continue
        if f is None or f.empty:
            fail.append((kind, contract, "empty factors"))
            continue
        f = f.copy()
        f["off"] = ((f["day"] - int(T)) // 86400).astype(int)
        for _, r in f[(f["off"] >= min(TRAJ)) & (f["off"] <= max(TRAJ))].iterrows():
            rec = {"kind": kind, "contract": contract, "T": int(T), "off": int(r["off"])}
            for fac in FACTORS:
                rec[fac] = r.get(fac)
            rows.append(rec)
        print(f"  ok [{kind}] {contract} T={T}", flush=True)

    panel = pd.DataFrame(rows)
    panel.to_csv(os.path.join(OUT_DIR, "event_panel.csv"), index=False)
    if fail:
        pd.DataFrame(fail, columns=["kind", "contract", "err"]).to_csv(
            os.path.join(OUT_DIR, "event_failures.csv"), index=False)

    # ---- stats: pre-event window mean per unit ----
    pre = panel[(panel["off"] >= -PRE_WIN[0]) & (panel["off"] <= -PRE_WIN[1])]
    unit_mean = pre.groupby(["kind", "contract", "T"])[FACTORS].mean().reset_index()
    stats = []
    for fac in FACTORS:
        a = unit_mean.loc[unit_mean["kind"] == "event", fac].dropna()
        b = unit_mean.loc[unit_mean["kind"] == "control", fac].dropna()
        if len(a) < 4 or len(b) < 4:
            stats.append({"factor": fac, "n_event": len(a), "n_control": len(b),
                          "p": np.nan, "rank_biserial": np.nan,
                          "event_median": a.median() if len(a) else np.nan,
                          "control_median": b.median() if len(b) else np.nan})
            continue
        u, p = mannwhitneyu(a, b, alternative="two-sided")
        rb = 1 - 2 * u / (len(a) * len(b))
        stats.append({"factor": fac, "n_event": len(a), "n_control": len(b),
                      "p": p, "rank_biserial": rb,
                      "event_median": a.median(), "control_median": b.median()})
    st = pd.DataFrame(stats).sort_values("p")
    # BH-FDR
    valid = st["p"].notna()
    m = valid.sum()
    st["q_bh"] = np.nan
    ranked = st.loc[valid].sort_values("p").reset_index()
    qs = ranked["p"] * m / (np.arange(m) + 1)
    qs = np.minimum.accumulate(qs[::-1])[::-1]
    for i, idx in enumerate(ranked["index"]):
        st.loc[idx, "q_bh"] = qs[i]
    st.to_csv(os.path.join(OUT_DIR, "event_study_stats.csv"), index=False)
    print(st.to_string(index=False))

    # ---- trajectory plots ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plot_facs = [f for f in FACTORS if panel[f].notna().sum() > 50]
    ncol = 3
    nrow = int(np.ceil(len(plot_facs) / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(15, 3.2 * nrow), squeeze=False)
    for i, fac in enumerate(plot_facs):
        ax = axes[i // ncol][i % ncol]
        for kind, color in [("event", "crimson"), ("control", "steelblue")]:
            g = (panel[panel["kind"] == kind].groupby("off")[fac]
                 .agg(["median", lambda x: x.quantile(0.25),
                       lambda x: x.quantile(0.75)]))
            g.columns = ["med", "q1", "q3"]
            ax.plot(g.index, g["med"], color=color, label=kind)
            ax.fill_between(g.index, g["q1"], g["q3"], color=color, alpha=0.15)
        ax.axvline(0, color="k", lw=0.8, ls="--")
        ax.set_title(fac)
        ax.legend(fontsize=7)
    fig.suptitle("Chip factors around squeeze episodes (day offset vs event T)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "event_trajectories.png"), dpi=110)
    print("saved plots + stats")


if __name__ == "__main__":
    sys.exit(main())
