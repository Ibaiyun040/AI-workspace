"""Pull ERC-20/BEP-20 Transfer logs via free public RPCs and store per-token.

Output: data/onchain/{chain}_{addr}.csv.gz  columns: block,log_index,ts,src,dst,value
value stored as string (raw integer units).
"""
import gzip
import io
import os
import sys
import time

import pandas as pd

from common import DATA_DIR, RateLimiter, get_json

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ONCHAIN_DIR = os.path.join(DATA_DIR, "onchain")
os.makedirs(ONCHAIN_DIR, exist_ok=True)

RPCS = {
    "BSC": ["https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc-dataseed1.bnbchain.org",
            "https://rpc.owlracle.info/bsc/70d38ce1826c4a60bb2a8e05a6c8b20f"],
    "ETH": ["https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
            "https://cloudflare-eth.com"],
}
LIMITERS = {"BSC": RateLimiter(8, 8), "ETH": RateLimiter(8, 8)}
_rpc_rr = {}


def rpc(chain: str, method: str, params, use_cache=True):
    urls = RPCS[chain]
    idx = _rpc_rr.get(chain, 0)
    last = None
    for k in range(len(urls) * 2):
        url = urls[(idx + k) % len(urls)]
        try:
            data = get_json(url, None, LIMITERS[chain],
                            f"rpc_{chain}", use_cache=use_cache,
                            method="POST",
                            body={"jsonrpc": "2.0", "id": 1,
                                  "method": method, "params": params},
                            max_retries=2)
            _rpc_rr[chain] = (idx + k) % len(urls)
            return data["result"]
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"all rpc failed {chain} {method}: {last}")


def latest_block(chain):
    return int(rpc(chain, "eth_blockNumber", [], use_cache=False), 16)


def block_ts(chain, number: int) -> int:
    b = rpc(chain, "eth_getBlockByNumber", [hex(number), False])
    return int(b["timestamp"], 16)


def find_block_at(chain, ts: int, lo=1, hi=None) -> int:
    hi = hi or latest_block(chain)
    while lo < hi:
        mid = (lo + hi) // 2
        if block_ts(chain, mid) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def first_code_block(chain, addr: str, hi=None) -> int:
    """Binary search for deployment block via eth_getCode."""
    hi = hi or latest_block(chain)
    lo = 1
    if rpc(chain, "eth_getCode", [addr, hex(hi)]) in ("0x", None):
        raise RuntimeError(f"{addr} has no code at head on {chain}")
    while lo < hi:
        mid = (lo + hi) // 2
        code = rpc(chain, "eth_getCode", [addr, hex(mid)])
        if code in ("0x", None):
            lo = mid + 1
        else:
            hi = mid
    return lo


def get_logs(chain, addr, from_block, to_block, chunk0=20000):
    logs = []
    cur = from_block
    chunk = chunk0
    while cur <= to_block:
        end = min(cur + chunk - 1, to_block)
        try:
            res = rpc(chain, "eth_getLogs", [{
                "address": addr,
                "topics": [TRANSFER_TOPIC],
                "fromBlock": hex(cur), "toBlock": hex(end)}])
        except RuntimeError:
            if chunk <= 500:
                raise
            chunk //= 4
            continue
        logs.extend(res)
        cur = end + 1
        if len(res) < 2000 and chunk < 100000:
            chunk *= 2
        elif len(res) > 8000:
            chunk = max(500, chunk // 2)
    return logs


def ts_anchors(chain, lo, hi, n=60):
    """Sparse block->ts anchors for interpolation."""
    pts = []
    step = max(1, (hi - lo) // n)
    for b in range(lo, hi + 1, step):
        pts.append((b, block_ts(chain, b)))
    if pts[-1][0] != hi:
        pts.append((hi, block_ts(chain, hi)))
    return pts


def interp_ts(anchors, block):
    import bisect
    blocks = [a[0] for a in anchors]
    i = bisect.bisect_left(blocks, block)
    if i == 0:
        return anchors[0][1]
    if i >= len(anchors):
        return anchors[-1][1]
    (b0, t0), (b1, t1) = anchors[i - 1], anchors[i]
    if b1 == b0:
        return t0
    return int(t0 + (t1 - t0) * (block - b0) / (b1 - b0))


def pull_token(chain: str, addr: str) -> str:
    addr = addr.lower()
    out_path = os.path.join(ONCHAIN_DIR, f"{chain}_{addr}.csv.gz")
    if os.path.exists(out_path):
        return out_path
    head = latest_block(chain)
    dep = first_code_block(chain, addr, hi=head)
    t0 = time.time()
    logs = get_logs(chain, addr, dep, head)
    anchors = ts_anchors(chain, dep, head)
    rows = []
    for lg in logs:
        if len(lg["topics"]) < 3:
            continue
        blk = int(lg["blockNumber"], 16)
        rows.append({
            "block": blk,
            "log_index": int(lg["logIndex"], 16),
            "ts": interp_ts(anchors, blk),
            "src": "0x" + lg["topics"][1][-40:],
            "dst": "0x" + lg["topics"][2][-40:],
            "value": str(int(lg["data"], 16)) if lg["data"] not in ("0x", "") else "0",
        })
    df = pd.DataFrame(rows).sort_values(["block", "log_index"])
    buf = io.BytesIO()
    with gzip.open(buf, "wt") as gz:
        df.to_csv(gz, index=False)
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())
    print(f"  {chain} {addr}: {len(df)} transfers, dep_block={dep}, "
          f"{time.time()-t0:.0f}s", flush=True)
    return out_path


if __name__ == "__main__":
    chain, addr = sys.argv[1], sys.argv[2]
    pull_token(chain, addr)
