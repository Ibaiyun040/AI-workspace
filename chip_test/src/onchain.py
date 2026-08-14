"""On-chain transfer data acquisition.

ETH: full history via Blockscout keyless API (tokentx, cursor walk).
BSC: windowed scan [t_lo, t_hi] via NodeReal getLogs (50k range, gentle pace)
     + initial balances at window start via batched archive eth_call.

Output per token: data/onchain/{key}.transfers.csv.gz (ts,src,dst,value)
                  data/onchain/{key}.b0.json  {addr: raw_balance_str}, meta
key = {chain}_{addr} for full history, {chain}_{addr}_{b0block} for windowed.
"""
import gzip
import io
import json
import os
import sys
import time

import pandas as pd
import requests

from common import DATA_DIR, RateLimiter, get_json

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ONCHAIN_DIR = os.path.join(DATA_DIR, "onchain")
os.makedirs(ONCHAIN_DIR, exist_ok=True)

NODEREAL = "https://bsc-mainnet.nodereal.io/v1/64a9df0874fb4a93b9d0a3849de012d3"
BSC_LIGHT = ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org"]
BLOCKSCOUT = "https://eth.blockscout.com/api"
HYPERSYNC = {"BSC": "https://bsc.hypersync.xyz", "ETH": "https://eth.hypersync.xyz"}
_ENVIO_TOKEN_PATH = os.path.join(DATA_DIR, "envio.token")

NR_LIMITER = RateLimiter(0.5, 1)      # nodereal demo key is shared: be gentle
LIGHT_LIMITER = RateLimiter(5, 5)
BS_LIMITER = RateLimiter(4, 4)
ZERO = "0x0000000000000000000000000000000000000000"


# ---------------- BSC RPC helpers ----------------

def bsc_light(method, params, use_cache=True):
    last = None
    for url in BSC_LIGHT:
        try:
            d = get_json(url, None, LIGHT_LIMITER, "rpc_bsc_light",
                         use_cache=use_cache, method="POST",
                         body={"jsonrpc": "2.0", "id": 1,
                               "method": method, "params": params},
                         max_retries=3)
            return d["result"]
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"bsc light rpc failed: {last}")


def bsc_latest() -> int:
    return int(bsc_light("eth_blockNumber", [], use_cache=False), 16)


def bsc_block_ts(number: int) -> int:
    return int(bsc_light("eth_getBlockByNumber", [hex(number), False])["timestamp"], 16)


def bsc_block_at(ts: int, hi: int | None = None) -> int:
    hi = hi or bsc_latest()
    lo = 1
    while lo < hi:
        mid = (lo + hi) // 2
        if bsc_block_ts(mid) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def nodereal(method, params, timeout=25, max_tries=10):
    """Single call to NodeReal with patient throttle handling."""
    key = json.dumps({"m": method, "p": params}, sort_keys=True)
    from common import cache_path
    cp = cache_path("rpc_nodereal", key)
    if os.path.exists(cp):
        with open(cp) as f:
            return json.load(f)
    for attempt in range(max(max_tries, 15)):
        NR_LIMITER.acquire()
        try:
            r = requests.post(NODEREAL, json={"jsonrpc": "2.0", "id": 1,
                                              "method": method, "params": params},
                              timeout=timeout)
            d = r.json()
            if "error" in d:
                msg = str(d["error"])
                low = msg.lower()
                if "usage limit" in low or "-32005" in low and "logs count" not in low \
                        or "rate" in low or "limit exceed" in low:
                    time.sleep(min(20 + attempt * 20, 120))
                    continue
                raise RuntimeError(msg[:200])
            res = d["result"]
            tmp = cp + ".tmp"
            with open(tmp, "w") as f:
                json.dump(res, f)
            os.replace(tmp, cp)
            return res
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(3 + attempt * 2)
    raise RuntimeError(f"nodereal exhausted retries: {method}")


