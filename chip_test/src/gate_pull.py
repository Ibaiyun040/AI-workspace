"""Pull Gate USDT perp universe: 1h contract_stats + 1h candles (~180d) per contract.

Output: data/gate/{contract}.csv.gz with hourly panel:
  t, open, high, low, close, vol_usd, oi_usd, short_liq_usd, long_liq_usd,
  lsr_account, top_lsr_size, top_lsr_account, long_users, short_users
"""
import gzip
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from common import DATA_DIR, RateLimiter, get_json

BASE = "https://api.gateio.ws/api/v4"
LIMITER = RateLimiter(rate_per_sec=12, burst=12)
GATE_DIR = os.path.join(DATA_DIR, "gate")
os.makedirs(GATE_DIR, exist_ok=True)

STATS_FIELDS = ["lsr_account", "top_lsr_size", "top_lsr_account",
                "long_users", "short_users", "open_interest_usd",
                "short_liq_usd", "long_liq_usd", "mark_price"]


def list_contracts():
    out = []
    for offset in range(0, 5000, 100):
        page = get_json(f"{BASE}/futures/usdt/contracts",
                        {"limit": 100, "offset": offset},
                        LIMITER, "gate_meta")
        out.extend(page)
        if len(page) < 100:
            break
    return out


def pull_stats(contract: str, t_from: int, t_to: int):
    rows = []
    cur = t_from
    while cur < t_to:
        page = get_json(f"{BASE}/futures/usdt/contract_stats",
                        {"contract": contract, "interval": "1h",
                         "from": cur, "limit": 1000},
                        LIMITER, "gate_stats")
        if not page:
            break
        page = sorted(page, key=lambda r: r["time"])
        rows.extend(page)
        nxt = page[-1]["time"] + 3600
        if nxt <= cur:
            break
        cur = nxt
        if len(page) < 1000:
            break
    return rows


def pull_candles(contract: str, t_from: int, t_to: int):
    rows = []
    cur = t_from
    while cur < t_to:
        page = get_json(f"{BASE}/futures/usdt/candlesticks",
                        {"contract": contract, "interval": "1h",
                         "from": cur, "to": min(cur + 1999 * 3600, t_to)},
                        LIMITER, "gate_candles")
        if not page:
            cur += 1999 * 3600
            continue
        page = sorted(page, key=lambda r: r["t"])
        rows.extend(page)
        nxt = page[-1]["t"] + 3600
        if nxt <= cur:
            break
        cur = nxt
    return rows


def build_panel(contract: str, t_from: int, t_to: int) -> pd.DataFrame | None:
    stats = pull_stats(contract, t_from, t_to)
    candles = pull_candles(contract, t_from, t_to)
    if not candles:
        return None
    cd = pd.DataFrame(candles).rename(columns={"t": "t"})
    for c in ["o", "h", "l", "c", "sum"]:
        cd[c] = pd.to_numeric(cd[c], errors="coerce")
    cd = cd[["t", "o", "h", "l", "c", "sum"]].rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "sum": "vol_usd"})
    if stats:
        st = pd.DataFrame(stats).rename(columns={"time": "t"})
        keep = ["t"] + [f for f in STATS_FIELDS if f in st.columns]
        st = st[keep].rename(columns={"open_interest_usd": "oi_usd"})
        panel = cd.merge(st, on="t", how="left")
    else:
        panel = cd
        panel["oi_usd"] = float("nan")
        panel["short_liq_usd"] = float("nan")
    panel = panel.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    return panel


def main():
    t_to = int(time.time()) // 3600 * 3600
    t_from = t_to - 179 * 86400
    contracts = list_contracts()
    live = [c for c in contracts if not c.get("in_delisting")]
    print(f"contracts total={len(contracts)} live={len(live)}", flush=True)
    meta = {c["name"]: {"quanto_multiplier": c.get("quanto_multiplier"),
                        "create_time": c.get("create_time")} for c in contracts}
    with open(os.path.join(GATE_DIR, "_meta.json"), "w") as f:
        json.dump(meta, f)

    done = 0
    t0 = time.time()

    def work(name):
        out_path = os.path.join(GATE_DIR, f"{name}.csv.gz")
        if os.path.exists(out_path):
            return name, "cached"
        try:
            panel = build_panel(name, t_from, t_to)
        except Exception as e:  # noqa: BLE001
            return name, f"FAILED: {str(e)[:120]}"
        if panel is None or len(panel) == 0:
            return name, "empty"
        buf = io.BytesIO()
        with gzip.open(buf, "wt") as gz:
            panel.to_csv(gz, index=False)
        with open(out_path, "wb") as f:
            f.write(buf.getvalue())
        return name, f"{len(panel)} rows"

    names = [c["name"] for c in live]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, n): n for n in names}
        for fut in as_completed(futs):
            name, status = fut.result()
            done += 1
            if done % 25 == 0 or done == len(names):
                rate = done / max(time.time() - t0, 1)
                print(f"[{done}/{len(names)}] {name}: {status} "
                      f"({rate:.1f} contracts/s)", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
