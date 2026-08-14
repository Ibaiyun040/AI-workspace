"""Chip-structure factors from transfer logs (full-history or windowed+B0).

Semantics under windowed mode:
  - balances initialized from archive snapshot at window start (B0)
  - B0 holders: first_seen=0 (old wallet), last_active=0 (dormant since <=B0)
  - supply(day) = supply0 + cumulative mints - burns observed in window
Factors (shares of effective supply):
  CR10, CR50, HHI, dCR10_7d, dCR10_30d, FWA_30d, WAR_14d,
  OPR, WCB_dist, HODL_90d, ExShare, PoolShare
"""
import os
from collections import defaultdict

import numpy as np

from common import RateLimiter, get_json
from onchain import load_token

BURN = {"0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "0x0000000000000000000000000000000000000001"}
ZERO = "0x0000000000000000000000000000000000000000"
EXCH_TX = 500
GT_LIMITER = RateLimiter(0.4, 2)
GT_NET = {"BSC": "bsc", "ETH": "eth"}


def dex_pools(chain: str, addr: str) -> set[str]:
    net = GT_NET.get(chain)
    if not net:
        return set()
    pools = set()
    try:
        data = get_json(
            f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{addr}/pools",
            {"page": 1}, GT_LIMITER, "gt_pools")
        for p in data.get("data", []):
            a = (p.get("attributes", {}).get("address") or "").lower()
            if a:
                pools.add(a)
    except RuntimeError:
        pass
    return pools


def hourly_price(gate_panel) -> dict[int, float]:
    return dict(zip(gate_panel["t"].astype(int), gate_panel["close"].astype(float)))


def compute_daily_factors(key: str, chain: str, addr: str, gate_panel,
                          emit_from: int, emit_to: int):
    import pandas as pd
    tr, meta = load_token(key)
    if tr.empty:
        return pd.DataFrame()
    addr = addr.lower()
    pools = dex_pools(chain, addr)
    px = hourly_price(gate_panel)

    bal: dict[str, int] = defaultdict(int)
    txn: dict[str, int] = defaultdict(int)
    first_seen: dict[str, int] = {}
    last_active: dict[str, int] = {}
    cost_qty: dict[str, float] = defaultdict(float)
    cost_avg: dict[str, float] = {}

    supply = int(meta.get("supply0") or 0)
    for a, v in (meta.get("balances") or {}).items():
        bal[a] = int(v)
        first_seen[a] = 0
        last_active[a] = 0
    if supply == 0 and bal:
        supply = sum(bal.values())

    first_ts = int(tr["ts"].iloc[0])
    warmup_from = emit_from - 31 * 86400  # dCR30/WAR need running history
    day0 = (max(warmup_from, first_ts) // 86400) * 86400
    days = list(range(day0, (emit_to // 86400 + 1) * 86400, 86400))
    if not days:
        return pd.DataFrame()
    day_idx = 0
    out = []
    cohort_hist: dict[int, tuple[set, float]] = {}
    wavg_hist: dict[int, float] = {}
    cr10_hist: dict[int, float] = {}

    ts_arr = tr["ts"].to_numpy()
    src_arr = tr["src"].to_numpy()
    dst_arr = tr["dst"].to_numpy()
    val_arr = tr["value"].to_numpy()

    def snapshot(day_end: int):
        nonlocal supply
        s = float(supply)
        if s <= 0:  # mint without Transfer event: fall back to held sum
            s = float(sum(v for v in bal.values() if v > 0))
        if s <= 0:
            return None
        excluded = pools | {addr} | BURN
        exch = {a for a, n in txn.items() if n >= EXCH_TX} - excluded
        holders = [(a, v) for a, v in bal.items()
                   if v > 0 and a not in excluded and a not in exch]
        holders.sort(key=lambda kv: -kv[1])
        hvals = [v for _, v in holders]
        cr10 = sum(hvals[:10]) / s
        cr50 = sum(hvals[:50]) / s
        hhi = sum((v / s) ** 2 for v in hvals[:1000])
        fwa = sum(v for a, v in holders
                  if first_seen.get(a, 0) >= day_end - 30 * 86400) / s
        hodl = sum(v for a, v in holders
                   if last_active.get(a, 0) <= day_end - 90 * 86400) / s
        exsh = sum(bal[a] for a in exch) / s
        poolsh = sum(bal.get(p, 0) for p in pools) / s

        price_now = px.get((day_end - 3600) // 3600 * 3600)
        if price_now is None:
            hs = [h for h in range(day_end - 86400, day_end, 3600) if h in px]
            price_now = px[hs[-1]] if hs else None

        top100 = holders[:100]
        top_set = {a for a, _ in top100}
        top_share = sum(v for _, v in top100) / s
        pq = sum(cost_qty[a] for a, _ in top100 if a in cost_avg)
        wavg = (sum(cost_qty[a] * cost_avg[a] for a, _ in top100 if a in cost_avg) / pq
                if pq > 0 else np.nan)
        cohort_hist[day_end] = (top_set, top_share)
        wavg_hist[day_end] = wavg
        cr10_hist[day_end] = cr10

        opr = np.nan
        if price_now is not None:
            priced = sum(min(v, cost_qty[a]) for a, v in holders if a in cost_avg)
            if priced > 0:
                in_profit = sum(min(v, cost_qty[a]) for a, v in holders
                                if a in cost_avg and cost_avg[a] < price_now)
                opr = in_profit / priced

        wcb = np.nan
        if price_now is not None and np.isfinite(wavg) and wavg > 0:
            wcb = price_now / wavg - 1

        war = np.nan
        past = cohort_hist.get(day_end - 14 * 86400)
        if past:
            pset, pshare = past
            war = sum(bal.get(a, 0) for a in pset) / s - pshare

        d7 = cr10_hist.get(day_end - 7 * 86400)
        d30 = cr10_hist.get(day_end - 30 * 86400)
        return {
            "day": day_end, "supply": s, "price": price_now,
            "CR10": cr10, "CR50": cr50, "HHI": hhi,
            "dCR10_7d": cr10 - d7 if d7 is not None else np.nan,
            "dCR10_30d": cr10 - d30 if d30 is not None else np.nan,
            "FWA_30d": fwa, "WAR_14d": war,
            "OPR": opr, "WCB_dist": wcb,
            "HODL_90d": hodl, "ExShare": exsh, "PoolShare": poolsh,
            "n_holders": len(holders),
        }

    for i in range(len(tr)):
        ts = ts_arr[i]
        while day_idx < len(days) and ts >= days[day_idx] + 86400:
            de = days[day_idx] + 86400
            if de >= warmup_from:
                row = snapshot(de)
                if row and de >= emit_from:
                    out.append(row)
            day_idx += 1
        s_, d_, v = src_arr[i], dst_arr[i], int(val_arr[i])
        if v == 0:
            continue
        if s_ == ZERO:
            supply += v
        else:
            bal[s_] -= v
        if d_ in BURN:
            supply -= v
        bal[d_] += v
        txn[s_] += 1
        txn[d_] += 1
        last_active[s_] = ts
        last_active[d_] = ts
        if d_ not in first_seen:
            first_seen[d_] = ts
        p = px.get(int(ts) // 3600 * 3600)
        if p is not None and d_ not in BURN:
            q0 = cost_qty[d_]
            c0 = cost_avg.get(d_)
            vf = float(v)
            if c0 is None or q0 <= 0:
                cost_qty[d_], cost_avg[d_] = vf, p
            else:
                cost_qty[d_] = q0 + vf
                cost_avg[d_] = (q0 * c0 + vf * p) / (q0 + vf)
        if s_ in cost_avg:
            cost_qty[s_] = max(0.0, cost_qty[s_] - float(v))

    while day_idx < len(days):
        de = days[day_idx] + 86400
        if de >= warmup_from:
            row = snapshot(de)
            if row and de >= emit_from:
                out.append(row)
        day_idx += 1

    return pd.DataFrame(out)