def nodereal_batch_calls(calls: list[dict], block: int, batch=100):
    """Batched eth_call at a historical block. calls: [{'to':..,'data':..}]"""
    out = []
    for i in range(0, len(calls), batch):
        chunk = calls[i:i + batch]
        key = json.dumps({"b": block, "c": chunk}, sort_keys=True)
        from common import cache_path
        cp = cache_path("rpc_nodereal_batch", key)
        if os.path.exists(cp):
            with open(cp) as f:
                out.extend(json.load(f))
            continue
        payload = [{"jsonrpc": "2.0", "id": j,
                    "method": "eth_call", "params": [c, hex(block)]}
                   for j, c in enumerate(chunk)]
        for attempt in range(10):
            NR_LIMITER.acquire()
            try:
                r = requests.post(NODEREAL, json=payload, timeout=30)
                arr = r.json()
                if isinstance(arr, dict):  # error object
                    time.sleep(3 + attempt * 2)
                    continue
                res = [None] * len(chunk)
                ok = True
                for item in arr:
                    if "result" not in item:
                        ok = False
                        break
                    res[item["id"]] = item["result"]
                if not ok:
                    time.sleep(3 + attempt * 2)
                    continue
                with open(cp + ".tmp", "w") as f:
                    json.dump(res, f)
                os.replace(cp + ".tmp", cp)
                out.extend(res)
                break
            except (requests.RequestException, json.JSONDecodeError):
                time.sleep(3 + attempt * 2)
        else:
            raise RuntimeError("nodereal batch exhausted retries")
    return out


LOG_BUDGET = 1_500_000  # skip mega-tokens (bad controls, quota burners)


