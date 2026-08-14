"""Chip-structure factors from raw transfer logs.

Walk transfers chronologically, emit daily (UTC midnight) factor snapshots.

Exclusions:
  burn addresses, token contract itself, DEX pools (GeckoTerminal top pools),
  exchange-like addresses (cumulative transfer participation count >= EXCH_TX).

Factors (all shares of effective supply S = minted - burned):
  CR10, CR50, HHI          concentration among non-excluded holders
  dCR10_7d, dCR10_30d      change in CR10
  FWA_30d                  balance held by wallets first seen <30d ago
  WAR_14d                  14d net accumulation of the top-100 cohort (fixed at d-14)
  OPR                      share of priced supply in profit at day price
  WCB_dist                 price / top-100 cohort weighted cost - 1
  HODL_90d                 share held by addresses inactive >90d
  ExShare / PoolShare      share held by exchange-like / DEX pools
"""
import gzip
import os
from collections import defaultdict

import numpy as np
import pandas as pd

from common import DATA_DIR, RateLimiter, get_json

BURN = {"0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "0x0000000000000000000000000000000000000001"}
EXCH_TX = 1000  # cumulative tx-participation threshold => exchange-like
GT_LIMITER = RateLimiter(0.4, 2)  # geckoterminal ~30/min
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


def load_transfers(chain: str, addr: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, "onchain", f"{chain}_{addr.lower()}.csv.gz")
    with gzip.open(path, "rt") as f:
        df = pd.read_csv(f, dtype={"value": str})
    return df


def hourly_price(gate_panel: pd.DataFrame) -> dict[int, float]:
    return dict(zip(gate_panel["t"].astype(int), gate_panel["close"].astype(float)))


def compute_daily_factors(chain: str, addr: str, gate_panel: pd.DataFrame,
                          emit_from: int, emit_to: int) -> pd.DataFrame:
    """emit_from/emit_to: unix ts bounds; snapshots at each UTC midnight."""
    addr = addr.lower()
    tr = load_transfers(chain, addr)
    if tr.empty:
        return pd.DataFrame()
    pools = dex_pools(chain, addr)
    px = hourly_price(gate_panel)

    bal: dict[str, int] = defaultdict(int)
    txn: dict[str, int] = defaultdict(int)
    first_seen: dict[str, int] = {}
    last_active: dict[str, int] = {}
    cost_qty: dict[str, float] = defaultdict(float)   # priced qty
    cost_avg: dict[str, float] = {}

    day0 = (max(emit_from, int(tr["ts"].iloc[0])) // 86400) * 86400
    days = list(range(day0, (emit_to // 86400 + 1) * 86400, 86400))
    day_idx = 0
    out = []
    cohort_hist: dict[int, tuple[set, float, float]] = {}  # day -> (top100 set, sum_share, wavg_cost)
    cr10_hist: dict[int, float] = {}

    ts_arr = tr["ts"].to_numpy()
    src_arr = tr["src"].to_numpy()
    dst_arr = tr["dst"].to_numpy()
    val_arr = tr["value"].to_numpy()

    def snapshot(day_end: int):
        supply = sum(bal.values()) - sum(bal.get(b, 0) for b in BURN)
        if supply <= 0:
            return None
        excluded = pools | {addr} | BURN
        exch = {a for a, n in txn.items() if n >= EXCH_TX} - excluded
        holders = [(a, v) for a, v in bal.items()
                   if v > 0 and a not in excluded and a not in exch]
        holders.sort(key=lambda kv: -kv[1])
        hsum = [v for _, v in holders]
        s = float(supply)
        cr10 = sum(hsum[:10]) / s
        cr50 = sum(hsum[:50]) / s
        hhi = sum((v / s) ** 2 for v in hsum[:1000])
        fwa = sum(v for a, v in holders
                  if first_seen.get(a, 0) >= day_end - 30 * 86400) / s
        hodl = sum(v for a, v in holders
                   if last_active.get(a, 0) <= day_end - 90 * 86400) / s
        exsh = sum(bal[a] for a in exch) / s
        poolsh = sum(bal.get(p, 0) for p in pools) / s

        price_now = px.get((day_end - 3600) // 3600 * 3600)
        if price_now is None:
            hours = [h for h in range(day_end - 86400, day_end, 3600) if h in px]
            price_now = px[hours[-1]] if hours else None

        top100 = holders[:100]
        top_set = {a for a, _ in top100}
        top_share = sum(v for _, v in top100) / s
        pq = sum(cost_qty[a] for a, _ in top100 if a in cost_avg)
        wavg = (sum(cost_qty[a] * cost_avg[a] for a, _ in top100 if a in cost_avg) / pq
                if pq > 0 else np.nan)
        cohort_hist[day_end] = (top_set, top_share, wavg)
        cr10_hist[day_end] = cr10

        opr = np.nan
        if price_now is not None:
            in_profit = sum(min(v, cost_qty[a]) for a, v in holders
                            if a in cost_avg and cost_avg[a] < price_now)
            priced = sum(min(v, cost_qty[a]) for a, v in holders if a in cost_avg)
            opr = in_profit / priced if priced > 0 else np.nan

        wcb = np.nan
        if price_now is not None and np.isfinite(wavg) and wavg > 0:
            wcb = price_now / wavg - 1

        war = np.nan
        past = cohort_hist.get(day_end - 14 * 86400)
        if past:
            pset, pshare, _ = past
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
            row = snapshot(days[day_idx] + 86400)
            if row and days[day_idx] + 86400 >= emit_from:
                out.append(row)
            day_idx += 1
        s, d, v = src_arr[i], dst_arr[i], int(val_arr[i])
        if v == 0:
            continue
        bal[s] -= v
        bal[d] += v
        txn[s] += 1
        txn[d] += 1
        last_active[s] = ts
        last_active[d] = ts
        if d not in first_seen:
            first_seen[d] = ts
        p = px.get(int(ts) // 3600 * 3600)
        if p is not None and d not in BURN:
            q0, c0 = cost_qty[d], cost_avg.get(d)
            vf = float(v)
            if c0 is None or q0 <= 0:
                cost_qty[d], cost_avg[d] = vf, p
            else:
                cost_qty[d] = q0 + vf
                cost_avg[d] = (q0 * c0 + vf * p) / (q0 + vf)
        if s in cost_avg:
            cost_qty[s] = max(0.0, cost_qty[s] - float(v))

    while day_idx < len(days):
        row = snapshot(days[day_idx] + 86400)
        if row and days[day_idx] + 86400 >= emit_from:
            out.append(row)
        day_idx += 1

    return pd.DataFrame(out)
