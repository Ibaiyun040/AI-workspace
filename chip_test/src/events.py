"""Detect squeeze-like event episodes on the Gate hourly panel, and pick
OI-matched non-event controls.

Event hour definition (outcome marker, not an alarm):
  - upward burst: (high-low)/open >= 25% AND close >= open, OR
  - short-liquidation burst: short_liq_usd >= 3% of prev-hour OI
  with prev-hour OI >= $1M in both cases.
Episodes: event hours merged when gaps < 48h; episode time T = first hour.

Controls: for each episode, contracts with no event hour in [T-14d, T+7d],
median OI over [T-7d, T) within [1/3, 3] x event's, closest OI ratio first.
"""
import glob
import gzip
import json
import os

import numpy as np
import pandas as pd

from common import DATA_DIR, OUT_DIR

GATE_DIR = os.path.join(DATA_DIR, "gate")
AMP_TH = 0.25
LIQ_TH = 0.03
OI_MIN = 1_000_000
MERGE_GAP = 48 * 3600


def load_panel(path: str) -> pd.DataFrame:
    with gzip.open(path, "rt") as f:
        df = pd.read_csv(f)
    return df


def event_hours(df: pd.DataFrame) -> pd.DataFrame:
    oi_prev = df["oi_usd"].shift(1)
    amp = (df["high"] - df["low"]) / df["open"].replace(0, np.nan)
    burst_up = (amp >= AMP_TH) & (df["close"] >= df["open"])
    liq = pd.to_numeric(df.get("short_liq_usd"), errors="coerce")
    liq_burst = liq >= LIQ_TH * oi_prev
    ok_oi = oi_prev >= OI_MIN
    mask = (burst_up | liq_burst.fillna(False)) & ok_oi.fillna(False)
    out = df.loc[mask, ["t"]].copy()
    out["amp"] = amp[mask]
    out["liq_frac"] = (liq / oi_prev)[mask]
    out["oi_prev"] = oi_prev[mask]
    return out


def episodes_for(df: pd.DataFrame) -> list[dict]:
    ev = event_hours(df)
    if ev.empty:
        return []
    eps = []
    cur = None
    for _, r in ev.iterrows():
        if cur is None or r["t"] - cur["t_last"] >= MERGE_GAP:
            if cur:
                eps.append(cur)
            cur = {"t_first": int(r["t"]), "t_last": int(r["t"]),
                   "n_hours": 1, "max_amp": float(r["amp"] or 0),
                   "max_liq_frac": float(r["liq_frac"] or 0),
                   "oi_at_event": float(r["oi_prev"])}
        else:
            cur["t_last"] = int(r["t"])
            cur["n_hours"] += 1
            cur["max_amp"] = max(cur["max_amp"], float(r["amp"] or 0))
            cur["max_liq_frac"] = max(cur["max_liq_frac"], float(r["liq_frac"] or 0))
    if cur:
        eps.append(cur)
    return eps


def main():
    files = sorted(glob.glob(os.path.join(GATE_DIR, "*.csv.gz")))
    all_eps = []
    med_oi = {}
    ev_windows = {}
    panels = {}
    for p in files:
        name = os.path.basename(p)[:-7]
        df = load_panel(p)
        if df.empty or "oi_usd" not in df:
            continue
        panels[name] = df
        eps = episodes_for(df)
        for e in eps:
            e["contract"] = name
            all_eps.append(e)
        ev_windows[name] = [(e["t_first"], e["t_last"]) for e in eps]

    eps_df = pd.DataFrame(all_eps).sort_values("t_first")
    eps_df.to_csv(os.path.join(OUT_DIR, "episodes.csv"), index=False)
    print(f"contracts scanned={len(panels)} episodes={len(eps_df)} "
          f"tokens={eps_df['contract'].nunique() if len(eps_df) else 0}")

    # ---- control matching ----
    controls = []
    for _, e in eps_df.iterrows():
        T = e["t_first"]
        tgt = panels[e["contract"]]
        w = tgt[(tgt["t"] >= T - 7 * 86400) & (tgt["t"] < T)]
        tgt_oi = w["oi_usd"].median()
        if not np.isfinite(tgt_oi) or tgt_oi <= 0:
            continue
        cands = []
        for name, df in panels.items():
            if name == e["contract"]:
                continue
            # no event hour in [T-14d, T+7d]
            bad = any(t0 <= T + 7 * 86400 and t1 >= T - 14 * 86400
                      for (t0, t1) in ev_windows.get(name, []))
            if bad:
                continue
            w2 = df[(df["t"] >= T - 7 * 86400) & (df["t"] < T)]
            if len(w2) < 100:
                continue
            oi2 = w2["oi_usd"].median()
            if not np.isfinite(oi2) or oi2 < OI_MIN / 3:
                continue
            ratio = oi2 / tgt_oi
            if ratio < 1 / 3 or ratio > 3:
                continue
            cands.append((abs(np.log(ratio)), name, oi2))
        cands.sort()
        for rank, (d, name, oi2) in enumerate(cands[:6]):
            controls.append({"event_contract": e["contract"], "event_t": int(T),
                             "control_contract": name, "control_oi": oi2,
                             "target_oi": tgt_oi, "rank": rank})
    ctrl_df = pd.DataFrame(controls)
    ctrl_df.to_csv(os.path.join(OUT_DIR, "controls.csv"), index=False)
    print(f"control candidates rows={len(ctrl_df)}")


if __name__ == "__main__":
    main()