def bsc_get_logs(addr, from_block, to_block, progress=True):
    logs = []
    cur = from_block
    chunk = 50000
    t0 = time.time()
    n_req = 0
    while cur <= to_block:
        if len(logs) > LOG_BUDGET:
            raise RuntimeError(f"log budget exceeded ({len(logs)}), token too heavy")
        end = min(cur + chunk - 1, to_block)
        try:
            res = nodereal("eth_getLogs", [{
                "address": addr, "topics": [TRANSFER_TOPIC],
                "fromBlock": hex(cur), "toBlock": hex(end)}])
        except RuntimeError as e:
            msg = str(e)
            retryable = ("block range" in msg or "logs count" in msg
                         or "response size" in msg or "timeout" in msg.lower())
            if retryable and chunk > 200:
                chunk = max(200, chunk // 4)
                continue
            raise
        logs.extend(res)
        n_req += 1
        cur = end + 1
        if progress and n_req % 40 == 0:
            pct = 100 * (cur - from_block) / max(to_block - from_block, 1)
            print(f"    bsc logs {pct:.0f}% ({len(logs)} logs, "
                  f"{time.time()-t0:.0f}s)", flush=True)
    return logs


def bsc_windowed_pull(addr: str, t_lo: int, t_hi: int) -> str:
    """Windowed transfers + initial balances snapshot at window start."""
    addr = addr.lower()
    head = bsc_latest()
    b0 = bsc_block_at(t_lo, hi=head)
    key = f"BSC_{addr}_{b0 // 10000}"
    tr_path = os.path.join(ONCHAIN_DIR, key + ".transfers.csv.gz")
    b0_path = os.path.join(ONCHAIN_DIR, key + ".b0.json")
    if os.path.exists(tr_path) and os.path.exists(b0_path):
        return key
    b1 = min(head, bsc_block_at(t_hi, hi=head) + 1000)
    print(f"  BSC {addr}: blocks {b0}..{b1} ({(b1-b0)/1e6:.1f}M)", flush=True)
    logs = bsc_get_logs(addr, b0, b1)

    # sparse ts anchors for interpolation
    n_anchor = max(30, (b1 - b0) // 200_000)
    step = max(1, (b1 - b0) // n_anchor)
    anchors = [(b, bsc_block_ts(b)) for b in range(b0, b1 + 1, step)]
    if anchors[-1][0] != b1:
        anchors.append((b1, bsc_block_ts(b1)))

    import bisect
    ab = [a[0] for a in anchors]

    def its(block):
        i = bisect.bisect_left(ab, block)
        if i == 0:
            return anchors[0][1]
        if i >= len(anchors):
            return anchors[-1][1]
        (x0, y0), (x1, y1) = anchors[i - 1], anchors[i]
        return int(y0 + (y1 - y0) * (block - x0) / max(x1 - x0, 1))

    rows = []
    addrs = set()
    for lg in logs:
        if len(lg["topics"]) < 3 or lg["data"] in ("0x", ""):
            continue
        blk = int(lg["blockNumber"], 16)
        s = "0x" + lg["topics"][1][-40:]
        d = "0x" + lg["topics"][2][-40:]
        rows.append({"block": blk, "log_index": int(lg["logIndex"], 16),
                     "ts": its(blk), "src": s, "dst": d,
                     "value": str(int(lg["data"], 16))})
        addrs.add(s)
        addrs.add(d)
    addrs.discard(ZERO)
    df = pd.DataFrame(rows).sort_values(["block", "log_index"])

    # initial balances at b0-1 for every address seen in window
    alist = sorted(addrs)
    calls = [{"to": addr, "data": "0x70a08231" + a[2:].rjust(64, "0")}
             for a in alist]
    print(f"    b0 balances for {len(alist)} addrs "
          f"({(len(alist)+99)//100} batches)", flush=True)
    res = nodereal_batch_calls(calls, b0 - 1)
    b0bal = {}
    for a, r in zip(alist, res):
        v = int(r, 16) if r and r not in ("0x",) else 0
        if v > 0:
            b0bal[a] = str(v)
    # supply at b0-1
    try:
        s0 = nodereal("eth_call", [{"to": addr, "data": "0x18160ddd"}, hex(b0 - 1)])
        s0 = int(s0, 16) if s0 and s0 != "0x" else None
    except RuntimeError:
        s0 = None

    buf = io.BytesIO()
    with gzip.open(buf, "wt") as gz:
        df.to_csv(gz, index=False)
    with open(tr_path, "wb") as f:
        f.write(buf.getvalue())
    with open(b0_path, "w") as f:
        json.dump({"b0_block": b0, "b0_ts": t_lo, "supply0": str(s0 or 0),
                   "balances": b0bal}, f)
    print(f"    saved {len(df)} transfers, {len(b0bal)} initial holders", flush=True)
    return key


# ---------------- HyperSync (full history, any supported chain) ----------------

def hypersync_full_pull(chain: str, addr: str) -> str:
    """Full-history transfers via Envio HyperSync. Exact block timestamps."""
    addr = addr.lower()
    key = f"{chain}_{addr}"
    tr_path = os.path.join(ONCHAIN_DIR, key + ".transfers.csv.gz")
    b0_path = os.path.join(ONCHAIN_DIR, key + ".b0.json")
    if os.path.exists(tr_path) and os.path.exists(b0_path):
        return key
    with open(_ENVIO_TOKEN_PATH) as f:
        token = f.read().strip()
    url = HYPERSYNC[chain] + "/query"
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    from_block = 0
    logs = []
    block_ts_map = {}
    t0 = time.time()
    n_page = 0
    while True:
        body = {"from_block": from_block,
                "logs": [{"address": [addr],
                          "topics": [[TRANSFER_TOPIC]]}],
                "field_selection": {
                    "log": ["block_number", "log_index", "data",
                            "topic1", "topic2"],
                    "block": ["number", "timestamp"]}}
        for attempt in range(8):
            try:
                r = requests.post(url, json=body, headers=headers, timeout=60)
                d = r.json()
                if "error" in d:
                    raise RuntimeError(str(d["error"])[:200])
                break
            except (requests.RequestException, json.JSONDecodeError) as e:
                if attempt == 7:
                    raise RuntimeError(f"hypersync failed: {e}")
                time.sleep(2 + attempt * 3)
        for blk in d.get("data", []):
            for b in blk.get("blocks", []):
                block_ts_map[b["number"]] = int(b["timestamp"], 16)
            for lg in blk.get("logs", []):
                logs.append(lg)
        n_page += 1
        nxt = d.get("next_block")
        arch = d.get("archive_height") or 0
        if n_page % 20 == 0:
            print(f"    hypersync {chain} page {n_page}: block {nxt}/{arch}, "
                  f"{len(logs)} logs ({time.time()-t0:.0f}s)", flush=True)
        if not nxt or nxt >= arch:
            break
        from_block = nxt
    if not logs:
        raise RuntimeError(f"no transfers via hypersync for {addr} on {chain}")
    rows = []
    for lg in logs:
        t1, t2 = lg.get("topic1"), lg.get("topic2")
        if not t1 or not t2 or not lg.get("data"):
            continue
        blk = lg["block_number"]
        rows.append({"block": blk, "log_index": lg["log_index"],
                     "ts": block_ts_map.get(blk, 0),
                     "src": "0x" + t1[-40:], "dst": "0x" + t2[-40:],
                     "value": str(int(lg["data"], 16))})
    df = (pd.DataFrame(rows)
          .sort_values(["block", "log_index"])
          .reset_index(drop=True))
    buf = io.BytesIO()
    with gzip.open(buf, "wt") as gz:
        df.to_csv(gz, index=False)
    with open(tr_path, "wb") as f:
        f.write(buf.getvalue())
    with open(b0_path, "w") as f:
        json.dump({"b0_block": 0, "b0_ts": int(df["ts"].iloc[0]),
                   "supply0": "0", "balances": {}}, f)
    print(f"    saved {len(df)} transfers via hypersync "
          f"({n_page} pages, {time.time()-t0:.0f}s)", flush=True)
    return key


# ---------------- ETH via Blockscout ----------------

def eth_full_pull(addr: str) -> str:
    addr = addr.lower()
    key = f"ETH_{addr}"
    tr_path = os.path.join(ONCHAIN_DIR, key + ".transfers.csv.gz")
    b0_path = os.path.join(ONCHAIN_DIR, key + ".b0.json")
    if os.path.exists(tr_path) and os.path.exists(b0_path):
        return key
    rows = []
    startblock = 0
    seen = set()
    t0 = time.time()
    while True:
        d = get_json(BLOCKSCOUT, {
            "module": "account", "action": "tokentx",
            "contractaddress": addr, "startblock": startblock,
            "endblock": 99999999, "sort": "asc",
            "page": 1, "offset": 1000},
            BS_LIMITER, "blockscout", use_cache=False)
        res = d.get("result")
        if not isinstance(res, list) or not res:
            break
        new = 0
        for r in res:
            uid = (r["hash"], r.get("logIndex", ""), r["from"], r["to"], r["value"])
            if uid in seen:
                continue
            seen.add(uid)
            new += 1
            rows.append({"block": int(r["blockNumber"]),
                         "log_index": int(r.get("logIndex") or 0),
                         "ts": int(r["timeStamp"]),
                         "src": r["from"].lower(), "dst": r["to"].lower(),
                         "value": r["value"]})
        last_block = int(res[-1]["blockNumber"])
        if len(res) < 1000:
            break
        if new == 0 and last_block == startblock:
            startblock = last_block + 1   # >1000 tx in one block: skip
        else:
            startblock = last_block       # re-fetch boundary block, dedup via seen
        if len(rows) % 20000 < 1000:
            print(f"    eth tokentx {len(rows)} rows ({time.time()-t0:.0f}s)",
                  flush=True)
        if len(rows) > 2_500_000:
            raise RuntimeError(f"{addr}: too many transfers for pilot")
    if not rows:
        raise RuntimeError(f"no transfers via blockscout for {addr}")
    df = (pd.DataFrame(rows)
          .sort_values(["block", "log_index"])
          .reset_index(drop=True))
    buf = io.BytesIO()
    with gzip.open(buf, "wt") as gz:
        df.to_csv(gz, index=False)
    with open(tr_path, "wb") as f:
        f.write(buf.getvalue())
    with open(b0_path, "w") as f:
        json.dump({"b0_block": 0, "b0_ts": int(df["ts"].iloc[0]),
                   "supply0": "0", "balances": {}}, f)
    print(f"    saved {len(df)} transfers (full history)", flush=True)
    return key


def load_token(key: str):
    tr_path = os.path.join(ONCHAIN_DIR, key + ".transfers.csv.gz")
    with gzip.open(tr_path, "rt") as f:
        df = pd.read_csv(f, dtype={"value": str})
    with open(os.path.join(ONCHAIN_DIR, key + ".b0.json")) as f:
        meta = json.load(f)
    return df, meta


if __name__ == "__main__":
    if sys.argv[1] == "BSC":
        bsc_windowed_pull(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
    else:
        eth_full_pull(sys.argv[2])
