"""Map Gate perp underlyings to on-chain contract addresses via spot currency API.

Output: out/chip_coverage.csv (currency, chain, addr, evm_ok)
"""
import glob
import os

import pandas as pd

from common import DATA_DIR, OUT_DIR, RateLimiter, get_json

BASE = "https://api.gateio.ws/api/v4"
LIMITER = RateLimiter(rate_per_sec=10, burst=10)

# chains with reliable free public RPC + log support
EVM_CHAINS = {"BSC", "ETH", "BASEEVM", "BASE", "ARBEVM", "ARBITRUM", "OPETH", "MATIC", "POLYGON"}


def currency_info(ccy: str):
    try:
        return get_json(f"{BASE}/spot/currencies/{ccy}", None, LIMITER, "gate_ccy")
    except RuntimeError:
        return None


def main(currencies: list[str]):
    rows = []
    for ccy in currencies:
        info = currency_info(ccy)
        if not info:
            rows.append({"currency": ccy, "chain": None, "addr": None,
                         "total_supply": None, "evm_ok": False, "note": "no spot listing"})
            continue
        import re
        chains = info.get("chains") or []
        best = None
        for ch in chains:
            nm = (ch.get("name") or "").upper()
            a = (ch.get("addr") or "").lower()
            if nm in EVM_CHAINS and re.fullmatch(r"0x[0-9a-f]{40}", a):
                pri = 0 if nm == "BSC" else (1 if nm == "ETH" else 2)
                if best is None or pri < best[0]:
                    best = (pri, nm, a)
        rows.append({
            "currency": ccy,
            "chain": best[1] if best else (chains[0].get("name") if chains else None),
            "addr": best[2].lower() if best else None,
            "total_supply": info.get("total_supply"),
            "evm_ok": best is not None,
            "note": "",
        })
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT_DIR, "chip_coverage.csv"), index=False)
    n_ok = int(df["evm_ok"].sum())
    print(f"coverage: {n_ok}/{len(df)} EVM-resolvable")
    return df


if __name__ == "__main__":
    files = sorted(glob.glob(os.path.join(DATA_DIR, "gate", "*.csv.gz")))
    ccys = [os.path.basename(p)[:-7].replace("_USDT", "") for p in files]
    main(ccys)
