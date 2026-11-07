"""Export captured data to CSV files that open in Excel.

    python -m solpaper.export                 # writes readings.csv and pairs.csv here
    python -m solpaper.export --out exports   # into a folder

Files are UTF-8 with a byte-order mark, so Excel shows emoji and non-English
coin names correctly (Windows' default encoding can't store them).
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .config import dex_label, load_config
from .db import connect

QUERIES = {
    "readings.csv": "SELECT p.name, p.dex, s.* FROM pairs_seen s "
                    "JOIN pairs p USING (pair_address) ORDER BY s.id",
    "pairs.csv": "SELECT * FROM pairs ORDER BY first_seen_at",
}


def export(db_path: str, out_dir: Path, cfg: dict | None = None) -> dict[str, int]:
    cfg = cfg or load_config()
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    counts = {}
    for name, sql in QUERIES.items():
        cur = conn.execute(sql)
        with open(out_dir / name, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            cols = [d[0] for d in cur.description]
            w.writerow(cols)
            dex_i = cols.index("dex") if "dex" in cols else None
            n = 0
            for row in cur:
                row = list(row)
                if dex_i is not None:
                    row[dex_i] = dex_label(cfg, row[dex_i])
                w.writerow(row)
                n += 1
        counts[name] = n
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export solpaper data to CSV.")
    ap.add_argument("--out", default=".", help="folder to write into (default: current)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    for name, n in export(load_config()["db_path"], out).items():
        print(f"{out / name}: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
