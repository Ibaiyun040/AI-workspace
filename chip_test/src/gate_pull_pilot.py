"""Pull Gate 1h panels only for pilot contracts (fast rebuild after VM loss)."""
import json
import os
import time

from common import OUT_DIR
from gate_pull import GATE_DIR, build_panel


def main():
    with open(os.path.join(OUT_DIR, "pilot_units.json")) as f:
        units = json.load(f)
    contracts = sorted({u["contract"] for u in units})
    t_to = int(time.time()) // 3600 * 3600
    t_from = t_to - 179 * 86400
    import gzip
    import io
    for c in contracts:
        out_path = os.path.join(GATE_DIR, f"{c}.csv.gz")
        if os.path.exists(out_path):
            continue
        panel = build_panel(c, t_from, t_to)
        if panel is None or panel.empty:
            print(f"{c}: EMPTY", flush=True)
            continue
        buf = io.BytesIO()
        with gzip.open(buf, "wt") as gz:
            panel.to_csv(gz, index=False)
        with open(out_path, "wb") as f:
            f.write(buf.getvalue())
        print(f"{c}: {len(panel)} rows", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
