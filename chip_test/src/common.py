"""Shared HTTP utilities: disk cache, global rate limiter, retries."""
import hashlib
import json
import os
import threading
import time

import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "out")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

_session_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_session_local, "s"):
        _session_local.s = requests.Session()
        _session_local.s.headers["User-Agent"] = "chip-test-research/0.1"
    return _session_local.s


class RateLimiter:
    """Token bucket, shared across threads."""

    def __init__(self, rate_per_sec: float, burst: int = 10):
        self.rate = rate_per_sec
        self.burst = burst
        self.tokens = float(burst)
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


def cache_path(namespace: str, key: str) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()[:24]
    d = os.path.join(DATA_DIR, "http_cache", namespace)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, h + ".json")


def get_json(url: str, params=None, limiter: RateLimiter | None = None,
             namespace: str = "default", use_cache: bool = True,
             method: str = "GET", body=None, timeout: int = 30,
             max_retries: int = 6):
    key = json.dumps({"u": url, "p": params, "b": body}, sort_keys=True)
    cp = cache_path(namespace, key)
    if use_cache and os.path.exists(cp):
        with open(cp) as f:
            return json.load(f)
    last_err = None
    for attempt in range(max_retries):
        if limiter:
            limiter.acquire()
        try:
            if method == "GET":
                r = _session().get(url, params=params, timeout=timeout)
            else:
                r = _session().post(url, params=params, json=body, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"http {r.status_code}"
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code != 200:
                last_err = f"http {r.status_code}: {r.text[:200]}"
                break
            data = r.json()
            # JSON-RPC error => retryable (public nodes flake)
            if isinstance(data, dict) and data.get("error"):
                last_err = f"rpc error: {data['error']}"
                time.sleep(min(2 ** attempt, 30))
                continue
            if use_cache:
                tmp = cp + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, cp)
            return data
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"request failed: {url} {params} :: {last_err}")
